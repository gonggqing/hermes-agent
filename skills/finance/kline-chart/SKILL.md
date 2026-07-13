---
name: kline-chart
description: >
  Use ONLY when the user asks about ONE specific stock/ticker in detail and a
  visual is clearly warranted — e.g. "show me NVDA's chart", "看看 0700.HK 的K线",
  "how has TSLA moved this quarter", "chart 600519.SS". Renders a candlestick /
  K-line PNG and returns it as a chat image. Do NOT use for: multi-symbol scans
  or comparisons, quick one-line price checks ("what's NVDA at?"), watchlist
  overviews, or market-wide/macro questions — those stay text-only to save
  tokens. One ticker + intent to see the trend = use this; otherwise don't.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, stocks, kline, candlestick, chart, hk, us, cn]
    category: finance
    requires_toolsets: [finance]
    related_skills: [stocks]
---

# K-line Chart

Render a candlestick / K-line chart for a single stock and show it in chat.

## When to use
- Exactly ONE ticker, and the user wants to SEE the trend / detail.
- US (NVDA, TSLA), Hong Kong (0700.HK), or mainland (600519.SS, 300750.SZ).

## When NOT to use (save tokens — stay text-only)
- More than one ticker, or a comparison/scan.
- A quick price check or a one-line answer — use `get_quote` instead.
- Index / macro / "how's the market" questions.

## How to run (token-frugal — do NOT call get_kline)
`get_kline` returns the full OHLCV array, which would flood the transcript.
Instead run the bundled script: it fetches the bars itself and prints only a
path + a one-line summary. Use the env-agnostic skills path so it works both in
the gateway container and on a host CLI:

    python3 "${HERMES_HOME:-$HOME/.hermes}/skills/finance/kline-chart/scripts/render_kline.py" <SYMBOL> --timeframe 1d --limit 120

Run it with the `terminal` tool. It prints one JSON line, e.g.:

    {"chart": "/tmp/hermes_kline/NVDA_1d.png", "summary": "NVDA 120 bars 1d: last 210.96 (+3.1% over range) | range 140-212"}

`--timeframe` accepts `1d` / `1h` / `1wk`; `--limit` is the number of most-recent
bars (default 120). Pick a timeframe/limit that fits the user's question
(e.g. `--timeframe 1wk --limit 104` for a ~2-year weekly view).

## Reply
1. Post the one-line `summary`.
2. On its own line, reference the image so the chat renders it:

       MEDIA:/tmp/hermes_kline/NVDA_1d.png

   (Use the exact `chart` path the script printed.)
3. Optionally add a short written read by calling `analyze_symbol` or
   `get_quote` (small JSON — fine in context). NEVER paste the OHLCV rows.

## On error
If the script prints `{"error": ...}`, tell the user briefly (e.g. bad ticker or
the finance service is unavailable). If it prints an `error` plus a `summary`,
give the text summary without the image.

## Verification
- [ ] Exactly one ticker, and a visual was actually wanted.
- [ ] Only the chart path + short summary entered context (no bars array).
- [ ] The reply contains `MEDIA:<absolute path>.png`.
