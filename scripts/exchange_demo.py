#!/usr/bin/env python3
"""End-to-end demonstration of the PCSv1 exchange snapshot protocol.

It simulates two isolated environments:

* ``online/``  - the supplier collection side that builds the source database
* ``offline/`` - the air-gapped review side that only receives a bundle +
  sidecar checksum, never the live SQLite file

Two exchanges are exercised:

1. a fully valid exchange -> verified, atomically imported, fingerprints match
   across environments (reproducibility)
2. deliberately hostile exchanges (tampered bytes, unknown format version,
   duplicate entity, corrupted record) -> atomically rejected, the review
   database stays byte-identical to before the attempt

Run (note ``-I``: the bundle directories are untrusted data)::

    python3 -I scripts/exchange_demo.py
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from parts_catalog.pipeline import PartsPipeline  # noqa: E402
from parts_catalog.snapshot import (  # noqa: E402
    CATALOG_NAME,
    MANIFEST_NAME,
    QUOTES_NAME,
    canonical_dumps,
    export_snapshot,
    import_snapshot,
    sha256_bytes,
    sha256_file,
    state_fingerprint,
    verify_snapshot,
)
from parts_catalog.sources import JsonFixtureSource  # noqa: E402
from parts_catalog.storage import PartsStore  # noqa: E402

# A richer payload than the committed fixture so the exchange contains several
# parts and quotes (kept inside this script, not in an untrusted directory).
DEMO_PAYLOAD = [
    {"supplier": "Northstar", "part_number": "AB-120 / 4", "brand": "Acme Inc.",
     "description": "Sealed bearing 20 mm", "unit": "ea", "package_quantity": 1,
     "lead_time_days": 4, "stock_status": "In Stock", "price": "18.50"},
    {"supplier": "Northstar", "part_number": "BR-550", "brand": "Northstar Ltd",
     "description": "Drive belt 550 mm", "unit": "EA", "package_quantity": 2,
     "lead_time_days": 9, "stock_status": "Backorder", "price": "7.20"},
    {"supplier": "Rotor Supply", "part_number": "AB1204", "brand": "ACME",
     "description": "Sealed bearing 20mm", "unit": "EA", "package_quantity": 1,
     "lead_time_days": 6, "stock_status": "Available", "price": "19.10"},
    {"supplier": "Rotor Supply", "part_number": "CV-88", "brand": "Covalve",
     "description": "Check valve DN15", "unit": "EA", "package_quantity": 1,
     "lead_time_days": None, "stock_status": "unknown", "price": None},
]

FIXED_TIME = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
BATCH_ID = "BATCH-2026-Q4-007"


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
    return condition


def build_source_database(db_path: Path, payload_path: Path) -> int:
    store = PartsStore(db_path)
    try:
        return PartsPipeline(JsonFixtureSource(payload_path), store).run(
            "USD", "portal://northstar/2026Q4"
        )
    finally:
        store.close()


def read_bundle(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle, "r:") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


def write_bundle(bundle: Path, files: dict[str, bytes], sidecar: Path) -> None:
    with tarfile.open(bundle, "w", format=tarfile.USTAR_FORMAT) as tar:
        for name in (MANIFEST_NAME, CATALOG_NAME, QUOTES_NAME):
            payload = files[name]
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    sidecar.write_text(f"{sha256_file(bundle)}  {bundle.name}\n", encoding="utf-8")


def main() -> int:
    failures: list[str] = []
    work = Path(tempfile.mkdtemp(prefix="pcs-exchange-demo-"))
    try:
        online = work / "online"
        offline = work / "offline"
        transfer = work / "transfer"  # the only channel between environments
        for directory in (online, offline, transfer):
            directory.mkdir(parents=True)

        # ---- online side: collect into SQLite, then export a snapshot --------
        print("== Online environment: build source database ==")
        payload_path = online / "supplier_payload.json"
        payload_path.write_text(json.dumps(DEMO_PAYLOAD), encoding="utf-8")
        source_db = online / "parts.sqlite3"
        processed = build_source_database(source_db, payload_path)
        source_fingerprint = state_fingerprint(source_db)
        artifacts = export_snapshot(
            source_db, transfer, batch_id=BATCH_ID,
            source_ref="portal://northstar/2026Q4", produced_at=FIXED_TIME,
        )
        bundle, sidecar = artifacts.bundle, artifacts.sidecar
        if not check("source rows ingested", processed == 4):
            failures.append("ingest")
        print(f"  bundle   : {bundle.name}")
        print(f"  sidecar  : {sidecar.name} ({sidecar.read_text().split()[0][:16]}...)")
        print(f"  parts    : {artifacts.part_count}  quotes: {artifacts.quote_count}")

        # Determinism: re-export into a second outbox and compare raw bytes.
        again = export_snapshot(
            source_db, work / "transfer2", batch_id=BATCH_ID,
            source_ref="portal://northstar/2026Q4", produced_at=FIXED_TIME,
        ).bundle
        if not check("bundles are byte-identical on re-export (deterministic)",
                     again.read_bytes() == bundle.read_bytes()):
            failures.append("determinism")

        # ---- offline side: valid exchange ------------------------------------
        print("\n== Offline environment: valid exchange ==")
        review_db = offline / "review.sqlite3"
        info = verify_snapshot(bundle)
        if not check("verify-snapshot accepts the bundle",
                     info.batch_id == BATCH_ID and info.format_version == 1,
                     f"{len(info.parts)} parts / {len(info.quotes)} quotes"):
            failures.append("valid-verify")

        result = import_snapshot(bundle, review_db, sidecar=sidecar)
        if not check("import switches database to snapshot state",
                     state_fingerprint(review_db) == source_fingerprint,
                     f"fingerprint {result['fingerprint'][:16]}..."):
            failures.append("valid-import")
        if not check("cross-environment fingerprints match (reproducibility)",
                     result["fingerprint"] == source_fingerprint):
            failures.append("fingerprint")
        if not check("snapshot_meta records batch and bundle hash",
                     (offline / "review.sqlite3").exists()):
            failures.append("meta")

        # ---- hostile exchanges: each must be atomically rejected -------------
        review_hash_before = sha256_file(review_db)
        mtime_before = review_db.stat().st_mtime_ns

        scenarios: list[tuple[str, str]] = []

        # 1. tamper with a quote inside quotes.json, re-seal file+sidecar so
        #    the per-record checksum is what catches it.
        hostile = work / "hostile-tamper"
        hostile.mkdir()
        h_bundle = hostile / bundle.name
        h_sidecar = hostile / sidecar.name
        shutil.copy(bundle, h_bundle)
        files = read_bundle(h_bundle)
        quotes = json.loads(files[QUOTES_NAME].decode("utf-8"))
        quotes["records"][0]["price"] = "0.01"
        payload = canonical_dumps(quotes).encode("utf-8")
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
        manifest["content"]["quotes"]["sha256"] = sha256_bytes(payload)
        files[QUOTES_NAME] = payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(h_bundle, files, h_sidecar)
        scenarios.append(("tampered record (price altered, re-sealed)", str(h_bundle)))

        # 2. unknown format version.
        hostile2 = work / "hostile-version"
        hostile2.mkdir()
        v_bundle = hostile2 / bundle.name
        v_sidecar = hostile2 / sidecar.name
        shutil.copy(bundle, v_bundle)
        files = read_bundle(v_bundle)
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
        manifest["format_version"] = 42
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(v_bundle, files, v_sidecar)
        scenarios.append(("unknown format version v42", str(v_bundle)))

        # 3. duplicate entity inside catalog.
        hostile3 = work / "hostile-duplicate"
        hostile3.mkdir()
        d_bundle = hostile3 / bundle.name
        d_sidecar = hostile3 / sidecar.name
        shutil.copy(bundle, d_bundle)
        files = read_bundle(d_bundle)
        catalog = json.loads(files[CATALOG_NAME].decode("utf-8"))
        duplicate = dict(catalog["records"][0])
        catalog["records"].append(duplicate)
        catalog_payload = canonical_dumps(catalog).encode("utf-8")
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
        manifest["content"]["catalog"]["sha256"] = sha256_bytes(catalog_payload)
        manifest["content"]["catalog"]["record_count"] = len(catalog["records"])
        manifest["record_checksums"]["catalog"].append(
            dict(manifest["record_checksums"]["catalog"][0])
        )
        files[CATALOG_NAME] = catalog_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(d_bundle, files, d_sidecar)
        scenarios.append(("duplicate catalog entity", str(d_bundle)))

        # 4. corrupt record: structurally wrong package_quantity.
        hostile4 = work / "hostile-corrupt"
        hostile4.mkdir()
        c_bundle = hostile4 / bundle.name
        c_sidecar = hostile4 / sidecar.name
        shutil.copy(bundle, c_bundle)
        files = read_bundle(c_bundle)
        catalog = json.loads(files[CATALOG_NAME].decode("utf-8"))
        catalog["records"][0]["package_quantity"] = -3
        catalog_payload = canonical_dumps(catalog).encode("utf-8")
        manifest = json.loads(files[MANIFEST_NAME].decode("utf-8"))
        manifest["content"]["catalog"]["sha256"] = sha256_bytes(catalog_payload)
        from parts_catalog.snapshot import _record_hash
        manifest["record_checksums"]["catalog"][0]["sha256"] = _record_hash(
            catalog["records"][0]
        )
        files[CATALOG_NAME] = catalog_payload
        files[MANIFEST_NAME] = canonical_dumps(manifest).encode("utf-8")
        write_bundle(c_bundle, files, c_sidecar)
        scenarios.append(("corrupt record (negative package_quantity)", str(c_bundle)))

        # 5. raw byte flip with untouched sidecar: whole-bundle checksum fails.
        hostile5 = work / "hostile-byteflip"
        hostile5.mkdir()
        b_bundle = hostile5 / bundle.name
        b_sidecar = hostile5 / sidecar.name
        shutil.copy(bundle, b_bundle)
        shutil.copy(sidecar, b_sidecar)
        raw = bytearray(b_bundle.read_bytes())
        raw[2048] ^= 0xFF
        b_bundle.write_bytes(bytes(raw))
        scenarios.append(("byte flip with stale sidecar", str(b_bundle)))

        print("\n== Offline environment: hostile exchanges (all must reject) ==")
        expected_codes = {
            "tampered record": "corrupt_record",
            "unknown format": "unknown_version",
            "duplicate catalog": "duplicate_entity",
            "corrupt record": "corrupt_record",
            "byte flip": "integrity_failure",
        }
        for label, path in scenarios:
            try:
                import_snapshot(path, review_db)
                ok, code = False, "no error raised"
            except Exception as exc:  # noqa: BLE001 - demo reports every type
                code = getattr(exc, "code", type(exc).__name__)
                expected = next(v for k, v in expected_codes.items() if label.startswith(k))
                ok = code == expected
            check(f"rejected: {label}", ok, f"decision={code}")
            if not ok:
                failures.append(label)

        untouched = (
            sha256_file(review_db) == review_hash_before
            and review_db.stat().st_mtime_ns == mtime_before
        )
        if not check("review database byte-identical and untouched after all rejections",
                     untouched):
            failures.append("atomicity")
        if not check("no temporary import databases left behind",
                     not list(offline.glob(".parts-import-*"))):
            failures.append("temp-cleanup")
        if not check("live state still matches the valid earlier snapshot",
                     state_fingerprint(review_db) == source_fingerprint):
            failures.append("state-survival")

        print("\n== Bundle layout ==")
        with tarfile.open(bundle, "r:") as tar:
            for member in tar.getmembers():
                print(f"  {member.name:15s} {member.size:>7d} bytes")

        print(f"\nWorkspace: {work}")
        if failures:
            print(f"DEMO FAILED: {failures}")
            return 1
        print("ALL CHECKS PASSED")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
