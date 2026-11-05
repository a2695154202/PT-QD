"""HTTP 会话工厂：**默认不使用 Windows 系统代理**。

背景：部分电脑开着系统代理（VPN/加速器/公司网络），`requests` 默认会通过
`trust_env` 自动读取系统代理设置，于是 AI 识别、CookieCloud 同步会莫名其妙超时，
而用户自己的浏览器打开网页却是正常的。

策略（与浏览器侧保持一致）：
- 用户在「全局设置」里配置了代理 → 显式使用该代理
- 没有配置 → 显式 `trust_env = False`，不走系统代理、也不走环境变量代理，直连
"""
from __future__ import annotations

import os
import socket
import time
from typing import Optional
from urllib.parse import quote, urlparse

import requests

_proxy_cfg = None
_configured = False

# Windows 系统代理所在的注册表位置（只读，不改）
_SYS_PROXY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# 本机代理常见的监听端口：Clash(7897/7890)、v2rayN(10809/10808)、SSR(1080)、
# 旧版 Clash(2080)、Privoxy(8118)、Clash Verge(7897)、Surge/其他
COMMON_PROXY_PORTS = (7897, 7890, 10809, 10808, 1080, 2080, 8118, 8889, 7891, 20171)


def configure(proxy_cfg) -> None:
    """启动/加载配置时注入全局代理设置（ProxyConfig）"""
    global _proxy_cfg, _configured
    _proxy_cfg = proxy_cfg
    _configured = True


def _current_proxy():
    global _proxy_cfg, _configured
    if not _configured:
        try:
            from .config import AppConfig
            _proxy_cfg = AppConfig.load().proxy
        except Exception:
            _proxy_cfg = None
        _configured = True
    return _proxy_cfg


def _proxy_url() -> Optional[str]:
    p = _current_proxy()
    if p is None or not getattr(p, "ready", False):
        return None
    url = (p.server or "").strip()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    if p.username:
        scheme, rest = url.split("://", 1)
        cred = quote(p.username, safe="")
        if p.password:
            cred += ":" + quote(p.password, safe="")
        url = f"{scheme}://{cred}@{rest}"
    return url


def proxies() -> Optional[dict]:
    """用户配置了代理时返回 requests 的 proxies 字典，否则 None"""
    url = _proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def session(use_proxy: bool = True, fallback: str = "none") -> requests.Session:
    """构造会话：只用用户配置的代理，绝不吃系统/环境变量代理。

    use_proxy=False 时即使配了代理也不用（例如明确要求直连的请求）。
    fallback="auto" 时，若用户没配代理，会自动找一个本机可用的代理
    （仅 Telegram 这类「国内直连必然不通」的场景使用，见 resolve）。
    """
    s = requests.Session()
    s.trust_env = False          # 关键：忽略 Windows 系统代理与 HTTP(S)_PROXY 环境变量
    px, _ = resolve(use_proxy, fallback)
    if px:
        s.proxies.update(px)
    return s


def request(method: str, url: str, use_proxy: bool = True,
            fallback: str = "none", **kwargs) -> requests.Response:
    """一次性请求（内部用上面的会话）"""
    s = session(use_proxy=use_proxy, fallback=fallback)
    try:
        return s.request(method, url, **kwargs)
    finally:
        s.close()


# ---------------- 本机代理自动发现（仅 fallback="auto" 时使用）----------------

def _tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    """探测端口是否可连接（不发送任何数据，毫秒级）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    return url


def _parse_proxy_server(raw: str) -> str:
    """ProxyServer 可能是 'host:port'，也可能是 'http=h:p;https=h:p;socks=h:p'"""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "=" in raw:
        parts: dict[str, str] = {}
        for seg in raw.split(";"):
            if "=" in seg:
                k, v = seg.split("=", 1)
                parts[k.strip().lower()] = v.strip()
        for key in ("https", "http"):
            if parts.get(key):
                return _normalize(parts[key])
        return ""                       # 只有 socks：requests 需要 PySocks，这里不赌
    return _normalize(raw)


def system_proxy() -> Optional[str]:
    """读 Windows 系统代理设置；未启用或读不到返回 None（只读，不修改注册表）"""
    if os.name != "nt":
        return None
    try:
        import winreg
    except Exception:
        return None
    key = None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SYS_PROXY_KEY)
        try:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            enabled = 0
        if not enabled:
            return None                  # 开关没打开：注册表里可能是上次留下的旧条目
        try:
            raw = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
        except (FileNotFoundError, OSError, TypeError):
            return None
    except Exception:
        return None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return _parse_proxy_server(raw) or None


def _proxy_reachable(url: str) -> bool:
    try:
        u = urlparse(url)
        host, port = u.hostname, u.port
        if not host:
            return False
        if u.scheme in ("socks5", "socks5h", "socks4"):
            return False                 # 同上，没有 PySocks 时不用
        return _tcp_open(host, int(port or 80))
    except Exception:
        return False


_auto_cache: dict = {"at": 0.0, "url": None}
_AUTO_TTL = 300.0


def discover_proxy(force: bool = False) -> Optional[str]:
    """找一个本机可用的代理：系统代理 → 常见本地端口。

    只在用户没配代理、且调用方显式要求 fallback="auto" 时才用。
    结果缓存 5 分钟，避免每次请求都扫端口。
    """
    now = time.time()
    if not force and _auto_cache["at"] and now - _auto_cache["at"] < _AUTO_TTL:
        return _auto_cache["url"]
    found: Optional[str] = None
    sysp = system_proxy()
    if sysp and _proxy_reachable(sysp):
        found = sysp
    if not found:
        for port in COMMON_PROXY_PORTS:
            if _tcp_open("127.0.0.1", port):
                found = f"http://127.0.0.1:{port}"
                break
    _auto_cache["at"] = now
    _auto_cache["url"] = found
    return found


def resolve(use_proxy: bool = True, fallback: str = "none") -> tuple[Optional[dict], str]:
    """解析出本次请求该走哪条路，返回 (proxies, 人话描述)"""
    if not use_proxy:
        return None, "直连"
    px = proxies()
    if px:
        return px, f"代理 {_proxy_url()}"
    if fallback == "auto":
        url = discover_proxy()
        if url:
            return {"http": url, "https": url}, f"自动发现的本机代理 {url}"
    return None, "直连（没找到可用代理）"


def describe() -> str:
    """给日志/界面用的一句话描述当前网络策略"""
    url = _proxy_url()
    return f"用户代理 {url}" if url else "直连（不使用系统代理）"
