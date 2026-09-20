"""启动入口。"""
from __future__ import annotations

import sys
import traceback

import uvicorn

from app.config import load_settings
from app.main import app


def _wait_before_exit() -> None:
    """打包为 exe 后，异常退出会立即关闭控制台窗口，用户看不到错误信息。
    等待用户按键后再退出，方便排查问题。
    """
    print("\n程序异常退出，按回车键关闭窗口...", file=sys.stderr)
    try:
        input()
    except EOFError:
        # 非交互式环境（如被其他进程调用）无 stdin，直接退出
        pass


def _install_excepthook() -> None:
    """安装全局异常钩子，兜底捕获未处理异常（含 uvicorn 内部抛出的）。"""
    _orig = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        # 用户按 Ctrl+C 主动退出时不需要打印堆栈、更不该停下来等按键
        if issubclass(exc_type, KeyboardInterrupt):
            return
        traceback.print_exception(exc_type, exc_value, exc_tb)
        _wait_before_exit()
        # 不调用 _orig，避免重复打印

    sys.excepthook = hook


def _stop_running_tasks() -> int:
    """给正在跑的任务发停止信号（Ctrl+C 时第一步就要做这件事）。"""
    tm = getattr(app.state, "task_manager", None)
    if tm is None:
        return 0
    try:
        return tm.request_stop_all()
    except Exception:
        return 0


class GracefulServer(uvicorn.Server):
    """Ctrl+C 时先让任务停下来，再走 uvicorn 的优雅退出流程。

    uvicorn 原生行为：收到 SIGINT 后先等待所有连接关闭，而浏览器的 SSE 长连接
    不会自己断开，于是退出会一直卡在 "Waiting for connections to close"，
    这期间 worker 还在继续举报 —— 表现为"按了停止/Ctrl+C 却停不下来"。
    这里在 handle_exit 里先给任务发停止信号，并配合 timeout_graceful_shutdown
    给等待设上限，保证几秒内真正停下来。
    """

    def handle_exit(self, sig, frame):  # type: ignore[override]
        count = _stop_running_tasks()
        if count:
            print(f"[停止] 已向 {count} 个进行中的任务发送停止信号，正在退出…", flush=True)
        else:
            print("[停止] 当前没有进行中的任务，正在退出…", flush=True)
        super().handle_exit(sig, frame)


def main() -> None:
    try:
        s = load_settings("config.yaml")
        host, port = s.server.host, s.server.port
    except Exception:
        host, port = "127.0.0.1", 8000
    # 直接传 app 对象，避免 PyInstaller 打包后字符串导入 "app.main:app" 失效
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        reload=False,
        # 给优雅退出设上限：SSE 长连接不会自己断开，否则 Ctrl+C 后进程会一直挂着
        timeout_graceful_shutdown=5,
    )
    GracefulServer(config).run()


if __name__ == "__main__":
    _install_excepthook()
    try:
        main()
    except KeyboardInterrupt:
        # uvicorn 关停后会重新抛出 SIGINT，这里按正常退出处理：
        # 不打印堆栈、不等待按键，直接结束
        print("\n已退出。")
    except SystemExit as e:
        # uvicorn 启动失败时会 sys.exit(nonzero)（如 lifespan 异常）。
        # 非 0 退出码表示异常退出，此时错误已在日志中打印，等待用户按键后再关闭窗口。
        if e.code and e.code != 0:
            _wait_before_exit()
        raise
    except Exception:
        traceback.print_exc()
        _wait_before_exit()
