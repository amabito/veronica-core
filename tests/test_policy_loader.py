"""Tests for veronica_core.policy.loader."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

from veronica_core.policy.loader import LoadedPolicy, PolicyLoader, WatchHandle
from veronica_core.policy.schema import PolicyValidationError
from veronica_core.shield.pipeline import ShieldPipeline


_MINIMAL_JSON = json.dumps(
    {
        "version": "1.0",
        "name": "Test Policy",
        "rules": [
            {
                "type": "token_budget",
                "params": {"max_output_tokens": 1000},
                "on_exceed": "halt",
            },
        ],
    }
)

_MINIMAL_YAML = """\
version: "1.0"
name: Test Policy
rules:
  - type: token_budget
    params:
      max_output_tokens: 1000
    on_exceed: halt
"""

_EMPTY_RULES_JSON = json.dumps(
    {
        "version": "1.0",
        "name": "Empty",
        "rules": [],
    }
)


class TestPolicyLoaderJSON:
    def test_load_from_json_string_returns_loaded_policy(self) -> None:
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_JSON, format="json")
        assert isinstance(result, LoadedPolicy)

    def test_loaded_policy_has_pipeline(self) -> None:
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_JSON, format="json")
        assert isinstance(result.pipeline, ShieldPipeline)

    def test_loaded_policy_has_schema(self) -> None:
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_JSON, format="json")
        assert result.schema.name == "Test Policy"
        assert result.schema.version == "1.0"

    def test_loaded_policy_has_hooks_list(self) -> None:
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_JSON, format="json")
        # One rule → one hook entry.
        assert len(result.hooks) == 1
        rule, component = result.hooks[0]
        assert rule.type == "token_budget"

    def test_single_factory_call_per_rule(self) -> None:
        """Factory must be called ONCE per rule; the same component instance
        must appear in both the pipeline slot and hooks list."""
        from veronica_core.policy.registry import PolicyRegistry
        from veronica_core.shield.token_budget import TokenBudgetHook

        call_count = 0
        instances: list[TokenBudgetHook] = []

        def tracking_factory(params):  # noqa: ANN001
            nonlocal call_count
            call_count += 1
            hook = TokenBudgetHook(
                max_output_tokens=int(params.get("max_output_tokens", 1000))
            )
            instances.append(hook)
            return hook

        registry = PolicyRegistry()
        registry.register_rule_type("token_budget", tracking_factory)
        loader = PolicyLoader(registry=registry)
        result = loader.load_from_string(_MINIMAL_JSON, format="json")

        assert call_count == 1, f"Factory called {call_count} times; expected 1"
        assert len(instances) == 1
        # The hook in result.hooks must be the same object as in pre_dispatch.
        _, hook_in_list = result.hooks[0]
        assert hook_in_list is instances[0]

    def test_pre_dispatch_is_same_instance_as_hook_in_list(self) -> None:
        """ShieldPipeline._pre_dispatch must be the identical object stored in hooks."""
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_JSON, format="json")
        _, hook = result.hooks[0]
        # Access the private slot to verify identity.
        assert result.pipeline._pre_dispatch is hook  # type: ignore[attr-defined]

    def test_load_from_json_file(self) -> None:
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)
        try:
            result = loader.load(tmp_path)
            assert isinstance(result, LoadedPolicy)
            assert isinstance(result.pipeline, ShieldPipeline)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_load_empty_rules_list(self) -> None:
        loader = PolicyLoader()
        result = loader.load_from_string(_EMPTY_RULES_JSON, format="json")
        assert isinstance(result, LoadedPolicy)
        assert result.hooks == []

    def test_load_invalid_json_raises(self) -> None:
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError) as exc_info:
            loader.load_from_string("{invalid json{{", format="json")
        assert "json" in exc_info.value.errors[0].lower()

    def test_load_json_not_object_raises(self) -> None:
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string("[1, 2, 3]", format="json")

    def test_load_missing_version_raises(self) -> None:
        data = json.dumps({"name": "No Version", "rules": []})
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_load_missing_name_raises(self) -> None:
        data = json.dumps({"version": "1.0", "rules": []})
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_load_unknown_format_raises(self) -> None:
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError) as exc_info:
            loader.load_from_string("{}", format="toml")  # type: ignore[arg-type]
        assert (
            "format" in exc_info.value.errors[0].lower()
            or "unsupported" in exc_info.value.errors[0].lower()
        )

    def test_validate_valid_file_returns_empty_list(self) -> None:
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)
        try:
            errors = loader.validate(tmp_path)
            assert errors == []
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_invalid_file_returns_errors(self) -> None:
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write("{invalid}")
            tmp_path = Path(f.name)
        try:
            errors = loader.validate(tmp_path)
            assert len(errors) > 0
            assert all(isinstance(e, PolicyValidationError) for e in errors)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_validate_unknown_rule_type_returns_error(self) -> None:
        data = json.dumps(
            {
                "version": "1.0",
                "name": "Bad",
                "rules": [{"type": "does_not_exist", "params": {}}],
            }
        )
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(data)
            tmp_path = Path(f.name)
        try:
            errors = loader.validate(tmp_path)
            assert len(errors) > 0
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_loaded_policy_proxies_pipeline_attributes(self) -> None:
        """LoadedPolicy.__getattr__ must delegate unknown attrs to pipeline."""
        loader = PolicyLoader()
        result = loader.load_from_string(_EMPTY_RULES_JSON, format="json")
        # get_events() is a ShieldPipeline method.
        assert result.get_events() == []  # type: ignore[attr-defined]


class TestPolicyLoaderYAML:
    def test_load_from_yaml_string(self) -> None:
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        result = loader.load_from_string(_MINIMAL_YAML, format="yaml")
        assert isinstance(result, LoadedPolicy)

    def test_load_from_yaml_string_same_schema_as_json(self) -> None:
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        json_result = loader.load_from_string(_MINIMAL_JSON, format="json")
        yaml_result = loader.load_from_string(_MINIMAL_YAML, format="yaml")
        assert json_result.schema.name == yaml_result.schema.name

    def test_load_from_yaml_file(self) -> None:
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".yaml", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_YAML)
            tmp_path = Path(f.name)
        try:
            result = loader.load(tmp_path)
            assert isinstance(result, LoadedPolicy)
        finally:
            tmp_path.unlink(missing_ok=True)


class TestWatchHandle:
    def test_watch_returns_watch_handle(self) -> None:
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)
        try:
            handle = loader.watch(tmp_path, lambda p: None, poll_interval=60.0)
            assert isinstance(handle, WatchHandle)
            handle.cancel()
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_watch_handle_cancel_is_idempotent(self) -> None:
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)
        try:
            handle = loader.watch(tmp_path, lambda p: None, poll_interval=60.0)
            handle.cancel()
            handle.cancel()  # Second cancel must not raise.
            assert handle.cancelled
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_watch_triggers_callback_on_change(self) -> None:
        sys.path.insert(0, str(Path(__file__).parent))
        from conftest import wait_for  # type: ignore[import]

        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)

        received: list[LoadedPolicy] = []

        def callback(loaded: LoadedPolicy) -> None:
            received.append(loaded)

        handle = loader.watch(tmp_path, callback, poll_interval=0.1)
        try:
            time.sleep(0.15)
            updated = json.dumps(
                {
                    "version": "1.0",
                    "name": "Updated Policy",
                    "rules": [],
                }
            )
            tmp_path.write_text(updated, encoding="utf-8")
            # Poll until the watcher fires the callback -- nogil-tolerant.
            wait_for(
                lambda: len(received) >= 1,
                timeout=5.0,
                interval=0.05,
                msg="File watcher callback did not fire within 5s",
            )
            assert received[0].schema.name == "Updated Policy"
        finally:
            handle.cancel()
            tmp_path.unlink(missing_ok=True)

    def test_watch_stops_after_cancel(self) -> None:
        """No callback must fire after cancel()."""
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(_MINIMAL_JSON)
            tmp_path = Path(f.name)

        call_count = 0

        def callback(loaded: LoadedPolicy) -> None:
            nonlocal call_count
            call_count += 1

        handle = loader.watch(tmp_path, callback, poll_interval=0.1)
        handle.cancel()  # Cancel immediately before first poll fires.
        # Modify file after cancel -- callback must not be triggered.
        tmp_path.write_text(
            json.dumps({"version": "1.0", "name": "Post-cancel", "rules": []}),
            encoding="utf-8",
        )
        time.sleep(0.35)
        assert call_count == 0, f"Callback fired {call_count} times after cancel"
        tmp_path.unlink(missing_ok=True)


class TestPolicyLoaderPathTraversal:
    """Tests for PolicyLoader.policy_root path traversal prevention."""

    def test_load_within_policy_root_succeeds(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "test.json"
        policy_file.write_text(_MINIMAL_JSON, encoding="utf-8")
        loader = PolicyLoader(policy_root=tmp_path)
        result = loader.load(policy_file)
        assert isinstance(result, LoadedPolicy)

    def test_load_outside_policy_root_raises(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "test.json"
        policy_file.write_text(_MINIMAL_JSON, encoding="utf-8")
        other_root = tmp_path / "subdir"
        other_root.mkdir()
        loader = PolicyLoader(policy_root=other_root)
        with pytest.raises(PolicyValidationError, match="Path traversal denied"):
            loader.load(policy_file)

    def test_traversal_with_dotdot_raises(self, tmp_path: Path) -> None:
        subdir = tmp_path / "policies"
        subdir.mkdir()
        policy_file = tmp_path / "secret.json"
        policy_file.write_text(_MINIMAL_JSON, encoding="utf-8")
        loader = PolicyLoader(policy_root=subdir)
        with pytest.raises(PolicyValidationError, match="Path traversal denied"):
            loader.load(subdir / ".." / "secret.json")

    def test_validate_outside_policy_root_returns_error(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "test.json"
        policy_file.write_text(_MINIMAL_JSON, encoding="utf-8")
        other_root = tmp_path / "subdir"
        other_root.mkdir()
        loader = PolicyLoader(policy_root=other_root)
        errors = loader.validate(policy_file)
        assert len(errors) >= 1
        assert "traversal" in str(errors[0]).lower()

    def test_no_policy_root_allows_any_path(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "test.json"
        policy_file.write_text(_MINIMAL_JSON, encoding="utf-8")
        loader = PolicyLoader()  # no policy_root
        result = loader.load(policy_file)
        assert isinstance(result, LoadedPolicy)

    def test_error_message_does_not_leak_paths(self, tmp_path: Path) -> None:
        """Error message must not contain absolute paths (info disclosure)."""
        subdir = tmp_path / "policies"
        subdir.mkdir()
        outside = tmp_path / "secret.json"
        outside.write_text(_MINIMAL_JSON, encoding="utf-8")
        loader = PolicyLoader(policy_root=subdir)
        with pytest.raises(PolicyValidationError) as exc_info:
            loader.load(outside)
        error_str = str(exc_info.value)
        assert str(subdir) not in error_str
        assert str(outside) not in error_str
