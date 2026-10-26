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
- No API keys, private endpoints, live orders, wallet scraping, or exchange credentials.

The existing [Trading Bot.html](../Trading%20Bot.html) is only a visual concept. The Python package in this folder is the beginning of the real application core.

## Safety boundary

An 80% historical win rate may be used as a research filter, but it is not a promise of future profit and cannot be treated as a probability without calibration. Every candidate must be evaluated after fees, funding, spread, slippage, drawdown, sample size, and out-of-sample performance.

The application will remain in paper mode until:

1. Backtests and unit tests pass.
2. Bybit demo and Binance testnet adapters are independently verified.
3. Reconciliation, protective orders, rate limits, and reconnect behavior are tested.
4. Live mode is explicitly enabled by the owner with least-privilege API keys.

Never grant withdrawal permission to trading API keys. Never put secrets in frontend code or commit them to GitHub.

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
4. Demo/testnet account adapters, reconciliation, and audit logs.
5. FastAPI service for phone and computer clients.
6. React/TypeScript responsive dashboard with a separate Three.js/R3F animal-agent scene.
7. Manual approvals, live mode, and exchange-specific protective-order validation.

## Research references

- [Bybit V5 API](https://bybit-exchange.github.io/docs/v5/intro)
- [Bybit demo trading](https://bybit-exchange.github.io/docs/v5/demo)
- [Bybit create order](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [Binance derivatives API catalogue](https://developers.binance.com/en/docs/catalog)
- [CCXT documentation](https://docs.ccxt.com/)
- [Freqtrade documentation](https://github.com/freqtrade/freqtrade/tree/develop/docs)
