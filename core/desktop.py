"""Windows 桌面控制：屏幕截图 + 鼠标/键盘原生输入（识图点击用）。

**与 core.browser 的区别**：
- `core.browser` 用 Playwright/CDP 驱动 Edge，浏览器带着自动化指纹，
  Cloudflare 能识别出"这是自动化浏览器"，对人机验证卡得特别死。
- 本模块用操作系统原生输入（`SetCursorPos` / `mouse_event` / `keybd_event`）
  加屏幕截图，等价于"真人看着屏幕点鼠标"，浏览器是用户**平时那个正常浏览器**，
  CF 看到的就是普通用户，不会触发自动化拦截。

**零新依赖**：截图用 GDI `BitBlt` → numpy 数组 → cv2 编码 PNG；
cv2 / numpy 项目里本来就有（opencv-python-headless）。

**坐标约定**：截图是屏幕物理像素，`click(x, y)` 的 x/y 也是屏幕物理像素，
一一对应，不需要 DPI 换算（启动时调用 `SetProcessDPIAware` 保证不被虚拟化）。
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from ctypes import wintypes
from typing import Optional

import cv2
import numpy as np

from .logger import get_logger

# ---- Win32 常量 ----
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_RETURN = 0x0D
SW_RESTORE = 9
SW_SHOW = 5
SM_CXSCREEN, SM_CYSCREEN = 0, 1
PW_RENDERFULLCONTENT = 2


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

_DPI_AWARE_SET = False


def _ensure_dpi_aware() -> None:
    """让本进程感知 DPI，保证坐标不被 Windows 虚拟化缩放"""
    global _DPI_AWARE_SET
    if _DPI_AWARE_SET:
        return
    for fn_name in ("SetProcessDpiAwareness", "SetProcessDPIAware"):
        try:
            fn = getattr(user32, fn_name, None)
            if fn is None:
                fn = getattr(ctypes.windll.shcore, fn_name, None)
            if fn is not None:
                if fn_name == "SetProcessDpiAwareness":
                    fn(2)  # PROCESS_PER_MONITOR_DPI_AWARE
                else:
                    fn()
                break
        except Exception:
            continue
    _DPI_AWARE_SET = True


class DesktopController:
    """OS 级桌面操作：截图 / 鼠标 / 键盘 / 窗口。

    ``dry_run=True`` 时只记录动作不执行（供离线测试）。
    """

    def __init__(self, screenshot_dir: str = "data/screenshots", dry_run: bool = False):
        from .config import _abs
        from pathlib import Path
        self.screenshot_dir = Path(_abs(screenshot_dir))
        self.dry_run = bool(dry_run)
        self.logger = get_logger()
        self.last_action = ""
        if not self.dry_run:
            _ensure_dpi_aware()

    # ---------- 屏幕尺寸 ----------
    def screen_size(self) -> tuple[int, int]:
        _ensure_dpi_aware()
        return int(user32.GetSystemMetrics(SM_CXSCREEN)), \
               int(user32.GetSystemMetrics(SM_CYSCREEN))

    # ---------- 截图 ----------
    def capture_screen(self, x: int = 0, y: int = 0,
                       width: Optional[int] = None, height: Optional[int] = None,
                       name: Optional[str] = None) -> bytes:
        """截取屏幕区域，返回 PNG bytes（物理像素，与鼠标坐标同尺度）"""
        if self.dry_run:
            w, h = int(width or 800), int(height or 600)
            ok, png = cv2.imencode(".png", np.zeros((h, w, 3), dtype=np.uint8))
            if not ok:
                raise RuntimeError("占位截图编码失败")
            data = png.tobytes()
            if name:
                self.screenshot_dir.mkdir(parents=True, exist_ok=True)
                (self.screenshot_dir / f"{name}.png").write_bytes(data)
            return data
        _ensure_dpi_aware()
        sw, sh = self.screen_size()
        x = max(0, min(int(x), sw - 1))
        y = max(0, min(int(y), sh - 1))
        w = int(width) if width else sw - x
        h = int(height) if height else sh - y
        w = max(1, min(w, sw - x))
        h = max(1, min(h, sh - y))

        hwnd = user32.GetDesktopWindow()
        hdc = user32.GetDC(hwnd)
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(memdc, bmp)
        gdi32.BitBlt(memdc, 0, 0, w, h, hdc, x, y, SRCCOPY)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h   # 负数 = 自上而下，省去翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)

        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)

        arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
        bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        ok, png = cv2.imencode(".png", bgr)
        if not ok:
            raise RuntimeError("屏幕截图编码失败")
        data = png.tobytes()
        if name:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            (self.screenshot_dir / f"{name}.png").write_bytes(data)
            self.logger.info(f"桌面截图已保存: {self.screenshot_dir / (name + '.png')}")
        return data

    # ---------- 鼠标 ----------
    def move(self, x: int, y: int, steps: int = 8) -> None:
        if self.dry_run:
            self.last_action = f"move({int(x)}, {int(y)})"
            return
        _ensure_dpi_aware()
        cx, cy = self.get_cursor_pos()
        for i in range(1, max(1, steps) + 1):
            t = i / max(1, steps)
            user32.SetCursorPos(int(cx + (x - cx) * t), int(cy + (y - cy) * t))
            time.sleep(0.012)

    def get_cursor_pos(self) -> tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)

    def click(self, x: int, y: int, button: str = "left") -> None:
        if self.dry_run:
            self.last_action = f"click({int(x)}, {int(y)}, {button})"
            self.logger.info(f"[dry-run] 点击 ({int(x)}, {int(y)}) {button}")
            return
        self.move(x, y)
        time.sleep(0.08)
        if button == "right":
            user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        else:
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.logger.info(f"已点击 ({int(x)}, {int(y)}) {button}")

    def double_click(self, x: int, y: int) -> None:
        if self.dry_run:
            self.last_action = f"dblclick({int(x)}, {int(y)})"
            return
        self.click(x, y)
        time.sleep(0.06)
        self.click(x, y)

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 700) -> None:
        """拟人化拖动（滑块验证码 / 拼图）"""
        if self.dry_run:
            self.last_action = f"drag({int(x1)},{int(y1)} -> {int(x2)},{int(y2)})"
            self.logger.info(f"[dry-run] 拖动 ({int(x1)},{int(y1)}) → ({int(x2)},{int(y2)})")
            return
        self.move(x1, y1)
        time.sleep(0.12)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.12)
        steps = max(6, int(duration_ms) // 18)
        for i in range(1, steps + 1):
            t = i / steps
            user32.SetCursorPos(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
            time.sleep(max(0.005, duration_ms / 1000 / steps))
        time.sleep(0.15)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.logger.info(f"已拖动 ({int(x1)},{int(y1)}) → ({int(x2)},{int(y2)})")

    def scroll(self, dy: int = 300) -> None:
        if self.dry_run:
            self.last_action = f"scroll({int(dy)})"
            return
        # 每格 120，dy 正数向下
        notches = int(-dy / 120) or (1 if dy > 0 else -1)
        user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, notches * 120, 0)
        self.logger.info(f"已滚动 {int(dy)}px")

    # ---------- 键盘 / 文本 ----------
    def type_text(self, text: str) -> None:
        """输入文本（含中文）：写剪贴板 → Ctrl+V 粘贴"""
        if self.dry_run:
            self.last_action = f"type({text[:20]!r})"
            self.logger.info(f"[dry-run] 输入 {text[:30]!r}")
            return
        self._set_clipboard(text)
        time.sleep(0.06)
        self.hotkey("ctrl", "v")
        self.logger.info(f"已输入文本（{len(text)} 字符）")

    def press_enter(self) -> None:
        if self.dry_run:
            self.last_action = "enter"
            return
        user32.keybd_event(VK_RETURN, 0, 0, 0)
        time.sleep(0.05)
        user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
        self.logger.info("已按回车")

    def hotkey(self, *keys: str) -> None:
        if self.dry_run:
            self.last_action = f"hotkey({'+'.join(keys)})"
            return
        vks = [self._vk(k) for k in keys]
        for vk in vks:
            user32.keybd_event(vk, 0, 0, 0)
            time.sleep(0.03)
        for vk in reversed(vks):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
            time.sleep(0.03)

    @staticmethod
    def _vk(key: str) -> int:
        k = key.strip().lower()
        named = {
            "ctrl": VK_CONTROL, "control": VK_CONTROL,
            "shift": 0x10, "alt": 0x12, "enter": VK_RETURN,
            "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
            "backspace": 0x08, "delete": 0x2E,
        }
        if k in named:
            return named[k]
        if len(k) == 1:
            return ord(k.upper())
        raise ValueError(f"未知按键: {key}")

    @staticmethod
    def _set_clipboard(text: str) -> None:
        if not user32.OpenClipboard(0):
            raise RuntimeError("打开剪贴板失败")
        try:
            user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not h:
                raise RuntimeError("分配内存失败")
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
        finally:
            user32.CloseClipboard()

    # ---------- 窗口 ----------
    @staticmethod
    def open_browser(url: str, browser_path: Optional[str] = None) -> None:
        """用**正常浏览器**打开 URL（不带任何自动化标志 / 调试端口）"""
        if browser_path:
            subprocess.Popen([browser_path, url])
            return
        # start 命令走系统默认浏览器（用户平时那个）
        subprocess.Popen(["cmd", "/c", "start", "", url])
        get_logger().info(f"已用正常浏览器打开: {url}")

    @staticmethod
    def find_browser_window(title_keyword: str = "") -> Optional[int]:
        """找浏览器窗口句柄（匹配窗口类 Chrome_WidgetWin_1，Edge/Chrome 都是它）"""
        _ensure_dpi_aware()
        result: list[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _l):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                is_browser = cls.value in ("Chrome_WidgetWin_1", "MozillaWindowClass")
                if not is_browser:
                    return True
                if title_keyword and title_keyword.lower() not in title.lower():
                    return True
                result.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return result[0] if result else None

    @staticmethod
    def window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)

    @staticmethod
    def activate_window(hwnd: int) -> bool:
        """把窗口提到最前（截图前调用，避免被其它窗口遮挡）"""
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.35)
            return True
        except Exception:
            return False

    def capture_window(self, hwnd: int, name: Optional[str] = None) -> tuple[bytes, tuple[int, int, int, int]]:
        """截取指定窗口区域（先激活前置），返回 (PNG bytes, 窗口矩形)"""
        rect = self.window_rect(hwnd)
        if rect is None:
            raise RuntimeError("取不到窗口矩形")
        self.activate_window(hwnd)
        x, y, w, h = rect
        data = self.capture_screen(x, y, w, h, name=name)
        return data, rect
