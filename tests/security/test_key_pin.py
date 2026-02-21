"""Tests for veronica_core.security.key_pin (J-2)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from veronica_core.security.key_pin import (
    KeyPinChecker,
    compute_key_hash,
    load_expected_pin,
)
from veronica_core.security.security_level import (
    SecurityLevel,
    reset_security_level,
    set_security_level,
)

_TEST_PEM = (
    b"-----BEGIN PUBLIC KEY-----\n"
    b"MCowBQYDK2VwAyEAKrhpqDBJoJp0CEthrZRdA0+dzRiTsFyYGrl1uOdbINk=\n"
    b"-----END PUBLIC KEY-----\n"
)
_TEST_HASH = compute_key_hash(_TEST_PEM)


@pytest.fixture(autouse=True)
def reset_level():
    """Reset security level singleton after each test."""
    reset_security_level()
    yield
    reset_security_level()


# ---------------------------------------------------------------------------
# compute_key_hash
# ---------------------------------------------------------------------------

class TestComputeKeyHash:
    def test_returns_64_char_hex(self):
        result = compute_key_hash(_TEST_PEM)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_is_deterministic(self):
        assert compute_key_hash(_TEST_PEM) == compute_key_hash(_TEST_PEM)

    def test_different_keys_produce_different_hashes(self):
        pem_a = b"data_aaa"
        pem_b = b"data_bbb"
        assert compute_key_hash(pem_a) != compute_key_hash(pem_b)

    def test_strips_trailing_whitespace(self):
        pem_padded = _TEST_PEM + b"   \n  "
        assert compute_key_hash(_TEST_PEM) == compute_key_hash(pem_padded)


# ---------------------------------------------------------------------------
# load_expected_pin
# ---------------------------------------------------------------------------

class TestLoadExpectedPin:
    def test_reads_from_env_var(self, monkeypatch):
        monkeypatch.setenv("VERONICA_KEY_PIN", "dead" * 16)
        assert load_expected_pin() == "dead" * 16

    def test_reads_from_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VERONICA_KEY_PIN", raising=False)
        pin_file = tmp_path / "key_pin.txt"
        pin_file.write_text(_TEST_HASH + "\n", encoding="utf-8")
        import veronica_core.security.key_pin as kp
        orig = kp._DEFAULT_PIN_FILE
        kp._DEFAULT_PIN_FILE = pin_file
        try:
            assert load_expected_pin() == _TEST_HASH
        finally:
            kp._DEFAULT_PIN_FILE = orig

    def test_returns_none_when_no_source(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VERONICA_KEY_PIN", raising=False)
        import veronica_core.security.key_pin as kp
        orig = kp._DEFAULT_PIN_FILE
        kp._DEFAULT_PIN_FILE = tmp_path / "nonexistent.txt"
        try:
            assert load_expected_pin() is None
        finally:
            kp._DEFAULT_PIN_FILE = orig


# ---------------------------------------------------------------------------
# KeyPinChecker.check
# ---------------------------------------------------------------------------

class TestKeyPinCheckerCheck:
    def _pin_checker(self, pin, monkeypatch, tmp_path):
        """Helper: build a checker with the given pin (None = no pin)."""
        monkeypatch.delenv("VERONICA_KEY_PIN", raising=False)
        import veronica_core.security.key_pin as kp
        if pin is None:
            kp._DEFAULT_PIN_FILE = tmp_path / "nofile.txt"
        else:
            f = tmp_path / "key_pin.txt"
            f.write_text(pin, encoding="utf-8")
            kp._DEFAULT_PIN_FILE = f
        return KeyPinChecker()

    def test_no_pin_returns_true(self, monkeypatch, tmp_path):
        checker = self._pin_checker(None, monkeypatch, tmp_path)
        assert checker.check(_TEST_PEM) is True

    def test_correct_pin_returns_true(self, monkeypatch, tmp_path):
        checker = self._pin_checker(_TEST_HASH, monkeypatch, tmp_path)
        assert checker.check(_TEST_PEM) is True

    def test_wrong_pin_returns_false(self, monkeypatch, tmp_path):
        checker = self._pin_checker("0" * 64, monkeypatch, tmp_path)
        assert checker.check(_TEST_PEM) is False

    def test_wrong_pin_emits_audit_event(self, monkeypatch, tmp_path):
        audit = MagicMock()
        checker = self._pin_checker("0" * 64, monkeypatch, tmp_path)
        checker._audit_log = audit
        checker.check(_TEST_PEM)
        audit.write.assert_called_once()
        event, payload = audit.write.call_args[0]
        assert event == "key_pin_mismatch"
        assert payload["expected"] == "0" * 64
        assert payload["actual"] == _TEST_HASH


# ---------------------------------------------------------------------------
# KeyPinChecker.enforce
# ---------------------------------------------------------------------------

class TestKeyPinCheckerEnforce:
    def _wrong_pin_checker(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VERONICA_KEY_PIN", raising=False)
        import veronica_core.security.key_pin as kp
        f = tmp_path / "key_pin.txt"
        f.write_text("0" * 64, encoding="utf-8")
        kp._DEFAULT_PIN_FILE = f
        return KeyPinChecker()

    def test_dev_wrong_pin_no_raise(self, monkeypatch, tmp_path):
        set_security_level(SecurityLevel.DEV)
        # Must not raise
        self._wrong_pin_checker(monkeypatch, tmp_path).enforce(_TEST_PEM)

    def test_ci_wrong_pin_raises(self, monkeypatch, tmp_path):
        set_security_level(SecurityLevel.CI)
        with pytest.raises(RuntimeError, match="Key pin mismatch"):
            self._wrong_pin_checker(monkeypatch, tmp_path).enforce(_TEST_PEM)

    def test_prod_wrong_pin_raises(self, monkeypatch, tmp_path):
        set_security_level(SecurityLevel.PROD)
        with pytest.raises(RuntimeError, match="Key pin mismatch"):
            self._wrong_pin_checker(monkeypatch, tmp_path).enforce(_TEST_PEM)

    def test_correct_pin_never_raises(self, monkeypatch, tmp_path):
        monkeypatch.delenv("VERONICA_KEY_PIN", raising=False)
        import veronica_core.security.key_pin as kp
        f = tmp_path / "key_pin.txt"
        f.write_text(_TEST_HASH, encoding="utf-8")
        kp._DEFAULT_PIN_FILE = f
        for level in SecurityLevel:
            set_security_level(level)
            KeyPinChecker().enforce(_TEST_PEM)  # Must not raise
