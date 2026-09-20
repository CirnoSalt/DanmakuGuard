"""账号管理器：多账号轮换、风控切换、全风控等待循环。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.bilibili import BiliClient
from app.config import BiliCookie, BilibiliConfig

logger = logging.getLogger("accounts")

# 登录态校验结果的复用有效期（秒）：避免内容级轮换每次切换都请求一次 nav 接口
VALIDATE_TTL = 600.0


class NoAccountAvailableError(RuntimeError):
    """所有账号均不可用（Cookie 失效 / 达上限 / 长期风控），无法继续提交举报。"""


@dataclass
class AccountState:
    """单个账号的运行时状态。"""
    cookie: BiliCookie
    # 风控/限流冷却到期时间戳（0 表示未受限）
    limited_until: float = 0.0
    # 进入冷却的原因（仅用于日志与前端展示）
    limited_reason: str = ""
    # 账号是否已判定失效（Cookie 失效 / 未登录 / 被封禁），本次运行内不再参与轮换
    invalid: bool = False
    invalid_reason: str = ""
    # 最近一次登录态校验通过的时间戳（0 表示从未校验）
    validated_at: float = 0.0
    # 累计举报成功次数
    success_count: int = 0
    # 累计举报失败次数
    fail_count: int = 0

    @property
    def is_limited(self) -> bool:
        return time.time() < self.limited_until

    @property
    def is_available(self) -> bool:
        """是否可参与轮换：未失效且不在冷却期内。"""
        return not self.invalid and not self.is_limited


class AccountManager:
    """管理多账号轮换。

    工作机制：
    - 启动时逐个校验全部账号的登录态，失效账号直接标记 invalid 并退出轮换
    - 触发风控时标记当前账号 limited_until，切换至下一个可用账号
    - 所有账号都风控时，进入等待循环，等最早解限的账号到期后恢复
    - Cookie 失效 / 当日上限的账号退出轮换；所有账号都不可用时抛出
      NoAccountAvailableError，由调用方终止任务，不再无限轮换

    关键点：**任何账号在被切换使用前都会做一次登录态校验**（结果带 TTL 缓存），
    因此失效 Cookie 不会因为「曾经只是风控冷却」而重新进入轮换。
    """

    def __init__(self, accounts: list[BiliCookie], bili: BiliClient, config: BilibiliConfig):
        if not accounts:
            raise RuntimeError(
                "账号列表为空，请在 accounts.yaml 中配置至少一个 B站账号"
            )
        self._states: list[AccountState] = [AccountState(cookie=c) for c in accounts]
        self._bili = bili
        self._config = config
        self._current_idx: int = 0
        self._lock = asyncio.Lock()
        # 由上层注入的「是否应中止」钩子（例如任务收到停止请求），
        # 用于打断「全账号冷却等待循环」，避免用户点了停止还要等几分钟
        self.should_abort: Callable[[], bool] | None = None

    # ---- 状态查询 ----

    @property
    def total(self) -> int:
        return len(self._states)

    @property
    def available_count(self) -> int:
        return sum(1 for s in self._states if s.is_available)

    @property
    def invalid_count(self) -> int:
        return sum(1 for s in self._states if s.invalid)

    @property
    def all_invalid(self) -> bool:
        """所有账号是否都已判定失效（Cookie 问题 / 达上限），此时已无任何救回可能。"""
        return all(s.invalid for s in self._states)

    @property
    def current(self) -> AccountState:
        return self._states[self._current_idx]

    def status(self) -> list[dict]:
        """返回所有账号状态，供前端展示。"""
        now = time.time()
        return [
            {
                "name": s.cookie.name,
                "is_current": i == self._current_idx,
                "is_limited": s.is_limited,
                "limited_remaining": max(0, int(s.limited_until - now)) if s.is_limited else 0,
                "limited_reason": s.limited_reason,
                "invalid": s.invalid,
                "invalid_reason": s.invalid_reason,
                "available": s.is_available,
                "success": s.success_count,
                "failed": s.fail_count,
            }
            for i, s in enumerate(self._states)
        ]

    # ---- 初始化 ----

    async def init_first_account(self) -> bool:
        """启动时校验全部账号登录态，标记失效账号并启用首个可用账号。

        返回是否找到至少一个可用账号。
        """
        first_ok: int | None = None
        for i, state in enumerate(self._states):
            self._apply(i)
            ok = await self._bili.fetch_login_state()
            if ok is True:
                state.validated_at = time.time()
                logger.info("账号登录态校验通过：%s", state.cookie.name)
                if first_ok is None:
                    first_ok = i
            elif ok is False:
                self._disable(i, "登录态校验失败（Cookie 已失效或该账号未登录）")
            else:
                logger.warning(
                    "账号 %s 登录态无法确认（接口请求失败），暂不标记失效", state.cookie.name
                )

        if first_ok is None:
            logger.error("全部 %d 个账号登录态校验均未通过", self.total)
            return False

        self._apply(first_ok)
        logger.info(
            "账号初始化成功：%s（共 %d 个账号，可用 %d 个，失效 %d 个）",
            self._states[first_ok].cookie.name, self.total,
            self.available_count, self.invalid_count,
        )
        return True

    # ---- 对外轮换接口 ----

    async def health_check(self) -> bool:
        """巡检当前账号登录态（带 TTL 缓存），失效则标记并切换到其它可用账号。

        返回是否存在可用账号。用于长循环任务中及时发现 Cookie 过期。
        """
        async with self._lock:
            state = self._states[self._current_idx]
            if state.invalid:
                return await self._ensure_locked()
            if self._is_fresh(state):
                return True
            ok = await self._bili.fetch_login_state()
            if ok is True:
                state.validated_at = time.time()
                return True
            if ok is False:
                self._disable(self._current_idx, "登录态巡检失败（Cookie 已失效）")
                return await self._ensure_locked()
            # 无法判定：不标记失效，保持现状
            logger.warning("账号 %s 登录态无法确认（接口请求失败），继续使用", state.cookie.name)
            return True

    async def switch_on_rate_limit(self, reason: str = "触发风控") -> bool:
        """当前账号触发风控：标记冷却并切换至下一个可用账号。

        返回是否成功切换到可用账号：
        - True  ：已切换到（或等待到）另一个可用账号
        - False ：单账号场景（交由调用方做指数退避）或所有账号均已失效
        """
        if self.total <= 1:
            # 单账号没有可轮换对象，交给调用方用指数退避熬过风控
            return False
        async with self._lock:
            now = time.time()
            current = self._states[self._current_idx]
            current.limited_until = now + max(1.0, self._config.all_limited_wait)
            current.limited_reason = reason
            logger.warning(
                "账号 %s %s，冷却至 %s",
                current.cookie.name,
                reason,
                time.strftime("%H:%M:%S", time.localtime(current.limited_until)),
            )
            return await self._ensure_locked()

    async def ensure_available(self) -> bool:
        """确保当前客户端指向一个可用账号；不可用时切换到其它账号或等待解限。

        返回是否存在可用账号（False 表示所有账号均已失效）。
        """
        async with self._lock:
            return await self._ensure_locked()

    async def switch_for_content(self, k: int) -> bool:
        """对「内容重复的弹幕」按内容已举报次数做账号轮换。

        单个账号对同一弹幕内容通常只能产生一次有效举报，借助多个账号即可让
        重复内容逐个生效。从 (k % 账号数) 起向后寻找第一个「可用且非当前使用」的
        账号切换；找不到可切换账号时保持当前账号尽力提交。
        返回是否真正切换成功。
        """
        n = len(self._states)
        if n <= 1:
            return False
        async with self._lock:
            origin = self._current_idx
            start = k % n
            for offset in range(n):
                idx = (start + offset) % n
                if idx == origin:
                    # 尽量避免继续用当前账号提交同一个重复内容
                    continue
                if not self._states[idx].is_available:
                    continue
                if await self._activate(idx):
                    logger.info(
                        "内容级账号轮换：切换至账号 %s 提交该条重复弹幕",
                        self._states[idx].cookie.name,
                    )
                    return True
            # 其余账号均不可切换（失效/冷却中）：恢复原账号，保持尽力提交
            if self._current_idx != origin:
                self._apply(origin)
            return False

    def on_success(self) -> None:
        """举报成功时调用，更新当前账号统计。"""
        self._states[self._current_idx].success_count += 1

    def on_fail(self) -> None:
        """举报失败时调用（非风控类失败）。"""
        self._states[self._current_idx].fail_count += 1

    # ---- 内部实现 ----

    def _apply(self, idx: int) -> None:
        """把客户端 Cookie 切到指定账号（不做登录态校验）。"""
        self._current_idx = idx
        state = self._states[idx]
        self._bili.switch_cookie(
            state.cookie.sessdata,
            state.cookie.bili_jct,
            state.cookie.name,
        )

    def _is_fresh(self, state: AccountState) -> bool:
        """该账号是否在 TTL 内已校验过登录态。"""
        return bool(state.validated_at) and (time.time() - state.validated_at) < VALIDATE_TTL

    def _disable(self, idx: int, reason: str) -> None:
        """把账号标记为失效，永久退出本次运行的轮换。"""
        state = self._states[idx]
        if state.invalid:
            return
        state.invalid = True
        state.invalid_reason = reason
        logger.error("账号 %s 已退出轮换：%s", state.cookie.name, reason)

    def mark_current_invalid(self, reason: str) -> None:
        """把当前账号标记为失效（Cookie 失效 / 未登录 / 被封禁）。"""
        self._disable(self._current_idx, reason)

    def mark_current_daily_limited(self, reason: str) -> None:
        """把当前账号标记为「当日上限」，冷却至次日 00:05 后自动恢复。"""
        state = self._states[self._current_idx]
        resume = _next_midnight_ts()
        if resume > state.limited_until:
            state.limited_until = resume
        state.limited_reason = reason
        logger.warning(
            "账号 %s %s，冷却至次日 %s",
            state.cookie.name, reason,
            time.strftime("%H:%M:%S", time.localtime(state.limited_until)),
        )

    async def _activate(self, idx: int) -> bool:
        """切换到指定账号并确保其登录态有效；失效则标记并返回 False。"""
        state = self._states[idx]
        self._apply(idx)
        if self._is_fresh(state):
            return True
        ok = await self._bili.fetch_login_state()
        if ok is True:
            state.validated_at = time.time()
            return True
        if ok is False:
            self._disable(idx, "登录态校验失败（Cookie 已失效或该账号未登录）")
            return False
        # 无法判定（网络异常）：不标记失效，放行本次使用，下次切换会重新校验
        logger.warning(
            "账号 %s 登录态无法确认（接口请求失败），暂按可用处理", state.cookie.name
        )
        return True

    def _find_next_available(self) -> int | None:
        """寻找下一个可用账号索引（从当前索引之后开始轮询一圈）。"""
        n = len(self._states)
        for offset in range(1, n + 1):
            idx = (self._current_idx + offset) % n
            if self._states[idx].is_available:
                return idx
        return None

    async def _ensure_locked(self) -> bool:
        """（需持有锁）确保当前账号可用，否则挑选其它账号激活。

        优先未受限的账号；仅剩冷却中的账号时等待其解限；全部失效则返回 False。
        """
        if self._states[self._current_idx].is_available:
            return True
        idx = self._find_next_available()
        while idx is not None:
            if await self._activate(idx):
                logger.info("已切换至可用账号：%s", self._states[idx].cookie.name)
                return True
            # 该校验失败已被标记失效，继续找下一个
            idx = self._find_next_available()

        if any(not s.invalid for s in self._states):
            # 还有未失效但正处于冷却期的账号：等待其解限
            return await self._wait_for_any_available()

        logger.error(
            "所有 %d 个账号均不可用（Cookie 失效 / 达上限），无法继续提交", self.total
        )
        return False

    async def _wait_for_any_available(self) -> bool:
        """所有可用账号都在冷却期时，按配置间隔轮询等待任一账号恢复。

        等待期间以 1 秒为步长检查中止信号（用户点停止时能立刻退出，而不是等满间隔）。
        """
        interval = max(1.0, self._config.all_limited_wait)
        logger.warning(
            "所有可用账号均在冷却中（共 %d 个账号，其中失效 %d 个），以 %.0fs 间隔轮询等待",
            self.total, self.invalid_count, interval,
        )
        while True:
            waited = 0.0
            while waited < interval:
                if self.should_abort is not None and self.should_abort():
                    logger.warning("等待账号解限期间收到停止请求，中止等待")
                    return False
                step = min(0.5, interval - waited)
                await asyncio.sleep(step)
                waited += step
            for i, state in enumerate(self._states):
                if state.is_available and await self._activate(i):
                    logger.info("等待结束，恢复使用账号：%s", state.cookie.name)
                    return True
            if self.all_invalid:
                logger.error("等待期间账号全部失效，无法恢复")
                return False


def _next_midnight_ts() -> float:
    """当日上限的恢复时间点：次日 00:05（B站每日限额按自然日重置）。"""
    now = time.localtime()
    today_reset = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, 0, 5, 0, 0, 0, -1)
    )
    return today_reset + 86400
