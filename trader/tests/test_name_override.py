"""Tests for user-set display-name overrides + rename parsing (name_override.py)."""

from __future__ import annotations

from swing_trader.name_override import NameOverrideStore, parse_rename


def test_store_set_get_all_clear(tmp_path):
    st = NameOverrideStore(url=f"sqlite:///{tmp_path/'n.db'}")
    st.set("159518.SZ", "标普油气ETF嘉实")
    assert st.get("159518.SZ") == "标普油气ETF嘉实"
    assert st.get("159518.sz") == "标普油气ETF嘉实"  # case-insensitive key
    assert st.all() == {"159518.SZ": "标普油气ETF嘉实"}
    st.set("159518.SZ", "改一次")  # upsert
    assert st.get("159518.SZ") == "改一次"
    st.set("159518.SZ", "")  # empty clears
    assert st.get("159518.SZ") is None and st.all() == {}


def test_parse_rename_symbol_then_keyword():
    assert parse_rename("159518 改名 标普油气ETF嘉实") == ("159518.SZ", "标普油气ETF嘉实")


def test_parse_rename_keyword_then_symbol():
    assert parse_rename("改名 159518 标普油气ETF嘉实") == ("159518.SZ", "标普油气ETF嘉实")


def test_parse_rename_natural_phrasing():
    assert parse_rename("把 159518 名字改成 标普油气ETF嘉实") == (
        "159518.SZ", "标普油气ETF嘉实")


def test_parse_rename_us_ticker():
    assert parse_rename("NVDA 改名 英伟达") == ("NVDA", "英伟达")


def test_parse_rename_not_a_rename():
    assert parse_rename("卖了 159813 200股 @1.893") is None
    assert parse_rename("159518 现在多少钱") is None
    assert parse_rename("改名") is None  # no symbol
