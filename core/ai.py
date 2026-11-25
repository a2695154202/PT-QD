"""AI 客户端：OpenAI 兼容接口（用户自行配置）。

用于：验证码图片识别、滑块缺口定位、CF 挑战分析等。
所有 AI 调用都收敛到 AIClient，任务代码不直接依赖具体厂商。

**多供应商主备**：`config.yaml` 的 `ai.providers` 是列表，**顺序即优先级**，
第 1 个是主用，后面都是备用。主用超时 / 401 / 额度用尽 / 返回格式异常，
会自动切到下一个，并把失败的线路冷却 `failover_cooldown` 秒，避免每次识别都先撞一遍墙。
本次运行里哪个线路成功了，后续调用就直接用它（黏住），直到它也失败。
"""
from __future__ import annotations

import base64
import re
import time
from typing import Callable, Optional

import requests

from . import net
from .config import AIConfig, AIProvider, _as_int
from .logger import get_logger


class AIError(RuntimeError):
    pass


class AIClient:
    def __init__(self, cfg: AIConfig):
        self.cfg = cfg
        self.logger = get_logger()
        self._cooldown: dict[str, float] = {}     # 供应商 key -> 恢复时间戳
        self._active: Optional[str] = None        # 当前在用的供应商 key

    # ---------- 供应商 ----------
    @staticmethod
    def _key(p: AIProvider) -> str:
        return f"{p.base_url.rstrip('/')}|{p.model}"

    def providers(self) -> list[AIProvider]:
        return self.cfg.ready_providers()

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.providers())

    def active_label(self) -> str:
        for p in self.providers():
            if self._key(p) == self._active:
                return p.label
        return ""

    def describe(self) -> str:
        text = self.cfg.describe()
        if self._active:
            text += f"（当前在用：{self.active_label()}）"
        return text

    def _ordered(self) -> list[AIProvider]:
        """按「当前健康 + 优先级」排好本次要尝试的顺序。"""
        ps = self.providers()
        if not ps:
            return []
        now = time.time()
        fresh = [p for p in ps if self._cooldown.get(self._key(p), 0) <= now]
        cooling = [p for p in ps if self._cooldown.get(self._key(p), 0) > now]
        if self._active:      # 粘住上次成功的线路，它一失败就自动回落
            fresh.sort(key=lambda p: 0 if self._key(p) == self._active else 1)
        # 全都在冷却里也要试一次，否则直接报错更没用
        return fresh + cooling

    def _mark_fail(self, p: AIProvider, err: Exception) -> None:
        seconds = max(_as_int(getattr(self.cfg, "failover_cooldown", 120), 120), 1)
        self._cooldown[self._key(p)] = time.time() + seconds
        if self._active == self._key(p):
            self._active = None
        self.logger.warning(f"AI [{p.label}] 调用失败（{seconds}s 内不再尝试）：{err}")

    def _mark_ok(self, p: AIProvider, switched: bool) -> None:
        self._cooldown.pop(self._key(p), None)
        self._active = self._key(p)
        if switched:
            self.logger.warning(f"AI 已切到 {p.label}（{p.model}）")

    # ---------- 底层请求 ----------
    def _post_raw(self, provider: AIProvider, payload: dict,
                  timeout: Optional[int] = None) -> dict:
        url = provider.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = net.request("POST", url, json=payload, headers=headers,
                               timeout=timeout or provider.timeout or self.cfg.timeout)
        except requests.RequestException as e:
            raise AIError(f"网络请求失败: {e}") from e
        if resp.status_code >= 400:
            body = (resp.text or "").strip().replace("\n", " ")[:160]
            raise AIError(f"HTTP {resp.status_code}: {body}")
        try:
            return resp.json()
        except ValueError as e:
            raise AIError(f"返回的不是 JSON: {(resp.text or '')[:160]}") from e

    def _run(self, build: Callable[[AIProvider], dict], timeout: Optional[int],
             kind: str) -> str:
        """按优先级逐个尝试，第一个成功的就返回"""
        providers = self._ordered()
        if not providers:
            raise AIError("AI 未启用或没有可用的供应商（检查是否填了 base_url 与 api_key）")
        errors: list[str] = []
        for p in providers:
            try:
                data = self._post_raw(p, build(p), timeout)
                text = self._extract(data)
            except Exception as e:
                self._mark_fail(p, e)
                errors.append(f"{p.label}: {e}")
                continue
            self._mark_ok(p, switched=bool(errors))
            if errors:
                self.logger.info(f"AI {kind} 由备用线路 {p.label} 完成")
            return text
        raise AIError("所有 AI 供应商都不可用 → " + "；".join(errors))

    # ---------- 对外能力 ----------
    def chat(self, prompt: str, system: str = "", timeout: Optional[int] = None) -> str:
        """纯文本对话"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._run(lambda p: {"model": p.with_model(), "messages": messages},
                         timeout, "对话")

    def vision(self, image: bytes | str, prompt: str, timeout: Optional[int] = None) -> str:
        """图片识别。image 为 bytes 或 base64 字符串"""
        if isinstance(image, bytes):
            b64 = base64.b64encode(image).decode("ascii")
        else:
            b64 = image
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }]
        return self._run(lambda p: {"model": p.with_model(vision=True), "messages": messages},
                         timeout, "识图")

    @staticmethod
    def _extract(data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise AIError(f"AI 响应格式异常: {str(data)[:300]}") from e
        if not isinstance(content, str) or not content.strip():
            raise AIError(f"AI 返回了空内容: {str(data)[:200]}")
        return content.strip()

    # ---------- 连通性测试 ----------
    def probe(self, prompt: str = "请只回复两个字：可用", timeout: int = 30) -> list[tuple[str, bool, str]]:
        """逐个供应商试一遍（忽略冷却），返回 [(名称, 是否成功, 说明)]。

        给命令行 `test-ai` 和界面上的「测试」按钮用。
        """
        results: list[tuple[str, bool, str]] = []
        for p in self.providers():
            try:
                data = self._post_raw(
                    p, {"model": p.with_model(),
                        "messages": [{"role": "user", "content": prompt}]},
                    timeout)
                results.append((p.label, True, self._extract(data)))
            except Exception as e:
                results.append((p.label, False, str(e)))
        return results

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
