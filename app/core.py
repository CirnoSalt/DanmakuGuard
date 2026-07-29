"""核心：任务模型、事件总线、冷却、违禁词词典、任务管理器。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

import yaml

from app.ai import AIAnalyzer
from app.accounts import AccountManager
from app.paths import resource_path
from app.bilibili import (
    BiliAPIError,
    BiliClient,
    CookieExpiredError,
    DailyLimitError,
    RateLimitError,
    extract_bvid,
    fetch_all_danmaku,
    get_video_info,
    report_danmaku,
)
from app.config import Settings

logger = logging.getLogger("core")


# =====================================================================
# 任务与统计数据模型
# =====================================================================

class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class TaskStats:
    total: int = 0          # 弹幕总数（原始 dmid 数）
    analyzed: int = 0       # 已分析的 dmid 数（与 total 同口径）
    violating: int = 0      # 判定违规的 dmid 数
    reported: int = 0       # 已尝试举报的条数
    success: int = 0        # 举报成功
    failed: int = 0         # 举报失败
    skipped: int = 0        # 因停止/上限未处理的
    analysis_failed: int = 0  # AI 分析失败的 dmid 数


@dataclass
class Task:
    id: str
    url: str
    bvid: str = ""
    cid: int = 0
    title: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: TaskStats = field(default_factory=TaskStats)
    error: str = ""
    stop_requested: bool = False
    # 循环模式：处理完一轮后自动重置 total/analyzed 并重新拉取弹幕，适合长时间挂机监测
    loop_mode: bool = False
    # 已完成的循环轮次数（仅 loop_mode 下递增）
    loop_round: int = 0
    # 用于唤醒正在 await AI/B站接口的 worker，实现快速停止
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "bvid": self.bvid,
            "cid": self.cid,
            "title": self.title,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "stats": _stats_dict(self.stats),
            "error": self.error,
            "loop_mode": self.loop_mode,
            "loop_round": self.loop_round,
        }


def _stats_dict(s: TaskStats) -> dict:
    return {
        "total": s.total,
        "analyzed": s.analyzed,
        "violating": s.violating,
        "reported": s.reported,
        "success": s.success,
        "failed": s.failed,
        "skipped": s.skipped,
        "analysis_failed": s.analysis_failed,
    }


def stats_to_dict(s: TaskStats) -> dict:
    return _stats_dict(s)


# =====================================================================
# 内存事件总线，供 SSE 消费
# =====================================================================

@dataclass
class Event:
    task_id: str
    type: str            # log | stats | done
    level: str = "info"  # log 级别
    message: str = ""
    data: dict = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {
            "task_id": self.task_id,
            "type": self.type,
            "level": self.level,
            "message": self.message,
            "data": self.data,
        }
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class EventBus:
    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        # publish 与 subscribe 互斥，避免订阅瞬间事件已发但订阅者未注册的竞态
        async with self._lock:
            subs = list(self._subscribers)
            for q in subs:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    # 队列满：done 事件必须保留，否则丢老的可能丢掉关键终态
                    if event.type == "done":
                        # 持续淘汰最旧的，直到 done 能入队
                        while True:
                            try:
                                q.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            try:
                                q.put_nowait(event)
                                break
                            except asyncio.QueueFull:
                                continue
                    else:
                        # 非 done 事件：丢老的一条让位，仍满则放弃
                        try:
                            q.get_nowait()
                            q.put_nowait(event)
                        except asyncio.QueueFull:
                            pass

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # 便捷发布方法
    async def log(self, task_id: str, level: str, message: str) -> None:
        await self.publish(Event(task_id=task_id, type="log", level=level, message=message))

    async def stats(self, task_id: str, data: dict) -> None:
        await self.publish(Event(task_id=task_id, type="stats", data=data))

    async def done(self, task_id: str, data: dict) -> None:
        await self.publish(Event(task_id=task_id, type="done", data=data))


# 全局事件总线
bus = EventBus()


# =====================================================================
# 自适应冷却与限速管理
# =====================================================================

class CooldownManager:
    """维护举报间隔：正常按 base 间隔加随机抖动模拟人工操作；命中风控指数提升；连续成功后逐步回落。

    默认 base=7.5、jitter=2.5，正常冷却时长在 [5, 10] 秒之间随机，
    既避免节奏固定被识别为机器行为，又保证整体吞吐稳定。
    """

    def __init__(self, base_interval: float, jitter: float = 2.5):
        self.base = max(0.5, base_interval)
        self.jitter = max(0.0, jitter)
        self.current = self.base
        self._consecutive_success = 0
        self._max = 300.0  # 单次等待上限 5 分钟
        self._backoff_base = max(1.0, base_interval)  # 风控退避独立基准，避免 current 放大后指数爆炸

    def next_wait(self) -> float:
        """返回本次举报前的冷却等待时长：当前间隔 ± 抖动随机扰动。"""
        if self.jitter <= 0:
            return self.current
        # 围绕 current 做对称随机抖动，下限保护 0.1s
        wait = self.current + random.uniform(-self.jitter, self.jitter)
        return max(0.1, wait)

    def on_success(self) -> None:
        self._consecutive_success += 1
        # 连续成功若干次后，间隔减半回落
        if self._consecutive_success >= 5 and self.current > self.base:
            self.current = max(self.base, self.current / 2)
            self._consecutive_success = 0
            logger.info("连续成功，冷却间隔回落至 %.1fs", self.current)

    def on_rate_limit(self) -> None:
        self._consecutive_success = 0
        # 风控后间隔放大 3 倍（带上限）
        self.current = min(self.current * 3, self._max)
        logger.warning("触发风控，冷却间隔升至 %.1fs", self.current)

    def backoff_wait(self, attempt: int) -> float:
        """风控重试时的退避时长。基于独立基准值指数退避，避免与 current 叠加爆炸。"""
        return min(self._backoff_base * (2 ** attempt), self._max)

    def reset(self) -> None:
        """重置到初始状态。切换账号后调用，让新账号从基础间隔开始。"""
        self.current = self.base
        self._consecutive_success = 0


# =====================================================================
# 违禁词词典：AI 审查前的本地预过滤，命中直接举报，节约 AI 开销
# =====================================================================

@dataclass
class DictMatch:
    """词典命中结果。"""
    word: str        # 命中的词条
    reason: int      # 对应 B站举报理由代码


@dataclass
class DictEntry:
    """词典单条条目（预编译正则）。"""
    word: str                 # 原词条
    reason: int               # 举报理由代码
    mode: str                 # 匹配模式：contains/word/exact
    pattern: "re.Pattern"     # 预编译正则


class BannedDictionary:
    """加载违禁词词典并提供大小写不敏感的匹配。

    匹配模式（每条词条独立）：
      - contains（默认）：子串包含匹配。中文词条默认采用此模式。
      - word：词边界匹配，前后不能是字母或数字。纯 ASCII 字母数字词条默认采用此模式，
              避免短词误伤（如 OP 命中 OPPO、SB 命中 USB）。
      - exact：完全匹配，整个弹幕内容必须等于词条。

    词典格式（YAML，向后兼容）：
        类别名:
          reason: <1-12>
          words:
            - 词条1
            - 词条2
            # 或使用对象形式精细控制匹配模式
            - word: OP
              mode: word

    若未显式指定 mode，按词条字符类型自动选择：纯 ASCII 字母数字 → word；否则 → contains。
    """

    def __init__(self):
        # 展平后的列表：[DictEntry, ...]
        self._entries: list[DictEntry] = []
        self._loaded = False

    def load(self, path: str | Path) -> int:
        """加载词典文件，返回词条总数。文件不存在或为空则返回 0（禁用预过滤）。"""
        p = Path(path)
        # 相对路径在 CWD 找不到时，尝试从打包资源中加载（兼容 PyInstaller 环境）
        if not p.is_absolute() and not p.exists():
            res = resource_path(str(path))
            if res.exists():
                p = res
        if not p.exists():
            logger.info("违禁词词典不存在：%s，跳过预过滤", p)
            self._entries = []
            self._loaded = True
            return 0

        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            # 解析失败属于配置错误，明确抛出，避免静默禁用让用户不知情
            raise RuntimeError(f"违禁词词典解析失败：{p} - {e}") from e

        count = 0
        entries: list[DictEntry] = []
        for category, cfg in data.items():
            if not isinstance(cfg, dict):
                continue
            reason = int(cfg.get("reason", 11))
            words = cfg.get("words") or []
            for w in words:
                if isinstance(w, dict):
                    # 对象形式：{word: str, mode: str}
                    word_str = w.get("word")
                    mode = w.get("mode")
                elif isinstance(w, str):
                    word_str = w
                    mode = None
                else:
                    continue
                if not isinstance(word_str, str) or not word_str.strip():
                    continue
                entry = self._build_entry(word_str, reason, mode)
                if entry:
                    entries.append(entry)
                    count += 1

        self._entries = entries
        self._loaded = True
        logger.info("违禁词词典已加载：%d 条词条", count)
        return count

    @staticmethod
    def _default_mode(word: str) -> str:
        """按词条字符类型自动选择匹配模式。

        纯 ASCII 字母数字使用词边界匹配（避免 OP↔OPPO 类误伤）；
        含中文或其他字符的使用子串匹配。
        """
        # word.isascii() 要求所有字符都是 ASCII；word.isalnum() 要求字母或数字
        # 同时满足时才用 word 边界，避免中文/符号词条被误处理
        if word.isascii() and word.isalnum():
            return "word"
        return "contains"

    @staticmethod
    def _build_entry(word: str, reason: int, mode: str | None = None) -> "DictEntry | None":
        """构建词条条目，预编译正则。"""
        w = word.strip()
        if not w:
            return None
        m = (mode or BannedDictionary._default_mode(w)).strip().lower()
        if m not in {"contains", "word", "exact"}:
            m = BannedDictionary._default_mode(w)
        escaped = re.escape(w)
        if m == "word":
            # 前后不能是字母或数字（独立词）；大小写不敏感
            pattern = re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
        elif m == "exact":
            pattern = re.compile(rf"^{escaped}$", re.IGNORECASE)
        else:  # contains
            pattern = re.compile(escaped, re.IGNORECASE)
        return DictEntry(word=w, reason=reason, mode=m, pattern=pattern)

    @property
    def enabled(self) -> bool:
        return bool(self._entries)

    def match(self, content: str) -> DictMatch | None:
        """检查内容是否命中任一违禁词。命中返回 DictMatch，否则 None。"""
        if not self._entries:
            return None
        for entry in self._entries:
            if entry.pattern.search(content):
                return DictMatch(word=entry.word, reason=entry.reason)
        return None


# =====================================================================
# 任务管理器：单任务串行队列 + worker 编排
# =====================================================================

class TaskManager:
    # 任务列表上限，避免长期运行内存膨胀
    _MAX_TASKS = 100

    def __init__(
        self,
        settings: Settings,
        bili: BiliClient,
        analyzer: AIAnalyzer,
        dictionary: BannedDictionary | None = None,
        account_manager: "AccountManager | None" = None,
    ):
        self.settings = settings
        self.bili = bili
        self.analyzer = analyzer
        self.dictionary = dictionary or BannedDictionary()
        self.account_manager = account_manager
        self.tasks: dict[str, Task] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._current_task_id: str | None = None

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # ---- 对外接口 ----
    async def submit(self, url: str, loop_mode: bool = False) -> Task:
        task_id = uuid.uuid4().hex[:12]
        task = Task(id=task_id, url=url, loop_mode=loop_mode)
        self.tasks[task_id] = task
        self._cleanup_old_tasks()
        await self._queue.put(task_id)
        mode_hint = "（循环模式）" if loop_mode else ""
        await bus.log(task_id, "info", f"任务已加入队列{mode_hint}：{url}")
        logger.info("任务入队 id=%s url=%s loop=%s", task_id, url, loop_mode)
        return task

    def _cleanup_old_tasks(self) -> None:
        """任务列表超上限时，清理最旧的已完成任务，保留最近 _MAX_TASKS 条。"""
        if len(self.tasks) <= self._MAX_TASKS:
            return
        # 按 created_at 升序，优先清理已完成的
        sorted_tasks = sorted(self.tasks.values(), key=lambda t: t.created_at)
        terminal = {TaskStatus.DONE, TaskStatus.STOPPED, TaskStatus.FAILED}
        removed = 0
        target_remove = len(self.tasks) - self._MAX_TASKS
        for t in sorted_tasks:
            if removed >= target_remove:
                break
            if t.status in terminal:
                self.tasks.pop(t.id, None)
                removed += 1
        # 若清理终态后仍超限，强制清理最旧的（含进行中：理论上不应出现）
        if removed < target_remove:
            for t in sorted_tasks[removed:]:
                if removed >= target_remove:
                    break
                self.tasks.pop(t.id, None)
                removed += 1

    def request_stop(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        task.stop_requested = True
        task.stop_event.set()
        logger.info("请求停止任务 id=%s", task_id)
        return True

    def list_tasks(self) -> list[Task]:
        # 最新的在前
        return sorted(
            self.tasks.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )

    def session_stats(self) -> dict:
        agg = TaskStats()
        for t in self.tasks.values():
            s = t.stats
            agg.total += s.total
            agg.analyzed += s.analyzed
            agg.violating += s.violating
            agg.reported += s.reported
            agg.success += s.success
            agg.failed += s.failed
            agg.skipped += s.skipped
            agg.analysis_failed += s.analysis_failed
        return stats_to_dict(agg)

    # ---- worker 主循环 ----
    async def _run(self) -> None:
        logger.info("任务 worker 已启动")
        while True:
            task_id = await self._queue.get()
            try:
                await self._process(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # _process 内部已 try/except，逃逸到这里说明有未预期异常，兜底置 FAILED
                logger.exception("任务处理异常 id=%s: %s", task_id, e)
                task = self.tasks.get(task_id)
                if task and task.status not in {TaskStatus.DONE, TaskStatus.STOPPED, TaskStatus.FAILED}:
                    task.error = f"任务异常：{e}"
                    task.finished_at = datetime.now()
                    await bus.log(task_id, "error", task.error)
                    await bus.done(task_id, task.to_dict())

    async def _process(self, task_id: str) -> None:
        task = self.tasks[task_id]
        self._current_task_id = task_id
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now()
        await bus.stats(task_id, stats_to_dict(task.stats))

        try:
            # 首轮：解析 BV 号与视频信息（后续循环轮复用，无需重复请求）
            bvid = extract_bvid(task.url)
            task.bvid = bvid
            await bus.log(task_id, "info", f"解析 BV 号：{bvid}")

            info = await get_video_info(self.bili, bvid)
            task.cid = info["cid"]
            task.title = info["title"]
            await bus.log(
                task_id, "info",
                f"视频：《{info['title']}》 cid={info['cid']} 时长={info['duration']}s",
            )

            if task.stop_requested:
                await self._finish_stopped(task)
                return

            # 循环模式：每轮处理完毕后重置部分统计并重新拉取弹幕
            while True:
                round_no = task.loop_round + 1
                if task.loop_mode:
                    await bus.log(
                        task_id, "info",
                        f"===== 循环模式第 {round_no} 轮开始 =====",
                    )

                # 单轮处理：拉取弹幕 → AI 分析 → 举报
                should_continue = await self._process_one_round(task, info)

                if not task.loop_mode:
                    break

                if task.stop_requested:
                    break

                if not should_continue:
                    # 单轮异常退出（如无弹幕），等待一段时间再重试
                    wait = 60.0
                    await bus.log(
                        task_id, "info",
                        f"本轮无新弹幕或异常，{wait:.0f}s 后开始下一轮循环",
                    )
                    if not await self._interruptible_sleep(task, wait):
                        break
                    continue

                # 循环模式：重置本轮相关统计，开始下一轮
                task.loop_round = round_no
                task.stats.total = 0
                task.stats.analyzed = 0
                task.stats.analysis_failed = 0
                task.stats.skipped = 0
                await bus.log(
                    task_id, "info",
                    f"第 {round_no} 轮处理完毕，重置统计后开始下一轮",
                )
                await bus.stats(task_id, stats_to_dict(task.stats))

            # 退出循环：收尾
            if task.stop_requested:
                await self._finish_stopped(task)
            else:
                await self._finish_done(task)

        except CookieExpiredError as e:
            task.error = f"Cookie 失效：{e}"
            await bus.log(task_id, "error", task.error)
            await self._finish_failed(task)
        except DailyLimitError as e:
            task.error = f"当日举报上限：{e}"
            await bus.log(task_id, "error", task.error)
            await self._finish_failed(task)
        except BiliAPIError as e:
            task.error = f"B站接口错误：{e}"
            await bus.log(task_id, "error", task.error)
            await self._finish_failed(task)
        except Exception as e:
            task.error = f"任务异常：{e}"
            logger.exception("任务异常 id=%s", task_id)
            await bus.log(task_id, "error", task.error)
            await self._finish_failed(task)
        finally:
            self._current_task_id = None

    async def _process_one_round(self, task: Task, info: dict) -> bool:
        """单轮处理：拉取弹幕 → AI 分析 → 举报。

        返回 True 表示本轮正常完成（循环模式下应继续下一轮）；
        返回 False 表示本轮无弹幕或异常（循环模式下等待后重试）。
        非循环模式下调用方不依赖返回值。
        """
        task_id = task.id
        # 拉取弹幕（按视频时长估算分段数，提供进度反馈）
        estimated_segs = max(1, (info["duration"] + 359) // 360)
        await bus.log(
            task_id, "info",
            f"开始拉取弹幕…预计 {estimated_segs} 段（时长 {info['duration']}s）",
        )
        danmaku = await fetch_all_danmaku(
            self.bili, info["cid"], self.settings.bilibili.segment_cap
        )
        task.stats.total = len(danmaku)
        await bus.log(task_id, "info", f"共拉取弹幕 {len(danmaku)} 条")
        await bus.stats(task_id, stats_to_dict(task.stats))

        if not danmaku:
            await bus.log(task_id, "info", "无弹幕")
            return False

        if task.stop_requested:
            task.stats.skipped = len(danmaku)
            return False

        # AI 分析 + 逐批举报（分析一批，立即举报该批违规弹幕）
        await self._analyze(task, danmaku)
        return True

    # ---- AI 分析阶段 ----
    async def _analyze(self, task: Task, danmaku) -> None:
        """AI 逐批分析弹幕，每批分析完毕立即举报该批违规弹幕。

        统计口径统一为 dmid 数（与 total 同口径），避免去重模式下 analyzed/violating 与 total 口径不一致。
        """
        dedup = self.settings.report.dedup_by_content
        threshold = self.settings.ai.confidence_threshold
        default_reason = self.settings.report.default_reason
        max_reports = self.settings.report.max_reports

        cooldown = CooldownManager(self.settings.bilibili.request_interval)
        reported_dmids: set[int] = set()

        if max_reports > 0:
            await bus.log(
                task.id, "info",
                f"单任务举报上限已设为 {max_reports} 条（max_reports）",
            )

        # 构建送审条目；dedup 模式下 content_to_dmids 记录每条内容对应的全部 dmid
        content_to_dmids: dict[str, list[int]] | None = None
        if dedup:
            content_to_dmids = {}
            for d in danmaku:
                content_to_dmids.setdefault(d.content, []).append(d.id)
            audit_items = [
                {"id": idx, "content": c}
                for idx, c in enumerate(content_to_dmids.keys())
            ]
            await bus.log(
                task.id, "info",
                f"去重后送审 {len(audit_items)} 条（共 {len(danmaku)} 条 dmid）",
            )
        else:
            audit_items = [{"id": d.id, "content": d.content} for d in danmaku]
            await bus.log(task.id, "info", f"送审 {len(audit_items)} 条 dmid")

        # ---- 词典预过滤：命中的直接举报，不送 AI ----
        dict_violating_dmids = 0  # 命中并实际举报的 dmid 数
        dict_analyzed_dmids = 0   # 词典处理过的 dmid 数（与 total 同口径）
        remaining: list[dict] = []
        if self.dictionary.enabled:
            for item in audit_items:
                m = self.dictionary.match(item["content"])
                if m:
                    if content_to_dmids is not None:
                        content = item["content"]
                        dmids = content_to_dmids[content]
                        for dmid in dmids:
                            await self._report_one(task, dmid, m.reason, item["content"], cooldown, reported_dmids)
                            dict_violating_dmids += 1
                        dict_analyzed_dmids += len(dmids)
                    else:
                        await self._report_one(task, item["id"], m.reason, item["content"], cooldown, reported_dmids)
                        dict_violating_dmids += 1
                        dict_analyzed_dmids += 1
                else:
                    remaining.append(item)
            # 计算剩余送 AI 的 dmid 数，与 stats 口径一致
            remaining_dmids = 0
            for item in remaining:
                if content_to_dmids is not None:
                    remaining_dmids += len(content_to_dmids[item["content"]])
                else:
                    remaining_dmids += 1
            await bus.log(
                task.id, "info",
                f"词典预过滤命中 {dict_violating_dmids} 条 dmid，剩余 {remaining_dmids} 条 dmid 送 AI",
            )
            task.stats.analyzed += dict_analyzed_dmids
            task.stats.violating += dict_violating_dmids
            await bus.stats(task.id, stats_to_dict(task.stats))
            audit_items = remaining
        # ---- 预过滤结束 ----

        if not audit_items:
            await bus.log(task.id, "info", "无需送 AI 分析的弹幕")
            await self._log_report_summary(task)
            return

        batch_size = max(1, self.settings.ai.batch_size)
        total_audit = len(audit_items)
        # 预初始化，避免循环首次迭代即 break 时引用未定义变量
        start = 0

        for start in range(0, total_audit, batch_size):
            if task.stop_requested:
                break
            # 已达举报上限时停止送审，避免继续浪费 AI token
            if max_reports > 0 and task.stats.reported >= max_reports:
                await bus.log(task.id, "info", "已达举报上限，停止 AI 送审")
                break
            batch = audit_items[start:start + batch_size]
            batch_no = start // batch_size + 1
            # 统计本批对应的 dmid 数，让用户理解送审内容数与 dmid 数的差异
            batch_dmid_count = 0
            for item in batch:
                if content_to_dmids is not None:
                    batch_dmid_count += len(content_to_dmids[item["content"]])
                else:
                    batch_dmid_count += 1
            if content_to_dmids is not None:
                await bus.log(
                    task.id, "info",
                    f"AI 分析批次 {batch_no}（送审 {len(batch)} 条内容，对应 {batch_dmid_count} 条 dmid，进度 {task.stats.analyzed}/{task.stats.total}）",
                )
            else:
                await bus.log(
                    task.id, "info",
                    f"AI 分析批次 {batch_no}（送审 {len(batch)} 条，进度 {task.stats.analyzed}/{task.stats.total}）",
                )
            results = await self._analyze_batch_interruptible(task, batch)
            if results is None:
                # 被停止信号中断
                break

            # AI 返回空列表视为分析失败：本批 dmid 全部计入 analysis_failed/skipped
            if not results:
                failed_dmids = 0
                for item in batch:
                    if content_to_dmids is not None:
                        failed_dmids += len(content_to_dmids[item["content"]])
                    else:
                        failed_dmids += 1
                task.stats.analysis_failed += failed_dmids
                task.stats.skipped += failed_dmids
                await bus.log(
                    task.id, "warning",
                    f"批次 {batch_no} AI 分析失败，跳过 {failed_dmids} 条 dmid（{task.stats.analyzed}/{task.stats.total}）",
                )
                await bus.stats(task.id, stats_to_dict(task.stats))
                continue

            # 分析完一批，立即举报该批违规弹幕
            batch_violating = 0
            batch_analyzed_dmids = 0
            for r in results:
                # 越界保护：AI 偶尔会幻觉出不在 batch 中的 id
                if r.id < 0 or r.id >= len(audit_items):
                    logger.warning("AI 返回的 id=%s 越界（audit_items=%d），跳过", r.id, len(audit_items))
                    continue
                item = audit_items[r.id]
                content = item["content"]
                if content_to_dmids is not None:
                    dmids = content_to_dmids[content]
                    batch_analyzed_dmids += len(dmids)
                else:
                    dmids = [r.id]
                    batch_analyzed_dmids += 1

                if r.violates and r.confidence >= threshold:
                    # 排除 8(剧透) 和 10(视频无关)：AI 无法判断这两类
                    reason = r.reason if r.reason in {1, 2, 3, 4, 5, 6, 7, 9, 11, 12} else default_reason
                    for dmid in dmids:
                        await self._report_one(task, dmid, reason, content, cooldown, reported_dmids)
                        batch_violating += 1

            task.stats.analyzed += batch_analyzed_dmids
            task.stats.violating += batch_violating
            if batch_violating > 0:
                await bus.log(
                    task.id, "info",
                    f"批次 {batch_no} 完成：发现违规 {batch_violating} 条 dmid，已分析 {task.stats.analyzed}/{task.stats.total} 条 dmid",
                )
            else:
                await bus.log(
                    task.id, "info",
                    f"批次 {batch_no} 完成：已分析 {task.stats.analyzed}/{task.stats.total} 条 dmid",
                )
            await bus.stats(task.id, stats_to_dict(task.stats))

        # 停止/上限时未分析的部分计入跳过（按 dmid 口径）
        if task.stop_requested or (max_reports > 0 and task.stats.reported >= max_reports):
            # 计算尚未进入 AI 分析阶段的 dmid 数：start 为下一个待处理批次的起点，
            # 即已送审的条目数（中断批次未完成，不计入已送审）
            remaining_audit = max(0, total_audit - start) if audit_items else 0
            if remaining_audit > 0:
                skipped_dmids = 0
                for item in audit_items[-remaining_audit:]:
                    if content_to_dmids is not None:
                        skipped_dmids += len(content_to_dmids[item["content"]])
                    else:
                        skipped_dmids += 1
                task.stats.skipped += skipped_dmids

        await self._log_report_summary(task)

    async def _log_report_summary(self, task: Task) -> None:
        """输出分析+举报阶段的汇总日志与统计。"""
        await bus.log(
            task.id, "info",
            f"处理结束：已分析 {task.stats.analyzed} 条 dmid，违规 {task.stats.violating} 条，"
            f"举报成功 {task.stats.success}，失败 {task.stats.failed}，"
            f"AI 分析失败 {task.stats.analysis_failed} 条，跳过 {task.stats.skipped}",
        )
        await bus.stats(task.id, stats_to_dict(task.stats))

    async def _analyze_batch_interruptible(
        self, task: Task, batch: list[dict]
    ):
        """执行一批 AI 分析，可被 task.stop_event 中断。

        返回 AnalysisResult 列表；若被停止信号中断则返回 None。
        停止时通过 analyzer.aclose() 强制关闭底层 HTTP 连接，实现秒级中断
        （asyncio.Task.cancel 对 httpx 进行中请求不立即生效）。
        """
        ai_task = asyncio.create_task(self.analyzer.analyze_batch(batch))
        stop_task = asyncio.create_task(task.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {ai_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                # 停止信号先到：强制关闭 AI 底层 HTTP transport，秒级中断进行中的请求
                logger.info("停止信号到达，强制中断 AI 请求...")
                # aclose 现在是同步关闭 transport，不会阻塞
                try:
                    self.analyzer.aclose()
                except Exception:
                    pass
                ai_task.cancel()
                # 短暂等待 ai_task 收到连接异常后退出
                try:
                    await asyncio.wait_for(asyncio.shield(ai_task), timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                return None
            # AI 先完成：取消 stop 监听
            stop_task.cancel()
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
            return ai_task.result()
        except asyncio.CancelledError:
            ai_task.cancel()
            stop_task.cancel()
            raise

    async def _report_one(
        self,
        task: Task,
        dmid: int,
        reason: int,
        content: str,
        cooldown: CooldownManager,
        reported_dmids: set[int],
    ) -> None:
        if dmid in reported_dmids:
            return
        # 已达举报上限：跳过并计入 skipped
        max_reports = self.settings.report.max_reports
        if max_reports > 0 and task.stats.reported >= max_reports:
            task.stats.skipped += 1
            await bus.stats(task.id, stats_to_dict(task.stats))
            return
        wait = cooldown.next_wait()
        if not await self._interruptible_sleep(task, wait):
            task.stats.skipped += 1
            await bus.stats(task.id, stats_to_dict(task.stats))
            return

        # 普通业务错误的重试次数（不含风控/Cookie 问题）
        max_attempts = 3
        attempt = 0

        while True:
            if task.stop_requested:
                task.stats.skipped += 1
                await bus.stats(task.id, stats_to_dict(task.stats))
                return
            try:
                await report_danmaku(
                    self.bili, task.cid, dmid, reason, content=content
                )
                reported_dmids.add(dmid)
                task.stats.reported += 1
                task.stats.success += 1
                cooldown.on_success()
                if self.account_manager:
                    self.account_manager.on_success()
                await bus.log(
                    task.id, "info",
                    f"举报成功 dmid={dmid} reason={reason}",
                )
                # 举报成功后立即推送 stats，前端实时刷新
                await bus.stats(task.id, stats_to_dict(task.stats))
                return
            except RateLimitError as e:
                # 风控：触发账号切换（多账号轮换）；单账号场景下退避重试
                if self.account_manager:
                    await bus.log(
                        task.id, "warning",
                        f"账号 {self.bili.current_account_name} 触发风控({e})，切换账号中…",
                    )
                    # 切换账号内部会处理全风控等待循环
                    await self.account_manager.switch_on_rate_limit()
                    await bus.log(
                        task.id, "info",
                        f"已切换至账号 {self.bili.current_account_name}，继续举报 dmid={dmid}",
                    )
                    # 切换账号后重置 cooldown，新账号从基础间隔开始
                    cooldown.reset()
                    continue
                # 单账号场景：指数退避重试
                cooldown.on_rate_limit()
                wait = cooldown.backoff_wait(1)
                await bus.log(
                    task.id, "warning",
                    f"触发风控({e})，{wait:.0f}s 后重试 dmid={dmid}",
                )
                if not await self._interruptible_sleep(task, wait):
                    task.stats.skipped += 1
                    await bus.stats(task.id, stats_to_dict(task.stats))
                    return
                continue
            except (CookieExpiredError, DailyLimitError) as e:
                # Cookie 失效/上限：多账号场景切到下一个账号；单账号场景停止任务
                if self.account_manager:
                    err_type = "Cookie失效" if isinstance(e, CookieExpiredError) else "当日上限"
                    await bus.log(
                        task.id, "warning",
                        f"账号 {self.bili.current_account_name} {err_type}({e})，切换账号中…",
                    )
                    # 复用 switch_on_rate_limit 的轮换逻辑（标记当前账号冷却）
                    switched = await self.account_manager.switch_on_rate_limit()
                    if switched:
                        await bus.log(
                            task.id, "info",
                            f"已切换至账号 {self.bili.current_account_name}，继续举报 dmid={dmid}",
                        )
                        cooldown.reset()
                        continue
                # 单账号或无可用账号：停止任务，避免继续浪费 token
                task.stop_requested = True
                task.stop_event.set()
                raise
            except BiliAPIError as e:
                task.stats.reported += 1
                task.stats.failed += 1
                if self.account_manager:
                    self.account_manager.on_fail()
                await bus.log(
                    task.id, "warning",
                    f"举报失败 dmid={dmid}: {e}",
                )
                await bus.stats(task.id, stats_to_dict(task.stats))
                return
            except Exception as e:
                attempt += 1
                if attempt >= max_attempts:
                    task.stats.reported += 1
                    task.stats.failed += 1
                    if self.account_manager:
                        self.account_manager.on_fail()
                    logger.warning("举报异常 dmid=%s: %s", dmid, e)
                    await bus.log(
                        task.id, "error",
                        f"举报异常 dmid={dmid}: {e}（重试 {attempt} 次仍失败）",
                    )
                    await bus.stats(task.id, stats_to_dict(task.stats))
                    return
                logger.warning("举报异常 dmid=%s: %s，第 %d 次重试", dmid, e, attempt)
                continue

    async def _interruptible_sleep(self, task: Task, seconds: float) -> bool:
        """可被 stop_event 中断的 sleep。被中断返回 False，正常结束返回 True。"""
        try:
            await asyncio.wait_for(task.stop_event.wait(), timeout=seconds)
            # event 被 set，说明请求停止
            return False
        except asyncio.TimeoutError:
            return True

    # ---- 结束态 ----
    async def _finish_done(self, task: Task) -> None:
        task.status = TaskStatus.DONE
        task.finished_at = datetime.now()
        await bus.log(task.id, "info", "任务完成")
        await bus.done(task.id, task.to_dict())

    async def _finish_stopped(self, task: Task) -> None:
        task.status = TaskStatus.STOPPED
        task.finished_at = datetime.now()
        await bus.log(task.id, "info", "任务已停止")
        await bus.done(task.id, task.to_dict())

    async def _finish_failed(self, task: Task) -> None:
        task.status = TaskStatus.FAILED
        task.finished_at = datetime.now()
        await bus.done(task.id, task.to_dict())
