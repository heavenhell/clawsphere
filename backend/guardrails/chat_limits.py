from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class ChatLease:
    limiter: "ChatLimiter"
    key: tuple[str, str]
    released: bool = False

    def release(self) -> None:
        if not self.released:
            self.limiter.release(self.key)
            self.released = True


class ChatLimiter:
    def __init__(
        self,
        *,
        per_minute: int = 30,
        burst_per_10_seconds: int = 8,
        concurrent_per_user: int = 2,
        concurrent_global: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.per_minute = per_minute
        self.burst_per_10_seconds = burst_per_10_seconds
        self.concurrent_per_user = concurrent_per_user
        self.concurrent_global = concurrent_global
        self.clock = clock
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._active: dict[tuple[str, str], int] = defaultdict(int)
        self._active_global = 0

    def try_acquire(self, user_id: str, tenant_id: str) -> ChatLease | None:
        key = (tenant_id, user_id)
        now = self.clock()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - 60:
                events.popleft()
            burst_count = sum(timestamp > now - 10 for timestamp in events)
            if (
                len(events) >= self.per_minute
                or burst_count >= self.burst_per_10_seconds
                or self._active[key] >= self.concurrent_per_user
                or self._active_global >= self.concurrent_global
            ):
                return None
            events.append(now)
            self._active[key] += 1
            self._active_global += 1
        return ChatLease(self, key)

    def release(self, key: tuple[str, str]) -> None:
        with self._lock:
            if self._active[key] > 0:
                self._active[key] -= 1
                self._active_global -= 1


chat_limiter = ChatLimiter(
    per_minute=int(os.getenv("DCS_CHAT_RATE_PER_MINUTE", "30")),
    burst_per_10_seconds=int(os.getenv("DCS_CHAT_BURST_PER_10S", "8")),
    concurrent_per_user=int(os.getenv(
        "DCS_CHAT_MAX_CONCURRENT_PER_USER",
        os.getenv("DCS_CHAT_CONCURRENT_PER_USER", "2"),
    )),
    concurrent_global=int(os.getenv(
        "DCS_CHAT_MAX_CONCURRENT_GLOBAL",
        os.getenv("DCS_CHAT_CONCURRENT_GLOBAL", "8"),
    )),
)
