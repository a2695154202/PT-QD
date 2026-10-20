"""Edge 浏览器控制器（Playwright + CDP 远程调试）。

两种工作模式：
- launch ：由本程序启动 Edge（channel=msedge），开放远程调试端口，
           用户可随时用 edge://inspect 或其他 CDP 客户端挂接调试；
           使用持久化用户目录，登录态可跨次保留。
- attach ：连接一个已经以调试模式运行的 Edge（http://127.0.0.1:9222），
           直接接管用户正在用的浏览器窗口，便于人工介入后交还程序控制。
"""
from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
)

from .config import BrowserConfig, _abs
from .events import EventBus, Events
from .logger import get_logger

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
window.chrome = window.chrome || {runtime: {}};
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--no-default-browser-check",
    "--no-first-run",
]


class BrowserError(RuntimeError):
    pass


class BrowserController:
    def __init__(self, cfg: BrowserConfig, events: Optional[EventBus] = None,
                 screenshot_dir: str = "data/screenshots"):
        self.cfg = cfg
        self.events = events or EventBus()
        self.logger = get_logger()
        self.screenshot_dir = Path(_abs(screenshot_dir))
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._attached = False
        self._active_proxy: Optional[dict] = None
        self._no_proxy = False
        self._network_key = (None, False)
        self._topmost_applied = False

    # ---------- 生命周期 ----------
    @property
    def is_running(self) -> bool:
        """浏览器是否处于可用状态（已启动且上下文可访问）"""
        return self._context is not None and self._page is not None

    @property
    def active_proxy(self) -> Optional[dict]:
        """当前浏览器实例使用的代理配置（None=未指定）"""
        return self._active_proxy

    @property
    def no_proxy(self) -> bool:
        """当前浏览器是否强制直连（--no-proxy-server）"""
        return self._no_proxy

    async def start(self, proxy: Optional[dict] = None, no_proxy: bool = False) -> "BrowserController":
        """启动/复用浏览器。

        proxy    : 代理配置（{"server": "http://127.0.0.1:7890", ...}）
        no_proxy : True 时以 --no-proxy-server 启动，完全不走代理（含系统代理）
        两者都不传 = 跟随系统代理设置
        """
        key = (tuple(sorted((proxy or {}).items())) if proxy else None, bool(no_proxy))
        if self.is_running:
            if key == self._network_key:
                return self
            self.logger.info("任务网络设置（代理）与当前浏览器不一致，重启浏览器")
            await self.stop()
        self._network_key = key
        self._active_proxy = proxy or None
        self._no_proxy = bool(no_proxy)
        self._pw = await async_playwright().start()
        if self.cfg.mode == "attach":
            await self._attach()
        else:
            await self._launch(proxy, no_proxy)
        self.events.emit(Events.BROWSER_READY, mode=self.cfg.mode)
        net = f"代理 {proxy['server']}" if proxy else ("强制直连" if no_proxy else "跟随系统")
        self.logger.info(f"浏览器已就绪（模式: {self.cfg.mode}，网络: {net}）")
        return self

    async def _launch(self, proxy: Optional[dict] = None, no_proxy: bool = False) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        args = [f"--remote-debugging-port={self.cfg.debugging_port}"] + LAUNCH_ARGS
        if no_proxy:
            args.append("--no-proxy-server")  # 忽略系统代理，直连
        if self.cfg.fullscreen:
            args.append("--start-fullscreen")
        kwargs = dict(
            user_data_dir=self.cfg.user_data_dir_abs,
            channel="msedge",
            headless=self.cfg.headless,
            slow_mo=self.cfg.slow_mo,
            args=args,
        )
        if self.cfg.fullscreen and not self.cfg.headless:
            # 全屏时让页面跟随窗口尺寸，避免固定 viewport 与窗口不一致
            kwargs["no_viewport"] = True
        else:
            kwargs["viewport"] = {"width": self.cfg.viewport_width, "height": self.cfg.viewport_height}
        if proxy and not no_proxy:
            kwargs["proxy"] = proxy
        try:
            self._context = await self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as e:
            msg = str(e)
            if "msedge" in msg.lower() or "executable" in msg.lower() or "channel" in msg.lower():
                raise BrowserError(
                    "未能启动 Microsoft Edge。请确认本机已安装 Edge（Windows 10/11 通常自带）。"
                    f" 原始错误: {msg}"
                ) from e
            raise
        if self.cfg.stealth:
            await self._context.add_init_script(STEALTH_JS)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self.logger.info(
            f"Edge 已启动，远程调试端口 {self.cfg.debugging_port}"
            f"（可在 Edge 地址栏输入 edge://inspect/#devices 挂接调试）"
        )

    async def _attach(self) -> None:
        try:
            self._browser = await self._pw.chromium.connect_over_cdp(self.cfg.cdp_endpoint)
        except Exception as e:
            raise BrowserError(
                f"无法连接 {self.cfg.cdp_endpoint}，请先以 "
                f"msedge --remote-debugging-port={self.cfg.debugging_port} 启动 Edge：{e}"
            ) from e
        self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        if self.cfg.stealth:
            try:
                await self._context.add_init_script(STEALTH_JS)
            except Exception:
                pass
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._attached = True

    async def stop(self) -> None:
        # 只要还持有 playwright / context / browser 任一引用就要清理，
        # 避免「context 已创建但页面创建失败」时泄漏浏览器进程
        if not (self._pw or self._context or self._browser):
            return
        try:
            if self._attached:
                if self._browser:
                    await self._browser.close()  # attach 模式仅断开 CDP 连接
            elif self._context:
                await self._context.close()
        finally:
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass
            self._pw = None
            self._browser = None
            self._context = None
            self._page = None
            self._attached = False
            self._active_proxy = None
            self._no_proxy = False
            self._network_key = (None, False)
            self._topmost_applied = False
        self.logger.info("浏览器已关闭")

    async def __aenter__(self) -> "BrowserController":
        return await self.start()

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    def apply_topmost(self) -> None:
        """把浏览器窗口置顶（仅 Windows）。在页面加载后调用，此时窗口标题才可识别。"""
        if not self.cfg.always_on_top or os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            targets: list[int] = []

            def _cb(hwnd, _lparam):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    n = user32.GetWindowTextLengthW(hwnd)
                    if n <= 0:
                        return True
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    # Edge 窗口标题固定以 "Microsoft Edge" 结尾，避免误匹配其它含 Edge 的程序
                    if "Microsoft Edge" in buf.value:
                        targets.append(hwnd)
                except Exception:
                    pass
                return True

            user32.EnumWindows(WNDENUMPROC(_cb), 0)
            HWND_TOPMOST = -1
            SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0040
            for hwnd in targets:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
            if targets:
                self.logger.info(f"浏览器窗口已置顶（{len(targets)} 个窗口）")
            else:
                self.logger.warning("未找到 Edge 窗口，置顶未生效")
        except Exception as e:
            self.logger.warning(f"窗口置顶失败: {e}")

    # ---------- 页面操作 ----------
    @property
    def page(self) -> Page:
        if not self._page:
            raise BrowserError("浏览器尚未启动")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if not self._context:
            raise BrowserError("浏览器尚未启动")
        return self._context

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> None:
        self.logger.info(f"打开页面: {url}")
        await self.page.goto(url, wait_until=wait_until, timeout=self.cfg.default_timeout)
        self.events.emit(Events.PAGE_LOADED, url=self.page.url)
        if not self._topmost_applied:
            # 页面加载后窗口标题才带有站点信息，此时置顶最可靠
            self.apply_topmost()
            self._topmost_applied = True

    async def reload(self, wait_until: str = "domcontentloaded") -> None:
        await self.page.reload(wait_until=wait_until, timeout=self.cfg.default_timeout)
        self.events.emit(Events.PAGE_LOADED, url=self.page.url)

    async def wait_selector(self, selector: str, timeout: Optional[int] = None,
                            state: str = "visible"):
        return await self.page.wait_for_selector(
            selector, timeout=timeout or self.cfg.default_timeout, state=state
        )

    async def wait_timeout(self, ms: int) -> None:
        await self.page.wait_for_timeout(ms)

    async def click(self, selector: str, timeout: Optional[int] = None) -> None:
        await self.page.click(selector, timeout=timeout or self.cfg.default_timeout)

    async def human_click(self, selector: str, timeout: Optional[int] = None) -> None:
        """拟人点击：先移动鼠标到元素附近（模拟轨迹），再由 Playwright 执行可靠点击"""
        locator = self.page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout or self.cfg.default_timeout)
        try:
            box = await locator.bounding_box()
            if box:
                x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                await self.page.mouse.move(x, y, steps=random.randint(5, 15))
                await self.wait_timeout(random.randint(60, 200))
        except Exception:
            pass
        # 用 locator.click 保证可点击性（自动滚动、遮挡检查），避免坐标点击落空
        await locator.click(timeout=timeout or self.cfg.default_timeout)

    async def fill(self, selector: str, value: str, timeout: Optional[int] = None) -> None:
        await self.page.fill(selector, value, timeout=timeout or self.cfg.default_timeout)

    async def evaluate(self, script: str):
        return await self.page.evaluate(script)

    async def scroll_to_bottom(self) -> None:
        await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    async def title(self) -> str:
        return await self.page.title()

    async def url(self) -> str:
        return self.page.url

    # ---------- 截图 / 取图 ----------
    @staticmethod
    def _shot_name(name: Optional[str]) -> str:
        return name or f"shot_{time.strftime('%Y%m%d_%H%M%S')}"

    async def screenshot(self, name: Optional[str] = None, full_page: bool = False) -> bytes:
        """整页截图并保存，返回 PNG bytes（scale=css：像素坐标与鼠标坐标一致，供 AI 定位用）"""
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{self._shot_name(name)}.png"
        data = await self.page.screenshot(path=str(path), full_page=full_page, scale="css")
        self.logger.info(f"截图已保存: {path}")
        return data

    async def save_screenshot(self, name: Optional[str] = None, full_page: bool = False) -> str:
        """整页截图并保存，返回保存路径"""
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        path = self.screenshot_dir / f"{self._shot_name(name)}.png"
        await self.page.screenshot(path=str(path), full_page=full_page, scale="css")
        self.logger.info(f"截图已保存: {path}")
        return str(path)

    async def element_screenshot(self, selector: str, name: Optional[str] = None) -> bytes:
        """获取页面中指定位置（元素）的图片，如验证码图片，返回 PNG bytes"""
        locator = self.page.locator(selector).first
        await locator.wait_for(state="visible", timeout=self.cfg.default_timeout)
        if name:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{name}.png"
            data = await locator.screenshot(path=str(path))
            self.logger.info(f"元素截图已保存: {path}")
            return data
        return await locator.screenshot()

    async def clip_screenshot(self, clip: Optional[dict], name: Optional[str] = None) -> bytes:
        """按区域截图 clip={"x","y","width","height"}（scale=css，AI 返回的坐标可直接用于鼠标）"""
        if name:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{name}.png"
            data = await self.page.screenshot(clip=clip, path=str(path), scale="css")
            self.logger.info(f"区域截图已保存: {path}")
            return data
        return await self.page.screenshot(clip=clip, scale="css")

    # ---------- Cookie / Storage ----------
    async def add_cookies(self, cookies: list[dict]) -> None:
        if cookies:
            await self.context.add_cookies(cookies)
            self.logger.info(f"已注入 {len(cookies)} 个 cookie")

    async def set_local_storage(self, origin: str, items: dict) -> None:
        """向指定源写入 localStorage"""
        script = """
        (items) => {
            for (const [k, v] of Object.entries(items)) {
                try { localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v)); } catch (e) {}
            }
        }
        """
        await self.page.goto(origin, wait_until="domcontentloaded")
        await self.page.evaluate(script, items)
