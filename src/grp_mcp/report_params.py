"""Deterministic classifier for classic report-screen parameters.

Classic report screens (AP630500, AP631200, GL632000, AR631000, …) do NOT publish a
machine-readable contract for HOW to submit each parameter: `ui_get_structure` fails,
the SOAP GetSchema carries only field NAMES (no control types), and the launcher's
server HTML has no descriptor — the parameter widgets are built client-side by bundle
JS. The ONE machine-readable source is the RENDERED DOM: after the JS runs, each
parameter widget's CSS class + its initial `_state` value deterministically encode its
wire shape, and its element id encodes the control-ID template.

This module turns those two DOM signals into a wire SHAPE, so an unfamiliar report
screen can be characterised once (via `PROBE_JS` in a browser) and then driven headless
with no per-field guessing. The mapping was verified live across AP630500 / AP631200 /
AR631000 (AP+AR+GL modules), 2026-07-23 — see KNOWLEDGE.md §19.

Shapes (how `set_report_parameters` emits each):
  combo         `_state=<PText Value="{code}"/>` + `$text`   (dropdown; code from options)
  lookup        `_state=<PXSelector Value="{v}"/>` + `$text`  (magnifier selector)
  bare_name     bare control name = v, `_state`=""            (Company/Branch tree selector)
  bare_selector `$text` only, NO `_state`                     (financial-period selector*)
  date          `$text` only (dd/mm/yyyy)                     (date picker)
  bool          `$text` = true/false                          (checkbox — UNVERIFIED)
  text          `$text` only                                  (plain text / integer)

* bare_selector is the ONE genuine ambiguity: a financial-period field renders with the
  SAME `selector` CSS class as a lookup, but sending `_state` CORRUPTS a dash-containing
  period value (verified: `<PXSelector Value="03-2026"/>` -> the report prints
  "Fin. Period: 03- 202"). So a period selector must post bare `$text`. The DOM cannot
  distinguish it from a lookup by class alone, so it is separated by field name below.
"""

from __future__ import annotations

# Financial-period selectors: `selector` CSS class like a lookup, but `_state` corrupts
# their dash-containing value — must post bare `$text`. Extend if another period-style
# field turns up (verify first: does its `_state` garble the value?).
PERIOD_SELECTOR_FIELDS: set[str] = {"PeriodID"}

# Wire shapes this classifier can emit. `set_report_parameters` maps each to a form shape.
SHAPES = ("combo", "lookup", "bare_name", "bare_selector", "date", "bool", "text")


def classify_param(raw_field: str, widget_class: str | None,
                   state_init: str | None) -> str:
    """Map a report parameter's two DOM signals to a wire shape (pure, unit-testable).

    raw_field:    the DAC field name (screen_get_schema's Parameters container `field`).
    widget_class: the parameter widget element's CSS class (from the rendered DOM).
    state_init:   the widget's initial `_state` hidden-input value (URL-decoded).

    The initial `_state` is the STRONGEST signal when non-empty — a field with a default
    value shows its own wrapper (`<PText …>` for a combo, `<PXSelector …>` for a lookup),
    which is unambiguous. When `_state` is empty (no default), fall back to the widget
    class. The one class-ambiguous case (a period selector vs a normal lookup, both
    `selector`) is split by PERIOD_SELECTOR_FIELDS.
    """
    si = (state_init or "").strip().lower()
    wc = (widget_class or "").lower()
    # 1) A non-empty default state wrapper is definitive.
    if si.startswith("<ptext"):
        return "combo"
    if si.startswith("<pxselector"):
        return "lookup"
    # 2) Distinctive widget classes.
    if "branch-selector" in wc:
        return "bare_name"
    if "qp-datetime" in wc or "datetime" in wc:
        return "date"
    if "qp-checkbox" in wc or "checkbox" in wc:
        return "bool"
    if "drop-down" in wc or "dropdown" in wc:
        return "combo"
    # 3) A magnifier selector — lookup, UNLESS it's a period selector (_state corrupts it).
    if "selector" in wc:
        return "bare_selector" if raw_field in PERIOD_SELECTOR_FIELDS else "lookup"
    # 4) qp-integer / qp-text / anything else -> plain bare `$text`.
    return "text"


# A browser-run probe that reads the machine-readable contract straight off the rendered
# launcher DOM. Run it in the report screen's page (or its launcher iframe); it returns
# one row per parameter — {field, template, widgetClass, stateInit} — ready to feed to
# classify_param(). This is the ONLY step that needs a browser; the resulting contract is
# cached and every later render of that screen is pure HTTP.
PROBE_JS = r"""
(function(){
  function scan(doc){
    var states = Array.prototype.slice.call(
      doc.querySelectorAll('[id*="_par_tab_"][id$="_state"]'));
    if(!states.length) return null;
    var out = [];
    for(var i=0;i<states.length;i++){
      var s = states[i], base = s.id.replace(/_state$/,'');
      var m = base.match(/_par_tab_t\d+_(pForm_)?ed(.+)$/);
      if(!m) continue;                                   // skip container rows
      var wrap = doc.getElementById(base);
      var val = s.value||''; try{ val = decodeURIComponent(val); }catch(e){}
      out.push({
        field: m[2],
        template: base.indexOf('_pForm_ed')>-1 ? 'pForm' : 'tab-direct',
        stateInit: val.slice(0,60),
        widgetClass: wrap ? (wrap.className||'').split(' ')
          .filter(function(x){return x && x!=='au-target' && x.indexOf('size')!==0;})
          .join(' ') : null
      });
    }
    return out;
  }
  var res = scan(document);
  if(!res){
    var fr = document.querySelectorAll('iframe');
    for(var i=0;i<fr.length;i++){ try{ var r=scan(fr[i].contentDocument); if(r){res=r;break;} }catch(e){} }
  }
  return res;
})()
"""


def build_contract(probed_fields: list[dict]) -> dict[str, dict]:
    """Turn PROBE_JS output into a `{raw_field: {"shape": ...}}` contract (pure).

    Each probed field is {field, widgetClass, stateInit, template}. Ignores rows whose
    field couldn't be classified into a real parameter (container artefacts). The
    `template` is not stored per field — `set_report_parameters` emits under BOTH
    templates regardless (the server binds whichever exists), so a screen-wide template
    is informational only.
    """
    contract: dict[str, dict] = {}
    for f in probed_fields or []:
        raw = f.get("field")
        if not raw:
            continue
        shape = classify_param(raw, f.get("widgetClass"), f.get("stateInit"))
        contract[raw] = {"shape": shape}
    return contract
