"""Tests for the watchlist universe (Loop.md §11)."""

from swing_trader import watchlist
from swing_trader.schemas import AiPhase, Role
from swing_trader.watchlist import UNIVERSE, by_phase, by_role, enabled_symbols, get


def test_no_duplicate_symbols():
    symbols = [i.symbol for i in UNIVERSE]
    assert len(symbols) == len(set(symbols))


def test_loop_md_section_11_symbols_present():
    expected = set(
        "SPY VOO IVV DIA QQQ VTI "
        "NVDA AMD AVGO MRVL TSM ASML AMAT LRCX KLAC "
        "MU WDC SNDK "
        "ANET CIEN LITE COHR CRDO "
        "SMCI DELL VRT ETN GEV CEG VST CCJ URA EQIX DLR "
        "MSFT AMZN GOOGL META ORCL "
        "PLTR NOW CRM SNOW DDOG CRWD ADBE IGV WCLD SKYY "
        "XBI IBB IWM "
        "XLE XOP XOM CVX GLD IAU TLT IEF".split()
    )
    have = {i.symbol for i in UNIVERSE}
    assert expected <= have


def test_crypto_disabled_until_osl_confirmed():
    """Loop.md §11-I: crypto only after OSL permission + API support confirmed."""
    for sym in ("BTC-USD", "ETH-USD"):
        item = get(sym)
        assert item is not None and item.enabled is False
        assert sym not in enabled_symbols()


def test_base_indices_are_core():
    for sym in ("SPY", "QQQ", "DIA", "VTI"):
        assert get(sym).role is Role.CORE


def test_conviction_layer_is_ai_infra():
    for item in by_role(Role.CONVICTION):
        assert item.ai_phase in (AiPhase.INFRA, AiPhase.MEMORY, AiPhase.NETWORK)


def test_hedges_uncorrelated_to_ai():
    hedges = by_role(Role.HEDGE)
    assert {i.symbol for i in hedges} == {"XLE", "XOP", "XOM", "CVX", "GLD", "IAU", "TLT", "IEF"}
    assert all(i.ai_phase is AiPhase.NONE for i in hedges)


def test_application_phase_watchable():
    apps = by_phase(AiPhase.APPLICATION)
    assert {"PLTR", "IGV"} <= {i.symbol for i in apps}


def test_get_normalizes_case():
    assert get(" nvda ") is not None
    assert get("nvda").symbol == "NVDA"


def test_unknown_symbol_returns_none():
    assert get("ZZZZZ") is None


def test_items_frozen():
    import pytest

    with pytest.raises(Exception):
        get("NVDA").role = Role.HEDGE
