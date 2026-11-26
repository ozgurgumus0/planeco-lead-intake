# PlaneCo lead intake

A local workspace that validates and deduplicates enquiries, scores their chance of becoming signed deals, routes them to eligible reps, and exports the assignment’s CRM and rejection CSVs. [Original brief](data/Case_Automation_Assignment.pdf)

## Open the dashboard

1. On Windows, extract the project and double-click **Start Planeco Dashboard.cmd**. It uses Python 3.11 or later, installs the pinned dependencies on first launch, and opens the browser. Keep the launcher window open.
2. In **CSV intake**, select **Run initial sample** or upload the original [assignment CSV](data/lead_events.csv). A fresh workspace generates **538 new CRM rows** and **18 rejected rows**. The rejection reasons open automatically. Use **CRM CSV** and **Rejected CSV** to download the two outputs.
3. Run the same file again to see zero new CRM rows, then run the follow-up sample to see new leads, updates, and a deletion. Switch to **Dashboard** for daily arrivals, source and channel mix, rep capacity, and historical campaign economics. **Recent batches** reopens previous results.

The dashboard reads the same SQLite state as the CLI. It refreshes after a batch and every 15 minutes while open. Date filters use Lisbon days and end on the last recorded arrival so the fixture stays visible.

## Assignment answers

### 1. What predicts a signed deal?

Project stage and phone availability showed strong differences in the 3,200 historical leads. A prior rejection that needed fixing won **34.6%** of the time (170 of 492) versus **7.1%** at the idea stage (69 of 969). Leads with a phone number won **21.9%** versus **10.1%** without one. In the later 364-lead holdout, the model’s top 73 contained **28 eventual wins**, or **38.4%** precision, against a **17.9%** overall win rate. Faster contact was associated with wins. The historical records leave the effect of faster contact and the maturity of some outcomes uncertain.

### 2. What value would I send to Google and Meta?

Send **predicted win probability × €3,737.78**, rounded to cents. The multiplier is the average net booked contract value across the 445 training wins. A lead with a 30% probability gets **€1,121.33**. Send one value per valid, deduplicated lead with its original attribution and a stable event ID. This gives bidding a signal proportional to each lead’s estimated booked value.

### 3. Where would I spend more or less?

Test moving about **€1,600 from Bing generic search to Partner** if the partner desk has capacity. In the August–October cohorts, Partner generated **27 wins at €586 per win** and **€6.45 booked per €1 spent**. Bing generated **7 wins at €2,303 per win** and **€1.23 booked per €1 spent**. Bing’s small sample and the observational data make this a measured budget test. Add cost per completed and qualified call alongside cost per lead and per win once the team records call outcomes.

### 4. How would I run it every few minutes without losing or duplicating leads?

Persist each delivery and its cursor before acknowledging it. Validate, deduplicate, and enqueue CRM changes in one database transaction. Apply changes by stable lead ID and version, retry transient failures, and reconcile confirmations before repeating ambiguous writes. Track time to first contact, queue and capacity, and score quality on mature outcomes. Run intake every few minutes, refresh the dashboard every **15 minutes**, and publish daily campaign and call reports. Future call logs would capture demonstrated interest after each conversation. Join them to closed-deal outcomes to evaluate rep matching.

The standalone submission write-up includes the confidence intervals, channel table, attribution details, and call and routing plans. [Full case answers](case_answers.md)

## Setup and run

Use Python 3.11 or later. Run from the repository root. After dependency installation, training and intake run locally without credentials.

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt
& $python -m unittest discover -s tests -v
& $python -m lead_intake train --data-dir data --model-dir artifacts/model
& $python -m lead_intake analyze --data-dir data --out-dir out/analysis
```

On macOS or Linux use `.venv/bin/python` in place of `& $python`. On Windows, if `python` resolves to the Microsoft Store alias, use the path to your installed Python to create the environment.

```powershell
& $python -m lead_intake run --input data/lead_events.csv --data-dir data --state state/intake.sqlite3 --model-dir artifacts/model --out-dir out/initial
```

This command also trains the scorer if the model directory has no saved scorer. Warm runs reuse the saved artifact. Run `train` explicitly to replace it. The training command writes `evaluation.md` and `evaluation.json` beside `scorer.joblib`. The analysis command writes the descriptive tables and the month-by-channel-by-campaign join.

To start the dashboard from the terminal after setup, run:

```powershell
& $python -m lead_intake serve
```

Use `--state`, `--model-dir`, `--out-dir`, and `--port` to select an isolated demo workspace. The server listens on loopback. Accepted first arrivals remain in aggregate counts through later updates and erasure. A pre-existing database seeds its initial chart from surviving leads. Capacity and queue figures show current state. Campaign economics uses the separate August–October historical cohorts.

## Demo on the same database and model

```powershell
& $python -m lead_intake run --input data/lead_events.csv --data-dir data --state state/intake.sqlite3 --model-dir artifacts/model --out-dir out/replay
& $python -m lead_intake run --input demo/live_events.csv --data-dir data --state state/intake.sqlite3 --model-dir artifacts/model --out-dir out/live
& $python -m lead_intake actions --state state/intake.sqlite3
```

For the interview file, substitute its path and use an unused output folder.

```powershell
& $python -m lead_intake run --input "C:\path\to\interview.csv" --data-dir data --state state/intake.sqlite3 --model-dir artifacts/model --out-dir out/interview
```

| Verified run on Python 3.12 | Deliveries | New CRM rows | Rejection rows | CRM updates | CRM deletes |
|---|---:|---:|---:|---:|---:|
| Initial | 648 | 538 | 18 | 0 | 0 |
| Unchanged replay | 648 | 0 | 0 | 0 | 0 |
| Live example | 5 | 1 | 0 | 2 | 1 |

Open the initial `run_report.md` first. The 648 deliveries break down into 40 event-ID repeats, 18 rejections, 541 canonical creates, 29 merges, 17 applied patches, and three deletions. All 135 active slots fill. Of the 403 queued leads, 402 have compatible reps at capacity and one lacks coverage. There are 111 leads in the queue with a score of at least 0.25.

Then compare the three CRM files. The replay contains just the header. In the live example, a cross-ID duplicate and a scoring-field patch produce one update to the original canonical ID. A deletion submitted through an alias produces one delete, and the released slot promotes a queued lead, producing the second update. The exact stale delivery is counted as a repeat. The new Portuguese-language partner lead is queued to `rep_06`. Both batches use scorer `planeco-061f236f6fb0` under the verified dependency and Python versions.

### One lead from intake to handoff

A German-language structural enquiry arrived by chat at 08:12 Lisbon time on November 16. It was at the planning stage, had a phone number, and came through organic search. The model gave it a win probability of **0.227415**. Multiplying the full-precision probability by the training mean net value per win of €3,737.78 gives **€850.03**. `rep_04` was on shift, covered both the service and language, and had zero open assignments against a capacity of 20. The live patch changes the stage to `rejected_needs_fix`, raising the score to **0.427585** and the value to **€1,598.22**. The same owner and assignment time are retained.

## Input, identity, and routing defaults

- A create requires source, names, email, country, postcode, language, service, and stage. All event types require IDs, an event type, and an ISO timestamp with an explicit timezone. Updates and deletion requests can use a sparse CSV header with the event envelope. Extra columns and broken CSV quoting are file errors. Rows with a different field count are rejected individually.
- Names have whitespace collapsed and emails are trimmed and case-folded. Germany requires five postcode digits, kept as text. Phone is optional. A populated phone must be plausible and is written in E.164. Only numbers that pass numbering-plan validation can serve as identity keys. `.example` email domains are valid. Filtering targets explicit test submissions and exact placeholder-only messages.
- The first payload received for an event ID is authoritative. Its keyed fingerprint lets exact repeats be ignored even after a lead is erased. Reusing that ID with a different payload produces a rejection. Valid events in a batch are applied by UTC timestamp, then create, update, delete, and event ID. The first accepted canonical ID stays fixed across later files.
- Exact normalized email is the primary contact match. A new ID that matches only a trusted phone also needs agreement on normalized names. Conflicting owners and reused lead IDs with neither matching contact key go to review. Merges fill empty phone and message fields. The canonical create keeps its other values, including its entire first-touch marketing bundle. Aliases are sorted lexically and joined with `|`.
- A nonblank patch may change a mutable field, including source and attribution. Blank fields leave existing values in place. Each field uses event time and a deterministic tie-breaker. A patch can precede its create in delivery order or a prior file. When necessary, the patch waits in SQLite until an ID or alias is established. Edits to the feature vector trigger rescoring, and eligible owners keep their assignment.
- Deletion erases the live record, pending patches, and old update payloads. Tombstones mark the canonical ID and known aliases, or the requested ID if no lead exists yet. A later event for a tombstoned ID is suppressed. The demo matches current contact values and has no way to link a never-seen ID to erased contact details.
- Shifts use Europe/Lisbon, Monday through Friday, with inclusive absence dates. A compatible rep with a free slot and a shift at arrival comes first. Otherwise the program uses the earliest eligible start. At the same time, lowest open-to-capacity ratio wins, with ties by rep ID. For probabilities of at least 0.25, seniority can break the rule within five percentage points of the lowest load. Scheduled future assignments reserve a slot.
- The appointed manager is the only active option for partner leads. Every fallback uses `rep_06` with the exact reason `queue` and a blank assignment time. A queue entry reserves zero slots. Once capacity is released, the oldest compatible waiting lead can be promoted using the latest accepted event time. In the fixtures, open assignments stay open until deletion. A production CRM status sync should free slots when deals close.

## Feature contract and evaluation

| Intake input | Shared model feature |
|---|---|
| Service, stage, language | Normalized categorical fields |
| `utm_source`, `utm_campaign` | Historical `channel` and `campaign`. Empty channel uses `partner` for partner API and `unknown` otherwise. Empty campaign uses `unknown`. |
| Validated phone present | Binary `phone_given` |
| Message | Length in characters after trimming. Blank is zero. Historical lengths are assumed to follow the same definition. |

The scoring adapter admits only these seven features. Routing uses `source`, with the partner fallback above supplying attribution when needed. IDs, click IDs, names, outcomes, and first-contact timing stay outside the feature vector. The historical weekend flag disagrees with the Lisbon date in 1,202 of 3,200 rows and is excluded. Unseen categories use the trained encoder's unknown handling.

Training fits the encoder, scaler, classifier, and mean won contract value on the training window. July–August 2026 chooses between the historical-prior baseline and regularized logistic regression. September–November 2 is the untouched test period for selection. The 20% shortlist is an evaluation assumption. The artifact records its feature order, source-data hash, model version, dependency versions, and split bounds. Exported scores have six decimal places and expected euro values have two. Routing and valuation use unrounded probabilities.

Logistic regression improved validation average precision from 0.217 to 0.457 and Brier loss from 0.171 to 0.147. On the final 364-lead test it achieved 0.370 average precision and 0.132 Brier loss. The top 73 contained 28 wins, giving 38.4% precision at 17.9% test prevalence. The worked run, statistics, and limitations are in [the case answers](case_answers.md).

## CRM handoff and recovery

`crm_import.csv` carries new canonical leads only and follows the supplied template. `rejected.csv` gives event identifiers and reasons. `run_report.md` reconciles deliveries, leads, slots, and scoring health without contact details. The outbox holds updates and erasures for canonical IDs with an earlier generated export.

```powershell
& $python -m lead_intake exports --state state/intake.sqlite3
& $python -m lead_intake actions --state state/intake.sqlite3
& $python -m lead_intake ack-export --state state/intake.sqlite3 --batch-id $batchId
& $python -m lead_intake ack-action --state state/intake.sqlite3 --action-id $actionId
& $python -m lead_intake recover --state state/intake.sqlite3
```

Set `$batchId` and `$actionId` from the corresponding values in `exports` and `actions`. Import each published delta using CRM upsert by `external_id`. Apply pending actions in order by `action_id` after the relevant initial import. Acknowledge each operation after CRM success. Published and CRM-confirmed timestamps are separate. Reconcile by canonical ID before retrying a partial or ambiguous CRM response. Skip tombstoned IDs if an old CSV must be processed.

SQLite commits deduplication, snapshots, outbox records, and the pending export as one write transaction. The publisher writes all three files in a sibling staging directory and renames it into place. It verifies file hashes if the directory appeared before a crash, then records publication. If publication fails, make the recorded destination writable and retry the original command or use `recover`. An identical completed command at the same path returns its recorded result when the file hashes match. A replay to a new path generates a header-only CRM delta. Use a new or empty directory for new output.

The operator manages previously published CSVs and the input retention policy. In production, use authenticated durable receipt, a transactional outbox worker, stable CRM IDs and monotonic versions, retention and erasure propagation to downstream stores, and a closure feed.

## Three decisions to defend

1. **Model choice.** The prior is the reference and fallback. Logistic regression earned its place in validation on both ranking and probability error before the test was opened.
2. **Train and serve agreement.** Both adapters return the same ordered feature types. The fitted preprocessor travels with the classifier, and tests check feature equivalence and identical predictions after reload.
3. **Retry semantics.** The database remembers the exact export to publish. A retry completes the pending publication before it processes new deliveries. The CRM still needs idempotent writes and independent acknowledgment.
