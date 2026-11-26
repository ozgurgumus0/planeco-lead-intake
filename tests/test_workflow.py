import csv
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lead_intake.batch import publish_directory, recover, run_batch
from lead_intake.events import CREATE, DELETE, UPDATE
from lead_intake.features import from_intake
from lead_intake.files import csv_text, read_table
from lead_intake.model import load_model, train
from lead_intake.state import confirm_action, confirm_export, crm_actions, open_state

DATA = Path(__file__).resolve().parents[1] / "data"


def event(event_id, external_id, kind=CREATE, at="2026-11-20T11:00:00Z", **fields):
    base = {"event_id": event_id, "external_id": external_id, "event_type": kind, "occurred_at_utc": at}
    if kind == CREATE:
        base.update(first_name="Ada", last_name="Example", email=f"{external_id}@mail.example",
                    source="chat", postcode="01067", country="DE", language="de", service="structural",
                    project_stage="planning", utm_source="organic", utm_campaign="seo")
    return {**base, **fields}


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.model_dir = Path(temp.name)
        cls.scorer = train(DATA / "historical_leads.csv", cls.model_dir)
        with (DATA / "lead_events.csv").open(encoding="utf-8-sig", newline="") as file:
            cls.columns = next(csv.reader(file))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / "state.sqlite3"

    def write_batch(self, name, records):
        path = self.root / f"{name}.csv"
        path.write_bytes(csv_text(self.columns, records).encode())
        return path

    def batch(self, name, records, *, destination=None):
        path = self.write_batch(name, records)
        return run_batch(path, DATA, self.state, self.model_dir, self.root / (destination or name))

    def test_saved_estimator_handles_optional_values_and_reloads_exactly(self):
        vectors = [
            from_intake({}),
            from_intake({"source": "chat", "utm_source": "new_source", "utm_campaign": "unseen"}),
            from_intake({"source": "partner_api", "phone": "+493012345678", "message": "Building plans"}),
        ]
        reloaded = load_model(self.model_dir)
        self.assertEqual(reloaded.predict(vectors), self.scorer.predict(vectors))
        self.assertEqual(reloaded.version, self.scorer.version)
        for score in reloaded.predict(vectors):
            self.assertGreaterEqual(score.probability, 0)
            self.assertLessEqual(score.probability, 1)
            self.assertGreaterEqual(score.expected_value, 0)
            self.assertLessEqual(score.expected_value, reloaded.metadata["value_given_won_eur"])
        self.assertEqual(reloaded.feature_health(vectors)["channel"], 2)

    def test_template_headers_and_replay_for_valid_and_rejected_rows(self):
        records = [event("a", "one"), event("a", "one"), event("b", "bad", email="bad")]
        initial = self.batch("initial", records)
        replay = self.batch("repeat", records)
        self.assertEqual((initial["crm_rows"], initial["rejection_rows"]), (1, 1))
        self.assertEqual((replay["crm_rows"], replay["rejection_rows"]), (0, 0))
        self.assertEqual(replay["events"]["duplicate_deliveries"], 3)
        for column_file in ("crm_import", "rejected"):
            with (DATA / f"{column_file}_TEMPLATE.csv").open(encoding="utf-8-sig", newline="") as expected:
                header = next(csv.reader(expected))
            for directory in ("initial", "repeat"):
                with (self.root / directory / f"{column_file}.csv").open(encoding="utf-8", newline="") as actual:
                    self.assertEqual(next(csv.reader(actual)), header)

    def test_conflicting_event_id_and_split_identity_require_review(self):
        self.batch("initial", [
            event("a", "one", phone="+493012345678"),
            event("b", "two", phone="+498912345678"),
        ])
        info = self.batch("conflicts", [
            event("a", "one", phone="+493012345679"),
            event("c", "three", email="one@mail.example", phone="+498912345678"),
            event("d", "one", UPDATE, at="2026-11-24T12:00:00Z", email="two@mail.example"),
            event("e", "one", email="unrelated@mail.example"),
        ])
        reasons = [row["reason"] for row in read_table(self.root / "conflicts" / "rejected.csv")]
        self.assertEqual(info["rejection_rows"], 4)
        self.assertEqual(sum("identity conflict" in reason for reason in reasons), 3)
        self.assertEqual(sum("conflicting payload" in reason for reason in reasons), 1)
        self.assertEqual(info["leads_after"], 2)

    def test_trusted_phone_can_merge_with_same_name_and_new_email(self):
        first = event("a", "one", phone="+49 30 12345678")
        second = event("b", "two", at="2026-11-20T11:05:00Z", phone="03012345678")
        info = self.batch("contact", [first, second])
        row, = read_table(self.root / "contact" / "crm_import.csv")
        self.assertEqual(info["events"]["merged_creates"], 1)
        self.assertEqual((row["external_id"], row["email"], row["merged_external_ids"]),
                         ("one", "one@mail.example", "two"))

    def test_deferred_patch_is_applied_across_files_and_same_batch(self):
        pending = event("patch-across", "one", UPDATE, at="2026-11-20T11:02:00Z", phone="03012345678")
        start = self.batch("first", [pending])
        self.assertEqual((start["events"]["patches_deferred"], start["crm_rows"]), (1, 0))
        inline = event("patch-inline", "two", UPDATE, at="2026-11-20T11:03:00Z", project_stage="rejected_needs_fix")
        resumed = self.batch("second", [inline, event("create1", "one"), event("create2", "two")])
        self.assertEqual(resumed["previously_deferred_patches"]["applied"], 1)
        self.assertEqual(resumed["events"]["patches_applied"], 1)
        self.assertEqual(resumed["pending_patches"], 0)
        rows = {row["external_id"]: row for row in read_table(self.root / "second" / "crm_import.csv")}
        self.assertEqual(rows["one"]["phone"], "+493012345678")
        self.assertEqual(rows["two"]["project_stage"], "rejected_needs_fix")

    def test_sparse_newer_patch_and_alias_preserve_owner_and_rescore(self):
        self.batch("first", [event("create", "one")])
        before, = read_table(self.root / "first" / "crm_import.csv")
        newer = event("newer", "two", UPDATE, at="2026-11-20T12:00:00Z",
                      project_stage="rejected_needs_fix", last_name="")
        alias = event("alias", "two", at="2026-11-20T11:30:00Z",
                      email=" ONE@MAIL.EXAMPLE ", utm_source="meta", utm_campaign="retarget_web")
        second = self.batch("second", [newer, alias])
        stale = self.batch("third", [event("stale", "one", UPDATE, at="2026-11-20T11:59:00Z", project_stage="idea")])
        self.assertEqual(stale["events"]["patches_ignored"], 1)
        self.assertEqual(second["crm_rows"], 0)
        self.assertEqual(second["scoring"]["newly_scored"], 1)
        self.assertEqual(second["model_version"], self.scorer.version)
        with open_state(self.state) as conn:
            action, = crm_actions(conn)
            after = action["payload"]
            self.assertEqual(action["external_id"], "one")
            self.assertEqual(after["merged_external_ids"], "two")
            self.assertEqual(after["project_stage"], "rejected_needs_fix")
            self.assertEqual(after["utm_source"], "organic")
            self.assertEqual(after["last_name"], before["last_name"])
            self.assertEqual(after["owner_rep_id"], before["owner_rep_id"])
            self.assertEqual(after["assigned_at_lisbon"], before["assigned_at_lisbon"])
            self.assertNotEqual(after["lead_score"], before["lead_score"])
            confirm_action(conn, action["action_id"])
            self.assertEqual(crm_actions(conn), [])

    def test_delete_via_alias_erases_payload_and_releases_capacity(self):
        self.batch("first", [event("a", "one")])
        self.batch("second", [event("b", "two", email="one@mail.example")])
        deleted = self.batch("third", [
            event("delete", "two", DELETE, at="2026-11-21T09:00:00Z"),
            event("a", "one"),
        ])
        self.assertEqual((deleted["crm_deletes"], deleted["active_assigned"]), (1, 0))
        self.assertEqual(deleted["events"]["duplicate_deliveries"], 1)
        stale = self.batch("stale", [event("different-id", "two")])
        self.assertEqual((stale["crm_rows"], stale["rejection_rows"]), (0, 1))
        with open_state(self.state) as conn:
            self.assertEqual(conn.execute("select count(*) from leads").fetchone()[0], 0)
            action, = crm_actions(conn)
            self.assertEqual((action["external_id"], action["operation"], action["payload"]), ("one", "delete", None))
            tombstones = {row["external_id"] for row in conn.execute("select external_id from tombstones")}
            self.assertEqual(tombstones, {"one", "two"})

    def test_unseen_deletion_and_same_batch_deletion_block_exports(self):
        summary = self.batch("mixed", [
            event("delete-b", "two", DELETE, at="2026-11-21T11:00:00Z"),
            event("p", "two", UPDATE, at="2026-11-20T11:30:00Z", phone="+493012345678"),
            event("create-a", "one"),
            event("delete-a", "one", DELETE, at="2026-11-21T11:00:00Z"),
        ])
        self.assertEqual((summary["crm_rows"], summary["crm_deletes"], summary["active_assigned"]), (0, 0, 0))
        self.assertEqual(summary["events"]["patches_discarded"], 1)
        self.assertEqual(summary["events"]["deletions_for_unseen_or_deleted_id"], 1)
        blocked = self.batch("late", [event("create-b", "two")])
        self.assertEqual((blocked["crm_rows"], blocked["rejection_rows"]), (0, 1))

    def test_retry_after_committed_state_and_failed_publication(self):
        rows = [event("create", "one")]
        with patch("lead_intake.batch.publish_directory", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "committed but publication failed"):
                self.batch("initial", rows)
        with open_state(self.state) as conn:
            self.assertEqual(conn.execute("select count(*) from leads").fetchone()[0], 1)
            job, = conn.execute("select published_at, documents from exports").fetchall()
            self.assertIsNone(job["published_at"])
            self.assertIsNotNone(job["documents"])
        resumed = self.batch("initial", rows)
        self.assertEqual(resumed["crm_rows"], 1)
        self.assertIsNone(resumed["crm_confirmed_at"])
        self.assertEqual(len(read_table(self.root / "initial" / "crm_import.csv")), 1)
        self.assertEqual(self.batch("initial", rows)["batch_id"], resumed["batch_id"])
        self.assertEqual(self.batch("replay", rows)["crm_rows"], 0)
        with open_state(self.state) as conn:
            confirm_export(conn, resumed["batch_id"])
            job = conn.execute("select crm_confirmed_at, documents from exports where batch_id = ?",
                               (resumed["batch_id"],)).fetchone()
            self.assertIsNone(job["documents"])
            self.assertIsNotNone(job["crm_confirmed_at"])

    def test_recover_after_directory_published_before_database_ack(self):
        def lose_ack(*args):
            publish_directory(*args)
            raise OSError("interrupted after rename")
        with patch("lead_intake.batch.publish_directory", side_effect=lose_ack):
            with self.assertRaises(RuntimeError):
                self.batch("initial", [event("create", "one")])
        before = (self.root / "initial" / "crm_import.csv").read_bytes()
        resumed = recover(self.state)
        self.assertEqual(resumed["crm_rows"], 1)
        self.assertEqual((self.root / "initial" / "crm_import.csv").read_bytes(), before)
        self.assertIsNone(recover(self.state))

    def test_concurrent_runs_publish_a_single_new_crm_row(self):
        records = [event("create", "only")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(self.batch, name, records) for name in ("run-a", "run-b")]
            counts = sorted(job.result()["crm_rows"] for job in jobs)
        self.assertEqual(counts, [0, 1])
        with open_state(self.state) as conn:
            self.assertEqual(conn.execute("select count(*) from leads").fetchone()[0], 1)
            self.assertEqual(conn.execute("select count(*) from exports where published_at is not null").fetchone()[0], 2)

    def test_actions_have_model_version_stable_after_unchanged_input(self):
        info = self.batch("initial", [event("a", "one")])
        second = self.batch("live", [event("b", "two", utm_campaign="new")])
        self.assertEqual(info["model_version"], second["model_version"])
        with open_state(self.state) as conn:
            rows = [json.loads(row["record"]) for row in conn.execute("select record from leads")]
        self.assertTrue(all(row["model_version"] == info["model_version"] for row in rows))

    def test_incompatible_patch_releases_owner_and_explains_coverage_in_report(self):
        first = self.batch("initial", [event("a", "one", source="partner_api")])
        second = self.batch("live", [
            event("b", "one", UPDATE, at="2026-11-20T11:45:00Z", language="pt")
        ])
        self.assertEqual(first["capacity"]["rep_06"]["open"], 1)
        self.assertEqual(second["capacity"]["rep_06"]["open"], 0)
        self.assertEqual((second["queue_without_coverage"], second["queue_at_capacity"]), (1, 0))
        with open_state(self.state) as conn:
            update, = crm_actions(conn)
        self.assertEqual(update["payload"]["assignment_reason"], "queue")


if __name__ == "__main__":
    unittest.main()
