import json
import shutil
from pathlib import Path

import pytest

from bbl_newsletter.__main__ import load_config, main

SCHEMA = Path(__file__).parents[1] / "schema.sql"
FIXTURE_REGISTRY = Path(__file__).parent / "fixtures" / "registry_small.yaml"


@pytest.fixture()
def config(tmp_path):
    shutil.copy(SCHEMA, tmp_path / "schema.sql")
    shutil.copy(FIXTURE_REGISTRY, tmp_path / "registry.yaml")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "registry: registry.yaml\n"
        "schema: schema.sql\n"
        "db: data/test.duckdb\n"
        "data_dir: data\n"
        "site_dir: site/bbl\n",
        encoding="utf-8",
    )
    return cfg


def run(capsys, *argv) -> dict:
    assert main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def test_init_creates_db_and_lists_sources(config, capsys):
    out = run(capsys, "--config", str(config), "init")
    assert out["sources"] == 4
    assert out["usable"] == ["hagen", "hessenschau"]
    assert (config.parent / "data" / "test.duckdb").exists()


def test_cluster_and_render_on_empty_db(config, capsys):
    out = run(capsys, "--config", str(config), "--date", "2026-10-12", "cluster")
    assert out["cluster"]["stories"] == 0
    out = run(capsys, "--config", str(config), "--date", "2026-10-12", "render")
    assert out["render"] is None  # no publishable summaries, no edition file


def test_generator_constrains_decoding_with_schema(monkeypatch):
    # the Ollama call must pass RESPONSE_SCHEMA as `format` so the model
    # cannot emit malformed JSON (2026-10-17: 99 parse-failure rejects)
    import httpx

    from bbl_newsletter.__main__ import make_generator
    from bbl_newsletter.summarize import RESPONSE_SCHEMA

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": '{"headline": "h", "summary": "s"}'}

    def fake_post(url, json=None, timeout=None):
        captured.update(json)
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)
    generate, model = make_generator({"ollama": {"model": "test-model:1"}})
    generate("prompt")
    assert captured["format"] == RESPONSE_SCHEMA
    assert captured["model"] == "test-model:1"


def test_config_missing_key_fails(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("registry: r.yaml\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing required key"):
        load_config(cfg)



def test_publish_failure_exits_nonzero(config, tmp_path, monkeypatch, capsys):
    """A failed publish must fail the command: the daily run's exit status
    and the Kuma heartbeat depend on it (website audit finding 2026-10-24)."""
    from datetime import date

    from bbl_newsletter import publish as publish_mod

    site = tmp_path / "site" / "bbl"
    site.mkdir(parents=True, exist_ok=True)
    (site / f"{date.today().isoformat()}.qmd").write_text(
        "---\ntitle: x\n---\n", encoding="utf-8"
    )
    repo = tmp_path / "website"
    repo.mkdir()
    with config.open("a", encoding="utf-8") as f:
        f.write(f"website_repo: {repo}\n")

    def boom(*args, **kwargs):
        raise publish_mod.PublishError("push rejected")

    monkeypatch.setattr(publish_mod, "publish_edition", boom)
    rc = main(["--config", str(config), "publish"])
    out = json.loads(capsys.readouterr().out)
    assert out["publish"].startswith("failed:")
    assert rc == 1


def test_link_requires_a_reference_package(config):
    with pytest.raises(SystemExit, match="--reference"):
        main(["--config", str(config), "link"])
