"""Unit tests for _utils.py — shared helper functions used by all fact tools."""

import math
import pytest
from alphabee.agents.facts.tools._utils import normalize_ts_code, to_pure_code, safe_float, safe_str


class TestNormalizeTsCode:
    def test_sh_prefix(self):
        assert normalize_ts_code("sh600519") == "600519.SH"

    def test_sz_prefix(self):
        assert normalize_ts_code("sz000001") == "000001.SZ"

    def test_bj_prefix(self):
        assert normalize_ts_code("bj430047") == "430047.BJ"

    def test_already_normalized_sh(self):
        assert normalize_ts_code("600519.SH") == "600519.SH"

    def test_already_normalized_sz(self):
        assert normalize_ts_code("000001.SZ") == "000001.SZ"

    def test_already_normalized_bj(self):
        assert normalize_ts_code("430047.BJ") == "430047.BJ"

    def test_sh_stock_by_prefix(self):
        """Stocks starting with 6 or 9 get .SH suffix."""
        assert normalize_ts_code("600519") == "600519.SH"
        assert normalize_ts_code("900901") == "900901.SH"
        assert normalize_ts_code("688981") == "688981.SH"

    def test_sz_stock_by_prefix(self):
        """Stocks starting with 0 or 3 get .SZ suffix."""
        assert normalize_ts_code("000001") == "000001.SZ"
        assert normalize_ts_code("300760") == "300760.SZ"

    def test_bj_stock_by_prefix(self):
        """Stocks starting with 4 or 8 get .BJ suffix."""
        assert normalize_ts_code("430047") == "430047.BJ"
        assert normalize_ts_code("833819") == "833819.BJ"

    def test_lowercase_input(self):
        assert normalize_ts_code("sh600519") == "600519.SH"

    def test_strips_whitespace(self):
        assert normalize_ts_code("  600519.SH  ") == "600519.SH"

    def test_raises_on_unknown_code(self):
        with pytest.raises(ValueError, match="Cannot determine exchange"):
            normalize_ts_code("abc123")


class TestToPureCode:
    def test_sh_code(self):
        assert to_pure_code("600519.SH") == "600519"

    def test_sz_code(self):
        assert to_pure_code("000001.SZ") == "000001"

    def test_bj_code(self):
        assert to_pure_code("430047.BJ") == "430047"


class TestSafeFloat:
    def test_normal_float(self):
        assert safe_float(123.45) == 123.45

    def test_int(self):
        assert safe_float(42) == 42.0

    def test_numeric_string(self):
        assert safe_float("3.14") == 3.14

    def test_none_returns_default(self):
        assert safe_float(None) == 0.0

    def test_none_returns_custom_default(self):
        assert safe_float(None, default=-999.0) == -999.0

    def test_nan_returns_default(self):
        assert safe_float(math.nan) == 0.0

    def test_nan_returns_custom_default(self):
        assert safe_float(math.nan, default=-999.0) == -999.0

    def test_invalid_string_returns_default(self):
        assert safe_float("not_a_number") == 0.0

    def test_invalid_string_returns_custom_default(self):
        assert safe_float("n/a", default=0.5) == 0.5

    def test_empty_string_returns_default(self):
        assert safe_float("") == 0.0

    def test_zero(self):
        assert safe_float(0.0) == 0.0
        assert safe_float(0) == 0.0

    def test_negative_number(self):
        assert safe_float(-100.5) == -100.5

    def test_boolean(self):
        """safe_float does NOT handle booleans specially — bool is subclass of int."""
        assert safe_float(True) == 1.0
        assert safe_float(False) == 0.0


class TestSafeStr:
    def test_normal_string(self):
        assert safe_str("hello") == "hello"

    def test_none_returns_default(self):
        assert safe_str(None) == ""

    def test_none_returns_custom_default(self):
        assert safe_str(None, default="N/A") == "N/A"

    def test_nan_string_returns_default(self):
        assert safe_str("nan") == ""

    def test_literal_none_string_returns_default(self):
        assert safe_str("None") == ""

    def test_empty_string_returns_default(self):
        assert safe_str("") == ""

    def test_whitespace_string(self):
        """Whitespace-only should become empty after strip."""
        assert safe_str("   ") == ""

    def test_whitespace_with_nan(self):
        assert safe_str("  nan  ") == ""

    def test_string_with_spaces(self):
        assert safe_str("  hello  ") == "hello"

    def test_number_converted_to_string(self):
        assert safe_str(42) == "42"
