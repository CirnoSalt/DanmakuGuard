"""资源路径处理：兼容开发环境与 PyInstaller 打包环境。

- `resource_path`：只读资源（如前端页面、违禁词词典），打包后位于 `_MEIPASS` 中
- `runtime_path`：可写文件（如 config.yaml、logs/），位于 exe 同级目录
"""
from __future__ import annotations

import sys
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(relative: str | Path = "") -> Path:
    """获取只读资源路径。打包后资源位于 `sys._MEIPASS` 临时目录中。"""
    if _is_frozen():
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative if relative else base


def runtime_path(relative: str | Path = "") -> Path:
    """获取运行时可写路径。打包后位于 exe 同级目录（用户可编辑 config.yaml）。"""
    if _is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative if relative else base
