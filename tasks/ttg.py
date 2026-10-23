"""TTG（听听歌）签到任务。

站点：https://site-c.example/index.php （NexusPHP 定制皮肤）

登录页特征：
- 5 个语言版本的 `form[action='takelogin.php']`，只有 `.login-screen.active` 那个可见，
  因此选择器必须用 `.login-screen.active` 限定，否则会命中隐藏的其他语言表单
- 字段：username / password / passan（安全问题答案，passid=0 时隐藏）/ passid / lang
- **没有图形验证码**，登录表单也**没有 2FA 输入框**

登录后特征：
- 导航栏 `<span id="sp_signed"><a id="signed">签到</a></span>`
- 签到是点击链接触发的 JS/AJAX 请求，结果可能以 alert 弹窗或 DOM 更新呈现
- 其余链接 href 值带前导空格（如 `" /logout.php"`），用子串匹配不受影响
"""
from __future__ import annotations

import asyncio

from playwright.async_api import TimeoutError as PWTimeout

from core.events import Events
from .base import TaskError, register
from .nexusphp import NexusPHPTask


@register
class TTGTask(NexusPHPTask):
    id = "ttg"
    name = "TTG（听听歌）"
    description = "TTG(site-c.example) 每日签到：点击导航栏签到链接，兼容 alert / DOM 两种结果呈现"
    site = "https://site-c.example/index.php"

    # ---- 登录页（只操作当前语言的活动表单）----
    SEL_LOGIN_FORM = ".login-screen.active form[action='takelogin.php']"
    SEL_USERNAME = ".login-screen.active input[name='username']"
    SEL_PASSWORD = ".login-screen.active input[name='password']"
    SEL_ONECODE = ""                       # 登录表单没有 2FA 输入框
    SEL_CAPTCHA_IMG = ""                   # 登录页没有图形验证码
    SEL_CAPTCHA_INPUT = ""
    SEL_SUBMIT = ".login-screen.active input[type='submit']"

    # ---- 登录后标志 ----
    LOGGED_IN_MARKERS = (
        "#signed",
        "#sp_signed",
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
    )

    # ---- 签到 ----
    CHECKIN_URL = "https://site-c.example/index.php"
    SEL_CHECKIN_LINK = "#signed"

    ALREADY_KEYWORDS = ("已签到", "已经签到", "今日已签到", "已经签过", "签到过了")
    SUCCESS_KEYWORDS = ("签到成功", "恭喜", "获得", "奖励")
    FAIL_KEYWORDS = ("签到失败", "签到出错", "验证码错误", "不能重复签到")

    async def _signed_state(self) -> str:
        """读取签到入口容器 #sp_signed 的当前文本（签到后通常变为“已签到”）"""
        try:
            text = await self.ctx.browser.page.evaluate(
                "() => { const el = document.querySelector('#sp_signed');"
                " return el ? (el.innerText || '').trim() : ''; }"
            )
            return text or ""
        except Exception:
            return ""

    async def do_checkin(self) -> tuple[bool, str]:
        browser = self.ctx.browser
        url = (self.cfg.options.get("checkin_url") or self.CHECKIN_URL or self.cfg.url).strip()
        self.log(f"进入签到页: {url}")
        await browser.goto(url)
        await browser.wait_timeout(1000)
        if self.cfg.has_cf:
            passed = await self.ctx.cf.solve()
            if not passed:
                raise TaskError(
                    "Cloudflare 人机验证未通过（等待超时），可改用 attach 模式人工完成一次验证"
                )

        body = await self.page_text()
        signed_text = await self._signed_state()
        if self._has_kw(body, self.ALREADY_KEYWORDS) or self._has_kw(signed_text, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"

        page = browser.page
        link = page.locator(self.SEL_CHECKIN_LINK).first
        try:
            await link.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            if self._has_kw(body, self.SUCCESS_KEYWORDS):
                return True, "签到成功"
            self.log("未找到签到入口 #signed，页面片段: " + body[:200].replace("\n", " "))
            return False, "未找到签到入口（#signed），页面结构可能变化"

        # 点击前注册弹窗处理，避免 alert 阻塞后续操作，同时记录弹窗内容用于判定
        dialogs: list[str] = []

        async def _accept_dialog(dialog):
            try:
                await dialog.accept()
            except Exception:
                pass

        def _on_dialog(dialog):
            dialogs.append(dialog.message or "")
            asyncio.ensure_future(_accept_dialog(dialog))

        # 注意：handler 必须在结束时移除，否则同一浏览器实例多次运行会不断累积，
        # 一个 alert 被多个 handler 重复 accept（Playwright 会报 "already handled"）
        page.on("dialog", _on_dialog)
        try:
            self.log("点击签到链接（#signed）")
            await link.click()
            self.events.emit(Events.CHECKIN_CLICKED, task_id=self.id)
            await browser.wait_timeout(3000)
        finally:
            try:
                page.remove_listener("dialog", _on_dialog)
            except Exception:
                pass

        body = await self.page_text()
        signed_text = await self._signed_state()
        parts = [*(dialogs or []), signed_text, body]
        combined = " | ".join(p for p in parts if p)
        if dialogs:
            self.log("签到弹窗内容: " + " / ".join(dialogs))

        if self._has_kw(combined, self.SUCCESS_KEYWORDS):
            return True, "签到成功" + (f"（{dialogs[-1]}）" if dialogs else "")
        if self._has_kw(combined, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if self._has_kw(combined, self.FAIL_KEYWORDS):
            return False, "签到失败" + (f"（{dialogs[-1]}）" if dialogs else "")
        # 点击前签到入口确认存在，点击后消失是较强的完成信号
        if not await page.query_selector(self.SEL_CHECKIN_LINK):
            return True, "签到完成（签到入口已消失，未取到文字提示）"
        self.log("签到结果无法判定，页面片段: " + body[:200].replace("\n", " "))
        return False, "签到结果未识别（请查看运行截图）"
