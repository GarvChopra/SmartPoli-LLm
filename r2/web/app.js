"use strict";
// All server-provided text is inserted with textContent, never innerHTML:
// tool arguments can contain text copied from web pages.
const $ = (id) => document.getElementById(id);
const KEY = "r2.token";

function getToken() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
function setToken(t) { try { t ? localStorage.setItem(KEY, t) : localStorage.removeItem(KEY); } catch (e) {} }

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401 && path !== "/api/pair") { setToken(null); show(false); }
  return res;
}

function show(paired) {
  $("pair").hidden = paired;
  $("app").hidden = !paired;
}

async function pair() {
  $("pairErr").textContent = "";
  const res = await api("/api/pair", { method: "POST", body: JSON.stringify({ code: $("code").value }) });
  if (!res.ok) { $("pairErr").textContent = "That code is wrong or has expired."; return; }
  setToken((await res.json()).token);
  history.replaceState(null, "", "/");
  show(true);
  refresh();
  loadPrivacy();
  loadProjects();
}

let approvalSeenAt = null, approvalId = null, lastShot = 0, busy = false;

function renderStatus(st) {
  const chip = $("chip");
  chip.textContent = st.paused ? "paused" : st.running ? "working" : "idle";
  chip.className = "chip" + (st.paused ? " paused" : st.running ? " busy" : "");
  $("demoNote").hidden = !st.demo;
  $("stop").hidden = !st.running;
  $("send").disabled = st.running || st.paused;

  const p = st.pending;
  $("approval").hidden = !p;
  if (p) {
    if (approvalId !== p.id) { approvalId = p.id; approvalSeenAt = Date.now(); }
    $("apTool").textContent = p.tool;
    $("apArgs").textContent = JSON.stringify(p.args, null, 2);
    const left = Math.max(0, Math.round(p.timeout - (Date.now() - approvalSeenAt) / 1000));
    $("apTimer").textContent = "Expires in " + left + "s (then it is denied)";
  } else { approvalId = null; }

  const live = st.running && st.partial;
  $("resultCard").hidden = !(st.result || live);
  if (live) {
    $("resMsg").textContent = st.message || "";
    $("resStatus").textContent = "answering...";
    $("resAnswer").textContent = st.partial;
  } else if (st.result) {
    $("resMsg").textContent = st.message || "";
    $("resStatus").textContent = st.result.status + " · " + st.result.steps + " step(s)";
    $("resAnswer").textContent = st.result.answer;
  }
  busy = !!st.running;
  if (st.running && Date.now() - lastShot > 4000) loadShot();
}

function li(text, buttonLabel, onClick) {
  const item = document.createElement("li");
  const span = document.createElement("span");
  span.textContent = text;
  item.appendChild(span);
  if (buttonLabel) {
    const b = document.createElement("button");
    b.textContent = buttonLabel;
    b.addEventListener("click", onClick);
    item.appendChild(b);
  }
  return item;
}

async function loadLists() {
  const f = await api("/api/facts");
  if (f.ok) {
    const box = $("facts"); box.replaceChildren();
    const facts = await f.json();
    if (!facts.length) box.appendChild(li("Nothing remembered yet"));
    for (const fact of facts) {
      box.appendChild(li(fact.text, "Forget", async () => { await api("/api/facts/" + fact.id, { method: "DELETE" }); loadLists(); }));
    }
  }
  const a = await api("/api/actions");
  if (a.ok) {
    const box = $("actions"); box.replaceChildren();
    for (const act of (await a.json()).slice(0, 10)) box.appendChild(li(act.tool + " · " + act.approval));
  }
}

function ledgerLine(r) {
  const when = new Date(r.ts * 1000).toLocaleTimeString();
  if (r.route) return when + " · " + r.route + " (" + r.model + ") · " + (r.left_pc ? "left this PC" : "stayed on this PC") + " · " + r.reason;
  const masked = Object.entries(r.masked || {}).map(([k, n]) => n + " " + k).join(", ") || "nothing masked";
  return when + " · sent to " + r.host + " · " + r.messages + " msgs, " + r.bytes + " B · " + masked;
}

async function loadPrivacy() {
  const res = await api("/api/ledger");
  if (res.ok) {
    const d = await res.json();
    const masked = Object.entries(d.summary.masked).map(([k, n]) => n + " " + k).join(", ") || "nothing";
    $("ledgerSummary").textContent = d.summary.requests
      ? d.summary.requests + " request(s) sent, " + d.summary.bytes + " bytes. Masked before sending: " + masked + ". " + d.stayed_local + " of " + d.routed + " routed requests stayed on this PC."
      : "Nothing has been sent to a hosted model yet.";
    const box = $("ledgerRows"); box.replaceChildren();
    for (const r of d.recent.slice(0, 8)) box.appendChild(li(ledgerLine(r)));
  }
  const s = await api("/api/skills");
  if (s.ok) {
    const d = await s.json();
    const list = $("skillsList"); list.replaceChildren();
    for (const k of d.skills) list.appendChild(li(k.name + " · " + k.description));
    const box = $("proposedList"); box.replaceChildren();
    $("proposedTitle").hidden = !d.proposed.length;
    for (const k of d.proposed) {
      const item = li(k.name + " · " + k.description, "Approve", async () => { await api("/api/skills/" + k.name + "/approve", { method: "POST" }); loadPrivacy(); });
      const no = document.createElement("button"); no.textContent = "Reject";
      no.addEventListener("click", async () => { await api("/api/skills/" + k.name + "/proposed", { method: "DELETE" }); loadPrivacy(); });
      item.appendChild(no);
      box.appendChild(item);
      // Show the full instructions before you approve: a web page could have steered R2 into proposing them.
      const steps = document.createElement("pre");
      steps.textContent = k.body;
      box.appendChild(steps);
    }
  }
}

async function loadShot() {
  lastShot = Date.now();
  const res = await api("/api/screenshot");
  if (!res.ok) return;
  const url = URL.createObjectURL(await res.blob());
  const img = $("shotImg");
  if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
  img.dataset.url = url; img.src = url; img.hidden = false;
}

async function refresh() {
  if (!getToken()) return;
  const res = await api("/api/status");
  if (res.ok) renderStatus(await res.json());
}

async function send() {
  $("err").textContent = "";
  const message = $("msg").value.trim();
  if (!message) return;
  const res = await api("/api/tasks", { method: "POST", body: JSON.stringify({ message }) });
  if (res.status === 202) { $("msg").value = ""; refresh(); loadLists(); loadPrivacy(); return; }
  const body = await res.json().catch(() => ({}));
  $("err").textContent = typeof body.detail === "string" ? body.detail : "Could not start the task.";
}

async function decide(approve) {
  if (!approvalId) return;
  await api("/api/approve", { method: "POST", body: JSON.stringify({ id: approvalId, approve }) });
  refresh();
}

$("pairBtn").addEventListener("click", pair);
$("code").addEventListener("keydown", (e) => { if (e.key === "Enter") pair(); });
$("send").addEventListener("click", send);
$("stop").addEventListener("click", async () => { await api("/api/stop", { method: "POST" }); refresh(); });
$("apYes").addEventListener("click", () => decide(true));
$("apNo").addEventListener("click", () => decide(false));
$("shot").addEventListener("click", loadShot);
$("signout").addEventListener("click", () => { setToken(null); show(false); });
$("revoke").addEventListener("click", async () => { await api("/api/revoke", { method: "POST" }); setToken(null); show(false); });

// ---- meetings: projects, minutes, tasks ----
let projects = [], currentProject = null;

function option(value, label) { const o = document.createElement("option"); o.value = value; o.textContent = label; return o; }

async function loadProjects(keep) {
  const res = await api("/api/projects");
  if (!res.ok) return;
  projects = await res.json();
  const sel = $("projSelect"); sel.replaceChildren();
  if (!projects.length) sel.appendChild(option("", "No project yet: create one"));
  for (const p of projects) sel.appendChild(option(p.id, p.name + " (" + p.open_tasks + " open)"));
  const wanted = keep || currentProject || (projects[0] && projects[0].id);
  if (wanted) sel.value = String(wanted);
  currentProject = sel.value ? Number(sel.value) : null;
  loadTasks();
}

function dueText(i) { return i.due ? i.due + (i.due_text ? " (" + i.due_text + ")" : "") : (i.due_text || "no deadline"); }

async function setItem(id, path, body) {
  await api("/api/items/" + id + "/" + path, { method: "POST", body: JSON.stringify(body) });
  loadProjects(currentProject);
}

async function loadTasks() {
  const box = $("taskList"); box.replaceChildren();
  if (!currentProject) return;
  const res = await api("/api/projects/" + currentProject);
  if (!res.ok) return;
  const data = await res.json();
  const owners = [...data.members, "Unassigned"];
  for (const i of data.items) if (!owners.includes(i.owner)) owners.push(i.owner);
  for (const owner of owners) {
    const mine = data.items.filter((i) => i.owner === owner);
    if (!mine.length) continue;
    const head = document.createElement("li"); head.className = "group";
    head.textContent = owner + " · " + mine.filter((i) => i.status === "open").length + " open";
    box.appendChild(head);
    for (const i of mine) {
      const item = document.createElement("li");
      const check = document.createElement("input"); check.type = "checkbox"; check.checked = i.status === "done";
      check.setAttribute("aria-label", "Done: " + i.task);
      check.addEventListener("change", () => setItem(i.id, "status", { status: check.checked ? "done" : "open" }));
      const text = document.createElement("span");
      text.textContent = i.task + " · due " + dueText(i) + (i.verified ? "" : " · UNVERIFIED, check the quote") + (i.quote ? "\n\"" + i.quote + "\"" : "");
      text.className = "task" + (i.status === "done" ? " done" : "");
      const who = document.createElement("select"); who.setAttribute("aria-label", "Owner");
      for (const name of [...data.members, "Unassigned"]) who.appendChild(option(name, name));
      who.value = owners.includes(i.owner) && [...data.members, "Unassigned"].includes(i.owner) ? i.owner : "Unassigned";
      who.addEventListener("change", () => setItem(i.id, "owner", { owner: who.value }));
      item.append(check, text, who);
      box.appendChild(item);
    }
  }
  if (!data.items.length) box.appendChild(li("No tasks yet"));
}

function fillList(id, rows, title) {
  const box = $(id); box.replaceChildren();
  if (rows.length) box.appendChild(li(title));
  for (const r of rows) box.appendChild(li(r));
}

async function summarize() {
  $("trErr").textContent = "";
  if (!currentProject) { $("trErr").textContent = "Create a project first."; return; }
  const body = { transcript: $("trText").value, title: $("trTitle").value, me: $("trMe").value, date: $("trDate").value };
  if (!body.transcript.trim()) { $("trErr").textContent = "Paste the transcript first."; return; }
  $("trGo").disabled = true; $("trGo").textContent = "Summarizing...";
  const res = await api("/api/projects/" + currentProject + "/meetings", { method: "POST", body: JSON.stringify(body) });
  $("trGo").disabled = false; $("trGo").textContent = "Summarize and save";
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("trErr").textContent = typeof data.detail === "string" ? data.detail : "Could not summarize that."; return; }
  $("trResult").hidden = false;
  $("trSummary").textContent = data.summary || "(no summary)";
  $("trMethod").textContent = "Made by: " + data.method + " · " + data.items.length + " action item(s) saved" + (data.warnings.length ? " · " + data.warnings.join(" ") : "");
  fillList("trDecisions", data.decisions, "Decisions");
  fillList("trQuestions", data.questions, "Open questions");
  $("trText").value = "";
  loadProjects(currentProject);
}

async function createProject() {
  $("projErr").textContent = "";
  const members = $("projMembers").value.split(",").map((m) => m.trim()).filter(Boolean);
  const res = await api("/api/projects", { method: "POST", body: JSON.stringify({ name: $("projName").value, members }) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { $("projErr").textContent = typeof data.detail === "string" ? data.detail : "Give a name and the team members."; return; }
  $("projForm").hidden = true; $("projName").value = ""; $("projMembers").value = "";
  currentProject = data.id;
  loadProjects(data.id);
}

$("projSelect").addEventListener("change", () => { currentProject = $("projSelect").value ? Number($("projSelect").value) : null; loadTasks(); });
$("projNew").addEventListener("click", () => { $("projForm").hidden = !$("projForm").hidden; });
$("projCreate").addEventListener("click", createProject);
$("trGo").addEventListener("click", summarize);

const fromLink = new URLSearchParams(location.search).get("code");
if (fromLink) $("code").value = fromLink;
show(!!getToken());
if (getToken()) { refresh(); loadLists(); loadPrivacy(); loadProjects(); }
(async function loop() { await refresh(); setTimeout(loop, busy ? 400 : 1500); })();
setInterval(() => { if (getToken()) { loadLists(); loadPrivacy(); } }, 10000);
