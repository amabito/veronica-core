# VERONICA Security Claims

Each claim is verified by one or more automated tests.
Run `pytest tests/ -v` to verify all claims.

## Claim Format

- **Claim**: A specific, verifiable security property
- **Phase**: Which phase implements it
- **Tests**: Exact pytest test node IDs

---

## Shell Execution Containment

### CLAIM-01: rm -rf is always DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAttackRegressionCases::test_case_01_rm_rf_root_is_denied`
`tests/redteam/test_redteam.py::TestCategoryADataExfiltration::test_a3_shell_curl_post_file`

### CLAIM-02: powershell is always DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAttackRegressionCases::test_case_02_powershell_execution_policy_bypass_is_denied`
`tests/redteam/test_redteam.py::TestCategoryDPersistence::test_d20_shell_powershell_encoded_command`

### CLAIM-03: cmd.exe shell is always DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAttackRegressionCases::test_case_03_cmd_shell_is_denied`
`tests/redteam/test_redteam.py::TestCategoryDPersistence::test_d19_shell_cmd_echo_to_startup`

### CLAIM-04: Pipe operator in shell is always DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAdditionalPolicyEdgeCases::test_pipe_operator_in_shell_is_denied`

### CLAIM-05: Redirect operator in shell is always DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAdditionalPolicyEdgeCases::test_redirect_operator_in_shell_is_denied`

### CLAIM-06: Raw subprocess usage is blocked at source level (AST lint)

**Phase**: E-1
**Tests**:
`tests/security/test_lint_no_raw_exec.py::TestSubprocessDetection::test_subprocess_run`
`tests/security/test_lint_no_raw_exec.py::TestSubprocessDetection::test_subprocess_popen`
`tests/security/test_lint_no_raw_exec.py::TestOsDetection::test_os_system`
`tests/security/test_lint_no_raw_exec.py::TestMain::test_main_exits_1_on_violation`
`tests/security/test_lint_no_raw_exec.py::TestMain::test_main_on_actual_src`

### CLAIM-07: All 20 red-team attack scenarios are blocked

**Phase**: F
**Tests**:
`tests/redteam/test_redteam.py::test_all_20_scenarios_blocked`

### CLAIM-08: Windows registry modification is DENY

**Phase**: A
**Tests**:
`tests/redteam/test_redteam.py::TestCategoryDPersistence::test_d16_shell_reg_add`

### CLAIM-09: Scheduled task creation is DENY

**Phase**: A
**Tests**:
`tests/redteam/test_redteam.py::TestCategoryDPersistence::test_d17_shell_schtasks_create`

---

## Sensitive File Access

### CLAIM-10: .env file read is DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAttackRegressionCases::test_case_05_file_read_env_is_denied`
`tests/redteam/test_redteam.py::TestCategoryBCredentialHunt::test_b9_read_env_file`

### CLAIM-11: SSH private key read is DENY

**Phase**: A, E-2
**Tests**:
`tests/security/test_policy_engine.py::TestAdditionalPolicyEdgeCases::test_file_read_ssh_key_is_denied`
`tests/redteam/test_redteam.py::TestCategoryBCredentialHunt::test_b6_read_ssh_id_rsa`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_id_rsa_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_id_ed25519_is_denied`

### CLAIM-12: Chrome cookies/login data read is DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAttackRegressionCases::test_case_06_file_read_chrome_login_data_is_denied`
`tests/redteam/test_redteam.py::TestCategoryBCredentialHunt::test_b8_read_chrome_login_data`

### CLAIM-13: AWS credentials file read is DENY

**Phase**: A
**Tests**:
`tests/redteam/test_redteam.py::TestCategoryBCredentialHunt::test_b7_read_aws_credentials`

### CLAIM-14: .pem, .p12, .pfx, .key files are DENY

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_pem_file_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_key_file_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_p12_file_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_pfx_file_is_denied`

### CLAIM-15: .npmrc, .pypirc, .netrc credential files are DENY

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_npmrc_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_pypirc_is_denied`
`tests/security/test_e2_secrets.py::TestFileReadCredentialPatterns::test_netrc_is_denied`

---

## Secret Masking

### CLAIM-16: NPM tokens are masked in all output

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_npm_token_masked`

### CLAIM-17: GitHub PAT and OAuth tokens are masked in all output

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_github_fine_grained_pat_masked`
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_github_cli_oauth_token_masked`

### CLAIM-18: SSH private key headers are masked in all output

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_ssh_rsa_private_key_header_masked`
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_ssh_openssh_private_key_header_masked`
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_ssh_dsa_private_key_header_masked`
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_ssh_ec_private_key_header_masked`

### CLAIM-19: PyPI tokens are masked in all output

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_pypi_token_masked`
`tests/security/test_e2_secrets.py::TestMaskingNewPatterns::test_pypi_token_standalone_masked`

### CLAIM-20: Secrets are masked in audit log entries

**Phase**: A, C
**Tests**:
`tests/security/test_audit_log.py::TestAuditLogSecretMasking::test_aws_key_is_masked_in_log`
`tests/security/test_audit_log.py::TestAuditLogSecretMasking::test_password_kv_is_masked_in_log`
`tests/security/test_audit_log.py::TestAuditLogSecretMasking::test_chain_valid_after_masked_write`

---

## Network Exfiltration

### CLAIM-21: HTTP POST to any host is DENY

**Phase**: A
**Tests**:
`tests/security/test_policy_engine.py::TestAdditionalPolicyEdgeCases::test_net_post_pypi_is_denied`
`tests/redteam/test_redteam.py::TestCategoryADataExfiltration::test_a5_net_post_secrets`
`tests/security/test_e3_network.py::TestGeneralNetworkRules::test_post_to_allowlisted_host_is_denied`

### CLAIM-22: HTTP GET to non-allowlisted hosts is DENY

**Phase**: A, E-3
**Tests**:
`tests/security/test_policy_engine.py::TestAdditionalPolicyEdgeCases::test_net_get_unknown_host_is_denied`
`tests/security/test_e3_network.py::TestGeneralNetworkRules::test_non_allowlisted_host_is_denied`

### CLAIM-23: High-entropy query parameters are DENY (exfil detection)

**Phase**: E-3
**Tests**:
`tests/security/test_e3_network.py::TestHighEntropyQueryDetection::test_high_entropy_value_is_denied`
`tests/security/test_e3_network.py::TestHighEntropyQueryDetection::test_low_entropy_value_is_not_denied_on_entropy_rule`

### CLAIM-24: Base64-encoded query parameters are DENY

**Phase**: E-3
**Tests**:
`tests/security/test_e3_network.py::TestBase64QueryDetection::test_base64_query_value_is_denied`
`tests/redteam/test_redteam.py::TestCategoryADataExfiltration::test_a1_get_with_base64_secret_in_query`

### CLAIM-25: Hex-encoded query parameters are DENY

**Phase**: E-3
**Tests**:
`tests/security/test_e3_network.py::TestHexQueryDetection::test_hex_token_is_denied`
`tests/redteam/test_redteam.py::TestCategoryADataExfiltration::test_a2_get_to_pypi_with_hex_secret_in_query`

### CLAIM-26: URLs exceeding 2048 characters are DENY

**Phase**: E-3
**Tests**:
`tests/security/test_e3_network.py::TestUrlLengthLimit::test_url_exceeding_2048_chars_is_denied`
`tests/security/test_e3_network.py::TestUrlLengthLimit::test_url_exactly_2048_chars_is_allowed`

---

## Supply Chain

### CLAIM-27: pip install requires approval

**Phase**: G-2
**Tests**:
`tests/security/test_supply_chain.py::test_pip_install_requires_approval`
`tests/security/test_supply_chain.py::test_pip_install_requirements_requires_approval`
`tests/security/test_e2_secrets.py::TestShellCredentialSubcommands::test_npm_install_requires_approval`

### CLAIM-28: npm/pnpm/yarn install requires approval

**Phase**: G-2
**Tests**:
`tests/security/test_supply_chain.py::test_npm_install_requires_approval`
`tests/security/test_supply_chain.py::test_pnpm_add_requires_approval`
`tests/security/test_supply_chain.py::test_yarn_add_requires_approval`

### CLAIM-29: uv add and cargo add require approval

**Phase**: G-2
**Tests**:
`tests/security/test_supply_chain.py::test_uv_add_requires_approval`
`tests/security/test_supply_chain.py::test_cargo_add_requires_approval`

### CLAIM-30: SBOM diff detects added/removed/changed packages

**Phase**: G-2
**Tests**:
`tests/tools/test_sbom_diff.py::TestDiffSbomAdded::test_added_package_detected`
`tests/tools/test_sbom_diff.py::TestDiffSbomRemoved::test_removed_package_detected`
`tests/tools/test_sbom_diff.py::TestDiffSbomChanged::test_version_change_detected`

### CLAIM-31: SBOM diff approval token is cryptographically verified

**Phase**: I-3
**Tests**:
`tests/tools/test_sbom_diff.py::TestApprovalToken::test_verify_valid_token`
`tests/tools/test_sbom_diff.py::TestApprovalToken::test_verify_wrong_token_fails`
`tests/tools/test_sbom_diff.py::TestApprovalToken::test_verify_wrong_secret_fails`

### CLAIM-32: git credential and gh auth token commands are DENY

**Phase**: E-2
**Tests**:
`tests/security/test_e2_secrets.py::TestShellCredentialSubcommands::test_git_credential_store_is_denied`
`tests/security/test_e2_secrets.py::TestShellCredentialSubcommands::test_gh_auth_login_is_denied`
`tests/security/test_e2_secrets.py::TestShellCredentialSubcommands::test_gh_token_is_denied`
`tests/security/test_e2_secrets.py::TestShellCredentialSubcommands::test_gh_secret_list_is_denied`

---

## Policy Integrity

### CLAIM-33: Tampered policy YAML is rejected at load time (HMAC v1)

**Phase**: G-1
**Tests**:
`tests/security/test_policy_signing.py::test_tampered_yaml_returns_false`
`tests/security/test_policy_signing.py::test_policy_engine_invalid_sig_raises`
`tests/security/test_policy_signing.py::test_policy_engine_tamper_raises_for_caller`

### CLAIM-34: Policy with invalid Ed25519 signature is rejected (v2)

**Phase**: I-1
**Tests**:
`tests/security/test_policy_signing_v2.py::test_tampered_yaml_fails_verification`
`tests/security/test_policy_signing_v2.py::test_tampered_sig_fails_verification`
`tests/security/test_policy_signing_v2.py::test_wrong_key_fails_verification`
`tests/security/test_policy_signing_v2.py::test_policy_engine_v2_tampered_raises`

### CLAIM-35: In CI/PROD, unsigned policy causes RuntimeError

**Phase**: J-1
**Tests**:
`tests/security/test_policy_signing.py::test_policy_engine_invalid_sig_raises`
`tests/security/test_policy_signing_v2.py::test_policy_engine_v2_tampered_raises`

### CLAIM-36: The committed public key and signature verify against default.yaml

**Phase**: I-1
**Tests**:
`tests/security/test_policy_signing_v2.py::test_committed_public_key_and_sig_verify`
`tests/security/test_policy_signing.py::test_default_policy_file_verifies`

---

## Key Management

### CLAIM-37: Key pin mismatch causes RuntimeError in CI/PROD

**Phase**: J-2
**Tests**:
`tests/security/test_key_pin.py::TestKeyPinCheckerEnforce::test_ci_wrong_pin_raises`
`tests/security/test_key_pin.py::TestKeyPinCheckerEnforce::test_prod_wrong_pin_raises`

### CLAIM-38: Key pin mismatch is logged to audit

**Phase**: J-2
**Tests**:
`tests/security/test_key_pin.py::TestKeyPinCheckerCheck::test_wrong_pin_emits_audit_event`

### CLAIM-39: Correct key pin never raises

**Phase**: J-2
**Tests**:
`tests/security/test_key_pin.py::TestKeyPinCheckerEnforce::test_correct_pin_never_raises`

### CLAIM-40: SHA-256 key hash is deterministic and unique per key

**Phase**: J-2
**Tests**:
`tests/security/test_key_pin.py::TestComputeKeyHash::test_is_deterministic`
`tests/security/test_key_pin.py::TestComputeKeyHash::test_different_keys_produce_different_hashes`

---

## Rollback Protection

### CLAIM-41: Policy rollback is detected and raises RuntimeError

**Phase**: J-3
**Tests**:
`tests/security/test_rollback_guard.py::TestRollbackGuardBasic::test_rollback_raises_runtime_error`

### CLAIM-42: Policy rollback is logged as audit event

**Phase**: J-3
**Tests**:
`tests/security/test_rollback_guard.py::TestRollbackGuardBasic::test_rollback_logs_policy_rollback_event`

### CLAIM-43: Engine version below min_engine_version is rejected

**Phase**: J-3
**Tests**:
`tests/security/test_rollback_guard.py::TestRollbackGuardEngineVersion::test_min_engine_version_not_met_raises`

### CLAIM-44: Policy version checkpoint enables fast rollback detection

**Phase**: J-3
**Tests**:
`tests/security/test_rollback_guard.py::TestRollbackGuardBasic::test_checkpoint_written_after_accept`
`tests/security/test_rollback_guard.py::TestRollbackGuardBasic::test_backward_scan_finds_checkpoint`
`tests/security/test_rollback_guard.py::TestAuditLogPolicyMethods::test_checkpoint_takes_priority_over_accepted_scan`

---

## Approval System

### CLAIM-45: Duplicate approval requests are batched (not shown twice)

**Phase**: H
**Tests**:
`tests/approval/test_batch.py::TestAddAndBatching::test_second_add_increments_count`
`tests/approval/test_batch.py::TestAddAndBatching::test_on_batch_ready_called_for_first_only`

### CLAIM-46: Approval flood triggers token bucket exhaustion

**Phase**: H
**Tests**:
`tests/approval/test_rate_limit.py::TestAcquire::test_acquire_exceeds_limit_returns_false`
`tests/approval/test_rate_limit.py::TestThreadSafety::test_concurrent_acquire_respects_limit`

### CLAIM-47: Approval tokens are tamper-evident (signature verification)

**Phase**: E-5
**Tests**:
`tests/security/test_approver.py::TestTamperedToken::test_tampered_signature_returns_false`
`tests/security/test_approver.py::TestTamperedToken::test_tampered_rule_id_returns_false`
`tests/security/test_approver.py::TestTamperedToken::test_tampered_args_hash_returns_false`

### CLAIM-48: Approval token replay is prevented via nonce registry

**Phase**: E-5
**Tests**:
`tests/security/test_e5_approval.py::TestReplayPrevention::test_approve_same_token_twice_second_returns_false`
`tests/security/test_e5_approval.py::TestReplayPrevention::test_replay_across_multiple_attempts_all_fail`
`tests/security/test_e5_approval.py::TestNonceRegistry::test_duplicate_nonce_returns_false`

### CLAIM-49: Expired approval tokens are rejected

**Phase**: E-5
**Tests**:
`tests/security/test_approver.py::TestExpiredToken::test_expired_token_approve_returns_false`
`tests/security/test_e5_approval.py::TestExpiredToken::test_expired_token_approve_returns_false`

### CLAIM-50: Approval scope mismatch is rejected

**Phase**: E-5
**Tests**:
`tests/security/test_e5_approval.py::TestScopeMismatch::test_wrong_scope_approve_returns_false`
`tests/security/test_e5_approval.py::TestScopeMismatch::test_tampered_scope_field_approve_returns_false`

---

## Attestation

### CLAIM-51: Environment fingerprint baseline is captured on startup

**Phase**: G-3
**Tests**:
`tests/runner/test_attestation.py::TestAttestationCheckerBaseline::test_check_returns_true_for_unchanged_env`
`tests/runner/test_attestation.py::TestAttestationCheckerBaseline::test_baseline_is_accessible`

### CLAIM-52: Changed username triggers attestation anomaly

**Phase**: G-3
**Tests**:
`tests/runner/test_attestation.py::TestAttestationCheckerAnomalyDetection::test_username_change_returns_false`
`tests/runner/test_attestation.py::TestAttestationCheckerAnomalyDetection::test_anomaly_writes_to_audit_log`

### CLAIM-53: Sandbox probe file permission failure returns blocked

**Phase**: I-2
**Tests**:
`tests/runner/test_attestation_v2.py::TestProbeRead::test_permission_error_means_blocked`
`tests/runner/test_attestation_v2.py::TestProbeRead::test_oserror_access_denied_means_blocked`

### CLAIM-54: Sandbox probe network failure returns blocked

**Phase**: I-2
**Tests**:
`tests/runner/test_attestation_v2.py::TestProbeNet::test_connection_refused_means_blocked`
`tests/runner/test_attestation_v2.py::TestProbeNet::test_oserror_means_blocked`

---

## Audit Log Integrity

### CLAIM-55: Audit log hash chain detects any modification

**Phase**: C
**Tests**:
`tests/security/test_audit_log.py::TestAuditLogTamperDetection::test_corrupt_single_line_returns_false`
`tests/security/test_audit_log.py::TestAuditLogTamperDetection::test_corrupt_hash_field_returns_false`
`tests/security/test_audit_log.py::TestAuditLogTamperDetection::test_deleted_line_returns_false`

### CLAIM-56: Audit log hash chain is valid across multiple entries

**Phase**: C
**Tests**:
`tests/security/test_audit_log.py::TestAuditLogChainIntegrity::test_five_entries_verify_chain_returns_true`
`tests/security/test_audit_log.py::TestAuditLogChainIntegrity::test_verify_chain_after_reopen`
`tests/security/test_audit_log.py::TestAuditLogChainIntegrity::test_appended_entries_chain_is_valid`

---

## Security Levels

### CLAIM-57: DEV level allows HMAC fallback (no Ed25519 required)

**Phase**: J-1
**Tests**:
`tests/security/test_key_pin.py::TestKeyPinCheckerEnforce::test_dev_wrong_pin_no_raise`

### CLAIM-58: CI level requires Ed25519 verification

**Phase**: J-1
**Tests**:
`tests/security/test_key_pin.py::TestKeyPinCheckerEnforce::test_ci_wrong_pin_raises`
`tests/security/test_security_level.py::TestDetectSecurityLevel::test_github_actions_detected_as_ci`

### CLAIM-59: GitHub Actions environment is auto-detected as CI

**Phase**: J-1
**Tests**:
`tests/security/test_security_level.py::TestDetectSecurityLevel::test_github_actions_detected_as_ci`
`tests/security/test_security_level.py::TestDetectSecurityLevel::test_generic_ci_var_detected`

---

## SAFE_MODE

### CLAIM-60: Risk score accumulation from DENY decisions triggers SAFE_MODE

**Phase**: Risk scoring
**Tests**:
`tests/security/test_risk_score.py::TestRiskScoreAccumulator::test_safe_mode_triggers_after_deny_threshold`
`tests/security/test_risk_score.py::TestRiskAwareHook::test_safe_mode_halts_before_llm_call`

### CLAIM-61: Once in SAFE_MODE, all subsequent tool calls are blocked

**Phase**: Risk scoring
**Tests**:
`tests/security/test_risk_score.py::TestRiskAwareHook::test_safe_mode_halts_allow_operation`
`tests/test_shield_safe_mode.py::TestSafeModeEnabled::test_blocks_tool_call`
`tests/test_shield_safe_mode.py::TestSafeModeEnabled::test_blocks_empty_tool_name`

### CLAIM-62: SAFE_MODE blocks retry attempts

**Phase**: Risk scoring
**Tests**:
`tests/test_shield_safe_mode.py::TestSafeModeEnabled::test_blocks_retry`

---

## Verification

```bash
# Verify all claims
pytest tests/ -v --tb=short 2>&1 | grep -E "PASSED|FAILED|ERROR"

# Count: 62 claims verified by 1049+ tests
```

All 62 claims above are backed by real pytest test node IDs verified by running:
```bash
pytest tests/ --collect-only -q 2>&1 | grep "::"
```
