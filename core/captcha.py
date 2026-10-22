"""验证码识别：把页面中的验证码图片交给 AI 识别。"""
from __future__ import annotations

from typing import Optional

from .ai import AIClient
from .browser import BrowserController
from .events import EventBus, Events
from .logger import get_logger


class CaptchaSolver:
    def __init__(self, ai: AIClient, browser: BrowserController, events: Optional[EventBus] = None):
        self.ai = ai
        self.browser = browser
        self.events = events or EventBus()
        self.logger = get_logger()

    async def recognize_element(self, selector: str, prompt_key: str = "captcha",
                                retries: int = 3) -> str:
        """识别页面元素（验证码图片）中的内容。

        AI 接口偶发 5xx/超时（如 502 Bad Gateway）会自动重试；
        每次重试都重新截图，避免用到已经过期的验证码图。
        """
        self.events.emit(Events.CAPTCHA_DETECTED, selector=selector)
        retries = max(1, int(retries))
        last_err: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                img = await self.browser.element_screenshot(selector)
                code = self.ai.recognize_captcha(img, prompt_key)
                if code:
                    return code
                last_err = ValueError(f"AI 未能识别出验证码内容（选择器: {selector}）")
            except Exception as e:
                last_err = e
                self.logger.warning(f"验证码识别第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                await self.browser.wait_timeout(1500 * attempt)   # 退避后重试
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
                await self.browser.wait_timeout(1500 * attempt)
        raise ValueError(f"验证码识别失败（已重试 {retries} 次）: {last_err}")

    async def fill_captcha(self, input_selector: str, code: str) -> None:
        await self.browser.fill(input_selector, code)

    async def solve_and_fill(self, img_selector: str, input_selector: str,
                             prompt_key: str = "captcha") -> str:
        """识别并填入输入框"""
        code = await self.recognize_element(img_selector, prompt_key)
        await self.fill_captcha(input_selector, code)
        return code
