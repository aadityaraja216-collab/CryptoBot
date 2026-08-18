#!/usr/bin/env python3
"""
Kalshi BTC 15-Minute Up/Down Bot
=================================

Automates Kalshi's KXBTC15M markets:
  - Pulls the open BTC 15-min market(s) and a live BTC spot price
  - Estimates a "fair value" probability that BTC finishes above the
    strike, using a short-horizon lognormal (zero-drift) model
  - Compares that model probability to the market's current implied
    probability, and places an entry order when the gap ("edge") clears
    a configurable threshold
  - Manages open positions with a take-profit / stop-loss / time-based exit

READ THIS FIRST
----------------
- Defaults to Kalshi's DEMO environment and DRY_RUN = True: it computes
  and logs every decision but places NO real orders. You have to change
  BOTH on purpose before it can touch real money.
- The probability model is a simple, transparent starting point, not a
  proven edge. 15-minute BTC direction is close to a coin flip once you
  net out spread and Kalshi's trading fee. Paper-trade for a while, look
  at kalshi_bot_trade_log.jsonl, and expect to tune or replace
  evaluate_market() entirely before trusting this with real funds.
- Field names in Kalshi's API responses have shifted before - Kalshi has
  been migrating from integer-cent price fields (yes_bid, yes_ask, ...) to
  dollar-string fields (yes_bid_dollars, yes_ask_dollars, ...), and the
  status=open query filter on GET /markets was unreliable in testing (it
  returned markets months old and long closed, apparently the oldest in
  the series' whole history rather than anything current). This script
  now queries by close-time window (min_close_ts/max_close_ts, documented
  GET /markets filters) instead, and filters for status == "active" itself
  rather than trusting the status query filter. Still, run
  `python kalshi_btc_bot.py --debug-market` and confirm the printed fields
  (from both the list and detail endpoints) match what
  evaluate_market()/check_exits() actually read - especially floor_strike,
  which was missing from the list endpoint's response in testing.
- Double check BASE_URL / the auth flow against https://docs.kalshi.com
  before relying on this - Kalshi has used more than one API hostname.

Setup
-----
1. pip install -r requirements.txt
2. Generate an RSA key pair and upload the PUBLIC key to Kalshi:
     openssl genrsa -out kalshi_private.pem 2048
     openssl rsa -in kalshi_private.pem -pubout -out kalshi_public.pem
   Upload kalshi_public.pem under Kalshi account settings -> API Keys,
   and copy the API Key ID it gives you.
3. Set environment variables:
     export KALSHI_API_KEY_ID="..."
     export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private.pem"
4. Sanity-check the logic with no network calls at all:
     python kalshi_btc_bot.py --self-test
5. Check the real market schema before trading:
     python kalshi_btc_bot.py --debug-market
6. Watch it paper-trade (no credentials even required for this, since
   DRY_RUN short-circuits before any order is sent):
     python kalshi_btc_bot.py
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


# ==========================================================================
# Configuration - all of this is meant to be tuned. Nothing here is a
# recommendation, just a working starting point.
# ==========================================================================

# --- Kalshi environment ---------------------------------------------------
# Confirm these against https://docs.kalshi.com before relying on them.
USE_DEMO = True
BASE_URL = (
    "https://demo-api.kalshi.co/trade-api/v2"
    if USE_DEMO
    else "https://api.elections.kalshi.com/trade-api/v2"
)
SERIES_TICKER = "KXBTC15M"

API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")

# --- Safety switch ---------------------------------------------------------
# When True, the bot computes and logs every decision but never actually
# calls the order endpoint. Flip to False only after you've watched
# dry-run output for a while and understand exactly what it will do.
DRY_RUN = True

# --- Strategy parameters ---------------------------------------------------
POLL_SECONDS = 5                    # how often to poll markets/spot price
VOL_LOOKBACK_SECONDS = 30 * 60      # realized vol computed over this window
VOL_SAMPLE_SECONDS = 10             # spot price sampling interval
MIN_SAMPLES_FOR_VOL = 30            # don't trust the vol estimate until warm
FALLBACK_ANNUALIZED_VOL = 0.55      # used only until enough samples exist
EDGE_THRESHOLD = 0.06               # required model-vs-market gap (probability units)
MIN_SECONDS_REMAINING_TO_ENTER = 45  # don't open new positions this close to close
ENTRY_PRICE_PADDING_CENTS = 1       # pay up to this many cents through the ask to help fills

# --- Risk management --------------------------------------------------------
MAX_CONTRACTS_PER_TRADE = 5
MAX_OPEN_POSITIONS = 1
MAX_DAILY_LOSS_CENTS = 2000          # stop opening new trades once today's realized loss hits this
TAKE_PROFIT_PCT = 0.15               # close when unrealized gain / max-possible-gain hits this
STOP_LOSS_PCT = -0.35                # close when unrealized loss / entry price hits this
EXIT_BEFORE_SETTLEMENT_SECONDS = 12  # flatten instead of holding to settlement inside this window

LOG_PATH = Path("kalshi_bot_trade_log.jsonl")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kalshi_btc_bot")


# ==========================================================================
# Auth - RSA-PSS request signing
# ==========================================================================

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_pss_sha256(private_key, message: str) -> str:
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


class KalshiAuth:
    def __init__(self, key_id: str, private_key_path: str):
        if not key_id or not private_key_path:
            raise ValueError("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH environment variables.")
        self.key_id = key_id
        self.private_key = load_private_key(private_key_path)

    def headers(self, method: str, path: str) -> dict:
        # `path` must include the /trade-api/v2 prefix and exclude the query string.
        timestamp_ms = str(int(time.time() * 1000))
        message = timestamp_ms + method.upper() + path
        signature = sign_pss_sha256(self.private_key, message)
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }


# ==========================================================================
# REST client
# ==========================================================================

class KalshiClient:
    def __init__(self, base_url: str, auth: Optional[KalshiAuth]):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.session = requests.Session()

    def _signed_path(self, endpoint: str) -> str:
        return urlparse(self.base_url).path + endpoint

    def _request(self, method: str, endpoint: str, params: dict = None, body: dict = None) -> dict:
        url = self.base_url + endpoint
        path = self._signed_path(endpoint)
        headers = self.auth.headers(method, path) if self.auth else {}
        resp = self.session.request(method, url, headers=headers, params=params, json=body, timeout=10)
        if resp.status_code >= 400:
            log.error("Kalshi API error %s %s -> %s: %s", method, endpoint, resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    def get_open_btc_markets(self, series_ticker: str = SERIES_TICKER) -> list:
        # Filtering by series_ticker + status=open was returning the OLDEST
        # markets ever created for this series (apparently the default sort/
        # pagination), not current ones - status filtering never had a
        # genuinely live market to find. min_close_ts/max_close_ts are
        # documented GET /markets filters that target a close-time window
        # directly, which is a much more precise way to ask "what's closing
        # in the next few minutes" than paging through the whole history.
        now_ts = int(time.time())
        data = self._request(
            "GET",
            "/markets",
            params={
                "series_ticker": series_ticker,
                "min_close_ts": now_ts,
                "max_close_ts": now_ts + 20 * 60,
                "limit": 25,
            },
        )
        markets = data.get("markets", [])
        # Belt-and-suspenders: still confirm client-side that whatever came
        # back is genuinely tradable right now, not just scheduled to close
        # in this window (e.g. not yet "active").
        active = [m for m in markets if m.get("status") == "active"]
        active.sort(key=lambda m: m.get("close_time") or "")
        return active

    def get_market_detail(self, ticker: str) -> dict:
        # The /markets list endpoint appears to omit some fields (notably
        # floor_strike was missing there in testing) that show up on the
        # single-market detail endpoint instead.
        data = self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    def get_balance(self) -> dict:
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> list:
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    def place_order(
        self, ticker: str, side: str, action: str, count: int, price_cents: int, order_type: str = "limit"
    ) -> dict:
        # NOT YET RE-VERIFIED against a live order response - fine while
        # DRY_RUN short-circuits this before any request goes out, but check
        # this against https://docs.kalshi.com/api-reference/orders before
        # ever setting DRY_RUN = False. As of this writing, Kalshi has a
        # newer "Create Order V2" endpoint (POST /portfolio/events/orders)
        # using side="bid"/"ask", string counts, and dollar-string prices,
        # and has flagged the legacy /portfolio/orders endpoint used below
        # for eventual deprecation. The legacy endpoint still accepts either
        # cent-integer (yes_price/no_price) or dollar-string
        # (yes_price_dollars/no_price_dollars) prices - exactly one of the
        # four - so this cent-based body should still work for now, but
        # confirm the endpoint hasn't been fully retired by the time you get
        # here.
        price_field = "yes_price" if side == "yes" else "no_price"
        body = {
            "ticker": ticker,
            "action": action,    # "buy" or "sell"
            "side": side,        # "yes" or "no"
            "type": order_type,  # "limit" or "market"
            "count": count,
            price_field: price_cents,
        }
        if DRY_RUN:
            log.info("[DRY RUN] would place_order: %s", body)
            return {"order": {"order_id": "dry-run", **body}}
        return self._request("POST", "/portfolio/orders", body=body)


# ==========================================================================
# BTC spot price feed (Coinbase public endpoint, no auth needed)
# ==========================================================================

def fetch_btc_spot_price() -> float:
    resp = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5)
    resp.raise_for_status()
    return float(resp.json()["data"]["amount"])


# ==========================================================================
# Volatility estimate + fair-value probability model
# ==========================================================================

class VolatilityEstimator:
    """Rolling realized volatility from sampled spot prices.

    Intentionally simple (stdev of log returns, annualized). It reacts
    slowly to regime changes and will be most wrong during sudden vol
    spikes - exactly the moments these markets move the most.
    """

    def __init__(self, lookback_seconds: int, sample_seconds: int):
        self.lookback_seconds = lookback_seconds
        self.sample_seconds = sample_seconds
        self._samples: deque = deque()  # list of (timestamp, price)

    def add_sample(self, timestamp: float, price: float) -> None:
        self._samples.append((timestamp, price))
        cutoff = timestamp - self.lookback_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def annualized_vol(self, fallback: float) -> float:
        prices = [p for _, p in self._samples]
        if len(prices) < MIN_SAMPLES_FOR_VOL:
            return fallback
        log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
        if len(log_returns) < 2:
            return fallback
        stdev = statistics.stdev(log_returns)
        samples_per_year = (365 * 24 * 3600) / max(self.sample_seconds, 1)
        return stdev * math.sqrt(samples_per_year)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_value_prob_above_strike(spot: float, strike: float, seconds_remaining: float, annualized_vol: float) -> float:
    """P(BTC spot finishes above `strike` at settlement), assuming a
    zero-drift lognormal random walk over the remaining window.

    Zero drift is a reasonable simplification over a few minutes, but real
    BTC returns have fatter tails and occasional jumps versus a lognormal
    model - treat this as a baseline to test and improve, not ground truth.
    """
    if seconds_remaining <= 0 or annualized_vol <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    t_years = seconds_remaining / (365 * 24 * 3600)
    sigma_window = annualized_vol * math.sqrt(t_years)
    if sigma_window <= 0:
        return 1.0 if spot > strike else 0.0
    d = math.log(spot / strike) / sigma_window
    return normal_cdf(d)


# ==========================================================================
# Trade decision
# ==========================================================================

@dataclass
class TradeSignal:
    ticker: str
    side: str  # "yes" or "no"
    model_prob: float
    market_prob: float
    edge: float
    price_cents: int


def _first_nonempty(market: dict, *keys: str):
    # Some fields (e.g. expiration_value) come back as "" rather than
    # missing/None on markets that haven't settled yet - treat that as
    # absent too, not as a found-but-blank value.
    for k in keys:
        v = market.get(k)
        if v is not None and v != "":
            return v
    return None


def _price_float(market: dict, *keys: str) -> Optional[float]:
    """Read a Kalshi price field as a 0.0-1.0 probability/dollar float.

    Kalshi migrated price fields from integer cents (yes_bid, yes_ask, ...)
    to dollar-denominated strings (yes_bid_dollars, "0.4200", ...). Pass the
    _dollars name(s) first and the legacy cent name(s) as a fallback so this
    keeps working whichever ones the account/endpoint actually returns.
    """
    raw = _first_nonempty(market, *keys)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # Legacy fields are whole cents (e.g. 42); dollar fields are already
    # 0.0-1.0 (e.g. 0.42) - normalize so callers always get a probability.
    return value / 100.0 if value > 1.0 else value


def dollars_to_cents(value: float) -> int:
    return int(round(value * 100))


def _to_epoch_seconds(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def evaluate_market(market: dict, spot: float, annualized_vol: float) -> Optional[TradeSignal]:
    ticker = market["ticker"]
    strike = _first_nonempty(market, "floor_strike", "floor_strike_dollars", "cap_strike", "cap_strike_dollars", "strike_dollars")
    close_ts = _first_nonempty(market, "close_time", "expiration_time")
    if strike is None or close_ts is None:
        log.warning(
            "Market %s missing a strike/close_time field - run --debug-market and check the field names.",
            ticker,
        )
        return None

    seconds_remaining = _to_epoch_seconds(close_ts) - time.time()
    if seconds_remaining < MIN_SECONDS_REMAINING_TO_ENTER:
        return None

    model_prob_yes = fair_value_prob_above_strike(spot, float(strike), seconds_remaining, annualized_vol)

    # yes_ask/yes_bid/no_ask/no_bid (integer cents) are being phased out in
    # favor of yes_ask_dollars/yes_bid_dollars/no_ask_dollars/no_bid_dollars
    # (dollar strings like "0.4200") - _price_float reads either.
    yes_ask = _price_float(market, "yes_ask_dollars", "yes_ask")
    yes_bid = _price_float(market, "yes_bid_dollars", "yes_bid")
    if yes_ask is None:
        return None
    # Kalshi's payloads have varied on whether no_bid/no_ask are given directly
    # or need to be derived from the complementary yes side - handle both.
    no_ask = _price_float(market, "no_ask_dollars", "no_ask")
    if no_ask is None and yes_bid is not None:
        no_ask = 1.0 - yes_bid

    market_prob_yes = yes_ask
    edge_yes = model_prob_yes - market_prob_yes

    edge_no = None
    market_prob_no = None
    if no_ask is not None:
        market_prob_no = no_ask
        edge_no = (1 - model_prob_yes) - market_prob_no

    if edge_yes >= EDGE_THRESHOLD and (edge_no is None or edge_yes >= edge_no):
        price_cents = min(99, dollars_to_cents(yes_ask) + ENTRY_PRICE_PADDING_CENTS)
        return TradeSignal(ticker, "yes", model_prob_yes, market_prob_yes, edge_yes, price_cents)
    if edge_no is not None and edge_no >= EDGE_THRESHOLD:
        price_cents = min(99, dollars_to_cents(no_ask) + ENTRY_PRICE_PADDING_CENTS)
        return TradeSignal(ticker, "no", 1 - model_prob_yes, market_prob_no, edge_no, price_cents)
    return None


# ==========================================================================
# Position management: take-profit / stop-loss / time-based exit
# ==========================================================================

@dataclass
class OpenPosition:
    ticker: str
    side: str
    count: int
    entry_price_cents: int
    opened_at: float


class PositionManager:
    def __init__(self, client: KalshiClient):
        self.client = client
        self.positions: dict = {}
        self.realized_pnl_cents_today = 0

    def open_position(self, signal: TradeSignal, count: int) -> None:
        self.client.place_order(signal.ticker, signal.side, "buy", count, signal.price_cents)
        self.positions[signal.ticker] = OpenPosition(signal.ticker, signal.side, count, signal.price_cents, time.time())
        _log_event({"event": "open", "ticker": signal.ticker, "side": signal.side, "model_prob": signal.model_prob,
                    "market_prob": signal.market_prob, "edge": signal.edge, "price_cents": signal.price_cents, "count": count})

    def check_exits(self, market_lookup: dict) -> None:
        for ticker, pos in list(self.positions.items()):
            market = market_lookup.get(ticker)
            if not market:
                continue

            if pos.side == "yes":
                current_bid_dollars = _price_float(market, "yes_bid_dollars", "yes_bid")
            else:
                current_bid_dollars = _price_float(market, "no_bid_dollars", "no_bid")
                if current_bid_dollars is None:
                    yes_ask_dollars = _price_float(market, "yes_ask_dollars", "yes_ask")
                    if yes_ask_dollars is not None:
                        current_bid_dollars = 1.0 - yes_ask_dollars
            if current_bid_dollars is None:
                continue
            current_bid = dollars_to_cents(current_bid_dollars)

            max_gain = 100 - pos.entry_price_cents
            unrealized = current_bid - pos.entry_price_cents
            gain_fraction = unrealized / max_gain if max_gain > 0 else 0
            loss_fraction = unrealized / pos.entry_price_cents if pos.entry_price_cents > 0 else 0

            close_ts = _first_nonempty(market, "close_time", "expiration_time")
            seconds_remaining = _to_epoch_seconds(close_ts) - time.time() if close_ts is not None else None

            reason = None
            if gain_fraction >= TAKE_PROFIT_PCT:
                reason = "take_profit"
            elif loss_fraction <= STOP_LOSS_PCT:
                reason = "stop_loss"
            elif seconds_remaining is not None and seconds_remaining <= EXIT_BEFORE_SETTLEMENT_SECONDS:
                reason = "time_exit"

            if reason:
                self._close(pos, current_bid, reason)

    def _close(self, pos: OpenPosition, exit_price_cents: int, reason: str) -> None:
        sell_price = max(1, exit_price_cents - ENTRY_PRICE_PADDING_CENTS)
        self.client.place_order(pos.ticker, pos.side, "sell", pos.count, sell_price)
        pnl = (exit_price_cents - pos.entry_price_cents) * pos.count
        self.realized_pnl_cents_today += pnl
        _log_event({"event": "close", "ticker": pos.ticker, "reason": reason, "exit_price_cents": exit_price_cents, "pnl_cents": pnl})
        del self.positions[pos.ticker]

    def daily_loss_limit_hit(self) -> bool:
        return self.realized_pnl_cents_today <= -abs(MAX_DAILY_LOSS_CENTS)

    def reset_daily_counter(self) -> None:
        self.realized_pnl_cents_today = 0


def _log_event(event: dict) -> None:
    event = {"ts": time.time(), **event}
    log.info(json.dumps(event))
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


# ==========================================================================
# Main loop
# ==========================================================================

def run(debug_market_only: bool = False) -> None:
    auth = KalshiAuth(API_KEY_ID, PRIVATE_KEY_PATH) if (API_KEY_ID and PRIVATE_KEY_PATH) else None
    if auth is None:
        log.warning("No API credentials found in the environment - fine for dry-run/paper testing, "
                    "required before any real order can be sent.")
    client = KalshiClient(BASE_URL, auth)
    vol_estimator = VolatilityEstimator(VOL_LOOKBACK_SECONDS, VOL_SAMPLE_SECONDS)
    positions = PositionManager(client)

    log.info("Starting. USE_DEMO=%s DRY_RUN=%s series=%s", USE_DEMO, DRY_RUN, SERIES_TICKER)

    last_sample_time = 0.0
    current_utc_date = datetime.datetime.now(datetime.timezone.utc).date()

    while True:
        try:
            markets = client.get_open_btc_markets()

            if debug_market_only:
                if not markets:
                    log.info("No market with status == 'active' closing in the next 20 minutes for series %s. "
                              "If this keeps happening, double check SERIES_TICKER is still correct and that "
                              "min_close_ts/max_close_ts are being accepted (see the raw response by adding a "
                              "print(data) in get_open_btc_markets).",
                              SERIES_TICKER)
                    return
                ticker = markets[0]["ticker"]
                log.info("Active market from the LIST endpoint (GET /markets):\n%s", json.dumps(markets[0], indent=2))
                detail = client.get_market_detail(ticker)
                log.info("Same market from the DETAIL endpoint (GET /markets/%s):\n%s", ticker, json.dumps(detail, indent=2))
                log.info("Confirm these field names appear somewhere above: ticker, floor_strike (or a "
                         "_dollars variant), close_time, yes_bid_dollars, yes_ask_dollars, no_bid_dollars, no_ask_dollars.")
                return

            today = datetime.datetime.now(datetime.timezone.utc).date()
            if today != current_utc_date:
                positions.reset_daily_counter()
                current_utc_date = today

            spot = fetch_btc_spot_price()
            now = time.time()
            if now - last_sample_time >= VOL_SAMPLE_SECONDS:
                vol_estimator.add_sample(now, spot)
                last_sample_time = now

            annualized_vol = vol_estimator.annualized_vol(FALLBACK_ANNUALIZED_VOL)
            # Enrich with the detail endpoint too, since floor_strike was
            # missing from the list endpoint in testing - merge it in so
            # evaluate_market() has the best chance of finding it.
            market_lookup = {}
            for m in markets:
                try:
                    detail = client.get_market_detail(m["ticker"])
                except requests.HTTPError:
                    detail = {}
                market_lookup[m["ticker"]] = {**m, **detail}

            positions.check_exits(market_lookup)

            if not positions.daily_loss_limit_hit() and len(positions.positions) < MAX_OPEN_POSITIONS:
                for market in market_lookup.values():
                    signal = evaluate_market(market, spot, annualized_vol)
                    if signal:
                        log.info(
                            "Signal: %s side=%s model=%.3f market=%.3f edge=%.3f",
                            signal.ticker, signal.side, signal.model_prob, signal.market_prob, signal.edge,
                        )
                        positions.open_position(signal, MAX_CONTRACTS_PER_TRADE)
                        break
            elif positions.daily_loss_limit_hit():
                log.warning("Daily loss limit hit - not opening new positions for the rest of today (UTC).")

        except requests.HTTPError as e:
            log.error("HTTP error: %s", e)
        except Exception:
            log.exception("Unexpected error in main loop - continuing after sleep.")

        time.sleep(POLL_SECONDS)


# ==========================================================================
# Offline self-test - exercises the math/decision logic with synthetic
# data and zero network calls, so you can sanity-check behavior before
# ever touching real credentials or the internet.
# ==========================================================================

def _self_test() -> None:
    assert abs(normal_cdf(0.0) - 0.5) < 1e-9
    assert normal_cdf(5.0) > 0.999
    assert normal_cdf(-5.0) < 0.001

    # Spot well above strike with plenty of time and low vol -> high prob "yes".
    p_high = fair_value_prob_above_strike(spot=65000, strike=64000, seconds_remaining=600, annualized_vol=0.4)
    assert p_high > 0.7, p_high

    # Spot well below strike, same conditions -> low prob "yes".
    p_low = fair_value_prob_above_strike(spot=63000, strike=64000, seconds_remaining=600, annualized_vol=0.4)
    assert p_low < 0.3, p_low

    # Spot exactly at strike -> ~50/50 regardless of vol/time.
    p_mid = fair_value_prob_above_strike(spot=64000, strike=64000, seconds_remaining=600, annualized_vol=0.4)
    assert abs(p_mid - 0.5) < 1e-6, p_mid

    # A market where the model thinks "yes" is much more likely than the
    # market is pricing it should generate a buy-yes signal. Field names/types
    # here intentionally mirror the real (dollar-string) API response.
    mispriced_market = {
        "ticker": "KXBTC15M-TEST-UP",
        "floor_strike": 64000,
        "close_time": time.time() + 600,
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.42",
        "no_bid_dollars": "0.57",
        "no_ask_dollars": "0.60",
    }
    signal = evaluate_market(mispriced_market, spot=65200, annualized_vol=0.35)
    assert signal is not None and signal.side == "yes", signal
    assert signal.price_cents == 43, signal.price_cents  # 42c ask + 1c padding

    # A fairly priced market (model ~= market) should NOT generate a signal.
    fair_market = {
        "ticker": "KXBTC15M-TEST-FAIR",
        "floor_strike": 64000,
        "close_time": time.time() + 600,
        "yes_bid_dollars": "0.49",
        "yes_ask_dollars": "0.51",
        "no_bid_dollars": "0.48",
        "no_ask_dollars": "0.50",
    }
    no_signal = evaluate_market(fair_market, spot=64010, annualized_vol=0.35)
    assert no_signal is None, no_signal

    # Legacy integer-cent fields should still parse correctly too, in case an
    # account/endpoint still returns them instead of the _dollars fields.
    legacy_cents_market = dict(mispriced_market)
    del legacy_cents_market["yes_bid_dollars"], legacy_cents_market["yes_ask_dollars"]
    del legacy_cents_market["no_bid_dollars"], legacy_cents_market["no_ask_dollars"]
    legacy_cents_market.update({"yes_bid": 40, "yes_ask": 42, "no_bid": 57, "no_ask": 60})
    legacy_signal = evaluate_market(legacy_cents_market, spot=65200, annualized_vol=0.35)
    assert legacy_signal is not None and legacy_signal.side == "yes", legacy_signal

    # Position manager: take-profit should trigger and clear the position.
    class _FakeClient:
        def place_order(self, *args, **kwargs):
            return {"order": {"order_id": "test"}}

    pm = PositionManager(_FakeClient())
    pm.positions["KXBTC15M-TEST-UP"] = OpenPosition("KXBTC15M-TEST-UP", "yes", 5, 40, time.time())
    pm.check_exits({"KXBTC15M-TEST-UP": {"yes_bid_dollars": "0.60", "close_time": time.time() + 300}})
    assert "KXBTC15M-TEST-UP" not in pm.positions
    assert pm.realized_pnl_cents_today == (60 - 40) * 5

    print("All self-tests passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug-market", action="store_true", help="Print one sample market payload and exit (no trading).")
    parser.add_argument("--self-test", action="store_true", help="Run offline logic checks with no network calls and exit.")
    args = parser.parse_args()

    if args.self_test:
        _self_test()
    else:
        run(debug_market_only=args.debug_market)
