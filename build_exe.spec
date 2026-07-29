# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

产物结构：
  dist/bili_report/
  ├── bili_report.exe          # 入口程序
  ├── _internal/               # 依赖与内置资源
  │   ├── app/web/static/      # 前端页面（只读）
  │   ├── dict/                # 违禁词词典（只读）
  │   ├── config.example.yaml  # 示例配置（用户复制后填写）
  │   └── accounts.example.yaml
  └── (用户运行后生成 config.yaml / accounts.yaml / logs/)
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# 收集 FastAPI / Uvicorn / OpenAI 等动态导入的子模块
hiddenimports = []
for pkg in ["fastapi", "uvicorn", "openai", "pydantic", "pydantic_core", "httpx", "httpcore", "anyio", "starlette"]:
    datas, binaries, hidden = collect_all(pkg)
    hiddenimports += hidden

# PyInstaller 6.x 自动收集大部分依赖，但以下子模块容易被漏掉
hiddenimports += collect_submodules("uvicorn.lifespan")
hiddenimports += collect_submodules("uvicorn.loops")
hiddenimports += collect_submodules("uvicorn.protocols")
hiddenimports += collect_submodules("email.mime")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[
        # 只读资源：前端页面、违禁词词典、示例配置
        ("app/web/static", "app/web/static"),
        ("dict", "dict"),
        ("config.example.yaml", "."),
        ("accounts.example.yaml", "."),
        ("README.md", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块，减小体积
        # 注意：不要排除 argparse（distro 等库依赖）、unittest（部分库测试时导入）
        "tkinter",
        "pydoc",
        "doctest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="bili_report",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[
        # UPX 压缩部分 DLL 会导致运行时崩溃
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "python3.dll",
        "ucrtbase.dll",
    ],
    runtime_tmpdir=None,
    console=True,  # 保留控制台窗口，方便查看启动日志
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # 可后续添加 icon="assets/icon.ico"
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "python3.dll",
        "ucrtbase.dll",
    ],
    name="bili_report",
)
