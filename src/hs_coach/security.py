"""Production security helpers: sliding-window rate limiting, proxy/TLS checks, ingress bounds, and bounded coaching jobs."""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

_TRUTHY = frozenset({"1", "true", "yes"})


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RateLimitRule:
    max_requests: int
    window_seconds: float


class RateLimiter:
    """Thread-safe in-memory sliding-window rate limiter."""

    def __init__(self, rules: dict[str, RateLimitRule] | None = None) -> None:
        if rules is None:
            rules = {
                "auth": RateLimitRule(_env_int("HS_COACH_RATE_LIMIT_AUTH", 20), 60.0),
                "upload": RateLimitRule(_env_int("HS_COACH_RATE_LIMIT_UPLOAD", 30), 60.0),
                "coach": RateLimitRule(_env_int("HS_COACH_RATE_LIMIT_COACH", 20), 60.0),
            }
        self._rules = dict(rules)
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, client_key: str) -> tuple[bool, int]:
        """Record a request if under limit; return (allowed, retry_after_seconds)."""
        rule = self._rules.get(bucket)
        if rule is None or rule.max_requests <= 0 or rule.window_seconds <= 0:
            return True, 0

        now = time.monotonic()
        cutoff = now - rule.window_seconds
        key = f"{bucket}:{client_key}"

        with self._lock:
            dq = self._buckets.setdefault(key, deque())
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= rule.max_requests:
                retry_after = max(1, math.ceil((dq[0] + rule.window_seconds) - now))
                return False, retry_after
            dq.append(now)
            return True, 0


def is_trusted_proxy(
    peer_host: str | None, trusted_proxies: tuple[str, ...]
) -> bool:
    """Return True only if peer_host matches an explicit trusted proxy IP, CIDR, or host label."""
    if not peer_host or not trusted_proxies:
        return False
    peer = peer_host.strip()
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        addr = None

    for raw in trusted_proxies:
        entry = raw.strip()
        if not entry:
            continue
        if peer == entry:
            return True
        if addr is not None:
            try:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def get_client_ip(
    request: Request, trusted_proxies: tuple[str, ...] = ()
) -> str:
    """Resolve client IP, honoring X-Forwarded-For only when peer matches trusted_proxies."""
    peer_host = request.client.host if request.client and request.client.host else None
    if is_trusted_proxy(peer_host, trusted_proxies):
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            first_ip = xff.split(",", 1)[0].strip()
            if first_ip:
                return first_ip
    return peer_host or "unknown"


def is_secure_request(
    request: Request,
    trusted_proxies: tuple[str, ...] = (),
    *,
    cookie_secure: bool = False,
) -> bool:
    """Determine if request uses HTTPS (directly or via trusted proxy) or cookie_secure."""
    if cookie_secure or _env_flag("HS_COACH_COOKIE_SECURE"):
        return True
    if request.url.scheme == "https":
        return True
    peer_host = request.client.host if request.client and request.client.host else None
    if is_trusted_proxy(peer_host, trusted_proxies):
        proto = (
            request.headers.get("x-forwarded-proto", "")
            .split(",", 1)[0]
            .strip()
            .lower()
        )
        if proto == "https":
            return True
    return False


class BodyTooLargeError(Exception):
    """Raised when streamed request body bytes exceed the configured ingress cap."""


class MaxBodySizeMiddleware:
    """Pure ASGI middleware enforcing per-path request body limits before parsing or spooling."""

    _BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(
        self,
        app: Any,
        *,
        default_limit: int,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.default_limit = max(1, int(default_limit))
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        limit = self.path_limits.get(path, self.default_limit)

        for name, value in scope.get("headers") or ():
            if name.lower() == b"content-length":
                try:
                    declared = int(value.decode("ascii").strip())
                except Exception:
                    resp = JSONResponse(
                        {"detail": "Invalid Content-Length header."}, status_code=400
                    )
                    await resp(scope, receive, send)
                    return
                if declared < 0:
                    resp = JSONResponse(
                        {"detail": "Invalid Content-Length header."}, status_code=400
                    )
                    await resp(scope, receive, send)
                    return
                if declared > limit:
                    resp = JSONResponse(
                        {"detail": f"Request payload exceeds {limit} byte limit."},
                        status_code=413,
                    )
                    await resp(scope, receive, send)
                    return

        total_bytes = 0
        response_started = False
        limit_exceeded_msg: str | None = None
        sent_limit_body = False

        async def _limited_receive() -> dict[str, Any]:
            nonlocal total_bytes, limit_exceeded_msg
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                total_bytes += len(chunk)
                if total_bytes > limit:
                    limit_exceeded_msg = f"Request payload exceeds {limit} byte limit."
                    raise BodyTooLargeError(limit_exceeded_msg)
            return message

        async def _wrapped_send(message: dict[str, Any]) -> None:
            nonlocal response_started, sent_limit_body
            if limit_exceeded_msg is not None:
                body_bytes = json.dumps({"detail": limit_exceeded_msg}).encode("utf-8")
                if message.get("type") == "http.response.start":
                    response_started = True
                    preserved = [
                        (k, v)
                        for k, v in (message.get("headers") or ())
                        if k.lower() not in (b"content-length", b"content-type")
                    ]
                    preserved.append((b"content-type", b"application/json"))
                    preserved.append(
                        (b"content-length", str(len(body_bytes)).encode("ascii"))
                    )
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 413,
                            "headers": preserved,
                        }
                    )
                    return
                if message.get("type") == "http.response.body":
                    if not sent_limit_body:
                        sent_limit_body = True
                        await send(
                            {
                                "type": "http.response.body",
                                "body": body_bytes,
                                "more_body": False,
                            }
                        )
                    return

            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, _limited_receive, _wrapped_send)
        except BodyTooLargeError as exc:
            if not response_started:
                resp = JSONResponse({"detail": str(exc)}, status_code=413)
                await resp(scope, receive, send)
            else:
                raise


class QueueFullError(RuntimeError):
    """Raised when the async coaching queue reaches its bounded capacity."""


class CoachJobManager:
    """Thread-safe bounded background executor and status tracker for async coaching jobs."""

    MAX_COMPLETED_JOBS = 500

    def __init__(self, max_workers: int = 4, max_queued: int = 32) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="hs-coach-worker",
        )
        self._max_queued = max(1, int(max_queued))
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._active_keys: dict[tuple[str, str], str] = {}

    @staticmethod
    def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
        snap = dict(job)
        if isinstance(snap.get("result"), dict):
            snap["result"] = dict(snap["result"])
        return snap

    def _prune_completed_locked(self) -> None:
        completed_ids = [
            jid
            for jid, job in self._jobs.items()
            if job["status"] in ("ready", "failed")
        ]
        excess = len(completed_ids) - self.MAX_COMPLETED_JOBS
        for jid in completed_ids[: max(0, excess)]:
            self._jobs.pop(jid, None)

    def get_active_job(self, user_id: str, game_id: str) -> dict[str, Any] | None:
        """Return snapshot of any in-flight (queued/running) job for (user_id, game_id)."""
        active_key = (user_id, game_id)
        with self._lock:
            existing_id = self._active_keys.get(active_key)
            if existing_id is None:
                return None
            existing = self._jobs.get(existing_id)
            if existing is not None and existing["status"] in ("queued", "running"):
                return self._snapshot(existing)
            return None

    def submit(
        self,
        user_id: str,
        game_id: str,
        fn: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Queue a coaching job, deduplicating in-flight (user_id, game_id) and bounding queue size."""
        active_key = (user_id, game_id)
        with self._lock:
            existing_id = self._active_keys.get(active_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing["status"] in ("queued", "running"):
                    return self._snapshot(existing)

            active_count = sum(
                1
                for j in self._jobs.values()
                if j["status"] in ("queued", "running")
            )
            if active_count >= self._max_queued:
                raise QueueFullError(
                    "Coaching queue is currently full. Please retry shortly."
                )

            self._prune_completed_locked()
            job_id = uuid.uuid4().hex[:16]
            job: dict[str, Any] = {
                "job_id": job_id,
                "user_id": user_id,
                "game_id": game_id,
                "status": "queued",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "result": None,
                "error": None,
            }
            self._jobs[job_id] = job
            self._active_keys[active_key] = job_id
            initial = self._snapshot(job)

        def _runner() -> None:
            with self._lock:
                cur = self._jobs.get(job_id)
                if cur is not None:
                    cur["status"] = "running"
            try:
                result = fn()
            except Exception as exc:
                with self._lock:
                    cur = self._jobs.get(job_id)
                    if cur is not None:
                        cur["status"] = "failed"
                        cur["error"] = str(exc)
                        cur["finished_at"] = datetime.now(timezone.utc).isoformat()
                    if self._active_keys.get(active_key) == job_id:
                        self._active_keys.pop(active_key, None)
            else:
                with self._lock:
                    cur = self._jobs.get(job_id)
                    if cur is not None:
                        cur["status"] = "ready"
                        cur["result"] = result
                        cur["finished_at"] = datetime.now(timezone.utc).isoformat()
                    if self._active_keys.get(active_key) == job_id:
                        self._active_keys.pop(active_key, None)

        self._executor.submit(_runner)
        return initial

    def get(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        """Return a snapshot of a job owned by user_id, or None."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["user_id"] != user_id:
                return None
            return self._snapshot(job)

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the background worker executor."""
        self._executor.shutdown(wait=wait)
