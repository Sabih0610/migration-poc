"""Tests for Phase 7 hardening (UI logic and DB isolation)."""

import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def test_deployment_js_logic():
    """Verify frontend logic without a JS runtime."""
    js_path = PROJECT_ROOT / "frontend" / "deployment.js"
    assert js_path.exists()
    
    content = js_path.read_text(encoding="utf-8")
    
    # Check that button is disabled unless APPROVED
    assert "$(\"btn-start\").disabled = !approved;" in content or "$('btn-start').disabled = !approved;" in content
    
    # Check reason is shown
    assert "Reason: Approval status is" in content
    
    # Check double-click prevention
    assert "$(\"btn-start\").disabled = true;" in content
    assert "$(\"btn-start\").disabled = false;" in content
    
    # Check API errors shown
    assert "Deployment failed (API Error)" in content

def test_verify_scripts_leave_main_db_unchanged():
    """Ensure verify scripts use temporary DBs and don't touch migration_poc.db."""
    main_db = PROJECT_ROOT / "migration_poc.db"
    
    # If the DB doesn't exist, we just ensure it's not created.
    # If it exists, we ensure its modification time does not change.
    initial_mtime = main_db.stat().st_mtime if main_db.exists() else None
    
    scripts = [
        "verify_phase2.py",
        "verify_phase3.py",
        "verify_phase4.py",
        "verify_phase5.py",
        "verify_phase6.py",
        "verify_phase7.py",
    ]
    
    for script in scripts:
        script_path = PROJECT_ROOT / "scripts" / script
        # Run the script
        result = subprocess.run(
            [".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python", str(script_path)],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0, f"{script} failed:\n{result.stdout}\n{result.stderr}"
        
        # Check DB
        if initial_mtime is None:
            assert not main_db.exists(), f"{script} created migration_poc.db!"
        else:
            current_mtime = main_db.stat().st_mtime
            assert current_mtime == initial_mtime, f"{script} modified migration_poc.db!"
