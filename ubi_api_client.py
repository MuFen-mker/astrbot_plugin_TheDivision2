import httpx
import asyncio
import base64
import time
import logging
from datetime import datetime
from typing import Optional, Dict, Any

logger = logging.getLogger("astrbot_plugin_TheDivision2")

class UbisoftAPIError(Exception):
    """所有 API 异常的基类"""
    pass

class UbisoftAPIAuthError(UbisoftAPIError):
    """认证失败（账号密码错误或 ticket 无效）"""
    pass

class UbisoftAPIRateLimitError(UbisoftAPIError):
    """请求被限流或临时风控（429/403）"""
    pass

class UbisoftAPIServerError(UbisoftAPIError):
    """服务器内部错误（5xx）"""
    pass

class UbisoftAPI:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.ticket: Optional[str] = None
        self.ticket_expiry: float = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

        # 登录请求头（与 curl 完全一致）
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
        # 查询请求头
        self.query_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        }

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_ticket(self):
        """获取或刷新 ticket，处理 401/403/限流等异常"""
        if self.ticket and time.time() < self.ticket_expiry - 300:
            return

        async with self._lock:
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
                if not self._client:
                    self._client = httpx.AsyncClient(timeout=30.0)

                resp = await self._client.post(url, headers=headers, json=payload)

                # 处理特定状态码
                if resp.status_code == 429:
                    raise UbisoftAPIRateLimitError("获取 ticket 被限流，请稍后重试")
                if resp.status_code == 403:
                    raise UbisoftAPIRateLimitError("请求被风控拦截，请稍后重试或更换 IP")

                resp.raise_for_status()
                data = resp.json()
                self.ticket = data["ticket"]
                exp_str = data["expiration"]
                expiry_dt = datetime.fromisoformat(exp_str.replace('Z', '+00:00'))
                self.ticket_expiry = expiry_dt.timestamp()
                logger.info(f"New ticket obtained, expires at {exp_str}")

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status == 401:
                    raise UbisoftAPIAuthError("育碧账号认证失败，请检查邮箱和密码")
                # 其他 HTTP 错误（如 500）交给上层重试
                raise UbisoftAPIError(f"获取 ticket 时遇到 HTTP {status}: {e.response.text}")
            except (httpx.RequestError, httpx.TimeoutException) as e:
                logger.error(f"Failed to obtain Ubisoft ticket: {e}")
                raise UbisoftAPIError(f"网络请求失败: {e}")

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict = None,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        **kwargs
    ) -> dict:
        """带重试的请求，处理 401/403/5xx 等可重试状态码"""
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                if not self._client:
                    self._client = httpx.AsyncClient(timeout=30.0)
                await self._ensure_ticket()

                req_headers = self.query_headers.copy()
                req_headers["Authorization"] = f"Ubi_v1 t={self.ticket}"
                req_headers["Ubi-AppId"] = "f35adcb5-1911-440c-b1c9-48fdc1701c68"
                if headers:
                    req_headers.update(headers)

                resp = await self._client.request(method, url, headers=req_headers, **kwargs)

                # 处理 401/403：重置客户端和 ticket，重试
                if resp.status_code in (401, 403):
                    logger.warning(f"收到 {resp.status_code}，重置客户端并刷新 ticket")
                    self.ticket = None
                    self.ticket_expiry = 0
                    if self._client:
                        await self._client.aclose()
                        self._client = None
                    # 重新获取 ticket 后继续循环（重试）
                    continue

                # 可重试的状态码：429, 5xx
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    wait = backoff_factor * (2 ** attempt)
                    logger.warning(f"请求失败 ({resp.status_code})，{wait:.1f}s 后重试 (尝试 {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue

                # 其他状态码直接抛出
                resp.raise_for_status()
                return resp.json()

            except (UbisoftAPIAuthError, UbisoftAPIRateLimitError):
                # 认证错误或限流错误不重试，直接抛出
                raise
            except (httpx.RequestError, httpx.TimeoutException) as e:
                # 网络层错误，可以重试
                last_exception = e
                if attempt < max_retries:
                    wait = backoff_factor * (2 ** attempt)
                    logger.warning(f"网络请求异常: {e}，{wait:.1f}s 后重试 (尝试 {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                else:
                    raise UbisoftAPIError(f"网络请求失败（重试 {max_retries} 次后）: {e}")
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # 如果状态码是可重试的（但未被前面的逻辑捕捉，通常是意外），继续重试
                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    wait = backoff_factor * (2 ** attempt)
                    logger.warning(f"HTTP 错误 ({status})，{wait:.1f}s 后重试 (尝试 {attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                # 否则抛出
                raise UbisoftAPIError(f"HTTP {status}: {e.response.text}")

        # 如果循环结束仍未返回，说明所有重试均失败
        if last_exception:
            raise UbisoftAPIError(f"请求失败: {last_exception}")
        raise UbisoftAPIError("未知请求错误")

    # ==================== 公开接口 ====================
    async def get_profile_by_username(self, username: str, platform: str = "uplay") -> Optional[Dict]:
        url = f"https://public-ubiservices.ubi.com/v2/profiles/?nameOnPlatform={username}&platformType={platform}"
        data = await self._request_with_retry("GET", url)
        profiles = data.get("profiles", [])
        return profiles[0] if profiles else None

    async def get_profile_by_id(self, profile_id: str) -> Optional[Dict]:
        url = f"https://public-ubiservices.ubi.com/v1/profiles/{profile_id}"
        data = await self._request_with_retry("GET", url)
        return data if data else None

    async def get_stats(self, profile_id: str, space_id: str) -> Dict[str, Any]:
        url = f"https://public-ubiservices.ubi.com/v1/profiles/stats?spaceId={space_id}&profileIds={profile_id}"
        data = await self._request_with_retry("GET", url)
        profiles = data.get("profiles", [])
        return profiles[0].get("stats", {}) if profiles else {}