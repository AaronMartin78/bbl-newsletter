from pathlib import Path

import pytest

from bbl_newsletter.registry import Registry

FIXTURE = Path(__file__).parent / "fixtures" / "registry_small.yaml"


def test_load_and_partition():
    reg = Registry.load(FIXTURE)
    assert reg.version == 1
    assert {s.id for s in reg.usable()} == {"hagen", "hessenschau"}
    assert {s.id for s in reg.pending()} == {"mdr"}
    assert reg.by_id("stuttgarter_zeitung").status == "disabled"


def test_entry_point_by_method():
    reg = Registry.load(FIXTURE)
    assert reg.by_id("hagen").entry_point == "https://phoenix-hagen.de/feed/"
    assert reg.by_id("mdr").entry_point.endswith("~news.xml")


def test_duplicate_ids_rejected(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    dupe = text + text[text.index("  - id: hagen"):text.index("  - id: hessenschau")]
    p = tmp_path / "dupe.yaml"
    p.write_text(dupe, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate source ids"):
        Registry.load(p)
