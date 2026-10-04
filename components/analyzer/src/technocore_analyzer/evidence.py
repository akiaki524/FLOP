"""Read-only Evidence Adapters over the Observer's stored formats.

Supported inputs (see docs/evidence-adapters.md):

* ``full-capture-archive`` — the ``technocore_full_capture`` Archive ``format=1``:
  either a spool-archive directory (``segment-<20 digits>/`` sub-archives plus an
  optional ``manifest.sqlite``) or a single Archive directory (``capture.json`` plus
  ``shard-<12 digits>.jsonl``). Published shards are immutable files; only shards named
  by the checkpoint are read. ``manifest.sqlite`` is opened only when the source opts in
  (``read_manifest``), with ``mode=ro``, one short read transaction and a short timeout.
* ``observer-state-sqlite`` — a ``technocore_observer`` ``state.sqlite`` *snapshot*
  (e.g. produced by the SQLite Online Backup API). A live WAL database is refused unless
  the source explicitly sets ``allow_live_sqlite``.

Nothing here writes, renames, locks for writing, repairs or deletes source files.
Locators are relative to the configured source id, never absolute host paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat

from .util import parse_rfc3339_ms

ARCHIVE_FORMAT = "full-capture-archive/1"
OBSERVER_FORMAT = "observer-state-sqlite/2"
HEADER_LIMIT = 4096
MAX_LINE = 256 * 1024
MAX_SHARD = 64 * 1024 * 1024
MAX_INT64 = 2**63 - 1
SHARD_NAME = re.compile(r"shard-([0-9]{12})\.jsonl\Z")
SEGMENT_NAME = re.compile(r"segment-[0-9]{20}\Z")
HEADER_KEYS = {"format", "room", "index", "generation", "anchor_seq", "first_seq", "last_seq",
               "count", "payload_bytes", "payload_sha256", "previous_sha256", "last_sha256",
               "captured_at"}
KNOWN_MESSAGE_FIELDS = {"seq", "ts", "from", "text", "nonce", "sig"}
MANIFEST_EVIDENCE_KINDS = {"GAP", "BOOTSTRAP_UNOBSERVED_PREFIX", "CONFLICT", "LATE_OBSERVATION",
                           "GENERATION_BOUNDARY", "BOUNDARY_OBSERVATION"}
ZERO_HASH = "0" * 64


@dataclass
class EvidenceRecord:
    source_id: str
    source_kind: str
    provenance: str
    format_version: str
    room: str
    stream: str
    seq: int | None
    epoch: int | None
    generation: int | None
    message: dict
    record_sha256: str
    hash_scope: str
    ref: str
    locator: dict
    captured_at: float | None
    quality: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source_id}|{self.stream}|{self.seq}"

    @property
    def ts_raw(self):
        return self.message.get("ts")

    @property
    def ts_ms(self):
        return parse_rfc3339_ms(self.message.get("ts"))


@dataclass
class CoverageItem:
    """GAP / conflict / boundary / late-observation evidence from the source itself."""
    source_id: str
    room: str
    stream: str
    kind: str
    start_seq: int | None
    end_seq: int | None
    detail: dict
    ref: str

    @property
    def key(self) -> str:
        return f"{self.source_id}|{self.ref}"


@dataclass
class UnitResult:
    """One read unit (a shard, a manifest, a snapshot) and what happened to it."""
    unit: str
    sha256: str | None
    status: str  # READ / SKIPPED_UNCHANGED / QUARANTINED / DEFERRED
    detail: str | None = None
    records: list = field(default_factory=list)
    # (stream, seq) -> sha256 of the verified Archive line, for EVERY verified shard (also ones
    # skipped as unchanged), so manifest MESSAGE receipts are cross-checked on incremental runs.
    line_hashes: dict = field(default_factory=dict)


@dataclass
class SourceRead:
    source_id: str
    kind: str
    room: str
    status: str  # READ / PARTIAL / UNAVAILABLE / DEFERRED
    reason: str | None
    units: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    streams: list = field(default_factory=list)
    manifest: dict | None = None
    manifest_quarantined: bool = False
    # Streams (segments) whose every published shard verified / that could not be fully verified.
    verified_streams: set = field(default_factory=set)
    unverified_streams: set = field(default_factory=set)
    latest_captured_at: float | None = None


class SourceUnavailable(Exception):
    pass


class SnapshotIdentityError(ValueError):
    """The snapshot is not the configured room's Observer state."""


class ManifestIntegrityError(ValueError):
    """manifest.sqlite does not satisfy the Observer's receipt-chain contract."""


class ManifestUnavailableError(Exception):
    """manifest.sqlite is temporarily unsafe to open (live WAL/journal state)."""


def _regular_read(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("NOT_REGULAR_FILE")
        if info.st_size > limit:
            raise ValueError("FILE_OVER_LIMIT")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("FILE_OVER_LIMIT")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sqlite_header(path: Path):
    """(application_id, user_version, wal_mode) from the file header, without SQLite."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("NOT_REGULAR_FILE")
        raw = os.read(fd, 100)
    finally:
        os.close(fd)
    if len(raw) < 100 or raw[:16] != b"SQLite format 3\x00":
        raise ValueError("NOT_SQLITE")
    return int.from_bytes(raw[68:72], "big"), int.from_bytes(raw[60:64], "big"), raw[18] == 2 or raw[19] == 2


def _stat_key(path: Path):
    info = os.stat(path, follow_symlinks=False)
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _reject_constant(name):
    raise ValueError("NON_JSON_CONSTANT")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("NON_FINITE_NUMBER")  # e.g. 1e999: canonical JSON could never persist it
    return value


def _finite(value):
    """A JSON-safe number (not bool, not NaN/Infinity)."""
    return type(value) in (int, float) and math.isfinite(value)


def _loads(raw: bytes):
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)


CHECKPOINT_KEYS = {"format", "room", "generation", "anchor_seq", "cursor", "cursor_sha256", "shards", "tip_sha256",
                   "message_count", "archive_bytes", "payload_bytes", "created_at", "updated_at",
                   "last_success_at", "status", "error", "failures"}


def _check_checkpoint(cp, room):
    """Schema/type checks of Observer `Archive._open` for capture.json (format=1)."""
    if not isinstance(cp, dict) or set(cp) != CHECKPOINT_KEYS or type(cp["format"]) is not int \
            or cp["format"] != 1 or cp["room"] != room \
            or cp["status"] not in {"WAITING", "RUNNING", "RETRY", "BLOCKED"}:
        raise ValueError("BAD_CHECKPOINT")
    for key in ("shards", "message_count", "archive_bytes", "payload_bytes", "failures"):
        if type(cp[key]) is not int or cp[key] < 0:
            raise ValueError("BAD_CHECKPOINT")
    positions = ("generation", "anchor_seq", "cursor")
    if cp["shards"]:
        if any(type(cp[k]) is not int or not 0 <= cp[k] <= 2**63 - 1 for k in positions) \
                or cp["message_count"] != cp["cursor"] - cp["anchor_seq"]:
            raise ValueError("BAD_CHECKPOINT")
    elif any(cp[k] is not None for k in positions):
        raise ValueError("BAD_CHECKPOINT")


def message_quality(message: dict) -> list:
    flags = []
    for name in ("text", "from"):
        if name not in message:
            flags.append("MISSING_" + name.upper())
        elif not isinstance(message[name], str):
            flags.append("NON_STRING_" + name.upper())
    if "ts" not in message:
        flags.append("MISSING_TS")
    elif parse_rfc3339_ms(message["ts"]) is None:
        flags.append("UNPARSEABLE_TS")
    if "sig" in message and "nonce" not in message:
        flags.append("SIG_WITHOUT_NONCE")
    if set(message) - KNOWN_MESSAGE_FIELDS:
        flags.append("UNKNOWN_FIELDS_PRESERVED")
    return flags


class ArchiveAdapter:
    kind = "full-capture-archive"

    def __init__(self, source: dict):
        self.source = source
        self.source_id = source["id"]
        self.room = source["room"]
        self.path = Path(source["path"])
        self.provenance = source.get("provenance", "captured")
        self.read_manifest = bool(source.get("read_manifest", False))

    def read(self, known_units: dict, manifest_progress: dict | None) -> SourceRead:
        result = SourceRead(self.source_id, self.kind, self.room, "READ", None)
        if not self.path.is_dir() or self.path.is_symlink():
            # An unmounted medium is not deletion, loss or fraud: defer and say so.
            result.status, result.reason = "UNAVAILABLE", "SOURCE_PATH_NOT_AVAILABLE"
            return result
        segments = sorted(p.name for p in self.path.iterdir()
                          if SEGMENT_NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
        if segments:
            archives = [(name, self.path / name) for name in segments]
        elif (self.path / "capture.json").exists():
            archives = [("archive", self.path)]
        else:
            archives = []
            result.status, result.reason = "READ", "NO_PUBLISHED_ARCHIVE_YET"
        segment_meta, manifest_messages = {}, {}
        if self.read_manifest and (self.path / "manifest.sqlite").exists():
            try:
                manifest = self._manifest(manifest_progress)
                result.manifest = manifest["state"]
                segment_meta = manifest["segments"]
                manifest_messages = manifest["messages"]
                result.coverage.extend(manifest["coverage"])
                result.units.append(UnitResult("manifest.sqlite", manifest["state"]["receipt_hash"], "READ"))
            except ManifestIntegrityError as exc:
                # A broken chain is not Evidence: import none of its GAP/CONFLICT/LATE items.
                result.manifest_quarantined = True
                result.units.append(UnitResult("manifest.sqlite", None, "QUARANTINED", str(exc)))
            except ManifestUnavailableError as exc:
                result.units.append(UnitResult("manifest.sqlite", None, "DEFERRED", str(exc)))
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                # Parse/schema/type failures mean the manifest no longer verifies. Cached
                # manifest-derived GAP/epoch/progress must be withdrawn by ingest().
                result.manifest_quarantined = True
                result.units.append(UnitResult("manifest.sqlite", None, "QUARANTINED",
                                               "MANIFEST_STRUCTURE_INVALID:" + type(exc).__name__))
            except sqlite3.OperationalError as exc:
                result.units.append(UnitResult("manifest.sqlite", None, "DEFERRED",
                                               "MANIFEST_READ_FAILED:" + type(exc).__name__))
            except sqlite3.DatabaseError as exc:
                result.manifest_quarantined = True
                result.units.append(UnitResult("manifest.sqlite", None, "QUARANTINED",
                                               "MANIFEST_SQLITE_INVALID:" + type(exc).__name__))
            except (sqlite3.Error, OSError) as exc:
                result.units.append(UnitResult("manifest.sqlite", None, "DEFERRED",
                                               "MANIFEST_READ_FAILED:" + type(exc).__name__))
        elif self.read_manifest:
            result.units.append(UnitResult("manifest.sqlite", None, "DEFERRED", "MANIFEST_ABSENT"))
        for stream, directory in archives:
            meta = segment_meta.get(stream, {})
            self._read_archive(stream, directory, meta, known_units, result)
        if result.manifest is not None:
            self._cross_check(result, segment_meta, manifest_messages)
        statuses = {u.status for u in result.units}
        if "QUARANTINED" in statuses or "DEFERRED" in statuses:
            result.status = "PARTIAL"
            result.reason = result.reason or "SOME_UNITS_NOT_READ"
        return result

    def _read_archive(self, stream, directory, meta, known_units, result):
        try:
            checkpoint = _loads(_regular_read(directory / "capture.json", HEADER_LIMIT))
            _check_checkpoint(checkpoint, self.room)
            shards = checkpoint["shards"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result.units.append(UnitResult(f"{stream}/capture.json", None, "DEFERRED",
                                           "CHECKPOINT_UNREADABLE:" + str(exc)[:60]))
            result.unverified_streams.add(stream)
            return
        # The checkpoint's count is only trusted against the shard files actually present (as the
        # Observer's Archive._open does): indices 1..k with k in {shards, shards+1}. Otherwise a
        # corrupted count could drive one filesystem lookup per declared index.
        try:
            present = sorted(int(m[1]) for m in (SHARD_NAME.fullmatch(p.name) for p in directory.iterdir()) if m)
        except OSError as exc:
            result.units.append(UnitResult(f"{stream}/capture.json", None, "DEFERRED",
                                           "ARCHIVE_LIST_FAILED:" + type(exc).__name__))
            result.unverified_streams.add(stream)
            return
        if len(present) not in (shards, shards + 1) or present != list(range(1, len(present) + 1)):
            result.units.append(UnitResult(f"{stream}/capture.json", None, "QUARANTINED", "ARCHIVE_SET_MISMATCH"))
            result.unverified_streams.add(stream)
            return
        if len(present) == shards + 1:
            # A published successor awaiting its checkpoint: read it on a later run.
            result.coverage.append(CoverageItem(
                self.source_id, self.room, stream, "SHARD_PENDING_CHECKPOINT", None, None,
                {"index": shards + 1}, f"{stream}/shard-{shards + 1:012d}.jsonl"))
        result.streams.append({"stream": stream, "shards": shards,
                               "generation": checkpoint.get("generation"),
                               "cursor": checkpoint.get("cursor"),
                               "tip_sha256": checkpoint.get("tip_sha256"),
                               "epoch": meta.get("epoch"),
                               "anchor_seq": checkpoint.get("anchor_seq"),
                               "status": checkpoint.get("status"),
                               "last_success_at": checkpoint.get("last_success_at")})
        first_unit = len(result.units)  # this stream's units start here
        previous = ZERO_HASH
        chain_ok = True
        expected_first = None if checkpoint.get("anchor_seq") is None else checkpoint["anchor_seq"] + 1
        count_total, last_seq, payload_total, bytes_total = 0, None, 0, 0
        for index in range(1, shards + 1):
            unit = f"{stream}/shard-{index:012d}.jsonl"
            path = directory / f"shard-{index:012d}.jsonl"
            try:
                raw = _regular_read(path, MAX_SHARD + HEADER_LIMIT)
            except (OSError, ValueError) as exc:
                result.units.append(UnitResult(unit, None, "DEFERRED", "SHARD_UNREADABLE:" + type(exc).__name__))
                chain_ok = False
                continue
            shard_sha = hashlib.sha256(raw).hexdigest()
            try:
                header, lines = self._verify_shard(raw, index, previous)
            except (ValueError, TypeError, KeyError) as exc:
                # One bad shard is quarantined; every other shard still analyses.
                result.units.append(UnitResult(unit, shard_sha, "QUARANTINED", str(exc)))
                previous, chain_ok = shard_sha, False
                continue
            previous = shard_sha
            # Same checks as the Observer's Archive._verify: constant generation/anchor and
            # contiguous seq across shards. A failure here quarantines, never repairs.
            if (not chain_ok or header["first_seq"] != expected_first
                    or header["generation"] != checkpoint.get("generation")
                    or header["anchor_seq"] != checkpoint.get("anchor_seq")):
                result.units.append(UnitResult(unit, shard_sha, "QUARANTINED", "ARCHIVE_CHAIN_FAILURE"))
                chain_ok = False
                continue
            expected_first, last_seq = header["last_seq"] + 1, header["last_seq"]
            count_total += header["count"]
            payload_total += header["payload_bytes"]
            bytes_total += len(raw)
            if result.latest_captured_at is None or header["captured_at"] > result.latest_captured_at:
                result.latest_captured_at = header["captured_at"]
            hashes = {(stream, header["first_seq"] + i): hashlib.sha256(line).hexdigest()
                      for i, line in enumerate(lines)}
            if known_units.get(unit) == shard_sha:
                result.units.append(UnitResult(unit, shard_sha, "SKIPPED_UNCHANGED", line_hashes=hashes))
                continue
            records = []
            for number, line in enumerate(lines, start=2):
                message = _loads(line)
                records.append(EvidenceRecord(
                    source_id=self.source_id, source_kind=self.kind, provenance=self.provenance,
                    format_version=ARCHIVE_FORMAT, room=self.room, stream=stream,
                    seq=message["seq"], epoch=meta.get("epoch"), generation=header["generation"],
                    message=message, record_sha256=hashlib.sha256(line).hexdigest(),
                    hash_scope="ARCHIVE_LINE_BYTES(canonical JSON + LF; not HTTP raw)",
                    ref=f"{self.source_id}:{unit}#L{number}",
                    locator={"source": self.source_id, "unit": unit, "line": number,
                             "shard_sha256": shard_sha, "archive_id": (result.manifest or {}).get("archive_id")},
                    captured_at=header["captured_at"],
                    quality=message_quality(message) + ["NO_HTTP_RAW_IN_ARCHIVE"]))
            result.units.append(UnitResult(unit, shard_sha, "READ", None, records, hashes))
        if chain_ok and shards and (previous != checkpoint.get("tip_sha256") or last_seq != checkpoint.get("cursor")
                                    or count_total != checkpoint.get("message_count")
                                    or payload_total != checkpoint.get("payload_bytes")
                                    or bytes_total != checkpoint.get("archive_bytes")):
            # The stream failed its final checkpoint verification: none of its provisional records
            # may reach analysis (torn or tampered archive). Units stay as digest-only facts.
            result.units[first_unit:] = [
                UnitResult(u.unit, u.sha256, "QUARANTINED", "ARCHIVE_CHECKPOINT_MISMATCH")
                if u.status in ("READ", "SKIPPED_UNCHANGED") else u for u in result.units[first_unit:]]
            result.units.append(UnitResult(f"{stream}/capture.json", None, "QUARANTINED",
                                           "TIP_CHECKPOINT_MISMATCH"))
            chain_ok = False
        (result.verified_streams if chain_ok else result.unverified_streams).add(stream)

    def _verify_shard(self, raw: bytes, index: int, previous: str):
        newline = raw.find(b"\n")
        if newline < 0 or newline + 1 > HEADER_LIMIT:
            raise ValueError("BAD_ARCHIVE_HEADER")
        header_raw = raw[:newline + 1]
        header = _loads(header_raw)
        if (set(header) != HEADER_KEYS or type(header["format"]) is not int or header["format"] != 1
                or header["room"] != self.room):
            raise ValueError("BAD_ARCHIVE_HEADER")
        # Positional fields feed arithmetic/comparisons below; a type-invalid header is an
        # integrity failure of this shard, not an adapter crash.
        if (any(type(header[k]) is not int or not 0 <= header[k] <= MAX_INT64
                for k in ("index", "generation", "anchor_seq", "first_seq", "last_seq", "count", "payload_bytes"))
                or type(header["captured_at"]) not in (int, float)):
            raise ValueError("BAD_ARCHIVE_HEADER")
        payload = raw[newline + 1:]
        if not payload.endswith(b"\n"):
            raise ValueError("ARCHIVE_PARTIAL_RECORD")
        lines = [line + b"\n" for line in payload[:-1].split(b"\n")]
        if (header["index"] != index or header["previous_sha256"] != previous
                or header["payload_sha256"] != hashlib.sha256(payload).hexdigest()
                or header["payload_bytes"] != len(payload) or header["count"] != len(lines)
                or any(len(line) > MAX_LINE for line in lines)
                or header["last_sha256"] != hashlib.sha256(lines[-1]).hexdigest()):
            raise ValueError("ARCHIVE_INTEGRITY_FAILURE")
        for offset, line in enumerate(lines):
            try:
                seq = _loads(line).get("seq")
            except (ValueError, AttributeError):
                raise ValueError("ARCHIVE_RECORD_NOT_JSON_OBJECT") from None
            # `True == 1` and `1.0 == 1` in Python: only a real integer is an Observer seq (it is
            # also the record's cache identity, so a look-alike would evade conflict detection).
            if type(seq) is not int or seq != header["first_seq"] + offset:
                raise ValueError("ARCHIVE_SEQUENCE_MISMATCH")
        if header["last_seq"] != header["first_seq"] + len(lines) - 1:
            raise ValueError("ARCHIVE_SEQUENCE_MISMATCH")
        return header, lines

    def _manifest(self, manifest_progress=None) -> dict:
        """Read manifest.sqlite and verify it against the Observer's receipt contract
        (spool_archive.ArchiveWorker._record / verify): contiguous entry ids from 1,
        chain_i = sha256(chain_{i-1} + document_i) from 64 zeros, document.entry_id match,
        and state.through_entry / receipt_hash / messages / gaps / unconfirmed equal to the
        recomputed totals. Stored chain hashes are never trusted on their own. When a prior
        Analyzer run saved a receipt-chain position, that position is also an append-only
        history anchor: rollback or a different chain at the saved entry is quarantined."""
        path = self.path / "manifest.sqlite"
        application_id, version, wal = _sqlite_header(path)
        if application_id != 0x5443414D or version != 1:
            raise ManifestIntegrityError("MANIFEST_FORMAT")
        if wal:
            # A read-only open of a WAL database may create -shm next to the Evidence.
            raise ManifestUnavailableError("MANIFEST_WAL_MODE_NOT_READ")
        for suffix in ("-journal", "-wal"):
            if Path(str(path) + suffix).exists():
                raise ManifestUnavailableError("MANIFEST_WRITE_IN_PROGRESS")
        conn = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.25, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("BEGIN")  # one consistent read snapshot
            try:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ManifestIntegrityError("MANIFEST_INTEGRITY")
                required_schema = {
                    "state": {"singleton", "room", "through_entry", "receipt_hash", "messages", "gaps", "unconfirmed"},
                    "segments": {"name", "shards", "tip_sha256", "first_seq", "last_seq"},
                    "receipts": {"entry_id", "document", "chain_hash"},
                }
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not set(required_schema) <= tables:
                    raise ManifestIntegrityError("MANIFEST_SCHEMA")
                for table, required in required_schema.items():
                    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                    if not required <= columns:
                        raise ManifestIntegrityError("MANIFEST_SCHEMA")
                row = conn.execute("SELECT * FROM state WHERE singleton=1").fetchone()
                if row is None:
                    raise ManifestIntegrityError("MANIFEST_STATE_MISSING")
                state = dict(row)
                if state.get("room") != self.room:
                    # Another room's manifest must never contribute Coverage to this source.
                    raise ManifestIntegrityError("MANIFEST_ROOM_MISMATCH")
                segments = {row["name"]: dict(row) for row in conn.execute("SELECT * FROM segments")}
                for name, seg in segments.items():
                    # SQLite column affinity does not enforce these types: a segments row is checked
                    # before _cross_check does arithmetic/comparisons with it.
                    if (type(name) is not str or type(seg["tip_sha256"]) is not str
                            or any(type(seg[k]) is not int or seg[k] < 0 for k in ("shards", "first_seq", "last_seq"))):
                        raise ManifestIntegrityError("MANIFEST_SEGMENT_INVALID")
                coverage, messages = [], {}
                chain, through, n_messages, gaps, uncertain = ZERO_HASH, 0, 0, 0, 0
                saved_through = manifest_progress.get("through_entry") if manifest_progress else None
                saved_chain = manifest_progress.get("chain_hash") if manifest_progress else None
                for row in conn.execute("SELECT entry_id,document,chain_hash FROM receipts ORDER BY entry_id"):
                    if row["entry_id"] != through + 1:
                        raise ManifestIntegrityError("MANIFEST_STREAM_HOLE")
                    chain = hashlib.sha256((chain + row["document"]).encode("ascii")).hexdigest()
                    if chain != row["chain_hash"]:
                        raise ManifestIntegrityError("MANIFEST_RECEIPT_HASH")
                    if saved_through and row["entry_id"] == saved_through and chain != saved_chain:
                        raise ManifestIntegrityError("MANIFEST_HISTORY_FORK")
                    try:
                        # Same strict parse as Archive lines: NaN/Infinity/1e999 cannot be persisted
                        # as Coverage, so such a receipt is an integrity failure, not Evidence.
                        item = _loads(row["document"].encode("utf-8"))
                    except ValueError:
                        raise ManifestIntegrityError("MANIFEST_RECEIPT_INVALID") from None
                    if not isinstance(item, dict):
                        raise ManifestIntegrityError("MANIFEST_RECEIPT_INVALID")
                    if type(item.get("entry_id")) is not int or item["entry_id"] != row["entry_id"]:
                        raise ManifestIntegrityError("MANIFEST_RECEIPT_ID")
                    through = row["entry_id"]
                    kind = item.get("kind")
                    if kind == "MESSAGE":
                        n_messages += 1
                        messages[(item.get("segment"), item.get("seq"))] = item.get("record_sha256")
                        continue
                    gaps += kind in {"GAP", "BOOTSTRAP_UNOBSERVED_PREFIX"}
                    uncertain += kind in {"GENERATION_BOUNDARY", "BOUNDARY_OBSERVATION", "CONFLICT", "LATE_OBSERVATION"}
                    if kind in MANIFEST_EVIDENCE_KINDS:
                        if any(item.get(k) is not None and type(item.get(k)) is not int for k in ("seq", "end_seq")):
                            raise ManifestIntegrityError("MANIFEST_RECEIPT_INVALID")
                        coverage.append(CoverageItem(
                            self.source_id, self.room, f"epoch-{item.get('epoch')}", kind, item.get("seq"),
                            item.get("end_seq"), {"epoch": item.get("epoch"), "evidence": item.get("evidence"),
                                                  "batch": item.get("batch")},
                            f"manifest.sqlite#receipt={row['entry_id']}"))
                if (through, chain, n_messages, gaps, uncertain) != (
                        state["through_entry"], state["receipt_hash"], state["messages"], state["gaps"],
                        state["unconfirmed"]):
                    raise ManifestIntegrityError("MANIFEST_TOTAL_MISMATCH")
                if saved_through is not None and through < saved_through:
                    raise ManifestIntegrityError("MANIFEST_HISTORY_ROLLBACK")
            finally:
                conn.execute("COMMIT")
        finally:
            conn.close()
        state["_through_entry_read"], state["_chain_hash_read"] = through, chain
        return {"state": state, "segments": segments, "coverage": coverage, "messages": messages}

    def _cross_check(self, result, segments, messages):
        """Manifest MESSAGE receipts and segment tips must agree with the shards read."""
        problem = None
        for stream in result.streams:
            seg = segments.get(stream["stream"])
            if seg is None:
                continue
            if stream["shards"] == seg["shards"] and stream["tip_sha256"] != seg["tip_sha256"]:
                problem = "MANIFEST_SEGMENT_MISMATCH"
            elif stream["shards"] not in (seg["shards"], seg["shards"] + 1):
                problem = "MANIFEST_SEGMENT_MISMATCH"
        # Observer `verify()` walks every MESSAGE receipt against the segment's Archive lines, so
        # each receipt needs a verified line with the same (segment, seq) and hash. The reverse is
        # not required: the Archive may lead the manifest by one published shard (receipts follow).
        lines = {}
        for unit in result.units:
            lines.update(unit.line_hashes)
        for (segment, seq), expected in messages.items():
            # Observer `_record` writes a MESSAGE receipt and its segments row in one transaction,
            # so a receipt needs its segment row and must lie inside that row's seq range.
            row = segments.get(segment)
            if row is None:
                problem = "MANIFEST_SEGMENT_MISSING"
                continue
            if type(seq) is not int or not row["first_seq"] <= seq <= row["last_seq"]:
                problem = "MANIFEST_RECORD_MISMATCH"
                continue
            if segment in result.verified_streams:
                if lines.get((segment, seq)) != expected:
                    problem = "MANIFEST_RECORD_MISMATCH"
            elif segment not in result.unverified_streams:
                problem = "MANIFEST_SEGMENT_MISSING"  # no such segment in the Archive at all
        if problem:
            result.manifest_quarantined = True
            result.coverage = [c for c in result.coverage if not c.ref.startswith("manifest.sqlite#")]
            result.units = [u if u.unit != "manifest.sqlite" else UnitResult(u.unit, u.sha256, "QUARANTINED", problem)
                            for u in result.units]
            result.manifest = None
            for unit in result.units:
                for rec in unit.records:
                    rec.epoch = None  # epoch came from the rejected manifest


class ObserverStateAdapter:
    kind = "observer-state-sqlite"

    @staticmethod
    def _message(row):
        """The stored record as a dict, or None when the row is not usable Evidence. Nothing is
        repaired or guessed: a non-object / unparseable / mistyped row is simply not Evidence."""
        raw = row["raw_record_json"]
        if (not isinstance(raw, str) or type(row["seq"]) is not int or type(row["observer_epoch"]) is not int
                or type(row["server_generation"]) is not int
                or (row["ingested_at"] is not None and not _finite(row["ingested_at"]))):
            return None
        try:
            message = _loads(raw.encode("utf-8", "surrogatepass"))
        except ValueError:
            return None
        if not isinstance(message, dict) or type(message.get("seq")) is not int or message["seq"] != row["seq"]:
            return None  # the preserved record must name the same integer sequence as its row
        return message

    def _malformed(self, table, epoch, key):
        label = f"{table}/epoch={str(epoch)[:24]!r}/key={str(key)[:24]!r}"
        return CoverageItem(self.source_id, self.room, "snapshot", "ROW_MALFORMED", None, None,
                            {"table": table, "note": "row is not valid Observer Evidence; excluded, not repaired"},
                            f"row-malformed:{label}")

    def __init__(self, source: dict):
        self.source = source
        self.source_id = source["id"]
        self.room = source["room"]
        self.path = Path(source["path"])
        self.provenance = source.get("provenance", "captured")
        self.allow_live = bool(source.get("allow_live_sqlite", False))
        self.snapshot = source.get("snapshot") is True

    def read(self, known_units: dict, manifest_progress: dict | None) -> SourceRead:
        """Snapshot contract: the source config declares ``snapshot: true`` for a file that
        no process writes (e.g. an SQLite Online Backup copy). Absence of ``-wal`` proves
        nothing (the Observer writer runs in WAL mode but init uses DELETE), so the file is
        opened ``immutable`` only under that declaration, with no sidecars present, and the
        read is discarded if the file changed meanwhile. Live reads stay a Human-gated opt-in."""
        result = SourceRead(self.source_id, self.kind, self.room, "READ", None)
        if not self.path.is_file() or self.path.is_symlink():
            result.status, result.reason = "UNAVAILABLE", "SOURCE_PATH_NOT_AVAILABLE"
            return result
        if not self.snapshot and not self.allow_live:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_CONTRACT_NOT_DECLARED"
            return result
        sidecars = [suffix for suffix in ("-wal", "-shm", "-journal") if Path(str(self.path) + suffix).exists()]
        if self.snapshot and sidecars:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_HAS_SQLITE_SIDECARS"
            return result
        try:
            application_id, version, _ = _sqlite_header(self.path)
        except (OSError, ValueError):
            result.status, result.reason = "DEFERRED", "SNAPSHOT_NOT_SQLITE"
            return result
        if application_id != 0x54434F42 or version != 2:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_NOT_OBSERVER_STATE_V2"
            return result
        before = _stat_key(self.path)
        uri = self.path.absolute().as_uri() + ("?mode=ro&immutable=1" if self.snapshot else "?mode=ro")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.25, isolation_level=None)
        except sqlite3.Error:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_OPEN_FAILED"
            return result
        records, coverage = [], []
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("BEGIN")
            try:
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("SNAPSHOT_INTEGRITY")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"state", "messages", "gaps", "epoch_history", "evidence_metadata"} <= tables:
                    raise sqlite3.DatabaseError("SNAPSHOT_SCHEMA")
                state = conn.execute("SELECT room FROM state WHERE singleton=1").fetchone()
                if state is None:
                    raise SnapshotIdentityError("SNAPSHOT_STATE_MISSING")
                if state["room"] != self.room:
                    # A different room's snapshot is not "a quiet source room with zero records".
                    raise SnapshotIdentityError("SNAPSHOT_ROOM_MISMATCH")
                for table in ("messages", "gaps"):
                    if conn.execute(f"SELECT 1 FROM {table} WHERE room<>? LIMIT 1", (self.room,)).fetchone():
                        raise SnapshotIdentityError("SNAPSHOT_ROWS_FOR_OTHER_ROOM")
                for row in conn.execute("SELECT * FROM messages WHERE room=? ORDER BY observer_epoch,seq",
                                        (self.room,)):
                    message = self._message(row)
                    if message is None:
                        coverage.append(self._malformed("messages", row["observer_epoch"], row["seq"]))
                        continue
                    stream = f"epoch-{row['observer_epoch']}"
                    records.append(EvidenceRecord(
                        source_id=self.source_id, source_kind=self.kind, provenance=self.provenance,
                        format_version=OBSERVER_FORMAT, room=self.room, stream=stream, seq=row["seq"],
                        epoch=row["observer_epoch"], generation=row["server_generation"], message=message,
                        record_sha256=hashlib.sha256(row["raw_record_json"].encode("utf-8", "surrogatepass")).hexdigest(),
                        hash_scope="OBSERVER_NORMALIZED_RAW_RECORD_JSON(not HTTP raw)",
                        ref=f"{self.source_id}:messages/{stream}/seq={row['seq']}",
                        locator={"source": self.source_id, "table": "messages",
                                 "observer_epoch": row["observer_epoch"],
                                 "server_generation": row["server_generation"], "seq": row["seq"]},
                        captured_at=row["ingested_at"],
                        quality=message_quality(message) + ["NO_HTTP_RAW_IN_OBSERVER_STATE"]))
                for row in conn.execute("SELECT * FROM gaps WHERE room=?", (self.room,)):
                    # Every value persisted into CoverageItem is checked: SQLite affinity lets a BLOB,
                    # text or Infinity sit in these columns, and canonical JSON cannot store them.
                    if (type(row["observer_epoch"]) is not int or type(row["gap_id"]) is not int
                            or type(row["start_seq"]) is not int or type(row["end_seq"]) is not int
                            or row["start_seq"] > row["end_seq"]
                            or not _finite(row["detected_at"]) or type(row["status"]) is not str):
                        coverage.append(self._malformed("gaps", row["observer_epoch"], row["gap_id"]))
                        continue
                    coverage.append(CoverageItem(
                        self.source_id, self.room, f"epoch-{row['observer_epoch']}", "GAP",
                        row["start_seq"], row["end_seq"], {"status": row["status"],
                                                           "detected_at": row["detected_at"]},
                        f"gaps/{row['gap_id']}"))
            finally:
                conn.execute("COMMIT")
        except SnapshotIdentityError as exc:
            result.status, result.reason = "DEFERRED", str(exc)
            return result
        except (sqlite3.Error, ValueError) as exc:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_READ_FAILED:" + type(exc).__name__
            return result
        finally:
            conn.close()
        if self.snapshot and _stat_key(self.path) != before:
            result.status, result.reason = "DEFERRED", "SNAPSHOT_CHANGED_DURING_READ"
            return result
        if not self.snapshot:
            result.reason = "LIVE_READ_BY_HUMAN_OPT_IN"
        digest = hashlib.sha256("".join(r.record_sha256 for r in records).encode()).hexdigest()
        result.units.append(UnitResult("state.sqlite", digest, "READ", None, records))
        result.coverage = coverage
        result.latest_captured_at = max((r.captured_at for r in records if r.captured_at), default=None)
        return result


ADAPTERS = {ArchiveAdapter.kind: ArchiveAdapter, ObserverStateAdapter.kind: ObserverStateAdapter}


def adapter_for(source: dict):
    try:
        return ADAPTERS[source["kind"]](source)
    except KeyError:
        raise ValueError("UNSUPPORTED_SOURCE_KIND") from None
