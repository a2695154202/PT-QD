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

    # ---- 独立「两步验证页」选择器 ----
    # 注意：登录表单里的 2FA 字段（SEL_ONECODE）和登录后跳转的独立 2FA 页是两回事。
    # 杜比等站点现在把 2FA 做成单独一页，提交账号密码后才出现，之前只填登录表单内的字段，
    # 于是「触发了验证码却没人填」。这里覆盖常见的输入框命名。
    SEL_2FA_CANDIDATES = (
        "input[name='2fa']",                       # 杜比 / 部分 NexusPHP 变体
        "input[name='oneCode']",                   # 原生 NexusPHP
        "input[name='two_step_code']",             # hhanclub 风格
        "input[name='code'][maxlength='6']",
        "input[id*='otp']",
        "input[name*='otp']",
        "input[id*='totp']",
        "input[name*='totp']",
        "input[autocomplete='one-time-code']",
        "input[maxlength='6']",
    )
    SEL_2FA_SUBMIT_CANDIDATES = (
        "form input[type='submit'][value*='验']",
        "form button[type='submit']",
        "form input[type='submit']",
        "button:has-text('验证')",
        "button:has-text('提交')",
    )

    # ---- 登录后才会出现的标志 ----
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
    )

    # ---- 签到页与按钮 ----
    CHECKIN_URL = ""                    # 签到页路径（可写绝对地址，域名会按 task.url 重挂）
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

    # ---------- 独立两步验证页 ----------
    async def find_2fa_input(self) -> str:
        """探测「独立两步验证页」的输入框。

        登录表单还在的时候不认：表单内的 2FA 字段由 SEL_ONECODE 负责，
        在这里重复填会把提交顺序打乱。支持 options.sel_2fa_input 覆盖。
        """
        browser = self.ctx.browser
        if await self.has_login_form():
            return ""
        page = browser.page
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

    async def _find_2fa_submit(self) -> str:
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

    async def submit_2fa(self) -> bool:
        """在独立两步验证页填动态码并提交，返回提交后是否已是登录态"""
        browser = self.ctx.browser
        sel = await self.find_2fa_input()
        if not sel:
            self.logger.warning("页面上没找到两步验证输入框")
            return False
        if not self.ctx.totp:
            raise TaskError(
                "站点要求两步验证，但没有可用的 2FA 密钥。"
                "请在任务配置勾选「有 2FA」并填写 2FA 密钥（或把密钥存进凭证库）"
            )
        if self.ctx.totp.remaining() < 5:
            wait_s = self.ctx.totp.remaining() + 1
            self.log(f"2FA 验证码即将过期，等待 {wait_s}s 进入新周期")
            await browser.wait_timeout(wait_s * 1000)
        code = self.ctx.totp.now_code()
        await browser.fill(sel, code)
        self.log(f"已在两步验证页填入验证码（{code}，剩余 {self.ctx.totp.remaining()}s 有效）")

        btn = await self._find_2fa_submit()
        if btn:
            await browser.click(btn)
            self.log("已提交两步验证码")
        else:
            self.logger.warning("未找到两步验证提交按钮，尝试回车提交")
            try:
                await browser.action_pause()
                await browser.page.keyboard.press("Enter")
            except Exception:
                pass
        await browser.wait_timeout(3000)
        return await self.is_logged_in()

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
        # 记下本次导航真实落地的站点域名（站点 301 换域名时，签到页/登录页跟着走）
        await self.note_site_origin()
        if self.cfg.has_cf:
            passed = await self.ctx.cf.solve()
            if not passed:
                # solve 超时不等于站点不能用：有的站点挑战页残留 / 判定偏严，
                # 但登录表单或已登录标志其实已经出现 → 继续，避免「页面好好的却报超时」
                await self.ctx.browser.wait_timeout(800)
                if await self.is_logged_in() or await self.has_login_form():
                    self.log("CF 等待超时，但页面已可用（登录表单/登录态已出现），继续后续流程")
                    return
                # 页面确实不可用：直接终止，避免后续反复重开站点、反复等同一挑战
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

        # 上次登录走到一半被打断时，可能停在独立两步验证页：直接补填动态码
        if await self.find_2fa_input():
            self.log("当前页面要求两步验证，直接填动态验证码")
            if await self.submit_2fa():
                self.log("两步验证通过")
                self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                return True
            self.log("两步验证未通过，转入账号密码登录")
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
            raise TaskError(
                f"站点无法访问（{title or url}），请检查网络或代理设置；"
                f"如果站点已经更换域名，请把任务配置里的 task.url 改成新域名"
                f"（当前为 {self.cfg.url or '未填写'}），签到页/登录页会自动跟随"
            )
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
                await browser.wait_selector(self.SEL_USERNAME)
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
                            # 识别不出来就**不提交**：空验证码必然失败，
                            # 而且会白白消耗一次登录机会（这属于 AI/识别问题，不计入失败次数）
                            self.logger.warning(
                                f"登录图形验证码识别失败，本次不提交登录（{e}）。"
                                "请检查 AI 接口是否可用")
                            await self.open_site()
                            await browser.wait_timeout(1000)
                            continue

                await browser.click(self.SEL_SUBMIT)
                await browser.wait_timeout(3000)
                if self.cfg.has_cf:
                    await self.handle_challenge_if_any()

                # 登录后可能跳到独立两步验证页（杜比现在就是这么做的）：填动态码再提交
                if await self.find_2fa_input():
                    self.log("进入两步验证页")
                    if await self.submit_2fa():
                        self.log("两步验证通过，登录成功")
                        self.events.emit(Events.LOGIN_SUCCESS, task_id=self.id)
                        return True
                    self.log("两步验证未通过，重新打开站点后重试")
                    self.events.emit(Events.LOGIN_FAILED, task_id=self.id, attempt=attempt)
                    if self.note_login_failure("两步验证未通过") >= limit:
                        break
                    await self.open_site()
                    await browser.wait_timeout(1000)
                    continue

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
        url = await self.checkin_page_url()
        if not url:
            raise TaskError("签到页地址为空：请检查任务配置里的 task.url / options.checkin_url")
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
