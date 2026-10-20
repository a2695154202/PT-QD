"""事件总线：驱动 AI 主动触发。

任务执行过程中产生事件（如检测到验证码、CF 挑战、页面加载等），
订阅者可接收事件并调用 AI 能力主动介入，实现"AI 主动触发"。
"""
from collections import defaultdict
from typing import Callable


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, handler: Callable) -> None:
        """订阅事件"""
        self._handlers[event].append(handler)

    def emit(self, event: str, **payload) -> None:
        """触发事件，同步调用所有订阅者"""
        for handler in self._handlers.get(event, []):
            try:
                handler(**payload)
            except Exception as e:  # 单个 handler 异常不影响其他 handler
                from .logger import get_logger
                get_logger().warning(f"事件 {event} 的 handler 执行失败: {e}")

    def events(self) -> list[str]:
        return list(self._handlers.keys())


# 预定义事件名
class Events:
    BROWSER_READY = "browser_ready"
    PAGE_LOADED = "page_loaded"            # 页面加载完成
    LOGIN_PAGE_DETECTED = "login_page_detected"   # 检测到登录页
    COOKIE_SYNCED = "cookie_synced"        # CookieCloud 同步完成
    CAPTCHA_DETECTED = "captcha_detected"  # 检测到验证码（AI 可主动识别）
    CHALLENGE_DETECTED = "challenge_detected"  # CF/人机挑战（AI 可主动介入）
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    CHECKIN_CLICKED = "checkin_clicked"
    CHECKIN_SUCCESS = "checkin_success"
    TASK_STARTED = "task_started"
    TASK_FINISHED = "task_finished"
    TASK_ERROR = "task_error"
