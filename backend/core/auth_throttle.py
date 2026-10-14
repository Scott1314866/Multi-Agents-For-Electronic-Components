"""Bounded, process-local limits for a single-worker company pilot."""
from collections import deque
from time import monotonic
from fastapi import HTTPException, Request

_attempts: dict[tuple[str,str], deque[float]] = {}


def limit_auth_attempts(request: Request, operation: str):
    now = monotonic()
    identity = request.client.host if request.client else "unknown"
    key = (operation, identity)
    # Do not use client-provided forwarded headers as an identity.
    if len(_attempts) > 4096:
        for old_key in list(_attempts):
            if not _attempts[old_key] or _attempts[old_key][-1] <= now - 300:
                _attempts.pop(old_key, None)
        if len(_attempts) > 4096:
            raise HTTPException(429, "登录请求较多，请稍后重试", headers={"Retry-After":"60"})
    window, maximum = (300, 40) if operation == "register" else (60, 60)
    attempts = _attempts.setdefault(key, deque())
    while attempts and attempts[0] <= now - window:
        attempts.popleft()
    if len(attempts) >= maximum:
        raise HTTPException(429, "尝试次数过多，请稍后重试", headers={"Retry-After":str(max(1,int(window-(now-attempts[0]))+1))})
    attempts.append(now)
