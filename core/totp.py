"""TOTP 二次验证（2FA）：支持谷歌身份验证器二维码导入。

支持两种二维码格式：
1. otpauth://totp/ISSUER:ACCOUNT?secret=XXX   —— 站点添加验证器时的标准格式
2. otpauth-migration://offline?data=<protobuf> —— 谷歌验证器 App「导出账号」的迁移格式
   （内部为 protobuf，可包含多个账号，本模块用手写 wire-format 解析，无需 protoc）
"""
from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import cv2
import numpy as np
import pyotp



class TOTPError(RuntimeError):
    pass


# ---------------------------------------------------------------- 二维码图片解码
def _read_image(path: str):
    """兼容中文路径的图片读取"""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        raise TOTPError(f"无法读取图片: {path}")
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise TOTPError(f"图片解码失败: {path}")
    return img


def _candidate_images(img):
    """生成多种预处理变体：多尺度 × 多阈值 × 反色 × 对比度增强。

    截图类二维码常有缩放模糊、屏幕反光、边框干扰，单一算法容易失败。
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yield img
    yield gray
    for scale in (1.0, 0.5, 1.5, 2.0, 3.0):
        if scale == 1.0:
            g = gray
        else:
            h, w = gray.shape[:2]
            if max(h, w) * scale > 4000:  # 超大图放大后解码极慢，跳过
                continue
            interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
            g = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=interp)
        yield g
        yield cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 51, 10)
        yield cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                    cv2.THRESH_BINARY, 31, 5)
        _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield otsu
        yield cv2.bitwise_not(otsu)  # 深色背景二维码
    try:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        yield clahe
        yield cv2.adaptiveThreshold(clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 51, 10)
        yield cv2.resize(clahe, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    except Exception:
        pass


def _opencv_decode(img) -> str:
    detector = cv2.QRCodeDetector()
    for cand in _candidate_images(img):
        try:
            text, _, _ = detector.detectAndDecode(cand)
        except cv2.error:
            text = ""
        if text:
            return text
    for cand in _candidate_images(img):
        try:
            ok, texts, _, _ = detector.detectAndDecodeMulti(cand)
            if ok:
                for t in texts:
                    if t:
                        return t
        except cv2.error:
            continue
    return ""


def _zbar_decode(img) -> str:
    """pyzbar 后备（对模糊/带边框二维码识别率更高），优先返回 otpauth 内容"""
    try:
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception:
        return ""
    fallback = ""
    for cand in _candidate_images(img):
        try:
            results = zbar_decode(cand)
        except Exception:
            continue
        for r in results:
            try:
                data = r.data.decode("utf-8", "replace")
            except Exception:
                continue
            if data.startswith("otpauth"):
                return data
            if data and not fallback:
                fallback = data
    return fallback


def _save_debug_variants(img, limit: int = 3) -> None:
    """失败时保存少量预处理图，便于排查（data/screenshots/qr_debug_*.png）"""
    try:
        from .config import LOG_DIR
        out_dir = LOG_DIR.parent / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, cand in enumerate(_candidate_images(img)):
            if i >= limit:
                break
            cv2.imencode(".png", cand)[1].tofile(str(out_dir / f"qr_debug_{i}.png"))
    except Exception:
        pass


def decode_qr_image(path: str) -> str:
    """解码二维码图片，返回其中的文本内容（OpenCV 多策略 → pyzbar 后备）"""
    img = _read_image(path)
    text = _opencv_decode(img)
    if text:
        return text
    text = _zbar_decode(img)
    if text:
        return text
    _save_debug_variants(img)
    raise TOTPError(
        "未能在图片中识别到二维码。请确认：\n"
        "1) 图片中二维码完整清晰（可只截取二维码区域，避免整屏截图或过度缩放）；\n"
        "2) 若二维码来自手机拍摄的屏幕，尽量正对拍摄、减少反光；\n"
        "3) 仍失败时，可直接在任务配置页填写 32 位 base32 密钥（secret）。\n"
        "已保存排查用预处理图到 data/screenshots/qr_debug_*.png"
    )


# ---------------------------------------------------------------- protobuf 解析（otpauth-migration）
def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        if pos >= len(buf):
            raise TOTPError("protobuf varint 解析越界")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _iter_fields(buf: bytes):
    """遍历 protobuf wire-format 字段 -> (field_number, wire_type, value)"""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        fn, wt = tag >> 3, tag & 7
        if wt == 0:
            val, pos = _read_varint(buf, pos)
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            val = buf[pos:pos + ln]
            pos += ln
        elif wt == 1:
            val = buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            val = buf[pos:pos + 4]
            pos += 4
        else:
            raise TOTPError(f"不支持的 protobuf wire type: {wt}")
        yield fn, wt, val


def _b32_encode(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def parse_migration(uri: str) -> list["TwoFactorAuth"]:
    """解析 otpauth-migration:// 链接，返回多个账号"""
    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    data = (params.get("data") or [""])[0]
    if not data:
        raise TOTPError("迁移链接缺少 data 参数")
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    accounts: list[TwoFactorAuth] = []
    for fn, wt, val in _iter_fields(raw):
        if fn == 1 and wt == 2:  # otp_parameters
            secret, name, issuer = "", "", ""
            for f2, w2, v2 in _iter_fields(val):
                if f2 == 1 and w2 == 2:
                    secret = _b32_encode(v2)
                elif f2 == 2 and w2 == 2:
                    name = v2.decode("utf-8", "replace")
                elif f2 == 3 and w2 == 2:
                    issuer = v2.decode("utf-8", "replace")
            if secret:
                accounts.append(TwoFactorAuth(secret=secret, issuer=issuer, account=name))
    if not accounts:
        raise TOTPError("迁移链接中未解析到任何账号")
    return accounts


def parse_otpauth(uri: str) -> "TwoFactorAuth":
    """解析单个 otpauth://totp/... 链接"""
    if uri.startswith("otpauth-migration://"):
        accounts = parse_migration(uri)
        if len(accounts) != 1:
            raise TOTPError(f"迁移格式含 {len(accounts)} 个账号，请使用多账号导入")
        return accounts[0]
    if not uri.startswith("otpauth://"):
        raise TOTPError(f"无法识别的二维码内容: {uri[:80]}")
    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    secret = (params.get("secret") or [""])[0].replace(" ", "").upper()
    if not secret:
        raise TOTPError("二维码中缺少 secret 参数")
    issuer = (params.get("issuer") or [""])[0]
    label = unquote(parsed.path.lstrip("/"))
    account = label.split(":", 1)[-1] if ":" in label else label
    if not issuer and ":" in label:
        issuer = label.split(":", 1)[0]
    return TwoFactorAuth(secret=secret, issuer=issuer, account=account)


def load_from_qr(path: str) -> list["TwoFactorAuth"]:
    """从二维码图片导入（自动识别两种格式），返回账号列表"""
    uri = decode_qr_image(path)
    if uri.startswith("otpauth-migration://"):
        return parse_migration(uri)
    return [parse_otpauth(uri)]


# ---------------------------------------------------------------- TOTP 本体
@dataclass
class TwoFactorAuth:
    """TOTP 动态验证码生成器"""
    secret: str
    issuer: str = ""
    account: str = ""

    def __post_init__(self):
        s = self.secret.replace(" ", "").upper()
        self.secret = s + "=" * ((-len(s)) % 8)
        try:
            self._totp = pyotp.TOTP(self.secret)
        except Exception as e:
            raise TOTPError(f"无效的 TOTP secret: {e}") from e

    @classmethod
    def from_qr_image(cls, path: str) -> "TwoFactorAuth":
        """从二维码导入单个账号（迁移格式含多账号时会报错）"""
        return parse_otpauth(decode_qr_image(path))

    def now_code(self) -> str:
        """当前 6 位验证码"""
        return self._totp.now()

    def remaining(self) -> int:
        """当前验证码剩余有效秒数"""
        interval = self._totp.interval
        return interval - (int(time.time()) % interval)

    def verify(self, code: str) -> bool:
        return self._totp.verify(code)

    def describe(self) -> str:
        who = f"{self.issuer}/{self.account}" if (self.issuer or self.account) else "未命名"
        return f"{who} (secret=****{self.secret[-4:]})"

    def to_dict(self) -> dict:
        return {"issuer": self.issuer, "name": self.account, "secret": self.secret}

    @classmethod
    def from_dict(cls, d: dict) -> "TwoFactorAuth":
        return cls(secret=d["secret"], issuer=d.get("issuer", ""), account=d.get("name", ""))
