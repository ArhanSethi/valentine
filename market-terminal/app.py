"""Live market terminal dashboard (first checkpoint: raw trade chart)."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import CRYPTO_SYMBOLS, DB_PATH, STOCK_SYMBOLS

STALE_AFTER = timedelta(seconds=10)

st.set_page_config(page_title="Market Terminal", layout="wide")
st.title("Live Market Terminal")


def read_db(query, params=()):
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        try:
            return pd.read_sql_query(query, db, params=params)
        except Exception:  # tables not created yet
            return pd.DataFrame()


# Controls live outside the fragment so the selection doesn't reset every second.
left, right = st.columns([2, 1])
symbol = left.selectbox("Symbol", STOCK_SYMBOLS + CRYPTO_SYMBOLS)
window_minutes = right.select_slider(
    "Window (minutes)", options=[1, 5, 15, 30, 60, 240, 1440], value=15
)


def render_feed_status():
    status = read_db("SELECT feed, state, last_message, detail FROM feed_status ORDER BY feed")
    if status.empty:
        st.warning("Collector isn't running. Start it with `python collector.py`.")
        return
    now = datetime.now(timezone.utc)
    cols = st.columns(len(status))
    for col, row in zip(cols, status.itertuples()):
        if row.last_message:
            age = now - pd.to_datetime(row.last_message, utc=True).to_pydatetime()
            age_text = f"last trade {age.total_seconds():.0f}s ago"
            fresh = age <= STALE_AFTER
        else:
            age_text, fresh = "no trades yet", False
        icon = "🟢" if fresh and row.state in ("streaming", "demo") else "🟡" if row.state != "error" else "🔴"
        label = f"{icon} **{row.feed}**: {row.state}, {age_text}"
        if row.detail:
            label += f" ({row.detail})"
        col.markdown(label)


@st.fragment(run_every="1s")
def dashboard():
    render_feed_status()

    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).isoformat(
        timespec="microseconds"
    )
    data = read_db(
        """
        SELECT timestamp, price, size FROM trades
        WHERE symbol = ? AND timestamp >= ?
        ORDER BY timestamp DESC LIMIT 50000
        """,
        (symbol, since),
    )
    if data.empty:
        st.info(
            f"No {symbol} trades in the last {window_minutes} minutes. "
            "Stocks only trade on IEX during market sessions; crypto trades around the clock."
        )
        return

    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, format="ISO8601")
    data = data.sort_values("timestamp")
    data["return"] = data["price"].pct_change() * 100
    latest = data.iloc[-1]
    first = data.iloc[0]
    window_change = (latest["price"] / first["price"] - 1) * 100

    a, b, c, d = st.columns(4)
    a.metric("Last price", f"${latest['price']:,.2f}", f"{window_change:+.3f}% in window")
    b.metric("Latest trade size", f"{latest['size']:,.4f}")
    c.metric(
        "Latest trade return",
        "n/a" if pd.isna(latest["return"]) else f"{latest['return']:.4f}%",
    )
    d.metric("Trades in window", f"{len(data):,}")

    # Plotly instead of st.line_chart: its y-axis auto-scales instead of starting at zero.
    fig = go.Figure(
        go.Scatter(x=data["timestamp"], y=data["price"], mode="lines", line_shape="hv", name=symbol)
    )
    fig.update_layout(
        height=420,
        margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="Price (USD)", tickformat=",.2f"),
        xaxis=dict(title=None),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True, key="price_chart")
    st.caption(f"Last trade: {latest['timestamp']:%Y-%m-%d %H:%M:%S.%f} UTC")


dashboard()
