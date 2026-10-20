"""2FA 凭证库：集中管理从谷歌验证器导入的多个 TOTP 账号。

- 存储于 config/twofa.yaml
- 支持按任务绑定（bound_task）或按 issuer 与站点域名模糊匹配
- 新增签到任务时，若该站点需要 2FA，先从库里找凭证，没有再提示用户导入
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import yaml

from .config import CONFIG_DIR
from .totp import TwoFactorAuth

VAULT_PATH = CONFIG_DIR / "twofa.yaml"


@dataclass
class TwoFactorAccount:
    issuer: str = ""
    name: str = ""
    secret: str = ""
    bound_task: str = ""

    @property
    def key(self) -> str:
        return f"{self.issuer}|{self.name}"

    def tfa(self) -> TwoFactorAuth:
        return TwoFactorAuth(secret=self.secret, issuer=self.issuer, account=self.name)


class TwoFactorVault:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else VAULT_PATH
        self.accounts: list[TwoFactorAccount] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.accounts = []
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        valid = set(TwoFactorAccount.__dataclass_fields__)
        items = []
        for raw in data.get("accounts", []) or []:
            if not isinstance(raw, dict):
                continue
            # 忽略未知字段，避免用户手工加了备注字段后整体加载失败
            items.append(TwoFactorAccount(**{k: v for k, v in raw.items() if k in valid}))
        self.accounts = items

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"accounts": [asdict(a) for a in self.accounts]}
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def add_many(self, accounts: list[TwoFactorAuth], bound_task: str = "") -> tuple[int, int]:
        """导入账号，按 issuer+name 去重。返回 (新增数, 更新数)"""
        added = updated = 0
        existing = {a.key: a for a in self.accounts}
        for tfa in accounts:
            acc = TwoFactorAccount(issuer=tfa.issuer, name=tfa.account,
                                   secret=tfa.secret, bound_task=bound_task)
            if acc.key in existing:
                existing[acc.key].secret = acc.secret
                if bound_task:
                    existing[acc.key].bound_task = bound_task
                updated += 1
            else:
                self.accounts.append(acc)
                existing[acc.key] = acc
                added += 1
        self.save()
        return added, updated

    def remove(self, issuer: str, name: str) -> bool:
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if not (a.issuer == issuer and a.name == name)]
        self.save()
        return len(self.accounts) < before

    def bind(self, issuer: str, name: str, task_id: str) -> bool:
        for a in self.accounts:
            if a.issuer == issuer and a.name == name:
                a.bound_task = task_id
                self.save()
                return True
        return False

    def find_by_key(self, vault_key: str) -> Optional[TwoFactorAccount]:
        """按 "issuer|name" 精确查找（任务配置里绑定的方式）"""
        if not vault_key:
            return None
        for a in self.accounts:
            if a.key == vault_key:
                return a
        return None

    @staticmethod
    def _norm(text: str) -> str:
        """归一化：去掉空格/连字符/点/下划线等分隔符，只保留字母数字"""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    @classmethod
    def _issuer_matches_domain(cls, issuer: str, host: str) -> bool:
        """issuer 与站点域名是否对应。

        例：issuer "M-Team - TP" 对应 host "site-d.example"：
        归一化后 issuer="mteamtp"、host="kpmteamcc"，两者互不包含，
        但主机名各段中的 "m-team" -> "mteam" 是 issuer 的子串，因此判定匹配。
        """
        if not issuer or not host:
            return False
        iss = cls._norm(issuer)
        h = cls._norm(host)
        if not iss or not h:
            return False
        if iss in h or h in iss:
            return True
        for part in host.lower().split(".")[:-1]:  # 排除顶级域
            p = cls._norm(part)
            if len(p) >= 3 and p in iss:
                return True
        return False

    def find_for_task(self, task_id: str, site_domain: str = "") -> Optional[TwoFactorAccount]:
        """为任务查找 2FA 凭证：优先显式绑定，其次 issuer 与站点域名匹配"""
        for a in self.accounts:
            if a.bound_task == task_id:
                return a
        if site_domain:
            for a in self.accounts:
                if not a.issuer:
                    continue  # 空 issuer 不参与域名匹配（否则空串会匹配任何域名）
                if self._issuer_matches_domain(a.issuer, site_domain):
                    return a
        return None
