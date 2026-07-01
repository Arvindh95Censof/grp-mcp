"""Smoke tests — pure logic, no live Acumatica instance required.

Covers the bits most worth guarding against regression: config/gating model,
the write/delete/publish gates, the value-wrapper, and the modern UI-screen
error parser. Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import json

import pytest

from grp_mcp import server
from grp_mcp.config import Config, Instance
from grp_mcp.screen import ScreenClient


def _inst(**over) -> Instance:
    base = dict(
        base_url="https://host/Site",
        client_id="cid", client_secret="sek",
        username="u", password="p", tenant="Company",
    )
    base.update(over)
    return Instance(**base)


@pytest.fixture
def cfg(monkeypatch):
    """Inject a 2-instance config: 'ro' (all gates off) and 'rw' (all on)."""
    c = Config(
        default="ro",
        instances={
            "ro": _inst(),
            "rw": _inst(allow_write=True, allow_delete=True, allow_publish=True),
        },
    )
    monkeypatch.setattr(server, "_config", c)
    return c


# ---- Instance model / properties -------------------------------------------

def test_gate_defaults_are_read_only():
    i = _inst()
    assert i.allow_write is False
    assert i.allow_delete is False
    assert i.allow_publish is False


def test_origin_is_scheme_host_only():
    assert _inst(base_url="https://Host:8080/Site/x").origin == "https://host:8080"


def test_token_and_odata_urls():
    i = _inst(base_url="https://h/Site/")
    assert i.token_url == "https://h/Site/identity/connect/token"
    assert i.dac_odata_base == "https://h/Site/t/Company/api/odata/dac"


def test_config_get_unknown_raises():
    c = Config(default="a", instances={"a": _inst()})
    with pytest.raises(KeyError):
        c.get("nope")


# ---- write / delete / publish gates ----------------------------------------

def test_require_write_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_write("ro")
    server._require_write("rw")  # no raise


def test_require_delete_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_delete("ro")
    server._require_delete("rw")


def test_require_publish_blocks_read_only(cfg):
    with pytest.raises(PermissionError):
        server._require_publish("ro")
    server._require_publish("rw")


def test_delete_gate_is_independent_of_write(monkeypatch):
    # write ON, delete OFF -> deletes must still be blocked
    c = Config(default="d", instances={"d": _inst(allow_write=True, allow_delete=False)})
    monkeypatch.setattr(server, "_config", c)
    server._require_write("d")  # ok
    with pytest.raises(PermissionError):
        server._require_delete("d")


def test_destructive_ui_actions_include_delete():
    assert "Delete" in server._DESTRUCTIVE_UI_ACTIONS


# ---- value wrapper ----------------------------------------------------------

def test_wrap_scalar():
    assert server._wrap("x") == {"value": "x"}


def test_wrap_passthrough_wrapped_scalar():
    assert server._wrap({"value": 5}) == {"value": 5}


def test_wrap_nested_and_id_delete_passthrough():
    out = server._wrap({"id": "R1", "delete": True, "Name": "Acme",
                        "Addr": {"City": "KL"}})
    assert out["id"] == "R1"            # not wrapped
    assert out["delete"] is True        # bare bool, not {"value": True}
    assert out["Name"] == {"value": "Acme"}
    assert out["Addr"] == {"City": {"value": "KL"}}


def test_wrap_list_of_rows():
    assert server._wrap([{"A": 1}]) == [{"A": {"value": 1}}]


# ---- modern UI-screen error parser -----------------------------------------

class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body) if isinstance(body, (dict, list)) else str(body)

    def json(self):
        if isinstance(self._body, (dict, list)):
            return self._body
        raise ValueError("not json")


def test_ui_error_success_empty_messages():
    assert ScreenClient._ui_error(_Resp(200, {"messages": []})) is None


def test_ui_error_200_explicit_error_message():
    r = _Resp(200, {"messages": [{"message": "boom", "messageType": "error"}]})
    assert ScreenClient._ui_error(r) == "boom"


def test_ui_error_200_info_message_is_not_error():
    r = _Resp(200, {"messages": [{"message": "fyi", "messageType": "info"}]})
    assert ScreenClient._ui_error(r) is None


def test_ui_error_200_untyped_message_is_not_error():
    r = _Resp(200, {"messages": [{"message": "no type"}]})
    assert ScreenClient._ui_error(r) is None


def test_ui_error_409_surfaces_all_messages():
    r = _Resp(409, {"messages": [{"message": "bad year", "messageType": "error"}]})
    assert ScreenClient._ui_error(r) == "bad year"


def test_ui_error_setup_not_entered():
    msg = ScreenClient._ui_error(_Resp(409, {"type": "SetupNotEntered"}))
    assert "PREREQUISITE NOT MET" in msg


def test_ui_error_500_title_detail():
    assert ScreenClient._ui_error(_Resp(500, {"title": "x", "detail": "y"})) == "Error: y"
