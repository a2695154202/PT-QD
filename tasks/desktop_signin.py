"""桌面模式签到骨架：用「正常浏览器 + AI 看屏幕识图点击」完成登录与签到。

**这套方法来自人人（Audiences）任务**——当年 Playwright/CDP 驱动的浏览器带自动化指纹，
Cloudflare 把人机验证卡得死死的，换成用户平时那个正常浏览器 + 像真人一样看屏幕点鼠标才通。

流程（每一步失败都有下一步兜底，不留死角）：

1. 先杀掉上一次遗留的、占着 profile 的浏览器（profile 被占用会导致注入/登录失败）
2. CookieCloud → 注入正常浏览器的 profile；没有就读 profile 里已有的 cookie
3. 带 cookie 请求一次签到页判断登录态（`ProfileBridge.check_login`，不开浏览器，几秒出结果）
4. 未登录 → **pydoll 登录**（直连 CDP、不设 navigator.webdriver，实测不触发 CF）
         → 失败退回**调试浏览器（Playwright）登录**（能处理图形验证码 / 两步验证）
5. 正常浏览器（**不带**调试端口，最大化 + 置顶）打开签到页
6. AI 看截图逐个动作操作，直到它自己 `finish`
7. 收尾关掉浏览器（`keep_normal_open: true` 时留着给人看）

子类只需要提供：
- `CHECKIN_URL` / `LOGIN_URL`（BaseTask 已有，域名会自动跟随 task.url）
- `DESKTOP_GOAL`：给 AI 看的目标说明（写清流程和判定文案）
- `DESKTOP_TITLE`：窗口标题关键字（可选）
- 自动化模式用的选择器与关键词（`desktop_mode: false` 时走 NexusPHP 那套）
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from core.browser import BrowserController
from core.browser_bridge import ProfileBridge, find_edge
from core.captcha import CaptchaSolver
from core.cf import CFHandler
from core.config import _as_bool
from core.desktop import DesktopController
from core.desktop_agent import DesktopAgent

from .base import TaskError, TaskResult


class DesktopSigninMixin:
    """正常浏览器 + AI 识图点击（人人任务当年那套，抽出来给多个站点复用）"""

    # ---- 子类覆盖 ----
    DESKTOP_GOAL = ""
    DESKTOP_TITLE = ""                       # 窗口标题关键字，留空则按「任意浏览器窗口」找
    CF_HINT = (
        "\n\n这个网页可能会遇到 Cloudflare 人机验证：如果需要点击或拖动才能过验证，"
        "请你完成它（点「请验证您是真人」的复选框，或按住滑块拖到缺口位置）；"
        "验证通过后页面会自动跳转，请继续完成签到。"
    )

    # 桌面模式由 AI 执行，只能从它给出的说明里判断是否属于「登录失败」
    LOGIN_FAIL_KEYWORDS = ("登录失败", "登录未成功", "登陆失败", "密码错误", "密码不正确",
                           "账号或密码", "用户名或密码", "账号不存在", "login failed",
                           "invalid password", "wrong password")

    # ---- 默认参数（都可用任务配置的 options 覆盖）----
    DEFAULT_PROFILE = "data/desktop_profile"
    DEFAULT_DEBUG_PORT = 9223
    DEFAULT_STARTUP_WAIT = 6.0
    DEFAULT_MAX_STEPS = 20
    DEFAULT_KEEP_OPEN = False

    # ---------- 模式选择 ----------
    @property
    def use_desktop(self) -> bool:
        """是否走桌面模式（默认开启）。

        任务配置里写 `desktop_mode: false` 可退回 Playwright 自动化，
        用于排查「到底是站点变了还是 AI/识图环节出问题」。
        """
        val = self.cfg.options.get("desktop_mode", True)
        return val is not False and str(val).lower() not in ("false", "0", "no", "off")

    def needs_browser(self) -> bool:
        """桌面模式不启动自动化浏览器（用用户自己的正常浏览器）"""
        return not self.use_desktop

    # ---------- 主流程 ----------
    async def run(self) -> TaskResult:
        # 登录失败次数用尽则直接跳过（跨天也不重置，只有登录成功才清零）
        blocked = self.login_lock_result()
        if blocked is not None:
            return blocked
        if not self.use_desktop:
            return await super().run()          # type: ignore[misc]
        return await self._run_desktop()

    async def _run_desktop(self) -> TaskResult:
        self.events.emit("task_started", task_id=self.id, task_name=self.name)
        self.log(f"===== 任务 [{self.name}] 开始（桌面模式：正常浏览器 + AI 识图点击）=====")
        if self.login_used():
            self.log(f"登录失败计数 {self.login_used()}/{self.login_limit}"
                     f"（剩余 {self.login_remaining()} 次机会，只有登录成功才清零）")

        ai = self.ctx.ai
        if not (ai and getattr(ai, "available", False)):
            msg = ("桌面模式需要 AI（识图点击依赖视觉模型）：请在「全局设置 → AI 模型」"
                   "配好接口并启用 AI")
            self.log(msg)
            self.events.emit("task_error", task_id=self.id, error=msg)
            return TaskResult(self.id, False, msg)

        edge = find_edge()
        if not edge:
            msg = ("找不到 Microsoft Edge（msedge.exe），桌面模式需要它打开正常浏览器；"
                   "请安装 Edge 或把本任务改成 desktop_mode: false")
            self.log(msg)
            self.events.emit("task_error", task_id=self.id, error=msg)
            return TaskResult(self.id, False, msg)

        opts = self.cfg.options
        try:
            debug_port = int(opts.get("desktop_debug_port") or self.DEFAULT_DEBUG_PORT)
        except (TypeError, ValueError):
            debug_port = self.DEFAULT_DEBUG_PORT
        bridge = ProfileBridge(
            profile_dir=(self.opt_str("desktop_profile") or self.DEFAULT_PROFILE),
            port=debug_port,
            events=self.events,
        )
        title_kw = (self.opt_str("desktop_window_title") or self.DESKTOP_TITLE).strip()
        # 统一用 _as_bool：手写 YAML 里的 "false"/"no"/"off" 不能被 bool() 当成 True
        maximized = _as_bool(opts.get("normal_maximized"), True)
        topmost = _as_bool(opts.get("normal_topmost"), True)
        keep_open = _as_bool(opts.get("keep_normal_open"), self.DEFAULT_KEEP_OPEN)
        try:
            startup_wait = float(opts.get("desktop_startup_wait") or self.DEFAULT_STARTUP_WAIT)
        except (TypeError, ValueError):
            startup_wait = self.DEFAULT_STARTUP_WAIT
        try:
            max_steps = max(1, int(opts.get("desktop_max_steps", self.DEFAULT_MAX_STEPS)
                                   or self.DEFAULT_MAX_STEPS))
        except (TypeError, ValueError):
            max_steps = self.DEFAULT_MAX_STEPS

        desktop = DesktopController()
        agent = DesktopAgent(ai, desktop)
        goal = self.DESKTOP_GOAL + self.CF_HINT + self._cred_hint()

        try:
            # ---------- 0. 清理上次遗留的浏览器（profile 被占用会导致注入/登录失败）----------
            bridge.close_stale()

            # ---------- 1. CookieCloud → 正常浏览器 profile ----------
            cookies = await self._cookie_cloud_cookies()
            if cookies:
                await bridge.inject_cookies(cookies)
            else:
                self.log("没有 CookieCloud cookie，读取 profile 里已有的 cookie")
                try:
                    cookies = await bridge.read_profile_cookies()
                except Exception as e:
                    self.logger.warning(f"读取 profile cookie 失败: {e}")
                    cookies = []
                if cookies:
                    self.log(f"profile 里有 {len(cookies)} 个 cookie，先验证是否仍有效")

            # ---------- 2. 判定 cookie 是否有效 ----------
            checkin_url = await self.checkin_page_url()
            logged = False
            if cookies:
                logged, why = await asyncio.to_thread(
                    ProfileBridge.check_login, cookies, checkin_url)
                self.log(f"cookie 判定：{'有效' if logged else '无效'} —— {why}")
            else:
                self.log("没有可用 cookie → 直接走登录")

            # ---------- 3. 无效则用「不触发 CF 的方式」登录 ----------
            if not logged:
                # 3a) 优先 pydoll：直连 CDP、不设 navigator.webdriver，实测不触发 CF
                if _as_bool(opts.get("pydoll_login"), True):
                    logged = await self._login_with_pydoll(bridge)
                # 3b) 退回 Playwright 调试浏览器登录（能处理图证/2FA）
                if not logged:
                    self.log("改用调试浏览器（Playwright）登录")
                    try:
                        logged = await self._login_in_debug_browser(bridge)
                    except TaskError as e:
                        if self.login_locked():
                            msg = str(e)
                            self.log(msg)
                            self.events.emit("task_error", task_id=self.id, error=msg)
                            return TaskResult(self.id, False, msg)
                        self.log(f"调试浏览器登录未成功：{e}")
                        logged = False
                if logged:
                    self.log("登录成功，cookie 已落盘到正常浏览器的 profile")
                    await asyncio.sleep(1.5)      # 等浏览器彻底退出、释放 profile
                else:
                    self.log("两条自动登录路径都没成功 → 改由 AI 在正常浏览器里完成登录 + 签到")

            # ---------- 4. 正常浏览器打开签到页（最大化 + 置顶）----------
            self.log("打开正常浏览器（无调试端口）执行签到")
            bridge.launch_normal(checkin_url, maximized=maximized, topmost=topmost,
                                 title_keyword=title_kw, wait=max(10.0, startup_wait + 8))
            await asyncio.sleep(startup_wait)      # 等页面渲染 + CF 初步判定

            # ---------- 5. AI 识图完成签到 ----------
            ok, message = await agent.drive(goal=goal, window_title=title_kw,
                                            max_steps=max_steps)
        except Exception as e:
            self.logger.exception(f"桌面模式执行异常: {e}")
            self.events.emit("task_error", task_id=self.id, error=str(e))
            msg = f"任务异常: {e}"
            hint = self.net_error_hint(e)
            if hint:
                msg += f"\n{hint}"
            return TaskResult(self.id, False, msg)
        finally:
            # 跑完就关掉本程序打开的浏览器（含 AI 识图时新开的标签页/窗口）；
            # keep_normal_open=true 时保留，供人工查看签到结果
            if not keep_open:
                try:
                    bridge.close_normal()
                    bridge.close_stale()
                except Exception as e:
                    self.logger.warning(f"关闭浏览器失败（不影响签到结果）: {e}")

        # 归一化消息（别把规则说明里的常驻词当成签到成功）
        message = self._normalize_message(message)
        if ok:
            self.note_login_success()              # 签到成功说明登录态有效 → 清零失败计数
        elif self._looks_like_login_failure(message):
            self.note_login_failure(message[:80])  # AI 明确报告登录失败 → 计入跨天累计
        self.events.emit("task_finished", task_id=self.id, success=ok)
        self.log(f"===== 任务 [{self.name}] 结束: {message} =====")
        return TaskResult(self.id, ok, message)

    # ---------- 辅助 ----------
    def _cred_hint(self) -> str:
        """把账号密码（以及 2FA 动态码）拼进给 AI 的说明（没有就返回空串）"""
        if not (self.cfg.username and self.cfg.password):
            return ""
        extra = ""
        if self.cfg.has_2fa:
            code, left = "", 0
            try:
                if self.ctx.totp:
                    code = self.ctx.totp.now_code()
                    left = self.ctx.totp.remaining()
            except Exception:
                code = ""
            if code:
                extra = (f"\n两步验证码：{code}（约 {left} 秒内有效；"
                         "若页面提示验证码错误，就等半分钟后再试一次）")
            else:
                extra = "\n本账号开了两步验证，但程序没取到动态码——如需填写请报告登录失败"
        return (f"\n\n账号信息（仅在需要登录时使用）：\n"
                f"用户名：{self.cfg.username}\n密码：{self.cfg.password}{extra}")

    async def _cookie_cloud_cookies(self) -> list[dict]:
        """从 CookieCloud 取本站 cookie（未启用/暂停同步时返回空列表）"""
        cc = self.ctx.cookiecloud
        if not cc or not getattr(cc.cfg, "enabled", False):
            self.log("CookieCloud 未启用，跳过同步")
            return []
        if getattr(cc.cfg, "mode", "overwrite") == "paused":
            self.log("CookieCloud 工作模式为「暂停同步」，跳过")
            return []
        try:
            cookies = cc.get_cookies(domains=[self.site_domain]) or []
        except Exception as e:
            self.logger.warning(f"读取 CookieCloud cookie 失败: {e}")
            return []
        self.log(f"CookieCloud 取到本站 {len(cookies)} 个 cookie" if cookies
                 else "CookieCloud 中未找到本站 cookie")
        return cookies

    async def _login_in_debug_browser(self, bridge: ProfileBridge) -> bool:
        """用「开了调试端口的浏览器」登录；登录态随 profile 落盘给正常浏览器用。"""
        cfg = replace(
            self.ctx.app_cfg.browser,
            mode="launch",
            user_data_dir=bridge.profile_abs,
            debugging_port=bridge.port,
            headless=False,          # 有验证码/CF 时看得见更好排查
            fullscreen=False,
            always_on_top=False,
        )
        dbg = BrowserController(cfg, self.events)
        old = (self.ctx.browser, self.ctx.captcha, self.ctx.cf)
        try:
            await dbg.start()
            self.ctx.browser = dbg
            self.ctx.captcha = CaptchaSolver(self.ctx.ai, dbg, self.events)
            self.ctx.cf = CFHandler(dbg, self.ctx.ai, self.events)
            self.log("调试浏览器已就绪，开始自动登录")
            ok = await self.login_with_password()
            if ok:
                try:
                    n = len(await dbg.context.cookies())
                    self.log(f"调试浏览器当前有 {n} 个 cookie，关闭后写入 profile")
                except Exception:
                    pass
            return ok
        finally:
            try:
                await dbg.stop()          # 关闭时 cookie 落盘到 profile
            except Exception:
                pass
            (self.ctx.browser, self.ctx.captcha, self.ctx.cf) = old

    async def _login_with_pydoll(self, bridge: ProfileBridge) -> bool:
        """用 pydoll 登录（无 WebDriver 指纹，实测不触发 CF）"""
        if not (self.cfg.username and self.cfg.password):
            self.log("没有填写账号密码，pydoll 登录跳过")
            return False
        try:
            from core.pydoll_login import pydoll_login
        except Exception as e:                     # pydoll 未安装时优雅降级
            self.logger.warning(f"pydoll 不可用，跳过：{e}")
            return False

        ai = self.ctx.ai
        if not (ai and getattr(ai, "available", False)):
            self.log("AI 不可用，无法识别验证码，跳过 pydoll 登录")
            return False

        # 站点登录页没有图形验证码时传 None，免得白等一轮「未找到验证码图片」
        recognize = None
        if getattr(self, "SEL_CAPTCHA_IMG", ""):
            async def recognize(png: bytes) -> str:      # noqa: F811
                return ai.recognize_captcha(png)

        totp = ""
        if self.cfg.has_2fa:
            try:
                totp = self.ctx.totp.now_code()
            except Exception as e:
                self.logger.warning(f"取 2FA 验证码失败: {e}")

        login_url = await self.login_page_url()
        self.log("用 pydoll 打开登录页（无 WebDriver 指纹，不触发 CF）")
        try:
            ok, msg, cookies = await pydoll_login(
                bridge.profile_abs, login_url,
                self.cfg.username, self.cfg.password, recognize,
                totp_code=totp,
                onecode_names=self.pydoll_onecode_names(),
                headless=not _as_bool(self.cfg.options.get("pydoll_headed"), True),
            )
        except Exception as e:
            self.logger.warning(f"pydoll 登录异常: {e}")
            return False
        if not ok:
            self.log(f"pydoll 登录未成功：{msg}")
            return False
        self.log(f"pydoll {msg}")
        if cookies:
            await bridge.inject_cookies(cookies)   # 落盘到 profile（并补 expires）
        self.note_login_success()
        return True

    def pydoll_onecode_names(self) -> tuple[str, ...]:
        """登录表单里两步验证字段的名字候选（NexusPHP 各站命名不统一）"""
        names = ["scode", "2fa_code", "oneCode", "two_step_code", "2fa", "code"]
        own = getattr(self, "SEL_ONECODE", "") or ""
        if "name='" in own:
            name = own.split("name='", 1)[1].split("'", 1)[0]
            if name:
                names.remove(name) if name in names else None
                names.insert(0, name)
        return tuple(names)

    # ---------- 结果归一化 ----------
    def _looks_like_login_failure(self, message: str) -> bool:
        text = (message or "").lower()
        return any(kw.lower() in text for kw in self.LOGIN_FAIL_KEYWORDS)

    def _normalize_message(self, message: str) -> str:
        """把 AI 给的结果说明归一化，避免把规则文案当成签到结果"""
        text = (message or "").strip()
        if not text:
            return "未取到结果说明"
        for kw in getattr(self, "ALREADY_KEYWORDS", ()):
            if kw in text:
                return "今日已签到（无需重复）"
        for kw in getattr(self, "SUCCESS_KEYWORDS", ()):
            if kw in text:
                return "签到成功"
        return text[:80]
