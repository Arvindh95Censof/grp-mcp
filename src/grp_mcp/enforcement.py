"""Enforcement layer — tool classification registry + post-write verification.

This module is the single source of truth for what every registered MCP tool
*does* (its risk class) and, from that, how the server should gate it and
whether its result must carry post-write verification.

Why a registry (and not just the existing per-tool ``_require_*`` calls):
  - The gates in ``server.py`` are scattered across ~66 call sites, several of
    them *runtime* branches (a tool that write-gates normally but delete-gates
    when the action is destructive — e.g. ``ui_screen_action``/``screen_submit``).
    You cannot see, from the outside, that every mutating tool is actually
    guarded. The registry makes the intended class explicit and a test
    (``test_smoke``) cross-checks it against the real ``_require_*`` calls via
    AST, so an unguarded write cannot slip in unnoticed.
  - The class also drives the post-write verification contract (see
    ``VerifyState``): a ``write``/``delete`` result should say whether the change
    was actually read back, because Acumatica routinely returns success-shaped
    no-ops (KNOWLEDGE.md §-passim). A clean 200 is not proof.

Design note — this registry documents and checks; it does NOT re-route the
existing gates. Ripping out working, branch-sensitive ``_require_*`` calls to
funnel them through a decorator would be a large, risky behaviour change for no
safety gain. The gates stay where they are; the registry keeps them honest.
"""

from __future__ import annotations

# --- tool classes -----------------------------------------------------------

READ = "read"                       # no mutation, no local file write
WRITE = "write"                     # creates/updates an ERP record or field
DELETE = "delete"                   # removes an ERP record/row
PUBLISH = "publish"                 # customization/endpoint publish-import-unpublish
BG_JOB = "bg_job"                   # long-running ERP mutation (has a status poller)
DIAGNOSTIC_WRITE = "diagnostic_write"  # replays a real Save for diagnosis
FILESYSTEM = "filesystem"           # renders/exports a local file, no ERP data mutation
ADMIN = "admin"                     # connector-level (instances/sessions/config)

TOOL_CLASSES = frozenset(
    {READ, WRITE, DELETE, PUBLISH, BG_JOB, DIAGNOSTIC_WRITE, FILESYSTEM, ADMIN}
)

# Classes that mutate ERP data/config and therefore MUST pass a write/delete/
# publish gate (checked against source in the test suite). ``admin`` gates on
# GRP_MCP_ALLOW_ADMIN for the *persisting* variants only, and ``filesystem`` is
# gated by the read/write-roots sandbox, so both are excluded here.
ERP_MUTATION_CLASSES = frozenset({WRITE, DELETE, PUBLISH, BG_JOB, DIAGNOSTIC_WRITE})

# Classes whose tool result should carry a post-write verification verdict
# (see verify_state / VerifyState). Publish/bg_job verify via their own status
# pollers; admin/filesystem have their own proof (config read-back / file on
# disk), so they are not in this set.
VERIFY_REQUIRED_CLASSES = frozenset({WRITE, DELETE, DIAGNOSTIC_WRITE})

# Mutation-class tools that intentionally call NO ``_require_*`` gate, with the
# reason. The gate-coverage test consults this allowlist. Keep it tiny and
# justified — every entry is a hole someone deliberately left open.
GATELESS_MUTATION_ALLOW = {
    # Fills the in-memory graph and returns the coerced values; performs no
    # Save, so there is nothing to persist and nothing to gate. A subsequent
    # screen_submit (which IS gated) does the actual write.
    "screen_autofill": "graph-only field fill, no Save",
}

# Tools whose gate is applied via a delegated helper rather than lexically in
# the tool body (AST cannot see through the call). Excluded from the
# lexical-gate consistency check; the delegated gate is real at runtime.
DELEGATED_GATE = {
    # calls extend_endpoint(), which performs _require_publish(instance)
    "generate_endpoint_entity": PUBLISH,
}


# --- the registry -----------------------------------------------------------
# name -> class. Every registered tool MUST appear here (coverage test). The
# class is the *semantic* risk of the tool; the gate-consistency test verifies
# that the class is backed by the appropriate real ``_require_*`` call(s).

TOOL_CLASS: dict[str, str] = {
    # -- connector / admin ---------------------------------------------------
    "add_instance": ADMIN,
    "set_active_instance": ADMIN,
    "remove_instance": ADMIN,
    "reload_config": ADMIN,          # re-reads connections.json into memory
    "release_sessions": ADMIN,       # drops held ERP API sessions
    "list_instances": READ,
    "test_connection": READ,
    "whoami": READ,
    # -- entity REST reads ---------------------------------------------------
    "list_endpoints": READ,
    "list_entities": READ,
    "get_entity_schema": READ,
    "get_endpoint_definition": READ,
    "get_entity": READ,
    "fetch_all_entities": READ,
    "count_entity": READ,
    "list_actions": READ,
    "list_attachments": READ,
    "list_dacs": READ,
    "get_dac_metadata": READ,
    "run_dac_odata": READ,
    "list_generic_inquiries": READ,
    "run_generic_inquiry": READ,
    "list_screens": READ,
    "list_published": READ,
    # -- discovery / preflight / guidance (read-only) ------------------------
    "screen_get_schema": READ,
    "screen_get": READ,
    "screen_capabilities": READ,
    "screen_health": READ,
    "screen_preflight": READ,
    "screen_prereqs": READ,
    "module_setup_plan": READ,
    "stock_scenario_info": READ,
    "validate_import_setup": READ,
    "setup_readiness": READ,
    "get_setup_guidance": READ,
    "guide": READ,
    "knowledge": READ,
    "tree_triage": READ,
    "ui_get_structure": READ,
    "ui_preflight": READ,
    "ui_resolve_selector": READ,
    "ui_lookup": READ,
    "ui_read_grid": READ,
    "report_param_probe": READ,
    # -- status pollers (read-only) ------------------------------------------
    "activate_features_status": READ,
    "load_status": READ,
    "poll_action": READ,
    "publish_status": READ,
    # -- entity REST writes --------------------------------------------------
    "create_or_update_entity": WRITE,
    "invoke_action": WRITE,
    "set_note": WRITE,
    "attach_file": WRITE,
    "attach_file_to_provider": WRITE,
    "ensure_entity_on_endpoint": WRITE,
    # -- modern UI-JSON writes -----------------------------------------------
    "ui_screen_action": WRITE,
    "ui_grid_row_action": WRITE,
    "ui_tree_dialog_insert": WRITE,
    "ui_populate_endpoint_entity_fields": WRITE,
    "ui_update_grid_row": WRITE,
    "ui_update_grid_rows": WRITE,
    "ui_insert_grid_row": WRITE,
    "ui_run_process": BG_JOB,        # runs a screen process; write+delete gated
    # -- classic SOAP / ASPX writes ------------------------------------------
    "screen_submit": WRITE,
    "screen_insert_rows": WRITE,
    "screen_bulk_load": WRITE,
    "screen_record": WRITE,
    "screen_autofill": WRITE,        # graph-only fill (gateless, allowlisted)
    "screen_discover_prereqs": WRITE,  # probes by attempting writes
    "import_screen_xml": WRITE,
    "aspx_grid_batch": WRITE,
    "aspx_tree_node_action": WRITE,
    # -- financial / setup writes --------------------------------------------
    "create_financial_calendar": WRITE,
    "create_ledger": WRITE,
    "set_gl_preferences": WRITE,
    "chart_of_accounts": WRITE,
    "build_company_tree": WRITE,
    "add_workgroup_member": WRITE,
    "build_approval_map": WRITE,
    "generate_master_calendar": WRITE,
    "manage_financial_periods": WRITE,
    "create_numbering_sequence": WRITE,
    "create_segmented_key": WRITE,
    "set_segment_value": WRITE,
    "setup_data_provider": WRITE,
    "build_import_scenario": WRITE,
    "enable_features": WRITE,
    # -- deletes -------------------------------------------------------------
    "delete_entity": DELETE,
    "ui_delete_grid_row": DELETE,
    "aspx_delete_grid_row": DELETE,
    "delete_financial_year": DELETE,
    "delete_segmented_key": DELETE,
    "reset_calendar": DELETE,        # clears the calendar (delete-gated)
    # -- long-running mutations (status-polled) ------------------------------
    "activate_features": BG_JOB,
    "load_from_excel": BG_JOB,
    "import_excel": BG_JOB,
    "run_import_scenario": BG_JOB,
    # -- diagnostic write ----------------------------------------------------
    "diagnose_save_error": DIAGNOSTIC_WRITE,
    # -- publish / customization / endpoint ----------------------------------
    "generate_endpoint_entity": PUBLISH,   # delegated gate (extend_endpoint)
    "import_customization": PUBLISH,
    "publish_customization": PUBLISH,
    "unpublish_customization": PUBLISH,
    # -- filesystem (render/export a local file; no ERP data mutation) -------
    "export_screen_xml": FILESYSTEM,
    "export_customization": FILESYSTEM,
    "snapshot_entity": FILESYSTEM,
    "download_file": FILESYSTEM,
    "run_report": FILESYSTEM,
    "download_classic_report": FILESYSTEM,   # ASPX POST => also write-gated
    "download_filter_report": FILESYSTEM,    # ASPX POST => also write-gated
}


# EVERY ERP-mutation tool runs the write-preflight (consult kb-mcp-dual + KNOWLEDGE.md,
# honour the enforcement level) — either inline (the four arteries below) or via the
# @_preflight_write decorator. A test asserts full coverage against source, so a new
# mutation tool cannot ship unwired.
#
# The four wired INLINE (their bodies have bespoke skip/stamp logic):
PREFLIGHT_INLINE = frozenset({
    "screen_submit",           # classic SOAP screens (skips dry_run, stamps result)
    "create_or_update_entity", # REST entities
    "ui_screen_action",        # modern UI screens (skips Cancel/Repaint)
    "delete_entity",           # REST deletes
})

# Mutation-class tools deliberately NOT preflight-wired, with the reason. Kept tiny
# and justified — each is a mutation class that does not perform a user-intended
# persisting write, so a KB consult would be noise.
PREFLIGHT_EXEMPT = {
    # graph-only field fill, no Save — nothing persists (also GATELESS_MUTATION_ALLOW)
    "screen_autofill": "graph-only fill, no Save",
    # a discovery probe that *attempts* writes to learn prerequisites; it is a
    # read-intent tool, not a user write, so consulting the KB before it is noise
    "screen_discover_prereqs": "prerequisite discovery probe, not a user write",
}


def classify(tool_name: str) -> str | None:
    """Return the risk class for a registered tool, or None if unclassified."""
    return TOOL_CLASS.get(tool_name)


def is_erp_mutation(tool_name: str) -> bool:
    """True if the tool mutates ERP data/config (write/delete/publish/bg/diag)."""
    return TOOL_CLASS.get(tool_name) in ERP_MUTATION_CLASSES


def requires_post_write_verify(tool_name: str) -> bool:
    """True if the tool's result should carry a post-write verification verdict."""
    return TOOL_CLASS.get(tool_name) in VERIFY_REQUIRED_CLASSES


# --- post-write verification state machine (task #2) ------------------------

VERIFIED = "verified"      # read-back persisted state matches what we sent
REJECTED = "rejected"      # the server refused the change (error surfaced)
UNVERIFIED = "unverified"  # success-shaped, but persistence could NOT be proven

VERIFY_STATES = frozenset({VERIFIED, REJECTED, UNVERIFIED})


def verify_state(*, changed: bool | None, readable: bool, matched: bool | None) -> str:
    """Collapse read-back signals into one terminal verification state.

    NO ``rolled_back`` state exists: automatic rollback across grp-mcp's five
    planes is unsafe (no cross-plane transaction, creates have side effects,
    delete-to-undo hits referential guards), so a failed verification is
    surfaced loudly as ``rejected``/``unverified`` and left for the caller to
    resolve — never silently reversed.

    Args:
        changed: did the write path itself report the server refused it?
                 True  -> the change was refused          -> REJECTED
                 (None/False means "the call looked OK", fall through)
        readable: can the target actually be read back? Some grids are
                 read-back-inert by construction (e.g. PY309000 child grids,
                 grid_rows_readable=False); False here => UNVERIFIED, never a
                 false REJECTED.
        matched: did the read-back match the expected persisted state?
                 True -> VERIFIED, False -> REJECTED, None -> UNVERIFIED.
    """
    if changed is True:
        return REJECTED
    if not readable:
        return UNVERIFIED
    if matched is True:
        return VERIFIED
    if matched is False:
        return REJECTED
    return UNVERIFIED


# The read-back flags the classic/modern planes already emit, and how each maps
# onto the three verdicts. A tool result may carry several; the WORST wins
# (REJECTED > UNVERIFIED > VERIFIED) — one refuted change poisons the batch.
_TRISTATE_FLAGS = ("save_verified", "delete_verified", "select_verified", "all_verified")


def normalize_verification(result: object) -> str | None:
    """Collapse whatever read-back flags a tool result carries into ONE verdict.

    Reads the existing scattered signals (save_verified / delete_verified /
    select_verified / all_verified — each True | False | "unverified"/None —
    plus grid_rows_readable=False and graph_is_dirty=True) and returns
    VERIFIED / REJECTED / UNVERIFIED, or None when the result carries no
    verifiable signal at all (e.g. a plain REST create with no read-back).

    Worst-case precedence: any REJECTED -> REJECTED; else any UNVERIFIED ->
    UNVERIFIED; else VERIFIED. A clean 200 with no read-back is None, NOT
    verified — absence of proof is never proof (KNOWLEDGE.md: success-shaped
    no-ops are real).
    """
    if not isinstance(result, dict):
        return None
    signals: list[str] = []
    for key in _TRISTATE_FLAGS:
        if key in result:
            v = result[key]
            if v is True:
                signals.append(VERIFIED)
            elif v is False:
                signals.append(REJECTED)
            else:  # "unverified" / None
                signals.append(UNVERIFIED)
    # a grid that cannot be read back can never prove persistence
    if result.get("grid_rows_readable") is False:
        signals.append(UNVERIFIED)
    # still-dirty graph after a save-intending op => not committed
    if result.get("graph_is_dirty") is True:
        signals.append(UNVERIFIED)
    if not signals:
        return None
    if REJECTED in signals:
        return REJECTED
    if UNVERIFIED in signals:
        return UNVERIFIED
    return VERIFIED


def stamp_verification(result: object) -> object:
    """Attach a unified ``verification`` verdict to a write/delete result dict,
    derived from its existing read-back flags. No-op if the result carries no
    verifiable signal, or already has a ``verification`` key. Returns the same
    object for convenient inline use."""
    if isinstance(result, dict) and "verification" not in result:
        verdict = normalize_verification(result)
        if verdict is not None:
            result["verification"] = verdict
    return result
