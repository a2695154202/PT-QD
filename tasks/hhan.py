"""憨憨（HHCLUB / site-g.example）每日签到任务。

站点是 NexusPHP（HHan 皮肤，实测登录页源码）：
- 登录页 https://site-g.example/login.php，表单 `form[action='takelogin.php']`
  - 字段：username / password / **two_step_code**（两步验证，"如有设置必须填写"）
          / imagestring（图形验证码）/ imagehash（hidden）
  - 提交按钮：`input[type='submit'][value='登录']`（另有 type=reset 的「重置」）
  - 高级选项（logout/securelogin/ssl/trackerssl）保持默认不动
- 登录成功后：打开 https://site-g.example/attendance.php **并停留 5 秒即完成签到**
  （NexusPHP attendance 插件在页面加载时即完成签到，无需再点按钮；
   页面上有签到按钮时也会顺手点一下作兜底）

实测：未登录访问 attendance.php 只返回一个空壳页面（无正文），
因此「正文为空」直接判定为未登录/页面异常，不会误报成功。

结果判定文案（NexusPHP attendance 插件通用）：
- 成功：签到成功 / 签到完成 / 恭喜
- 已签到：已签到 / 今日已签到 / 您今天已经签到 / 不能重复签到 / 明天再来
"""
from __future__ import annotations

from core.events import Events
from .base import register
from .nexusphp import NexusPHPTask


@register
class HHanTask(NexusPHPTask):
    id = "hhan"
    name = "憨憨（HHCLUB）"
    description = "HHCLUB(site-g.example) 每日签到：NexusPHP + 图形验证码 + 可选两步验证"
    site = "https://site-g.example/"

    # ---- 登录页（实测源码）----
    SEL_LOGIN_FORM = "form[action='takelogin.php']"
    SEL_USERNAME = "form[action='takelogin.php'] input[name='username']"
    SEL_PASSWORD = "form[action='takelogin.php'] input[name='password']"
    # 两步验证字段名是 two_step_code（站点提示：如有设置必须填写）
    SEL_ONECODE = "form[action='takelogin.php'] input[name='two_step_code']"
    SEL_CAPTCHA_IMG = "img[src*='image.php?action=regimage']"
    SEL_CAPTCHA_INPUT = "form[action='takelogin.php'] input[name='imagestring']"
    SEL_SUBMIT = "form[action='takelogin.php'] input[type='submit']"

    # ---- 登录后标志 ----
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
        "a[href*='attendance.php']",
    )

    # ---- 签到页（打开并停留 5 秒即完成）----
    CHECKIN_URL = "https://site-g.example/attendance.php"
    DEFAULT_STAY_SECONDS = 5

    # 兜底用的签到按钮候选（该站一般不用点，页面加载即签到）
    CHECKIN_BTN_CANDIDATES = (
        "input[type='submit'][value*='签到']",
        "input[type='button'][value*='签到']",
        "button:has-text('签到')",
        "#signin",
        "form[action*='attendance'] input[type='submit']",
    )

    # ---- 结果关键词 ----
    ALREADY_KEYWORDS = ("已签到", "已经签到", "今日已签到", "您今天已经签到",
                        "不能重复签到", "明天再来", "明日再来")
    SUCCESS_KEYWORDS = ("签到成功", "签到完成", "恭喜", "签到获得", "获得奖励")
    FAIL_KEYWORDS = ("签到失败", "签到出错", "无法签到", "操作失败")

    # ---------- 签到：打开 attendance.php 并停留 N 秒 ----------
    async def do_checkin(self) -> tuple[bool, str]:
        browser = self.ctx.browser
        url = (self.cfg.options.get("checkin_url") or self.CHECKIN_URL).strip()
        try:
            stay = int(self.cfg.options.get("stay_seconds", self.DEFAULT_STAY_SECONDS) or 0)
        except (TypeError, ValueError):
            stay = self.DEFAULT_STAY_SECONDS
        stay = max(1, stay)

        self.log(f"进入签到页: {url}")
        await browser.goto(url)
        await browser.wait_timeout(1500)
        if self.cfg.has_cf:
            await self.handle_challenge_if_any()

        body = await self.page_text()
        # 实测：未登录/异常时 attendance.php 是空白页，直接失败，不猜成功
        if not body.strip():
            self.log("签到页正文为空（未登录或页面结构变化），页面标题: "
                     + await self._safe_title())
            return False, "签到页为空（可能未登录或页面结构变化）"

        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if self._has_kw(body, self.FAIL_KEYWORDS):
            return False, "签到失败（页面出现失败提示）"

        # 兜底：页面上若仍有签到按钮就点一下（本站在页面加载时即签到，通常点不到）
        btn_sel = await self._find_checkin_button()
        if btn_sel:
            self.log(f"页面上还有签到按钮，点击兜底: {btn_sel}")
            try:
                await browser.click(btn_sel)
                self.events.emit(Events.CHECKIN_CLICKED, task_id=self.id)
                await browser.wait_timeout(1500)
            except Exception as e:
                self.logger.warning(f"点击签到按钮失败: {e}")

        # 按站点机制：停留在签到页即完成签到
        self.log(f"在签到页停留 {stay} 秒（打开 attendance.php 即完成签到）")
        await browser.wait_timeout(stay * 1000)

        body = await self.page_text()
        if self._has_kw(body, self.SUCCESS_KEYWORDS):
            return True, "签到成功"
        if self._has_kw(body, self.ALREADY_KEYWORDS):
            return True, "今日已签到（无需重复）"
        if self._has_kw(body, self.FAIL_KEYWORDS):
            return False, "签到失败（页面出现失败提示）"

        # 文案没匹配上时：页面正常渲染 + 仍在登录态 → 按「打开即签到」的站点机制视为完成
        if body.strip() and await self.is_logged_in():
            return True, f"签到完成（已在 attendance.php 停留 {stay} 秒）"

        self.log("签到结果无法从页面文本判定，页面片段: " + body[:200].replace("\n", " "))
        return False, "签到结果未识别（请查看运行截图）"

    async def _safe_title(self) -> str:
        try:
            return await self.ctx.browser.title()
        except Exception:
            return ""
