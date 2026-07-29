"""启动入口。"""
from __future__ import annotations

import uvicorn

from app.config import load_settings
from app.main import app


def main() -> None:
    try:
        s = load_settings("config.yaml")
        host, port = s.server.host, s.server.port
    except Exception:
        host, port = "127.0.0.1", 8000
    # 直接传 app 对象，避免 PyInstaller 打包后字符串导入 "app.main:app" 失效
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
