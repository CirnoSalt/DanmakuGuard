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
        traceback.print_exception(exc_type, exc_value, exc_tb)
        _wait_before_exit()
        # 不调用 _orig，避免重复打印

    sys.excepthook = hook


def main() -> None:
    try:
        s = load_settings("config.yaml")
        host, port = s.server.host, s.server.port
    except Exception:
        host, port = "127.0.0.1", 8000
    # 直接传 app 对象，避免 PyInstaller 打包后字符串导入 "app.main:app" 失效
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    _install_excepthook()
    try:
        main()
    except SystemExit as e:
        # uvicorn 启动失败时会 sys.exit(nonzero)（如 lifespan 异常）。
        # 非 0 退出码表示异常退出，此时错误已在日志中打印，等待用户按键后再关闭窗口。
        if e.code and e.code != 0:
            _wait_before_exit()
        raise
    except Exception:
        traceback.print_exc()
        _wait_before_exit()
