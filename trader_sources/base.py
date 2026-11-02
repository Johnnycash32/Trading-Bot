from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from threading import Event
from typing import Callable, Iterator


@dataclass(frozen=True)
class TraderEvent:
    source_trader_id: str
    exchange: str
    delivery_source: str
    instrument: str
    direction: str
    price: float
    quantity: float
    timestamp: str
    event_id: str
    event_type: str
    source_verified: bool
    timestamp_ms: int
    state_timestamp_ms: int
    state_checked_at_ms: int
    state_position_size: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class TraderEventSource(ABC):
    @abstractmethod
    def discover_traders(self, limit: int = 20) -> list[dict[str, object]]:
        raise NotImplementedError

    @abstractmethod
    def stream_events(
        self,
        trader_ids: list[str],
        stop_event: Event,
        status_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> Iterator[TraderEvent]:
        raise NotImplementedError
