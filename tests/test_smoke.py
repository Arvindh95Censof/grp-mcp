"""Smoke tests — pure logic, no live Acumatica instance required.

Covers the bits most worth guarding against regression: config/gating model,
the write/delete/publish gates, the value-wrapper, and the modern UI-screen
error parser. Run with:  python -m pytest tests/ -q
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grp_mcp import server
from grp_mcp.acumatica import AcumaticaClient, AcumaticaError
from grp_mcp.config import Config, Instance
from grp_mcp.customization import CustomizationClient
from grp_mcp.screen import ScreenClient, ScreenError


def _inst(**over) -> Instance:
    base = dict(
        base_url="https://host/Site",
        client_id="cid", client_secret="sek",
        username="u", password="p", tenant="Company",
    )
    base.update(over)
    return Instance(**base)


@pytest.fixture(autouse=True)
def _clear_ui_session_cache():
    """The shared UI-plane cookie + /structure caches are module-global; isolate tests."""
    from grp_mcp import screen as _screen
    _screen._SESSION_CACHE.clear()
    _screen._STRUCT_CACHE.clear()
    yield
    _screen._SESSION_CACHE.clear()
    _screen._STRUCT_CACHE.clear()


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


def test_destructive_actions_include_delete():
    assert "Delete" in server._DESTRUCTIVE_ACTIONS
    assert "DeleteRow" in server._DESTRUCTIVE_ACTIONS


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
    def __init__(self, status: int, body, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
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


def test_ui_error_structure_duplicate_key_server_bug():
    # Proven live on EP203000: Acumatica's OWN /structure metadata-builder throws
    # an unhandled .NET Dictionary duplicate-key exception and returns a bare 500
    # with no further detail. Must be labeled as a SERVER-side bug (not a caller
    # mistake or a grp-mcp parsing issue) and point at the proven-working fallback.
    r = _Resp(500, {"title": "An item with the same key has already been added.",
                    "status": 500, "traceId": "00-abc"})
    msg = ScreenClient._ui_error(r)
    assert "SERVER-SIDE BUG" in msg
    assert "screen_get_schema" in msg


def test_ui_error_duplicate_key_message_only_matches_when_failed():
    # The same text on a 200 (not that this happens in practice) must not
    # falsely trip the server-bug branch — only >=400 responses are failures.
    r = _Resp(200, {"title": "An item with the same key has already been added."})
    assert ScreenClient._ui_error(r) is None


def test_ui_error_login_redirect_200_is_auth_error():
    # an unauthenticated/expired modern-plane session answers 200 with a Login
    # redirect body — must surface as a clear auth error, not silent None.
    r = _Resp(200, {"redirect": "/2026R1/Frames/Login.aspx?ReturnUrl=%2f..."})
    msg = ScreenClient._ui_error(r)
    assert msg is not None and "NOT AUTHENTICATED" in msg


def test_ui_error_login_redirect_302_is_auth_error():
    r = _Resp(302, {"redirect": "/2026R1/Frames/Login.aspx"})
    assert "NOT AUTHENTICATED" in ScreenClient._ui_error(r)


def test_ui_error_non_login_redirect_not_flagged_as_auth():
    # a goTo/other redirect (e.g. post-restore hand-off to SM203510) is NOT an
    # auth failure — the Login-specific guard must not swallow it as one.
    r = _Resp(200, {"redirect": "/Scripts/Screens/SalesDemo/SM203510.html"})
    assert ScreenClient._ui_error(r) is None


# ---- modern-plane GRID CRUD payload shapes ---------------------------------
#
# These lock the exact JSON the grid write path emits. The bugs they guard
# against are the silent no-persist / wrong-row traps that a clean HTTP 200
# hides: dropping the KEY from `values` (→ blank-row insert), dropping the
# `columns` echo (→ 200 no-op), a wrong dataKey/changeType, or (master-detail)
# failing to navigate the master or scope viewsParams to the parent view.

class _FakeHTTP:
    """Records outgoing payloads; serves `read_body` to reads, OK to Saves."""

    def __init__(self, read_body: dict):
        self.calls: list[dict] = []
        self._read = read_body

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append(json or {})
        if (json or {}).get("command"):
            return _Resp(200, {"messages": []})
        return _Resp(200, self._read)

    async def aclose(self):
        pass


def _client(screen: str = "GL202500") -> ScreenClient:
    s = ScreenClient(_inst(), screen)
    # these tests unit-test payload SHAPING with a fake _http; mark the session
    # authenticated so the self-heal login() guard (real network) stays a no-op.
    s._logged_in = True
    return s


def _read_body(grid: str, columns, rows, key_names, qff=None) -> dict:
    return {"controlsData": {grid: {"columns": columns, "rows": rows,
            "dataKeyNames": key_names, "quickFilterFields": qff or []}}}


def _save(fake: _FakeHTTP) -> dict:
    saves = [c for c in fake.calls if c.get("command")]
    assert saves, "no Save POST was emitted"
    return saves[-1]


# -- pure builders --

def test_kv_keeps_bool_strings_the_rest():
    d = {x["field"]: x["value"] for x in ScreenClient._kv({"a": True, "b": 5, "c": "x"})}
    assert d["a"] is True and d["b"] == "5" and d["c"] == "x"


def test_cell_key_unwraps_lookup_value():
    assert ScreenClient._cell_key({"cells": {"k": {"value": {"id": "X", "text": "t"}}}}, "k") == "X"
    assert ScreenClient._cell_key({"cells": {"k": {"value": "Y"}}}, "k") == "Y"


def test_full_key_builds_composite_incl_parent_id():
    s = _client()
    row = {"cells": {"CashAccountID": {"value": 994},
                     "EntryTypeID": {"value": {"id": "BANKCHG"}}}}
    assert s._full_key(row, ["CashAccountID", "EntryTypeID"]) == {
        "CashAccountID": 994, "EntryTypeID": "BANKCHG"}


def test_grid_ctrl_echoes_columns_and_gates_datakey():
    s = _client()
    cols = [{"field": "A"}]
    g = {"columns": cols, "rows": [{}], "quick_filter_fields": ["A"]}
    ins = s._grid_ctrl("G", g, {"inserted": []}, None)
    assert ins["columns"] is cols and ins["resultType"] == "GridData" and "pageSize" in ins
    assert "dataKey" not in ins                       # insert: no dataKey
    upd = s._grid_ctrl("G", g, {"modified": []}, {"A": "1"})
    assert upd["dataKey"] == {"A": "1"}               # update/delete: dataKey set


# -- top-level insert/update/delete payloads --

def test_insert_payload_has_key_columns_id_no_datakey():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g1", "cells": {"AccountCD": {"value": "10100"}}}], ["AccountCD"]))
    asyncio.run(s.ui_insert_grid_row(
        "AccountRecords", {"AccountCD": "40100", "Type": "I", "Description": "X"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    ins = ctrl["changes"]["inserted"][0]
    assert "AccountCD" in {v["field"] for v in ins["values"]}   # key present (else blank-row insert)
    assert ctrl["columns"] == [{"field": "AccountCD"}, {"field": "Description"}]  # echoed (else no-op)
    assert "dataKey" not in ctrl and ins.get("id") and ins["index"] == 1
    assert _save(s._http)["activeRowContexts"] == []


def test_update_payload_resends_key_in_values():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g8", "cells": {"AccountCD": {"value": "40000"},
                                 "Description": {"value": "Sales"}}}], ["AccountCD"]))
    asyncio.run(s.ui_update_grid_row("AccountRecords", {"AccountCD": "40000"},
                                     {"Description": "New"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    mod = ctrl["changes"]["modified"][0]
    fields = {v["field"]: v["value"] for v in mod["values"]}
    assert fields.get("AccountCD") == "40000" and fields.get("Description") == "New"
    assert ctrl["dataKey"] == {"AccountCD": "40000"} and mod["id"] == "g8"


# ---- warning/info toasts ----------------------------------------------------
#
# The top-right toast is `messages[]`. _ui_error only surfaces messageType=="error"
# on a 200 -- correctly, since its return value raises and a warning is not a failure
# -- so warnings/info were dropped entirely. They are the messages that explain an
# accepted-but-ignored write, so they must ride on the RESULT instead.

def test_notices_returns_warnings_and_info_not_errors():
    j = {"messages": [
        {"message": "The period is closed.", "messageType": "Warning"},
        {"message": "3 rows skipped.", "messageType": "Info"},
        {"message": "boom", "messageType": "Error"},   # already raises via _ui_error
        {"message": "untyped note"},                     # no type -> info
        {"messageType": "Warning"},                      # no text -> dropped
    ]}
    assert ScreenClient._notices(j) == [
        {"type": "warning", "message": "The period is closed."},
        {"type": "info", "message": "3 rows skipped."},
        {"type": "info", "message": "untyped note"},
    ]


def test_notices_tolerates_junk():
    assert ScreenClient._notices(None) == []
    assert ScreenClient._notices({"messages": None}) == []
    assert ScreenClient._notices({}) == []


def test_warning_on_200_does_not_raise_but_is_reported():
    # The whole point: a warning must NOT become an error (that filter is deliberate),
    # yet must stop being invisible.
    warn = _Resp(200, {"messages": [{"message": "Year already generated.",
                                     "messageType": "Warning"}]})
    assert ScreenClient._ui_error(warn) is None          # unchanged: does not raise
    assert ScreenClient._notices(warn.json()) == [
        {"type": "warning", "message": "Year already generated."}]


def test_grid_save_annotates_notices():
    class _WarnHTTP(_FakeHTTP):
        async def post(self, url, json=None, headers=None):  # noqa: A002
            self.calls.append(json or {})
            if (json or {}).get("command"):
                return _Resp(200, {"messages": [{"message": "Row ignored.",
                                                 "messageType": "Warning"}]})
            return _Resp(200, self._read)

    s = _client("GL202500")
    s._http = _WarnHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g8", "cells": {"AccountCD": {"value": "40000"},
                                "Description": {"value": "Sales"}}}], ["AccountCD"]))
    out = asyncio.run(s.ui_update_grid_row("AccountRecords", {"AccountCD": "40000"},
                                           {"Description": "New"}))
    assert out["@grp.notices"] == [{"type": "warning", "message": "Row ignored."}]


def test_update_grid_rows_surfaces_per_chunk_notices():
    rows = _rows(4)
    s = _client("GL202500")
    http = _EchoHTTP(_read_body("Details", [{"field": "LineNbr"}, {"field": "Descr"}],
                                rows, ["LineNbr"]), "Details", echo_rows=rows)
    s._http = http
    orig = http.post

    async def post(url, json=None, headers=None):  # noqa: A002
        r = await orig(url, json=json, headers=headers)
        if (json or {}).get("command"):
            body = dict(r.json())
            body["messages"] = [{"message": "Locked.", "messageType": "Warning"}]
            return _Resp(200, body)
        return r

    http.post = post
    out = asyncio.run(s.ui_update_grid_rows(
        "Details",
        [{"key": {"LineNbr": str(i)}, "values": {"Descr": f"d{i}"}} for i in range(1, 5)],
        skip_validation=True, chunk_size=2))
    assert out["notices"] == [{"chunk": 1, "type": "warning", "message": "Locked."},
                              {"chunk": 2, "type": "warning", "message": "Locked."}]
    assert "note" in out  # updated counts rows SENT, not rows kept


# ---- clearSession footgun + sibling-view error surfacing --------------------
#
# Two bugs found verifying an external PY309000 report: (1) ui_grid_read's
# clearSession wipes ui_set_field edits staged earlier in the SAME session when
# a write wrapper reads the grid before saving, producing misleading validator
# errors on fields that WERE already set; (2) _grid_save only echoed back
# grid_view + parent in viewsParams, so a validator error rooted in a THIRD,
# sibling view (e.g. Employments.Step/Level while inserting a bank-details row
# under Employees) came back as a useless generic "record raised at least one
# error" with zero field-level detail.

def test_ui_grid_read_default_clears_session():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body("AccountRecords", [{"field": "AccountCD"}], [], ["AccountCD"]))
    asyncio.run(s.ui_grid_read("AccountRecords"))
    assert s._http.calls[0]["clearSession"] is True


def test_ui_grid_read_preserve_session_skips_clear():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body("AccountRecords", [{"field": "AccountCD"}], [], ["AccountCD"]))
    asyncio.run(s.ui_grid_read("AccountRecords", preserve_session=True))
    assert s._http.calls[0]["clearSession"] is False


def test_ui_grid_read_preserve_session_skips_clear_with_parent():
    s = _client("PY309000")
    s._http = _FakeHTTP(_read_body("EmployeeBankDetails", [{"field": "EmployeeBankDetailID"}],
                                   [], []))
    asyncio.run(s.ui_grid_read("EmployeeBankDetails",
                              parent={"view": "Employees", "key": {"EmployeeCD": "EMP001"}},
                              preserve_session=True))
    assert s._http.calls[0]["clearSession"] is False


def test_ui_insert_grid_row_preserves_prior_session_state():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g1", "cells": {"AccountCD": {"value": "10100"}}}], ["AccountCD"]))
    asyncio.run(s.ui_insert_grid_row(
        "AccountRecords", {"AccountCD": "40100", "Type": "I", "Description": "X"}))
    assert s._http.calls[0].get("clearSession") is False


def test_ui_update_grid_row_preserves_prior_session_state():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}, {"field": "Description"}],
        [{"id": "g8", "cells": {"AccountCD": {"value": "40000"}}}], ["AccountCD"]))
    asyncio.run(s.ui_update_grid_row("AccountRecords", {"AccountCD": "40000"},
                                    {"Description": "New"}))
    assert s._http.calls[0].get("clearSession") is False


def test_ui_delete_grid_row_preserves_prior_session_state():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}],
        [{"id": "g8", "cells": {"AccountCD": {"value": "40000"}}}], ["AccountCD"]))
    asyncio.run(s.ui_delete_grid_row("AccountRecords", {"AccountCD": "40000"}))
    assert s._http.calls[0].get("clearSession") is False


def test_ui_update_grid_rows_preserves_prior_session_state():
    out, http = _bulk(echo_rows=_rows(4))
    assert http.calls[0].get("clearSession") is False


def test_ui_bootstrap_tracks_and_replaces_bootstrapped_views():
    s = _client("PY309000")
    s._http = _FakeHTTP({"graphIsDirty": False})
    asyncio.run(s.ui_bootstrap(["Employments", "CurrentEmployees"]))
    assert s._bootstrapped_views == {"Employments", "CurrentEmployees"}
    asyncio.run(s.ui_bootstrap(["Employees"]))
    assert s._bootstrapped_views == {"Employees"}  # replaced, not unioned -- fresh graph


def test_ui_navigate_record_adds_to_bootstrapped_views():
    s = _client("PY309000")
    s._http = _FakeHTTP({"messages": []})
    s._ui_booted = True  # simulate: ui_bootstrap already ran this session
    s._bootstrapped_views = {"Employments"}
    asyncio.run(s.ui_navigate_record("Employees", {"EmployeeCD": "EMP001"}))
    assert s._bootstrapped_views == {"Employments", "Employees"}


def test_field_state_errors_extracts_all_flagged_fields():
    j = {"fieldStates": {
        "Employments": [
            {"fieldName": "Step", "fieldState": {"error": "Step is required", "errorLevel": 4}},
            {"fieldName": "Level", "fieldState": {"error": "Level is required", "errorLevel": 4}},
            {"fieldName": "BranchID", "fieldState": {"value": "MAIN"}},  # no error -> excluded
        ],
        "Employees": [{"fieldName": "EmployeeCD", "fieldState": {"value": "EMP001"}}],
    }}
    assert ScreenClient._field_state_errors(j) == [
        "Employments.Step: Step is required",
        "Employments.Level: Level is required",
    ]


def test_field_state_errors_tolerates_junk():
    assert ScreenClient._field_state_errors(None) == []
    assert ScreenClient._field_state_errors({}) == []
    assert ScreenClient._field_state_errors({"fieldStates": None}) == []


def test_grid_save_includes_bootstrapped_views_in_viewsparams():
    s = _client("PY309000")
    s._bootstrapped_views = {"Employments", "CurrentEmployees"}
    s._http = _FakeHTTP(_read_body(
        "EmployeeBankDetails", [{"field": "EmployeeBankDetailID"}], [], []))
    asyncio.run(s.ui_insert_grid_row(
        "EmployeeBankDetails", {"EmployeeBankID": 1147, "AccountNo": "1", "Percent": 100}))
    save = _save(s._http)
    assert set(save["viewsParams"].keys()) >= {
        "EmployeeBankDetails", "Employments", "CurrentEmployees"}


def test_grid_save_surfaces_sibling_view_field_errors():
    class _FaultHTTP(_FakeHTTP):
        async def post(self, url, json=None, headers=None):  # noqa: A002
            self.calls.append(json or {})
            if (json or {}).get("command"):
                return _Resp(409, {
                    "messages": [{"message": "Error: Inserting 'Employment' record "
                                             "raised at least one error.",
                                  "messageType": "error"}],
                    "fieldStates": {"Employments": [
                        {"fieldName": "Step", "fieldState": {"error": "Step is required"}},
                        {"fieldName": "Level", "fieldState": {"error": "Level is required"}},
                    ]},
                })
            return _Resp(200, self._read)

    s = _client("PY309000")
    s._bootstrapped_views = {"Employments"}
    s._http = _FaultHTTP(_read_body(
        "EmployeeBankDetails", [{"field": "EmployeeBankDetailID"}], [], []))
    with pytest.raises(ScreenError) as exc:
        asyncio.run(s.ui_insert_grid_row(
            "EmployeeBankDetails", {"EmployeeBankID": 1147, "AccountNo": "1", "Percent": 100}))
    msg = str(exc.value)
    assert "Step is required" in msg and "Level is required" in msg


def test_grid_save_no_extra_detail_when_fieldstates_empty():
    # the selector/lookup-failure case (proven live: "Employee Bank ... cannot be
    # found") has no per-field annotation anywhere -- the message must stay as-is,
    # not grow a bogus "field detail: " suffix with nothing behind it.
    s = _client("PY309000")
    s._http = _FakeHTTP(_read_body(
        "EmployeeBankDetails", [{"field": "EmployeeBankDetailID"}], [], []))

    class _GenericFaultHTTP(_FakeHTTP):
        async def post(self, url, json=None, headers=None):  # noqa: A002
            self.calls.append(json or {})
            if (json or {}).get("command"):
                return _Resp(409, {"messages": [{"message": "generic failure",
                                                 "messageType": "error"}]})
            return _Resp(200, self._read)

    s._http = _GenericFaultHTTP(_read_body(
        "EmployeeBankDetails", [{"field": "EmployeeBankDetailID"}], [], []))
    with pytest.raises(ScreenError) as exc:
        asyncio.run(s.ui_insert_grid_row(
            "EmployeeBankDetails", {"EmployeeBankID": 1147, "AccountNo": "1", "Percent": 100}))
    assert str(exc.value).endswith("generic failure")


# ---- read-back guard --------------------------------------------------------
#
# The plane discards an unparseable value and WIPES the field (proven live on
# AP301000: DueDate 2027-01-01 -> null, clean 200, graphIsDirty TRUE). A Save then
# persists the blank over real data. Reading the field back is the only signal that
# catches it -- but it must flag ONLY blank-after-non-blank, because the plane
# reformats what it stores and value-equality would fire on every date.

class _ReadBackHTTP:
    """Serves scripted fieldStates to the read-back POST."""

    def __init__(self, values):
        self.values = values      # {(view, field): stored_value}
        self.posts = 0

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.posts += 1
        views = {}
        for (v, f), val in self.values.items():
            views.setdefault(v, []).append({"fieldName": f, "fieldState": {"value": val}})
        return _Resp(200, {"graphIsDirty": True, "fieldStates": views, "messages": []})

    async def aclose(self):
        pass


def _verify(sets, stored):
    s = _client("AP301000")
    s._http = _ReadBackHTTP(stored)
    s._logged_in = True
    s._ui_booted = True
    return asyncio.run(s.verify_sets(sets)), s._http


def test_read_back_flags_a_wiped_field():
    # the live AP301000.DueDate case: sent a date, field now holds nothing
    out, http = _verify([{"view": "Document", "field": "DueDate", "value": "NOT-A-DATE"}],
                        {("Document", "DueDate"): None})
    assert len(out) == 1
    assert out[0]["field"] == "DueDate" and out[0]["stored"] is None
    assert "did NOT take" in out[0]["reason"]
    assert http.posts == 1  # ONE round-trip for the whole guard


def test_read_back_does_not_flag_a_reformatted_value():
    # THE false-positive trap: the plane stores its own format. Sent "01/01/2027",
    # reads back "2027-01-01T00:00:00.0000000". Equality-checking would flag every
    # date, enum and selector on every call -- so only blankness may be judged.
    out, _ = _verify([{"view": "Document", "field": "DueDate", "value": "01/01/2027"}],
                     {("Document", "DueDate"): "2027-01-01T00:00:00.0000000"})
    assert out == []


def test_read_back_ignores_deliberately_blank_sets():
    # clearing a field on purpose is not a rejection
    out, http = _verify([{"view": "Document", "field": "DueDate", "value": ""}],
                        {("Document", "DueDate"): None})
    assert out == [] and http.posts == 0  # nothing to check -> no round-trip at all


def test_read_back_handles_selector_and_bool_shapes():
    out, _ = _verify(
        [{"view": "Document", "field": "Vendor", "value": {"id": "V1", "text": "Acme"}},
         {"view": "Document", "field": "Hold", "value": True},
         {"view": "Document", "field": "Ref", "value": "X"}],
        {("Document", "Vendor"): {"id": "V1", "text": "Acme"},   # took
         ("Document", "Hold"): False,                             # False is NOT blank
         ("Document", "Ref"): "   "})                             # whitespace = blank
    assert [r["field"] for r in out] == ["Ref"]


def test_read_back_skips_fields_the_graph_did_not_echo():
    # no fieldState for it -> nothing can be concluded, so don't guess
    out, _ = _verify([{"view": "Document", "field": "Missing", "value": "x"}],
                     {("Document", "DueDate"): None})
    assert out == []


# ---- silent-rejection net (graphIsDirty) ------------------------------------
#
# Probed live on GL101000 (2026-07-15): the plane reports NOTHING when it refuses a
# value -- no messages, no fieldStates, clean 200. graphIsDirty is the only signal,
# and only in the clean->still-clean direction. These lock that reading, including
# the control that makes it trustworthy and the limitation that bounds it.

class _DirtyHTTP:
    """Serves a scripted graphIsDirty per POST (bootstrap first, then each set)."""

    def __init__(self, dirty_seq):
        self.seq = list(dirty_seq)
        self.calls = []

    async def post(self, url, json=None, headers=None):  # noqa: A002
        self.calls.append(json or {})
        d = self.seq.pop(0) if self.seq else False
        return _Resp(200, {"graphIsDirty": d, "messages": []})

    async def aclose(self):
        pass


def _set_field(dirty_seq, value="x"):
    s = _client("GL101000")
    s._http = _DirtyHTTP(dirty_seq)
    asyncio.run(s.ui_bootstrap(["FiscalYearSetup"]))
    asyncio.run(s.ui_set_field("FiscalYearSetup", "BegFinYear", value))
    return s


def test_set_field_flags_silently_refused_value():
    # bootstrap -> clean, set -> STILL clean = refused (the live invalid-date case)
    s = _set_field([False, False], value="NOT-A-DATE")
    assert len(s._rejected_sets) == 1
    r = s._rejected_sets[0]
    assert r["field"] == "BegFinYear" and r["value"] == "NOT-A-DATE"
    assert "REFUSED" in r["reason"]


def test_set_field_accepts_when_graph_goes_dirty():
    # clean -> dirty = the value landed
    assert _set_field([False, True])._rejected_sets == []


def test_set_field_no_change_is_not_a_false_positive():
    # THE control that makes this signal usable: setting a field to its own current
    # value still returns dirty=True live, so clean->clean cannot be explained away as
    # "nothing changed". If that ever regressed to dirty=False, this net would cry wolf
    # on every no-op write -- so pin the accepted reading explicitly.
    assert _set_field([False, True])._rejected_sets == []


def test_set_field_does_not_guess_when_dirty_state_unknown():
    # Never observed clean (non-JSON bootstrap) -> must NOT flag.
    class _JunkHTTP(_DirtyHTTP):
        async def post(self, url, json=None, headers=None):  # noqa: A002
            self.calls.append(json or {})
            return _Resp(200, "not json")

    s = _client("GL101000")
    s._http = _JunkHTTP([])
    asyncio.run(s.ui_bootstrap(["FiscalYearSetup"]))
    assert s._graph_dirty is None
    asyncio.run(s.ui_set_field("FiscalYearSetup", "BegFinYear", "x"))
    assert s._rejected_sets == []


def test_set_field_net_misses_values_the_plane_accepts():
    # COVERAGE, pinned so nobody re-generalizes this net from its one hit. Live probe
    # (2026-07-15, 5 screens / 4 graphs) found most bad values are ACCEPTED into the
    # graph -- AP301000 DocDate="NOT-A-DATE", "abc" into an Int16, even a read-only
    # write, all returned dirty=True. clean->dirty is indistinguishable from a good
    # set, so the net cannot flag them. "No rejected_fields" != "the values were good".
    s = _set_field([False, True], value="NOT-A-DATE")   # accepted-but-invalid
    assert s._rejected_sets == []


def test_set_field_cannot_see_refusal_once_graph_is_dirty():
    # Documented LIMIT: dirty stays dirty, so a refusal after a successful set is
    # invisible. Pin it so nobody mistakes this net for a guarantee.
    s = _client("GL101000")
    s._http = _DirtyHTTP([False, True, True])
    asyncio.run(s.ui_bootstrap(["FiscalYearSetup"]))
    asyncio.run(s.ui_set_field("FiscalYearSetup", "BegFinYear", "01/01/2027"))  # lands
    asyncio.run(s.ui_set_field("FiscalYearSetup", "FinPeriods", "abc"))         # refused
    assert s._rejected_sets == []  # not a bug: the signal genuinely cannot discriminate


# ---- reconcile_rejected_sets: the clean->clean FALSE-POSITIVE guard ----------
#
# The graphIsDirty net fires clean->clean even when the field ALREADY holds the
# sent value (re-setting a key on an existing record is a no-op, not a refusal).
# Measured false positive on CS101500/CS102000 AcctCD during a verify pass -- the
# record was created fine, yet the net cried "silently refused". The reconcile
# step reads the fields back and keeps only the genuine refusals.

def _reconcile(entries, stored):
    s = _client("CS101500")
    s._http = _ReadBackHTTP(stored)
    s._logged_in = True
    s._ui_booted = True
    return asyncio.run(s.reconcile_rejected_sets(entries))


def test_reconcile_drops_key_reset_noop():
    # THE reported false positive: AcctCD already holds "AI" -> no-op, not refused
    genuine, noops = _reconcile(
        [{"view": "BAccount", "field": "AcctCD", "value": "AI"}],
        {("BAccount", "AcctCD"): "AI"})
    assert genuine == []
    assert len(noops) == 1 and noops[0]["field"] == "AcctCD" and noops[0]["current"] == "AI"


def test_reconcile_keeps_genuine_refusal():
    # field holds a DIFFERENT value than sent -> a real refusal, must survive
    genuine, noops = _reconcile(
        [{"view": "BAccount", "field": "AcctCD", "value": "NEW"}],
        {("BAccount", "AcctCD"): "OLD"})
    assert noops == []
    assert len(genuine) == 1 and genuine[0]["value"] == "NEW"


def test_reconcile_keeps_when_field_not_readable():
    # can't read the field back -> conservatively KEEP (never silently drop a real refusal)
    genuine, noops = _reconcile(
        [{"view": "BAccount", "field": "AcctCD", "value": "AI"}], {})
    assert noops == [] and len(genuine) == 1


def test_reconcile_empty_makes_no_round_trip():
    s = _client("CS101500")
    s._http = _ReadBackHTTP({})
    s._logged_in = True
    s._ui_booted = True
    assert asyncio.run(s.reconcile_rejected_sets([])) == ([], [])
    assert s._http.posts == 0


def test_values_match_is_conservative():
    m = ScreenClient._values_match
    assert m("AI", "AI")                                   # key re-set no-op
    assert not m("OLD", "NEW")                             # real refusal
    assert m("V1", {"id": "V1", "text": "Acme"})          # selector: id vs {id,text}
    assert m({"id": "V1", "text": "Acme"}, {"id": "V1"})  # both selector shapes
    assert m("true", True) and m(True, True)              # bool normalize
    assert not m("", "")                                   # blank sent never matches
    assert not m("AI", "")                                 # blank sent never matches


# ---- relocated tool notes ---------------------------------------------------
#
# Trimming a docstring only saves tokens if the text it dropped is still REACHABLE.
# These lock both halves of that bargain: guide() serves every note, and no docstring
# advertises a note that doesn't exist.

def test_guide_serves_every_relocated_tool_note():
    from grp_mcp.server import _TOOL_NOTES, guide
    assert _TOOL_NOTES, "no notes relocated yet"
    for name, text in _TOOL_NOTES.items():
        got = guide(topic=name)
        assert got["tool"] == name
        assert got["notes"] == text


def test_unknown_guide_topic_lists_available_tool_notes():
    from grp_mcp.server import _TOOL_NOTES, guide
    got = guide(topic="definitely-not-a-topic")
    assert "error" in got
    assert got["tool_notes"] == sorted(_TOOL_NOTES)


def test_docstring_note_pointers_resolve():
    # A docstring saying guide(topic="X") must actually reach a note named X, or the
    # trim silently destroyed the guidance instead of relocating it.
    import pathlib
    import re
    from grp_mcp import server as _server
    from grp_mcp.server import _TOOL_NOTES
    src = pathlib.Path(_server.__file__).read_text(encoding="utf-8")
    pointed = set(re.findall(r'guide\(topic="([a-z_]+)"\)', src))
    # the generic routing topics guide() already knew about are not tool notes
    pointed -= {"read", "write", "grid", "process", "setup", "lookup", "customization",
                "import", "files", "actions", "session", "discover", "planes"}
    assert pointed, "no docstring points at a tool note"
    missing = pointed - set(_TOOL_NOTES)
    assert not missing, f"docstrings point at notes that don't exist: {sorted(missing)}"


# ---- /structure ETag cache --------------------------------------------------
#
# The endpoint's ETag is an ENVIRONMENT stamp, identical for every screen on a
# tenant (verified live on 25.101: AP301000's etag replayed at GL101000's url
# returns 304). So the server cannot be relied on to notice a screen mix-up — the
# cache key has to, which is what these lock.

class _StructHTTP:
    """Serves /structure with an ETag; 304s when the matching If-None-Match arrives."""

    def __init__(self, bodies: dict, etag: str = 'W/"env-v1"'):
        self.bodies = bodies          # screen_id -> raw structure body
        self.etag = etag
        self.full = 0
        self.conditional = 0
        self.sent_inm: list = []

    async def get(self, url, headers=None):
        h = headers or {}
        inm = h.get("If-None-Match")
        self.sent_inm.append(inm)
        if inm == self.etag:
            self.conditional += 1
            return _Resp(304, None, headers={"ETag": self.etag})
        self.full += 1
        screen = url.split("/ui/screen/")[1].split("/")[0]
        return _Resp(200, self.bodies[screen], headers={"ETag": self.etag})

    async def aclose(self):
        pass


def _struct_body(dac: str) -> dict:
    return {"primaryDacName": dac, "fieldStates": {}, "actionStates": {},
            "controlsData": {}}


def _struct_client(screen: str, http) -> ScreenClient:
    s = ScreenClient(_inst(), screen)
    s._logged_in = True
    s._http = http
    return s


def test_structure_memoized_within_one_client():
    http = _StructHTTP({"GL101000": _struct_body("FinYearSetup")})
    s = _struct_client("GL101000", http)
    for _ in range(3):
        asyncio.run(s.get_ui_structure())
    assert http.full == 1 and http.conditional == 0  # memo: no repeat HTTP at all


def test_structure_revalidates_with_etag_across_clients():
    http = _StructHTTP({"GL101000": _struct_body("FinYearSetup")})
    first = asyncio.run(_struct_client("GL101000", http).get_ui_structure())
    # a NEW client (as every tool call builds) must revalidate, not re-download
    second = asyncio.run(_struct_client("GL101000", http).get_ui_structure())
    assert http.full == 1 and http.conditional == 1
    assert http.sent_inm == [None, 'W/"env-v1"']
    assert second == first


def test_structure_cache_does_not_leak_across_screens():
    # The guard that matters: the server 304s on a matching etag WITHOUT checking the
    # screen, so a key that ignored screen_id would serve GL101000's metadata for
    # AP301000. A second screen must therefore be fetched in FULL, never revalidated.
    http = _StructHTTP({"GL101000": _struct_body("FinYearSetup"),
                        "AP301000": _struct_body("APInvoice")})
    gl = asyncio.run(_struct_client("GL101000", http).get_ui_structure())
    ap = asyncio.run(_struct_client("AP301000", http).get_ui_structure())
    assert gl["primary_dac"] == "FinYearSetup"
    assert ap["primary_dac"] == "APInvoice"
    assert http.full == 2 and http.conditional == 0
    assert http.sent_inm == [None, None]  # no cross-screen etag replay


def test_structure_refresh_forces_full_fetch():
    http = _StructHTTP({"GL101000": _struct_body("FinYearSetup")})
    s = _struct_client("GL101000", http)
    asyncio.run(s.get_ui_structure())
    asyncio.run(s.get_ui_structure(refresh=True))
    assert http.full == 2 and http.sent_inm == [None, None]


def test_clear_struct_cache_drops_entries():
    from grp_mcp.screen import clear_struct_cache
    http = _StructHTTP({"GL101000": _struct_body("FinYearSetup")})
    asyncio.run(_struct_client("GL101000", http).get_ui_structure())
    assert clear_struct_cache()  # returns the keys it dropped
    asyncio.run(_struct_client("GL101000", http).get_ui_structure())
    # cache was empty -> a full fetch with no conditional header
    assert http.full == 2 and http.sent_inm == [None, None]


class _EchoHTTP:
    """Like _FakeHTTP, but a Save can ECHO grid rows back (as the real plane does).

    Counts reads vs Saves separately so a test can assert the bulk path stopped
    re-reading the whole grid between chunks.
    """

    def __init__(self, read_body: dict, grid: str, echo_rows=None):
        self.calls: list[dict] = []
        self.reads = 0
        self.saves = 0
        self._read = read_body
        self._grid = grid
        self._echo = echo_rows

    async def post(self, url, json=None, headers=None):  # noqa: A002
        payload = json or {}
        self.calls.append(payload)
        if payload.get("command"):
            self.saves += 1
            body: dict = {"messages": []}
            if self._echo is not None:
                body["controlsData"] = {self._grid: {"rows": self._echo}}
            return _Resp(200, body)
        self.reads += 1
        return _Resp(200, self._read)

    async def aclose(self):
        pass


def _rows(n: int) -> list:
    return [{"id": f"r{i}", "cells": {"LineNbr": {"value": str(i)}}}
            for i in range(1, n + 1)]


def _bulk(echo_rows, chunk_size=2, n=4):
    rows = _rows(n)
    s = _client("GL202500")
    http = _EchoHTTP(_read_body("Details", [{"field": "LineNbr"}, {"field": "Descr"}],
                                rows, ["LineNbr"]), "Details", echo_rows=echo_rows)
    s._http = http
    out = asyncio.run(s.ui_update_grid_rows(
        "Details",
        [{"key": {"LineNbr": str(i)}, "values": {"Descr": f"d{i}"}} for i in range(1, n + 1)],
        skip_validation=True, chunk_size=chunk_size))
    return out, http


def test_update_grid_rows_reuses_save_echo_instead_of_rereading():
    # The full-grid echo a Save returns carries fresh row ids, so chunk 2 must be
    # mapped from it — the whole-grid re-read per chunk was the dominant cost.
    out, http = _bulk(echo_rows=_rows(4))
    assert out["updated"] == 4 and out["chunks"] == 2 and out["ok"]
    assert http.saves == 2
    assert http.reads == 1  # the initial read only


def test_update_grid_rows_rereads_when_save_echoes_nothing():
    # No echo -> must fall back to a real read, i.e. the old behaviour.
    out, http = _bulk(echo_rows=None)
    assert out["updated"] == 4 and out["chunks"] == 2
    assert http.reads == 2  # initial + one re-read between the chunks


def test_update_grid_rows_rereads_on_partial_echo():
    # A SHORT echo is a delta, not the grid: reusing it would drop rows from the map
    # and mis-report them as not_found. Must re-read instead.
    out, http = _bulk(echo_rows=_rows(4)[:1])
    assert out["updated"] == 4 and out["chunks"] == 2
    assert out["not_found"] == []
    assert http.reads == 2


def test_delete_payload_sends_key_in_values():
    s = _client("GL202500")
    s._http = _FakeHTTP(_read_body(
        "AccountRecords", [{"field": "AccountCD"}],
        [{"id": "g9", "cells": {"AccountCD": {"value": "40100"}}}], ["AccountCD"]))
    asyncio.run(s.ui_delete_grid_row("AccountRecords", {"AccountCD": "40100"}))
    ctrl = _save(s._http)["controlsParams"]["AccountRecords"]
    dl = ctrl["changes"]["deleted"][0]
    assert "AccountCD" in {v["field"] for v in dl["values"]}
    assert ctrl["dataKey"] == {"AccountCD": "40100"}


# -- master-detail payloads --

def test_md_insert_navigates_master_and_scopes_viewsparams():
    s = _client("CA202000")
    s._http = _FakeHTTP(_read_body(
        "ETDetails", [{"field": "EntryTypeID"}, {"field": "CashAccountID"}],
        [], ["CashAccountID", "EntryTypeID"]))
    parent = {"view": "CashAccount", "key": {"CashAccountCD": "10200"}}
    asyncio.run(s.ui_insert_grid_row("ETDetails", {"EntryTypeID": "BANKCHG"}, parent))
    nav = [c for c in s._http.calls
           if any(d.get("changeType") == 5 for d in (c.get("data") or []))]
    assert nav, "master was not navigated"
    d = nav[0]["data"][0]
    assert (d["viewName"], d["fieldName"], d["value"]) == ("CashAccount", "CashAccountCD", "10200")
    save = _save(s._http)
    assert "CashAccount" in save["viewsParams"] and "ETDetails" in save["viewsParams"]
    ins = save["controlsParams"]["ETDetails"]["changes"]["inserted"][0]
    assert {v["field"] for v in ins["values"]} == {"EntryTypeID"}  # parent id auto-filled server-side
    assert save["activeRowContexts"] == []


def test_md_delete_resolves_full_composite_key_from_lookup():
    s = _client("CA202000")
    s._http = _FakeHTTP(_read_body(
        "ETDetails", [{"field": "EntryTypeID"}, {"field": "CashAccountID"}],
        [{"id": "994;BANKCHG", "cells": {"CashAccountID": {"value": 994},
          "EntryTypeID": {"value": {"id": "BANKCHG", "text": "BANKCHG"}}}}],
        ["CashAccountID", "EntryTypeID"]))
    parent = {"view": "CashAccount", "key": {"CashAccountCD": "10200"}}
    asyncio.run(s.ui_delete_grid_row("ETDetails", {"EntryTypeID": "BANKCHG"}, parent))
    ctrl = _save(s._http)["controlsParams"]["ETDetails"]
    dl = ctrl["changes"]["deleted"][0]
    assert {v["field"]: v["value"] for v in dl["values"]} == {
        "CashAccountID": "994", "EntryTypeID": "BANKCHG"}   # full key, lookup unwrapped to id
    assert ctrl["dataKey"] == {"CashAccountID": 994, "EntryTypeID": "BANKCHG"}
    assert dl["id"] == "994;BANKCHG"


# -- the classic positional-row footgun stays disabled --

def test_positional_row_spec_hard_errors():
    from grp_mcp.screen import ScreenError
    s = _client("GL202500")
    with pytest.raises(ScreenError):
        s._spec_to_command({"row": "AccountRecords", "to": 8})


# ---- ui.py CSRF guard (_is_same_origin) -------------------------------------
# A page open in any OTHER tab/site can still fire a blind cross-origin POST at
# the config UI while it happens to be running (CORS blocks it from reading the
# response, not from sending the request) — do_POST must reject anything that
# doesn't prove same-origin via Origin (or, failing that, Referer).

def test_same_origin_exact_origin_match():
    from grp_mcp.ui import _is_same_origin
    assert _is_same_origin({"Origin": "http://127.0.0.1:8765"}, "http://127.0.0.1:8765")


def test_same_origin_rejects_cross_origin():
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({"Origin": "https://evil.com"}, "http://127.0.0.1:8765")


# ---- TREE node selection (ui_select_tree_node) ------------------------------
# A tree control (e.g. SM207060's EntityTree) isn't a normal data grid — it needs
# its own activeRowContexts/controlsParams/viewsParams shape. These three pure
# builders were derived by bisecting a live browser capture; lock the exact shape
# down since a silently-wrong field here produces a 200 with no error, not a
# visible failure (proven live, 2026-07-02).

def test_discover_prereqs_parses_real_payroll_errors():
    from grp_mcp.server import _UI_VALIDATION_PAT, _DETAIL_RULE_PAT, _flagged_field_names
    # the exact hand-thrown rule captured live on PY301000
    pcb = "PCB Pay Code can not be empty"
    assert _UI_VALIDATION_PAT.search(pcb)
    assert "PCB Pay Code" in _flagged_field_names(pcb)
    assert not _DETAIL_RULE_PAT.search(pcb)
    # the detail-grid rule from CSPYPayCompanyEntry
    tax = "Atleast one or more Tax Office details are required"
    assert _UI_VALIDATION_PAT.search(tax)
    assert _DETAIL_RULE_PAT.search(tax)


def test_xlsx_read_risk_flags_openpyxl_and_inline_strings(tmp_path):
    from grp_mcp.server import _xlsx_read_risk
    import openpyxl, zipfile, shutil
    # an actual openpyxl-authored file -> flagged (matches the live 0-row repro)
    p = tmp_path / "bad.xlsx"
    wb = openpyxl.Workbook(); wb.active.append(["A", "B"]); wb.save(p)
    risk = _xlsx_read_risk(p)
    assert risk and "openpyxl" in risk
    # simulate a real-Excel file: rewrite app.xml + add sharedStrings -> no risk
    good = tmp_path / "good.xlsx"
    with zipfile.ZipFile(p) as zin, zipfile.ZipFile(good, "w") as zout:
        for item in zin.namelist():
            if item == "docProps/app.xml":
                zout.writestr(item, '<Properties><Application>Microsoft Excel</Application></Properties>')
            else:
                zout.writestr(item, zin.read(item))
        zout.writestr("xl/sharedStrings.xml", "<sst/>")
    assert _xlsx_read_risk(good) is None
    # non-xlsx passes through
    csv = tmp_path / "x.csv"; csv.write_text("a,b")
    assert _xlsx_read_risk(csv) is None
    # garbage zip -> flagged
    bad = tmp_path / "junk.xlsx"; bad.write_bytes(b"not a zip")
    assert "not a valid" in (_xlsx_read_risk(bad) or "")


def test_provider_filename_value_matches_stock_format():
    from grp_mcp.server import _provider_filename_value
    # exact format read from the working stock provider on csmdev
    assert (_provider_filename_value("ACU Import AR Invoices", "ARV3.xlsx")
            == "Data Providers (ACU Import AR Invoices)\\ARV3.xlsx")


def test_import_error_hints_recognize_live_gates():
    from grp_mcp.server import _import_error_hints
    hints = _import_error_hints([
        "Error: Cannot generate the next number for the ARINVOICE sequence.",
        "Error: The posting period 01-2020 is closed.",
    ])
    assert any("NUMBERING" in h for h in hints)
    assert any("PERIOD" in h for h in hints)
    assert _import_error_hints(["all good"]) == []


def test_prepared_data_summary_reads_resolved_keys_and_gates_on_processed():
    from grp_mcp.server import _prepared_data_summary
    # exact shape the SOAP export returns (keyed by resolved field names) — the
    # live "finished but committed nothing" case: 0 processed, 0 errors.
    none_committed = [
        {"Name": "SC", "LineNbr": "1", "IsProcessed": "False", "ErrorMessage": ""},
        {"Name": "SC", "LineNbr": "2", "IsProcessed": "False", "ErrorMessage": ""},
        {"Name": "SC", "LineNbr": "3", "IsProcessed": "False", "ErrorMessage": ""},
    ]
    s = _prepared_data_summary(none_committed)
    assert s["processed"] == 0 and s["errors"] == []
    # all committed
    ok = [{"LineNbr": "1", "IsProcessed": "True", "ErrorMessage": ""},
          {"LineNbr": "2", "IsProcessed": "true", "ErrorMessage": ""}]
    assert _prepared_data_summary(ok)["processed"] == 2
    # one real error, surfaced with line + text (resolved key ErrorMessage)
    mixed = [{"LineNbr": "1", "IsProcessed": "True", "ErrorMessage": ""},
             {"LineNbr": "2", "IsProcessed": "False",
              "ErrorMessage": "Cannot generate the next number for the ARINVOICE sequence."}]
    s = _prepared_data_summary(mixed)
    assert s["processed"] == 1
    assert s["errors"] == [{"line": "2",
                            "error": "Cannot generate the next number for the ARINVOICE sequence."}]
    assert "ARINVOICE" in s["error_texts"][0]


def test_norm_map_row_and_marker_detection():
    from grp_mcp.server import _norm_map_row, _is_marker_field
    # line_break sugar -> ## marker on the detail object, no source
    assert _norm_map_row({"line_break": "Transactions"}) == {
        "target_object": "Transactions", "field": "##"}
    # a plain field row passes through untouched
    fld = {"target_object": "Document", "field": "Customer", "source": "CustomerID", "commit": True}
    assert _norm_map_row(fld) == fld
    # marker detection
    assert _is_marker_field("##")
    assert _is_marker_field("<Save>")
    assert _is_marker_field("<Cancel>")
    assert not _is_marker_field("Country ID")
    assert not _is_marker_field("Customer")


def test_mapping_action_rows_separates_structural_from_fields():
    from grp_mcp.server import _mapping_action_rows
    # structural rows from the real working ARTEST mapping shape
    rows = [
        {"FieldName": "DocType", "Value": "Type"},        # field
        {"FieldName": "@@DocType", "Value": "=[...]"},    # key restriction (structural)
        {"FieldName": "<Cancel>", "Value": None},         # action (structural)
        {"FieldName": "##", "Value": None},               # line marker (structural)
        {"FieldName": "CustomerID", "Value": "CustomerID"},  # field
        {"FieldName": "<Save>", "Value": None},           # commit action (structural)
    ]
    act = _mapping_action_rows(rows)
    assert {r["FieldName"] for r in act} == {"@@DocType", "<Cancel>", "##", "<Save>"}


def test_looks_like_constant_source_flags_the_phantom_column_trap():
    from grp_mcp.server import _looks_like_constant_source
    # bare literals: the AR301000 'BaseQty cannot be empty' cause (Qty="1" -> empty)
    assert _looks_like_constant_source("1")
    assert _looks_like_constant_source("1.0")
    assert _looks_like_constant_source("-5")
    # real column ref / expression / empty: NOT the trap
    assert not _looks_like_constant_source("Quantity")
    assert not _looks_like_constant_source("=IsNull([Quantity], [Transactions.Qty])")
    assert not _looks_like_constant_source("='1'")
    assert not _looks_like_constant_source("")
    assert not _looks_like_constant_source(None)


def test_mapping_column_refs_extracts_source_columns():
    from grp_mcp.server import _mapping_column_refs
    assert _mapping_column_refs("Quantity") == ["Quantity"]
    # IsNull expr: pull the source column, skip the [Object.Field] self-ref (has a dot)
    assert _mapping_column_refs("=IsNull([Quantity], [Transactions.Qty])") == ["Quantity"]
    assert _mapping_column_refs("=IsNull([Unit Price], [Transactions.CuryUnitPrice])") == \
        ["Unit Price"]
    assert _mapping_column_refs("") == []


def test_detail_priming_gaps_flags_missing_field_before_qty():
    from grp_mcp.server import _detail_priming_gaps
    # stock AR-shape: InventoryID precedes Qty on the detail object (Transactions)
    stock = [
        {"LineNbr": 32, "ObjectName": "Document", "FieldName": "DocType", "Value": "Type"},
        {"LineNbr": 320, "ObjectName": "Transactions", "FieldName": "##", "Value": None},
        {"LineNbr": 352, "ObjectName": "Transactions", "FieldName": "InventoryID",
         "Value": "Inventory ID"},
        {"LineNbr": 384, "ObjectName": "Transactions", "FieldName": "Qty",
         "Value": "Quantity"},
        {"LineNbr": 416, "ObjectName": "Transactions", "FieldName": "CuryUnitPrice",
         "Value": "Unit Price"},
    ]
    # candidate omits InventoryID -> flagged
    cand = [
        {"target_object": "Document", "field": "DocType", "source": "Type"},
        {"target_object": "Transactions", "field": "Qty", "source": "Quantity"},
    ]
    gaps = _detail_priming_gaps(cand, stock)
    assert [g["field"] for g in gaps] == ["InventoryID"]
    assert gaps[0]["object"] == "Transactions"
    # candidate that includes it -> no gap
    cand2 = cand + [{"target_object": "Transactions", "field": "InventoryID",
                     "source": "Inventory ID"}]
    assert _detail_priming_gaps(cand2, stock) == []
    # no stock scenario -> no gaps
    assert _detail_priming_gaps(cand, []) == []


def test_detail_object_of_finds_last_line_marker_object():
    from grp_mcp.server import _detail_object_of
    rows = [
        {"ObjectName": "Document", "FieldName": "DocType"},
        {"ObjectName": "Transactions", "FieldName": "##"},
        {"ObjectName": "Transactions", "FieldName": "Qty"},
    ]
    assert _detail_object_of(rows) == "Transactions"
    assert _detail_object_of([{"ObjectName": "Country", "FieldName": "CountryID"}]) is None


def test_split_knowledge_sections_parses_numbered_headings():
    from grp_mcp.server import _split_knowledge_sections
    text = "intro line\n\n## 1. Planes\nbody one\n\n## 2. KB-first\nbody two\nmore\n"
    secs = _split_knowledge_sections(text)
    assert [s["num"] for s in secs] == ["1", "2"]
    assert secs[0]["title"] == "Planes"
    assert secs[0]["heading"] == "1. Planes"
    assert "body one" in secs[0]["body"]
    assert "body two" in secs[1]["body"] and "more" in secs[1]["body"]
    # intro before the first numbered heading is dropped
    assert "intro line" not in secs[0]["body"]


def test_advertised_tool_count_stays_current():
    # the "~NN tools" strings in the server instructions + guide() must track reality so a
    # fresh AI is told the right scale (they silently drifted 77 -> 95 once). Small slack.
    import inspect
    import re as _re
    actual = len(asyncio.run(server.mcp.list_tools()))
    advertised = [int(n) for n in _re.findall(r"~(\d+)\s+tools", inspect.getsource(server))]
    assert advertised, "expected at least one '~NN tools' advertised count in server.py"
    for n in advertised:
        assert abs(n - actual) <= 8, (
            f"advertised ~{n} tools but there are {actual} — update the count string(s)")


def test_knowledge_tool_serves_toc_and_sections():
    from grp_mcp import server
    toc = server.knowledge()
    assert toc["table_of_contents"], "expected a non-empty table of contents"
    # keyword lookup returns one section's content
    mig = server.knowledge("migration")
    assert "content" in mig and "Provider" in mig["content"]
    # exact section number works
    assert "content" in server.knowledge("1")
    # whole doc
    assert "content" in server.knowledge("all")
    # bogus section reports the TOC back
    assert "table_of_contents" in server.knowledge("no-such-section-xyz")


def test_endpoint_top_level_entities_parses_path_tree():
    from grp_mcp.server import _endpoint_top_level_entities
    tree = [
        {"Path": {"value": "Endpoint"}, "Text": {"value": "Endpoint"}},          # root -> skip
        {"Path": {"value": "Endpoint/Account"}, "Text": {"value": "Account"}},   # top-level
        {"Path": {"value": "Endpoint/SalesOrder"}, "Text": {"value": "SalesOrder"}},
        {"Path": {"value": "Endpoint/SalesOrder/Details"}, "Text": {"value": "Details"}},  # nested -> skip
        {"Path": {"value": "Endpoint/PayGroup"}, "Text": {"value": "PayGroup ↓"}},  # inherited: strip arrow
    ]
    assert _endpoint_top_level_entities(tree) == ["Account", "PayGroup", "SalesOrder"]
    # legacy tree shape fallback
    legacy = [{"ObjectName": {"value": "PayCode"}}, {"Name": "Ledger"}]
    assert _endpoint_top_level_entities(legacy) == ["Ledger", "PayCode"]
    assert _endpoint_top_level_entities(None) == []


def test_topo_order_linear_and_cycle():
    from grp_mcp.server import _topo_order
    # C depends on B depends on A  -> A, B, C
    order, cyclic = _topo_order(["A", "B", "C"], {"B": {"A"}, "C": {"B"}})
    assert order == ["A", "B", "C"] and cyclic == []
    # independent nodes -> sorted, no cycle
    order, cyclic = _topo_order(["X", "Y"], {})
    assert order == ["X", "Y"] and cyclic == []
    # a 2-cycle is reported, not ordered
    order, cyclic = _topo_order(["P", "Q"], {"P": {"Q"}, "Q": {"P"}})
    assert set(cyclic) == {"P", "Q"}


def test_selector_value_hint_matches_cannot_be_found():
    from grp_mcp.screen import _selector_value_hint
    hint = _selector_value_hint("'Employee Bank' cannot be found in the system.")
    assert hint is not None
    # Must name BOTH causes — an earlier version led with SubstituteKey alone,
    # which mis-diagnosed the commoner "value simply doesn't exist" case
    # (caught by cross-screen testing: GL301000 account "ZZZ999").
    assert "does not exist" in hint          # cause 1, listed first
    assert "SubstituteKey" in hint           # cause 2
    assert "value_field" in hint             # how to find the right value
    assert hint.index("does not exist") < hint.index("SubstituteKey")
    # case-insensitive
    assert _selector_value_hint("X CANNOT BE FOUND IN THE SYSTEM") is not None


def test_selector_value_hint_ignores_other_errors():
    from grp_mcp.screen import _selector_value_hint
    assert _selector_value_hint("Percent should be 100 for sum of all banks") is None
    assert _selector_value_hint("'Employee Bank' cannot be empty.") is None
    assert _selector_value_hint(None) is None
    assert _selector_value_hint("") is None


def test_parse_field_errors_attaches_selector_hint():
    # A "cannot be found in the system" field error gets an actionable hint;
    # an unrelated field error does not.
    xml = (
        "<Content><Value>"
        "<FieldName>EmployeeBankID</FieldName><ObjectName>CSPYEmployeeBankDetail</ObjectName>"
        "<Message>'Employee Bank' cannot be found in the system.</Message>"
        "<IsError>true</IsError></Value>"
        "<Value><FieldName>Percent</FieldName>"
        "<Message>'Percent' cannot be empty.</Message><IsError>true</IsError></Value>"
        "</Content>")
    errs = ScreenClient._parse_field_errors(xml)
    by_field = {e["field"]: e for e in errs}
    assert "hint" in by_field["EmployeeBankID"]
    assert "SubstituteKey" in by_field["EmployeeBankID"]["hint"]
    assert "hint" not in by_field["Percent"]


def test_leaf_class_name():
    from grp_mcp.screen import _leaf
    assert _leaf("Payroll.Graph.Entry.CSPYOvertimeRate") == "cspyovertimerate"
    assert _leaf("PX.Api.ContractBased.UI.EntityConfigurationMaint+EntityDescriptionInsertModel") == "entitydescriptioninsertmodel"
    assert _leaf("Flat") == "flat"
    assert _leaf(None) is None
    assert _leaf("") is None


def _tree_client(actions):
    """A ScreenClient stub with just enough for ui_select_tree_node's preflight.

    The guard has to run BEFORE any network call, so the stub deliberately has no
    transport: if the refusal path ever regresses into posting first, these tests
    fail with AttributeError rather than passing quietly.
    """
    import asyncio
    from grp_mcp.screen import ScreenClient

    c = object.__new__(ScreenClient)
    c.screen_id = "EP205015"
    struct = {"actions": [{"name": a} for a in actions],
              "views": {"NodesTree": [], "CurrentNode": []}, "grids": {}}

    async def _struct():
        return struct
    c.get_ui_structure = _struct
    return c, asyncio


def test_select_tree_node_refuses_command_the_screen_does_not_have():
    # EP205015 has no "EnablePopulate" (that is SM207060's handler). Sending it
    # anyway selected nothing and the next write hit the CURRENT node and COMMITTED
    # — ok:true, wrong row. Refuse before the post, not after the DB read-back.
    import pytest
    from grp_mcp.screen import ScreenError
    c, aio = _tree_client(["Save", "Cancel", "AddStep", "AddRule", "DeleteRoute"])
    with pytest.raises(ScreenError) as e:
        aio.run(c.ui_select_tree_node("NodesTree", {"RuleID": "fb884e75"}))
    msg = str(e.value)
    assert "EnablePopulate" in msg
    assert "CURRENT node" in msg          # says WHY silence is dangerous
    assert "AddStep" in msg               # lists what this screen does have
    assert "aspx_tree_node_action" in msg  # points at the plane that can do it


def test_select_tree_node_allows_a_command_the_screen_does_have():
    # SM207060 really does expose EnablePopulate — the guard must not break it.
    # It gets past the preflight and only then needs transport (which the stub
    # lacks), so reaching AttributeError proves the guard let it through.
    import pytest
    c, aio = _tree_client(["Save", "InsertNew", "EnablePopulate"])
    with pytest.raises(AttributeError):
        aio.run(c.ui_select_tree_node("EntityTree", {"Key": "ROOT#GRPMCP"}))


def test_tree_active_row_context_root_node():
    from grp_mcp.screen import _tree_active_row_context
    ctx = _tree_active_row_context("EntityTree", {"Key": "ROOT#GRPMCP"}, None)
    assert ctx == {
        "dataView": "EntityTree", "syncPosition": True,
        "dataKey": {"Key": "ROOT#GRPMCP"},
        "selectedNodeParentId": None, "resultType": "TreeActiveDataRow",
    }


def test_tree_active_row_context_child_node():
    from grp_mcp.screen import _tree_active_row_context
    ctx = _tree_active_row_context(
        "EntityTree", {"Key": "ENT#Companies"}, [{"Key": "ROOT#GRPMCP"}])
    assert ctx["selectedNodeParentId"] == "ROOT#GRPMCP"


def test_tree_active_row_context_detail_node_uses_immediate_parent():
    from grp_mcp.screen import _tree_active_row_context
    # depth-2 detail node: ancestors root -> entity; parent is the entity
    ctx = _tree_active_row_context(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690/1/2691"},
        [{"Key": "ROOT#GRPMCP"}, {"Key": "GRPMCP/25.200.001#E/2690"}])
    assert ctx["selectedNodeParentId"] == "GRPMCP/25.200.001#E/2690"


def test_tree_control_block_root_parameters():
    from grp_mcp.screen import _tree_control_block
    block = _tree_control_block(
        "EntityTree", {"Key": "ROOT#GRPMCP"}, None,
        columns=["Key", "Title", "Icon", "IconColor"], key_fields=["Key"])
    assert block["columns"] == ["Key", "Title", "Icon", "IconColor"]
    assert block["treeKeys"] == ["Key"]
    assert block["parameters"] == ["ROOT#GRPMCP", None, "ROOT#GRPMCP"]
    assert block["selectedNodeParentId"] is None


def test_tree_control_block_entity_parameters_full_path():
    from grp_mcp.screen import _tree_control_block
    # depth-1 entity: [root, None, entity] (matches the live browser payload)
    block = _tree_control_block(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690"}, [{"Key": "ROOT#GRPMCP"}],
        columns=["Key"], key_fields=["Key"])
    assert block["parameters"] == ["ROOT#GRPMCP", None, "GRPMCP/25.200.001#E/2690"]
    assert block["selectedNodeParentId"] == "ROOT#GRPMCP"


def test_tree_control_block_detail_parameters_full_path():
    from grp_mcp.screen import _tree_control_block
    # depth-2 detail: [root, entity, None, detail] — the whole chain, not just parent
    block = _tree_control_block(
        "EntityTree", {"Key": "GRPMCP/25.200.001#E/2690/1/2691"},
        [{"Key": "ROOT#GRPMCP"}, {"Key": "GRPMCP/25.200.001#E/2690"}],
        columns=["Key"], key_fields=["Key"])
    assert block["parameters"] == [
        "ROOT#GRPMCP", "GRPMCP/25.200.001#E/2690", None,
        "GRPMCP/25.200.001#E/2690/1/2691"]
    assert block["selectedNodeParentId"] == "GRPMCP/25.200.001#E/2690"


def test_tree_control_block_falls_back_to_key_field_when_metadata_missing():
    from grp_mcp.screen import _tree_control_block
    block = _tree_control_block(
        "EntityTree", {"Key": "ROOT#GRPMCP"}, None, columns=None, key_fields=None)
    assert block["columns"] == []
    assert block["treeKeys"] == ["Key"]


def test_tree_context_views_picks_selected_star_only():
    from grp_mcp.screen import _tree_context_views
    views = ["Endpoint", "SelectedEndpoint", "SelectedEntity", "SelectedAction",
             "EntityTree", "CreateEntityView"]
    ctx_views = _tree_context_views(views, "EntityTree", {"Key": "ROOT#GRPMCP"})
    assert ctx_views == {
        "SelectedEndpoint": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedEntity": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedAction": {"parameters": {"Key": "ROOT#GRPMCP"}},
    }


def test_tree_context_views_excludes_the_tree_itself():
    from grp_mcp.screen import _tree_context_views
    # a pathological tree view name starting with "Selected" must not self-select
    ctx_views = _tree_context_views(
        ["SelectedTree"], "SelectedTree", {"Key": "X"})
    assert ctx_views == {}


def test_ui_post_auto_attaches_active_tree_row_and_controls():
    """_ui_post must merge the cached tree selection into every later call unless
    the caller already named that dataView/view — this is what lets InsertNew
    (fired via plain ui_command) see the node ui_select_tree_node selected."""
    s = _client("SM207060")
    s._ui_booted = True  # skip network bootstrap
    ctx = {"dataView": "EntityTree", "syncPosition": True,
           "dataKey": {"Key": "ROOT#GRPMCP"}, "selectedNodeParentId": None,
           "resultType": "TreeActiveDataRow"}
    block = {"view": "EntityTree", "dataKey": {"Key": "ROOT#GRPMCP"}}
    s._active_tree_row = ctx
    s._active_tree_controls = {"EntityTree": block}

    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    asyncio.run(s._ui_post({"command": [{"name": "InsertNew"}], "data": [],
                             "controlsParams": {}, "activeRowContexts": [],
                             "viewsParams": {}}))
    assert captured["activeRowContexts"] == [ctx]
    assert captured["controlsParams"]["EntityTree"] == block


def test_ui_post_does_not_override_caller_supplied_tree_context():
    """If the caller already names the SAME dataView explicitly, don't stomp it."""
    s = _client("SM207060")
    s._ui_booted = True
    s._active_tree_row = {"dataView": "EntityTree", "dataKey": {"Key": "ROOT#GRPMCP"}}
    s._active_tree_controls = {"EntityTree": {"view": "EntityTree"}}

    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    caller_ctx = [{"dataView": "EntityTree", "dataKey": {"Key": "ENT#Companies"}}]
    asyncio.run(s._ui_post({"activeRowContexts": caller_ctx, "controlsParams": {}}))
    assert captured["activeRowContexts"] == caller_ctx


def test_ui_post_auto_attaches_tree_context_views():
    """The Selected* context viewsParams must ride on every later command too — this
    is what lets a dialog commit STAGE the node (without them the trailing Save
    persists an empty graph). Proven load-bearing live (SM207060, 2026-07-02)."""
    s = _client("SM207060")
    s._ui_booted = True
    s._active_tree_context_views = {
        "SelectedEndpoint": {"parameters": {"Key": "ROOT#GRPMCP"}},
        "SelectedEntity": {"parameters": {"Key": "ROOT#GRPMCP"}},
    }
    captured = {}

    async def fake_post(url, json, headers):
        captured.update(json)
        return _Resp(200, {})

    s._http.post = fake_post
    asyncio.run(s._ui_post({"command": [{"name": "Save"}], "viewsParams": {}}))
    assert captured["viewsParams"]["SelectedEndpoint"] == {"parameters": {"Key": "ROOT#GRPMCP"}}
    assert captured["viewsParams"]["SelectedEntity"] == {"parameters": {"Key": "ROOT#GRPMCP"}}


# ---- selector (lookup) field resolution (ui_resolve_selector) ---------------
# A selector field's /structure fieldState carries everything to query its own
# grid sub-endpoint — no browser capture per field. Lock the extraction + payload.

def test_selector_meta_extracts_graph_from_field_dac_name():
    from grp_mcp.screen import _selector_meta
    # the real SM207060 CreateEntityView.ScreenID shape (trimmed)
    st = {"selectorMode": 33, "viewName": "_EntityDescriptionInsertModelScreenID_X",
          "fieldDacName": "PX.Api.ContractBased.UI.EntityConfigurationMaint+EntityDescriptionInsertModel",
          "valueField": "screenID", "descriptionName": "title",
          "fieldList": ["title", "screenID"], "headerList": ["Title", "Screen ID"]}
    sel = _selector_meta(st)
    assert sel["graph"] == "PX.Api.ContractBased.UI.EntityConfigurationMaint"
    assert sel["value_field"] == "screenID"
    assert sel["search_field"] == "title"
    assert sel["columns"] == ["title", "screenID"]


def test_selector_meta_none_for_non_selector():
    from grp_mcp.screen import _selector_meta
    assert _selector_meta({"typeName": "String"}) is None
    assert _selector_meta({"selectorMode": 0}) is None


def test_selector_grid_payload_shape():
    from grp_mcp.screen import _selector_grid_payload
    sel = {"view": "_V", "graph": "PX.Foo", "value_field": "screenID",
           "search_field": "title", "columns": ["title", "screenID"], "headers": []}
    p = _selector_grid_payload(sel, "ScreenID", "CreateEntityView", "Companies")
    assert p["view"] == "_V"
    assert p["graph"] == "PX.Foo"
    assert p["dataField"] == "ScreenID"
    assert p["dataView"] == "CreateEntityView"
    assert p["fastFilter"] == "Companies"
    assert p["searchField"] == "title"
    assert p["columns"] == [{"field": "title"}, {"field": "screenID"}]
    assert "activeRowContexts" not in p  # omitted when none passed


def test_selector_grid_payload_includes_active_row_contexts_when_given():
    from grp_mcp.screen import _selector_grid_payload
    sel = {"view": "_V", "graph": "g", "value_field": "v", "search_field": "s",
           "columns": ["s"], "headers": []}
    arc = [{"dataView": "EntityTree", "dataKey": {"Key": "ROOT#X"}}]
    p = _selector_grid_payload(sel, "F", "DV", "q", arc)
    assert p["activeRowContexts"] == arc


def test_selector_meta_falls_back_columns_when_field_list_absent():
    """Some selectors (SM207060 PopulateFilterView.Container) omit fieldList AND
    fieldDacName — columns must fall back to [valueField, descriptionName], else the
    grid query returns unfiltered/unreadable rows (proven live: 8 empty {} rows)."""
    from grp_mcp.screen import _selector_meta
    st = {"selectorMode": 16, "viewName": "_PopulateFilterContainer_X",
          "valueField": "mappedObject", "descriptionName": "displayName"}
    sel = _selector_meta(st)
    assert sel["graph"] is None                       # no fieldDacName -> caller fills it
    assert sel["columns"] == ["mappedObject", "displayName"]
    assert sel["value_field"] == "mappedObject"
    assert sel["search_field"] == "displayName"


def test_tree_row_by_title_matches_and_strips_inheritance_marker():
    s = _client("SM207060")
    resp = {"controlsData": {"EntityTree": {"rows": [
        {"cells": {"Title": {"value": "Endpoint"}, "Key": {"value": "ROOT#GRPMCP"}}},
        {"cells": {"Title": {"value": "DataProvider"}, "Key": {"value": "GRPMCP/25.200.001#E/2692"}}},
        {"cells": {"Title": {"value": "Account ↓"}, "Key": {"value": "Default/24.200.001#E/12887"}}},
    ]}}}
    assert s._tree_row_by_title(resp, "EntityTree", "DataProvider") == {"Key": "GRPMCP/25.200.001#E/2692"}
    # inherited node ('Account ↓') matched by its bare title
    assert s._tree_row_by_title(resp, "EntityTree", "Account") == {"Key": "Default/24.200.001#E/12887"}
    assert s._tree_row_by_title(resp, "EntityTree", "Nonexistent") is None


def test_same_origin_falls_back_to_referer_when_origin_absent():
    from grp_mcp.ui import _is_same_origin
    assert _is_same_origin(
        {"Referer": "http://127.0.0.1:8765/index.html"}, "http://127.0.0.1:8765"
    )


def test_same_origin_rejects_mismatched_referer():
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({"Referer": "https://evil.com/"}, "http://127.0.0.1:8765")


def test_same_origin_rejects_no_headers_at_all():
    # e.g. a bare curl/script with no browser-supplied Origin or Referer — secure
    # default is to reject, not to assume good faith just because both are absent.
    from grp_mcp.ui import _is_same_origin
    assert not _is_same_origin({}, "http://127.0.0.1:8765")


# ---- snapshot_entity write_roots enforcement (default path) -----------------
# The default (no `path` given) snapshot destination used to skip _check_write_path
# entirely — only a caller-SUPPLIED path was fenced. Verify the auto-computed
# default now goes through the same check, by configuring write_roots to a
# directory that can't contain it and confirming a PermissionError fires before
# any network call (proves the check now runs first, not after the fetch).

def test_snapshot_entity_default_path_respects_write_roots(monkeypatch):
    c = Config(
        default="restricted",
        instances={"restricted": _inst(write_roots=["C:/nowhere-real-should-not-match"])},
    )
    monkeypatch.setattr(server, "_config", c)
    monkeypatch.delenv("GRP_MCP_CONNECTIONS", raising=False)
    with pytest.raises(PermissionError):
        asyncio.run(server.snapshot_entity("SomeEntity", instance="restricted"))


# ---- ui_grid_row_action: select an existing grid row, then fire an action ----
# The capability the classic SOAP plane structurally lacks (it cannot address an
# existing grid row by key). Verify the row is made active via activeRowContexts
# (GridActiveDataRow with the row key), the action fires, and a 302 openDialog is
# auto-answered with a dialogCallback OK (dialogResult:1).

def test_ui_grid_row_action_selects_row_and_answers_dialog():
    s = _client("SM203520")
    s._ui_booted = True  # skip the bootstrap network round-trip
    calls = []

    async def fake_post(url, json, headers=None):  # noqa: A002
        calls.append(json)
        # first command call -> a confirmation dialog would open (302)
        if json.get("command") and "dialogCallback" not in json:
            return _Resp(302, {"redirects": [
                {"settings": {"type": "openDialog", "viewName": "SnapshotsHistory"}}]})
        return _Resp(200, {"graphIsDirty": False, "messages": []})

    s._http.post = fake_post
    res = asyncio.run(s.ui_grid_row_action(
        "Snapshots", {"SnapshotID": "abc"}, "importSnapshotCommand"))
    fires = [c for c in calls if c.get("command")]
    # the action fired with the row active as a GridActiveDataRow
    arc = fires[0]["activeRowContexts"]
    assert arc == [{"dataView": "Snapshots", "syncPosition": True,
                    "dataKey": {"SnapshotID": "abc"}, "resultType": "GridActiveDataRow"}]
    # the 302 openDialog was answered OK on the follow-up call, row still active
    assert fires[-1]["dialogCallback"] == {
        "dialogResult": 1, "validateInput": False, "viewName": "SnapshotsHistory"}
    assert fires[-1]["activeRowContexts"] == arc
    assert res["ok"] and res["status"] == "committed"


def test_ui_grid_row_action_confirm_false_leaves_dialog_open():
    """confirm=False must NOT answer the dialog — a safe arm-without-firing for a
    destructive action."""
    s = _client("SM203520")
    s._ui_booted = True
    calls = []

    async def fake_post(url, json, headers=None):  # noqa: A002
        calls.append(json)
        if json.get("command"):
            return _Resp(302, {"redirects": [
                {"settings": {"type": "openDialog", "viewName": "SnapshotsHistory"}}]})
        return _Resp(200, {})

    s._http.post = fake_post
    res = asyncio.run(s.ui_grid_row_action(
        "Snapshots", {"SnapshotID": "abc"}, "importSnapshotCommand", confirm=False))
    assert res["status"] == "dialog_open"
    assert not any("dialogCallback" in c for c in calls)  # never committed


# ---- bearer-token URL guard: origin AND base-path scoped --------------------
# The token must never ride to a same-host but different-app URL (poll_action /
# download take caller-supplied URLs). base_url "https://host/2026R1" -> only
# paths under /2026R1 are allowed; a bare-root base falls back to origin-only.

def _acu(base_url):
    return AcumaticaClient(_inst(base_url=base_url))


def test_token_guard_allows_url_under_base_path():
    c = _acu("https://host/2026R1")
    c._assert_allowed_url("https://host/2026R1/entity/Default/24.200.001/SalesOrder")
    c._assert_allowed_url("https://host/2026R1/t/Company/api/odata/dac/Account")
    c._assert_allowed_url("https://host/2026R1")  # the prefix itself


def test_token_guard_blocks_different_origin():
    c = _acu("https://host/2026R1")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://evil.example.com/2026R1/entity/x")


def test_token_guard_blocks_same_host_different_app_path():
    c = _acu("https://host/2026R1")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://host/OtherApp/entity/x")
    # a path that merely shares the prefix as a substring, not a segment, is blocked
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://host/2026R1Evil/x")


def test_token_guard_root_hosted_base_is_origin_only():
    c = _acu("https://host")  # site at domain root -> no path to scope to
    c._assert_allowed_url("https://host/entity/Default/24.200.001/SalesOrder")
    c._assert_allowed_url("https://host/anything/at/all")
    with pytest.raises(AcumaticaError):
        c._assert_allowed_url("https://elsewhere/x")


def test_extend_endpoint_is_not_a_registered_tool():
    # deregistered on purpose: a REST PUT to WebServiceEndpoints is a verified no-op,
    # so exposing it as a tool only invited a silent do-nothing "success".
    async def _names():
        return {t.name for t in await server.mcp.list_tools()}
    names = asyncio.run(_names())
    assert "extend_endpoint" not in names
    assert "ui_tree_dialog_insert" in names  # the working replacement


# ---- admin gate: persisting config mutations need GRP_MCP_ALLOW_ADMIN --------

def test_admin_gate_blocks_persist_without_env(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.set_active_instance("rw", persist=True)


def test_admin_gate_allows_session_only_switch(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    # persist=false touches no file -> ungated
    out = server.set_active_instance("rw", persist=False)
    assert out["active"] == "rw" and out["persisted"] is False


def test_admin_gate_opens_with_env(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("GRP_MCP_ALLOW_ADMIN", "1")
    monkeypatch.setenv("GRP_MCP_CONNECTIONS", str(tmp_path / "connections.json"))
    out = server.set_active_instance("rw", persist=True)
    assert out["active"] == "rw" and out["persisted"] is True


def test_admin_gate_remove_instance_persist_blocked(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.remove_instance("rw", persist=True)


# ---- fs sandbox status is honest about "empty roots = unrestricted" ----------

def test_fs_sandbox_unrestricted_when_empty():
    s = _inst().fs_sandbox("write")
    assert "UNRESTRICTED" in s and "write_roots" in s


def test_fs_sandbox_restricted_when_set():
    s = _inst(read_roots=["C:/data"]).fs_sandbox("read")
    assert "restricted to" in s and "C:/data" in s


# ---- publish job view / status (non-blocking publish) -----------------------

def test_publish_job_view_in_progress_vs_completed_vs_error():
    base = {"job": "grp_mcp", "project_names": ["grp_mcp"], "completed": False,
            "failed": None, "result": None, "error": None}
    v = server._publish_job_view(dict(base))
    assert v["status"] == "in_progress" and v["completed"] is False and v["note"]
    done = server._publish_job_view({**base, "completed": True, "failed": False,
                                     "result": {"isCompleted": True}})
    assert done["status"] == "completed" and done["note"] is None
    err = server._publish_job_view({**base, "error": "boom"})
    assert err["status"] == "error" and err["note"] is None


def test_publish_status_reads_module_state(monkeypatch):
    monkeypatch.setattr(server, "_publish_jobs", {
        "a": {"job": "a", "project_names": ["a"], "completed": True, "failed": False,
              "result": {}, "error": None},
        "b": {"job": "b", "project_names": ["b"], "completed": False, "failed": None,
              "result": None, "error": None},
    })
    # explicit job
    assert asyncio.run(server.publish_status("a"))["status"] == "completed"
    # default = most recently inserted
    assert asyncio.run(server.publish_status())["job"] == "b"
    # unknown
    assert asyncio.run(server.publish_status("zzz"))["status"] == "unknown"


# ---- v0.35: endpoint override, publish phases, timeout wrapping -------------

def test_client_endpoint_override(cfg, monkeypatch):
    monkeypatch.setattr(server, "_clients", {})
    default = server._client("ro")
    assert default.instance.endpoint_name == "Default"
    over = server._client("ro", "grp_mcp/25.200.001")
    assert over.instance.endpoint_name == "grp_mcp"
    assert over.instance.endpoint_version == "25.200.001"
    assert over.instance.entity_base.endswith("/entity/grp_mcp/25.200.001")
    # distinct cache slots; base client untouched
    assert over is not default and server._client("ro") is default
    assert server._client("ro", "grp_mcp/25.200.001") is over


def test_client_endpoint_override_bad_format(cfg, monkeypatch):
    monkeypatch.setattr(server, "_clients", {})
    with pytest.raises(ValueError):
        server._client("ro", "grp_mcp")  # missing /Version
    with pytest.raises(ValueError):
        server._client("ro", "/25.200.001")  # missing name


def test_publish_job_view_phases():
    base = {"job": "j", "project_names": ["j"], "completed": False,
            "failed": None, "result": None, "error": None}
    begin = server._publish_job_view({**base, "phase": "begin"})
    assert begin["status"] == "in_progress" and begin["phase"] == "begin"
    assert "publishBegin" in begin["note"]
    pub = server._publish_job_view({**base, "phase": "publishing"})
    assert pub["phase"] == "publishing" and "recompile" in pub["note"]
    # begin failure surfaces as error via the job (the v0.35 fix)
    err = server._publish_job_view({**base, "phase": "begin", "error": "login failed (401)"})
    assert err["status"] == "error" and err["error"].startswith("login failed")


def test_request_raw_wraps_timeout_with_explicit_message():
    import httpx

    cli = AcumaticaClient(_inst())

    async def fake_auth_header():
        return {"Authorization": "Bearer x"}

    async def boom(*a, **k):
        raise httpx.ReadTimeout("")  # httpx timeouts often stringify to ""

    cli._auth_header = fake_auth_header
    cli._http.request = boom
    with pytest.raises(AcumaticaError) as ei:
        asyncio.run(cli._request_raw("GET", "https://host/Site/entity/x"))
    msg = str(ei.value)
    assert "TIMED OUT" in msg and "ReadTimeout" in msg and "cold IIS" in msg


def test_default_http_timeout_is_120():
    cli = AcumaticaClient(_inst())
    assert cli._http.timeout.read == 120.0
    assert cli._http.timeout.connect == 30.0


# ---- v0.36: submit-path validation, filter conditions, fault boundary ------

from grp_mcp.screen import _normalize_condition


def test_filter_condition_aliases():
    assert _normalize_condition({"field": "A", "value": "1", "op": ">="}) == "GreaterOrEqual"
    assert _normalize_condition({"field": "A", "value": "1", "op": ">"}) == "Greater"
    assert _normalize_condition({"field": "A", "value": "1", "op": "!="}) == "NotEqual"
    # canonical names pass through; alias is case-insensitive
    assert _normalize_condition({"field": "A", "value": "1", "op": "STARTSWITH"}) == "StartsWith"
    # default when neither given
    assert _normalize_condition({"field": "A", "value": "1"}) == "Equals"


def test_filter_rejects_unknown_condition_and_keys():
    with pytest.raises(ValueError):
        _normalize_condition({"field": "A", "value": "1", "op": "~="})   # bad operator
    with pytest.raises(ValueError):
        _normalize_condition({"field": "A", "value": "1", "foo": "bar"})  # unknown key


def test_filter_rejects_contains_with_working_alternatives():
    # bug report 2026-07-10 #5 (reproduced live): the SOAP FilterCondition enum has
    # no 'Contains' — the server 500s on it. Refuse client-side, point at what works.
    for spec in ({"condition": "Contains"}, {"op": "contains"}, {"op": "CONTAINS"}):
        with pytest.raises(ValueError) as e:
            _normalize_condition({"field": "A", "value": "1", **spec})
        msg = str(e.value)
        assert "Contains" in msg and "StartsWith" in msg and "run_dac_odata" in msg


def test_fault_boundary_keeps_full_message():
    # the #4 truncation: " at least one error" must NOT be treated as a stack frame
    import re
    msg = ("PX.Data.PXException: Error: Inserting 'Numbering Sequence' record raised "
           "at least one error. Please review the errors. at PX.Data.PXCache.Insert(Object)")
    m = re.search(r"PX\.\w[\w.]*Exception: (.+?)(?: at [A-Z][\w.]*[.(]|---|\Z)", msg)
    got = m.group(1).strip()
    assert "at least one error" in got and "Please review the errors" in got
    assert "PXCache.Insert" not in got  # stack frame stripped


def _screen_stub():
    from grp_mcp.screen import ScreenClient
    return ScreenClient(_inst(), "GL102000")


def test_validate_sets_flags_readonly_and_bad_enum(monkeypatch):
    import xml.etree.ElementTree as ET
    s = _screen_stub()
    # fake modern-plane metadata
    s._ui_meta = {
        ("GLSetupRecord", "TrialBalanceSign"): {
            "readonly": False, "enabled": True,
            "options": [{"value": "N", "text": "Normal"}, {"value": "R", "text": "Reversed"}]},
        ("GLSetupRecord", "AllocationNumberingID"): {
            "readonly": True, "enabled": False, "options": None},
    }

    def fake_find_field(name):
        fld = {"SignOfTheTrialBalance": ("TrialBalanceSign", "GLSetupRecord"),
               "AllocationNumberingSequence": ("AllocationNumberingID", "GLSetupRecord")}[name]
        el = ET.Element("x")
        ET.SubElement(el, "FieldName").text = fld[0]
        ET.SubElement(el, "ObjectName").text = fld[1]
        return el

    s._find_field = fake_find_field
    issues = asyncio.run(s._validate_sets([
        {"set": "AllocationNumberingSequence", "to": "ALLOCATION"},   # read-only
        {"set": "SignOfTheTrialBalance", "to": "Disbursement"},        # invalid enum
        {"set": "SignOfTheTrialBalance", "to": "R"},                   # valid -> no issue
    ]))
    flagged = {i["field"] for i in issues}
    assert flagged == {"AllocationNumberingSequence", "SignOfTheTrialBalance"}
    assert len(issues) == 2
    enum_issue = next(i for i in issues if i["field"] == "SignOfTheTrialBalance")
    assert "allowed" in enum_issue


def test_validate_sets_noop_without_metadata():
    s = _screen_stub()
    s._ui_meta = {}   # modern plane unreachable -> never block
    assert asyncio.run(s._validate_sets([{"set": "Whatever", "to": "x"}])) == []


# ---- v0.37: modern-plane write safety (coerce/validate, grid columns) -------

def _screen_ui(meta):
    from grp_mcp.screen import ScreenClient
    s = ScreenClient(_inst(), "GL102000")

    async def fake_meta():
        return meta
    s._ui_field_meta = fake_meta
    return s


def test_ui_coerce_validate_label_to_value_and_view_resolution():
    meta = {("GLSetupRecord", "TrialBalanceSign"): {
        "readonly": False, "enabled": True,
        "options": [{"value": "N", "text": "Normal"}, {"value": "R", "text": "Reversed"}]}}
    s = _screen_ui(meta)
    # label -> value coercion, and view omitted (resolved by unique field name)
    norm, issues, notes = asyncio.run(s.ui_coerce_validate(
        [{"field": "TrialBalanceSign", "value": "Reversed"}]))
    assert not issues
    assert norm[0] == {"view": "GLSetupRecord", "field": "TrialBalanceSign", "value": "R"}
    assert any("coerced" in n for n in notes)


def test_ui_coerce_validate_flags_readonly_and_bad_enum():
    meta = {
        ("V", "RO"): {"readonly": True, "enabled": False, "options": None},
        ("V", "E"): {"readonly": False, "enabled": True,
                     "options": [{"value": "A", "text": "Apple"}]},
    }
    s = _screen_ui(meta)
    _, issues, _ = asyncio.run(s.ui_coerce_validate([
        {"view": "V", "field": "RO", "value": "x"},
        {"view": "V", "field": "E", "value": "Zebra"},
    ]))
    probs = {i["field"]: i for i in issues}
    assert "read-only" in probs["V.RO"]["problem"]
    assert "allowed" in probs["V.E"]


def test_ui_coerce_validate_ambiguous_field():
    meta = {("V1", "Dup"): {"readonly": False, "enabled": True, "options": None},
            ("V2", "Dup"): {"readonly": False, "enabled": True, "options": None}}
    s = _screen_ui(meta)
    _, issues, _ = asyncio.run(s.ui_coerce_validate([{"field": "Dup", "value": "1"}]))
    assert issues and "ambiguous" in issues[0]["problem"]


def test_ui_coerce_validate_passthrough_when_no_meta():
    s = _screen_ui({})
    norm, issues, notes = asyncio.run(s.ui_coerce_validate(
        [{"view": "V", "field": "F", "value": "x"}]))
    assert issues == [] and norm[0]["value"] == "x"




# ---- v0.38: grid-cell write validation (realistic column fixtures) ----------

def _grid_client():
    from grp_mcp.screen import ScreenClient
    return ScreenClient(_inst(), "GL202500")


# real GL202500 AccountRecords column shape (from a live grid read)
_GL_COLS = [
    {"field": "AccountID", "allowUpdate": False, "dataType": 9},
    {"field": "AccountCD", "allowUpdate": True, "dataType": 18},
    {"field": "Description", "allowUpdate": True, "dataType": 18},
    {"field": "Type", "allowUpdate": True,
     "valueItems": {"items": [{"value": "A", "text": "Asset"}, {"value": "L", "text": "Liability"},
                              {"value": "I", "text": "Income"}, {"value": "E", "text": "Expense"}]}},
    {"field": "PostOption", "allowUpdate": True,
     "valueItems": {"items": [{"value": "S", "text": "Summary"}, {"value": "D", "text": "Detail"}]}},
    {"key": "Files", "allowUpdate": False},  # meta column, no `field`
]


def test_parse_grid_cols():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    assert set(meta) == {"AccountID", "AccountCD", "Description", "Type", "PostOption"}
    assert meta["AccountID"]["readonly"] is True
    assert meta["AccountCD"]["readonly"] is False
    assert [o["value"] for o in meta["Type"]["options"]] == ["A", "L", "I", "E"]
    assert meta["Description"]["options"] is None


def test_grid_validate_coerce_readonly_enum_and_label():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    # label -> value coercion (Expense -> E, Summary -> S), read-only + bad enum flagged
    coerced, issues = s._grid_validate_coerce(meta, {
        "AccountCD": "60000",            # editable, no enum -> untouched
        "Type": "Expense",               # label -> "E"
        "PostOption": "S",               # already a value -> kept
        "AccountID": "999",              # read-only -> issue
        "Description": "Ok",             # editable free text
    })
    assert coerced["Type"] == "E"
    assert coerced["AccountCD"] == "60000"
    probs = {i["field"] for i in issues}
    assert probs == {"AccountID"}


def test_grid_validate_coerce_bad_enum_lists_allowed():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    _, issues = s._grid_validate_coerce(meta, {"Type": "Widget"})
    assert len(issues) == 1 and issues[0]["field"] == "Type"
    assert [o["value"] for o in issues[0]["allowed"]] == ["A", "L", "I", "E"]


def test_grid_validate_coerce_unknown_column_passes_through():
    s = _grid_client()
    meta = s._parse_grid_cols(_GL_COLS)
    coerced, issues = s._grid_validate_coerce(meta, {"Nonexistent": "x"})
    assert issues == [] and coerced["Nonexistent"] == "x"   # never block on unknown col


def test_grid_write_guard_skips_without_columns():
    s = _grid_client()
    coerced, refusal = asyncio.run(
        s._grid_write_guard("AccountRecords", {"columns": []}, {"Type": "bad"}, "op", False))
    # _grid_col_meta would try the /structure fallback (no network in test) -> {} -> skip


# ---- v0.39: process-runner helpers -----------------------------------------

def test_is_processing_signals():
    S = ScreenClient
    assert S._is_processing({"redirects": [{"settings": {"type": "longRun"}}]}) is True
    assert S._is_processing({"controlsData": {"LongRunData": {"status": "InProcess"}}}) is True
    assert S._is_processing({"controlsData": {"LongRunData": {"running": True}}}) is True
    # the verified SYNC case: plain envelope -> not processing
    assert S._is_processing({"actionStates": {}, "graphIsDirty": False}) is False
    assert S._is_processing({"controlsData": {"LongRunData": {}}}) is False


def test_process_summary_extracts_result_and_messages():
    j = {"controlsData": {"ProcessingResultData": {"rows": [{"ok": 1}]}},
         "messages": [{"message": "2 records processed."}, {"message": None}]}
    out = ScreenClient._process_summary(j)
    assert out["processing_result"] == {"rows": [{"ok": 1}]}
    assert out["messages"] == ["2 records processed."]


# ---- v0.40: guide tool ------------------------------------------------------

def test_guide_full_overview():
    g = server.guide()
    assert set(g) >= {"start_here", "the_four_planes", "by_task", "plane_by_shape"}
    assert len(g["the_four_planes"]) == 4
    assert "write ONE record" in g["by_task"]


def test_guide_topic_filter_and_aliases():
    assert server.guide("write")["topic"] == "write ONE record"
    assert server.guide("gl")["topic"] == "financial-foundation / GL setup"
    p = server.guide("planes")
    assert "the_four_planes" in p and "plane_by_shape" in p


def test_guide_unknown_topic_lists_options():
    out = server.guide("nope")
    assert "error" in out and "planes" in out["topics"]


# ---- v0.41: CoA type mapping (#9) -------------------------------------------

def test_coa_type_map_letters_and_equity():
    n = server._normalize_coa_type
    assert n("A") == "Asset"
    assert n("L") == "Liability"
    assert n("E") == "Liability"  # Equity -> Liability (no Equity type)
    assert n("B") == "Expense"
    assert n("H") == "Income"


def test_coa_type_full_names_passthrough_case_insensitive():
    n = server._normalize_coa_type
    assert n("asset") == "Asset"
    assert n("LIABILITY") == "Liability"
    assert n("Income") == "Income"


def test_coa_type_override_and_invalid():
    n = server._normalize_coa_type
    assert n("B", {"B": "Asset"}) == "Asset"  # override built-in
    with pytest.raises(ValueError):
        n("Z")


# ---- v0.41: seat-limit detection (#10) --------------------------------------

def test_seat_limit_signatures():
    from grp_mcp.acumatica import looks_like_seat_limit as f
    assert f("API Login Limit reached")
    assert f("You have exceeded the maximum number of users")
    assert f("concurrent login not allowed")
    assert f(None, status=429)
    assert not f("some other fault")
    assert not f(None)


# ---- v0.41: run_dac_odata dedup (#12) ---------------------------------------

def test_dac_dedup_collapses_identical_rows(cfg, monkeypatch):
    dup = {"value": [{"FinPeriodID": "01-2026"}, {"FinPeriodID": "01-2026"},
                     {"FinPeriodID": "02-2026"}]}

    async def fake_run_dac(self, dac, params=None, timeout=None):
        return json.loads(json.dumps(dup))  # fresh copy

    monkeypatch.setattr(AcumaticaClient, "run_dac", fake_run_dac)
    out = asyncio.run(server.run_dac_odata("FinPeriod", instance="rw"))
    assert len(out["value"]) == 2
    assert out["@grp.deduped"] == 1
    # dedup=false leaves the raw payload untouched
    raw = asyncio.run(server.run_dac_odata("FinPeriod", dedup=False, instance="rw"))
    assert len(raw["value"]) == 3 and "@grp.deduped" not in raw


# ---- v0.41: session-only routing + collisions (#6) --------------------------

def test_list_instances_flags_tenant_collision(monkeypatch):
    c = Config(default="a", instances={
        "a": _inst(tenant="Company"), "b": _inst(tenant="Company"),
        "c": _inst(tenant="Other")})
    monkeypatch.setattr(server, "_config", c)
    out = server.list_instances()
    assert out["tenant_collisions"] == {"Company": ["a", "b"]}
    by_name = {i["name"]: i for i in out["instances"]}
    assert by_name["a"]["shares_tenant_with"] == ["b"]
    assert by_name["c"]["shares_tenant_with"] is None


def test_add_instance_session_only_marks_and_warns(monkeypatch):
    c = Config(default="a", instances={"a": _inst(tenant="Company")})
    monkeypatch.setattr(server, "_config", c)
    out = server.add_instance(
        "b", "https://h/S", "cid", "sek", "u", "p", tenant="Company", persist=False)
    assert out["session_only"] is True
    assert out["active"] == "a"  # not made active
    assert "a" in out["same_tenant_collision"]
    assert "b" in c.session_only


# ---- v0.41: bulk-load driver + resume (#5) ----------------------------------

def test_drive_load_success_and_next_offset(cfg, monkeypatch):
    puts = []

    async def fake_put(self, entity, body):
        puts.append(body)

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    state = {"job": "j", "entity": "E", "total": 3, "processed": 0, "succeeded": 0,
             "failed": 0, "next_offset": 0, "errors": [], "completed": False, "error": None}
    client = server._client("rw")
    mapped = [{"X": "1"}, {"X": "2"}, {"X": "3"}]
    asyncio.run(server._drive_load(state, client, "E", mapped, 0, False))
    assert state["succeeded"] == 3 and state["completed"] and state["next_offset"] == 3
    assert len(puts) == 3


def test_drive_load_stop_on_error_leaves_resume_offset(cfg, monkeypatch):
    async def fake_put(self, entity, body):
        if body["X"]["value"] == "2":
            raise AcumaticaError("boom")

    # _wrap_fields wraps scalars as {"value": ...}; assert resume points at the failed row
    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    state = {"job": "j", "entity": "E", "total": 3, "processed": 0, "succeeded": 0,
             "failed": 0, "next_offset": 5, "errors": [], "completed": False, "error": None}
    client = server._client("rw")
    mapped = [{"X": "1"}, {"X": "2"}, {"X": "3"}]
    asyncio.run(server._drive_load(state, client, "E", mapped, 5, True))
    assert state["succeeded"] == 1 and state["failed"] == 1
    assert state["next_offset"] == 6  # base_offset 5 + failed index 1
    assert state["errors"][0]["row"] == 1 + 5 + 1 + 1


# ---- v0.42: calendar teardown + range validation (#7, #3) --------------------

def test_reset_calendar_rejects_inverted_range(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.reset_calendar("2027", "2025", instance="rw"))


def test_generate_master_calendar_rejects_inverted_range(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.generate_master_calendar("2027", "2025", instance="rw"))


def test_delete_financial_year_needs_delete_gate(cfg):
    # 'ro' has allow_delete=False -> refused before any network call
    with pytest.raises(PermissionError):
        asyncio.run(server.delete_financial_year("2027", instance="ro"))


def test_reset_calendar_needs_delete_gate(cfg):
    with pytest.raises(PermissionError):
        asyncio.run(server.reset_calendar("2026", instance="ro"))


# ---- v0.43: company-tree limitation is documented ---------------------------

def test_setup_map_documents_company_tree_recipe():
    ids = {r["id"] for r in server._setup_map()["cross_cutting_rules"]}
    assert "company-tree-build-via-ep204060" in ids


# ---- v0.42: non-SOAP cookie login for the modern plane ----------------------

class _FakeResp:
    def __init__(self, status=204, text=""):
        self.status_code = status
        self.text = text


def test_cookie_login_sends_company_separately():
    # a tenant whose name has a space MUST go in `company`, not name@tenant
    inst = _inst(username="jdoe", tenant="My Tenant", password="pw")
    s = ScreenClient(inst, "PY101500")
    captured = {}

    async def fake_post(url, json=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResp(204)

    s._http.post = fake_post
    asyncio.run(s._cookie_login())
    assert s._logged_in is True and s._cookie_session is True
    assert captured["json"] == {"name": "jdoe", "password": "pw", "company": "My Tenant"}
    assert "@" not in captured["json"]["name"]
    assert captured["url"].endswith("/entity/auth/login")


def test_cookie_login_raises_on_non_2xx():
    s = ScreenClient(_inst(), "PY101500")

    async def fake_post(url, json=None, headers=None):
        return _FakeResp(401, "denied")

    s._http.post = fake_post
    with pytest.raises(ScreenError):
        asyncio.run(s._cookie_login())
    assert s._logged_in is False


def test_ensure_login_falls_back_to_cookie_when_soap_fails():
    s = ScreenClient(_inst(), "PY101500")

    async def boom():
        raise ScreenError("SOAP login disabled")

    async def cookie():
        s._logged_in = True
        s._cookie_session = True

    s.login = boom
    s._cookie_login = cookie
    asyncio.run(s._ensure_login())
    assert s._logged_in is True and s._cookie_session is True


def test_ensure_login_prefers_soap_and_is_noop_when_logged_in():
    s = ScreenClient(_inst(), "PY101500")
    calls = {"soap": 0, "cookie": 0}

    async def soap():
        calls["soap"] += 1
        s._logged_in = True

    async def cookie():
        calls["cookie"] += 1

    s.login = soap
    s._cookie_login = cookie
    asyncio.run(s._ensure_login())          # SOAP works -> cookie not tried
    assert calls == {"soap": 1, "cookie": 0} and s._cookie_session is False
    asyncio.run(s._ensure_login())          # already logged in -> no-op
    assert calls == {"soap": 1, "cookie": 0}


# ---- v0.43: publish merge-not-replace safety (#1/#2) ------------------------

def test_published_project_names_extractor():
    f = server._published_project_names
    assert f({"projects": [{"name": "A"}, {"name": "B"}], "items": []}) == ["A", "B"]
    assert f(["X", {"name": "Y"}]) == ["X", "Y"]
    assert f({"nope": 1}) == []


def _mock_published(monkeypatch, names):
    async def fake_get_published(self):
        return {"projects": [{"name": n} for n in names]}

    async def fake_aclose(self):
        return None

    monkeypatch.setattr(CustomizationClient, "get_published", fake_get_published)
    monkeypatch.setattr(CustomizationClient, "aclose", fake_aclose)


def test_publish_merge_dry_run_keeps_existing(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="merge",
                                                   dry_run=True, instance="rw"))
    assert out["will_publish"] == ["grp_mcp", "NewThing"]
    assert out["will_unpublish"] == []


def test_publish_replace_dry_run_flags_unpublish(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="replace",
                                                   dry_run=True, instance="rw"))
    assert out["will_publish"] == ["NewThing"]
    assert out["will_unpublish"] == ["grp_mcp"]


def test_publish_replace_refuses_without_confirm(cfg, monkeypatch):
    _mock_published(monkeypatch, ["grp_mcp", "other"])
    out = asyncio.run(server.publish_customization(["NewThing"], mode="replace",
                                                   instance="rw"))
    assert out["refused"] is True
    assert set(out["would_unpublish"]) == {"grp_mcp", "other"}


def test_publish_requires_allow_publish(cfg):
    # 'ro' profile has allow_publish=False
    with pytest.raises(PermissionError):
        asyncio.run(server.publish_customization(["X"], dry_run=True, instance="ro"))


def test_publish_invalid_mode_raises(cfg):
    with pytest.raises(ValueError):
        asyncio.run(server.publish_customization(["X"], mode="bogus", instance="rw"))


# ---- v0.44: parser & robustness fixes ---------------------------------------

def test_get_swagger_raises_clear_error_on_non_json(monkeypatch):
    from grp_mcp.acumatica import AcumaticaError

    inst = _inst(endpoint_name="GRP9", endpoint_version="1")
    client = AcumaticaClient(inst)

    async def fake_request(method, url, **kw):
        return "<html>error page</html>"  # non-JSON -> a str, not a dict

    client._request = fake_request
    with pytest.raises(AcumaticaError) as ei:
        asyncio.run(client.get_swagger())
    assert "OpenAPI" in str(ei.value) and "GRP9/1" in str(ei.value)


def test_get_schema_captures_unnamed_container(monkeypatch):
    import xml.etree.ElementTree as ET

    # a summary container with NO DisplayName (the old regex dropped it) + a named one
    root = ET.fromstring(
        "<root>"
        "<SummaryHeader>"
        "  <PayCodeCD><FieldName>PayCodeCD</FieldName><ObjectName>PayCode</ObjectName></PayCodeCD>"
        "  <Type><FieldName>PayType</FieldName><ObjectName>PayCode</ObjectName></Type>"
        "</SummaryHeader>"
        "<Actions><Save><FieldName>Save</FieldName><ObjectName>x</ObjectName></Save></Actions>"
        "<Details>"
        "  <DisplayName>Detail Lines</DisplayName>"
        "  <Amount><FieldName>Amount</FieldName><ObjectName>Line</ObjectName></Amount>"
        "</Details>"
        "</root>")
    s = ScreenClient(_inst(), "PY302000")

    async def fake_tree():
        return root

    s._ensure_tree = fake_tree
    out = asyncio.run(s.get_schema())
    conts = out["containers"]
    assert "SummaryHeader" in conts                       # unnamed (no DisplayName) captured
    assert conts["SummaryHeader"]["PayCodeCD"] == {"object": "PayCode", "field": "PayCodeCD"}
    assert "Details" in conts                              # named still works
    assert "Actions" not in conts                          # action container excluded


def test_run_dac_filter_in_runs_per_value(cfg, monkeypatch):
    calls = []

    async def fake_run_dac(self, dac, params=None, timeout=None):
        calls.append((params or {}).get("$filter"))
        v = (params or {}).get("$filter", "").split("'")[1]
        return {"value": [{"ScreenID": v}]}

    monkeypatch.setattr(AcumaticaClient, "run_dac", fake_run_dac)
    out = asyncio.run(server.run_dac_odata(
        "SiteMap", select="ScreenID",
        filter_in={"ScreenID": ["GL101000", "GL201000"]}, instance="rw"))
    assert len(calls) == 2 and "or" not in " ".join(calls)   # per-value, no giant OR
    assert {r["ScreenID"] for r in out["value"]} == {"GL101000", "GL201000"}


def test_config_unknown_instance_error_mentions_session_only():
    c = Config(default="a", instances={"a": _inst()})
    try:
        c.get("csmstg")
        assert False
    except KeyError as e:
        assert "session-only" in str(e) and "persist" in str(e)


# ---- v0.45: shared cookie session + endpoint-entity generator ---------------

def test_shared_session_reuse_across_clients(monkeypatch):
    from grp_mcp import screen as scr
    scr._SESSION_CACHE.clear()
    logins = {"soap": 0}

    async def fake_login(self):
        logins["soap"] += 1
        self._logged_in = True
        self._cookie_session = False

    monkeypatch.setattr(ScreenClient, "login", fake_login)
    inst = _inst()
    # first client logs in + caches; second reuses the cache (no new login)
    a = ScreenClient(inst, "GL101000")
    asyncio.run(a._ensure_login())
    b = ScreenClient(inst, "GL201000")
    asyncio.run(b._ensure_login())
    assert logins["soap"] == 1               # only ONE login for both clients
    assert a._shared and b._shared           # both defer logout to the cache
    key = a._session_key
    assert key in scr._SESSION_CACHE
    assert scr.clear_session_cache(key) == [key]
    assert key not in scr._SESSION_CACHE


def test_shared_session_not_logged_out(monkeypatch):
    from grp_mcp import screen as scr
    scr._SESSION_CACHE.clear()

    async def fake_login(self):
        self._logged_in = True

    called = {"soap_logout": 0}

    async def fake_call(self, op, inner, _seat_retried=False):
        if op == "Logout":
            called["soap_logout"] += 1
        return ""

    monkeypatch.setattr(ScreenClient, "login", fake_login)
    monkeypatch.setattr(ScreenClient, "_call", fake_call)
    s = ScreenClient(_inst(), "GL101000")
    asyncio.run(s._ensure_login())     # creates + caches -> _shared True
    asyncio.run(s.logout())            # must NOT log out a shared session
    assert called["soap_logout"] == 0
    assert s._session_key in scr._SESSION_CACHE


def _fake_async_client(sink):
    """A stand-in httpx.AsyncClient that records POST urls instead of hitting the net."""
    import httpx as _httpx

    class _Fake:
        def __init__(self, *a, **k):
            self._cookies = k.get("cookies")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, *a, **k):
            sink.append(url)
            return _httpx.Response(204)
    return _Fake


def test_logout_session_cache_ends_session_serverside(monkeypatch):
    # The leak fix: dropping a shared cookie session must LOG IT OUT server-side
    # (POST /entity/auth/logout with its cookies), not just forget it locally.
    from grp_mcp import screen as scr
    import httpx as _httpx
    scr._SESSION_CACHE.clear()
    key = "http://localhost/AcumaticaERP|admin|Company"
    scr._SESSION_CACHE[key] = {"cookies": _httpx.Cookies(), "at": 0.0, "kind": "cookie"}
    posted: list[str] = []
    monkeypatch.setattr(scr.httpx, "AsyncClient", _fake_async_client(posted))

    done = asyncio.run(scr.logout_session_cache())

    assert done == [key]                                   # returned the identity logged out
    assert key not in scr._SESSION_CACHE                   # dropped locally
    # base_url parsed from the key, contract logout endpoint hit
    assert posted == ["http://localhost/AcumaticaERP/entity/auth/logout"]


def test_logout_session_cache_best_effort_on_failure(monkeypatch):
    # A failed server logout must still drop the entry (idle-timeout is the backstop) —
    # never leave a dead cookie wedged in the cache.
    from grp_mcp import screen as scr
    import httpx as _httpx
    scr._SESSION_CACHE.clear()
    key = "http://localhost/AcumaticaERP|admin|Company"
    scr._SESSION_CACHE[key] = {"cookies": _httpx.Cookies(), "at": 0.0, "kind": "cookie"}

    class _Boom:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            raise _httpx.ConnectError("network down")

    monkeypatch.setattr(scr.httpx, "AsyncClient", _Boom)
    done = asyncio.run(scr.logout_session_cache())
    assert done == [key]                     # still reported handled
    assert key not in scr._SESSION_CACHE      # and still dropped


# ---- v0.47: tree_triage ------------------------------------------------------

def test_indent_actions_detects_indent_not_reorder():
    from grp_mcp.server import _indent_actions
    # Left/Right and indent/outdent/promote/demote count; Up/Down (reorder) do NOT.
    acts = ["Save", "Up", "Down", "Left", "Right", "AddWorkGroup", "Indent", "Promote"]
    got = _indent_actions(acts)
    assert set(got) == {"Left", "Right", "Indent", "Promote"}
    assert "Up" not in got and "Down" not in got


def test_parent_fields_finds_settable_parent_link():
    from grp_mcp.server import _parent_fields
    struct = {
        "grids": {"Nodes": {"columns": ["NodeID", "ParentID", "Description"]}},
        "views": {
            "Header": [
                {"field": "ParentGroupID", "readonly": False},
                {"field": "ReadOnlyParent", "readonly": True},   # read-only -> ignored
                {"field": "Name", "readonly": False},
            ]
        },
    }
    got = _parent_fields(struct)
    assert "Nodes.ParentID" in got
    assert "Header.ParentGroupID" in got
    assert "Header.ReadOnlyParent" not in got   # read-only parent isn't settable
    assert "Nodes.NodeID" not in got


def test_tree_triage_is_registered_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "tree_triage" in names


def test_reframe_ui_validation_is_actionable():
    # A screen business-rule rejection must read as "fixable — supply field + retry",
    # NOT as "this screen can't be set up".
    from grp_mcp.server import _reframe_ui_validation, _flagged_field_names, _UI_VALIDATION_PAT
    struct = {"views": {"CurrentCompany": [
        {"field": "CashAcctID", "required": True, "readonly": False},
        {"field": "BranchID", "required": True, "readonly": True},   # readonly -> not askable
        {"field": "RetirementAge", "required": False, "readonly": False},
    ]}}
    out = _reframe_ui_validation("PY301000", "Save",
                                 "ui_command Save on PY301000: PCB Pay Code can not be empty.",
                                 struct)
    assert out["ok"] is False and out["status"] == "validation_failed"
    assert out["reachable"] is True and out["writable"] is True
    assert "PCB Pay Code" in out["flagged_fields"]
    assert "CurrentCompany.CashAcctID" in out["required_fields"]
    assert "CurrentCompany.BranchID" not in out["required_fields"]   # readonly excluded
    assert "not a 'cannot set up'" in out["guidance"].lower()
    # the classifier recognizes the common phrasings, and ignores unrelated errors
    assert _UI_VALIDATION_PAT.search("Field X is required")
    assert _UI_VALIDATION_PAT.search("PREREQUISITE NOT MET — configure ARSetup")
    assert not _UI_VALIDATION_PAT.search("Object reference not set to an instance")


def test_flagged_field_names_parsing():
    from grp_mcp.server import _flagged_field_names
    assert _flagged_field_names("PCB Pay Code can not be empty") == ["PCB Pay Code"]
    assert "Cash Account" in _flagged_field_names("'Cash Account' is required")


def test_get_swagger_404_scopes_error_to_rest_plane(monkeypatch):
    # A contract-REST endpoint 404 must NOT read as a hard dead end — it should say the
    # screen SOAP / modern-UI planes are independent (the give-up trap the eval found).
    from grp_mcp.acumatica import AcumaticaError
    c = _acu("https://host/Site")

    async def boom(method, url, **kw):
        raise AcumaticaError(
            "GET https://host/Site/entity/GRPSetup/24.200.001/swagger.json -> "
            "404: Endpoint [GRPSetup/24.200.001] not found")

    monkeypatch.setattr(c, "_request", boom)
    try:
        asyncio.run(c.get_swagger())
        assert False, "should raise"
    except AcumaticaError as e:
        s = str(e)
        assert "CONTRACT-REST endpoint only" in s
        assert "screen_health" in s and "modern-UI" in s


def test_whoami_scopes_reachability_and_hints(monkeypatch):
    from grp_mcp.config import Config
    cfg = Config(default="t", instances={"t": _inst()})
    monkeypatch.setattr(server, "_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_clients", {})

    class FakeClient:
        async def get_swagger(self, refresh=False):
            raise server.AcumaticaError(
                "GET .../swagger.json -> 404: Endpoint [X/Y] not found")

    monkeypatch.setattr(server, "_client", lambda instance=None: FakeClient())
    out = asyncio.run(server.whoami())
    assert out["reachable"] is False
    assert "contract-REST" in out["reachable_scope"]          # scope always present
    assert "screen_health" in out["hint"]                     # hint only when unreachable
    assert "CONTRACT-REST plane ONLY" in out["hint"]


def test_guide_teaches_validation_is_not_dead_end():
    g = server.guide()
    assert "can't be set up" in g["start_here"] or "can't be driven" in g["start_here"] \
        or "not a 'this screen" in g["start_here"].lower()
    assert "validation error" in g["start_here"].lower()


def test_guide_names_every_registered_tool():
    # guide() is the START-HERE router — every registered tool must be discoverable
    # through it, or an agent leaning on guide alone can't find the tool (e.g. a poll
    # companion like load_status/activate_features_status). Grow NOT_IN_GUIDE only for
    # a tool deliberately excluded, with a reason.
    import json as _json
    NOT_IN_GUIDE: dict[str, str] = {
        # (empty) — guide is expected to name all tools; add exclusions here with cause
    }
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    blob = _json.dumps(server.guide())
    missing = sorted(n for n in names if n not in blob and n not in NOT_IN_GUIDE)
    assert not missing, (
        f"tool(s) not referenced anywhere in guide() — add them to the right by_task "
        f"bucket (or to NOT_IN_GUIDE with a reason): {missing}")


def test_guide_and_setupmap_document_odata_role():
    # The OData v4 role prerequisite must be discoverable IN the MCP (guide +
    # get_setup_guidance), not only in the external Word doc.
    g = server.guide()
    assert "odata_v4_role" in g.get("env_prerequisites", {})
    assert "OData v4" in g["env_prerequisites"]["odata_v4_role"]
    assert "OData v4" in g["the_four_planes"]["DAC / GI OData (raw read)"]
    ids = {r["id"] for r in server._setup_map()["cross_cutting_rules"]}
    assert "odata-v4-role-required-for-probing" in ids


# ---- v0.48 audit fixes -------------------------------------------------------

def test_save_config_excludes_session_only(tmp_path):
    # persist=false is a promise: those credentials must NEVER land on disk —
    # even when a LATER persisting call writes the config.
    from grp_mcp.config import Config, Instance, save_config
    import json as _json
    kw = dict(client_id="x", client_secret="y", username="u", password="SECRET")
    cfg = Config(
        default="mem",
        instances={"disk": Instance(base_url="http://a/A", **kw),
                   "mem": Instance(base_url="http://b/B", **kw)},
        session_only={"mem"},
    )
    target = str(tmp_path / "connections.json")
    save_config(cfg, target)
    data = _json.loads((tmp_path / "connections.json").read_text(encoding="utf-8"))
    assert "mem" not in data["instances"]          # session-only stayed in memory
    assert "disk" in data["instances"]
    assert data["default"] == "disk"               # on-disk default must exist on disk
    assert "SECRET" not in (tmp_path / "connections.json").read_text(encoding="utf-8") \
        or data["instances"]["disk"]["password"] == "SECRET"  # only via the disk profile


def test_save_config_all_session_only_refuses(tmp_path):
    from grp_mcp.config import Config, Instance, save_config
    kw = dict(client_id="x", client_secret="y", username="u", password="p")
    cfg = Config(default="mem",
                 instances={"mem": Instance(base_url="http://b/B", **kw)},
                 session_only={"mem"})
    try:
        save_config(cfg, str(tmp_path / "c.json"))
        assert False, "should refuse to write an empty instance set"
    except RuntimeError as e:
        assert "session-only" in str(e)


def test_seat_limit_regex_not_fooled_by_business_errors():
    from grp_mcp.acumatica import looks_like_seat_limit
    # business errors must NOT trigger session logouts + LoginLimitError masking
    assert not looks_like_seat_limit("You have exceeded the credit limit for this customer")
    assert not looks_like_seat_limit("you have exceeded the maximum row count")
    # real seat-limit phrasings still match
    assert looks_like_seat_limit("API Login Limit")
    assert looks_like_seat_limit("You have exceeded the maximum number of concurrent sessions")
    assert looks_like_seat_limit("you have exceeded the allowed number of users")
    assert looks_like_seat_limit("", status=429)


def test_oq_escapes_odata_quotes():
    from grp_mcp.server import _oq
    assert _oq("O'Brien Import") == "O''Brien Import"
    assert _oq("plain") == "plain"
    assert _oq(123) == "123"


def test_customization_login_sanitizes_error_and_relieves_seats(monkeypatch):
    import httpx as _httpx
    from grp_mcp.customization import CustomizationClient, CustomizationError
    inst = _inst()
    calls = {"n": 0, "relieved": 0}

    # 1) seat-limit 500 -> reliever fires, retry succeeds
    async def fake_post_seat(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _httpx.Response(500, text='{"exceptionMessage":"API Login Limit"}')
        return _httpx.Response(204)

    async def fake_reliever(exclude=None):
        calls["relieved"] += 1

    c = CustomizationClient(inst)
    monkeypatch.setattr(c._http, "post", fake_post_seat)
    monkeypatch.setattr(c, "seat_reliever", fake_reliever)
    asyncio.run(c._login())
    assert calls == {"n": 2, "relieved": 1} and c._logged_in

    # 2) credential failure -> structured message, raw body NOT echoed
    async def fake_post_bad(url, **kw):
        return _httpx.Response(401, text='{"message":"Invalid credentials",'
                                         '"secretEcho":"do-not-leak"}')
    c2 = CustomizationClient(inst)
    monkeypatch.setattr(c2._http, "post", fake_post_bad)
    try:
        asyncio.run(c2._login())
        assert False, "should have raised"
    except CustomizationError as e:
        assert "Invalid credentials" in str(e)
        assert "do-not-leak" not in str(e)     # raw body never echoed


def test_cookie_login_relieves_seat_limit_and_retries(monkeypatch):
    # Seat-limit self-heal parity: /entity/auth/login 500 "API Login Limit" must
    # trigger the reliever and retry ONCE (previously only classic SOAP _call did;
    # a SOAP-disabled instance could never recover from a seat jam).
    import httpx as _httpx
    calls = {"n": 0, "relieved": 0}

    async def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _httpx.Response(500, text='{"exceptionMessage":"API Login Limit"}')
        return _httpx.Response(204)

    async def fake_reliever(exclude=None):
        calls["relieved"] += 1

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s._http, "post", fake_post)
    monkeypatch.setattr(s, "seat_reliever", fake_reliever)
    asyncio.run(s._cookie_login())
    assert calls == {"n": 2, "relieved": 1}   # relieved once, retried once, succeeded
    assert s._logged_in and s._cookie_session


def test_cookie_login_non_seat_error_no_retry(monkeypatch):
    # A non-seat 500 must NOT loop the reliever — raise immediately.
    import httpx as _httpx
    calls = {"n": 0}

    async def fake_post(url, **kw):
        calls["n"] += 1
        return _httpx.Response(500, text='{"exceptionMessage":"boom"}')

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s._http, "post", fake_post)
    try:
        asyncio.run(s._cookie_login())
        assert False, "should have raised"
    except ScreenError as e:
        assert "cookie login" in str(e)
    assert calls["n"] == 1


def test_ui_command_dialog_answer_none_returns_dialog(monkeypatch):
    # answer="none" must NOT auto-confirm — it returns the unanswered dialog info.
    import httpx as _httpx
    posted = []

    async def fake_ui_post(payload, _auth_retried=False):
        posted.append(payload)
        return _httpx.Response(302, json={"redirects": [
            {"settings": {"type": "openDialog", "viewName": "ConfirmView"}}]})

    s = ScreenClient(_inst(), "GL101000")
    monkeypatch.setattr(s, "_ui_post", fake_ui_post)
    out = asyncio.run(s.ui_command("Generate", answer="none"))
    assert out["dialog_open"] is True and out["dialog_view"] == "ConfirmView"
    assert len(posted) == 1                     # never sent a dialogCallback
    assert "dialogCallback" not in posted[0]


def test_ui_command_rejects_unknown_answer():
    s = ScreenClient(_inst(), "GL101000")
    try:
        asyncio.run(s.ui_command("Save", answer="maybe"))
        assert False, "should have raised"
    except ScreenError as e:
        assert "unknown dialog answer" in str(e)


def test_indent_pref_prefers_indent_verb():
    from grp_mcp.server import _indent_pref
    assert _indent_pref(["Left", "Right"]) == "Right"      # Right = nest deeper
    assert _indent_pref(["Left"]) == "Left"                # fall back to what's there
    assert _indent_pref(["Outdent", "Indent"]) == "Indent"


def test_list_grid_guess_skips_tree_and_members():
    from grp_mcp.server import _list_grid_guess
    # EP204060's grids — the insert target is Items, not the tree/detail grids.
    assert _list_grid_guess(["Folders", "Items", "Members"]) == "Items"
    assert _list_grid_guess(["EntityTree"]) == "EntityTree"   # nothing else -> use it
    assert _list_grid_guess([]) == "<grid>"


def test_flatten_tree_preorder_depths():
    f = server._flatten_tree
    struct = [{"name": "R", "children": [
        {"name": "A", "children": ["A1", "A2"]},
        "B"]}]
    assert f(struct) == [("R", 0), ("A", 1), ("A1", 2), ("A2", 2), ("B", 1)]
    assert f(["X"]) == [("X", 0)]
    with pytest.raises(ValueError):
        f([{"children": []}])   # node without a name


def test_build_company_tree_is_registered_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "build_company_tree" in names
    assert "_flatten_tree" not in names   # helper stays private


def test_edm_to_valuetype_mapping():
    f = server._edm_to_valuetype
    assert f("Edm.String") == "StringValue"
    assert f("Boolean") == "BooleanValue"
    assert f("Int16") == "ShortValue"
    assert f("Int32") == "IntValue"
    assert f("DateTime") == "DateTimeValue"
    assert f("WeirdType") == "StringValue"   # safe default


# ---- tool-registration integrity (guards the decorator-on-helper bug) --------

def test_tool_registration_integrity():
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    # every real tool must be registered
    for must in ("run_dac_odata", "publish_customization", "screen_health",
                 "generate_endpoint_entity", "list_published", "get_endpoint_definition",
                 "activate_features"):
        assert must in names, f"{must} is not a registered MCP tool"
    # module-private helpers must NOT leak as tools (a @mcp.tool() landing on the wrong
    # def steals the tool's decorator — how run_dac_odata briefly de-registered)
    for helper in ("_dedup_rows", "_published_project_names", "_activation_status",
                   "_edm_to_valuetype"):
        assert helper not in names, f"internal helper {helper} wrongly exposed as a tool"
    # belt-and-suspenders: no underscore-prefixed tool names at all
    assert not [n for n in names if n.startswith("_")]


def test_every_public_function_is_a_registered_tool():
    # STRUCTURAL guard for the decorator-theft bug class: any PUBLIC module-level
    # function in server.py is either a registered tool or on the explicit non-tool
    # allowlist. This is what a hand-picked must-list can't do — activate_features
    # was silently unregistered for 4 releases (v0.43.1..v0.47.1) because the fix
    # for a stolen decorator DELETED it instead of moving it back, and no list
    # mentioned the tool. Grow the allowlist only for a function that is genuinely
    # not meant to be a tool.
    import inspect
    NOT_TOOLS = {
        "extend_endpoint",   # deliberately deregistered (REST PUT is a verified no-op)
        "main",              # console entry point
    }
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    missing = []
    for fname, fn in inspect.getmembers(server, inspect.isfunction):
        if fn.__module__ != server.__name__:   # imported, not defined here
            continue
        if fname.startswith("_") or fname in NOT_TOOLS:
            continue
        if fname not in names:
            missing.append(fname)
    assert not missing, (
        f"public function(s) in server.py not registered as MCP tools (lost/stolen "
        f"@mcp.tool() decorator?): {missing} — if intentional, add to NOT_TOOLS")


# ---- v0.52.4: insert_rows one-Submit-per-row (fixes the CS205010 cross-row
# corruption bug — see screen.py insert_rows docstring) ---------------------

def test_insert_rows_one_submit_per_row_and_header():
    """insert_rows must issue a SEPARATE submit() call per row (plus one for the
    header, if given) — never bundle multiple NewRow+Set blocks into one Submit."""
    s = ScreenClient(_inst(), "CS205010")
    calls = []

    async def fake_submit(cmds, dry_run=False, auto_answer=None):
        calls.append(cmds)
        return {"ok": True, "messages": [], "field_errors": []}

    s.submit = fake_submit
    rows = [
        {"Building": "A", "Description": "Bldg A"},
        {"Building": "B", "Description": "Bldg B"},
        {"Building": "C", "Description": "Bldg C"},
    ]
    result = asyncio.run(s.insert_rows("Buildings", rows, header={"Branch": "000"}))

    # header + 3 rows = 4 separate submit() calls, never bundled together
    assert len(calls) == 4
    assert calls[0] == [{"set": "Branch", "to": "000"}]
    for i, row in enumerate(rows):
        row_cmds = calls[i + 1]
        assert row_cmds[0] == {"new_row": "Buildings"}
        assert {"set": "Building", "to": row["Building"]} in row_cmds
        assert {"set": "Description", "to": row["Description"]} in row_cmds
        assert row_cmds[-1] == {"action": "Save"}

    assert result["ok"] is True
    assert result["row_count"] == 3
    assert result["succeeded"] == 3
    assert result["failed"] == 0
    assert len(result["results"]) == 3


def test_insert_rows_partial_failure_does_not_abort_remaining_rows():
    """One bad row should not block the rest — each row is isolated."""
    s = ScreenClient(_inst(), "CS205010")
    seen_rows = []

    async def fake_submit(cmds, dry_run=False, auto_answer=None):
        # identify which row this is from its Building value
        building = next((c["to"] for c in cmds if c.get("set") == "Building"), None)
        seen_rows.append(building)
        if building == "B":
            return {"ok": False, "messages": ["Building 'B' already exists"], "field_errors": []}
        return {"ok": True, "messages": [], "field_errors": []}

    s.submit = fake_submit
    rows = [
        {"Building": "A", "Description": "Bldg A"},
        {"Building": "B", "Description": "Bldg B"},
        {"Building": "C", "Description": "Bldg C"},
    ]
    result = asyncio.run(s.insert_rows("Buildings", rows))

    assert seen_rows == ["A", "B", "C"]   # all 3 attempted despite B failing
    assert result["ok"] is False
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    assert result["results"][1]["ok"] is False
    assert "already exists" in result["messages"][0]


def test_insert_rows_header_failure_skips_all_rows():
    """If the header field-set itself fails, no row NewRow/Set should be attempted."""
    s = ScreenClient(_inst(), "CS205010")
    calls = []

    async def fake_submit(cmds, dry_run=False, auto_answer=None):
        calls.append(cmds)
        return {"ok": False, "messages": ["Branch '999' does not exist"], "field_errors": []}

    s.submit = fake_submit
    result = asyncio.run(s.insert_rows(
        "Buildings", [{"Building": "A", "Description": "Bldg A"}],
        header={"Branch": "999"},
    ))

    assert len(calls) == 1   # only the header submit — no row was attempted
    assert result["ok"] is False
    assert "note" in result


# ---- v0.52.5: ui_insert_grid_row key-mangle read-back guard -----------------

def _row(**cells):
    """A modern-plane grid row in {cells:{Field:{value}}} shape."""
    return {"id": "r", "cells": {k: {"value": v} for k, v in cells.items()}}


def test_key_mangle_norm():
    n = ScreenClient._key_mangle_norm
    assert n("A. SELERA") == "A SELERA"
    assert n("A  SELERA") == "A SELERA"      # collapses to same as the punctuated form
    assert n("BP/KPK/HT") == "BP KPK HT"
    assert n("KK.") == "KK"
    assert n("KEDAI") == "KEDAI"


def test_verify_stored_key_flags_mangled_key():
    s = _grid_client()
    g = {"key_names": ["BuildingCD"]}
    # sent 'A. SELERA' but the row persisted as 'A  SELERA' (punctuation -> space)
    resp = {"controlsData": {"building": {"rows": [_row(BuildingCD="A  SELERA",
                                                        Description="ANJUNG SELERA")]}}}
    warn = asyncio.run(s._verify_stored_key(
        "building", g, {"BuildingCD": "A. SELERA", "Description": "ANJUNG SELERA"},
        resp, None))
    assert warn is not None
    assert warn["sent_key"] == {"BuildingCD": "A. SELERA"}
    assert warn["stored_key"] == {"BuildingCD": "A  SELERA"}


def test_verify_stored_key_flags_truncated_key():
    s = _grid_client()
    g = {"key_names": ["BuildingCD"]}
    # sent 11-char 'ZZ.TEST/GRD' but the field truncated it to 10 -> 'ZZ.TEST/GR'
    resp = {"controlsData": {"building": {"rows": [_row(BuildingCD="ZZ.TEST/GR",
                                                        Description="X")]}}}
    warn = asyncio.run(s._verify_stored_key(
        "building", g, {"BuildingCD": "ZZ.TEST/GRD", "Description": "X"}, resp, None))
    assert warn is not None
    assert warn["stored_key"] == {"BuildingCD": "ZZ.TEST/GR"}


def test_is_altered_key_matrix():
    f = ScreenClient._is_altered_key
    assert f("A. SELERA", "A  SELERA") is True     # punctuation -> space
    assert f("ZZ.TEST/GRD", "ZZ.TEST/GR") is True  # right-truncation
    assert f("KEDAI", "KEDAI") is False            # identical
    assert f("KOLOMBONG", "KEPAYAN") is False      # unrelated row
    assert f("ABC", "") is False                   # empty stored -> not a match


def test_verify_stored_key_none_on_exact_match():
    s = _grid_client()
    g = {"key_names": ["BuildingCD"]}
    resp = {"controlsData": {"building": {"rows": [_row(BuildingCD="KEDAI",
                                                        Description="X")]}}}
    warn = asyncio.run(s._verify_stored_key(
        "building", g, {"BuildingCD": "KEDAI", "Description": "X"}, resp, None))
    assert warn is None   # stored exactly as sent -> no warning


def test_verify_stored_key_none_without_key_fields():
    s = _grid_client()
    g = {"key_names": ["BuildingCD"]}
    # caller supplied no key field -> nothing to verify, never warns/reads
    warn = asyncio.run(s._verify_stored_key(
        "building", g, {"Description": "X"}, {"controlsData": {}}, None))
    assert warn is None


# ---- v0.60: external bug-report fixes (2026-07-10 report, validated live) ----

def test_put_operation_infers_created_vs_updated():
    f = server._put_operation
    # equal audit stamps -> the PUT inserted (the partial-composite-key tripwire)
    assert f({"CreatedDateTime": {"value": "2026-07-10T14:52:29.923+08:00"},
              "LastModifiedDateTime": {"value": "2026-07-10T14:52:29.923+08:00"}}) == "created"
    # modified moved days later -> update
    assert f({"CreatedDateTime": {"value": "2026-07-10T14:52:43.73+08:00"},
              "LastModifiedDateTime": {"value": "2026-07-15T19:22:27.727+08:00"}}) == "updated"
    # trailing-Z form parses too
    assert f({"CreatedDateTime": {"value": "2026-07-10T06:00:00Z"},
              "LastModifiedDateTime": {"value": "2026-07-10T06:00:01Z"}}) == "created"
    # absent / unparseable stamps -> no claim
    assert f({}) is None
    assert f({"CreatedDateTime": {"value": "garbage"},
              "LastModifiedDateTime": {"value": "2026-07-10T06:00:00Z"}}) is None


def test_create_or_update_raises_when_detail_readback_confirms_empty(cfg, monkeypatch):
    # bug report #6 (reproduced live on Customer.Contacts): PUT echoes the detail
    # as [], the read-back CONFIRMS it's empty -> must fail loud, not success-shaped.
    async def fake_put(self, entity, body):
        return {"id": "RID", "Contacts": []}

    async def fake_get(self, entity, record_id, params=None):
        return {"id": "RID", "Contacts": []}

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    monkeypatch.setattr(AcumaticaClient, "get_entity", fake_get)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(server.create_or_update_entity(
            "Customer", {"CustomerID": "A", "Contacts": [{"DisplayName": "c"}]},
            instance="rw"))
    assert "did NOT persist" in str(e.value) and "Contacts" in str(e.value)


def test_create_or_update_soft_flags_when_readback_itself_fails(cfg, monkeypatch):
    # read-back errored -> can't tell either way -> soft _unverified_details, no raise
    async def fake_put(self, entity, body):
        return {"id": "RID", "Contacts": []}

    async def fake_get(self, entity, record_id, params=None):
        raise AcumaticaError("boom")

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    monkeypatch.setattr(AcumaticaClient, "get_entity", fake_get)
    out = asyncio.run(server.create_or_update_entity(
        "Customer", {"CustomerID": "A", "Contacts": [{"DisplayName": "c"}]},
        instance="rw"))
    assert out["_unverified_details"] == ["Contacts"]


def test_create_or_update_patches_echo_quirk_from_readback(cfg, monkeypatch):
    # the known echo quirk: [] echo but the read-back has the rows -> patched in, no raise
    async def fake_put(self, entity, body):
        return {"id": "RID", "Contacts": []}

    async def fake_get(self, entity, record_id, params=None):
        return {"id": "RID", "Contacts": [{"DisplayName": "c"}]}

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    monkeypatch.setattr(AcumaticaClient, "get_entity", fake_get)
    out = asyncio.run(server.create_or_update_entity(
        "Customer", {"CustomerID": "A", "Contacts": [{"DisplayName": "c"}]},
        instance="rw"))
    assert out["Contacts"] == [{"DisplayName": "c"}]
    assert "_unverified_details" not in out


def test_enum_issue_accepts_value_or_text_and_flags_bogus():
    # bug report #3 (reproduced live on GL202500): Type:'Bogus' persisted as the
    # silent default 'Asset'. The shared enum check must flag it — and accept
    # either the option value or its display text, case-insensitively.
    opts = [{"value": "A", "text": "Asset"}, {"value": "E", "text": "Expense"}]
    f = ScreenClient._enum_issue
    bad = f(opts, "Type", "Bogus", "grid column")
    assert bad is not None and bad["allowed"][0]["text"] == "Asset"
    assert f(opts, "Type", "A", "grid column") is None        # option value
    assert f(opts, "Type", "expense", "grid column") is None  # display text, any case
    assert f(opts, "Type", None, "grid column") is None       # nothing to check
    assert f(opts, "Type", True, "grid column") is None       # booleans pass
    assert f(None, "Type", "Bogus", "grid column") is None    # not an enum


def test_delete_verify_targets_pairs_nearest_preceding_set():
    # bug #7 (found during validation cleanup): pair each delete_row with the set
    # that navigated to the row, so the delete can be read back afterwards.
    f = ScreenClient._delete_verify_targets
    cmds = [{"set": "Account", "to": "99801"},
            {"delete_row": "AccountRecords"},
            {"action": "Save"}]
    assert f(cmds) == [("AccountRecords", "Account", "99801")]
    # no preceding set -> nothing to verify with
    assert f([{"delete_row": "X"}, {"action": "Save"}]) == []
    # blank set values are not navigation
    assert f([{"set": "A", "to": ""}, {"delete_row": "X"}]) == []
    # the NEAREST preceding set wins
    cmds2 = [{"set": "A", "to": "1"}, {"set": "B", "to": "2"}, {"delete_row": "X"}]
    assert f(cmds2) == [("X", "B", "2")]


def _verify_deletes_stub(monkeypatch, export_rows):
    """A ScreenClient whose export() returns `export_rows`, for _verify_deletes."""
    c = ScreenClient.__new__(ScreenClient)

    async def fake_export(fields, top=10, filters=None):
        return {"rows": export_rows}

    c.export = fake_export                                     # type: ignore[assignment]
    c._container_has_field = lambda container, field: True     # type: ignore[assignment]
    return c


def test_verify_deletes_flags_only_a_row_that_carries_the_value():
    # A returned row actually carrying the searched value = a REAL silent no-op.
    c = _verify_deletes_stub(None, [{"ValueID": "BBB"}])
    cmds = [{"set": "AttributeDetails.ValueID", "to": "BBB"},
            {"delete_row": "AttributeDetails"}]
    out = asyncio.run(c._verify_deletes(cmds, {"ok": True}))
    assert out["ok"] is False
    assert out["delete_verified"] is False
    assert "STILL EXISTS" in out["error"]


def test_verify_deletes_reports_unverified_when_filter_does_not_discriminate():
    # Found live on CS205000: the classic Export returns a header-level row with a
    # BLANK detail column whether the row exists or not, so non-empty `rows` alone
    # used to report every successful targeted delete as a silent no-op.
    c = _verify_deletes_stub(None, [{"ValueID": ""}])
    cmds = [{"set": "AttributeDetails.ValueID", "to": "CCC"},
            {"delete_row": "AttributeDetails"}]
    out = asyncio.run(c._verify_deletes(cmds, {"ok": True}))
    assert out["ok"] is True                       # must NOT fail the delete
    assert out["delete_verified"] == "unverified"
    assert "does not discriminate" in out["delete_verify_note"]


def test_verify_deletes_passes_when_export_returns_nothing():
    c = _verify_deletes_stub(None, [])
    cmds = [{"set": "AttributeDetails.ValueID", "to": "CCC"},
            {"delete_row": "AttributeDetails"}]
    out = asyncio.run(c._verify_deletes(cmds, {"ok": True}))
    assert out["ok"] is True
    assert out["delete_verified"] is True


# ---- v0.61: external audit fixes (2026-07-15 report) ------------------------

# --- #1: session-only add_instance must not bypass the admin gate when it
# requests allow_write/allow_delete/allow_publish (the local-file-exfiltration
# path via attach_file_to_provider to an attacker-controlled base_url). ------

def test_add_instance_session_only_elevated_requires_admin(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError) as e:
        server.add_instance(
            "evil", "https://attacker.example", "cid", "sek", "u", "p",
            allow_write=True, persist=False)
    # the error must NOT tell the caller to retry with persist=false — that's
    # exactly what was just tried and refused (a live-verified copy bug: the
    # shared _require_admin message used to say this unconditionally).
    assert "persist=false" not in str(e.value).lower()
    assert "GRP_MCP_ALLOW_ADMIN" in str(e.value)


def test_add_instance_session_only_elevated_delete_requires_admin(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.add_instance(
            "evil", "https://attacker.example", "cid", "sek", "u", "p",
            allow_delete=True, persist=False)


def test_add_instance_session_only_elevated_publish_requires_admin(cfg, monkeypatch):
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    with pytest.raises(PermissionError):
        server.add_instance(
            "evil", "https://attacker.example", "cid", "sek", "u", "p",
            allow_publish=True, persist=False)


def test_add_instance_session_only_elevated_allowed_with_admin_env(cfg, monkeypatch):
    monkeypatch.setenv("GRP_MCP_ALLOW_ADMIN", "1")
    out = server.add_instance(
        "ok", "https://real.example", "cid", "sek", "u", "p",
        allow_write=True, persist=False)
    assert out["added"] == "ok" and out["session_only"] is True


def test_add_instance_session_only_readonly_still_ungated(cfg, monkeypatch):
    # the intended low-friction case must keep working without the admin env var
    monkeypatch.delenv("GRP_MCP_ALLOW_ADMIN", raising=False)
    out = server.add_instance(
        "quicktest", "https://other.example", "cid", "sek", "u", "p", persist=False)
    assert out["added"] == "quicktest" and out["session_only"] is True


# --- #2: get_bytes now streams and aborts once it exceeds a byte cap, instead
# of buffering the whole response first. -------------------------------------

class _FakeStreamResp:
    def __init__(self, status_code, chunks):
        self.status_code = status_code
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c

    async def aread(self):
        return b"".join(self._chunks)


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def test_get_bytes_streams_ok_under_cap(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site"))

    async def fake_auth():
        return {}

    monkeypatch.setattr(c, "_auth_header", fake_auth)
    monkeypatch.setattr(
        c._http, "stream",
        lambda method, url, headers=None, **kw: _FakeStreamCtx(
            _FakeStreamResp(200, [b"abcd", b"efgh"])))
    data = asyncio.run(c.get_bytes("https://host/Site/files/x", max_bytes=100))
    assert data == b"abcdefgh"


def test_get_bytes_aborts_when_exceeding_explicit_cap(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site"))

    async def fake_auth():
        return {}

    monkeypatch.setattr(c, "_auth_header", fake_auth)
    monkeypatch.setattr(
        c._http, "stream",
        lambda method, url, headers=None, **kw: _FakeStreamCtx(
            _FakeStreamResp(200, [b"x" * 10, b"y" * 10])))
    with pytest.raises(AcumaticaError, match="max_file_bytes"):
        asyncio.run(c.get_bytes("https://host/Site/files/x", max_bytes=15))


def test_get_bytes_defaults_to_instance_max_file_bytes(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site", max_file_bytes=5))

    async def fake_auth():
        return {}

    monkeypatch.setattr(c, "_auth_header", fake_auth)
    monkeypatch.setattr(
        c._http, "stream",
        lambda method, url, headers=None, **kw: _FakeStreamCtx(
            _FakeStreamResp(200, [b"123456789"])))
    with pytest.raises(AcumaticaError, match="max_file_bytes"):
        asyncio.run(c.get_bytes("https://host/Site/files/x"))  # no explicit max_bytes


def test_get_bytes_zero_cap_means_uncapped(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site", max_file_bytes=0))

    async def fake_auth():
        return {}

    monkeypatch.setattr(c, "_auth_header", fake_auth)
    monkeypatch.setattr(
        c._http, "stream",
        lambda method, url, headers=None, **kw: _FakeStreamCtx(
            _FakeStreamResp(200, [b"x" * 1000])))
    data = asyncio.run(c.get_bytes("https://host/Site/files/x"))
    assert len(data) == 1000


def test_get_bytes_retries_once_on_401(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site"))
    calls = {"auth": 0}

    async def fake_auth():
        calls["auth"] += 1
        return {}

    monkeypatch.setattr(c, "_auth_header", fake_auth)
    responses = [_FakeStreamResp(401, []), _FakeStreamResp(200, [b"ok"])]

    def fake_stream(method, url, headers=None, **kw):
        return _FakeStreamCtx(responses.pop(0))

    monkeypatch.setattr(c._http, "stream", fake_stream)
    data = asyncio.run(c.get_bytes("https://host/Site/files/x", max_bytes=100))
    assert data == b"ok"
    assert calls["auth"] == 2


# --- #3: run_report must resolve a site-absolute Location via _abs(), not a
# naive base_url + location concat (which doubles the virtual directory). ---

class _FakeRawResp:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def test_run_report_resolves_site_absolute_location_without_doubling(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site"))
    seen_urls = []
    calls = {"n": 0}

    async def fake_request_raw(method, url, **kw):
        seen_urls.append(url)
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeRawResp(202, b"", {"Location": "/Site/entity/job/1"})
        return _FakeRawResp(200, b"PDFDATA")

    monkeypatch.setattr(c, "_request_raw", fake_request_raw)
    data = asyncio.run(
        c.run_report("PrintInvoice", {"parameters": {}}, poll_interval=0.01, timeout=1))
    assert data == b"PDFDATA"
    # the polled URL must NOT double the '/Site' virtual directory
    assert seen_urls[1] == "https://host/Site/entity/job/1"


def test_run_report_enforces_max_file_bytes_after_fetch(monkeypatch):
    c = AcumaticaClient(_inst(base_url="https://host/Site", max_file_bytes=5))

    async def fake_request_raw(method, url, **kw):
        return _FakeRawResp(200, b"way too long for the cap")

    monkeypatch.setattr(c, "_request_raw", fake_request_raw)
    with pytest.raises(AcumaticaError, match="max_file_bytes"):
        asyncio.run(c.run_report("PrintInvoice", {"parameters": {}}))


# --- #4: _drive_load caps stored per-row errors; _prune_load_jobs evicts the
# oldest completed jobs once the retained count exceeds the cap. ------------

def test_drive_load_caps_stored_errors_but_counts_every_failure(cfg, monkeypatch):
    async def fake_put(self, entity, body):
        raise AcumaticaError("always fails")

    monkeypatch.setattr(AcumaticaClient, "put_entity", fake_put)
    n = server._MAX_STORED_ROW_ERRORS + 10
    state = {"job": "j", "entity": "E", "total": n, "processed": 0, "succeeded": 0,
             "failed": 0, "next_offset": 0, "errors": [], "completed": False, "error": None}
    client = server._client("rw")
    mapped = [{"X": str(i)} for i in range(n)]
    asyncio.run(server._drive_load(state, client, "E", mapped, 0, False))
    assert state["failed"] == n            # every failure counted
    assert state["completed"] is True
    # capped at MAX + 1 sentinel row, not one entry per failure
    assert len(state["errors"]) == server._MAX_STORED_ROW_ERRORS + 1
    assert state["errors"][-1]["row"] is None
    assert "cap" in state["errors"][-1]["error"].lower()


def test_prune_load_jobs_evicts_oldest_completed_first():
    server._load_jobs.clear()
    try:
        for i in range(server._MAX_RETAINED_LOAD_JOBS + 5):
            server._load_jobs[f"job{i}"] = {"completed": True, "error": None}
        server._prune_load_jobs()
        assert len(server._load_jobs) == server._MAX_RETAINED_LOAD_JOBS
        assert "job0" not in server._load_jobs
        assert f"job{server._MAX_RETAINED_LOAD_JOBS + 4}" in server._load_jobs
    finally:
        server._load_jobs.clear()


def test_prune_load_jobs_never_evicts_in_progress():
    server._load_jobs.clear()
    try:
        server._load_jobs["running"] = {"completed": False, "error": None}
        for i in range(server._MAX_RETAINED_LOAD_JOBS + 5):
            server._load_jobs[f"done{i}"] = {"completed": True, "error": None}
        server._prune_load_jobs()
        assert "running" in server._load_jobs
    finally:
        server._load_jobs.clear()


# --- #5: ui._load() must treat ONLY "no config at all" as a fresh install;
# a malformed EXISTING connections.json must propagate, not silently vanish. -

def test_ui_load_returns_empty_config_when_none_exists(monkeypatch):
    # mock load_config directly rather than relying on filesystem discovery — this
    # repo's OWN connections.json would otherwise leak in via the cwd/package-root
    # fallback candidates load_config() tries after an unset/missing env path.
    from grp_mcp import ui as ui_mod
    from grp_mcp.config import ConfigNotFoundError

    def fake_load_config():
        raise ConfigNotFoundError("no configuration found")

    monkeypatch.setattr(ui_mod, "load_config", fake_load_config)
    cfg = ui_mod._load()
    assert cfg.instances == {} and cfg.source_path is None


def test_ui_load_propagates_malformed_json(tmp_path, monkeypatch):
    from grp_mcp import ui as ui_mod

    bad = tmp_path / "connections.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setenv("GRP_MCP_CONNECTIONS", str(bad))
    with pytest.raises(json.JSONDecodeError):
        ui_mod._load()


def test_ui_load_propagates_validation_error(tmp_path, monkeypatch):
    from grp_mcp import ui as ui_mod

    bad = tmp_path / "connections.json"
    # 'instances' present but a profile is missing required fields -> pydantic error
    bad.write_text(json.dumps({"default": "a", "instances": {"a": {"base_url": "x"}}}),
                   encoding="utf-8")
    monkeypatch.setenv("GRP_MCP_CONNECTIONS", str(bad))
    with pytest.raises(Exception) as exc_info:
        ui_mod._load()
    assert not isinstance(exc_info.value, ui_mod.ConfigNotFoundError)


# --- #6: editing a profile through the UI must preserve branch/max_file_bytes
# (and any other field the form doesn't expose), not silently reset them. ---

def test_save_profile_preserves_branch_and_max_file_bytes(tmp_path, monkeypatch):
    from grp_mcp import ui as ui_mod

    monkeypatch.setenv("GRP_MCP_CONNECTIONS", str(tmp_path / "connections.json"))
    existing = _inst(base_url="https://h/S", branch="HQ", max_file_bytes=200_000_000)
    cfg = Config(default="p", instances={"p": existing})
    monkeypatch.setattr(ui_mod, "_load", lambda: cfg)
    monkeypatch.setattr(ui_mod, "save_config", lambda c: None)

    handler = ui_mod._Handler.__new__(ui_mod._Handler)  # bypass BaseHTTPRequestHandler.__init__
    handler._json = lambda *a, **k: None
    ui_mod._Handler._save_profile(handler, {
        "name": "p", "base_url": "https://h/S2", "client_id": "cid",
        "username": "u", "tenant": "NewTenant",
    })
    saved = cfg.instances["p"]
    assert saved.branch == "HQ"                     # NOT reset to ""
    assert saved.max_file_bytes == 200_000_000        # NOT reset to the 50MB default
    assert saved.base_url == "https://h/S2"           # the field that WAS edited did change
    assert saved.tenant == "NewTenant"


def test_save_profile_new_profile_gets_defaults(tmp_path, monkeypatch):
    from grp_mcp import ui as ui_mod

    cfg = Config(default="", instances={})
    monkeypatch.setattr(ui_mod, "_load", lambda: cfg)
    monkeypatch.setattr(ui_mod, "save_config", lambda c: None)

    handler = ui_mod._Handler.__new__(ui_mod._Handler)
    handler._json = lambda *a, **k: None
    ui_mod._Handler._save_profile(handler, {
        "name": "new", "base_url": "https://h/S", "client_id": "cid",
        "client_secret": "sek", "username": "u", "password": "p",
    })
    saved = cfg.instances["new"]
    assert saved.branch == ""
    assert saved.max_file_bytes == 50_000_000


# ---- v0.62: ui_screen_action unknown-field escape hatch (2026-07-16 report) --
#
# grp-mcp's modern-plane /structure only ever exposes ONE container per view name.
# A screen whose classic SOAP schema disambiguates several containers bound to the
# SAME view as "ViewName", "ViewName: 1", "ViewName: 2" (multiple tabs reading the
# same DAC — proven live on PY309000: PayMode lives on "Employments: 2") has those
# numbered duplicates' fields completely absent from /structure. ui_screen_action's
# unknown-field check was unconditional (not even skip_validation could bypass it),
# so such a field could never be set through this tool. Fixed: skip_validation now
# also lets an unknown-to-structure field through, reported in unverifiable_fields
# (this plane's own read-back shares the same blind spot, so it's never silently
# treated as verified).

_PY_LIKE_STRUCT = {
    "views": {
        "Employments": [
            {"field": "BasicPay", "label": "Basic Pay", "type": "Decimal",
             "required": True, "readonly": False, "enabled": True,
             "options": None, "selector": None, "lookup": None},
        ],
        "Employees": [
            {"field": "EmployeeCD", "label": "Employee Code", "type": "String",
             "required": True, "readonly": False, "enabled": True,
             "options": None, "selector": None, "lookup": None},
        ],
    },
    "actions": [{"name": "Save", "label": "Save", "enabled": True, "visible": True,
                "confirm": None}],
    "grids": {"EmpPayTransactions": {"key_fields": ["EmpPayTransactionID"],
                                     "dac": "X", "columns": ["Amount"]}},
}


class _FakeUIScreenClient:
    """Stands in for ScreenClient in ui_screen_action tests — real network/login
    replaced with in-memory fakes for exactly the methods that function calls."""

    def __init__(self, struct, dirty_after_action=False, set_field_errors=None):
        self._struct = struct
        self._rejected_sets: list[dict] = []
        self._graph_dirty = None
        self._dirty_after_action = dirty_after_action
        self._set_field_errors = set_field_errors or {}
        self.set_calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_ui_structure(self):
        return self._struct

    async def ui_bootstrap(self, views):
        pass

    async def ui_navigate_record(self, view, key):
        pass

    async def ui_select_tree_node(self, view, key, parent_key):
        pass

    def ui_select_grid_row(self, view, key):
        pass

    async def ui_coerce_validate(self, sets):
        return sets, [], []  # no read-only/enum issues — isolates the unknown-field path

    async def ui_set_field(self, view, field, value):
        self.set_calls.append((view, field, value))
        if (view, field) in self._set_field_errors:
            raise ScreenError(f"ui_set_field {view}.{field}: refused")

    async def verify_sets(self, sets):
        return []

    async def reconcile_rejected_sets(self, entries):
        # no _rejected_sets are produced in these tests; keep all as genuine
        return list(entries), []

    async def ui_command(self, action, answer=None):
        return {"graphIsDirty": self._dirty_after_action}

    @staticmethod
    def _notices(result):
        return []


def _patch_screen_client(monkeypatch, client):
    monkeypatch.setattr(server, "ScreenClient", lambda inst, screen_id: client)


def test_ui_screen_action_unknown_field_raises_by_default(cfg, monkeypatch):
    client = _FakeUIScreenClient(_PY_LIKE_STRUCT)
    _patch_screen_client(monkeypatch, client)
    with pytest.raises(ScreenError, match="unknown field"):
        asyncio.run(server.ui_screen_action(
            "PY309000", "Save",
            set_fields=[{"view": "Employments", "field": "PayMode", "value": "C"}],
            instance="rw"))


def test_ui_screen_action_skip_validation_allows_unstructured_field(cfg, monkeypatch):
    client = _FakeUIScreenClient(_PY_LIKE_STRUCT)
    _patch_screen_client(monkeypatch, client)
    out = asyncio.run(server.ui_screen_action(
        "PY309000", "Save",
        set_fields=[{"view": "Employments", "field": "PayMode", "value": "C"}],
        skip_validation=True, instance="rw"))
    assert out["ok"] is True
    assert ("Employments", "PayMode", "C") in client.set_calls  # actually attempted
    assert len(out["unverifiable_fields"]) == 1
    assert out["unverifiable_fields"][0]["view"] == "Employments"
    assert out["unverifiable_fields"][0]["field"] == "PayMode"
    assert "structure" in out["unverifiable_fields"][0]["reason"].lower()
    assert "unverifiable_fields" in out["warning"]


def test_ui_screen_action_skip_validation_still_blocks_grid_column(cfg, monkeypatch):
    # a GRID column is a different mistake (wrong tool) — must still raise even
    # with skip_validation=true, not silently downgraded like a genuinely-unknown field.
    client = _FakeUIScreenClient(_PY_LIKE_STRUCT)
    _patch_screen_client(monkeypatch, client)
    with pytest.raises(ScreenError, match="GRID column"):
        asyncio.run(server.ui_screen_action(
            "PY309000", "Save",
            set_fields=[{"view": "EmpPayTransactions", "field": "Amount", "value": 5}],
            skip_validation=True, instance="rw"))


def test_ui_screen_action_known_field_has_no_unverifiable_fields(cfg, monkeypatch):
    client = _FakeUIScreenClient(_PY_LIKE_STRUCT)
    _patch_screen_client(monkeypatch, client)
    out = asyncio.run(server.ui_screen_action(
        "PY309000", "Save",
        set_fields=[{"view": "Employments", "field": "BasicPay", "value": 1000}],
        instance="rw"))
    assert out["ok"] is True
    assert "unverifiable_fields" not in out
    assert ("Employments", "BasicPay", 1000) in client.set_calls


# ---- screen_capabilities: graceful degrade on a /structure server bug ------
#
# Proven live on EP203000: Acumatica's own /structure metadata-builder throws an
# unhandled .NET Dictionary duplicate-key exception (two fields/views collide
# under an internal key) and returns a bare HTTP 500. screen_capabilities exists
# to tell the caller which plane to use -- crashing here instead of answering
# that question is exactly backwards, so it must degrade to classic-SOAP-only
# guidance rather than propagate the exception.

class _RaisingUIScreenClient:
    """Stands in for ScreenClient (real network/login bypassed) — get_ui_structure
    raises whatever error the test hands it, to exercise screen_capabilities'
    error-handling branch without touching a real instance."""

    def __init__(self, error: ScreenError):
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get_ui_structure(self, refresh=False):
        raise self._error


def test_screen_capabilities_degrades_on_structure_server_bug(cfg, monkeypatch):
    err = ScreenError(
        "get_ui_structure EP203000: SERVER-SIDE BUG in Acumatica's /structure "
        "endpoint for this screen (not a grp-mcp or caller issue) — its own "
        "metadata-builder throws \"An item with the same key has already been "
        "added\" ... Use screen_get_schema (classic SOAP) instead.")
    _patch_screen_client(monkeypatch, _RaisingUIScreenClient(err))
    out = asyncio.run(server.screen_capabilities("EP203000", instance="ro"))
    assert out["screen_id"] == "EP203000"
    assert out["grids"] == {} and out["actions"] == [] and out["selector_fields"] == []
    assert "SERVER-SIDE BUG" in out["modern_plane_unavailable"]
    tools = {r["tool"] for r in out["recommendations"]}
    assert any("screen_get_schema" in t for t in tools)
    assert any("diagnose_save_error" in t for t in tools)


def test_screen_capabilities_reraises_other_screen_errors(cfg, monkeypatch):
    # Only the specific "structure server bug" message is swallowed into a
    # graceful degrade -- any OTHER ScreenError (auth failure, real caller
    # mistake, etc.) must still propagate normally, not be silently hidden.
    err = ScreenError("get_ui_structure X: NOT AUTHENTICATED — session expired.")
    _patch_screen_client(monkeypatch, _RaisingUIScreenClient(err))
    with pytest.raises(ScreenError, match="NOT AUTHENTICATED"):
        asyncio.run(server.screen_capabilities("X", instance="ro"))


def test_classic_grid_missing_routing():
    """#3: an ASPX call that failed because the grid has no classic binding is
    detected (so the caller is routed to modern-plane tools) — but a genuine
    grid-not-on-page-yet or a business error is NOT swallowed as that case."""
    from grp_mcp.server import _classic_grid_missing, _no_classic_grid_result
    from grp_mcp.screen import ScreenError

    assert _classic_grid_missing(ScreenError(
        "aspx: no control bound to view 'ETDetails' on this page "
        "(dataMember not found in the page HTML)"))
    assert _classic_grid_missing(ScreenError(
        "aspx: page has no control config declarations — not a classic "
        "WebForms page?"))
    # NOT the no-classic-grid case: an ordinary validation/business error
    assert not _classic_grid_missing(ScreenError(
        "Percent should be 100 for sum of all banks"))
    assert not _classic_grid_missing(ScreenError("session lost"))

    r = _no_classic_grid_result("CA202000", "ETDetails", "http://x/CA202000.aspx",
                                ScreenError("no control bound to view 'ETDetails'"))
    assert r["no_classic_grid"] is True and r["ok"] is False
    assert "ui_delete_grid_row" in r["recommend"]


# ---- build_company_tree indent sequence (EP204060) ---------------------------
#
# The indent is off-by-one AND absolute: the Right presses issued after inserting
# node N set node N+1's level, and the level resets each step. Measured live
# 2026-07-20 with a 4-node probe (0/1/0/1 presses -> ROOT / ROOT / child-of-#2 /
# ROOT). The old code fired Right `depth` times on the row it had just inserted,
# which mis-nested every tree deeper than one level. Pin the command sequence.

class _TreeBuildClient:
    """Captures the command stream build_company_tree emits."""

    def __init__(self):
        self.ops: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def ui_bootstrap(self, views):
        pass

    async def ui_insert_grid_row(self, view, values, skip_validation=False):
        self.ops.append(f"insert:{values['Description']}")

    async def ui_command(self, cmd, answer=None):
        self.ops.append(cmd)
        return {"graphIsDirty": False}


def test_build_company_tree_indent_is_offset_by_one_and_absolute(cfg, monkeypatch):
    client = _TreeBuildClient()
    monkeypatch.setattr(server, "ScreenClient", lambda inst, screen_id: client)

    class _Dac:
        async def run_dac(self, *a, **k):
            return {"value": []}          # empty instance; skips + verifies vacuously

    monkeypatch.setattr(server, "_client", lambda inst: _Dac())
    asyncio.run(server.build_company_tree(
        {"name": "R", "children": [{"name": "A", "children": ["C"]}, "B"]},
        instance="rw"))

    # depths are R=0, A=1, C=2, B=1 -> each step issues the NEXT node's depth
    assert client.ops == [
        "insert:R", "Right", "Save",                 # next (A) is depth 1
        "insert:A", "Right", "Right", "Save",        # next (C) is depth 2
        "insert:C", "Right", "Save",                 # next (B) is depth 1
        "insert:B", "Save",                          # last -> none
    ]


def test_build_company_tree_flat_list_needs_no_indent(cfg, monkeypatch):
    client = _TreeBuildClient()
    monkeypatch.setattr(server, "ScreenClient", lambda inst, screen_id: client)

    class _Dac:
        async def run_dac(self, *a, **k):
            return {"value": []}

    monkeypatch.setattr(server, "_client", lambda inst: _Dac())
    asyncio.run(server.build_company_tree(["X", "Y"], instance="rw"))
    assert client.ops == ["insert:X", "Save", "insert:Y", "Save"]
