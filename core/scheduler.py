"""每日定时运行：时间解析与「现在该不该跑」的纯逻辑。

与界面无关，方便单测；界面只负责轮询调用 `should_run()`。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

# 错过时间点后，不补跑模式下允许的触发窗口（分钟）
DEFAULT_WINDOW_MINUTES = 3


def parse_hhmm(text) -> Optional[tuple[int, int]]:
    """把 "9:30" / "09：30" / "0930"... 解析成 (时, 分)，非法返回 None"""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        return None
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) != 2:
            return None
        hh, mm = parts
    elif raw.isdigit() and len(raw) in (3, 4):
        hh, mm = raw[:-2], raw[-2:]
    else:
        return None
    try:
        h, m = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def normalize_hhmm(text, default: str = "09:30") -> str:
    """规整成 HH:MM；非法则返回 default"""
    hm = parse_hhmm(text)
    if hm is None:
        return default
    return f"{hm[0]:02d}:{hm[1]:02d}"


def scheduled_at(now: datetime, hhmm: str) -> Optional[datetime]:
    """今天的计划时间点（当天 HH:MM）"""
    hm = parse_hhmm(hhmm)
    if hm is None:
        return None
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def should_run(now: datetime, hhmm: str, last_run_date: str = "",
               catch_up: bool = True, window_minutes: int = DEFAULT_WINDOW_MINUTES) -> bool:
    """现在是否该触发今天的定时运行。

    - 时间格式非法 → False
    - 今天已经跑过（last_run_date == 今天）→ False
    - 还没到点 → False
    - 已过点：catch_up=True 直接跑；catch_up=False 只在时间点后 window 分钟内跑
    """
    target = scheduled_at(now, hhmm)
    if target is None:
        return False
    if (last_run_date or "") == now.strftime("%Y-%m-%d"):
        return False
    if now < target:
        return False
    if catch_up:
        return True
    return (now - target) <= timedelta(minutes=max(1, int(window_minutes)))


def next_run_at(now: datetime, hhmm: str, last_run_date: str = "") -> Optional[datetime]:
    """下一次计划时间（用于界面显示）"""
    target = scheduled_at(now, hhmm)
    if target is None:
        return None
    if (last_run_date or "") != now.strftime("%Y-%m-%d") and now < target:
        return target
    return target + timedelta(days=1)


def pick_scheduled(selected, available) -> tuple[list[str], list[str]]:
    """决定定时运行要跑哪些任务。

    - `selected` 为空（旧配置或"全部"）→ 跑全部**启用**的任务
    - `selected` 有值 → 只跑选中的任务；任务被删了或自身未启用则跳过
    - `available`: [(任务 id, 是否启用)]

    返回 (要跑的 id 列表, 被跳过的说明列表)
    """
    picked = [str(s) for s in (selected or []) if str(s or "").strip()]
    if not picked:
        return [tid for tid, enabled in available if enabled], []
    enabled_map = {tid: bool(enabled) for tid, enabled in available}
    run: list[str] = []
    skipped: list[str] = []
    for tid in picked:
        if tid not in enabled_map:
            skipped.append(f"{tid}（任务配置已不存在）")
        elif not enabled_map[tid]:
            skipped.append(f"{tid}（任务未启用）")
        else:
            run.append(tid)
    return run, skipped


def describe(now: datetime, hhmm: str, last_run_date: str = "", enabled: bool = True,
             catch_up: bool = True) -> str:
    """界面状态文案"""
    if not enabled:
        return "定时：未启用"
    if parse_hhmm(hhmm) is None:
        return f"定时：时间格式不对（{hhmm}），应为 HH:MM"
    today = now.strftime("%Y-%m-%d")
    if (last_run_date or "") == today:
        nxt = next_run_at(now, hhmm, last_run_date)
        return f"定时：今天已运行，下次 {(nxt or now).strftime('%m-%d %H:%M')}"
    target = scheduled_at(now, hhmm)
    if target and now >= target:
        return f"定时：已到 {normalize_hhmm(hhmm)}" + ("，待补跑" if catch_up else "，已过补跑窗口")
    nxt = next_run_at(now, hhmm, last_run_date)
    return f"定时：每天 {normalize_hhmm(hhmm)}，下次 {(nxt or now).strftime('%m-%d %H:%M')}"
