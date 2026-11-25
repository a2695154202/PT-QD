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


def _as_int(value, default: int = 0) -> int:
    """健壮整数解析：YAML 里手写的 "60" / "60s" / 空值都能兜住"""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip().rstrip("sS秒")))
        except (TypeError, ValueError):
            return default


def _clean_providers(raw) -> list:
    """把 YAML/界面里拿到的供应商列表洗成干净的 dict 列表。

    - 丢掉不是 dict 的项、以及没填接口地址的项
    - 缺字段补默认值、timeout 统一成 int
    - 保持列表顺序（顺序就是主用 → 备用的优先级）
    """
    out = []
    if not isinstance(raw, list):
        return out
    for i, item in enumerate(raw):
        if isinstance(item, dict):
            base = str(item.get("base_url") or "").strip()
            if not base:
                continue
            out.append({
                "name": str(item.get("name") or "").strip() or f"AI {i + 1}",
                "base_url": base,
                "api_key": str(item.get("api_key") or "").strip(),
                "model": str(item.get("model") or "").strip() or "gpt-4o-mini",
                "vision_model": str(item.get("vision_model") or "").strip(),
                "timeout": _as_int(item.get("timeout"), 60) or 60,
            })
        elif hasattr(item, "base_url"):          # 直接塞了 AIProvider
            base = (getattr(item, "base_url", "") or "").strip()
            if not base:
                continue
            out.append({
                "name": (getattr(item, "name", "") or "").strip() or f"AI {i + 1}",
                "base_url": base,
                "api_key": (getattr(item, "api_key", "") or "").strip(),
                "model": (getattr(item, "model", "") or "").strip() or "gpt-4o-mini",
                "vision_model": (getattr(item, "vision_model", "") or "").strip(),
                "timeout": _as_int(getattr(item, "timeout", 60), 60) or 60,
            })
    return out


@dataclass
class BrowserConfig:
    mode: str = "launch"                    # launch=程序启动Edge; attach=连接已运行的Edge    cdp_endpoint: str = "http://127.0.0.1:9222"
    debugging_port: int = 9222              # launch 模式下开放的远程调试端口
    user_data_dir: str = "data/edge_profile"
    headless: bool = False
    slow_mo: int = 50                       # 每步操作间隔(ms)，拟人化
    default_timeout: int = 20000            # 单步操作等待(ms)：慢机器/慢网络留足时间
    nav_timeout: int = 45000                # 打开页面等待(ms)：CF 挑战页常迟迟不触发 domcontentloaded
    action_delay_ms: int = 150              # 每个动作前的拟人小延时(ms，0=关闭，实际带随机抖动)
    viewport_width: int = 1440
    viewport_height: int = 900
    stealth: bool = True                    # 反自动化检测
    fullscreen: bool = True                 # 启动后全屏（窗口铺满屏幕）
    always_on_top: bool = True              # 浏览器窗口置顶显示

    @property
    def user_data_dir_abs(self) -> str:
        return _abs(self.user_data_dir)


@dataclass
class AIProvider:
    """一组 AI 接口配置（OpenAI 兼容）。

    多个供应商按列表顺序排优先级：第一个是「主用」，后面都是「备用」。
    主用调不通（超时 / 401 / 额度用尽 / 返回格式异常）会自动切到下一个。
    """
    name: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    vision_model: str = ""                  # 留空则复用 model
    timeout: int = 60

    @property
    def ready(self) -> bool:
        """能不能用：地址和 key 都填了"""
        return bool((self.base_url or "").strip() and (self.api_key or "").strip())

    @property
    def label(self) -> str:
        """日志/界面里怎么称呼它"""
        return (self.name or "").strip() or _host_of(self.base_url) or "未命名"

    def with_model(self, vision: bool = False) -> str:
        """取该线路实际要用的模型名；视觉模型留空时回落普通模型"""
        if vision:
            return (self.vision_model or "").strip() or (self.model or "").strip()
        return (self.model or "").strip()


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse((url or "").strip()).hostname or "").strip()
    except Exception:
        return ""


@dataclass
class AIConfig:
    enabled: bool = False
    # ---- 旧字段：只配置了一个 AI 时用它（保持向后兼容）----
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    vision_model: str = ""                  # 留空则复用 model
    timeout: int = 60
    # ---- 多供应商：列表顺序即优先级，第一个是主用 ----
    providers: list = field(default_factory=list)
    failover_cooldown: int = 120            # 某个供应商失败后，多久内不再尝试（秒）
    prompts: dict = field(default_factory=lambda: {
        "captcha": (
            "这是一张网站登录/签到用的图形验证码图片，通常是4位字母数字。"
            "请只输出验证码内容本身，不要输出任何解释、标点或多余字符。"
        ),
        "popup_captcha": (
            "这是一张签到弹窗中的图形验证码图片。请只输出验证码内容本身，"
            "不要输出任何解释、标点或多余字符。"
        ),
    })

    # ---------- 供应商列表 ----------
    def normalized_providers(self) -> list:
        """把配置整理成 AIProvider 列表（按优先级）。

        - 配了 providers → 用它们（丢掉地址或 key 不全的）
        - 没配 providers → 用旧字段拼一个，保证老配置文件照常能用
        """
        out: list[AIProvider] = []
        raw = self.providers if isinstance(self.providers, list) else []
        for i, item in enumerate(raw):
            if isinstance(item, AIProvider):
                p = item
            elif isinstance(item, dict):
                p = AIProvider(
                    name=str(item.get("name") or ""),
                    base_url=str(item.get("base_url") or "").strip(),
                    api_key=str(item.get("api_key") or "").strip(),
                    model=str(item.get("model") or "").strip(),
                    vision_model=str(item.get("vision_model") or "").strip(),
                    timeout=_as_int(item.get("timeout"), 60),
                )
            else:
                continue
            if not p.base_url:
                continue
            if not p.name:
                p.name = _host_of(p.base_url) or f"AI {i + 1}"
            if not p.model:
                p.model = "gpt-4o-mini"
            out.append(p)
        if out:
            return out
        legacy = AIProvider(
            name="默认",
            base_url=(self.base_url or "").strip(),
            api_key=(self.api_key or "").strip(),
            model=(self.model or "").strip() or "gpt-4o-mini",
            vision_model=(self.vision_model or "").strip(),
            timeout=self.timeout or 60,
        )
        return [legacy] if legacy.base_url else []

    def ready_providers(self) -> list:
        return [p for p in self.normalized_providers() if p.ready]

    def mirror_primary(self) -> None:
        """把主用供应商写回旧字段，老版本/命令行看到的仍是同一套配置"""
        ps = self.normalized_providers()
        if not ps:
            return
        p = ps[0]
        self.base_url = p.base_url
        self.api_key = p.api_key
        self.model = p.model
        self.vision_model = p.vision_model
        self.timeout = p.timeout or 60

    def describe(self) -> str:
        """一句话说明当前 AI 线路，给命令行/日志用"""
        ready = self.ready_providers()
        if not ready:
            return "未配置可用线路（缺 api_key 或 base_url）"
        if len(ready) == 1:
            return f"{ready[0].label}（{ready[0].model}）"
        backup = "、".join(p.label for p in ready[1:])
        return f"主用 {ready[0].label}（{ready[0].model}），备用 {backup}"


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
    """全局代理设置：任务勾选「走代理」时才使用。

    未配置 / 未勾选 → 强制直连。本程序**不使用 Windows 系统代理**
    （开着代理或 VPN 的电脑上，系统代理会让签到流程莫名超时）。
    """
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
    """每日定时运行：到点自动跑一遍定时选中的任务"""
    enabled: bool = False
    time: str = "09:30"                     # 每天运行时间 HH:MM（24 小时制）
    skip_signed: bool = True                # 今天已签到的任务不再跑
    catch_up: bool = True                   # 错过时间点（程序后开）也补跑当天一次
    tasks: list = field(default_factory=list)   # 定时运行哪些任务；空=全部启用任务

    def is_valid_time(self) -> bool:
        from .scheduler import parse_hhmm
        return parse_hhmm(self.time) is not None


@dataclass
class TelegramConfig:
    """Telegram 通知：跑完一遍任务后把汇总结果发到指定会话。

    默认 use_proxy=True：api.telegram.org 在国内直连基本不通，
    这里默认复用「全局设置 → 代理」里的地址；没配代理时才直连（并写日志提示）。
    """
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""
    use_proxy: bool = True              # 默认走代理
    only_on_failure: bool = False       # 只在有任务失败时推送

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.bot_token.strip() and self.chat_id.strip())


@dataclass
class AppConfig:
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    cookiecloud: CookieCloudConfig = field(default_factory=CookieCloudConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
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
        if not isinstance(data, dict):        # 文件内容是列表/标量时不要崩
            return cfg
        logging_section = data.get("logging") or {}
        if not isinstance(logging_section, dict):
            logging_section = {}
        cfg.log_level = logging_section.get("level", cfg.log_level)
        cfg.log_file = logging_section.get("file", cfg.log_file)
        for key, dc in (("browser", BrowserConfig), ("ai", AIConfig),
                        ("cookiecloud", CookieCloudConfig), ("proxy", ProxyConfig),
                        ("telegram", TelegramConfig), ("schedule", ScheduleConfig)):
            section = data.get(key, {}) or {}
            if not isinstance(section, dict):
                section = {}
            current = getattr(cfg, key)
            merged = {}
            for fname in dc.__dataclass_fields__:
                merged[fname] = section[fname] if fname in section else getattr(current, fname)
            setattr(cfg, key, dc(**merged))
        # 提示词按「默认值 + 用户覆盖」合并：用户只写部分键时不能缺 captcha，
        # 否则 recognize_captcha 取默认提示词会 KeyError
        if isinstance(cfg.ai.prompts, dict):
            merged_prompts = dict(AIConfig().prompts)
            merged_prompts.update({k: v for k, v in cfg.ai.prompts.items()
                                   if isinstance(v, str) and v.strip()})
            cfg.ai.prompts = merged_prompts
        # AI 供应商列表：洗掉脏数据，数值字段统一类型（手写的 "60s" 之类也能兜住）
        cfg.ai.providers = _clean_providers(cfg.ai.providers)
        cfg.ai.enabled = _as_bool(cfg.ai.enabled, False)
        cfg.ai.timeout = _as_int(cfg.ai.timeout, 60) or 60
        cfg.ai.failover_cooldown = _as_int(cfg.ai.failover_cooldown, 120)
        return cfg

    def save(self, path: Optional[Path] = None) -> str:
        path = path or (CONFIG_DIR / "config.yaml")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        ai_data = {f.name: getattr(self.ai, f.name) for f in AIConfig.__dataclass_fields__.values()}
        ai_data["providers"] = _clean_providers(self.ai.providers)   # AIProvider 对象转 dict
        ai_data["timeout"] = _as_int(ai_data.get("timeout"), 60) or 60
        ai_data["failover_cooldown"] = _as_int(ai_data.get("failover_cooldown"), 120)
        data = {
            "browser": {f.name: getattr(self.browser, f.name) for f in BrowserConfig.__dataclass_fields__.values()},
            "ai": ai_data,
            "cookiecloud": {f.name: getattr(self.cookiecloud, f.name)
                            for f in CookieCloudConfig.__dataclass_fields__.values()},
            "proxy": {f.name: getattr(self.proxy, f.name) for f in ProxyConfig.__dataclass_fields__.values()},
            "telegram": {f.name: getattr(self.telegram, f.name)
                         for f in TelegramConfig.__dataclass_fields__.values()},
            "schedule": {f.name: getattr(self.schedule, f.name)
                         for f in ScheduleConfig.__dataclass_fields__.values()},
            "logging": {"level": self.log_level, "file": self.log_file},
        }
        header = (
            "# ============ 全局配置（可在图形界面「全局设置」中修改）============\n"
            "# browser.mode: launch=程序启动Edge并开放调试端口；attach=连接已运行的Edge\n"
            "# browser.fullscreen / always_on_top: 启动后全屏 / 窗口置顶（仅 Windows 生效）\n"
            "# ai: 任意 OpenAI 兼容接口；识别验证码需支持视觉的模型\n"
            "#     providers 是供应商列表，**顺序即优先级**：第 1 个是主用，其余是备用；\n"
            "#     主用超时/401/额度用尽会自动切到下一个（failover_cooldown 秒内不再试它）。\n"
            "#     没配 providers 时退回上面的 base_url/api_key/model 单配置，老配置照常可用。\n"
            "# cookiecloud.mode: overwrite=拉取并覆盖到浏览器 / paused=暂停同步\n"
            "# proxy: 全局代理；任务配置里勾选「走代理」的任务才使用它。\n"
            "#        没配置代理 / 或本任务没勾选时一律直连，**从不使用 Windows 系统代理**\n"
            "# telegram: 签到汇总推送到 Telegram；use_proxy=true（默认）时复用上面的代理地址\n"
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
        if not isinstance(data, dict):
            data = {}
        task = data.get("task", {}) or {}
        if not isinstance(task, dict):
            task = {}
        account = data.get("account", {}) or {}
        options = data.get("options", {}) or {}
        return cls(
            id=task.get("id", Path(path).stem),
            name=task.get("name", task.get("id", Path(path).stem)),
            # 必须走 _as_bool：YAML 里手写 enabled: "false"（带引号）时 bool("false") 是 True
            enabled=_as_bool(task.get("enabled"), True),
            url=task.get("url", ""),
            account=account if isinstance(account, dict) else {},
            options=options if isinstance(options, dict) else {},
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
            "# options.use_proxy     : 本任务是否走全局代理（勾选才走；未勾选强制直连，\n"
            "#                           系统代理从不使用，AI/CookieCloud 请求同理）\n"
            "# options.max_login_retry : 登录失败次数上限（跨天累计，只有登录成功才清零；\n"
            "#                           用尽后该任务不再执行，需登录成功或手动重置）\n"
            "# options.schedule_time : 预留的每日签到时间\n"
        )
        ts = self.account.get("two_step")
        if not isinstance(ts, dict):
            ts = {}
        two_step_lines = [
            "  two_step:",
            f"    enabled: {_q(bool(self.has_2fa))}",
            f"    secret: {_q(self.totp_secret)}",
            f"    vault_key: {_q(self.vault_key)}",
        ]
        # issuer / account 是 import-qr 与 set_totp_secret 写入的（凭证库列表显示、
        # 站点域名自动匹配都要用），早前这里不写 → 保存一次就丢，补上
        for key in ("issuer", "account"):
            if ts.get(key):
                two_step_lines.append(f"    {key}: {_q(ts[key])}")
        known_ts = {"enabled", "secret", "vault_key", "issuer", "account"}
        extra_ts = {k: v for k, v in ts.items() if k not in known_ts}
        if extra_ts:
            two_step_lines.extend("    " + ln for ln in
                                  yaml.safe_dump(extra_ts, allow_unicode=True,
                                                 sort_keys=False).splitlines())

        body = [
            "task:",
            f"  id: {_q(self.id)}",
            f"  name: {_q(self.name)}",
            f"  enabled: {_q(bool(self.enabled))}",
            f"  url: {_q(self.url)}",
            "account:",
            f"  username: {_q(self.username)}",
            f"  password: {_q(self.password)}",
            *two_step_lines,
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
    """扫描 config/tasks/ 下全部任务配置。

    单个文件写坏（YAML 语法错误）只跳过这一个并记警告，不能整个列表都读不出来。
    """
    from .logger import get_logger

    if not TASKS_CONFIG_DIR.exists():
        return []
    configs: list[TaskConfig] = []
    for path in sorted(TASKS_CONFIG_DIR.glob("*.yaml")):
        try:
            configs.append(TaskConfig.from_yaml(path))
        except Exception as e:
            get_logger().warning(f"读取任务配置 {path.name} 失败（已跳过）: {e}")
    return configs
