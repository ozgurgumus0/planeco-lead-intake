import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lead_intake.model import train
from lead_intake.state import open_state
from lead_intake.web import Workspace, WorkspaceServer

DATA = Path(__file__).resolve().parents[1] / "data"


class WebTests(unittest.TestCase):
    def test_browser_upload_replay_and_dashboard_share_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model"
            train(DATA / "historical_leads.csv", model)
            workspace = Workspace(DATA, root / "state.sqlite3", model, root / "exports")
            with WorkspaceServer(0, workspace) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"

                    def read(path, body=None, headers=None):
                        with urlopen(Request(base + path, data=body, headers=headers or {}), timeout=30) as response:
                            return response.read()

                    def request(path, body=None, headers=None):
                        return json.loads(read(path, body, headers))

                    self.assertIn(b"Lead dashboard", read("/"))
                    self.assertEqual(request("/api/dashboard?days=30")["new_leads"], 0)
                    first = request("/api/sample?batch=initial", b"")["batch_id"]
                    preview = request(f"/api/runs/{first}")
                    self.assertEqual((preview["crm_rows"], preview["rejection_rows"]), (538, 18))
                    self.assertEqual(len(preview["crm_preview"]), 25)
                    self.assertEqual(len(preview["rejected_preview"]), 18)
                    self.assertTrue(all(row["reason"] for row in preview["rejected_preview"]))
                    self.assertNotIn("email", preview["crm_preview"][0])

                    crm = read(f"/api/runs/{first}/download/crm").decode()
                    with (DATA / "crm_import_TEMPLATE.csv").open(encoding="utf-8-sig", newline="") as template:
                        self.assertEqual(next(csv.reader(io.StringIO(crm))), next(csv.reader(template)))

                    data = DATA.joinpath("lead_events.csv").read_bytes()
                    headers = {"Content-Type": "text/csv"}
                    replay = request("/api/intake?name=uploaded.csv", data, headers)["batch_id"]
                    replay_info = request(f"/api/runs/{replay}")
                    self.assertEqual((replay_info["filename"], replay_info["crm_rows"]), ("uploaded.csv", 0))
                    self.assertEqual(replay_info["events"]["duplicate_deliveries"], 648)
                    self.assertEqual(replay_info["rejection_rows"], 0)
                    self.assertEqual(replay_info["previous_rejections"], {
                        "batch_id": first, "count": 18, "rows": preview["rejected_preview"],
                    })
                    replay_rejected = read(f"/api/runs/{replay}/download/rejected").decode()
                    self.assertEqual(len(list(csv.reader(io.StringIO(replay_rejected)))), 1)
                    followup = request("/api/sample?batch=follow-up", b"")["batch_id"]
                    self.assertEqual(request(f"/api/runs/{followup}")["crm_rows"], 1)
                    dashboard = request("/api/dashboard?days=30")
                    self.assertEqual((dashboard["new_leads"], dashboard["active"], dashboard["queued"]), (542, 135, 403))
                    self.assertEqual(sum(row["total"] for row in dashboard["arrivals"]), 542)
                    self.assertEqual(dashboard["pending_actions"], {"update": 2, "delete": 1})
                    self.assertEqual(len(request("/api/actions")), 3)
                    self.assertEqual(len(dashboard["history"]["channels"]), 5)
                    self.assertEqual(request("/api/health")["workspace"], workspace.identity)
                    with self.assertRaises(HTTPError) as error:
                        request("/api/intake?name=uploaded.csv", data, {**headers, "Origin": "http://example.invalid"})
                    self.assertEqual(error.exception.code, 403)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)

    def test_fresh_uploads_replace_the_dashboard_and_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model = root / "model"
            train(DATA / "historical_leads.csv", model)
            workspace = Workspace(DATA, root / "state.sqlite3", model, root / "exports", fresh_uploads=True)
            with WorkspaceServer(0, workspace) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"

                    def get(path, body=None):
                        headers = {"Content-Type": "text/csv"} if body is not None else {}
                        with urlopen(Request(base + path, data=body, headers=headers), timeout=30) as response:
                            return json.load(response)

                    def upload(content):
                        batch = get("/api/intake?name=full.csv", content)["batch_id"]
                        return get(f"/api/runs/{batch}")

                    self.assertEqual(get("/api/dashboard")["new_leads"], 0)
                    original = (DATA / "lead_events.csv").read_bytes()
                    first = upload(original)
                    second = upload(original)
                    for result in (first, second):
                        self.assertEqual((result["deliveries"], result["crm_rows"], result["rejection_rows"]), (648, 538, 18))
                        self.assertEqual(len(result["rejected_preview"]), 18)
                    full = get("/api/dashboard")
                    self.assertTrue(full["fresh_uploads"])
                    self.assertEqual(full["new_leads"], 541)
                    self.assertEqual([run["batch_id"] for run in full["runs"]], [second["batch_id"]])
                    with self.assertRaises(HTTPError) as expired:
                        get(f"/api/runs/{first['batch_id']}")
                    self.assertEqual(expired.exception.code, 404)

                    previous_id = second["crm_preview"][0]["external_id"]
                    reader = csv.DictReader(io.StringIO(original.decode("utf-8-sig")))
                    overlapping_row = next(row for row in reader
                                           if row["external_id"] == previous_id and row["event_type"] == "lead.created")
                    subset = io.StringIO(newline="")
                    writer = csv.DictWriter(subset, fieldnames=reader.fieldnames)
                    writer.writeheader()
                    writer.writerow(overlapping_row)
                    latest = upload(subset.getvalue().encode("utf-8"))
                    self.assertEqual((latest["crm_rows"], latest["rejection_rows"]), (1, 0))
                    with self.assertRaises(HTTPError) as invalid:
                        upload(b"wrong_header\ninvalid\n")
                    self.assertEqual(invalid.exception.code, 400)
                    refreshed = get("/api/dashboard")
                    self.assertEqual(refreshed["new_leads"], 1)
                    self.assertEqual([run["batch_id"] for run in refreshed["runs"]], [latest["batch_id"]])
                    self.assertFalse(workspace.state.exists())
                    self.assertFalse(workspace.output.exists())
                    active_snapshot = server.workspace.state.parent
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
            self.assertFalse(active_snapshot.exists())

    def test_older_state_seeds_the_arrival_counts_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.sqlite3"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE leads (external_id TEXT PRIMARY KEY, record TEXT NOT NULL)")
                conn.execute("INSERT INTO leads VALUES (?, ?)", ("ld_1", json.dumps({
                    "created_at_utc": "2026-11-16T08:00:00Z",
                    "fields": {"source": "web_form", "utm_source": "google"},
                })))
            for _ in range(2):
                with open_state(path) as conn:
                    row = conn.execute("SELECT day, source, channel, total FROM arrival_counts").fetchone()
                    self.assertEqual(tuple(row), ("2026-11-16", "web_form", "google", 1))


if __name__ == "__main__":
    unittest.main()
