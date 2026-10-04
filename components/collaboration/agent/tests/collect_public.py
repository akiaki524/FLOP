"""Explicit research-only collection. Never imported by the offline Agent."""

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import urllib.request

URL = "https://technocore.chat/r/tclk-offers?format=json&limit=200"
MAX_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse(raw):
    def invalid(_):
        raise ValueError("non-finite JSON")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                      parse_float=Decimal, parse_constant=invalid)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def collect(output):
    if output.exists():
        raise ValueError("output already exists; refusing a second collection or overwrite")
    start = timestamp()
    # No proxy credentials, cookie jar, auth handler, redirect, retry or content-driven URL.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(URL, method="GET", headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "User-Agent": "Collaboration-Agent-Offline-Evaluation/0.1"})
    with opener.open(request, timeout=30) as response:
        if response.status != 200 or response.geturl() != URL:
            raise ValueError("unexpected response")
        raw = response.read(MAX_BYTES + 1)
        headers = {k.lower(): v for k, v in response.headers.items()
                   if k.lower() in {"date", "content-type", "content-length", "x-room-generation", "etag", "last-modified"}}
    if len(raw) > MAX_BYTES:
        raise ValueError("snapshot byte cap exceeded")
    output.mkdir(parents=True)
    (output / "raw.json").write_bytes(raw)
    metadata = {"schema": 1, "collection_started_at": start, "collection_finished_at": timestamp(),
                "url": URL, "method": "GET", "source_room": "tclk-offers", "headers": headers,
                "raw_bytes": len(raw), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "protocol_validity": "SOURCE_UNVERIFIED", "max_records": 200, "max_bytes": MAX_BYTES,
                "holdout_rule": "record indices i with i % 5 == 4; do not inspect before improvement freeze"}
    # Always retain successfully received bytes, even if parsing fails.
    try:
        value = parse(raw)
        records = value["messages"]
        if not isinstance(records, list) or len(records) > 200:
            raise ValueError("record cap/schema")
        metadata.update(record_count=len(records), generation=value.get("generation", headers.get("x-room-generation")),
                        first_seq=value.get("first_seq"), last_seq=value.get("last_seq"),
                        returned_first_seq=records[0].get("seq") if records else None,
                        returned_last_seq=records[-1].get("seq") if records else None,
                        source_metadata={k: v for k, v in value.items() if k != "messages"},
                        development_indices=[i for i in range(len(records)) if i % 5 != 4],
                        holdout_indices=[i for i in range(len(records)) if i % 5 == 4])
    finally:
        (output / "provenance.json").write_text(json.dumps(metadata, indent=2, default=str) + "\n")
    print(json.dumps(metadata, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One bounded, fixed-URL public read; no Agent runtime integration")
    parser.add_argument("--output", type=Path, required=True)
    collect(parser.parse_args().output)
