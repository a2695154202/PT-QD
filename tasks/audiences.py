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

from core.desktop import DesktopController
from core.desktop_agent import DesktopAgent

from .base import TaskResult, register
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
        if not self.use_desktop:
            return await super().run()
        return await self._run_desktop()

    async def _run_desktop(self) -> TaskResult:
        self.events.emit("task_started", task_id=self.id, task_name=self.name)
        self.log(f"===== 任务 [{self.name}] 开始（桌面模式：正常浏览器 + 识图点击）=====")
        self.log("Cloudflare 对自动化浏览器校验严，改用你平时那个正常浏览器，像真人一样看图点击")

        if not (self.ctx.ai and getattr(self.ctx.ai, "available", False)):
            msg = "桌面模式需要配置 AI（识图点击依赖视觉模型），请先在全局设置里配置 AI 接口"
            self.log(msg)
            self.events.emit("task_error", task_id=self.id, error=msg)
            return TaskResult(self.id, False, msg)

        desktop = DesktopController()
        agent = DesktopAgent(self.ctx.ai, desktop)

        # 账号密码（未登录时 AI 要用）
        cred = ""
        if self.cfg.username and self.cfg.password:
            cred = (f"\n\n账号信息（仅在需要登录时使用）：\n"
                    f"用户名：{self.cfg.username}\n密码：{self.cfg.password}\n"
                    f"（若页面有两步验证码输入框，留空即可，该账号未开启 2FA）")

        goal = self.DESKTOP_GOAL + cred

        try:
            # 用正常浏览器打开签到页（不带自动化标志 / 调试端口）
            DesktopController.open_browser(self.CHECKIN_URL)
            self.log("已用正常浏览器打开签到页，等待页面加载…")
            import time
            time.sleep(6)   # 等浏览器启动 + 页面渲染 + CF 初步判定

            ok, message = await agent.drive(
                goal=goal,
                window_title="",          # 匹配任意浏览器窗口
                max_steps=int(self.cfg.options.get("desktop_max_steps", 20)),
            )
        except Exception as e:
            self.logger.exception(f"桌面模式执行异常: {e}")
            self.events.emit("task_error", task_id=self.id, error=str(e))
            return TaskResult(self.id, False, f"任务异常: {e}")

        # 归一化消息（不要把「获得/爆米花」这类常驻词当成成功）
        message = self._normalize_message(message)
        self.events.emit("task_finished", task_id=self.id, success=ok)
        self.log(f"===== 任务 [{self.name}] 结束: {message} =====")
        return TaskResult(self.id, ok, message)

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
