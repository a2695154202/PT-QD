"""Akile（AkileCloud / site-f.example）每日签到任务。

需求（实测流程）：
1. 打开首页；未登录时点击右上角「登录」进入登录页
2. 登录页填邮箱 + 密码，点「登录」
3. 若账号开了谷歌验证器，会弹出 2FA 弹窗（6 位分格输入 + 「继续」按钮），填完点「继续」
4. 登录后右上角有「控制台」入口，直接进 https://site-f.example/console
5. 控制台（工作台）里点「每日签到」按钮完成签到

站点是 Vue 3 + Arco Design 单页应用（实测 DOM）：
- 登录页 https://site-f.example/login，表单 #login-credential-form
  - 邮箱 input#login-email、密码 input#login-password
  - 提交按钮 form#login-credential-form 内的 arco-btn-primary（文案「登录」）
- 2FA 是**全屏弹窗**（挂载在 #app 之外）：
  - div.verification-code > div.code-content > input.ak-code-input（name=ak-code-0..5 共 6 格）
  - 标题「使用验证器应用验证身份」，按钮 div.code-footer 的「继续」（未填满 6 位时 disabled）
- 登录后可能弹「请添加 Passkey 验证」提示 → 点「下次一定」跳过
- 控制台 https://site-f.example/console，签到按钮 .dashboard-checkin-pill（文案「每日签到」）
- 签到结果文案（取自站点 JS 包 i18n）：
  - 成功：「恭喜您签到成功，获得 X AK币」/「签到成功」
  - 已签到：「今日已签到」（按钮文案也会变成它）
"""
from __future__ import annotations

from urllib.parse import urlparse

from core.config import TaskConfig
from core.events import Events
from core.totp import TwoFactorAuth
from .base import BaseTask, TaskContext, TaskError, register


@register
class AkileTask(BaseTask):
    id = "akile"
    name = "Akile（AkileCloud）"
    description = "AkileCloud(site-f.example) 每日签到：账号密码 + 谷歌验证器(2FA) + 控制台「每日签到」"
    site = "https://site-f.example/"

    LOGIN_URL = "https://site-f.example/login"
    CHECKIN_URL = "https://site-f.example/console"

    # ---- 首页右上角登录入口 ----
    SEL_HOME_LOGIN = "button:has-text('登录')"

    # ---- 登录表单（实测）----
    SEL_LOGIN_FORM = "#login-credential-form"
    SEL_USERNAME = "#login-email"
    SEL_PASSWORD = "#login-password"
    SEL_LOGIN_BTN = "form#login-credential-form button.arco-btn-primary"

    # ---- 2FA 弹窗（验证器应用 6 位码）----
    SEL_2FA_BOX = "div.verification-code input.ak-code-input"
    SEL_2FA_CONTINUE = "div.verification-code button:has-text('继续')"

    # ---- 登录后可能出现的 Passkey 提示（跳过即可）----
    SEL_PASSKEY_SKIP = "button:has-text('下次一定')"

    # ---- 登录后才会出现的标志 ----
    LOGGED_IN_MARKERS = (
        ".dashboard-hero",
        ".console-dashboard-page",
        ".dashboard-checkin-pill",
        "a[href*='console']",
    )

    # ---- 签到按钮与结果文案 ----
    SEL_CHECKIN_BTN = ".dashboard-checkin-pill"
    CHECKIN_BTN_CANDIDATES = (
        ".dashboard-checkin-pill",
        "button:has-text('每日签到')",
    )
    ALREADY_KEYWORDS = ("今日已签到", "已签到", "明天再来", "明日再来")
    SUCCESS_KEYWORDS = ("恭喜您签到成功", "签到成功", "签到完成")
    FAIL_KEYWORDS = ("签到失败", "签到出错", "无法签到", "请先登录", "操作失败")

    def __init__(self, cfg: TaskConfig, ctx: TaskContext):
        super().__init__(cfg, ctx)
        self._detail = ""
        self._domain = urlparse(self.cfg.url or self.site).hostname or "site-f.example"
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
            self.logger.warning("任务勾选了 2FA 但未找到可用密钥，登录遇到验证器弹窗时会失败")

    # ---------- 页面判定 ----------
    async def has_login_form(self) -> bool:
        try:
            return await self.ctx.browser.page.query_selector(self.SEL_LOGIN_FORM) is not None
        except Exception:
            return False

    async def is_logged_in(self) -> bool:
        page = self.ctx.browser.page
        try:
            url = await self.ctx.browser.url()
        except Exception:
            return False
        if "/login" in url:
            return False
        if await self.has_login_form():
            return False
        for sel in self.LOGGED_IN_MARKERS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue
        # 首页右上角出现「控制台」入口即视为已登录
        try:
            loc = page.locator("a:has-text('控制台'), button:has-text('控制台')").first
            if await loc.count() and await loc.is_visible():
                return True
        except Exception:
            pass
        return False

    async def is_login_page(self) -> bool:
        if await self.is_logged_in():
            return False
        return await self.has_login_form() or "/login" in (await self.ctx.browser.url())

    # ---------- 打开站点 ----------
    async def open_site(self, url: str = "") -> None:
        await self.ctx.browser.goto((url or self.cfg.url or self.site).strip())
        await self.ctx.browser.wait_timeout(2500)   # Vue SPA 渲染
        await self.handle_challenge_if_any()

    async def prepare_cookies(self) -> bool:
        """打开站点之前把 CookieCloud 的 cookie 注入浏览器上下文"""
        cc = self.ctx.cookiecloud
        if not cc or not cc.cfg.enabled:
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
            self.events.emit(Events.COOKIE_SYNCED, task_id=self.id, count=len(cookies))
            self.log(f"打开站点前已注入 {len(cookies)} 个 cookie")
            return True
        except Exception as e:
            self.logger.warning(f"CookieCloud 预注入失败: {e}")
            return False

    # ---------- 主流程 ----------
    async def ensure_logged_in(self) -> bool:
        await self.prepare_cookies()
        await self.open_site()

        if await self.is_logged_in():
            self.log("已登录（CookieCloud cookie 有效）")
            return True

        # 未登录 → 点右上角「登录」（实在找不到就直接打开登录页）
        if not await self.has_login_form():
            self.log("未登录，点击右上角「登录」进入登录页")
            await self._goto_login()
        self.events.emit(Events.LOGIN_PAGE_DETECTED, task_id=self.id)
        return await self.login_with_password()

    async def _goto_login(self) -> None:
        page = self.ctx.browser.page
        try:
            loc = page.locator(self.SEL_HOME_LOGIN).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                await self.ctx.browser.wait_timeout(2500)
                if await self.has_login_form():
                    return
        except Exception as e:
            self.logger.warning(f"点击右上角「登录」失败: {e}")
        self.log(f"直接打开登录页: {self.LOGIN_URL}")
        await self.open_site(self.LOGIN_URL)

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
                    self.log(f"第 {attempt}/{remaining} 次尝试：页面上没有登录表单，重新打开登录页")
                    await self.open_site(self.LOGIN_URL)
                    if await self.is_logged_in():
                        self.log("重新打开后已登录")
                        return True
                    if not await self.has_login_form():
                        continue

                self.log(f"第 {attempt}/{remaining} 次登录尝试（累计失败 {self.login_used()}/{limit}）")
                await browser.wait_selector(self.SEL_USERNAME, timeout=10000)
                await browser.fill(self.SEL_USERNAME, username)
                await browser.fill(self.SEL_PASSWORD, password)
                await browser.click(self.SEL_LOGIN_BTN)
                self.log("已提交邮箱密码")
                await browser.wait_timeout(3000)

                # 谷歌验证器弹窗（6 位码 + 继续）
                if await self._2fa_modal_visible():
                    self.log("弹出验证器应用验证（2FA），填写 6 位动态码")
                    if not await self._fill_2fa():
                        self.log("2FA 未完成，计入一次登录失败")
                        if self.note_login_failure("2FA 未完成") >= limit:
                            break
                        await self.open_site(self.LOGIN_URL)
                        continue
                    await browser.wait_timeout(2500)

                # 登录后可能弹「请添加 Passkey」提示
                await self._dismiss_passkey()

                if await self.is_logged_in():
                    self.log("登录成功")
                    self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                    return True

                text = await self.page_text()
                hint = ""
                for kw in ("验证码", "密码", "错误", "失败", "禁用", "验证器", "2fa", "邮箱"):
                    if kw in text:
                        hint = f"（页面提示可能含: {kw}）"
                        break
                self.log(f"登录未成功{hint}")
                self.events.emit(Events.LOGIN_FAILED, task_id=self.id, attempt=attempt)
                if self.note_login_failure(f"登录未成功{hint}") >= limit:
                    break   # 预算用尽：不再继续尝试（跨天也不会自动恢复）
                await self.open_site(self.LOGIN_URL)
            except Exception as e:
                self.logger.warning(f"登录尝试 {attempt} 异常: {e}")
                try:
                    await self.open_site(self.LOGIN_URL)
                except Exception:
                    pass

        if self.login_locked():
            raise TaskError(self.login_lock_message())
        raise TaskError(f"账号密码登录失败（本次重试 {remaining} 次，累计失败 "
                        f"{self.login_used()}/{limit} 次）")

    # ---------- 2FA 弹窗 ----------
    async def _2fa_modal_visible(self) -> bool:
        try:
            loc = self.ctx.browser.page.locator("div.verification-code").first
            return bool(await loc.count() and await loc.is_visible())
        except Exception:
            return False

    async def _fill_2fa(self) -> bool:
        """填 6 位验证器码并点「继续」，返回是否提交成功"""
        browser = self.ctx.browser
        page = browser.page
        if not self.ctx.totp:
            self.logger.warning("站点要求验证器应用验证，但没有可用的 2FA 密钥")
            return False
        if self.ctx.totp.remaining() < 5:
            wait_s = self.ctx.totp.remaining() + 1
            self.log(f"2FA 验证码即将过期，等待 {wait_s}s 进入新周期")
            await browser.wait_timeout(wait_s * 1000)
        code = self.ctx.totp.now_code()

        # 6 个分格输入框：input[name=ak-code-0..5]
        filled = 0
        for i, ch in enumerate(code):
            sel = f"input[name='ak-code-{i}']"
            try:
                if await page.query_selector(sel):
                    await browser.fill(sel, ch)
                    filled += 1
            except Exception:
                continue
        if filled < 6:
            # 兜底：点第一格后整体输入（组件通常会自动跳格）
            try:
                first = page.locator(self.SEL_2FA_BOX).first
                await first.click()
                await page.keyboard.type(code, delay=80)
                filled = 6
                self.log("改用整串输入填写 2FA")
            except Exception as e:
                self.logger.warning(f"2FA 输入失败: {e}")
                return False
        self.log(f"已填入 2FA 验证码（{code}，剩余 {self.ctx.totp.remaining()}s 有效）")

        try:
            btn = page.locator(self.SEL_2FA_CONTINUE).first
            await btn.wait_for(state="visible", timeout=5000)
            for _ in range(6):
                if not await btn.is_disabled():
                    break
                await browser.wait_timeout(400)      # 6 格填满后按钮才启用
            await btn.click()
            self.log("已点击 2FA「继续」")
        except Exception as e:
            self.logger.warning(f"点击 2FA「继续」失败: {e}")
            return False
        return True

    async def _dismiss_passkey(self) -> None:
        """跳过「请添加 Passkey 验证」提示（点「下次一定」）"""
        try:
            loc = self.ctx.browser.page.locator(self.SEL_PASSKEY_SKIP).first
            if await loc.count() and await loc.is_visible():
                await loc.click()
                self.log("已跳过 Passkey 添加提示（下次一定）")
                await self.ctx.browser.wait_timeout(1200)
        except Exception as e:
            self.logger.warning(f"跳过 Passkey 提示失败: {e}")

    # ---------- 签到 ----------
    @staticmethod
    def _has_kw(text: str, keywords) -> bool:
        return any(kw in text for kw in keywords)

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
        url = (self.cfg.options.get("checkin_url") or self.CHECKIN_URL).strip()
        self.log(f"进入控制台: {url}")
        await browser.goto(url)
        await browser.wait_timeout(3000)     # SPA 渲染
        await self.handle_challenge_if_any()

        body = await self.page_text()
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"

        if not await self.is_logged_in():
            return False, "签到前检测到仍处于未登录状态"

        btn_sel = await self._find_checkin_button()
        if not btn_sel:
            if self._has_kw(body, self.SUCCESS_KEYWORDS):
                return True, "签到成功"
            self.log("未找到「每日签到」按钮，页面文本片段: " + body[:200].replace("\n", " "))
            return False, "未找到签到按钮（可在任务配置 checkin_btn 指定选择器）"

        self.log(f"找到签到按钮: {btn_sel}")
        await browser.click(btn_sel)
        self.events.emit(Events.CHECKIN_CLICKED, task_id=self.id)
        self.log("已点击「每日签到」，等待结果")
        await browser.wait_timeout(2500)

        body = await self.page_text()
        if self._has_kw(body, self.SUCCESS_KEYWORDS):
            return True, "签到成功"
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if self._has_kw(body, self.FAIL_KEYWORDS):
            return False, "签到失败（页面出现失败提示）"

        # 按钮文案变成「今日已签到」也算完成
        try:
            btn_text = (await self.ctx.browser.page.locator(btn_sel).first.inner_text() or "").strip()
        except Exception:
            btn_text = ""
        if any(kw in btn_text for kw in self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"

        self.log(f"签到结果无法判定，按钮文案「{btn_text}」，页面片段: "
                 + body[:200].replace("\n", " "))
        return False, "签到结果未识别（请查看运行截图）"
