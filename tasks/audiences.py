"""人人（Audiences / site-e.example）签到任务。

**执行模式（默认桌面模式）**：
- **桌面模式**（`options.desktop_mode: true`，默认）：
  用用户**平时那个正常浏览器**打开站点，再由 AI **看屏幕截图识图点击**完成全流程。
  这样 CF 看到的是普通用户的浏览器与鼠标行为，不会触发自动化拦截。
- **自动化模式**（`options.desktop_mode: false`）：
  退回 Playwright/CDP 自动化（有 CF 时较容易被拦）。

其它任务（PT-空 / HDDolby / TTG / 馒头）**不受影响**，仍走自动化流程。

---

站点细节（自动化模式用）：
NexusPHP（BlasphemyOrange 皮肤），登录页 https://site-e.example/index.php
- form[action='takelogin.php']，字段：username / password / scode（两步验证码）/ imagestring + imagehash
- 有图形验证码（image.php?action=regimage），需要 AI 识别
- 站点有 Cloudflare 保护
- 该账号未开启两步验证，因此 scode 留空即可

签到页 attendance.php 使用 **Cloudflare Turnstile** 人机验证（没有提交按钮）：
  <form id="attendance-form" action="attendance.php" method="post">
    <input type="hidden" name="cf-token" id="cf-token" value="">
    <div class="cf-turnstile" data-sitekey="..." data-callback="cfCallback"></div>
  </form>
  cfCallback(token) 里直接 attendance-form.submit()

注意：签到页规则区常驻「首次签到获得 20 粒爆米花」等文本，
因此不能用「获得 / 爆米花 / 连续签到」判断成功，只认结果页专有文案。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from core.browser import BrowserController
from core.browser_bridge import ProfileBridge
from core.captcha import CaptchaSolver
from core.cf import CFHandler
from core.desktop import DesktopController
from core.desktop_agent import DesktopAgent
from core.slider import SliderSolver

from .base import TaskError, TaskResult, register
from .nexusphp import NexusPHPTask


@register
class AudiencesTask(NexusPHPTask):
    id = "audiences"
    name = "人人（Audiences）"
    description = "Audiences(site-e.example) 每日签到：NexusPHP + 图形验证码 + Turnstile 人机验证"
    site = "https://site-e.example/index.php"

    # 两步验证码字段名为 scode（未开启 2FA 时留空）
    SEL_ONECODE = "form[action='takelogin.php'] input[name='scode']"

    # 登录后页面标志
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
        "a[href*='attendance.php']",
    )

    # 签到页
    CHECKIN_URL = "https://site-e.example/index.php"

    # Turnstile 相关选择器
    SEL_TURNSTILE = ".cf-turnstile, iframe[src*='challenges.cloudflare.com']"
    SEL_VERIFY_CARD = ".attendance-card--verify"

    # 注意：签到页规则区常驻「首次签到获得 20 粒爆米花」等文本，
    # 因此不能用「获得」「爆米花」「连续签到」判断成功，只认结果页专有文案
    ALREADY_KEYWORDS = ("今日已签到", "已经签到", "今日已经签到", "明日再来")
    SUCCESS_KEYWORDS = ("签到成功", "签到完成", "明天再来", "今日已签到", "已经签到")

    DEFAULT_TURNSTILE_TIMEOUT = 40

    # Turnstile 专用 AI 提示词：只截验证控件区域，AI 返回控件内相对坐标
    TURNSTILE_PROMPT = (
        "这是 Cloudflare Turnstile 人机验证控件的截图。"
        "如果图中存在一个尚未勾选的复选框（通常位于控件左侧），"
        "请返回该复选框在图片中的像素坐标，格式为 JSON：{\"click\": [x, y]}；"
        "如果已经显示勾选、正在验证中、或没有可点击的复选框，请返回 {\"click\": null}。"
        "只输出 JSON，不要任何解释。"
    )

    # 桌面模式给 AI 的 CF 处理说明（正常浏览器没有调试端口，只能靠识图操作）
    CF_HINT = (
        "\n\n现在这个网页可能会遇到 CF 拦截：如果拦截时需要点击或拖动才能过验证，"
        "请你进行相关操作（点「请验证您是真人」的复选框，或按住滑块拖到缺口位置）；"
        "验证通过后页面会自动跳转，请继续完成签到。"
    )

    # ---------- 桌面模式（默认）：正常浏览器 + 识图点击 ----------
    @property
    def use_desktop(self) -> bool:
        """是否走桌面模式（正常浏览器 + 识图点击）。

        默认开启；如需退回自动化模式，任务配置里写 `desktop_mode: false`。
        """
        val = self.cfg.options.get("desktop_mode", True)
        return val is not False and str(val).lower() not in ("false", "0", "no", "off")

    def needs_browser(self) -> bool:
        """桌面模式不需要启动 Playwright 浏览器（用用户自己的正常浏览器）"""
        return not self.use_desktop

    DESKTOP_GOAL = (
        "在人人（site-e.example）网站完成今日每日签到。\n"
        "流程：\n"
        "1. 打开 https://site-e.example/index.php （签到页）\n"
        "   - 如果页面是登录页（有用户名/密码输入框），先用下面的账号登录\n"
        "   - 如果遇到 Cloudflare 人机验证（「请稍候」/「正在进行安全验证」），"
        "等它自动通过，或点复选框/拖滑块通过\n"
        "2. 签到页有一个人机验证（Cloudflare Turnstile），通过后表单会自动提交\n"
        "3. 看到「签到成功」「今日已签到」「明天再来」即为完成\n\n"
        "判定标准：\n"
        "- 看到「签到成功」→ finish(passed=true, message=\"签到成功\")\n"
        "- 看到「今日已签到」「已经签到」→ finish(passed=true, message=\"今日已签到（无需重复）\")\n"
        "- 页面出现明确失败提示 → finish(passed=false, message=失败原因)\n"
        "注意：页面里的「首次签到获得 20 粒爆米花」「连续签到加成」是常驻规则说明，"
        "不代表签到成功，不要据此判定。"
    )

    async def run(self) -> TaskResult:
        """桌面模式：用正常浏览器 + AI 识图点击完成签到"""
        # 登录失败次数用尽则直接跳过（跨天也不重置，只有登录成功才清零）
        blocked = self.login_lock_result()
        if blocked is not None:
            return blocked
        if not self.use_desktop:
            return await super().run()
        return await self._run_desktop()

    async def _run_desktop(self) -> TaskResult:
        """桌面模式：CookieCloud → 正常浏览器（无调试端口）签到；cookie 无效则用调试浏览器登录。

        流程：
        1. 从 CookieCloud 取本站 cookie，写入「正常浏览器」的 profile
        2. 判定这些 cookie 是否还有效（带 cookie 请求签到页看是否被踢到登录页）
        3. 有效 → 直接打开正常浏览器签到
           无效 → 用「开了调试端口的 Edge」打开登录页自动登录（识别验证码/2FA），
                  cookie 落盘到同一 profile，再打开正常浏览器签到
        4. 正常浏览器最大化 + 置顶，由 AI 识图通过 Turnstile 并点签到
        """
        self.events.emit("task_started", task_id=self.id, task_name=self.name)
        self.log(f"===== 任务 [{self.name}] 开始（桌面模式：正常浏览器签到 + 调试浏览器登录）=====")
        if self.login_used():
            self.log(f"登录失败计数 {self.login_used()}/{self.login_limit}"
                     f"（剩余 {self.login_remaining()} 次机会，只有登录成功才清零）")

        if not (self.ctx.ai and getattr(self.ctx.ai, "available", False)):
            msg = "桌面模式需要配置 AI（识图点击依赖视觉模型），请先在全局设置里配置 AI 接口"
            self.log(msg)
            self.events.emit("task_error", task_id=self.id, error=msg)
            return TaskResult(self.id, False, msg)

        opts = self.cfg.options
        bridge = ProfileBridge(
            profile_dir=(opts.get("desktop_profile") or "data/desktop_profile"),
            port=int(opts.get("desktop_debug_port") or 9223),
            events=self.events,
        )
        title_kw = (opts.get("desktop_window_title") or "Audiences").strip()
        maximized = bool(opts.get("normal_maximized", True))
        topmost = bool(opts.get("normal_topmost", True))
        keep_open = bool(opts.get("keep_normal_open", False))
        try:
            startup_wait = float(opts.get("desktop_startup_wait") or 6)
        except (TypeError, ValueError):
            startup_wait = 6.0

        desktop = DesktopController()
        agent = DesktopAgent(self.ctx.ai, desktop)
        cred = ""
        if self.cfg.username and self.cfg.password:
            cred = (f"\n\n账号信息（仅在需要登录时使用）：\n"
                    f"用户名：{self.cfg.username}\n密码：{self.cfg.password}\n"
                    f"（若页面有两步验证码输入框，留空即可，该账号未开启 2FA）")
        goal = self.DESKTOP_GOAL + self.CF_HINT + cred

        try:
            # ---------- 0. 清理上次遗留的浏览器（profile 被占用会导致注入/登录失败）----------
            bridge.close_stale()

            # ---------- 1. CookieCloud → 正常浏览器 profile ----------
            cookies = await self._cookie_cloud_cookies()
            if cookies:
                await bridge.inject_cookies(cookies)
            else:
                self.log("没有 CookieCloud cookie，读取正常浏览器 profile 里已有的 cookie")
                try:
                    cookies = await bridge.read_profile_cookies()
                except Exception as e:
                    self.logger.warning(f"读取 profile cookie 失败: {e}")
                    cookies = []
                if cookies:
                    self.log(f"profile 里有 {len(cookies)} 个 cookie，先验证是否仍有效")

            # ---------- 2. 判定 cookie 是否有效 ----------
            logged = False
            if cookies:
                logged, why = await asyncio.to_thread(
                    ProfileBridge.check_login, cookies, self.CHECKIN_URL)
                self.log(f"cookie 判定：{'有效' if logged else '无效'} —— {why}")
            else:
                self.log("没有可用 cookie → 直接走登录")

            # ---------- 3. 无效则用调试浏览器登录 ----------
            if not logged:
                self.log("在「开了调试端口的浏览器」打开登录页登录（识别验证码/2FA）")
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
                    await asyncio.sleep(1.5)      # 等调试浏览器彻底退出、释放 profile
                else:
                    self.log("登录页可能被 CF 拦住（自动化浏览器过不去）→ "
                             "改在正常浏览器里由 AI 完成登录 + 签到")

            # ---------- 4. 正常浏览器打开签到页（最大化 + 置顶）----------
            self.log("打开正常浏览器（无调试端口）执行签到")
            bridge.launch_normal(self.CHECKIN_URL, maximized=maximized, topmost=topmost,
                                 title_keyword=title_kw, wait=max(10.0, startup_wait + 8))
            await asyncio.sleep(startup_wait)      # 等页面渲染 + CF 初步判定

            # ---------- 5. AI 识图完成签到 ----------
            ok, message = await agent.drive(
                goal=goal,
                window_title=title_kw,
                max_steps=int(opts.get("desktop_max_steps", 20)),
            )
        except Exception as e:
            self.logger.exception(f"桌面模式执行异常: {e}")
            self.events.emit("task_error", task_id=self.id, error=str(e))
            return TaskResult(self.id, False, f"任务异常: {e}")
        finally:
            if not keep_open:
                bridge.close_normal()

        # 归一化消息（不要把「获得/爆米花」这类常驻词当成成功）
        message = self._normalize_message(message)
        if ok:
            # 签到成功说明登录态有效 → 清零累计失败次数
            self.note_login_success()
        elif self._looks_like_login_failure(message):
            # AI 明确报告登录失败 → 计入跨天累计的登录失败次数
            self.note_login_failure(message[:80])
        self.events.emit("task_finished", task_id=self.id, success=ok)
        self.log(f"===== 任务 [{self.name}] 结束: {message} =====")
        return TaskResult(self.id, ok, message)

    # ---------- 桌面模式辅助 ----------
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
            cookies = cc.get_cookies(domains=[self._domain]) or []
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
        old = (self.ctx.browser, self.ctx.captcha, self.ctx.slider, self.ctx.cf)
        try:
            await dbg.start()
            self.ctx.browser = dbg
            self.ctx.captcha = CaptchaSolver(self.ctx.ai, dbg, self.events)
            self.ctx.slider = SliderSolver(self.ctx.ai, dbg)
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
            (self.ctx.browser, self.ctx.captcha, self.ctx.slider, self.ctx.cf) = old

    # 桌面模式由 AI 执行，只能从它给出的说明里判断是否属于「登录失败」
    LOGIN_FAIL_KEYWORDS = ("登录失败", "登录未成功", "登陆失败", "密码错误", "密码不正确",
                           "账号或密码", "用户名或密码", "账号不存在", "login failed",
                           "invalid password", "wrong password")

    def _looks_like_login_failure(self, message: str) -> bool:
        text = (message or "").lower()
        return any(kw.lower() in text for kw in self.LOGIN_FAIL_KEYWORDS)

    def _normalize_message(self, message: str) -> str:
        """把 AI 给的结果说明归一化，避免把规则文案当成签到结果"""
        text = (message or "").strip()
        if not text:
            return "未取到结果说明"
        for kw in self.ALREADY_KEYWORDS:
            if kw in text:
                return "今日已签到（无需重复）"
        for kw in self.SUCCESS_KEYWORDS:
            if kw in text:
                return "签到成功"
        return text[:80]

    # ---------- 自动化模式（desktop_mode: false 时用） ----------
    async def _checkin_finished(self) -> bool:
        """页面是否已显示签到结果（Turnstile 通过并提交后的页面）。

        先查验证卡片（快），未提交时直接短路，避免每秒解析整页文本。
        """
        page = self.ctx.browser.page
        try:
            if await page.query_selector(self.SEL_VERIFY_CARD):
                return False
            text = await self.page_text()
        except Exception:
            return False
        return self._has_kw(text, self.SUCCESS_KEYWORDS)

    async def _turnstile_ready(self) -> bool:
        """Turnstile 控件是否已加载完成（iframe 已渲染且有可见尺寸）"""
        page = self.ctx.browser.page
        try:
            iframe = page.locator("iframe[src*='challenges.cloudflare.com']").first
            if await iframe.count() == 0:
                return await page.query_selector(".cf-turnstile") is not None
            if not await iframe.is_visible():
                return False
            bbox = await iframe.bounding_box()
            return bool(bbox and bbox.get("width", 0) > 20 and bbox.get("height", 0) > 10)
        except Exception:
            return False

    async def solve_turnstile(self) -> bool:
        """通过 Turnstile：每 1 秒轮询一次，控件一加载完成就立即处理。

        具体处理逻辑复用 core.cf.CFHandler.solve_turnstile：
        跨域 iframe 内的元素点击 → 几何兜底 → AI 定位，控件出现即刻响应。
        """
        try:
            total = int(self.cfg.options.get("turnstile_timeout", self.DEFAULT_TURNSTILE_TIMEOUT) or 0)
        except (TypeError, ValueError):
            total = self.DEFAULT_TURNSTILE_TIMEOUT
        total = max(6, total)
        return await self.ctx.cf.solve_turnstile(
            timeout_s=total, is_done=self._checkin_finished
        )

    async def _click_turnstile(self) -> bool:
        """尝试点击 Turnstile 复选框（iframe 内优先，其次按坐标点击）"""
        page = self.ctx.browser.page
        try:
            iframe = page.locator("iframe[src*='challenges.cloudflare.com']").first
            if await iframe.count() == 0:
                return False
            # 1) 进入 iframe 找复选框
            try:
                frame = page.frame_locator("iframe[src*='challenges.cloudflare.com']")
                box = frame.locator("input[type='checkbox'], label.cb-lb, .cb-lb, #challenge-stage").first
                if await box.count() > 0:
                    await box.click(timeout=4000)
                    self.log("已点击 Turnstile 复选框（iframe 内）")
                    return True
            except Exception:
                pass
            # 2) 按坐标点击 iframe 左侧（复选框常见位置）
            bbox = await iframe.bounding_box()
            if bbox and bbox["width"] > 0:
                x = bbox["x"] + min(30, bbox["width"] / 2)
                y = bbox["y"] + bbox["height"] / 2
                await page.mouse.move(x, y, steps=8)
                await self.ctx.browser.wait_timeout(150)
                await page.mouse.click(x, y)
                self.log(f"已按坐标点击 Turnstile 验证框（{x:.0f}, {y:.0f}）")
                return True
        except Exception as e:
            self.logger.warning(f"点击 Turnstile 失败: {e}")
        return False

    async def do_checkin(self) -> tuple[bool, str]:
        """签到：进入 attendance.php，自动通过 Turnstile，等表单自动提交后的结果"""
        browser = self.ctx.browser
        url = (self.cfg.options.get("checkin_url") or self.CHECKIN_URL or self.cfg.url).strip()
        self.log(f"进入签到页: {url}")
        await browser.goto(url)
        await browser.wait_timeout(1500)
        page = browser.page
        has_turnstile = await page.query_selector(self.SEL_TURNSTILE)
        # 整页 CF 挑战（5 秒盾）交给通用处理；签到页的 Turnstile 由 solve_turnstile 专门处理，
        # 避免两套逻辑重复等待
        if self.cfg.has_cf and not has_turnstile:
            await self.handle_challenge_if_any()

        body = await self.page_text()
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"

        if not has_turnstile:
            # 没有 Turnstile：可能是结果页或页面结构变化
            if self._has_kw(body, self.SUCCESS_KEYWORDS):
                return True, "签到成功"
            self.log("未检测到 Turnstile 验证组件，页面片段: " + body[:200].replace("\n", " "))
            return False, "未检测到人机验证组件（页面结构可能变化）"

        if not await self.solve_turnstile():
            still = await page.query_selector(self.SEL_VERIFY_CARD)
            detail = "（验证卡片仍存在）" if still else ""
            self.log("Turnstile 未通过，页面片段: " + (await self.page_text())[:200].replace("\n", " "))
            return False, f"人机验证（Turnstile）未通过{detail}"

        # 验证通过后表单自动提交，等结果页渲染
        await browser.wait_timeout(2500)
        body = await self.page_text()
        if self._has_kw(body, self.SUCCESS_KEYWORDS):
            return True, "签到成功"
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if await page.query_selector(self.SEL_VERIFY_CARD):
            return False, "人机验证已通过但表单未提交（验证卡片仍存在）"
        return False, "签到结果未识别（请查看运行截图）"
