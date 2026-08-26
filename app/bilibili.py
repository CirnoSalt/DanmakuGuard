"""B站 API 客户端：HTTP 重试、视频信息、弹幕拉取、protobuf 解析、举报提交。"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from app.config import BilibiliConfig

logger = logging.getLogger("bilibili")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# =====================================================================
# HTTP 客户端与异常
# =====================================================================

class BiliAPIError(Exception):
    """B站业务错误。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class RateLimitError(BiliAPIError):
    """风控 / 请求过频，需要冷却后重试。"""


class CookieExpiredError(BiliAPIError):
    """Cookie 失效 / 未登录。"""


class DailyLimitError(BiliAPIError):
    """当日操作上限。"""


class BiliClient:
    def __init__(self, config: BilibiliConfig):
        self.config = config
        # 当前账号信息（可热切换）；初始为空，由 AccountManager 注入
        self.current_sessdata: str = ""
        self.current_jct: str = ""
        self.current_account_name: str = ""
        self._http = httpx.AsyncClient(
            base_url="https://api.bilibili.com",
            headers={
                "User-Agent": UA,
                "Cookie": "",  # 初始为空，由 switch_cookie 动态更新
                "Referer": "https://www.bilibili.com/",
                "Accept": "*/*",
            },
            timeout=httpx.Timeout(15.0, connect=10.0),
            # 显式设置连接池上限，避免并发场景下连接泄漏
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30.0,
            ),
        )

    def switch_cookie(self, sessdata: str, bili_jct: str, name: str = "") -> None:
        """热切换当前账号 Cookie。更新请求头与 CSRF token。"""
        self.current_sessdata = sessdata
        self.current_jct = bili_jct
        self.current_account_name = name
        cookie = f"SESSDATA={sessdata}; bili_jct={bili_jct}"
        self._http.headers["Cookie"] = cookie
        logger.info("已切换至账号：%s", name or "未命名")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request_with_retry(self, method: str, url: str, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = await self._http.request(method, url, **kwargs)
                if resp.status_code == 412 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                last_exc = e
                wait = min(2 ** attempt, 16)
                logger.warning(
                    "请求 %s 失败(第%d次): %s，%.1fs 后重试", url, attempt, e, wait
                )
                await asyncio.sleep(wait)
        raise BiliAPIError(-1, f"请求多次失败: {last_exc}")

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        resp = await self._request_with_retry("GET", path, params=params)
        return resp.json()

    async def post_form(self, path: str, data: dict) -> dict:
        resp = await self._request_with_retry("POST", path, data=data)
        return resp.json()

    async def get_bytes(self, path: str, params: dict | None = None) -> bytes:
        resp = await self._request_with_retry("GET", path, params=params)
        return resp.content

    async def check_login(self) -> bool:
        """通过 nav 接口校验 Cookie 登录态。"""
        try:
            data = await self.get_json("/x/web-interface/nav")
            return bool(data.get("data", {}).get("isLogin"))
        except Exception as e:
            logger.error("Cookie 校验失败: %s", e)
            return False


# =====================================================================
# 轻量 protobuf 解析（DmSegMobileReply / DanmakuElem）
# 仅按 wire format 提取所需字段，避免引入 protoc 编译依赖。
# DanmakuElem 字段：
#   1 id(int64)  2 progress(int32)  3 mode(int32)  5 color(uint32)
#   6 midHash(string)  7 content(string)  9 weight(int32)  11 pool(int32)
# =====================================================================

@dataclass
class DanmakuElem:
    id: int = 0
    progress: int = 0
    mode: int = 0
    color: int = 0
    mid_hash: str = ""
    content: str = ""
    weight: int = 0
    pool: int = 0


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")
    return result, pos


def _iter_fields(data: bytes):
    pos = 0
    n = len(data)
    while pos < n:
        key, pos = _read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 0x7
        if wire_type == 0:  # varint
            value, pos = _read_varint(data, pos)
            yield field_number, 0, value
        elif wire_type == 2:  # length-delimited
            length, pos = _read_varint(data, pos)
            value = data[pos:pos + length]
            pos += length
            yield field_number, 2, value
        elif wire_type == 5:  # 32-bit
            pos += 4
        elif wire_type == 1:  # 64-bit
            pos += 8
        else:
            # 未知 wire_type，跳过剩余（罕见）
            break


def _parse_elem(data: bytes) -> DanmakuElem:
    elem = DanmakuElem()
    for field_number, wire_type, value in _iter_fields(data):
        if wire_type == 0:
            if field_number == 1:
                elem.id = value
            elif field_number == 2:
                elem.progress = value
            elif field_number == 3:
                elem.mode = value
            elif field_number == 5:
                elem.color = value
            elif field_number == 9:
                elem.weight = value
            elif field_number == 11:
                elem.pool = value
        elif wire_type == 2:
            if field_number == 6:
                elem.mid_hash = value.decode("utf-8", "replace")
            elif field_number == 7:
                elem.content = value.decode("utf-8", "replace")
    return elem


def parse_dm_seg(data: bytes) -> list[DanmakuElem]:
    """解析 DmSegMobileReply 二进制，返回弹幕列表。"""
    if not data:
        return []
    elems: list[DanmakuElem] = []
    for field_number, wire_type, value in _iter_fields(data):
        if field_number == 1 and wire_type == 2:
            elems.append(_parse_elem(value))
    return elems


# =====================================================================
# 视频信息：BV 解析、获取 cid/title/duration
# =====================================================================

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
# av 号：数字前需为「非字母数字」边界，避免误伤普通文本中的 av 字样
_AV_RE = re.compile(r"(?<![A-Za-z0-9])av(\d+)", re.IGNORECASE)

# ---- av/bv 互转（B站现行 base58 算法，av 上限 2^51，来自官方 JS 实现）----
_B58_TABLE = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_XOR_CODE = 23442827791579
_MASK_CODE = (1 << 51) - 1
_MAX_AID = 1 << 51


def _bv2av(bvid: str) -> int:
    arr = list(bvid)
    arr[3], arr[9] = arr[9], arr[3]
    arr[4], arr[7] = arr[7], arr[4]
    tmp = 0
    for c in arr[3:]:
        tmp = tmp * 58 + _B58_TABLE.index(c)
    return (tmp & _MASK_CODE) ^ _XOR_CODE


def _av2bv(avid: int) -> str:
    arr = ['B', 'V', '1', '0', '0', '0', '0', '0', '0', '0', '0', '0']
    bv_idx = len(arr) - 1
    tmp = (_MAX_AID | int(avid)) ^ _XOR_CODE
    while tmp > 0:
        arr[bv_idx] = _B58_TABLE[tmp % 58]
        tmp //= 58
        bv_idx -= 1
    arr[3], arr[9] = arr[9], arr[3]
    arr[4], arr[7] = arr[7], arr[4]
    return "".join(arr)


def extract_bvid(url_or_bv: str) -> str:
    """从链接或编号中解析视频标识，统一返回 BV 号。

    同时支持 BV 号与 av 号输入（如 .../video/BV1xx... 或 .../video/av117149813182185/），
    av 号会自动转换为对应 BV 号。
    """
    s = url_or_bv.strip()
    m = _BV_RE.search(s)
    if m:
        return m.group(1)
    m = _AV_RE.search(s)
    if m:
        return _av2bv(int(m.group(1)))
    raise ValueError(f"无法从输入中解析 BV 号或 av 号: {url_or_bv}")


async def get_video_info(client: BiliClient, bvid: str) -> dict:
    """返回 {aid, bvid, cid, title, duration}（多P取第一分P）。"""
    data = await client.get_json("/x/web-interface/view", params={"bvid": bvid})
    if data.get("code") != 0:
        raise BiliAPIError(data.get("code", -1), data.get("message", "未知错误"))
    d = data["data"]
    pages = d.get("pages") or []
    cid = pages[0]["cid"] if pages else d.get("cid")
    info = {
        "aid": d["aid"],
        "bvid": d["bvid"],
        "cid": cid,
        "title": d["title"],
        "duration": d.get("duration", 0),
    }
    logger.info("视频信息: %s (cid=%s, 时长=%ss)", info["title"], info["cid"], info["duration"])
    return info


# =====================================================================
# 弹幕分段拉取
# =====================================================================

async def fetch_all_danmaku(
    client: BiliClient, cid: int, segment_cap: int = 30
) -> list[DanmakuElem]:
    """逐段拉取弹幕，遇空停止，超 segment_cap 段熔断。"""
    all_elems: list[DanmakuElem] = []
    for seg in range(1, segment_cap + 1):
        try:
            content = await client.get_bytes(
                "/x/v2/dm/web/seg.so",
                params={"type": 1, "oid": cid, "segment_index": seg},
            )
        except Exception as e:
            logger.warning("拉取弹幕分段 %d 失败: %s，停止拉取", seg, e)
            break

        if not content:
            logger.info("弹幕分段 %d 为空，停止拉取", seg)
            break

        elems = parse_dm_seg(content)
        if not elems:
            logger.info("弹幕分段 %d 无数据，停止拉取", seg)
            break

        all_elems.extend(elems)
        logger.info(
            "弹幕分段 %d: 获取 %d 条，累计 %d 条", seg, len(elems), len(all_elems)
        )

    logger.info("视频 cid=%s 共拉取弹幕 %d 条", cid, len(all_elems))
    return all_elems


# =====================================================================
# 弹幕举报提交
# =====================================================================

# 风控 / 频繁 相关 code
_RATE_LIMIT_CODES = {-799, 509, -509}
# 未登录 / 账号封停
_COOKIE_EXPIRED_CODES = {-101, -102}
# 当日操作上限
_DAILY_LIMIT_CODES = {36715}


async def report_danmaku(
    client: BiliClient,
    cid: int,
    dmid: int,
    reason: int,
    content: str = "",
) -> dict:
    """举报单条弹幕。成功返回原始响应 dict，失败抛出对应异常。"""
    form: dict = {
        "cid": cid,
        "dmid": dmid,
        "reason": reason,
        "csrf": client.current_jct,
    }
    if content:
        form["content"] = content

    data = await client.post_form("/x/dm/report/add", data=form)
    code = data.get("code", -1)
    msg = data.get("message", "")

    if code == 0:
        return data

    if code in _COOKIE_EXPIRED_CODES:
        raise CookieExpiredError(code, msg)
    if code in _DAILY_LIMIT_CODES or "上限" in msg:
        raise DailyLimitError(code, msg)
    if code in _RATE_LIMIT_CODES or "频繁" in msg or "风控" in msg:
        raise RateLimitError(code, msg)
    # 其他业务错误（如已举报、参数错误等）按普通错误抛出
    raise BiliAPIError(code, msg)
