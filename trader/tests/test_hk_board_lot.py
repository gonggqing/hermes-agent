"""HK board-lot arithmetic + injected lookup (Loop.md §5.7, offline)."""

import pytest

from swing_trader.hk_board_lot import (
    BoardLotTable,
    is_valid_lot,
    lot_violation,
    round_down_to_lot,
)


class TestLotMath:
    def test_valid_multiples(self):
        assert is_valid_lot(300, 100) is True
        assert is_valid_lot(100, 100) is True
        assert is_valid_lot(1000, 500) is True

    def test_invalid_multiples(self):
        assert is_valid_lot(350, 100) is False
        assert is_valid_lot(0, 100) is False
        assert is_valid_lot(50, 100) is False
        assert is_valid_lot(250.5, 100) is False

    def test_round_down(self):
        assert round_down_to_lot(350, 100) == 300
        assert round_down_to_lot(99, 100) == 0     # below one lot
        assert round_down_to_lot(1000, 500) == 1000

    def test_lot_violation_message(self):
        assert lot_violation(300, 100) is None
        msg = lot_violation(350, 100)
        assert msg and "board lot" in msg

    def test_nonpositive_lot_raises(self):
        with pytest.raises(ValueError, match="positive"):
            is_valid_lot(100, 0)
        with pytest.raises(ValueError, match="positive"):
            round_down_to_lot(100, -5)


class TestBoardLotTable:
    def test_unknown_symbol_returns_none(self):
        table = BoardLotTable()
        assert table.get("0700.HK") is None
        assert table.known("0700.HK") is False

    def test_set_and_get_case_insensitive(self):
        table = BoardLotTable()
        table.set("0700.HK", 100)
        assert table.get("0700.hk") == 100
        assert table.known("0700.HK") is True

    def test_from_config(self):
        table = BoardLotTable.from_config({"0700.HK": 100, "9988.HK": 100})
        assert table.get("9988.HK") == 100

    def test_set_rejects_nonpositive(self):
        with pytest.raises(ValueError, match="positive"):
            BoardLotTable().set("0700.HK", 0)
