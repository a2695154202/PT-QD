"""浏览器 profile 桥接：把 cookie 在「调试浏览器」和「正常浏览器」之间打通。

**为什么这样做**
- 签到页有 Cloudflare Turnstile，用 Playwright/CDP 驱动的浏览器容易被卡死；
- 登录页只是普通图形验证码，用 Playwright 自动化反而又快又稳。

**怎么做到两个浏览器共用登录态**
两者使用**同一个独立 profile 目录**（默认 `data/desktop_profile`），cookie 天然共享：
1. 调试浏览器：`playwright launch_persistent_context(profile)`（带 `--remote-debugging-port`）
   —— 只用来注入 CookieCloud 的 cookie、自动登录；用完立刻关闭（关闭时 cookie 落盘到 profile）
2. 正常浏览器：`msedge --user-data-dir=profile`（**不带**调试端口）
   —— 用来打开签到页；没有自动化指纹，CF 看到的是普通浏览器

两者**不会同时运行**（同一 profile 有独占锁），所以流程是「调试用完 → 关 → 正常打开」。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .desktop import DesktopController
from .logger import get_logger

# Edge 常见安装位置
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0")


def find_edge() -> str:
    """定位 msedge.exe，找不到返回空串"""
    for p in EDGE_CANDIDATES:
        if p and Path(p).exists():
            return p
    return ""


class ProfileBridge:
    """同一个 Edge profile ：调试侧写 cookie / 正常侧跑签到"""

    def __init__(self, profile_dir: str = "data/desktop_profile", port: int = 9223,
                 events=None, startup_wait: float = 6.0):
        self.profile_dir = profile_dir
        self.port = int(port)
        self.events = events
        self.startup_wait = startup_wait
        self.logger = get_logger()
        self._proc: Optional[subprocess.Popen] = None
        self._hwnd: Optional[int] = None

    # ---------- 基础 ----------
    @property
    def profile_abs(self) -> str:
        p = Path(self.profile_dir)
        if not p.is_absolute():
            from .config import PROJECT_ROOT
            p = PROJECT_ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    @property
    def normal_running(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    # ---------- 调试侧：写 cookie 到 profile ----------
    @staticmethod
    def persistable(cookies: list[dict], days: int = 30) -> list[dict]:
        """把会话 cookie（expires=-1/缺失）补成有期限的 cookie。

        Chromium 只在 cookie 带 expires 时才写入 profile 的 Cookies 数据库，
        会话 cookie 一关浏览器就没了 —— 这会导致「写进去了但正常浏览器读不到」。
        """
        now = time.time()
        out: list[dict] = []
        for c in cookies or []:
            item = dict(c)
            try:
                exp = float(item.get("expires"))
            except (TypeError, ValueError):
                exp = -1.0
            if exp <= now:                     # -1（会话）或已过期 → 补一个未来时间
                item["expires"] = now + max(1, days) * 86400
            out.append(item)
        return out

    async def inject_cookies(self, cookies: list[dict], days: int = 30) -> int:
        """用调试浏览器把 cookie 写进 profile（随后关闭，cookie 落盘）"""
        if not cookies:
            return 0
        cookies = self.persistable(cookies, days=days)
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        ctx = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=self.profile_abs,
                channel="msedge",
                headless=True,
                args=[f"--remote-debugging-port={self.port}"],
            )
            await ctx.add_cookies(cookies)
            self.logger.info(f"已把 {len(cookies)} 个 cookie 写入 profile: {self.profile_abs}")
            return len(cookies)
        finally:
            try:
                if ctx is not None:
                    await ctx.close()      # 关闭时 Playwright 会把 cookie 落盘
            finally:
                await pw.stop()

    async def read_profile_cookies(self) -> list[dict]:
        """读取 profile 里的 cookie（调试浏览器短暂打开）"""
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        ctx = None
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=self.profile_abs, channel="msedge", headless=True,
                args=[f"--remote-debugging-port={self.port}"],
            )
            return await ctx.cookies()
        finally:
            try:
                if ctx is not None:
                    await ctx.close()
            finally:
                await pw.stop()

    # ---------- 网络层：判断 cookie 是否还有效 ----------
    @staticmethod
    def check_login(cookies: list[dict], url: str, timeout: float = 20.0) -> tuple[bool, str]:
        """带 cookie 请求页面，判断是否已登录。

        返回 (是否已登录, 说明)。不启动浏览器，几秒钟出结果。
        """
        if not cookies:
            return False, "没有可用 cookie"
        try:
            import requests
        except Exception as e:                       # pragma: no cover
            return False, f"缺少 requests 库: {e}"
        try:
            s = requests.Session()
            for c in cookies:
                name = c.get("name")
                if not name:
                    continue
                s.cookies.set(name, c.get("value", ""),
                              domain=c.get("domain", "") or None,
                              path=c.get("path", "/") or "/")
            r = s.get(url, timeout=timeout, allow_redirects=True,
                      headers={"User-Agent": UA})
            html = r.text or ""
        except Exception as e:
            return False, f"请求失败: {e}"

        logged_in = ("logout.php" in html) or ("userdetails.php" in html)
        is_login = ("takelogin.php" in html) or ('name="username"' in html)
        if logged_in and not is_login:
            return True, f"已登录（HTTP {r.status_code}）"
        if is_login:
            return False, f"cookie 已失效（被重定向到登录页，HTTP {r.status_code}）"
        return False, f"无法判定登录态（HTTP {r.status_code}）"

    # ---------- 正常侧：无调试端口的浏览器 ----------
    def build_normal_args(self, url: str, maximized: bool = True,
                          edge_path: str = "") -> list[str]:
        """拼「正常浏览器」启动参数：**不带** --remote-debugging-port"""
        exe = edge_path or find_edge()
        args = [exe, f"--user-data-dir={self.profile_abs}",
                "--no-first-run", "--no-default-browser-check"]
        if maximized:
            args.append("--start-maximized")
        args.append(url)
        return args

    def launch_normal(self, url: str, maximized: bool = True, topmost: bool = True,
                      title_keyword: str = "", wait: float = 25.0,
                      edge_path: str = "") -> bool:
        """用**正常浏览器**打开页面（不带调试端口），最大化 + 置顶"""
        self.close_normal()
        time.sleep(1.0)                  # 等上一轮（调试浏览器/正常浏览器）释放 profile 锁
        exe = edge_path or find_edge()
        if not exe:
            raise RuntimeError("未找到 Microsoft Edge（msedge.exe），无法打开正常浏览器")
        args = self.build_normal_args(url, maximized=maximized, edge_path=exe)
        self.logger.info("启动正常浏览器（无调试端口）: " + " ".join(args))

        before = set(DesktopController.list_browser_windows())
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 优先用「新出现的浏览器窗口」定位（标题可能是 CF 挑战页，靠标题不保险）
        hwnd = self._wait_new_window(before, timeout=wait)
        if hwnd is None:
            hwnd = DesktopController.wait_browser_window(title_keyword, timeout=2.0)
        if hwnd is None:
            self.logger.warning("没等到浏览器窗口，页面可能已打开但无法最大化/置顶")
            return True                      # 页面照样打开了
        self._hwnd = hwnd
        if maximized:
            DesktopController.maximize_window(hwnd)
        DesktopController.activate_window(hwnd)
        if topmost:
            DesktopController.set_topmost(hwnd, True)
        self.logger.info(f"正常浏览器已打开并{'最大化 + 置顶' if topmost else '前置'}: {url}")
        return True

    @staticmethod
    def _wait_new_window(before: set[int], timeout: float = 25.0,
                         interval: float = 0.6) -> Optional[int]:
        """等一个新的浏览器窗口出现（与 before 快照对比）"""
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            now = DesktopController.list_browser_windows()
            new = [h for h in now if h not in before]
            if new:
                return new[0]
            time.sleep(interval)
        return None

    def bring_to_front(self, topmost: bool = True) -> bool:
        """把正常浏览器窗口重新置顶并聚焦"""
        hwnd = self._hwnd or DesktopController.find_browser_window("")
        if not hwnd:
            return False
        DesktopController.maximize_window(hwnd)
        DesktopController.activate_window(hwnd)
        if topmost:
            DesktopController.set_topmost(hwnd, True)
        return True

    def close_normal(self) -> None:
        """关闭由本程序启动的正常浏览器（只杀自己的进程树）"""
        proc, self._proc, self._hwnd = self._proc, None, None
        if not proc or proc.poll() is not None:
            return
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=20)
            time.sleep(1.5)               # 等 profile 锁释放
        except Exception as e:
            self.logger.warning(f"关闭正常浏览器失败: {e}")
        else:
            self.logger.info("已关闭正常浏览器")

    def close_stale(self) -> int:
        """关掉占用本 profile 的 msedge 进程（含 AI 新开的标签页/窗口）"""
        return kill_browsers_on_profile(self.profile_abs, logger=self.logger)


def kill_browsers_on_profile(profile_dir: str, logger=None) -> int:
    """关掉所有命令行里带该 profile 路径的 msedge 进程。

    用途：
    - 任务跑完后确保不留浏览器窗口/标签页（包括 AI 识图时新开的窗口）
    - 下一次运行前清理占用（profile 被占用会导致 cookie 注入 / 登录失败）
    """
    profile = str(profile_dir or "").replace("'", "''")   # 单引号会破坏下面的 PowerShell 字面量
    if not profile:
        return 0
    script = (
        "$p = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{profile}*' }}; "
        "if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
        "-ErrorAction SilentlyContinue }; $p.Count } else { 0 }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return 0
    # 输出可能夹着 PowerShell 警告：从后往前找第一个纯数字行，找不到就当 0
    n = 0
    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if line.isdigit():
            n = int(line)
            break
    if n:
        time.sleep(1.0)
        if logger:
            logger.info(f"已关闭 {n} 个占用该 profile 的浏览器进程")
    return n
