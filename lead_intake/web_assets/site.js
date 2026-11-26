"use strict";

const el = (id) => document.getElementById(id);
const integer = new Intl.NumberFormat("en-GB");
const percent = new Intl.NumberFormat("en-GB", {style: "percent", maximumFractionDigits: 1});
const euro = new Intl.NumberFormat("en-IE", {style: "currency", currency: "EUR"});
const wholeEuro = new Intl.NumberFormat("en-IE", {style: "currency", currency: "EUR", maximumFractionDigits: 0});
const lisbonTime = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Lisbon", hour: "2-digit", minute: "2-digit"
});
const lisbonStamp = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Lisbon", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
});
const dayFormat = new Intl.DateTimeFormat("en-GB", {timeZone: "UTC", month: "short", day: "numeric"});
const monthFormat = new Intl.DateTimeFormat("en-GB", {timeZone: "UTC", month: "short", year: "numeric"});
const groups = {
  source: {
    web_form: ["Web form", "#2a644c"], chat: ["Chat", "#8da995"], partner_api: ["Partner API", "#cc9e63"]
  },
  channel: {
    google: ["Google", "#2a644c"], meta: ["Meta", "#6b94a3"], organic: ["Organic", "#8da995"],
    partner: ["Partner", "#cc9e63"], bing: ["Bing", "#9986a6"], unknown: ["Unknown", "#b8c5bb"],
    other: ["Other", "#71877c"]
  }
};
let snapshot = null;
let file = null;
let currentRun = null;
let previewMode = "crm";
let dashboardRequest = 0;
let runRequest = 0;
let busy = false;

function node(tag, className = "", text = "") {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== "") item.textContent = text;
  return item;
}

function link(label, href) {
  const item = node("a", "button secondary", label);
  item.href = href;
  return item;
}

function button(label, action) {
  const item = node("button", "button secondary", label);
  item.type = "button";
  item.addEventListener("click", action);
  return item;
}

function labelize(value) {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function dateObject(value) {
  return new Date(value + "T12:00:00Z");
}

function dateLabel(value) {
  return dayFormat.format(dateObject(value));
}

function shown(value, formatter) {
  return value === null || value === undefined || value === "" ? "—" : formatter.format(value);
}

function notice(target, message, warning = false) {
  const destination = el(target);
  destination.textContent = message;
  destination.hidden = !message;
  destination.classList.toggle("warning", warning);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The request failed. Try again.");
  return data;
}

function showView(view) {
  const entering = el(view).hidden;
  for (const item of document.querySelectorAll("[data-view]")) {
    const active = item.dataset.view === view;
    el(item.dataset.view).hidden = !active;
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  if (entering) window.scrollTo(0, 0);
}

window.addEventListener("hashchange", () => {
  if (location.hash === "#intake") showView("intake");
  if (location.hash === "#dashboard" || !location.hash) {
    showView("dashboard");
    loadDashboard();
  }
});

function key(color) {
  const square = node("i", "key");
  square.style.backgroundColor = color;
  return square;
}

function track(fraction, color) {
  const bar = node("span", "track");
  const fill = node("span");
  fill.style.width = Math.min(100, fraction * 100) + "%";
  if (color) fill.style.backgroundColor = color;
  bar.append(fill);
  return bar;
}

function renderChart() {
  if (!snapshot) return;
  const dimension = el("group").value;
  const series = groups[dimension];
  const dates = new Map();
  const end = dateObject(snapshot.end);
  for (const day = dateObject(snapshot.start); day <= end; day.setUTCDate(day.getUTCDate() + 1)) {
    dates.set(day.toISOString().slice(0, 10), Object.fromEntries(Object.keys(series).map((name) => [name, 0])));
  }
  for (const row of snapshot.arrivals) {
    const day = dates.get(row.day);
    if (day && Object.hasOwn(day, row[dimension])) day[row[dimension]] += row.total;
  }
  const totals = {};
  for (const name of Object.keys(series)) {
    totals[name] = [...dates.values()].reduce((sum, day) => sum + day[name], 0);
  }
  const categories = Object.keys(series).filter((name) => totals[name] || dimension === "source");
  const chart = el("volume-chart");
  const legend = el("chart-legend");
  const mix = el("mix");
  el("mix-title").textContent = dimension === "source" ? "Intake sources" : "Marketing channels";
  chart.replaceChildren();
  legend.replaceChildren();
  mix.replaceChildren();
  if (!snapshot.new_leads) {
    const empty = node("div", "empty", "Run a batch to see daily lead volume.");
    empty.append(link("Open CSV intake", "#intake"));
    chart.append(empty);
    mix.append(node("p", "empty", "Source counts appear after the first batch."));
    return;
  }
  const max = Math.max(...[...dates.values()].map((day) => Object.values(day).reduce((a, b) => a + b, 0)));
  const step = Math.max(5, Math.ceil(max / 4 / 5) * 5);
  const axisMax = step * 4;
  const plot = node("div", "plot");
  plot.style.setProperty("--days", dates.size);
  const yAxis = node("div", "axis");
  const lines = node("div", "gridlines");
  lines.setAttribute("aria-hidden", "true");
  yAxis.setAttribute("aria-hidden", "true");
  for (let mark = axisMax; mark >= 0; mark -= step) {
    yAxis.append(node("span", "", mark));
    lines.append(node("span"));
  }
  const bars = node("div", "bars");
  const labels = node("div", "bar-labels");
  labels.setAttribute("aria-hidden", "true");
  const reading = node("div", "bar-reading");
  const interval = dates.size > 18 ? 5 : dates.size > 9 ? 2 : 1;
  let index = 0;
  for (const [date, values] of dates) {
    const total = Object.values(values).reduce((a, b) => a + b, 0);
    const detail = categories.filter((name) => values[name])
      .map((name) => series[name][0] + " " + values[name]).join(", ");
    const annotation = dateLabel(date) + " · " + integer.format(total) + " leads" + (detail ? " · " + detail : "");
    const bar = node("div", "bar");
    bar.setAttribute("role", "img");
    bar.setAttribute("aria-label", annotation);
    bar.tabIndex = 0;
    bar.title = annotation;
    bar.style.height = (100 * total / axisMax) + "%";
    for (const name of categories) {
      if (!values[name]) continue;
      const part = node("span");
      part.style.flexBasis = (100 * values[name] / total) + "%";
      part.style.backgroundColor = series[name][1];
      bar.append(part);
    }
    bar.addEventListener("focus", () => { reading.textContent = annotation; });
    bar.addEventListener("mouseenter", () => { reading.textContent = annotation; });
    bars.append(bar);
    labels.append(node("span", "", index % interval === 0 ? (dates.size <= 7 ? dateLabel(date) : date.slice(-2)) : ""));
    index += 1;
  }
  plot.append(yAxis, lines, bars, labels);
  chart.append(plot, reading);
  for (const name of categories) {
    const [label, color] = series[name];
    const item = node("span");
    item.append(key(color), node("span", "", label));
    legend.append(item);
  }
  for (const name of [...categories].sort((a, b) => totals[b] - totals[a])) {
    const [label, color] = series[name];
    const summary = node("div");
    const line = node("div", "mix-line");
    const title = node("span");
    const count = node("b", "", integer.format(totals[name]));
    count.append(node("small", "", percent.format(totals[name] / snapshot.new_leads)));
    title.append(key(color), document.createTextNode(label));
    line.append(title, count);
    summary.append(line, track(totals[name] / snapshot.new_leads, color));
    mix.append(summary);
  }
}

function rowOf(values, classIndex = -1) {
  const row = node("tr");
  values.forEach((value, index) => row.append(node("td", index === classIndex ? "reason" : "", value)));
  return row;
}

function renderEconomics() {
  if (!snapshot) return;
  const history = snapshot.history;
  el("history-period").textContent = "Historical cohorts · "
    + monthFormat.format(dateObject(history.first_month + "-01")) + " – "
    + monthFormat.format(dateObject(history.last_month + "-01"));
  const values = history[el("economics-group").value];
  const body = el("economics");
  body.replaceChildren();
  for (const row of values) {
    const name = row.name.split(" / ");
    const display = (groups.channel[name[0]]?.[0] || labelize(name[0])) +
      (name.length > 1 ? " / " + labelize(name[1]) : "");
    body.append(rowOf([
      display, shown(row.spend_eur, euro), integer.format(row.leads), integer.format(row.wins),
      shown(row.win_rate, percent), shown(row.cost_per_lead_eur, euro), shown(row.cost_per_win_eur, euro),
      shown(row.net_booked_eur, euro), row.net_booked_per_eur_spent === null
        ? "—" : row.net_booked_per_eur_spent.toFixed(2) + "×"
    ]));
  }
}

function renderRuns() {
  const holder = el("runs");
  holder.replaceChildren();
  if (!snapshot.runs.length) {
    holder.append(node("p", "empty", "Run a CSV to see its results here."));
    return;
  }
  for (const run of snapshot.runs) {
    const line = node("div", "run-line");
    const identity = node("div");
    const status = run.published_at
      ? lisbonStamp.format(new Date(run.published_at)) + " · " + run.crm_rows + " CRM rows · " + run.rejection_rows + " rejected"
      : "Waiting for publication";
    identity.append(node("strong", "", run.filename), node("span", "", status));
    line.append(identity, button("Inspect batch", () => openRun(run.batch_id)));
    holder.append(line);
  }
}

function renderDashboard() {
  el("stat-new").textContent = integer.format(snapshot.new_leads);
  el("stat-active").textContent = integer.format(snapshot.active);
  el("stat-queue").textContent = integer.format(snapshot.queued);
  el("stat-score").textContent = shown(snapshot.mean_score, percent);
  el("score-note").textContent = snapshot.expected_value == null
    ? "Across current leads" : wholeEuro.format(snapshot.expected_value) + " estimated booked value";
  el("active-note").textContent = integer.format(snapshot.capacity) + " slots in the roster";
  el("queue-note").textContent = snapshot.queued
    ? snapshot.queue_high_score + " with a win score of at least 25%" : "Awaiting a compatible slot";
  el("range-text").textContent = snapshot.has_arrivals
    ? dateLabel(snapshot.start) + " – " + dateLabel(snapshot.end) + " · Lisbon dates"
    : "Waiting for the first CSV";
  el("refresh-time").textContent = "Updated " + lisbonTime.format(new Date(snapshot.refreshed_at)) + " Lisbon";
  el("capacity-badge").textContent = snapshot.active + " / " + snapshot.capacity + " used";
  el("handoff-updates").textContent = snapshot.pending_actions.update ?? 0;
  el("handoff-deletes").textContent = snapshot.pending_actions.delete ?? 0;
  const capacity = el("capacity");
  capacity.replaceChildren();
  for (const rep of snapshot.reps) {
    const item = node("div");
    const line = node("div", "rep-line");
    const title = node("span", "", rep.id.replace("rep_", "Rep "));
    if (rep.partner) title.append(node("small", "", "Partner"));
    line.append(title, node("b", "", rep.open + " / " + rep.max));
    item.append(line, track(rep.max ? rep.open / rep.max : 0));
    capacity.append(item);
  }
  renderChart();
  renderEconomics();
  renderRuns();
}

async function loadDashboard() {
  const request = ++dashboardRequest;
  el("refresh").disabled = true;
  el("refresh").textContent = "Refreshing…";
  try {
    const data = await api("/api/dashboard?days=" + el("range").value);
    if (request !== dashboardRequest) return;
    snapshot = data;
    notice("dashboard-error", "");
    renderDashboard();
  } catch (error) {
    if (request === dashboardRequest) notice("dashboard-error", error.message, true);
  } finally {
    if (request === dashboardRequest) {
      el("refresh").disabled = false;
      el("refresh").textContent = "Refresh";
    }
  }
}

function setFile(selected) {
  if (selected && (!selected.name.toLowerCase().endsWith(".csv") || selected.size > 10 * 1024 * 1024 || !selected.size)) {
    file = null;
    el("csv-file").value = "";
    notice("intake-message", "Choose a nonempty CSV file up to 10 MB.", true);
  } else {
    file = selected || null;
    notice("intake-message", "");
  }
  if (!file) el("csv-file").value = "";
  el("file-name").textContent = file ? file.name : "Drop your CSV here";
  el("file-detail").textContent = file ? integer.format(Math.max(1, Math.round(file.size / 1024))) + " KB ready to process" : "CSV files up to 10 MB";
  const picker = el("file-picker");
  picker.textContent = file ? "Change file" : "Choose file";
  picker.classList.toggle("primary", !file);
  picker.classList.toggle("secondary", Boolean(file));
  el("clear-file").hidden = !file;
  el("run-intake").hidden = !file;
  el("run-intake").disabled = !file || busy;
}

function resultPreview() {
  const container = el("result-preview");
  container.replaceChildren();
  for (const tab of document.querySelectorAll("[data-preview]")) {
    const selected = tab.dataset.preview === previewMode;
    tab.classList.toggle("selected", selected);
    tab.setAttribute("aria-pressed", selected);
  }
  if (previewMode === "report") {
    const list = node("div", "event-list");
    const events = Object.entries(currentRun.events);
    for (const [label, count] of events) {
      const pair = node("div");
      pair.append(node("span", "", labelize(label)), node("strong", "", integer.format(count)));
      list.append(pair);
    }
    container.append(list);
    const scoring = currentRun.scoring;
    if (scoring?.n) {
      container.append(node("p", "footnote", "Mean win score " + percent.format(scoring.mean)
        + " · " + scoring.newly_scored + " scored with " + currentRun.model_version));
    }
    return;
  }
  const rejected = previewMode === "rejected";
  const rows = rejected ? currentRun.rejected_preview : currentRun.crm_preview;
  if (!rows.length) {
    container.append(node("p", "empty", currentRun.files_available
      ? (rejected ? "There are no rejected rows in this batch." : "There are no new CRM rows in this batch.")
      : "The export files are unavailable."));
    return;
  }
  const frame = node("div", "table-scroll");
  const table = node("table");
  const header = node("tr");
  const labels = rejected
    ? ["Event ID", "External ID", "Reason"]
    : ["External ID", "Service", "Source", "Owner", "Win score", "Expected value", "Assignment reason"];
  for (const label of labels) {
    const cell = node("th", "", label);
    cell.scope = "col";
    header.append(cell);
  }
  const head = node("thead");
  head.append(header);
  const tbody = node("tbody");
  for (const item of rows) {
    if (rejected) {
      tbody.append(rowOf([item.event_id, item.external_id, item.reason], 2));
    } else {
      tbody.append(rowOf([
        item.external_id, labelize(item.service), groups.source[item.source]?.[0] || item.source,
        item.owner_rep_id.replace("rep_", "Rep "), percent.format(Number(item.lead_score)),
        euro.format(Number(item.expected_value_eur)), item.assignment_reason === "queue" ? "Queue" : item.assignment_reason
      ], 6));
    }
  }
  table.append(head, tbody);
  frame.append(table);
  container.append(frame, node("p", "footnote", "Showing "
    + rows.length + " of " + (rejected ? currentRun.rejection_rows : currentRun.crm_rows) + " rows."));
}

function renderRun(run) {
  currentRun = run;
  previewMode = run.rejection_rows > 0 ? "rejected" : "crm";
  el("output-panel").hidden = false;
  el("result-file").textContent = run.filename + (run.published_at ? " · " + lisbonStamp.format(new Date(run.published_at)) : "");
  const status = el("result-status");
  status.textContent = run.crm_confirmed_at ? "CRM confirmed" : run.published_at ? "Export generated" : "Publication pending";
  status.classList.toggle("waiting", !run.published_at);
  const stats = el("result-stats");
  stats.replaceChildren();
  for (const [name, value] of [
    ["Deliveries", run.deliveries], ["New CRM rows", run.crm_rows], ["Rejected", run.rejection_rows],
    ["CRM updates", run.crm_updates], ["CRM deletions", run.crm_deletes]
  ]) {
    const item = node("div", name === "Rejected" && value ? "rejected-count" : "");
    item.append(node("strong", "", integer.format(value)), node("small", "", name));
    stats.append(item);
  }
  const replay = run.deliveries > 0 && run.events.duplicate_deliveries === run.deliveries;
  const note = el("result-note");
  note.textContent = replay
    ? "Replay complete. Every delivery was already recorded."
    : run.published_at ? (run.crm_rows_queued ? integer.format(run.crm_rows_queued) + " new leads queued" : "")
      : "The batch is committed. Finish publication to download its files.";
  note.hidden = !note.textContent;
  const actions = el("download-bar");
  actions.replaceChildren();
  if (run.files_available) {
    for (const [kind, label] of [["crm", "CRM CSV"], ["rejected", "Rejected CSV"], ["report", "Run report"]]) {
      const download = link(label, "/api/runs/" + run.batch_id + "/download/" + kind);
      if (kind === "crm" && run.crm_rows) download.className = "button primary";
      actions.append(download);
    }
  } else if (!run.published_at) {
    actions.append(button("Recover batch", async () => runAction("/api/recover")));
  }
  if (run.crm_updates || run.crm_deletes) actions.append(link("Pending CRM actions", "/api/actions"));
  resultPreview();
}

async function openRun(batchId) {
  const request = ++runRequest;
  if (location.hash !== "#intake") location.hash = "#intake";
  showView("intake");
  try {
    const run = await api("/api/runs/" + batchId);
    if (request !== runRequest) return;
    notice("intake-message", "");
    renderRun(run);
    el("output-panel").scrollIntoView({block: "start"});
    el("result-title").focus({preventScroll: true});
  } catch (error) {
    if (request === runRequest) notice("intake-message", error.message, true);
  }
}

async function runAction(path, body) {
  if (busy) return;
  busy = true;
  el("run-intake").disabled = true;
  for (const item of document.querySelectorAll("[data-sample]")) item.disabled = true;
  notice("intake-message", path === "/api/recover" ? "Finishing publication…" : "Validating, scoring, and routing the batch…");
  try {
    const result = await api(path, {
      method: "POST", ...(body ? {body, headers: {"Content-Type": "text/csv"}} : {})
    });
    if (result.batch_id) await openRun(result.batch_id);
    else notice("intake-message", "There is no batch awaiting publication.");
    await loadDashboard();
  } catch (error) {
    notice("intake-message", error.message, true);
    await loadDashboard();
  } finally {
    busy = false;
    el("run-intake").disabled = !file;
    for (const item of document.querySelectorAll("[data-sample]")) item.disabled = false;
  }
}

el("range").addEventListener("change", loadDashboard);
el("refresh").addEventListener("click", loadDashboard);
el("group").addEventListener("change", renderChart);
el("economics-group").addEventListener("change", renderEconomics);
el("csv-file").addEventListener("change", (event) => setFile(event.target.files[0]));
el("clear-file").addEventListener("click", () => setFile(null));
el("run-intake").addEventListener("click", () => {
  if (file) runAction("/api/intake?name=" + encodeURIComponent(file.name), file);
});
for (const item of document.querySelectorAll("[data-sample]")) {
  item.addEventListener("click", () => runAction("/api/sample?batch=" + item.dataset.sample));
}
for (const tab of document.querySelectorAll("[data-preview]")) {
  tab.addEventListener("click", () => {
    previewMode = tab.dataset.preview;
    resultPreview();
  });
}
const drop = el("drop-zone");
drop.addEventListener("dragover", (event) => {
  event.preventDefault();
  drop.classList.add("dragging");
});
drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
drop.addEventListener("drop", (event) => {
  event.preventDefault();
  drop.classList.remove("dragging");
  if (!busy) setFile(event.dataTransfer.files[0]);
});

showView(location.hash === "#intake" ? "intake" : "dashboard");
loadDashboard();
setInterval(loadDashboard, 15 * 60 * 1000);
