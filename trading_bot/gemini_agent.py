from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .market_data import Candle, Ticker


DEFAULT_MODEL = "gemini-2.5-flash"
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
JsonRequester = Callable[[Request, float], dict[str, Any]]


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
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
        if not separator or name.strip() not in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL"}:
            continue
        value = raw_value.strip()
        if value.startswith(("'", '"')):
            closing_quote = value.find(value[0], 1)
            if closing_quote != -1:
                value = value[1:closing_quote]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[name.strip()] = value
    return values


def gemini_config(
    *,
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> tuple[str | None, str]:
    environment = os.environ if env is None else env
    file_values = _read_dotenv(dotenv_path or Path(__file__).resolve().parents[1] / ".env")
    api_key = environment.get("GEMINI_API_KEY") or environment.get("GOOGLE_API_KEY")
    api_key = api_key or file_values.get("GEMINI_API_KEY") or file_values.get("GOOGLE_API_KEY")
    model = environment.get("GEMINI_MODEL") or file_values.get("GEMINI_MODEL") or DEFAULT_MODEL
    if not MODEL_PATTERN.fullmatch(model):
        raise ValueError("GEMINI_MODEL contains unsupported characters.")
    return (api_key.strip() if api_key and api_key.strip() else None), model


def _request_json(request: Request, timeout: float) -> dict[str, Any]:
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Gemini returned an invalid response.")
    return payload


class GeminiAnalysisAgent:
    """On-demand public-market analysis; this agent cannot place or approve orders."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        requester: JsonRequester = _request_json,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A Gemini API key is required.")
        if not MODEL_PATTERN.fullmatch(model):
            raise ValueError("GEMINI_MODEL contains unsupported characters.")
        self._api_key = api_key
        self.model = model
        self._requester = requester

    def analyze(
        self,
        *,
        exchange: str,
        symbol: str,
        interval: str,
        ticker: Ticker,
        candles: list[Candle],
    ) -> str:
        recent_candles = candles[-80:]
        candle_rows = [
            {
                "timestamp_ms": candle.timestamp_ms,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in recent_candles
        ]
        prompt = (
            "You are an on-demand market research assistant for a paper-only trading dashboard. "
            "Analyze the public market data below. Summarize the observed short-term trend, notable "
            "volatility or limitations, and what evidence would invalidate the observation. "
            "Do not give personalized financial advice, claim profitability, or instruct the app to trade. "
            "State clearly that this is descriptive analysis, not a trading signal or order approval.\n\n"
            f"Exchange: {exchange}\nSymbol: {symbol}\nInterval: {interval}\n"
            f"Last price: {ticker.last}\n24h change percent: {ticker.change_percent}\n"
            f"Public candles, oldest to newest (up to {len(candle_rows)}):\n"
            f"{json.dumps(candle_rows, separators=(',', ':'), allow_nan=False)}"
        )
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps({
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
                "User-Agent": "trading-bot-paper/0.1",
            },
            method="POST",
        )
        try:
            payload = self._requester(request, 30)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise RuntimeError("Gemini rejected the API key or it lacks permission. Check GEMINI_API_KEY in .env.") from None
            if exc.code == 429:
                raise RuntimeError("Gemini request quota or rate limit was reached.") from None
            raise RuntimeError(f"Gemini request failed with HTTP {exc.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise RuntimeError("Could not connect to Gemini. Check network connectivity and try again.") from None
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise RuntimeError("Gemini returned an invalid response.") from None

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("Gemini returned no analysis. Check model availability and API access.")
        content = candidates[0].get("content", {}) if isinstance(candidates[0], dict) else {}
        parts = content.get("parts", []) if isinstance(content, dict) else []
        text = "\n".join(
            part["text"].strip()
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
        )
        if not text:
            raise RuntimeError("Gemini returned no readable analysis.")
        return text
