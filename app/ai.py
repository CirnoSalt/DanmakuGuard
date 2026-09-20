"""OpenAI 兼容客户端：提示词、批量分析弹幕、JSON 解析。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass

from openai import APIConnectionError, AsyncOpenAI

import httpx

from app.config import AIConfig

logger = logging.getLogger("ai")


# =====================================================================
# 系统提示词
# =====================================================================

SYSTEM_PROMPT = """你是B站弹幕审核员，凭关键词直觉快速判定，输出必须极简。
违规类型：人身攻击辱骂、色情低俗、引战对立、违法违禁、垃圾广告引流、侵犯隐私、恶意刷屏、青少年不良。
不违规：正常吐槽、玩梗、表达观点、剧透、与视频相关的闲聊。禁止以"剧透""与视频无关"判违规。

输出规则（严格遵守，用于降低开销）：
- 只列出违规弹幕，严格遵守下面格式，不要输出解释、思考过程、<think> 标签或 markdown 代码块。
- 没有任何违规时，必须输出 {"items":[]}。
- id 必须与输入的 id 完全一致；未列出的 id 一律视为不违规。

输出格式（严格 JSON，无其他文字）：
{"items":[{"id":<输入id>,"violates":true,"reason":<代码>,"confidence":<0.0-1.0>}]}

reason 代码（禁止使用 8 剧透、10 视频无关）：
1违法违禁 2色情低俗 3非法交易 4人身攻击 5侵犯隐私 6垃圾广告 7引战 9恶意刷屏 11其他 12青少年不良
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
        # max_retries=0：禁用 openai SDK 内置重试，确保 aclose() 后请求立即失败
        # 重试逻辑由 analyze_batch 内部控制（基于异常类型决定是否重试）
        self.client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout,
            max_retries=0,
        )
        # 模型连通性标记：启动探测失败时置 False，任务处理自动退化为纯字典模式
        self.available: bool = False

    async def check_connectivity(self) -> bool:
        """启动时探测配置的模型是否可连接。

        探测使用独立的短超时客户端发起一次极小 chat 补全（不干扰日常分析用的主客户端），
        能同时验证 base_url 可达性与配置的 model 是否可用。探测失败则标记 available=False，
        上层据此自动切换为纯字典模式。
        """
        probe = AsyncOpenAI(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout=min(self.config.timeout, 20.0),
            max_retries=0,
        )
        try:
            resp = await probe.chat.completions.create(
                model=self.config.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            self.available = bool(resp.choices)
            if self.available:
                logger.info("AI 模型连通性探测成功：%s", self.config.model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.available = False
            logger.warning("AI 模型连通性探测失败，将自动切换为纯字典模式：%s", exc)
        finally:
            try:
                await probe.close()
            except Exception:
                pass
        return self.available

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接，中断所有进行中的 AI 请求。

        停止任务时调用，实现秒级中断。注意必须走 SDK/httpx 的**异步** close：
        httpx 的 AsyncHTTPTransport 并没有同步 close()（早期实现里调
        `transport.close()` 会抛 AttributeError 并被静默吞掉，等于没中断），
        而 `await client.close()` 会立刻关闭连接池，让在途请求马上以
        ReadError/APIConnectionError 结束（本机实测 0.5s 内）。

        关闭后重建 client，避免后续请求复用已关闭的连接。
        """
        old = self.client
        try:
            await old.close()
        except Exception as e:
            logger.warning("关闭 AI 连接时出错（忽略）: %s", e)
        finally:
            self.client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout,
                max_retries=0,
            )

    async def analyze_batch(self, items: list[dict]) -> list[AnalysisResult]:
        """分析一批弹幕。

        items: [{"id": int, "content": str}, ...]
        返回与输入对应的 AnalysisResult 列表；AI 失败的批次返回空列表（由调用方决定跳过）。
        """
        if not items:
            return []

        # 紧凑序列化：去掉分隔符后的空格，减少输入 token
        user_content = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
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
        # extra_body 原样透传给接口（OpenRouter 的 reasoning、LM Studio 关闭 Qwen3 思考
        # 的 chat_template_kwargs 等都从这里传），能省下大量思考 token
        if self.config.extra_body:
            kwargs["extra_body"] = self.config.extra_body
        # reasoning_effort 是 OpenAI 风格的顶层参数：OpenRouter 只认 reasoning 对象、
        # 不认顶层的 reasoning_effort（会报错或浪费一次请求），因此当 extra_body 里
        # 已经给了 reasoning 时就不再发送它。
        has_reasoning_obj = bool(self.config.extra_body.get("reasoning"))
        if self.config.reasoning_effort.strip() and not has_reasoning_obj:
            kwargs["reasoning_effort"] = self.config.reasoning_effort.strip()
        # 日志里展示实际生效的推理设置，方便确认"到底有没有关掉思考"
        if has_reasoning_obj:
            reasoning_desc = json.dumps(self.config.extra_body["reasoning"], ensure_ascii=False)
        else:
            reasoning_desc = kwargs.get("reasoning_effort", "-")

        for attempt in range(1, 4):
            try:
                t0 = time.monotonic()
                cur_max = kwargs.get("max_tokens", self.config.max_tokens)
                logger.info(
                    "AI 请求开始 model=%s count=%d attempt=%d max_tokens=%d reasoning=%s",
                    self.config.model, len(items), attempt, cur_max, reasoning_desc,
                )
                resp = await self.client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                text = choice.message.content or ""
                elapsed = time.monotonic() - t0
                if not text.strip():
                    # 思考型模型可能因 max_tokens 不足在推理阶段被截断（finish=stop 但 content 为空）
                    # 临时加倍 max_tokens 重试，给模型更多输出空间
                    if attempt < 3:
                        new_max = min(cur_max * 2, 16000)
                        logger.warning(
                            "AI 返回空内容(finish=%s, %.1fs)，max_tokens=%d 不足，加倍到 %d 后重试",
                            choice.finish_reason, elapsed, cur_max, new_max,
                        )
                        kwargs["max_tokens"] = new_max
                        # OpenAI 风格接口下移除 reasoning_effort 进一步抑制思考；
                        # extra_body 保持原样（其中可能已经关掉/压缩了思考预算）
                        kwargs.pop("reasoning_effort", None)
                        continue
                    logger.warning(
                        "AI 返回空内容(finish=%s, %.1fs)，已重试 %d 次仍失败，该批 %d 条计入分析失败",
                        choice.finish_reason, elapsed, attempt - 1, len(items),
                    )
                    return []
                logger.info(
                    "AI 请求完成 %.1fs finish=%s content_len=%d",
                    elapsed, choice.finish_reason, len(text),
                )
                return self._parse(text, items)
            except asyncio.CancelledError:
                # 任务被取消（通常是停止信号触发）：不重试，向上抛出
                raise
            except Exception as e:
                elapsed = time.monotonic() - t0
                last_exc = e
                # 检查是否为连接层异常（aclose 会导致连接被关闭）
                # 若是，说明多半是停止信号触发的中断，不再重试（重试会再等一个 timeout）
                err_msg = str(e).lower()
                is_connection_error = (
                    isinstance(e, (APIConnectionError, httpx.TransportError, ConnectionError))
                    or "connection error" in err_msg
                    or "connection reset" in err_msg
                    or "connection closed" in err_msg
                )
                if is_connection_error:
                    logger.warning(
                        "AI 连接异常(第%d次, %.1fs): %s，可能是停止信号触发，不再重试",
                        attempt, elapsed, e,
                    )
                    return []
                # 参数兼容性兜底：部分后端不支持 reasoning_effort 或透传字段，
                # 首次失败时移除后重试一次，避免整批因"参数不支持"而全军覆没
                if attempt == 1 and ("reasoning_effort" in kwargs or "extra_body" in kwargs):
                    removed = [
                        k for k in ("reasoning_effort", "extra_body")
                        if kwargs.pop(k, None) is not None
                    ]
                    logger.warning(
                        "AI 批分析失败(第%d次, %.1fs): %s，移除 %s 后重试",
                        attempt, elapsed, e, "/".join(removed),
                    )
                    continue
                logger.warning("AI 批分析失败(第%d次, %.1fs): %s", attempt, elapsed, e)
                # 重试前短退避，避免本地模型过载时连续冲击
                if attempt < 3:
                    backoff = 2.0 * attempt
                    logger.info("AI 重试 %.1fs 后再次请求", backoff)
                    await asyncio.sleep(backoff)

        logger.error("AI 批分析最终失败，跳过该批 %d 条: %s", len(items), last_exc)
        return []

    def _parse(self, text: str, items: list[dict]) -> list[AnalysisResult]:
        obj = self._extract_json(text)
        if obj is None:
            logger.warning("AI 返回无法解析为 JSON，原始内容：\n%s", text)
            return []

        raw_items = obj.get("items") if isinstance(obj, dict) else None
        if not isinstance(raw_items, list):
            # 兼容直接返回数组的情况
            if isinstance(obj, list):
                raw_items = obj
            else:
                logger.warning("AI 返回缺少 items 字段，原始内容：\n%s", text)
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

        # 保证每条输入都有结果；AI 按提示词只返回违规项，未返回的视为不违规。
        # 注意：这里默认按"不违规"处理，所以模型漏答只会少举报，不会误举报。
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
