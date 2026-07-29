"""FastAPI 应用装配与生命周期。"""
from __future__ import annotations

import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.accounts import AccountManager
from app.ai import AIAnalyzer
from app.bilibili import BiliClient
from app.config import Settings, load_accounts, load_settings
from app.core import BannedDictionary, TaskManager
from app.logger import register_secret, setup_logging
from app.paths import resource_path, runtime_path
from app.web.routes import create_router


def _ensure_user_configs() -> None:
    """首次启动时自动从内置示例复制 config.yaml / accounts.yaml 到 exe 同级目录。

    打包后示例配置位于 _internal/（只读），程序运行时在 exe 同级目录查找可写配置。
    若用户未手动复制示例配置，此处自动复制一份，免去用户查找 _internal 目录的麻烦。
    """
    for name in ("config.example.yaml", "accounts.example.yaml"):
        target_name = name.replace(".example", "")
        target = runtime_path(target_name)
        if target.exists():
            continue
        src = resource_path(name)
        if src.exists():
            try:
                shutil.copyfile(src, target)
            except OSError:
                # 复制失败不阻断启动，后续 load_settings 会给出明确错误
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    log = logging.getLogger("app.main")
    _ensure_user_configs()
    s: Settings = load_settings("config.yaml")
    setup_logging()

    # 注册敏感信息脱敏（API key）
    register_secret(s.ai.api_key)

    # 加载账号列表：优先 accounts.yaml；为空时回退到 config.yaml 的单账号（向后兼容）
    accounts = load_accounts(s.bilibili.accounts_path)
    if not accounts and s.bilibili.cookie is not None:
        log.warning(
            "accounts.yaml 未配置，回退到 config.yaml 中的单账号模式（建议迁移至 accounts.yaml）"
        )
        accounts = [s.bilibili.cookie]

    if not accounts:
        raise RuntimeError(
            "未配置任何 B站账号，请在 accounts.yaml 中填写至少一个账号的 Cookie"
        )

    # Cookie 格式预检（真实 Cookie 应为 ASCII）
    for acc in accounts:
        for name, val in [("SESSDATA", acc.sessdata), ("bili_jct", acc.bili_jct)]:
            if not val or not val.strip() or "你的" in val:
                raise RuntimeError(
                    f"账号 {acc.name} 的 {name} 未填写，请填写真实 Cookie 值"
                )
            try:
                val.encode("ascii")
            except UnicodeEncodeError:
                raise RuntimeError(
                    f"账号 {acc.name} 的 {name} 含非 ASCII 字符，请确认填写的是真实 Cookie 值"
                )
        # 注册脱敏
        register_secret(acc.sessdata)
        register_secret(acc.bili_jct)

    bili = BiliClient(s.bilibili)
    account_manager = AccountManager(accounts, bili, s.bilibili)
    ok = await account_manager.init_first_account()
    if not ok:
        await bili.aclose()
        raise RuntimeError(
            "所有账号登录态校验失败，请检查 accounts.yaml 中的 SESSDATA / bili_jct"
        )

    analyzer = AIAnalyzer(s.ai)

    # 加载违禁词词典（用于 AI 审查前的本地预过滤）
    dictionary = BannedDictionary()
    dict_count = dictionary.load(s.report.dictionary_path)
    if dict_count > 0:
        log.info("违禁词预过滤已启用：%d 条词条", dict_count)

    tm = TaskManager(s, bili, analyzer, dictionary, account_manager)
    await tm.start()

    app.state.settings = s
    app.state.task_manager = tm
    app.state.bili_client = bili
    app.state.account_manager = account_manager

    log.info("服务启动完成，监听 %s:%s", s.server.host, s.server.port)
    yield

    await tm.shutdown()
    await bili.aclose()
    log.info("服务已关闭")


def create_app() -> FastAPI:
    app = FastAPI(title="DanmakuGuard", lifespan=lifespan)
    app.include_router(create_router())
    static_dir = resource_path("app/web/static")
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


app = create_app()
