"""AI 托管浏览器：内置兜底，Cloudflare 自动处理失败时由 AI 自己驱动浏览器。

**与 MCP 的关系**：本模块和 `core.mcp_server` 共用 `core.browser_tools` 的同一套工具。
区别只在于"谁在调用工具"：
- MCP：外部 AI 客户端调用（用户装了 MCP 客户端，由它接管）
- 本模块：程序内置的视觉模型自己调用（无需任何 MCP 客户端，开箱即用）

**工作方式**（ReAct 式循环）：
1. 截图（全局 + 验证控件局部）→ 发给视觉模型
2. 模型返回下一步动作：click / drag / scroll / wait / screenshot / finish
3. 执行动作 → 重复
4. 挑战消失或达到步数上限即结束

这样即使内置的"每秒探测 + 几何点击"策略在某些站点失灵，
也能由 AI 看图决策把验证过掉。
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .ai import AIClient
from .browser_tools import BrowserToolbox, format_tools_for_prompt
from .logger import get_logger

SYSTEM_PROMPT = """你是一个浏览器自动化助手，正在帮用户通过网站的 Cloudflare 人机验证。
你可以调用下列工具操作浏览器，坐标单位是 CSS 像素（相对浏览器视口左上角）。

可用工具：
{tools}

**每次只返回一个动作**，输出严格 JSON（不要任何解释文字）：
  {{"tool": "工具名", "args": {{...}}}}

常见流程：
- 复选框验证：先 screenshot 了解位置 → click 复选框中心 → wait 1500 → state 确认
- 滑块验证：先 screenshot 看缺口位置 → drag 从滑块起点到缺口 → wait 1500 → state 确认
- 页面要往下滑才能看到控件：scroll(dy=300) 再 screenshot

**重要**：
1. 截图里只显示视口内的内容，控件可能需要 scroll 才能看到。
2. 点击前先看清楚控件位置，不要点空。
3. 同一个动作连续失败 2 次就换策略（比如改用 drag 或 scroll）。
4. 确认验证已通过（state 显示 挑战中=false 且 token 非空）就调用 finish(passed=true)。
5. 总步数有限，不要做无意义的重复操作。
"""

MAX_STEPS = 12


class BrowserAgent:
    """AI 托管浏览器（视觉模型 + 工具调用循环）"""

    def __init__(self, ai: AIClient, toolbox: BrowserToolbox):
        self.ai = ai
        self.toolbox = toolbox
        self.logger = get_logger()

    # ---------- 主流程 ----------
    async def drive(self, goal: str = "通过 Cloudflare 人机验证",
                    max_steps: int = MAX_STEPS) -> bool:
        """让 AI 驱动浏览器，返回是否达成目标

        循环里把每一步的动作结果记进历史，让 AI 能看见"我上一步做了什么、
        结果如何"，避免反复做同一个无效动作。
        """
        if not (self.ai and getattr(self.ai, "available", False)):
            self.logger.warning("AI 未启用，无法托管浏览器")
            return False

        history: list[str] = []
        self.logger.info(f"[AI托管] 开始（最多 {max_steps} 步）")

        for step in range(1, max_steps + 1):
            # 1) 先看一眼页面
            shot = await self.toolbox.call("screenshot")
            image = shot.get("image")
            state = await self.toolbox.call("state")

            # 2) 检查是否已通过
            if await self._passed():
                self.logger.info(f"[AI托管 {step}/{max_steps}] 挑战已通过")
                return True

            # 3) 问 AI 下一步（附上工具清单 + 历史，避免重复无效动作）
            history_text = "\n".join(history[-6:]) if history else "(无)"
            prompt = (
                f"{SYSTEM_PROMPT.format(tools=format_tools_for_prompt())}\n\n"
                f"目标：{goal}\n\n"
                f"当前页面状态：\n{state.get('result', '')}\n\n"
                f"前面已经做过的动作及结果：\n{history_text}\n\n"
                f"这是当前截图。请判断下一步该做什么，返回 JSON。"
            )
            action = self._ask(prompt, image)
            if not action:
                self.logger.warning(f"[AI托管 {step}/{max_steps}] AI 未返回有效动作，继续尝试")
                history.append("AI 未返回有效动作，等待 1.5s")
                await self.toolbox.call("wait", {"ms": 1500})
                continue

            tool = action.get("tool", "")
            args = action.get("args") or {}

            if tool == "finish":
                passed = bool(args.get("passed"))
                self.logger.info(
                    f"[AI托管 {step}/{max_steps}] 结束：{args.get('reason', '')}")
                return passed

            # 4) 执行动作
            self.logger.info(f"[AI托管 {step}/{max_steps}] 执行 {tool}({args})")
            out = await self.toolbox.call(tool, args)
            result = str(out.get("result", ""))[:120]
            history.append(f"{tool}({args}) → {result}")
            if not out.get("ok"):
                self.logger.warning(f"[AI托管] 工具执行失败: {out.get('result')}")
            if tool not in ("wait", "state"):
                await self.toolbox.call("wait", {"ms": 1200})

        ok = await self._passed()
        self.logger.info(f"[AI托管] 步数用尽，最终{'通过' if ok else '未通过'}")
        return ok

    # ---------- AI 调用 ----------
    def _ask(self, prompt: str, image: Optional[bytes]) -> Optional[dict]:
        """问 AI 下一步动作，解析 JSON"""
        try:
            text = self.ai.vision(image, prompt) if image else self.ai.chat(prompt)
        except Exception as e:
            self.logger.warning(f"[AI托管] AI 请求失败: {e}")
            return None
        return _parse_action(text)

    # ---------- 状态判定 ----------
    async def _passed(self) -> bool:
        """挑战是否已通过：state 工具返回的文本里 "挑战中=False" 即认为已过"""
        out = await self.toolbox.call("state")
        text = (out.get("result") or "").lower()
        return "挑战中=false" in text


def _parse_action(text: str) -> Optional[dict]:
    """从 AI 输出中提取 {tool, args} JSON（容忍 ```json 包裹与前后杂文本）"""
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
