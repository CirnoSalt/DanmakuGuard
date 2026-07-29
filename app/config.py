"""配置加载与校验。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from app.paths import runtime_path


class BiliCookie(BaseModel):
    """单个 B站账号的 Cookie 凭证。"""
    name: str = ""        # 账号别名，仅用于日志展示
    sessdata: str
    bili_jct: str


class BilibiliConfig(BaseModel):
    # 账号已迁移至 accounts.yaml；此字段保留仅为向后兼容旧 config.yaml（不推荐）
    cookie: Optional[BiliCookie] = None
    request_interval: float = 7.5
    max_retries: int = 3
    segment_cap: int = 30
    # accounts.yaml 路径，默认与 config.yaml 同目录
    accounts_path: str = "accounts.yaml"
    # 全部账号风控后的等待循环间隔（秒）
    all_limited_wait: float = 300.0


class AIConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = "sk-xxx"
    model: str = "gpt-4o-mini"
    batch_size: int = 20
    temperature: float = 0.0
    confidence_threshold: float = 0.6
    max_tokens: int = 2000  # 仅容纳 JSON 输出，过大会鼓励思考型模型展开推理
    timeout: float = 120.0  # 单次 AI 调用超时秒数，避免本地模型卡死拖垮 worker
    # 思考型模型的推理深度控制：minimal/low/medium/high。
    # minimal 适合本场景的快速判定，可避免思考型模型过度推理。
    # 留空则不发送该参数（兼容不支持 reasoning_effort 的 API）。
    reasoning_effort: str = "minimal"


class ReportConfig(BaseModel):
    default_reason: int = 7
    dedup_by_content: bool = True
    max_reports: int = 0  # 单任务举报上限，0 表示不限
    dictionary_path: str = "dict/banned_words.yaml"  # 违禁词词典路径，不存在则禁用预过滤


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


class Settings(BaseModel):
    bilibili: BilibiliConfig
    ai: AIConfig = Field(default_factory=AIConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    p = Path(path)
    # 相对路径优先从 exe 同级目录查找（兼容 PyInstaller 打包环境）
    if not p.is_absolute():
        p = runtime_path(str(path))
    if not p.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {p}，请复制 config.example.yaml 为 config.yaml 并填写"
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings(**data)


def load_accounts(path: str | Path = "accounts.yaml") -> list[BiliCookie]:
    """加载账号列表文件。返回 BiliCookie 列表，文件不存在或为空返回 []。

    accounts.yaml 格式：
        accounts:
          - name: 账号1
            sessdata: xxx
            bili_jct: yyy
          - name: 账号2
            sessdata: xxx
            bili_jct: yyy
    """
    p = Path(path)
    if not p.is_absolute():
        p = runtime_path(str(path))
    if not p.exists():
        return []
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    # 兼容两种顶层结构：{accounts: [...]} 或直接 [...]
    accounts_data = data.get("accounts") if isinstance(data, dict) else data
    if not isinstance(accounts_data, list):
        return []
    result: list[BiliCookie] = []
    for i, item in enumerate(accounts_data, 1):
        if not isinstance(item, dict):
            continue
        try:
            result.append(BiliCookie(
                name=item.get("name") or f"账号{i}",
                sessdata=item.get("sessdata", ""),
                bili_jct=item.get("bili_jct", ""),
            ))
        except Exception:
            continue
    return result
