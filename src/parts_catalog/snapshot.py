"""Parts-catalog exchange snapshot protocol (PCS).

A snapshot bundle is an uncompressed, deterministically written tar archive
containing exactly three regular members::

    manifest.json   protocol metadata, file hashes and per-record hashes
    catalog.json    {"records": [ {part fields...}, ... ]}
    quotes.json     {"records": [ {quote fields...}, ... ]}

A sidecar ``<bundle>.sha256`` carries the SHA-256 of the whole archive so the
offline reviewer can prove that the bytes received are the bytes produced.

Import is all-or-nothing: every structural, version, checksum, schema,
duplicate and referential check must pass before a temporary database is
built; the live database is swapped in a single ``os.replace`` only after the
temporary database passes ``PRAGMA integrity_check``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FORMAT_MAGIC = "parts-catalog-snapshot"
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = frozenset({FORMAT_VERSION})

MANIFEST_NAME = "manifest.json"
CATALOG_NAME = "catalog.json"
QUOTES_NAME = "quotes.json"
BUNDLE_MEMBERS = (MANIFEST_NAME, CATALOG_NAME, QUOTES_NAME)
BUNDLE_SUFFIX = ".pcsnap.tar"
SIDECAR_SUFFIX = ".sha256"

CATALOG_FIELDS = (
    "canonical_number",
    "brand",
    "description",
    "unit",
    "package_quantity",
)
QUOTE_FIELDS = (
    "canonical_number",
    "supplier",
    "currency",
    "price",
    "lead_time_days",
    "stock_status",
    "source_ref",
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class SnapshotError(Exception):
    """Base class for every snapshot protocol failure."""

    code = "snapshot_error"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}


class UnknownVersionError(SnapshotError):
    code = "unknown_version"


class DuplicateEntityError(SnapshotError):
    code = "duplicate_entity"


class CorruptRecordError(SnapshotError):
    code = "corrupt_record"


class IntegrityError(SnapshotError):
    code = "integrity_failure"


# --------------------------------------------------------------------------- #
# Small deterministic helpers
# --------------------------------------------------------------------------- #
def canonical_dumps(obj: Any) -> str:
    """JSON serialization that is byte-stable across machines/environments."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_hash(record: dict[str, Any]) -> str:
    return sha256_bytes(canonical_dumps(record).encode("utf-8"))


def _quote_key(record: dict[str, Any]) -> str:
    return "|".join(
        (record["canonical_number"], record["supplier"], record["source_ref"])
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SnapshotArtifacts:
    bundle: Path
    sidecar: Path
    snapshot_id: str
    batch_id: str
    part_count: int
    quote_count: int


def _read_database_state(database: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise SnapshotError(
                "source database failed integrity_check", {"result": integrity}
            )
        connection.execute("BEGIN")
        parts = [
            dict(zip(CATALOG_FIELDS, row))
            for row in connection.execute(
                "SELECT canonical_number, brand, description, unit, package_quantity "
                "FROM parts ORDER BY canonical_number"
            )
        ]
        quotes = [
            dict(zip(QUOTE_FIELDS, row))
            for row in connection.execute(
                "SELECT canonical_number, supplier, currency, price, lead_time_days, "
                "stock_status, source_ref FROM quotes "
                "ORDER BY canonical_number, supplier, source_ref"
            )
        ]
        return parts, quotes
    except sqlite3.OperationalError as exc:
        raise SnapshotError(f"cannot read source database: {exc}") from exc
    finally:
        connection.close()


def _add_tar_member(tar: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    # Fixed metadata: archives built from the same data are byte-identical.
    info.mtime = 0
    info.mode = 0o644
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(payload))


def export_snapshot(
    database: str | Path,
    output_dir: str | Path,
    batch_id: str,
    source_ref: str,
    produced_at: datetime | None = None,
) -> SnapshotArtifacts:
    """Export a consistent read-only view of ``database`` into a bundle."""
    if not batch_id or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")
    if not source_ref or not source_ref.strip():
        raise ValueError("source_ref must be a non-empty string")

    produced_at = produced_at or datetime.now(timezone.utc)
    produced_at = produced_at.astimezone(timezone.utc).replace(microsecond=0)
    timestamp = produced_at.strftime("%Y%m%dT%H%M%SZ")
    snapshot_id = f"pcs-{batch_id}-{timestamp}"

    parts, quotes = _read_database_state(database)

    catalog_payload = canonical_dumps({"records": parts}).encode("utf-8")
    quotes_payload = canonical_dumps({"records": quotes}).encode("utf-8")

    manifest: dict[str, Any] = {
        "format": FORMAT_MAGIC,
        "format_version": FORMAT_VERSION,
        "snapshot_id": snapshot_id,
        "batch_id": batch_id,
        "source_ref": source_ref,
        "produced_at": produced_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "content": {
            "catalog": {
                "file": CATALOG_NAME,
                "sha256": sha256_bytes(catalog_payload),
                "record_count": len(parts),
            },
            "quotes": {
                "file": QUOTES_NAME,
                "sha256": sha256_bytes(quotes_payload),
                "record_count": len(quotes),
            },
        },
        "record_checksums": {
            "catalog": [
                {"key": record["canonical_number"], "sha256": _record_hash(record)}
                for record in parts
            ],
            "quotes": [
                {"key": _quote_key(record), "sha256": _record_hash(record)}
                for record in quotes
            ],
        },
        "database_identity": {
            "sqlite_version": sqlite3.sqlite_version,
        },
    }
    manifest_payload = canonical_dumps(manifest).encode("utf-8")

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    bundle = out / f"{snapshot_id}{BUNDLE_SUFFIX}"
    sidecar = Path(str(bundle) + SIDECAR_SUFFIX)

    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as tar:
        _add_tar_member(tar, MANIFEST_NAME, manifest_payload)
        _add_tar_member(tar, CATALOG_NAME, catalog_payload)
        _add_tar_member(tar, QUOTES_NAME, quotes_payload)

    digest = sha256_file(bundle)
    sidecar.write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")

    return SnapshotArtifacts(
        bundle=bundle,
        sidecar=sidecar,
        snapshot_id=snapshot_id,
        batch_id=batch_id,
        part_count=len(parts),
        quote_count=len(quotes),
    )


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    batch_id: str
    source_ref: str
    format_version: int
    produced_at: str
    parts: tuple[dict[str, Any], ...]
    quotes: tuple[dict[str, Any], ...]
    bundle_sha256: str

    def summary(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "batch_id": self.batch_id,
            "source_ref": self.source_ref,
            "format_version": self.format_version,
            "produced_at": self.produced_at,
            "part_count": len(self.parts),
            "quote_count": len(self.quotes),
            "bundle_sha256": self.bundle_sha256,
        }


def _resolve_sidecar(bundle: Path, sidecar: str | Path | None) -> Path | None:
    if sidecar is not None:
        path = Path(sidecar)
        if not path.is_file():
            raise IntegrityError(
                "sidecar checksum file not found", {"sidecar": str(path)}
            )
        return path
    default = Path(str(bundle) + SIDECAR_SUFFIX)
    return default if default.is_file() else None


def _check_sidecar(bundle: Path, sidecar: Path | None, require: bool) -> str:
    if sidecar is None:
        if require:
            raise IntegrityError(
                "sidecar checksum required but not provided",
                {"expected": str(bundle) + SIDECAR_SUFFIX},
            )
        return sha256_file(bundle)
    text = sidecar.read_text(encoding="utf-8").strip().split()
    if len(text) < 2 or len(text[0]) != 64:
        raise IntegrityError(
            "sidecar checksum file is malformed", {"sidecar": str(sidecar)}
        )
    expected = text[0].lower()
    actual = sha256_file(bundle)
    if actual != expected:
        raise IntegrityError(
            "bundle checksum does not match sidecar",
            {"expected": expected, "actual": actual, "sidecar": str(sidecar)},
        )
    return actual


def _safe_extract(tar: tarfile.TarFile) -> dict[str, bytes]:
    members = tar.getmembers()
    names = [member.name for member in members]
    if sorted(names) != sorted(BUNDLE_MEMBERS) or len(names) != len(set(names)):
        raise IntegrityError(
            "bundle must contain exactly the three protocol members",
            {"members": names},
        )
    extracted: dict[str, bytes] = {}
    for member in members:
        if member.islnk() or member.issym() or member.isdev() or member.isdir():
            raise IntegrityError(
                "bundle contains an unsupported member type",
                {"member": member.name, "type": str(member.type)},
            )
        if os.path.isabs(member.name) or ".." in Path(member.name).parts:
            raise IntegrityError("unsafe member path", {"member": member.name})
        handle = tar.extractfile(member)
        if handle is None:
            raise IntegrityError("member cannot be read", {"member": member.name})
        extracted[member.name] = handle.read()
    return extracted


def _load_records(payload: bytes, file_name: str) -> list[dict[str, Any]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptRecordError(
            f"{file_name} is not valid JSON", {"file": file_name}
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        raise CorruptRecordError(
            f"{file_name} must be an object with a 'records' list",
            {"file": file_name},
        )
    return document["records"]


def _validate_catalog_record(record: Any, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise CorruptRecordError(
            "catalog record is not an object", {"index": index}
        )
    if set(record) != set(CATALOG_FIELDS):
        raise CorruptRecordError(
            "catalog record has unexpected fields",
            {"index": index, "fields": sorted(record) if isinstance(record, dict) else None},
        )
    for field in ("canonical_number", "brand", "description", "unit"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise CorruptRecordError(
                f"catalog record field {field!r} must be a non-empty string",
                {"index": index, "key": record.get("canonical_number")},
            )
    quantity = record["package_quantity"]
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
        raise CorruptRecordError(
            "catalog record package_quantity must be a positive integer",
            {"index": index, "key": record["canonical_number"]},
        )
    return record


def _validate_quote_record(record: Any, index: int) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise CorruptRecordError("quote record is not an object", {"index": index})
    if set(record) != set(QUOTE_FIELDS):
        raise CorruptRecordError(
            "quote record has unexpected fields",
            {"index": index, "fields": sorted(record) if isinstance(record, dict) else None},
        )
    for field in ("canonical_number", "supplier", "currency", "stock_status", "source_ref"):
        if not isinstance(record[field], str) or not record[field].strip():
            raise CorruptRecordError(
                f"quote record field {field!r} must be a non-empty string",
                {"index": index, "key": _quote_key(record) if isinstance(record, dict) else None},
            )
    price = record["price"]
    if price is not None and not isinstance(price, str):
        raise CorruptRecordError(
            "quote record price must be a string or null",
            {"index": index, "key": _quote_key(record)},
        )
    lead_time = record["lead_time_days"]
    if lead_time is not None and (
        not isinstance(lead_time, int) or isinstance(lead_time, bool) or lead_time < 0
    ):
        raise CorruptRecordError(
            "quote record lead_time_days must be a non-negative integer or null",
            {"index": index, "key": _quote_key(record)},
        )
    return record


def _check_record_hashes(
    records: Iterable[dict[str, Any]],
    checksums: list[dict[str, str]],
    key_fn,
    table: str,
) -> None:
    by_key = {entry["key"]: entry["sha256"] for entry in checksums}
    seen: set[str] = set()
    for index, record in enumerate(records):
        key = key_fn(record)
        if key not in by_key:
            raise CorruptRecordError(
                "record is missing from manifest checksum list",
                {"table": table, "key": key, "index": index},
            )
        if _record_hash(record) != by_key[key]:
            raise CorruptRecordError(
                "record checksum does not match manifest",
                {"table": table, "key": key, "index": index},
            )
        if key in seen:
            # Defined import decision: duplicates inside one snapshot reject
            # the whole exchange; no partial data is ever loaded.
            raise DuplicateEntityError(
                "duplicate entity inside snapshot",
                {"table": table, "key": key, "index": index},
            )
        seen.add(key)
    if seen != set(by_key):
        raise CorruptRecordError(
            "manifest checksum list does not cover exactly the shipped records",
            {"table": table, "missing_in_bundle": sorted(set(by_key) - seen),
             "missing_in_manifest": sorted(seen - set(by_key))},
        )


def verify_snapshot(
    bundle: str | Path,
    sidecar: str | Path | None = None,
    require_sidecar: bool = False,
) -> SnapshotInfo:
    """Run every read-only validation; raise on the first failure."""
    bundle_path = Path(bundle)
    if not bundle_path.is_file():
        raise IntegrityError("snapshot bundle not found", {"bundle": str(bundle_path)})

    sidecar_path = _resolve_sidecar(bundle_path, sidecar)
    bundle_digest = _check_sidecar(bundle_path, sidecar_path, require_sidecar)

    try:
        with tarfile.open(bundle_path, "r:") as tar:
            files = _safe_extract(tar)
    except tarfile.TarError as exc:
        raise IntegrityError("bundle is not a readable tar archive", {"reason": str(exc)}) from exc

    # 1. manifest structure and protocol version
    try:
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorruptRecordError("manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_MAGIC:
        raise IntegrityError("missing or unknown snapshot format marker")
    version = manifest.get("format_version")
    if version not in SUPPORTED_VERSIONS:
        # Defined import decision: never guess forward/backward compatibility.
        raise UnknownVersionError(
            "snapshot format version is not supported by this importer",
            {"received": version, "supported": sorted(SUPPORTED_VERSIONS)},
        )

    content = manifest.get("content")
    if not isinstance(content, dict):
        raise IntegrityError("manifest content section is missing")

    # 2. file-level hashes from the manifest
    for table, file_name in (("catalog", CATALOG_NAME), ("quotes", QUOTES_NAME)):
        section = content.get(table)
        if not isinstance(section, dict) or section.get("file") != file_name:
            raise IntegrityError("manifest content entry is invalid", {"table": table})
        actual = sha256_bytes(files[file_name])
        if actual != section.get("sha256"):
            raise IntegrityError(
                f"{file_name} hash does not match manifest",
                {"table": table, "expected": section.get("sha256"), "actual": actual},
            )

    parts = [_validate_catalog_record(r, i) for i, r in enumerate(_load_records(files[CATALOG_NAME], CATALOG_NAME))]
    quotes = [_validate_quote_record(r, i) for i, r in enumerate(_load_records(files[QUOTES_NAME], QUOTES_NAME))]

    # 3. declared counts
    for table, records, section_name in (
        ("catalog", parts, "catalog"),
        ("quotes", quotes, "quotes"),
    ):
        if len(records) != content[section_name].get("record_count"):
            raise CorruptRecordError(
                "record count does not match manifest",
                {"table": table, "manifest": content[section_name].get("record_count"),
                 "actual": len(records)},
            )

    # 4. per-record hashes + in-snapshot duplicate detection
    checksums = manifest.get("record_checksums")
    if not isinstance(checksums, dict):
        raise IntegrityError("manifest record_checksums section is missing")
    _check_record_hashes(parts, checksums.get("catalog", []), lambda r: r["canonical_number"], "catalog")
    _check_record_hashes(quotes, checksums.get("quotes", []), _quote_key, "quotes")

    # 5. referential integrity: every quote must point at a shipped part
    part_keys = {record["canonical_number"] for record in parts}
    for index, record in enumerate(quotes):
        if record["canonical_number"] not in part_keys:
            raise CorruptRecordError(
                "quote references a canonical_number absent from the catalog",
                {"index": index, "key": _quote_key(record),
                 "canonical_number": record["canonical_number"]},
            )

    return SnapshotInfo(
        snapshot_id=str(manifest["snapshot_id"]),
        batch_id=str(manifest["batch_id"]),
        source_ref=str(manifest["source_ref"]),
        format_version=version,
        produced_at=str(manifest["produced_at"]),
        parts=tuple(parts),
        quotes=tuple(quotes),
        bundle_sha256=bundle_digest,
    )


# --------------------------------------------------------------------------- #
# Atomic import
# --------------------------------------------------------------------------- #
def _build_temp_database(info: SnapshotInfo, target: Path) -> Path:
    fd, tmp_name = tempfile.mkstemp(
        prefix=".parts-import-", suffix=".sqlite3", dir=str(target.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        connection = sqlite3.connect(tmp_path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("""CREATE TABLE parts (
                canonical_number TEXT PRIMARY KEY, brand TEXT NOT NULL,
                description TEXT NOT NULL, unit TEXT NOT NULL,
                package_quantity INTEGER NOT NULL
            )""")
            connection.execute("""CREATE TABLE quotes (
                canonical_number TEXT NOT NULL, supplier TEXT NOT NULL,
                currency TEXT NOT NULL, price TEXT, lead_time_days INTEGER,
                stock_status TEXT NOT NULL, source_ref TEXT NOT NULL,
                PRIMARY KEY (canonical_number, supplier, source_ref)
            )""")
            connection.execute("""CREATE TABLE snapshot_meta (
                snapshot_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL,
                source_ref TEXT NOT NULL, format_version INTEGER NOT NULL,
                produced_at TEXT NOT NULL, imported_at TEXT NOT NULL,
                bundle_sha256 TEXT NOT NULL
            )""")
            connection.executemany(
                "INSERT INTO parts VALUES (?, ?, ?, ?, ?)",
                [tuple(record[field] for field in CATALOG_FIELDS) for record in info.parts],
            )
            connection.executemany(
                "INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)",
                [tuple(record[field] for field in QUOTE_FIELDS) for record in info.quotes],
            )
            connection.execute(
                "INSERT INTO snapshot_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    info.snapshot_id,
                    info.batch_id,
                    info.source_ref,
                    info.format_version,
                    info.produced_at,
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    info.bundle_sha256,
                ),
            )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise SnapshotError(
                    "temporary database failed integrity_check", {"result": integrity}
                )
        finally:
            connection.close()
        os.chmod(tmp_path, 0o644)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def import_snapshot(
    bundle: str | Path,
    database: str | Path,
    sidecar: str | Path | None = None,
    require_sidecar: bool = False,
) -> dict[str, Any]:
    """Validate then atomically replace ``database`` with snapshot state."""
    target = Path(database)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Raises on any protocol failure; the target is never touched before this
    # returns, so a rejection leaves the live database byte-identical.
    info = verify_snapshot(bundle, sidecar=sidecar, require_sidecar=require_sidecar)

    temp_db = _build_temp_database(info, target)
    backup: Path | None = None
    try:
        for stale in (
            str(target) + "-wal",
            str(target) + "-shm",
            str(target) + "-journal",
        ):
            Path(stale).unlink(missing_ok=True)

        if target.exists():
            backup = target.with_name(
                target.name
                + ".pre-import."
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + ".bak"
            )
            os.replace(target, backup)
        os.replace(temp_db, target)
        _fsync_directory(target.parent)
    except Exception:
        # Roll back: restore the previous database if the swap did not land.
        if not target.exists() and backup is not None:
            os.replace(backup, target)
        raise

    return {
        **info.summary(),
        "database": str(target),
        "backup": str(backup) if backup is not None else None,
        "fingerprint": state_fingerprint(target),
    }


# --------------------------------------------------------------------------- #
# State fingerprint for cross-environment reproducibility checks
# --------------------------------------------------------------------------- #
def state_fingerprint(database: str | Path) -> str:
    """Stable SHA-256 over the business rows of a catalog database."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    digest = hashlib.sha256()
    try:
        for table, columns, order in (
            ("parts", CATALOG_FIELDS, "canonical_number"),
            ("quotes", QUOTE_FIELDS, "canonical_number, supplier, source_ref"),
        ):
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
            ).fetchall()
            for row in rows:
                digest.update(
                    canonical_dumps({"table": table, "row": list(row)}).encode("utf-8")
                )
            digest.update(canonical_dumps({"table": table, "count": len(rows)}).encode("utf-8"))
    finally:
        connection.close()
    return digest.hexdigest()
