"""Specs for ha/kitchen_dashboard.yaml and tools/dashboard_check.py.

The dashboard must validate clean against the captured live HA entity set:
every entity reference exists, and no card type needs HACS.
"""
import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def dc():
    return load_tool("dashboard_check")


@pytest.fixture(scope="module")
def config(dc, repo_root):
    path = repo_root / "ha" / "kitchen_dashboard.yaml"
    if not path.is_file():
        pytest.fail("missing ha/kitchen_dashboard.yaml")
    return dc.load_config(path)


def test_repo_dashboard_validates_clean_against_live_capture(dc, config, live_entity_ids):
    errors = dc.validate(config, live_entity_ids)
    assert errors == [], "dashboard errors:\n" + "\n".join(errors)


def test_repo_dashboard_is_titled_kitchen(config):
    assert config.get("title") == "Kitchen"


def test_repo_dashboard_has_home_and_energy_views(config):
    paths = [v.get("path") for v in config["views"]]
    assert "home" in paths and "energy" in paths


def test_unknown_entity_is_reported(dc, live_entity_ids):
    cfg = {
        "title": "t",
        "views": [
            {
                "title": "x",
                "path": "x",
                "cards": [{"type": "entities", "entities": ["sensor.definitely_not_real"]}],
            }
        ],
    }
    errors = dc.validate(cfg, live_entity_ids)
    assert any("sensor.definitely_not_real" in e for e in errors), errors


def test_hacs_card_is_rejected(dc, live_entity_ids):
    cfg = {
        "title": "t",
        "views": [{"title": "x", "path": "x", "cards": [{"type": "custom:button-card"}]}],
    }
    errors = dc.validate(cfg, live_entity_ids)
    assert any("custom:button-card" in e for e in errors), errors


def test_entities_in_nested_cards_are_collected(dc, live_entity_ids):
    card = {
        "type": "grid",
        "cards": [
            {"type": "vertical-stack", "cards": [{"type": "gauge", "entity": "sensor.a_b"}]},
            {"type": "entities", "entities": [{"entity": "sensor.c_d"}, "sensor.e_f"]},
        ],
    }
    assert dc.collect_entity_ids(card) == {"sensor.a_b", "sensor.c_d", "sensor.e_f"}


def test_nested_unknown_entity_is_reported(dc):
    cfg = {
        "title": "t",
        "views": [
            {
                "title": "x",
                "path": "x",
                "cards": [
                    {"type": "vertical-stack", "cards": [{"type": "gauge", "entity": "sensor.hidden"}]}
                ],
            }
        ],
    }
    errors = dc.validate(cfg, {"sensor.known"})
    assert any("sensor.hidden" in e for e in errors), errors


def test_duplicate_view_paths_are_rejected(dc):
    cfg = {
        "title": "t",
        "views": [
            {"title": "a", "path": "dup", "cards": []},
            {"title": "b", "path": "dup", "cards": []},
        ],
    }
    errors = dc.validate(cfg, set())
    assert any("dup" in e and "duplicate" in e.lower() for e in errors), errors


def test_view_path_must_be_url_safe(dc):
    cfg = {"title": "t", "views": [{"title": "a", "path": "Not Safe!", "cards": []}]}
    errors = dc.validate(cfg, set())
    assert any("Not Safe!" in e for e in errors), errors


def test_missing_card_type_is_reported(dc):
    cfg = {"title": "t", "views": [{"title": "a", "path": "a", "cards": [{"entity": "sensor.known"}]}]}
    errors = dc.validate(cfg, {"sensor.known"})
    assert any("type" in e.lower() for e in errors), errors
