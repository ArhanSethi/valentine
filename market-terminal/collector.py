"""Collect live stock (IEX) and crypto trades from Alpaca into SQLite.

Usage:
    python collector.py          # live Alpaca streams (needs .env keys)
    python collector.py --demo   # synthetic random-walk trades, no keys needed
"""

import argparse
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone

from config import CRYPTO_SYMBOLS, DB_PATH, STOCK_SYMBOLS
from db import DbWriter


def utc_iso(dt):
    """Normalize to a fixed-width UTC ISO string so text sorting is time sorting."""
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class FeedMonitor:
    """Tracks per-feed freshness in memory and publishes it once a second."""

    def __init__(self, writer):
        self.writer = writer
        self.lock = threading.Lock()
        self.last_message = {}
        self.state = {}

    def set_state(self, feed, state, detail=None):
        with self.lock:
            self.state[feed] = (state, detail)
        self.writer.set_status(feed, state, detail=detail)

    def touch(self, feed):
        with self.lock:
            self.last_message[feed] = utc_iso(datetime.now(timezone.utc))
            state, _ = self.state.get(feed, ("connecting", None))
            if state == "connecting":
                self.state[feed] = ("streaming", None)

    def run(self):
        while True:
            time.sleep(1)
            with self.lock:
                snapshot = {
                    feed: (state, detail, self.last_message.get(feed))
                    for feed, (state, detail) in self.state.items()
                }
            for feed, (state, detail, last) in snapshot.items():
                self.writer.set_status(feed, state, last_message=last, detail=detail)


def make_handler(writer, monitor, market):
    async def on_trade(trade):
        writer.add_trade(
            trade.symbol,
            utc_iso(trade.timestamp),
            float(trade.price),
            float(trade.size),
            market,
        )
        monitor.touch(market)
        print(f"{market:6} {trade.symbol:8} {float(trade.price):>12,.4f}  x {float(trade.size):g}")

    return on_trade


def run_stream_forever(feed, build_stream, monitor):
    """Run an Alpaca stream; if it dies (bad keys, network loss), retry with backoff.

    alpaca-py already reconnects on ordinary socket drops. This outer loop
    covers the cases where run() returns or raises instead.
    """
    delay = 2
    while True:
        monitor.set_state(feed, "connecting")
        try:
            build_stream().run()
            detail = "stream stopped"
        except Exception as exc:  # noqa: BLE001 - keep the collector alive
            detail = f"{type(exc).__name__}: {exc}"
        monitor.set_state(feed, "error", detail)
        print(f"[{feed}] {detail}; retrying in {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 60)


def run_live(writer, monitor):
    from dotenv import load_dotenv
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import CryptoDataStream, StockDataStream

    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise SystemExit(
            "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY. Copy .env.example to .env "
            "and add your keys, or run with --demo."
        )

    def build_stock_stream():
        # feed must be the DataFeed enum; the plain string "iex" fails inside alpaca-py.
        stream = StockDataStream(key, secret, feed=DataFeed.IEX)
        stream.subscribe_trades(make_handler(writer, monitor, "stock"), *STOCK_SYMBOLS)
        return stream

    def build_crypto_stream():
        stream = CryptoDataStream(key, secret)
        stream.subscribe_trades(make_handler(writer, monitor, "crypto"), *CRYPTO_SYMBOLS)
        return stream

    threading.Thread(
        target=run_stream_forever,
        args=("stock", build_stock_stream, monitor),
        name="stock-stream",
        daemon=True,
    ).start()
    run_stream_forever("crypto", build_crypto_stream, monitor)


def run_demo(writer, monitor):
    """Emit fake trades so you can build and test the dashboard offline."""
    start_prices = {"AAPL": 230, "NVDA": 140, "SPY": 590, "QQQ": 510, "TSLA": 340,
                    "BTC/USD": 95_000, "ETH/USD": 3_300}
    prices = {s: float(start_prices.get(s, 100)) for s in STOCK_SYMBOLS + CRYPTO_SYMBOLS}
    for feed in ("stock", "crypto"):
        monitor.set_state(feed, "demo", "synthetic data, not real prices")
    while True:
        symbol = random.choice(list(prices))
        market = "crypto" if symbol in CRYPTO_SYMBOLS else "stock"
        prices[symbol] *= 1 + random.gauss(0, 0.0004)
        size = round(random.uniform(0.001, 0.5), 4) if market == "crypto" else random.randint(1, 300)
        writer.add_trade(symbol, utc_iso(datetime.now(timezone.utc)), prices[symbol], size, market)
        monitor.touch(market)
        time.sleep(random.uniform(0.02, 0.15))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="generate synthetic trades instead of connecting to Alpaca")
    args = parser.parse_args()
    # Surfaces alpaca-py's connect, auth, and reconnect messages in the terminal.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    writer = DbWriter(DB_PATH)
    writer.start()
    writer.ready.wait()

    monitor = FeedMonitor(writer)
    threading.Thread(target=monitor.run, name="feed-monitor", daemon=True).start()

    try:
        (run_demo if args.demo else run_live)(writer, monitor)
    except KeyboardInterrupt:
        print("Stopping collector.")
    finally:
        time.sleep(0.5)  # let the writer flush its last batch


if __name__ == "__main__":
    main()
