"""Unit tests for data_fetch.models."""

import pytest

from alphabee.data_fetch.models import (
    ErrorSeverity,
    ErrorType,
    IssueStatus,
)


class TestEnums:
    def test_error_type_values(self):
        assert ErrorType.PERMISSION.value == "permission"
        assert ErrorType.TIMEOUT.value == "timeout"
        assert ErrorType.NETWORK.value == "network"
        assert ErrorType.UNKNOWN.value == "unknown"

    def test_error_severity_values(self):
        assert ErrorSeverity.LOW.value == "low"
        assert ErrorSeverity.MEDIUM.value == "medium"
        assert ErrorSeverity.HIGH.value == "high"

    def test_issue_status_values(self):
        assert IssueStatus.NEW.value == "new"
        assert IssueStatus.FIXED.value == "fixed"
        assert IssueStatus.IGNORED.value == "ignored"

    def test_error_type_from_string(self):
        assert ErrorType("timeout") == ErrorType.TIMEOUT
        assert ErrorType("missing_field") == ErrorType.MISSING_FIELD

    def test_error_type_invalid_raises(self):
        with pytest.raises(ValueError):
            ErrorType("not_a_real_type")
