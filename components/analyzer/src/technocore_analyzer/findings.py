"""Finding construction. Categories, severity and verification are kept separate.

Severity is impact (INFO..CRITICAL, or UNKNOWN when impact is not established); it is
never raised by frequency, odd style or missing signatures alone. Evidence is described
facet by facet instead of one "strength" number.
"""

from __future__ import annotations

from .util import SCHEMA_VERSION, digest

CATEGORIES = ("PROTOCOL_INVALID", "SECURITY_FINDING_CANDIDATE", "PROTOCOL_FRAUD_CANDIDATE",
              "ABUSE_CANDIDATE", "MARKET_AGENT_ANOMALY", "QUALITY_REPUTATION_CONCERN",
              "EVIDENCE_CONFLICT", "UNVERIFIED_CLAIM")
SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")
VERIFICATION = ("UNCHECKED", "SUPPORTED", "CONTRADICTED", "INCONCLUSIVE")


def finding_id(category: str, rule_id: str, subject: str) -> str:
    return "F-" + digest([category, rule_id, subject])[:20]


def make(*, category, rule, subject, claim, expected, observed, evidence_refs, severity,
         detection_method, coverage, alternatives, evidence_facets, info_class="Derived",
         verification="UNCHECKED", processing="SUCCEEDED", details=None):
    if category not in CATEGORIES or severity not in SEVERITIES or verification not in VERIFICATION:
        raise ValueError("INVALID_FINDING_ENUM")
    refs = sorted(set(evidence_refs))
    return {
        "schema": SCHEMA_VERSION + "#finding",
        "finding_id": finding_id(category, rule["id"], subject),
        "category": category,
        "subject": subject,
        "claim": claim,
        "expected_behavior": expected,
        "observed_behavior": observed,
        "evidence_refs": refs[:200],
        "evidence_refs_total": len(refs),
        "rule": rule,
        "detection_method": detection_method,
        "information_class": info_class,
        "severity": severity,
        "severity_meaning": "impact, not confidence",
        "evidence": evidence_facets,
        "coverage": coverage,
        "alternative_explanations": alternatives,
        "processing_status": processing,
        "verification_status": verification,
        "lifecycle": "DETECTED",
        "details": details or {},
    }


def facets(*, direct, signature, coverage, corroboration="NONE_ESTABLISHED", reproducible=True,
           unverified=()):
    return {"direct_observation": direct,
            "independent_corroboration": corroboration,
            "reproducible_from_stored_evidence": reproducible,
            "signature_verification": signature,
            "coverage": coverage,
            "unverified_points": list(unverified)}
