# Live market terminal

A free, locally hosted market terminal for US stocks, ETFs, and crypto. A Python
collector streams trades from Alpaca into SQLite, and a Streamlit dashboard
refreshes every second.

This is the first checkpoint: live streams connected and trades appearing in the
dashboard. One-second OHLCV candles, indicators, options, and alerts come next.

## Files

| File | Purpose |
| --- | --- |
| `collector.py` | Streams IEX stock trades and crypto trades into `market.db`. `--demo` generates fake trades. |
| `app.py` | Streamlit dashboard: feed status, latest metrics, and a live price chart. |
| `db.py` | SQLite schema and the single writer thread. |
| `config.py` | Watchlist symbols and database path. |

## Set up (Windows PowerShell)

1. Install Python 3.11 or newer.
2. In this folder, create a virtual environment and install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

   If PowerShell blocks activation, run `.venv\Scripts\activate.bat` in Command Prompt instead.

3. Create an Alpaca account, generate paper-trading API keys, then copy
   `.env.example` to `.env` and fill in `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`.
   The `.gitignore` in this folder keeps `.env` and `market.db` out of Git.

## Run

Open two terminals in this folder with the virtual environment activated.

Terminal 1, collect data:

```powershell
python collector.py
```

Terminal 2, start the dashboard:

```powershell
python -m streamlit run app.py
```

Then open http://localhost:8501.

To try the dashboard without keys, or while the stock market is closed, run
`python collector.py --demo` instead. Demo prices are random and labeled as
synthetic in the dashboard.

## What changed from the starter code

- **Thread-safe writes.** Stream callbacks put rows on a queue, and one writer
  thread owns the only SQLite connection and commits in batches. That replaces the
  `check_same_thread=False` plus lock workaround.
- **IEX feed enum.** `StockDataStream(..., feed="iex")` fails inside alpaca-py
  because it calls `feed.value`. The collector passes `DataFeed.IEX`.
- **Reconnects and feed status.** Each stream restarts with backoff if it exits,
  and a `feed_status` table records state and the time of the last trade. The
  dashboard shows each feed as fresh, stale, or in error.
- **WAL mode and an index.** The dashboard reads while the collector writes, and
  queries by `(symbol, timestamp)` stay fast as the table grows.
- **Per-symbol time window.** The dashboard queries one symbol over a chosen
  window instead of the latest 10,000 trades overall, so busy crypto feeds don't
  push stock trades out of view.
- **Stable controls and scaled chart.** The symbol picker sits outside the
  one-second fragment so it doesn't reset, and the Plotly chart auto-scales the
  y-axis instead of starting at zero.
- **Consistent timestamps.** Trades are stored as fixed-width UTC ISO strings,
  so text order matches time order.

## Data limitations

- The free Basic plan streams IEX only, which is a small share of total US
  stock volume. Prices match the market closely, but volume is far below the
  consolidated tape.
- Stocks only trade on IEX during market sessions. The dashboard shows no new
  stock trades outside those hours, and it never fills gaps with invented prices.
- Crypto trades around the clock.
- Free options data is indicative only, without full OPRA coverage. That's a
  separate module in a later milestone.
