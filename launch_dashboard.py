"""Start the local dashboard, setting up its Python environment on the first run."""

import argparse
import errno
import json
import os
import subprocess
import sys
import threading
import webbrowser
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
ENVIRONMENT = ROOT / ".venv"
PYTHON = ENVIRONMENT / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
REQUIREMENTS = ROOT / "requirements.txt"
PORTS = (8765, 8766, 25971, 0)


def run_in_environment() -> int:
    if not PYTHON.is_file():
        print("Setting up the dashboard environment...", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(ENVIRONMENT)], check=True, cwd=ROOT)
    return subprocess.call([str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT)


def install_requirements() -> None:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, target = line.split("==", maxsplit=1)
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = None
        if installed != target:
            print("Installing PlaneCo dependencies. This runs once after setup or a dependency change.", flush=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)], check=True, cwd=ROOT)
            return


def workspace_is_available(url: str, identity: str) -> bool:
    try:
        with urlopen(url + "/api/health", timeout=1) as response:
            info = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(info, dict) and info.get("workspace") == identity


def open_dashboard(url: str, open_browser: bool) -> None:
    print(f"PlaneCo dashboard is ready at {url}", flush=True)
    if open_browser and not webbrowser.open(url, new=2):
        print("Open the displayed address in your browser.", flush=True)


def serve_dashboard(open_browser: bool, *, fresh_uploads: bool) -> None:
    from lead_intake.web import Workspace, WorkspaceServer

    workspace = Workspace(ROOT / "data", ROOT / "state" / "intake.sqlite3",
                          ROOT / "artifacts" / "model", ROOT / "out" / "web", fresh_uploads=fresh_uploads)
    for port in PORTS:
        address = f"http://127.0.0.1:{port}"
        if port and workspace_is_available(address, workspace.identity):
            open_dashboard(address, open_browser)
            return
        try:
            server = WorkspaceServer(port, workspace)
            break
        except OSError as exc:
            if not port or (exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) not in {10013, 10048}):
                raise
    else:
        raise OSError("Could not find an available local port for the dashboard.")

    with server:
        address = f"http://127.0.0.1:{server.server_port}"
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            if not workspace_is_available(address, workspace.identity):
                raise OSError("The local dashboard did not respond.")
            open_dashboard(address, open_browser)
            print("Keep this window open while using the dashboard. Press Ctrl+C to stop it.", flush=True)
            while worker.is_alive():
                worker.join(timeout=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            worker.join()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="start without opening a browser tab")
    parser.add_argument("--keep-history", action="store_true", help="run the cumulative intake workspace")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("PlaneCo needs Python 3.11 or newer.")
    try:
        if Path(sys.prefix).resolve() != ENVIRONMENT.resolve():
            return run_in_environment()
        install_requirements()
        serve_dashboard(not args.no_browser, fresh_uploads=not args.keep_history)
    except subprocess.CalledProcessError:
        print("Environment setup failed. Check the installation message above.", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"PlaneCo could not start. {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
