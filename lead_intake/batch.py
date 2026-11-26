import csv
import hashlib
import json
import os
import uuid
from pathlib import Path

from .events import read_deliveries
from .files import csv_text, json_text, sync_directory
from .intake import CRM_FIELDS, REJECTED_FIELDS, Intake
from .model import MODEL_FILE, load_model, train
from .routing import load_roster
from .state import open_state, record_crm_action, transaction

OUTPUTS = ("crm_import.csv", "rejected.csv", "run_report.md")


def verify_templates(data_dir: Path) -> None:
    for name, fields in (("crm_import_TEMPLATE.csv", CRM_FIELDS), ("rejected_TEMPLATE.csv", REJECTED_FIELDS)):
        with (data_dir / name).open(encoding="utf-8-sig", newline="") as file:
            if tuple(next(csv.reader(file), [])) != fields:
                raise ValueError(f"{name} headers do not match the supported schema")


def report(batch_id: str, summary: dict) -> str:
    changes = summary["events"]
    lines = [
        "# PlaneCo lead intake run", "",
        f"Batch ID **{batch_id}**. Scorer loaded for this run **{summary['model_version']}**.", "",
        "Publishing these files generates a CRM delta. CRM confirmation requires a separate acknowledgment.",
        "", "## Event dispositions", "",
        "| Disposition | Deliveries |", "|---|---:|",
        *(f"| {key.replace('_', ' ').capitalize()} | {value} |" for key, value in changes.items()),
        f"| **Total deliveries** | **{summary['deliveries']}** |",
        "",
    ]
    prior = summary["previously_deferred_patches"]
    lines += [
        "Previously deferred patches resolved in this run are outside the delivery total. "
        f"{prior['applied']} applied, {prior['ignored']} ignored, {prior['rejected']} rejected, "
        f"and {prior['discarded']} discarded after deletion.",
        "", "## Canonical leads and CRM handoff", "",
        f"- {summary['leads_before']} before + {changes['canonical_created']} created "
        f"− {changes['leads_deleted']} deleted = **{summary['leads_after']} live leads**.",
        f"- New CRM rows **{summary['crm_rows']}**. Queued among these **{summary['crm_rows_queued']}**.",
        f"- CRM update actions **{summary['crm_updates']}**. CRM delete actions **{summary['crm_deletes']}**.",
        f"- Rejected CSV rows **{summary['rejection_rows']}**. Patches awaiting a create **{summary['pending_patches']}**.",
        f"- {summary['active_assigned']} active assignments and {summary['queued']} queued leads. "
        "Queue entries reserve zero slots.",
        f"- The queue includes **{summary['queue_without_coverage']}** without a compatible rep and "
        f"**{summary['queue_at_capacity']}** with compatible reps at capacity. "
        f"**{summary['queue_high_score']}** score at least 0.25.",
        "", "| Rep | Open | Capacity |", "|---|---:|---:|",
        *(f"| {rep} | {counts['open']} | {counts['max']} |" for rep, counts in summary["capacity"].items()),
        "", "## Intake feature and score summary", "",
    ]
    health = summary["scoring"]
    n = health["n"]
    lines.append(f"{n} live leads touched by accepted creates or changing patches. {health['newly_scored']} scored.")
    if n:
        versions = ", ".join(f"{key} ({count})" for key, count in health["score_versions"].items())
        lines += [
            "", f"Minimum {health['min']:.3f}, mean {health['mean']:.3f}, maximum {health['max']:.3f}.",
            f"Versions on the monitored scores are {versions}.",
            "", "| Probability band | Leads |", "|---|---:|",
            *(f"| {band} | {count} |" for band, count in health["bands"].items()),
            "", "| Missing or unseen feature | Leads | Rate |", "|---|---:|---:|",
            *(f"| {key} | {value} | {value / n:.1%} |" for key, value in health["missing_or_unknown"].items()),
        ]
    return "\n".join([*lines, ""])


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def manifest_matches(destination: Path, manifest: dict) -> bool:
    return destination.is_dir() and all(
        (destination / name).is_file() and digest((destination / name).read_bytes()) == manifest[name]
        for name in OUTPUTS
    )


def require_empty(destination: Path) -> None:
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError(f"output destination already contains files: {destination}")


def publish_directory(destination: Path, batch_id: str, documents: dict, manifest: dict) -> None:
    if manifest_matches(destination, manifest):
        # A previous process published the directory before marking the batch published in SQLite.
        sync_directory(destination.parent)
        return
    require_empty(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.planeco-{batch_id}.tmp")
    if staging.is_symlink():
        raise ValueError(f"staging directory is a symbolic link: {staging}")
    staging.mkdir(exist_ok=True)
    if any(path.name not in OUTPUTS or not path.is_file() or path.is_symlink() for path in staging.iterdir()):
        raise ValueError(f"staging directory contains unexpected entries: {staging}")
    for name in OUTPUTS:
        with (staging / name).open("wb") as file:
            file.write(documents[name].encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
    sync_directory(staging)
    if destination.is_dir():
        destination.rmdir()
    staging.rename(destination)
    sync_directory(destination.parent)


def pending_export(conn):
    return conn.execute("SELECT * FROM exports WHERE published_at IS NULL").fetchone()


def publish_export(conn, job):
    destination = Path(job["destination"])
    try:
        publish_directory(destination, job["batch_id"], json.loads(job["documents"]), json.loads(job["manifest"]))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"batch {job['batch_id']} is committed but publication failed for {destination}. "
            f"Retry the run or use recover with the same state. {exc}"
        ) from exc
    conn.execute("""
        UPDATE exports SET published_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), documents = NULL
        WHERE batch_id = ?
    """, (job["batch_id"],))
    return conn.execute("SELECT * FROM exports WHERE batch_id = ?", (job["batch_id"],)).fetchone()


def export_info(job) -> dict:
    return {
        **json.loads(job["summary"]),
        "batch_id": job["batch_id"],
        "destination": job["destination"],
        "published_at": job["published_at"],
        "crm_confirmed_at": job["crm_confirmed_at"],
    }


def stage_export(conn, deliveries, fingerprint: str, destination: Path, data_dir: Path, model_dir: Path) -> str:
    verify_templates(data_dir)
    roster = load_roster(data_dir / "sales_team.csv")
    if (model_dir / MODEL_FILE).exists():
        scorer = load_model(model_dir)
    else:
        scorer = train(data_dir / "historical_leads.csv", model_dir)
    intake = Intake(conn)
    intake.ingest(deliveries)
    rows, changes, summary = intake.finish(roster, scorer)
    batch_id = uuid.uuid4().hex
    documents = {
        "crm_import.csv": csv_text(CRM_FIELDS, rows),
        "rejected.csv": csv_text(REJECTED_FIELDS, intake.rejected),
        "run_report.md": report(batch_id, summary),
    }
    manifest = {name: digest(content.encode("utf-8")) for name, content in documents.items()}
    conn.execute(
        "INSERT INTO exports (batch_id, input_fingerprint, destination, summary, manifest, documents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (batch_id, fingerprint, str(destination), json_text(summary), json_text(manifest), json_text(documents)),
    )
    for external_id, row in changes.items():
        record_crm_action(conn, batch_id, external_id, row)
    return batch_id


def run_batch(input_path: Path, data_dir: Path, state_path: Path, model_dir: Path, out_dir: Path) -> dict:
    destination = out_dir.resolve()
    content = input_path.read_bytes()
    fingerprint = digest(content)
    deliveries = read_deliveries(content)
    with open_state(state_path) as conn:
        while True:
            with transaction(conn):
                pending = pending_export(conn)
                if pending is None:
                    existing = conn.execute("""
                        SELECT * FROM exports WHERE input_fingerprint = ? AND destination = ? AND published_at IS NOT NULL
                        ORDER BY rowid DESC LIMIT 1
                    """, (fingerprint, str(destination))).fetchone()
                    if existing and manifest_matches(destination, json.loads(existing["manifest"])):
                        return export_info(existing)
                    require_empty(destination)
                    batch_id = stage_export(conn, deliveries, fingerprint, destination, data_dir, model_dir)
                    break
                published = publish_export(conn, pending)
                if published["input_fingerprint"] == fingerprint and published["destination"] == str(destination):
                    return export_info(published)
        with transaction(conn):
            job = conn.execute("SELECT * FROM exports WHERE batch_id = ?", (batch_id,)).fetchone()
            return export_info(job if job["published_at"] else publish_export(conn, job))


def recover(state_path: Path) -> dict | None:
    with open_state(state_path) as conn, transaction(conn):
        pending = pending_export(conn)
        return export_info(publish_export(conn, pending)) if pending else None
