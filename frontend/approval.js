// Minimal approval page controller — Phase 6. No framework.
// Uses the existing discovery/assessment/plan/approval APIs.

const state = { planId: null, approvalId: null };

const DECIDED = ["APPROVED", "REJECTED", "INVALIDATED"];

function $(id) {
  return document.getElementById(id);
}

function msg(text, kind) {
  const el = $("message");
  el.textContent = text;
  el.className = "message " + (kind || "");
}

function getUser() {
  return ($("user").value || "").trim();
}

function requireUser() {
  if (!getUser()) {
    msg("A non-blank user is required.", "error");
    return false;
  }
  return true;
}

async function api(method, url, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(url, opts);
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { detail: "Network error: " + err } };
  }
}

// Enable/disable buttons based on the current approval status.
// PENDING       -> Request disabled, Approve/Reject enabled.
// APPROVED/REJECTED/INVALIDATED -> Approve/Reject disabled, Request enabled.
// NONE / none   -> Request enabled, Approve/Reject disabled.
function setButtonStates(status) {
  const hasPlan = state.planId !== null;
  const isPending = status === "PENDING";
  const isDecided = DECIDED.includes(status);

  $("btn-request").disabled = !hasPlan || isPending;
  $("btn-approve").disabled = !hasPlan || !isPending || isDecided;
  $("btn-reject").disabled = !hasPlan || !isPending || isDecided;
}

function renderPlan(plan, meta) {
  $("plan-meta").innerHTML =
    `<span class="badge">Plan #${meta.plan_id} v${meta.version}</span>` +
    `<span class="badge risk-${plan.overall_risk}">Risk: ${plan.overall_risk}</span>` +
    `<span class="badge">${plan.executable ? "Executable" : "Not executable"}</span>`;

  const tbody = $("actions-table").querySelector("tbody");
  tbody.innerHTML = "";
  (plan.actions || []).forEach((a) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${a.order}</td><td>${a.action_type}</td>` +
      `<td>${a.target_item_type}: ${a.target_item_name}</td>` +
      `<td>${a.risk}</td><td>${a.approval_required ? "yes" : "no"}</td>`;
    tbody.appendChild(tr);
  });

  const manual = $("manual-list");
  manual.innerHTML = "";
  if (!plan.manual_actions || plan.manual_actions.length === 0) {
    manual.innerHTML = "<li class='muted'>None</li>";
  } else {
    plan.manual_actions.forEach((m) => {
      const li = document.createElement("li");
      li.textContent = `${m.source_asset}: ${m.reason}`;
      manual.appendChild(li);
    });
  }

  const artifactBody = $("artifacts-table").querySelector("tbody");
  artifactBody.innerHTML = "";
  const artifacts = (plan.generated_package || {}).artifacts || [];
  artifacts.forEach((artifact) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${artifact.artifact_id}</td>` +
      `<td>${artifact.target_type}: ${artifact.target_name}</td>` +
      `<td title="${artifact.content_digest}">${artifact.content_digest.slice(0, 12)}</td>` +
      `<td>${(artifact.warnings || []).join("; ") || "none"}</td>`;
    artifactBody.appendChild(tr);
  });

  const rules = $("validation-list");
  rules.innerHTML = "";
  (plan.validation_rules || []).forEach((r) => {
    const li = document.createElement("li");
    li.textContent = `${r.name} (${r.rule_type}, tol ${r.tolerance})`;
    rules.appendChild(li);
  });
}

function renderApproval(status) {
  const el = $("approval-status");
  const value = status ? status.status : "NONE";
  if (!status || value === "NONE") {
    el.textContent = "No approval requested yet.";
    el.className = "status";
    state.approvalId = null;
    setButtonStates("NONE");
    return;
  }
  state.approvalId = status.approval ? status.approval.approval_id : null;
  el.textContent =
    `Status: ${value}` +
    (status.can_deploy ? " — deployment allowed" : " — deployment blocked");
  el.className = "status status-" + value;
  setButtonStates(value);
}

async function loadPlan() {
  const res = await api("GET", "/api/plans/latest");
  if (!res.ok) {
    $("plan-meta").textContent =
      "No plan found. Run discovery, assessment, and plan generation first.";
    setButtonStates(null);
    return;
  }
  state.planId = res.data.plan_id;
  renderPlan(res.data.plan, res.data);
  await loadApprovalStatus();
}

async function loadApprovalStatus() {
  if (state.planId === null) return;
  const res = await api("GET", `/api/plans/${state.planId}/approval-status`);
  if (res.ok) {
    renderApproval(res.data);
  } else {
    msg(res.data.detail || "Could not load approval status.", "error");
  }
}

function body() {
  return { user: getUser(), comment: $("comment").value };
}

async function requestApproval() {
  if (state.planId === null) return msg("No plan loaded.", "error");
  if (!requireUser()) return;
  const res = await api("POST", `/api/plans/${state.planId}/request-approval`, body());
  if (res.ok) {
    msg("Approval requested.", "ok");
    await loadApprovalStatus();
  } else {
    msg(res.data.detail || "Request failed.", "error");
  }
}

async function decide(kind) {
  if (!state.approvalId) return msg("No pending approval.", "error");
  if (!requireUser()) return;
  const res = await api("POST", `/api/approvals/${state.approvalId}/${kind}`, body());
  if (res.ok) {
    msg(`Approval ${kind}d.`, "ok");
    await loadApprovalStatus();
  } else {
    msg(res.data.detail || "Decision failed.", "error");
  }
}

window.addEventListener("DOMContentLoaded", () => {
  // Start with everything disabled until a plan loads.
  setButtonStates(null);
  $("btn-request").addEventListener("click", requestApproval);
  $("btn-approve").addEventListener("click", () => decide("approve"));
  $("btn-reject").addEventListener("click", () => decide("reject"));
  loadPlan();
});
