"""识图点击 Agent：AI 看屏幕截图，决定点哪里、填什么，再由桌面控制器执行。

**为什么需要它**：
Cloudflare 能识别 Playwright/CDP 驱动的浏览器（有自动化指纹），把人机验证卡得死死的。
换成用户**平时那个正常浏览器** + 像真人一样"看着屏幕点鼠标"，CF 就无从识别。

**工作方式**（ReAct 循环）：
1. 截取浏览器窗口截图 → 发给视觉模型
2. 模型返回下一步动作：click / type / drag / scroll / wait / finish
3. 执行动作 → 重复
4. 达成目标或步数用尽即结束

坐标约定：截图是屏幕物理像素，模型返回的 x/y 直接就是 `SetCursorPos` 的坐标，
一一对应，不需要换算。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .ai import AIClient
from .desktop import DesktopController
from .logger import get_logger

SYSTEM_PROMPT = """你是一个浏览器自动化助手，正在帮用户完成网站签到。
你看到的是**屏幕截图**，坐标单位是屏幕像素（图片左上角为 0,0，与鼠标点击坐标一一对应）。

你可以执行下列动作，**每次只返回一个动作**，输出严格 JSON（不要任何解释文字）：

  {{"tool": "click", "args": {{"x": 数字, "y": 数字}}}}        # 点击
  {{"tool": "double_click", "args": {{"x": 数字, "y": 数字}}}}  # 双击
  {{"tool": "type", "args": {{"text": "要输入的文本"}}}}        # 在当前焦点输入（含中文）
  {{"tool": "hotkey", "args": {{"keys": ["ctrl", "v"]}}}}     # 组合键
  {{"tool": "enter", "args": {{}}}}                            # 回车
  {{"tool": "drag", "args": {{"x1":..,"y1":..,"x2":..,"y2":..}}}}  # 拖动（滑块/拼图）
  {{"tool": "scroll", "args": {{"dy": 300}}}}                  # 向下滚（负数向上）
  {{"tool": "wait", "args": {{"ms": 1500}}}}                   # 等待
  {{"tool": "finish", "args": {{"passed": true, "message": "签到成功"}}}}  # 结束

注意事项：
1. 坐标必须落在截图范围内，点按钮要点**按钮中心**，不要点边缘。
2. 需要先点输入框让其获得焦点，再 type 输入文本。
3. 遇到人机验证（复选框 / 滑块 / 拼图）：
   - 复选框 → click 它的中心
   - 滑块 → drag 从滑块起点到缺口位置
   - 「请稍候」→ wait 1500 再看
4. 同一个动作连续失败 2 次就换策略（改滚动、改拖动、或等待后重试）。
5. 步数有限，不要做无意义的重复操作。
6. 看到签到成功 / 今日已签到 / 已签到 类提示就立刻 finish(passed=true)。
"""

MAX_STEPS = 20


class DesktopAgent:
    """AI 识图点击 Agent（视觉模型 + 桌面操作循环）"""

    def __init__(self, ai: AIClient, desktop: DesktopController):
        self.ai = ai
        self.desktop = desktop
        self.logger = get_logger()

    async def drive(self, goal: str, window_title: str = "",
                    max_steps: int = MAX_STEPS,
                    on_screenshot=None) -> tuple[bool, str]:
        """驱动桌面完成目标，返回 (是否达成, 结果说明)

        on_screenshot: 可选回调，每次截图后调用（用于测试断言）
        """
        if not (self.ai and getattr(self.ai, "available", False)):
            self.logger.warning("AI 未启用，无法识图点击")
            return False, "AI 未启用"

        history: list[str] = []
        self.logger.info(f"[识图点击] 开始（目标：{goal}，最多 {max_steps} 步）")

        for step in range(1, max_steps + 1):
            # 1) 截图
            hwnd = self.desktop.find_browser_window(window_title) if window_title else \
                self.desktop.find_browser_window()
            if hwnd is not None:
                try:
                    image, rect = self.desktop.capture_window(hwnd, name=f"desktop_{step:02d}")
                except Exception as e:
                    self.logger.warning(f"[识图点击] 窗口截图失败: {e}")
                    image = self.desktop.capture_screen(name=f"desktop_{step:02d}")
                    rect = (0, 0, 0, 0)
            else:
                image = self.desktop.capture_screen(name=f"desktop_{step:02d}")
                rect = (0, 0, 0, 0)
            if on_screenshot is not None:
                try:
                    on_screenshot(step, image, rect)
                except Exception:
                    pass

            # 2) 问 AI 下一步
            history_text = "\n".join(history[-8:]) if history else "(无)"
            prompt = (
                f"{SYSTEM_PROMPT}\n\n"
                f"目标：{goal}\n\n"
                f"前面已做过的动作及结果：\n{history_text}\n\n"
                f"这是当前屏幕截图，请判断下一步动作，返回 JSON。"
            )
            action = self._ask(prompt, image)
            if not action:
                self.logger.warning(f"[识图点击 {step}/{max_steps}] AI 未返回有效动作")
                history.append("AI 未返回有效动作，等待 1.5s")
                self._wait(1500)
                continue

            tool = action.get("tool", "")
            args = action.get("args") or {}

            # 3) 截图若来自窗口，把图片坐标换算回屏幕坐标
            if rect != (0, 0, 0, 0) and tool in ("click", "double_click", "drag"):
                ox, oy = rect[0], rect[1]
                if tool in ("click", "double_click"):
                    args = dict(args, x=float(args.get("x", 0)) + ox,
                                y=float(args.get("y", 0)) + oy)
                else:
                    args = dict(args,
                                x1=float(args.get("x1", 0)) + ox,
                                y1=float(args.get("y1", 0)) + oy,
                                x2=float(args.get("x2", 0)) + ox,
                                y2=float(args.get("y2", 0)) + oy)

            if tool == "finish":
                passed = bool(args.get("passed"))
                msg = str(args.get("message", ""))
                self.logger.info(f"[识图点击 {step}/{max_steps}] 结束：{msg}")
                return passed, msg

            self.logger.info(f"[识图点击 {step}/{max_steps}] 执行 {tool}({args})")
            result = self._exec(tool, args)
            history.append(f"{tool}({args}) → {result[:80]}")
            self._wait(1100)

        self.logger.info("[识图点击] 步数用尽")
        return False, "步数用尽，未完成"

    # ---------- 执行 ----------
    def _exec(self, tool: str, args: dict) -> str:
        try:
            if tool == "click":
                self.desktop.click(float(args["x"]), float(args["y"]))
                return "已点击"
            if tool == "double_click":
                self.desktop.double_click(float(args["x"]), float(args["y"]))
                return "已双击"
            if tool == "type":
                self.desktop.type_text(str(args.get("text", "")))
                return "已输入"
            if tool == "hotkey":
                keys = args.get("keys") or []
                self.desktop.hotkey(*[str(k) for k in keys])
                return f"已按 {'+'.join(map(str, keys))}"
            if tool == "enter":
                self.desktop.press_enter()
                return "已回车"
            if tool == "drag":
                self.desktop.drag(float(args["x1"]), float(args["y1"]),
                                  float(args["x2"]), float(args["y2"]))
                return "已拖动"
            if tool == "scroll":
                self.desktop.scroll(int(args.get("dy", 0)))
                return "已滚动"
            if tool == "wait":
                self._wait(int(args.get("ms", 1000)))
                return "已等待"
            return f"未知工具 {tool}"
        except Exception as e:
            return f"执行失败: {e}"

    @staticmethod
    def _wait(ms: int) -> None:
        import time as _t
        _t.sleep(max(0, ms) / 1000)

    # ---------- AI ----------
    def _ask(self, prompt: str, image: bytes) -> Optional[dict]:
        try:
            text = self.ai.vision(image, prompt)
        except Exception as e:
            self.logger.warning(f"[识图点击] AI 请求失败: {e}")
            return None
        return _parse_action(text)


def _parse_action(text: str) -> Optional[dict]:
    """从 AI 输出提取 {tool, args} JSON（容忍 ```json 包裹与前后杂文本）"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool") or data.get("action") or data.get("name")
    if not tool:
        return None
    args = data.get("args") or data.get("arguments") or data.get("params") or {}
    if not isinstance(args, dict):
        args = {}
    return {"tool": str(tool), "args": args}
