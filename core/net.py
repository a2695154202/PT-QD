"""HTTP 会话工厂：**默认不使用 Windows 系统代理**。

背景：部分电脑开着系统代理（VPN/加速器/公司网络），`requests` 默认会通过
`trust_env` 自动读取系统代理设置，于是 AI 识别、CookieCloud 同步会莫名其妙超时，
而用户自己的浏览器打开网页却是正常的。

策略（与浏览器侧保持一致）：
- 用户在「全局设置」里配置了代理 → 显式使用该代理
- 没有配置 → 显式 `trust_env = False`，不走系统代理、也不走环境变量代理，直连
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote

import requests

_proxy_cfg = None
_configured = False


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


def session(use_proxy: bool = True) -> requests.Session:
    """构造会话：只用用户配置的代理，绝不吃系统/环境变量代理。

    use_proxy=False 时即使配了代理也不用（例如明确要求直连的请求）。
    """
    s = requests.Session()
    s.trust_env = False          # 关键：忽略 Windows 系统代理与 HTTP(S)_PROXY 环境变量
    px = proxies() if use_proxy else None
    if px:
        s.proxies.update(px)
    return s


def request(method: str, url: str, use_proxy: bool = True, **kwargs) -> requests.Response:
    """一次性请求（内部用上面的会话）"""
    s = session(use_proxy=use_proxy)
    try:
        return s.request(method, url, **kwargs)
    finally:
        s.close()


def describe() -> str:
    """给日志/界面用的一句话描述当前网络策略"""
    url = _proxy_url()
    return f"用户代理 {url}" if url else "直连（不使用系统代理）"
