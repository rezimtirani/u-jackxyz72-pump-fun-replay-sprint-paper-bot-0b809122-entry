"""Snipe-and-Scale: a pump.fun bonding-curve replay bot (agent-fun/replay/v2).

Idea
----
Most new pump.fun coins die within seconds; a few run hard. This bot only buys
launches, only in their first few slots, only when the opening tape looks like a
real crowd rather than the creator's own cluster about to dump:

* the creator has not sold before entry, and their own create-slot buy is small;
* only a handful of distinct non-creator wallets bought so far (a launch bundled
  by many wallets is a dump in waiting);
* real SOL in the curve is at least 1 SOL, so a 0.01+ SOL buy fits the 1% size
  cap and counts toward the guardrail;
* no single seller has dumped a big share of the real reserve yet, and the
  price has not already run away from (or collapsed below) its create price.

The first buy is small (0.05 SOL). If the coin keeps climbing and fresh SOL
keeps flowing in, one add of 0.05 SOL pyramids into strength; losers never get
a second tranche. Every position exits whole on the first of: take-profit
(+25% over cost), stop-loss (-10% under cost), a trailing stop once up 10%
(8% give-back), 30 slots held, the creator selling, or a single big sell.
A drawdown guard stops new entries once equity is 20% below its peak, so the
50% guardrail can never be approached.

Pure function of the batches it is fed: no clock, no environment, no
unseeded randomness. Iteration is over insertion-ordered dicts, and every set
is only ever measured for size, so two runs produce identical replies.

Run by the grader as ``python3 -P -s /bot/main.py``. The protocol shim lives
in vendor/makefun_bot.py (the kit's own sdk/python file, vendored verbatim).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bot

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
BPS = makefun_bot.BPS
# Curve fee on sells: protocol 95 bps + creator 30 bps fallback = 125 bps.
CURVE_FEE_BPS = 125

# --- entry ---------------------------------------------------------------
FIRST_STAKE = 100_000_000         # 0.10 SOL, first buy per coin (no add in this variant)
ADD_STAKE = 50_000_000            # 0.05 SOL, the single pyramid add
ENABLE_ADD = False                # variant switch: the add hurt in V1/V2, test without it
MIN_BUY = 10_000_000              # 0.01 SOL: smallest buy worth a tx fee, and
                                  # the guardrail's minimum round-trip buy
MAX_AGE_FIRST = 3                 # slots after create in which a first buy may be decided
MAX_AGE_ADD = 15                  # slots after create in which the add may be decided
ADD_STEP_BPS = 500                # add only once price is >= 5% above the last buy's price
MIN_REAL_SOL = 1_000_000_000      # 1 SOL in the curve at entry
MAX_REAL_SOL = 30_000_000_000     # 30 SOL in the curve at entry
MAX_DEV_BUY = 1_000_000_000       # creator's own create-slot buy, at most 1.0 SOL
MIN_BUYERS = 1                    # distinct non-creator buyers seen before entry
MAX_BUYERS = 2                    # ... at most: more smells like a bundled launch
MIN_TRADES = 2                    # trades seen before entry
MAX_SELL_RATIO_BPS = 1_000        # lamports sold by others / lamports bought, at most 10%
MAX_BIG_SELL_BPS = 3_000          # largest single non-creator sell, at most 30% of real SOL
MIN_RUNUP_BPS = 9_500             # price now vs create price, at least -5%
MAX_RUNUP_BPS = 30_000            # ... at most +200%
FLOW_WINDOW = 10                  # slots of recent tape for the flow checks
MIN_FLOW = 5_000_000              # net SOL bought minus sold over FLOW_WINDOW at entry: >= 0.005 SOL

# --- exits ---------------------------------------------------------------
TP1_BPS = 1_000_000              # partial take-profit disabled in this variant
TP1_PART_BPS = 5_000              # ... sell 50% of the tokens
TP_BPS = 2_500                    # +25% over cost with no partial taken: sell everything
SL_BPS = 800                    # -8% under cost: sell everything
SL_AFTER_TP1_BPS = 0              # after the partial, the rest stops out at breakeven
TRAIL_ARM_BPS = 1_000             # trailing stop arms once up 10%
TRAIL_BPS = 800                   # ... then exit on an 8% fall from the best value
TRAIL_AFTER_TP1_BPS = 1_500       # ... after the partial, a wider 15% trail lets runners run
MAX_HOLD = 30                     # slots held, then exit whatever the price
MAX_HOLD_AFTER_TP1 = 300          # ... the ridden remainder may live up to 300 slots
STALE_SLOTS = 60                  # exit when no trade has hit the coin for this long
BIG_SELL_EXIT_BPS = 2_500         # exit when one recorded sell is >= 25% of real SOL
EXIT_ON_DEV_SELL = True           # exit at once when the creator sells

# --- risk ----------------------------------------------------------------
DD_GUARD_BPS = 2_000              # stop new entries once equity is 20% below its peak
BUY_SLIPPAGE_BPS = 10_000         # buys accept the fill price after latency
SELL_SLIPPAGE_BPS = 10_000        # sells always go out at 10000


class Tick:
    __slots__ = ("slot", "is_buy", "sol", "user")

    def __init__(self, slot, is_buy, sol, user):
        self.slot = slot
        self.is_buy = is_buy
        self.sol = sol
        self.user = user


class Coin:
    __slots__ = (
        "mint", "create_slot", "creator", "vq0", "vt0", "dev_buy", "trades",
        "buyers", "bought", "sold_by_others", "big_sell_bps", "dev_sold",
        "last_trade_slot", "recent", "buys_sent", "spent_sent", "last_buy_vq",
        "last_buy_vt", "last_buy_slot", "done", "complete",
    )

    def __init__(self, mint, create_slot, creator, vq0, vt0):
        self.mint = mint
        self.create_slot = create_slot
        self.creator = creator
        self.vq0 = vq0
        self.vt0 = vt0
        self.dev_buy = 0
        self.trades = 0
        self.buyers = set()
        self.bought = 0
        self.sold_by_others = 0
        self.big_sell_bps = 0
        self.dev_sold = False
        self.last_trade_slot = create_slot
        self.recent = []
        self.buys_sent = 0
        self.spent_sent = 0
        self.last_buy_vq = 0
        self.last_buy_vt = 1
        self.last_buy_slot = 0
        self.done = False
        self.complete = False


class Held:
    __slots__ = ("opened_slot", "tx_paid", "best", "dev_sold_at_open", "added", "took_tp1")

    def __init__(self, opened_slot, tx_paid, dev_sold_at_open):
        self.opened_slot = opened_slot
        self.tx_paid = tx_paid
        self.best = 0
        self.dev_sold_at_open = dev_sold_at_open
        self.added = False
        self.took_tp1 = False


def sell_value(tokens, vq, vt):
    """Lamports a sell of `tokens` against (vq, vt) would pay after curve fees."""
    if tokens <= 0 or vt <= 0:
        return 0
    quote = (tokens * vq) // (vt + tokens)
    return quote - (quote * CURVE_FEE_BPS + BPS - 1) // BPS


class SnipeAndScale:
    def __init__(self):
        self.coins = {}
        self.young = []          # coins created within MAX_AGE_ADD slots, in create order
        self.held = {}
        self.selling = set()    # mints with a sell in flight
        self.peak_equity = None
        self.peak_cash = None

    # -- tape ----------------------------------------------------------
    def _read_events(self, batch):
        for e in batch["events"]:
            kind = e["event"]
            if kind == "CreateEvent":
                coin = Coin(
                    e["mint"], e["slot"], e["creator"],
                    int(e["virtual_quote_reserves"]), int(e["virtual_token_reserves"]),
                )
                self.coins[e["mint"]] = coin
                self.young.append(coin)
            elif kind == "TradeEvent":
                coin = self.coins.get(e["mint"])
                if coin is None:
                    continue
                sol = int(e["sol_amount"])
                coin.trades += 1
                coin.last_trade_slot = e["slot"]
                coin.recent.append(Tick(e["slot"], e["is_buy"], sol, e["user"]))
                is_dev = e["user"] == coin.creator
                if e["is_buy"]:
                    coin.bought += sol
                    if is_dev and e["slot"] == coin.create_slot:
                        coin.dev_buy += sol
                    elif not is_dev:
                        coin.buyers.add(e["user"])
                elif is_dev:
                    coin.dev_sold = True
                else:
                    coin.sold_by_others += sol
                    rq_before = int(e["real_quote_reserves"]) + sol
                    if rq_before > 0:
                        bps = (sol * BPS) // rq_before
                        if bps > coin.big_sell_bps:
                            coin.big_sell_bps = bps
            else:  # CompleteEvent
                coin = self.coins.get(e["mint"])
                if coin is not None:
                    coin.complete = True
                    coin.done = True

    def _flow(self, coin, slot):
        """Net lamports bought minus sold over the last FLOW_WINDOW slots."""
        start = slot - FLOW_WINDOW
        drop = 0
        recent = coin.recent
        while drop < len(recent) and recent[drop].slot <= start:
            drop += 1
        if drop:
            del recent[:drop]
        net = 0
        for t in recent:
            net += t.sol if t.is_buy else -t.sol
        return net

    def _prune_young(self, slot):
        """Drop coins too old to ever buy again from the entry scan list."""
        keep = []
        for coin in self.young:
            if slot - coin.create_slot <= MAX_AGE_ADD and not coin.done:
                keep.append(coin)
        self.young = keep

    # -- main ----------------------------------------------------------
    def on_batch(self, state, batch):
        self._read_events(batch)
        now = state.slot
        tx = state.tx_fee_lamports()
        marks = {}
        for m in batch["marks"]:
            marks[m["mint"]] = (int(m["virtual_quote_reserves"]), int(m["virtual_token_reserves"]))

        # Track fills: tx fees paid, positions opened, harness closes.
        for f in batch["fills"]:
            if f["client_id"].startswith("_"):
                self.held.pop(f["mint"], None)
                self.selling.discard(f["mint"])
                continue
            if f["status"] == "filled":
                h = self.held.get(f["mint"])
                if h is not None:
                    h.tx_paid += int(f["tx_fee_lamports"])
                elif f["side"] == "buy":
                    coin = self.coins.get(f["mint"])
                    self.held[f["mint"]] = Held(
                        f["slot"], int(f["tx_fee_lamports"]),
                        coin.dev_sold if coin is not None else False,
                    )
        for mint in list(self.selling):
            pending_sell = False
            for p in state.pending.values():
                if p.order["mint"] == mint and p.order["side"] == "sell":
                    pending_sell = True
                    break
            if not pending_sell:
                self.selling.discard(mint)
        for mint in list(self.held):
            if mint not in state.positions and mint not in self.selling:
                pending_buy = False
                for p in state.pending.values():
                    if p.order["mint"] == mint and p.order["side"] == "buy":
                        pending_buy = True
                        break
                if not pending_buy:
                    del self.held[mint]

        orders = []
        cap = state.hello["caps"]["max_orders_per_batch"]

        # Equity + drawdown guard (cash + marked sell value of every position).
        equity = state.cash
        for mint, pos in state.positions.items():
            mk = marks.get(mint)
            if mk is None:
                curve = state.curves.get(mint)
                if curve is None:
                    continue
                mk = (curve.virtual_quote_reserves, curve.virtual_token_reserves)
            equity += sell_value(pos.token_amount, mk[0], mk[1]) - tx
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        entries_blocked = (
            self.peak_equity > 0
            and (self.peak_equity - equity) * BPS > self.peak_equity * DD_GUARD_BPS
        )

        # Exits first.
        big_sell_now = set()
        for e in batch["events"]:
            if e["event"] == "TradeEvent" and not e["is_buy"]:
                rq_before = int(e["real_quote_reserves"]) + int(e["sol_amount"])
                if rq_before > 0 and int(e["sol_amount"]) * BPS // rq_before >= BIG_SELL_EXIT_BPS:
                    big_sell_now.add(e["mint"])
        for mint, pos in list(state.positions.items()):
            if len(orders) >= cap:
                break
            if mint in self.selling:
                continue
            coin = self.coins.get(mint)
            h = self.held.get(mint)
            if h is None:
                h = Held(now, tx, coin.dev_sold if coin is not None else False)
                self.held[mint] = h
            mk = marks.get(mint)
            if mk is None:
                curve = state.curves.get(mint)
                if curve is None:
                    continue
                mk = (curve.virtual_quote_reserves, curve.virtual_token_reserves)
            value = sell_value(pos.token_amount, mk[0], mk[1]) - tx
            basis = pos.cost_lamports + h.tx_paid
            if value > h.best:
                h.best = value
            # Partial take-profit: bank half at +20%, then let the rest ride with
            # a wider trail and a breakeven stop instead of a hard take-profit.
            if basis > 0 and not h.took_tp1 and value * BPS >= basis * (BPS + TP1_BPS):
                part = (pos.token_amount * TP1_PART_BPS) // BPS
                if 0 < part < pos.token_amount and len(orders) < cap:
                    orders.append({
                        "side": "sell", "mint": mint,
                        "token_amount": part,
                        "max_slippage_bps": SELL_SLIPPAGE_BPS,
                    })
                    self.selling.add(mint)
                    h.took_tp1 = True
                    h.best = 0  # re-arm the trail on the remainder's own scale
                    continue
            exit_now = False
            if basis > 0:
                if not h.took_tp1 and value * BPS >= basis * (BPS + TP_BPS):
                    exit_now = True
                elif h.took_tp1 and value * BPS <= basis * (BPS - SL_AFTER_TP1_BPS):
                    exit_now = True
                elif not h.took_tp1 and value * BPS <= basis * (BPS - SL_BPS):
                    exit_now = True
                elif (
                    h.best * BPS >= basis * (BPS + TRAIL_ARM_BPS)
                    and value * BPS
                    <= h.best * (BPS - (TRAIL_AFTER_TP1_BPS if h.took_tp1 else TRAIL_BPS))
                ):
                    exit_now = True
            if now - h.opened_slot >= (MAX_HOLD_AFTER_TP1 if h.took_tp1 else MAX_HOLD):
                exit_now = True
            if coin is not None:
                if EXIT_ON_DEV_SELL and coin.dev_sold and not h.dev_sold_at_open:
                    exit_now = True
                if mint in big_sell_now:
                    exit_now = True
                # A coin nobody trades any more will not come back: free the slot.
                if now - coin.last_trade_slot >= STALE_SLOTS:
                    exit_now = True
            if not exit_now:
                continue
            orders.append({
                "side": "sell", "mint": mint,
                "token_amount": pos.token_amount,
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })
            self.selling.add(mint)
            if coin is not None:
                coin.done = True

        # Entries and the single pyramid add.
        self._prune_young(now)
        free = state.free_cash_lamports() - tx * len(orders)
        open_count = state.committed_position_count()
        max_positions = state.hello["caps"]["max_open_positions"]
        for coin in list(self.young):
            if len(orders) >= cap:
                break
            if coin.done or coin.complete:
                continue
            age = now - coin.create_slot
            curve = state.curves.get(coin.mint)
            if curve is None or curve.complete:
                continue
            pending_buy = False
            for p in state.pending.values():
                if p.order["mint"] == coin.mint and p.order["side"] == "buy":
                    pending_buy = True
                    break
            if pending_buy:
                continue
            vq = curve.virtual_quote_reserves
            vt = curve.virtual_token_reserves
            if coin.buys_sent == 0:
                # First buy.
                if entries_blocked:
                    continue
                if age > MAX_AGE_FIRST:
                    continue
                if open_count >= max_positions:
                    continue
                if mint_is_ruggy(coin, curve):
                    coin.done = True
                    continue
                if not entry_signal(coin, vq, vt, now, self._flow(coin, now)):
                    continue
                want = FIRST_STAKE
            else:
                # The one add: only into strength, only while still young.
                if not ENABLE_ADD:
                    continue
                if coin.buys_sent > 1 or self.held.get(coin.mint, Held(0, 0, False)).added:
                    continue
                if age > MAX_AGE_ADD:
                    continue
                h = self.held.get(coin.mint)
                if h is None or mint in self.selling:
                    continue
                if vq * coin.last_buy_vt * BPS < coin.last_buy_vq * vt * (BPS + ADD_STEP_BPS):
                    continue
                if self._flow(coin, now) <= 0:
                    continue
                want = ADD_STAKE
            room = state.max_buy_lamports(coin.mint)
            amount = want if want < room else room
            if amount < MIN_BUY or free < amount + tx:
                continue
            orders.append({
                "side": "buy", "mint": coin.mint,
                "quote_lamports": amount,
                "max_slippage_bps": BUY_SLIPPAGE_BPS,
            })
            free -= amount + tx
            coin.buys_sent += 1
            coin.spent_sent += amount
            coin.last_buy_vq = vq
            coin.last_buy_vt = vt
            coin.last_buy_slot = now
            if coin.buys_sent == 1:
                open_count += 1
            else:
                h = self.held.get(coin.mint)
                if h is not None:
                    h.added = True
        return orders


def mint_is_ruggy(coin, curve):
    """Hard rug filters on the opening tape. True = never buy this coin."""
    rq = curve.real_quote_reserves
    if rq < MIN_REAL_SOL or rq > MAX_REAL_SOL:
        return True
    if coin.dev_buy > MAX_DEV_BUY:
        return True
    if coin.dev_sold:
        return True
    if coin.trades < MIN_TRADES:
        return True
    if len(coin.buyers) < MIN_BUYERS or len(coin.buyers) > MAX_BUYERS:
        return True
    if coin.bought > 0 and coin.sold_by_others * BPS > coin.bought * MAX_SELL_RATIO_BPS:
        return True
    if coin.big_sell_bps > MAX_BIG_SELL_BPS:
        return True
    return False


def entry_signal(coin, vq, vt, now, flow):
    """Price and flow shape at entry: not already run away, not dumped, inflow."""
    if coin.vq0 <= 0 or coin.vt0 <= 0 or vt <= 0:
        return False
    runup = (vq * coin.vt0 * BPS) // (vt * coin.vq0)
    if runup < MIN_RUNUP_BPS or runup > MAX_RUNUP_BPS:
        return False
    if flow < MIN_FLOW:
        return False
    return True


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


_BOT = SnipeAndScale()

if __name__ == "__main__":
    makefun_bot.run(on_batch)
