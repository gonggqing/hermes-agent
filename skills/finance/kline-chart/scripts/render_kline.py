#!/usr/bin/env python3
"""Render a K-line / candlestick PNG for ONE symbol (Loop.md Phase 0.75-B3).

Token-frugal by design: this script fetches the OHLCV bars from the local
Finance service ITSELF and prints ONLY a chart path plus a one-line summary —
the bulky bars never enter the agent's transcript. The agent then replies with
the summary and `MEDIA:<path>` so the chat surface renders the image.

Usage:
    render_kline.py SYMBOL [--timeframe 1d] [--limit 120]

Output (stdout, single JSON line):
    {"chart": "/abs/path.png", "summary": "NVDA 120d 1d: last 210.96 ..."}
  or on failure:
    {"error": "..."}            (exit code 1)

Dependencies: stdlib + Pillow (present in the Hermes runtime). No network libs
beyond urllib; no charting library required.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

SERVICE_URL = os.environ.get("HERMES_FINANCE_SERVICE_URL", "http://127.0.0.1:9319").rstrip("/")
OUT_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "hermes_kline")

W, H = 960, 500
PAD_L, PAD_R, PAD_T, PAD_B = 64, 16, 36, 28
UP = (38, 166, 91)     # green
DOWN = (219, 68, 55)   # red
GRID = (60, 63, 70)
BG = (22, 24, 28)
FG = (210, 214, 220)


def _fetch_bars(symbol: str, timeframe: str, limit: int) -> list[dict]:
    q = urllib.parse.urlencode({"symbol": symbol, "timeframe": timeframe, "limit": limit})
    url = f"{SERVICE_URL}/v1/bars?{q}"
    with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 (local service)
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("bars", [])


def _summary(symbol: str, timeframe: str, bars: list[dict]) -> str:
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    last, first = closes[-1], closes[0]
    chg = (last - first) / first * 100.0 if first else 0.0
    return (f"{symbol} {len(bars)} bars {timeframe}: last {last:g} "
            f"({chg:+.1f}% over range) | range {min(lows):g}-{max(highs):g}")


def _render(symbol: str, timeframe: str, bars: list[dict], path: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    pmax, pmin = max(highs), min(lows)
    span = (pmax - pmin) or (pmax or 1.0)
    plot_w = W - PAD_L - PAD_R
    plot_h = H - PAD_T - PAD_B

    def y(price: float) -> float:
        return PAD_T + (pmax - price) / span * plot_h

    # horizontal price grid + labels (5 lines)
    for i in range(5):
        price = pmax - span * i / 4
        yy = y(price)
        d.line([(PAD_L, yy), (W - PAD_R, yy)], fill=GRID, width=1)
        d.text((4, yy - 6), f"{price:.2f}", fill=FG, font=font)

    n = len(bars)
    slot = plot_w / max(n, 1)
    cw = max(1, int(slot * 0.6))
    for i, b in enumerate(bars):
        cx = PAD_L + slot * (i + 0.5)
        color = UP if b["close"] >= b["open"] else DOWN
        # wick
        d.line([(cx, y(b["high"])), (cx, y(b["low"]))], fill=color, width=1)
        # body
        oy, cy = y(b["open"]), y(b["close"])
        top, bot = min(oy, cy), max(oy, cy)
        if bot - top < 1:
            bot = top + 1
        d.rectangle([cx - cw / 2, top, cx + cw / 2, bot], fill=color)

    last = bars[-1]["close"]
    d.text((PAD_L, 8), f"{symbol}  {timeframe}  last {last:g}", fill=FG, font=font)
    img.save(path, "PNG")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--limit", type=int, default=120)
    args = ap.parse_args()

    symbol = args.symbol.strip().upper()
    limit = max(5, min(500, args.limit))
    try:
        bars = _fetch_bars(symbol, args.timeframe, limit)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"could not fetch bars for {symbol}: {exc}"}))
        return 1
    if not bars:
        print(json.dumps({"error": f"no bars returned for {symbol}"}))
        return 1

    os.makedirs(OUT_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in symbol)
    path = os.path.join(OUT_DIR, f"{safe}_{args.timeframe}.png")
    try:
        _render(symbol, args.timeframe, bars, path)
    except Exception as exc:  # noqa: BLE001
        # Rendering failed (e.g. Pillow missing) — still give the text summary.
        print(json.dumps({"error": f"chart render failed ({exc})",
                          "summary": _summary(symbol, args.timeframe, bars)}))
        return 1

    print(json.dumps({"chart": path, "summary": _summary(symbol, args.timeframe, bars)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
