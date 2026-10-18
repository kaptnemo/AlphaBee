"""A 股板块涨跌停映射（P0 设计 §5）单测：纯函数、前缀判定、ST、未知前缀回落。"""

from __future__ import annotations

from alphabee.midterm.board_limits import BOARD_LIMITS, ST_LIMIT, board_limit_pct, limit_ratio


def test_board_limits_table_five_boards():
    assert BOARD_LIMITS == {
        "sh_main": 10.0,
        "sz_main": 10.0,
        "chinext": 20.0,
        "star": 20.0,
        "bse": 30.0,
    }
    assert ST_LIMIT == 5.0


def test_sh_main_prefixes():
    for code in ("600519.SH", "601318", "603288", "605001"):
        assert board_limit_pct(code) == 10.0


def test_sz_main_prefixes():
    for code in ("000001.SZ", "001979", "002415", "003816"):
        assert board_limit_pct(code) == 10.0


def test_chinext_prefixes():
    for code in ("300750.SZ", "301039"):
        assert board_limit_pct(code) == 20.0


def test_star_prefixes():
    for code in ("688981.SH", "689009"):
        assert board_limit_pct(code) == 20.0


def test_bse_prefixes():
    for code in ("830799", "430047", "920002"):
        assert board_limit_pct(code) == 30.0


def test_st_override_is_5_percent_regardless_of_board():
    assert board_limit_pct("300750.SZ", is_st=True) == 5.0
    assert board_limit_pct("688981.SH", is_st=True) == 5.0
    assert board_limit_pct("600519.SH", is_st=True) == 5.0


def test_unknown_prefix_falls_back_to_main_board():
    assert board_limit_pct("999999") == 10.0
    assert board_limit_pct("") == 10.0
    assert board_limit_pct("ABC") == 10.0


def test_limit_ratio_is_abs_change_over_board_limit():
    assert limit_ratio(5.0, "600519.SH") == 0.5
    assert limit_ratio(10.0, "300750.SZ") == 0.5
    assert limit_ratio(15.0, "830799") == 0.5
    assert limit_ratio(4.95, "600519.SH", is_st=True) == 0.99
    assert limit_ratio(-5.0, "600519.SH") == 0.5
