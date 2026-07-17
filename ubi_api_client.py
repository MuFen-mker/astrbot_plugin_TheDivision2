import aiohttp
import asyncio
import base64
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

logger = logging.getLogger("astrbot_plugin_TheDivision2")

class UbisoftAPIError(Exception):
    """育碧 API 调用异常基类"""
    pass

class UbisoftAPIAuthError(UbisoftAPIError):
    """认证失败（401）"""
    pass

class UbisoftAPIRateLimitError(UbisoftAPIError):
    """请求限流（429）"""
    pass

class UbisoftAPIServerError(UbisoftAPIError):
    """服务器错误（5xx）"""
    pass

class UbisoftAPI:
    """异步育碧 API 客户端（带健壮性增强）"""
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.ticket: Optional[str] = None
        self.ticket_expiry: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()  # 防止并发刷新 ticket

        # ---------- 登录请求头 ----------
        self.login_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "ubi-appid": "82b650c0-6cb3-40c0-9f41-25a53b62b206",
            "sec-ch-ua-platform": '"Windows"',
            "ubi-requestedplatformtype": "uplay",
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
            "sec-ch-ua-mobile": "?0",
            "ubi-origin-appid": "412802ed-8163-4642-a931-8299f209fecb",
            "origin": "https://connect.ubisoft.com",
            "referer": "https://connect.ubisoft.com/",
            "accept-language": "zh-CN,zh;q=0.9",
        }
        # ---------- 查询请求头 ----------
        self.query_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        }

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    # ---------- Ticket 管理 ----------
    async def _ensure_ticket(self):
        """保证 ticket 有效，若剩余时间少于 5 分钟则提前刷新"""
        # 如果 ticket 还有效且剩余时间 > 300 秒，直接返回
        if self.ticket and time.time() < self.ticket_expiry - 300:
            return

        # 加锁防止并发刷新
        async with self._lock:
            # 双重检查
            if self.ticket and time.time() < self.ticket_expiry - 300:
                return

            logger.info("Ubisoft ticket expired or will expire soon, refreshing...")
            auth_str = f"{self.email}:{self.password}"
            auth_basic = base64.b64encode(auth_str.encode()).decode()
            headers = self.login_headers.copy()
            headers["authorization"] = f"Basic {auth_basic}"
            url = "https://public-ubiservices.ubi.com/v3/profiles/sessions"
            payload = {"rememberMe": True}

            try:
                if not self._session:
                    self._session = aiohttp.ClientSession()
                async with self._session.post(url, headers=headers, json=payload, timeout=30) as resp:
                    if resp.status == 429:
                        raise UbisoftAPIRateLimitError("获取 ticket 被限流，请稍后重试")
                    resp.raise_for_status()
                    data = await resp.json()
                    self.ticket = data["ticket"]
                    exp_str = data["expiration"]
                    expiry_dt = datetime.fromisoformat(exp_str.replace('Z', '+00:00'))
                    self.ticket_expiry = expiry_dt.timestamp()
                    logger.info(f"New ticket obtained, expires at {exp_str}")
            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    raise UbisoftAPIAuthError("育碧账号认证失败，请检查邮箱和密码")
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Failed to obtain Ubisoft ticket: {e}")
                raise UbisoftAPIError(f"网络请求失败: {e}")

    # ---------- 带重试的核心请求 ----------
    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict = None,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        **kwargs
    ) -> dict:
        """
        发送 HTTP 请求，自动重试（指数退避）
        支持 429（限流）、5xx（服务器错误）重试，401 触发 ticket 刷新
        """
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                # 每次重试前确保 session 和 ticket
                if not self._session:
                    self._session = aiohttp.ClientSession()
                await self._ensure_ticket()

                req_headers = self.query_headers.copy()
                req_headers["Authorization"] = f"Ubi_v1 t={self.ticket}"
                req_headers["Ubi-AppId"] = "f35adcb5-1911-440c-b1c9-48fdc1701c68"
                if headers:
                    req_headers.update(headers)

                async with self._session.request(method, url, headers=req_headers, **kwargs) as resp:
                    # 处理 401：强制刷新 ticket 并重试
                    if resp.status == 401:
                        self.ticket = None
                        self.ticket_expiry = 0
                        await self._ensure_ticket()
                        # 重试一次（递归调用，但通过循环处理）
                        continue

                    # 可重试的状态码
                    if resp.status in (429, 502, 503, 504) and attempt < max_retries:
                        wait = backoff_factor * (2 ** attempt)
                        logger.warning(f"请求失败 ({resp.status})，{wait:.1f}s 后重试 (尝试 {attempt+1}/{max_retries})")
                        await asyncio.sleep(wait)
                        continue

                    # 其他错误直接抛出
                    resp.raise_for_status()
                    return await resp.json()

            except (UbisoftAPIAuthError, UbisoftAPIRateLimitError):
                # 这些异常不再重试
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exception = e
                if attempt < max_retries:
                    wait = backoff_factor * (2 ** attempt)
                    logger.warning(f"网络请求异常: {e}，{wait:.1f}s 后重试 (尝试 {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                else:
                    raise UbisoftAPIError(f"网络请求失败（重试 {max_retries} 次后）: {e}")

        # 如果循环结束仍未返回，抛出最后一个异常
        if last_exception:
            raise UbisoftAPIError(f"请求失败: {last_exception}")
        raise UbisoftAPIError("未知请求错误")

    # ---------- 公开接口（带空数据兜底） ----------
    async def get_profile_by_username(self, username: str, platform: str = "uplay") -> Optional[Dict]:
        """通过用户名查询玩家信息，返回 None 表示未找到"""
        url = f"https://public-ubiservices.ubi.com/v2/profiles/?nameOnPlatform={username}&platformType={platform}"
        try:
            data = await self._request_with_retry("GET", url)
            profiles = data.get("profiles", [])
            if not profiles:
                return None
            return profiles[0]
        except UbisoftAPIError:
            # 向上抛出，让调用者处理
            raise

    async def get_profile_by_id(self, profile_id: str) -> Optional[Dict]:
        """通过 ProfileId 查询玩家信息，返回 None 表示未找到"""
        url = f"https://public-ubiservices.ubi.com/v1/profiles/{profile_id}"
        try:
            data = await self._request_with_retry("GET", url)
            # v1 接口直接返回 profile 对象
            if not data or not isinstance(data, dict):
                return None
            return data
        except UbisoftAPIError:
            raise

    async def get_stats(self, profile_id: str, space_id: str) -> Dict[str, Any]:
        """获取玩家统计数据，若未找到返回空字典"""
        url = f"https://public-ubiservices.ubi.com/v1/profiles/stats?spaceId={space_id}&profileIds={profile_id}"
        try:
            data = await self._request_with_retry("GET", url)
            profiles = data.get("profiles", [])
            if not profiles:
                return {}
            stats = profiles[0].get("stats")
            if stats is None:
                return {}
            return stats
        except UbisoftAPIError:
            raise