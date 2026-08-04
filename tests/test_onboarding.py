"""Tests for `secagent init` (onboarding.py): URL derivation, and the models.json /
~/.secagent/config.yaml writers (merge, --force backup, idempotency, KIMI guard)."""

from __future__ import annotations

import json

import pytest
import yaml

from secagent.onboarding import (
    OnboardingError,
    SuitePeers,
    derive_peers,
    resolve_peers,
    run_init,
)

# ── derive_peers / resolve_peers ────────────────────────────────────────────────────


def test_derive_peers_matches_suite_convention():
    peers = derive_peers("example.internal")
    assert peers.secrouter_url == "https://secrouter.example.internal:47002/v1"
    assert peers.device_authorization_url == \
        "https://secsso.example.internal:9000/application/o/device/"
    assert peers.token_url == "https://secsso.example.internal:9000/application/o/token/"


def test_resolve_peers_domain_only():
    peers = resolve_peers(domain="sec.internal", secrouter_url=None, secsso_url=None)
    assert peers == derive_peers("sec.internal")


def test_resolve_peers_explicit_urls_without_domain():
    peers = resolve_peers(
        domain=None,
        secrouter_url="https://custom-secrouter.example.com:9999/v1",
        secsso_url="https://custom-secsso.example.com:8888",
    )
    assert peers.secrouter_url == "https://custom-secrouter.example.com:9999/v1"
    assert peers.device_authorization_url == \
        "https://custom-secsso.example.com:8888/application/o/device/"
    assert peers.token_url == "https://custom-secsso.example.com:8888/application/o/token/"


def test_resolve_peers_secsso_base_trailing_slash_stripped():
    peers = resolve_peers(
        domain=None,
        secrouter_url="https://secrouter.example.com:47002/v1",
        secsso_url="https://secsso.example.com:9000/",
    )
    assert peers.token_url == "https://secsso.example.com:9000/application/o/token/"


def test_resolve_peers_partial_override_keeps_domain_for_the_rest():
    # --domain plus ONLY --secrouter-url: secsso stays domain-derived.
    peers = resolve_peers(
        domain="sec.internal", secrouter_url="https://custom:1/v1", secsso_url=None,
    )
    assert peers.secrouter_url == "https://custom:1/v1"
    assert peers.device_authorization_url == derive_peers("sec.internal").device_authorization_url
    assert peers.token_url == derive_peers("sec.internal").token_url


def test_resolve_peers_nothing_given_raises():
    with pytest.raises(OnboardingError, match="--domain"):
        resolve_peers(domain=None, secrouter_url=None, secsso_url=None)


def test_resolve_peers_only_secrouter_url_raises():
    with pytest.raises(OnboardingError, match="--domain"):
        resolve_peers(domain=None, secrouter_url="https://x:1/v1", secsso_url=None)


def test_resolve_peers_only_secsso_url_raises():
    with pytest.raises(OnboardingError, match="--domain"):
        resolve_peers(domain=None, secrouter_url=None, secsso_url="https://x:1")


# ── run_init: fresh install (no existing files) ─────────────────────────────────────


def _paths(tmp_path):
    return tmp_path / "pi" / "agent" / "models.json", tmp_path / "secagent" / "config.yaml"


def test_run_init_fresh_models_json_only_secrouter_provider(tmp_path):
    models_path, config_path = _paths(tmp_path)
    result = run_init(
        domain="example.internal", models_json_path=models_path, config_path=config_path,
    )

    data = json.loads(models_path.read_text())
    assert set(data["providers"]) == {"secrouter"}
    provider = data["providers"]["secrouter"]
    assert provider["baseUrl"] == "https://secrouter.example.internal:47002/v1"
    assert provider["apiKey"] == "!secagent token --user"
    assert provider["models"] == [{"id": "balanced", "name": "balanced (SecRouter)"}]
    # No literal secret/token value anywhere in the generated file.
    assert "Bearer" not in models_path.read_text()
    assert result.models_json_path == models_path


def test_run_init_fresh_models_json_has_no_kimi_or_prc_model(tmp_path):
    models_path, config_path = _paths(tmp_path)
    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)
    text = models_path.read_text().lower()
    # "kimi" appears ONLY inside the guard comment, never as an enabled provider/model.
    assert "kimi for coding" in text  # the guard comment itself
    data = json.loads(models_path.read_text())
    assert "kimi" not in json.dumps(data["providers"]).lower()
    for bad in ("moonshot", "qwen", "deepseek", "glm"):
        assert bad not in text


def test_run_init_fresh_user_config_yaml(tmp_path):
    models_path, config_path = _paths(tmp_path)
    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)

    doc = yaml.safe_load(config_path.read_text())
    assert doc["llm"]["base_url"] == "https://secrouter.example.internal:47002/v1"
    assert doc["llm"]["api_key"] == "!secagent token --user"
    assert doc["llm"]["model"] == "balanced"
    assert doc["secsso"]["device_authorization_url"] == \
        "https://secsso.example.internal:9000/application/o/device/"
    assert doc["secsso"]["token_url"] == "https://secsso.example.internal:9000/application/o/token/"
    assert doc["secsso"]["device_client_id"] == "secagent-pi"
    assert "secrouter" in doc["secsso"]["device_scope"]
    assert "Bearer" not in config_path.read_text()


def test_run_init_custom_model(tmp_path):
    models_path, config_path = _paths(tmp_path)
    run_init(
        domain="example.internal", model="quality",
        models_json_path=models_path, config_path=config_path,
    )
    data = json.loads(models_path.read_text())
    assert data["providers"]["secrouter"]["models"][0]["id"] == "quality"
    doc = yaml.safe_load(config_path.read_text())
    assert doc["llm"]["model"] == "quality"


def test_run_init_creates_parent_directories(tmp_path):
    models_path = tmp_path / "deep" / "nested" / "pi" / "models.json"
    config_path = tmp_path / "other" / "nested" / "secagent" / "config.yaml"
    run_init(domain="x.example", models_json_path=models_path, config_path=config_path)
    assert models_path.exists()
    assert config_path.exists()


def test_run_init_explicit_urls_no_domain(tmp_path):
    models_path, config_path = _paths(tmp_path)
    result = run_init(
        secrouter_url="https://secrouter.custom.com:47002/v1",
        secsso_url="https://secsso.custom.com:9000",
        models_json_path=models_path, config_path=config_path,
    )
    assert result.peers.secrouter_url == "https://secrouter.custom.com:47002/v1"
    data = json.loads(models_path.read_text())
    assert data["providers"]["secrouter"]["baseUrl"] == "https://secrouter.custom.com:47002/v1"


def test_run_init_rejects_excluded_model(tmp_path):
    models_path, config_path = _paths(tmp_path)
    with pytest.raises(OnboardingError, match="kimi"):
        run_init(
            domain="example.internal", model="kimi-k2-instruct",
            models_json_path=models_path, config_path=config_path,
        )
    assert not models_path.exists()
    assert not config_path.exists()


@pytest.mark.parametrize("bad_model", ["moonshot-v1", "qwen2.5-72b", "deepseek-v3", "chatglm-6b"])
def test_run_init_rejects_other_prc_jurisdiction_families(tmp_path, bad_model):
    models_path, config_path = _paths(tmp_path)
    with pytest.raises(OnboardingError):
        run_init(
            domain="example.internal", model=bad_model,
            models_json_path=models_path, config_path=config_path,
        )


def test_run_init_no_domain_or_urls_raises(tmp_path):
    models_path, config_path = _paths(tmp_path)
    with pytest.raises(OnboardingError, match="--domain"):
        run_init(models_json_path=models_path, config_path=config_path)


# ── run_init: idempotency + merge behavior ──────────────────────────────────────────


def test_run_init_rerun_is_idempotent(tmp_path):
    models_path, config_path = _paths(tmp_path)
    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)
    first = models_path.read_text()
    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)
    second = models_path.read_text()
    assert first == second
    data = json.loads(second)
    assert list(data["providers"]) == ["secrouter"]  # never duplicated


def test_run_init_rerun_with_new_domain_updates_urls(tmp_path):
    models_path, config_path = _paths(tmp_path)
    run_init(domain="old.internal", models_json_path=models_path, config_path=config_path)
    run_init(domain="new.internal", models_json_path=models_path, config_path=config_path)
    data = json.loads(models_path.read_text())
    assert data["providers"]["secrouter"]["baseUrl"] == "https://secrouter.new.internal:47002/v1"
    doc = yaml.safe_load(config_path.read_text())
    assert doc["llm"]["base_url"] == "https://secrouter.new.internal:47002/v1"


def test_run_init_preserves_other_models_json_providers(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    models_path.write_text(json.dumps({
        "providers": {
            "local-gemma": {"baseUrl": "http://localhost:8000/v1", "apiKey": "not-needed",
                            "models": [{"id": "gemma-3-12b-it"}]},
        }
    }))

    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)

    data = json.loads(models_path.read_text())
    assert set(data["providers"]) == {"local-gemma", "secrouter"}
    assert data["providers"]["local-gemma"]["baseUrl"] == "http://localhost:8000/v1"
    assert data["providers"]["secrouter"]["baseUrl"] == "https://secrouter.example.internal:47002/v1"


def test_run_init_replaces_only_the_secrouter_provider_on_rerun(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    # A hand-edited secrouter entry with an extra field should be REPLACED wholesale
    # (init owns this key entirely), not deep-merged field by field.
    models_path.write_text(json.dumps({
        "providers": {"secrouter": {"baseUrl": "https://old:1/v1", "weirdField": True}},
    }))

    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)

    provider = json.loads(models_path.read_text())["providers"]["secrouter"]
    assert provider["baseUrl"] == "https://secrouter.example.internal:47002/v1"
    assert "weirdField" not in provider


def test_run_init_preserves_other_config_yaml_sections(tmp_path):
    models_path, config_path = _paths(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(yaml.safe_dump({"scan": {"max_files": 5}}))

    run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)

    doc = yaml.safe_load(config_path.read_text())
    assert doc["scan"] == {"max_files": 5}
    assert doc["llm"]["base_url"] == "https://secrouter.example.internal:47002/v1"


def test_run_init_no_backup_without_force(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    models_path.write_text(json.dumps({"providers": {}}))
    result = run_init(
        domain="example.internal", models_json_path=models_path, config_path=config_path,
    )
    assert result.models_json_backup is None
    assert result.config_backup is None
    backups = list(models_path.parent.glob("*.bak-*"))
    assert backups == []


def test_run_init_force_backs_up_existing_files(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    models_path.write_text(json.dumps({"providers": {"secrouter": {"baseUrl": "https://old:1/v1"}}}))
    config_path.write_text(yaml.safe_dump({"llm": {"base_url": "https://old:1/v1"}}))

    result = run_init(
        domain="example.internal", force=True,
        models_json_path=models_path, config_path=config_path,
    )

    assert result.models_json_backup is not None
    assert result.models_json_backup.exists()
    assert "old:1" in result.models_json_backup.read_text()
    assert result.config_backup is not None
    assert result.config_backup.exists()
    # And the real files reflect the new domain.
    assert "secrouter.example.internal" in models_path.read_text()


def test_run_init_corrupt_existing_file_without_force_raises(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    models_path.write_text("{ not valid json !!")

    with pytest.raises(OnboardingError, match="--force"):
        run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)
    # Must not have been silently clobbered.
    assert models_path.read_text() == "{ not valid json !!"


def test_run_init_corrupt_existing_file_with_force_recovers(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    models_path.write_text("{ not valid json !!")

    result = run_init(
        domain="example.internal", force=True,
        models_json_path=models_path, config_path=config_path,
    )
    assert result.models_json_backup is not None
    assert result.models_json_backup.read_text() == "{ not valid json !!"
    data = json.loads(models_path.read_text())
    assert list(data["providers"]) == ["secrouter"]


def test_run_init_corrupt_config_yaml_without_force_raises(tmp_path):
    models_path, config_path = _paths(tmp_path)
    config_path.parent.mkdir(parents=True)
    config_path.write_text("llm: [unterminated")

    with pytest.raises(OnboardingError, match="--force"):
        run_init(domain="example.internal", models_json_path=models_path, config_path=config_path)


# ── InitResult.summary_lines ─────────────────────────────────────────────────────────


def test_summary_lines_mention_next_step_and_paths(tmp_path):
    models_path, config_path = _paths(tmp_path)
    result = run_init(
        domain="example.internal", models_json_path=models_path, config_path=config_path,
    )
    text = "\n".join(result.summary_lines())
    assert "secagent login" in text
    assert str(models_path) in text
    assert str(config_path) in text


def test_summary_lines_mention_backups_when_forced(tmp_path):
    models_path, config_path = _paths(tmp_path)
    models_path.parent.mkdir(parents=True)
    models_path.write_text(json.dumps({"providers": {}}))
    result = run_init(
        domain="example.internal", force=True,
        models_json_path=models_path, config_path=config_path,
    )
    text = "\n".join(result.summary_lines())
    assert "Backed up" in text


def test_suite_peers_is_frozen():
    peers = SuitePeers("a", "b", "c")
    with pytest.raises(AttributeError):
        peers.secrouter_url = "changed"  # type: ignore[misc]
