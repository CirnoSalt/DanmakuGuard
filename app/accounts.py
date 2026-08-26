"""账号管理器：多账号轮换、风控切换、全风控等待循环。"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.bilibili import BiliClient
from app.config import BiliCookie, BilibiliConfig

logger = logging.getLogger("accounts")


@dataclass
class AccountState:
    """单个账号的运行时状态。"""
    cookie: BiliCookie
    # 风控冷却到期时间戳（0 表示未风控）
    limited_until: float = 0.0
    # 累计举报成功次数
    success_count: int = 0
    # 累计举报失败次数
    fail_count: int = 0

    @property
    def is_limited(self) -> bool:
        return time.time() < self.limited_until


class AccountManager:
    """管理多账号轮换。

    工作机制：
    - 启动时选择第一个可用账号
    - 触发风控时标记当前账号 limited_until，切换至下一个可用账号
    - 所有账号都风控时，进入等待循环，等最早解限的账号到期后恢复
    - Cookie 失效/上限的账号跳过，不算可用
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

    @property
    def total(self) -> int:
        return len(self._states)

    @property
    def available_count(self) -> int:
        return sum(1 for s in self._states if not s.is_limited)

    @property
    def current(self) -> AccountState:
        return self._states[self._current_idx]

    async def init_first_account(self) -> bool:
        """初始化首个可用账号到 BiliClient。返回是否成功。"""
        for i, state in enumerate(self._states):
            self._current_idx = i
            self._bili.switch_cookie(
                state.cookie.sessdata,
                state.cookie.bili_jct,
                state.cookie.name,
            )
            ok = await self._bili.check_login()
            if ok:
                logger.info(
                    "账号初始化成功：%s（共 %d 个账号）",
                    state.cookie.name, self.total
                )
                return True
            else:
                logger.warning("账号登录态校验失败：%s，跳过", state.cookie.name)
        return False

    async def switch_on_rate_limit(self) -> bool:
        """当前账号触发风控：标记并切换至下一个可用账号。

        返回是否成功切换到可用账号。若所有账号都风控则进入等待循环，
        等到最早解限的账号恢复后切换；返回 True 表示等待后已恢复。
        """
        async with self._lock:
            # 标记当前账号风控，冷却时长使用配置 all_limited_wait
            now = time.time()
            current = self._states[self._current_idx]
            current.limited_until = now + max(1.0, self._config.all_limited_wait)
            logger.warning(
                "账号 %s 触发风控，标记冷却至 %s",
                current.cookie.name,
                time.strftime("%H:%M:%S", time.localtime(current.limited_until)),
            )

            # 寻找下一个可用账号
            next_idx = self._find_next_available()
            if next_idx is not None:
                self._current_idx = next_idx
                state = self._states[next_idx]
                self._bili.switch_cookie(
                    state.cookie.sessdata,
                    state.cookie.bili_jct,
                    state.cookie.name,
                )
                logger.info("已切换至可用账号：%s", state.cookie.name)
                return True

            # 所有账号都风控，进入等待循环
            await self._wait_for_any_available()
            return True

    def _find_next_available(self) -> int | None:
        """寻找下一个未风控的账号索引（从当前索引之后开始轮询一圈）。"""
        n = len(self._states)
        for offset in range(1, n + 1):
            idx = (self._current_idx + offset) % n
            if not self._states[idx].is_limited:
                return idx
        return None

    async def _wait_for_any_available(self) -> None:
        """所有账号风控时，按配置间隔轮询等待任一账号解限后恢复。"""
        interval = max(1.0, self._config.all_limited_wait)
        logger.warning(
            "所有 %d 个账号均已风控，以 %.0fs 间隔轮询等待可用账号",
            self.total, interval,
        )
        while True:
            await asyncio.sleep(interval)
            # 等待结束后重新检查：可能有账号已自然解限
            for i, state in enumerate(self._states):
                if not state.is_limited:
                    self._current_idx = i
                    self._bili.switch_cookie(
                        state.cookie.sessdata,
                        state.cookie.bili_jct,
                        state.cookie.name,
                    )
                    logger.info("等待结束，恢复使用账号：%s", state.cookie.name)
                    return

    def on_success(self) -> None:
        """举报成功时调用，更新当前账号统计。"""
        self._states[self._current_idx].success_count += 1

    def on_fail(self) -> None:
        """举报失败时调用（非风控类失败）。"""
        self._states[self._current_idx].fail_count += 1

    def switch_for_content(self, k: int) -> bool:
        """对「内容重复的弹幕」按内容已举报次数做账号轮换。

        单个账号对同一弹幕内容通常只能产生一次有效举报，借助多个账号即可让
        重复内容逐个生效。从 (k % 账号数) 起向后寻找第一个「未风控且非当前使用」
        的账号切换；找不到可切换账号时保持当前账号尽力提交。
        返回是否真正切换成功。
        """
        n = len(self._states)
        start = k % n
        for offset in range(n):
            idx = (start + offset) % n
            if self._states[idx].is_limited:
                continue
            if idx == self._current_idx:
                # 尽量避免继续用当前账号提交同一个重复内容
                continue
            self._current_idx = idx
            state = self._states[idx]
            self._bili.switch_cookie(
                state.cookie.sessdata,
                state.cookie.bili_jct,
                state.cookie.name,
            )
            logger.info("内容级账号轮换：切换至账号 %s 提交该条重复弹幕", state.cookie.name)
            return True
        # 其余账号均已风控：保持当前账号尽力提交
        return False

    def status(self) -> list[dict]:
        """返回所有账号状态，供前端展示。"""
        now = time.time()
        return [
            {
                "name": s.cookie.name,
                "is_current": i == self._current_idx,
                "is_limited": s.is_limited,
                "limited_remaining": max(0, int(s.limited_until - now)) if s.is_limited else 0,
                "success": s.success_count,
                "failed": s.fail_count,
            }
            for i, s in enumerate(self._states)
        ]
