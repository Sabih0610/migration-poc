const state = { deploymentId: null };
const $ = id => document.getElementById(id);

function message(text, className = "") {
  $("msg").textContent = text;
  $("msg").className = className;
}

async function api(method, url, body) {
  try {
    const response = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, data };
  } catch (error) {
    return { ok: false, data: { detail: error.message } };
  }
}

function badge(status) {
  const node = document.createElement("span");
  node.className = `badge status-${status}`;
  node.textContent = status;
  return node;
}

async function init() {
  const response = await api("GET", "/api/deployments/latest");
  const meta = $("deployment-meta");
  meta.textContent = "";
  if (!response.ok) { meta.textContent = "No deployment found."; return; }
  const deployment = response.data;
  state.deploymentId = deployment.deployment_id;
  meta.append(document.createTextNode(`Deployment ${deployment.deployment_id} — `), badge(deployment.status), document.createTextNode(` — ${deployment.mode}`));
  const eligible = deployment.status === "SUCCEEDED" && deployment.mode === "MOCK";
  $("btn-validate").disabled = !eligible;
  $("btn-runtime").disabled = !eligible;
  if (!eligible) message("Structural validation requires a successful MOCK deployment.", "error");
}

async function runStructural() {
  $("btn-validate").disabled = true;
  message("Comparing artifact definitions…");
  const response = await api("POST", "/api/validations/run", { deployment_id: state.deploymentId });
  $("btn-validate").disabled = false;
  if (!response.ok) { message(response.data.detail || "Validation failed.", "error"); return; }
  message("Structural validation completed.", "ok");
  render(response.data);
}

async function runRuntime() {
  $("btn-runtime").disabled = true;
  const response = await api("POST", "/api/runtime-validations/run", { deployment_id: state.deploymentId });
  $("btn-runtime").disabled = false;
  message(response.ok ? `Optional runtime checks: ${response.data.status}. Structural status is unchanged.` : (response.data.detail || "Runtime checks failed."), response.ok ? "ok" : "error");
}

function render(data) {
  $("results").hidden = false;
  const summary = $("val-summary"); summary.textContent = "";
  summary.append(badge(data.status), document.createTextNode(` — ${data.summary.passed} passed, ${data.summary.warnings} warnings, ${data.summary.failed} failed (${data.summary.total_checks} checks)`));
  const reports = $("reports"); reports.textContent = "";
  for (const [label, extension] of [["JSON report", "json"], ["HTML report", "html"]]) {
    const link = document.createElement("a"); link.textContent = label; link.href = `/api/reports/${data.validation_id}.${extension}`; link.target = "_blank"; link.rel = "noopener";
    if (reports.childNodes.length) reports.append(document.createTextNode(" | "));
    reports.append(link);
  }
  const body = $("val-checks"); body.textContent = "";
  for (const check of data.checks) {
    const row = document.createElement("tr");
    for (const value of [check.category, null, check.source_reference || "", check.target_artifact_id || "", check.message]) {
      const cell = document.createElement("td");
      if (value === null) cell.append(badge(check.status)); else cell.textContent = value;
      row.append(cell);
    }
    body.append(row);
  }
}

$("btn-validate").addEventListener("click", runStructural);
$("btn-runtime").addEventListener("click", runRuntime);
window.addEventListener("load", init);

// ── Phase 11: controlled execution + runtime-equivalence validation ─────

function execMessage(text, className = "") {
  $("exec-msg").textContent = text;
  $("exec-msg").className = className;
}

async function loadReadiness() {
  const [source, target] = await Promise.all([
    api("GET", "/api/executions/source-readiness"),
    api("GET", "/api/executions/target-readiness"),
  ]);
  const box = $("exec-readiness");
  box.textContent = "";
  const line = (label, data) => {
    const p = document.createElement("p");
    p.append(
      document.createTextNode(`${label}: `),
      badge(data.ready ? "SUCCEEDED" : "FAILED"),
      document.createTextNode(data.ready ? " ready" : ` not ready (enabled=${data.enabled}, missing=${(data.missing_settings || []).join(", ") || "none"})`)
    );
    return p;
  };
  box.append(line("Source (ADF)", source.data), line("Target (Fabric)", target.data));
  $("btn-start-source").disabled = !source.data.ready;
  $("btn-start-target").disabled = !target.data.ready;
}

async function loadExecutionHistory() {
  const response = await api("GET", "/api/executions");
  const body = $("exec-history");
  body.textContent = "";
  if (!response.ok) return;
  for (const execution of response.data) {
    const row = document.createElement("tr");
    for (const value of [execution.execution_id, execution.side, execution.pipeline_identity, null, execution.run_id || "", execution.duration_seconds ?? ""]) {
      const cell = document.createElement("td");
      if (value === null) cell.append(badge(execution.status)); else cell.textContent = value;
      row.append(cell);
    }
    body.append(row);
  }
}

async function startSourceExecution() {
  execMessage("Starting controlled source execution…");
  const response = await api("POST", "/api/executions/source/start", {});
  execMessage(response.ok ? `Source execution ${response.data.execution_id}: ${response.data.status}.` : (response.data.detail?.message || response.data.detail || "Failed to start."), response.ok ? "ok" : "error");
  loadExecutionHistory();
}

async function startTargetExecution() {
  const planId = Number($("in-target-plan").value);
  const deploymentId = Number($("in-target-deployment").value);
  if (!planId || !deploymentId) {
    execMessage("Enter both a plan id and a REAL deployment id to start the target execution.", "error");
    return;
  }
  execMessage("Starting controlled target execution (requires full authorization)…");
  const response = await api("POST", "/api/executions/target/start", { plan_id: planId, deployment_id: deploymentId });
  execMessage(response.ok ? `Target execution ${response.data.execution_id}: ${response.data.status}.` : (response.data.detail?.message || response.data.detail || "Failed to start."), response.ok ? "ok" : "error");
  loadExecutionHistory();
}

async function runRuntimeValidationComparison() {
  const sourceExecutionId = Number($("in-source-exec").value);
  const targetExecutionId = Number($("in-target-exec").value);
  $("runtime-val-msg").textContent = "Comparing runtime metrics…";
  const response = await api("POST", "/api/executions/runtime-validation/start", {
    source_execution_id: sourceExecutionId,
    target_execution_id: targetExecutionId,
  });
  if (!response.ok) {
    $("runtime-val-msg").textContent = response.data.detail?.message || response.data.detail || "Comparison failed.";
    $("runtime-val-msg").className = "error";
    return;
  }
  $("runtime-val-msg").textContent = `Runtime validation: ${response.data.status}. Structural status above is unchanged.`;
  $("runtime-val-msg").className = "ok";
  const summary = $("runtime-val-summary");
  summary.textContent = "";
  summary.append(badge(response.data.status), document.createTextNode(` — ${response.data.summary.passed} passed, ${response.data.summary.warnings} warnings, ${response.data.summary.failed} failed, ${response.data.summary.inconclusive} inconclusive (${response.data.summary.total_checks} checks)`));
  const body = $("runtime-val-checks");
  body.textContent = "";
  for (const check of response.data.checks) {
    const row = document.createElement("tr");
    for (const value of [check.name, null, check.source_value, check.target_value, check.tolerance, check.explanation]) {
      const cell = document.createElement("td");
      if (value === null) cell.append(badge(check.status)); else cell.textContent = value ?? "";
      row.append(cell);
    }
    body.append(row);
  }
}

$("btn-start-source").addEventListener("click", startSourceExecution);
$("btn-start-target").addEventListener("click", startTargetExecution);
$("btn-run-runtime-validation").addEventListener("click", runRuntimeValidationComparison);
window.addEventListener("load", loadReadiness);
window.addEventListener("load", loadExecutionHistory);
