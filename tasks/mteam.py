"""馒头（M-Team / site-d.example）保活任务。

需求：打开 https://site-d.example/index 并保持 10 秒；若跳转到登录页则先完成登录
（账号密码 + 两步验证码），登录成功后再保持 10 秒。

站点是 React 单页应用（antd），页面由 JS 渲染，登录页真实结构（实测）：
- input#username（placeholder 用戶名）
- input#password（placeholder 密碼）
- 单个提交按钮（文案「登 錄」）
- 登录后可能进入两步验证页，需要填 6 位动态验证码

因此选择器以 id 为主、2FA 输入框用多候选探测，并支持在任务配置里覆盖。
"""
from __future__ import annotations

from urllib.parse import urlparse


from core.config import TaskConfig
from core.events import Events
from core.totp import TwoFactorAuth
from .base import BaseTask, TaskContext, TaskError, register


@register
class MTeamTask(BaseTask):
    id = "mteam"
    name = "馒头（M-Team）"
    description = "M-Team(site-d.example) 保活：打开首页保持 10 秒；跳登录页则登录（含 2FA）后再保持"
    site = "https://site-d.example/index"

    # ---- 登录页（实测结构）----
    SEL_USERNAME = "#username"
    SEL_PASSWORD = "#password"
    SEL_LOGIN_BTN = "button.ant-btn-primary, button"

    # ---- 2FA 输入框候选（登录后可能出现的两步验证页）----
    SEL_2FA_CANDIDATES = (
        "input[id*='code']",
        "input[id*='otp']",
        "input[id*='2fa']",
        "input[id*='totp']",
        "input[name*='code']",
        "input[name*='otp']",
        "input[placeholder*='驗證']",
        "input[placeholder*='验证']",
        "input[placeholder*='2FA']",
        "input[placeholder*='動態']",
        "input[maxlength='6']",
    )
    SEL_2FA_SUBMIT_CANDIDATES = (
        "button[type='submit']",
        "button.ant-btn-primary",
        "button:has-text('確')",
        "button:has-text('确')",
        "button:has-text('驗')",
        "button:has-text('验')",
        "button:has-text('提交')",
        "button:has-text('下一步')",
    )

    DEFAULT_KEEP_SECONDS = 10

    def __init__(self, cfg: TaskConfig, ctx: TaskContext):
        super().__init__(cfg, ctx)
        self._detail = ""
        self._domain = urlparse(self.cfg.url).hostname or "site-d.example"
        if self.cfg.has_2fa:
            self._setup_totp()

    # ---------- 2FA 准备 ----------
    def _setup_totp(self) -> None:
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
            self.logger.warning("任务勾选了 2FA 但未找到可用密钥，登录遇到 2FA 页时会失败")

    # ---------- 页面判定 ----------
    async def has_login_form(self) -> bool:
        try:
            return await self.ctx.browser.page.query_selector(self.SEL_USERNAME) is not None
        except Exception:
            return False

    # 只认「登录后才有」的元素：早前用裸 "header" 当判据，加上最后 `bool(text)`
    # 兜底，导致任何有文字的页面（维护页/报错页）都被判成已登录 → 假成功
    LOGGED_IN_SELECTORS = ("a[href*='logout']", "a[href*='userdetails']",
                           ".ant-layout-header", ".ant-menu", ".ant-layout-sider")
    LOGIN_PAGE_HINTS = ("Sign in to MTeam", "用戶名", "密碼", "登 錄", "登錄")

    async def is_logged_in(self) -> bool:
        """单页应用判定：排除登录页、2FA 页；再用「登录后才有」的元素确认

        没有命中任何正向标志时一律按未登录处理（登录会重试；判错了会漏签一整天）。
        """
        browser = self.ctx.browser
        try:
            url = await browser.url()
        except Exception:
            return False
        if "/login" in url or "/2fa" in url:
            return False
        if await self.has_login_form():
            return False
        if await self.find_2fa_input():
            return False  # 仍停留在两步验证页，视为未完成登录
        try:
            page = browser.page
            for sel in self.LOGGED_IN_SELECTORS:
                if await page.query_selector(sel):
                    return True
            text = await self.page_text()
            if any(h in text for h in self.LOGIN_PAGE_HINTS):
                return False
            self.logger.info("未找到「已登录」标志元素，按未登录处理，页面片段: "
                             + text[:120].replace("\n", " "))
            return False
        except Exception:
            return False

    async def find_2fa_input(self) -> str:
        """探测两步验证输入框（支持配置覆盖）"""
        page = self.ctx.browser.page
        candidates = []
        cfg_sel = (self.cfg.options.get("sel_2fa_input") or "").strip()
        if cfg_sel:
            candidates.append(cfg_sel)
        candidates.extend(self.SEL_2FA_CANDIDATES)
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        return ""

    async def _find_submit_button(self) -> str:
        page = self.ctx.browser.page
        candidates = []
        cfg_sel = (self.cfg.options.get("sel_2fa_submit") or "").strip()
        if cfg_sel:
            candidates.append(cfg_sel)
        candidates.extend(self.SEL_2FA_SUBMIT_CANDIDATES)
        for sel in candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return sel
            except Exception:
                continue
        return ""

    # ---------- Cookie ----------
    # prepare_cookies（打开站点前注入 CookieCloud cookie）统一在 BaseTask.prepare_cookies

    # ---------- 主流程 ----------
    async def ensure_logged_in(self) -> bool:
        browser = self.ctx.browser
        await self.prepare_cookies()
        await browser.goto(self.cfg.url)
        await browser.wait_timeout(3500)  # 等待单页应用渲染

        if await self.has_login_form():
            self.log("跳转到登录页，开始登录")
            self.events.emit(Events.LOGIN_PAGE_DETECTED, task_id=self.id)
            return await self.login_with_password()

        if await self.find_2fa_input():
            self.log("当前页面要求两步验证，直接提交动态验证码")
            ok = await self.submit_2fa()
            if not ok:
                if self.note_login_failure("两步验证未通过") >= self.login_limit:
                    raise TaskError(self.login_lock_message())
            return ok

        if await self.is_logged_in():
            self.log("已登录（cookie 有效）")
            return True

        await browser.wait_timeout(2000)
        if await self.is_logged_in():
            self.log("已登录（加载完成后确认）")
            return True
        raise TaskError("无法识别页面状态（既非登录页也非已登录），请查看运行截图")

    async def login_with_password(self) -> bool:
        browser = self.ctx.browser
        username, password = self.cfg.username, self.cfg.password
        if not username or not password:
            raise TaskError("需要登录但未配置账号密码（config/tasks/mteam.yaml -> account）")

        # 登录重试次数是**跨天累计**的预算：只有登录成功才清零（不会因为换了一天就重置）
        limit = self.login_limit
        remaining = self.login_remaining()
        if remaining <= 0:
            raise TaskError(self.login_lock_message())
        self.log(f"登录预算：已失败 {self.login_used()}/{limit} 次，本次最多再试 {remaining} 次")
        for attempt in range(1, remaining + 1):
            if await self.is_logged_in():
                return True
            self.log(f"第 {attempt}/{remaining} 次登录尝试（累计失败 {self.login_used()}/{limit}）")
            try:
                if not await self.has_login_form():
                    self.log("登录表单未出现，重新打开站点")
                    await browser.goto(self.cfg.url)
                    await browser.wait_timeout(3000)
                    if not await self.has_login_form():
                        continue
                await browser.wait_selector(self.SEL_USERNAME, timeout=10000)
                await browser.fill(self.SEL_USERNAME, username)
                await browser.fill(self.SEL_PASSWORD, password)
                await browser.click(self.SEL_LOGIN_BTN)
                self.log("已提交用户名密码")
                await browser.wait_timeout(3500)

                if await self.find_2fa_input():
                    self.log("进入两步验证页")
                    if await self.submit_2fa():
                        self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                        return True
                    self.log("两步验证未通过")
                elif await self.is_logged_in():
                    self.log("登录成功")
                    self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                    return True

                text = await self.page_text()
                hint = ""
                for kw in ("錯誤", "错误", "失敗", "失败", "密碼", "密码", "驗證", "验证", "禁用"):
                    if kw in text:
                        hint = f"（页面提示可能含: {kw}）"
                        break
                self.log(f"登录未成功{hint}")
                self.events.emit(Events.LOGIN_FAILED, task_id=self.id, attempt=attempt)
                if self.note_login_failure(f"登录未成功{hint}") >= limit:
                    break   # 预算用尽：不再继续尝试（跨天也不会自动恢复）
                await browser.goto(self.cfg.url)
                await browser.wait_timeout(2500)
            except TaskError:
                raise  # 配置类错误（如缺少 2FA 密钥）直接上抛，保留真实原因
            except Exception as e:
                self.logger.warning(f"登录尝试 {attempt} 异常: {e}")
                try:
                    await browser.goto(self.cfg.url)
                    await browser.wait_timeout(2500)
                except Exception:
                    pass
        if self.login_locked():
            raise TaskError(self.login_lock_message())
        raise TaskError(f"账号密码登录失败（本次重试 {remaining} 次，累计失败 "
                        f"{self.login_used()}/{limit} 次）")

    async def submit_2fa(self) -> bool:
        """填写两步验证码并提交"""
        browser = self.ctx.browser
        sel = await self.find_2fa_input()
        if not sel:
            self.logger.warning("未找到 2FA 输入框")
            return False
        if not self.ctx.totp:
            raise TaskError(
                "站点要求两步验证，但没有可用的 2FA 密钥。"
                "请在任务配置勾选「有 2FA」并绑定验证器（凭证库中 M-Team 的账号），或直接填 secret"
            )
        if self.ctx.totp.remaining() < 5:
            wait_s = self.ctx.totp.remaining() + 1
            self.log(f"2FA 验证码即将过期，等待 {wait_s}s 进入新周期")
            await browser.wait_timeout(wait_s * 1000)
        code = self.ctx.totp.now_code()
        await browser.fill(sel, code)
        self.log(f"已填入 2FA 验证码（{code}，剩余 {self.ctx.totp.remaining()}s 有效）")

        btn = await self._find_submit_button()
        if btn:
            await browser.click(btn)
            self.log("已提交 2FA 验证码")
        else:
            # 部分实现会在输入满 6 位后自动提交
            self.logger.warning("未找到 2FA 提交按钮，尝试回车提交")
            try:
                await browser.page.keyboard.press("Enter")
            except Exception:
                pass
        await browser.wait_timeout(4000)
        if await self.is_logged_in():
            self.log("两步验证通过，已登录")
            return True
        # 登录后可能仍在原路由，主动打开首页再判定一次
        await browser.goto(self.cfg.url)
        await browser.wait_timeout(3000)
        return await self.is_logged_in()

    async def do_checkin(self) -> tuple[bool, str]:
        """保活：在首页保持指定秒数（默认 10 秒）"""
        browser = self.ctx.browser
        try:
            keep = int(self.cfg.options.get("keep_seconds", self.DEFAULT_KEEP_SECONDS) or self.DEFAULT_KEEP_SECONDS)
        except (TypeError, ValueError):
            keep = self.DEFAULT_KEEP_SECONDS
        keep = max(1, keep)

        url = await browser.url()
        if "/login" in url or await self.has_login_form():
            self.log("当前仍在登录页，重新打开首页")
            await browser.goto(self.cfg.url)
            await browser.wait_timeout(3000)
            if await self.has_login_form():
                return False, "无法进入首页（仍停留在登录页）"

        self.log(f"页面已打开，保持 {keep} 秒")
        await browser.wait_timeout(keep * 1000)

        if await self.has_login_form():
            return False, "保持期间会话失效（跳转回登录页）"
        return True, f"已在 M-Team 首页保持 {keep} 秒"
