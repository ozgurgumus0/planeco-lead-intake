import hmac
import json
import re
import secrets
from collections import Counter
from dataclasses import asdict, dataclass, field

from .events import (
    CREATE, DELETE, UPDATE, FIELDS, ID_PATTERN, ROUTING_FIELDS,
    Delivery, Event, parse_phone, trusted_phone, validate,
)
from .features import MISSING, category, from_intake
from .files import json_text, parse_time, utc_text
from .routing import HIGH_SCORE, Assignment, assign, still_eligible
from .state import record_arrival

CRM_FIELDS = (
    "external_id", "created_at_utc",
    *(key for key in FIELDS if key != "message"),
    "owner_rep_id", "assigned_at_lisbon", "lead_score", "expected_value_eur",
    "assignment_reason", "merged_external_ids",
)
REJECTED_FIELDS = ("event_id", "external_id", "reason")
DISPOSITIONS = (
    "duplicate_deliveries", "rejected_deliveries", "canonical_created", "merged_creates",
    "patches_applied", "patches_ignored", "patches_deferred", "patches_discarded",
    "leads_deleted", "deletions_for_unseen_or_deleted_id",
)
EARLIEST_CLOCK = ("", -1, "")


@dataclass
class Lead:
    external_id: str
    created_at_utc: str
    fields: dict
    clocks: dict
    aliases: list[str] = field(default_factory=list)
    probability: float = 0.0
    expected_value: float = 0.0
    model_version: str = ""
    routing_changed_at: str = ""
    assignment: Assignment | None = None

    def crm_row(self) -> dict:
        assert self.assignment is not None
        return {
            "external_id": self.external_id, "created_at_utc": self.created_at_utc,
            **{key: self.fields.get(key, "") for key in FIELDS if key != "message"},
            "owner_rep_id": self.assignment.owner,
            "assigned_at_lisbon": self.assignment.lisbon_text,
            "lead_score": f"{self.probability:.6f}",
            "expected_value_eur": f"{self.expected_value:.2f}",
            "assignment_reason": self.assignment.reason,
            "merged_external_ids": "|".join(sorted(self.aliases)),
        }

    @property
    def route_since(self):
        created = parse_time(self.created_at_utc)
        return max(created, parse_time(self.routing_changed_at)) if self.routing_changed_at else created


def contact_keys(fields: dict) -> set[tuple[str, str]]:
    keys = {("email", fields["email"])}
    phone = trusted_phone(fields.get("phone", ""))
    if phone:
        keys.add(("phone", phone))
    return keys


def same_name(first: dict, second: dict) -> bool:
    return all(first[key].casefold() == second[key].casefold() for key in ("first_name", "last_name"))


class Intake:
    """Apply one batch while the caller holds the SQLite write transaction."""

    def __init__(self, conn):
        self.conn = conn
        self.leads = {}
        self.identities = {}
        self.contact_owners = {}
        self.tombstones = {row["external_id"] for row in conn.execute("SELECT external_id FROM tombstones")}
        self.counters = Counter(dict.fromkeys(DISPOSITIONS, 0))
        self.previous_patches = Counter(dict.fromkeys(("applied", "ignored", "rejected", "discarded"), 0))
        self.rejected = []
        self.deferred_here = set()
        self.touched = set()
        for row in conn.execute("SELECT record FROM leads"):
            record = json.loads(row["record"])
            record["assignment"] = Assignment(**record["assignment"])
            lead = Lead(**record)
            self.leads[lead.external_id] = lead
            for key in (lead.external_id, *lead.aliases):
                self.identities[key] = lead.external_id
            self.index_contacts(lead)
        self.before = {key: lead.crm_row() for key, lead in self.leads.items()}
        self.old_features = {key: from_intake(lead.fields) for key, lead in self.leads.items()}
        self.watermark = self.setting("event_watermark")
        secret = self.setting("receipt_key")
        if secret is None:
            secret = secrets.token_hex(32)
            self.set_setting("receipt_key", secret)
        self.secret = bytes.fromhex(secret)

    def setting(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT (name) DO UPDATE SET value = excluded.value",
            (name, value),
        )

    def index_contacts(self, lead: Lead) -> None:
        for key in contact_keys(lead.fields):
            self.contact_owners[key] = lead.external_id

    def drop_contacts(self, lead: Lead) -> None:
        for key in contact_keys(lead.fields):
            self.contact_owners.pop(key)

    def reject(self, row: Delivery | Event, reasons: list[str]) -> str:
        self.rejected.append({
            "event_id": row.event_id,
            "external_id": row.external_id,
            "reason": "; ".join(reasons),
        })
        return "rejected_deliveries"

    def receive(self, delivery: Delivery) -> Event | None:
        event_id = delivery.event_id
        if ID_PATTERN.fullmatch(event_id):
            content = json_text({
                "columns": {key: value for key, value in delivery.raw.items() if value != ""},
                "extra": delivery.extra,
                "shape_error": delivery.problem,
            }).encode()
            fingerprint = hmac.digest(self.secret, content, "sha256").hex()
            previous = self.conn.execute("SELECT fingerprint FROM events WHERE event_id = ?", (event_id,)).fetchone()
            if previous is not None:
                if previous["fingerprint"] == fingerprint:
                    self.counters["duplicate_deliveries"] += 1
                else:
                    self.counters[self.reject(delivery, ["conflicting payload for previously seen event_id"])] += 1
                return None
            self.conn.execute("INSERT INTO events VALUES (?, ?)", (event_id, fingerprint))
        event, reasons = validate(delivery)
        if reasons:
            self.counters[self.reject(delivery, reasons)] += 1
        return event

    def create(self, event: Event) -> str:
        fields = event.fields
        by_id = self.identities.get(event.external_id)
        by_email = self.contact_owners.get(("email", fields["email"]))
        phone_key = trusted_phone(fields.get("phone", ""))
        by_phone = self.contact_owners.get(("phone", phone_key))
        owners = {owner for owner in (by_id, by_email, by_phone) if owner}
        if len(owners) > 1:
            return self.reject(event, ["identity conflict: ID, email or phone refer to different leads"])
        if not owners:
            lead = Lead(event.external_id, utc_text(event.at), fields.copy(),
                        dict.fromkeys(fields, event.clock))
            self.leads[lead.external_id] = lead
            record_arrival(self.conn, event.at, fields)
            outcome = "canonical_created"
        else:
            lead = self.leads[owners.pop()]
            if by_id and not (by_email or by_phone):
                return self.reject(event, ["identity conflict: existing ID has no matching email or trusted phone"])
            if not (by_id or by_email) and not same_name(lead.fields, fields):
                return self.reject(event, ["identity conflict: name differs on a phone match without email agreement"])
            outcome = "merged_creates"
            if not by_id:
                lead.aliases.append(event.external_id)
            for key in ("phone", "message"):
                if key in fields and not lead.fields.get(key):
                    lead.fields[key] = fields[key]
                    lead.clocks[key] = event.clock
        self.index_contacts(lead)
        self.identities[event.external_id] = lead.external_id
        self.touched.add(lead.external_id)
        self.resolve_patches(event.external_id, lead)
        return outcome

    def patch(self, event: Event, lead: Lead) -> str:
        changes = {
            key: value for key, value in event.fields.items()
            if event.clock > tuple(lead.clocks.get(key, EARLIEST_CLOCK))
        }
        if not changes:
            return "patches_ignored"
        fields = {**lead.fields, **changes}
        errors = []
        if fields["country"] == "DE" and not re.fullmatch(r"[0-9]{5}", fields["postcode"]):
            errors.append("invalid German postcode (expected five digits)")
        if "phone" in changes:
            try:
                fields["phone"] = parse_phone(fields["phone"], fields["country"])
            except ValueError as exc:
                errors.append(str(exc))
        if not errors:
            new_owners = {self.contact_owners.get(key) for key in contact_keys(fields)}
            if new_owners - {None, lead.external_id}:
                errors.append("identity conflict: patched email or phone belongs to another lead")
        if errors:
            return self.reject(event, errors)
        changed = fields != lead.fields
        if changed:
            if any(fields[key] != lead.fields[key] for key in ROUTING_FIELDS):
                lead.routing_changed_at = max(lead.routing_changed_at, event.clock[0])
            self.drop_contacts(lead)
            lead.fields = fields
            self.index_contacts(lead)
            self.touched.add(lead.external_id)
        lead.clocks.update(dict.fromkeys(changes, event.clock))
        return "patches_applied" if changed else "patches_ignored"

    def update(self, event: Event) -> str:
        if event.external_id in self.identities:
            return self.patch(event, self.leads[self.identities[event.external_id]])
        if not event.fields:
            return "patches_ignored"
        self.conn.execute(
            "INSERT INTO pending_patches VALUES (?, ?, ?, ?)",
            (event.event_id, event.external_id, event.clock[0], json_text(event.fields)),
        )
        self.deferred_here.add(event.event_id)
        return "patches_deferred"

    def resolve_patches(self, external_id: str, lead: Lead | None) -> None:
        pending = self.conn.execute("""
            SELECT * FROM pending_patches WHERE external_id = ? ORDER BY occurred_at, event_id
        """, (external_id,)).fetchall()
        self.conn.execute("DELETE FROM pending_patches WHERE external_id = ?", (external_id,))
        previous = {
            "patches_discarded": "discarded", "patches_applied": "applied",
            "patches_ignored": "ignored", "rejected_deliveries": "rejected",
        }
        for row in pending:
            event_id = row["event_id"]
            outcome = "patches_discarded"
            if lead is not None:
                event = Event(event_id, UPDATE, external_id, parse_time(row["occurred_at"]), json.loads(row["fields"]))
                outcome = self.patch(event, lead)
            if event_id in self.deferred_here:
                self.deferred_here.remove(event_id)
                self.counters["patches_deferred"] -= 1
                self.counters[outcome] += 1
            else:
                self.previous_patches[previous[outcome]] += 1

    def delete(self, event: Event) -> str:
        canonical = self.identities.get(event.external_id)
        ids = [event.external_id]
        if canonical is not None:
            lead = self.leads.pop(canonical)
            self.drop_contacts(lead)
            self.conn.execute("DELETE FROM leads WHERE external_id = ?", (canonical,))
            ids = [canonical, *lead.aliases]
        for external_id in ids:
            self.identities.pop(external_id, None)
            self.tombstones.add(external_id)
            self.resolve_patches(external_id, None)
            self.conn.execute("""
                INSERT INTO tombstones VALUES (?, ?)
                ON CONFLICT (external_id) DO UPDATE SET deleted_at = max(deleted_at, excluded.deleted_at)
            """, (external_id, event.clock[0]))
        return "leads_deleted" if canonical is not None else "deletions_for_unseen_or_deleted_id"

    def ingest(self, deliveries: list[Delivery]) -> None:
        events = [event for delivery in deliveries if (event := self.receive(delivery)) is not None]
        for event in sorted(events, key=lambda event: event.clock):
            if event.kind == DELETE:
                outcome = self.delete(event)
            elif event.external_id in self.tombstones:
                outcome = self.reject(event, ["external_id is deleted"])
            elif event.kind == CREATE:
                outcome = self.create(event)
            else:
                outcome = self.update(event)
            self.counters[outcome] += 1
            if outcome != "rejected_deliveries":
                self.watermark = max(self.watermark or "", event.clock[0])
        if self.watermark:
            self.set_setting("event_watermark", self.watermark)
        assert sum(self.counters.values()) == len(deliveries)

    def finish(self, roster, scorer) -> tuple[list[dict], dict, dict]:
        by_arrival = sorted(self.leads.values(), key=lambda lead: (parse_time(lead.created_at_utc), lead.external_id))
        monitored = {lead.external_id: from_intake(lead.fields) for lead in by_arrival if lead.external_id in self.touched}
        rescore = [
            lead for lead in by_arrival
            if lead.external_id in monitored and monitored[lead.external_id] != self.old_features.get(lead.external_id)
        ]
        scores = scorer.predict([monitored[lead.external_id] for lead in rescore])
        for lead, score in zip(rescore, scores, strict=True):
            lead.probability = score.probability
            lead.expected_value = score.expected_value
            lead.model_version = scorer.version
        loads = Counter(dict.fromkeys(roster, 0))
        retained = set()
        prior_active = [lead for lead in by_arrival if lead.assignment and lead.assignment.active]
        for lead in sorted(prior_active, key=lambda item: (parse_time(item.assignment.at_utc), item.external_id)):
            assignment = lead.assignment
            assert assignment is not None
            if still_eligible(assignment, lead.fields, roster, loads):
                retained.add(lead.external_id)
                loads[assignment.owner] += 1
                if any(self.before[lead.external_id][key] != lead.fields[key] for key in ROUTING_FIELDS):
                    lead.assignment = Assignment(
                        assignment.owner, assignment.at_utc,
                        f"{lead.fields['service']} + {lead.fields['language']} | prior assignment still eligible",
                    )
        for lead in by_arrival:
            if lead.external_id not in retained:
                arrival = lead.route_since
                reconsidered = lead.external_id in self.before
                if reconsidered:
                    arrival = max(arrival, parse_time(self.watermark))
                lead.assignment = assign(
                    lead.fields, lead.probability, arrival, roster, loads, reconsidered=reconsidered
                )
                if lead.assignment.active:
                    loads[lead.assignment.owner] += 1

        new_rows = []
        actions = dict.fromkeys(sorted(self.before.keys() - self.leads.keys()))
        output_queue = 0
        for lead in by_arrival:
            exported = self.before.get(lead.external_id)
            row = lead.crm_row()
            if exported is None:
                new_rows.append(row)
                output_queue += row["assignment_reason"] == "queue"
            elif row != exported:
                actions[lead.external_id] = row
        self.conn.executemany("""
            INSERT INTO leads VALUES (?, ?)
            ON CONFLICT (external_id) DO UPDATE SET record = excluded.record WHERE record <> excluded.record
        """, ((lead.external_id, json_text(asdict(lead))) for lead in by_arrival))
        probabilities = [self.leads[key].probability for key in monitored]
        queued = [lead for lead in by_arrival if not lead.assignment.active]
        without_coverage = sum(not any(rep.covers(lead.fields) for rep in roster.values()) for lead in queued)
        health = {
            "n": len(monitored),
            "newly_scored": len(rescore),
            "min": min(probabilities) if probabilities else None,
            "mean": sum(probabilities) / len(probabilities) if probabilities else None,
            "max": max(probabilities) if probabilities else None,
            "bands": {
                "below 0.10": sum(p < 0.10 for p in probabilities),
                "0.10 to below 0.25": sum(0.10 <= p < 0.25 for p in probabilities),
                "0.25 and above": sum(p >= 0.25 for p in probabilities),
            },
            "score_versions": dict(Counter(self.leads[key].model_version for key in monitored)),
            "missing_or_unknown": {
                **scorer.feature_health(list(monitored.values())),
                "utm_source absent before fallback": sum(
                    category(self.leads[key].fields.get("utm_source")) == MISSING for key in monitored
                ),
            },
        }
        summary = {
            "deliveries": sum(self.counters.values()),
            "events": dict(self.counters),
            "previously_deferred_patches": dict(self.previous_patches),
            "rejection_rows": len(self.rejected),
            "leads_before": len(self.before),
            "leads_after": len(self.leads),
            "crm_rows": len(new_rows),
            "crm_rows_queued": output_queue,
            "crm_updates": sum(value is not None for value in actions.values()),
            "crm_deletes": sum(value is None for value in actions.values()),
            "pending_patches": self.conn.execute("SELECT count(*) FROM pending_patches").fetchone()[0],
            "active_assigned": sum(loads.values()),
            "queued": len(queued),
            "queue_without_coverage": without_coverage,
            "queue_at_capacity": len(queued) - without_coverage,
            "queue_high_score": sum(lead.probability >= HIGH_SCORE for lead in queued),
            "capacity": {rep_id: {"open": loads[rep_id], "max": rep.maximum} for rep_id, rep in roster.items()},
            "model_version": scorer.version,
            "scoring": health,
        }
        assert summary["leads_before"] + self.counters["canonical_created"] - self.counters["leads_deleted"] == summary["leads_after"]
        assert len(self.rejected) == self.counters["rejected_deliveries"] + self.previous_patches["rejected"]
        assert summary["active_assigned"] + summary["queued"] == summary["leads_after"]
        return new_rows, actions, summary
