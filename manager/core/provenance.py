"""Pure helpers that describe *how* a test suite was produced.

These functions operate on plain ``dict`` representations of scenarios/steps (call ``model_dump()`` on
pydantic models first) and are shared by the generator agent (which records provenance while
generating) and the Visualize tests page (which renders that evidence).
"""

import re
from typing import Any

TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")

_STEP_PHASES = ("setup_steps", "test_steps", "teardown_steps")


def new_generation_report() -> dict[str, Any]:
    """Returns an empty provenance report skeleton for one generation run."""
    return {
        "version": 1,
        "generated_at": None,
        "model": None,
        "provider": None,
        "credentials_available": False,
        "endpoint_count": 0,
        "cluster_count": 0,
        "clusters": {},
        "scenarios": {},
        "summary": {},
    }


def is_fallback_scenario(sc: dict[str, Any]) -> bool:
    """True when the journey came from the deterministic fallback builder (Gemini synthesis degraded for its domain)."""
    sc_id = str(sc.get("scenario_id") or "")
    if sc_id.startswith("fallback_"):
        return True
    step_ids = [str(s.get("step_id") or "") for s in (sc.get("test_steps") or [])]
    return sc_id.startswith("neg_") and bool(step_ids) and all(s.startswith(("fallback_", "neg_")) for s in step_ids)


def infer_scenario_source(sc: dict[str, Any]) -> str:
    """Best-effort provenance from naming conventions (used for sessions generated before provenance tracking)."""
    sc_id = str(sc.get("scenario_id") or "")
    if sc_id.startswith("global_"):
        return "template"
    if is_fallback_scenario(sc):
        return "fallback"
    return "gemini"


def scenario_steps(sc: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns setup + test + teardown steps in execution order."""
    steps: list[dict[str, Any]] = []
    for phase in _STEP_PHASES:
        steps.extend(s for s in (sc.get(phase) or []) if isinstance(s, dict))
    return steps


def template_refs(value: Any) -> set[str]:
    """Collects every ``{{variable}}`` reference inside a (possibly nested) value."""
    refs: set[str] = set()
    if isinstance(value, str):
        refs.update(TEMPLATE_REF_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            refs |= template_refs(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            refs |= template_refs(v)
    return refs


def count_template_refs(value: Any) -> int:
    """Counts ``{{variable}}`` occurrences inside a (possibly nested) value."""
    if isinstance(value, str):
        return len(TEMPLATE_REF_RE.findall(value))
    if isinstance(value, dict):
        return sum(count_template_refs(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(count_template_refs(v) for v in value)
    return 0


def resource_of(path_template: str) -> str:
    """Returns the last literal (non-parameter) path segment, e.g. ``/v1/products/{id}/reviews`` -> ``reviews``."""
    segments = [s for s in str(path_template or "").strip("/").split("/") if s and not s.startswith("{")]
    return segments[-1] if segments else ""


def _step_consumes(step: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for key in ("path_params", "query_params", "request_body", "headers"):
        refs |= template_refs(step.get(key))
    return refs


def scenario_stats(sc: dict[str, Any]) -> dict[str, Any]:
    """Per-journey metrics used to compare Gemini-designed journeys against the deterministic baseline."""
    steps = scenario_steps(sc)
    captures = sum(len(st.get("captures") or []) for st in steps)
    chained = sum(len(_step_consumes(st)) for st in steps)
    negative = sum(1 for st in steps if int(st.get("expected_status") or 0) >= 400)
    assertions = sum(len(st.get("semantic_assertions") or []) for st in steps)
    resources = sorted({resource_of(st.get("path_template", "")) for st in steps} - {""})
    methods = sorted({str(st.get("method") or "").upper() for st in steps} - {""})
    return {
        "steps": len(steps),
        "captures": captures,
        "chained_refs": chained,
        "negative_steps": negative,
        "assertions": assertions,
        "resources": resources,
        "methods": methods,
    }


def aggregate_stats(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Sums ``scenario_stats`` across journeys (plus the journey count and distinct resources touched)."""
    totals: dict[str, Any] = {"journeys": len(scenarios), "steps": 0, "captures": 0, "chained_refs": 0, "negative_steps": 0, "assertions": 0}
    resources: set[str] = set()
    for sc in scenarios:
        st = scenario_stats(sc)
        for key in ("steps", "captures", "chained_refs", "negative_steps", "assertions"):
            totals[key] += int(st[key])
        resources.update(st["resources"])
    totals["resources"] = sorted(resources)
    return totals


def data_flow_edges(steps: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    """Returns ``(producer_index, consumer_index, variable)`` edges for captured values reused by later steps."""
    edges: list[tuple[int, int, str]] = []
    producers: dict[str, int] = {}
    for idx, step in enumerate(steps):
        for var in sorted(_step_consumes(step)):
            if var in producers and producers[var] != idx:
                edges.append((producers[var], idx, var))
        for cap in step.get("captures") or []:
            name = cap.get("variable_name") if isinstance(cap, dict) else None
            if name:
                producers.setdefault(str(name), idx)
    return edges


def _is_ref(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


def _step_label(step: dict[str, Any]) -> str:
    return f"{str(step.get('method') or '').upper()} {step.get('path_template') or ''}".strip()


def diff_generated_scenarios(
    raw_scenarios: list[dict[str, Any]],
    final_scenarios: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Describes what the deterministic guardrails changed between Gemini's raw output and the final journeys.

    Returns ``{scenario_id: [{"step_id", "kind", "detail"}, ...]}`` where ``kind`` is one of
    ``status_forced``, ``body_backfilled``, ``required_fields_added``, ``body_chained``, ``path_chained``,
    ``captures_added``, ``negative_cleaned``, ``step_dropped`` or ``steps_reordered``.
    """
    raw_by_id = {str(sc.get("scenario_id")): sc for sc in raw_scenarios}
    result: dict[str, list[dict[str, Any]]] = {}

    for final_sc in final_scenarios:
        sc_id = str(final_sc.get("scenario_id"))
        raw_sc = raw_by_id.get(sc_id)
        if raw_sc is None:
            continue
        adjustments: list[dict[str, Any]] = []
        is_negative = str(final_sc.get("scenario_type") or "").upper() == "BOUNDARY_NEGATIVE"

        raw_steps = scenario_steps(raw_sc)
        final_steps = scenario_steps(final_sc)
        raw_by_step = {str(st.get("step_id")): st for st in raw_steps}
        final_ids = [str(st.get("step_id")) for st in final_steps]

        surviving_raw_order = [str(st.get("step_id")) for st in raw_steps if str(st.get("step_id")) in final_ids]
        if surviving_raw_order != [sid for sid in final_ids if sid in surviving_raw_order]:
            adjustments.append({"step_id": None, "kind": "steps_reordered", "detail": "Destructive steps (DELETE / cancel) moved to the end of the journey"})

        for raw_st in raw_steps:
            if str(raw_st.get("step_id")) not in final_ids:
                adjustments.append({
                    "step_id": str(raw_st.get("step_id")),
                    "kind": "step_dropped",
                    "detail": f"Dropped {_step_label(raw_st)} expecting {raw_st.get('expected_status')} (404 is not a valid expectation on a collection endpoint)",
                })

        for final_st in final_steps:
            st_id = str(final_st.get("step_id"))
            raw_st = raw_by_step.get(st_id)
            if raw_st is None:
                continue

            if raw_st.get("expected_status") != final_st.get("expected_status"):
                adjustments.append({
                    "step_id": st_id,
                    "kind": "status_forced",
                    "detail": f"expected_status {raw_st.get('expected_status')} → {final_st.get('expected_status')} (happy-path steps must expect 2XX)",
                })

            raw_body = raw_st.get("request_body") if isinstance(raw_st.get("request_body"), dict) else {}
            final_body = final_st.get("request_body") if isinstance(final_st.get("request_body"), dict) else {}
            if final_body and not raw_body:
                adjustments.append({
                    "step_id": st_id,
                    "kind": "body_backfilled",
                    "detail": f"Request body synthesized from the OpenAPI schema ({', '.join(sorted(final_body))}) — Gemini left it empty",
                })
            elif final_body:
                added = sorted(k for k in final_body if k not in raw_body)
                if added:
                    adjustments.append({
                        "step_id": st_id,
                        "kind": "required_fields_added",
                        "detail": f"Required field(s) added from the schema: {', '.join(added)}",
                    })
                chained = sorted(k for k in final_body if k in raw_body and raw_body[k] != final_body[k] and _is_ref(final_body[k]))
                if chained:
                    adjustments.append({
                        "step_id": st_id,
                        "kind": "body_chained",
                        "detail": "Body field(s) re-pointed at captured values: " + ", ".join(f"{k} → {final_body[k]}" for k in chained),
                    })

            raw_pp = raw_st.get("path_params") if isinstance(raw_st.get("path_params"), dict) else {}
            final_pp = final_st.get("path_params") if isinstance(final_st.get("path_params"), dict) else {}
            chained_pp = sorted(k for k, v in final_pp.items() if _is_ref(v) and raw_pp.get(k) != v)
            if chained_pp:
                adjustments.append({
                    "step_id": st_id,
                    "kind": "path_chained",
                    "detail": "Path parameter(s) re-pointed at captured values: " + ", ".join(f"{k} → {final_pp[k]}" for k in chained_pp),
                })

            raw_caps = {str(c.get("variable_name")) for c in (raw_st.get("captures") or []) if isinstance(c, dict)}
            final_caps = [c for c in (final_st.get("captures") or []) if isinstance(c, dict)]
            added_caps = [c for c in final_caps if str(c.get("variable_name")) not in raw_caps]
            if added_caps:
                adjustments.append({
                    "step_id": st_id,
                    "kind": "captures_added",
                    "detail": "Capture(s) added so later steps can chain IDs: " + ", ".join(f"{c.get('variable_name')} ← {c.get('json_path')}" for c in added_caps),
                })

            if is_negative and (raw_st.get("captures") or raw_st.get("semantic_assertions")) and not (final_st.get("captures") or final_st.get("semantic_assertions")):
                adjustments.append({
                    "step_id": st_id,
                    "kind": "negative_cleaned",
                    "detail": "Captures and assertions removed from a negative (4XX) step",
                })

        result[sc_id] = adjustments
    return result


def diff_step_fields(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Lists field-level changes a healing patch made to a step (``{"field", "before", "after"}``)."""
    changes: list[dict[str, Any]] = []
    for key in ("path_params", "query_params", "request_body", "headers"):
        b = before.get(key) if isinstance(before.get(key), dict) else {}
        a = after.get(key) if isinstance(after.get(key), dict) else {}
        for field in sorted(set(b) | set(a)):
            if b.get(field) != a.get(field):
                changes.append({"field": f"{key}.{field}", "before": b.get(field), "after": a.get(field)})
    if before.get("expected_status") != after.get("expected_status"):
        changes.append({"field": "expected_status", "before": before.get("expected_status"), "after": after.get("expected_status")})
    b_assert = len(before.get("semantic_assertions") or [])
    a_assert = len(after.get("semantic_assertions") or [])
    if b_assert != a_assert:
        changes.append({"field": "semantic_assertions", "before": f"{b_assert} assertion(s)", "after": f"{a_assert} assertion(s)"})
    return changes
