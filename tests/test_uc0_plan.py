"""UC0: the analysis `plan` affordance bins components by language with per-language tools."""

from __future__ import annotations

import json

from secagent.affordances import queries
from secagent.affordances.api import index_repo
from secagent.affordances.store import AffordanceStore
from secagent.config import Settings


def test_plan_bins_components_by_language(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "main.c").write_text("int main(void) { return 0; }\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "run.py").write_text("def run():\n    return 1\n")

    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / ".store")
    index_repo(tmp_path, s)

    store = AffordanceStore(tmp_path, s.affordances.store_dir)
    try:
        plan = json.loads(queries.plan(store))
    finally:
        store.close()

    assert "Python" in plan["languages"] and "C" in plan["languages"]
    assert "svc" in plan["bins"].get("C", [])
    assert "app" in plan["bins"].get("Python", [])
    # every binned language gets a tool list; C# / C/C++ get their specialized tools
    assert set(plan["per_language_tools"]) == set(plan["bins"])
    assert any("scan" in cmd for cmd in plan["per_language_tools"]["C"])
    assert any("docs build" in cmd for cmd in plan["always_run"])
