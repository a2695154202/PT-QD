"""任务管理器：发现配置 -> 组装上下文 -> 执行任务 -> 收尾（日志落盘 + 关闭浏览器）"""
from __future__ import annotations

from typing import Optional

from core.ai import AIClient
from core.browser import BrowserController
from core.captcha import CaptchaSolver
from core.cf import CFHandler
from core.config import AppConfig, TaskConfig, load_task_configs, LOG_DIR, _abs
from core.cookiecloud import CookieCloudClient
from core.events import EventBus
from core.logger import current_run_log, get_logger, setup_logging, start_run_log, stop_run_log
from core.slider import SliderSolver
from core.state import record_result
from .base import BaseTask, TaskContext, TaskResult, load_registry


class TaskManager:
    def __init__(self, app_cfg: Optional[AppConfig] = None):
        self.app_cfg = app_cfg or AppConfig.load()
        setup_logging(self.app_cfg.log_level, _abs(self.app_cfg.log_file))
        self.logger = get_logger()
        self.events = EventBus()
        self._browser: Optional[BrowserController] = None
        self._ai: Optional[AIClient] = None
        self._cookiecloud: Optional[CookieCloudClient] = None
        self._queue_running = False
        self._register_event_logging()

    # ---------- 事件日志 ----------
    def _register_event_logging(self) -> None:
        def make_handler(name):
            def handler(**payload):
                self.logger.info(f"[事件] {name} {payload if payload else ''}")
            return handler
        for ev in ("task_started", "task_finished", "task_error", "login_page_detected",
                   "cookie_synced", "login_success", "login_failed", "login_locked",
                   "checkin_clicked", "captcha_detected", "challenge_detected",
                   "browser_ready", "page_loaded"):
            self.events.on(ev, make_handler(ev))

    # ---------- 共享组件（懒加载，跨任务复用） ----------
    @property
    def browser(self) -> BrowserController:
        if self._browser is None:
            self._browser = BrowserController(self.app_cfg.browser, self.events)
        return self._browser

    @property
    def ai(self) -> AIClient:
        if self._ai is None:
            self._ai = AIClient(self.app_cfg.ai)
        return self._ai

    @property
    def cookiecloud(self) -> Optional[CookieCloudClient]:
        if self.app_cfg.cookiecloud.enabled and self._cookiecloud is None:
            self._cookiecloud = CookieCloudClient(self.app_cfg.cookiecloud)
        return self._cookiecloud

    # ---------- 任务发现 ----------
    def discover(self) -> list[TaskConfig]:
        """扫描 config/tasks/ 下的任务配置，并校验是否有对应实现"""
        registry = load_registry()
        configs = load_task_configs()
        for cfg in configs:
            if cfg.id not in registry:
                self.logger.warning(f"任务 [{cfg.id}] 有配置但未找到实现类")
        return configs

    def create_task(self, cfg: TaskConfig) -> BaseTask:
        registry = load_registry()
        cls = registry.get(cfg.id)
        if not cls:
            raise ValueError(f"未找到任务实现: {cfg.id}（已注册: {list(registry)}）")
        ctx = TaskContext(
            browser=self.browser,
            ai=self.ai,
            captcha=CaptchaSolver(self.ai, self.browser, self.events),
            slider=SliderSolver(self.ai, self.browser),
            cf=CFHandler(self.browser, self.ai, self.events),
            cookiecloud=self.cookiecloud,
            events=self.events,
            app_cfg=self.app_cfg,
        )
        return cls(cfg, ctx)

    # ---------- 执行 ----------
    def _ensure_run_log(self) -> None:
        if not current_run_log():
            start_run_log(str(LOG_DIR))

    def _network_for(self, task_cfg: TaskConfig) -> tuple[Optional[dict], bool]:
        """按任务决定网络：返回 (proxy, no_proxy)。

        - 全局未配置代理：跟随系统（None, False）
        - 任务勾选「走代理」：使用全局代理
        - 任务未勾选：强制直连（--no-proxy-server），不走任何代理
        """
        p = self.app_cfg.proxy
        if not p.ready:
            return None, False
        if task_cfg.use_proxy:
            self.logger.info(f"任务 [{task_cfg.name}] 使用全局代理 {p.server}")
            return p.for_playwright(), False
        self.logger.info(f"任务 [{task_cfg.name}] 未勾选代理，强制直连")
        return None, True

    async def _run_one(self, task_id: str) -> TaskResult:
        cfg = next((c for c in self.discover() if c.id == task_id), None)
        if not cfg:
            return TaskResult(task_id, False, f"任务配置不存在: {task_id}")
        task = self.create_task(cfg)
        # 桌面模式的任务用用户自己的正常浏览器（识图点击），不启动自动化浏览器
        if task.needs_browser():
            proxy, no_proxy = self._network_for(cfg)
            if self.browser.is_running and (
                    self.browser.active_proxy != proxy or self.browser.no_proxy != no_proxy):
                self.logger.info("任务网络设置与当前浏览器不一致，重启浏览器（非任务结束，仅切换代理）")
                await self.browser.stop()
            if not self.browser.is_running:
                await self.browser.start(proxy=proxy, no_proxy=no_proxy)
        else:
            self.logger.info(f"任务 [{cfg.name}] 走桌面模式（正常浏览器 + 识图点击），"
                             "不启动自动化浏览器")
        result = await task.run()
        result.run_log = current_run_log()
        # 记录任务状态，供界面显示「已签到 / 未签到」
        try:
            record_result(task_id, result.success, result.message)
        except Exception as e:
            self.logger.warning(f"记录任务状态失败: {e}")
        return result

    async def run_task(self, task_id: str, close_browser: bool = True) -> TaskResult:
        """运行单个任务。结束后（默认）写入本次运行日志并关闭浏览器。"""
        if self._queue_running:
            self.logger.warning("已有任务在执行，忽略本次请求")
            return TaskResult(task_id, False, "已有任务在执行，请等待当前任务完成")
        self._queue_running = True
        self._ensure_run_log()
        try:
            result = await self._run_one(task_id)
        finally:
            try:
                if close_browser:
                    await self.close()
            finally:
                self._queue_running = False
        return result

    async def run_tasks(self, task_ids: list[str], close_browser: bool = True) -> list[TaskResult]:
        """串行执行任务队列：一个跑完再跑下一个，全部结束后才关闭浏览器。"""
        if self._queue_running:
            self.logger.warning("已有任务队列在执行，忽略本次请求")
            return []
        self._queue_running = True
        self._ensure_run_log()
        total = len(task_ids)
        results: list[TaskResult] = []
        if total == 0:
            self._queue_running = False
            return results
        self.logger.info(f"任务队列开始：共 {total} 个任务，串行执行，全部完成后统一关闭浏览器")
        idx = 0
        try:
            for idx, tid in enumerate(task_ids, 1):
                self.logger.info(f"---------- 队列 {idx}/{total}：{tid} 开始 ----------")
                result = await self._run_one(tid)
                results.append(result)
                mark = "成功" if result.success else "失败"
                self.logger.info(f"---------- 队列 {idx}/{total}：{tid} {mark}"
                                 f"（浏览器保持运行）----------")
        finally:
            if close_browser:
                self.logger.info("任务队列全部结束，关闭浏览器")
                await self.close()
            self._queue_running = False
        return results

    async def run_all(self, only_enabled: bool = True) -> list[TaskResult]:
        ids = [c.id for c in self.discover() if (c.enabled or not only_enabled)]
        if not ids:
            self.logger.info("没有可运行的任务")
            return []
        return await self.run_tasks(ids)

    async def close(self) -> None:
        """收尾：关闭浏览器 + 结束本次运行日志（文件保留）"""
        if self._browser and self._browser.is_running:
            self.logger.info("签到运行结束，关闭浏览器")
            await self._browser.stop()
        self._browser = None
        log_path = stop_run_log()
        if log_path:
            self.logger.info(f"本次运行日志已保存: {log_path}")
