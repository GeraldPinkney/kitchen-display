"""Specs for tools/push_dashboard.py.

The tool is split from its transport: functions take an async-free `call`
callable that performs one HA websocket command and returns its result dict,
so the payload shapes can be tested without a live HA.
"""
import pytest

from conftest import load_tool


class FakeHA:
    def __init__(self, dashboards=None, fail_on=None):
        self.dashboards = dashboards or [{"url_path": "map", "mode": "storage", "title": "Map"}]
        self.calls = []
        self.fail_on = fail_on

    def call(self, type_, **payload):
        self.calls.append((type_, payload))
        if self.fail_on and type_ == self.fail_on:
            return {"success": False, "error": {"code": "invalid_format", "message": "boom"}}
        if type_ == "lovelace/dashboards/list":
            return {"success": True, "result": self.dashboards}
        if type_ == "lovelace/dashboards/create":
            return {"success": True, "result": {
                "id": payload["url_path"], "url_path": payload["url_path"],
                "title": payload["title"], "mode": "storage",
            }}
        if type_ == "lovelace/config/save":
            return {"success": True, "result": None}
        raise AssertionError(f"unexpected call {type_}")


@pytest.fixture(scope="module")
def push():
    return load_tool("push_dashboard")


def test_creates_the_dashboard_when_missing(push):
    ha = FakeHA()
    result = push.ensure_dashboard(ha.call, ha.dashboards, "kitchen-display", "Kitchen", "mdi:silverware-fork-knife")
    assert result["url_path"] == "kitchen-display"
    creates = [c for c in ha.calls if c[0] == "lovelace/dashboards/create"]
    assert len(creates) == 1
    payload = creates[0][1]
    assert payload["url_path"] == "kitchen-display"
    assert payload["title"] == "Kitchen"
    assert payload["mode"] == "storage"
    assert payload["show_in_sidebar"] is True


def test_reuses_an_existing_dashboard(push):
    ha = FakeHA(dashboards=[{"url_path": "kitchen-display", "mode": "storage", "title": "Kitchen"}])
    result = push.ensure_dashboard(ha.call, ha.dashboards, "kitchen-display", "Kitchen", "mdi:silverware-fork-knife")
    assert result["url_path"] == "kitchen-display"
    assert not [c for c in ha.calls if c[0] == "lovelace/dashboards/create"]


def test_saves_the_config_for_the_dashboard(push):
    ha = FakeHA()
    config = {"title": "Kitchen", "views": [{"title": "Home", "path": "home", "cards": []}]}
    push.push_config(ha.call, "kitchen-display", config)
    saves = [c for c in ha.calls if c[0] == "lovelace/config/save"]
    assert len(saves) == 1
    assert saves[0][1]["url_path"] == "kitchen-display"
    assert saves[0][1]["config"] == config


def test_transport_failure_raises(push):
    ha = FakeHA(fail_on="lovelace/config/save")
    with pytest.raises(push.HAError) as excinfo:
        push.push_config(ha.call, "kitchen-display", {"views": []})
    assert "boom" in str(excinfo.value)


def test_dashboard_url_path_matches_the_kiosk_default(push):
    # HA requires a hyphen in url_path, and launch_kiosk.sh must agree with it.
    assert push.DASHBOARD_URL_PATH == "kitchen-display"
    assert "-" in push.DASHBOARD_URL_PATH and push.DASHBOARD_URL_PATH != "lovelace"
