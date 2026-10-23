"""验证码识别：把验证码图片交给 AI 识别。

**取图方式（按优先级）**：
1. **直接下载** `<img>` 的 `src`（用页面上下文的 cookie，验证码与登录会话绑定）
   —— 拿到原始图片字节，不受页面缩放/边框/渲染时机影响，最稳
2. 元素截图兜底（**只截验证码元素本身的范围**，不是整页截图）
   —— 适用于 canvas 绘制、图片跨域或下载失败的情况

AI 偶发 5xx/超时会自动重试（每次重新取图，避免用到过期验证码）。
"""
from __future__ import annotations

import base64
from typing import Optional

from .ai import AIClient
from .browser import BrowserController
from .events import EventBus, Events
from .logger import get_logger

# 常见图片格式魔数（判断下载到的确实是图片，而不是错误页 HTML）
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"\x00\x00\x01\x00")


def looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 100:
        return False
    if data[:4] == b"RIFF" and b"WEBP" in data[:16]:      # WEBP
        return True
    return any(data.startswith(magic) for magic in _IMAGE_MAGIC)


class CaptchaSolver:
    def __init__(self, ai: AIClient, browser: BrowserController, events: Optional[EventBus] = None):
        self.ai = ai
        self.browser = browser
        self.events = events or EventBus()
        self.logger = get_logger()

    # ---------- 取图 ----------
    async def _download_image(self, selector: str) -> Optional[bytes]:
        """直接下载验证码图片，失败返回 None。

        用 page.request 发起请求，自动带上页面上下文的 cookie，
        因此 session 绑定的验证码也能取到；不经过页面渲染，拿到的就是原图。
        """
        try:
            page = self.browser.page
        except Exception:
            # browser.page 是 property，浏览器没启动时抛 BrowserError；
            # getattr(..., None) 的默认值只对 AttributeError 生效，所以必须显式捕获
            return None
        try:
            src = await page.eval_on_selector(
                selector,
                "e => (e.currentSrc || e.src || e.getAttribute('src') || '').trim()",
            )
        except Exception as e:
            self.logger.warning(f"读取验证码图片地址失败: {e}")
            return None
        if not src:
            return None

        # data:image/...;base64,... 内联图片直接解码
        if src.startswith("data:image"):
            try:
                _, _, b64 = src.partition(",")
                data = base64.b64decode(b64)
                if looks_like_image(data):
                    self.logger.info(f"验证码内联图片解码成功（{len(data)} 字节）")
                    return data
            except Exception as e:
                self.logger.warning(f"解码内联验证码失败: {e}")
            return None

        try:
            resp = await page.request.get(src, timeout=15000)
            if not resp.ok:
                self.logger.warning(f"下载验证码图片失败: HTTP {resp.status}")
                return None
            data = await resp.body()
            if not looks_like_image(data):
                self.logger.warning("下载到的验证码不是图片（可能是错误页），改用元素截图")
                return None
            self.logger.info(f"验证码图片直接下载成功（{len(data)} 字节）")
            return data
        except Exception as e:
            self.logger.warning(f"下载验证码图片异常: {e}")
            return None

    async def fetch_image(self, selector: str) -> bytes:
        """取验证码图片字节：优先直接下载，失败退回元素截图（只截该元素范围）"""
        data = await self._download_image(selector)
        if data:
            return data
        self.logger.info("改用元素截图获取验证码（只截验证码图片范围）")
        return await self.browser.element_screenshot(selector)

    # ---------- 识别 ----------
    async def recognize_element(self, selector: str, prompt_key: str = "captcha",
                                retries: int = 3) -> str:
        """识别页面元素（验证码图片）中的内容。

        AI 接口偶发 5xx/超时（如 502 Bad Gateway）会自动重试；
        每次重试都重新取图，避免用到已经过期的验证码。
        """
        self.events.emit(Events.CAPTCHA_DETECTED, selector=selector)
        retries = max(1, int(retries))
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                img = await self.fetch_image(selector)
                code = self.ai.recognize_captcha(img, prompt_key)
                if code:
                    return code
                last_err = ValueError(f"AI 未能识别出验证码内容（选择器: {selector}）")
            except Exception as e:
                last_err = e
                self.logger.warning(f"验证码识别第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                await self.browser.wait_timeout(2000 * attempt)   # 退避后重试
        raise ValueError(f"验证码识别失败（已重试 {retries} 次）: {last_err}")

    async def recognize_bytes(self, img: bytes, prompt_key: str = "captcha",
                              retries: int = 3) -> str:
        """识别已有图片字节（同样带重试）"""
        retries = max(1, int(retries))
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                code = self.ai.recognize_captcha(img, prompt_key)
                if code:
                    return code
                last_err = ValueError("AI 未能识别出验证码内容")
            except Exception as e:
                last_err = e
                self.logger.warning(f"验证码识别第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                await self.browser.wait_timeout(2000 * attempt)
        raise ValueError(f"验证码识别失败（已重试 {retries} 次）: {last_err}")

    async def fill_captcha(self, input_selector: str, code: str) -> None:
        await self.browser.fill(input_selector, code)

    async def solve_and_fill(self, img_selector: str, input_selector: str,
                             prompt_key: str = "captcha") -> str:
        """识别并填入输入框"""
        code = await self.recognize_element(img_selector, prompt_key)
        await self.fill_captcha(input_selector, code)
        return code
