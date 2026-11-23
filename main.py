"""Fern Trend-Rider: close-driven, long-only CBBTC race desk."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars


SYMBOL = "CBBTC"
BPS = 10_000


class TrendRider:
    def __init__(self):
        self.closes = []
        # Store SMA numerators so threshold equalities stay exact in raw units.
        self.sma_sums = []
        self.window_sum = 0
        self.avg_gain = None
        self.avg_loss = None
        self.seed_gain = 0
        self.seed_loss = 0
        self.rsi = 50.0
        self.running_high = 0
        self.washout_armed = True
        self.trend_used = False
        self.blowoff_done = False
        self.buy_fill_count = 0
        self.primary_entries = 0
        self.strikes = 0
        self.last_exit_seq = None
        self.pending_mode = None
        self.mode = None
        self.entry_price = None
        self.entry_seq = None
        self.peak_close = 0
        self.exit_pending = False

    def update_indicators(self, close):
        if self.closes:
            change = close - self.closes[-1]
            gain, loss = max(change, 0), max(-change, 0)
            if self.avg_gain is None:
                self.seed_gain += gain
                self.seed_loss += loss
                if len(self.closes) == 14:
                    self.avg_gain = self.seed_gain / 14
                    self.avg_loss = self.seed_loss / 14
            else:
                self.avg_gain = (13 * self.avg_gain + gain) / 14
                self.avg_loss = (13 * self.avg_loss + loss) / 14
            if self.avg_gain is not None:
                total = self.avg_gain + self.avg_loss
                self.rsi = 50.0 if total == 0 else 100 * self.avg_gain / total

        self.closes.append(close)
        self.window_sum += close
        if len(self.closes) > 120:
            self.window_sum -= self.closes[-121]
        sma = self.window_sum / 120 if len(self.closes) >= 120 else None
        self.sma_sums.append(self.window_sum if sma is not None else None)
        if close > self.running_high:
            self.running_high = close
            self.washout_armed = True
        return sma

    def process_fills(self, batch, seq):
        closed_now = False
        for fill in batch["fills"]:
            if fill["symbol"] != SYMBOL or fill["status"] != "filled":
                continue
            if fill["side"] == "buy":
                self.buy_fill_count += 1
                self.mode = self.pending_mode
                self.entry_price = int(fill["price"])
                self.entry_seq = seq
                self.peak_close = self.entry_price
                self.exit_pending = False
                if self.mode != "fallback":
                    self.primary_entries += 1
            elif fill["side"] == "sell":
                self.last_exit_seq = seq
                closed_now = True
                self.mode = None
                self.entry_price = None
                self.entry_seq = None
                self.peak_close = 0
                self.exit_pending = False
        self.pending_mode = None
        return closed_now

    def buy(self, state, mode):
        amount = min(state.cash, state.weight_room(SYMBOL))
        if mode == "fallback":
            amount //= 2
        minimum = int(state.hello["fees"]["min_notional_quote_units"])
        if amount <= 0 or amount < minimum:
            return []
        self.pending_mode = mode
        # These setup latches change when an entry is submitted; budgets count fills.
        if mode == "trend":
            self.trend_used = True
        elif mode == "wash":
            self.washout_armed = False
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": amount, "max_slippage_bps": 100}]

    @staticmethod
    def sell(qty):
        return [{"side": "sell", "symbol": SYMBOL,
                 "qty": qty, "max_slippage_bps": 100}]

    def exit_signal(self, close, sma, seq):
        entry = self.entry_price
        profit = (close - entry) * BPS
        if self.mode == "trend":
            # Invalidation: broken structure, disaster loss, or a lost peak.
            structure = sma is not None and close * BPS * 120 < self.window_sum * 9980
            disaster = close * BPS <= entry * 9875
            trailing = (self.peak_close * BPS >= entry * 10100
                        and close * BPS <= self.peak_close * 9900)
            blowoff = self.rsi >= 80 and profit >= entry * 250
            if blowoff:
                self.blowoff_done = True
            return structure or disaster or trailing or blowoff
        if self.mode == "wash":
            return (close * BPS >= entry * 10225
                    or (self.rsi >= 74 and profit >= entry * 200)
                    or close * BPS <= entry * 9940
                    or seq - self.entry_seq >= 600)
        return (close * BPS >= entry * 10150
                or close * BPS <= entry * 9925 or seq >= 1399)

    def on_batch(self, state, batch):
        seq = state.seq - state.warmup_bars
        closed_now = self.process_fills(batch, seq)
        bar = next((b for b in batch["bars"] if b["symbol"] == SYMBOL), None)
        close = int(bar["c"]) if bar is not None else None
        sma = self.update_indicators(close) if close is not None else None
        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding else 0
        if qty and close is not None:
            self.peak_close = max(self.peak_close, close)

        if state.warming_up or not 0 <= seq < 1439:
            return []
        if qty:
            # A rejected liquidation remains live, even on a missing-bar batch.
            if self.exit_pending:
                return self.sell(qty)
            if close is not None and self.exit_signal(close, sma, seq):
                self.exit_pending = True
                if (close - self.entry_price) * BPS < self.entry_price * 25:
                    self.strikes += 1
            return self.sell(qty) if self.exit_pending else []
        if closed_now:
            return []

        # Participation fallback is independent of the primary-entry risk book.
        if seq >= 1100 and self.buy_fill_count == 0:
            return self.buy(state, "fallback")
        if close is None:
            return []
        if self.primary_entries >= 3 or self.strikes >= 2:
            return []

        # Regime: rising long average plus positive trailing two-hour progress.
        gate = (len(self.closes) >= 150 and close * 120 > self.window_sum
                and self.window_sum > self.sma_sums[-31]
                and close * BPS > self.closes[-120] * 10030)
        depth = (self.running_high - close) * BPS
        if gate and seq <= 1100:
            if not self.trend_used:
                return self.buy(state, "trend")
            if (not self.blowoff_done and self.last_exit_seq is not None
                    and seq - self.last_exit_seq >= 30
                    and self.running_high * 35 <= depth <= self.running_high * 100
                    and self.rsi < 55 and close * 120 * BPS > self.window_sum * 9980):
                return self.buy(state, "trend")
        # Washout contingency requires a fresh high before another washout cycle.
        if (not gate and self.washout_armed and seq <= 1380
                and depth >= self.running_high * 175 and self.rsi <= 22):
            return self.buy(state, "wash")
        return []


desk = TrendRider()


def on_batch(state, batch):
    return desk.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
