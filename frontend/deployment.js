// Minimal deployment page controller — Phase 7. No framework.
// Runs dry-run / mock deployments via the existing APIs. No REAL mode.

const state = { planId: null, approvalId: null };

function $(id) {
  return document.getElementById(id);
}

function msg(text, kind) {
  const el = $("message");
  el.textContent = text;
  el.className = "message " + (kind || "");
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

function renderSteps(steps) {
  const tbody = $("steps-table").querySelector("tbody");
  tbody.innerHTML = "";
  (steps || []).forEach((s) => {
    const tr = document.createElement("tr");
    const detail = s.error ? s.error : s.resource_id || s.message || "";
    tr.innerHTML =
      `<td>${s.order}</td><td>${s.action_type}</td>` +
      `<td>${s.target_item_type}: ${s.target_item_name}</td>` +
      `<td class="step-${s.status}">${s.status}</td>` +
      `<td>${detail}</td>`;
    tbody.appendChild(tr);
  });
}

function renderStatus(result) {
  const el = $("deploy-status");
  el.textContent = `Mode: ${result.mode} — Status: ${result.status}`;
  el.className = "status status-" + result.status;
  renderSteps(result.steps);
}

async function loadContext() {
  const plan = await api("GET", "/api/plans/latest");
  if (!plan.ok) {
    $("context-meta").textContent =
      "No plan found. Run discovery, assessment, and plan generation first.";
    $("btn-start").disabled = true;
    return;
  }
  state.planId = plan.data.plan_id;

  const status = await api(
    "GET",
    `/api/plans/${state.planId}/approval-status`
  );
  const approved =
    status.ok && status.data.status === "APPROVED" && status.data.approval;
  if (approved) {
    state.approvalId = status.data.approval.approval_id;
  }
  $("context-meta").innerHTML =
    `<span class="badge">Plan #${plan.data.plan_id} v${plan.data.version}</span>` +
    `<span class="badge risk-${plan.data.plan.overall_risk}">Risk: ${plan.data.plan.overall_risk}</span>` +
    `<span class="badge">Approval: ${status.ok ? status.data.status : "unknown"}</span>`;
  $("btn-start").disabled = !approved;
  if (!approved) {
    const reason = status.ok ? status.data.status : "API ERROR";
    msg(`Deploy button disabled. Reason: Approval status is ${reason}`, "error");
  }
}

async function startDeployment() {
  if (state.planId === null || state.approvalId === null) {
    return msg("An approved plan is required.", "error");
  }
  const mode = $("mode").value;
  msg("Deploying (" + mode + ")…", "");
  $("btn-start").disabled = true; // prevent double-click while running
  const res = await api("POST", "/api/deployments/start", {
    plan_id: state.planId,
    approval_id: state.approvalId,
    mode,
  });
  $("btn-start").disabled = false; // re-enable
  if (res.ok) {
    msg(`Deployment ${res.data.status}.`, res.data.status === "SUCCEEDED" ? "ok" : "error");
    renderStatus(res.data);
  } else {
    msg(res.data.detail || "Deployment failed (API Error).", "error");
    if (res.data.steps) renderStatus(res.data);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  $("btn-start").addEventListener("click", startDeployment);
  loadContext();
});
