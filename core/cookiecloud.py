"""CookieCloud 同步：从服务器拉取加密的 Cookie 并在本地解密注入浏览器。

协议（easychen/CookieCloud）：
- GET {server}/get/{uuid} -> {"encrypted": "...", "crypto_type": "legacy"|"aes-128-cbc-fixed"}
- key = md5(uuid + "-" + password) 十六进制串的前 16 个字符（UTF-8 16 字节）
- aes-128-cbc-fixed: IV = 16 个零字节, PKCS7, 密文为裸 Base64（新格式，推荐）
- legacy: CryptoJS 口令格式, "Salted__" + 8 字节盐, EVP_BytesToKey(MD5) 派生 AES-256-CBC
- 明文 JSON: {"cookie_data": {domain: [cookie...]}, "local_storage_data": {...}}
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Optional

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .config import CookieCloudConfig
from .logger import get_logger


class CookieCloudError(RuntimeError):
    pass


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 解密 + 去 PKCS7 填充"""
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    """OpenSSL EVP_BytesToKey(MD5, 1 iteration) — CryptoJS 口令格式用"""
    d = b""
    d_i = b""
    while len(d) < key_len + iv_len:
        d_i = hashlib.md5(d_i + password + salt).digest()
        d += d_i
    return d[:key_len], d[key_len:key_len + iv_len]


@dataclass
class CookieCloudClient:
    cfg: CookieCloudConfig
    logger: object = None

    def __post_init__(self):
        if self.logger is None:
            self.logger = get_logger()

    # ---------- 网络层 ----------
    def fetch_raw(self) -> dict:
        """GET /get/{uuid}，返回 {"encrypted":..., "crypto_type":...}"""
        if not (self.cfg.server and self.cfg.uuid):
            raise CookieCloudError("CookieCloud 未配置 server/uuid")
        url = self.cfg.server.rstrip("/") + "/get/" + self.cfg.uuid
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise CookieCloudError(f"CookieCloud 拉取失败: {e}") from e
        except json.JSONDecodeError as e:
            raise CookieCloudError(f"CookieCloud 响应非 JSON: {resp.text[:200]}") from e

    # ---------- 解密层 ----------
    def _key(self) -> bytes:
        digest = hashlib.md5(f"{self.cfg.uuid}-{self.cfg.password}".encode("utf-8")).hexdigest()
        return digest[:16].encode("utf-8")  # 16 字节 AES-128 key

    def decrypt(self, encrypted: str, crypto_type: str = "aes-128-cbc-fixed") -> dict:
        key = self._key()
        raw = base64.b64decode(encrypted)
        if crypto_type == "legacy" or raw[:8] == b"Salted__":
            if raw[:8] != b"Salted__":
                raise CookieCloudError("legacy 密文缺少 Salted__ 头")
            salt = raw[8:16]
            dk, iv = _evp_bytes_to_key(key, salt, 32, 16)
            plain = _aes_cbc_decrypt(dk, iv, raw[16:])
        else:
            plain = _aes_cbc_decrypt(key, b"\x00" * 16, raw)
        return json.loads(plain.decode("utf-8"))

    def fetch(self) -> dict:
        """拉取并解密，返回完整明文 dict"""
        raw = self.fetch_raw()
        return self.decrypt(raw.get("encrypted", ""), raw.get("crypto_type", "aes-128-cbc-fixed"))

    # ---------- 应用层 ----------
    @staticmethod
    def _domain_match(cookie_domain: str, wanted: list[str]) -> bool:
        """域名匹配：支持父子域双向匹配（如 cookie 存 .site-d.example，任务用 site-d.example）"""
        if not wanted:
            return True
        cd = cookie_domain.lstrip(".").lower()
        if not cd:
            return False
        for w in wanted:
            wd = w.lstrip(".").lower()
            if not wd:
                continue
            if cd == wd:
                return True
            # 用 "." 边界判断父子域，避免 evil-site-d.example 被误判为 site-d.example
            if cd.endswith("." + wd) or wd.endswith("." + cd):
                return True
        return False

    @staticmethod
    def _to_playwright_cookie(c: dict) -> Optional[dict]:
        """CookieCloud cookie -> Playwright add_cookies 格式"""
        name, value = c.get("name"), c.get("value")
        if not name:
            return None
        same_site_map = {"unspecified": "Lax", "lax": "Lax", "strict": "Strict", "no_restriction": "None"}
        out = {
            "name": name,
            "value": value if isinstance(value, str) else json.dumps(value),
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
            "sameSite": same_site_map.get(str(c.get("sameSite", "lax")).lower(), "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp:
            try:
                out["expires"] = float(exp)
            except (TypeError, ValueError):
                out["expires"] = -1
        else:
            out["expires"] = -1
        return out

    def get_cookies(self, domains: Optional[list[str]] = None) -> list[dict]:
        """解密并筛选出目标域名的 cookie（Playwright 格式）"""
        data = self.fetch()
        cookie_data = data.get("cookie_data", {}) or {}
        wanted = domains if domains is not None else self.cfg.domains
        result = []
        for domain, cookies in cookie_data.items():
            if not self._domain_match(domain, wanted):
                continue
            for c in cookies:
                pc = self._to_playwright_cookie(c)
                if pc:
                    result.append(pc)
        self.logger.info(f"CookieCloud 解密得到 {len(result)} 个 cookie（域名筛选: {wanted or '全部'}）")
        return result

    def get_local_storage(self, domains: Optional[list[str]] = None) -> dict:
        """解密并获取 localStorage 数据 {origin: {key: value}}"""
        data = self.fetch()
        ls = data.get("local_storage_data", {}) or {}
        wanted = domains if domains is not None else self.cfg.domains
        result = {}
        for origin, items in ls.items():
            if wanted and not any(origin.endswith(w.lstrip(".")) for w in wanted):
                continue
            result[origin] = items
        return result

    def test_connection(self) -> tuple[bool, str]:
        """测试连通性，返回 (成功?, 消息)"""
        try:
            cookies = self.get_cookies()
            domains = sorted({c["domain"].lstrip(".") for c in cookies})
            return True, f"连接成功，解密得到 {len(cookies)} 个 cookie，涉及域名: {', '.join(domains[:10]) or '无'}"
        except Exception as e:
            return False, str(e)
