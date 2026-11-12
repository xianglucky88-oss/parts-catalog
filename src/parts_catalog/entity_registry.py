"""Auditable supplier entity resolution registry.

The registry deliberately keeps four concepts apart:

* ``supplier_observations`` -- the *raw* supplier string exactly as received,
  together with its ``source_ref``.  Raw values are never rewritten.
* ``supplier_aliases`` -- explicit alias claims (handover spreadsheets, old
  portal accounts, regional trading names) and the basis/source that supports
  them.
* ``supplier_entities`` -- stable entity identifiers.  A display name can
  change; the entity id does not.
* ``supplier_decisions`` / ``supplier_evidence`` -- the audit trail: what was
  decided, by whom, from which evidence, and whether the decision was later
  superseded or revoked.

Automatic resolution is intentionally conservative.  Only strong evidence
(an explicit confirmed alias, an exact entity display-name match, or an exact
case/whitespace variant of an already resolved name) can produce a
``resolved`` decision.  Similar-looking names and competing alias claims are
kept as ``pending`` / ``conflict`` for human disposition.

All writes are idempotent with respect to ``(source_ref, raw_name)`` and
``(alias, entity, source_ref, basis)``; receiving the same handover material
twice neither creates new observations nor accumulates new evidence rows.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

RESOLVED = "resolved"
PENDING = "pending"
CONFLICT = "conflict"
VALID_STATES = (RESOLVED, PENDING, CONFLICT)

AUTO_DECIDER = "auto_registry"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_supplier_name(raw_name: str) -> str:
    """Return a comparison key, not a re-written display name.

    Case folding, Unicode width folding and whitespace collapsing make
    ``Acme Trading`` / ``ACME TRADING`` / ``Acme   Trading`` identical.  We
    deliberately do *not* remove legal suffixes or punctuation, because that
    would risk merging genuinely different companies.
    """
    value = unicodedata.normalize("NFKC", raw_name)
    value = value.casefold()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _name_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


@dataclass(frozen=True)
class Entity:
    entity_id: str
    display_name: str
    display_norm: str
    created_at: str
    created_by: str


@dataclass(frozen=True)
class Observation:
    observation_id: int
    raw_name: str
    normalized_name: str
    source_ref: str
    content_key: str
    observed_at: str
    created_entity_id: str | None


@dataclass(frozen=True)
class AliasClaim:
    claim_id: int
    alias_raw: str
    alias_norm: str
    entity_id: str
    basis: str
    source_ref: str
    status: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class Evidence:
    evidence_id: int
    observation_id: int
    entity_id: str | None
    kind: str
    strength: str
    detail: str
    claim_id: int | None
    source_ref: str
    created_at: str


@dataclass(frozen=True)
class Decision:
    decision_id: int
    observation_id: int
    state: str
    entity_id: str | None
    reason: str
    evidence_ids: tuple[int, ...]
    decided_by: str
    decided_at: str
    is_hold: bool
    supersedes_decision_id: int | None
    superseded_by_decision_id: int | None
    revoked_at: str | None
    revoked_by: str | None

    @property
    def is_current(self) -> bool:
        return self.revoked_at is None and self.superseded_by_decision_id is None


class SupplierRegistry:
    """SQLite-backed supplier entity registry.

    A registry can share the connection of :class:`PartsStore` so that part /
    quote ingestion and supplier resolution participate in the same audit
    database.
    """

    SIMILARITY_THRESHOLD = 0.8
    MIN_SIMILARITY_LENGTH = 4

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS supplier_entities (
                entity_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                display_norm TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_from_observation_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS supplier_aliases (
                claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias_raw TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                entity_id TEXT NOT NULL REFERENCES supplier_entities(entity_id),
                basis TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                UNIQUE (alias_norm, entity_id, source_ref, basis)
            );

            CREATE TABLE IF NOT EXISTS supplier_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                content_key TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_entity_id TEXT REFERENCES supplier_entities(entity_id),
                UNIQUE (source_ref, raw_name)
            );

            CREATE INDEX IF NOT EXISTS idx_supplier_observations_norm
                ON supplier_observations (normalized_name);

            CREATE TABLE IF NOT EXISTS supplier_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER NOT NULL REFERENCES supplier_observations(observation_id),
                entity_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                strength TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                claim_id INTEGER NOT NULL DEFAULT 0,
                source_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (observation_id, entity_id, kind, claim_id)
            );

            CREATE TABLE IF NOT EXISTS supplier_decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id INTEGER NOT NULL REFERENCES supplier_observations(observation_id),
                state TEXT NOT NULL CHECK (state IN ('resolved', 'pending', 'conflict')),
                entity_id TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                evidence_ids TEXT NOT NULL DEFAULT '[]',
                decided_by TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                is_hold INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL,
                supersedes_decision_id INTEGER,
                superseded_by_decision_id INTEGER,
                revoked_at TEXT,
                revoked_by TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_supplier_decisions_current
                ON supplier_decisions (observation_id, superseded_by_decision_id, revoked_at);
            """)
        self.connection.commit()

    # ------------------------------------------------------------------
    # entities

    def create_entity(
        self,
        entity_id: str,
        display_name: str,
        *,
        created_by: str = "handover",
        created_from_observation_id: int | None = None,
    ) -> Entity:
        """Register a stable entity.  Idempotent for an existing entity id."""
        entity_id = entity_id.strip()
        display_name = display_name.strip()
        if not entity_id:
            raise ValueError("entity_id must not be empty")
        if not display_name:
            raise ValueError("display_name must not be empty")
        display_norm = normalize_supplier_name(display_name)
        self.connection.execute(
            """INSERT OR IGNORE INTO supplier_entities
               (entity_id, display_name, display_norm, created_at, created_by,
                created_from_observation_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, display_name, display_norm, utc_now(), created_by,
             created_from_observation_id),
        )
        self.connection.commit()
        return self.get_entity(entity_id)

    def get_entity(self, entity_id: str) -> Entity:
        row = self.connection.execute(
            """SELECT entity_id, display_name, display_norm, created_at, created_by
               FROM supplier_entities WHERE entity_id = ?""",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown supplier entity: " + entity_id)
        return Entity(*row)

    def entities(self) -> list[Entity]:
        rows = self.connection.execute(
            """SELECT entity_id, display_name, display_norm, created_at, created_by
               FROM supplier_entities ORDER BY entity_id"""
        ).fetchall()
        return [Entity(*row) for row in rows]

    # ------------------------------------------------------------------
    # alias claims

    def register_alias(
        self,
        alias: str,
        entity_id: str,
        basis: str = "handover",
        source_ref: str = "handover",
        confirmed: bool = True,
    ) -> AliasClaim:
        """Record an explicit alias claim.

        Repeated registration of the same ``(alias, entity, source_ref, basis)``
        returns the original claim and adds nothing.  Registering the same alias
        for two different entities is allowed and is exactly how a conflict is
        represented.
        """
        self.get_entity(entity_id)
        alias_raw = alias.strip()
        alias_norm = normalize_supplier_name(alias_raw)
        if not alias_norm:
            raise ValueError("alias must not be empty")
        status = "confirmed" if confirmed else "unverified"
        self.connection.execute(
            """INSERT OR IGNORE INTO supplier_aliases
               (alias_raw, alias_norm, entity_id, basis, source_ref, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (alias_raw, alias_norm, entity_id, basis, source_ref, status, utc_now()),
        )
        self.connection.commit()
        row = self.connection.execute(
            """SELECT claim_id, alias_raw, alias_norm, entity_id, basis, source_ref,
                      status, created_at, revoked_at
               FROM supplier_aliases
               WHERE alias_norm = ? AND entity_id = ? AND source_ref = ? AND basis = ?
               ORDER BY claim_id LIMIT 1""",
            (alias_norm, entity_id, source_ref, basis),
        ).fetchone()
        return AliasClaim(*row)

    def alias_claims(self) -> list[AliasClaim]:
        rows = self.connection.execute(
            """SELECT claim_id, alias_raw, alias_norm, entity_id, basis, source_ref,
                      status, created_at, revoked_at
               FROM supplier_aliases ORDER BY claim_id"""
        ).fetchall()
        return [AliasClaim(*row) for row in rows]

    # ------------------------------------------------------------------
    # observations

    def observe(self, raw_name: str, source_ref: str, auto: bool = True) -> Observation:
        """Record one raw supplier name and (by default) evaluate it.

        The observation is keyed by ``(source_ref, raw_name)`` so re-reading the
        same fixture/handover material is a no-op.
        """
        if not source_ref:
            raise ValueError("source_ref must not be empty")
        raw_name = str(raw_name)
        normalized = normalize_supplier_name(raw_name)
        content_key = _sha256(f"{source_ref}\x00{raw_name}")
        row = self.connection.execute(
            """SELECT observation_id FROM supplier_observations
               WHERE source_ref = ? AND raw_name = ?""",
            (source_ref, raw_name),
        ).fetchone()
        if row is None:
            cursor = self.connection.execute(
                """INSERT INTO supplier_observations
                   (raw_name, normalized_name, source_ref, content_key, observed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (raw_name, normalized, source_ref, content_key, utc_now()),
            )
            self.connection.commit()
            observation_id = int(cursor.lastrowid)
        else:
            observation_id = int(row[0])
        observation = self.get_observation(observation_id)
        if auto:
            self.evaluate(observation_id)
        return observation

    def get_observation(self, observation_id: int) -> Observation:
        row = self.connection.execute(
            """SELECT observation_id, raw_name, normalized_name, source_ref,
                      content_key, observed_at, created_entity_id
               FROM supplier_observations WHERE observation_id = ?""",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown supplier observation: " + str(observation_id))
        return Observation(*row)

    def observations(self) -> list[Observation]:
        rows = self.connection.execute(
            """SELECT observation_id, raw_name, normalized_name, source_ref,
                      content_key, observed_at, created_entity_id
               FROM supplier_observations ORDER BY observation_id"""
        ).fetchall()
        return [Observation(*row) for row in rows]

    # ------------------------------------------------------------------
    # decisions and evidence (audit trail)

    def current_decision(self, observation_id: int) -> Decision | None:
        row = self.connection.execute(
            """SELECT decision_id, observation_id, state, entity_id, reason,
                      evidence_ids, decided_by, decided_at, is_hold,
                      supersedes_decision_id, superseded_by_decision_id,
                      revoked_at, revoked_by
               FROM supplier_decisions
               WHERE observation_id = ?
                 AND superseded_by_decision_id IS NULL
                 AND revoked_at IS NULL
               ORDER BY decision_id DESC LIMIT 1""",
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        return self._decision_from_row(row)

    def decisions_for(self, observation_id: int) -> list[Decision]:
        rows = self.connection.execute(
            """SELECT decision_id, observation_id, state, entity_id, reason,
                      evidence_ids, decided_by, decided_at, is_hold,
                      supersedes_decision_id, superseded_by_decision_id,
                      revoked_at, revoked_by
               FROM supplier_decisions WHERE observation_id = ?
               ORDER BY decision_id""",
            (observation_id,),
        ).fetchall()
        return [self._decision_from_row(row) for row in rows]

    def evidence_for(self, observation_id: int) -> list[Evidence]:
        rows = self.connection.execute(
            """SELECT evidence_id, observation_id, entity_id, kind, strength,
                      detail, claim_id, source_ref, created_at
               FROM supplier_evidence WHERE observation_id = ?
               ORDER BY evidence_id""",
            (observation_id,),
        ).fetchall()
        return [self._evidence_from_row(row) for row in rows]

    @staticmethod
    def _decision_from_row(row: tuple) -> Decision:
        (decision_id, observation_id, state, entity_id, reason, evidence_ids,
         decided_by, decided_at, is_hold, supersedes_decision_id,
         superseded_by_decision_id, revoked_at, revoked_by) = row
        return Decision(
            decision_id=decision_id,
            observation_id=observation_id,
            state=state,
            entity_id=entity_id or None,
            reason=reason,
            evidence_ids=tuple(json.loads(evidence_ids or "[]")),
            decided_by=decided_by,
            decided_at=decided_at,
            is_hold=bool(is_hold),
            supersedes_decision_id=supersedes_decision_id,
            superseded_by_decision_id=superseded_by_decision_id,
            revoked_at=revoked_at,
            revoked_by=revoked_by,
        )

    @staticmethod
    def _evidence_from_row(row: tuple) -> Evidence:
        (evidence_id, observation_id, entity_id, kind, strength,
         detail, claim_id, source_ref, created_at) = row
        return Evidence(
            evidence_id=evidence_id,
            observation_id=observation_id,
            entity_id=entity_id or None,
            kind=kind,
            strength=strength,
            detail=detail,
            claim_id=claim_id or None,
            source_ref=source_ref,
            created_at=created_at,
        )

    # ------------------------------------------------------------------
    # automatic resolution

    def evaluate(self, observation_id: int, force: bool = False) -> Decision:
        """Evaluate an observation and return its current decision.

        A manual decision is sticky: automatic re-evaluation will not overwrite
        it.  This is what makes revocation meaningful -- revoking an automatic
        decision creates a manual ``pending`` hold until a human disposes of it.
        """
        observation = self.get_observation(observation_id)
        active = self.current_decision(observation_id)
        if active and active.decided_by != AUTO_DECIDER and not force:
            return active

        norm = observation.normalized_name

        if not norm:
            return self._settle(
                observation,
                PENDING,
                None,
                "empty_supplier_name",
                [],
            )

        claims = self._confirmed_claims(norm)
        claim_entities = sorted({claim.entity_id for claim in claims})
        bindings = self._resolved_bindings(observation)
        display_matches = self._display_name_matches(observation)
        known_entities = set(bindings)
        if observation.created_entity_id is not None:
            known_entities.add(observation.created_entity_id)
        known_entities.update(display_matches)

        if len(claim_entities) > 1:
            evidence = [
                self._add_evidence(
                    observation,
                    claim.entity_id,
                    "conflicting_alias_claim",
                    "strong",
                    f"{claim.basis} via {claim.source_ref}",
                    claim.claim_id,
                )
                for claim in claims
            ]
            return self._settle(
                observation,
                CONFLICT,
                None,
                "conflicting_alias_claims",
                evidence,
            )

        if len(claim_entities) == 1:
            claimed = claim_entities[0]
            evidence = [
                self._add_evidence(
                    observation,
                    claim.entity_id,
                    "confirmed_alias",
                    "strong",
                    f"{claim.basis} via {claim.source_ref}",
                    claim.claim_id,
                )
                for claim in claims
            ]
            conflicting = sorted(known_entities - {claimed})
            if conflicting:
                evidence.extend(
                    self._add_evidence(
                        observation,
                        entity_id,
                        "conflicting_name_binding",
                        "strong",
                        "alias claim disagrees with an existing name binding",
                    )
                    for entity_id in conflicting
                )
                return self._settle(
                    observation,
                    CONFLICT,
                    None,
                    "alias_claim_conflicts_with_existing_binding",
                    evidence,
                )
            return self._settle(
                observation,
                RESOLVED,
                claimed,
                "confirmed_alias_claim",
                evidence,
            )

        if len(display_matches) > 1:
            evidence = [
                self._add_evidence(
                    observation,
                    entity_id,
                    "conflicting_entity_display_name",
                    "strong",
                    "same normalized name matches multiple entity display names",
                )
                for entity_id in display_matches
            ]
            return self._settle(
                observation,
                CONFLICT,
                None,
                "conflicting_entity_display_names",
                evidence,
            )

        if observation.created_entity_id is not None:
            evidence = [
                self._add_evidence(
                    observation,
                    observation.created_entity_id,
                    "new_entity",
                    "strong",
                    "entity created for this observation",
                )
            ]
            return self._settle(
                observation,
                RESOLVED,
                observation.created_entity_id,
                "new_entity_no_similar_existing",
                evidence,
            )

        if len(bindings) > 1:
            evidence = [
                self._add_evidence(
                    observation,
                    entity_id,
                    "conflicting_name_binding",
                    "strong",
                    "same normalized name already resolved to multiple entities",
                )
                for entity_id in bindings
            ]
            return self._settle(
                observation,
                CONFLICT,
                None,
                "conflicting_name_bindings",
                evidence,
            )

        if len(bindings) == 1:
            evidence = [
                self._add_evidence(
                    observation,
                    bindings[0],
                    "case_variant",
                    "strong",
                    "exact normalized-name match with an existing resolved observation",
                )
            ]
            return self._settle(
                observation,
                RESOLVED,
                bindings[0],
                "case_variant_of_existing_name",
                evidence,
            )

        if len(display_matches) == 1:
            evidence = [
                self._add_evidence(
                    observation,
                    display_matches[0],
                    "entity_display_name",
                    "strong",
                    "exact normalized match with registered entity display name",
                )
            ]
            return self._settle(
                observation,
                RESOLVED,
                display_matches[0],
                "entity_display_name_match",
                evidence,
            )

        similar = self._similar_entities(norm, exclude_entity_id=None)
        if similar:
            evidence = [
                self._add_evidence(
                    observation,
                    entity_id,
                    "similar_name",
                    "weak",
                    f"name similarity {score:.3f}; requires human review",
                )
                for entity_id, score in similar
            ]
            return self._settle(
                observation,
                PENDING,
                None,
                "similar_name_requires_review",
                evidence,
            )

        created = self.create_entity(
            f"sup_{uuid.uuid4().hex[:16]}",
            observation.raw_name.strip(),
            created_by=AUTO_DECIDER,
            created_from_observation_id=observation.observation_id,
        )
        self.connection.execute(
            "UPDATE supplier_observations SET created_entity_id = ? WHERE observation_id = ?",
            (created.entity_id, observation.observation_id),
        )
        self.connection.commit()
        observation = self.get_observation(observation.observation_id)
        evidence = [
            self._add_evidence(
                observation,
                created.entity_id,
                "new_entity",
                "strong",
                "no existing claim, binding or similar name",
            )
        ]
        return self._settle(
            observation,
            RESOLVED,
            created.entity_id,
            "new_entity_no_similar_existing",
            evidence,
        )

    def _settle(
        self,
        observation: Observation,
        state: str,
        entity_id: str | None,
        reason: str,
        evidence_ids: Iterable[int],
        *,
        decided_by: str = AUTO_DECIDER,
        is_hold: bool = False,
        supersedes_decision_id: int | None = None,
    ) -> Decision:
        evidence_tuple = tuple(sorted(int(e) for e in evidence_ids))
        fingerprint = _sha256(
            json.dumps(
                [
                    observation.observation_id,
                    state,
                    entity_id or "",
                    reason,
                    list(evidence_tuple),
                    decided_by,
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        active = self.current_decision(observation.observation_id)
        if (
            active
            and active.decided_by == decided_by
            and self._fingerprint_of(active.decision_id) == fingerprint
        ):
            return active

        now = utc_now()
        cursor = self.connection.execute(
            """INSERT INTO supplier_decisions
               (observation_id, state, entity_id, reason, evidence_ids,
                decided_by, decided_at, is_hold, fingerprint,
                supersedes_decision_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation.observation_id,
                state,
                entity_id or "",
                reason,
                json.dumps(list(evidence_tuple)),
                decided_by,
                now,
                int(is_hold),
                fingerprint,
                supersedes_decision_id,
            ),
        )
        new_id = int(cursor.lastrowid)
        if active and active.decision_id != supersedes_decision_id:
            self.connection.execute(
                "UPDATE supplier_decisions SET superseded_by_decision_id = ? WHERE decision_id = ?",
                (new_id, active.decision_id),
            )
        if supersedes_decision_id is not None:
            self.connection.execute(
                """UPDATE supplier_decisions
                   SET superseded_by_decision_id = COALESCE(superseded_by_decision_id, ?)
                   WHERE decision_id = ?""",
                (new_id, supersedes_decision_id),
            )
        self.connection.commit()
        return self._get_decision(new_id)

    def _fingerprint_of(self, decision_id: int) -> str:
        row = self.connection.execute(
            "SELECT fingerprint FROM supplier_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return row[0] if row else ""

    def _get_decision(self, decision_id: int) -> Decision:
        row = self.connection.execute(
            """SELECT decision_id, observation_id, state, entity_id, reason,
                      evidence_ids, decided_by, decided_at, is_hold,
                      supersedes_decision_id, superseded_by_decision_id,
                      revoked_at, revoked_by
               FROM supplier_decisions WHERE decision_id = ?""",
            (decision_id,),
        ).fetchone()
        if row is None:
            raise KeyError("unknown decision: " + str(decision_id))
        return self._decision_from_row(row)

    # ------------------------------------------------------------------
    # human disposition

    def revoke(self, decision_id: int, revoked_by: str = "manual", note: str = "") -> Decision:
        """Revoke a decision, preserving the full audit history.

        If the revoked decision was the current one, a manual ``pending`` hold
        is created so that automatic evaluation cannot silently recreate the
        same decision.  A human must dispose of the observation afterwards.
        """
        decision = self._get_decision(decision_id)
        if decision.revoked_at is not None:
            current = self.current_decision(decision.observation_id)
            if current:
                return current
            return decision

        was_current = decision.is_current
        self.connection.execute(
            """UPDATE supplier_decisions
               SET revoked_at = ?, revoked_by = ?
               WHERE decision_id = ?""",
            (utc_now(), revoked_by, decision_id),
        )
        self.connection.commit()

        if not was_current:
            current = self.current_decision(decision.observation_id)
            return current if current else decision

        observation = self.get_observation(decision.observation_id)
        return self._settle(
            observation,
            PENDING,
            None,
            "revoked_decision_requires_review",
            (),
            decided_by="manual",
            is_hold=True,
            supersedes_decision_id=decision_id,
        )

    def resolve_manually(
        self,
        observation_id: int,
        state: str,
        entity_id: str | None = None,
        decided_by: str = "human",
        note: str = "",
    ) -> Decision:
        """Record a human disposition for a pending/conflict observation."""
        if state not in VALID_STATES:
            raise ValueError(f"state must be one of {VALID_STATES}")
        if state == RESOLVED:
            if not entity_id:
                raise ValueError("a resolved decision needs an entity_id")
            self.get_entity(entity_id)
        observation = self.get_observation(observation_id)
        evidence_ids = ()
        active = self.current_decision(observation_id)
        if active:
            evidence_ids = active.evidence_ids
        return self._settle(
            observation,
            state,
            entity_id,
            note or "manual_decision",
            evidence_ids,
            decided_by=decided_by,
            is_hold=state != RESOLVED,
        )

    # ------------------------------------------------------------------
    # lookups used by the resolver

    def _confirmed_claims(self, norm: str) -> list[AliasClaim]:
        rows = self.connection.execute(
            """SELECT claim_id, alias_raw, alias_norm, entity_id, basis, source_ref,
                      status, created_at, revoked_at
               FROM supplier_aliases
               WHERE alias_norm = ? AND status = 'confirmed' AND revoked_at IS NULL
               ORDER BY claim_id""",
            (norm,),
        ).fetchall()
        return [AliasClaim(*row) for row in rows]

    def _resolved_bindings(self, observation: Observation) -> list[str]:
        rows = self.connection.execute(
            """SELECT DISTINCT d.entity_id
               FROM supplier_decisions d
               JOIN supplier_observations o ON o.observation_id = d.observation_id
               WHERE o.normalized_name = ?
                 AND o.observation_id <> ?
                 AND d.state = 'resolved'
                 AND d.entity_id <> ''
                 AND d.revoked_at IS NULL
                 AND d.superseded_by_decision_id IS NULL
               ORDER BY d.entity_id""",
            (observation.normalized_name, observation.observation_id),
        ).fetchall()
        return [row[0] for row in rows]

    def _display_name_matches(self, observation: Observation) -> list[str]:
        rows = self.connection.execute(
            """SELECT entity_id FROM supplier_entities
               WHERE display_norm = ?
                 AND (created_from_observation_id IS NULL
                      OR created_from_observation_id <> ?)
               ORDER BY entity_id""",
            (observation.normalized_name, observation.observation_id),
        ).fetchall()
        return [row[0] for row in rows]

    def _similar_entities(
        self, norm: str, exclude_entity_id: str | None = None
    ) -> list[tuple[str, float]]:
        if len(norm) < self.MIN_SIMILARITY_LENGTH:
            return []
        candidates: dict[str, float] = {}
        for entity in self.entities():
            if entity.entity_id == exclude_entity_id:
                continue
            score = _name_similarity(norm, entity.display_norm)
            if score >= self.SIMILARITY_THRESHOLD:
                candidates[entity.entity_id] = max(
                    candidates.get(entity.entity_id, 0.0), score
                )
        for claim in self.alias_claims():
            if claim.revoked_at is not None or claim.status != "confirmed":
                continue
            if claim.entity_id == exclude_entity_id:
                continue
            score = _name_similarity(norm, claim.alias_norm)
            if score >= self.SIMILARITY_THRESHOLD:
                candidates[claim.entity_id] = max(
                    candidates.get(claim.entity_id, 0.0), score
                )
        return sorted(candidates.items())

    def _add_evidence(
        self,
        observation: Observation,
        entity_id: str | None,
        kind: str,
        strength: str,
        detail: str = "",
        claim_id: int | None = None,
    ) -> int:
        self.connection.execute(
            """INSERT OR IGNORE INTO supplier_evidence
               (observation_id, entity_id, kind, strength, detail, claim_id,
                source_ref, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation.observation_id,
                entity_id or "",
                kind,
                strength,
                detail,
                claim_id or 0,
                observation.source_ref,
                utc_now(),
            ),
        )
        row = self.connection.execute(
            """SELECT evidence_id FROM supplier_evidence
               WHERE observation_id = ? AND entity_id = ? AND kind = ? AND claim_id = ?""",
            (observation.observation_id, entity_id or "", kind, claim_id or 0),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------
    # reporting

    def report(self) -> dict:
        """Return the whole registry as JSON-serialisable audit data."""
        observations = []
        for observation in self.observations():
            decision = self.current_decision(observation.observation_id)
            observations.append(
                {
                    "observation_id": observation.observation_id,
                    "raw_name": observation.raw_name,
                    "normalized_name": observation.normalized_name,
                    "source_ref": observation.source_ref,
                    "created_entity_id": observation.created_entity_id,
                    "current_decision": _decision_dict(decision),
                    "decision_history": [
                        _decision_dict(item)
                        for item in self.decisions_for(observation.observation_id)
                    ],
                    "evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "entity_id": item.entity_id,
                            "kind": item.kind,
                            "strength": item.strength,
                            "detail": item.detail,
                            "claim_id": item.claim_id,
                            "source_ref": item.source_ref,
                        }
                        for item in self.evidence_for(observation.observation_id)
                    ],
                }
            )
        return {
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "display_name": entity.display_name,
                    "created_by": entity.created_by,
                }
                for entity in self.entities()
            ],
            "aliases": [
                {
                    "claim_id": claim.claim_id,
                    "alias": claim.alias_raw,
                    "alias_norm": claim.alias_norm,
                    "entity_id": claim.entity_id,
                    "basis": claim.basis,
                    "source_ref": claim.source_ref,
                    "status": claim.status,
                    "revoked_at": claim.revoked_at,
                }
                for claim in self.alias_claims()
            ],
            "observations": observations,
        }


def _decision_dict(decision: Decision | None) -> dict | None:
    if decision is None:
        return None
    return {
        "decision_id": decision.decision_id,
        "state": decision.state,
        "entity_id": decision.entity_id,
        "reason": decision.reason,
        "evidence_ids": list(decision.evidence_ids),
        "decided_by": decision.decided_by,
        "decided_at": decision.decided_at,
        "is_hold": decision.is_hold,
        "supersedes_decision_id": decision.supersedes_decision_id,
        "superseded_by_decision_id": decision.superseded_by_decision_id,
        "revoked_at": decision.revoked_at,
        "revoked_by": decision.revoked_by,
        "is_current": decision.is_current,
    }
