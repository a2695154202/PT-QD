"""滑块验证码：AI 定位缺口 + 拟人化拖动轨迹。

坐标约定：截图统一使用 CSS 像素（scale=css），AI 返回的 gap_x 与鼠标坐标同尺度，
无需再做设备像素比（DPR）换算。
"""
from __future__ import annotations

import random
from typing import Optional

from .ai import AIClient
from .browser import BrowserController
from .logger import get_logger


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


class SliderSolver:
    def __init__(self, ai: AIClient, browser: BrowserController):
        self.ai = ai
        self.browser = browser
        self.logger = get_logger()

    async def solve(self, slider_selector: str, container_selector: Optional[str] = None,
                    timeout_ms: int = 15000) -> bool:
        """尝试完成滑块拖动。

        slider_selector   : 滑块按钮元素
        container_selector: 滑块背景图容器（建议传入，坐标以容器左缘为基准；不传则用整页）
        返回是否完成拖动（是否通过由调用方判断）
        """
        page = self.browser.page
        try:
            slider = page.locator(slider_selector).first
            await slider.wait_for(state="visible", timeout=timeout_ms)
        except Exception as e:
            self.logger.warning(f"滑块元素未出现: {e}")
            return False

        # 1. 取背景图（CSS 像素）+ 背景原点
        origin_x = 0.0
        if container_selector:
            try:
                bgbox = await page.locator(container_selector).first.bounding_box()
            except Exception:
                bgbox = None
            if bgbox:
                origin_x = bgbox["x"]
                bg = await self.browser.clip_screenshot(bgbox, name="slider_bg")
            else:
                bg = await self.browser.screenshot(name="slider_bg")
        else:
            bg = await self.browser.screenshot(name="slider_bg")

        # 2. AI 定位缺口（相对背景图左缘的 CSS 像素）
        try:
            gap_x = self.ai.locate_slider_gap(bg)
            self.logger.info(f"AI 定位滑块缺口 x = {gap_x}px（相对背景左缘）")
        except Exception as e:
            self.logger.warning(f"AI 定位滑块缺口失败: {e}")
            return False

        # 3. 拟人拖动：位移 = 缺口页面坐标 - 滑块中心起点
        box = await slider.bounding_box()
        if not box:
            return False
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2
        move_x = (origin_x + gap_x) - start_x
        if move_x <= 0:
            self.logger.warning(f"滑块位移计算异常（gap={gap_x}, start={start_x:.1f}），放弃")
            return False

        await page.mouse.move(start_x, start_y, steps=random.randint(5, 12))
        await self.browser.wait_timeout(random.randint(100, 350))
        await page.mouse.down()
        await self.browser.wait_timeout(random.randint(100, 250))

        cur = 0
        for step in human_track(int(move_x)):
            cur += step
            await page.mouse.move(start_x + cur, start_y + random.uniform(-1.5, 1.5), steps=1)
            await self.browser.wait_timeout(random.randint(5, 25))
        await self.browser.wait_timeout(random.randint(100, 300))
        await page.mouse.up()
        self.logger.info(f"滑块拖动完成，位移 {int(move_x)}px")
        return True
