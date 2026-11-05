"""Telegram 通知：把一遍任务的签到结果汇总发到手机。

- 默认 `use_proxy=True`：api.telegram.org 国内直连基本不通，默认复用「全局设置 → 代理」；
  没配代理时直接尝试直连，并在日志里说明（失败不重试，不影响任务结果）。
- 消息用 HTML 解析（只转义 & < >），比 MarkdownV2 少一堆转义坑。
- 超过 Telegram 单条上限（4096）时自动截断，避免整条发不出去。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from . import net
from .config import TelegramConfig
from .logger import get_logger

API_BASE = "https://api.telegram.org"
MAX_LEN = 3800          # 留出余量（Telegram 硬上限 4096）


class TelegramError(RuntimeError):
    pass


def _esc(text: str) -> str:
    """HTML parse_mode 只需要转义这三个字符"""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short(text: str, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize(rows: Sequence[tuple[str, bool, str]],
              when: Optional[str] = None,
              title: str = "签到助手") -> str:
    """把 [(任务名, 是否成功, 说明)] 拼成一条 HTML 消息"""
    total = len(rows)
    ok = sum(1 for _, success, _ in rows if success)
    bad = total - ok
    stamp = when or datetime.now().strftime("%m-%d %H:%M")
    head = f"<b>{_esc(title)}</b> · {_esc(stamp)}"
    stat = f"成功 {ok} / 失败 {bad} / 共 {total}"
    lines = [head, stat, ""]
    for name, success, message in rows:
        mark = "✅" if success else "❌"
        lines.append(f"{mark} <b>{_esc(name)}</b>  {_esc(_short(message))}")
    text = "\n".join(lines).strip()
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 16].rstrip() + "\n…（结果过多已截断）"
    return text


@dataclass
class TelegramNotifier:
    cfg: TelegramConfig
    logger: object = None

    def __post_init__(self):
        if self.logger is None:
            self.logger = get_logger()

    @property
    def ready(self) -> bool:
        return self.cfg.ready

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.cfg.bot_token.strip()}/{method}"

    def _masked(self) -> str:
        token = self.cfg.bot_token.strip()
        return token[:8] + "***" + token[-4:] if len(token) > 14 else "***"

    def send(self, text: str, disable_notification: bool = False) -> bool:
        """发一条消息；失败抛 TelegramError（调用方决定是否只记日志）"""
        if not self.ready:
            raise TelegramError("Telegram 未启用或未填写 bot_token / chat_id")
        payload = {
            "chat_id": self.cfg.chat_id.strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if disable_notification:
            payload["disable_notification"] = True
        via = "代理" if self.cfg.use_proxy else "直连"
        try:
            resp = net.request("POST", self._url("sendMessage"), json=payload,
                               timeout=20, use_proxy=bool(self.cfg.use_proxy))
        except Exception as e:
            raise TelegramError(f"网络请求失败（{via}，bot {self._masked()}）: {e}") from e
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            desc = (data or {}).get("description") or resp.text[:200]
            raise TelegramError(f"Telegram 返回错误（HTTP {resp.status_code}）: {desc}")
        self.logger.info(f"Telegram 消息已发送（{via}，chat {self.cfg.chat_id}）")
        return True

    def send_text(self, text: str) -> bool:
        """给测试按钮用：带一条明确前缀，方便辨认"""
        return self.send(f"<b>签到助手 · 测试消息</b>\n{_esc(text)}")

    def send_summary(self, rows: Sequence[tuple[str, bool, str]]) -> bool:
        return self.send(summarize(rows))
