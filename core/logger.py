"""日志模块：控制台 + 全局日志 + 每次运行独立日志文件"""
import logging
import sys
import time
from pathlib import Path

_initialized = False
_run_handler: logging.FileHandler | None = None
_run_log_path: str | None = None

FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")


def setup_logging(level: str = "INFO", log_file: str = "data/logs/app.log") -> logging.Logger:
    """初始化基础日志（控制台 + 全局滚动日志），重复调用只更新日志级别"""
    global _initialized
    logger = logging.getLogger("signin")
    # 级别每次都重新应用：在「全局设置」里改完日志级别应当立即生效
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if _initialized:
        return logger

    # 中文 Windows 控制台为 GBK，无法编码的字符替换而非抛异常；
    # line_buffering=True 保证重定向到文件/管道时日志也是逐行即时刷出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace", line_buffering=True)
        except Exception:
            pass

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(FMT)
    logger.addHandler(console)

    if log_file:
        try:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(path, encoding="utf-8")
            fh.setFormatter(FMT)
            logger.addHandler(fh)
        except OSError:
            pass  # 文件日志失败不影响主流程

    _initialized = True
    return logger


def start_run_log(log_dir: str = "data/logs", prefix: str = "run") -> str:
    """为当次运行创建独立日志文件（data/logs/run_YYYYMMDD_HHMMSS.log），返回路径。

    每次签到运行开始时调用，运行结束（含异常）调用 stop_run_log 收尾。
    """
    global _run_handler, _run_log_path
    stop_run_log()  # 清理上一次的句柄，避免重复写入
    logger = get_logger()
    try:
        path = Path(log_dir) / f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(FMT)
        fh.setLevel(logging.INFO)
        logger.addHandler(fh)
        _run_handler = fh
        _run_log_path = str(path)
        logger.info(f"本次运行日志: {path}")
        return str(path)
    except OSError as e:
        logger.warning(f"创建运行日志文件失败: {e}")
        return ""


def stop_run_log() -> str:
    """关闭当次运行日志并返回其路径"""
    global _run_handler, _run_log_path
    path = _run_log_path or ""
    if _run_handler is not None:
        try:
            get_logger().removeHandler(_run_handler)
            _run_handler.close()
        except Exception:
            pass
        _run_handler = None
    _run_log_path = None
    return path


def current_run_log() -> str:
    return _run_log_path or ""


def get_logger() -> logging.Logger:
    return logging.getLogger("signin")


def redirect_console_to_stderr() -> None:
    """把控制台日志改写到 stderr。

    MCP 服务端用 stdout 作为 JSON-RPC 通道（MCP stdio 约定），
    日志若继续写 stdout 会污染协议流，必须改写到 stderr。
    """
    logger = logging.getLogger("signin")
    for handler in list(logger.handlers):
        if isinstance(handler, logging.StreamHandler) and handler.stream is sys.stdout:
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(FMT)
    logger.addHandler(sh)
