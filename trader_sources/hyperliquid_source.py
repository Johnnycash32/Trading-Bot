from __future__ import annotations

import json
import logging
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Callable, Iterator, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import websocket

from .base import TraderEvent, TraderEventSource


LOGGER = logging.getLogger(__name__)
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"
WEBSOCKET_URL = "wss://api.hyperliquid.xyz/ws"
ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
MAX_BACKOFF_SECONDS = 30
MAX_USERS_PER_WEBSOCKET = 15
MAX_EVENT_IDS = 20_000
MAX_EVENTS = 500
STATE_CACHE_MS = 1_000
INITIAL_BACKFILL_MS = 60_000
RECONNECT_OVERLAP_MS = 2_000
MAX_FILLS_PER_BACKFILL = 2_000

StatusCallback = Callable[[dict[str, object]], None]


class _HttpResponse(Protocol):
    def __enter__(self) -> _HttpResponse: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def read(self) -> bytes: ...


class _WebSocketConnection(Protocol):
    def settimeout(self, timeout: float) -> None: ...

    def send(self, payload: str) -> object: ...

    def recv(self) -> str | bytes: ...

    def close(self) -> None: ...


class HyperliquidSource(TraderEventSource):
    """Public Hyperliquid wallet discovery and fill stream adapter."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10,
        http_open: Callable[..., _HttpResponse] = urlopen,
        websocket_connect: Callable[..., _WebSocketConnection] = websocket.create_connection,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._http_open = http_open
        self._websocket_connect = websocket_connect

    def _post_info(self, payload: dict[str, object]) -> object:
        request = Request(
            INFO_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._http_open(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def discover_traders(self, limit: int = 20) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("Leaderboard limit must be between 1 and 100.")
        request = Request(LEADERBOARD_URL, headers={"Accept": "application/json"})
        with self._http_open(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("leaderboardRows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Hyperliquid leaderboard response has no leaderboardRows list.")

        traders: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            address = row.get("ethAddress")
            month_pnl = self._month_pnl(row.get("windowPerformances"))
            if not isinstance(address, str) or not ADDRESS_PATTERN.fullmatch(address) or month_pnl is None:
                continue
            traders.append(
                {
                    "source_trader_id": address.lower(),
                    "account_value": self._optional_float(row.get("accountValue")),
                    "month_pnl": month_pnl,
                    "month_roi": self._month_metric(row.get("windowPerformances"), "roi"),
                    "month_volume": self._month_metric(row.get("windowPerformances"), "vlm"),
                    "display_name": row.get("displayName") or address[:10] + "…" + address[-6:],
                    "discovery_source": "public_leaderboard_undocumented",
                }
            )
        traders.sort(
            key=lambda trader: self._optional_float(trader["month_pnl"]) or 0.0,
            reverse=True,
        )
        return traders[:limit]

    @staticmethod
    def _month_performance(window_performances: object) -> dict[str, object] | None:
        if not isinstance(window_performances, list):
            return None
        for item in window_performances:
            if (
                isinstance(item, list)
                and len(item) == 2
                and item[0] == "month"
                and isinstance(item[1], dict)
            ):
                return item[1]
        return None

    @classmethod
    def _month_metric(cls, window_performances: object, key: str) -> float | None:
        performance = cls._month_performance(window_performances)
        if performance is None:
            return None
        return cls._optional_float(performance.get(key))

    @staticmethod
    def _month_pnl(window_performances: object) -> float | None:
        return HyperliquidSource._month_metric(window_performances, "pnl")

    @staticmethod
    def _optional_float(value: object) -> float | None:
        if not isinstance(value, (str, int, float)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number == number and abs(number) != float("inf") else None

    def clearinghouse_state(self, address: str) -> dict[str, object]:
        if not ADDRESS_PATTERN.fullmatch(address):
            raise ValueError("A valid Hyperliquid wallet address is required.")
        payload = self._post_info({"type": "clearinghouseState", "user": address})
        if not isinstance(payload, dict) or not isinstance(payload.get("assetPositions"), list):
            raise ValueError("Hyperliquid returned an invalid clearinghouse state.")
        if self._optional_float(payload.get("time")) is None:
            raise ValueError("Hyperliquid clearinghouse state has no valid timestamp.")
        return payload

    def fills_by_time(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, object]]:
        if not ADDRESS_PATTERN.fullmatch(address):
            raise ValueError("A valid Hyperliquid wallet address is required.")
        if start_ms < 0 or end_ms < start_ms:
            raise ValueError("Hyperliquid fill backfill time range is invalid.")
        payload = self._post_info(
            {
                "type": "userFillsByTime",
                "user": address,
                "startTime": start_ms,
                "endTime": end_ms,
            }
        )
        if not isinstance(payload, list) or any(not isinstance(fill, dict) for fill in payload):
            raise ValueError("Hyperliquid returned an invalid userFillsByTime response.")
        return payload

    @staticmethod
    def _state_position(state: dict[str, object], instrument: str) -> float:
        positions = state.get("assetPositions")
        if not isinstance(positions, list):
            raise ValueError("Clearinghouse state has no asset-position list.")
        for item in positions:
            if not isinstance(item, dict):
                continue
            position = item.get("position")
            if isinstance(position, dict) and position.get("coin") == instrument:
                size = HyperliquidSource._optional_float(position.get("szi"))
                if size is None:
                    raise ValueError("Clearinghouse position has an invalid size.")
                return size
        return 0.0

    @classmethod
    def normalize_fill(
        cls,
        address: str,
        fill: dict[str, object],
        state: dict[str, object],
        *,
        state_checked_at_ms: int,
        delivery_source: str,
    ) -> TraderEvent:
        if not ADDRESS_PATTERN.fullmatch(address):
            raise ValueError("A valid Hyperliquid wallet address is required.")
        instrument = fill.get("coin")
        side = fill.get("side")
        timestamp_ms = cls._optional_float(fill.get("time"))
        price = cls._optional_float(fill.get("px"))
        quantity = cls._optional_float(fill.get("sz"))
        trade_id = fill.get("tid")
        if (
            not isinstance(instrument, str)
            or not instrument
            or side not in {"B", "A"}
            or timestamp_ms is None
            or price is None
            or quantity is None
            or price <= 0
            or quantity <= 0
            or trade_id is None
        ):
            raise ValueError("Hyperliquid fill is missing a required event field.")
        state_timestamp = cls._optional_float(state.get("time"))
        if state_timestamp is None or not isinstance(state.get("assetPositions"), list):
            raise ValueError("A valid clearinghouse state is required to verify a fill.")

        state_position = cls._state_position(state, instrument)
        fill_start = cls._optional_float(fill.get("startPosition"))
        if fill_start is None:
            raise ValueError("Hyperliquid fill has no startPosition; its position change cannot be inferred.")
        before_size = fill_start
        after_size = before_size + (quantity if side == "B" else -quantity)
        if before_size == 0 and after_size != 0:
            event_type = "position_opened"
        elif before_size != 0 and after_size == 0:
            event_type = "position_closed"
        elif before_size * after_size < 0:
            event_type = "position_reversed"
        elif abs(after_size) > abs(before_size):
            event_type = "position_increased"
        elif abs(after_size) < abs(before_size):
            event_type = "position_reduced"
        else:
            raise ValueError("Fill did not produce an inferable position-size change.")

        direction_size = after_size if after_size != 0 else before_size
        timestamp_value = int(timestamp_ms)
        event_timestamp = datetime.fromtimestamp(timestamp_value / 1000, tz=timezone.utc)
        return TraderEvent(
            source_trader_id=address.lower(),
            exchange="Hyperliquid",
            delivery_source=delivery_source,
            instrument=instrument,
            direction="long" if direction_size > 0 else "short",
            price=price,
            quantity=quantity,
            timestamp=event_timestamp.isoformat(),
            event_id=f"{address.lower()}:{timestamp_value}:{instrument}:{trade_id}",
            event_type=event_type,
            source_verified=True,
            timestamp_ms=timestamp_value,
            state_timestamp_ms=int(state_timestamp),
            state_checked_at_ms=state_checked_at_ms,
            state_position_size=state_position,
        )

    def stream_events(
        self,
        trader_ids: list[str],
        stop_event: Event,
        status_callback: StatusCallback | None = None,
    ) -> Iterator[TraderEvent]:
        addresses = list(dict.fromkeys(address.lower() for address in trader_ids if ADDRESS_PATTERN.fullmatch(address)))
        if not addresses:
            raise ValueError("At least one valid Hyperliquid wallet address is required.")

        batches = [
            addresses[index:index + MAX_USERS_PER_WEBSOCKET]
            for index in range(0, len(addresses), MAX_USERS_PER_WEBSOCKET)
        ]
        worker_stop = Event()
        event_queue: Queue[TraderEvent] = Queue()
        connection_states = [False] * len(batches)
        duplicate_counts = [0] * len(batches)
        state_lock = Lock()

        def publish_status(batch_index: int, updates: dict[str, object]) -> None:
            if status_callback is None:
                return
            with state_lock:
                if "connected" in updates:
                    connection_states[batch_index] = updates["connected"] is True
                if "backfill_websocket_duplicate_count" in updates:
                    count = updates["backfill_websocket_duplicate_count"]
                    if isinstance(count, int):
                        duplicate_counts[batch_index] = count
                combined = dict(updates)
                combined["connected"] = any(connection_states)
                combined["connected_groups"] = sum(connection_states)
                combined["connection_groups"] = len(batches)
                combined["backfill_websocket_duplicate_count"] = sum(duplicate_counts)
            status_callback(combined)

        def collect_batch(batch_index: int, batch: list[str]) -> None:
            try:
                for event in self._stream_batch(
                    batch,
                    worker_stop,
                    lambda updates: publish_status(batch_index, updates),
                ):
                    event_queue.put(event)
            except Exception as exc:
                LOGGER.exception("Hyperliquid WebSocket worker failed for connection group %s.", batch_index + 1)
                publish_status(batch_index, {"connected": False, "error": str(exc)[:300]})

        workers = [
            Thread(
                target=collect_batch,
                args=(batch_index, batch),
                name=f"hyperliquid-fills-{batch_index + 1}",
                daemon=True,
            )
            for batch_index, batch in enumerate(batches)
        ]
        try:
            for worker in workers:
                worker.start()
            while any(worker.is_alive() for worker in workers) or not event_queue.empty():
                if stop_event.is_set():
                    worker_stop.set()
                try:
                    yield event_queue.get(timeout=0.25)
                except Empty:
                    continue
        finally:
            worker_stop.set()
            for worker in workers:
                worker.join(timeout=self.timeout_seconds + 2)

    def _stream_batch(
        self,
        addresses: list[str],
        stop_event: Event,
        status_callback: StatusCallback | None,
    ) -> Iterator[TraderEvent]:
        seen_ids: OrderedDict[str, None] = OrderedDict()
        last_seen_ms: dict[str, int] = {}
        backoff_seconds = 1
        backfill_websocket_duplicates = 0
        while not stop_event.is_set():
            connection: _WebSocketConnection | None = None
            try:
                connection = self._websocket_connect(WEBSOCKET_URL, timeout=self.timeout_seconds)
                connection.settimeout(1)
                for address in addresses:
                    connection.send(
                        json.dumps(
                            {
                                "method": "subscribe",
                                "subscription": {"type": "userFills", "user": address},
                            }
                        )
                    )
                self._notify_status(status_callback, connected=True, error=None)

                state_cache: dict[str, tuple[int, dict[str, object]]] = {}
                connection_backfill_ids: set[str] = set()
                backfill_end = int(time.time() * 1000)
                for address in addresses:
                    start_ms = last_seen_ms.get(address, max(0, backfill_end - INITIAL_BACKFILL_MS))
                    fills = self.fills_by_time(address, start_ms, backfill_end)
                    state = self.clearinghouse_state(address)
                    state_cache[address] = (int(time.time() * 1000), state)
                    if len(fills) >= MAX_FILLS_PER_BACKFILL:
                        self._notify_status(
                            status_callback,
                            backfill_truncated=True,
                            warning=f"{address} reached the {MAX_FILLS_PER_BACKFILL}-fill backfill response cap.",
                        )
                    for fill in sorted(
                        fills,
                        key=lambda item: self._optional_float(item.get("time")) or 0,
                    ):
                        event_id = self._fill_event_id(address, fill)
                        if event_id is not None:
                            connection_backfill_ids.add(event_id)
                        event = self._make_event(
                            address,
                            fill,
                            state,
                            state_cache[address][0],
                            seen_ids,
                            last_seen_ms,
                            "rest_backfill",
                        )
                        if event is not None:
                            yield event
                backoff_seconds = 1

                while not stop_event.is_set():
                    try:
                        raw_message = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw_message:
                        raise ConnectionError("Hyperliquid WebSocket closed without a message.")
                    message = json.loads(raw_message)
                    if not isinstance(message, dict):
                        continue
                    if message.get("channel") == "error":
                        raise ConnectionError(str(message.get("data", "Hyperliquid WebSocket returned an error.")))
                    if message.get("channel") != "userFills":
                        continue
                    data = message.get("data")
                    if not isinstance(data, dict):
                        continue
                    address = data.get("user")
                    fills = data.get("fills")
                    if not isinstance(address, str) or address.lower() not in addresses or not isinstance(fills, list):
                        continue
                    is_snapshot = message.get("isSnapshot") is True or data.get("isSnapshot") is True
                    delivery_source = "websocket_snapshot" if is_snapshot else "websocket"
                    normalized_address = address.lower()
                    cached_state = state_cache.get(normalized_address)
                    now_ms = int(time.time() * 1000)
                    if cached_state is None or now_ms - cached_state[0] >= STATE_CACHE_MS:
                        state = self.clearinghouse_state(normalized_address)
                        state_cache[normalized_address] = (now_ms, state)
                    else:
                        state = cached_state[1]
                    checked_at_ms = state_cache[normalized_address][0]
                    for fill in fills:
                        if not isinstance(fill, dict):
                            continue
                        event_id = self._fill_event_id(normalized_address, fill)
                        if event_id is not None and event_id in connection_backfill_ids:
                            backfill_websocket_duplicates += 1
                            self._notify_status(
                                status_callback,
                                backfill_websocket_duplicate_count=backfill_websocket_duplicates,
                                last_backfill_websocket_duplicate_id=event_id,
                                last_backfill_websocket_duplicate_at=datetime.now(timezone.utc).isoformat(),
                            )
                        event = self._make_event(
                            normalized_address,
                            fill,
                            state,
                            checked_at_ms,
                            seen_ids,
                            last_seen_ms,
                            delivery_source,
                        )
                        if event is not None:
                            yield event
            except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, websocket.WebSocketException, ConnectionError) as exc:
                self._notify_status(
                    status_callback,
                    connected=False,
                    error=str(exc)[:300],
                    reconnect_in_seconds=backoff_seconds,
                )
                delay = min(backoff_seconds, MAX_BACKOFF_SECONDS) * random.uniform(0.8, 1.2)
                stop_event.wait(delay)
                backoff_seconds = min(backoff_seconds * 2, MAX_BACKOFF_SECONDS)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except (OSError, websocket.WebSocketException):
                        LOGGER.debug("Hyperliquid WebSocket close failed.", exc_info=True)

    @classmethod
    def _make_event(
        cls,
        address: str,
        fill: dict[str, object],
        state: dict[str, object],
        state_checked_at_ms: int,
        seen_ids: OrderedDict[str, None],
        last_seen_ms: dict[str, int],
        delivery_source: str,
    ) -> TraderEvent | None:
        event_id = cls._fill_event_id(address, fill)
        if event_id is None:
            LOGGER.warning("Skipping Hyperliquid fill without a stable trade identifier for %s.", address)
            return None
        timestamp = cls._optional_float(fill.get("time"))
        if timestamp is None:
            return None
        timestamp_ms = int(timestamp)
        last_seen_ms[address] = max(last_seen_ms.get(address, 0), timestamp_ms)
        if event_id in seen_ids:
            return None
        event = cls.normalize_fill(
            address,
            fill,
            state,
            state_checked_at_ms=state_checked_at_ms,
            delivery_source=delivery_source,
        )
        seen_ids[event_id] = None
        if len(seen_ids) > MAX_EVENT_IDS:
            seen_ids.popitem(last=False)
        return event

    @staticmethod
    def _fill_event_id(address: str, fill: dict[str, object]) -> str | None:
        timestamp = HyperliquidSource._optional_float(fill.get("time"))
        instrument = fill.get("coin")
        trade_id = fill.get("tid")
        if (
            not ADDRESS_PATTERN.fullmatch(address)
            or timestamp is None
            or not isinstance(instrument, str)
            or trade_id is None
        ):
            return None
        return f"{address.lower()}:{int(timestamp)}:{instrument}:{trade_id}"

    @staticmethod
    def _notify_status(callback: StatusCallback | None, **updates: object) -> None:
        if callback is not None:
            callback(updates)


class HyperliquidMonitor:
    """Background collector exposed read-only to the paper dashboard."""

    def __init__(self, source: HyperliquidSource | None = None) -> None:
        self.source = source or HyperliquidSource()
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None
        self._traders: list[dict[str, object]] = []
        self._events: deque[dict[str, object]] = deque(maxlen=MAX_EVENTS)
        self._event_ids: OrderedDict[str, None] = OrderedDict()
        self._status: dict[str, object] = {
            "connected": False,
            "state": "not_started",
            "error": None,
            "discovery_source": "public_undocumented_leaderboard",
            "events_are_paper_only": True,
            "auto_copy_enabled": False,
            "last_event_received_at": None,
            "latest_event_timestamp": None,
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._run, name="hyperliquid-trader-monitor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "traders": list(self._traders),
                "events": list(self._events),
                "monitor": dict(self._status),
            }

    def _update_status(self, updates: dict[str, object]) -> None:
        with self._lock:
            self._status.update(updates)
            self._status["state"] = "connected" if self._status.get("connected") else "reconnecting"

    def _run(self) -> None:
        retry_seconds = 1
        while not self._stop.is_set():
            try:
                traders = self.source.discover_traders(limit=20)
                with self._lock:
                    self._traders = traders
                    self._status.update(
                        {
                            "state": "connecting",
                            "discovered_count": len(traders),
                            "discovered_at": datetime.now(timezone.utc).isoformat(),
                            "error": None,
                            "discovery_source": "public_undocumented_leaderboard",
                        }
                    )
                if not traders:
                    raise ValueError("Hyperliquid leaderboard returned no valid wallet addresses.")
                for event in self.source.stream_events(
                    [str(trader["source_trader_id"]) for trader in traders],
                    self._stop,
                    self._update_status,
                ):
                    if not event.source_verified:
                        LOGGER.warning("Ignoring unverified Hyperliquid trader event %s.", event.event_id)
                        continue
                    with self._lock:
                        if event.event_id in self._event_ids:
                            continue
                        self._event_ids[event.event_id] = None
                        if len(self._event_ids) > MAX_EVENT_IDS:
                            self._event_ids.popitem(last=False)
                        self._events.appendleft(event.as_dict())
                        self._status["last_event_received_at"] = datetime.now(timezone.utc).isoformat()
                        self._status["latest_event_timestamp"] = event.timestamp
                retry_seconds = 1
            except Exception as exc:
                LOGGER.exception("Hyperliquid trader discovery or monitoring failed.")
                with self._lock:
                    self._status.update(
                        {
                            "connected": False,
                            "state": "error",
                            "error": str(exc)[:300],
                        }
                    )
                self._stop.wait(retry_seconds)
                retry_seconds = min(retry_seconds * 2, MAX_BACKOFF_SECONDS)
