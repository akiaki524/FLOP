"""Offline output declassification; never interprets text as capabilities."""
import base64

from .model import canonical, decode, review
from .runtime_interface import MAX_RESULT_BYTES, frozen_task, task_digest


class OutputRejected(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("runtime output withheld")


def protected_variants(protected):
    # Secondary defense only. Does not prove absence of arbitrary encodings,
    # partial secrets or covert channels. Protected values never enter model input.
    variants = []
    for value in protected:
        value = value.encode("utf-8")
        variants.extend((value, base64.b64encode(value), base64.urlsafe_b64encode(value),
                         value.hex().encode(), value.hex().upper().encode()))
    return tuple(variants)


def validate_unicode(value):
    """JSON permits escaped lone surrogates; final results must be Unicode scalars."""
    if type(value) is str:
        value.encode("utf-8", errors="strict")
    elif type(value) is dict:
        for key, item in value.items():
            validate_unicode(key)
            validate_unicode(item)
    elif type(value) is list:
        for item in value:
            validate_unicode(item)


def validated_result(raw, request, protected=(), *, real=False, offline=True, api=False):
    if type(raw) is not bytes or len(raw) > MAX_RESULT_BYTES:
        raise OutputRejected("oversized_output")
    variants = protected_variants(protected)
    def scan(data):
        if any(value in data for value in variants):
            raise OutputRejected("secret_withheld")
    scan(raw)
    try:
        report = decode(raw.decode("utf-8", errors="strict"))
        validate_unicode(report)
        # Decode JSON escapes before scanning; include keys and nested values.
        scan(str(report).encode("utf-8"))
        review(report, frozen_task(request))
        if api and real:
            raise ValueError()
        provider = ("anthropic-messages-offline-v1" if offline else "anthropic-messages-real-v1") if api else (
            "claude-cli-real-v1" if real else "claude-cli-offline-v1")
        if report["provider"] != provider:
            raise ValueError()
        result = {"version": 1, "status": "succeeded", "offline": offline,
                  "task_sha256": task_digest(request), "report": report}
        encoded = canonical(result)
    except OutputRejected:
        raise
    except Exception:
        raise OutputRejected("malformed_output") from None
    if len(encoded) > MAX_RESULT_BYTES:
        raise OutputRejected("oversized_output")
    scan(encoded)
    return result
