import argparse
import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from .batch import export_info, recover, run_batch
from .files import json_text
from .model import TEST_START, VALIDATION_START, train
from .state import confirm_action, confirm_export, crm_actions, open_state


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Validate, score, and route PlaneCo lead events.")
    commands = root.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="process an intake CSV and publish a CRM delta")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--data-dir", type=Path, default=Path("data"))
    run.add_argument("--state", type=Path, default=Path("state/intake.sqlite3"))
    run.add_argument("--model-dir", type=Path, default=Path("artifacts/model"))
    run.add_argument("--out-dir", type=Path, required=True)
    training = commands.add_parser("train", help="train, evaluate, and replace the saved model")
    training.add_argument("--data-dir", type=Path, default=Path("data"))
    training.add_argument("--model-dir", type=Path, default=Path("artifacts/model"))
    training.add_argument("--validation-start", type=date.fromisoformat, default=VALIDATION_START)
    training.add_argument("--test-start", type=date.fromisoformat, default=TEST_START)
    analysis = commands.add_parser("analyze", help="reproduce descriptive and channel-spend figures")
    analysis.add_argument("--data-dir", type=Path, default=Path("data"))
    analysis.add_argument("--out-dir", type=Path, required=True)
    web = commands.add_parser("serve", help="open the local dashboard and CSV intake workspace")
    web.add_argument("--data-dir", type=Path, default=Path("data"))
    web.add_argument("--state", type=Path, default=Path("state/intake.sqlite3"))
    web.add_argument("--model-dir", type=Path, default=Path("artifacts/model"))
    web.add_argument("--out-dir", type=Path, default=Path("out/web"))
    web.add_argument("--port", type=int, default=8765)
    for name, help_text in (
        ("actions", "print unacknowledged CRM updates and deletions as JSON"),
        ("exports", "list generated exports and their CRM confirmation status"),
        ("ack-action", "record a confirmed CRM update or deletion"),
        ("ack-export", "record a confirmed CRM import from a generated delta"),
        ("recover", "publish a previously committed batch at its recorded destination"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--state", type=Path, default=Path("state/intake.sqlite3"))
        if name == "ack-action":
            sub.add_argument("--action-id", type=int, required=True)
        if name == "ack-export":
            sub.add_argument("--batch-id", required=True)
    return root


def print_result(info: dict) -> None:
    print(f"Batch {info['batch_id']} published in {info['destination']}")
    print(
        f"Deliveries {info['deliveries']} | New CRM rows {info['crm_rows']} | "
        f"Rejected rows {info['rejection_rows']} | Updates {info['crm_updates']} | Deletes {info['crm_deletes']}"
    )
    print(f"Scorer {info['model_version']}")


def main() -> None:
    root = parser()
    args = root.parse_args()
    try:
        if args.command == "train":
            scorer = train(args.data_dir / "historical_leads.csv", args.model_dir,
                           args.validation_start, args.test_start)
            print(f"{scorer.version} ({scorer.metadata['classifier']}) saved in {args.model_dir}")
        elif args.command == "analyze":
            from .analysis import analyze
            analyze(args.data_dir, args.out_dir)
            print(f"Descriptive and spend analysis saved in {args.out_dir}")
        elif args.command == "run":
            print_result(run_batch(args.input, args.data_dir, args.state, args.model_dir, args.out_dir))
        elif args.command == "serve":
            from .web import serve
            serve(args.data_dir, args.state, args.model_dir, args.out_dir, args.port)
        else:
            if not args.state.is_file():
                raise ValueError(f"no state database at {args.state}")
            if args.command == "recover":
                result = recover(args.state)
                if result:
                    print_result(result)
                else:
                    print("No export needs recovery.")
                return
            with open_state(args.state) as conn:
                if args.command == "actions":
                    # ASCII escapes keep JSON readable when the Windows console is redirected.
                    print(json.dumps(crm_actions(conn), indent=2))
                elif args.command == "exports":
                    exports = conn.execute("SELECT * FROM exports ORDER BY rowid")
                    print(json_text([export_info(job) for job in exports]))
                elif args.command == "ack-action":
                    confirm_action(conn, args.action_id)
                elif args.command == "ack-export":
                    confirm_export(conn, args.batch_id)
    except (OSError, ValueError, KeyError, csv.Error, sqlite3.Error, RuntimeError) as exc:
        root.exit(1, f"PlaneCo: {exc}\n")


if __name__ == "__main__":
    main()
