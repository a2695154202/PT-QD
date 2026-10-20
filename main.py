"""网站签到助手 - 入口

不带参数运行 = 启动图形界面（exe 双击即用）。
命令行用法：
  python main.py list                      列出所有签到任务
  python main.py run <task_id>             运行指定任务（如 pt_kong）
  python main.py run --all                 运行所有启用的任务
  python main.py import-qr <图片> [--task] 导入谷歌身份验证器二维码（2FA）
  python main.py test-ai                   测试 AI 配置是否可用
  python main.py test-cookiecloud          测试 CookieCloud 连接与解密
  python main.py show-config               查看当前配置
  python main.py gui                       启动图形界面
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.ai import AIClient
from core.config import AppConfig, TaskConfig, TASKS_CONFIG_DIR, load_task_configs, _abs
from core.cookiecloud import CookieCloudClient
from core.logger import setup_logging
from tasks.manager import TaskManager


def _print_task_table(configs) -> None:
    if not configs:
        print("（没有任务配置，请在 config/tasks/ 下添加 *.yaml）")
        return
    print(f"{'ID':<14}{'名称':<10}{'启用':<5}{'账号':<8}{'2FA':<5}{'CF':<5}站点")
    print("-" * 86)
    for c in configs:
        print(f"{c.id:<14}{c.name:<10}{'是' if c.enabled else '否':<5}"
              f"{'已配置' if c.username else '未配置':<8}"
              f"{'开' if c.has_2fa else '关':<5}{'开' if c.has_cf else '关':<5}{c.url}")


def cmd_list(args) -> None:
    _print_task_table(load_task_configs())


async def _run_async(args) -> int:
    if not args.all and not args.task_id:
        print("请指定任务 ID（例如 python main.py run pt_kong），或用 --all 运行所有启用任务")
        print()
        _print_task_table(load_task_configs())
        return 2
    manager = TaskManager()
    try:
        if args.all:
            results = await manager.run_all(only_enabled=True)
        else:
            results = [await manager.run_task(args.task_id)]
        print("\n========== 执行结果 ==========")
        ok_all = True
        for r in results:
            mark = "成功" if r.success else "失败"
            print(f"[{mark}] [{r.task_id}] {r.message}")
            if r.screenshots:
                print(f"  截图: {', '.join(r.screenshots)}")
            ok_all = ok_all and r.success
        log_path = results[-1].run_log if results else ""
        if log_path:
            print(f"本次运行日志: {log_path}")
        print("浏览器已关闭")
        return 0 if ok_all else 1
    finally:
        await manager.close()


def cmd_run(args) -> None:
    sys.exit(asyncio.run(_run_async(args)))


def cmd_import_qr(args) -> None:
    """导入谷歌身份验证器二维码（支持 otpauth:// 与 otpauth-migration:// 导出格式）"""
    from core.totp import load_from_qr
    from core.twofa_store import TwoFactorVault, VAULT_PATH

    accounts = load_from_qr(args.path)
    print(f"二维码解析成功，共 {len(accounts)} 个账号:")
    for i, a in enumerate(accounts, 1):
        print(f"  [{i}] {a.describe()}")

    vault = TwoFactorVault()
    added, updated = vault.add_many(accounts)
    print(f"已写入凭证库 {VAULT_PATH}（新增 {added}，更新 {updated}）")

    if args.task:
        # 绑定到指定任务：issuer 与任务站点域名匹配优先；仅 1 个账号时直接绑定
        task_path = TASKS_CONFIG_DIR / f"{args.task}.yaml"
        host = ""
        if task_path.exists():
            host = urlparse(TaskConfig.from_yaml(task_path).url).hostname or ""
        stem = host.split(".")[0].lower() if host else ""
        bound = 0
        bound_keys = []
        for a in accounts:
            iss = (a.issuer or "").lower()
            matched = bool(host and iss) and ((stem and stem in iss) or host in iss or iss in host)
            if not matched and len(accounts) == 1:
                matched = True  # 只有一个账号时直接绑定
            if matched:
                bound_keys.append(f"{a.issuer}|{a.account}")
                vault.bind(a.issuer, a.account, args.task)
                bound += 1
        if task_path.exists():
            cfg = TaskConfig.from_yaml(task_path)
            if bound_keys:
                cfg.set_vault_binding(bound_keys[0], two_step=True)
            cfg.save()
        print(f"已绑定 {bound} 个账号到任务 [{args.task}] 并勾选「有 2FA」")
        if bound == 0:
            print(f"提示：没有账号的 issuer 与 {host or args.task} 匹配，"
                  f"可在图形界面任务「配置」中手动选择绑定")
    if accounts:
        print(f"验证码自检示例（{accounts[0].issuer or '首个账号'}）: {accounts[0].now_code()}")


def cmd_test_ai(args) -> None:
    cfg = AppConfig.load()
    ai = AIClient(cfg.ai)
    if not ai.available:
        print("AI 未配置：请在 config/config.yaml 的 ai 节点填写 base_url / api_key / model")
        sys.exit(1)
    try:
        reply = ai.chat("请只回复两个字：可用")
        print(f"AI 连接成功，回复: {reply}")
    except Exception as e:
        print(f"AI 连接失败: {e}")
        sys.exit(1)


def cmd_test_cookiecloud(args) -> None:
    cfg = AppConfig.load()
    if not cfg.cookiecloud.enabled:
        print("CookieCloud 未启用：请在 config/config.yaml 的 cookiecloud 节点填写 server/uuid/password 并设 enabled=true")
        sys.exit(1)
    ok, msg = CookieCloudClient(cfg.cookiecloud).test_connection()
    print(msg)
    sys.exit(0 if ok else 1)


def cmd_show_config(args) -> None:
    cfg = AppConfig.load()
    print(f"浏览器模式     : {cfg.browser.mode}"
          + (f" ({cfg.browser.cdp_endpoint})" if cfg.browser.mode == "attach"
             else f" (调试端口 {cfg.browser.debugging_port})"))
    print(f"用户数据目录   : {cfg.browser.user_data_dir_abs}")
    print(f"AI             : {'已启用' if cfg.ai.enabled else '未启用'} | {cfg.ai.base_url} | model={cfg.ai.model}")
    print(f"CookieCloud    : {'已启用' if cfg.cookiecloud.enabled else '未启用'} | {cfg.cookiecloud.server}")
    print(f"日志           : {_abs(cfg.log_file)} ({cfg.log_level})")
    print()
    _print_task_table(load_task_configs())


def cmd_gui(args) -> None:
    from ui.gui import run_gui
    run_gui()


def cmd_mcp(args) -> None:
    """启动浏览器 MCP 服务端，把浏览器暴露给外部 AI 客户端接管。

    用途：Cloudflare 人机验证实在自动过不去时，把控制权交给外部 AI
    （支持 MCP 的 AI 助手），由它读取页面、点击、拖动滑块通过验证。

    在 MCP 客户端里注册（例如 Claude Desktop / 各类 AI 助手）：
      {"command": "python", "args": ["main.py", "mcp"], "cwd": "本项目目录"}
    """
    import asyncio

    from core.browser import BrowserController
    from core.browser_tools import BrowserToolbox
    from core.cf import CFHandler
    from core.logger import redirect_console_to_stderr
    from core.mcp_server import BrowserMCP

    cfg = AppConfig.load()
    # MCP 用 stdout 传 JSON-RPC，日志必须改写到 stderr，否则会污染协议流
    redirect_console_to_stderr()

    async def _serve() -> int:
        browser = BrowserController(cfg.browser)
        try:
            await browser.start()
            url = (args.url or "").strip()
            if url:
                try:
                    await browser.goto(url)
                except Exception as e:
                    print(f"打开 {url} 失败: {e}", file=sys.stderr)
            # 挑战处理器用于 state 工具读取挑战状态
            cf = CFHandler(browser, None, None)
            toolbox = BrowserToolbox(browser, cf)
            mcp = BrowserMCP(toolbox)
            await mcp.serve_stdio()
            return 0
        finally:
            await browser.stop()

    print("浏览器 MCP 服务端启动中…（stdio 传输，供外部 AI 客户端接管浏览器）",
          file=sys.stderr)
    sys.exit(asyncio.run(_serve()))


def _bootstrap_runtime() -> None:
    """打包成 exe 时，首次运行把内置的配置模板释放到 exe 同目录（用户可编辑）"""
    if not getattr(sys, "frozen", False):
        return
    import shutil
    from core.config import CONFIG_DIR
    bundle = Path(getattr(sys, "_MEIPASS", "")) / "config"
    if CONFIG_DIR.exists() or not bundle.exists():
        return
    try:
        shutil.copytree(bundle, CONFIG_DIR)
        # 写 stderr：MCP 用 stdout 传 JSON-RPC，不能被无关输出污染
        print(f"已释放初始配置到: {CONFIG_DIR}", file=sys.stderr)
    except Exception as e:
        print(f"释放初始配置失败: {e}", file=sys.stderr)


def _safe_console() -> None:
    """Windows 中文控制台为 GBK 编码，无法编码的字符（如图标符号）改为替换而非崩溃"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def main() -> None:
    _safe_console()
    _bootstrap_runtime()
    parser = argparse.ArgumentParser(
        prog="signin",
        description="网站签到助手：Edge 浏览器自动化 + AI 验证码识别 + CookieCloud + 2FA",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="列出所有任务").set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="运行任务")
    p_run.add_argument("task_id", nargs="?", help="任务 ID，如 pt_kong")
    p_run.add_argument("--all", action="store_true", help="运行所有启用的任务")
    p_run.set_defaults(func=cmd_run)

    p_qr = sub.add_parser("import-qr", help="导入谷歌身份验证器二维码")
    p_qr.add_argument("path", help="二维码图片路径")
    p_qr.add_argument("--task", default=None, help="绑定到哪个任务（默认只入凭证库，不绑定）")
    p_qr.set_defaults(func=cmd_import_qr)

    p_mcp = sub.add_parser("mcp", help="启动浏览器 MCP 服务端（CF 过不去时让外部 AI 接管浏览器）")
    p_mcp.add_argument("--url", default=None, help="启动后先打开的 URL")
    p_mcp.set_defaults(func=cmd_mcp)

    sub.add_parser("test-ai", help="测试 AI 配置").set_defaults(func=cmd_test_ai)
    sub.add_parser("test-cookiecloud", help="测试 CookieCloud 连接").set_defaults(func=cmd_test_cookiecloud)
    sub.add_parser("show-config", help="查看当前配置").set_defaults(func=cmd_show_config)
    sub.add_parser("gui", help="启动图形界面").set_defaults(func=cmd_gui)

    args = parser.parse_args()
    if not args.command:
        # 无参数（如 exe 双击）→ 启动图形界面
        from ui.gui import run_gui
        run_gui()
        return

    cfg = AppConfig.load()
    setup_logging(cfg.log_level, _abs(cfg.log_file))
    args.func(args)


if __name__ == "__main__":
    main()
