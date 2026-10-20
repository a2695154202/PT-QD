"""浏览器 MCP 服务端：把浏览器暴露为 MCP 工具，供外部 AI 客户端接管。

**用途**：当内置的 Cloudflare 自动处理实在过不去时，把浏览器的控制权
交给外部 AI（例如支持 MCP 的 AI 助手）——外部 AI 通过本服务端
读取页面、点击、拖动滑块，手动把人机验证通过。

**协议**：MCP (Model Context Protocol) over stdio，JSON-RPC 2.0。
每行一条 JSON-RPC 消息（Content-Length 头可选，这里用换行分隔的 JSON）。

**用法**：
    python main.py mcp
    # 或指定要打开的 URL / 连接已运行的 Edge
    python main.py mcp --url https://example.com --mode attach

在支持 MCP 的客户端（如 Claude Desktop / 各类 AI 助手）里注册：
    {
      "command": "python",
      "args": ["D:/path/to/main.py", "mcp"],
      "cwd": "D:/path/to/project"
    }

注册后，外部 AI 就能调用 screenshot / click / drag / state 等工具操作浏览器。
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any, Optional

from .logger import get_logger

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "signin-browser", "version": "1.0.0"}


class BrowserMCP:
    """浏览器 MCP 服务端（stdio JSON-RPC 2.0）"""

    def __init__(self, toolbox):
        self.toolbox = toolbox
        self.logger = get_logger()
        self._initialized = False

    # ---------- JSON-RPC 分发 ----------
    async def handle(self, msg: dict) -> Optional[dict]:
        method = msg.get("method", "")
        mid = msg.get("id")
        params = msg.get("params") or {}

        try:
            if method == "initialize":
                self._initialized = True
                return self._ok(mid, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                })
            if method == "notifications/initialized":
                return None
            if method == "tools/list":
                from .browser_tools import TOOL_SPECS
                return self._ok(mid, {"tools": TOOL_SPECS})
            if method == "tools/call":
                return await self._call_tool(mid, params)
            if method == "ping":
                return self._ok(mid, {})
            # 未知方法
            return self._err(mid, -32601, f"未知方法: {method}")
        except Exception as e:
            self.logger.error(f"MCP 方法 {method} 执行失败: {e}")
            return self._err(mid, -32000, f"执行失败: {e}")

    async def _call_tool(self, mid: Any, params: dict) -> dict:
        name = params.get("name", "")
        args = params.get("arguments") or {}
        self.logger.info(f"[MCP] 调用工具 {name} 参数={args}")
        out = await self.toolbox.call(name, args)
        if not out.get("ok"):
            return self._ok(mid, {
                "content": [{"type": "text", "text": str(out.get("result", "失败"))}],
                "isError": True,
            })
        content: list[dict] = []
        text = str(out.get("result", ""))
        if text:
            content.append({"type": "text", "text": text})
        img = out.get("image")
        if img:
            content.append({
                "type": "image",
                "data": base64.b64encode(img).decode("ascii"),
                "mimeType": "image/png",
            })
        result: dict = {"content": content, "isError": False}
        if "passed" in out:
            result["passed"] = out["passed"]
        return self._ok(mid, result)

    # ---------- JSON-RPC 编码 ----------
    @staticmethod
    def _ok(mid: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    @staticmethod
    def _err(mid: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    # ---------- stdio 主循环 ----------
    async def serve_stdio(self) -> None:
        """从 stdin 读 JSON-RPC，写响应到 stdout（每行一条 JSON）。

        Windows 下 asyncio 的 connect_read_pipe 无法注册 stdin 句柄
        （proactor 报 WinError 6「句柄无效」），因此改用线程池阻塞读，
        这样跨平台都稳定。
        """
        self.logger.info("浏览器 MCP 服务端已启动（stdio）。等待 AI 客户端连接…")
        loop = asyncio.get_running_loop()
        while True:
            try:
                # 在线程池里阻塞读一行，不占用事件循环
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception as e:
                self.logger.warning(f"读取 stdin 失败: {e}")
                break
            if not line:
                break  # stdin 关闭
            text = line.decode("utf-8", errors="replace").strip() if isinstance(line, bytes) \
                else line.strip()
            if not text:
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            resp = await self.handle(msg)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        self.logger.info("浏览器 MCP 服务端结束")
