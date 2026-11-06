from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Callable

from trader_sources.base import TraderEvent


ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
INSTRUMENT_PATTERN = re.compile(r"^[A-Z0-9]{1,14}$")
MAX_SELECTED_WALLETS = 15
MAX_PROCESSED_EVENT_IDS = 20_000
MAX_EVENT_AGE_MS = 5_000
FUTURE_EVENT_TOLERANCE_MS = 2_000
DEFAULT_STOP_LOSS_PERCENT = 2.0
DEFAULT_TAKE_PROFIT_PERCENT = 4.5
DEFAULT_LEVERAGE = 1.0


@dataclass(frozen=True)
class PaperCopySettings:
    selected_wallets: tuple[str, ...] = ()
    stop_loss_percent: float = DEFAULT_STOP_LOSS_PERCENT
    take_profit_percent: float = DEFAULT_TAKE_PROFIT_PERCENT
    leverage: float = DEFAULT_LEVERAGE
    processed_event_ids: tuple[str, ...] = ()

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        processed_event_ids: tuple[str, ...] = (),
    ) -> PaperCopySettings:
        if not isinstance(payload, dict):
            raise ValueError("Paper-copy settings must be an object.")
        wallets = payload.get("selected_wallets", [])
        if (
            not isinstance(wallets, list)
            or len(wallets) > MAX_SELECTED_WALLETS
            or any(not isinstance(wallet, str) or not ADDRESS_PATTERN.fullmatch(wallet) for wallet in wallets)
        ):
            raise ValueError("Select up to 15 valid Hyperliquid wallet addresses.")
        normalized_wallets = tuple(dict.fromkeys(wallet.lower() for wallet in wallets))

        stop = cls._finite_number(payload.get("stop_loss_percent"), "Stop-loss percentage")
        target = cls._finite_number(payload.get("take_profit_percent"), "Take-profit percentage")
        leverage = cls._finite_number(payload.get("leverage"), "Paper leverage")
        if not 0.1 <= stop <= 10:
            raise ValueError("Stop-loss percentage must be between 0.1 and 10.")
        if not stop * 2 + 0.5 <= target <= 50:
            raise ValueError("Take-profit must be at least twice the stop plus 0.5% for costs, and no more than 50%.")
        if not 1 <= leverage <= 2:
            raise ValueError("Paper leverage must be between 1x and 2x.")
        return cls(
            normalized_wallets,
            stop,
            target,
            leverage,
            tuple(processed_event_ids[-MAX_PROCESSED_EVENT_IDS:]),
        )

    @staticmethod
    def _finite_number(value: object, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"{label} must be a finite number.")
        return float(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "selected_wallets": list(self.selected_wallets),
            "stop_loss_percent": self.stop_loss_percent,
            "take_profit_percent": self.take_profit_percent,
            "leverage": self.leverage,
        }


def load_settings(path: Path) -> PaperCopySettings:
    if not path.exists():
        return PaperCopySettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read paper-copy settings at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid paper-copy settings at {path}.")
    processed_ids = payload.get("processed_event_ids", [])
    if (
        not isinstance(processed_ids, list)
        or any(not isinstance(event_id, str) or not event_id for event_id in processed_ids)
    ):
        raise ValueError(f"Invalid processed event IDs in paper-copy settings at {path}.")
    settings = PaperCopySettings.from_payload(payload, processed_event_ids=tuple(processed_ids))
    return settings


def save_settings(path: Path, settings: PaperCopySettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.as_dict()
    payload["processed_event_ids"] = list(settings.processed_event_ids)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, allow_nan=False, indent=2), encoding="utf-8")
    temporary_path.replace(path)


def candidate_rejection(
    event: TraderEvent,
    settings: PaperCopySettings,
    monitored_wallets: set[str],
    *,
    now_ms: int,
) -> str | None:
    if not event.source_verified:
        return "Fill is not source-verified."
    if event.delivery_source != "websocket":
        return "Only new WebSocket fills are eligible; REST backfill and snapshots are not copied."
    if event.event_type != "position_opened":
        return "Only new position opens are copied; increases, reductions, closes, and reversals are skipped."
    if event.source_trader_id not in settings.selected_wallets:
        return "Source wallet is not selected for paper copying."
    if event.source_trader_id not in monitored_wallets:
        return "Source wallet is not in the currently monitored top-15 list."
    if not event.event_id:
        return "Fill has no unique event ID."
    age_ms = now_ms - event.timestamp_ms
    if age_ms < -FUTURE_EVENT_TOLERANCE_MS or age_ms > MAX_EVENT_AGE_MS:
        return "Fill is stale or has a future timestamp."
    state_age_ms = now_ms - event.state_checked_at_ms
    if state_age_ms < -FUTURE_EVENT_TOLERANCE_MS or state_age_ms > MAX_EVENT_AGE_MS:
        return "Verified position state is stale."
    if event.state_timestamp_ms < event.timestamp_ms or event.state_timestamp_ms - event.timestamp_ms > MAX_EVENT_AGE_MS:
        return "Position state does not closely follow the source fill."
    if event.direction not in {"long", "short"} or event.price <= 0 or event.quantity <= 0:
        return "Fill direction, price, or size is invalid."
    if not all(isfinite(value) for value in (event.price, event.quantity, event.state_position_size)):
        return "Fill contains a non-finite value."
    expected_sign = 1 if event.direction == "long" else -1
    if event.state_position_size * expected_sign <= 0 or abs(event.state_position_size) < event.quantity:
        return "Current source position does not confirm the opened fill."
    return None


def destination_symbol(instrument: str) -> str:
    normalized = instrument.upper()
    if not INSTRUMENT_PATTERN.fullmatch(normalized):
        raise ValueError("Hyperliquid instrument has no supported USDT perpetual mapping.")
    return f"{normalized}USDT"


class PaperCopyEventWorker:
    def __init__(self, handler: Callable[[TraderEvent], None], *, capacity: int = 256) -> None:
        if not callable(handler):
            raise TypeError("Paper-copy event handler must be callable.")
        self._handler: Callable[[TraderEvent], None] = handler
        self._queue: Queue[TraderEvent] = Queue(maxsize=capacity)
        self._thread = Thread(target=self._run, name="paper-copy-events", daemon=True)
        self._thread.start()

    def submit(self, event: TraderEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except Full:
            return False

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                self._handler(event)
            except Exception:
                logging.getLogger(__name__).exception("Paper-copy event processing failed.")
            finally:
                self._queue.task_done()
