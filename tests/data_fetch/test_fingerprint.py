"""Unit tests for data_fetch.fingerprint."""

from alphabee.data_fetch.fingerprint import compute_fingerprint


class TestComputeFingerprint:
    def test_same_inputs_same_fingerprint(self):
        fp1 = compute_fingerprint("tushare", "income", "timeout")
        fp2 = compute_fingerprint("tushare", "income", "timeout")
        assert fp1 == fp2

    def test_different_provider_same_api(self):
        fp1 = compute_fingerprint("tushare", "income", "timeout")
        fp2 = compute_fingerprint("akshare", "income", "timeout")
        assert fp1 != fp2

    def test_different_error_type(self):
        fp1 = compute_fingerprint("tushare", "income", "timeout")
        fp2 = compute_fingerprint("tushare", "income", "permission")
        assert fp1 != fp2

    def test_missing_fields_affect_fingerprint(self):
        fp1 = compute_fingerprint("tushare", "income", "missing_field", missing_fields=["revenue", "profit"])
        fp2 = compute_fingerprint("tushare", "income", "missing_field", missing_fields=["profit", "revenue"])
        assert fp1 == fp2  # sorted → same

    def test_different_missing_fields_different_fingerprint(self):
        fp1 = compute_fingerprint("tushare", "income", "missing_field", missing_fields=["revenue"])
        fp2 = compute_fingerprint("tushare", "income", "missing_field", missing_fields=["profit"])
        assert fp1 != fp2

    def test_error_prefix_affects_fingerprint(self):
        fp1 = compute_fingerprint("tushare", "income", "timeout", error_prefix="Connection timed out")
        fp2 = compute_fingerprint("tushare", "income", "timeout", error_prefix="Read timed out")
        assert fp1 != fp2

    def test_error_prefix_truncated(self):
        long_prefix = "x" * 200
        fp1 = compute_fingerprint("tushare", "income", "timeout", error_prefix=long_prefix)
        fp2 = compute_fingerprint("tushare", "income", "timeout", error_prefix=long_prefix[:80])
        assert fp1 == fp2

    def test_fingerprint_length(self):
        fp = compute_fingerprint("tushare", "income", "timeout")
        assert len(fp) == 16

    def test_no_symbol_in_fingerprint(self):
        fp1 = compute_fingerprint("tushare", "income", "timeout")
        fp2 = compute_fingerprint("tushare", "income", "timeout")
        assert fp1 == fp2
