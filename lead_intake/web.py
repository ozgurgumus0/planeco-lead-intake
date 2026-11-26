import csv
import io
import json
import re
import socket
import sqlite3
import tempfile
import threading
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .batch import digest, recover, run_batch
from .dashboard import dashboard, run_summary
from .intake import CRM_FIELDS, REJECTED_FIELDS
from .state import crm_actions, open_state

ASSETS = Path(__file__).with_name("web_assets")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/site.css": ("site.css", "text/css; charset=utf-8"),
    "/site.js": ("site.js", "text/javascript; charset=utf-8"),
}
FILES = {
    "crm": ("crm_import.csv", "text/csv; charset=utf-8"),
    "rejected": ("rejected.csv", "text/csv; charset=utf-8"),
    "report": ("run_report.md", "text/markdown; charset=utf-8"),
}
PREVIEW_FIELDS = (
    "external_id", "service", "source", "owner_rep_id", "lead_score", "expected_value_eur",
    "assignment_reason", "assigned_at_lisbon",
)
RUN_PATH = re.compile(r"/api/runs/([a-f0-9]{32})(?:/download/(crm|rejected|report))?")
MAX_UPLOAD = 10 * 1024 * 1024


@dataclass(frozen=True)
class Workspace:
    data: Path
    state: Path
    model: Path
    output: Path
    fresh_uploads: bool = False

    @property
    def identity(self) -> str:
        paths = (self.data, self.state, self.model, self.output)
        key = str(self.fresh_uploads) + "\0" + "\0".join(str(path.resolve()) for path in paths)
        return digest(key.encode("utf-8"))

    def ingest(self, content: bytes, filename: str) -> str:
        with tempfile.TemporaryDirectory(prefix="planeco-") as folder:
            uploaded = Path(folder) / "input.csv"
            uploaded.write_bytes(content)
            run = run_batch(uploaded, self.data, self.state, self.model, self.output / uuid.uuid4().hex)
        batch_id = run["batch_id"]
        with open_state(self.state) as conn:
            conn.execute("INSERT OR IGNORE INTO export_labels VALUES (?, ?)", (batch_id, filename))
        return batch_id

    def find_run(self, batch_id):
        with open_state(self.state) as conn:
            row = conn.execute("""
                SELECT e.*, l.filename FROM exports AS e LEFT JOIN export_labels AS l USING (batch_id)
                WHERE e.batch_id = ?
            """, (batch_id,)).fetchone()
        if row is None:
            raise FileNotFoundError("Batch not found.")
        return row

    def previous_rejections(self, row):
        with open_state(self.state) as conn:
            earlier = conn.execute("""
                SELECT * FROM exports
                WHERE input_fingerprint = ? AND published_at IS NOT NULL
                  AND rowid < (SELECT rowid FROM exports WHERE batch_id = ?)
                  AND json_extract(summary, '$.rejection_rows') > 0
                ORDER BY rowid DESC
            """, (row["input_fingerprint"], row["batch_id"]))
            for original in earlier:
                try:
                    content = export_file(original, "rejected").decode("utf-8")
                    rows = csv_preview(content, REJECTED_FIELDS, REJECTED_FIELDS)
                except (OSError, ValueError, csv.Error):
                    continue
                return {
                    "batch_id": original["batch_id"],
                    "count": json.loads(original["summary"])["rejection_rows"],
                    "rows": rows,
                }
        return None


def export_file(row, kind) -> bytes:
    filename, _ = FILES[kind]
    if row["published_at"] is None:
        raise FileNotFoundError("This batch is waiting for publication.")
    content = (Path(row["destination"]) / filename).read_bytes()
    if digest(content) != json.loads(row["manifest"])[filename]:
        raise ValueError("The published file differs from the saved export.")
    return content


def csv_preview(content, headers, fields) -> list:
    reader = csv.DictReader(io.StringIO(content))
    if tuple(reader.fieldnames or ()) != headers:
        raise ValueError("Export headers differ from the template.")
    return [{key: record[key] for key in fields} for record in islice(reader, 25)]


def preview(row) -> dict:
    data = {**run_summary(row), "crm_preview": [], "rejected_preview": [], "files_available": False}
    if row["published_at"]:
        try:
            crm, rejected = (export_file(row, kind).decode("utf-8") for kind in ("crm", "rejected"))
            export_file(row, "report")
        except (OSError, ValueError):
            return data
        for kind, content, headers, shown in (
            ("crm", crm, CRM_FIELDS, PREVIEW_FIELDS),
            ("rejected", rejected, REJECTED_FIELDS, REJECTED_FIELDS),
        ):
            data[f"{kind}_preview"] = csv_preview(content, headers, shown)
        data["files_available"] = True
    return data


class WorkspaceServer(ThreadingHTTPServer):
    def __init__(self, port: int, workspace: Workspace):
        self.workspace = workspace
        self.workspace_identity = workspace.identity
        self.request_lock = threading.Lock() if workspace.fresh_uploads else nullcontext()
        self.daemon_threads = not workspace.fresh_uploads
        self.snapshot = None
        super().__init__(("127.0.0.1", port), Handler)
        if workspace.fresh_uploads:
            try:
                self.snapshot, self.workspace = self.new_snapshot()
            except OSError:
                self.server_close()
                raise

    def new_snapshot(self):
        folder = tempfile.TemporaryDirectory(prefix="planeco-snapshot-", ignore_cleanup_errors=True)
        root = Path(folder.name)
        return folder, replace(self.workspace, state=root / "intake.sqlite3", output=root / "exports")

    def ingest(self, content, filename):
        if not self.workspace.fresh_uploads:
            return self.workspace.ingest(content, filename)
        # Handler requests hold the snapshot lock through reads and downloads.
        folder, candidate = self.new_snapshot()
        try:
            batch_id = candidate.ingest(content, filename)
        except BaseException:
            folder.cleanup()
            raise
        previous = self.snapshot
        self.snapshot, self.workspace = folder, candidate
        previous.cleanup()
        return batch_id

    def server_close(self):
        super().server_close()
        if self.snapshot is not None:
            self.snapshot.cleanup()

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # SO_REUSEADDR can bind over an existing listener on Windows.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def response(self, body: bytes, content_type: str, *, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, *, status=200):
        self.response(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                      "application/json; charset=utf-8", status=status)

    def allowed(self):
        port = self.server.server_port
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        return host in {f"127.0.0.1:{port}", f"localhost:{port}"} and (
            origin is None or origin == f"http://{host}"
        )

    def do_GET(self):
        if not self.allowed():
            return self.json({"error": "Use the local PlaneCo address."}, status=403)
        url = urlsplit(self.path)
        if url.path == "/api/health":
            return self.json({"workspace": self.server.workspace_identity})
        with self.server.request_lock:
            self.get_workspace_response(url)

    def get_workspace_response(self, url):
        workspace = self.server.workspace
        try:
            if url.path in STATIC:
                filename, content_type = STATIC[url.path]
                return self.response((ASSETS / filename).read_bytes(), content_type)
            if url.path == "/api/dashboard":
                default_days = "all" if workspace.fresh_uploads else "14"
                selected = parse_qs(url.query).get("days", [default_days])[0]
                if selected not in {"all", "7", "14", "30"}:
                    raise ValueError("Choose all arrivals or a range of 7, 14, or 30 days.")
                result = dashboard(workspace.state, workspace.data, None if selected == "all" else int(selected))
                return self.json({**result, "fresh_uploads": workspace.fresh_uploads})
            if url.path == "/api/actions":
                with open_state(workspace.state) as conn:
                    return self.response(json.dumps(crm_actions(conn), indent=2).encode("utf-8"),
                                         "application/json; charset=utf-8", filename="crm_actions.json")
            run = RUN_PATH.fullmatch(url.path)
            if run:
                row = workspace.find_run(run[1])
                if not run[2]:
                    result = preview(row)
                    if result["deliveries"] and result["events"]["duplicate_deliveries"] == result["deliveries"]:
                        result["previous_rejections"] = workspace.previous_rejections(row)
                    return self.json(result)
                filename, content_type = FILES[run[2]]
                return self.response(export_file(row, run[2]), content_type, filename=filename)
            return self.json({"error": "Page not found."}, status=404)
        except FileNotFoundError:
            return self.json({"error": "The requested file or batch is unavailable."}, status=404)
        except (OSError, ValueError, sqlite3.Error, csv.Error) as exc:
            return self.json({"error": str(exc)}, status=400)

    def do_POST(self):
        if not self.allowed():
            return self.json({"error": "Use the local PlaneCo address."}, status=403)
        with self.server.request_lock:
            self.post_workspace_response()

    def post_workspace_response(self):
        url = urlsplit(self.path)
        workspace = self.server.workspace
        try:
            if url.path == "/api/recover":
                run = recover(workspace.state)
                return self.json({"batch_id": run["batch_id"] if run else None})
            if url.path == "/api/sample":
                choice = parse_qs(url.query).get("batch", ["initial"])[0]
                samples = {
                    "initial": workspace.data / "lead_events.csv",
                    "follow-up": workspace.data.parent / "demo" / "live_events.csv",
                }
                if choice not in samples:
                    raise ValueError("Choose an initial or follow-up sample.")
                sample = samples[choice]
                return self.json({"batch_id": self.server.ingest(sample.read_bytes(), sample.name)})
            if url.path != "/api/intake":
                return self.json({"error": "Page not found."}, status=404)
            filename = parse_qs(url.query).get("name", ["batch.csv"])[0].replace("\\", "/").split("/")[-1]
            if not filename.lower().endswith(".csv") or not 1 <= len(filename) <= 240:
                raise ValueError("Choose a CSV file.")
            if self.headers.get_content_type() not in {"text/csv", "application/octet-stream"}:
                raise ValueError("Upload CSV content.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_UPLOAD:
                raise ValueError("Choose a nonempty CSV file up to 10 MB.")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("The upload ended early. Try uploading again.")
            return self.json({"batch_id": self.server.ingest(body, filename)})
        except (OSError, ValueError, RuntimeError, sqlite3.Error, csv.Error) as exc:
            return self.json({"error": str(exc)}, status=400)


def serve(data: Path, state: Path, model: Path, output: Path, port: int, *, fresh_uploads=False):
    if not 0 <= port <= 65535:
        raise ValueError("Port must be between 0 and 65535.")
    workspace = Workspace(data.resolve(), state.resolve(), model.resolve(), output.resolve(), fresh_uploads=fresh_uploads)
    with WorkspaceServer(port, workspace) as server:
        print(f"PlaneCo workspace at http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
