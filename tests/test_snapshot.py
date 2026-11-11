"""Protocol-level tests for the parts-catalog exchange snapshot (PCSv1).

Runs with the standard library only::

    PYTHONPATH=src python3 -m unittest tests.test_snapshot -v
"""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from parts_catalog.pipeline import PartsPipeline
from parts_catalog.snapshot import (
    BUNDLE_SUFFIX,
    CATALOG_NAME,
    CorruptRecordError,
    DuplicateEntityError,
    FORMAT_MAGIC,
    FORMAT_VERSION,
    IntegrityError,
    MANIFEST_NAME,
    QUOTES_NAME,
    UnknownVersionError,
    _record_hash,
    canonical_dumps,
    export_snapshot,
    import_snapshot,
    sha256_bytes,
    sha256_file,
    state_fingerprint,
    verify_snapshot,
)
from parts_catalog.sources import JsonFixtureSource
from parts_catalog.storage import PartsStore

FIXTURE = str(Path(__file__).resolve().parents[1] / "fixtures" / "supplier_payload.json")
FIXED_TIME = datetime(2026, 11, 11, 9, 0, 0, tzinfo=timezone.utc)


def build_database(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = PartsStore(path)
    try:
        PartsPipeline(JsonFixtureSource(FIXTURE), store).run("USD", FIXTURE)
    finally:
        store.close()


def read_bundle(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle, "r:") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}


def write_bundle(bundle: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in (MANIFEST_NAME, CATALOG_NAME, QUOTES_NAME):
            payload = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(payload))
    sidecar = Path(str(bundle) + ".sha256")
    sidecar.write_text(f"{sha256_file(bundle)}  {bundle.name}\n", encoding="utf-8")


def load_json(payload: bytes):
    return json.loads(payload.decode("utf-8"))


class SnapshotProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source_db = self.root / "online" / "parts.sqlite3"
        self.out_dir = self.root / "outbox"
        build_database(self.source_db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _export(self) -> tuple[Path, Path]:
        artifacts = export_snapshot(
            self.source_db,
            self.out_dir,
            batch_id="BATCH-2026-011",
            source_ref="supplier-portal",
            produced_at=FIXED_TIME,
        )
        return artifacts.bundle, artifacts.sidecar

    def test_valid_export_verify_and_atomic_import_round_trip(self) -> None:
        bundle, sidecar = self._export()
        self.assertTrue(bundle.name.endswith(BUNDLE_SUFFIX))

        info = verify_snapshot(bundle)
        self.assertEqual(info.format_version, FORMAT_VERSION)
        self.assertEqual(info.batch_id, "BATCH-2026-011")
        # Fixture rows normalize to one canonical part; identical
        # (part, supplier, source_ref) collapses to one quote in the store.
        self.assertEqual(len(info.parts), 1)
        self.assertEqual(len(info.quotes), 1)
        self.assertEqual(info.bundle_sha256, sha256_file(bundle))

        # Sidecar proves whole-bundle integrity.
        sidecar_digest = sidecar.read_text().split()[0]
        self.assertEqual(sidecar_digest, info.bundle_sha256)

        target = self.root / "offline" / "review.sqlite3"
        result = import_snapshot(bundle, target, sidecar=sidecar)
        self.assertEqual(result["status"] if "status" in result else "imported", "imported")
        self.assertEqual(state_fingerprint(self.source_db), result["fingerprint"])
        self.assertEqual(state_fingerprint(self.source_db), state_fingerprint(target))

        connection = sqlite3.connect(target)
        try:
            meta = connection.execute(
                "SELECT snapshot_id, batch_id, bundle_sha256 FROM snapshot_meta"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(meta[0], info.snapshot_id)
        self.assertEqual(meta[1], "BATCH-2026-011")
        self.assertEqual(meta[2], info.bundle_sha256)

    def test_bundle_is_deterministic_across_runs(self) -> None:
        first = self._export()[0]
        second_dir = self.root / "outbox2"
        second = export_snapshot(
            self.source_db,
            second_dir,
            batch_id="BATCH-2026-011",
            source_ref="supplier-portal",
            produced_at=FIXED_TIME,
        ).bundle
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_unknown_version_is_rejected_and_target_untouched(self) -> None:
        bundle, sidecar = self._export()
        files = read_bundle(bundle)
        manifest = load_json(files[MANIFEST_NAME])
        manifest["format_version"] = 99
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(bundle, files)  # re-sign sidecar so version check is what fails

        target = self.root / "offline" / "review.sqlite3"
        build_database(target)
        before = sha256_file(target)
        with self.assertRaises(UnknownVersionError) as ctx:
            import_snapshot(bundle, target)
        self.assertEqual(ctx.exception.detail["received"], 99)
        self.assertEqual(sha256_file(target), before)
        self.assertFalse(any(self.root.rglob(".parts-import-*")))

    def test_duplicate_entity_inside_snapshot_rejects_whole_import(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        catalog = load_json(files[CATALOG_NAME])
        catalog["records"].append(dict(catalog["records"][0]))  # same primary key
        catalog_payload = canonical_dumps(catalog).encode("utf-8")
        manifest = load_json(files[MANIFEST_NAME])
        manifest["content"]["catalog"]["sha256"] = sha256_bytes(catalog_payload)
        manifest["content"]["catalog"]["record_count"] = len(catalog["records"])
        manifest["record_checksums"]["catalog"].append(
            dict(manifest["record_checksums"]["catalog"][0])
        )
        files[CATALOG_NAME] = catalog_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(bundle, files)

        target = self.root / "offline" / "review.sqlite3"
        with self.assertRaises(DuplicateEntityError) as ctx:
            import_snapshot(bundle, target)
        self.assertEqual(ctx.exception.detail["table"], "catalog")
        self.assertFalse(target.exists())

    def test_corrupt_record_checksum_is_rejected_with_key(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        catalog = load_json(files[CATALOG_NAME])
        original = catalog["records"][0]["description"]
        catalog["records"][0]["description"] = original + " TAMPERED"
        catalog_payload = canonical_dumps(catalog).encode("utf-8")
        # File hash is re-sealed but per-record hash is not: proves record-level checks work.
        manifest = load_json(files[MANIFEST_NAME])
        manifest["content"]["catalog"]["sha256"] = sha256_bytes(catalog_payload)
        files[CATALOG_NAME] = catalog_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(bundle, files)

        target = self.root / "offline" / "review.sqlite3"
        with self.assertRaises(CorruptRecordError) as ctx:
            import_snapshot(bundle, target)
        self.assertEqual(ctx.exception.detail["table"], "catalog")
        self.assertEqual(ctx.exception.detail["key"], "AB1204")
        self.assertFalse(target.exists())

    def test_file_level_tampering_is_rejected(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        quotes = load_json(files[QUOTES_NAME])
        quotes["records"][0]["price"] = "0.01"
        files[QUOTES_NAME] = canonical_dumps(quotes).encode("utf-8")
        # Manifest left untouched: file hash mismatch must trip first.
        write_bundle(bundle, files)
        with self.assertRaises(IntegrityError):
            verify_snapshot(bundle)

    def test_malformed_record_fields_are_corrupt(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        catalog = load_json(files[CATALOG_NAME])
        catalog["records"][0]["package_quantity"] = 0
        catalog_payload = canonical_dumps(catalog).encode("utf-8")
        manifest = load_json(files[MANIFEST_NAME])
        manifest["content"]["catalog"]["sha256"] = sha256_bytes(catalog_payload)
        manifest["record_checksums"]["catalog"][0]["sha256"] = _record_hash(
            catalog["records"][0]
        )
        files[CATALOG_NAME] = catalog_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(bundle, files)
        with self.assertRaises(CorruptRecordError):
            verify_snapshot(bundle)

    def test_dangling_quote_reference_is_corrupt(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        quotes = load_json(files[QUOTES_NAME])
        quotes["records"][0]["canonical_number"] = "GHOST999"
        quotes_payload = canonical_dumps(quotes).encode("utf-8")
        manifest = load_json(files[MANIFEST_NAME])
        manifest["content"]["quotes"]["sha256"] = sha256_bytes(quotes_payload)
        checksums = [
            {"key": "GHOST999|Northstar|" + r["source_ref"], "sha256": _record_hash(r)}
            for r in quotes["records"]
        ]
        manifest["record_checksums"]["quotes"] = checksums
        files[QUOTES_NAME] = quotes_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(bundle, files)
        with self.assertRaises(CorruptRecordError) as ctx:
            verify_snapshot(bundle)
        self.assertIn("absent from the catalog", ctx.exception.message)

    def test_sidecar_tampering_rejects_import(self) -> None:
        bundle, sidecar = self._export()
        sidecar.write_text("0" * 64 + f"  {bundle.name}\n", encoding="utf-8")
        with self.assertRaises(IntegrityError):
            verify_snapshot(bundle, sidecar=sidecar)
        with self.assertRaises(IntegrityError):
            import_snapshot(bundle, self.root / "x.sqlite3", sidecar=sidecar)

    def test_require_sidecar_enforces_transport_proof(self) -> None:
        bundle, sidecar = self._export()
        moved = sidecar.with_suffix(".hidden")
        sidecar.rename(moved)
        try:
            with self.assertRaises(IntegrityError):
                verify_snapshot(bundle, require_sidecar=True)
        finally:
            moved.rename(sidecar)

    def test_unsafe_tar_member_is_rejected(self) -> None:
        bundle, _ = self._export()
        files = read_bundle(bundle)
        files["../escape.txt"] = b"x"
        with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as tar:
            for name, payload in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(payload)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(IntegrityError):
            verify_snapshot(bundle)

    def test_reimport_replaces_state_and_keeps_backup(self) -> None:
        bundle, _ = self._export()
        target = self.root / "live.sqlite3"
        build_database(target)
        first_backup = None
        result = import_snapshot(bundle, target)
        first_backup = result["backup"]
        self.assertTrue(Path(first_backup).exists())
        fingerprint_after_first = state_fingerprint(target)

        # Re-import the same valid snapshot: still atomic, state unchanged.
        second = import_snapshot(bundle, target)
        self.assertEqual(second["fingerprint"], fingerprint_after_first)
        self.assertTrue(Path(second["backup"]).exists())

    def test_manifest_carries_required_protocol_fields(self) -> None:
        bundle, _ = self._export()
        manifest = load_json(read_bundle(bundle)[MANIFEST_NAME])
        self.assertEqual(manifest["format"], FORMAT_MAGIC)
        self.assertEqual(manifest["format_version"], FORMAT_VERSION)
        self.assertEqual(manifest["batch_id"], "BATCH-2026-011")
        self.assertIn("produced_at", manifest)
        self.assertIn("snapshot_id", manifest)
        for table in ("catalog", "quotes"):
            self.assertIn("sha256", manifest["content"][table])
            self.assertIn("record_count", manifest["content"][table])
            self.assertTrue(manifest["record_checksums"][table])


if __name__ == "__main__":
    unittest.main()
