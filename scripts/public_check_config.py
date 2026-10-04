"""Explicit local personalization; no private defaults are distributed."""
import argparse
import json
from pathlib import Path

class ConfigError(ValueError):
    pass

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("duplicate configuration key")
        result[key] = value
    return result

def load_config(path):
    if path is None:
        print("Personalized identifier checks: DISABLED (no --private-config); generic checks only.")
        return {"usernames": [], "emails": [], "owner_names": []}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("private configuration unreadable or invalid") from None
    if not isinstance(data, dict) or set(data) - {"usernames", "emails", "owner_names"}:
        raise ConfigError("invalid private configuration schema")
    for key in ("usernames", "emails", "owner_names"):
        values = data.get(key, [] if key == "owner_names" else None)
        if not isinstance(values, list) or (key != "owner_names" and not values):
            raise ConfigError("missing or invalid identifier category")
        if any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in values):
            raise ConfigError("invalid identifier entry")
        data[key] = values
    print("Personalized identifier checks: ENABLED (local values withheld).")
    return data

def arguments(argv=None, revisions=False):
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-config", metavar="PATH")
    if revisions:
        parser.add_argument("revisions", nargs="+")
    args = parser.parse_args(argv)
    if revisions and len(args.revisions) not in (1, 2):
        parser.error("expected HEAD or BASE HEAD")
    try:
        config = load_config(args.private_config)
    except ConfigError as exc:
        parser.error(str(exc))
    return args, config

def identifier_literals(config, binary=False):
    values = {}
    for key, label in (("usernames", "private username"), ("emails", "private email")):
        for i, value in enumerate(config[key]):
            values[f"{label} #{i + 1}"] = value.encode() if binary else value
    return values
