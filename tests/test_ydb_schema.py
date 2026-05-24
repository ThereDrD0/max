from __future__ import annotations

import ast
import re
from pathlib import Path

from app.ydb_schema import SCHEMA_STATEMENTS


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
STORAGE_TABLE_RE = re.compile(
    r"\b(?:FROM|INTO|UPDATE|JOIN|DELETE\s+FROM)\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


def test_ydb_storage_tables_are_declared_in_schema():
    schema_tables = {
        match.group(1)
        for statement in SCHEMA_STATEMENTS
        for match in SCHEMA_TABLE_RE.finditer(statement)
    }
    storage_source = (ROOT / "app" / "storage" / "ydb.py").read_text(encoding="utf-8")
    storage_yql_parts = [
        node.value
        for node in ast.walk(ast.parse(storage_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    storage_tables = {
        match.group(1)
        for statement in storage_yql_parts
        for match in STORAGE_TABLE_RE.finditer(statement)
        if match.group(1).lower() != "select"
    }

    assert storage_tables <= schema_tables, (
        "YdbStorage обращается к таблицам, которых нет в app.ydb_schema: "
        f"{sorted(storage_tables - schema_tables)}"
    )
