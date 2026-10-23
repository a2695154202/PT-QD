"""用 pydoll 完成登录（不触发 Cloudflare），cookie 落盘给「正常浏览器」用。

**为什么需要它**
Playwright/CDP 驱动的浏览器带自动化指纹，audiences 的登录页会被 CF 托管挑战拦住；
pydoll 直连 CDP、不设置 `navigator.webdriver`，实测打开同一个登录页**根本不弹 CF**，
可以直接填账号密码 + 图形验证码。pydoll 与「正常浏览器」共用同一 profile，
登录后 cookie 天然共享，正常浏览器打开即登录状态（配合 core/browser_bridge）。

**实测踩到的四个坑（都已规避）**
1. pydoll 会抓 Edge 的第一个标签（首次启动是「同步确认页」），导航时该标签被关掉，
   后续重连旧 target 会 HTTP 500 —— 必须 `browser.new_tab()` 自己建标签。
2. pydoll 3.0 未加 `--remote-allow-origins=*`，Chromium 111+ 会拒绝 CDP WebSocket。
3. pydoll 3.0 退出时还原 Preferences 备份，新 profile 没有该文件会 FileNotFoundError
   —— 启动前先写一个空 Preferences。
4. CF 挑战页不会触发 load 事件，`go_to` 会抛 PageLoadTimeout —— 吞掉异常后轮询标题。
"""
from __future__ import annotations

import asyncio
import base64
import random
import socket
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .browser_bridge import find_edge
from .logger import get_logger

# 登录页特征
SEL_USER = "username"
SEL_PASS = "password"
SEL_CAPTCHA_INPUT = "imagestring"
SEL_ONECODE = "scode"
SEL_SUBMIT_TEXTS = ("登录", "登陆", "Login", "登录/注册")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _maybe_await(value):
    """pydoll 同类方法在部分对象/版本上返回协程、部分直接返回值，这里统一处理"""
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _prepare_profile(profile_dir: str) -> str:
    """确保 Default/Preferences 存在（pydoll 退出时会还原备份，否则报错）"""
    default_dir = Path(profile_dir) / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    pref = default_dir / "Preferences"
    if not pref.exists():
        pref.write_text(
            '{"sync": {"suppress_start": true}, "signin": {"allowed": false}}',
            encoding="utf-8",
        )
    return str(default_dir)


def build_options(profile_dir: str, headless: bool = False, edge_path: str = ""):
    """构造 pydoll 启动参数（反检测偏好 + 共用 profile）"""
    from pydoll.browser.options import ChromiumOptions

    _prepare_profile(profile_dir)
    o = ChromiumOptions()
    exe = edge_path or find_edge()
    if exe:
        o.binary_location = exe
    o.headless = bool(headless)          # CF 托管挑战需要非无头
    o.start_timeout = 60
    o.add_argument(f"--user-data-dir={profile_dir}")
    o.add_argument("--remote-allow-origins=*")     # 坑 2
    o.add_argument("--window-size=1366,860")
    o.add_argument("--disable-sync")
    fake = int(time.time()) - random.randint(7, 30) * 24 * 3600
    o.browser_preferences = {
        "profile": {
            "last_engagement_time": fake,
            "exit_type": "Normal",
            "exited_cleaned": True,
            "default_content_setting_values": {"notifications": 2, "geolocation": 2},
        },
        "session": {"restore_on_startup": 1},
        "intl": {"accept_languages": "zh-CN,zh,en-US,en"},
    }
    o.webrtc_leak_protection = True
    return o


def cookies_to_dicts(cookies) -> list[dict]:
    """pydoll Cookie → Playwright add_cookies 能吃的 dict（字段名转成小写驼峰兼容）"""
    out: list[dict] = []
    for c in cookies or []:
        def g(key, default=None):
            if isinstance(c, dict):
                return c.get(key, default)
            return getattr(c, key, default)
        name, value = g("name"), g("value")
        if not name:
            continue
        item = {"name": name, "value": value or "",
                "domain": g("domain") or "", "path": g("path") or "/",
                "httpOnly": bool(g("httpOnly", False)),
                "secure": bool(g("secure", False))}
        exp = g("expires")
        if exp:
            item["expires"] = float(exp)
        same_site = g("sameSite")
        if same_site in ("Strict", "Lax", "None"):
            item["sameSite"] = same_site
        out.append(item)
    return out


class PydollLogin:
    """用 pydoll 打开登录页并提交登录（可选 dry_run 只填不提交）"""

    def __init__(self, profile_dir: str, login_url: str, *, headless: bool = False,
                 edge_path: str = "", timeout_s: int = 90):
        self.profile_dir = profile_dir
        self.login_url = login_url
        self.headless = headless
        self.edge_path = edge_path
        self.timeout_s = timeout_s
        self.logger = get_logger()

    async def run(self, username: str, password: str,
                  recognize_captcha: Optional[Callable[[bytes], Awaitable[str]]] = None,
                  totp_code: str = "", dry_run: bool = False,
                  captcha_prompt: str = "captcha") -> tuple[bool, str, list[dict]]:
        """返回 (是否登录成功, 说明, cookies)"""
        from pydoll.browser import Chrome

        options = build_options(self.profile_dir, headless=self.headless,
                                edge_path=self.edge_path)
        port = _free_port()
        self.logger.info(f"pydoll 启动（无 WebDriver 指纹），CDP 端口 {port}")
        async with Chrome(options=options, connection_port=port) as browser:
            await browser.start()
            tab = await browser.new_tab(self.login_url)      # 坑 1：自己建标签
            try:
                await self._wait_loaded(tab)
                if not await self._has_login_form(tab):
                    if await self._looks_logged_in(tab):
                        cookies = cookies_to_dicts(await tab.get_cookies())
                        return True, "打开登录页时已是登录状态", cookies
                    return False, "登录页结构不符合预期（找不到用户名输入框）", []

                await self._fill(tab, SEL_USER, username)
                await self._fill(tab, SEL_PASS, password)

                captcha_msg = ""
                if recognize_captcha is not None:
                    captcha_msg = await self._solve_captcha(tab, recognize_captcha, captcha_prompt)

                if totp_code:
                    await self._fill(tab, SEL_ONECODE, totp_code, optional=True)

                if dry_run:
                    return False, f"dry-run：已填入账号/密码{captcha_msg}，未提交", []

                await self._submit(tab)
                ok = await self._wait_result(tab)
                cookies = cookies_to_dicts(await tab.get_cookies())
                if ok:
                    return True, f"pydoll 登录成功{captcha_msg}", cookies
                return False, f"pydoll 登录后仍未进入登录状态{captcha_msg}", cookies
            finally:
                try:
                    await browser.stop()
                except Exception:
                    pass
                # pydoll 的 stop() 可能只断开 CDP 连接而不结束浏览器进程，
                # 这里按 profile 兜底杀掉，避免任务跑完还留着一个 Edge 窗口
                try:
                    from .browser_bridge import kill_browsers_on_profile
                    kill_browsers_on_profile(self.profile_dir)
                except Exception:
                    pass

    # ---------- 内部步骤 ----------
    async def _wait_loaded(self, tab, wait_s: float = 6.0) -> None:
        """等页面稳定。`new_tab(url)` 已经加载过一次，除非还在空白页才再导航。

        （重复 `go_to` 会让页面重新加载，验证码有可能被刷新掉，白等一轮。）
        """
        cur = ""
        try:
            cur = (await tab.current_url()) or ""
        except Exception:
            pass
        if not cur or cur.startswith("about:") or cur.startswith("edge://"):
            try:
                await asyncio.wait_for(tab.go_to(self.login_url, timeout=30), timeout=45)
            except Exception as e:
                self.logger.info(f"页面加载未在预期时间内完成（CF 挑战页常见）：{type(e).__name__}")
        await asyncio.sleep(wait_s)

    async def _has_login_form(self, tab) -> bool:
        try:
            el = await tab.find(name=SEL_USER, timeout=8, raise_exc=False)
            return el is not None
        except Exception:
            return False

    async def _looks_logged_in(self, tab) -> bool:
        try:
            html = await tab.page_source()
        except Exception:
            return False
        return "logout.php" in html or "userdetails.php" in html

    async def _fill(self, tab, name: str, value: str, optional: bool = False) -> None:
        try:
            el = await tab.find(name=name, timeout=6, raise_exc=False)
            if el is None:
                if not optional:
                    raise RuntimeError(f"找不到输入框 name={name}")
                return
            await el.clear()
            await el.type_text(value, humanize=True)
        except Exception as e:
            if optional:
                self.logger.info(f"可选字段 {name} 填写跳过：{e}")
                return
            raise

    async def _solve_captcha(self, tab, recognize_captcha, prompt_key: str) -> str:
        """截取验证码图片交给 AI，填入输入框（失败重试一次，pydoll 偶发返回 str）"""
        last_err = ""
        for attempt in (1, 2):
            try:
                img = await tab.find(tag_name="img", find_all=True, timeout=5) or []
                target = None
                for el in img:
                    src = await _maybe_await(el.get_attribute("src")) or ""
                    if "regimage" in src or "captcha" in src.lower():
                        target = el
                        break
                if target is None:
                    return "（未找到验证码图片）"
                b64 = await _maybe_await(target.take_screenshot(as_base64=True))
                if not b64:
                    last_err = "验证码截图失败"
                    await asyncio.sleep(1.5)
                    continue
                raw = base64.b64decode(b64 if isinstance(b64, (bytes, str)) else str(b64))
                code = await recognize_captcha(raw)
                if not code:
                    last_err = "AI 未识别出验证码"
                    await asyncio.sleep(1.0)
                    continue
                await self._fill(tab, SEL_CAPTCHA_INPUT, code, optional=True)
                return f"，验证码 {code}"
            except Exception as e:
                last_err = str(e)
                self.logger.warning(f"pydoll 处理验证码第 {attempt} 次失败: {e}")
                await asyncio.sleep(1.0)
        return f"（验证码处理失败：{last_err}）"

    async def _submit(self, tab) -> None:
        submit = None
        try:
            submit = await tab.find(tag_name="input", type="submit", timeout=4, raise_exc=False)
        except Exception:
            submit = None
        if submit is None:
            for text in SEL_SUBMIT_TEXTS:
                try:
                    submit = await tab.find(text=text, timeout=2, raise_exc=False)
                    if submit is not None:
                        break
                except Exception:
                    continue
        if submit is None:
            raise RuntimeError("找不到登录提交按钮")
        await submit.click(humanize=True)
        self.logger.info("pydoll 已点击登录")

    async def _wait_result(self, tab, timeout_s: float = 40.0) -> bool:
        """轮询判断是否登录成功（URL 变化 / 出现 logout.php / 出现失败提示）"""
        start = time.time()
        while time.time() - start < timeout_s:
            await asyncio.sleep(2)
            try:
                url = (await tab.current_url()) or ""
                html = await tab.page_source()
            except Exception:
                continue
            if "login.php" not in url and ("logout.php" in html or "userdetails.php" in html):
                return True
            if await self._looks_logged_in(tab):
                return True
            if any(k in html for k in ("用户名或密码错误", "验证码错误", "登录失败", "两次验证码")):
                self.logger.warning("pydoll 登录页面出现失败提示")
                return False
        return False


async def pydoll_login(profile_dir: str, login_url: str, username: str, password: str,
                       recognize_captcha=None, *, totp_code: str = "",
                       headless: bool = False, timeout_s: int = 90,
                       dry_run: bool = False) -> tuple[bool, str, list[dict]]:
    """便捷入口：返回 (是否成功, 说明, cookies)"""
    login = PydollLogin(profile_dir, login_url, headless=headless, timeout_s=timeout_s)
    return await login.run(username, password, recognize_captcha=recognize_captcha,
                           totp_code=totp_code, dry_run=dry_run)
