from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .files import atomic_file, csv_text, parse_time, read_table, utc_text
from .model import interval, percent, read_history, wilson

MONTHS = ("2026-08", "2026-09", "2026-10")
SPEND_COLUMNS = (
    "month", "channel", "campaign", "spend_eur", "leads", "wins", "win_rate",
    "cost_per_lead_eur", "cost_per_win_eur", "net_booked_eur", "net_booked_per_eur_spent",
)


@dataclass
class Cohort:
    leads: int = 0
    wins: int = 0
    net_booked: Decimal = Decimal(0)
    spend: Decimal | None = None

    def add_lead(self, row: dict) -> None:
        self.leads += 1
        if row["won"] == "1":
            self.wins += 1
            self.net_booked += Decimal(row["contract_value_eur_net"])

    def figures(self) -> dict:
        return {
            "spend_eur": self.spend,
            "leads": self.leads,
            "wins": self.wins,
            "win_rate": ratio(self.wins, self.leads),
            "cost_per_lead_eur": ratio(self.spend, self.leads),
            "cost_per_win_eur": ratio(self.spend, self.wins),
            "net_booked_eur": self.net_booked,
            "net_booked_per_eur_spent": ratio(self.net_booked, self.spend),
        }


def ratio(numerator, denominator):
    return numerator / denominator if numerator is not None and denominator else None


def month(row: dict) -> str:
    return utc_text(parse_time(row["created_at_utc"]))[:7]


def spend_cohorts(history, spend) -> dict:
    cohorts = defaultdict(Cohort)
    for row in history:
        lead_month = month(row)
        if lead_month in MONTHS:
            cohorts[lead_month, row["channel"], row["campaign"]].add_lead(row)
    for row in spend:
        if row["month"] in MONTHS:
            cohort = cohorts[row["month"], row["channel"], row["campaign"]]
            cost = Decimal(row["spend_eur"])
            if not cost.is_finite() or cost < 0:
                raise ValueError("invalid campaign spend")
            cohort.spend = (cohort.spend or Decimal(0)) + cost
    return dict(cohorts)


def rollup(cohorts: dict, *, include_campaign: bool) -> dict[str, Cohort]:
    groups = defaultdict(list)
    for (_, channel, campaign), cohort in cohorts.items():
        name = f"{channel} / {campaign}" if include_campaign else channel
        groups[name].append(cohort)
    return {
        name: Cohort(
            leads=sum(row.leads for row in rows),
            wins=sum(row.wins for row in rows),
            net_booked=sum(row.net_booked for row in rows),
            spend=sum(row.spend for row in rows) if all(row.spend is not None for row in rows) else None,
        )
        for name, rows in sorted(groups.items())
    }


def money(value) -> str:
    return "—" if value is None else f"€{value:,.2f}"


def spend_table(groups: dict) -> list[str]:
    lines = [
        "| Cohort | Spend | Leads | Wins | Win rate | Cost / lead | Cost / win | Net booked | Net booked / spend |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, row in groups.items():
        stats = row.figures()
        booked_return = stats["net_booked_per_eur_spent"]
        multiple = "—" if booked_return is None else f"{booked_return:.2f}×"
        lines.append(
            f"| {key} | {money(row.spend)} | {row.leads} | {row.wins} | {percent(stats['win_rate'])} | "
            f"{money(stats['cost_per_lead_eur'])} | {money(stats['cost_per_win_eur'])} | "
            f"{money(row.net_booked)} | {multiple} |"
        )
    return lines


def contact_band(row: dict) -> str:
    value = row["minutes_to_first_contact"].strip()
    if not value:
        return "unknown"
    minutes = int(value)
    if minutes <= 30:
        return "0–30 min"
    if minutes <= 120:
        return "31–120 min"
    return "Over 120 min"


def group_table(history, label, group) -> list[str]:
    groups = defaultdict(Cohort)
    for row in history:
        groups[group(row)].add_lead(row)
    lines = [f"| {label} | Leads | Wins | Win rate | Wilson 95% interval |", "|---|---:|---:|---:|---:|"]
    for key, row in sorted(groups.items()):
        limits = interval(wilson(row.wins, row.leads), as_percent=True)
        lines.append(f"| {key} | {row.leads} | {row.wins} | {percent(ratio(row.wins, row.leads))} | {limits} |")
    return lines


def analyze(data_dir: Path, out_dir: Path) -> None:
    history = read_history((data_dir / "historical_leads.csv").read_bytes())
    cohorts = spend_cohorts(history, read_table(data_dir / "ad_spend.csv"))
    lines = [
        "# Descriptive evidence and channel spend", "",
        f"{len(history):,} historical leads. Group rates describe the supplied outcome snapshot.", "",
        *group_table(history, "Project stage", lambda row: row["project_stage"]),
        "", *group_table(history, "Phone given", lambda row: row["phone_given"]),
        "", *group_table(history, "Intake channel", lambda row: row["channel"]),
        "", *group_table(history, "Time to first contact", contact_band),
        "", *group_table([row for row in history if month(row) >= MONTHS[0]], "Recent intake month (UTC)", month),
        "", "## August through October 2026", "",
        "The full outer join uses UTC intake month, channel, and campaign. A dash indicates an unavailable "
        "spend or ratio. Net booked contract value uses recorded wins.", "",
        *spend_table(rollup(cohorts, include_campaign=False)),
        "", "## Campaign detail", "",
        *spend_table(rollup(cohorts, include_campaign=True)),
        "",
    ]
    flat = [
        {"month": month, "channel": channel, "campaign": campaign, **cohort.figures()}
        for (month, channel, campaign), cohort in sorted(cohorts.items())
    ]
    atomic_file(out_dir / "analysis.md", "\n".join(lines).encode())
    atomic_file(out_dir / "spend_cohorts.csv", csv_text(SPEND_COLUMNS, flat).encode())
