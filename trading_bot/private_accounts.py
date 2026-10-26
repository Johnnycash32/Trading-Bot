"""Read-only private-account monitoring for paper copy research.

This module intentionally has no order, transfer, withdrawal, or copy-trade
methods. It only signs authenticated Bybit queries when the user supplies a
read-only API key through environment variables.
"""

from dataclasses import dataclass
import hashlib
import hmac
import os
import time
from typing import Any, Callable
from urllib.parse import urlencode

from .market_data import load_json
from .models import Rejection, RiskLimits, Side, TradeProposal
from .risk import RiskEngine


JsonLoader = Callable[[str, dict[str, str]], dict[str, Any]]


def authenticated_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    from urllib.request import Request, urlopen

    request = Request(url, headers=headers)
    with urlopen(request, timeout=10) as response:
        import json

        return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True)
class PrivateAccountConfig:
    api_key: str
    api_secret: str
    base_url: str = "https://api.bybit.com"
    account_type: str = "UNIFIED"

    @classmethod
    def from_environment(cls) -> "PrivateAccountConfig | None":
        api_key = os.getenv("BYBIT_READONLY_API_KEY", "").strip()
        api_secret = os.getenv("BYBIT_READONLY_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return None
        return cls(api_key, api_secret, os.getenv("BYBIT_API_BASE_URL", cls.base_url).strip() or cls.base_url)


@dataclass(frozen=True)
class PrivatePosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    stop_loss: float
    take_profit: float
    lion_approved: bool
    lion_reason: str

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


class BybitReadOnlyAccount:
    """Bybit private monitor; authenticated reads only."""

    def __init__(self, config: PrivateAccountConfig, loader: JsonLoader = authenticated_json) -> None:
        self.config = config
        self._load = loader

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        query = urlencode(params)
        signature_payload = timestamp + self.config.api_key + recv_window + query
        signature = hmac.new(self.config.api_secret.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": self.config.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        return self._load(f"{self.config.base_url}{path}?{query}", headers)

    def verify_read_only(self) -> bool:
        payload = self._get("/v5/user/query-api", {})
        if payload.get("retCode") != 0:
            raise RuntimeError(payload.get("retMsg", "Bybit API-key verification failed"))
        return bool(payload.get("result", {}).get("readOnly") == 1)

    def snapshot(self, *, risk_engine: RiskEngine | None = None) -> dict[str, object]:
        if not self.verify_read_only():
            raise PermissionError("The configured Bybit key is not read-only; private monitoring is disabled.")
        engine = risk_engine or RiskEngine(RiskLimits())
        positions_payload = self._get("/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        balance_payload = self._get("/v5/account/wallet-balance", {"accountType": self.config.account_type, "coin": "USDT"})
        rows = positions_payload.get("result", {}).get("list", [])
        equity = float(balance_payload.get("result", {}).get("list", [{}])[0].get("totalEquity", 0) or 0)
        positions: list[PrivatePosition] = []
        for row in rows:
            size = float(row.get("size", 0) or 0)
            if size <= 0:
                continue
            side = Side.LONG if str(row.get("side", "Buy")).lower() == "buy" else Side.SHORT
            entry = float(row.get("avgPrice", 0) or 0)
            mark = float(row.get("markPrice", 0) or 0)
            stop = float(row.get("stopLoss", 0) or 0)
            target = float(row.get("takeProfit", 0) or 0)
            proposal = TradeProposal("bybit-private", str(row.get("symbol", "")), side, entry, stop, target, equity, open_positions=len(rows), leverage=1.0)
            decision = engine.validate(proposal) if equity > 0 else Rejection("invalid_equity", "Private account equity is unavailable.")
            approved = not isinstance(decision, Rejection)
            reason = "Lion risk policy passed for paper copying." if approved else decision.reason
            positions.append(PrivatePosition(proposal.symbol, side.value, size, entry, mark, float(row.get("unrealisedPnl", 0) or 0), stop, target, approved, reason))
        return {"source": "Bybit private read-only", "mode": "paper", "read_only": True, "equity": equity, "positions": [position.as_dict() for position in positions]}


def private_account_snapshot() -> dict[str, object]:
    config = PrivateAccountConfig.from_environment()
    if config is None:
        return {"configured": False, "mode": "paper", "read_only": True, "positions": [], "message": "Set BYBIT_READONLY_API_KEY and BYBIT_READONLY_API_SECRET to enable private monitoring."}
    try:
        return {"configured": True, **BybitReadOnlyAccount(config).snapshot()}
    except Exception as exc:
        return {"configured": True, "mode": "paper", "read_only": True, "positions": [], "error": str(exc)}
