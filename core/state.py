"""任务运行状态：记录每个任务最近一次是否签到成功，供界面显示。

存储位置：data/task_state.json（随 exe 目录，纯本地）
结构：{"audiences": {"signed": true, "time": "2026-10-20 13:50", "message": "签到成功"}}
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import DATA_DIR

STATE_PATH = DATA_DIR / "task_state.json"


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


def record_result(task_id: str, success: bool, message: str) -> None:
    """任务结束后记录结果：success 为真视为已签到"""
    state = load_state()
    state[task_id] = {
        "signed": bool(success),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "message": message or "",
    }
    save_state(state)


def is_signed(task_id: str) -> bool:
    return bool(load_state().get(task_id, {}).get("signed"))


def last_time(task_id: str) -> str:
    return str(load_state().get(task_id, {}).get("time", ""))
