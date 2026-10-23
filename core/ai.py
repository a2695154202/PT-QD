"""AI 客户端：OpenAI 兼容接口（用户自行配置）。

用于：验证码图片识别、滑块缺口定位、CF 挑战分析等。
所有 AI 调用都收敛到 AIClient，任务代码不直接依赖具体厂商。
"""
from __future__ import annotations

import base64
import re
from typing import Optional

import requests

from .config import AIConfig
from .logger import get_logger


class AIError(RuntimeError):
    pass


class AIClient:
    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self.logger = get_logger()

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.api_key and self.cfg.base_url)

    def _post(self, payload: dict, timeout: Optional[int] = None) -> dict:
        if not self.available:
            raise AIError("AI 未启用或未配置 api_key/base_url，请先在 config/config.yaml 中配置")
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout or self.cfg.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise AIError(f"AI 请求失败: {e}") from e

    def chat(self, prompt: str, system: str = "", timeout: Optional[int] = None) -> str:
        """纯文本对话"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        data = self._post({"model": self.cfg.model, "messages": messages}, timeout)
        return self._extract(data)

    def vision(self, image: bytes | str, prompt: str, timeout: Optional[int] = None) -> str:
        """图片识别。image 为 bytes 或 base64 字符串"""
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode("ascii")
        else:
            b64 = image
        model = self.cfg.vision_model or self.cfg.model
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }]
        data = self._post({"model": model, "messages": messages}, timeout)
        return self._extract(data)

    @staticmethod
    def _extract(data: dict) -> str:
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise AIError(f"AI 响应格式异常: {str(data)[:300]}") from e

    # ---- 针对签到场景的封装 ----
    def recognize_captcha(self, image: bytes, prompt_key: str = "captcha") -> str:
        """识别图形验证码，返回纯验证码文本"""
        prompts = self.cfg.prompts if isinstance(self.cfg.prompts, dict) else {}
        # 用户自定义 prompts 时可能缺键：逐级兜底，不能直接下标取
        prompt = (prompts.get(prompt_key) or prompts.get("captcha")
                  or "请只输出图片中的验证码内容，不要任何解释。")
        text = self.vision(image, prompt)
        # 去掉空白和常见干扰字符，只保留字母数字
        code = re.sub(r"[^A-Za-z0-9]", "", text)
        self.logger.info(f"AI 识别验证码: {text!r} -> {code!r}")
        return code
