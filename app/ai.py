"""OpenAI 兼容客户端：提示词、批量分析弹幕、JSON 解析。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import AIConfig

logger = logging.getLogger("ai")


# =====================================================================
# 系统提示词
# =====================================================================

SYSTEM_PROMPT = """你是弹幕审核员，对B站弹幕做快速违规判定。基于关键词直觉判断，不要逐条推理分析。

判定违规的情形（命中任一即违规）：
- 人身攻击、辱骂、侮辱性言论
- 色情低俗
- 引战、煽动对立、地域/性别攻击
- 违法违禁
- 垃圾广告、引流
- 侵犯隐私
- 恶意刷屏
- 青少年不良信息

不违规：正常吐槽、玩梗、表达观点（即使语气重但无攻击性）、剧透讨论、与视频相关的闲聊。

限制：
- 无法获知视频内容，不得以"剧透"或"与视频无关"为由判违规。
- 不要思考、不要分析、不要解释，直接给出 JSON 结果。
- 禁止输出 <think> 标签、推理过程、markdown 代码块标记或任何额外文字。
- 输出必须以 { 开头、以 } 结尾，是一个紧凑的 JSON 对象。

输出格式（严格 JSON，无任何附加内容）：
{"items":[{"id":<整数id>,"violates":<true|false>,"reason":<下方代码表中整数>,"confidence":<0.0-1.0>}]}

reason 代码表（仅可使用以下代码，禁止使用 8 剧透 和 10 视频无关）：
1 违法违禁
2 色情低俗
3 非法交易
4 人身攻击
5 侵犯隐私
6 垃圾广告
7 引战
9 恶意刷屏
11 其他
12 青少年不良

规则：
- id 必须与输入 id 完全一致，每条输入都必须有且仅有一条结果。
- 不违规时 reason 填 11、violates 填 false。
- confidence 反映判定置信度（0.0-1.0），简单明确的判定给 0.9 以上。
"""


# =====================================================================
# 分析结果与客户端
# =====================================================================

@dataclass
class AnalysisResult:
    """单条弹幕分析结果。id 为送审时传入的 id。"""
    id: int
    violates: bool
    reason: int
    confidence: float


class AIAnalyzer:
    def __init__(self, config: AIConfig):
        self.config = config
        self.client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
        )

    async def analyze_batch(self, items: list[dict]) -> list[AnalysisResult]:
        """分析一批弹幕。

        items: [{"id": int, "content": str}, ...]
        返回与输入对应的 AnalysisResult 列表；AI 失败的批次返回空列表（由调用方决定跳过）。
        """
        if not items:
            return []

        user_content = json.dumps(items, ensure_ascii=False)
        last_exc: Exception | None = None

        # 构建请求参数：reasoning_effort 仅在配置非空时发送（兼容不支持该参数的 API）
        kwargs: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        # temperature=0 在部分思考型模型实现中会触发更长推理，仅在配置 >0 时传递
        if self.config.temperature > 0:
            kwargs["temperature"] = self.config.temperature
        # reasoning_effort 控制思考型模型推理深度，minimal 显著降低思考开销
        if self.config.reasoning_effort and self.config.reasoning_effort.strip():
            kwargs["reasoning_effort"] = self.config.reasoning_effort.strip()

        for attempt in range(1, 3):
            try:
                t0 = time.monotonic()
                logger.info(
                    "AI 请求开始 model=%s count=%d attempt=%d max_tokens=%d reasoning=%s",
                    self.config.model, len(items), attempt, self.config.max_tokens,
                    self.config.reasoning_effort or "-",
                )
                resp = await self.client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                text = choice.message.content or ""
                elapsed = time.monotonic() - t0
                if not text.strip():
                    # 思考型模型可能因 max_tokens 不足在推理阶段被截断
                    logger.warning(
                        "AI 返回空内容(finish=%s, %.1fs)，可能 max_tokens 不足或模型仍在思考，跳过该批 %d 条",
                        choice.finish_reason, elapsed, len(items),
                    )
                    return []
                logger.info(
                    "AI 请求完成 %.1fs finish=%s content_len=%d",
                    elapsed, choice.finish_reason, len(text),
                )
                return self._parse(text, items)
            except Exception as e:
                elapsed = time.monotonic() - t0
                last_exc = e
                # 部分后端不支持 reasoning_effort 参数，首次失败时移除后重试
                if attempt == 1 and "reasoning_effort" in kwargs:
                    logger.warning(
                        "AI 批分析失败(第%d次, %.1fs): %s，移除 reasoning_effort 后重试",
                        attempt, elapsed, e,
                    )
                    kwargs.pop("reasoning_effort", None)
                    continue
                logger.warning("AI 批分析失败(第%d次, %.1fs): %s", attempt, elapsed, e)
                # 重试前短退避，避免本地模型过载时连续冲击
                if attempt < 2:
                    backoff = 2.0 * attempt
                    logger.info("AI 重试 %.1fs 后再次请求", backoff)
                    await asyncio.sleep(backoff)

        logger.error("AI 批分析最终失败，跳过该批 %d 条: %s", len(items), last_exc)
        return []

    def _parse(self, text: str, items: list[dict]) -> list[AnalysisResult]:
        obj = self._extract_json(text)
        if obj is None:
            logger.warning("AI 返回无法解析为 JSON: %s", text[:200])
            return []

        raw_items = obj.get("items") if isinstance(obj, dict) else None
        if not isinstance(raw_items, list):
            # 兼容直接返回数组的情况
            if isinstance(obj, list):
                raw_items = obj
            else:
                logger.warning("AI 返回缺少 items 字段: %s", text[:200])
                return []

        id_map: dict[int, AnalysisResult] = {}
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            try:
                did = int(it.get("id"))
                violates = bool(it.get("violates"))
                reason = int(it.get("reason", 11))
                conf = float(it.get("confidence", 0.5))
                id_map[did] = AnalysisResult(did, violates, reason, conf)
            except (TypeError, ValueError):
                continue

        # 保证每条输入都有结果；AI 漏掉的按"不违规"处理
        results: list[AnalysisResult] = []
        for it in items:
            did = int(it["id"])
            results.append(id_map.get(did, AnalysisResult(did, False, 11, 0.0)))
        return results

    @staticmethod
    def _extract_json(text: str):
        text = text.strip()
        # 剥离内联 <think>...</think> 思考块（部分模型会内联输出）
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = text.strip()
        # 去除可能的 ```json ... ``` 包裹
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 取最后一个 {...} 块（避免思考残留中的 { 干扰）
            matches = re.findall(r"\{.*\}", text, re.DOTALL)
            for m in reversed(matches):
                try:
                    return json.loads(m)
                except json.JSONDecodeError:
                    continue
            return None
