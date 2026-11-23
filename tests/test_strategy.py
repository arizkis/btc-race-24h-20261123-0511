"""Boundary and order-lifecycle checks for the frozen trading rules."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import TrendRider


def call(desk, seq, close=100_000, *, qty=0, fills=(), warmup=0, room=800_000_000):
    state = SimpleNamespace(
        seq=seq + warmup, warmup_bars=warmup, warming_up=seq < 0,
        cash=1_000_000_000, weight_room=lambda symbol: room,
        positions={"CBBTC": SimpleNamespace(qty=qty)} if qty else {},
        hello={"fees": {"min_notional_quote_units": "1000000"}},
    )
    batch = {"seq": state.seq, "fills": list(fills),
             "bars": [] if close is None else [{"symbol": "CBBTC", "c": str(close)}]}
    return desk.on_batch(state, batch)


def fill(side, seq, price, warmup=0):
    return {"symbol": "CBBTC", "status": "filled", "side": side,
            "fill_bar": seq + warmup, "price": str(price)}


def position(mode="trend", entry=100_000, entry_seq=0):
    desk = TrendRider()
    desk.mode = mode
    desk.entry_price = entry
    desk.entry_seq = entry_seq
    desk.buy_fill_count = 1
    return desk


class StrategyTests(unittest.TestCase):
    def test_wilder_seed_smoothing_and_flat_rsi(self):
        desk = TrendRider()
        for _ in range(15):
            desk.update_indicators(100_000)
        self.assertEqual(desk.rsi, 50)
        desk.update_indicators(100_140)
        self.assertEqual(desk.avg_gain, 10)
        self.assertEqual(desk.rsi, 100)
        desk.update_indicators(100_000)
        self.assertAlmostEqual(desk.avg_gain, 130 / 14)
        self.assertAlmostEqual(desk.avg_loss, 10)

    def test_history_gate_requires_150_closes(self):
        desk = TrendRider()
        for seq in range(149):
            self.assertEqual(call(desk, seq, 100_000 + seq * 10), [])
        orders = call(desk, 149, 101_490)
        self.assertEqual(orders[0]["notional_quote_units"], 800_000_000)
        self.assertTrue(desk.trend_used)
        self.assertEqual(desk.primary_entries, 0)
        call(desk, 150, 101_500, qty=100, fills=[fill("buy", 150, 101_520)])
        self.assertEqual(desk.entry_price, 101_520)
        self.assertEqual(desk.entry_seq, 150)
        self.assertEqual(desk.primary_entries, 1)

    def test_warmup_collects_without_orders(self):
        desk = TrendRider()
        for seq in range(-160, 0):
            self.assertEqual(call(desk, seq, 100_000 + (seq + 160) * 10, warmup=160), [])
        self.assertEqual(len(desk.closes), 160)
        self.assertEqual(call(desk, 0, 101_600, warmup=160)[0]["side"], "buy")

    def test_reentry_cooldown_depth_and_cutoff(self):
        for depth in (35, 100):
            desk = TrendRider()
            for i in range(150):
                desk.update_indicators(98_000 + i * 5)
            desk.running_high = 100_000
            desk.trend_used = True
            desk.buy_fill_count = 1
            desk.last_exit_seq = 0
            # A test fixture isolates the strict RSI <55 entry boundary.
            desk.avg_gain, desk.avg_loss = 0, 100_000
            price = 100_000 - depth * 10
            self.assertEqual(call(desk, 29, price), [])
            self.assertEqual(call(desk, 30, price)[0]["side"], "buy")
        self.assertEqual(call(desk, 1101, price), [])

    def test_primary_budget_and_strikes_block_new_entries(self):
        for field, value in (("primary_entries", 3), ("strikes", 2)):
            desk = TrendRider()
            for i in range(150):
                desk.update_indicators(100_000 + i * 10)
            setattr(desk, field, value)
            self.assertEqual(call(desk, 300, 101_500), [])

    def test_washout_disarms_and_new_high_rearms(self):
        desk = TrendRider()
        for _ in range(150):
            desk.update_indicators(100_000)
        self.assertEqual(call(desk, 10, 98_250)[0]["side"], "buy")
        self.assertFalse(desk.washout_armed)
        self.assertEqual(call(desk, 11, 98_200), [])
        desk.update_indicators(100_001)
        self.assertTrue(desk.washout_armed)

    def test_exit_boundaries_by_mode(self):
        cases = [("trend", 98_750), ("wash", 102_250), ("wash", 99_400),
                 ("fallback", 101_500), ("fallback", 99_250)]
        for mode, close in cases:
            with self.subTest(mode=mode, close=close):
                desk = position(mode)
                self.assertTrue(desk.exit_signal(close, None, 10))
        self.assertFalse(position("fallback").exit_signal(99_251, None, 10))
        self.assertFalse(position("wash").exit_signal(99_401, None, 10))

    def test_structure_break_is_strict(self):
        desk = position()
        desk.window_sum = 100_000 * 120
        self.assertFalse(desk.exit_signal(99_800, 100_000, 1))
        self.assertTrue(desk.exit_signal(99_799, 100_000, 1))

    def test_trailing_and_blowoff(self):
        desk = position()
        desk.peak_close = 101_000
        self.assertTrue(desk.exit_signal(99_990, None, 1))
        self.assertFalse(desk.exit_signal(99_991, None, 1))
        desk.rsi = 80
        self.assertTrue(desk.exit_signal(102_500, None, 2))
        self.assertTrue(desk.blowoff_done)

    def test_wash_rsi_profit_and_time_stop(self):
        desk = position("wash", entry_seq=20)
        desk.rsi = 74
        self.assertFalse(desk.exit_signal(101_999, None, 21))
        self.assertTrue(desk.exit_signal(102_000, None, 21))
        desk.rsi = 50
        self.assertFalse(desk.exit_signal(100_000, None, 619))
        self.assertTrue(desk.exit_signal(100_000, None, 620))

    def test_exit_latches_and_strike_is_recorded_once_at_trigger(self):
        desk = position("wash")
        self.assertEqual(call(desk, 600, 100_249, qty=123)[0]["qty"], 123)
        self.assertEqual(desk.strikes, 1)
        self.assertEqual(call(desk, 601, 101_000, qty=123)[0]["side"], "sell")
        self.assertEqual(call(desk, 602, None, qty=123)[0]["side"], "sell")
        self.assertEqual(desk.strikes, 1)
        self.assertEqual(call(desk, 603, 100_000, fills=[fill("sell", 603, 99_000)]), [])
        self.assertEqual(desk.last_exit_seq, 603)
        self.assertEqual(desk.strikes, 1)
        desk = position("wash")
        call(desk, 600, 100_250, qty=123)
        self.assertEqual(desk.strikes, 0)

    def test_fallback_retries_until_fill_then_forces_exit(self):
        desk = TrendRider()
        desk.strikes, desk.primary_entries = 2, 3
        self.assertEqual(call(desk, 1099), [])
        for seq in (1100, 1101, 1381):
            orders = call(desk, seq)
            self.assertEqual(orders[0]["notional_quote_units"], 400_000_000)
        self.assertEqual(call(desk, 1382, None)[0]["side"], "buy")
        self.assertEqual(call(desk, 1383, qty=123, warmup=150,
                              fills=[fill("buy", 1383, 100_000, 150)]), [])
        self.assertEqual(desk.mode, "fallback")
        self.assertEqual(desk.entry_seq, 1383)
        self.assertEqual(desk.buy_fill_count, 1)
        self.assertEqual(desk.primary_entries, 3)
        self.assertEqual(call(desk, 1398, qty=123), [])
        self.assertEqual(call(desk, 1399, qty=123)[0]["side"], "sell")
        self.assertEqual(desk.strikes, 2)
        self.assertEqual(call(desk, 1439, qty=123), [])

    def test_minimum_and_final_bar_never_emit_buy(self):
        self.assertEqual(call(TrendRider(), 1100, room=1_999_999), [])
        self.assertEqual(call(TrendRider(), 1100, room=2_000_000)[0]["notional_quote_units"], 1_000_000)
        self.assertEqual(call(TrendRider(), 1439), [])


if __name__ == "__main__":
    unittest.main()
