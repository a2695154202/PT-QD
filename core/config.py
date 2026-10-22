"""配置系统：全局配置 + 任务配置（YAML）

所有用户设置均落地为项目/exe 同目录下的 YAML 文件，启动时自动载入：
- config/config.yaml    全局设置（浏览器 / AI / CookieCloud / 日志）
- config/tasks/*.yaml   各签到任务设置（账号、2FA 勾选与验证器绑定、CF 勾选等）
- config/twofa.yaml     2FA 凭证库（谷歌验证器二维码导入的账号）
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


def _project_root() -> Path:
    """项目根目录：源码运行=项目目录；打包 exe 运行=exe 所在目录（配置与数据随 exe 走）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
CONFIG_DIR = PROJECT_ROOT / "config"
TASKS_CONFIG_DIR = CONFIG_DIR / "tasks"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"


def _abs(p: str) -> str:
    """相对路径一律相对于项目根目录（exe 目录）解析为绝对路径"""
    path = Path(p)
    return str(path if path.is_absolute() else (PROJECT_ROOT / path))


def _q(v) -> str:
    """生成 YAML 安全的标量字符串（避免密码等特殊字符破坏格式）"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return '""'
    return json.dumps(str(v), ensure_ascii=False)


def _as_bool(value, default: bool = False) -> bool:
    """健壮布尔解析：兼容 YAML 中手写的 "true"/"false"/"yes"/"on" 等字符串"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "是", "开")
    return bool(value)


@dataclass
class BrowserConfig:
    mode: str = "launch"                    # launch=程序启动Edge; attach=连接已运行的Edge
    cdp_endpoint: str = "http://127.0.0.1:9222"
    debugging_port: int = 9222              # launch 模式下开放的远程调试端口
    user_data_dir: str = "data/edge_profile"
    headless: bool = False
    slow_mo: int = 50                       # 每步操作间隔(ms)，拟人化
    default_timeout: int = 15000            # 默认等待超时(ms)
    viewport_width: int = 1440
    viewport_height: int = 900
    stealth: bool = True                    # 反自动化检测
    fullscreen: bool = True                 # 启动后全屏（窗口铺满屏幕）
    always_on_top: bool = True              # 浏览器窗口置顶显示

    @property
    def user_data_dir_abs(self) -> str:
        return _abs(self.user_data_dir)


@dataclass
class AIConfig:
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    vision_model: str = ""                  # 留空则复用 model
    timeout: int = 60
    proactive: bool = True                  # 是否允许 AI 主动介入（CF/滑块等）
    agent_fallback: bool = True             # CF 自动处理失败后，是否用 AI 看图驱动浏览器兜底
    prompts: dict = field(default_factory=lambda: {
        "captcha": (
            "这是一张网站登录/签到用的图形验证码图片，通常是4位字母数字。"
            "请只输出验证码内容本身，不要输出任何解释、标点或多余字符。"
        ),
        "popup_captcha": (
            "这是一张签到弹窗中的图形验证码图片。请只输出验证码内容本身，"
            "不要输出任何解释、标点或多余字符。"
        ),
        "slider": (
            "这是一张滑块验证码的截图，背景图中有一处缺口。"
            "请返回缺口左边缘距离图片左侧的像素 x 坐标，只输出 JSON：{\"gap_x\": 数字}"
        ),
        "challenge": (
            "这是一张 Cloudflare 人机验证控件的截图（只截取控件所在区域）。"
            "如果图中存在一个尚未勾选的复选框（通常在控件左侧、是一个方框），"
            "请返回该复选框在图片中的像素坐标，格式为 JSON：{\"click\": [x, y]}；"
            "如果已经显示勾选、正在旋转验证、或者图里没有可点击的复选框，"
            "请返回 {\"click\": null}。只输出 JSON，不要任何解释。"
        ),
    })


@dataclass
class CookieCloudConfig:
    enabled: bool = False
    mode: str = "overwrite"                 # overwrite=覆盖到浏览器(拉取注入); paused=暂停同步
    server: str = ""                        # 如 http://127.0.0.1:8088
    uuid: str = ""
    password: str = ""                      # 同步密钥
    domains: list = field(default_factory=list)  # 仅同步这些域名，空=全部站点

    MODES = ("overwrite", "paused")


@dataclass
class ProxyConfig:
    """全局代理设置：任务勾选「走代理」时才使用；未勾选的任务强制直连"""
    enabled: bool = False
    server: str = ""                        # http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
    username: str = ""
    password: str = ""

    def for_playwright(self) -> dict:
        """转成 Playwright proxy 参数"""
        p = {"server": self.server.strip()}
        if self.username:
            p["username"] = self.username
        if self.password:
            p["password"] = self.password
        return p

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.server.strip())


@dataclass
class ScheduleConfig:
    """每日定时运行：到点自动跑一遍启用中的任务"""
    enabled: bool = False
    time: str = "09:30"                     # 每天运行时间 HH:MM（24 小时制）
    skip_signed: bool = True                # 今天已签到的任务不再跑
    catch_up: bool = True                   # 错过时间点（程序后开）也补跑当天一次

    def is_valid_time(self) -> bool:
        from .scheduler import parse_hhmm
        return parse_hhmm(self.time) is not None


@dataclass
class AppConfig:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    cookiecloud: CookieCloudConfig = field(default_factory=CookieCloudConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    log_level: str = "INFO"
    log_file: str = "data/logs/app.log"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        path = path or (CONFIG_DIR / "config.yaml")
        cfg = cls()
        if not Path(path).exists():
            return cfg
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg.log_level = data.get("logging", {}).get("level", cfg.log_level)
        cfg.log_file = data.get("logging", {}).get("file", cfg.log_file)
        for key, dc in (("browser", BrowserConfig), ("ai", AIConfig),
                        ("cookiecloud", CookieCloudConfig), ("proxy", ProxyConfig),
                        ("schedule", ScheduleConfig)):
            section = data.get(key, {}) or {}
            current = getattr(cfg, key)
            merged = {}
            for fname in dc.__dataclass_fields__:
                merged[fname] = section[fname] if fname in section else getattr(current, fname)
            setattr(cfg, key, dc(**merged))
        return cfg

    def save(self, path: Optional[Path] = None) -> str:
        path = path or (CONFIG_DIR / "config.yaml")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = {
            "browser": {f.name: getattr(self.browser, f.name) for f in BrowserConfig.__dataclass_fields__.values()},
            "ai": {f.name: getattr(self.ai, f.name) for f in AIConfig.__dataclass_fields__.values()},
            "cookiecloud": {f.name: getattr(self.cookiecloud, f.name)
                            for f in CookieCloudConfig.__dataclass_fields__.values()},
            "proxy": {f.name: getattr(self.proxy, f.name) for f in ProxyConfig.__dataclass_fields__.values()},
            "schedule": {f.name: getattr(self.schedule, f.name)
                         for f in ScheduleConfig.__dataclass_fields__.values()},
            "logging": {"level": self.log_level, "file": self.log_file},
        }
        header = (
            "# ============ 全局配置（可在图形界面「全局设置」中修改）============\n"
            "# browser.mode: launch=程序启动Edge并开放调试端口；attach=连接已运行的Edge\n"
            "# browser.fullscreen / always_on_top: 启动后全屏 / 窗口置顶（仅 Windows 生效）\n"
            "# ai: 任意 OpenAI 兼容接口；识别验证码需支持视觉的模型\n"
            "# cookiecloud.mode: overwrite=拉取并覆盖到浏览器 / paused=暂停同步\n"
            "# proxy: 全局代理；任务配置里勾选「走代理」的任务才使用它，未勾选的任务强制直连\n"
            "# schedule: 每日定时运行（time 为 HH:MM）；skip_signed=跳过今天已签到的任务；\n"
            "#           catch_up=错过时间点（程序后开）也补跑当天一次\n"
            "# logging: 日志级别与全局日志文件\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(header)
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        return str(path)


@dataclass
class TaskConfig:
    """单个签到任务的配置"""
    id: str
    name: str
    enabled: bool = True
    url: str = ""
    account: dict = field(default_factory=dict)   # username/password/two_step
    options: dict = field(default_factory=dict)   # has_2fa/has_cf/max_login_retry...
    path: Optional[str] = None

    # ---- 便捷属性 ----
    @property
    def username(self) -> str:
        return self.account.get("username", "") or ""

    @property
    def password(self) -> str:
        return self.account.get("password", "") or ""

    @property
    def has_2fa(self) -> bool:
        """本任务是否配置了二次验证（任务级勾选，兼容旧配置 two_step.enabled）"""
        return _as_bool(self.options.get("has_2fa"), False) or self.two_step_enabled

    @property
    def has_cf(self) -> bool:
        """本任务是否有 CF/人机验证（任务级勾选，未勾选则完全不处理）"""
        return _as_bool(self.options.get("has_cf"), False)

    @property
    def use_proxy(self) -> bool:
        """本任务是否走代理（全局设置里配置的代理）；未勾选则强制直连"""
        return _as_bool(self.options.get("use_proxy"), False)

    @property
    def two_step_enabled(self) -> bool:
        """旧字段兼容：account.two_step.enabled"""
        return _as_bool(self.account.get("two_step", {}).get("enabled"), False)

    @property
    def totp_secret(self) -> str:
        return self.account.get("two_step", {}).get("secret", "") or ""

    @property
    def vault_key(self) -> str:
        """绑定的 2FA 凭证库账号，格式 "issuer|name" """
        return self.account.get("two_step", {}).get("vault_key", "") or ""

    @property
    def max_login_retry(self) -> int:
        """登录失败次数上限。

        **跨天累计**：每次登录失败 +1，只有登录成功才清零（不按日期重置）；
        累计到上限后该任务不再执行，直到登录成功或手动重置。
        """
        try:
            return max(1, int(self.options.get("max_login_retry", 3)))
        except (TypeError, ValueError):
            return 3

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskConfig":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        task = data.get("task", {}) or {}
        return cls(
            id=task.get("id", Path(path).stem),
            name=task.get("name", task.get("id", Path(path).stem)),
            enabled=bool(task.get("enabled", True)),
            url=task.get("url", ""),
            account=data.get("account", {}) or {},
            options=data.get("options", {}) or {},
            path=str(path),
        )

    def set_totp_secret(self, secret: str, issuer: str = "", account: str = "") -> None:
        ts = self.account.setdefault("two_step", {})
        ts["enabled"] = True
        ts["secret"] = secret
        self.options["has_2fa"] = True
        if issuer:
            ts["issuer"] = issuer
        if account:
            ts["account"] = account

    def set_vault_binding(self, vault_key: str, two_step: bool = True) -> None:
        ts = self.account.setdefault("two_step", {})
        ts["vault_key"] = vault_key
        ts["enabled"] = bool(two_step)
        self.options["has_2fa"] = bool(two_step)

    def save(self) -> str:
        if not self.path:
            raise ValueError("任务配置未绑定文件路径")
        header = (
            f"# ============ {self.name}（{self.id}）签到任务配置 ============\n"
            "# task.enabled          : 是否参与「运行全部启用」\n"
            "# account.username      : 登录账号\n"
            "# account.password      : 登录密码\n"
            "# account.two_step.enabled : 本任务是否有 2FA（也可用 options.has_2fa 勾选）\n"
            "# account.two_step.secret  : 直接填 base32 密钥（可选，填了优先用它）\n"
            "# account.two_step.vault_key : 绑定 2FA 凭证库账号，格式 \"issuer|name\"\n"
            "# options.has_2fa       : 本任务是否有二次验证（勾选）\n"
            "# options.has_cf        : 本任务是否有 Cloudflare 人机验证（勾选；未勾选则不处理）\n"
            "# options.use_proxy     : 本任务是否走全局代理（勾选才走，未勾选强制直连）\n"
            "# options.max_login_retry : 登录失败次数上限（跨天累计，只有登录成功才清零；\n"
            "#                           用尽后该任务不再执行，需登录成功或手动重置）\n"
            "# options.schedule_time : 预留的每日签到时间\n"
        )
        body = [
            "task:",
            f"  id: {_q(self.id)}",
            f"  name: {_q(self.name)}",
            f"  enabled: {_q(bool(self.enabled))}",
            f"  url: {_q(self.url)}",
            "account:",
            f"  username: {_q(self.username)}",
            f"  password: {_q(self.password)}",
            "  two_step:",
            f"    enabled: {_q(bool(self.has_2fa))}",
            f"    secret: {_q(self.totp_secret)}",
            f"    vault_key: {_q(self.vault_key)}",
            "options:",
            f"  has_2fa: {_q(bool(self.has_2fa))}",
            f"  has_cf: {_q(bool(self.has_cf))}",
            f"  use_proxy: {_q(bool(self.use_proxy))}",
            f"  max_login_retry: {_q(self.max_login_retry)}",
            f"  schedule_time: {_q(self.options.get('schedule_time', '09:30'))}",
        ]
        # 保留框架未知的扩展字段，避免保存时丢失（供未来任务自定义参数）
        known_opts = {"has_2fa", "has_cf", "use_proxy", "max_login_retry", "schedule_time"}
        extra_opts = {k: v for k, v in self.options.items() if k not in known_opts}
        if extra_opts:
            body.extend("  " + ln for ln in
                        yaml.safe_dump(extra_opts, allow_unicode=True, sort_keys=False).splitlines())
        known_acc = {"username", "password", "two_step"}
        extra_acc = {k: v for k, v in self.account.items() if k not in known_acc}
        if extra_acc:
            body.extend("  " + ln for ln in
                        yaml.safe_dump(extra_acc, allow_unicode=True, sort_keys=False).splitlines())
        body.append("")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(header + "\n".join(body))
        return self.path


def load_task_configs() -> list[TaskConfig]:
    """扫描 config/tasks/ 下全部任务配置"""
    if not TASKS_CONFIG_DIR.exists():
        return []
    return [TaskConfig.from_yaml(p) for p in sorted(TASKS_CONFIG_DIR.glob("*.yaml"))]
