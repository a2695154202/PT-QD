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
from urllib.parse import urljoin, urlparse, urlunparse

from core.config import AppConfig, TaskConfig, _as_bool
from core.events import EventBus, Events
from core.logger import get_logger
from core.state import (
    bump_login_fail,
    is_login_locked,
    login_fail_count,
    login_fail_info,
    login_remaining,
    reset_login_fail,
)

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
    "tasks.akile",
    "tasks.hhan",
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


def known_task_ids() -> set[str]:
    """当前版本真正有实现的任务 id（用来过滤已下线任务遗留的配置文件）"""
    return set(load_registry().keys())


def split_known_configs(configs):
    """把配置分成 (本版本支持的, 已下线的)。

    已删除的任务（如旧版的人人/Audiences）如果还留着 config/tasks/*.yaml，
    不应该再出现在界面和任务列表里 —— 否则用户会看到一个点不动的幽灵任务。
    """
    ids = known_task_ids()
    known = [c for c in configs if c.id in ids]
    stale = [c for c in configs if c.id not in ids]
    return known, stale


@dataclass
class TaskContext:
    """任务运行期上下文"""
    browser: object                      # BrowserController
    ai: object                           # AIClient
    captcha: object                      # CaptchaSolver
    cf: object                           # CFHandler
    cookiecloud: Optional[object] = None # CookieCloudClient
    totp: Optional[object] = None        # TwoFactorAuth
    events: Optional[EventBus] = None
    app_cfg: Optional[AppConfig] = None
    slider: Optional[object] = None      # 已废弃（AI 滑块缺口定位已移除）


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

    # 站点地址（子类可覆盖；运行时一律经 checkin_page_url / login_page_url 解析，
    # 站点换域名时只要改任务配置里的 task.url，这些内置地址会自动跟随）
    CHECKIN_URL = ""
    LOGIN_URL = ""

    def __init__(self, cfg: TaskConfig, ctx: TaskContext):
        self.cfg = cfg
        self.ctx = ctx
        self.logger = get_logger()
        self.events = ctx.events or EventBus()
        self.result_shots: list[str] = []
        self._url_rebased: set[tuple[str, str]] = set()   # 已记过日志的地址重挂
        self._nav_origin: str = ""                        # 本任务自己导航落地的站点域名

    # ---------- 模板方法 ----------
    async def run(self) -> TaskResult:
        self.events.emit("task_started", task_id=self.id, task_name=self.name)
        self.log(f"===== 任务 [{self.name}] 开始 =====")
        # 登录重试次数用尽则直接跳过（跨天也不重置，只有登录成功才清零）
        blocked = self.login_lock_result()
        if blocked is not None:
            return blocked
        try:
            await self.before()
            logged_in = await self.ensure_logged_in()
            if not logged_in:
                raise TaskError("登录失败，无法继续签到")
            # 登录成功（含 cookie 仍然有效）→ 清零累计失败次数
            self.note_login_success()
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

    # ---------- CookieCloud（所有任务：先同步 cookie，再打开站点） ----------
    @property
    def site_domain(self) -> str:
        """本站域名（用于从 CookieCloud 里挑出本站 cookie）"""
        domain = getattr(self, "_domain", "") or ""
        if not domain:
            try:
                from urllib.parse import urlparse
                domain = urlparse(self.cfg.url or getattr(self, "site", "")).hostname or ""
            except Exception:
                domain = ""
        return domain

    # ---------- 站点地址（域名变更自适应） ----------
    # 背景：各任务里写过内置的签到页/登录页地址（如 https://site-b.example/attendance.php）。
    # 站点换域名后，用户只把 task.url 改成新域名，内置地址里的旧域名仍会被访问 → 签到页打不开。
    # 因此所有 URL 统一走下面几个方法：只取内置地址的「路径/参数」，域名一律换成当前站点的。
    def opt_str(self, key: str, default: str = "") -> str:
        """读取 options 里的字符串项（非字符串也能安全转成 str）"""
        val = self.cfg.options.get(key)
        if val is None:
            return default
        return str(val).strip()

    def site_origin(self) -> str:
        """配置里的站点根地址 scheme://host（task.url 优先，其次类属性 site）"""
        for raw in (getattr(self.cfg, "url", "") or "", getattr(self, "site", "") or ""):
            p = urlparse(str(raw).strip())
            if p.scheme in ("http", "https") and p.netloc:
                return f"{p.scheme}://{p.netloc}"
        return ""

    async def page_origin(self) -> str:
        """浏览器当前所在页面的 scheme://host（没有页面时返回空串）"""
        try:
            current = await self.ctx.browser.url()
        except Exception:
            return ""
        p = urlparse(str(current or "").strip())
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}"
        return ""

    async def note_site_origin(self) -> str:
        """**本任务自己打开页面之后**调用：记下这次导航真实落地的站点域名。

        为什么不能直接读「浏览器当前页面」：任务队列串行复用同一个浏览器，
        第二个任务开始时页面还停在上一个任务站点的地址上。若拿它当本站域名，
        第二个任务的签到页就会被拼到上一个任务的网站上（谁先跑，后一个就跟谁）。
        因此只在任务自己 goto 过之后再采样，且只认第一次（登录跳转可能换主机，
        之后再采样会来回抖）。
        """
        if self._nav_origin:
            return self._nav_origin
        conf = self.site_origin()
        live = await self.page_origin()
        if not live:
            self._nav_origin = conf
            return self._nav_origin
        h_conf = (urlparse(conf).hostname or "").lower() if conf else ""
        h_live = (urlparse(live).hostname or "").lower()
        # www 有无、大小写这类小差异按配置走
        if not h_conf or h_live == h_conf or h_live.endswith("." + h_conf) \
                or h_conf.endswith("." + h_live):
            self._nav_origin = conf or live
        else:
            # 站点自己 301 到新域名：按落地域名走（配置里的 url 可以晚点再改）
            self.log(f"站点已跳转到新域名 {h_live}（配置里是 {h_conf}），后续按新域名访问")
            self._nav_origin = live
        return self._nav_origin

    async def site_base_origin(self) -> str:
        """当前站点根地址：优先用本任务自己导航落地的域名，其次配置里的 task.url

        **不读**浏览器当前页面，避免读到上一个任务遗留的站点（共享浏览器场景）。
        """
        return self._nav_origin or self.site_origin()

    def rebase_url(self, url: str, origin: str = "") -> str:
        """把地址重挂到当前站点域名上（保留路径/参数，只换协议+域名）。

        - 相对地址（"/attendance.php"）→ 拼到站点根地址后面
        - 绝对地址但域名与站点不同 → 只取路径/参数/锚点，域名换成站点的
        - 域名相同 → 原样返回
        - options.keep_url_host: true 时完全不重挂（站点确实另用一个域名时用）
        """
        raw = (url or "").strip()
        if not raw:
            return ""
        if _as_bool(self.cfg.options.get("keep_url_host"), False):
            return raw
        base = (origin or self.site_origin()).rstrip("/")
        p = urlparse(raw)
        if not p.netloc:                                  # 相对地址
            if not base:
                return raw
            return urljoin(base + "/", raw)
        b = urlparse(base)
        if not b.netloc or b.netloc.lower() == p.netloc.lower():
            return raw
        return urlunparse((b.scheme or p.scheme, b.netloc, p.path or "/",
                           p.params, p.query, p.fragment))

    async def resolve_site_url(self, url: str, what: str = "页面") -> str:
        """按当前站点域名解析地址；发生重挂时打一行日志（同一地址只打一次）"""
        raw = (url or "").strip()
        if not raw:
            return ""
        resolved = self.rebase_url(raw, await self.site_base_origin())
        if resolved != raw:
            key = (raw, resolved)
            if key not in self._url_rebased:
                self._url_rebased.add(key)
                self.log(f"{what}地址已按当前站点域名重挂：{raw} → {resolved}")
        return resolved

    async def checkin_page_url(self) -> str:
        """签到页地址：options.checkin_url → 内置 CHECKIN_URL → 站点首页（域名随之重挂）"""
        raw = (self.opt_str("checkin_url") or self.CHECKIN_URL
               or getattr(self.cfg, "url", "") or "").strip()
        return await self.resolve_site_url(raw, "签到页")

    async def login_page_url(self) -> str:
        """登录页地址：内置 LOGIN_URL → 站点首页（域名随之重挂）"""
        raw = (self.opt_str("login_url") or self.LOGIN_URL
               or getattr(self.cfg, "url", "") or "").strip()
        return await self.resolve_site_url(raw, "登录页")

    async def prepare_cookies(self) -> bool:
        """**打开站点之前**把 CookieCloud 的 cookie 注入浏览器上下文。

        每个任务的 ensure_logged_in 第一步都调用它，保证「先同步 cookie 再开始运行」。
        """
        cc = self.ctx.cookiecloud
        if not cc or not cc.cfg.enabled:
            return False
        if cc.cfg.mode == "paused":
            self.log("CookieCloud 工作模式为「暂停同步」，跳过")
            return False
        try:
            cookies = cc.get_cookies(domains=[self.site_domain])
            if not cookies:
                self.log("CookieCloud 中未找到本站 cookie")
                return False
            await self.ctx.browser.add_cookies(cookies)
            self.events.emit(Events.COOKIE_SYNCED, task_id=self.id, count=len(cookies))
            self.log(f"打开站点前已注入 {len(cookies)} 个 cookie")
            return True
        except Exception as e:
            self.logger.warning(f"CookieCloud 预注入失败: {e}")
            return False

    # ---------- 登录重试预算（跨天累计，只有登录成功才清零） ----------
    @property
    def login_limit(self) -> int:
        """本任务允许的登录失败总次数（options.max_login_retry，默认 3）"""
        return max(1, int(self.cfg.max_login_retry))

    def login_used(self) -> int:
        return login_fail_count(self.id)

    def login_remaining(self) -> int:
        """还能再尝试几次登录（0 表示已用尽，本次不再登录）"""
        return login_remaining(self.id, self.login_limit)

    def login_locked(self) -> bool:
        return is_login_locked(self.id, self.login_limit)

    def login_lock_message(self) -> str:
        info = login_fail_info(self.id)
        used = info["count"]
        when = f"，最近一次 {info['time']}" if info.get("time") else ""
        why = f"，原因：{info['reason']}" if info.get("reason") else ""
        return (
            f"登录失败次数已用尽（{used}/{self.login_limit}{when}{why}）。"
            "该计数不会按天清零，只有登录成功一次才会重置；"
            "请先检查账号/密码/2FA/验证码是否有效，手动登录站点或点界面「重置登录失败次数」后再运行"
        )

    def login_lock_result(self) -> Optional[TaskResult]:
        """已用尽则返回一个失败结果用于直接跳过任务，否则返回 None"""
        if not self.login_locked():
            return None
        msg = self.login_lock_message()
        self.logger.warning(f"[{self.name}] 跳过本次任务：{msg}")
        self.events.emit(Events.LOGIN_LOCKED, task_id=self.id, used=self.login_used(),
                         limit=self.login_limit)
        self.events.emit("task_finished", task_id=self.id, success=False)
        self.log(f"===== 任务 [{self.name}] 未执行：登录失败次数已用尽 =====")
        return TaskResult(self.id, False, f"未执行：{msg}")

    def note_login_success(self) -> None:
        """登录成功 → 唯一的清零入口"""
        used = self.login_used()
        reset_login_fail(self.id)
        if used:
            self.log(f"登录成功，已清零累计的 {used} 次登录失败计数")

    def note_login_failure(self, reason: str = "") -> int:
        """登录失败一次 → 计数 +1（持久化，跨天不清零），返回累计次数"""
        count = bump_login_fail(self.id, reason or "", self.login_limit)
        limit = self.login_limit
        left = max(0, limit - count)
        if left <= 0:
            self.logger.warning(
                f"[{self.name}] 登录失败次数已达上限（{count}/{limit}）："
                "之后不会再自动尝试登录，直到登录成功一次（不会因第二天而重置）")
        else:
            self.log(f"登录失败计数 {count}/{limit}（剩余 {left} 次机会，"
                     "该计数跨天累计、只有登录成功才清零）")
        return count

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
