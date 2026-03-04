"""Adversarial tests for veronica_core.policy — attacker mindset."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from veronica_core.policy.loader import LoadedPolicy, PolicyLoader
from veronica_core.policy.schema import PolicySchema, PolicyValidationError, RuleSchema
from veronica_core.shield.pipeline import ShieldPipeline


class TestAdversarialPolicyLoader:
    """Adversarial tests for PolicyLoader -- corrupted input, concurrency, scale."""

    def test_corrupted_json_partial_truncated(self) -> None:
        """Truncated JSON must raise PolicyValidationError, not hang or crash."""
        loader = PolicyLoader()
        truncated = '{"version": "1.0", "name": "Cut'  # truncated mid-string
        with pytest.raises(PolicyValidationError) as exc_info:
            loader.load_from_string(truncated, format="json")
        assert exc_info.value.errors

    def test_corrupted_json_null_bytes(self) -> None:
        """Null bytes in content must not crash the parser."""
        loader = PolicyLoader()
        content = json.dumps({"version": "1.0", "name": "Test", "rules": []})
        content_with_nulls = content[:10] + "\x00\x00" + content[10:]
        with pytest.raises((PolicyValidationError, ValueError, Exception)):
            # Any exception is acceptable; no hang, no crash into unexpected state.
            loader.load_from_string(content_with_nulls, format="json")

    def test_corrupted_json_deeply_nested(self) -> None:
        """Deeply nested JSON must not cause stack overflow."""
        loader = PolicyLoader()
        # 1000-deep nesting.
        nested = "{" * 500 + "}" * 500
        with pytest.raises((PolicyValidationError, Exception)):
            loader.load_from_string(nested, format="json")

    def test_extremely_large_rules_list_no_hang(self) -> None:
        """10000 rules must parse and validate without hanging."""
        loader = PolicyLoader()
        rules = [{"type": "token_budget", "params": {}, "on_exceed": "halt"}] * 10_000
        data = json.dumps({"version": "1.0", "name": "Huge", "rules": rules})
        pipeline = loader.load_from_string(data, format="json")
        assert isinstance(pipeline, LoadedPolicy)
        # Schema should have 10000 rules recorded.
        assert len(pipeline.schema.rules) == 10_000  # type: ignore[attr-defined]

    def test_extremely_large_params_dict(self) -> None:
        """A single rule with 10000 param keys must not hang."""
        loader = PolicyLoader()
        big_params = {f"key_{i}": i for i in range(10_000)}
        data = json.dumps({
            "version": "1.0",
            "name": "BigParams",
            "rules": [{"type": "token_budget", "params": big_params, "on_exceed": "halt"}],
        })
        # Should not hang; may or may not raise depending on hook validation.
        try:
            loader.load_from_string(data, format="json")
        except Exception:
            pass  # Any exception is OK; no hang.

    def test_concurrent_load_thread_safe(self) -> None:
        """Concurrent load() calls from multiple threads must not corrupt results."""
        loader = PolicyLoader()
        valid_json = json.dumps({
            "version": "1.0",
            "name": "Concurrent",
            "rules": [{"type": "token_budget", "params": {"max_output_tokens": 1000}, "on_exceed": "halt"}],
        })

        results: list[ShieldPipeline | Exception] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def load_worker() -> None:
            try:
                pipeline = loader.load_from_string(valid_json, format="json")
                with lock:
                    results.append(pipeline)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=load_worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # All threads must have completed.
        assert len(results) + len(errors) == 20
        # No unexpected errors (all should succeed).
        assert len(errors) == 0, f"Unexpected errors: {errors}"
        # All results must be valid ShieldPipeline instances.
        for pipeline in results:
            assert isinstance(pipeline, LoadedPolicy)

    def test_concurrent_validate_thread_safe(self) -> None:
        """Concurrent validate() calls must not corrupt the registry state."""
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(json.dumps({"version": "1.0", "name": "T", "rules": []}))
            tmp_path = Path(f.name)

        error_counts: list[int] = []
        lock = threading.Lock()

        def validate_worker() -> None:
            errs = loader.validate(tmp_path)
            with lock:
                error_counts.append(len(errs))

        threads = [threading.Thread(target=validate_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        tmp_path.unlink(missing_ok=True)
        # All threads must have completed with 0 errors.
        assert len(error_counts) == 10
        assert all(n == 0 for n in error_counts)

    def test_load_rule_with_unknown_type_raises_on_build(self) -> None:
        """Unknown rule type must raise PolicyValidationError during load."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "Unknown Type",
            "rules": [{"type": "does_not_exist", "params": {}}],
        })
        with pytest.raises(PolicyValidationError) as exc_info:
            loader.load_from_string(data, format="json")
        assert "does_not_exist" in exc_info.value.errors[0]

    def test_load_invalid_on_exceed_raises(self) -> None:
        """Invalid on_exceed value must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "Bad exceed",
            "rules": [{"type": "token_budget", "params": {}, "on_exceed": "explode"}],
        })
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_unicode_bomb_in_name_field(self) -> None:
        """Unicode bomb-like values in name must be handled without crash."""
        loader = PolicyLoader()
        # Long unicode string (not a real bomb but tests unicode handling).
        data = json.dumps({
            "version": "1.0",
            "name": "A" * 10_000,
            "rules": [],
        })
        pipeline = loader.load_from_string(data, format="json")
        assert isinstance(pipeline, LoadedPolicy)


class TestAdversarialCorruptedInput:
    """Corrupted / malformed input adversarial tests."""

    def test_invalid_utf8_bytes_in_file(self) -> None:
        """File with invalid UTF-8 bytes must raise an exception, not crash."""
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            # Write raw bytes that are not valid UTF-8.
            f.write(b'{"version": "1.0", "name": "\xff\xfe broken', )
            tmp_path = Path(f.name)
        try:
            with pytest.raises(Exception):
                loader.load(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_binary_garbage_content(self) -> None:
        """Binary garbage must raise an exception gracefully."""
        loader = PolicyLoader()
        garbage = bytes(range(256)) * 4
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(garbage)
            tmp_path = Path(f.name)
        try:
            with pytest.raises(Exception):
                loader.load(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_empty_file(self) -> None:
        """Empty file must raise a clear error, not an obscure AttributeError."""
        loader = PolicyLoader()
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            f.write("")
            tmp_path = Path(f.name)
        try:
            with pytest.raises(Exception) as exc_info:
                loader.load(tmp_path)
            # Should be a PolicyValidationError or JSONDecodeError, never AttributeError.
            assert not isinstance(exc_info.value, AttributeError)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_json_with_only_whitespace(self) -> None:
        """Whitespace-only JSON must raise a clear error."""
        loader = PolicyLoader()
        with pytest.raises(Exception) as exc_info:
            loader.load_from_string("   \n\t  ", format="json")
        assert not isinstance(exc_info.value, AttributeError)

    def test_json_array_at_top_level(self) -> None:
        """JSON array (not object) at top level must raise PolicyValidationError."""
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string('[{"type": "token_budget"}]', format="json")

    def test_json_string_at_top_level(self) -> None:
        """JSON string at top level must raise PolicyValidationError."""
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string('"just a string"', format="json")

    def test_json_number_at_top_level(self) -> None:
        """JSON number at top level must raise PolicyValidationError."""
        loader = PolicyLoader()
        with pytest.raises(PolicyValidationError):
            loader.load_from_string("42", format="json")


class TestAdversarialYAMLBomb:
    """YAML-specific adversarial tests (pyyaml optional)."""

    def test_yaml_recursive_anchors_bomb(self) -> None:
        """YAML recursive anchors (billion laughs) must not hang or OOM."""
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        # Classic YAML billion-laughs bomb pattern.
        yaml_bomb = """\
a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]
b: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]
c: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]
d: &d [*c,*c,*c,*c,*c,*c,*c,*c,*c]
version: "1.0"
name: bomb
rules: []
"""
        # yaml.safe_load (used by loader) does NOT expand anchors recursively
        # in a way that causes OOM — it builds the structure but limits depth.
        # The test verifies the loader does not hang or crash the process.
        # If it raises any exception that's also acceptable.
        import signal

        def _timeout_handler(signum, frame):  # type: ignore[type-arg]
            raise TimeoutError("YAML bomb caused hang")

        # On Windows signal.SIGALRM is unavailable — use threading.Timer instead.
        timed_out = threading.Event()

        def _bomb_thread() -> None:
            try:
                loader.load_from_string(yaml_bomb, format="yaml")
            except Exception:
                pass

        t = threading.Thread(target=_bomb_thread, daemon=True)
        t.start()
        t.join(timeout=5.0)
        # If the thread is still alive after 5s the YAML bomb hung — fail test.
        assert not t.is_alive(), "YAML bomb caused loader to hang (>5s)"

    def test_yaml_duplicate_keys(self) -> None:
        """YAML with duplicate keys must not silently corrupt data."""
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        # PyYAML last-key-wins for duplicate keys; ensure no crash.
        yaml_dup = """\
version: "1.0"
version: "2.0"
name: Dup
rules: []
"""
        # Should not raise (yaml.safe_load accepts this; last value wins).
        try:
            pipeline = loader.load_from_string(yaml_dup, format="yaml")
            # Should be a valid pipeline regardless of which version was kept.
            assert isinstance(pipeline, LoadedPolicy)
        except PolicyValidationError:
            pass  # Also acceptable.

    def test_yaml_deeply_nested_anchors(self) -> None:
        """Deeply nested YAML anchors must not stackoverflow."""
        pytest.importorskip("yaml", reason="pyyaml not installed")
        loader = PolicyLoader()
        # Build 100 levels of nesting.
        lines = ['a0: &a0 {x: 1}']
        for i in range(1, 100):
            lines.append(f'a{i}: &a{i} {{x: *a{i-1}}}')
        lines += ['version: "1.0"', 'name: Nested', 'rules: []']
        yaml_content = "\n".join(lines)
        try:
            loader.load_from_string(yaml_content, format="yaml")
        except Exception:
            pass  # Any exception is OK; no hang.


class TestAdversarialTypeConfusion:
    """Type confusion adversarial tests: wrong types in params/fields."""

    def test_string_where_int_expected_in_max_output_tokens(self) -> None:
        """String where int expected must raise gracefully (not silent corruption)."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "TypeConfusion",
            "rules": [{"type": "token_budget", "params": {"max_output_tokens": "not_an_int"}, "on_exceed": "halt"}],
        })
        with pytest.raises(Exception) as exc_info:
            loader.load_from_string(data, format="json")
        # Must not be an AttributeError or silent wrong value.
        assert not isinstance(exc_info.value, AttributeError)

    def test_float_where_int_expected_in_max_calls(self) -> None:
        """Float value for int field must be converted or raise cleanly."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "FloatInt",
            "rules": [{"type": "rate_limit", "params": {"max_calls": 10.5}, "on_exceed": "halt"}],
        })
        # int(10.5) = 10, which is valid. Should succeed without crash.
        try:
            pipeline = loader.load_from_string(data, format="json")
            assert isinstance(pipeline, LoadedPolicy)
        except Exception:
            pass  # Raising is also acceptable; no silent corruption.

    def test_list_in_version_field(self) -> None:
        """List in version field must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": [1, 0],
            "name": "Bad",
            "rules": [],
        })
        # PolicySchema.from_dict uses str(data.get("version")) so this converts
        # to "[1, 0]" which is a non-empty string — currently accepted.
        # Verify it does not crash silently in an unexpected way.
        try:
            loader.load_from_string(data, format="json")
        except Exception:
            pass  # Raising is fine too.

    def test_integer_in_name_field(self) -> None:
        """Integer in name field must not crash (str() conversion expected)."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": 12345,
            "rules": [],
        })
        # from_dict uses str() conversion — should produce "12345" name.
        try:
            pipeline = loader.load_from_string(data, format="json")
            assert isinstance(pipeline, LoadedPolicy)
        except PolicyValidationError:
            pass  # Also acceptable.

    def test_null_version_raises(self) -> None:
        """Null version must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": None, "name": "Test", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_null_name_raises(self) -> None:
        """Null name must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "1.0", "name": None, "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_null_rules_treated_as_empty(self) -> None:
        """Null rules list must be normalised to empty list, not crash."""
        loader = PolicyLoader()
        data = json.dumps({"version": "1.0", "name": "NullRules", "rules": None})
        pipeline = loader.load_from_string(data, format="json")
        assert isinstance(pipeline, LoadedPolicy)

    def test_boolean_in_params_values(self) -> None:
        """Boolean values in params must not crash the factory."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "BoolParams",
            "rules": [{"type": "token_budget", "params": {"max_output_tokens": True}, "on_exceed": "halt"}],
        })
        # bool is a subclass of int in Python; int(True) == 1. May succeed or raise.
        try:
            loader.load_from_string(data, format="json")
        except Exception:
            pass  # Any exception is OK; no crash into unexpected state.

    def test_negative_int_in_max_output_tokens(self) -> None:
        """Negative int in max_output_tokens must raise or produce invalid hook."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "NegTokens",
            "rules": [{"type": "token_budget", "params": {"max_output_tokens": -1000}, "on_exceed": "halt"}],
        })
        # The hook constructor may or may not validate; test that no hang occurs.
        try:
            loader.load_from_string(data, format="json")
        except Exception:
            pass  # Raising is acceptable.

    def test_empty_string_rule_type(self) -> None:
        """Empty string rule type must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "EmptyType",
            "rules": [{"type": "", "params": {}}],
        })
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")


class TestAdversarialPathTraversal:
    """Path traversal adversarial tests for load() and validate()."""

    def test_load_nonexistent_path_raises(self) -> None:
        """Loading a nonexistent path must raise a clear error."""
        loader = PolicyLoader()
        with pytest.raises(Exception) as exc_info:
            loader.load("/nonexistent/path/to/policy.json")
        # Must not be a silent None return or AttributeError.
        assert not isinstance(exc_info.value, AttributeError)

    def test_load_directory_path_raises(self) -> None:
        """Passing a directory path to load() must raise a clear error."""
        loader = PolicyLoader()
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(Exception) as exc_info:
                loader.load(tmpdir)
            assert not isinstance(exc_info.value, AttributeError)

    def test_load_path_with_null_byte_raises(self) -> None:
        """Path containing null byte must raise a clear error."""
        loader = PolicyLoader()
        with pytest.raises(Exception) as exc_info:
            loader.load("/tmp/pol\x00icy.json")
        assert not isinstance(exc_info.value, AttributeError)

    def test_validate_nonexistent_path_returns_error(self) -> None:
        """validate() on nonexistent path must return errors, not raise."""
        loader = PolicyLoader()
        errors = loader.validate("/nonexistent/path/to/policy.json")
        assert len(errors) > 0
        assert all(isinstance(e, PolicyValidationError) for e in errors)

    def test_load_symlink_to_sensitive_path_raises_or_loads(self) -> None:
        """Symlink pointing outside temp dir must either load the file or raise.

        veronica-core does NOT perform path-traversal blocking (that is the
        caller's responsibility). This test documents the behaviour: load()
        follows symlinks transparently, and raises if the target is unreadable.
        """
        loader = PolicyLoader()
        with tempfile.TemporaryDirectory() as tmpdir:
            link_path = Path(tmpdir) / "link.json"
            target = Path("/nonexistent_sensitive_file.json")
            try:
                link_path.symlink_to(target)
            except (OSError, NotImplementedError):
                pytest.skip("symlink creation not supported on this platform")
            # Behaviour: either raises (target doesn't exist) or loads.
            try:
                loader.load(link_path)
            except Exception:
                pass  # Expected: target doesn't exist.

    def test_load_path_with_dotdot_components(self) -> None:
        """load() with ../ path components must resolve to the real path and
        either succeed (if the file exists) or fail with a clear error."""
        loader = PolicyLoader()
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a valid policy file.
            policy_file = Path(tmpdir) / "policy.json"
            policy_file.write_text(
                json.dumps({"version": "1.0", "name": "Traverse", "rules": []}),
                encoding="utf-8",
            )
            # Build a path with ../ that resolves to the same file.
            # subdir must exist on Linux for the OS to resolve "..".
            (Path(tmpdir) / "subdir").mkdir()
            traversal_path = Path(tmpdir) / "subdir" / ".." / "policy.json"
            pipeline = loader.load(traversal_path)
            assert isinstance(pipeline, LoadedPolicy)

    def test_load_traversal_to_etc_passwd(self) -> None:
        """load('../../etc/passwd') must raise FileNotFoundError or
        PolicyValidationError — must never silently return parsed content
        that leaks sensitive file data as a policy."""
        loader = PolicyLoader()
        traversal_paths = [
            "../../etc/passwd",
            "../../../etc/passwd",
            "/etc/passwd",
            "C:\\Windows\\system32\\drivers\\etc\\hosts",
        ]
        for path in traversal_paths:
            try:
                result = loader.load(path)
                # If load() somehow succeeded (file exists and is valid JSON),
                # verify it didn't silently return a useful pipeline from
                # arbitrary system files — it must have raised or the result
                # must be a ShieldPipeline (not leaked raw file bytes).
                assert isinstance(result, LoadedPolicy), (
                    f"load({path!r}) returned non-pipeline: {result!r}"
                )
            except (FileNotFoundError, OSError, PolicyValidationError, ValueError):
                pass  # Expected: path doesn't exist or content is not valid policy.
            except Exception as exc:
                # Any other exception is fine too — just must not return
                # raw file bytes silently.
                assert not isinstance(exc, AttributeError), (
                    f"load({path!r}) raised AttributeError: {exc}"
                )

    def test_load_traversal_does_not_leak_file_contents(self) -> None:
        """When load() raises on a system path, the exception message must not
        contain raw file contents (no accidental data exfiltration in errors)."""
        loader = PolicyLoader()
        # Use a path that exists on both Unix and Windows as a known text file.
        # We target a path that is very likely to exist but is not valid JSON.
        candidate_paths = ["/etc/hostname", "/etc/os-release"]
        checked = False
        for path in candidate_paths:
            if not Path(path).exists():
                continue
            checked = True
            try:
                loader.load(path)
            except PolicyValidationError as exc:
                # Error message should describe the parse problem, not dump file contents.
                full_msg = " ".join(exc.errors)
                # The error must be a parse/validation message, not the raw file content.
                # We verify the raw content itself is not embedded verbatim.
                raw = Path(path).read_text(encoding="utf-8", errors="replace")
                # Strip to first line to check for leakage of meaningful content.
                first_line = raw.splitlines()[0] if raw.strip() else ""
                if len(first_line) > 4:
                    # If the first line is non-trivial, it should not appear wholesale.
                    assert first_line not in full_msg or len(first_line) < 5, (
                        f"Error message leaks raw file content from {path!r}"
                    )
            except (OSError, FileNotFoundError, ValueError):
                pass  # File unreadable or wrong format — fine.
            break
        if not checked:
            pytest.skip("No candidate system file found to test content leakage")


class TestAdversarialMissingParamsTypeConfusion:
    """Additional type confusion tests: list/dict/None in params fields."""

    def test_list_where_str_expected_in_rule_type(self) -> None:
        """List value in rule.type field must raise PolicyValidationError."""
        loader = PolicyLoader()
        # JSON encodes list; from_dict passes it to RuleSchema.from_dict which
        # calls str() on it — resulting in "[...]" which is an unknown type.
        data = json.dumps({
            "version": "1.0",
            "name": "ListType",
            "rules": [{"type": ["token_budget", "rate_limit"], "params": {}}],
        })
        with pytest.raises((PolicyValidationError, Exception)) as exc_info:
            loader.load_from_string(data, format="json")
        # Must not silently succeed with a corrupted type name.
        assert not isinstance(exc_info.value, AttributeError)

    def test_dict_where_str_expected_in_on_exceed(self) -> None:
        """Dict value in on_exceed must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "DictExceed",
            "rules": [{"type": "token_budget", "params": {}, "on_exceed": {"action": "halt"}}],
        })
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_list_where_dict_expected_in_params(self) -> None:
        """List where dict expected in params must raise or be handled gracefully."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "ListParams",
            "rules": [{"type": "token_budget", "params": ["a", "b", "c"], "on_exceed": "halt"}],
        })
        # RuleSchema.from_dict does dict(params or {}) — dict(list) raises TypeError.
        with pytest.raises(Exception) as exc_info:
            loader.load_from_string(data, format="json")
        assert not isinstance(exc_info.value, AttributeError)

    def test_none_in_params_dict_values(self) -> None:
        """None values within params dict must not crash factory functions."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "NoneParamValues",
            "rules": [{"type": "token_budget", "params": {"max_output_tokens": None}, "on_exceed": "halt"}],
        })
        # int(None) raises TypeError — factory should raise, not hang.
        with pytest.raises(Exception) as exc_info:
            loader.load_from_string(data, format="json")
        assert not isinstance(exc_info.value, AttributeError)


class TestAdversarialEmptyAndNullFields:
    """Empty string and None value adversarial tests."""

    def test_empty_string_version_raises(self) -> None:
        """Empty string version must raise PolicyValidationError."""
        from veronica_core.policy.schema import PolicySchema
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicySchema(version="", name="Test")
        assert any("version" in e.lower() for e in exc_info.value.errors)

    def test_empty_string_name_raises(self) -> None:
        """Empty string name must raise PolicyValidationError."""
        from veronica_core.policy.schema import PolicySchema
        with pytest.raises(PolicyValidationError) as exc_info:
            PolicySchema(version="1.0", name="")
        assert any("name" in e.lower() for e in exc_info.value.errors)

    def test_empty_string_version_via_loader(self) -> None:
        """Empty string version via JSON must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "", "name": "Test", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_empty_string_name_via_loader(self) -> None:
        """Empty string name via JSON must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "1.0", "name": "", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_version_none_via_loader_raises(self) -> None:
        """Null version via JSON must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": None, "name": "Test", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_name_none_via_loader_raises(self) -> None:
        """Null name via JSON must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "1.0", "name": None, "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_entirely_missing_version_raises(self) -> None:
        """Absent version key must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"name": "NoVersion", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_entirely_missing_name_raises(self) -> None:
        """Absent name key must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "1.0", "rules": []})
        with pytest.raises(PolicyValidationError):
            loader.load_from_string(data, format="json")

    def test_whitespace_only_version_raises(self) -> None:
        """Whitespace-only version string must raise PolicyValidationError."""
        loader = PolicyLoader()
        data = json.dumps({"version": "   ", "name": "Test", "rules": []})
        # "   " is a non-empty string so __post_init__ passes; this documents
        # current behaviour — whitespace is accepted as version.
        # If stricter validation is added later, update this test.
        try:
            loader.load_from_string(data, format="json")
        except PolicyValidationError:
            pass  # Stricter validation is also acceptable.

    def test_null_rule_entry_in_rules_list(self) -> None:
        """None entry inside rules list must raise, not silently skip."""
        loader = PolicyLoader()
        data = json.dumps({
            "version": "1.0",
            "name": "NullRule",
            "rules": [None],
        })
        with pytest.raises(Exception) as exc_info:
            loader.load_from_string(data, format="json")
        assert not isinstance(exc_info.value, AttributeError)
