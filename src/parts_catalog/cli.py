from __future__ import annotations
import argparse
import json
from .entity_registry import SupplierRegistry
from .pipeline import PartsPipeline
from .sources import JsonFixtureSource, JsonHandoverSource, apply_handover
from .storage import PartsStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--database", default="parts-catalog.sqlite3")
    parser.add_argument("--currency", default="USD")
    parser.add_argument("--handover", help="JSON file with handover entities and alias claims")
    parser.add_argument(
        "--supplier-report",
        action="store_true",
        help="print the supplier resolution registry audit report as JSON",
    )
    parser.add_argument(
        "--revoke-decision",
        type=int,
        help="revoke a decision id after the run (creates a pending review hold)",
    )
    parser.add_argument("--revoked-by", default="cli-user")
    args = parser.parse_args()
    store = PartsStore(args.database)
    registry = SupplierRegistry(store.connection)
    try:
        if args.handover:
            apply_handover(registry, JsonHandoverSource(args.handover).read(), args.handover)
        count = PartsPipeline(JsonFixtureSource(args.fixture), store, registry).run(args.currency, args.fixture)
        revoked = None
        if args.revoke_decision is not None:
            decision = registry.revoke(args.revoke_decision, revoked_by=args.revoked_by)
            revoked = {
                "revoked_decision_id": args.revoke_decision,
                "replacement_decision_id": decision.decision_id,
                "replacement_state": decision.state,
                "replacement_reason": decision.reason,
            }
        result = {"processed": count, "database": args.database}
        if revoked is not None:
            result["revocation"] = revoked
        print(json.dumps(result, ensure_ascii=False))
        if args.supplier_report:
            print(json.dumps(registry.report(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
