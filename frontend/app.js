"use strict";
/*
 * AI Document Processing Pipeline - front end.
 *
 * No frameworks, no build step. Everything the API returns is inserted with
 * textContent (never innerHTML), so document content can never be executed even
 * if it contains markup or script-like text.
 */
const API_BASE = window.DOC_API_BASE || "http://localhost:8000/api/v1";
const TOKEN_KEY = "adp.token";

const state = {
  token: sessionStorage.getItem(TOKEN_KEY) || null,
  user: null,
  meta: null,
  view: "dashboard",
  documents: { page: 1, page_size: 20, search: "", status: "", document_type: "" },
  audit: { page: 1, page_size: 50, action: "", all: false },
  current: null,      // open document detail
  authMode: "login",
};

/* ------------------------------------------------------------------ utils */
const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function fmt(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function money(amount, currency) {
  if (amount === null || amount === undefined || amount === "") return "—";
  const value = Number(amount);
  const shown = Number.isFinite(value)
    ? value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : String(amount);
  return currency ? `${shown} ${currency}` : shown;
}

function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString();
}

function statusBadge(status) {
  return el("span", { class: `status ${status || ""}`, text: String(status || "").replace(/_/g, " ") });
}

function flatten(value, prefix = "", out = {}) {
  if (value === null || value === undefined) return out;
  if (Array.isArray(value)) {
    if (!value.length) return out;
    value.forEach((item, index) => flatten(item, `${prefix}[${index}]`, out));
    return out;
  }
  if (typeof value === "object") {
    Object.entries(value).forEach(([key, item]) => flatten(item, prefix ? `${prefix}.${key}` : key, out));
    return out;
  }
  out[prefix] = typeof value === "boolean" ? String(value) : String(value);
  return out;
}

/* -------------------------------------------------------------- API client */
class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(path, options = {}) {
  const { method = "GET", body, headers = {}, raw = false } = options;
  const request = { method, headers: { ...headers } };
  if (state.token) request.headers.Authorization = `Bearer ${state.token}`;
  if (body instanceof FormData) {
    request.body = body;
  } else if (body !== undefined) {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, request);
  } catch (cause) {
    throw new ApiError(0, "network", `Cannot reach the API at ${API_BASE}.`);
  }
  // A 401 from the sign-in / sign-up endpoints is just "wrong credentials" -
  // only a 401 on an authenticated call means the session really expired.
  const isAuthEntry = path.startsWith("/auth/login") || path.startsWith("/auth/register");
  if (response.status === 401 && !isAuthEntry) {
    signOut(true);
    throw new ApiError(401, "unauthorized", "Your session expired. Please sign in again.");
  }
  if (raw) {
    if (!response.ok) throw await toError(response);
    return response;
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw await toError(response, data);
  return data;
}

async function toError(response, data) {
  const body = data || (await response.json().catch(() => ({})));
  return new ApiError(
    response.status,
    (body.error && body.error.code) || "error",
    (body.error && body.error.message) || `Request failed (${response.status}).`
  );
}

let toastTimer = null;
function toast(message, kind = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", kind === "error");
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 4500);
}

/* ------------------------------------------------------------------- auth */
function setToken(token) {
  state.token = token;
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function showAuth() {
  $("#app-view").classList.add("hidden");
  $("#auth-view").classList.remove("hidden");
  $("#auth-error").textContent = "";
}

function showApp() {
  $("#auth-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#who-email").textContent = state.user ? state.user.email : "";
  $("#who-role").textContent = state.user ? state.user.role : "";
}

async function signOut(silent = false) {
  try {
    if (!silent && state.token) await api("/auth/logout", { method: "POST" });
  } catch (_) {
    /* signing out locally is always possible */
  }
  setToken(null);
  state.user = null;
  closeDrawer();
  // Always return to the sign-in tab with an empty form: staying on "register"
  // makes the next sign-in attempt look like a duplicate-account error.
  setAuthMode("login");
  $("#auth-email").value = "";
  $("#auth-password").value = "";
  showAuth();
}

/* ------------------------------------------------------------------- boot */
async function boot() {
  try {
    state.meta = await api("/meta");
  } catch (error) {
    state.meta = null;
  }
  try {
    state.user = await api("/auth/me");
  } catch (_) {
    return showAuth();
  }
  fillSelect($("#f-status"), state.meta ? state.meta.statuses : []);
  fillSelect($("#f-type"), state.meta ? state.meta.document_types : []);
  fillSelect(
    $("#a-action"),
    ["register", "login", "login_failed", "logout", "upload", "view", "download", "edit",
      "approve", "reject", "reprocess", "delete", "export"]
  );
  const limits = (state.meta && state.meta.limits) || {};
  $("#upload-limit").textContent = `PDF, TXT or Markdown · up to ${fmt(limits.max_upload_mb)} MB`;
  // The global audit view is admin-only; hide the control instead of letting
  // a regular user trigger a 403.
  $("#a-all").closest("label").classList.toggle("hidden", state.user.role !== "admin");
  showApp();
  navigate("dashboard");
}

function fillSelect(select, values) {
  const first = select.options[0];
  clear(select);
  if (first) select.appendChild(first);
  values.forEach((value) => select.appendChild(el("option", { value, text: value.replace(/_/g, " ") })));
}

function navigate(view) {
  state.view = view;
  ["dashboard", "documents", "review", "audit"].forEach((name) => {
    $(`#view-${name}`).classList.toggle("hidden", name !== view);
  });
  document.querySelectorAll(".nav-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (view === "dashboard") loadDashboard();
  if (view === "documents") loadDocuments();
  if (view === "review") loadQueue();
  if (view === "audit") loadAudit();
}

/* -------------------------------------------------------------- dashboard */
async function loadDashboard() {
  const cards = $("#stats-cards");
  clear(cards);
  let stats;
  try {
    stats = await api("/stats");
  } catch (error) {
    return toast(error.message, "error");
  }
  const entries = [
    ["Documents", stats.total],
    ["Processed", stats.processed],
    ["Needs review", stats.needs_review],
    ["Approved", (stats.by_status || {}).APPROVED || 0],
    ["Duplicates", stats.flagged_duplicates || 0],
  ];
  entries.forEach(([label, value]) => {
    cards.appendChild(el("div", { class: "card" }, [
      el("div", { class: "value", text: fmt(value, "0") }),
      el("div", { class: "label", text: label }),
    ]));
  });

  const totals = $("#approved-totals");
  clear(totals);
  const approved = Object.entries(stats.approved_totals || {});
  if (!approved.length) {
    totals.appendChild(el("p", { class: "muted", text: "No approved documents yet." }));
  } else {
    const list = el("div", { class: "kv" });
    approved.forEach(([currency, value]) => {
      list.appendChild(el("dt", { text: currency }));
      list.appendChild(el("dd", { text: money(value, currency) }));
    });
    totals.appendChild(list);
  }
  totals.appendChild(
    el("div", { class: "export-actions" }, [
      el("button", { type: "button", class: "small", text: "Export CSV (approved)", onclick: () => downloadExport("csv") }),
      el("button", { type: "button", class: "small", text: "Export JSON (approved)", onclick: () => downloadExport("json") }),
    ])
  );
}

/* -------------------------------------------------------------- documents */
function documentRow(document) {
  return el("tr", {}, [
    el("td", { text: fmt(document.original_filename) }),
    el("td", { text: fmt(document.document_type) }),
    el("td", { text: fmt(document.document_number) }),
    el("td", { text: fmt(document.vendor_name) }),
    el("td", { text: money(document.total_amount, document.currency) }),
    el("td", {}, [statusBadge(document.status)]),
    el("td", { text: String(document.duplicate_status || "").replace(/_/g, " ") }),
    el("td", { text: dateTime(document.uploaded_at) }),
    el("td", { class: "actions" }, [
      el("button", { type: "button", class: "small", text: "Open", onclick: () => openDocument(document.id) }),
    ]),
  ]);
}

function renderPager(node, page, totalPages, onGo) {
  clear(node);
  node.appendChild(el("button", { type: "button", class: "ghost small", text: "Previous", disabled: page <= 1, onclick: () => onGo(page - 1) }));
  node.appendChild(el("span", { class: "muted small", text: `Page ${page} of ${totalPages}` }));
  node.appendChild(el("button", { type: "button", class: "ghost small", text: "Next", disabled: page >= totalPages, onclick: () => onGo(page + 1) }));
}

async function loadDocuments() {
  const table = $("#docs-table tbody");
  const filters = state.documents;
  const query = new URLSearchParams({ page: filters.page, page_size: filters.page_size });
  if (filters.search) query.set("search", filters.search);
  if (filters.status) query.set("status", filters.status);
  if (filters.document_type) query.set("document_type", filters.document_type);

  let data;
  try {
    data = await api(`/documents?${query.toString()}`);
  } catch (error) {
    return toast(error.message, "error");
  }
  clear(table);
  if (!data.items.length) {
    table.appendChild(el("tr", {}, [el("td", { colspan: "9", class: "muted", text: "No documents match these filters." })]));
  }
  data.items.forEach((document) => table.appendChild(documentRow(document)));
  renderPager($("#docs-pager"), data.page, data.total_pages, (page) => {
    state.documents.page = page;
    loadDocuments();
  });
}

async function loadQueue() {
  const table = $("#review-table tbody");
  let data;
  try {
    data = await api("/review/queue?page_size=50");
  } catch (error) {
    return toast(error.message, "error");
  }
  clear(table);
  if (!data.items.length) {
    table.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "Nothing is waiting for review." })]));
  }
  data.items.forEach((document) => {
    table.appendChild(el("tr", {}, [
      el("td", { text: fmt(document.original_filename) }),
      el("td", { text: fmt(document.document_type) }),
      el("td", { text: fmt(document.document_number) }),
      el("td", { text: money(document.total_amount, document.currency) }),
      el("td", { text: document.duplicate_status === "possible_duplicate" ? "Possible duplicate" : "Findings" }),
      el("td", { class: "actions" }, [
        el("button", { type: "button", class: "small", text: "Open", onclick: () => openDocument(document.id) }),
        el("button", { type: "button", class: "small ok", text: "Approve", onclick: () => approve(document.id) }),
      ]),
    ]));
  });
}

async function loadAudit() {
  const table = $("#audit-table tbody");
  const query = new URLSearchParams({ page: state.audit.page, page_size: state.audit.page_size });
  if (state.audit.action) query.set("action", state.audit.action);
  if (state.audit.all) query.set("scope", "all");

  let data;
  try {
    data = await api(`/audit?${query.toString()}`);
  } catch (error) {
    return toast(error.message, "error");
  }
  clear(table);
  if (!data.items.length) {
    table.appendChild(el("tr", {}, [el("td", { colspan: "6", class: "muted", text: "No audit entries." })]));
  }
  data.items.forEach((entry) => {
    const detail = entry.detail ? JSON.stringify(entry.detail) : "";
    table.appendChild(el("tr", {}, [
      el("td", { text: dateTime(entry.timestamp) }),
      el("td", { text: fmt(entry.actor_user_id) }),
      el("td", { text: entry.action }),
      el("td", { text: entry.target_document_id ? entry.target_document_id.slice(0, 8) : "—" }),
      el("td", { text: entry.outcome }),
      el("td", { class: "muted small", text: detail }),
    ]));
  });
  renderPager($("#audit-pager"), data.page, data.total_pages, (page) => {
    state.audit.page = page;
    loadAudit();
  });
}

/* ------------------------------------------------------------ detail drawer */
function closeDrawer() {
  $("#drawer").classList.add("hidden");
  $("#drawer-backdrop").classList.add("hidden");
  state.current = null;
}

async function openDocument(id) {
  $("#drawer").classList.remove("hidden");
  $("#drawer-backdrop").classList.remove("hidden");
  $("#drawer-body").replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  try {
    const data = await api(`/documents/${encodeURIComponent(id)}`);
    state.current = data.document;
    renderDrawer(data.document);
  } catch (error) {
    toast(error.message, "error");
    closeDrawer();
  }
}

function summaryList(document) {
  const list = el("dl", { class: "kv" });
  const rows = [
    ["File name", document.original_filename],
    ["Type", document.document_type],
    ["Number", document.document_number],
    ["Vendor", document.vendor_name],
    ["Date", document.document_date],
    ["Total", money(document.total_amount, document.currency)],
    ["Status", document.status],
    ["Validation", document.validation_status],
    ["Duplicate", document.duplicate_status],
    ["File size", `${(document.file_size / 1024).toFixed(1)} KB`],
    ["Pages / characters", `${fmt(document.page_count)} / ${fmt(document.text_length)}`],
    ["Provider", document.ai_provider],
    ["Processing time", document.processing_ms ? `${document.processing_ms} ms` : "—"],
    ["Uploaded", dateTime(document.uploaded_at)],
    ["Reviewed", document.reviewed_at ? `${dateTime(document.reviewed_at)}` : "—"],
  ];
  if (document.error_message) rows.push(["Error", document.error_message]);
  if (document.rejection_reason) rows.push(["Rejection reason", document.rejection_reason]);
  rows.forEach(([label, value]) => {
    list.appendChild(el("dt", { text: label }));
    list.appendChild(el("dd", { text: fmt(value) }));
  });
  return list;
}

function extractedFields(document) {
  const data = (document.extraction && document.extraction.data) || null;
  if (!data) return el("p", { class: "muted", text: "No structured data was extracted." });
  const list = el("dl", { class: "kv" });
  Object.entries(flatten(data)).forEach(([field, value]) => {
    list.appendChild(el("dt", { text: field }));
    list.appendChild(el("dd", { text: fmt(value) }));
  });
  return list;
}

function findingsList(document) {
  if (!document.findings.length) return el("p", { class: "muted", text: "No validation findings." });
  return el("div", {}, document.findings.map((finding) =>
    el("div", { class: `finding ${finding.severity}` }, [
      el("div", { text: finding.message }),
      el("div", { class: "code", text: `${finding.stage} · ${finding.code}${finding.field ? ` · ${finding.field}` : ""}` }),
    ])
  ));
}

function runsList(document) {
  if (!document.runs.length) return el("p", { class: "muted", text: "No processing runs." });
  return el("div", {}, document.runs.slice().reverse().map((run) => {
    const stages = el("div", {}, (run.stages || []).map((stage) =>
      el("div", { class: "stage" }, [
        el("span", { class: stage.status, text: stage.status }),
        el("strong", { text: stage.name }),
        el("span", { class: "muted", text: stage.detail || "" }),
      ])
    ));
    return el("div", { class: "run" }, [
      el("div", {}, [
        el("strong", { text: `Attempt ${run.attempt}` }),
        el("span", { class: "muted", text: ` · ${run.provider} · ${dateTime(run.finished_at || run.started_at)} · ${run.duration_ms || 0} ms` }),
      ]),
      stages,
    ]);
  }));
}

function correctionsList(document) {
  if (!document.corrections.length) return el("p", { class: "muted", text: "No human corrections yet." });
  return el("div", {}, document.corrections.map((correction) =>
    el("div", { class: "run" }, [
      el("div", {}, [
        el("strong", { text: correction.field_path }),
        el("span", { class: "muted", text: ` · ${dateTime(correction.created_at)}` }),
      ]),
      el("div", { class: "small muted", text: `${fmt(correction.original_value, "(empty)")} → ${fmt(correction.corrected_value, "(empty)")}` }),
    ])
  ));
}

function renderDrawer(document) {
  $("#drawer-title").textContent = document.original_filename;
  const badges = $("#drawer-badges");
  clear(badges);
  badges.appendChild(statusBadge(document.status));
  if (document.duplicate_status && document.duplicate_status !== "unique") {
    badges.appendChild(el("span", { class: "badge", text: `duplicate: ${document.duplicate_status.replace(/_/g, " ")}` }));
  }
  if (document.validation_status) {
    badges.appendChild(el("span", { class: "badge", text: `validation: ${document.validation_status}` }));
  }

  const actions = $("#drawer-actions");
  clear(actions);
  actions.appendChild(el("button", { type: "button", class: "small", text: "Download", onclick: () => downloadSource(document.id) }));
  if (document.actions.can_reprocess) {
    actions.appendChild(el("button", { type: "button", class: "small", text: "Reprocess", onclick: () => reprocess(document.id) }));
  }
  if (document.actions.can_approve) {
    actions.appendChild(el("button", { type: "button", class: "small ok", text: "Approve", onclick: () => approve(document.id) }));
  }
  if (document.actions.can_reject) {
    const reason = el("input", { type: "text", maxlength: "300", placeholder: "Reason for rejection" });
    actions.appendChild(el("div", { class: "reject-box" }, [
      reason,
      el("button", {
        type: "button", class: "small danger", text: "Reject",
        onclick: () => rejectDocument(document.id, reason.value),
      }),
    ]));
  }
  if (document.actions.can_delete) {
    actions.appendChild(el("button", { type: "button", class: "small ghost", text: "Delete", onclick: () => removeDocument(document.id) }));
  }

  const body = $("#drawer-body");
  clear(body);
  body.appendChild(el("section", {}, [el("h3", { text: "Summary" }), summaryList(document)]));
  body.appendChild(el("section", {}, [el("h3", { text: "Extracted fields" }), extractedFields(document)]));
  body.appendChild(el("section", {}, [el("h3", { text: "Findings" }), findingsList(document)]));
  body.appendChild(el("section", {}, [
    el("h3", { text: "Human corrections" }),
    correctionsList(document),
    document.actions.can_correct ? correctionForm(document) : el("div"),
  ]));
  body.appendChild(el("section", {}, [el("h3", { text: "Processing runs" }), runsList(document)]));
  body.appendChild(el("section", {}, [
    el("h3", { text: "Source text" }),
    el("button", {
      type: "button", class: "small", text: "Show extracted text",
      onclick: (event) => showSourceText(document.id, event.target.parentNode),
    }),
  ]));
}

function correctionForm(document) {
  const field = el("input", { type: "text", placeholder: "field, e.g. invoice_number", maxlength: "120" });
  const value = el("input", { type: "text", placeholder: "new value" });
  return el("form", {
    class: "correct-form",
    onsubmit: (event) => {
      event.preventDefault();
      applyCorrection(document.id, field.value.trim(), value.value);
    },
  }, [
    el("div", {}, [el("label", { text: "Field" }), field]),
    el("div", {}, [el("label", { text: "Value" }), value]),
    el("button", { type: "submit", class: "small primary", text: "Save correction" }),
  ]);
}

/* --------------------------------------------------------- drawer actions */
async function refreshCurrent() {
  if (state.current) await openDocument(state.current.id);
  if (state.view === "documents") loadDocuments();
  if (state.view === "review") loadQueue();
  if (state.view === "dashboard") loadDashboard();
}

async function approve(id) {
  try {
    await api(`/documents/${id}/approve`, { method: "POST", body: {} });
    toast("Document approved.");
    await refreshCurrent();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function rejectDocument(id, reason) {
  try {
    await api(`/documents/${id}/reject`, { method: "POST", body: { reason } });
    toast("Document rejected.");
    await refreshCurrent();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function reprocess(id) {
  toast("Reprocessing…");
  try {
    const result = await api(`/documents/${id}/reprocess`, { method: "POST" });
    toast(`Reprocessed → ${String(result.status).replace(/_/g, " ")}.`);
    await refreshCurrent();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function applyCorrection(id, fieldPath, value) {
  try {
    await api(`/documents/${id}/corrections`, {
      method: "PATCH",
      body: { field_path: fieldPath, value: value === "" ? null : value },
    });
    toast("Correction saved; all checks re-ran.");
    await refreshCurrent();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function removeDocument(id) {
  if (!window.confirm("Delete this document and its stored file?")) return;
  try {
    await api(`/documents/${id}`, { method: "DELETE" });
    toast("Document deleted.");
    closeDrawer();
    if (state.view === "documents") loadDocuments();
    if (state.view === "review") loadQueue();
    if (state.view === "dashboard") loadDashboard();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function saveBlob(response, fallbackName) {
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const link = el("a", { href: URL.createObjectURL(blob), download: match ? match[1] : fallbackName });
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

async function downloadSource(id) {
  try {
    await saveBlob(await api(`/documents/${id}/download`, { raw: true }), "document");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function downloadExport(format) {
  try {
    await saveBlob(await api(`/export?format=${format}`, { raw: true }), `export.${format}`);
    toast(`Exported ${format.toUpperCase()} of the approved documents.`);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function showSourceText(id, container) {
  clear(container);
  container.appendChild(el("p", { class: "muted", text: "Loading…" }));
  try {
    const data = await api(`/documents/${id}/text`);
    clear(container);
    container.appendChild(el("pre", { class: "source", text: data.text }));
  } catch (error) {
    clear(container);
    container.appendChild(el("p", { class: "muted", text: error.message }));
  }
}

/* ------------------------------------------------------------------ wiring */
function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  $("#tab-login").classList.toggle("active", !registering);
  $("#tab-register").classList.toggle("active", registering);
  $("#auth-submit").textContent = registering ? "Create account" : "Sign in";
  $("#auth-error").textContent = "";
  $("#password-hint").hidden = !registering;
  $("#auth-password").setAttribute(
    "autocomplete", registering ? "new-password" : "current-password"
  );
}

$("#tab-login").addEventListener("click", () => setAuthMode("login"));
$("#tab-register").addEventListener("click", () => setAuthMode("register"));

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const email = $("#auth-email").value.trim();
  const password = $("#auth-password").value;
  const button = $("#auth-submit");
  button.disabled = true;
  $("#auth-error").textContent = "";
  try {
    const path = state.authMode === "register" ? "/auth/register" : "/auth/login";
    const result = await api(path, { method: "POST", body: { email, password } });
    setToken(result.access_token);
    state.user = result.user;
    $("#auth-password").value = "";
    await boot();
  } catch (error) {
    $("#auth-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#logout-btn").addEventListener("click", () => signOut());

document.querySelectorAll(".nav-btn").forEach((button) =>
  button.addEventListener("click", () => navigate(button.dataset.view)));

$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDrawer();
});

$("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#file-input");
  if (!input.files || !input.files.length) return toast("Choose a file first.", "error");
  const payload = new FormData();
  payload.append("file", input.files[0]);
  const button = $("#upload-btn");
  button.disabled = true;
  button.textContent = "Processing…";
  try {
    const result = await api("/documents/upload", { method: "POST", body: payload });
    toast(`Uploaded → ${String(result.status).replace(/_/g, " ")}.`);
    input.value = "";
    navigate("documents");
    openDocument(result.document.id);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "Upload & process";
  }
});

$("#filters-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.documents = {
    page: 1,
    page_size: state.documents.page_size,
    search: $("#f-search").value.trim(),
    status: $("#f-status").value,
    document_type: $("#f-type").value,
  };
  loadDocuments();
});

$("#f-reset").addEventListener("click", () => {
  $("#f-search").value = "";
  $("#f-status").value = "";
  $("#f-type").value = "";
  state.documents = { page: 1, page_size: 20, search: "", status: "", document_type: "" };
  loadDocuments();
});

$("#audit-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  state.audit.action = $("#a-action").value;
  state.audit.all = $("#a-all").checked;
  state.audit.page = 1;
  loadAudit();
});

setAuthMode("login");
if (state.token) boot();
else showAuth();