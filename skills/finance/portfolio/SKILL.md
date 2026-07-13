---
name: portfolio
description: >
  Use whenever the user asks about THEIR REAL portfolio / holdings / positions /
  P&L, or reports a real trade to record — e.g. "看看我们当前持仓", "我的持仓/仓位",
  "portfolio", "holdings", "我赚了多少/盈亏", "总市值", "我的账户里有什么", "今天买了 3 股
  NVDA @208.5", "把 510300 清了", "记一笔交易", "update my holdings". Routes to the
  REAL Portfolio Journal tools (their actual money across 蚂蚁财富/平安证券/IBKR etc.).
  Do NOT confuse this with the PAPER trading simulation (account_view) — when the
  user says 持仓/portfolio they mean their REAL holdings, not the paper account.
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, portfolio, holdings, 持仓, 交易, pnl, positions]
    category: finance
    requires_toolsets: [finance]
    related_skills: [kline-chart]
---

# Portfolio (real holdings) — view & record

The user has a **real, multi-account Portfolio Journal** (their actual money —
e.g. 蚂蚁财富 场外基金, 平安证券 场内 ETF, later an IBKR account). It is stored in a
SQL database behind the Finance service and is **completely separate** from the
PAPER trading simulation.

**Critical distinction — do not mix these up:**

| The user means… | Use | NOT |
|---|---|---|
| 我的持仓 / portfolio / holdings / 盈亏 / 市值 / 我的账户 | `portfolio_valuation`, `portfolio_holdings`, `portfolio_accounts` | ~~account_view~~ |
| the system's paper trading sim / 模拟盘 / 候选审批 | `account_view` | — |

If you catch yourself running `ls`/`find`/reading files to answer a holdings
question, STOP — use the finance tools below. Never report the paper account's
$2,000 sim balance when the user asked about their real 持仓.

## Viewing holdings ("看看我们当前持仓", "我赚了多少")

1. Call **`portfolio_valuation`** (omit `account_id`) → the aggregated real
   portfolio with **市值 / 成本 / 盈亏 / 盈亏%** per holding + per-currency totals.
   For one account, first `portfolio_accounts` to get its id, then
   `portfolio_valuation` with that `account_id`.
2. Present clearly in Chinese, grouped by account, e.g.:
   - a totals line: 总市值 / 总成本 / 总盈亏 (+/−, %) with currency (mostly CNY).
   - a per-holding table: 名称/代码 · 份额 · 成本 · 现价 · 市值 · 盈亏(%).
3. **Unknown price is not zero.** When `price`/`market_value`/`unrealized_pnl`
   is `null` (some 场外基金 have no live NAV feed), show **"现价未知"** and exclude
   them from the P&L total — say how many are unpriced. Never invent a number.
4. `portfolio_holdings` gives raw 份额+成本 only (no P&L) — prefer
   `portfolio_valuation` unless the user only wants quantities.

## Recording / updating a trade ("今天买了…", "清仓", "记一笔")

You may **draft** a change but you can **never** commit it — an authenticated
human must confirm it in the Finance UI or Telegram (this is a hard rule).

1. Parse the user's statement into a draft and call **`draft_portfolio_trade`**
   (`event_type` buy/sell/dividend/fee/split/opening_balance, `symbol`, `qty`,
   `price`, `account_id`, `original_text`, …). For a full exit ("清仓/全卖了")
   use **`draft_close_position`** (it derives the quantity from current holdings).
2. **Never guess.** If the account, symbol, quantity, price, currency, or whether
   the order actually FILLED (vs just placed) is unclear, ASK — pass what's
   uncertain in `ambiguities`. Unknown price is allowed (cost basis stays unknown).
3. After drafting, tell the user plainly: *"已生成草稿,请在 Finance 页面/Telegram 确认
   后才会记入持仓"* — and, if fields were missing, what you still need from them.

## Guardrails
- READ + DRAFT only. There is no tool to confirm/commit/place an order here, by
  design — do not attempt to, and do not claim a holding was updated until the
  user confirms the draft.
- Symbols: US `NVDA`, HK `0700.HK`, 上海 `600519.SS` / `510300.SS`, 深圳
  `000001.SZ` / `159813.SZ`, 场外基金 = its fund code (e.g. `017436`).
