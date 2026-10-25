"""Server-rendered "Visualize tests" page.

Every generated journey is drawn as a flowchart and annotated with provenance evidence: which
journeys Gemini designed (versus the deterministic baseline builder), which deterministic guardrails
adjusted Gemini's output, and which steps the validator agent repaired at execution time.
"""

import html
import json
from typing import Any
from urllib.parse import quote

from manager.core.provenance import (
    data_flow_edges,
    infer_scenario_source,
    scenario_stats,
    scenario_steps,
)
from ui_theme import icon, render_page

MERMAID_VERSION = "11.17.2"
MERMAID_CDN_URL = f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}/dist/mermaid.min.js"
# Subresource Integrity hash of the pinned bundle above (sha384 of dist/mermaid.min.js). Recompute when bumping
# MERMAID_VERSION:  curl -sL <url> | openssl dgst -sha384 -binary | openssl base64 -A
MERMAID_SRI = "sha384-EOXBFmc3gx5mb+vn0vPvvGqACToJD24hhacX5Yx+8NUUQrHIle/Qi5Bg9o3zKwW2"

SOURCE_LABELS = {
    "gemini": "Designed by Gemini",
    "fallback": "Deterministic baseline",
    "template": "Built-in template",
}
SOURCE_BADGES = {"gemini": "badge-info", "fallback": "badge-neutral", "template": "badge-neutral"}
TYPE_LABELS = {
    "HAPPY_PATH_E2E": "Happy path",
    "BOUNDARY_NEGATIVE": "Negative",
    "SECURITY_PERMUTATION": "Security",
    "IDEMPOTENCY": "Idempotency",
}
TYPE_BADGES = {
    "HAPPY_PATH_E2E": "badge-success",
    "BOUNDARY_NEGATIVE": "badge-warning",
    "SECURITY_PERMUTATION": "badge-danger",
    "IDEMPOTENCY": "badge-heal",
}
REASON_LABELS = {
    "no_credentials": "no Gemini credentials were configured",
    "timeout": "the Gemini call timed out",
    "rate_limited": "Gemini quota / rate limits were hit",
    "empty_response": "Gemini returned an empty response",
    "empty_after_guardrails": "every journey Gemini proposed was rejected by the guardrails",
    "no_response": "no usable Gemini response was received",
}

VISUALIZER_CSS = """
.vz-legend { display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 13px; font-weight: 600; color: var(--text-2); margin: 14px 0 18px; }
.vz-swatch { display: inline-block; width: 12px; height: 12px; border-radius: 3px; border: 1.5px solid; margin-right: 6px; vertical-align: -1px; }
.vz-swatch--gemini { background: #e8f0fe; border-color: #1a73e8; }
.vz-swatch--base { background: #f1f3f4; border-color: #80868b; }
.vz-swatch--negative { background: #fef7e0; border-color: #f9ab00; }
.vz-swatch--healed { background: #e0f5f2; border-color: #0b8a7a; }
.vz-journey { margin-bottom: 16px; }
.vz-journey .card__header h2 { font-size: 16px; }
.vz-desc { font-size: 14px; color: var(--text-2); margin: 0 0 14px; }
.vz-journey__grid { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); gap: 20px; }
@media (max-width: 920px) { .vz-journey__grid { grid-template-columns: 1fr; } }
.vz-chart { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; overflow: auto; }
.vz-chart .mermaid { display: flex; justify-content: center; min-height: 72px; margin: 0; font-family: inherit; white-space: pre; }
.vz-chart svg { max-width: 100%; height: auto; }
.vz-h { font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--text-3); margin: 0 0 8px; }
.vz-panel { border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; background: var(--surface); }
.vz-panel + .vz-panel { margin-top: 12px; }
.vz-panel--gemini { border-color: var(--primary-border); background: #f4f8ff; }
.vz-panel--heal { border-color: var(--heal-border); background: var(--heal-soft); }
.vz-panel__title { display: flex; align-items: center; gap: 8px; font-weight: 700; font-size: 14px; margin-bottom: 8px; }
.vz-panel--gemini .vz-panel__title { color: var(--primary-text); }
.vz-panel--heal .vz-panel__title { color: var(--heal-text); }
.vz-panel p { font-size: 13.5px; line-height: 1.55; margin: 0; }
.vz-list { margin: 0; padding-left: 18px; font-size: 13.5px; line-height: 1.55; }
.vz-list li + li { margin-top: 4px; }
.vz-heal { padding-top: 10px; margin-top: 10px; border-top: 1px dashed var(--heal-border); }
.vz-heal:first-of-type { padding-top: 0; margin-top: 0; border-top: 0; }
.vz-err { font-family: var(--font-mono); font-size: 12px; color: var(--danger-text); background: var(--danger-soft); border: 1px solid var(--danger-border); border-radius: 4px; padding: 6px 8px; margin: 8px 0; overflow-wrap: anywhere; }
.vz-change { font-family: var(--font-mono); font-size: 12.5px; }
.vz-change s { color: var(--text-3); }
.vz-steps { display: flex; flex-direction: column; gap: 8px; }
.vz-step { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 8px 10px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface-2); font-size: 13px; }
.vz-step__idx { width: 22px; height: 22px; border-radius: 6px; border: 1px solid var(--border); background: var(--surface); display: grid; place-items: center; font-size: 11px; font-weight: 700; color: var(--text-3); flex: none; }
.vz-step code { font-weight: 600; }
.vz-step__op { color: var(--text-3); font-family: var(--font-mono); font-size: 12px; }
.vz-step__meta { margin-left: auto; display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.vz-table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
.vz-table th { text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-3); padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
.vz-table td { padding: 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
.vz-table tr:last-child td { border-bottom: 0; }
.vz-num { font-weight: 700; font-variant-numeric: tabular-nums; }
.vz-vs { display: block; font-size: 12px; color: var(--text-3); font-weight: 500; white-space: nowrap; }
.vz-reason { display: block; font-size: 12px; color: var(--text-3); font-weight: 500; max-width: 46ch; margin-top: 4px; line-height: 1.4; overflow-wrap: anywhere; }
.vz-footnote { font-size: 13px; color: var(--text-3); margin-top: 20px; max-width: 90ch; line-height: 1.55; }
"""

MERMAID_INIT_JS = (
    "(function () {"
    "  if (!window.mermaid) {"
    "    document.querySelectorAll('.vz-chart').forEach(function (el) {"
    "      var note = document.createElement('div');"
    "      note.className = 'muted';"
    "      note.textContent = 'Flowchart renderer could not be loaded (offline?). Showing the graph definition instead.';"
    "      el.insertBefore(note, el.firstChild);"
    "    });"
    "    return;"
    "  }"
    "  mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'strict', "
    "flowchart: { htmlLabels: true, curve: 'basis', padding: 10 }, "
    "themeVariables: { fontFamily: 'ui-sans-serif, system-ui, -apple-system, \"Segoe UI\", Roboto, sans-serif', fontSize: '13px' } });"
    "})();"
)


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _mermaid_text(value: Any) -> str:
    """Escapes user data for use inside a quoted Mermaid node label (entity codes, per Mermaid's syntax)."""
    text = "" if value is None else str(value)
    return (
        text.replace("#", "#35;")
        .replace("&", "#amp;")
        .replace('"', "#quot;")
        .replace("<", "#lt;")
        .replace(">", "#gt;")
    )


def _mermaid_edge_text(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(value or ""))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _fmt_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return "∅" if value is None else str(value)


def build_mermaid_graph(
    steps: list[dict[str, Any]],
    source: str,
    healed_step_ids: set[str],
    adjusted_step_ids: set[str],
) -> str:
    """Builds the Mermaid ``graph TD`` source for one journey, with data-flow edges and provenance classes."""
    lines = ["graph TD"]
    classes: dict[str, list[str]] = {"gemini": [], "base": [], "negative": [], "healed": []}
    for idx, step in enumerate(steps):
        step_id = str(step.get("step_id") or "")
        markers = [f"expect {step.get('expected_status')}"]
        if step_id in healed_step_ids:
            markers.append("healed")
        elif step_id in adjusted_step_ids:
            markers.append("guardrail")
        method = _mermaid_text(str(step.get("method") or "").upper())
        path = _mermaid_text(step.get("path_template") or "")
        lines.append(f'    S{idx}["<b>{method}</b> {path}<br/><small>{" · ".join(markers)}</small>"]')
        if idx > 0:
            lines.append(f"    S{idx - 1} --> S{idx}")
        if step_id in healed_step_ids:
            classes["healed"].append(f"S{idx}")
        elif int(step.get("expected_status") or 0) >= 400:
            classes["negative"].append(f"S{idx}")
        elif source == "gemini":
            classes["gemini"].append(f"S{idx}")
        else:
            classes["base"].append(f"S{idx}")

    seen: set[tuple[int, int, str]] = set()
    for producer, consumer, var in data_flow_edges(steps):
        if (producer, consumer, var) in seen:
            continue
        seen.add((producer, consumer, var))
        lines.append(f"    S{producer} -. {_mermaid_edge_text(var)} .-> S{consumer}")

    lines.append("    classDef gemini fill:#e8f0fe,stroke:#1a73e8,color:#185abc,stroke-width:1.5px;")
    lines.append("    classDef base fill:#f1f3f4,stroke:#80868b,color:#3c4043,stroke-width:1.5px;")
    lines.append("    classDef negative fill:#fef7e0,stroke:#f9ab00,color:#7a5200,stroke-width:1.5px;")
    lines.append("    classDef healed fill:#e0f5f2,stroke:#0b8a7a,color:#0b6b5f,stroke-width:3px;")
    for cls_name, node_ids in classes.items():
        if node_ids:
            lines.append(f"    class {','.join(node_ids)} {cls_name};")
    return "\n".join(lines)


def resolve_provenance(sc: dict[str, Any], report: dict[str, Any] | None) -> dict[str, Any]:
    """Returns the provenance record for a journey, inferring it from naming conventions when no report exists."""
    sc_id = str(sc.get("scenario_id") or "")
    recorded = (report or {}).get("scenarios", {}).get(sc_id) if isinstance(report, dict) else None
    if isinstance(recorded, dict) and recorded.get("source"):
        info = dict(recorded)
        info.setdefault("adjustments", [])
        info.setdefault("stats", scenario_stats(sc))
        info["inferred"] = False
        return info
    return {
        "source": infer_scenario_source(sc),
        "cluster": None,
        "model": (report or {}).get("model") if isinstance(report, dict) else None,
        "reason": None,
        "adjustments": [],
        "stats": scenario_stats(sc),
        "inferred": True,
    }


def _render_gemini_panel(prov: dict[str, Any], sc: dict[str, Any], steps: list[dict[str, Any]], cluster_entry: dict[str, Any] | None) -> str:
    stats = prov.get("stats") or scenario_stats(sc)
    type_label = TYPE_LABELS.get(str(sc.get("scenario_type") or ""), "test")
    resources = stats.get("resources") or []
    items: list[str] = []
    res_html = ", ".join(f"<code>{_esc(r)}</code>" for r in resources) if resources else "the domain"
    items.append(f"Composed a <strong>{int(stats.get('steps', 0))}-step {_esc(type_label.lower())}</strong> journey across {res_html}.")

    flows: dict[str, list[int]] = {}
    for producer, consumer, var in data_flow_edges(steps):
        flows.setdefault(f"{var}@{producer}", []).append(consumer + 1)
    if int(stats.get("captures", 0)) or flows:
        flow_bits = ", ".join(
            f"<code>{_esc(key.split('@')[0])}</code> #{int(key.split('@')[1]) + 1} → {', '.join(f'#{c}' for c in sorted(set(consumers)))}"
            for key, consumers in flows.items()
        )
        items.append(
            f"Captured <strong>{_plural(int(stats.get('captures', 0)), 'value')}</strong> and chained them into "
            f"<strong>{_plural(int(stats.get('chained_refs', 0)), 'downstream field')}</strong>{(': ' + flow_bits) if flow_bits else ''}."
        )
    if int(stats.get("negative_steps", 0)):
        items.append(f"Added <strong>{_plural(int(stats['negative_steps']), 'negative test')}</strong> expecting a 4XX rejection.")
    if int(stats.get("assertions", 0)):
        items.append(f"Added <strong>{_plural(int(stats['assertions']), 'semantic assertion')}</strong> on response fields.")

    baseline = (cluster_entry or {}).get("baseline_stats") if isinstance(cluster_entry, dict) else None
    if isinstance(baseline, dict):
        items.append(
            "<span class=\"muted\">Without Gemini this domain would get "
            f"{_plural(int(baseline.get('journeys', 0)), 'baseline journey')} with {_plural(int(baseline.get('steps', 0)), 'test')}, "
            f"{_plural(int(baseline.get('chained_refs', 0)), 'chained value')}, {_plural(int(baseline.get('negative_steps', 0)), 'negative test')} "
            f"and {_plural(int(baseline.get('assertions', 0)), 'assertion')}.</span>"
        )
    model_html = f' <span class="chip"><code>{_esc(prov.get("model"))}</code></span>' if prov.get("model") else ""
    return (
        '<div class="vz-panel vz-panel--gemini">'
        f'<div class="vz-panel__title">{icon("sparkles")} Gemini designed this journey{model_html}</div>'
        f'<ul class="vz-list">{"".join(f"<li>{it}</li>" for it in items)}</ul>'
        "</div>"
    )


def _render_guardrail_panel(prov: dict[str, Any], step_index: dict[str, int]) -> str:
    adjustments = prov.get("adjustments") or []
    if adjustments:
        items = []
        for adj in adjustments:
            st_id = adj.get("step_id")
            prefix = f"<strong>#{step_index[st_id]}</strong> " if st_id in step_index else ""
            items.append(f"<li>{prefix}{_esc(adj.get('detail'))}</li>")
        body = f'<ul class="vz-list">{"".join(items)}</ul>'
    elif prov.get("inferred"):
        body = "<p>Guardrail details were not recorded for this session (generated before provenance tracking).</p>"
    else:
        body = "<p>No adjustments were needed — Gemini's output passed every guardrail unchanged.</p>"
    return (
        '<div class="vz-panel">'
        f'<div class="vz-panel__title">{icon("shield")} Deterministic guardrails</div>'
        f"{body}"
        "</div>"
    )


def _render_baseline_panel(prov: dict[str, Any]) -> str:
    reason = str(prov.get("reason") or "")
    if reason in REASON_LABELS:
        why = REASON_LABELS[reason]
    elif reason.startswith("error:"):
        why = f"the Gemini call failed ({_esc(reason[6:].strip())})"
    elif prov.get("inferred"):
        why = "Gemini synthesis was unavailable for this domain"
    else:
        why = "Gemini synthesis degraded for this domain"
    return (
        '<div class="vz-panel">'
        f'<div class="vz-panel__title">{icon("layers")} Deterministic baseline</div>'
        f"<p>Built by the deterministic baseline builder because {why}. Steps follow CRUD order, "
        "IDs are chained heuristically and request payloads come from the schema's examples.</p>"
        "</div>"
    )


def _render_template_panel() -> str:
    return (
        '<div class="vz-panel">'
        f'<div class="vz-panel__title">{icon("lock")} Built-in probe</div>'
        "<p>Security and idempotency probes are fixed templates applied to the first endpoint — not generated by Gemini.</p>"
        "</div>"
    )


def _render_healing_panel(entries: list[dict[str, Any]], step_index: dict[str, int], step_labels: dict[str, str], source: str) -> str:
    if not entries:
        return ""
    title = "Enhanced by Gemini at execution time" if source != "gemini" and any(e.get("source") == "gemini" for e in entries) else "Self-healing at execution time"
    blocks: list[str] = []
    for entry in entries:
        heal_src = str(entry.get("source") or "deterministic")
        src_badge = (
            f'<span class="badge badge-heal">{icon("sparkles")} Healed by Gemini</span>'
            if heal_src == "gemini"
            else '<span class="badge badge-neutral">Deterministic repair</span>'
        )
        st_id = str(entry.get("step_id") or "")
        where = f"Test #{step_index[st_id]} · {_esc(step_labels.get(st_id, ''))}" if st_id in step_index else _esc(st_id)
        outcome = entry.get("re_run_success")
        outcome_html = (
            '<span class="badge badge-success">Re-run passed</span>'
            if outcome is True
            else ('<span class="badge badge-danger">Re-run failed</span>' if outcome is False else "")
        )
        model_html = f' <span class="chip"><code>{_esc(entry.get("model"))}</code></span>' if entry.get("model") else ""
        changes = entry.get("changes") or []
        changes_html = (
            '<ul class="vz-list vz-change">'
            + "".join(
                f"<li>{_esc(ch.get('field'))}: <s>{_esc(_fmt_value(ch.get('before')))}</s> → {_esc(_fmt_value(ch.get('after')))}</li>"
                for ch in changes
            )
            + "</ul>"
            if changes
            else ""
        )
        error_html = f'<div class="vz-err">{_esc(str(entry.get("error") or "")[:400])}</div>' if entry.get("error") else ""
        blocks.append(
            '<div class="vz-heal">'
            f'<div class="row">{src_badge}{model_html}<span class="muted">{where}</span>{outcome_html}</div>'
            f"{error_html}"
            f"<p>{_esc(entry.get('reasoning'))}</p>"
            f"{changes_html}"
            "</div>"
        )
    return (
        '<div class="vz-panel vz-panel--heal">'
        f'<div class="vz-panel__title">{icon("activity")} {title}</div>'
        f"{''.join(blocks)}"
        "</div>"
    )


def _render_journey(
    sc: dict[str, Any],
    prov: dict[str, Any],
    cluster_entry: dict[str, Any] | None,
    heal_entries: list[dict[str, Any]],
    last_run: dict[str, Any] | None,
) -> str:
    steps = scenario_steps(sc)
    source = str(prov.get("source") or "gemini")
    step_index = {str(st.get("step_id")): i + 1 for i, st in enumerate(steps)}
    step_labels = {str(st.get("step_id")): f"{str(st.get('method') or '').upper()} {st.get('path_template') or ''}" for st in steps}
    healed_ids = {str(e.get("step_id")) for e in heal_entries if e.get("step_id")}
    adjusted_ids = {str(a.get("step_id")) for a in (prov.get("adjustments") or []) if a.get("step_id")}
    sc_type = str(sc.get("scenario_type") or "")

    badges = [
        f'<span class="badge {TYPE_BADGES.get(sc_type, "badge-neutral")}">{_esc(TYPE_LABELS.get(sc_type, sc_type or "journey"))}</span>',
        f'<span class="badge {SOURCE_BADGES.get(source, "badge-neutral")}">{_esc(SOURCE_LABELS.get(source, source))}</span>',
    ]
    if adjusted_ids or prov.get("adjustments"):
        badges.append(f'<span class="badge badge-neutral">{_plural(len(prov.get("adjustments") or []), "guardrail adjustment")}</span>')
    if heal_entries:
        gemini_heals = sum(1 for e in heal_entries if e.get("source") == "gemini")
        label = f"Healed by Gemini ×{gemini_heals}" if gemini_heals else f"Healed ×{len(heal_entries)}"
        badges.append(f'<span class="badge badge-heal">{icon("sparkles")} {_esc(label)}</span>')
    if isinstance(last_run, dict) and last_run.get("status") in {"SUCCESS", "FAILED"}:
        ok = last_run.get("status") == "SUCCESS"
        badges.append(f'<span class="badge {"badge-success" if ok else "badge-danger"}">Last run {"passed" if ok else "failed"}</span>')

    if source == "gemini":
        panels = _render_gemini_panel(prov, sc, steps, cluster_entry) + _render_guardrail_panel(prov, step_index)
    elif source == "template":
        panels = _render_template_panel()
    else:
        panels = _render_baseline_panel(prov)
    panels += _render_healing_panel(heal_entries, step_index, step_labels, source)

    step_rows: list[str] = []
    step_statuses = (last_run or {}).get("steps") if isinstance(last_run, dict) else {}
    for i, st in enumerate(steps):
        st_id = str(st.get("step_id") or "")
        method = str(st.get("method") or "").upper()
        tags: list[str] = []
        if st_id in healed_ids:
            tags.append('<span class="badge badge-heal">Healed</span>')
        elif st_id in adjusted_ids:
            tags.append('<span class="badge badge-neutral">Guardrail</span>')
        for cap in st.get("captures") or []:
            if isinstance(cap, dict):
                tags.append(f'<span class="chip"><code>{_esc(cap.get("variable_name"))}</code> ← <code>{_esc(cap.get("json_path"))}</code></span>')
        st_status = str((step_statuses or {}).get(st_id) or "").upper()
        if st_status in {"SUCCESS", "PASSED"}:
            tags.append(f'<span class="muted" title="Last run passed">{icon("check-circle")}</span>')
        elif st_status == "FAILED":
            tags.append(f'<span class="muted" title="Last run failed">{icon("x-circle")}</span>')
        step_rows.append(
            '<div class="vz-step">'
            f'<span class="vz-step__idx" title="Test ID: {_esc(st_id)}">{i + 1}</span>'
            f'<span class="method method-{_esc(method)}">{_esc(method)}</span>'
            f"<code>{_esc(st.get('path_template'))}</code>"
            f'<span class="vz-step__op">{_esc(st.get("operation_id"))}</span>'
            f'<span class="vz-step__meta"><span class="chip">→ {_esc(st.get("expected_status"))}</span>{"".join(tags)}</span>'
            "</div>"
        )

    graph = build_mermaid_graph(steps, source, healed_ids, adjusted_ids)
    return (
        f'<section class="card vz-journey" id="journey-{_esc(sc.get("scenario_id"))}">'
        '<div class="card__header">'
        f'<h2>{icon("layers")} {_esc(sc.get("name"))}</h2>'
        f'<div class="row">{"".join(badges)}</div>'
        "</div>"
        '<div class="card__body">'
        f'<p class="vz-desc">{_esc(sc.get("description"))}</p>'
        '<div class="vz-journey__grid">'
        f'<div><div class="vz-h">Execution flow</div><div class="vz-chart"><pre class="mermaid">{_esc(graph)}</pre></div></div>'
        f'<div><div class="vz-h">How this journey was built</div>{panels}</div>'
        "</div>"
        f'<div class="vz-h" style="margin-top:18px;">Tests</div><div class="vz-steps">{"".join(step_rows)}</div>'
        "</div>"
        "</section>"
    )


def _render_domain_comparison(report: dict[str, Any]) -> str:
    clusters = report.get("clusters") if isinstance(report, dict) else None
    if not isinstance(clusters, dict) or not clusters:
        return ""

    def cell(value: Any, baseline: Any, is_gemini: bool) -> str:
        vs = f'<span class="vz-vs">baseline {_esc(baseline)}</span>' if is_gemini and baseline is not None else ""
        return f'<td><span class="vz-num">{_esc(value)}</span>{vs}</td>'

    rows: list[str] = []
    for name, entry in clusters.items():
        if not isinstance(entry, dict):
            continue
        src = str(entry.get("source") or "fallback")
        stats = entry.get("stats") or {}
        base = entry.get("baseline_stats") or {}
        is_gemini = src == "gemini"
        how = f'<span class="badge {SOURCE_BADGES.get(src, "badge-neutral")}">{_esc(SOURCE_LABELS.get(src, src))}</span>'
        reason = str(entry.get("reason") or "")
        if not is_gemini and reason:
            how += f'<span class="vz-reason">{_esc(REASON_LABELS.get(reason, reason))}</span>'
        latency = entry.get("latency_ms")
        latency_html = f"{int(latency) / 1000:.1f}s" if isinstance(latency, (int, float)) and is_gemini else "—"
        rows.append(
            "<tr>"
            f"<td><strong>{_esc(str(name).replace('_', ' ').title())}</strong><span class=\"vz-vs\">{_plural(len(entry.get('operations') or []), 'endpoint')}</span></td>"
            f"<td>{how}</td>"
            + cell(stats.get("journeys", 0), base.get("journeys"), is_gemini)
            + cell(stats.get("steps", 0), base.get("steps"), is_gemini)
            + cell(stats.get("chained_refs", 0), base.get("chained_refs"), is_gemini)
            + cell(stats.get("negative_steps", 0), base.get("negative_steps"), is_gemini)
            + cell(stats.get("assertions", 0), base.get("assertions"), is_gemini)
            + f"<td>{latency_html}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<section class="card" style="margin-bottom:20px;">'
        f'<div class="card__header"><h2>{icon("sparkles")} Gemini vs. deterministic baseline, per API domain</h2>'
        '<span class="muted">Baseline = what the deterministic builder produces from the same spec</span></div>'
        '<div class="card__body" style="overflow-x:auto;">'
        '<table class="vz-table"><thead><tr><th>Domain</th><th>Built by</th><th>Journeys</th><th>Tests</th><th>Chained values</th><th>Negative tests</th><th>Assertions</th><th>Gemini latency</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def build_visualizer_page(
    session_id: str,
    suite: dict[str, Any],
    generation_report: dict[str, Any] | None = None,
    healing_log: list[dict[str, Any]] | None = None,
    execution_results: dict[str, Any] | None = None,
    api_title: str = "",
) -> str:
    """Renders the complete Visualize tests page for a session from its suite, provenance report, healing log and results."""
    scenarios = [sc for sc in (suite.get("scenarios") or []) if isinstance(sc, dict)]
    report = generation_report if isinstance(generation_report, dict) else None
    heals = [e for e in (healing_log or []) if isinstance(e, dict)]
    heals_by_sc: dict[str, list[dict[str, Any]]] = {}
    for entry in heals:
        heals_by_sc.setdefault(str(entry.get("scenario_id") or ""), []).append(entry)
    results = (execution_results or {}).get("scenarios") if isinstance(execution_results, dict) else {}
    clusters = report.get("clusters") if report else {}

    provenance = {str(sc.get("scenario_id")): resolve_provenance(sc, report) for sc in scenarios}
    counts = {"gemini": 0, "fallback": 0, "template": 0}
    adjustments_total = 0
    for prov in provenance.values():
        counts[str(prov.get("source"))] = counts.get(str(prov.get("source")), 0) + 1
        adjustments_total += len(prov.get("adjustments") or [])
    gemini_heals = sum(1 for e in heals if e.get("source") == "gemini")
    any_inferred = any(p.get("inferred") for p in provenance.values())
    model = (report or {}).get("model") or next((e.get("model") for e in heals if e.get("model")), None)

    sid_q = quote(session_id, safe="")
    header_actions = (
        f'<a class="btn btn-ghost btn-sm" href="/tests/{sid_q}">{icon("code")} View generated test code</a>'
        f'<a class="btn btn-primary btn-sm" href="/execution_dashboard/{sid_q}">{icon("play")} Open dashboard</a>'
    )

    chips = [f'<span class="chip" title="Target base URL">{icon("globe")}<code>{_esc(suite.get("target_base_url") or "—")}</code></span>']
    if model:
        chips.append(f'<span class="chip" title="Gemini model">{icon("sparkles")}<code>{_esc(model)}</code></span>')
    if report and report.get("generated_at"):
        chips.append(f'<span class="chip" title="Generated at">{icon("clock")}<code>{_esc(report.get("generated_at"))}</code></span>')
    chips.append(f'<span class="chip" title="Session {_esc(session_id)}">{icon("hash")}<code>{_esc(session_id[:8])}…</code></span>')

    total = len(scenarios)
    pct = f"{round(100 * counts['gemini'] / total)}% of journeys" if total else "—"
    stats_html = (
        '<div class="stats" style="margin-bottom:6px;">'
        f'<div class="stat"><div class="stat__label">Journeys</div><div class="stat__value">{total}</div></div>'
        f'<div class="stat"><div class="stat__label">Designed by Gemini</div><div class="stat__value">{counts["gemini"]}</div><div class="stat__caption">{_esc(pct)}</div></div>'
        f'<div class="stat"><div class="stat__label">Deterministic baseline</div><div class="stat__value">{counts["fallback"]}</div><div class="stat__caption">{_plural(counts["template"], "built-in probe")}</div></div>'
        f'<div class="stat"><div class="stat__label">Guardrail adjustments</div><div class="stat__value">{adjustments_total}</div><div class="stat__caption">deterministic fixes to Gemini output</div></div>'
        f'<div class="stat"><div class="stat__label">Self-heals</div><div class="stat__value">{len(heals)}</div><div class="stat__caption">{gemini_heals} by Gemini · {len(heals) - gemini_heals} deterministic</div></div>'
        "</div>"
    )
    legend_html = (
        '<div class="vz-legend">'
        '<span><i class="vz-swatch vz-swatch--gemini"></i>Designed by Gemini</span>'
        '<span><i class="vz-swatch vz-swatch--base"></i>Deterministic baseline / template</span>'
        '<span><i class="vz-swatch vz-swatch--negative"></i>Negative test (expects 4XX)</span>'
        '<span><i class="vz-swatch vz-swatch--healed"></i>Repaired at execution time</span>'
        "<span>⇢ dotted edge = captured value reused downstream</span>"
        "</div>"
    )
    inferred_note = (
        f'<div class="alert alert-info" style="margin-bottom:16px;">{icon("info")}<div>Provenance for this session is inferred from journey naming because it was generated before '
        "provenance tracking. New runs record exactly what Gemini designed, which guardrails adjusted it and every repair applied during execution.</div></div>"
        if any_inferred
        else ""
    )

    journeys_html = "".join(
        _render_journey(
            sc,
            provenance[str(sc.get("scenario_id"))],
            clusters.get(provenance[str(sc.get("scenario_id"))].get("cluster") or "") if isinstance(clusters, dict) else None,
            heals_by_sc.get(str(sc.get("scenario_id") or ""), []),
            results.get(str(sc.get("scenario_id"))) if isinstance(results, dict) else None,
        )
        for sc in scenarios
    ) or f'<div class="alert alert-info">{icon("info")}<div>This suite has no journeys yet.</div></div>'

    body = (
        '<main class="page">'
        '<div class="page-title hero"><div style="min-width:0;">'
        f"<h1>{_esc(api_title) + ' · ' if api_title else ''}Visualize tests</h1>"
        "<p>Each journey is drawn as a flowchart and annotated with evidence of how it was built: what Gemini designed, "
        "which deterministic guardrails adjusted it, and what the validator agent repaired against the live API.</p>"
        f'<div class="page-title__meta">{"".join(chips)}</div>'
        "</div></div>"
        f"{stats_html}{legend_html}{inferred_note}"
        f"{_render_domain_comparison(report) if report else ''}"
        '<h3 class="section-title">Journeys</h3>'
        f"{journeys_html}"
        '<p class="vz-footnote">Request payload values are synthesized from the OpenAPI schema (examples, defaults, enums) on every path. '
        "Gemini's contribution is the journey design — which operations form a workflow, how captured IDs chain across resources, "
        "which negative cases and assertions to include — plus the repairs it proposes when a step fails against the live API.</p>"
        "</main>"
        f'<script src="{MERMAID_CDN_URL}" integrity="{MERMAID_SRI}" crossorigin="anonymous"></script>'
    )
    return render_page(
        title="Visualize tests",
        body_html=body,
        header_title="Visualize tests",
        header_actions_html=header_actions,
        extra_css=VISUALIZER_CSS,
        scripts_js=MERMAID_INIT_JS,
    )
