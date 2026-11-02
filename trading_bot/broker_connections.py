"""Broker directory, local connection requests, and isolated Bybit testnet checks."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.request import Request, urlopen
from urllib.parse import urlencode

BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"
BROKER_DIRECTORY = (
    {"id": "paper", "name": "Paper Trading", "category": "Simulation", "connection": "available"},
    {"id": "fxpro", "name": "FxPro", "category": "Forex", "connection": "request"},
    {"id": "bybit", "name": "Bybit", "category": "Crypto", "connection": "testnet"},
    {"id": "easymarkets", "name": "easyMarkets", "category": "Forex", "connection": "request"},
    {"id": "trade-nation", "name": "Trade Nation", "category": "Forex", "connection": "request"},
    {"id": "deriv", "name": "Deriv", "category": "Forex", "connection": "request"},
    {"id": "thinkmarkets", "name": "ThinkMarkets", "category": "Forex", "connection": "request"},
    {"id": "pepperstone", "name": "Pepperstone", "category": "Forex", "connection": "request"},
    {"id": "fusion-markets", "name": "Fusion Markets", "category": "Forex", "connection": "request"},
    {"id": "capital-com", "name": "Capital.com", "category": "Multi-asset", "connection": "request"},
    {"id": "ninjatrader", "name": "NinjaTrader", "category": "Futures", "connection": "request"},
    {"id": "tickmill", "name": "Tickmill", "category": "Forex", "connection": "request"},
    {"id": "ic-markets", "name": "IC Markets", "category": "Forex", "connection": "request"},
    {"id": "whitebit", "name": "WhiteBIT", "category": "Crypto", "connection": "request"},
    {"id": "activtrades", "name": "ActivTrades", "category": "Multi-asset", "connection": "request"},
    {"id": "kraken", "name": "Kraken", "category": "Crypto", "connection": "request"},
    {"id": "bingx", "name": "BingX", "category": "Crypto", "connection": "request"},
    {"id": "cmc-markets", "name": "CMC Markets", "category": "Multi-asset", "connection": "request"},
    {"id": "gate", "name": "Gate", "category": "Crypto", "connection": "request"},
    {"id": "binance", "name": "Binance", "category": "Crypto", "connection": "request"},
    {"id": "p2p-prime", "name": "P2PRIME", "category": "Forex", "connection": "request"},
    {"id": "crypto-com", "name": "Crypto.com Exchange", "category": "Crypto", "connection": "request"},
    {"id": "coinw", "name": "CoinW", "category": "Crypto", "connection": "request"},
    {"id": "fp-markets", "name": "FP Markets", "category": "Forex", "connection": "request"},
)

REQUEST_MODES = {"read-only", "testnet", "live"}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")
JsonLoader = Callable[[str, dict[str, str]], dict[str, Any]]
JsonPoster = Callable[[str, dict[str, str], str], dict[str, Any]]


def _local_testnet_values() -> dict[str, str]:
    names = {
        "BYBIT_TESTNET_API_KEY",
        "BYBIT_TESTNET_API_SECRET",
        "BYBIT_TESTNET_ACCOUNT_LABEL",
    }
    values = {name: os.environ[name] for name in names if os.environ.get(name)}
    env_path = Path(__file__).resolve().parents[1] / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return values
    except OSError as exc:
        raise RuntimeError(f"Could not read the local environment file: {exc}") from exc

    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        name, separator, raw_value = entry.partition("=")
        key = name.strip()
        if not separator or key not in names or key in values:
            continue
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            closing_quote = value.find(value[0], 1)
            if closing_quote != -1:
                value = value[1:closing_quote]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


@dataclass(frozen=True)
class BybitTestnetConfig:
    api_key: str
    api_secret: str
    account_label: str = "Bybit Testnet account"
    account_type: str = "UNIFIED"

    @classmethod
    def from_environment(cls) -> "BybitTestnetConfig | None":
        values = _local_testnet_values()
        api_key = values.get("BYBIT_TESTNET_API_KEY", "").strip()
        api_secret = values.get("BYBIT_TESTNET_API_SECRET", "").strip()
        if not api_key or not api_secret:
            return None
        label = values.get("BYBIT_TESTNET_ACCOUNT_LABEL", "").strip() or "Bybit Testnet account"
        if len(label) > 80:
            raise ValueError("BYBIT_TESTNET_ACCOUNT_LABEL must be at most 80 characters.")
        return cls(api_key, api_secret, label)


def authenticated_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = Request(url, headers={**headers, "User-Agent": "trading-bot-testnet/0.1"})
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Bybit testnet returned an invalid response.")
    return result


def authenticated_json_post(url: str, headers: dict[str, str], body: str) -> dict[str, Any]:
    request_headers = {**headers, "Content-Type": "application/json"}
    request = Request(url, data=body.encode("utf-8"), headers=request_headers, method="POST")
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Bybit testnet returned an invalid response.")
    return result


class BybitTestnetAccount:
    """Reads the designated Bybit testnet account; the base URL cannot be overridden."""

    def __init__(
        self,
        config: BybitTestnetConfig,
        loader: JsonLoader = authenticated_json,
        poster: JsonPoster = authenticated_json_post,
    ) -> None:
        self.config = config
        self._load = loader
        self._post = poster

    @staticmethod
    def _key_is_read_only(key_result: dict[str, Any]) -> bool:
        return key_result.get("readOnly") not in (0, "0", False)

    def _headers(self, signature_payload: str) -> dict[str, str]:
        signature = hmac.new(
            self.config.api_secret.encode(),
            signature_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.config.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": str(int(time.time() * 1000)),
            "X-BAPI-RECV-WINDOW": "5000",
        }

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        query = urlencode(params)
        signature_payload = timestamp + self.config.api_key + recv_window + query
        headers = self._headers(signature_payload)
        headers["X-BAPI-TIMESTAMP"] = timestamp
        return self._load(f"{BYBIT_TESTNET_URL}{path}?{query}", headers)

    def snapshot(self) -> dict[str, object]:
        key_payload = self._get("/v5/user/query-api", {})
        if key_payload.get("retCode") != 0:
            raise RuntimeError(str(key_payload.get("retMsg", "Bybit testnet rejected the API key.")))
        key_result = key_payload.get("result", {})
        wallet_payload = self._get(
            "/v5/account/wallet-balance",
            {"accountType": self.config.account_type, "coin": "USDT"},
        )
        if wallet_payload.get("retCode") != 0:
            raise RuntimeError(str(wallet_payload.get("retMsg", "Bybit testnet wallet lookup failed.")))
        accounts = wallet_payload.get("result", {}).get("list", [])
        if not accounts:
            raise RuntimeError("Bybit testnet returned no wallet for this account.")
        equity = float(accounts[0].get("totalEquity", 0) or 0)
        return {
            "configured": True,
            "verified": True,
            "broker": "Bybit",
            "environment": "TESTNET",
            "account_label": self.config.account_label,
            "account_type": self.config.account_type,
            "equity_usdt": equity,
            "api_key_read_only": self._key_is_read_only(key_result),
            "order_submission_enabled": not self._key_is_read_only(key_result),
            "message": "Testnet account verified. Only explicit manual testnet orders are supported; live orders are disabled.",
        }

    def place_market_order(self, symbol: str, side: str, quantity: str) -> dict[str, object]:
        normalized_symbol = symbol.strip().upper()
        normalized_side = side.strip().lower()
        if not re.fullmatch(r"[A-Z0-9]{2,20}", normalized_symbol):
            raise ValueError("Enter a valid uppercase market symbol such as SOLUSDT.")
        if normalized_side not in {"buy", "sell"}:
            raise ValueError("Choose Buy or Sell.")
        if not isinstance(quantity, str) or len(quantity.strip()) > 32:
            raise ValueError("Enter a valid positive order quantity.")
        try:
            parsed_quantity = Decimal(quantity.strip())
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Enter a valid positive order quantity.") from exc
        if not parsed_quantity.is_finite() or parsed_quantity <= 0 or not re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)", quantity.strip()):
            raise ValueError("Enter a valid positive order quantity.")

        key_payload = self._get("/v5/user/query-api", {})
        if key_payload.get("retCode") != 0:
            raise RuntimeError(str(key_payload.get("retMsg", "Bybit testnet rejected the API key.")))
        if self._key_is_read_only(key_payload.get("result", {})):
            raise PermissionError("The configured Bybit testnet key is read-only and cannot submit test orders.")

        body = json.dumps(
            {
                "category": "linear",
                "symbol": normalized_symbol,
                "side": "Buy" if normalized_side == "buy" else "Sell",
                "orderType": "Market",
                "qty": str(quantity).strip(),
                "positionIdx": 0,
                "orderLinkId": "paperbot-test-" + uuid.uuid4().hex[:20],
            },
            separators=(",", ":"),
        )
        timestamp = str(int(time.time() * 1000))
        signature_payload = timestamp + self.config.api_key + "5000" + body
        headers = self._headers(signature_payload)
        headers["X-BAPI-TIMESTAMP"] = timestamp
        response = self._post(
            f"{BYBIT_TESTNET_URL}/v5/order/create",
            headers,
            body,
        )
        if response.get("retCode") != 0:
            raise RuntimeError(str(response.get("retMsg", "Bybit testnet rejected the order.")))
        order_id = response.get("result", {}).get("orderId")
        if not isinstance(order_id, str) or not order_id:
            raise RuntimeError("Bybit testnet accepted the request without returning an order ID.")
        return {
            "environment": "TESTNET",
            "broker": "Bybit",
            "account_label": self.config.account_label,
            "symbol": normalized_symbol,
            "side": "Buy" if normalized_side == "buy" else "Sell",
            "order_type": "Market",
            "quantity": str(quantity).strip(),
            "order_id": order_id,
            "status": "accepted",
            "message": "Testnet order accepted; check the Bybit testnet account for fill status.",
        }


def bybit_testnet_snapshot() -> dict[str, object]:
    config = BybitTestnetConfig.from_environment()
    if config is None:
        return {
            "configured": False,
            "verified": False,
            "environment": "TESTNET",
            "order_submission_enabled": False,
            "message": "Configure a dedicated BYBIT_TESTNET_API_KEY and BYBIT_TESTNET_API_SECRET to connect.",
        }
    try:
        return BybitTestnetAccount(config).snapshot()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        return {
            "configured": True,
            "verified": False,
            "environment": "TESTNET",
            "order_submission_enabled": False,
            "message": f"Bybit testnet verification failed: {exc}",
        }


class BrokerRequestStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def _read(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read saved broker requests: {exc}") from exc
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            raise RuntimeError("Saved broker requests have an invalid format.")
        return content

    def list_requests(self) -> list[dict[str, str]]:
        with self._lock:
            return self._read()

    def add_request(self, broker_name: str, mode: str, notes: str = "") -> dict[str, str]:
        name = broker_name.strip()
        request_mode = mode.strip().lower()
        note = notes.strip()
        if not REQUEST_ID_PATTERN.fullmatch(name):
            raise ValueError("Enter a broker name with 1 to 80 letters, numbers, spaces, dots, or hyphens.")
        if request_mode not in REQUEST_MODES:
            raise ValueError("Choose read-only, testnet, or live as the requested connection mode.")
        if len(note) > 500:
            raise ValueError("Request notes must be 500 characters or fewer.")
        request = {
            "id": uuid.uuid4().hex,
            "broker_name": name,
            "requested_mode": request_mode,
            "notes": note,
            "status": "Requested locally",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            requests = self._read()
            requests.insert(0, request)
            requests = requests[:100]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                temporary.write_text(json.dumps(requests, indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
            except OSError as exc:
                raise RuntimeError(f"Could not save broker request: {exc}") from exc
        return request


def broker_directory_payload(request_store: BrokerRequestStore) -> dict[str, object]:
    testnet = bybit_testnet_snapshot()
    brokers: list[dict[str, object]] = []
    for broker in BROKER_DIRECTORY:
        item: dict[str, object] = {
            "id": broker["id"],
            "name": broker["name"],
            "category": broker["category"],
            "connection": broker["connection"],
        }
        if item["id"] == "bybit":
            item["account"] = testnet
        brokers.append(item)
    return {"brokers": brokers, "requests": request_store.list_requests()}
