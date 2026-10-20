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

    async def recognize_element(self, selector: str, prompt_key: str = "captcha") -> str:
        """识别页面元素（验证码图片）中的内容"""
        self.events.emit(Events.CAPTCHA_DETECTED, selector=selector)
        img = await self.browser.element_screenshot(selector)
        code = self.ai.recognize_captcha(img, prompt_key)
        if not code:
            raise ValueError(f"AI 未能识别出验证码内容（选择器: {selector}）")
        return code

    async def recognize_bytes(self, img: bytes, prompt_key: str = "captcha") -> str:
        """识别已有图片字节"""
        code = self.ai.recognize_captcha(img, prompt_key)
        if not code:
            raise ValueError("AI 未能识别出验证码内容")
        return code

    async def fill_captcha(self, input_selector: str, code: str) -> None:
        await self.browser.fill(input_selector, code)

    async def solve_and_fill(self, img_selector: str, input_selector: str,
                             prompt_key: str = "captcha") -> str:
        """识别并填入输入框"""
        code = await self.recognize_element(img_selector, prompt_key)
        await self.fill_captcha(input_selector, code)
        return code
