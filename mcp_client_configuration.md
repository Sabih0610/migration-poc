# MCP Client Configuration & Connection Guide

## 1. Architecture
The MCP (Model Context Protocol) layer acts as a strictly controlled interface between an AI client and the deterministic Python migration services. 

AI client -> Local Python STDIO MCP server -> Existing deterministic migration services -> Azure ADF & Fabric connectors

## 2. MCP Server Purpose
The server securely exposes specific migration read and state-change workflows (discovery, assessment, planning, guarded deployment, and execution) without permitting arbitrary cloud operations, deleting resources, or AI self-approval.

## 3. Launch Command
The server must be launched via standard STDIO:
```bash
.venv/Scripts/python.exe -m src.mcp.server
```

## 4. Virtual-Environment Requirement
The server must be launched using the project's virtual-environment executable (`.venv/Scripts/python.exe`) so that all required migration dependencies are available.

## 5. Safe Client Configuration Example
Below is an example configuration for an MCP-compatible client. Notice that no actual secrets are embedded; it strictly utilizes environment variables.

```json
{
  "mcpServers": {
    "migration_poc": {
      "command": "C:\\RC-Projects\\migration-poc\\.venv\\Scripts\\python.exe",
      "args": [
        "-m",
        "src.mcp.server"
      ],
      "env": {
        "AZURE_TENANT_ID": "${AZURE_TENANT_ID}",
        "AZURE_CLIENT_ID": "${AZURE_CLIENT_ID}",
        "AZURE_CLIENT_SECRET": "${AZURE_CLIENT_SECRET}",
        "AZURE_SUBSCRIPTION_ID": "${AZURE_SUBSCRIPTION_ID}",
        "AZURE_RESOURCE_GROUP": "${AZURE_RESOURCE_GROUP}",
        "AZURE_DATA_FACTORY_NAME": "${AZURE_DATA_FACTORY_NAME}",
        "FABRIC_TENANT_ID": "${FABRIC_TENANT_ID}",
        "FABRIC_CLIENT_ID": "${FABRIC_CLIENT_ID}",
        "FABRIC_CLIENT_SECRET": "${FABRIC_CLIENT_SECRET}",
        "FABRIC_WORKSPACE_ID": "${FABRIC_WORKSPACE_ID}"
      }
    }
  }
}
```

## 6. Environment Prerequisites
- Active Python 3.12+ virtual environment.
- Correct `.env` containing Azure and Fabric credentials.
- `RUNTIME_EXECUTION_ENABLED=False` (unless explicit live execution is authorized).

## 7. Tool List
- `health_status`, `capability_status`, `verify_azure_environment`, `verify_fabric_environment`
- `get_discovery`, `get_dependencies`, `get_assessment`, `get_plan`, `get_package_summary`, `get_manifest_summary`
- `get_approval_status`, `get_deployment`, `get_execution`, `get_structural_validation`, `get_runtime_validation`, `get_report`, `get_final_migration_status`
- `scan_adf`, `run_assessment`, `generate_plan`
- `request_approval`, `run_structural_validation`, `run_runtime_validation`, `generate_report`
- `deploy_fabric_package`
- `run_source_pipeline`, `run_fabric_pipeline`

## 8. Permission Categories
- `READ_ONLY`: Queries state without modifications.
- `STATE_CHANGE`: Updates internal DB states safely (e.g., plans).
- `GUARDED_CLOUD_WRITE`: Modifies Fabric (only if approved).
- `GUARDED_EXECUTION`: Triggers pipelines (only if approved and enabled).

## 9. Approval Flow
- `request_approval` generates a `PENDING` request.
- The AI **cannot** mark it `APPROVED`. Approval must occur externally.

## 10. Deployment Guards
Deployment only succeeds if the underlying records match exactly (plan ID, fingerprint, approved status, workspace).

## 11. Execution Guards
Execution verifies that the source and target match the configured identifiers exactly. Arbitrary execution is blocked.

## 12. Restart/Resume Behavior
The server is stateless in-memory and relies entirely on SQLite DB persistence. Any interrupted workflow can be safely queried/resumed upon restart.

## 13. Logging and Redaction
Raw SDK exceptions and secrets are recursively redacted from all outputs and logs.

## 14. Limitations
- Native Gemini IDE may require an external inspector tool if local STDIO configuration isn't directly exposed in your client build.
- Large definitions are paginated or truncated.

## 15. Troubleshooting
- If MCP fails to start, verify the `.venv` activation and `mcp` dependency installation.
- If deployments return `blocked`, check external approval status.

## 16. Explicit Statement: Approval Guard
**The AI client cannot approve its own deployment.** All approvals must be provided by a human user via the external validation UI or database process.

## 17. Explicit Statement: Safety Boundaries
**No delete, shell, Python, SQL or unrestricted filesystem tool exists within this MCP server.**
