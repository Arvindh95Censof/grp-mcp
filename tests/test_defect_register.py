"""Pins for the 2026-07-20 MPM-build defect register.

Each test pins one fix: silent failures that reported success-shaped results,
bare errors with no recovery routing, and a canonical_order proven wrong live
on a blank tenant.
"""
from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

from grp_mcp import server
from grp_mcp.acumatica import AcumaticaError
from grp_mcp.config import Config, Instance
from grp_mcp.screen import ScreenClient, ScreenError


def _inst(**kw) -> Instance:
    return Instance(
        base_url="https://erp.example.com/x", client_id="c", client_secret="s",
        username="u", password="p", **kw)


@pytest.fixture
def cfg(monkeypatch):
    c = Config(default="ro", instances={
        "ro": _inst(),
        "rw": _inst(allow_write=True, allow_delete=True, allow_publish=True),
    })
    monkeypatch.setattr(server, "_config", c)
    return c


# ---- defect 1: activate_features swallowed the server-side NRE --------------

def test_is_transport_drop_classifies():
    for m in ("ReadTimeout: timed out", "Connection reset by peer",
              "Server disconnected without response", "unexpected EOF"):
        assert server._is_transport_drop(m), m
    for m in ("ui_command requestValidation on CS100000: An error occurred during "
              "processing of the field ProjectAccounting: Object reference not set "
              "to an instance of an object.",
              "PXException: something invalid"):
        assert not server._is_transport_drop(m), m


class _NREScreenClient:
    """activate_features client whose Enable command NREs server-side."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def ui_bootstrap(self, views=None):
        pass

    async def ui_command(self, name, answer=None):
        raise ScreenError(
            "ui_command requestValidation on CS100000: An error occurred during "
            "processing of the field ProjectAccounting: Object reference not set "
            "to an instance of an object.")


def test_activate_features_hard_error_is_failed_not_in_progress(cfg, monkeypatch):
    monkeypatch.setattr(server, "ScreenClient", _NREScreenClient)

    async def _stuck(inst, poll, wait):
        return "Pending Activation"

    monkeypatch.setattr(server, "_activation_status", _stuck)
    out = asyncio.run(server.activate_features(wait_seconds=5, instance="rw"))
    # the old code returned "in_progress" here — an infinite poll on work that
    # never started (register defect 1, swallowed NRE)
    assert out["status"] == "failed"
    assert out["activated"] is False
    # the full message survives — the old [:160] cut off the "instance of an
    # object" tail that identifies the null
    assert "instance of an object" in out["error"]
    assert "REJECTED" in out["note"]


def test_activate_features_transport_drop_still_polls(cfg, monkeypatch):
    class _DropClient(_NREScreenClient):
        async def ui_command(self, name, answer=None):
            raise ScreenError("ReadTimeout: connection reset by peer mid-recompile")

    monkeypatch.setattr(server, "ScreenClient", _DropClient)

    async def _stuck(inst, poll, wait):
        return "Pending Activation"

    monkeypatch.setattr(server, "_activation_status", _stuck)
    out = asyncio.run(server.activate_features(wait_seconds=5, instance="rw"))
    assert out["status"] == "in_progress"     # a drop IS benign — keep polling


# ---- defect 2: create_or_update_entity silently shipped unknown fields ------

class _SchemaClient:
    """create_or_update_entity client with a known Ledger schema."""

    _META_FIELDS = ("id", "rowNumber", "note", "_links", "custom", "files")

    def __init__(self):
        self.put_calls = []

    async def _merged_props(self, entity, refresh=False):
        return {"LedgerID": {}, "Type": {}, "Description": {}, "Currency": {}}

    async def put_entity(self, entity, fields):
        self.put_calls.append((entity, fields))
        return {"id": "x", "LedgerID": {"value": "STAT"}}


def test_create_or_update_entity_rejects_unknown_field(cfg, monkeypatch):
    client = _SchemaClient()
    monkeypatch.setattr(server, "_client", lambda inst, ep=None: client)
    with pytest.raises(ValueError) as ei:
        asyncio.run(server.create_or_update_entity(
            "Ledger", {"LedgerID": "STAT", "BalanceType": "Statistical"},
            instance="rw"))
    msg = str(ei.value)
    # names the offender, suggests the real field, and never sent the PUT
    assert "BalanceType" in msg
    assert "Type" in msg
    assert client.put_calls == []


def test_create_or_update_entity_meta_fields_pass(cfg, monkeypatch):
    client = _SchemaClient()
    monkeypatch.setattr(server, "_client", lambda inst, ep=None: client)
    out = asyncio.run(server.create_or_update_entity(
        "Ledger", {"LedgerID": "STAT", "Type": "Statistical", "note": "n"},
        instance="rw"))
    assert client.put_calls and out["id"] == "x"


def test_create_or_update_entity_fails_open_without_schema(cfg, monkeypatch):
    class _NoSchema(_SchemaClient):
        async def _merged_props(self, entity, refresh=False):
            raise AcumaticaError("swagger unavailable")

    client = _NoSchema()
    monkeypatch.setattr(server, "_client", lambda inst, ep=None: client)
    out = asyncio.run(server.create_or_update_entity(
        "Ledger", {"Whatever": "x"}, instance="rw"))
    assert client.put_calls          # schema missing -> PUT proceeds as before
    assert out["id"] == "x"


# ---- defect 2b: create_financial_calendar never sent the period start -------

class _CalendarClient:
    """Captures the command stream create_financial_calendar submits."""

    last_cmds: list | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def submit(self, cmds, auto_answer=None, skip_validation=False):
        _CalendarClient.last_cmds = cmds
        return {"ok": True}


def test_create_financial_calendar_sends_first_period_start_date(cfg, monkeypatch):
    monkeypatch.setattr(server, "ScreenClient", _CalendarClient)
    asyncio.run(server.create_financial_calendar("2026", instance="rw"))
    cmds = _CalendarClient.last_cmds
    want = {"set": "FirstPeriodStartDate", "to": "1/1/2026"}
    # required on a blank tenant; AutoFill does NOT derive it — and it must be
    # the classic plane's FRIENDLY name, not the DAC name PeriodsStartDate
    assert want in cmds
    assert cmds.index(want) < cmds.index({"action": "AutoFill"})
    assert not any(c.get("set") == "PeriodsStartDate" for c in cmds)


def test_create_financial_calendar_explicit_periods_start_date(cfg, monkeypatch):
    monkeypatch.setattr(server, "ScreenClient", _CalendarClient)
    asyncio.run(server.create_financial_calendar(
        "2026", starts_on="7/1/2026", periods_start_date="7/15/2026",
        instance="rw"))
    assert {"set": "FirstPeriodStartDate", "to": "7/15/2026"} \
        in _CalendarClient.last_cmds


# ---- defect 2c: canonical_order wrong for a blank tenant --------------------

def test_setup_map_gl_order_matches_blank_tenant_reality():
    m = server._setup_map()
    co = m["canonical_order"]
    gl = [s for s in co if s.startswith("GL")]
    # proven live: GL201000 Generate fails with SetupNotEntered until GLSetup
    # exists, and GL102000 cannot save without the CoA's RetEarn/YTD accounts —
    # so ledger, classes, CoA and prefs all precede the calendar generation
    assert gl == ["GL101000", "GL201500", "GL202000", "GL202500",
                  "GL102000", "GL201000", "GL503000"]
    # the per-screen order ints must agree with canonical_order (both are
    # published surfaces; they disagreed before this fix)
    orders = [m["screens"][s]["order"] for s in co if s in m["screens"]]
    assert orders == sorted(orders)
    # the prerequisite that forced the reorder is recorded on the screen itself
    assert any("GLSetup" in p for p in m["screens"]["GL201000"]["prerequisites"])


# ---- defects 5/6: run_dac_odata failure routing -----------------------------

class _DacHintClient:
    def __init__(self, names, csdl=""):
        self._names = names
        self._csdl = csdl

    async def list_dacs(self):
        return {"value": [{"name": n, "url": n} for n in self._names]}

    async def dac_metadata(self, refresh=False):
        return self._csdl


def test_dac_failure_hint_close_matches():
    client = _DacHintClient(["Numbering", "Account", "FinPeriod"])
    hint = asyncio.run(server._dac_failure_hint(client, "Numberin", "404"))
    assert "Numbering" in hint


def test_dac_failure_hint_entity_type_without_entityset():
    csdl = '<Schema><EntityType Name="SYData"><Property/></EntityType></Schema>'
    client = _DacHintClient(["Account"], csdl)
    hint = asyncio.run(server._dac_failure_hint(client, "SYData", "404 Not Found"))
    assert "NO EntitySet" in hint
    assert "get_dac_metadata" in hint


def test_dac_failure_hint_wrong_dac_property_error():
    client = _DacHintClient(["Numbering"])
    err = ("GET .../NumberingSequence -> 400: Could not find a property named "
           "'StartNbr' on type 'PX.Objects.CS.Numbering'")
    hint = asyncio.run(server._dac_failure_hint(client, "NumberingSequence", err))
    assert "WRONG DAC" in hint
    assert "ui_read_grid" in hint


def test_dac_failure_hint_never_masks_original_error():
    class _Broken:
        async def list_dacs(self):
            raise RuntimeError("boom")

    hint = asyncio.run(server._dac_failure_hint(_Broken(), "X", "404"))
    assert hint is None


# ---- defect 7: no-classic-page routing --------------------------------------

def test_aspx_page_missing_matcher():
    assert server._aspx_page_missing(ScreenError(
        "aspx GET returned no __RequestVerificationToken — not a classic "
        "WebForms page (modern-only screens have no ASPX plane) or not "
        "authenticated"))
    assert server._aspx_page_missing(ScreenError(
        "aspx: page has no control config declarations — not a classic "
        "WebForms page?"))
    assert not server._aspx_page_missing(ScreenError("record did not load"))


def test_no_aspx_page_result_routes_to_modern_tools():
    out = server._no_aspx_page_result(
        "CS201010", "http://x/pages/cs/cs201010.aspx",
        ScreenError("no __RequestVerificationToken"))
    assert out["ok"] is False and out["no_classic_page"] is True
    for tool in ("ui_screen_action", "ui_read_grid"):
        assert tool in out["recommend"]


# ---- defect 8: _find_field errors now route to screen_get_schema ------------

def _client_with_tree(xml: str) -> ScreenClient:
    c = object.__new__(ScreenClient)
    c.screen_id = "SM203520"
    c._tree = ET.fromstring(xml)
    return c


_SCHEMA_XML = """<root>
  <CompanySummary><CompanyID/><CompanyName/></CompanySummary>
  <Details><LoginName/></Details>
</root>"""


def test_find_field_bad_container_lists_available_and_names_schema_tool():
    c = _client_with_tree(_SCHEMA_XML)
    with pytest.raises(ScreenError) as ei:
        c._find_field("Companies.CompanyID")
    msg = str(ei.value)
    assert "'Companies'" in msg and "CompanySummary" in msg
    assert "screen_get_schema" in msg


def test_find_field_bad_field_in_good_container_lists_fields():
    c = _client_with_tree(_SCHEMA_XML)
    with pytest.raises(ScreenError) as ei:
        c._find_field("CompanySummary.Nope")
    msg = str(ei.value)
    assert "container exists" in msg and "CompanyID" in msg


def test_find_field_unqualified_not_found_lists_containers():
    c = _client_with_tree(_SCHEMA_XML)
    with pytest.raises(ScreenError) as ei:
        c._find_field("Nope")
    msg = str(ei.value)
    assert "CompanySummary" in msg and "screen_get_schema" in msg
