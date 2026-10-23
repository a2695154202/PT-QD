"""拟人化鼠标轨迹（滑块拖动用）。

原「AI 定位滑块缺口」已移除：缺口定位交给不了可靠的自动化，
这里只提供拟人化拖动轨迹，由 CF 处理逻辑直接拖到轨道末端。
"""
from __future__ import annotations

import random


def human_track(distance: int) -> list[int]:
    """生成拟人化拖动轨迹（先加速后减速，带轻微回弹）"""
    if distance <= 0:
        return [0]
    track: list[int] = []
    current = 0
    mid = distance * 0.75
    v = 0
    while current < distance:
        a = random.randint(2, 5) if current < mid else -random.randint(3, 6)
        v = v * 0.82 + a
        step = min(max(1, int(v)), distance - current)
        current += step
        track.append(step)
    if random.random() < 0.6:  # 结尾小幅回弹，更像真人
        track.append(-random.randint(1, 3))
        track.append(random.randint(1, 3))
    return track
