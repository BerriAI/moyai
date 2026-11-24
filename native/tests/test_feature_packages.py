"""Keep feature imports lazy and model/arithmetic modules independent."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "moyai"
FEATURES = ("reporting", "billing", "preferences", "automations", "continuations")


@pytest.mark.parametrize("feature", FEATURES)
def test_feature_initializers_do_not_load_other_modules(feature):
    tree = ast.parse((PACKAGE / feature / "__init__.py").read_text())
    assert all(isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
               and isinstance(node.value.value, str) for node in tree.body)


def test_report_arithmetic_is_database_and_runtime_independent():
    tree = ast.parse((PACKAGE / "reporting" / "analytics.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module.split(".")[0] in sys.stdlib_module_names
        elif isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)


@pytest.mark.parametrize("relative", [
    "billing/billing_storage_models.py",
    "preferences/settings_models.py",
    "preferences/settings_views.py",
    "automations/product_automation_models.py",
    "automations/product_automation_access_models.py",
    "automations/product_automation_context_models.py",
    "continuations/product_continuation_models.py",
])
def test_persistence_and_wire_models_do_not_import_command_modules(relative):
    tree = ast.parse((PACKAGE / relative).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.module == "models"
            else:
                assert node.module.split(".")[0] in {"sqlalchemy", "pydantic", *sys.stdlib_module_names}
        elif isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)


def test_existing_billing_storage_command_is_retained_without_database_access():
    result = subprocess.run(
        [sys.executable, "-m", "moyai.billing_storage", "--help"],
        text=True, capture_output=True, check=True, timeout=30,
    )
    assert "{prepare,backfill,activate,consolidate,rollback,status}" in result.stdout
    assert "--apply" in result.stdout
