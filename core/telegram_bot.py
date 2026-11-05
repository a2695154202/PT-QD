"""Telegram 简单交互：命令菜单 + 内联按钮。

程序运行期间在后台线程里做 getUpdates 长轮询，支持：

    /status  今日签到状态
    /run     运行全部启用任务
    /retry   重试「今天还没签到」的任务（也可只重试通知里点名的失败项）
    /menu    菜单（带内联按钮）
    /help    帮助

安全：只响应配置里那个 chat_id，别人的消息一律忽略（bot 用户名可能被搜到）。
所有网络请求都走 core.net，遵循「默认走代理」的策略。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from . import net
from .config import TelegramConfig
from .logger import get_logger
from .telegram import API_BASE, TelegramError, TelegramNotifier, _esc, menu_keyboard

HELP_TEXT = (
    "<b>签到助手 · 菜单</b>\n"
    "/status  今日签到状态\n"
    "/run     运行全部启用任务\n"
    "/retry   重试今天还没签到的任务\n"
    "/menu    显示菜单按钮\n"
    "/help    显示这段帮助\n\n"
    "有失败时，汇总消息下方会出现「🔁 重试失败项」按钮，点一下就会重跑那几个任务。"
)

# 注册到 Telegram 的「/」命令菜单
BOT_COMMANDS = [
    {"command": "status", "description": "今日签到状态"},
    {"command": "run", "description": "运行全部启用任务"},
    {"command": "retry", "description": "重试今天还未签到的任务"},
    {"command": "menu", "description": "显示菜单按钮"},
    {"command": "help", "description": "帮助"},
]


class TelegramBot:
    """后台轮询 Telegram 更新并分发到 handlers。

    handlers:
        status() -> str      今日状态文本
        run() -> str         触发运行全部
        retry(ids: list[str] | None) -> str   重试
        help() -> str        可选，默认内置文案
    """

    def __init__(self, cfg: TelegramConfig, handlers: Optional[dict] = None,
                 logger: object = None):
        self.cfg = cfg
        self.handlers = handlers or {}
        self.logger = logger or get_logger()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._hinted = False            # 网络提示只打一次，避免日志刷屏
        self._notifier = TelegramNotifier(cfg, self.logger)

    # ---------- 生命周期 ----------
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if not self.cfg.ready:
            return False
        if self.running:
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)
        self._thread.start()
        self.logger.info("Telegram 交互已启动（发送 /menu 或 /help 查看用法）")
        return True

    def stop(self) -> None:
        self._stop.set()

    # ---------- 网络 ----------
    def _call(self, method: str, payload: Optional[dict] = None, timeout: int = 25) -> dict:
        url = f"{API_BASE}/bot{self.cfg.bot_token.strip()}/{method}"
        resp = net.request("POST", url, json=payload or {}, timeout=timeout,
                           use_proxy=bool(self.cfg.use_proxy), fallback="auto")
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200 or not data.get("ok"):
            desc = (data or {}).get("description") or resp.text[:200]
            raise TelegramError(f"{method} 失败（HTTP {resp.status_code}）: {desc}")
        return data

    def _reply(self, text: str, markup: Optional[dict] = None) -> None:
        try:
            self._notifier.send(text, reply_markup=markup)
        except Exception as e:
            self.logger.warning(f"Telegram 回复失败: {e}")

    def _answer(self, callback_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:190]
        try:
            self._call("answerCallbackQuery", payload, timeout=10)
        except Exception as e:
            self.logger.warning(f"answerCallbackQuery 失败: {e}")

    # ---------- 轮询 ----------
    def _hint_network(self, route: str) -> None:
        """网络不通时给一次能照着做的提示，而不是每 5 秒刷一次同样的报错"""
        if self._hinted:
            return
        self._hinted = True
        self.logger.warning(
            f"连不上 api.telegram.org（当前走：{route}）。"
            "国内直连通常不通，请二选一：①启动 Clash/v2rayN 等代理客户端；"
            "②在「全局设置 → 代理」填写地址（如 127.0.0.1:7897）并勾选启用。"
            "签到任务本身不受影响。"
        )

    def _loop(self) -> None:
        route = "直连"
        try:
            _, route = net.resolve(bool(self.cfg.use_proxy), "auto")
            self.logger.info(f"Telegram 交互网络：{route}")
        except Exception:
            pass
        try:
            self._call("setMyCommands", {"commands": BOT_COMMANDS}, timeout=15)
        except Exception as e:
            self.logger.warning(f"注册命令菜单失败（网络不通时交互不可用）: {e}")
            self._hint_network(route)
        backoff = 5
        while not self._stop.is_set():
            try:
                data = self._call("getUpdates", {
                    "offset": self._offset,
                    "timeout": 20,
                    "allowed_updates": ["message", "callback_query"],
                }, timeout=35)
            except Exception as e:
                if self._stop.is_set():
                    break
                text = str(e)
                if "409" in text or "terminated by other" in text:
                    # 同一个 bot 只能有一个实例在轮询，常见于新旧版本同时开着
                    self.logger.warning(
                        "Telegram 轮询冲突：已有另一个实例在用这个 bot_token，"
                        "请关掉多余的签到助手窗口（60 秒后重试）")
                    backoff = 60
                else:
                    self.logger.warning(f"Telegram 轮询失败，{backoff} 秒后重试: {e}")
                    self._hint_network(route)
                self._stop.wait(backoff)
                if self._stop.is_set():
                    break
                backoff = min(backoff * 2, 60)
                continue
            backoff = 5
            for upd in data.get("result") or []:
                self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
                try:
                    self._handle(upd)
                except Exception as e:
                    self.logger.warning(f"处理 Telegram 更新失败: {e}")

    def _handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            self._on_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != str(self.cfg.chat_id).strip():
            return                      # 不是配置的会话，忽略
        text = (msg.get("text") or "").strip()
        if not text:
            return
        cmd = text.split()[0].lower().lstrip("/").split("@")[0]
        if cmd in ("start", "menu"):
            self._reply(HELP_TEXT, menu_keyboard())
        elif cmd == "help":
            self._reply(HELP_TEXT, menu_keyboard())
        elif cmd == "status":
            self._reply(self._run_handler("status"), menu_keyboard())
        elif cmd == "run":
            self._reply(self._run_handler("run"), menu_keyboard())
        elif cmd == "retry":
            self._reply(self._run_handler("retry", None), menu_keyboard())
        else:
            self._reply("没听懂这个命令，发 /help 看用法。", menu_keyboard())

    def _on_callback(self, cb: dict) -> None:
        data = (cb.get("data") or "").strip()
        chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat_id != str(self.cfg.chat_id).strip():
            self._answer(cb.get("id", ""), "无权操作")
            return
        if data == "status":
            self._answer(cb.get("id", ""), "正在查询…")
            self._reply(self._run_handler("status"), menu_keyboard())
        elif data == "run":
            self._answer(cb.get("id", ""), "已收到")
            self._reply(self._run_handler("run"), menu_keyboard())
        elif data.startswith("retry"):
            ids = None
            if "|" in data:
                ids = [t for t in data.split("|", 1)[1].split(",") if t]
            self._answer(cb.get("id", ""), "已收到")
            self._reply(self._run_handler("retry", ids), menu_keyboard())
        elif data == "help":
            self._answer(cb.get("id", ""))
            self._reply(HELP_TEXT, menu_keyboard())
        else:
            self._answer(cb.get("id", ""), "未知操作")

    def _run_handler(self, key: str, ids: Optional[list] = None) -> str:
        try:
            if key == "retry":
                fn: Callable = self.handlers.get("retry")
                if fn is None:
                    return "当前没有可执行的重试处理器。"
                return fn(ids) or "已提交。"
            fn = self.handlers.get(key)
            if fn is None:
                return f"当前不支持该操作（{key}）。"
            return fn() or "已提交。"
        except Exception as e:
            self.logger.warning(f"Telegram 操作 {key} 失败: {e}")
            return f"操作失败：{_esc(str(e))}"
