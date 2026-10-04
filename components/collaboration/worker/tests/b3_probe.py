"""B3 offline evidence. Fixed saved binary; never auth/login/inference."""
import hashlib
import json
from pathlib import Path
import tempfile
import sys

from support import REPO

BINARY = REPO / ".local/b2-probe-vg70xwcm/run/human/claude-runtime/claude"
IDENTITY = "15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07"


def main():
    area = Path(tempfile.mkdtemp(prefix="b3-schema-", dir=REPO / ".local"))
    data = BINARY.read_bytes()
    assert hashlib.sha256(data).hexdigest() == IDENTITY
    if len(sys.argv) > 1:
        if sys.argv[1] == "--range":
            offset, length = map(int, sys.argv[2:])
            print(data[offset:offset+length].decode("utf-8", "replace"))
            return
        for argument in sys.argv[1:]:
            term = argument.encode()
            start = 188000000
            for _ in range(12):
                offset = data.find(term, start)
                if offset < 0:
                    break
                print(offset, data[max(0, offset-120):offset+1300].decode("utf-8", "replace"))
                start = offset + len(term)
        return
    # Offsets are specific to the hash above. These are static source excerpts,
    # not executed schema code or evidence of a provider response.
    ranges = {"result_schema": (191810280, 1800), "model_usage_schema": (191719974, 1650),
        "subagents_and_accounting": (191802400, 6100), "fast_mode_enums": (191899634, 600),
        "structured_output_tool": (193797459, 1400), "tool_end_turn": (204256800, 650),
        "max_turn_guard": (204262580, 700), "result_producer": (211593700, 6300),
        "haiku_catalog": (191097550, 700)}
    snippets = {name: {"offset": offset, "length": length,
        "text": data[offset:offset+length].decode("utf-8", "strict")}
        for name, (offset, length) in ranges.items()}
    result = {"binary_sha256": IDENTITY, "static_only": True, "snippets": snippets}
    (area / "schema.json").write_text(json.dumps(result, ensure_ascii=True, indent=2))
    print(area)
    print(json.dumps({"binary_sha256": IDENTITY, "static_only": True,
                      "sections": sorted(snippets)}))


if __name__ == "__main__":
    main()
