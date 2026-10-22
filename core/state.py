"""任务运行状态：记录签到结果 + 登录失败次数（都持久化到本地）。

存储位置：data/task_state.json（随 exe 目录，纯本地）

结构：
{
  "audiences": {
    "signed": true,                    # 最近一次任务是否成功（界面显示用；跨天自动视为未签到）
    "time": "2026-10-20 13:50",
    "message": "签到成功",
    "login_fail": {                    # 登录失败累计（**不按天清零**）
      "count": 2,
      "limit": 3,
      "time": "2026-10-20 18:02",
      "reason": "登录未成功（页面提示可能含: 密码）"
    },
    "login_ok_time": "2026-10-20 18:05"   # 最近一次登录成功的时间
  }
}

**登录失败次数语义（重要）**：
- 每次「提交了登录表单但没登录成功」累加 1；
- **只有登录成功才清零**（`reset_login_fail`）；
- 不做任何基于日期的清零/重置 —— 今天用尽了次数且没登录成功，明天照样不会执行任务；
- 次数用尽的唯一解除方式：登录成功一次（手动登录站点或点界面的「重置登录失败次数」）。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from .config import DATA_DIR

STATE_PATH = DATA_DIR / "task_state.json"

# 登录失败计数在 task_state.json 里的字段名
LOGIN_FAIL_KEY = "login_fail"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _record_date(text: Any) -> str:
    """从 "2026-10-21 09:30" / "2026-10-21T09:30" 里取出日期部分"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    return raw.replace("T", " ").split(" ")[0]


def load_state() -> dict[str, dict[str, Any]]:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _entry(state: dict[str, dict[str, Any]], task_id: str) -> dict[str, Any]:
    """取（必要时创建）任务的记录字典。避免覆盖写导致登录计数被清掉。"""
    ent = state.get(task_id)
    if not isinstance(ent, dict):
        ent = {}
        state[task_id] = ent
    return ent


def record_result(task_id: str, success: bool, message: str) -> None:
    """任务结束后记录结果：success 为真视为已签到。

    **合并写**：只更新 signed/time/message，保留 login_fail 等其它字段，
    否则每次签到都会把登录失败计数顺手清掉。
    """
    state = load_state()
    ent = _entry(state, task_id)
    ent["signed"] = bool(success)
    ent["time"] = _now()
    ent["message"] = message or ""
    save_state(state)


def is_signed(task_id: str) -> bool:
    """**今天**是否已签到。

    跨天自动视为未签到：昨天成功、今天还没跑 → 返回 False。
    （签到结果里记录的 time 是「YYYY-MM-DD HH:MM」，用它和今天比对）
    """
    info = load_state().get(task_id) or {}
    if not info.get("signed"):
        return False
    return _record_date(info.get("time")) == today_str()


def record_date(task_id: str) -> str:
    """最近一次任务记录的日期（YYYY-MM-DD），没有则空串"""
    return _record_date((load_state().get(task_id) or {}).get("time"))


def is_signed_on(task_id: str, date_str: str) -> bool:
    """指定日期当天是否签到过（date_str 形如 2026-10-21）"""
    info = load_state().get(task_id) or {}
    return bool(info.get("signed")) and _record_date(info.get("time")) == date_str


def last_time(task_id: str) -> str:
    return str(load_state().get(task_id, {}).get("time", ""))


# ---------------- 定时运行标记（每天最多自动跑一次） ----------------
SCHEDULE_KEY = "_schedule"


def schedule_last_run() -> str:
    """最近一次定时运行触发的日期（YYYY-MM-DD），没有则空串"""
    raw = load_state().get(SCHEDULE_KEY)
    if isinstance(raw, dict):
        return str(raw.get("last_run", "") or "")
    return ""


def schedule_info() -> dict[str, Any]:
    raw = load_state().get(SCHEDULE_KEY)
    return raw if isinstance(raw, dict) else {}


def set_schedule_last_run(date_str: str = "", note: str = "") -> None:
    """记录定时运行已触发（默认记为今天）"""
    state = load_state()
    state[SCHEDULE_KEY] = {
        "last_run": date_str or today_str(),
        "time": _now(),
        "note": (note or "")[:200],
    }
    save_state(state)


def clear_schedule_last_run() -> None:
    """清掉标记（下次到点还能再跑一次）"""
    state = load_state()
    state.pop(SCHEDULE_KEY, None)
    save_state(state)


# ---------------- 登录失败计数（跨天累计，只有登录成功才清零） ----------------
def login_fail_info(task_id: str) -> dict[str, Any]:
    """返回 {"count": int, "limit": int, "time": str, "reason": str}"""
    raw = load_state().get(task_id, {}).get(LOGIN_FAIL_KEY)
    info: dict[str, Any] = {"count": 0, "limit": 0, "time": "", "reason": ""}
    if isinstance(raw, dict):
        try:
            info["count"] = max(0, int(raw.get("count", 0) or 0))
        except (TypeError, ValueError):
            info["count"] = 0
        try:
            info["limit"] = int(raw.get("limit", 0) or 0)
        except (TypeError, ValueError):
            info["limit"] = 0
        info["time"] = str(raw.get("time", "") or "")
        info["reason"] = str(raw.get("reason", "") or "")
    elif isinstance(raw, int):        # 兼容手工写的纯数字
        info["count"] = max(0, raw)
    return info


def login_fail_count(task_id: str) -> int:
    return int(login_fail_info(task_id)["count"])


def bump_login_fail(task_id: str, reason: str = "", limit: Optional[int] = None) -> int:
    """登录失败一次：计数 +1 并落盘（不按日期重置），返回累加后的次数。"""
    state = load_state()
    ent = _entry(state, task_id)
    info = login_fail_info(task_id)
    count = info["count"] + 1
    ent[LOGIN_FAIL_KEY] = {
        "count": count,
        "limit": int(limit) if limit is not None else int(info["limit"]),
        "time": _now(),
        "reason": (reason or "")[:200],
    }
    save_state(state)
    return count


def reset_login_fail(task_id: str) -> None:
    """登录成功（或用户手动重置）后清零。**这是唯一的清零入口**。"""
    state = load_state()
    ent = _entry(state, task_id)
    if LOGIN_FAIL_KEY in ent:
        ent.pop(LOGIN_FAIL_KEY, None)
    ent["login_ok_time"] = _now()
    save_state(state)


def reset_all_login_fail() -> list[str]:
    """清空所有任务的登录失败计数，返回被清零的任务 id 列表"""
    state = load_state()
    cleared: list[str] = []
    for tid, ent in state.items():
        if isinstance(ent, dict) and ent.pop(LOGIN_FAIL_KEY, None) is not None:
            cleared.append(tid)
    save_state(state)
    return cleared


def is_login_locked(task_id: str, max_retry: int) -> bool:
    """登录重试次数是否已用尽（判断只看持久化计数，与当前日期无关）"""
    try:
        limit = max(1, int(max_retry))
    except (TypeError, ValueError):
        limit = 1
    return login_fail_count(task_id) >= limit


def login_remaining(task_id: str, max_retry: int) -> int:
    """本次还能尝试登录几次（= 上限 - 已累计失败次数）"""
    try:
        limit = max(1, int(max_retry))
    except (TypeError, ValueError):
        limit = 1
    return max(0, limit - login_fail_count(task_id))
