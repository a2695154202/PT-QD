"""Cloudflare / 人机挑战处理：每秒实时探测 + 元素定位优先 + AI 局部截图兜底。

核心策略（针对实际使用中的问题反复调整）：

1. **绝不降级为被动等待**
   「请稍候…」只是挑战页的初始状态，几秒后页面**可能**才渲染出：
   - 需要点击的复选框（Turnstile / Verify you are human）
   - 需要拖动的滑块（拖动拼图）
   因此每一秒都必须重新探测页面上是否出现了可交互控件，
   探测到就立刻处理，而不是"等 10 秒再看看"。

2. **元素定位优先，不浪费 AI**
   能通过读取页面元素（复选框 / label / 按钮）确定点击坐标时直接点击；
   只有在页面没有可精确定位的元素时，才截图交给 AI 定位。

3. **AI 截图只截控件区域，并把控件滚到窗口正中间**
   截图前先把控件 scrollIntoView + 调整主窗口滚动位置，
   保证控件完整可见、像素坐标与鼠标坐标严格一致（scale=css）。

4. **滑块同样实时探测**
   识别到滑块按钮即用 AI 定位缺口（不可用时退化为拖到轨道末端），
   并用拟人化轨迹拖动。
"""
from __future__ import annotations

import time
from typing import Optional

from .ai import AIClient
from .browser import BrowserController
from .events import EventBus, Events
from .logger import get_logger
from .slider import human_track

# ---------------------------------------------------------------------------
# 页面特征
# ---------------------------------------------------------------------------
TITLE_MARKERS = [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "attention required",
    "正在进行安全验证",
    "请稍等",
    "请稍候",
]
# 挑战页正文特征（新版托管挑战会整页替换，且不一定有 iframe / 特征 id）
TEXT_MARKERS = [
    "正在进行安全验证",
    "验证您不是自动程序",
    "安全服务防护恶意自动程序",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies to continue",
]
# 判定"处于挑战中"的元素特征（包含 Turnstile 组件本身）
SELECTOR_MARKERS = [
    "#challenge-running",
    "#challenge-stage",
    "#challenge-form",
    "#cf-wrapper",
    "#cf-challenge-running",
    "iframe[src*='challenges.cloudflare.com']",
    "div[class*='cf-turnstile']",
    "div[class*='cf-browser-verification']",
]
# 判定" interstitial 挑战页正在展示"的元素特征（比 SELECTOR_MARKERS 更严格：
# 单纯的 Turnstile 组件残留不代表挑战还在进行）
INTERSTITIAL_SELECTORS = [
    "#challenge-running",
    "#challenge-stage",
    "#challenge-form",
    "#cf-wrapper",
    "#cf-challenge-running",
    "div.cf-browser-verification",
]

# ---------------------------------------------------------------------------
# 可交互控件
# ---------------------------------------------------------------------------
# 复选框 / 按钮：能精确定位点击点，无需 AI
CHECKBOX_SELECTORS = (
    "label.cb-lb",
    ".cb-lb",
    "input[type='checkbox']",
    "input[id*='checkbox']",
    "[role='checkbox']",
    "button",
)
# 主文档里的验证容器（其内部往往还嵌着 Turnstile iframe）
CONTAINER_SELECTORS = (
    "div.cf-turnstile",
    "div[class*='cf-turnstile']",
    "#challenge-stage",
    "#challenge-running",
    "#challenge-form",
    "#cf-wrapper",
    "div.cf-browser-verification",
)
# CF 验证 iframe（在主文档里能找到其位置）
CF_IFRAME_SELECTOR = "iframe[src*='challenges.cloudflare.com']"
# 滑块：成对定义（滑块按钮, 背景容器）——必须成对，否则会把容器当成按钮
SLIDER_PAIRS = (
    (".geetest_slider_button", ".geetest_slider"),
    (".geetest_slider", ".geetest_window"),
    (".nc_iconfont.btn_slide", ".nc_scale"),
    (".nc_scale", ".nc_wrapper"),
    (".ctp-slider", ".ctp-slider"),
    (".slider-btn", ".slider-container"),
    (".slider-button", ".slider-container"),
    ("[class*='slider'] [class*='btn']", "[class*='slider']"),
    ("[class*='slider'] [class*='button']", "[class*='slider']"),
    ("[class*='slider'] [class*='handle']", "[class*='slider']"),
    ("[class*='slider'] [class*='thumb']", "[class*='slider']"),
    ("div[draggable='true']", "[class*='slider']"),
)


class CFHandler:
    """Cloudflare 人机挑战处理。

    典型用法::

        if await cf.is_challenge():
            await cf.solve()
    """

    MAX_CLICKS = 3             # 单轮挑战最多点击次数
    MAX_SLIDES = 2             # 单轮挑战最多拖动滑块次数
    CLICK_INTERVAL = 2.0       # 两次点击的最小间隔（秒）
    SLIDE_INTERVAL = 4.0       # 两次滑动的最小间隔（秒）
    AI_INTERVAL = 3.0          # 两次 AI 定位的最小间隔（秒）
    BUDGET_RENEW_S = 15.0      # 超过这个时间没通过 → 重新给一次点击/滑动预算
    POLL_INTERVAL_MS = 1000    # 状态检测间隔（1 秒）

    def __init__(self, browser: BrowserController, ai: Optional[AIClient] = None,
                 events: Optional[EventBus] = None):
        self.browser = browser
        self.ai = ai
        self.events = events or EventBus()
        self.logger = get_logger()

    # ---------- 状态检测 ----------
    async def is_challenge(self) -> bool:
        """当前页面是否处于人机验证挑战（标题 / 特征元素 / 正文特征三重判定）"""
        page = self.browser.page
        try:
            title = (await page.title()).lower()
        except Exception:
            return False
        if any(m.lower() in title for m in TITLE_MARKERS):
            return True
        for sel in SELECTOR_MARKERS:
            try:
                if await page.query_selector(sel):
                    return True
            except Exception:
                continue
        try:
            text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
            )
            if text and any(m in text for m in TEXT_MARKERS):
                return True
        except Exception:
            pass
        return False

    async def _turnstile_pending(self) -> bool:
        """Turnstile 组件是否还在等待验证。

        判定依据：组件存在，但还没有拿到 token（验证通过时 CF 会写入
        `cf-turnstile-response` / `cf-token` 之类的隐藏域）。
        这是"页面正常但内嵌 Turnstile"场景下判断挑战是否结束的可靠信号。
        """
        # 1) 已经拿到 token → 验证已通过
        for sel in ("input[name='cf-turnstile-response']", "input#cf-token",
                    "input[name='cf-token']"):
            try:
                loc = self.browser.page.locator(sel).first
                if await loc.count() == 0:
                    continue
                value = await loc.input_value(timeout=1500)
                if value and value.strip():
                    return False
            except Exception:
                continue
        # 2) 没有 token，但组件还在 → 仍在等待
        return await self._locate_turnstile_widget() is not None

    async def _is_active_challenge(self) -> bool:
        """挑战是否**仍在进行中**（区别于"页面上还残留 Turnstile 组件"）。

        判定依据（按可靠性排序）：
        1. 标题是挑战页标题（"Just a moment…" / "请稍候…"）
        2. 正文含挑战文案（"正在进行安全验证" / "验证您不是自动程序"）
        3. 托管挑战容器还在，但页面主体尚未渲染出正常内容
        4. 内嵌 Turnstile 组件还在等待（没有拿到 token）

        注意：#challenge-stage 这类容器在验证通过后**仍可能留在 DOM 里**，
        所以不能只看容器是否存在，否则会永远认为"没通过"。
        """
        page = self.browser.page
        try:
            title = (await page.title()).lower()
        except Exception:
            return False
        if any(m.lower() in title for m in TITLE_MARKERS):
            return True
        try:
            text = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
            )
        except Exception:
            text = ""
        if text and any(m in text for m in TEXT_MARKERS):
            return True
        # 托管挑战容器仍在：只有页面主体还没渲染好时才算挑战中
        for sel in INTERSTITIAL_SELECTORS:
            try:
                if await page.query_selector(sel):
                    return not await self._page_ready()
            except Exception:
                continue
        # 内嵌 Turnstile：没有拿到 token 就还在等
        try:
            if await self._turnstile_pending():
                return True
        except Exception:
            pass
        return False

    async def _page_ready(self) -> bool:
        """挑战页跳转过程中的页面有效性校验，避免把"跳转间隙"误判为已通过"""
        page = self.browser.page
        try:
            if "__cf_chl" in page.url:
                return False
            title = (await page.title()).strip().lower()
            if not title or "just a moment" in title or "请稍候" in title:
                return False
            length = await page.evaluate(
                "() => document.body ? document.body.innerText.trim().length : 0"
            )
            return int(length or 0) > 30
        except Exception:
            return False

    # ---------- 通用小工具 ----------
    async def _frames(self) -> list:
        """主 frame + 所有子 frame（CF 验证控件通常在跨域 iframe 里）"""
        page = self.browser.page
        try:
            frames = list(page.frames)
            return frames or [page.main_frame]
        except Exception:
            return [page.main_frame]

    @staticmethod
    async def _visible(loc) -> bool:
        try:
            if await loc.count() == 0:
                return False
            return await loc.is_visible()
        except Exception:
            return False

    async def _center_and_box(self, locator) -> Optional[dict]:
        """把元素滚到**浏览器窗口正中间**，返回主视口坐标系下的矩形。

        坐标系说明（已用离线测试验证）：
        - Playwright 的 bounding_box() 始终相对**主 frame 视口**，
          即使元素位于跨域 iframe 内部，因此可直接用于鼠标点击与区域截图。
        - page.screenshot(clip=...) 的 clip 同样是**视口相对**坐标。
        """
        try:
            await locator.evaluate(
                "el => { try { el.scrollIntoView({block: 'center', inline: 'center'}); }"
                " catch (e) {} }"
            )
        except Exception:
            pass
        try:
            box = await locator.bounding_box()
        except Exception:
            box = None
        if box:
            # 元素在 iframe 内时，scrollIntoView 只滚动 iframe 自身，
            # 还需要调整主窗口滚动量，把控件真正推到窗口正中。
            try:
                vp = await self.browser.page.evaluate(
                    "() => ({w: window.innerWidth, h: window.innerHeight})"
                )
                dx = (box["x"] + box["width"] / 2) - vp["w"] / 2
                dy = (box["y"] + box["height"] / 2) - vp["h"] / 2
                if abs(dx) > 2 or abs(dy) > 2:
                    await self.browser.page.evaluate(
                        "([dx, dy]) => window.scrollBy(dx, dy)", [dx, dy]
                    )
                    await self.browser.wait_timeout(200)
                    box = await locator.bounding_box()
            except Exception:
                pass
        if not box:
            return None
        return {"x": box["x"], "y": box["y"],
                "width": box["width"], "height": box["height"]}

    # ---------- 控件定位 ----------
    async def _locate_checkbox(self) -> Optional[dict]:
        """寻找可精确点击的复选框 / 按钮（不需要 AI）。

        优先级：
        1. CF 验证 iframe 内的复选框（最常见）
        2. 主文档验证容器内部的复选框
        3. 仅有容器时返回容器本身（kind 区分能否做几何兜底点击）

        kind:
        - exact     : 元素本身就是要点的东西 → 直接元素点击
        - turnstile : Turnstile 控件 / CF iframe → 复选框通常在左侧，可几何兜底
        - container : 其它挑战容器（无明确复选框）→ 不做几何盲点
        """
        frames = await self._frames()
        # 1) CF 验证 iframe 内部
        for frame in frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            for sel in CHECKBOX_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if not await self._visible(loc):
                        continue
                    return {"frame": frame, "locator": loc, "selector": sel, "kind": "exact"}
                except Exception:
                    continue
        # 2) 主文档容器内部
        main = self.browser.page.main_frame
        for csel in CONTAINER_SELECTORS:
            try:
                cloc = main.locator(csel).first
                if not await self._visible(cloc):
                    continue
            except Exception:
                continue
            for sel in CHECKBOX_SELECTORS:
                try:
                    sloc = cloc.locator(sel).first
                    if await self._visible(sloc):
                        return {"frame": main, "locator": sloc,
                                "selector": f"{csel} >> {sel}", "kind": "exact"}
                except Exception:
                    continue
            # 容器内部还嵌着 CF iframe → 认定为 Turnstile 控件
            try:
                if await cloc.locator(CF_IFRAME_SELECTOR).count() > 0:
                    return {"frame": main, "locator": cloc,
                            "selector": csel, "kind": "turnstile"}
            except Exception:
                pass
            # 注意：真实 CF 会把 Turnstile 的 iframe 渲染在**闭合 shadow root** 里，
            # 主文档的 CSS 选择器查不到 iframe（只有 page.frames 能看到）。
            # 因此只要容器本身就是 turnstile，也必须按 Turnstile 处理，
            # 否则几何兜底点击不会触发，控件永远点不到。
            if "turnstile" in csel.lower():
                return {"frame": main, "locator": cloc,
                        "selector": csel, "kind": "turnstile"}
            # 其它挑战容器：只有"控件大小"（不是整页）才值得作为点击目标
            try:
                cbox = await cloc.bounding_box()
                vp = await self.browser.page.evaluate(
                    "() => ({w: window.innerWidth, h: window.innerHeight})"
                )
                if cbox and cbox["width"] < vp["w"] * 0.9 and cbox["height"] < vp["h"] * 0.9:
                    return {"frame": main, "locator": cloc,
                            "selector": csel, "kind": "container"}
            except Exception:
                pass
        # 3) 主文档里孤立的 CF 验证 iframe
        try:
            floc = main.locator(CF_IFRAME_SELECTOR).first
            if await self._visible(floc):
                return {"frame": main, "locator": floc,
                        "selector": CF_IFRAME_SELECTOR, "kind": "turnstile"}
        except Exception:
            pass
        return None

    async def _locate_slider(self) -> Optional[dict]:
        """寻找需要拖动的滑块控件。

        必须"成对"定位：先找到滑块按钮，再在它附近找到背景容器，
        否则容易把整个容器当成按钮，导致拖动距离计算错误。
        """
        frames = await self._frames()
        for frame in frames:
            for btn_sel, cont_sel in SLIDER_PAIRS:
                try:
                    loc = frame.locator(btn_sel).first
                    if not await self._visible(loc):
                        continue
                    box = await loc.bounding_box()
                    if not box or box["width"] < 8 or box["height"] < 8:
                        continue
                except Exception:
                    continue
                # 背景容器：优先同名/同类容器，且必须明显比按钮宽
                container = None
                try:
                    cloc = frame.locator(cont_sel).first
                    if await self._visible(cloc):
                        cbox = await cloc.bounding_box()
                        if cbox and cbox["width"] > box["width"] * 1.2:
                            container = cloc
                except Exception:
                    pass
                if container is None:
                    # 退而求其次：用按钮的父元素作为轨道
                    try:
                        ploc = loc.locator("xpath=..").first
                        if await self._visible(ploc):
                            pbox = await ploc.bounding_box()
                            if pbox and pbox["width"] > box["width"] * 1.2:
                                container = ploc
                    except Exception:
                        pass
                if container is None:
                    continue
                return {"frame": frame, "locator": loc, "selector": btn_sel,
                        "container": container}
        return None

    # ---------- 点击 ----------
    async def _click_locator(self, target: dict) -> tuple[bool, str]:
        """点击一个已定位的元素（优先元素级点击，失败则用元素坐标点击）"""
        page = self.browser.page
        loc = target["locator"]
        try:
            await loc.click(timeout=3000)
            return True, f"元素点击（{target['selector']}）"
        except Exception:
            pass
        # 元素被遮挡 / 不可直接点击时，退化为按元素自身坐标点击
        try:
            box = await self._center_and_box(loc)
            if not box or box["width"] < 4 or box["height"] < 4:
                return False, ""
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y, steps=10)
            await self.browser.wait_timeout(120)
            await page.mouse.click(x, y)
            return True, f"元素坐标点击（{x:.0f}, {y:.0f}）"
        except Exception:
            return False, ""

    async def _ai_click(self, target: dict) -> tuple[bool, str]:
        """元素无法精确定位时：只截控件区域交给 AI 定位并立即点击"""
        if not (self.ai and getattr(self.ai, "available", False)
                and getattr(self.ai.cfg, "proactive", True)):
            return False, ""
        page = self.browser.page
        try:
            rect = await self._target_rect(target)
            if not rect or rect["width"] < 8 or rect["height"] < 8:
                return False, ""
            clip = {"x": rect["x"], "y": rect["y"],
                    "width": rect["width"], "height": rect["height"]}
            self.events.emit(Events.CHALLENGE_DETECTED)
            img = await self.browser.clip_screenshot(clip, name="cf_challenge")
            result = self.ai.vision_json(img, self._prompt()) or {}
            click = result.get("click")
            if not click:
                self.logger.info("AI 判断当前无需点击（可能已自动通过或正在验证）")
                return False, ""
            cx = rect["x"] + float(click[0])
            cy = rect["y"] + float(click[1])
            await page.mouse.move(cx, cy, steps=10)
            await self.browser.wait_timeout(100)
            await page.mouse.click(cx, cy)
            return True, f"AI 定位点击（{cx:.0f}, {cy:.0f}）"
        except Exception as e:
            self.logger.warning(f"AI 定位挑战控件失败: {e}")
            return False, ""

    async def _target_rect(self, target: dict) -> Optional[dict]:
        """控件区域：Turnstile 优先取内部 iframe 的矩形（更精确）"""
        if target.get("kind") == "turnstile":
            return await self._turnstile_rect(target)
        return await self._center_and_box(target["locator"])

    def _prompt(self) -> str:
        prompts = getattr(self.ai.cfg, "prompts", {}) if self.ai else {}
        return prompts.get("challenge") or (
            "这是 Cloudflare 人机验证控件的截图。若图中存在尚未勾选的复选框，"
            "返回其像素坐标 JSON：{\"click\": [x, y]}；若已勾选或无可点击项返回 {\"click\": null}。"
        )

    async def _try_click(self, allow_ai: bool = True) -> tuple[bool, str]:
        """尝试处理"需要点击"的验证控件。

        顺序：元素定位优先 → AI 局部截图兜底 → 几何兜底（仅 Turnstile 控件）。
        allow_ai=False 时跳过 AI，避免每秒都请求 AI。
        """
        target = await self._locate_checkbox()
        if not target:
            return False, ""

        if target["kind"] == "exact":
            ok, msg = await self._click_locator(target)
            if ok:
                return True, msg
            # 元素点不动 → 退化为 AI 定位
            if allow_ai:
                return await self._ai_click(target)
            return False, ""

        # 只有容器：优先 AI 定位（能区分"需要点"与"正在验证中"）
        if allow_ai:
            ok, msg = await self._ai_click(target)
            if ok:
                return True, msg

        # 几何兜底：Turnstile 复选框位于控件左侧
        if target["kind"] == "turnstile":
            try:
                rect = await self._turnstile_rect(target)
                if not rect:
                    return False, ""
                x = rect["x"] + min(30, rect["width"] / 2)
                y = rect["y"] + rect["height"] / 2
                await self.browser.page.mouse.move(x, y, steps=10)
                await self.browser.wait_timeout(100)
                await self.browser.page.mouse.click(x, y)
                return True, f"控件左侧坐标点击（{x:.0f}, {y:.0f}）"
            except Exception:
                return False, ""
        return False, ""

    async def _turnstile_rect(self, target: dict) -> Optional[dict]:
        """Turnstile 控件的实际位置：优先取内部 CF iframe 的矩形（更精确）"""
        loc = target["locator"]
        try:
            inner = loc.locator(CF_IFRAME_SELECTOR).first
            if await self._visible(inner):
                rect = await self._center_and_box(inner)
                if rect:
                    return rect
        except Exception:
            pass
        return await self._center_and_box(loc)

    # ---------- 滑块 ----------
    @staticmethod
    async def _box_of(loc) -> Optional[dict]:
        """只取矩形，不做任何滚动（用于在同一次滚动后统一读坐标）"""
        try:
            box = await loc.bounding_box()
        except Exception:
            return None
        if not box:
            return None
        return {"x": box["x"], "y": box["y"],
                "width": box["width"], "height": box["height"]}

    async def _try_slide(self, target: dict) -> tuple[bool, str]:
        """处理需要拖动的滑块：AI 定位缺口 → 拟人化拖动

        坐标要点：先滚动定位（只做一次），再统一读取滑块与轨道的矩形，
        避免二次滚动导致先前读到的坐标失效。
        """
        page = self.browser.page
        loc = target["locator"]
        container = target.get("container")
        try:
            # 1) 先把控件滚到窗口正中（容器优先，保证整条轨道可见）
            await self._center_and_box(container if container is not None else loc)
            await self.browser.wait_timeout(200)
            # 2) 统一读坐标
            box = await self._box_of(loc)
            if not box or box["width"] < 8:
                return False, ""
            cbox = await self._box_of(container) if container is not None else None
            if not cbox:
                return False, ""

            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2

            # 3) 计算拖动距离
            distance = 0
            gap = None
            if self.ai and getattr(self.ai, "available", False) \
                    and getattr(self.ai.cfg, "proactive", True):
                try:
                    clip = {"x": cbox["x"], "y": cbox["y"],
                            "width": cbox["width"], "height": cbox["height"]}
                    img = await self.browser.clip_screenshot(clip, name="cf_slider")
                    gap = self.ai.locate_slider_gap(img)
                    self.logger.info(f"AI 定位滑块缺口 x = {gap}px")
                except Exception as e:
                    self.logger.warning(f"AI 定位滑块缺口失败: {e}")

            if gap is not None and 0 <= gap <= cbox["width"]:
                # 拼图块左边缘对齐缺口左边缘
                distance = int((cbox["x"] + gap) - box["x"])
            else:
                # 无缺口信息时拖到轨道末端
                distance = int(cbox["width"] - box["width"])
            distance = max(0, min(distance, int(cbox["width"])))

            if distance <= 0:
                return False, ""

            await page.mouse.move(start_x, start_y, steps=8)
            await self.browser.wait_timeout(120)
            await page.mouse.down()
            await self.browser.wait_timeout(120)
            cur = 0
            for step in human_track(distance):
                cur += step
                await page.mouse.move(start_x + cur, start_y, steps=1)
                await self.browser.wait_timeout(12)
            await self.browser.wait_timeout(150)
            await page.mouse.up()
            return True, f"滑块拖动 {distance}px（{target['selector']}）"
        except Exception as e:
            self.logger.warning(f"滑块拖动失败: {e}")
            return False, ""

    # ---------- Turnstile 专用 ----------
    async def _locate_turnstile_widget(self):
        """定位 Turnstile 控件元素。

        真实结构是 <div class="cf-turnstile" data-sitekey="..."><div><div></div>
        <input type="hidden" name="cf-turnstile-response"></div></div>，
        而 **iframe 被渲染在闭合 shadow root 里**，主文档的 CSS 选择器查不到它
        （实测 `iframe[src*='challenges.cloudflare.com']` 命中数为 0），
        只有 `page.frames` 能看到那些跨域 frame。

        因此这里返回**容器元素**（用于定位与点击位置）；
        iframe 内的真实复选框元素则通过 frame 遍历去找。
        """
        page = self.browser.page
        try:
            floc = page.locator(CF_IFRAME_SELECTOR).first
            if await self._visible(floc):
                return floc
        except Exception:
            pass
        try:
            for csel in CONTAINER_SELECTORS:
                cloc = page.locator(csel).first
                if not await self._visible(cloc):
                    continue
                inner = cloc.locator(CF_IFRAME_SELECTOR).first
                if await self._visible(inner):
                    return inner
                return cloc
        except Exception:
            pass
        return None

    # 兼容旧名称
    async def _locate_turnstile_iframe(self):
        return await self._locate_turnstile_widget()

    async def _click_turnstile(self) -> tuple[bool, str]:
        """点击 Turnstile 复选框：元素优先 → 几何兜底。

        元素优先的含义：直接在 CF 的跨域 iframe 里（或容器内）找真正的复选框元素并点击，
        不需要消耗 AI；找不到元素时再按"复选框在控件左侧"的几何位置点击。
        """
        page = self.browser.page
        # 1) 跨域 iframe 内的真实复选框元素（Playwright 可直接操作跨域 frame）
        try:
            for frame in await self._frames():
                if "challenges.cloudflare.com" not in (frame.url or ""):
                    continue
                for sel in CHECKBOX_SELECTORS:
                    loc = frame.locator(sel).first
                    if not await self._visible(loc):
                        continue
                    try:
                        await loc.click(timeout=3000)
                        return True, f"Turnstile 复选框元素点击（{sel}）"
                    except Exception:
                        box = await self._box_of(loc)
                        if box and box["width"] > 2:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            await page.mouse.move(x, y, steps=8)
                            await self.browser.wait_timeout(100)
                            await page.mouse.click(x, y)
                            return True, f"Turnstile 复选框坐标点击（{x:.0f}, {y:.0f}）"
        except Exception as e:
            self.logger.warning(f"在 CF iframe 内查找复选框失败: {e}")

        # 2) 主文档容器内的复选框元素（无 iframe 的降级渲染）
        try:
            for csel in CONTAINER_SELECTORS:
                cloc = page.locator(csel).first
                if not await self._visible(cloc):
                    continue
                for sel in CHECKBOX_SELECTORS:
                    sloc = cloc.locator(sel).first
                    if not await self._visible(sloc):
                        continue
                    try:
                        await sloc.click(timeout=3000)
                        return True, f"Turnstile 复选框元素点击（{csel} >> {sel}）"
                    except Exception:
                        box = await self._box_of(sloc)
                        if box and box["width"] > 2:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            await page.mouse.move(x, y, steps=8)
                            await self.browser.wait_timeout(100)
                            await page.mouse.click(x, y)
                            return True, f"Turnstile 复选框坐标点击（{x:.0f}, {y:.0f}）"
        except Exception as e:
            self.logger.warning(f"在主文档查找复选框失败: {e}")

        # 3) 几何兜底：点击控件左侧
        try:
            floc = await self._locate_turnstile_widget()
            if not floc:
                return False, ""
            rect = await self._center_and_box(floc)
            if not rect or rect["width"] < 10:
                return False, ""
            x = rect["x"] + min(30, rect["width"] / 2)
            y = rect["y"] + rect["height"] / 2
            await page.mouse.move(x, y, steps=10)
            await self.browser.wait_timeout(100)
            await page.mouse.click(x, y)
            return True, f"Turnstile 控件左侧坐标点击（{x:.0f}, {y:.0f}）"
        except Exception as e:
            self.logger.warning(f"Turnstile 几何点击失败: {e}")
            return False, ""

    async def _ai_click_turnstile(self, floc) -> tuple[bool, str]:
        """Turnstile 兜底：只截控件区域交给 AI 定位复选框"""
        if not (self.ai and getattr(self.ai, "available", False)):
            return False, ""
        page = self.browser.page
        try:
            rect = await self._center_and_box(floc)
            if not rect or rect["width"] < 10:
                return False, ""
            clip = {"x": rect["x"], "y": rect["y"],
                    "width": rect["width"], "height": rect["height"]}
            img = await self.browser.clip_screenshot(clip, name="turnstile")
            result = self.ai.vision_json(img, self._prompt()) or {}
            click = result.get("click")
            if not click:
                self.logger.info("AI 判断当前无需点击（可能已自动通过或正在验证）")
                return False, ""
            cx = rect["x"] + float(click[0])
            cy = rect["y"] + float(click[1])
            await page.mouse.move(cx, cy, steps=10)
            await self.browser.wait_timeout(80)
            await page.mouse.click(cx, cy)
            return True, f"AI 定位 Turnstile 复选框并点击（{cx:.0f}, {cy:.0f}）"
        except Exception as e:
            self.logger.warning(f"AI 定位 Turnstile 失败: {e}")
            return False, ""

    async def solve_turnstile(self, timeout_s: int = 40,
                              is_done=None) -> bool:
        """通过 Cloudflare Turnstile：**每 1 秒**探测，控件就绪立即处理。

        处理顺序：跨域 iframe 内的元素点击 → 几何兜底 → AI 定位。
        is_done: 可选的异步回调，返回 True 表示已经通过（如签到表单已提交）。
        """
        deadline = time.monotonic() + timeout_s
        attempts = 0
        last_click = 0.0
        started = time.monotonic()
        self.logger.info(
            f"开始处理 Turnstile：每 1 秒探测一次控件加载状态，最长 {timeout_s}s"
        )

        while time.monotonic() < deadline:
            elapsed = time.monotonic() - started

            if is_done is not None:
                try:
                    if await is_done():
                        self.logger.info(f"[Turnstile {elapsed:.0f}s] 验证已通过")
                        return True
                except Exception:
                    pass

            floc = await self._locate_turnstile_iframe()
            ready = floc is not None
            if ready and attempts < 3 and (time.monotonic() - last_click) >= 2.0:
                attempts += 1
                ok, method = await self._click_turnstile()
                if ok:
                    last_click = time.monotonic()
                    self.logger.info(
                        f"[Turnstile {elapsed:.0f}s] 已处理验证控件（{method}，第 {attempts} 次）"
                    )
                    await self.browser.wait_timeout(1000)
                    continue
                # 元素点不动 → AI 兜底
                ok, method = await self._ai_click_turnstile(floc)
                if ok:
                    last_click = time.monotonic()
                    self.logger.info(
                        f"[Turnstile {elapsed:.0f}s] 已处理验证控件（{method}，第 {attempts} 次）"
                    )
                    await self.browser.wait_timeout(1000)
                    continue
                self.logger.info(
                    f"[Turnstile {elapsed:.0f}s] 控件就绪={ready}，第 {attempts} 次无需点击"
                )
            else:
                self.logger.info(
                    f"[Turnstile {elapsed:.0f}s] 控件就绪={ready} 已尝试={attempts}次"
                )

            await self.browser.wait_timeout(self.POLL_INTERVAL_MS)

        if is_done is not None:
            try:
                return await is_done()
            except Exception:
                pass
        return False

    # ---------- 主流程 ----------
    async def wait_pass(self, timeout_s: int = 30, poll_ms: int = POLL_INTERVAL_MS) -> bool:
        """仅等待（不点击）直到挑战消失，需连续 2 次确认，避免跳转间隙误判"""
        self.logger.info(f"等待人机验证自动通过（最长 {timeout_s}s）")
        stable = 0
        rounds = max(1, timeout_s * 1000 // poll_ms)
        for _ in range(rounds):
            if await self._is_active_challenge() or not await self._page_ready():
                stable = 0
            else:
                stable += 1
                if stable >= 2:
                    return True
            await self.browser.wait_timeout(poll_ms)
        return False

    async def handle(self, timeout_s: int = 60) -> bool:
        """实时处理人机挑战：**每一秒**重新探测页面，通过即返回。

        探测顺序：
        1. 挑战是否已消失 → 立即返回成功
        2. 页面上是否出现**滑块**控件 → 立即拖动
        3. 页面上是否出现**可点击**控件 → 元素定位优先点击，必要时 AI 兜底
        4. 都没有 → 等待 1 秒后继续探测（绝不改成低频重探）
        """
        if not await self.is_challenge():
            return True

        start = time.monotonic()
        deadline = start + timeout_s
        clicks = 0            # 本轮点击次数
        slides = 0            # 本轮滑动次数
        last_click = 0.0
        last_slide = 0.0
        last_ai = 0.0
        self.logger.info(
            f"检测到人机验证挑战，开始实时处理（每 1 秒探测一次点击/滑动控件，最长 {timeout_s}s）"
        )

        while time.monotonic() < deadline:
            elapsed = time.monotonic() - start
            now = time.monotonic()

            # 1) 通过了就立即返回
            if not await self._is_active_challenge():
                self.logger.info(f"[CF {elapsed:.0f}s] 挑战已通过")
                return True

            acted = False

            # 点击/滑动预算可续期：距上次操作很久仍没通过时，
            # 说明页面可能换了一个新控件，重新给一次机会
            if clicks and (now - last_click) >= self.BUDGET_RENEW_S:
                self.logger.info("[CF] 距上次操作已超过阈值，重新获得一次点击预算")
                clicks = 0
            if slides and (now - last_slide) >= self.BUDGET_RENEW_S:
                slides = 0

            # 2) 滑块控件（拖动）
            if slides < self.MAX_SLIDES and (now - last_slide) >= self.SLIDE_INTERVAL:
                slider = await self._locate_slider()
                if slider:
                    ok, method = await self._try_slide(slider)
                    if ok:
                        slides += 1
                        last_slide = time.monotonic()
                        acted = True
                        self.logger.info(
                            f"[CF {elapsed:.0f}s] 已处理滑块（{method}，第 {slides} 次）"
                        )
                        await self.browser.wait_timeout(1500)
                        continue

            # 3) 可点击控件
            if clicks < self.MAX_CLICKS and (now - last_click) >= self.CLICK_INTERVAL:
                allow_ai = (now - last_ai) >= self.AI_INTERVAL
                clicked, method = await self._try_click(allow_ai=allow_ai)
                if allow_ai:
                    last_ai = time.monotonic()
                if clicked:
                    clicks += 1
                    last_click = time.monotonic()
                    acted = True
                    self.logger.info(
                        f"[CF {elapsed:.0f}s] 已点击验证控件（{method}，第 {clicks} 次）"
                    )
                    await self.browser.wait_timeout(1200)
                    continue

            if not acted:
                self.logger.info(
                    f"[CF {elapsed:.0f}s] 仍在验证中（点击 {clicks} 次 / 滑动 {slides} 次），"
                    "继续每秒探测可交互控件"
                )
            await self.browser.wait_timeout(self.POLL_INTERVAL_MS)

        ok = not await self._is_active_challenge()
        if ok:
            self.logger.info("挑战已通过")
        else:
            self.logger.warning(
                f"[CF {timeout_s}s] 仍未通过：该站点对自动化浏览器校验较严，"
                "可改用 attach 模式人工完成一次验证（cf_clearance 会保存在用户目录中复用）"
            )
        return ok

    async def handle_with_ai_agent(self, timeout_s: int = 60) -> bool:
        """内置兜底：自动处理失败后，让 AI 模型看图自己驱动浏览器过验证。

        与 MCP 是同一套工具（core.browser_tools），区别只在于调用者是
        程序内置的视觉模型，无需用户安装任何 MCP 客户端。
        """
        # 先按常规策略实时处理一遍
        if await self.handle(timeout_s):
            return True
        if not (self.ai and getattr(self.ai, "available", False)):
            self.logger.info("AI 未启用，无法启用 AI 托管浏览器")
            return False

        from .browser_agent import BrowserAgent
        from .browser_tools import BrowserToolbox
        self.logger.info("自动处理失败，切换到 AI 托管模式（AI 看图驱动浏览器）")
        toolbox = BrowserToolbox(self.browser, self)
        agent = BrowserAgent(self.ai, toolbox)
        try:
            ok = await agent.drive(goal="通过 Cloudflare 人机验证")
        except Exception as e:
            self.logger.warning(f"AI 托管执行异常: {e}")
            return False
        if ok:
            self.logger.info("AI 托管成功通过验证")
        else:
            self.logger.warning(
                "AI 托管仍未通过。可安装 MCP 客户端让更强大的 AI 接管浏览器"
                "（运行 `python main.py mcp` 启动浏览器 MCP 服务端）"
            )
        return ok

    async def solve(self, timeout_s: int = 60) -> bool:
        """统一入口：常规实时处理 → 失败后按配置切换 AI 托管 / MCP 兜底。

        任务代码应调用本方法，而不是直接调 handle()，
        这样 CF 过不去时能自动升级到 AI 驱动浏览器。
        """
        use_agent = bool(self.ai and getattr(self.ai, "available", False)
                         and getattr(self.ai.cfg, "agent_fallback", True))
        if use_agent:
            return await self.handle_with_ai_agent(timeout_s)
        return await self.handle(timeout_s)
