"""First local pilot policy. Human assertions are not provider verification."""

# Exact previously reviewed request/payload identities; absent pins fail closed.
REQUEST_SHA256 = "6b70c47a0ba818b80471260efa9eee6931b59baad73be803549d87277775b35e"
PAYLOAD_SHA256 = "25f497ec65a1a77371aedf596b587ef62c27f7b0c8a749734f132e025a2b3b8c"
WIRE_BODY_SHA256 = "86f954f206096d56cdbab8f9d9b9c471ef91c7abad1ba28b1d9ec3a69c1febd7"


def human_conditions():
    return {
        "execution": "local-human-supervised-one-shot-only",
        "material": "one-reviewed-public-FLOP-document-and-fixed-prompt",
        "credential": "Personal-API-key-scoped-to-single-non-Default-Workspace",
        "credential_expiry_hours": 3,
        "workspace_spend_limit_usd": 1,
        "console_limit_confirmed_required": True,
        "after_execution": "Human-Disable-or-Delete-key-including-failure-or-unknown",
        "output": "Human-Preview-only-untrusted-data-no-external-action",
        "network": "provider-only-egress-not-guaranteed-general-TCP-risk-accepted-for-this-local-attempt-only",
        "excluded": ["VPS", "24H", "Autopilot"],
        "consumed_failure": "no-automatic-retry-no-refund",
    }


def required_attestation(policy_sha):
    """Required Human statement, never an automatically observed fact."""
    return {"version": 1, "contract_sha256": policy_sha,
            "conditions": human_conditions(), "confirmed_by": "Human",
            "console_workspace_scope_expiry_and_usd1_limit_checked": True,
            "key_disable_or_delete_after_attempt_committed": True,
            "final_release_diff_independent_review_pass": True,
            "local_environment_and_stop_recovery_ready": True,
            "authorize_this_exact_one_shot_now": True}
