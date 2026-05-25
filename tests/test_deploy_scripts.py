from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_powershell_deploy_applies_ydb_schema_before_function_version():
    script = (ROOT / "scripts" / "deploy-yc.ps1").read_text(encoding="utf-8")

    assert "YDB_ACCESS_TOKEN_CREDENTIALS" in script
    assert "python -m app.ydb_schema" in script
    assert '"PERFORMANCE_METRICS_ENABLED"' in script
    assert '"PERFORMANCE_METRICS_SLOW_MS"' in script
    assert script.rindex("Invoke-YdbSchemaMigration") < script.index(
        "serverless function version create"
    )


def test_bash_deploy_applies_ydb_schema_before_function_version():
    script = (ROOT / "scripts" / "deploy-yc.sh").read_text(encoding="utf-8")

    assert "YDB_ACCESS_TOKEN_CREDENTIALS" in script
    assert "python -m app.ydb_schema" in script
    assert "PERFORMANCE_METRICS_ENABLED" in script
    assert "PERFORMANCE_METRICS_SLOW_MS" in script
    assert script.rindex("apply_ydb_schema") < script.index(
        "serverless function version create"
    )
