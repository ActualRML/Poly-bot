import os
import pytest
from decimal import Decimal

from src.utils.config import _get_bool, _get_float, _get_int, _get_decimal

@pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "Yes", "YES"])
def test_get_bool_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("_TEST_BOOL", val)
    assert _get_bool("_TEST_BOOL", False) is True

@pytest.mark.parametrize("val", ["false", "False", "FALSE", "0", "no", "No"])
def test_get_bool_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("_TEST_BOOL", val)
    assert _get_bool("_TEST_BOOL", True) is False

def test_get_bool_default_true_when_missing():
    os.environ.pop("_NONEXISTENT_BOOL", None)
    assert _get_bool("_NONEXISTENT_BOOL", True) is True

def test_get_bool_default_false_when_missing():
    os.environ.pop("_NONEXISTENT_BOOL", None)
    assert _get_bool("_NONEXISTENT_BOOL", False) is False

def test_get_bool_invalid_string_returns_false(monkeypatch):
    monkeypatch.setenv("_TEST_BOOL", "maybe")
    assert _get_bool("_TEST_BOOL", True) is False

def test_get_bool_empty_string_returns_false(monkeypatch):
    monkeypatch.setenv("_TEST_BOOL", "")
    assert _get_bool("_TEST_BOOL", True) is False

def test_get_float_valid_decimal(monkeypatch):
    monkeypatch.setenv("_TEST_FLOAT", "3.14")
    assert _get_float("_TEST_FLOAT", 0.0) == pytest.approx(3.14)

def test_get_float_integer_string(monkeypatch):
    monkeypatch.setenv("_TEST_FLOAT", "42")
    assert _get_float("_TEST_FLOAT", 0.0) == pytest.approx(42.0)

def test_get_float_negative(monkeypatch):
    monkeypatch.setenv("_TEST_FLOAT", "-0.15")
    assert _get_float("_TEST_FLOAT", 0.0) == pytest.approx(-0.15)

def test_get_float_default_when_missing():
    os.environ.pop("_NONEXISTENT_FLOAT", None)
    assert _get_float("_NONEXISTENT_FLOAT", 9.99) == pytest.approx(9.99)

def test_get_float_zero(monkeypatch):
    monkeypatch.setenv("_TEST_FLOAT", "0.0")
    assert _get_float("_TEST_FLOAT", 1.0) == pytest.approx(0.0)

def test_get_int_valid(monkeypatch):
    monkeypatch.setenv("_TEST_INT", "7")
    assert _get_int("_TEST_INT", 0) == 7

def test_get_int_default_when_missing():
    os.environ.pop("_NONEXISTENT_INT", None)
    assert _get_int("_NONEXISTENT_INT", 42) == 42

def test_get_int_large_value(monkeypatch):
    monkeypatch.setenv("_TEST_INT", "10000")
    assert _get_int("_TEST_INT", 0) == 10000

def test_get_int_zero(monkeypatch):
    monkeypatch.setenv("_TEST_INT", "0")
    assert _get_int("_TEST_INT", 5) == 0

def test_get_decimal_valid(monkeypatch):
    monkeypatch.setenv("_TEST_DEC", "1000.50")
    assert _get_decimal("_TEST_DEC", "0") == Decimal("1000.50")

def test_get_decimal_default_when_missing():
    os.environ.pop("_NONEXISTENT_DEC", None)
    assert _get_decimal("_NONEXISTENT_DEC", "500") == Decimal("500")

def test_get_decimal_integer_string(monkeypatch):
    monkeypatch.setenv("_TEST_DEC", "100")
    assert _get_decimal("_TEST_DEC", "0") == Decimal("100")

def test_get_decimal_preserves_precision(monkeypatch):
    monkeypatch.setenv("_TEST_DEC", "0.123456789")
    result = _get_decimal("_TEST_DEC", "0")
    assert result == Decimal("0.123456789")

