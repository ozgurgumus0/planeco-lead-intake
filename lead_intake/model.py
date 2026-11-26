import csv
import hashlib
import io
import math
import platform
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .features import CATEGORICAL, FEATURES, MISSING, NUMERIC, from_history
from .files import atomic_file, json_text, parse_time, utc_text

MODEL_FILE = "scorer.joblib"
CONTRACT_VERSION = 1
VALIDATION_START = date(2026, 7, 1)
TEST_START = date(2026, 9, 1)
SHORTLIST_SHARE = 0.20
LOGISTIC = {"C": 1.0, "solver": "lbfgs", "max_iter": 1000}
PACKAGES = ("scikit-learn", "numpy", "scipy", "joblib", "threadpoolctl", "tzdata", "phonenumbers")


@dataclass(frozen=True)
class Score:
    probability: float
    expected_value: float


@dataclass(frozen=True)
class Scorer:
    pipeline: Pipeline
    metadata: dict

    @property
    def version(self) -> str:
        return self.metadata["version"]

    def predict(self, vectors: list[dict]) -> list[Score]:
        if not vectors:
            return []
        probabilities = self.pipeline.predict_proba(matrix(vectors))[:, 1]
        return [
            Score(float(p), float(p) * self.metadata["value_given_won_eur"])
            for p in probabilities
        ]

    def feature_health(self, vectors: list[dict]) -> dict[str, int]:
        vocabulary = self.metadata["category_vocabulary"]
        return {
            **{
                name: sum(row[name] == MISSING or row[name] not in vocabulary[name] for row in vectors)
                for name in CATEGORICAL
            },
            "phone absent": sum(not row["phone_given"] for row in vectors),
            "message blank": sum(not row["message_length"] for row in vectors),
        }


def read_history(data: bytes) -> list[dict[str, str]]:
    text = io.StringIO(data.decode("utf-8-sig"), newline="")
    return sorted(
        csv.DictReader(text, strict=True),
        key=lambda row: (parse_time(row["created_at_utc"]), row["lead_id"]),
    )


def matrix(rows):
    return np.asarray([[row[field] for field in FEATURES] for row in rows], dtype=object)


def pipeline(classifier) -> Pipeline:
    preprocessing = ColumnTransformer([
        ("categories", OneHotEncoder(handle_unknown="ignore"), list(range(len(CATEGORICAL)))),
        ("numbers", StandardScaler(), list(range(len(CATEGORICAL), len(FEATURES)))),
    ])
    return Pipeline([("preprocess", preprocessing), ("classifier", classifier)])


def wilson(wins: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.95996398454
    rate = wins / n
    denominator = 1 + z * z / n
    center = (rate + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
    return [max(0, center - radius), min(1, center + radius)]


def metrics(y, probabilities) -> dict:
    y, probabilities = np.asarray(y), np.asarray(probabilities)
    n = len(y)
    shortlist = math.ceil(SHORTLIST_SHARE * n)
    top = np.argsort(-probabilities, kind="stable")[:shortlist]
    top_wins = int(y[top].sum())
    prevalence = float(y.mean())
    precision = top_wins / shortlist
    return {
        "n": n,
        "wins": int(y.sum()),
        "prevalence": prevalence,
        "average_precision": float(average_precision_score(y, probabilities)),
        "brier": float(brier_score_loss(y, probabilities)),
        "shortlist_n": shortlist,
        "shortlist_wins": top_wins,
        "precision": precision,
        "precision_ci95": wilson(top_wins, shortlist),
        "lift": precision / prevalence,
    }


def calibration(y, p) -> list[dict]:
    boundaries = (0.0, 0.10, 0.20, 0.30, 0.50, 1.0)
    bands = []
    for low, high in zip(boundaries, boundaries[1:]):
        mask = (p >= low) & (p <= high if high == 1 else p < high)
        n = int(mask.sum())
        bands.append({
            "low": low,
            "high": high,
            "n": n,
            "mean_prediction": float(p[mask].mean()) if n else None,
            "win_rate": float(y[mask].mean()) if n else None,
            "win_rate_ci95": wilson(int(y[mask].sum()), n),
        })
    return bands


def test_intervals(y, p) -> dict:
    rng = np.random.default_rng(20261126)
    samples = []
    for _ in range(600):
        sample = rng.integers(0, len(y), size=len(y))
        ys, ps = y[sample], p[sample]
        if 0 < ys.sum() < len(ys):
            samples.append((average_precision_score(ys, ps), brier_score_loss(ys, ps)))
    limits = np.quantile(samples, [0.025, 0.975], axis=0)
    return {"average_precision_ci95": limits[:, 0].tolist(), "brier_ci95": limits[:, 1].tolist()}


def chronological_splits(history, validation_start: date, test_start: date) -> dict:
    if validation_start >= test_start:
        raise ValueError("validation start must precede test start")
    cutoffs = [datetime.combine(day, time.min, timezone.utc) for day in (validation_start, test_start)]
    names = ("train", "validation", "test")
    groups = {name: [] for name in names}
    for row in history:
        when = parse_time(row["created_at_utc"])
        name = names[sum(when >= cutoff for cutoff in cutoffs)]
        groups[name].append(row)
    for name, rows in groups.items():
        if {row["won"] for row in rows} != {"0", "1"}:
            raise ValueError(f"{name} must contain both binary won outcomes")
    return groups


def fit_model(
    history_data: bytes, validation_start: date = VALIDATION_START, test_start: date = TEST_START
) -> tuple[Scorer, dict]:
    history = read_history(history_data)
    groups = chronological_splits(history, validation_start, test_start)
    vectors = {name: [from_history(row) for row in rows] for name, rows in groups.items()}
    features = {name: matrix(rows) for name, rows in vectors.items()}
    targets = {name: np.asarray([int(row["won"]) for row in rows]) for name, rows in groups.items()}
    candidates = {
        "historical win rate": pipeline(DummyClassifier(strategy="prior")),
        "logistic regression": pipeline(LogisticRegression(**LOGISTIC)),
    }
    validation = {}
    for name, candidate in candidates.items():
        candidate.fit(features["train"], targets["train"])
        probabilities = candidate.predict_proba(features["validation"])[:, 1]
        validation[name] = metrics(targets["validation"], probabilities)

    baseline = validation["historical win rate"]
    learned = validation["logistic regression"]
    choose_logistic = (
        learned["average_precision"] >= baseline["average_precision"] + 0.01
        and learned["brier"] <= baseline["brier"] + 0.002
    )
    chosen = "logistic regression" if choose_logistic else "historical win rate"
    selected = candidates[chosen]
    values = [float(row["contract_value_eur_net"]) for row in groups["train"] if row["won"] == "1"]
    if not all(math.isfinite(value) and value >= 0 for value in values):
        raise ValueError("invalid training win contract value")
    metadata = {
        "contract_version": CONTRACT_VERSION,
        "feature_schema": {**dict.fromkeys(CATEGORICAL, "category"), **dict.fromkeys(NUMERIC, "integer")},
        "feature_order": FEATURES,
        "classifier": chosen,
        "logistic_parameters": LOGISTIC,
        "history_sha256": hashlib.sha256(history_data).hexdigest(),
        "validation_start_utc": validation_start.isoformat(),
        "test_start_utc": test_start.isoformat(),
        "splits": {
            name: {
                "first_utc": utc_text(parse_time(rows[0]["created_at_utc"])),
                "last_utc": utc_text(parse_time(rows[-1]["created_at_utc"])),
                "n": len(rows),
                "wins": int(targets[name].sum()),
                "prevalence": float(targets[name].mean()),
            }
            for name, rows in groups.items()
        },
        "value_given_won_eur": math.fsum(values) / len(values),
        "value_sample_wins": len(values),
        "category_vocabulary": {
            key: sorted({row[key] for row in vectors["train"]}) for key in CATEGORICAL
        },
        "dependency_versions": {**{name: version(name) for name in PACKAGES}, "python": platform.python_version()},
    }
    metadata["version"] = "planeco-" + hashlib.sha256(json_text(metadata).encode()).hexdigest()[:12]
    y = targets["test"]
    p = selected.predict_proba(features["test"])[:, 1]
    test_baseline = candidates["historical win rate"].predict_proba(features["test"])[:, 1]
    services = {}
    for service in sorted({row["service"] for row in vectors["test"]}):
        mask = np.asarray([row["service"] == service for row in vectors["test"]])
        if mask.sum() >= 70 and 0 < y[mask].sum() < mask.sum():
            services[service] = metrics(y[mask], p[mask])
    report = {
        "model": metadata,
        "validation": validation,
        "test_baseline": metrics(y, test_baseline),
        "test": {**metrics(y, p), **test_intervals(y, p)},
        "calibration": calibration(y, p),
        "test_by_service": services,
        "holdout_value": {
            "predicted_net_eur": math.fsum(p) * metadata["value_given_won_eur"],
            "observed_net_eur": math.fsum(
                float(row["contract_value_eur_net"]) for row in groups["test"] if row["won"] == "1"
            ),
        },
    }
    return Scorer(selected, metadata), report


def percent(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def interval(values, *, as_percent=False) -> str:
    if values is None:
        return "—"
    return " to ".join(percent(value) if as_percent else f"{value:.3f}" for value in values)


def evaluation_markdown(report: dict) -> str:
    model = report["model"]
    lines = [
        "# Chronological model evaluation", "",
        f"Selected **{model['classifier']}**, version **{model['version']}**.", "",
        "Selection requires a validation average precision gain of at least 0.010 and a Brier loss "
        "increase of at most 0.002. The test window is scored after model selection. "
        "Probabilities use the fitted classifier directly.", "",
        "| Split | First intake (UTC) | Last intake (UTC) | Leads | Wins | Win rate |",
        "|---|---|---|---:|---:|---:|",
    ]
    for name, split in model["splits"].items():
        lines.append(
            f"| {name} | {split['first_utc']} | {split['last_utc']} | {split['n']} | "
            f"{split['wins']} | {percent(split['prevalence'])} |"
        )
    lines += [
        "", "The call-first list is the top 20%, rounded up. This is an evaluation assumption. "
        "Ties use chronological intake and then historical lead ID.", "",
        "| Window and model | Avg. precision | Brier | Top wins / leads | Top precision | Lift |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    results = [(f"Validation · {name}", metric) for name, metric in report["validation"].items()]
    results += [("Test · historical win rate", report["test_baseline"]), ("Test · selected", report["test"])]
    results += [(f"Test · {s}", metric) for s, metric in report["test_by_service"].items()]
    for name, measure in results:
        lines.append(
            f"| {name} | {measure['average_precision']:.3f} | {measure['brier']:.3f} | "
            f"{measure['shortlist_wins']}/{measure['shortlist_n']} | "
            f"{percent(measure['precision'])} | {measure['lift']:.2f}× |"
        )
    test = report["test"]
    lines += [
        "", f"Selected test 95% row-bootstrap intervals are "
        f"{interval(test['average_precision_ci95'])} for average precision and "
        f"{interval(test['brier_ci95'])} for Brier. Shortlist precision has a Wilson interval of "
        f"{interval(test['precision_ci95'], as_percent=True)}. These intervals reflect sample size "
        "and do not account for outcome maturity or time drift.", "",
        "Segments have at least 70 holdout leads. Each segment uses its own top 20%.", "",
        "| Test probability band | Leads | Mean score | Observed rate | Observed 95% interval |",
        "|---|---:|---:|---:|---:|",
    ]
    for band in report["calibration"]:
        lines.append(
            f"| {band['low']:.2f}–{band['high']:.2f} | {band['n']} | "
            f"{percent(band['mean_prediction'])} | {percent(band['win_rate'])} | "
            f"{interval(band['win_rate_ci95'], as_percent=True)} |"
        )
    lines += [
        "", f"The mean net contract value given a win is €{model['value_given_won_eur']:,.2f} "
        f"across {model['value_sample_wins']} training wins.", "",
        f"Across {test['n']} holdout leads, the expected values sum to "
        f"€{report['holdout_value']['predicted_net_eur']:,.2f}. Recorded wins total "
        f"€{report['holdout_value']['observed_net_eur']:,.2f} in net contract value. "
        "This checks aggregate value against the supplied outcome snapshot.", "",
    ]
    return "\n".join(lines)


def train(history: Path, model_dir: Path, validation_start=VALIDATION_START, test_start=TEST_START) -> Scorer:
    scorer, report = fit_model(history.read_bytes(), validation_start, test_start)
    payload = io.BytesIO()
    joblib.dump(scorer, payload, compress=3)
    atomic_file(model_dir / "evaluation.json", (json_text(report) + "\n").encode())
    atomic_file(model_dir / "evaluation.md", evaluation_markdown(report).encode())
    atomic_file(model_dir / MODEL_FILE, payload.getvalue())
    return scorer


def load_model(model_dir: Path) -> Scorer:
    artifact = model_dir / MODEL_FILE
    scorer = joblib.load(artifact)
    if not isinstance(scorer, Scorer) or scorer.metadata["contract_version"] != CONTRACT_VERSION:
        raise ValueError(f"incompatible scorer in {artifact}; retrain with this code")
    return scorer
