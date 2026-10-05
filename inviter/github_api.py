# -*- coding: utf-8 -*-
"""GitHub REST 客户端。

每个响应都是 (status, payload) —— 4xx 也照常返回, 由调用处决定怎么判。
限流退避在这里做, 调用处**不要**再套一层 sleep(搜索接口的 30/分钟限速除外)。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .console import log
from .constants import API_VERSION, DEFAULT_API, DEFAULT_PAGE, MAX_RETRIES, USER_AGENT


class ApiError(Exception):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload


def describe_error(status: int, payload: Any) -> str:
    """把 GitHub 的错误响应压成一句人话。"""
    if isinstance(payload, dict):
        message = str(payload.get("message") or "").strip()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts: list[str] = []
            for item in errors:
                if isinstance(item, dict):
                    parts.append(str(item.get("message") or item.get("field") or item))
                else:
                    parts.append(str(item))
            joined = "; ".join(parts)
            message = f"{message} ({joined})" if message else joined
        if message:
            return message
    return f"HTTP {status}"


class GitHub:
    """GitHub REST 客户端。每个响应都是 (status, payload), 4xx 也照常返回。"""

    def __init__(self, token: str, api_url: str = DEFAULT_API, timeout: int = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _once(self, method: str, path: str, body: Any = None) -> tuple[int, Any, dict]:
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                payload = json.loads(raw) if raw.strip() else {}
                return response.status, payload, dict(response.headers)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                payload = {"message": raw[:400]}
            return exc.code, payload, dict(exc.headers or {})
        except urllib.error.URLError as exc:
            raise ApiError(0, f"网络错误: {exc.reason}") from exc
        except TimeoutError as exc:
            raise ApiError(0, "请求超时") from exc

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """带限流/网络退避的请求。"""
        attempt = 0
        while True:
            attempt += 1
            try:
                status, payload, headers = self._once(method, path, body)
            except ApiError:
                if attempt > MAX_RETRIES:
                    raise
                time.sleep(min(60.0, 2.0 ** attempt))
                continue

            if status < 400 or status not in (403, 429, 500, 502, 503, 504):
                return status, payload

            message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
            lowered = message.lower()
            wait: float | None = None

            retry_after = headers.get("Retry-After")
            if retry_after and str(retry_after).isdigit():
                wait = float(retry_after)

            if status == 403 and "secondary rate limit" in lowered:
                wait = wait or min(120.0, 60.0 * attempt)
                log(f"  ! 触发二级限流, 等 {wait:.0f}s 再试 ({attempt}/{MAX_RETRIES})")
            elif status == 403 and headers.get("X-RateLimit-Remaining") == "0":
                reset = headers.get("X-RateLimit-Reset")
                wait = max(wait or 0.0, (float(reset) - time.time() + 2) if reset else 60.0)
                wait = max(wait, 1.0)
                log(f"  ! 主限流已用尽, 等 {wait:.0f}s 再试 ({attempt}/{MAX_RETRIES})")
            elif status in (500, 502, 503, 504):
                wait = wait or min(30.0, 3.0 * attempt)
            elif status == 429:
                wait = wait or 30.0

            if wait is None or attempt > MAX_RETRIES:
                return status, payload
            time.sleep(min(wait, 900.0))

    def get_paged(self, path: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        separator = "&" if "?" in path else "?"
        while True:
            status, payload = self.request(
                "GET", f"{path}{separator}per_page={DEFAULT_PAGE}&page={page}")
            if status >= 400:
                raise ApiError(status, describe_error(status, payload), payload)
            if not isinstance(payload, list) or not payload:
                break
            items.extend(payload)
            if len(payload) < DEFAULT_PAGE:
                break
            page += 1
        return items

    # ---- 具体接口 ----

    def user_id(self, login: str, cache: dict[str, int | None]) -> int | None:
        """登录名 -> 数字 id。取不到(拼错/不存在)返回 None。"""
        key = login.lower()
        if key in cache:
            return cache[key]
        status, payload = self.request("GET", f"/users/{urllib.parse.quote(login)}")
        value = None
        if status < 400 and isinstance(payload, dict) and payload.get("id"):
            value = int(payload["id"])
        cache[key] = value
        return value

    def org_members(self, org: str) -> set[str]:
        return {
            str(item.get("login") or "").lower()
            for item in self.get_paged(f"/orgs/{org}/members")
            if item.get("login")
        }

    def user_exists(self, login: str) -> bool:
        """这个 GitHub 用户名到底存不存在。不存在 -> 后面什么邀请都做不了。"""
        status, _ = self.request("GET", f"/users/{urllib.parse.quote(login)}")
        return status < 400

    def org_pending(self, org: str) -> tuple[set[str], set[str]]:
        """返回 (待处理邀请的邮箱集合, 待处理邀请的用户名集合)。

        注意: 按邮箱发出的邀请在 API 里 login 恒为 null, 所以两个集合都要看。
        """
        emails: set[str] = set()
        logins: set[str] = set()
        for item in self.get_paged(f"/orgs/{org}/invitations"):
            if item.get("email"):
                emails.add(str(item["email"]).lower())
            if item.get("login"):
                logins.add(str(item["login"]).lower())
        return emails, logins

    def failed_invitations(self, org: str) -> list[dict]:
        return [item for item in self.get_paged(f"/orgs/{org}/failed_invitations")
                if isinstance(item, dict)]

    def org_teams(self, org: str) -> list[dict]:
        return [item for item in self.get_paged(f"/orgs/{org}/teams") if isinstance(item, dict)]

    def team_members(self, org: str, slug: str) -> set[str]:
        return {
            str(item.get("login") or "").lower()
            for item in self.get_paged(f"/orgs/{org}/teams/{urllib.parse.quote(slug)}/members")
            if item.get("login")
        }

    def search_login_by_email(self, email: str) -> str:
        """用搜索接口把邮箱反查成登录名, 命中就赚(能走 invitee_id, 还能精确认成员)。

        只对「在 GitHub 上公开了邮箱」的账号有效, 所以命中率有限。
        """
        query = urllib.parse.quote(f"{email} in:email", safe="")
        status, payload = self.request("GET", f"/search/users?q={query}")
        if status >= 400 or not isinstance(payload, dict):
            return ""
        for item in (payload.get("items") or [])[:3]:
            login = str(item.get("login") or "")
            if not login:
                continue
            check, profile = self.request("GET", f"/users/{urllib.parse.quote(login)}")
            if check < 400 and isinstance(profile, dict) \
                    and str(profile.get("email") or "").strip().lower() == email.lower():
                return login
        return ""
