import csv
import io
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)


def parse_time(value: str) -> datetime:
    if not ISO_TIMESTAMP.fullmatch(value):
        raise ValueError("expected an ISO timestamp with explicit timezone")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def utc_text(when: datetime, *, fixed_precision: bool = False) -> str:
    precision = "microseconds" if fixed_precision or when.microsecond else "seconds"
    return when.astimezone(timezone.utc).isoformat(timespec=precision).replace("+00:00", "Z")


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def csv_text(headers, rows) -> str:
    content = io.StringIO(newline="")
    writer = csv.DictWriter(content, fieldnames=headers, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return content.getvalue()


def read_table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file, strict=True))


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary, path)
            sync_directory(path.parent)
        finally:
            file.close()
            temporary.unlink(missing_ok=True)
