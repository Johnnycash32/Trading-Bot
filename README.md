# A.Y.O. — Artificial Yielding Operative

AI Trading & Agent Command Centre: a safety-first, paper-first application coordinating market research, agents, risk analysis, and trading workflows.

## Current status

This is a paper-first application; production/live broker order routing is not enabled. It provides:

- A shared exchange interface for Bybit and Binance adapters.
- Explicit paper-trading models.
- Independent risk validation and conservative limits.
- Position sizing based on account equity and stop distance.
- Read-only Bybit and Binance futures plus Coinbase spot ticker/candle adapters.
- Optional public WebSocket candle transport with heartbeat/reconnect behavior; REST remains the fallback.
- A transparent moving-average crossover backtest with fees and slippage.
- An explainable behavioral decision layer with evidence, signal, chart, risk, paper-execution, and notification agents.
- Approval-aware paper behavior for large trades, auto-trading-off state, duplicate signals, position capacity, and risk gates.
- A public lead monitor that discovers Hyperliquid wallet addresses and displays individual fills from its user-specific public feed; the separate copy-research lab still uses clearly labeled synthetic examples.
- A paper-only Hyperliquid adapter with REST backfill, WebSocket monitoring, position-transition inference, reconnection, and fill deduplication; automatic copying is disabled until events are safely integrated with paper risk sizing.
- Optional read-only Bybit private-position monitoring with Lion-policy filtering; no live orders, transfers, withdrawals, or wallet scraping.
- A broker directory with locally saved support requests and an explicitly confirmed Bybit testnet-only market-order path. Testnet orders use a dedicated testnet API key and never target the production API.

The existing [Trading Bot.html](../Trading%20Bot.html) is only a visual concept. The Python package in this folder is the beginning of the real application core.

## Public trader monitoring

The Public lead monitor ranks up to 20 addresses by monthly P&L from Hyperliquid's public leaderboard at `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`. That leaderboard endpoint is undocumented; discovery results are labeled accordingly and are not treated as proof of a trader's real-world identity. Hyperliquid returned a "Cannot track more than 15 total users" error when 20 wallets were monitored across multiple WebSocket connections, so only the top 15 ranked wallets are subscribed; the remaining discovered wallets are visibly marked as not monitored. The adapter rejects attempts to subscribe to more than 15 users rather than retrying around the limit. Individual fills are collected from Hyperliquid's documented public `userFills` WebSocket subscription, with `userFillsByTime` REST backfill. Events are deduplicated using wallet address, timestamp, instrument, and trade ID. The UI distinguishes REST backfill, WebSocket snapshots, and new WebSocket events; fills without `startPosition` are not assigned an inferred position transition.

The adapter exposes verified source data for paper analysis only. A verified fill can be used to prepare an editable paper-ticket draft with a fresh destination-market quote, but the lead's quantity and risk levels are deliberately not copied; the user must enter their own quantity, stop, and target and submit manually. It does not submit orders, and the server rejects attempts to enable automatic copying. Fill streams do not provide a complete order lifecycle: stop/target changes, order completion, and partial-fill order status are not inferred. Hyperliquid documentation: [WebSocket subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) and [Info endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint). No other exchange's public copy-trader data is currently integrated; an exchange's market-wide trade feed must not be presented as trader-specific activity.

## Market data coverage

Public crypto adapters provide Bybit linear-futures, Binance USD-M futures, and Coinbase USD-spot data. An optional Twelve Data adapter can display mapped Forex, stock, index, commodity, and crypto symbols when a server-side API key and a plan covering the instrument, real-time/delayed status, and intended use are available. The catalog is not proof of feed coverage: exchange/provider symbol availability, plan entitlements, market hours, quotas, and licensing still apply. The adapter currently maps spot gold and silver (`XAUUSD`, `XAGUSD`); commodity futures such as `CL1!` and `GC1!` intentionally fail with a visible unsupported-mapping message. No feed is used to evaluate paper trades except the explicitly supported Bybit/Binance perpetual markets.

To configure Twelve Data, add this line to the ignored local `.env` file and restart the app:

~~~dotenv
TWELVE_DATA_API_KEY=your-provider-api-key
~~~

The key is read by the Python server only, is not returned by API endpoints, and must never be pasted into the dashboard, chat, source code, or a commit. Twelve Data chart refresh is throttled to two minutes and its watchlist to five minutes to limit requests; this does not guarantee real-time data or compliance with a particular plan. Its published pricing distinguishes internal non-display access from internal display and business/external-display rights: do not use the free Basic plan for this visible chart unless Twelve Data confirms your use is permitted. Confirm the provider contract, market coverage, request allowance, and data delay for the intended personal or business use before deploying. Chart access does not grant trading permission: paper execution remains restricted to supported crypto perpetuals with separately enforced risk controls. See [Twelve Data pricing and licensing tiers](https://twelvedata.com/pricing.md).

TradingView's published widget documentation describes display widgets, not an API for exporting their underlying market data. TradingView data must not be reverse-engineered or used for automated trading decisions. Official references: [TradingView policies](https://www.tradingview.com/policies/), [widget data FAQ](https://www.tradingview.com/widget-docs/faq/data/), and [widget FAQ](https://www.tradingview.com/widget-docs/faq/general/). A TradingView widget may be embedded only under its terms and does not replace a properly licensed server-side feed.

Paper session orders, positions, closed trades, balance changes, and activity events are saved locally in `.paper-state.json` so they survive a server restart. This file is ignored by Git. A new session starts with the configured starting balance and no fabricated sample trades; its history fills only as paper evaluations create or close positions.

Paper position management checks each candle's high and low so a target can close a simulated trade even if the candle later retraces before its close. When a trade moves favorably, a stop is advanced to lock a small net profit after estimated fees and slippage. Stop/target ambiguity within one candle is resolved conservatively in favor of the stop. Fresh moving-average crossovers and confirmed fast-average pullback recoveries can create entries, with one entry per symbol per candle, one open position per symbol, and the daily-loss and position-size risk gates still enforced. This is paper research behavior, not a profit guarantee.

Paper evaluation persists the last processed candle per symbol and replays every newly available candle in chronological order, so a stop crossed between dashboard polls is not skipped. If candles needed to manage an open position are missing, the position is marked as monitoring-halted, no simulated exit is fabricated, and new paper entries are blocked across symbols. The warning remains until the affected position is manually reconciled or closed; historical chart requests cannot trigger paper evaluation. This replay is only a paper simulation and does not provide exchange-side protection during a process, network, or data outage.

The Paper Trading Positions tab provides per-position and close-all controls. A manual paper close requires an exchange-timestamped quote no more than five seconds old. It prefers the position's exchange and falls back between Bybit and Binance linear futures; it uses the best bid to close a long and the best ask to close a short. A stale, missing, invalid, or unavailable quote refuses the close instead of using a saved mark. The selected quote source and timestamp are recorded in trade history and activity. Displayed unrealized P&L remains based on the most recent paper candle mark and may lag live prices; the UI labels this distinction. Manual close requests are server-rejected while auto-copy is enabled; the server currently rejects attempts to enable auto-copy, so monitoring does not prevent manual closes. This is paper simulation only and is not an exchange order.

The paper session also has a daily realized-profit target lock. Its target rate is fixed at 10.00% for balances up to $100, then increases linearly to 15.00% at $10,000, 25.00% at $20,000, and 30.00% at $30,000; it remains capped at 30.00% above that balance. These are aggressive, aspirational paper targets, not realistic expectations or guaranteed returns, and must never be used to justify increasing lot size or bypassing risk controls. The target amount is calculated from the UTC day's starting paper balance and net realized paper P&L; unrealized gains do not count. When reached, the lock pauses strategy entries, disables auto-copy, cancels pre-existing pending paper entries, and stays in force for the rest of that UTC day even if later manual losses reduce realized P&L. Existing positions remain managed by the paper engine. The ticket warns that pursuing more profit will not raise the day's target. Manual paper entries remain available under stricter limits: 0.25% risk per trade, 0.5% daily loss limit, at most two open/pending entries, 1x leverage, 10% notional cap, and a minimum 2.0 net risk/reward ratio. Orders larger than the risk-approved quantity are rejected before placement. These are simulation safeguards, not a way to make trading risk-free. A network/data failure still pauses or rejects actions that require fresh data rather than fabricating a fill.

The profit lock is currently tied only to this local paper account. The separate, explicitly confirmed Bybit testnet connector has no automatic trading and does not share the paper account's balance or P&L. Live real-money routing remains disabled. Any future live feature must first implement account-specific UTC-day starting equity, verified realized P&L, exchange-side protective orders, and fail-closed behavior before applying a daily target policy.

The chart uses public Bybit/Binance futures candles, Coinbase USD-spot candles, or the optional Twelve Data feed described above; its label identifies the source and market type. It does not use or reverse-engineer TradingView's proprietary chart services. The chart renders candle wicks/bodies, numeric price and UTC date/time axes, volume bars only when the feed reports volume, and locally calculated indicators (including Stoch RSI and daily-reset VWAP). A market that does not report volume is labeled as such instead of displaying invented zero-volume bars. Indicator values use the displayed candle history, not TradingView data. A ticker failure does not discard successfully loaded candles; the last candle close is shown with an explicit unavailable-ticker label. Successfully loaded chart data is cached in the browser and can be displayed during a later outage with a prominent saved/not-live warning. Cached chart data is display-only and is never used for trade evaluation or manual closes. If no exchange has supplied candles yet and no browser cache exists, the chart cannot be populated until a feed is reachable; the app reports that condition rather than fabricating market data.

The public lead monitor uses clearly labeled synthetic demo positions, not real exchange trader accounts. It refreshes marks from public exchange tickers, hides P&L when timestamps are stale or unavailable, and places the public and private monitors side by side on wider screens. “Draft in ticket” fetches a fresh executable-feed chart quote and pre-fills a one-times, editable paper ticket with stop/target offsets scaled from the sample position. It never submits an order; a person must inspect the values and explicitly submit the separate paper ticket. Synthetic demo profiles must not be interpreted as verified trader performance or copy recommendations.

The chart's date control displays the current UTC day in `YYYY-MM-DD` while remaining in live mode until a date is explicitly selected. Zoom-in, zoom-out, and reset controls change the visible candle window without changing the candles used by paper evaluation. A bottom navigation bar links to paper trading, public-copy research/positions, the private read-only monitor, and broker connections; unavailable API-backed sections now show their error in place rather than disappearing.

After a newly observed losing paper close, the dashboard scans other supported crypto pairs on the selected Bybit or Binance perpetual feed. The scan uses fully closed candles, the existing strategy, and the same paper risk/behavior gates on an isolated preview session; it does not create preview orders in saved account state. The dashboard switches to the eligible candidate with the highest behavior-engine confidence score and resumes paper evaluation. That score is a heuristic, not a calibrated probability or guarantee. If no candidate passes, automatic paper entries remain paused until a coin is manually selected. This recovery workflow is paper-only and cannot prevent losses or provide exchange-side protection.

The chart-side trade ticket is paper-only. It supports immediate market simulations and candle-triggered limit simulations, editable quantity, leverage (within the risk engine's limit), stop loss, and take profit. Market entries require a fresh executable-side quote; limit fills are simulated at the limit price only after an available candle touches it. The server rejects quantities above the existing stop-based risk allowance. Pending limits can be loaded into the ticket, cancelled, edited, and submitted again. Missing candle history halts pending orders for review. These simulated stops and targets are not exchange-hosted orders, and no ticket action routes to a broker.

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

## Broker directory and Bybit testnet

The Broker connections panel lists the brokers visible in the supplied TradingView screenshots, including FxPro, Bybit, Pepperstone, Kraken, Binance, and FP Markets. The Bybit entry can verify a dedicated **Bybit testnet** account and show its USDT equity. If its API key has trading permission, the panel also exposes a manually confirmed market-order form that submits only to Bybit's fixed testnet API URL. Live trading is not implemented or enabled.

The app's **Step-by-step Bybit testnet setup** button walks through creating a testnet account, choosing the intended account/subaccount, creating a dedicated testnet API key, saving it locally, and verifying the connection. It copies only a placeholder template; it never asks you to enter a key in the browser.

For the account you intend to use, create a Bybit **testnet** API key from that exact testnet account/subaccount, with only the permissions needed for account verification and (if desired) test orders. Never grant withdrawals, transfers, or wallet permissions. Add the values directly to the ignored local `.env` file in VS Code:

~~~dotenv
BYBIT_TESTNET_API_KEY=your-testnet-api-key
BYBIT_TESTNET_API_SECRET=your-testnet-api-secret
BYBIT_TESTNET_ACCOUNT_LABEL=My testnet subaccount
~~~

The app reads `.env` during each connection check, so a restart is normally unnecessary after saving. The app does not create, fund, or move money between wallets; the API key must belong to the desired broker account. The testnet connector has no production URL setting and never uses the separate `BYBIT_READONLY_API_KEY` credentials. `.env` is plain text protected by your Windows account; do not share, sync, screenshot, or commit it. Revoke the API key at Bybit if the file or computer may have been exposed.

“Request support” accepts any broker name and saves the request locally in the ignored `.broker-requests.json` file. Requests are not sent to TradingView, the broker, or a remote service. A saved request does not connect an account or enable live trading; each broker needs its own separately implemented and validated adapter.

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
