const $ = (sel) => document.querySelector(sel);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const MODE_LABELS = { ai: "AI", keyword: "Keyword", change: "Change" };
const ALERT_STATUS_STYLE = { sent: "pill ok", failed: "pill bad", simulated: "pill test", "not sent": "pill off" };
const MODE_TEXT = {
  live: "Live: alerts are emailed",
  test: "Test mode: alerts are simulated, nothing is sent",
  unset: "Email not configured",
};
const ICONS = {
  check: '<path d="M20 6 9 17l-5-5"/>',
  scan: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  pause: '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
  play: '<polygon points="6 3 20 12 6 21 6 3"/>',
  trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/>',
};
let watches = [];
let editingId = null;
let emailEnabled = true;

function icon(name) {
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
}

function iconButton(action, id, name, label, extraClass = "") {
  return `<button class="icon-btn ${extraClass}" data-action="${action}" data-id="${id}" title="${label}" aria-label="${label}">${icon(name)}</button>`;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.style.display = "block";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.style.display = "none"), 4000);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Error ${response.status}`);
  return data;
}

function formatTime(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" });
}

function describeState(watch) {
  const s = watch.state || {};
  if (!watch.enabled) return `<span class="pill off">Disabled</span>`;
  if (s.last_status === "error") return `<span class="status-error">Error</span><div class="reason">${esc(s.last_error)}</div>`;
  if (!s.last_checked_ts) return `<span class="pill off">Pending</span>`;
  if (watch.mode === "ai" && s.ai_probability !== undefined) {
    return `<span class="status-ok">OK</span> · <span class="prob">${s.ai_probability} %</span>` +
      (s.ai_reason ? `<div class="reason">${esc(s.ai_reason)}</div>` : "");
  }
  if (watch.mode === "keyword") {
    return `<span class="status-ok">OK</span> · keyword ${s.keyword_present ? "present" : "absent"}`;
  }
  return `<span class="status-ok">OK</span>`;
}

function describeLastAlert(alert) {
  if (!alert) return `<span class="url">No alert yet</span>`;
  return `<span class="${ALERT_STATUS_STYLE[alert.status] || "pill off"}">${esc(alert.status)}</span> ` +
    `<span class="url nowrap">${formatTime(alert.ts)}</span><div class="reason">${esc(alert.title)}</div>`;
}

function describeCapture(watch) {
  const state = watch.state || {};
  if (!state.last_checked_ts) return `<span class="url">Not checked yet</span>`;
  if (!state.has_image) return `<span class="url">Capture unavailable</span>`;
  const url = `/api/watches/${encodeURIComponent(watch.id)}/image?v=${encodeURIComponent(state.last_checked_ts)}`;
  const label = `Open last check capture for ${watch.name}, ${formatTime(state.last_checked_ts)} (opens in a new tab)`;
  return `<a class="thumbnail-link" href="${url}" target="_blank" rel="noopener noreferrer" title="${esc(label)}" aria-label="${esc(label)}">` +
    `<img class="watch-thumbnail" src="${url}" alt="${esc(`Last check capture for ${watch.name}`)}" width="108" height="72" loading="lazy" decoding="async"></a>`;
}

function renderWatches() {
  $("#watches-empty").classList.toggle("hidden", watches.length > 0);
  $("#watches").innerHTML = watches.map((w) => {
    const modeText = w.mode === "ai" ? `${MODE_LABELS.ai} · threshold ${w.ai_threshold} %`
      : w.mode === "keyword" ? `${MODE_LABELS.keyword} · ${esc(w.keyword)}`
      : MODE_LABELS.change;
    const condition = w.mode === "ai" ? `<div class="reason">${esc(w.ai_condition)}</div>` : "";
    const mails = w.extra_emails?.length ? `<div class="url">Also sent to: ${esc(w.extra_emails.join(", "))}</div>` : "";
    return `
      <tr>
        <td><strong>${esc(w.name)}</strong><div class="url">${esc(w.url)}</div>${condition}${mails}</td>
        <td class="nowrap">${modeText}</td>
        <td>${w.interval_minutes} min</td>
        <td class="last-check">${formatTime(w.state?.last_checked_ts)}</td>
        <td>${describeCapture(w)}</td>
        <td>${describeState(w)}</td>
        <td>${describeLastAlert(w.last_alert)}</td>
        <td><div class="actions">
          ${iconButton("check", w.id, "check", "Check now")}
          ${iconButton("scan", w.id, "scan", "Scan (analyze only, no email sent)")}
          ${iconButton("edit", w.id, "edit", "Edit")}
          ${iconButton("toggle", w.id, w.enabled ? "pause" : "play", w.enabled ? "Pause monitoring" : "Resume monitoring")}
          ${iconButton("delete", w.id, "trash", "Delete", "danger")}
        </div></td>
      </tr>`;
  }).join("");
}

async function loadWatches() {
  watches = await api("/api/watches");
  renderWatches();
}

function updateModeFields() {
  const mode = $("#mode").value;
  document.querySelectorAll("[data-mode]").forEach((el) => {
    el.classList.toggle("hidden", el.dataset.mode !== mode);
  });
}

function openForm(watch = null) {
  editingId = watch ? watch.id : null;
  const form = $("#watch-form");
  form.reset();
  $("#form-title").textContent = watch ? "Edit page" : "New page";
  if (watch) {
    for (const [key, value] of Object.entries(watch)) {
      const input = form.elements[key];
      if (!input) continue;
      if (input.type === "checkbox") input.checked = !!value;
      else input.value = value ?? "";
    }
    form.elements.extra_emails.value = (watch.extra_emails || []).join(", ");
  } else {
    form.elements.url.value = "https://summit.microsoft.com/en-us/";
    form.elements.name.value = "MVP Summit";
    form.elements.ai_condition.value = "Registrations for MVP Summit are open";
  }
  updateModeFields();
  $("#form-card").classList.remove("hidden");
  form.elements.url.focus();
}

function closeForm() {
  editingId = null;
  $("#form-card").classList.add("hidden");
}

function readWatchForm() {
  const form = $("#watch-form");
  return {
    name: form.elements.name.value,
    url: form.elements.url.value,
    mode: form.elements.mode.value,
    interval_minutes: Number(form.elements.interval_minutes.value),
    ai_condition: form.elements.ai_condition.value,
    ai_threshold: Number(form.elements.ai_threshold.value),
    keyword: form.elements.keyword.value,
    extra_emails: form.elements.extra_emails.value,
    enabled: form.elements.enabled.checked,
  };
}

async function runScan(id) {
  const watch = watches.find((w) => w.id === id);
  $("#scan-title").textContent = `Scan: ${watch ? watch.name : ""}`;
  $("#scan-body").textContent = "Analyzing (capture, AI)…";
  $("#scan-card").classList.remove("hidden");
  try {
    renderScan(await api(`/api/watches/${id}/scan`, { method: "POST" }));
  } catch (err) {
    $("#scan-body").textContent = "";
    toast(err.message, true);
  }
}

function renderScan(r) {
  let detail;
  if (r.mode === "ai") {
    detail = `Probability: <strong>${r.result.probability} %</strong> (threshold ${r.result.threshold} %)<div class="reason">${esc(r.result.reason)}</div>`;
  } else if (r.mode === "keyword") {
    detail = `Keyword "${esc(r.result.keyword)}": ${r.result.present ? "present" : "absent"}`;
  } else {
    detail = `Change: ${r.result.changed === null ? "no reference yet" : r.result.changed ? "yes" : "no"}`;
  }
  const image = r.image
    ? `<div style="max-height:75vh;overflow:auto"><img src="data:image/jpeg;base64,${r.image}" alt="Page capture" class="page-capture"></div>`
    : `<div class="url">Capture unavailable</div>`;
  $("#scan-body").innerHTML = `
    <div class="row" style="align-items:flex-start">
      <div style="flex:1;min-width:280px">${image}</div>
      <div style="flex:1;min-width:280px">
        <div><strong>${r.mode === "ai" ? "AI result" : "Check result"}</strong></div>
        <div>${detail}</div>
        <div style="margin-top:10px"><strong>Alert:</strong> ${r.triggered ? "yes, the condition is met" : "no"}</div>
        <div class="url">Analysis only: no email is sent and the alert state is unchanged.</div>
      </div>
    </div>
    <div class="alert" style="margin-top:12px">
      <div><strong>Email content (sent only if the alert is triggered)</strong></div>
      <div><strong>${esc(r.mail.subject)}</strong></div>
      <div class="url">To: ${esc(r.mail.recipients.join(", ") || "no recipient")}</div>
      <pre>${esc(r.mail.body)}</pre>
    </div>`;
}

async function loadSettings() {
  const data = await api("/api/settings");
  const form = $("#settings-form");
  form.elements.email_sender.value = data.email_sender;
  form.elements.email_recipient.value = data.email_recipient;
  $("#ai-badge").textContent = data.ai_enabled ? "AI active" : "AI not configured";
  emailEnabled = data.email_enabled;
  $("#btn-toggle-email").textContent = emailEnabled ? "Disable sending (test mode)" : "Enable sending";
  const mode = !data.channels.email ? "unset" : emailEnabled ? "live" : "test";
  $("#channels").className = `mode-pill ${mode}`;
  $("#channels").textContent = MODE_TEXT[mode];
}

async function loadLogin() {
  const data = await api("/api/login");
  $("#login-form").elements.username.value = data.username;
  $("#login-status").textContent = data.configured
    ? "Saved login is active."
    : "No login saved: the deployment password is used, with any username.";
}

async function loadAlerts() {
  const alerts = await api("/api/alerts");
  $("#alerts-empty").classList.toggle("hidden", alerts.length > 0);
  $("#alerts").innerHTML = alerts.map((a) => `
    <div class="alert">
      <div class="row">
        <span class="${ALERT_STATUS_STYLE[a.status] || "pill off"}">${esc(a.status)}</span>
        <span class="url">${formatTime(a.ts)}</span>
      </div>
      <div><strong>${esc(a.title)}</strong></div>
      <div class="url">To: ${esc((a.recipients || []).join(", ") || "no recipient")}</div>
      <pre>${esc(a.body)}</pre>
      ${a.errors && a.errors.length ? `<div class="status-error">${esc(a.errors.join(" ; "))}</div>` : ""}
      ${a.has_image ? `<img src="/api/alerts/${a.id}/image" loading="lazy" alt="Page capture" class="page-capture">` : ""}
    </div>`).join("");
}

$("#mode").addEventListener("change", updateModeFields);
$("#btn-new").addEventListener("click", () => openForm());
$("#btn-cancel").addEventListener("click", closeForm);
$("#btn-scan-close").addEventListener("click", () => $("#scan-card").classList.add("hidden"));

$("#watch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    if (editingId) await api(`/api/watches/${editingId}`, { method: "PUT", body: readWatchForm() });
    else await api("/api/watches", { method: "POST", body: readWatchForm() });
    closeForm();
    toast("Page saved");
    await loadWatches();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#watches").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = button.dataset.id;
  const watch = watches.find((w) => w.id === id);
  const action = button.dataset.action;
  try {
    if (action === "scan") return runScan(id);
    if (action === "edit") return openForm(watch);
    if (action === "delete") {
      if (!confirm(`Delete "${watch.name}"?`)) return;
      await api(`/api/watches/${id}`, { method: "DELETE" });
      toast("Page deleted");
    }
    if (action === "toggle") {
      await api(`/api/watches/${id}`, { method: "PUT", body: { enabled: !watch.enabled } });
    }
    if (action === "check") {
      button.disabled = true;
      const updated = await api(`/api/watches/${id}/check`, { method: "POST" });
      const errors = updated.state?.notify_errors || [];
      if (errors.length) toast("Notification not sent: " + errors.join(" ; "), true);
      else toast("Check complete");
    }
    await loadWatches();
  } catch (err) {
    toast(err.message, true);
    await loadWatches();
  }
});

$("#watches").addEventListener("error", (event) => {
  if (!event.target.matches("img.watch-thumbnail")) return;
  const message = document.createElement("span");
  message.className = "url";
  message.textContent = "Capture unavailable";
  event.target.closest(".thumbnail-link").replaceWith(message);
  toast("Could not load the last check capture. Run a new check to try again.", true);
}, true);

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const body = {
    email_sender: form.elements.email_sender.value,
    email_recipient: form.elements.email_recipient.value,
  };
  try {
    await api("/api/settings", { method: "PUT", body });
    toast("Settings saved");
    await loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    await api("/api/login", {
      method: "PUT",
      body: { username: form.elements.username.value, password: form.elements.password.value },
    });
    form.elements.password.value = "";
    toast("Login updated. Sign in again with the new credentials if asked.");
    $("#login-status").textContent = "Saved login is active.";
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-test").addEventListener("click", async () => {
  try {
    const result = await api("/api/settings/test", { method: "POST" });
    if (result.errors.length) toast("Failed: " + result.errors.join(" ; "), true);
    else toast("Test notification sent");
  } catch (err) {
    toast(err.message, true);
  }
});

$("#btn-toggle-email").addEventListener("click", async () => {
  try {
    await api("/api/settings", { method: "PUT", body: { email_enabled: !emailEnabled } });
    toast(emailEnabled ? "Sending disabled: alerts will be simulated" : "Sending enabled");
    await loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
});

function setSectionCollapsed(name, collapsed) {
  document.getElementById(`${name}-body`).classList.toggle("hidden", collapsed);
  const button = document.querySelector(`[data-collapse="${name}"]`);
  button.classList.toggle("collapsed", collapsed);
  button.setAttribute("aria-expanded", String(!collapsed));
  button.title = collapsed ? "Expand" : "Collapse";
  button.setAttribute("aria-label", button.title);
}

document.querySelectorAll("[data-collapse]").forEach((button) => {
  const name = button.dataset.collapse;
  setSectionCollapsed(name, localStorage.getItem(`wpc.${name}.collapsed`) === "1");
  button.addEventListener("click", () => {
    const collapsed = !button.classList.contains("collapsed");
    localStorage.setItem(`wpc.${name}.collapsed`, collapsed ? "1" : "0");
    setSectionCollapsed(name, collapsed);
  });
});

async function init() {
  try {
    await loadSettings();
    await loadLogin();
    await loadWatches();
    await loadAlerts();
  } catch (err) {
    toast(err.message, true);
  }
  setInterval(() => {
    loadWatches().catch(() => {});
    loadAlerts().catch(() => {});
  }, 30000);
}

init();
