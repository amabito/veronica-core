"""Tests for ShieldConfig (PR-1A)."""

import json
import sys

import pytest

from veronica_core.shield import ShieldConfig
from veronica_core import VeronicaIntegration
from veronica_core.backends import MemoryBackend


class TestShieldConfig:
    """Shield configuration unit tests."""

    def test_shield_config_defaults_noop(self):
        """Default ShieldConfig has all features disabled -- zero impact."""
        config = ShieldConfig()

        assert not config.safe_mode.enabled
        assert not config.budget.enabled
        assert not config.circuit_breaker.enabled
        assert not config.egress.enabled
        assert not config.secret_guard.enabled
        assert not config.is_any_enabled

    def test_shield_config_from_env_safe_mode(self, monkeypatch):
        """VERONICA_SAFE_MODE=1 activates safe_mode.enabled."""
        monkeypatch.setenv("VERONICA_SAFE_MODE", "1")

        config = ShieldConfig.from_env()

        assert config.safe_mode.enabled
        assert config.is_any_enabled
        # Other features remain disabled
        assert not config.budget.enabled
        assert not config.circuit_breaker.enabled

    def test_shield_config_from_yaml_roundtrip(self, tmp_path):
        """Config survives JSON roundtrip via from_yaml (no PyYAML needed)."""
        original = ShieldConfig()
        original.safe_mode.enabled = True
        original.budget.enabled = True
        original.budget.max_tokens = 50_000

        # Write as JSON
        path = tmp_path / "shield.json"
        with open(path, "w") as fh:
            json.dump(original.to_dict(), fh)

        # Load back
        loaded = ShieldConfig.from_yaml(str(path))

        assert loaded.safe_mode.enabled
        assert loaded.budget.enabled
        assert loaded.budget.max_tokens == 50_000
        assert not loaded.circuit_breaker.enabled
        assert loaded.to_dict() == original.to_dict()

    def test_from_yaml_raises_without_pyyaml(self, tmp_path, monkeypatch):
        """from_yaml raises RuntimeError for .yaml when PyYAML is unavailable."""
        path = tmp_path / "shield.yaml"
        path.write_text("safe_mode:\n  enabled: true\n")

        # Hide PyYAML from import machinery
        monkeypatch.setitem(sys.modules, "yaml", None)

        with pytest.raises(RuntimeError, match="PyYAML is required"):
            ShieldConfig.from_yaml(str(path))

    def test_integration_accepts_shield(self):
        """VeronicaIntegration preserves shield config without behavior change."""
        backend = MemoryBackend()
        shield = ShieldConfig()
        shield.safe_mode.enabled = True

        veronica = VeronicaIntegration(backend=backend, shield=shield)

        assert veronica.shield.safe_mode.enabled

    def test_from_yaml_config_root_allows_valid_path(self, tmp_path) -> None:
        config_file = tmp_path / "shield.json"
        config_file.write_text('{"safe_mode": {"enabled": true}}')
        result = ShieldConfig.from_yaml(str(config_file), config_root=tmp_path)
        assert result.safe_mode.enabled is True

    def test_from_yaml_config_root_blocks_traversal(self, tmp_path) -> None:
        config_file = tmp_path / "shield.json"
        config_file.write_text('{"safe_mode": {"enabled": true}}')
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        with pytest.raises(ValueError, match="Path traversal denied"):
            ShieldConfig.from_yaml(str(config_file), config_root=subdir)

    def test_from_yaml_traversal_error_no_path_leak(self, tmp_path) -> None:
        """Error must not contain absolute filesystem paths."""
        config_file = tmp_path / "shield.json"
        config_file.write_text("{}")
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        with pytest.raises(ValueError) as exc_info:
            ShieldConfig.from_yaml(str(config_file), config_root=subdir)
        error_str = str(exc_info.value)
        assert str(subdir) not in error_str
        assert str(config_file) not in error_str

    def test_from_yaml_case_insensitive_suffix(self, tmp_path) -> None:
        config_file = tmp_path / "shield.JSON"
        config_file.write_text('{"safe_mode": {"enabled": true}}')
        result = ShieldConfig.from_yaml(str(config_file))
        assert result.safe_mode.enabled is True

    def test_integration_without_shield_unchanged(self):
        """VeronicaIntegration works identically without shield (backward compat)."""
        backend = MemoryBackend()
        veronica = VeronicaIntegration(backend=backend)

        assert veronica.shield is None
        # Existing behavior unaffected
        veronica.record_fail("test_entity")
        assert veronica.get_fail_count("test_entity") == 1
