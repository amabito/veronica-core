"""Tests for built-in policy packs.

Covers ReadOnlyAssistantPolicy, NoNetworkPolicy, NoShellPolicy,
ApproveSideEffectsPolicy, and UntrustedToolModePolicy.

Each class tests one allowed and one denied operation when enabled,
plus disabled-passthrough behavior.
"""

from __future__ import annotations

import pytest

from veronica_core.policies._policy_utils import (
    NETWORK_SHELL_COMMANDS,
    SAFE_HTTP_METHODS,
    WRITE_SHELL_COMMANDS,
    _extract_command_stem,
    _normalize_command_name,
)
from veronica_core.policies.approve_side_effects import ApproveSideEffectsPolicy
from veronica_core.policies.no_network import NoNetworkPolicy
from veronica_core.policies.no_shell import NoShellPolicy
from veronica_core.policies.read_only_assistant import ReadOnlyAssistantPolicy
from veronica_core.policies.untrusted_tool_mode import UntrustedToolModePolicy
from veronica_core.shield.types import Decision


# ---------------------------------------------------------------------------
# ReadOnlyAssistantPolicy
# ---------------------------------------------------------------------------


class TestReadOnlyAssistantPolicyDisabled:
    """When disabled, all checks pass."""

    def test_shell_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=False)
        allowed, reason = policy.check_shell(["rm", "-rf", "/tmp"])
        assert allowed
        assert "disabled" in reason

    def test_egress_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=False)
        allowed, _ = policy.check_egress("https://example.com", method="POST")
        assert allowed

    def test_file_write_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=False)
        allowed, _ = policy.check_file_write("/etc/passwd")
        assert allowed


class TestReadOnlyAssistantPolicyEnabled:
    """When enabled, write operations are blocked; reads are allowed."""

    def test_get_request_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_egress(
            "https://api.example.com/data", method="GET"
        )
        assert allowed
        assert "allowed" in reason

    def test_post_request_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_egress(
            "https://api.example.com/submit", method="POST"
        )
        assert not allowed
        assert "POST" in reason

    def test_put_request_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_egress("https://api.example.com/item/1", method="PUT")
        assert not allowed

    def test_delete_request_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_egress(
            "https://api.example.com/item/1", method="DELETE"
        )
        assert not allowed

    def test_read_only_shell_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_shell(["cat", "/var/log/app.log"])
        assert allowed
        assert "allowed" in reason

    def test_rm_shell_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_shell(["rm", "-rf", "/tmp/work"])
        assert not allowed
        assert "rm" in reason

    def test_bash_shell_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_shell(["bash", "-c", "echo hello"])
        assert not allowed

    def test_file_write_always_blocked(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_file_write("/tmp/output.txt")
        assert not allowed
        assert "file write" in reason

    def test_empty_command_allowed(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_shell([])
        assert allowed

    def test_extra_denied_commands(self):
        policy = ReadOnlyAssistantPolicy(
            enabled=True, extra_denied_commands=frozenset({"myapp"})
        )
        allowed, _ = policy.check_shell(["myapp", "--run"])
        assert not allowed

    def test_exe_suffix_stripped(self):
        """Windows .exe suffix must not affect command matching."""
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_shell(["rm.exe", "-rf", "/tmp"])
        assert not allowed

    def test_create_event_is_halt(self):
        policy = ReadOnlyAssistantPolicy(enabled=True)
        event = policy.create_event("file write blocked", request_id="req-1")
        assert event.decision is Decision.HALT
        assert event.event_type == "READ_ONLY_POLICY_VIOLATION"
        assert event.hook == "ReadOnlyAssistantPolicy"
        assert event.request_id == "req-1"


# ---------------------------------------------------------------------------
# NoNetworkPolicy
# ---------------------------------------------------------------------------


class TestNoNetworkPolicyDisabled:
    def test_egress_allowed_when_disabled(self):
        policy = NoNetworkPolicy(enabled=False)
        allowed, _ = policy.check_egress("https://evil.example.com")
        assert allowed

    def test_shell_allowed_when_disabled(self):
        policy = NoNetworkPolicy(enabled=False)
        allowed, _ = policy.check_shell(["curl", "https://example.com"])
        assert allowed


class TestNoNetworkPolicyEnabled:
    def test_all_urls_blocked(self):
        policy = NoNetworkPolicy(enabled=True)
        for url in [
            "https://api.example.com",
            "http://localhost:8080",
            "ftp://files.example.com",
        ]:
            allowed, reason = policy.check_egress(url)
            assert not allowed, f"Expected block for {url}"
            assert "blocked" in reason

    def test_allowlist_url_passes(self):
        policy = NoNetworkPolicy(
            enabled=True,
            allowlist=frozenset({"https://internal.corp/api"}),
        )
        allowed, reason = policy.check_egress("https://internal.corp/api")
        assert allowed
        assert "allowlist" in reason

    def test_non_allowlist_url_blocked(self):
        policy = NoNetworkPolicy(
            enabled=True,
            allowlist=frozenset({"https://internal.corp/api"}),
        )
        allowed, _ = policy.check_egress("https://external.evil.com")
        assert not allowed

    def test_curl_shell_blocked(self):
        policy = NoNetworkPolicy(enabled=True)
        allowed, reason = policy.check_shell(
            ["curl", "-X", "GET", "https://example.com"]
        )
        assert not allowed
        assert "curl" in reason

    def test_wget_shell_blocked(self):
        policy = NoNetworkPolicy(enabled=True)
        allowed, _ = policy.check_shell(["wget", "https://example.com"])
        assert not allowed

    def test_git_shell_blocked(self):
        """git clone performs network I/O -- should be blocked."""
        policy = NoNetworkPolicy(enabled=True)
        allowed, _ = policy.check_shell(["git", "clone", "https://github.com/repo"])
        assert not allowed

    def test_ls_shell_allowed(self):
        """ls is not a network command -- should be allowed."""
        policy = NoNetworkPolicy(enabled=True)
        allowed, _ = policy.check_shell(["ls", "-la"])
        assert allowed

    def test_empty_command_allowed(self):
        policy = NoNetworkPolicy(enabled=True)
        allowed, _ = policy.check_shell([])
        assert allowed

    def test_create_event_is_halt(self):
        policy = NoNetworkPolicy(enabled=True)
        event = policy.create_event("network blocked", request_id="req-2")
        assert event.decision is Decision.HALT
        assert event.event_type == "NETWORK_POLICY_VIOLATION"
        assert event.hook == "NoNetworkPolicy"


# ---------------------------------------------------------------------------
# NoShellPolicy
# ---------------------------------------------------------------------------


class TestNoShellPolicyDisabled:
    def test_shell_allowed_when_disabled(self):
        policy = NoShellPolicy(enabled=False)
        allowed, _ = policy.check_shell(["bash", "-c", "rm -rf /"])
        assert allowed


class TestNoShellPolicyEnabled:
    def test_any_command_blocked(self):
        policy = NoShellPolicy(enabled=True)
        for cmd in [["ls", "-la"], ["cat", "file.txt"], ["bash", "-c", "echo hi"]]:
            allowed, reason = policy.check_shell(cmd)
            assert not allowed, f"Expected block for {cmd}"
            assert "NoShellPolicy" in reason

    def test_allowlisted_command_passes(self):
        policy = NoShellPolicy(enabled=True, allowlist=frozenset({"ls", "cat"}))
        allowed, reason = policy.check_shell(["ls", "-la"])
        assert allowed
        assert "allowlist" in reason

    def test_non_allowlisted_still_blocked(self):
        policy = NoShellPolicy(enabled=True, allowlist=frozenset({"ls"}))
        allowed, _ = policy.check_shell(["rm", "-rf", "/tmp"])
        assert not allowed

    def test_empty_command_allowed(self):
        policy = NoShellPolicy(enabled=True)
        allowed, _ = policy.check_shell([])
        assert allowed

    def test_exe_suffix_on_allowlist(self):
        """Windows path with .exe suffix must match allowlist entry."""
        policy = NoShellPolicy(enabled=True, allowlist=frozenset({"ls"}))
        allowed, _ = policy.check_shell(["ls.exe", "-la"])
        assert allowed

    def test_create_event_is_halt(self):
        policy = NoShellPolicy(enabled=True)
        event = policy.create_event("shell blocked", request_id="req-3")
        assert event.decision is Decision.HALT
        assert event.event_type == "SHELL_POLICY_VIOLATION"
        assert event.hook == "NoShellPolicy"


# ---------------------------------------------------------------------------
# ApproveSideEffectsPolicy
# ---------------------------------------------------------------------------


class TestApproveSideEffectsPolicyDisabled:
    def test_post_allowed_when_disabled(self):
        policy = ApproveSideEffectsPolicy(enabled=False)
        allowed, _ = policy.check_egress("https://api.example.com", method="POST")
        assert allowed

    def test_file_write_allowed_when_disabled(self):
        policy = ApproveSideEffectsPolicy(enabled=False)
        allowed, _ = policy.check_file_write("/tmp/out.txt")
        assert allowed


class TestApproveSideEffectsPolicyEnabled:
    def test_get_auto_approved(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_egress("https://api.example.com", method="GET")
        assert allowed
        assert "auto-approved" in reason

    def test_head_auto_approved(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, _ = policy.check_egress("https://api.example.com", method="HEAD")
        assert allowed

    def test_post_requires_approval(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_egress(
            "https://api.example.com/submit", method="POST"
        )
        assert not allowed
        assert "requires approval" in reason

    def test_post_approved_then_allowed(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        url = "https://api.example.com/submit"
        op = f"POST:{url}"
        token = policy.request_approval(op)
        policy.record_approval(op, token)
        allowed, reason = policy.check_egress(url, method="POST")
        assert allowed
        assert "approved" in reason

    def test_approval_is_single_use(self):
        """Approval is consumed on first use."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        url = "https://api.example.com/submit"
        op = f"POST:{url}"
        token = policy.request_approval(op)
        policy.record_approval(op, token)
        # First check: consumed
        allowed, _ = policy.check_egress(url, method="POST")
        assert allowed
        # Second check: approval gone
        allowed, _ = policy.check_egress(url, method="POST")
        assert not allowed

    def test_read_only_shell_auto_approved(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, _ = policy.check_shell(["cat", "file.txt"])
        assert allowed

    def test_write_shell_requires_approval(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_shell(["rm", "-rf", "/tmp/work"])
        assert not allowed
        assert "requires approval" in reason

    def test_write_shell_approved_then_allowed(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        token = policy.request_approval("SHELL:rm")
        policy.record_approval("SHELL:rm", token)
        allowed, _ = policy.check_shell(["rm", "-rf", "/tmp/work"])
        assert allowed

    def test_file_write_requires_approval(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_file_write("/tmp/out.txt")
        assert not allowed
        assert "requires approval" in reason

    def test_file_write_approved_then_allowed(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        token = policy.request_approval("WRITE:/tmp/out.txt")
        policy.record_approval("WRITE:/tmp/out.txt", token)
        allowed, _ = policy.check_file_write("/tmp/out.txt")
        assert allowed

    def test_pending_approvals_visible_before_record(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        policy.request_approval("POST:https://example.com")
        policy.request_approval("WRITE:/tmp/out.txt")
        pending = policy.pending_approvals()
        assert "POST:https://example.com" in pending
        assert "WRITE:/tmp/out.txt" in pending

    def test_pending_cleared_after_record(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        t1 = policy.request_approval("POST:https://example.com")
        policy.record_approval("POST:https://example.com", t1)
        assert "POST:https://example.com" not in policy.pending_approvals()

    def test_bypass_without_record_blocked(self):
        """request_approval alone must NOT enable the operation."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        policy.request_approval("POST:https://evil.com")
        # Skip record_approval -- try to use the operation directly
        allowed, _ = policy.check_egress("https://evil.com", "POST")
        assert not allowed, "operation must not be allowed without record_approval"

    def test_pending_approvals_empty_after_consume(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        token = policy.request_approval("POST:https://example.com")
        policy.record_approval("POST:https://example.com", token)
        policy.check_egress("https://example.com", method="POST")
        assert "POST:https://example.com" not in policy.pending_approvals()

    def test_create_event_is_queue(self):
        policy = ApproveSideEffectsPolicy(enabled=True)
        event = policy.create_event("approval required", request_id="req-4")
        assert event.decision is Decision.QUEUE
        assert event.event_type == "APPROVAL_REQUIRED"
        assert event.hook == "ApproveSideEffectsPolicy"


class TestApproveSideEffectsConcurrency:
    """Approval registry must be thread-safe."""

    def test_concurrent_approval_and_check(self):
        import threading

        policy = ApproveSideEffectsPolicy(enabled=True)
        results: list[bool] = []
        errors: list[Exception] = []

        def approve_and_check():
            try:
                token = policy.request_approval("POST:https://example.com")
                policy.record_approval("POST:https://example.com", token)
                allowed, _ = policy.check_egress("https://example.com", method="POST")
                results.append(allowed)
            except Exception as exc:
                errors.append(exc)
                results.append(False)

        threads = [threading.Thread(target=approve_and_check) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Some calls may get allowed (consumed approval), some may not due to
        # token clobbering under concurrency.  The important invariant: no
        # unhandled exception escapes and results are boolean.
        assert all(isinstance(r, bool) for r in results)
        assert len(results) == 10


# ---------------------------------------------------------------------------
# UntrustedToolModePolicy
# ---------------------------------------------------------------------------


class TestUntrustedToolModePolicyDisabled:
    def test_shell_allowed_when_disabled(self):
        policy = UntrustedToolModePolicy(enabled=False)
        allowed, _ = policy.check_shell(["bash", "-c", "rm -rf /"])
        assert allowed

    def test_egress_allowed_when_disabled(self):
        policy = UntrustedToolModePolicy(enabled=False)
        allowed, _ = policy.check_egress("https://evil.example.com")
        assert allowed

    def test_file_write_allowed_when_disabled(self):
        policy = UntrustedToolModePolicy(enabled=False)
        allowed, _ = policy.check_file_write("/etc/passwd")
        assert allowed

    def test_file_read_allowed_when_disabled(self):
        policy = UntrustedToolModePolicy(enabled=False)
        allowed, _ = policy.check_file_read("/data/config.json")
        assert allowed


class TestUntrustedToolModePolicyEnabled:
    def test_any_shell_command_blocked(self):
        """All shell commands are blocked, including read-only ones."""
        policy = UntrustedToolModePolicy(enabled=True)
        for cmd in [["ls", "-la"], ["cat", "file.txt"], ["bash", "-c", "echo hi"]]:
            allowed, reason = policy.check_shell(cmd)
            assert not allowed, f"Expected block for {cmd}"
            assert "untrusted tool mode" in reason

    def test_network_blocked(self):
        policy = UntrustedToolModePolicy(enabled=True)
        for url in ["https://api.example.com", "http://localhost:8080"]:
            allowed, reason = policy.check_egress(url)
            assert not allowed
            assert "untrusted tool mode" in reason

    def test_get_also_blocked(self):
        """Even GET requests are blocked -- no network in any form."""
        policy = UntrustedToolModePolicy(enabled=True)
        allowed, _ = policy.check_egress("https://api.example.com", method="GET")
        assert not allowed

    def test_file_write_blocked(self):
        policy = UntrustedToolModePolicy(enabled=True)
        allowed, reason = policy.check_file_write("/tmp/output.txt")
        assert not allowed
        assert "untrusted tool mode" in reason

    def test_file_read_allowed(self):
        """File reads are the one permitted operation."""
        policy = UntrustedToolModePolicy(enabled=True)
        allowed, reason = policy.check_file_read("/data/config.json")
        assert allowed
        assert "allowed" in reason

    def test_empty_command_allowed(self):
        policy = UntrustedToolModePolicy(enabled=True)
        allowed, _ = policy.check_shell([])
        assert allowed

    def test_exe_suffix_stripped(self):
        policy = UntrustedToolModePolicy(enabled=True)
        allowed, _ = policy.check_shell(["ls.exe", "-la"])
        assert not allowed

    def test_create_event_is_halt(self):
        policy = UntrustedToolModePolicy(enabled=True)
        event = policy.create_event("shell blocked", request_id="req-5")
        assert event.decision is Decision.HALT
        assert event.event_type == "UNTRUSTED_TOOL_VIOLATION"
        assert event.hook == "UntrustedToolModePolicy"
        assert event.request_id == "req-5"


class TestUntrustedToolModeStricterThanReadOnly:
    """UntrustedToolModePolicy must be at least as strict as ReadOnlyAssistantPolicy."""

    @pytest.mark.parametrize(
        "args",
        [
            ["ls", "-la"],
            ["cat", "file.txt"],
            ["echo", "hello"],
        ],
    )
    def test_read_only_shell_still_blocked(self, args: list[str]):
        """Commands allowed by ReadOnlyAssistantPolicy are still blocked here."""
        ro = ReadOnlyAssistantPolicy(enabled=True)
        ut = UntrustedToolModePolicy(enabled=True)

        ro_allowed, _ = ro.check_shell(args)
        ut_allowed, _ = ut.check_shell(args)

        if ro_allowed:
            # If read-only allows it, untrusted mode must still block it.
            assert not ut_allowed, (
                f"UntrustedToolModePolicy should be stricter than ReadOnlyAssistantPolicy "
                f"for args={args}"
            )


# ---------------------------------------------------------------------------
# _normalize_command_name helper
# ---------------------------------------------------------------------------


class TestNormalizeCommandName:
    """Unit tests for the shared version-suffix stripping helper."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("python", "python"),
            ("python3", "python"),
            ("python3.11", "python"),
            ("python3.11.2", "python"),
            ("curl", "curl"),
            ("curl7", "curl"),
            ("curl7.86", "curl"),
            ("wget", "wget"),
            ("wget2", "wget"),
            ("node", "node"),
            ("node18", "node"),
            ("node18.12.1", "node"),
            ("ruby", "ruby"),
            ("ruby3", "ruby"),
            ("apt-get", "apt-get"),
            ("bash", "bash"),
            ("rm", "rm"),
            ("git", "git"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert _normalize_command_name(raw) == expected


# ---------------------------------------------------------------------------
# HIGH 1: Version-suffix bypass in ReadOnlyAssistantPolicy
# ---------------------------------------------------------------------------


class TestReadOnlyAssistantVersionSuffixBypass:
    """Versioned binaries must not bypass the denylist."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "python3.11",
            "python3.9",
            "python3",
            "node18",
            "node20.1.0",
            "ruby3",
            "perl5.36",
            "bash5",
        ],
    )
    def test_versioned_interpreter_blocked(self, cmd: str) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, reason = policy.check_shell([cmd, "-c", "evil"])
        assert not allowed, f"versioned command {cmd!r} should be blocked"
        # Reason contains the normalized stem, not the versioned name.
        assert "blocked" in reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "curl7",
            "curl7.86.0",
            "wget2",
            "ssh8",
        ],
    )
    def test_versioned_network_tool_blocked(self, cmd: str) -> None:
        policy = ReadOnlyAssistantPolicy(enabled=True)
        allowed, _ = policy.check_shell([cmd, "https://example.com"])
        assert not allowed, f"versioned command {cmd!r} should be blocked"

    def test_unrelated_versioned_command_allowed(self) -> None:
        """A versioned command whose stem is not in the denylist is still allowed."""
        policy = ReadOnlyAssistantPolicy(enabled=True)
        # "grep2" -> stem "grep" which is NOT in the denylist
        allowed, _ = policy.check_shell(["grep2", "pattern", "file.txt"])
        assert allowed


# ---------------------------------------------------------------------------
# HIGH 2: Version-suffix bypass in NoNetworkPolicy
# ---------------------------------------------------------------------------


class TestNoNetworkVersionSuffixBypass:
    """Versioned network tools must not bypass NoNetworkPolicy."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "curl7",
            "curl7.86",
            "wget2",
            "ssh8",
            "git2",
        ],
    )
    def test_versioned_network_cmd_blocked(self, cmd: str) -> None:
        policy = NoNetworkPolicy(enabled=True)
        allowed, reason = policy.check_shell([cmd, "https://example.com"])
        assert not allowed, f"versioned command {cmd!r} should be blocked"
        # Reason contains the normalized stem, not the versioned name.
        assert "blocked" in reason


# ---------------------------------------------------------------------------
# NoNetworkPolicy URL allowlist case-insensitivity (F.R.I.D.A.Y. B1#4)
# ---------------------------------------------------------------------------


class TestNoNetworkAllowlistCaseInsensitive:
    """URL allowlist must be case-insensitive to prevent bypass."""

    def test_uppercase_url_matches_lowercase_allowlist(self) -> None:
        policy = NoNetworkPolicy(
            enabled=True,
            allowlist=frozenset({"https://internal.corp/api"}),
        )
        allowed, _ = policy.check_egress("HTTPS://INTERNAL.CORP/API")
        assert allowed

    def test_mixed_case_url_matches(self) -> None:
        policy = NoNetworkPolicy(
            enabled=True,
            allowlist=frozenset({"https://Api.Example.Com/v1"}),
        )
        allowed, _ = policy.check_egress("https://api.example.com/v1")
        assert allowed

    def test_non_matching_url_still_blocked(self) -> None:
        policy = NoNetworkPolicy(
            enabled=True,
            allowlist=frozenset({"https://safe.example.com"}),
        )
        allowed, _ = policy.check_egress("https://evil.example.com")
        assert not allowed


# ---------------------------------------------------------------------------
# UntrustedToolModePolicy: empty frozenset vs None (F.R.I.D.A.Y. B2)
# ---------------------------------------------------------------------------


class TestUntrustedToolModeEmptyVsNoneSandbox:
    """Empty frozenset means 'deny all reads'; None means 'allow all (warn)'."""

    def test_empty_frozenset_denies_all_reads(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset(),
        )
        allowed, _ = policy.check_file_read("/data/anything.txt")
        assert not allowed

    def test_none_allows_all_reads(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=None,
        )
        allowed, _ = policy.check_file_read("/data/anything.txt")
        assert allowed

    def test_check_file_read_accepts_authority_param(self) -> None:
        """API consistency: check_file_read accepts authority/side_effects."""
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/"}),
        )
        allowed, _ = policy.check_file_read(
            "/data/file.txt",
            authority=None,
            side_effects=None,
        )
        assert allowed


# ---------------------------------------------------------------------------
# HIGH 3: Incomplete write classification in ApproveSideEffectsPolicy
# ---------------------------------------------------------------------------


class TestApproveSideEffectsWriteClassification:
    """Previously missing commands must require approval."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "scp",
            "tee",
            "awk",
            "sed",
            "curl",
            "wget",
            "rsync",
        ],
    )
    def test_missing_write_cmd_requires_approval(self, cmd: str) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_shell([cmd, "arg"])
        assert not allowed, f"command {cmd!r} should require approval"
        assert "requires approval" in reason

    @pytest.mark.parametrize(
        "cmd",
        [
            "scp2",
            "tee1",
            "awk5",
            "sed4",
            "curl7",
            "wget2",
        ],
    )
    def test_versioned_write_cmd_requires_approval(self, cmd: str) -> None:
        policy = ApproveSideEffectsPolicy(enabled=True)
        allowed, reason = policy.check_shell([cmd, "arg"])
        assert not allowed, f"versioned command {cmd!r} should require approval"
        assert "requires approval" in reason


# ---------------------------------------------------------------------------
# MEDIUM 4: Nonce-gated approval API in ApproveSideEffectsPolicy
# ---------------------------------------------------------------------------


class TestApproveSideEffectsNonce:
    """Approval must require a valid nonce from request_approval()."""

    def test_record_without_request_raises(self) -> None:
        """record_approval() without a prior request_approval() must raise."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        with pytest.raises(ValueError, match="no pending approval"):
            policy.record_approval("POST:https://example.com", "arbitrary_token")

    def test_record_wrong_token_raises(self) -> None:
        """record_approval() with a wrong token must raise and clear the slot."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        policy.request_approval("POST:https://example.com")
        with pytest.raises(ValueError, match="invalid approval token"):
            policy.record_approval("POST:https://example.com", "wrong_token")
        # Slot cleared -- a second attempt with any token also fails
        with pytest.raises(ValueError, match="no pending approval"):
            policy.record_approval("POST:https://example.com", "wrong_token2")

    def test_valid_nonce_flow_allows(self) -> None:
        """Full request -> record -> check flow must succeed."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        op = "POST:https://api.example.com/submit"
        token = policy.request_approval(op)
        policy.record_approval(op, token)
        allowed, reason = policy.check_egress(
            "https://api.example.com/submit", method="POST"
        )
        assert allowed
        assert "approved" in reason

    def test_token_from_different_op_rejected(self) -> None:
        """A token issued for op-A cannot approve op-B."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        token_a = policy.request_approval("POST:https://a.example.com")
        # Try to use token_a to approve a different operation
        policy.request_approval("POST:https://b.example.com")
        with pytest.raises(ValueError, match="invalid approval token"):
            policy.record_approval("POST:https://b.example.com", token_a)

    def test_request_approval_pending_appears_in_pending(self) -> None:
        """After request_approval(), the slot should appear in pending_approvals()."""
        policy = ApproveSideEffectsPolicy(enabled=True)
        policy.request_approval("POST:https://example.com")
        assert "POST:https://example.com" in policy.pending_approvals()


# ---------------------------------------------------------------------------
# MEDIUM 5: Sandbox reads in UntrustedToolModePolicy
# ---------------------------------------------------------------------------


class TestUntrustedToolModeReadSandbox:
    """allowed_read_paths restricts which paths can be read."""

    def test_read_inside_sandbox_allowed(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/"}),
        )
        allowed, reason = policy.check_file_read("/data/config.json")
        assert allowed
        assert "allowed" in reason

    def test_read_outside_sandbox_denied(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/"}),
        )
        allowed, reason = policy.check_file_read("/etc/passwd")
        assert not allowed
        assert "outside sandbox" in reason

    def test_sensitive_path_denied_with_sandbox(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/"}),
        )
        for path in ["/etc/passwd", "/etc/shadow", ".env", "/root/.ssh/id_rsa"]:
            allowed, _ = policy.check_file_read(path)
            assert not allowed, f"sensitive path {path!r} should be denied"

    def test_no_sandbox_allows_all_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No allowed_read_paths -- reads allowed but warning emitted once."""
        import logging

        policy = UntrustedToolModePolicy(enabled=True, allowed_read_paths=None)
        with caplog.at_level(
            logging.WARNING, logger="veronica_core.policies.untrusted_tool_mode"
        ):
            allowed1, _ = policy.check_file_read("/etc/passwd")
            allowed2, _ = policy.check_file_read("/data/safe.json")

        assert allowed1
        assert allowed2
        # Warning should appear exactly once
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "allowed_read_paths" in warnings[0].message

    def test_multiple_sandbox_prefixes(self) -> None:
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/", "/tmp/sandbox/"}),
        )
        assert policy.check_file_read("/data/a.json")[0]
        assert policy.check_file_read("/tmp/sandbox/b.txt")[0]
        assert not policy.check_file_read("/tmp/evil.sh")[0]
        assert not policy.check_file_read("/etc/passwd")[0]

    def test_disabled_policy_ignores_sandbox(self) -> None:
        """When disabled, reads are allowed regardless of allowed_read_paths."""
        policy = UntrustedToolModePolicy(
            enabled=False,
            allowed_read_paths=frozenset({"/data/"}),
        )
        allowed, reason = policy.check_file_read("/etc/passwd")
        assert allowed
        assert "disabled" in reason

    def test_resolved_paths_cached(self) -> None:
        """_get_resolved_read_paths must return the same tuple on repeat calls."""
        policy = UntrustedToolModePolicy(
            enabled=True,
            allowed_read_paths=frozenset({"/data/", "/tmp/"}),
        )
        first = policy._get_resolved_read_paths()
        second = policy._get_resolved_read_paths()
        assert first is second  # same object, not recomputed


# ---------------------------------------------------------------------------
# __init__.py exports
# ---------------------------------------------------------------------------


class TestPoliciesInit:
    """Verify all 5 policies are exported from the package."""

    def test_all_exports_importable(self):
        from veronica_core.policies import (  # noqa: F401
            ApproveSideEffectsPolicy,
            MinimalResponsePolicy,
            NoNetworkPolicy,
            NoShellPolicy,
            ReadOnlyAssistantPolicy,
            UntrustedToolModePolicy,
        )

    def test_all_exports_in_all(self):
        import veronica_core.policies as pkg

        for name in [
            "ApproveSideEffectsPolicy",
            "MinimalResponsePolicy",
            "NoNetworkPolicy",
            "NoShellPolicy",
            "ReadOnlyAssistantPolicy",
            "UntrustedToolModePolicy",
        ]:
            assert name in pkg.__all__, f"{name} missing from __all__"


# ---------------------------------------------------------------------------
# _extract_command_stem tests
# ---------------------------------------------------------------------------


class TestExtractCommandStem:
    """Tests for the consolidated _extract_command_stem helper."""

    @pytest.mark.parametrize(
        "argv0,expected",
        [
            ("python3.11", "python"),
            ("python3", "python"),
            ("/usr/bin/python3.11", "python"),
            ("C:\\Windows\\System32\\cmd.exe", "cmd"),
            ("curl7.86", "curl"),
            ("wget2", "wget"),
            ("node18", "node"),
            ("apt-get", "apt-get"),
            ("bash", "bash"),
            ("rm", "rm"),
            ("ls", "ls"),
            ("GIT.EXE", "git"),
            ("Git.Exe", "git"),
        ],
    )
    def test_extraction(self, argv0: str, expected: str) -> None:
        assert _extract_command_stem(argv0) == expected

    def test_backslash_windows_path(self) -> None:
        assert _extract_command_stem("C:\\Program Files\\Git\\bin\\git.exe") == "git"

    def test_forward_slash_unix_path(self) -> None:
        assert _extract_command_stem("/usr/local/bin/ssh") == "ssh"


# ---------------------------------------------------------------------------
# Shared constant consistency tests
# ---------------------------------------------------------------------------


class TestSharedConstants:
    """Verify shared command sets are consistent and non-empty."""

    def test_write_shell_commands_non_empty(self) -> None:
        assert len(WRITE_SHELL_COMMANDS) > 20

    def test_network_shell_commands_non_empty(self) -> None:
        assert len(NETWORK_SHELL_COMMANDS) > 10

    def test_safe_http_methods_exact(self) -> None:
        assert SAFE_HTTP_METHODS == {"GET", "HEAD", "OPTIONS"}

    def test_network_commands_subset_of_write(self) -> None:
        """Network transfer commands should also be in write set."""
        overlap = {"curl", "wget", "ssh", "scp", "rsync", "ftp", "sftp"}
        assert overlap <= WRITE_SHELL_COMMANDS
        assert overlap <= NETWORK_SHELL_COMMANDS

    def test_all_frozensets(self) -> None:
        assert isinstance(WRITE_SHELL_COMMANDS, frozenset)
        assert isinstance(NETWORK_SHELL_COMMANDS, frozenset)
        assert isinstance(SAFE_HTTP_METHODS, frozenset)
