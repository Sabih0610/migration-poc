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
