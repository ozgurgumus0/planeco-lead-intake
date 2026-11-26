from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

from .events import LANGUAGES, SERVICES
from .files import parse_time, read_table, utc_text

LISBON = ZoneInfo("Europe/Lisbon")
QUEUE_OWNER = "rep_06"
HIGH_SCORE = 0.25
COMPARABLE_LOAD = Fraction(1, 20)
SENIORITY = {"senior": 0, "mid": 1, "junior": 2}


@dataclass(frozen=True)
class Rep:
    rep_id: str
    languages: frozenset[str]
    services: frozenset[str]
    maximum: int
    start: time
    end: time
    absences: tuple[tuple[date, date], ...]
    seniority: str
    partner_manager: bool

    def covers(self, lead: dict) -> bool:
        return (
            lead["language"] in self.languages
            and lead["service"] in self.services
            and (lead["source"] != "partner_api" or self.partner_manager)
        )

    def works_on(self, day: date) -> bool:
        return day.weekday() < 5 and not any(start <= day <= end for start, end in self.absences)

    def on_shift(self, when: datetime) -> bool:
        local = when.astimezone(LISBON)
        return self.works_on(local.date()) and self.start <= local.time() < self.end

    def next_available(self, arrival: datetime) -> datetime:
        if self.on_shift(arrival):
            return arrival
        local = arrival.astimezone(LISBON)
        day = local.date()
        if local.time() >= self.end:
            day += timedelta(days=1)
        while not self.works_on(day):
            day += timedelta(days=1)
        return datetime.combine(day, self.start, LISBON).astimezone(timezone.utc)


@dataclass(frozen=True)
class Assignment:
    owner: str
    at_utc: str
    reason: str

    @property
    def active(self) -> bool:
        return bool(self.at_utc)

    @property
    def lisbon_text(self) -> str:
        if not self.active:
            return ""
        return parse_time(self.at_utc).astimezone(LISBON).strftime("%Y-%m-%d %H:%M")


def load_roster(path: Path) -> dict[str, Rep]:
    reps = {}
    for row in read_table(path):
        rep_id = row["rep_id"].strip()
        languages = frozenset(value.strip() for value in row["languages"].split(";"))
        services = frozenset(value.strip() for value in row["services"].split(";"))
        start, end = map(time.fromisoformat, row["working_hours_lisbon"].split("-"))
        absences = tuple(
            tuple(map(date.fromisoformat, span.strip().split("..")))
            for span in row["absent"].split(";") if span.strip()
        )
        maximum = int(row["max_open_leads"])
        seniority = row["seniority"].strip()
        manager = row["partner_manager"].strip()
        if (
            not rep_id or rep_id in reps or not languages <= LANGUAGES or not services <= SERVICES
            or maximum < 0 or start.tzinfo or end.tzinfo or start >= end
            or any(first > last for first, last in absences)
            or seniority not in SENIORITY or manager not in {"yes", "no"}
        ):
            raise ValueError(f"invalid sales_team.csv configuration for {rep_id}")
        reps[rep_id] = Rep(rep_id, languages, services, maximum, start, end,
                           absences, seniority, manager == "yes")
    if QUEUE_OWNER not in reps or sum(rep.partner_manager for rep in reps.values()) != 1:
        raise ValueError("roster needs rep_06 and exactly one partner manager")
    return reps


def still_eligible(assignment: Assignment, lead: dict, roster: dict, loads: dict) -> bool:
    rep = roster.get(assignment.owner)
    return (
        assignment.active and rep is not None and rep.covers(lead)
        and loads.get(rep.rep_id, 0) < rep.maximum and rep.on_shift(parse_time(assignment.at_utc))
    )


def assign(
    lead: dict, probability: float, arrival: datetime, roster: dict, loads: dict, *, reconsidered: bool = False
) -> Assignment:
    candidates = []
    for rep in roster.values():
        open_leads = loads.get(rep.rep_id, 0)
        if rep.covers(lead) and open_leads < rep.maximum:
            candidates.append((rep.next_available(arrival), Fraction(open_leads, rep.maximum), rep))
    if not candidates:
        return Assignment(QUEUE_OWNER, "", "queue")

    earliest = min(slot for slot, _, _ in candidates)
    available = [(load, rep) for slot, load, rep in candidates if slot == earliest]
    lowest, fair_rep = min(available, key=lambda candidate: (candidate[0], candidate[1].rep_id))
    selected = fair_rep
    if probability >= HIGH_SCORE:
        comparable = [(load, rep) for load, rep in available if load <= lowest + COMPARABLE_LOAD]
        _, selected = min(comparable, key=lambda item: (SENIORITY[item[1].seniority], item[0], item[1].rep_id))

    local = earliest.astimezone(LISBON)
    if earliest == arrival:
        timing = "available when reconsidered" if reconsidered else "available at arrival"
    else:
        timing = f"next shift {local:%Y-%m-%d %H:%M}"
    if lead["source"] == "partner_api":
        rule = "partner manager"
    elif selected != fair_rep:
        rule = "seniority preference within 5 percentage points of lowest open load"
    else:
        rule = f"lowest open load {loads.get(selected.rep_id, 0)}/{selected.maximum}"
    reason = f"{lead['service']} + {lead['language']} | {timing} | {rule}"
    return Assignment(selected.rep_id, utc_text(earliest), reason)
