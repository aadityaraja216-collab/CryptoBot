# Kalshi BTC 15-Minute Bot

Automates Kalshi's `KXBTC15M` up/down markets end to end: pulls the open
market(s) and a live BTC spot price, estimates a fair-value probability
that BTC finishes above the strike, compares it to what the market is
currently pricing, and — if the gap is big enough — places an entry order.
Open positions are then watched for a take-profit level, a stop-loss level,
or a "close is coming, get out" time-based exit.

## Before you touch real money

- **Defaults are safe on purpose**: `USE_DEMO = True` and `DRY_RUN = True`
  at the top of `kalshi_btc_bot.py`. In this mode it logs exactly what it
  *would* do and never sends an order. You flip both, deliberately, when
  you're ready.
- **The strategy is a starting point, not an edge.** It assumes BTC's
  short-term moves are a random walk with no drift and normally
  distributed returns. Real BTC has fatter tails and occasional jumps, so
  the model's probabilities will be wrong in exactly the moments that
  matter most (news, liquidations, etc.). Kalshi's fee and the bid/ask
  spread eat into whatever edge is left. Treat this as a framework to
  test and improve, not a working money-maker out of the box.
- **API schemas drift.** Field names like `floor_strike` or `close_time`
  are based on current docs and public examples, but Kalshi has changed
  field names before. Always run `--debug-market` first and check the
  printed JSON matches what the code expects.

## Setup

```bash
pip install -r requirements.txt

# Generate a key pair and upload the PUBLIC key to Kalshi
# (Account settings -> API Keys). Keep the private key file secret.
openssl genrsa -out kalshi_private.pem 2048
openssl rsa -in kalshi_private.pem -pubout -out kalshi_public.pem

export KALSHI_API_KEY_ID="your-key-id-here"
export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private.pem"
```

## Recommended run order

1. **`python kalshi_btc_bot.py --self-test`**
   Pure logic/math check, zero network calls. Confirms the probability
   model and position-exit logic behave as expected on synthetic data.

2. **`python kalshi_btc_bot.py --debug-market`**
   Hits the real (demo) market-data endpoint, prints one raw market
   payload, then exits. Compare the field names in that output against
   what `evaluate_market()` and `check_exits()` read (`floor_strike`,
   `close_time`, `yes_bid`, `yes_ask`, `no_bid`, `no_ask`) and fix any
   mismatches before going further.

3. **`python kalshi_btc_bot.py`**
   Full loop against Kalshi's demo environment. With `DRY_RUN = True`
   (the default) this works even without API credentials set, since it
   never needs to place a real order — good for watching the strategy's
   decisions for a while before it can touch anything. Everything it
   does or would do is appended to `kalshi_bot_trade_log.jsonl` so you
   can review decisions after the fact.

4. Only once you've watched it for a while and are comfortable with its
   behavior: set `USE_DEMO = False` and `DRY_RUN = False` to trade for
   real. Start with a small `MAX_CONTRACTS_PER_TRADE`.

## The knobs (all in the config block at the top of the file)

| Setting | What it controls |
|---|---|
| `EDGE_THRESHOLD` | How big the model-vs-market probability gap must be before it trades. Higher = fewer, more selective trades. |
| `MAX_CONTRACTS_PER_TRADE` / `MAX_OPEN_POSITIONS` | Position sizing / concurrency caps. |
| `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | The profit/loss exit points you asked about — expressed as a fraction of max possible gain (take-profit) and of entry price (stop-loss). |
| `EXIT_BEFORE_SETTLEMENT_SECONDS` | Flattens the position instead of holding to settlement once this close to the window closing, to avoid last-second settlement-index surprises. |
| `MAX_DAILY_LOSS_CENTS` | Kill switch — stops opening new positions for the rest of the UTC day once hit. |
| `VOL_LOOKBACK_SECONDS` / `FALLBACK_ANNUALIZED_VOL` | How the realized-volatility estimate (which feeds the probability model) is computed and what it uses before it has enough data. |

## What this does *not* do (yet)

- No WebSocket feed — it polls REST every `POLL_SECONDS`, which is simple
  and plenty fast for a 15-minute window, but a WebSocket connection would
  cut latency further if you need it.
- No backtesting harness against historical settlements — `evaluate_market`
  and `fair_value_prob_above_strike` are pulled out as standalone functions
  specifically so you can feed them historical data and check calibration
  before trusting them live.
- One strategy function. `evaluate_market()` is the one place to swap in
  a different signal entirely.

## Disclaimer

Not financial advice. Kalshi contracts are real-money financial
instruments and short-dated crypto markets are volatile — you can lose
money quickly, including all of what you put into a trade. This code is
a technical starting point, not a claim that the included strategy is
profitable.
