from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from backend.app.config import Settings


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0
    reason: str = ""


class InMemoryRateLimiter:
    """단일 프로세스용 슬라이딩 윈도 호출 제한기."""

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ip_per_minute = settings.chat_ip_per_minute
        self._ip_per_hour = settings.chat_ip_per_hour
        self._global_per_minute = settings.chat_global_per_minute
        self._global_per_day = settings.chat_global_per_day
        self._clock = clock
        self._lock = asyncio.Lock()
        self._ip_events: dict[str, deque[float]] = {}
        self._global_minute_events: deque[float] = deque()
        self._daily_events: deque[float] = deque()
        self._checks = 0

    @staticmethod
    def _prune(events: deque[float], cutoff: float) -> None:
        while events and events[0] <= cutoff:
            events.popleft()

    @staticmethod
    def _decision(
        events: deque[float],
        limit: int,
        window_seconds: float,
        now: float,
        reason: str,
    ) -> RateLimitDecision | None:
        if len(events) < limit:
            return None
        retry_after = max(1, math.ceil(events[-limit] + window_seconds - now))
        return RateLimitDecision(False, retry_after, reason)

    async def check_and_record(self, client_ip: str) -> RateLimitDecision:
        now = self._clock()
        async with self._lock:
            self._checks += 1
            self._prune(self._global_minute_events, now - 60)
            decision = self._decision(
                self._global_minute_events,
                self._global_per_minute,
                60,
                now,
                "현재 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
            )
            if decision is not None:
                return decision

            ip_events = self._ip_events.setdefault(client_ip, deque())
            self._prune(ip_events, now - 3_600)
            for limit, window, reason in (
                (self._ip_per_hour, 3_600, "시간당 사용 한도에 도달했습니다."),
                (self._ip_per_minute, 60, "요청이 너무 빠릅니다. 잠시 후 다시 시도해 주세요."),
            ):
                matching = deque(event for event in ip_events if event > now - window)
                decision = self._decision(matching, limit, window, now, reason)
                if decision is not None:
                    return decision

            ip_events.append(now)
            self._global_minute_events.append(now)

            if self._checks % 100 == 0:
                for key in list(self._ip_events):
                    events = self._ip_events[key]
                    self._prune(events, now - 3_600)
                    if not events:
                        del self._ip_events[key]

        return RateLimitDecision(True)

    async def check_and_record_daily(self) -> RateLimitDecision:
        """실제 Gemini 호출을 시작하기 직전에 일일 비용 한도를 예약한다."""
        now = self._clock()
        async with self._lock:
            self._prune(self._daily_events, now - 86_400)
            decision = self._decision(
                self._daily_events,
                self._global_per_day,
                86_400,
                now,
                "오늘의 전체 챗봇 사용 한도에 도달했습니다.",
            )
            if decision is not None:
                return decision
            self._daily_events.append(now)
        return RateLimitDecision(True)
