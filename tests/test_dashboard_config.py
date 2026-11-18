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


def test_repo_dashboard_has_panel_solar_iframe_view(config):
    solar_views = [v for v in config["views"] if v.get("path") == "solar"]
    assert len(solar_views) == 1, [v.get("path") for v in config["views"]]
    solar = solar_views[0]
    assert solar.get("title") == "Solar"
    assert solar.get("panel") is True
    cards = solar.get("cards", [])
    assert len(cards) == 1
    assert cards[0].get("type") == "iframe", "iframe is the built-in card; webpage is not a card type"
    assert cards[0].get("url") == "http://192.168.178.56:8788/solar.html"
    assert "aspect_ratio" in cards[0]


def test_repo_dashboard_has_panel_performance_iframe_view(config):
    perf_views = [v for v in config["views"] if v.get("path") == "performance"]
    assert len(perf_views) == 1, [v.get("path") for v in config["views"]]
    perf = perf_views[0]
    assert perf.get("title") == "Performance"
    assert perf.get("panel") is True
    cards = perf.get("cards", [])
    assert len(cards) == 1
    assert cards[0].get("type") == "iframe"
    assert cards[0].get("url") == "http://192.168.178.56:8788/performance.html"
    assert "aspect_ratio" in cards[0]


def test_energy_view_surfaces_east_and_west_forecast_planes(dc, config):
    energy_views = [v for v in config["views"] if v.get("path") == "energy"]
    assert len(energy_views) == 1
    eids = dc.collect_entity_ids(energy_views[0])
    required = {
        "sensor.energy_production_today_2",
        "sensor.energy_production_today_remaining_2",
        "sensor.energy_production_tomorrow_2",
        "sensor.power_production_now_2",
        "sensor.energy_production_today_3",
        "sensor.energy_production_today_remaining_3",
        "sensor.energy_production_tomorrow_3",
        "sensor.power_production_now_3",
    }
    missing = required - eids
    assert not missing, f"energy view missing EW plane forecast entities: {sorted(missing)}"


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


def test_unknown_card_type_is_rejected(dc, live_entity_ids):
    """`webpage` is not a card type: HA renders it as 'Configuration error' (#12)."""
    cfg = {
        "title": "t",
        "views": [{"title": "x", "path": "x", "cards": [{"type": "webpage", "url": "http://h/x"}]}],
    }
    errors = dc.validate(cfg, live_entity_ids)
    assert any("webpage" in e and "Configuration error" in e for e in errors), errors


def test_iframe_card_is_accepted(dc, live_entity_ids):
    cfg = {
        "title": "t",
        "views": [
            {
                "title": "x",
                "path": "x",
                "panel": True,
                "cards": [{"type": "iframe", "url": "http://h/x", "aspect_ratio": "100%"}],
            }
        ],
    }
    assert dc.validate(cfg, live_entity_ids) == []


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
