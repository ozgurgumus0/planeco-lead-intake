import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean

from .analysis import MONTHS, rollup, spend_cohorts
from .batch import export_info
from .files import read_table, utc_text
from .model import read_history
from .routing import HIGH_SCORE, LISBON, load_roster
from .state import open_state


def run_summary(row) -> dict:
    info = export_info(row)
    info.pop("destination")
    info["filename"] = row["filename"] or "Command-line batch"
    return info


def history_metrics(data_dir) -> dict:
    history = read_history((data_dir / "historical_leads.csv").read_bytes())
    cohorts = spend_cohorts(history, read_table(data_dir / "ad_spend.csv"))

    def figures(groups):
        return [
            {"name": name, **{
                key: float(value) if isinstance(value, Decimal) else value
                for key, value in cohort.figures().items()
            }}
            for name, cohort in groups.items()
        ]

    return {
        "first_month": MONTHS[0], "last_month": MONTHS[-1],
        "channels": figures(rollup(cohorts, include_campaign=False)),
        "campaigns": figures(rollup(cohorts, include_campaign=True)),
    }


def dashboard(state, data_dir, days: int | None) -> dict:
    with open_state(state) as conn:
        # All panels use one SQLite snapshot even if intake is committing concurrently.
        conn.execute("BEGIN")
        earliest, latest = conn.execute("SELECT min(day), max(day) FROM arrival_counts").fetchone()
        end = date.fromisoformat(latest) if latest else datetime.now(LISBON).date()
        if days is None:
            start = date.fromisoformat(earliest) if earliest else end
        else:
            start = end - timedelta(days=days - 1)
        arrivals = [dict(row) for row in conn.execute(
            "SELECT * FROM arrival_counts WHERE day >= ? ORDER BY day, source, channel", (start.isoformat(),)
        )]
        leads = [json.loads(row[0]) for row in conn.execute("SELECT record FROM leads")]
        runs = [
            run_summary(row) for row in conn.execute("""
                SELECT e.*, l.filename FROM exports AS e LEFT JOIN export_labels AS l USING (batch_id)
                ORDER BY e.rowid DESC LIMIT 6
            """)
        ]
        actions = dict(conn.execute("""
            SELECT a.operation, count(*) FROM crm_actions AS a JOIN exports AS e USING (batch_id)
            WHERE a.confirmed_at IS NULL AND e.published_at IS NOT NULL GROUP BY a.operation
        """))

    roster = load_roster(data_dir / "sales_team.csv")
    active = Counter(lead["assignment"]["owner"] for lead in leads if lead["assignment"]["at_utc"])
    queued = [lead for lead in leads if not lead["assignment"]["at_utc"]]
    without_coverage = sum(not any(rep.covers(lead["fields"]) for rep in roster.values()) for lead in queued)
    return {
        "refreshed_at": utc_text(datetime.now(timezone.utc)),
        "start": start.isoformat(), "end": end.isoformat(), "has_arrivals": latest is not None,
        "arrivals": arrivals, "new_leads": sum(row["total"] for row in arrivals),
        "active": sum(active.values()), "capacity": sum(rep.maximum for rep in roster.values()),
        "queued": len(queued), "queue_without_coverage": without_coverage,
        "queue_high_score": sum(lead["probability"] >= HIGH_SCORE for lead in queued),
        "mean_score": mean(lead["probability"] for lead in leads) if leads else None,
        "expected_value": sum(lead["expected_value"] for lead in leads) if leads else None,
        "reps": [
            {"id": rep.rep_id, "open": active[rep.rep_id], "max": rep.maximum, "partner": rep.partner_manager}
            for rep in roster.values()
        ],
        "runs": runs, "pending_actions": actions,
        "history": history_metrics(data_dir),
    }
