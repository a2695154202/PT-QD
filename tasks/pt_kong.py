"""PT-空（HDSky）签到任务

站点: https://site-a.example/index.php (NexusPHP)
流程:
1. 打开站点；仅当任务勾选了「有 CF 验证」时才处理人机挑战
2. 若落到登录页(login.php / form[action=takelogin.php]):
   a. 先用 CookieCloud 同步 cookie 并刷新，仍登录态失效再走登录
   b. 用任务配置的账号密码登录；任务勾选「有 2FA」时自动填 TOTP 验证码；
      图形验证码交给 AI 识别；失败自动刷新验证码重试
3. 点击导航栏「签到」(#showup) 弹出验证码层（#showupimg / #imagestring / #showupbutton）
4. AI 识别弹窗验证码并提交，完成签到
"""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PWTimeout

from core.config import TaskConfig
from core.events import Events
from core.totp import TwoFactorAuth
from .base import BaseTask, TaskContext, TaskError, register


@register
class PTKongTask(BaseTask):
    id = "pt_kong"
    name = "PT-空"
    description = "HDSky(site-a.example) 每日签到，支持 CookieCloud / 账号密码登录 / 2FA / AI 验证码"
    site = "https://site-a.example/index.php"

    # ---- 登录页选择器（依据登录页源码）----
    SEL_LOGIN_FORM = "form[action='takelogin.php']"
    SEL_USERNAME = "form[action='takelogin.php'] input[name='username']"
    SEL_PASSWORD = "form[action='takelogin.php'] input[name='password']"
    SEL_ONECODE = "form[action='takelogin.php'] input[name='oneCode']"
    SEL_CAPTCHA_IMG = "img[src*='image.php?action=regimage']"
    SEL_CAPTCHA_INPUT = "form[action='takelogin.php'] input[name='imagestring']"
    SEL_SUBMIT = "form[action='takelogin.php'] input[type='submit']"
    # ---- 签到相关（依据登录后源码 / 弹窗 DOM 截图）----
    SEL_SHOWUP = "#showup"
    SEL_POPUP_IMG = "#showupimg"            # 弹窗验证码图片
    SEL_POPUP_INPUT = ".layui-layer input#imagestring, #imagestring"
    SEL_POPUP_BTN = "#showupbutton"
    SEL_LAYER_TEXT = ".layui-layer-content"

    KW_SUCCESS = ["签到成功", "恭喜", "获得", "奖励"]
    KW_ALREADY = ["已经签到", "重复签到", "今日已签到", "已签到", "请勿重复"]
    KW_FAIL = ["错误", "失败", "不正确", "无效", "过期"]

    def __init__(self, cfg: TaskConfig, ctx: TaskContext):
        super().__init__(cfg, ctx)
        self._detail = ""
        self._domain = urlparse(self.cfg.url).hostname or "site-a.example"
        if self.cfg.has_2fa:
            self._setup_totp()

    def _setup_totp(self) -> None:
        """按优先级准备 TOTP：任务直填 secret -> 绑定的凭证库账号 -> 按站点域名匹配"""
        tfa = None
        if self.cfg.totp_secret:
            try:
                tfa = TwoFactorAuth(secret=self.cfg.totp_secret)
                self.log("2FA 使用任务配置中的 secret")
            except Exception as e:
                self.logger.warning(f"任务配置的 2FA secret 无效: {e}")
        if tfa is None:
            try:
                from core.twofa_store import TwoFactorVault
                vault = TwoFactorVault()
                acc = vault.find_by_key(self.cfg.vault_key) if self.cfg.vault_key else None
                if acc is None and not self.cfg.vault_key:
                    acc = vault.find_for_task(self.id, self._domain)
                if acc:
                    tfa = acc.tfa()
                    self.log(f"2FA 凭证来自凭证库: {acc.issuer or '(无issuer)'} / {acc.name}")
                elif self.cfg.vault_key:
                    self.logger.warning(f"任务绑定的 2FA 账号不存在: {self.cfg.vault_key}")
            except Exception as e:
                self.logger.warning(f"读取 2FA 凭证库失败: {e}")
        if tfa is not None:
            self.ctx.totp = tfa
            self.log(f"2FA 已启用: {tfa.describe()}")
        else:
            self.logger.warning("任务勾选了 2FA 但未找到可用密钥，本次将不填 oneCode")

    # ---------- 主流程 ----------
    # ---------- 登录态判定 ----------
    # HDSky 登录后导航栏才会出现：退出链接 / 签到入口 / 用户详情链接
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "#showup",
        "a[href*='userdetails.php']",
    )

    async def is_logged_in(self) -> bool:
        """是否处于已登录状态：看页面真实标志，不看 URL（登录后仍可能停在 login.php）"""
        page = self.ctx.browser.page
        for sel in self.LOGGED_IN_MARKERS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    async def has_login_form(self) -> bool:
        """页面是否存在登录表单"""
        try:
            return await self.ctx.browser.page.query_selector(self.SEL_LOGIN_FORM) is not None
        except Exception:
            return False

    async def is_login_page(self) -> bool:
        """真正的登录页 = 未登录 且 页面上有登录表单"""
        if await self.is_logged_in():
            return False
        return await self.has_login_form()

    # ---------- 主流程 ----------
    async def ensure_logged_in(self) -> bool:
        # 1) 先同步 CookieCloud 的 cookie（在打开站点之前注入到浏览器上下文）
        await self.prepare_cookies()

        # 2) 打开站点（此时请求已带上 cookie）
        await self.open_site()

        # 3) 判定登录态
        if await self.is_logged_in():
            self.log("已登录（CookieCloud cookie 有效）")
            return True

        if await self.has_login_form():
            self.log("检测到登录页，当前无有效登录态")
            self.events.emit(Events.LOGIN_PAGE_DETECTED, task_id=self.id)
            # 兜底：再同步一次并刷新（localStorage 等需要页面上下文才能写入）
            if await self.sync_cookies_and_reload():
                if await self.is_logged_in():
                    self.log("CookieCloud 同步后已恢复登录态")
                    return True
                self.log("CookieCloud 同步后仍未登录，转入账号密码登录")
            return await self.login_with_password()

        # 4) 既无登录标志也无表单：可能正在跳转，稍等后再判
        await self.ctx.browser.wait_timeout(1500)
        if await self.is_logged_in():
            self.log("已登录（跳转完成后确认）")
            return True
        if await self.has_login_form():
            if await self.sync_cookies_and_reload() and await self.is_logged_in():
                return True
            return await self.login_with_password()

        # 5) 仍未识别，重新打开站点再判一次
        self.log("页面状态未识别，重新打开站点后复核")
        await self.open_site()
        if await self.is_logged_in():
            return True
        if await self.has_login_form():
            if await self.sync_cookies_and_reload() and await self.is_logged_in():
                return True
            return await self.login_with_password()
        raise TaskError("无法识别页面状态（既非登录页也非已登录页），请查看截图确认站点是否可访问")

    async def do_checkin(self) -> tuple[bool, str]:
        browser = self.ctx.browser
        await self.open_site()

        if await self.has_login_form():
            return False, "签到前检测到仍处于未登录状态"

        try:
            # 不写死短超时：慢机器/CF 之后首屏渲染慢，统一用 default_timeout
            await browser.wait_selector(self.SEL_SHOWUP)
        except PWTimeout:
            # HDSky 签到成功后会把「签到」入口移除，此时页面显示「已签到」
            body = await self.page_text()
            if "已签到" in body or "已经签到" in body:
                return True, "今日已签到（无需重复）"
            if await self.is_logged_in():
                return True, "今日已签到（签到入口已消失）"
            return False, "未找到签到按钮（#showup），可能未登录或页面结构变化"

        # 签到成功后站点会把入口文字改成「已签到(Showed Up)」，此时不必再点
        if await self.showup_text_signed():
            return True, "今日已签到（无需重复）"

        # 等签到事件真正绑定好：只等 jQuery 加载完还不够，common.js 的
        # jQuery(function(){...}) 要等 DOM ready 才给 #showup 挂 click
        if not await self.wait_showup_ready():
            self.logger.warning("未能确认签到事件已绑定（jQuery/layer 可能未加载完），仍尝试点击")

        # 点击签到，弹窗未出现时重试；点击本身失败也要兜底，不能直接抛错
        popup_ok = False
        for attempt in range(1, 4):
            clicked = await self.click_showup(attempt)
            if clicked:
                self.events.emit(Events.CHECKIN_CLICKED, task_id=self.id)
                self.log(f"已点击签到按钮（第 {attempt} 次），等待验证码弹窗")
            try:
                await browser.wait_selector(self.SEL_POPUP_IMG, timeout=8000)
                popup_ok = True
                break
            except PWTimeout:
                self.log("验证码弹窗未出现")
                hint = await self.popup_hint()
                if hint:
                    self.log(f"弹窗提示: {hint}")
                    if "获取验证码失败" in hint:
                        return False, "站点返回「获取验证码失败」，本次签到未进行（可稍后重试）"
                if await self.showup_text_signed():
                    return True, "今日已签到（无需重复）"
                await browser.wait_timeout(1200)

        if not popup_ok:
            await self.dump_showup_diag()
            body = await self.page_text()
            if "已签到" in body or "已经签到" in body:
                return True, "今日已签到（无需重复）"
            return False, "点击签到后验证码弹窗未出现（可能今日已签到或页面结构变化）"

        # 等验证码图片真正加载出来（naturalWidth>0），否则 AI 拿到的可能是空白图
        try:
            await browser.page.wait_for_function(
                "() => { const i = document.querySelector('#showupimg');"
                " return !!(i && i.complete && i.naturalWidth > 0); }",
                timeout=10000,
            )
        except Exception:
            self.logger.warning("验证码图片加载确认超时，继续尝试识别")
        await browser.wait_timeout(600)  # 等验证码图片渲染完成

        # 识别弹窗验证码
        try:
            await self.ctx.captcha.solve_and_fill(
                self.SEL_POPUP_IMG, self.SEL_POPUP_INPUT, prompt_key="popup_captcha"
            )
        except Exception as e:
            return False, f"验证码识别失败: {e}"

        # 提交
        if not await self.submit_popup():
            return False, "无法点击「提交」按钮（验证码弹窗结构变化或被浮层遮挡）"
        self.log("已提交签到验证码，等待结果")

        status = await self.wait_checkin_result()
        return self._interpret_result(status)

    # ---------- 子步骤 ----------
    # ---------- 签到入口（#showup）辅助 ----------
    async def showup_text_signed(self) -> bool:
        """签到入口的文字是否已被站点改成「已签到(Showed Up)」"""
        try:
            txt = await self.ctx.browser.evaluate(
                "() => { const el = document.querySelector('#showup');"
                " return el ? (el.innerText || el.textContent || '') : ''; }"
            ) or ""
        except Exception:
            return False
        low = txt.lower()
        return "已签到" in txt or "已经签到" in txt or "showed up" in low

    async def wait_showup_ready(self, timeout_s: int = 15) -> bool:
        """等 common.js 给 #showup 绑定 click 处理函数。

        只等 `typeof jQuery !== 'undefined'` 是不够的：jQuery 加载完成时
        `jQuery(function(){...})` 里的绑定还没执行，此时点击没有任何反应。
        """
        js = (
            "() => {"
            "  try {"
            "    if (typeof jQuery === 'undefined') return false;"
            "    if (typeof layer === 'undefined') return false;"   # 弹窗由 layer.open 构建
            "    const el = document.querySelector('#showup');"
            "    if (!el) return false;"
            "    const ev = jQuery._data ? jQuery._data(el, 'events') : null;"
            "    if (!ev) return false;"
            "    return !!(ev.click && ev.click.length);"
            "  } catch (e) { return false; }"
            "}"
        )
        try:
            await self.ctx.browser.page.wait_for_function(js, timeout=timeout_s * 1000)
            return True
        except Exception:
            # 读不到事件表（jQuery 版本差异）不等于没绑定，只当作「无法确认」
            try:
                await self.ctx.browser.page.wait_for_function(
                    "() => typeof jQuery !== 'undefined'", timeout=3000
                )
            except Exception:
                pass
            return False

    async def click_showup(self, attempt: int = 1) -> bool:
        """分策略点击签到（每次重试用一种更强的兜底）：

        1 拟人点击（正常路径）  2 强制点击（绕过遮挡/可点击性检查）
        3 DOM click()（绕过浮层拦截，直接触发已绑定的处理器）
        站点把入口做成无 href 的 <a>，被浮层遮挡或事件尚未绑定时，
        Playwright 的 actionability 检查会一直重试到超时，这里逐级降级避免整任务失败。
        """
        browser = self.ctx.browser
        try:
            if attempt <= 1:
                await browser.human_click(self.SEL_SHOWUP)
            elif attempt == 2:
                locator = browser.page.locator(self.SEL_SHOWUP).first
                try:
                    await locator.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                await locator.click(force=True, timeout=5000)
            else:
                await browser.evaluate(
                    "() => { const el = document.querySelector('#showup'); if (el) el.click(); }"
                )
            return True
        except Exception as e:
            self.logger.warning(f"第 {attempt} 次点击签到未成功（{e}），下次换更强的方式")
            return False

    async def submit_popup(self) -> bool:
        """点「Let's Go」提交验证码；常规点击被遮挡时降级为 DOM click"""
        browser = self.ctx.browser
        try:
            await browser.click(self.SEL_POPUP_BTN)
            return True
        except Exception as e:
            self.logger.warning(f"提交按钮常规点击失败（{e}），改用 DOM 点击")
        try:
            await browser.evaluate(
                "() => { const el = document.querySelector('%s'); if (el) el.click(); }"
                % self.SEL_POPUP_BTN
            )
            return True
        except Exception as e:
            self.logger.warning(f"DOM 点击提交按钮也失败: {e}")
            return False

    async def popup_hint(self) -> str:
        """读取当前所有 layer 弹层的文字（用于判断「请稍后」「获取验证码失败」等）"""
        try:
            txt = await self.ctx.browser.evaluate(
                "() => Array.from(document.querySelectorAll('.layui-layer-content'))"
                ".map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ')"
            )
        except Exception:
            return ""
        return (txt or "").strip()

    async def dump_showup_diag(self) -> None:
        """弹窗没出现时记录现场（写日志 + 截图），便于下次定位"""
        try:
            info = await self.ctx.browser.evaluate(
                "() => ({"
                "  url: location.href,"
                "  hasShowup: !!document.querySelector('#showup'),"
                "  showupText: ((document.querySelector('#showup') || {}).innerText || '').trim(),"
                "  jQuery: typeof jQuery !== 'undefined',"
                "  layer: typeof layer !== 'undefined',"
                "  layerCount: document.querySelectorAll('.layui-layer').length,"
                "  imgIds: Array.from(document.querySelectorAll('img[id^=showup]')).map(e => e.id)"
                "})"
            )
            self.log(f"签到弹窗诊断: {info}")
        except Exception as e:
            self.logger.warning(f"签到弹窗诊断取值失败: {e}")
        try:
            path = await self.ctx.browser.save_screenshot(name=f"{self.id}_showup_diag")
            self.log(f"已保存签到现场截图: {path}")
        except Exception:
            pass

    async def open_site(self) -> None:
        await self.ctx.browser.goto(self.cfg.url)
        await self.note_site_origin()
        if self.cfg.has_cf:
            await self.handle_challenge_if_any()

    # prepare_cookies（打开站点前注入 CookieCloud cookie）统一在 BaseTask.prepare_cookies

    async def sync_cookies_and_reload(self) -> bool:
        """兜底同步：再次注入 cookie + localStorage 并刷新当前页"""
        cc = self.ctx.cookiecloud
        if not cc or not cc.cfg.enabled:
            self.log("CookieCloud 未启用，跳过同步")
            return False
        if cc.cfg.mode == "paused":
            self.log("CookieCloud 工作模式为「暂停同步」，跳过")
            return False
        try:
            cookies = cc.get_cookies(domains=[self._domain])
            if not cookies:
                self.log("CookieCloud 中未找到本站 cookie")
                return False
            await self.ctx.browser.add_cookies(cookies)
            try:
                ls = cc.get_local_storage(domains=[self._domain])
                for origin, items in ls.items():
                    await self.ctx.browser.set_local_storage(origin, items)
            except Exception as e:
                self.logger.warning(f"localStorage 同步跳过: {e}")
            await self.ctx.browser.reload()
            await self.ctx.browser.wait_timeout(1200)  # 等重定向完成后再判定
            if self.cfg.has_cf:
                await self.handle_challenge_if_any()
            return True
        except Exception as e:
            self.logger.warning(f"CookieCloud 同步失败: {e}")
            return False

    async def login_with_password(self) -> bool:
        browser = self.ctx.browser
        username, password = self.cfg.username, self.cfg.password
        if not username or not password:
            raise TaskError("需要登录但未配置账号密码（config/tasks/pt_kong.yaml -> account）")

        # 登录重试次数是**跨天累计**的预算：只有登录成功才清零（不会因为换了一天就重置）
        limit = self.login_limit
        remaining = self.login_remaining()
        if remaining <= 0:
            raise TaskError(self.login_lock_message())
        self.log(f"登录预算：已失败 {self.login_used()}/{limit} 次，本次最多再试 {remaining} 次")
        for attempt in range(1, remaining + 1):
            if await self.is_logged_in():
                self.log("已是登录状态，跳过登录")
                return True
            try:
                if not await self.has_login_form():
                    self.log(f"第 {attempt}/{remaining} 次尝试：页面上没有登录表单，重新打开站点")
                    await self.open_site()
                    await browser.wait_timeout(1200)
                    if await self.is_logged_in():
                        self.log("重新打开后已登录")
                        return True
                    if not await self.has_login_form():
                        continue  # 仍无表单，下一轮再试

                self.log(f"第 {attempt}/{remaining} 次登录尝试（累计失败 {self.login_used()}/{limit}）")
                await browser.wait_selector(self.SEL_USERNAME)
                await browser.fill(self.SEL_USERNAME, username)
                await browser.fill(self.SEL_PASSWORD, password)

                # 2FA：任务勾选「有 2FA」时自动生成并填写动态验证码
                if self.cfg.has_2fa and self.ctx.totp:
                    if self.ctx.totp.remaining() < 5:
                        wait_s = self.ctx.totp.remaining() + 1
                        self.log(f"2FA 验证码即将过期，等待 {wait_s}s 进入新周期")
                        await browser.wait_timeout(wait_s * 1000)
                    code = self.ctx.totp.now_code()
                    await browser.fill(self.SEL_ONECODE, code)
                    self.log(f"已填入 2FA 验证码（{code}，剩余 {self.ctx.totp.remaining()}s 有效）")

                # AI 识别登录验证码（识别不出来就不提交，避免空验证码必然失败）
                has_img = False
                try:
                    has_img = await browser.page.query_selector(self.SEL_CAPTCHA_IMG) is not None
                except Exception:
                    has_img = False
                if has_img:
                    try:
                        await self.ctx.captcha.solve_and_fill(
                            self.SEL_CAPTCHA_IMG, self.SEL_CAPTCHA_INPUT, prompt_key="captcha"
                        )
                    except Exception as e:
                        self.logger.warning(
                            f"登录图形验证码识别失败，本次不提交登录（{e}）。"
                            "请检查 AI 接口是否可用")
                        await self.open_site()
                        await browser.wait_timeout(1000)
                        continue

                await browser.click(self.SEL_SUBMIT)
                await browser.wait_timeout(3000)  # 等待跳转/响应
                if self.cfg.has_cf:
                    await self.handle_challenge_if_any()

                if await self.is_logged_in():
                    self.log("登录成功")
                    self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                    return True

                # 失败原因记录
                text = await self.page_text()
                hint = ""
                for kw in ("验证码", "密码", "错误", "失败", "禁用", "二次验证", "passcode"):
                    if kw in text:
                        hint = f"（页面提示可能含: {kw}）"
                        break
                self.log(f"登录未成功{hint}，重新打开站点后重试")
                self.events.emit(Events.LOGIN_FAILED, task_id=self.id, attempt=attempt)
                if self.note_login_failure(f"登录未成功{hint}") >= limit:
                    break   # 预算用尽：不再继续尝试（跨天也不会自动恢复）
                await self.open_site()
                await browser.wait_timeout(1000)
            except Exception as e:
                self.logger.warning(f"登录尝试 {attempt} 异常: {e}")
                try:
                    await self.open_site()
                    await browser.wait_timeout(1000)
                except Exception:
                    pass

        if self.login_locked():
            raise TaskError(self.login_lock_message())
        raise TaskError(f"账号密码登录失败（本次重试 {remaining} 次，累计失败 "
                        f"{self.login_used()}/{limit} 次）")

    async def wait_checkin_result(self, timeout_s: int = 15) -> str:
        """轮询弹窗/页面文本，判断签到结果"""
        browser = self.ctx.browser
        for _ in range(timeout_s * 2):
            await browser.wait_timeout(500)
            try:
                layer_text = await browser.evaluate(
                    "() => { const el = document.querySelector('%s'); return el ? el.innerText : ''; }"
                    % self.SEL_LAYER_TEXT
                ) or ""
            except Exception:
                layer_text = ""
            if layer_text:
                for kw in self.KW_ALREADY:
                    if kw in layer_text:
                        return "already"
                for kw in self.KW_SUCCESS:
                    if kw in layer_text:
                        return "success"
                for kw in self.KW_FAIL:
                    if kw in layer_text:
                        return "fail"
            # 页面自己的成功标志最可靠：showup.php 成功后入口文字变成「已签到(Showed Up)」
            if await self.showup_text_signed():
                return "success"
            # 弹窗消失：等页面刷新后再确认，避免把"验证码错误导致弹窗关闭"误判为成功
            try:
                if not await browser.page.query_selector(self.SEL_POPUP_BTN):
                    await browser.wait_timeout(600)
                    body = await self.page_text()
                    if "已签到" in body or "已经签到" in body:
                        return "already"
                    return "closed"  # 弹窗关闭但无明确成功标志
            except Exception:
                pass
        return "unknown"

    def _interpret_result(self, status: str) -> tuple[bool, str]:
        mapping = {
            "success": (True, "签到成功"),
            "already": (True, "今日已签到（无需重复）"),
            "closed": (True, "签到完成（验证码弹窗已关闭，未取到明确提示，建议核对）"),
            "fail": (False, "签到失败（验证码错误或弹窗提示失败）"),
            "unknown": (False, "签到结果未知（请查看截图确认）"),
        }
        ok, msg = mapping.get(status, (False, f"未知状态: {status}"))
        self._detail = f"弹窗判定: {status}"
        return ok, msg
