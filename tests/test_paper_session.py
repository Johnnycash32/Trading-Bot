from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from datetime import datetime, timezone

from trading_bot.behavior import BehaviorEngine, BehaviorPolicy
from trading_bot.market_data import Candle
from trading_bot.paper_session import PaperExecutionSession
from trading_bot.strategy import MovingAverageCrossStrategy


def candles(closes: list[float]) -> list[Candle]:
    return [Candle(index * 60_000, value - 0.5, value + 1, value - 1, value, 100) for index, value in enumerate(closes)]


def next_candle(previous: Candle, *, open_price: float, high: float, low: float, close: float) -> Candle:
    return Candle(previous.timestamp_ms + 60_000, open_price, high, low, close, 100)


class PaperSessionTests(unittest.TestCase):
    def test_insufficient_history_holds(self) -> None:
        result = PaperExecutionSession().evaluate("bybit", "SOLUSDT", candles([100, 101]))
        self.assertEqual(result["status"], "hold")

    def test_crossover_is_filtered_when_slow_average_confirms_the_opposite_trend(self) -> None:
        strategy = MovingAverageCrossStrategy(2, 4)

        signal = strategy.signal(candles([100, 80, 70, 60, 65, 68]))

        self.assertEqual(signal.value, "hold")

    def test_crossover_creates_simulated_order(self) -> None:
        values = [100, 100, 100, 100, 80, 80, 80, 110]
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        result = session.evaluate("bybit", "SOLUSDT", candles(values))
        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["order"]["status"], "simulated")
        self.assertEqual(len(session.orders), 1)

    def test_behavior_daily_loss_gate_blocks_paper_entry(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        session.behavior_engine = BehaviorEngine(BehaviorPolicy(max_daily_loss_percent=1))
        session.history.append({
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "realized_pnl": -150,
            "status": "closed",
        })

        result = session.evaluate("bybit", "SOLUSDT", candles([100, 100, 100, 100, 80, 80, 80, 110]))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["code"], "behavior_gate")
        self.assertEqual(session.orders, [])
        self.assertEqual(session.positions, [])

    def test_manual_market_ticket_creates_risk_checked_paper_position(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))

        order = session.submit_manual_order(
            "bybit", "SOLUSDT", "long", 0.01, 100, 99.5, 101, 1, 60_000, "market"
        )

        self.assertEqual(order["status"], "simulated")
        self.assertEqual(order["signal_source"], "manual_paper_ticket")
        self.assertEqual(session.positions[0]["quantity"], 0.01)
        self.assertEqual(session.positions[0]["opened_candle_timestamp_ms"], 60_000)

    def test_manual_ticket_rejects_quantity_above_risk_approved_maximum(self) -> None:
        session = PaperExecutionSession()

        with self.assertRaisesRegex(ValueError, "risk-approved maximum"):
            session.submit_manual_order(
                "bybit", "SOLUSDT", "long", 10_000, 100, 99.5, 101, 1, 60_000
            )

        self.assertEqual(session.orders, [])
        self.assertEqual(session.positions, [])

    def test_limit_ticket_waits_for_price_touch_then_fills_on_candle(self) -> None:
        session = PaperExecutionSession()
        pending = session.submit_manual_order(
            "bybit", "SOLUSDT", "long", 0.01, 100, 99, 102, 1, 60_000, "limit"
        )
        previous = candles([100, 101])[-1]
        fill = Candle(120_000, 101, 102, 99, 100.5, 75)

        result = session.evaluate("bybit", "SOLUSDT", [previous, fill])

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(session.orders[0]["status"], "simulated")
        self.assertEqual(session.orders[0]["filled_candle_timestamp_ms"], fill.timestamp_ms)
        self.assertEqual(session.positions[0]["entry_price"], 100)
        self.assertEqual(result["open_positions"][0]["status"], "open")

    def test_pending_limit_ticket_can_be_cancelled(self) -> None:
        session = PaperExecutionSession()
        order = session.submit_manual_order(
            "bybit", "SOLUSDT", "short", 0.01, 100, 101, 98, 1, 60_000, "limit"
        )

        cancelled = session.cancel_pending_order(str(order["order_id"]))

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(session.cancel_pending_order(str(order["order_id"])))
        self.assertEqual(session.positions, [])

    def test_pending_limit_is_halted_when_candle_history_has_a_gap(self) -> None:
        session = PaperExecutionSession()
        order = session.submit_manual_order(
            "bybit", "SOLUSDT", "long", 0.01, 100, 99, 102, 1, 60_000, "limit"
        )
        distant = Candle(240_000, 100, 101, 99, 100, 50)

        result = session.evaluate("bybit", "SOLUSDT", [distant])

        self.assertEqual(result["status"], "halted")
        self.assertTrue(result["monitoring_gap"])
        self.assertEqual(session.orders[0]["status"], "data_gap")
        with self.assertRaisesRegex(ValueError, "unresolved market-data gap"):
            session.submit_manual_order(
                "bybit", "BTCUSDT", "long", 0.001, 100, 99, 102, 1, 240_000
            )
        self.assertEqual(session.cancel_pending_order(str(order["order_id"]))["status"], "cancelled")

    def test_duplicate_signal_is_not_repeated(self) -> None:
        values = [100, 100, 100, 100, 80, 80, 80, 110]
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        session.evaluate("bybit", "SOLUSDT", candles(values))
        result = session.evaluate("bybit", "SOLUSDT", candles(values))
        self.assertEqual(result["status"], "hold")
        self.assertEqual(len(session.orders), 1)

    def test_target_closes_open_paper_position(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry_values = [100, 100, 100, 100, 80, 80, 80, 110]
        entry_candles = candles(entry_values)
        session.evaluate("bybit", "SOLUSDT", entry_candles)
        exit_candles = [*entry_candles, next_candle(entry_candles[-1], open_price=110, high=117, low=109, close=110)]
        result = session.evaluate("bybit", "SOLUSDT", exit_candles)
        self.assertEqual(result["closed_positions"][0]["status"], "closed")
        self.assertGreater(result["equity"], 10_000.0)
        self.assertEqual(result["open_positions"], [])
        history = session.snapshot()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["order_id"], result["closed_positions"][0]["order_id"])
        self.assertEqual(history[0]["status"], "closed")
        self.assertIn("realized_pnl", history[0])
        self.assertEqual(session.orders[0]["status"], "closed")
        self.assertEqual(len(session.balance_history), 2)
        self.assertTrue(any(event["type"] == "position_closed" for event in session.events))

    def test_target_uses_candle_high_even_when_close_retraces(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        reversal = next_candle(entry[-1], open_price=110, high=117, low=109, close=109)

        result = session.evaluate("bybit", "SOLUSDT", [*entry, reversal])

        self.assertEqual(result["closed_positions"][0]["exit_reason"], "take_profit")
        self.assertGreater(result["closed_positions"][0]["realized_pnl"], 0)
        self.assertGreater(result["equity"], 10_000.0)

    def test_trailing_stop_locks_profit_after_favorable_move(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        favorable = next_candle(entry[-1], open_price=110, high=112.3, low=111.8, close=112)

        session.evaluate("bybit", "SOLUSDT", [*entry, favorable])
        position = session.snapshot("SOLUSDT")["open_positions"][0]
        self.assertTrue(position["trailing_stop_active"])
        self.assertGreater(position["stop_loss"], position["entry_price"])

        retrace = next_candle(favorable, open_price=112, high=112.2, low=110, close=111)
        result = session.evaluate("bybit", "SOLUSDT", [*entry, favorable, retrace])

        self.assertEqual(result["closed_positions"][0]["exit_reason"], "trailing_stop")
        self.assertGreater(result["closed_positions"][0]["realized_pnl"], 0)

    def test_stop_wins_if_stop_and_target_are_both_inside_same_candle(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        ambiguous = next_candle(entry[-1], open_price=110, high=117, low=107, close=110)

        result = session.evaluate("bybit", "SOLUSDT", [*entry, ambiguous])

        self.assertEqual(result["closed_positions"][0]["exit_reason"], "stop_loss")
        self.assertLess(result["closed_positions"][0]["realized_pnl"], 0)

    def test_replays_missed_candles_and_closes_at_the_first_crossed_stop(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 120, 120, 120, 90])
        opened = session.evaluate("bybit", "SOLUSDT", entry)
        position = session.positions[0]
        self.assertEqual(opened["order"]["side"], "short")
        position["stop_loss"] = 92.5
        position["take_profit"] = 80
        stop_candle = next_candle(entry[-1], open_price=95, high=96, low=90, close=91)
        recovery_candle = next_candle(stop_candle, open_price=91, high=92, low=89, close=90)

        result = session.evaluate("bybit", "SOLUSDT", [*entry, stop_candle, recovery_candle])

        closed = result["closed_positions"][0]
        self.assertEqual(closed["exit_reason"], "stop_loss")
        self.assertEqual(closed["exit_price"], 95)
        self.assertEqual(closed["closed_candle_timestamp_ms"], stop_candle.timestamp_ms)
        self.assertEqual(session._last_evaluated_candle["SOLUSDT"], recovery_candle.timestamp_ms)

    def test_missing_candles_halt_open_position_without_fabricating_an_exit(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        unavailable_gap = next_candle(
            next_candle(entry[-1], open_price=110, high=111, low=109, close=110),
            open_price=110,
            high=111,
            low=109,
            close=110,
        )

        result = session.evaluate("bybit", "SOLUSDT", [unavailable_gap])

        self.assertEqual(result["status"], "halted")
        self.assertTrue(result["monitoring_gap"])
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertEqual(result["open_positions"][0]["monitoring_status"], "data_gap")
        self.assertFalse(any(event["type"] == "position_closed" for event in session.events))
        self.assertEqual(sum(event["type"] == "market_data_gap" for event in session.events), 1)
        repeated = session.evaluate("bybit", "SOLUSDT", [unavailable_gap])
        self.assertEqual(repeated["status"], "halted")
        self.assertEqual(sum(event["type"] == "market_data_gap" for event in session.events), 1)
        other_symbol_signal = session.evaluate(
            "bybit",
            "BTCUSDT",
            candles([100, 100, 100, 100, 80, 80, 80, 110]),
        )
        self.assertEqual(other_symbol_signal["status"], "halted")
        self.assertEqual(len(session.orders), 1)

    def test_manual_close_updates_order_history_balance_and_activity(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry_values = [100, 100, 100, 100, 80, 80, 80, 110]
        result = session.evaluate("bybit", "SOLUSDT", candles(entry_values))

        closed = session.close_position(result["order"]["order_id"], 112)

        self.assertIsNotNone(closed)
        self.assertEqual(closed["exit_reason"], "manual_close")
        self.assertEqual(session.orders[0]["status"], "closed")
        self.assertEqual(session.snapshot()["history"][0]["order_id"], result["order"]["order_id"])
        self.assertEqual(len(session.snapshot()["balance_history"]), 2)
        self.assertTrue(any(event["exit_reason"] == "manual_close" for event in session.events if event["type"] == "position_closed"))
        self.assertEqual(session.snapshot()["open_positions"], [])

    def test_close_all_positions_closes_every_open_order(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        first = session.evaluate("bybit", "SOLUSDT", candles([100, 100, 100, 100, 80, 80, 80, 110]))
        second = session.evaluate("bybit", "BTCUSDT", candles([100, 100, 100, 100, 80, 80, 80, 110]))

        closed = session.close_all_positions({
            first["order"]["order_id"]: (112, "bybit_bid", 1_700_000_000_000),
            second["order"]["order_id"]: (90, "bybit_bid", 1_700_000_000_000),
        })

        self.assertEqual(len(closed), 2)
        self.assertEqual(session.snapshot()["open_positions"], [])
        self.assertEqual(len(session.snapshot()["history"]), 2)
        self.assertTrue(all(order["status"] == "closed" for order in session.orders))

    def test_existing_positions_without_scalp_fields_remain_manageable(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        position = session.positions[0]
        for key in ("initial_stop_loss", "initial_stop_distance", "opened_candle_timestamp_ms", "trailing_stop_active"):
            position.pop(key)
        current = next_candle(entry[-1], open_price=110, high=111, low=109, close=110)

        closed = session._mark_positions("SOLUSDT", current)

        self.assertEqual(closed, [])
        self.assertIn("initial_stop_distance", position)
        self.assertFalse(position["trailing_stop_active"])

    def test_pullback_signal_allows_same_trend_reentry(self) -> None:
        strategy = MovingAverageCrossStrategy(2, 4)
        signal = strategy.pullback_signal(candles([100, 100, 100, 110, 109, 111]))

        self.assertEqual(signal.value, "long")

    def test_paper_session_reenters_after_profitable_exit_and_pullback_recovery(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        entry = candles([100, 100, 100, 100, 80, 80, 80, 110])
        session.evaluate("bybit", "SOLUSDT", entry)
        profitable_exit = next_candle(entry[-1], open_price=110, high=117, low=109, close=114)
        session.evaluate("bybit", "SOLUSDT", [*entry, profitable_exit])
        pullback = next_candle(profitable_exit, open_price=114, high=115, low=108, close=109)
        recovery = next_candle(pullback, open_price=109, high=118, low=108, close=117)

        result = session.evaluate("bybit", "SOLUSDT", [*entry, profitable_exit, pullback, recovery])

        self.assertEqual(result["status"], "simulated")
        self.assertEqual(result["order"]["side"], "long")
        self.assertEqual(len(session.orders), 2)

    def test_daily_loss_limit_is_applied_to_new_paper_entries(self) -> None:
        session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
        session.history.append({
            "closed_at": session._timestamp(),
            "realized_pnl": -200.0,
            "symbol": "BTCUSDT",
        })
        values = [100, 100, 100, 100, 80, 80, 80, 110]

        result = session.evaluate("bybit", "SOLUSDT", candles(values))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["code"], "daily_loss_limit")
        self.assertEqual(session.orders, [])

    def test_paper_records_survive_restart_and_order_ids_continue(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "paper-state.json"
            session = PaperExecutionSession(strategy=MovingAverageCrossStrategy(2, 4))
            entry_values = [100, 100, 100, 100, 80, 80, 80, 110]
            entry_candles = candles(entry_values)
            session.evaluate("bybit", "SOLUSDT", entry_candles)
            session.save(path)

            restored = PaperExecutionSession.load(path)
            self.assertEqual(len(restored.snapshot()["orders"]), 1)
            self.assertEqual(restored.snapshot()["open_positions"][0]["order_id"], "PAPER-000001")
            self.assertEqual(len(restored.snapshot()["balance_history"]), 1)
            self.assertEqual(restored._last_evaluated_candle["SOLUSDT"], entry_candles[-1].timestamp_ms)

            result = restored.evaluate("bybit", "SOLUSDT", candles(entry_values))
            self.assertEqual(result["status"], "hold")
            self.assertEqual(len(restored.orders), 1)

            exit_candle = next_candle(entry_candles[-1], open_price=110, high=117, low=109, close=110)
            restored.evaluate("bybit", "SOLUSDT", [*entry_candles, exit_candle])
            restored.save(path)
            reloaded = PaperExecutionSession.load(path)
            self.assertEqual(reloaded.account_equity, restored.account_equity)
            self.assertEqual(reloaded.snapshot()["trade_history"][0]["status"], "closed")
            self.assertEqual(reloaded.snapshot()["orders"][0]["status"], "closed")
            self.assertEqual(reloaded.snapshot()["analytics"]["total_trades"], 1)


if __name__ == "__main__":
    unittest.main()
