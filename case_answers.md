# PlaneCo case answers

Reproduce the numbers from the supplied fixtures after setup in the [README](README.md).

```powershell
& $python -m lead_intake train --data-dir data --model-dir artifacts/model
& $python -m lead_intake analyze --data-dir data --out-dir out/analysis
```

## 1. What predicts a win, and how sure am I?

Project stage, phone presence, and attribution show large differences in the 3,200 recorded leads. The rates below are descriptive and have Wilson 95% intervals.

| Group | Wins / leads | Win rate | 95% interval |
|---|---:|---:|---:|
| Rejected application that needs a fix | 170 / 492 | 34.6% | 30.5–38.9% |
| Application submitted | 118 / 477 | 24.7% | 21.1–28.8% |
| Planning | 239 / 1,262 | 18.9% | 16.9–21.2% |
| Idea | 69 / 969 | 7.1% | 5.7–8.9% |
| Phone given | 507 / 2,320 | 21.9% | 20.2–23.6% |
| Phone absent | 89 / 880 | 10.1% | 8.3–12.3% |

The stage gap between fix-needed and idea is 27.5 percentage points. In the full history, partner converted 134/419 (32.0%) and Meta 86/795 (10.8%). Lead intent, offer mix, routing, and attribution can all contribute to those gaps. Faster first contact also correlates with wins. The recorded rates were 398/1,696 (23.5%) within 30 minutes, 171/1,098 (15.6%) at 31–120 minutes, and 27/406 (6.7%) after 120 minutes. Rep availability and the existing tendency to call promising leads first could explain some of that pattern. A prospective response-time experiment can estimate the effect of changing the policy. First-contact timing is excluded from the intake score.

The training window was June 2025–June 2026 (2,439 leads, 445 wins). Logistic regression beat the prior on July–August 2026 validation (397 leads, 86 wins) with average precision of 0.457 vs. 0.217 and Brier loss of 0.147 vs. 0.171. The September 1–November 2 holdout contained 65 wins from 364 leads (17.9%). The chosen model achieved 0.370 average precision (95% row-bootstrap interval 0.278–0.502) and 0.132 Brier loss (prior 0.147). The top 20% assumption found **28 wins in 73 calls**, or **38.4% precision** (Wilson 28.1–49.8%) and **2.15× lift** over test prevalence. Building permit had 11 wins in its 25-lead shortlist (44.0%). Structural had 8 in 27 (29.6%). The evaluation report includes the split bounds and reliability table. In the 0.30–0.50 band, 55 leads averaged 38.4% predicted and won at 32.7%. The 0.50+ band had only ten leads.

The history has no close timestamps or label snapshot time. The 15 November records are especially recent, with five recorded wins. A zero can represent a loss or an open lead. The holdout measures the supplied snapshot and may give a different picture once leads have had equal time to close. In production, I would use outcome timestamps, measure the sales cycle, and evaluate rolling cohorts after the same maturity window. That would also let training observe only the labels available at each simulated fitting date.

## 2. What value would I send to Google and Meta?

Send **`round(P(won) × training mean net contract value per win, 2)` EUR** once for the first valid, deduplicated, attributable canonical lead. For a lead at 0.30, the 445-win training mean of €3,737.78 gives **€1,121.33**. Dispatch soon after committing the lead and initial score, with its original conversion time. A flat value gives every lead the same bidding signal. This rule reflects measured differences in win likelihood. The pooled contract average gives thin campaign segments a common, stable price per predicted win.

On the holdout, summed expected value was €241,812.01 against €238,550.00 in recorded net booked contracts. That is a check on the total, with substantial uncertainty for individual leads and sparse campaigns. Start with a limited comparison against flat values. Channel and campaign are model inputs and can reinforce the current mix when fed back into bidding. Monitor traffic mix, match rates, and calibration on mature leads.

- **Google Ads.** Use Data Manager with the original `gclid` where captured, the EUR amount, and a stable `transactionId` tied to the canonical lead and conversion action. Use capture-time attribution, with additional identifiers only when the account has the necessary consent and configuration. [Google Data Manager offline conversions](https://developers.google.com/data-manager/api/devguides/events/google-ads/offline/send-events)
- **Meta.** Send a Conversions API Lead event with `value`, `currency=EUR`, and a stable event ID. Pass the stored `_fbc` as `fbc`, or form it from the unchanged `fbclid` and the recorded click-ID observation time. Capture that time at receipt. If Pixel and server both send the same event, use a matching event name and ID. Keep leads with missing matching data visible in internal reporting. [Meta click-ID guidance](https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/fbp-and-fbc) · [Meta event deduplication](https://developers.facebook.com/documentation/ads-commerce/conversions-api/deduplicate-pixel-and-server-events)

Record actual wins and euro amounts against the same canonical lead in the CRM. If exporting them for measurement, use a separately named, idempotent win event and keep it out of the bidding objective while the lead-value event is the primary signal. Internal revenue reporting counts booked contracts once using the CRM outcome. The local demo computes the value and keeps click IDs for the handoff.

## 3. Where would I spend more or less?

August–October 2026. Join by UTC intake month, channel, and campaign, then total by channel. Wins and value follow the intake cohort. Net booked value sums contract amounts for recorded wins.

| Channel | Spend | Leads | Wins | Win rate | Cost / lead | Cost / win | Net booked | Net booked / spend |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Partner | €15,820 | 69 | 27 | 39.1% | €229.28 | €585.93 | €101,970 | 6.45× |
| Google | €55,010 | 232 | 43 | 18.5% | €237.11 | €1,279.30 | €162,230 | 2.95× |
| Meta | €38,860 | 138 | 18 | 13.0% | €281.59 | €2,158.89 | €70,860 | 1.82× |
| Bing | €16,120 | 33 | 7 | 21.2% | €488.48 | €2,302.86 | €19,760 | 1.23× |
| Organic | Unavailable | 71 | 11 | 15.5% | — | — | €42,830 | — |

I would increase partner spend in a small test once the partner desk can handle the incoming leads. `immo_portal` produced 17 wins from 36 leads at €470 per win and 8.88× booked value per euro spent. I would decrease Bing generic search first and use about €1,600, roughly 10% of its observed spend, as the scale for a measured reallocation. Bing's seven wins are a small sample. I would also trim Meta's `lead_ads_umbau` (€2,784 per win, 1.24×) before cutting Meta retargeting (2.61×). I would hold Google's total initially and favor testing `search_bauantrag` (€937 per win, 15 wins) within it.

These are average attributed results. Delivery mix, contract maturity, branded demand, partner capacity, and ad attribution all affect the comparison. Booked value excludes service delivery costs and collection risk. A controlled budget test with a fixed wait for wins should measure incremental net booked value and time to first contact.

## 4. How would I run it every few minutes?

1. **Receive and reconcile.** Persist each source delivery and its cursor before acknowledging receipt. Use a source-scoped idempotency key in production. Validate and store the disposition. Commit deduplication, identities, field timestamps, assignments, and outbox actions in one transaction. Check source offsets against accepted, rejected, deferred, and duplicate totals after an outage.
2. **Apply CRM changes.** Consume the durable outbox ordered per canonical ID. Use versioned upserts and deletes so late retries cannot restore older data. Retry transient errors with backoff and jitter. Record CRM receipts, and reconcile ambiguous or partial responses by stable ID before retrying. Send permanent failures to an operator with a reason. Propagate deletion to retained exports and downstream stores, and keep the minimum replay marker. Sync closed CRM leads to release active slots.
3. **Measure the decision and the model.** At intake, log score version, input availability, score bands, unknown-feature rates, and routing disposition. Join the contact timestamp later to measure time to first contact. The fixture filled all 135 active slots and left 111 leads scoring at least 0.25 in the queue. Track staffing and SLA alongside ranking. When outcomes mature, join them to the original scored population and check average precision, shortlist precision, Brier loss, reliability by segment, and predicted versus booked euros.
4. **Retrain and roll back.** Investigate sustained feature drift, mature-label calibration decay, coverage shifts, or a material intake change. Train on time-ordered data with label availability cutoffs. Give the deployed version and candidate the same future cohorts, roster, and limits in replay or shadow. Register ranking, calibration, and operational gates before comparing them. If the candidate earns a trial, start with a limited rollout and retain the prior artifact for immediate rollback. Track observed results at a fixed maturity before expanding.

The demo uses SQLite for the ledger, lead state, tombstones, assignment reservations, and pending writes. It verifies stage-to-publication retries and preserves ordered CRM update and delete actions. Operators record confirmation after actual CRM work. The local dashboard reads that state after each upload and every 15 minutes while open. The recurring ingestion worker, CRM connection, and model monitoring jobs are production work.

### Future call measurement

Record a stable call ID, canonical lead ID, rep, time, and whether the call was attempted, completed, and qualified. Join each call to the lead's original campaign and intake cohort. Report campaign spend per completed call and spend per lead with a completed, qualified call. The second metric counts each lead once so repeat calls do not inflate campaign performance. Refresh those reports daily and compare cohorts that have had equal time to reach a call.

After each completed call, score the lead's demonstrated interest using a shared rubric and record a short reason from what they actually said. Start with **low** for information gathering, **medium** for a specific need that is still being explored, and **high** for a clear need with an agreed next step. Use **insufficient evidence** when the call leaves interest unclear, and track unanswered calls separately. Start with structured rep notes. Check that reps apply the rubric consistently and that higher scores predict wins on later cohorts with time to close. Use the post-call score for follow-up and campaign quality.

### Future routing experiment

Log who was eligible at dispatch, the assigned rep and first caller, assignment and contact times, transfers, and close outcomes with timestamps. Use those records to compare close rates for similar enquiries and shifts after the same time to close. Small rep cohorts need more evidence before their rates inform assignments.

Replay a capacity-constrained allocator alongside the current rules on the same time-ordered batches, with identical language, service, partner, shift, and capacity limits. Start with estimated lead value as a shared signal, then test a rep-specific estimate when the records support it. Compare assignment coverage, scheduled wait, workload balance, and the estimated value of leads assigned. If the replay shows a material benefit, use a controlled pilot to measure signed deals and booked value.
