"""Human display names for research-universe symbols (Loop.md §11 research UI).

A curated, VERIFIED symbol → name map so the Market/research desks show a name
next to opaque codes (513310, 005930.KS, 688981.SS…) instead of just the code.
Names for the CN/HK/KR tech majors + indices are in Chinese (the operator reads
CN/HK/KR names best); US tickers are self-explanatory so they are omitted (the
brief falls back to the code). Only names verified with reasonable confidence
are listed — an unknown symbol returns "" (code-only), never a guessed name.

Extend here (not per-watchlist) so names live in one auditable place.
"""

from __future__ import annotations

#: symbol (UPPERCASE) → display name. Kept in sync with cn_watchlist / kr_watchlist.
SYMBOL_NAMES: dict[str, str] = {
    # --- KR semiconductors (kr_watchlist) ---
    "005930.KS": "三星电子",
    "000660.KS": "SK海力士",
    "042700.KS": "韩美半导体",
    "000990.KS": "DB HiTek",
    "^KS11": "韩国综合指数",
    # --- CN / HK tech majors (cn_watchlist) ---
    "0700.HK": "腾讯控股",
    "3690.HK": "美团",
    "9988.HK": "阿里巴巴",
    "9618.HK": "京东集团",
    "1810.HK": "小米集团",
    "0992.HK": "联想集团",
    "0981.HK": "中芯国际",
    "1347.HK": "华虹半导体",
    "2382.HK": "舜宇光学",
    "1211.HK": "比亚迪股份",
    "2015.HK": "理想汽车",
    "3033.HK": "南方恒生科技ETF",
    "2800.HK": "盈富基金",
    "688981.SS": "中芯国际",
    "688256.SS": "寒武纪",
    "002049.SZ": "紫光国微",
    "002415.SZ": "海康威视",
    "000725.SZ": "京东方A",
    "300750.SZ": "宁德时代",
    "002594.SZ": "比亚迪",
    "600519.SS": "贵州茅台",
    "^HSI": "恒生指数",
    "^HSCE": "恒生中国企业指数",
}


def name_for(symbol: str) -> str:
    """Display name for a symbol, or "" when none is known (never guessed)."""
    if not symbol:
        return ""
    return SYMBOL_NAMES.get(symbol.strip().upper(), "")
