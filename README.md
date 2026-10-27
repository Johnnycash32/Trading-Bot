# Trading Bot

A safety-first foundation for a mobile- and desktop-compatible trading application for Bybit and Binance futures.

## Current status

This first implementation is deliberately paper-only. It provides:

- A shared exchange interface for Bybit and Binance adapters.
- Explicit paper-trading models.
- Independent risk validation and conservative limits.
- Position sizing based on account equity and stop distance.
- Read-only Bybit and Binance ticker/candle adapters.
- Optional public WebSocket candle transport with heartbeat/reconnect behavior; REST remains the fallback.
- A transparent moving-average crossover backtest with fees and slippage.
- An explainable behavioral decision layer with evidence, signal, chart, risk, paper-execution, and notification agents.
- Approval-aware paper behavior for large trades, auto-trading-off state, duplicate signals, position capacity, and risk gates.
- An active-copy tab showing synthetic lead accounts, positions, entry/mark prices, size, unrealized P&L, and stop/target levels.
- A stateful auto-copy control: turning it off pauses new copies and creates a close-request event while monitoring remains active.
- Optional read-only Bybit private-position monitoring with Lion-policy filtering; no live orders, transfers, withdrawals, or wallet scraping.

The existing [Trading Bot.html](../Trading%20Bot.html) is only a visual concept. The Python package in this folder is the beginning of the real application core.

Paper session orders, positions, closed trades, balance changes, and activity events are saved locally in `.paper-state.json` so they survive a server restart. This file is ignored by Git. A new session starts with the configured starting balance and no fabricated sample trades; its history fills only as paper evaluations create or close positions.

Paper position management checks each candle's high and low so a target can close a simulated trade even if the candle later retraces before its close. When a trade moves favorably, a stop is advanced to lock a small net profit after estimated fees and slippage. Stop/target ambiguity within one candle is resolved conservatively in favor of the stop. Fresh moving-average crossovers and confirmed fast-average pullback recoveries can create entries, with one entry per symbol per candle, one open position per symbol, and the daily-loss and position-size risk gates still enforced. This is paper research behavior, not a profit guarantee.

The Paper Trading Positions tab provides per-position and close-all controls. A manual paper close requires an exchange-timestamped quote no more than five seconds old. It prefers the position's exchange and falls back between Bybit and Binance linear futures; it uses the best bid to close a long and the best ask to close a short. A stale, missing, invalid, or unavailable quote refuses the close instead of using a saved mark. The selected quote source and timestamp are recorded in trade history and activity. Displayed unrealized P&L remains based on the most recent paper candle mark and may lag live prices; the UI labels this distinction. Manual close requests are disabled and server-rejected while auto-copy is enabled; disabling auto-copy pauses new copies without discarding open paper positions, which can then be closed manually. This is paper simulation only and is not an exchange order.

The chart requests candles independently of ticker quotes. If its selected venue fails, it tries the other supported futures venue and translates the selected timeframe to that venue's interval format. A ticker failure does not discard successfully loaded candles; the last candle close is shown with an explicit unavailable-ticker label. Successfully loaded chart data is cached in the browser and can be displayed during a later outage with a prominent saved/not-live warning. Cached chart data is display-only and is never used for trade evaluation or manual closes. If no exchange has supplied candles yet and no browser cache exists, the chart cannot be populated until a feed is reachable; the app reports that condition rather than fabricating market data.

## Optional Gemini agent

The dashboard offers an on-demand Gemini market-analysis agent when `GEMINI_API_KEY` is present in the local `.env` file. The key stays on the server and is sent to Google's API in an HTTP header, never returned to the browser or written to paper activity. When you explicitly select Gemini and run a review, only the selected exchange, symbol, interval, public ticker, and recent public candles are sent. Reviews are descriptive only; they cannot place, approve, or trigger paper or live orders. The built-in local-agent review is read-only and does not require an API key.

Set `GEMINI_MODEL` in `.env` to override the default `gemini-2.5-flash` model. Restart the server after changing the key or model.

## Safety boundary

An 80% historical win rate may be used as a research filter, but it is not a promise of future profit and cannot be treated as a probability without calibration. Every candidate must be evaluated after fees, funding, spread, slippage, drawdown, sample size, and out-of-sample performance.

The application will remain in paper mode until:

1. Backtests and unit tests pass.
2. Bybit demo and Binance testnet adapters are independently verified.
3. Reconciliation, protective orders, rate limits, and reconnect behavior are tested.
4. Live mode is explicitly enabled by the owner with least-privilege API keys.

Never grant withdrawal permission to trading API keys. Never put secrets in frontend code or commit them to GitHub.

## Optional private Bybit paper monitor

The private monitor uses only official authenticated read endpoints. It requires a separate Bybit API key configured as read-only, with no order, transfer, or withdrawal permissions. Set these variables locally; do not commit them:

~~~powershell
$env:BYBIT_READONLY_API_KEY = "your-read-only-key"
$env:BYBIT_READONLY_API_SECRET = "your-read-only-secret"
~~~

The dashboard reads private open positions, marks them against public prices, applies the Lion risk policy, and displays only approved positions as paper-copy candidates. It never sends an order. If the key is missing, not read-only, expired, or unavailable, the monitor remains disabled or reports the error without exposing the secret.

## Run the paper demo

From this folder:

~~~powershell
C:/Users/Hp/AppData/Roaming/uv/python/cpython-3.15.1-windows-x86_64-none/python.exe -m trading_bot.main
~~~

Run the tests:

~~~powershell
C:/Users/Hp/AppData/Roaming/uv/python/cpython-3.15.1-windows-x86_64-none/python.exe -m unittest discover -s tests -v
~~~

## Planned layers

1. Core models and risk engine — complete.
2. Public market-data adapters for Bybit and Binance — complete.
3. Backtesting with realistic costs — current phase.
4. Copy-trading research controls: fixed amount/ratio sizing, symbol allowlists, slippage caps, independent TP/SL, and portfolio stop-loss.
5. Demo/testnet account adapters, reconciliation, and audit logs.
6. FastAPI service for phone and computer clients.
7. React/TypeScript responsive dashboard with a separate Three.js/R3F animal-agent scene.
8. Manual approvals, live mode, and exchange-specific protective-order validation.

## Behavioral decision contract

Every paper evaluation returns a decision from the behavioral layer:

- `monitor`: no actionable signal; continue observing.
- `reject`: a signal exists, but evidence or a hard risk gate failed.
- `paper_simulate`: the signal passed the paper gates and may be simulated; it still requires approval for any future live mode.

The animal roles are intentionally observable: Raccoon collects public evidence, Eagle dispatches the strategy signal, Hawk examines chart structure, Lion enforces limits, Cheetah represents paper execution, and Hare delivers the decision. Confidence is a transparent research score, not a probability of profit.

Hare's bell in the dashboard header opens a compact, scrollable news and activity panel. Its labeled Corner alerts switch turns brief browser pop-up alerts on or off; off is muted gray and on is green, and the switch choice is saved in the browser. The in-panel news and activity feeds remain available either way. Hare dispatches notable paper lifecycle, risk rejection, and agent-failure events; routine monitor and duplicate-signal entries are grouped to keep the feed readable. Activity rows open their full event details, and the unread badge clears when the panel is opened.

## Research references

- [Bybit V5 API](https://bybit-exchange.github.io/docs/v5/intro)
- [Bybit demo trading](https://bybit-exchange.github.io/docs/v5/demo)
- [Bybit create order](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [Binance derivatives API catalogue](https://developers.binance.com/en/docs/catalog)
- [CCXT documentation](https://docs.ccxt.com/)
- [Freqtrade documentation](https://github.com/freqtrade/freqtrade/tree/develop/docs)
