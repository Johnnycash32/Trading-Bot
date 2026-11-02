import json
import tempfile
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from trading_bot import server
from trading_bot.market_data import Candle
from trading_bot.paper_session import PaperExecutionSession, daily_profit_target_percent
from trading_bot.strategy import MovingAverageCrossStrategy


def signal_candles() -> list[Candle]:
    closes = [100, 100, 100, 100, 80, 80, 80, 110]
    return [
        Candle(index * 60_000, value - 0.5, value + 1, value - 1, value, 100)
        for index, value in enumerate(closes)
    ]


def mark_daily_profit(session: PaperExecutionSession, start_balance: float, realized_pnl: float) -> None:
    now = datetime.now(timezone.utc)
    session.account_equity = start_balance + realized_pnl
    session.history.append({
        "order_id": "closed-today",
        "symbol": "BTCUSDT",
        "status": "closed",
        "closed_at": now.isoformat(),
        "realized_pnl": realized_pnl,
    })


class DailyProfitPolicyTests(unittest.TestCase):
    def test_target_rate_progresses_to_thirty_percent_and_caps(self) -> None:
        self.assertEqual(daily_profit_target_percent(50), 10.0)
        self.assertEqual(daily_profit_target_percent(100), 10.0)
        self.assertEqual(daily_profit_target_percent(5_050), 12.5)
        self.assertEqual(daily_profit_target_percent(10_000), 15.0)
        self.assertEqual(daily_profit_target_percent(15_000), 20.0)
        self.assertEqual(daily_profit_target_percent(20_000), 25.0)
        self.assertEqual(daily_profit_target_percent(25_000), 27.5)
        self.assertEqual(daily_profit_target_percent(30_000), 30.0)
        self.assertEqual(daily_profit_target_percent(1_000_000), 30.0)

    def test_daily_target_uses_net_realized_pnl_and_start_of_day_balance(self) -> None:
        session = PaperExecutionSession(account_equity=101)
        mark_daily_profit(session, 100, 10)

        status = session.daily_profit_status()

        self.assertEqual(status["starting_balance"], 100)
        self.assertEqual(status["target_percent"], 10.0)
        self.assertEqual(status["target_amount"], 10.0)
        self.assertTrue(status["target_met"])
        self.assertFalse(status["guaranteed"])

    def test_target_lock_cancels_preexisting_pending_orders_and_blocks_strategy_entries(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        pending = session.submit_manual_order(
            "bybit", "SOLUSDT", "long", 0.01, 100, 99, 102, 1, 60_000, "limit"
        )
        mark_daily_profit(session, 10_000, 1_600)

        result = session.evaluate("bybit", "ETHUSDT", signal_candles(), 60_000)

        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["code"], "daily_profit_target")
        self.assertEqual(session.orders[0]["status"], "cancelled")
        self.assertFalse(any(order["status"] == "simulated" for order in session.orders))
        self.assertTrue(any(event["type"] == "daily_profit_target_reached" for event in session.events))
        self.assertNotEqual(pending["status"], session.orders[0]["status"])

    def test_target_lock_stays_active_for_the_utc_day_after_later_manual_losses(self) -> None:
        session = PaperExecutionSession()
        mark_daily_profit(session, 1_000, 120)
        session.apply_daily_profit_lock()
        session.account_equity -= 30
        session.history.append({
            "order_id": "later-manual-loss",
            "symbol": "ETHUSDT",
            "status": "closed",
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "realized_pnl": -30,
        })

        status = session.daily_profit_status()

        self.assertFalse(status["currently_at_target"])
        self.assertTrue(status["target_met"])
        self.assertEqual(status["target_reached_pnl"], 120)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper-state.json"
            session.save(path)
            restored = PaperExecutionSession.load(path)
        self.assertTrue(restored.daily_profit_status()["target_met"])

    def test_manual_order_after_target_requires_stricter_rr_and_is_risk_capped(self) -> None:
        session = PaperExecutionSession()
        mark_daily_profit(session, 1_000, 120)

        with self.assertRaisesRegex(ValueError, "require at least 2.00 risk/reward"):
            session.submit_manual_order(
                "bybit", "ETHUSDT", "long", 0.01, 100, 99, 101.5, 1, 60_000
            )

        with self.assertRaisesRegex(ValueError, "Daily profit target reached; large lot rejected"):
            session.submit_manual_order(
                "bybit", "ETHUSDT", "long", 1.2, 100, 99, 103, 1, 60_000
            )

        order = session.submit_manual_order(
            "bybit", "ETHUSDT", "long", 0.01, 100, 99, 103, 1, 60_000
        )

        self.assertEqual(order["daily_target_mode"], "post_target_strict")
        risk_reward = order["risk_reward_ratio"]
        if not isinstance(risk_reward, (int, float)):
            self.fail("Risk/reward ratio should be numeric.")
        self.assertGreaterEqual(risk_reward, 2.0)
        limits = session.daily_profit_status()["manual_risk_limits"]
        if not isinstance(limits, dict):
            self.fail("Post-target risk limits should be available.")
        risk_per_trade = limits.get("risk_per_trade_percent")
        if not isinstance(risk_per_trade, (int, float)):
            self.fail("Post-target risk-per-trade limit should be numeric.")
        self.assertEqual(risk_per_trade, 0.25)

    def test_auto_copy_cannot_be_reenabled_after_target_is_met(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = PaperExecutionSession()
        mark_daily_profit(self.session, 100, 10)
        self.control = {"auto_trade_enabled": True, "close_request": False, "last_action": ""}
        patches = [
            patch.object(server, "PAPER_SESSION", self.session),
            patch.object(server, "PAPER_STATE_FILE", Path(self.temporary.name) / "paper-state.json"),
            patch.object(server, "COPY_CONTROL", self.control),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        thread = Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http_server.server_close)
        self.addCleanup(http_server.shutdown)
        self.addCleanup(thread.join, 2)

        request = Request(
            f"http://127.0.0.1:{http_server.server_port}/api/copy/control",
            data=json.dumps({"enabled": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            response = exc
        with response:
            body = json.loads(response.read())

        self.assertEqual(response.status, 409)
        self.assertFalse(self.control["auto_trade_enabled"])
        self.assertTrue(body["daily_profit"]["target_met"])


if __name__ == "__main__":
    unittest.main()
