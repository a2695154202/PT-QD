"""签到任务框架

- BaseTask       ：任务基类，模板方法定义 打开站点 -> 确保登录 -> 执行签到 的标准流程
- TaskContext    ：任务运行期共享的上下文（浏览器/AI/验证码/CookieCloud/TOTP/事件总线）
- TaskResult     ：任务执行结果
- register       ：任务注册装饰器，新增任务只需继承 BaseTask 并 @register
"""
from __future__ import annotations

import importlib
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from core.config import AppConfig, TaskConfig
from core.events import EventBus
from core.logger import get_logger

# 全局任务注册表 {task_id: task_class}
_REGISTRY: dict[str, type["BaseTask"]] = {}


def register(cls: type["BaseTask"]) -> type["BaseTask"]:
    """注册任务类（使用类属性 id）"""
    if not getattr(cls, "id", None):
        raise ValueError(f"任务类 {cls.__name__} 缺少 id 属性")
    _REGISTRY[cls.id] = cls
    return cls


# 已知任务模块：打包成 exe 后 pkgutil 无法扫描物理目录，靠此列表加载。
# 新增任务时：在 tasks/ 下新建模块，并把 "tasks.xxx" 加到这里（同时加入打包 spec 的 hiddenimports）。
KNOWN_TASKS = (
    "tasks.pt_kong",
    "tasks.hddolby",
    "tasks.ttg",
    "tasks.mteam",
    "tasks.audiences",
)


def load_registry() -> dict[str, type["BaseTask"]]:
    """加载任务实现：显式列表（exe 环境）+ 目录扫描（开发环境）双通道"""
    for mod in KNOWN_TASKS:
        try:
            importlib.import_module(mod)
        except Exception as e:  # 单个任务模块出错不影响其它任务
            get_logger().warning(f"加载任务模块 {mod} 失败: {e}")
    try:
        import tasks as pkg
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            if mod_info.name in ("base", "manager"):
                continue
            try:
                importlib.import_module(f"tasks.{mod_info.name}")
            except Exception as e:
                get_logger().warning(f"加载任务模块 tasks.{mod_info.name} 失败: {e}")
    except Exception:
        pass  # frozen 环境无物理目录，已由 KNOWN_TASKS 覆盖
    return _REGISTRY


@dataclass
class TaskContext:
    """任务运行期上下文"""
    browser: object                      # BrowserController
    ai: object                           # AIClient
    captcha: object                      # CaptchaSolver
    slider: object                       # SliderSolver
    cf: object                           # CFHandler
    cookiecloud: Optional[object] = None # CookieCloudClient
    totp: Optional[object] = None        # TwoFactorAuth
    events: Optional[EventBus] = None
    app_cfg: Optional[AppConfig] = None


@dataclass
class TaskResult:
    task_id: str
    success: bool
    message: str
    detail: str = ""
    screenshots: list = field(default_factory=list)
    run_log: str = ""


class TaskError(RuntimeError):
    pass


class BaseTask(ABC):
    """签到任务基类：定义标准执行流程，子类实现各站点细节"""

    id: str = ""
    name: str = ""
    description: str = ""
    site: str = ""

    def __init__(self, cfg: TaskConfig, ctx: TaskContext):
        self.cfg = cfg
        self.ctx = ctx
        self.logger = get_logger()
        self.events = ctx.events or EventBus()
        self.result_shots: list[str] = []

    # ---------- 模板方法 ----------
    async def run(self) -> TaskResult:
        self.events.emit("task_started", task_id=self.id, task_name=self.name)
        self.log(f"===== 任务 [{self.name}] 开始 =====")
        try:
            await self.before()
            logged_in = await self.ensure_logged_in()
            if not logged_in:
                raise TaskError("登录失败，无法继续签到")
            ok, message = await self.do_checkin()
            await self.after()
            self.events.emit("task_finished", task_id=self.id, success=ok)
            self.log(f"===== 任务 [{self.name}] 结束: {message} =====")
            return TaskResult(self.id, ok, message, detail=getattr(self, "_detail", ""),
                              screenshots=self.result_shots)
        except Exception as e:
            self.logger.exception(f"任务 [{self.name}] 执行异常")
            self.events.emit("task_error", task_id=self.id, error=str(e))
            try:
                shot_path = await self.ctx.browser.save_screenshot(name=f"{self.id}_error")
                self.result_shots.append(shot_path)
            except Exception:
                pass
            return TaskResult(self.id, False, f"任务异常: {e}", screenshots=self.result_shots)

    # ---------- 钩子（子类按需覆盖） ----------
    def needs_browser(self) -> bool:
        """本任务是否需要 Playwright 浏览器。

        桌面模式的任务（用用户自己的正常浏览器 + 识图点击）返回 False，
        管理器就不会为它启动自动化浏览器。
        """
        return True

    async def before(self) -> None:
        pass

    async def after(self) -> None:
        pass

    @abstractmethod
    async def ensure_logged_in(self) -> bool:
        """确保已登录，返回是否成功"""

    @abstractmethod
    async def do_checkin(self) -> tuple[bool, str]:
        """执行签到，返回 (成功?, 消息)"""

    # ---------- 通用工具 ----------
    def log(self, msg: str) -> None:
        self.logger.info(f"[{self.name}] {msg}")

    async def handle_challenge_if_any(self) -> None:
        """检测并处理 CF/人机挑战（AI 可主动介入）"""
        try:
            if await self.ctx.cf.is_challenge():
                self.log("检测到人机验证挑战，尝试处理...")
                await self.ctx.cf.solve()
        except Exception as e:
            self.logger.warning(f"挑战处理异常: {e}")

    async def page_text(self) -> str:
        return await self.ctx.browser.evaluate("() => document.body ? document.body.innerText : ''")
