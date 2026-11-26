import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime

import phonenumbers

from .files import parse_time, utc_text

CREATE = "lead.created"
UPDATE = "lead.updated"
DELETE = "lead.deletion_requested"
EVENT_ORDER = {CREATE: 0, UPDATE: 1, DELETE: 2}
ENVELOPE = ("event_id", "event_type", "occurred_at_utc", "external_id")
FIELDS = (
    "first_name", "last_name", "email", "phone", "postcode", "country", "language",
    "service", "project_stage", "source", "message", "utm_source", "utm_campaign", "gclid", "fbclid",
)
COLUMNS = set(ENVELOPE + FIELDS + ("deletion_reason",))
CREATE_REQUIRED = (
    "first_name", "last_name", "email", "postcode", "country", "language",
    "service", "project_stage", "source",
)
LANGUAGES = {"de", "en", "pt"}
SERVICES = {"building_permit", "change_of_use", "structural", "fire_safety"}
ENUMS = {
    "source": {"web_form", "chat", "partner_api"},
    "language": LANGUAGES,
    "service": SERVICES,
    "project_stage": {"idea", "planning", "application_submitted", "rejected_needs_fix"},
    "country": phonenumbers.SUPPORTED_REGIONS,
}
LOWERCASE_FIELDS = {
    "email", "source", "language", "service", "project_stage", "utm_source", "utm_campaign",
}
ATTRIBUTION = {"utm_source", "utm_campaign", "gclid", "fbclid"}
ROUTING_FIELDS = ("source", "language", "service")
ID_PATTERN = re.compile(r"[^\s|\x00-\x1f\x7f]{1,128}")
EMAIL_PATTERN = re.compile(
    r"[a-z0-9.!#$%&'*+/=?^_\x60{|}~-]+@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
    re.ASCII,
)
PHONE_PATTERN = re.compile(r"\+?[0-9\s()./-]+", re.ASCII)
JUNK_PATTERN = re.compile(r"(?:asdf(?:gh)?|qwerty|xxx|dummy)[.!]*", re.IGNORECASE)


@dataclass(frozen=True)
class Delivery:
    raw: dict
    extra: list[str]
    problem: str | None

    @property
    def event_id(self) -> str:
        return self.raw.get("event_id", "").strip()

    @property
    def external_id(self) -> str:
        return self.raw.get("external_id", "").strip()


@dataclass(frozen=True)
class Event:
    event_id: str
    kind: str
    external_id: str
    at: datetime
    fields: dict[str, str]

    @property
    def clock(self) -> tuple[str, int, str]:
        return utc_text(self.at, fixed_precision=True), EVENT_ORDER[self.kind], self.event_id


def read_deliveries(content: bytes) -> list[Delivery]:
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
    headers = next(reader, [])
    if len(set(headers)) != len(headers):
        raise ValueError("input has duplicate column names")
    missing = set(ENVELOPE) - set(headers)
    unknown = set(headers) - COLUMNS
    if missing or unknown:
        raise ValueError(f"input header mismatch (missing {sorted(missing)}, unknown {sorted(unknown)})")
    deliveries = []
    for values in reader:
        if not values:
            continue
        problem = None
        if len(values) != len(headers):
            problem = f"CSV row has {len(values)} fields, expected {len(headers)}"
        raw = dict(zip(headers, values))
        deliveries.append(Delivery(raw, values[len(headers):], problem))
    return deliveries


def parse_phone(value: str, country: str | None) -> str:
    try:
        phone = phonenumbers.parse(value, country)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("invalid phone") from exc
    if not phonenumbers.is_possible_number(phone) or len(str(phone.national_number)) < 7:
        raise ValueError("invalid phone")
    return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)


def trusted_phone(value: str) -> str:
    if value and phonenumbers.is_valid_number(phonenumbers.parse(value, None)):
        return value
    return ""


def field_values(raw: dict) -> dict[str, str]:
    fields = {key: raw.get(key, "").strip() for key in FIELDS}
    for key in ("first_name", "last_name"):
        fields[key] = " ".join(fields[key].split())
    for key in LOWERCASE_FIELDS:
        fields[key] = fields[key].casefold()
    fields["country"] = fields["country"].upper()
    return {key: value for key, value in fields.items() if value}


def valid_email(value: str) -> bool:
    local = value.split("@", 1)[0]
    return (
        len(value) <= 254 and len(local) <= 64 and ".." not in local
        and not local.startswith(".") and not local.endswith(".")
        and EMAIL_PATTERN.fullmatch(value) is not None
    )


def validate(delivery: Delivery) -> tuple[Event | None, list[str]]:
    raw = delivery.raw
    kind = raw.get("event_type", "").strip()
    errors = [delivery.problem] if delivery.problem else []
    for key, value in (("event_id", delivery.event_id), ("external_id", delivery.external_id)):
        if not value:
            errors.append(f"missing {key}")
        elif not ID_PATTERN.fullmatch(value):
            errors.append(f"invalid {key}")
    if kind not in EVENT_ORDER:
        errors.append("invalid event_type" if kind else "missing event_type")
    at = None
    try:
        at = parse_time(raw.get("occurred_at_utc", "").strip())
    except (ValueError, OverflowError):
        errors.append("invalid occurred_at_utc" if raw.get("occurred_at_utc", "").strip()
                      else "missing occurred_at_utc")

    fields = field_values(raw)
    if kind == DELETE:
        fields = {key: fields[key] for key in ("source",) if key in fields}
    if kind == CREATE:
        errors.extend(f"missing {key}" for key in CREATE_REQUIRED if key not in fields)
    if kind in EVENT_ORDER:
        for key, choices in ENUMS.items():
            if key in fields and fields[key] not in choices:
                errors.append(f"invalid {key}")
        email, phone = fields.get("email"), fields.get("phone")
        country, postcode = fields.get("country"), fields.get("postcode")
        if email and not valid_email(email):
            errors.append("invalid email")
        if country == "DE" and postcode and not re.fullmatch(r"[0-9]{5}", postcode):
            errors.append("invalid German postcode (expected five digits)")
        if phone:
            if not PHONE_PATTERN.fullmatch(phone) or sum(char.isdigit() for char in phone) < 7:
                errors.append("invalid phone")
            elif country in ENUMS["country"] or phone.startswith("+"):
                try:
                    fields["phone"] = parse_phone(phone, country)
                except ValueError as exc:
                    errors.append(str(exc))
        if kind == CREATE:
            message = fields.get("message", "").casefold()
            name = (fields.get("first_name", "").casefold(), fields.get("last_name", "").casefold())
            if name == ("test", "test") or message == "test bitte ignorieren":
                errors.append("explicit test submission")
            elif JUNK_PATTERN.fullmatch(message):
                errors.append("placeholder-only message")
    if errors:
        return None, errors
    assert at is not None
    return Event(delivery.event_id, kind, delivery.external_id, at, fields), []
