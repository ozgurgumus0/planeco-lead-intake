"""Adapters from both input schemas to one explicit pre-contact feature vector."""

CATEGORICAL = ("service", "project_stage", "language", "channel", "campaign")
NUMERIC = ("phone_given", "message_length")
FEATURES = CATEGORICAL + NUMERIC
MISSING = "unknown"


def category(value: str | None) -> str:
    return (value or "").strip().casefold() or MISSING


def prepare(row, *, channel, campaign, phone_given, message_length) -> dict[str, str | int]:
    return {
        "service": category(row.get("service")),
        "project_stage": category(row.get("project_stage")),
        "language": category(row.get("language")),
        "channel": category(channel),
        "campaign": category(campaign),
        "phone_given": int(phone_given),
        "message_length": message_length,
    }


def from_intake(row) -> dict[str, str | int]:
    channel = category(row.get("utm_source"))
    if channel == MISSING and category(row.get("source")) == "partner_api":
        channel = "partner"
    return prepare(
        row,
        channel=channel,
        campaign=row.get("utm_campaign"),
        phone_given=bool((row.get("phone") or "").strip()),
        message_length=len((row.get("message") or "").strip()),
    )


def from_history(row) -> dict[str, str | int]:
    phone = (row.get("phone_given") or "").strip().casefold()
    if phone not in {"yes", "no", "1", "0", ""}:
        raise ValueError("invalid historical phone_given value")
    length = int(row.get("message_length") or "0")
    if length < 0:
        raise ValueError("negative historical message_length")
    return prepare(
        row,
        channel=row.get("channel"),
        campaign=row.get("campaign"),
        phone_given=phone in {"yes", "1"},
        message_length=length,
    )
