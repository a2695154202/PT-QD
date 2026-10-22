"""NexusPHP 站点通用签到任务基类。

典型 PT 站（pt_kong / hddolby 等 NexusPHP 站点）的共性流程都在这里：
1. 打开站点之前先注入 CookieCloud 的 cookie
2. 打开站点，按任务勾选处理 Cloudflare 人机验证
3. 以页面真实标志判定登录态（不依赖 URL）
4. 需要时用账号密码 + 2FA + AI 图形验证码登录
5. 进入签到页：已签到则直接成功；否则找签到按钮 → 识别验证码 → 提交 → 判定结果

子类只需要覆盖选择器（如 2FA 字段名）、签到页地址与关键词。
"""
from __future__ import annotations

from urllib.parse import urlparse


from core.events import Events
from core.totp import TwoFactorAuth
from .base import BaseTask, TaskContext, TaskError


class NexusPHPTask(BaseTask):
    # ---- 登录页选择器（子类按站点覆盖）----
    SEL_LOGIN_FORM = "form[action='takelogin.php']"
    SEL_USERNAME = "form[action='takelogin.php'] input[name='username']"
    SEL_PASSWORD = "form[action='takelogin.php'] input[name='password']"
    SEL_ONECODE = "form[action='takelogin.php'] input[name='oneCode']"   # 2FA 输入框
    SEL_CAPTCHA_IMG = "img[src*='image.php?action=regimage']"
    SEL_CAPTCHA_INPUT = "form[action='takelogin.php'] input[name='imagestring']"
    SEL_SUBMIT = "form[action='takelogin.php'] input[type='submit']"

    # ---- 登录后才会出现的标志 ----
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
    )

    # ---- 签到页与按钮 ----
    CHECKIN_URL = ""                    # 签到页地址（也可用 options.checkin_url 覆盖）
    SEL_CHECKIN_BTN = ""                # 签到按钮选择器（也可用 options.checkin_btn 覆盖）
    # 只探测"提交签到"类控件；导航入口链接不作为签到按钮（点击它只跳转，不代表签到完成）
    CHECKIN_BTN_CANDIDATES = (
        "input[type='submit'][value*='签到']",
        "input[type='button'][value*='签到']",
        "button:has-text('签到')",
        "#signin",
    )

    # ---- 结果关键词 ----
    ALREADY_KEYWORDS = ("已签到", "已经签到", "今日已签到", "今天已签到", "已领取")
    SUCCESS_KEYWORDS = ("签到成功", "恭喜", "签到获得", "获得奖励")
    # 失败判定用强表述：页面常驻「失败登录次数」「无效邀请」等文本，
    # 用「失败」「无效」这类宽泛词会把成功误判为失败
    FAIL_KEYWORDS = ("签到失败", "签到出错", "验证码错误", "验证码不正确",
                     "不能重复签到", "无法签到")

    def __init__(self, cfg, ctx: TaskContext):
        super().__init__(cfg, ctx)
        self._detail = ""
        self._domain = urlparse(self.cfg.url).hostname or ""
        if self.cfg.has_2fa:
            self._setup_totp()

    # ---------- 2FA ----------
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
            self.logger.warning("任务勾选了 2FA 但未找到可用密钥，本次将不填 2FA")

    # ---------- 登录态判定 ----------
    async def is_logged_in(self) -> bool:
        page = self.ctx.browser.page
        for sel in self.LOGGED_IN_MARKERS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue
        return False

    async def has_login_form(self) -> bool:
        try:
            return await self.ctx.browser.page.query_selector(self.SEL_LOGIN_FORM) is not None
        except Exception:
            return False

    async def is_login_page(self) -> bool:
        if await self.is_logged_in():
            return False
        return await self.has_login_form()

    # ---------- 页面与 Cookie ----------
    async def open_site(self) -> None:
        await self.ctx.browser.goto(self.cfg.url)
        if self.cfg.has_cf:
            passed = await self.ctx.cf.solve()
            if not passed:
                # 直接终止，避免后续流程反复重开站点、反复等待同一挑战
                raise TaskError(
                    "Cloudflare 人机验证未通过（等待超时）。该站点对自动化浏览器校验较严，"
                    "可把浏览器模式改为 attach 人工完成一次验证，"
                    "cf_clearance 会保存在浏览器用户目录里供后续运行复用"
                )

    # prepare_cookies（打开站点前注入 CookieCloud cookie）统一在 BaseTask.prepare_cookies

    async def sync_cookies_and_reload(self) -> bool:
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
            await self.ctx.browser.wait_timeout(1200)
            if self.cfg.has_cf:
                await self.handle_challenge_if_any()
            return True
        except Exception as e:
            self.logger.warning(f"CookieCloud 同步失败: {e}")
            return False

    # ---------- 主流程 ----------
    async def ensure_logged_in(self) -> bool:
        await self.prepare_cookies()
        await self.open_site()

        if await self.is_logged_in():
            self.log("已登录（CookieCloud cookie 有效）")
            return True

        if await self.has_login_form():
            self.log("检测到登录页，当前无有效登录态")
            self.events.emit(Events.LOGIN_PAGE_DETECTED, task_id=self.id)
            if await self.sync_cookies_and_reload():
                if await self.is_logged_in():
                    self.log("CookieCloud 同步后已恢复登录态")
                    return True
                self.log("CookieCloud 同步后仍未登录，转入账号密码登录")
            return await self.login_with_password()

        await self.ctx.browser.wait_timeout(1500)
        if await self.is_logged_in():
            self.log("已登录（跳转完成后确认）")
            return True
        if await self.has_login_form():
            if await self.sync_cookies_and_reload() and await self.is_logged_in():
                return True
            return await self.login_with_password()

        self.log("页面状态未识别，重新打开站点后复核")
        await self.open_site()
        if await self.is_logged_in():
            return True
        if await self.has_login_form():
            if await self.sync_cookies_and_reload() and await self.is_logged_in():
                return True
            return await self.login_with_password()

        # 区分「CF 挑战未过」「站点打不开」「页面结构变化」，给出准确提示
        try:
            if await self.ctx.cf.is_challenge():
                raise TaskError(
                    "Cloudflare 人机验证未通过（等待超时）。可在任务配置勾选「有 CF 验证」后重试；"
                    "若仍失败，可把浏览器模式改为 attach 人工过验证后再跑"
                )
            url = await self.ctx.browser.url()
            title = await self.ctx.browser.title()
        except TaskError:
            raise
        except Exception:
            url, title = "", ""
        if url.startswith("chrome-error://") or "ERR_" in title or "无法访问" in title \
                or "This site can" in title:
            raise TaskError(f"站点无法访问（{title or url}），请检查网络或代理设置")
        raise TaskError("无法识别页面状态（既非登录页也非已登录页），请查看运行截图确认站点是否正常")

    async def login_with_password(self) -> bool:
        browser = self.ctx.browser
        username, password = self.cfg.username, self.cfg.password
        if not username or not password:
            raise TaskError(f"需要登录但未配置账号密码（config/tasks/{self.id}.yaml -> account）")

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
                        continue

                self.log(f"第 {attempt}/{remaining} 次登录尝试（累计失败 {self.login_used()}/{limit}）")
                await browser.wait_selector(self.SEL_USERNAME, timeout=8000)
                await browser.fill(self.SEL_USERNAME, username)
                await browser.fill(self.SEL_PASSWORD, password)

                # 2FA：仅当站点存在对应输入框时才填写（部分站点登录表单无 2FA 字段）
                if self.cfg.has_2fa and self.ctx.totp and self.SEL_ONECODE:
                    try:
                        has_field = await browser.page.query_selector(self.SEL_ONECODE) is not None
                    except Exception:
                        has_field = False
                    if has_field:
                        if self.ctx.totp.remaining() < 5:
                            wait_s = self.ctx.totp.remaining() + 1
                            self.log(f"2FA 验证码即将过期，等待 {wait_s}s 进入新周期")
                            await browser.wait_timeout(wait_s * 1000)
                        code = self.ctx.totp.now_code()
                        await browser.fill(self.SEL_ONECODE, code)
                        self.log(f"已填入 2FA 验证码（{code}，剩余 {self.ctx.totp.remaining()}s 有效）")
                    else:
                        self.log("当前登录页没有 2FA 输入框，跳过 2FA 填写")

                if self.SEL_CAPTCHA_IMG:
                    try:
                        if await browser.page.query_selector(self.SEL_CAPTCHA_IMG):
                            await self.ctx.captcha.solve_and_fill(
                                self.SEL_CAPTCHA_IMG, self.SEL_CAPTCHA_INPUT, prompt_key="captcha"
                            )
                    except Exception as e:
                        self.logger.warning(f"登录图形验证码处理失败: {e}")

                await browser.click(self.SEL_SUBMIT)
                await browser.wait_timeout(3000)
                if self.cfg.has_cf:
                    await self.handle_challenge_if_any()

                if await self.is_logged_in():
                    self.log("登录成功")
                    self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                    return True

                text = await self.page_text()
                hint = ""
                for kw in ("验证码", "密码", "错误", "失败", "禁用", "二次验证", "两步验证", "2fa"):
                    if kw in text:
                        hint = f"（页面提示可能含: {kw}）"
                        break
                self.log(f"登录未成功{hint}，重新打开站点后重试")
                self.events.emit(Events.LOGIN_FAILED, task_id=self.id, attempt=attempt)
                if self.note_login_failure(f"登录未成功{hint}") >= limit:
                    # 预算用尽：不再继续尝试（跨天也不会自动恢复）
                    break
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

    # ---------- 签到 ----------
    @staticmethod
    def _has_kw(text: str, keywords) -> bool:
        return any(kw in text for kw in keywords)

    async def _captcha_on_page(self) -> bool:
        if not self.SEL_CAPTCHA_IMG:
            return False
        try:
            return await self.ctx.browser.page.query_selector(self.SEL_CAPTCHA_IMG) is not None
        except Exception:
            return False

    async def _find_checkin_button(self) -> str:
        """按配置选择器 → 类默认 → 候选列表顺序探测可见的签到按钮"""
        page = self.ctx.browser.page
        candidates: list[str] = []
        cfg_btn = (self.cfg.options.get("checkin_btn") or "").strip()
        if cfg_btn:
            candidates.append(cfg_btn)
        if self.SEL_CHECKIN_BTN:
            candidates.append(self.SEL_CHECKIN_BTN)
        candidates.extend(self.CHECKIN_BTN_CANDIDATES)
        for sel in candidates:
            if not sel:
                continue
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        return ""

    async def do_checkin(self) -> tuple[bool, str]:
        browser = self.ctx.browser
        url = (self.cfg.options.get("checkin_url") or self.CHECKIN_URL or self.cfg.url).strip()
        self.log(f"进入签到页: {url}")
        await browser.goto(url)
        await browser.wait_timeout(1200)
        if self.cfg.has_cf:
            await self.handle_challenge_if_any()

        body = await self.page_text()
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"

        btn_sel = await self._find_checkin_button()
        if not btn_sel:
            if self._has_kw(body, self.SUCCESS_KEYWORDS):
                return True, "签到成功"
            self.log("未找到签到按钮，页面文本片段: " + body[:200].replace("\n", " "))
            return False, "未找到签到按钮（可在任务配置 checkin_btn 指定选择器）"

        self.log(f"找到签到按钮: {btn_sel}")
        if await self._captcha_on_page():
            try:
                await self.ctx.captcha.solve_and_fill(
                    self.SEL_CAPTCHA_IMG, self.SEL_CAPTCHA_INPUT, prompt_key="popup_captcha"
                )
            except Exception as e:
                self.logger.warning(f"签到验证码识别失败: {e}")
        await browser.click(btn_sel)
        self.events.emit(Events.CHECKIN_CLICKED, task_id=self.id)
        self.log("已提交签到，等待结果")
        await browser.wait_timeout(2500)
        if self.cfg.has_cf:
            await self.handle_challenge_if_any()

        body = await self.page_text()
        if self._has_kw(body, self.SUCCESS_KEYWORDS):
            return True, "签到成功"
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if self._has_kw(body, self.FAIL_KEYWORDS):
            return False, "签到失败（页面出现失败提示）"
        # 无法从页面文本确认结果时一律不猜成功，交由人工看截图确认
        self.log("签到结果无法从页面文本判定，页面片段: " + body[:200].replace("\n", " "))
        return False, "签到结果未识别（请查看运行截图；可在任务配置 checkin_btn 指定正确的签到按钮）"
