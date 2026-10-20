"""浏览器工具箱：MCP 服务端与 AI 托管共用的一套浏览器操作工具。

设计目标：
- 一套工具定义，**MCP 服务端**（供外部 AI 客户端接管浏览器）与
  **内置 AI 托管**（CF 实在过不了时由 AI 自己驱动浏览器）复用同一份能力，
  避免两套实现各写一遍、行为不一致。
- 所有坐标都是 **CSS 像素**（与 `page.screenshot(scale="css")` 同尺度），
  AI 返回的坐标可直接用于鼠标，不需要换算设备像素比。
"""
from __future__ import annotations

import time
from typing import Optional

from .logger import get_logger

# ---------------------------------------------------------------------------
# 工具定义（MCP tools/list 与 AI 托管提示词共用）
# ---------------------------------------------------------------------------
TOOL_SPECS: list[dict] = [
    {
        "name": "screenshot",
        "description": "截取当前浏览器窗口的截图并返回图片。用于了解页面当前状态。"
                       "可选参数 x/y/width/height 只截取某个区域（如验证控件那一小块）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "区域左上角 x（CSS 像素，视口相对）"},
                "y": {"type": "number", "description": "区域左上角 y（CSS 像素，视口相对）"},
                "width": {"type": "number", "description": "区域宽度"},
                "height": {"type": "number", "description": "区域高度"},
            },
        },
    },
    {
        "name": "click",
        "description": "在页面指定坐标点击鼠标左键。坐标为 CSS 像素（视口相对）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number", "description": "点击位置 x"},
                "y": {"type": "number", "description": "点击位置 y"},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "drag",
        "description": "从 (x1,y1) 拖动到 (x2,y2)，用于滑块验证码 / 拖动拼图。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "number"}, "y1": {"type": "number"},
                "x2": {"type": "number"}, "y2": {"type": "number"},
                "duration_ms": {"type": "integer", "description": "拖动总耗时，默认 600ms"},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "scroll",
        "description": "滚动页面。dy 为正向下、为负向上（像素）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dx": {"type": "number"}, "dy": {"type": "number"},
            },
            "required": ["dy"],
        },
    },
    {
        "name": "fill",
        "description": "向选择器选中的输入框填入文本。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器"},
                "text": {"type": "string"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "goto",
        "description": "跳转到指定 URL。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "evaluate",
        "description": "在页面执行一段 JavaScript 并返回结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
    },
    {
        "name": "state",
        "description": "获取当前页面状态：URL、标题、是否仍是人机验证页、"
                      "Turnstile token 是否已拿到、页面文本片段。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait",
        "description": "等待指定毫秒数（用于等验证结果）。",
        "inputSchema": {
            "type": "object",
            "properties": {"ms": {"type": "integer", "description": "等待毫秒，默认 1000"}},
        },
    },
    {
        "name": "finish",
        "description": "结束操作。passed=true 表示人机验证已通过，false 表示放弃。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "reason": {"type": "string", "description": "说明"},
            },
            "required": ["passed"],
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_SPECS]


class BrowserToolbox:
    """把浏览器操作包装成可被 MCP / AI 调用的工具。

    返回值统一为 dict：``{"ok": bool, "result": Any, "image": bytes|None}``，
    ``image`` 是截图类工具返回的 PNG bytes（供 AI 视觉输入或 MCP image content）。
    """

    def __init__(self, browser, cf=None):
        self.browser = browser
        self.cf = cf          # 可选：用于读取挑战状态
        self.logger = get_logger()
        self.started_at = time.time()

    # ---------- 工具执行 ----------
    async def call(self, name: str, args: Optional[dict] = None) -> dict:
        args = args or {}
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"ok": False, "result": f"未知工具: {name}", "image": None}
        try:
            return await handler(**args)
        except TypeError as e:
            return {"ok": False, "result": f"参数错误: {e}", "image": None}
        except Exception as e:
            return {"ok": False, "result": f"工具执行失败: {e}", "image": None}

    async def tool_screenshot(self, x: Optional[float] = None, y: Optional[float] = None,
                              width: Optional[float] = None, height: Optional[float] = None) -> dict:
        if x is not None and width and height:
            clip = {"x": float(x), "y": float(y or 0), "width": float(width),
                    "height": float(height)}
            data = await self.browser.clip_screenshot(clip, name="mcp_shot")
        else:
            data = await self.browser.screenshot(name="mcp_shot")
        return {"ok": True, "result": "截图完成", "image": data}

    async def tool_click(self, x: float, y: float) -> dict:
        page = self.browser.page
        await page.mouse.move(float(x), float(y), steps=10)
        await self.browser.wait_timeout(120)
        await page.mouse.click(float(x), float(y))
        await self.browser.wait_timeout(300)
        return {"ok": True, "result": f"已点击 ({float(x):.0f}, {float(y):.0f})"}

    async def tool_drag(self, x1: float, y1: float, x2: float, y2: float,
                        duration_ms: int = 600) -> dict:
        """拟人化拖动：分段移动，模拟真人速度"""
        page = self.browser.page
        await page.mouse.move(float(x1), float(y1), steps=8)
        await self.browser.wait_timeout(120)
        await page.mouse.down()
        await self.browser.wait_timeout(120)
        steps = max(6, int(duration_ms) // 15)
        for i in range(1, steps + 1):
            t = i / steps
            await page.mouse.move(float(x1) + (float(x2) - float(x1)) * t,
                                  float(y1) + (float(y2) - float(y1)) * t, steps=1)
            await self.browser.wait_timeout(int(duration_ms) // steps)
        await self.browser.wait_timeout(200)
        await page.mouse.up()
        return {"ok": True,
                "result": f"已从 ({float(x1):.0f},{float(y1):.0f}) 拖动到 ({float(x2):.0f},{float(y2):.0f})"}

    async def tool_scroll(self, dx: float = 0, dy: float = 0) -> dict:
        await self.browser.page.evaluate(
            "([dx, dy]) => window.scrollBy(dx, dy)", [float(dx), float(dy)]
        )
        await self.browser.wait_timeout(300)
        return {"ok": True, "result": f"已滚动 dx={float(dx):.0f} dy={float(dy):.0f}"}

    async def tool_fill(self, selector: str, text: str) -> dict:
        await self.browser.fill(selector, text)
        return {"ok": True, "result": f"已向 {selector!r} 填入 {len(text)} 个字符"}

    async def tool_goto(self, url: str) -> dict:
        await self.browser.goto(url)
        return {"ok": True, "result": f"已打开 {url}"}

    async def tool_evaluate(self, script: str) -> dict:
        result = await self.browser.evaluate(script)
        return {"ok": True, "result": str(result)[:2000]}

    async def tool_state(self) -> dict:
        page = self.browser.page
        try:
            url = page.url
            title = await page.title()
        except Exception:
            url, title = "", ""
        is_ch = False
        if self.cf is not None:
            try:
                is_ch = await self.cf.is_challenge()
            except Exception:
                pass
        # Turnstile token 状态
        token_state = ""
        try:
            token_state = await page.evaluate("""() => {
                const ins = document.querySelectorAll("input[name='cf-turnstile-response'],#cf-token,input[name='cf-token']");
                return Array.from(ins).map(i => i.value ? '有token' : '空').join(',');
            }""")
        except Exception:
            pass
        text = ""
        try:
            text = (await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 400) : ''")) or ""
        except Exception:
            pass
        info = (f"url={url}\ntitle={title}\n挑战中={is_ch}\n"
                f"Turnstile token={token_state or '(无)'}\n页面文本片段={text.strip()[:300]}")
        return {"ok": True, "result": info}

    async def tool_wait(self, ms: int = 1000) -> dict:
        await self.browser.wait_timeout(max(0, int(ms)))
        return {"ok": True, "result": f"已等待 {int(ms)}ms"}

    async def tool_finish(self, passed: bool, reason: str = "") -> dict:
        return {"ok": True,
                "result": f"{'已通过' if passed else '已放弃'}：{reason}",
                "passed": bool(passed)}


def format_tools_for_prompt() -> str:
    """把工具定义格式化成 AI 提示词里可读的清单"""
    lines = []
    for t in TOOL_SPECS:
        props = t.get("inputSchema", {}).get("properties", {})
        args = ", ".join(f"{k}({v.get('type', 'any')})" for k, v in props.items())
        req = t.get("inputSchema", {}).get("required", [])
        req_s = f"（必填: {', '.join(req)}）" if req else ""
        lines.append(f"- {t['name']}({args}){req_s}：{t['description']}")
    return "\n".join(lines)
