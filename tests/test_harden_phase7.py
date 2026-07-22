"""Tests for Phase 7 hardening (UI logic and DB isolation)."""

import subprocess
import sys
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
    """Verification uses temporary DB, generated, and report directories."""
    main_db = PROJECT_ROOT / "migration_poc.db"
    generated = PROJECT_ROOT / "generated"
    reports = PROJECT_ROOT / "reports"

    def snapshot(directory):
        if not directory.exists():
            return None
        return {
            str(path.relative_to(directory)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in directory.rglob("*")
            if path.is_file()
        }
    
    # If the DB doesn't exist, we just ensure it's not created.
    # If it exists, we ensure its modification time does not change.
    initial_mtime = main_db.stat().st_mtime if main_db.exists() else None
    initial_generated = snapshot(generated)
    initial_reports = snapshot(reports)
    
    scripts = [
        "verify_phase2.py",
        "verify_phase3.py",
        "verify_phase4.py",
        "verify_phase5.py",
        "verify_phase6.py",
        "verify_phase7.py",
        "verify_phase8.py",
        "verify_phase8_foundation.py",
        "verify_phase8_packages.py",
        "verify_phase8_deployment.py",
        "verify_phase8_structural.py",
        "verify_phase10_deployment.py",
        "verify_phase10_corrected.py",
    ]
    
    for script in scripts:
        script_path = PROJECT_ROOT / "scripts" / script
        # Run the script
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_ROOT),
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
        assert snapshot(generated) == initial_generated, (
            f"{script} modified the workspace generated directory!"
        )
        assert snapshot(reports) == initial_reports, (
            f"{script} modified the workspace reports directory!"
        )
