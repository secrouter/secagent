"""Tests for CUI marking (CMMC-6)."""

from __future__ import annotations

from pathlib import Path

import httpx

from secagent.agents.docs.agent import build_docs
from secagent.agents.review.agent import _apply_marking, review_merge_request
from secagent.config import Settings

from .conftest import make_chat_response, mock_client
from .test_review import ALIGN, gitlab_client

FIXTURE = Path(__file__).parent / "fixtures" / "sample_repo"


def test_apply_marking_wraps_body():
    assert _apply_marking("", "x") == "x"
    out = _apply_marking("CUI", "review text")
    assert out.startswith("**CUI**")
    assert out.rstrip().endswith("**CUI**")
    assert "review text" in out


def test_review_comment_is_marked():
    posted: list[dict] = []
    gl = gitlab_client(posted)
    llm = mock_client(lambda r: httpx.Response(200, json=make_chat_response(content="looks good")))
    s = Settings()
    s.persona.profile = str(ALIGN / "default.yaml")
    s.marking.banner = "CUI//SP-PRVCY"
    result = review_merge_request(s, project="42", mr_iid=7, post=True, gitlab=gl, llm=llm)
    assert result["review"].startswith("**CUI//SP-PRVCY**")
    assert posted[0]["body"].startswith("**CUI//SP-PRVCY**")


def test_docs_marking_in_conf(tmp_path):
    s = Settings()
    s.affordances.llm_summaries = False
    s.affordances.store_dir = str(tmp_path / "store")
    s.marking.banner = "CUI"
    out = tmp_path / "docs"
    report = build_docs(FIXTURE, out, s, run_sphinx=False)
    conf = Path(report["write"]["source_dir"], "conf.py").read_text()
    assert "announcement" in conf and "CUI" in conf
    assert "rst_epilog" in conf
