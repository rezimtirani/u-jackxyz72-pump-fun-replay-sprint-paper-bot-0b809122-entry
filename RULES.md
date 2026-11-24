# Snipe-and-Scale — strategy notes

Paper-trading bot for the make.fun "Pump.fun Replay Sprint" bounty
(`u-jackxyz72-pump-fun-replay-sprint-paper-bot-0b809122`, harness
pumpfun-replay/v2). Graded on a replayed 24h window of real pump.fun
trades; the bot speaks `agent-fun/replay/v2` over stdin/stdout and is run
as `python3 -P -s main.py` with `PYTHONHASHSEED=0`. Standard library only;
the protocol shim is vendored in `vendor/makefun_bot.py` (the kit's own
`sdk/python/makefun_bot.py`, copied verbatim).

## What the grader supplies

* `hello`: 10 SOL paper capital, caps (max 5 open positions, buys capped at
  1% of a curve's real SOL per batch, max 16 orders per batch, no shorts or
  leverage), 2-slot latency, fees (95 bps protocol + 30 bps creator fallback,
  5000 + 100000 lamports tx fee per order, 2% failed-tx draw).
* One batch per slot that has events: the slot's Create/Trade/Complete
  events, marks (latest reserves) for held/pending curves, fills for orders
  decided 2 slots earlier, and the account (cash + positions).
* Scoring: net PnL over the final 24h window. Guardrails: at least 20
  distinct mints with round trips of >= 0.01 SOL buys, max drawdown <= 50%,
  net PnL > 0 to take a paid place. The bot runs twice; both action logs
  must match byte for byte.

## Strategy

1. **Watch every launch from its CreateEvent.** Keep a short tape: the
   creator's own create-slot buy, distinct non-creator buyers, lamports
   bought vs sold by others, the largest single sell as a share of real
   SOL, and whether the creator has sold.
2. **First buy (0.05 SOL), only in the first 6 slots**, only when:
   real SOL is 1–30, dev buy <= 2.5 SOL, creator has not sold, 1–4 distinct
   non-creator buyers, >= 1 trade, sells by others <= 10% of buys, no
   single sell above 30% of real SOL, price within -5%..+200% of create,
   net flow over the last 10 slots >= 0.
3. **One pyramid add (0.05 SOL)** while the coin is at most 15 slots old,
   only if the price is >= 5% above the last buy's price and net flow is
   still positive. Losers never get a second tranche.
4. **Exit whole** on the first of: +25% over cost, -10% under cost, a
   trailing stop (arms at +10%, exits on an 8% fall from the best value),
   30 slots held, the creator selling, one sell >= 25% of real SOL, or no
   trade for 60 slots. A coin is never re-entered after an exit.
5. **Drawdown guard:** equity (cash + marked sell value of positions) is
   tracked against its peak; new entries stop once equity is 20% below the
   peak, so the 50% guardrail is never approached. Exits keep running.
6. **Determinism:** no clock, no environment, no unseeded randomness;
   iteration is over insertion-ordered dicts and sets are only measured
   for size. The bot never writes files anywhere.

## Local grading

```sh
node cli/makefun.mjs grade-local --kit <kit> --bot . --driver process --json
```

The process driver needs `python3.13` on PATH; this VM ships 3.12, so a
standalone 3.13.7 was installed at `~/workspace/python313/` (symlinked as
`~/workspace/bin/python3.13`). Without it grade-local refuses Python bots.

Dev-window baselines measured on this machine (for reference):
pilot momentum -9.49 SOL (drawdown 95.7%, non-compliant), pilot sniper
-0.47 SOL (drawdown 5.2%, compliant), pilot reversion -4.00 SOL
(drawdown 49.3%, compliant).

Tuning log (dev window, 10 SOL capital; all variants deterministic,
l1 reproduced, compliant, 0 rejected orders):

| variant | change vs previous | net PnL | win | trips | fills | mints | dd |
|---|---|---|---|---|---|---|---|
| V1 | first build: 0.05+0.05 add, age 6, buyers <= 4 | -0.533 | 15.8% | 272 | 616 | 272 | 5.6% |
| V2 | V1 + partial TP 50% at +20%, ride rest | -0.703 | 15.4% | 272 | 636 | 272 | 7.3% |
| V3 | no add, stake 0.10, age 3, buyers <= 3 | -0.355 | 15.9% | 207 | 414 | 207 | 4.0% |
| V4 | V3 + buyers <= 2, trades >= 2 | -0.135 | 12.4% | 105 | 210 | 105 | 1.7% |
| V5 | V4 + TP +40%, trail 15% | -0.145 | 11.4% | 105 | 210 | 105 | 1.8% |
| V6 | V4 + min flow 0.005 SOL (no-op) | -0.135 | 12.4% | 105 | 210 | 105 | 1.7% |
| V7 | V4 + dev buy <= 1.0 SOL | -0.072 | 10.3% | 68 | 136 | 68 | 1.0% |
| V8 | V7 + stop-loss -8% (shipped) | -0.069 | 10.3% | 68 | 136 | 68 | 1.0% |
| V9 | V8 + run-up cap +100% (reverted) | -0.128 | 9.0% | 67 | 134 | 67 | 1.5% |

Lessons: the entry filter is the whole game — every tightening that
removed bundled/whale launches helped, while exit tweaks barely moved
anything. The pyramid add and the partial-TP ride both hurt (losers get
sized up, winners give back their gains). Coins that already ran >100% in
their first 3 slots keep running — capping the run-up hurt. The shipped
V8 is still slightly negative on dev (-0.69% of capital); the final window
is a different 24h, so this is a close-to-breakeven sniper, not a proven
winner. Stress copies (rebuilt from the dev window's own trade blocks,
`--stress 5 --stress-jobs 5`, all 5 ok, 0 failed): copy PnLs -0.077,
-0.175, -0.048, -0.057, -0.008 SOL; median -0.057, p10 -0.175, p90
-0.008; all 5 copies lost money; dev score ranks 2nd of 6 (dev + 5
copies), so the dev number is representative, not a lucky fit — the bot
is a stable small loser across resampled windows, with no blowups
(worst copy -0.175 SOL, drawdown ~1%).
