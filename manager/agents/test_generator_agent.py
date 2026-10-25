"""LLM-driven TestGeneratorAgent for synthesizing modular OpenAPI integration test scenarios."""

import asyncio
from collections import defaultdict
import concurrent.futures
import copy
from datetime import datetime, timezone
import json
import logging
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from manager.core.graph import DependencyGraph
from manager.core.provenance import (
    aggregate_stats,
    diff_generated_scenarios,
    infer_scenario_source,
    new_generation_report,
    scenario_stats,
)
from manager.core.schemas import (
    DomainScenarioBatch,
    HTTPMethod,
    OperationNode,
    ParameterLocation,
    ParameterSchema,
    ScenarioType,
    TestScenario,
    TestStep,
    TestSuite,
    VariableCapture,
)
from manager.models import (
    OASMetadata,
    ParameterMetadata,
    TestCasePlan,
    TestPlan,
    WorkflowPlan,
    WorkflowStep,
)

try:
    import re._parser as sre_parse
except ImportError:
    import sre_parse

logger = logging.getLogger(__name__)


def sample_from_regex(pattern_str: str, param_name: str = "") -> str | None:
    """Synthesizes a minimal valid string matching a regular expression pattern."""
    if not pattern_str:
        return None
    try:
        ast = sre_parse.parse(pattern_str)

        def _walk(node_list: Any) -> str:
            out: list[str] = []
            for op, av in node_list:
                if op == sre_parse.LITERAL:
                    out.append(chr(av))
                elif op == sre_parse.NOT_LITERAL:
                    ch = "a" if chr(av) != "a" else "b"
                    out.append(ch)
                elif op == sre_parse.IN:
                    is_negated = any(item[0] == sre_parse.NEGATE for item in av)
                    if is_negated:
                        out.append("x")
                    else:
                        candidates = [item for item in av if item[0] == sre_parse.LITERAL]
                        if "zone" in param_name.lower() and any(chr(item[1]) == "Z" for item in candidates):
                            out.append("Z")
                            continue

                        first = av[0]
                        if first[0] == sre_parse.LITERAL:
                            out.append(chr(first[1]))
                        elif first[0] == sre_parse.RANGE:
                            out.append(chr(first[1][0]))
                        elif first[0] == sre_parse.CATEGORY:
                            cat = first[1]
                            if cat == sre_parse.CATEGORY_DIGIT:
                                out.append("1")
                            elif cat == sre_parse.CATEGORY_WORD:
                                out.append("a")
                            elif cat == sre_parse.CATEGORY_SPACE:
                                out.append(" ")
                            else:
                                out.append("x")
                        else:
                            out.append("a")
                elif op == sre_parse.BRANCH:
                    branches = av[1]
                    for b in branches:
                        sub = _walk(b)
                        if sub is not None:
                            out.append(sub)
                            break
                elif op == sre_parse.SUBPATTERN:
                    sub = _walk(av[3])
                    if sub is not None:
                        out.append(sub)
                elif op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT):
                    min_r, max_r, sub_ast = av
                    count = min_r if min_r > 0 else (1 if max_r >= 1 else 0)
                    for _ in range(count):
                        sub = _walk(sub_ast)
                        if sub:
                            out.append(sub)
                elif op == sre_parse.ANY:
                    out.append("a")
                elif op == sre_parse.AT:
                    pass
            return "".join(out)

        res = _walk(ast)
        if re.search(pattern_str, res):
            return res
        return None
    except Exception:
        return None


def get_dummy_param_value(param: ParameterMetadata) -> Any:
    """Generates a realistic dummy value for a parameter based on schema constraints."""
    if param.example is not None:
        return param.example
    if param.default is not None:
        return param.default
    if param.enum:
        return param.enum[0]
    if getattr(param, "pattern", None):
        pat_val = sample_from_regex(param.pattern, param.name)
        if pat_val:
            return pat_val

    fmt = (param.format or "").lower()
    param_type = (param.param_type or "string").lower()

    if fmt == "uuid":
        return "123e4567-e89b-12d3-a456-426614174000"
    if fmt in ["date", "date-time"]:
        return "2026-01-01T00:00:00Z"
    if param_type in ["int", "integer"]:
        return 1
    if param_type in ["number", "float", "double"]:
        return 1.0
    if param_type == "boolean":
        return True
    return f"sample_{param.name}"


def resolve_path_parameters(
    path: str,
    parameters: list[ParameterMetadata],
    test_config: dict[str, Any] | None = None,
) -> str:
    """Substitutes {parameter} in URL path with dummy or configured values."""
    param_map = {p.name: p for p in parameters if p.in_location == "path"}
    resolved = path
    placeholders = re.findall(r"\{([^}]+)\}", path)

    if not test_config:
        test_config = {}

    global_paths = test_config.get("global", {}).get(
        "path_parameters", test_config.get("path_parameters", {})
    )
    endpoint_paths = (
        test_config.get("endpoints", {}).get(path, {}).get("path_parameters", {})
    )

    for ph in placeholders:
        if ph in endpoint_paths:
            val = endpoint_paths[ph]
        elif ph in global_paths:
            val = global_paths[ph]
        else:
            param_obj = param_map.get(ph)
            if param_obj:
                val = get_dummy_param_value(param_obj)
            else:
                val = "123e4567-e89b-12d3-a456-426614174000"
        resolved = resolved.replace(f"{{{ph}}}", str(val))
    return resolved


def generate_mock_json_payload(
    schema: dict[str, Any], properties: dict[str, Any]
) -> dict[str, Any]:
    """Generates a valid dummy JSON payload matching schema types and required properties."""
    payload: dict[str, Any] = {}
    if not properties and isinstance(schema, dict) and "properties" in schema:
        properties = schema.get("properties", {})

    for prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            payload[prop_name] = f"sample_{prop_name}"
            continue

        if "example" in prop_def:
            payload[prop_name] = prop_def["example"]
            continue
        if "default" in prop_def:
            payload[prop_name] = prop_def["default"]
            continue
        if "enum" in prop_def and prop_def["enum"]:
            payload[prop_name] = prop_def["enum"][0]
            continue

        prop_type = prop_def.get("type", "string")
        fmt = prop_def.get("format", "")

        if fmt == "email":
            payload[prop_name] = "user@example.com"
        elif fmt == "uuid":
            payload[prop_name] = "123e4567-e89b-12d3-a456-426614174000"
        elif prop_type in ["int", "integer"]:
            payload[prop_name] = prop_def.get("minimum", 1)
        elif prop_type in ["number", "float"]:
            payload[prop_name] = float(prop_def.get("minimum", 1.0))
        elif prop_type == "boolean":
            payload[prop_name] = True
        elif prop_type == "array":
            payload[prop_name] = ["item_1"]
        elif prop_type == "object" or "properties" in prop_def:
            payload[prop_name] = generate_mock_json_payload(
                prop_def, prop_def.get("properties", {})
            )
        else:
            payload[prop_name] = f"sample_{prop_name}"

    return payload


def generate_invalid_json_payload(request_body: Any) -> dict[str, Any]:
    """Generates an intentionally invalid payload violating the schema (omitting required fields)."""
    payload = generate_mock_json_payload(
        request_body.raw_schema, request_body.properties
    )
    if request_body.required_properties and payload:
        first_required = request_body.required_properties[0]
        if first_required in payload:
            del payload[first_required]
            return payload

    return {}


def extract_domain_cluster(path: str, tags: list[str] | None = None) -> str:
    """Extracts a domain cluster name for any API route generically without hardcoding."""
    if tags:
        clean = re.sub(r"[^a-zA-Z0-9_]+", "_", tags[0].strip().lower()).strip("_")
        if clean:
            return clean

    segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    filtered = [
        s
        for s in segments
        if not re.match(r"^(api|v\d+|\d+\.\d+|rest|public)$", s, re.IGNORECASE)
    ]
    if filtered:
        return re.sub(r"[^a-zA-Z0-9_]+", "_", filtered[0].lower()).strip("_")
    if segments:
        return re.sub(r"[^a-zA-Z0-9_]+", "_", segments[0].lower()).strip("_")
    return "general"


def bundle_clusters(
    raw_clusters: dict[str, list[Any]],
    max_clusters: int = 10,
    min_independent_size: int = 3,
) -> dict[str, list[Any]]:
    """Groups operations into manageable domain bundles, preserving cohesive domains while bundling orphan endpoints."""
    sorted_desc = sorted(raw_clusters.items(), key=lambda x: len(x[1]), reverse=True)
    significant: list[tuple[str, list[Any]]] = []
    small: list[tuple[str, list[Any]]] = []

    for k, ops in sorted_desc:
        if len(ops) >= min_independent_size:
            significant.append((k, ops))
        else:
            small.append((k, ops))

    if len(significant) >= max_clusters:
        kept = dict(significant[: max_clusters - 1])
        leftover = significant[max_clusters - 1 :] + small
    else:
        if len(small) <= 1:
            return dict(significant + small)
        kept = dict(significant)
        leftover = small

    aux_ops: list[Any] = []
    for _, ops in leftover:
        aux_ops.extend(ops)
    if aux_ops:
        kept["auxiliary_endpoints"] = aux_ops
    return kept


_ACTION_POST_SEGMENTS = {
    "cancel",
    "restock",
    "apply-promotion",
    "apply_promotion",
    "activate",
    "deactivate",
    "archive",
    "unarchive",
    "restore",
    "verify",
    "reset",
    "approve",
    "reject",
    "submit",
    "publish",
    "unpublish",
    "close",
    "reopen",
    "lock",
    "unlock",
    "pay",
    "refund",
    "capture",
    "void",
    "clone",
    "copy",
    "move",
    "merge",
    "split",
    "retry",
    "trigger",
    "execute",
    "run",
    "start",
    "stop",
    "pause",
    "resume",
}

_GENERIC_ID_PREFIXES = {
    "id",
    "item",
    "resource",
    "record",
    "entity",
    "object",
    "target",
    "created",
    "captured",
    "fetched",
    "new",
}

_LIST_COLLECTION_KEYS = (
    "items",
    "@graph",
    "features",
    "data",
    "results",
    "records",
    "entry",
)


def _to_snake_case(name: str) -> str:
    """Converts camelCase, PascalCase, or kebab-case identifier to lowercase snake_case."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    return s.strip("_").lower()


def _singularize(word: str) -> str:
    """Normalizes a resource name to its singular lowercase stem."""
    w = re.sub(r"[^a-zA-Z0-9]+", "", word).lower()
    if w.endswith("ies") and len(w) > 3:
        return w[:-3] + "y"
    if w.endswith("ses") and len(w) > 3:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 2:
        return w[:-1]
    return w


def _is_action_post(path: str, success_status: int | None = None) -> bool:
    """Returns True if a POST route represents an action on an existing resource rather than creation."""
    p = path.lower().rstrip("/")
    if p.endswith("}"):
        return True
    if "{" not in p:
        return False
    last_seg = p.split("/")[-1]
    if (
        last_seg in _ACTION_POST_SEGMENTS
        or last_seg.startswith("apply-")
        or last_seg.startswith("mark-")
    ):
        return True
    if (
        success_status is not None
        and success_status != 201
        and not last_seg.endswith("s")
    ):
        return True
    return False


def _infer_step_resource_stem(path: str, cluster_name: str = "") -> str:
    """Infers the singular resource stem produced or targeted by an endpoint path."""
    segments = [
        s
        for s in path.strip("/").split("/")
        if s and not re.match(r"^(api|v\d+|\d+\.\d+|rest|public)$", s, re.IGNORECASE)
    ]
    static_segments = [
        s for s in segments if not (s.startswith("{") and s.endswith("}"))
    ]
    if static_segments:
        last_static = static_segments[-1]
        if (
            last_static.lower() in _ACTION_POST_SEGMENTS
            and len(static_segments) >= 2
        ):
            return _singularize(static_segments[-2])
        return _singularize(last_static)
    if cluster_name:
        return _singularize(cluster_name)
    return "resource"


def _infer_param_resource_stem(
    path: str, param_name: str, cluster_name: str = ""
) -> str | None:
    """Infers which resource stem a path parameter ({id}, {product_id}, {productId}) refers to."""
    snake = _to_snake_case(param_name)
    if snake != "id" and not snake.endswith("_id"):
        return None

    segments = [
        s
        for s in path.strip("/").split("/")
        if s and not re.match(r"^(api|v\d+|\d+\.\d+|rest|public)$", s, re.IGNORECASE)
    ]
    static_stems = {
        _singularize(s)
        for s in segments
        if not s.startswith("{") and s.lower() not in _ACTION_POST_SEGMENTS
    }

    if snake.endswith("_id"):
        prefix = snake[:-3].strip("_")
        for adj in ("created_", "captured_", "fetched_", "new_", "target_"):
            if prefix.startswith(adj) and len(prefix) > len(adj):
                prefix = prefix[len(adj):]
        if prefix and prefix not in _GENERIC_ID_PREFIXES:
            param_stem = _singularize(prefix)
            if not static_stems or param_stem in static_stems or len(static_stems) < 2:
                return param_stem

    target_token = f"{{{param_name}}}"
    for idx, seg in enumerate(segments):
        if seg == target_token or target_token in seg:
            for prev_idx in range(idx - 1, -1, -1):
                prev_seg = segments[prev_idx]
                if (
                    not prev_seg.startswith("{")
                    and prev_seg.lower() not in _ACTION_POST_SEGMENTS
                ):
                    return _singularize(prev_seg)
            break

    if cluster_name:
        return _singularize(cluster_name)
    return "resource"


def _var_name_for_stem(stem: str, cluster_name: str) -> str:
    """Returns canonical context variable name for a resource stem within a cluster."""
    if cluster_name and stem == _singularize(cluster_name):
        return f"{cluster_name}_id"
    return f"{stem}_id"


def _stem_from_variable_name(var_name: str, fallback_stem: str = "") -> str:
    """Extracts resource stem from a variable name like created_product_id or captured_cwsu_id."""
    snake = _to_snake_case(var_name)
    if snake.endswith("_id"):
        prefix = snake[:-3].strip("_")
        for adj in ("created_", "captured_", "fetched_", "new_", "target_"):
            if prefix.startswith(adj) and len(prefix) > len(adj):
                prefix = prefix[len(adj):]
        if prefix and prefix not in _GENERIC_ID_PREFIXES:
            return _singularize(prefix)
    return fallback_stem


def _inject_context_into_payload(
    payload: Any,
    captured_by_stem: dict[str, str],
) -> Any:
    """Recursively wires captured foreign-key IDs (e.g. product_id, customer_id) into request payloads."""
    if isinstance(payload, dict):
        updated: dict[str, Any] = {}
        for k, v in payload.items():
            snake = _to_snake_case(k)
            if snake != "id" and snake.endswith("_id"):
                fk_stem = _singularize(snake[:-3])
                if fk_stem and fk_stem in captured_by_stem:
                    if not (
                        isinstance(v, str)
                        and ("{{" in v or v.startswith("$"))
                    ):
                        updated[k] = f"{{{{{captured_by_stem[fk_stem]}}}}}"
                        continue
            updated[k] = _inject_context_into_payload(v, captured_by_stem)
        return updated
    if isinstance(payload, list):
        return [
            _inject_context_into_payload(item, captured_by_stem)
            for item in payload
        ]
    return payload


def _extract_schema_props(schema_dict: dict[str, Any]) -> dict[str, Any]:
    """Extracts properties from an OpenAPI schema dict, including allOf/anyOf/oneOf compositions."""
    merged: dict[str, Any] = {}
    if not isinstance(schema_dict, dict):
        return merged
    if "properties" in schema_dict and isinstance(schema_dict["properties"], dict):
        merged.update(schema_dict["properties"])
    for comp_key in ("allOf", "anyOf", "oneOf"):
        if comp_key in schema_dict and isinstance(schema_dict[comp_key], list):
            for sub in schema_dict[comp_key]:
                merged.update(_extract_schema_props(sub))
    return merged


def _infer_op_captures(
    op: dict[str, Any],
    cluster_name: str,
    all_cluster_ops: list[dict[str, Any]],
) -> list[VariableCapture]:
    """Determines VariableCapture rules for a POST creation or GET collection/lookup endpoint."""
    method = str(op.get("method", "GET")).upper()
    path = str(op.get("path", "/"))
    p_clean = path.rstrip("/")
    resp_props: list[str] = list(op.get("response_properties") or [])
    step_stem = _infer_step_resource_stem(path, cluster_name)
    primary_var = _var_name_for_stem(step_stem, cluster_name)

    captures: list[VariableCapture] = []
    seen_vars: set[str] = set()

    def add_capture(var_name: str, json_path: str) -> None:
        if var_name not in seen_vars:
            seen_vars.add(var_name)
            captures.append(
                VariableCapture(variable_name=var_name, json_path=json_path)
            )

    # Check which resource stems and foreign-key fields are consumed by any operation in the cluster
    consumed_stems: set[str] = set()
    consumed_body_fk_fields: dict[str, str] = {}
    for other_op in all_cluster_ops:
        other_path = str(other_op.get("path", "/"))
        for ph in re.findall(r"\{([^}]+)\}", other_path):
            st = _infer_param_resource_stem(other_path, ph, cluster_name)
            if st:
                consumed_stems.add(st)
        body_props = list(other_op.get("request_body_properties") or [])
        if isinstance(other_op.get("request_body"), dict):
            rb_props = other_op["request_body"].get("properties")
            if isinstance(rb_props, dict):
                body_props.extend(rb_props.keys())
        for body_prop in body_props:
            bp_snake = _to_snake_case(body_prop)
            if bp_snake != "id" and bp_snake.endswith("_id"):
                fk_stem = _singularize(bp_snake[:-3])
                if fk_stem:
                    consumed_stems.add(fk_stem)
                    if fk_stem != step_stem:
                        consumed_body_fk_fields.setdefault(fk_stem, body_prop)

    if method == "POST" and not _is_action_post(path, op.get("success_status")):
        add_capture(primary_var, "$.id")
        for rp in resp_props:
            if "." not in rp and "[]" not in rp:
                rp_snake = _to_snake_case(rp)
                if rp_snake != "id" and rp_snake.endswith("_id"):
                    fk_stem = _singularize(rp_snake[:-3])
                    if fk_stem and fk_stem != step_stem:
                        add_capture(
                            _var_name_for_stem(fk_stem, cluster_name),
                            f"$.{rp}",
                        )
        if not resp_props:
            for fk_stem, fk_prop in consumed_body_fk_fields.items():
                add_capture(
                    _var_name_for_stem(fk_stem, cluster_name),
                    f"$.{fk_prop}",
                )
        return captures

    if (
        method == "GET"
        and not p_clean.endswith("}")
        and not p_clean.endswith("/count")
        and not p_clean.endswith("/latest")
    ):
        # 1. Explicit array item id in response_properties (e.g. items[].id, @graph[].id)
        matched_primary = False
        for rp in resp_props:
            if "[]." in rp:
                col, field = rp.split("[].", 1)
                if field in ("id", "@id"):
                    jp = f"$.{col}[0].id" if col else "$[0].id"
                    add_capture(primary_var, jp)
                    matched_primary = True
                    break

        # 2. Explicit array item *Id or *Code in response_properties (e.g. @graph[].productCode)
        if not matched_primary:
            for rp in resp_props:
                if "[]." in rp:
                    col, field = rp.split("[].", 1)
                    f_snake = _to_snake_case(field)
                    if f_snake.endswith("_id") or f_snake.endswith("_code"):
                        jp = f"$.{col}[0].{field}" if col else f"$[0].{field}"
                        add_capture(primary_var, jp)
                        matched_primary = True
                        break

        # 3. Collection endpoint whose stem is consumed downstream and has a list/map container or unspecified schema
        plural_stem = f"{step_stem}s"
        list_col = "items"
        if "@graph" in resp_props:
            list_col = "@graph"
        elif "features" in resp_props:
            list_col = "features"
        elif plural_stem in resp_props:
            list_col = plural_stem
        if not matched_primary and step_stem in consumed_stems:
            has_list_container = not resp_props or any(
                k in resp_props
                for k in (*_LIST_COLLECTION_KEYS, plural_stem, "id", "@id")
            )
            if has_list_container:
                add_capture(primary_var, f"$.{list_col}[0].id")

        # 4. Foreign key IDs returned in collection response (e.g. items[].product_id, items[].customer_id, product_id)
        for rp in resp_props:
            if "[]." in rp:
                col, field = rp.split("[].", 1)
                f_snake = _to_snake_case(field)
                if f_snake != "id" and f_snake.endswith("_id"):
                    fk_stem = _singularize(f_snake[:-3])
                    if fk_stem and fk_stem != step_stem:
                        jp = f"$.{col}[0].{field}" if col else f"$[0].{field}"
                        add_capture(
                            _var_name_for_stem(fk_stem, cluster_name), jp
                        )
            elif "." not in rp:
                f_snake = _to_snake_case(rp)
                if f_snake != "id" and f_snake.endswith("_id"):
                    fk_stem = _singularize(f_snake[:-3])
                    if fk_stem and fk_stem != step_stem:
                        add_capture(
                            _var_name_for_stem(fk_stem, cluster_name),
                            f"$.{rp}",
                        )
        if not resp_props or not any("[]." in rp for rp in resp_props):
            for fk_stem, fk_prop in consumed_body_fk_fields.items():
                add_capture(
                    _var_name_for_stem(fk_stem, cluster_name),
                    f"$.{list_col}[0].{fk_prop}",
                )

        # 5. Composite date, time, and sequence parameters consumed downstream
        downstream_placeholders: set[str] = set()
        for other_op in (all_cluster_ops or []):
            if other_op == op:
                continue
            downstream_placeholders.update(
                re.findall(r"\{([^}]+)\}", other_op.get("path", ""))
            )

        for comp_field in ("sequence", "date", "time"):
            if comp_field in downstream_placeholders:
                for rp in resp_props:
                    rp_lower = rp.lower()
                    if (
                        rp_lower == comp_field
                        or rp_lower.endswith(f".{comp_field}")
                        or rp_lower.endswith(f"_{comp_field}")
                    ):
                        col = list_col
                        if "[]." in rp:
                            col_part, _ = rp.split("[].", 1)
                            if col_part:
                                col = col_part
                        jp = f"$.{col}[0].{rp.split('.')[-1]}" if col else f"$[0].{rp.split('.')[-1]}"
                        add_capture(f"{comp_field}", jp)
                        break

    return captures


def _step_crud_rank(
    method: str, path: str, success_status: int | None = None
) -> tuple[int, int]:
    """Orders CRUD steps so collection GETs and creation POSTs precede item reads, updates, actions, cancellation, and deletion."""
    m = method.upper()
    p = path.lower().rstrip("/")
    num_params = len(re.findall(r"\{[^}]+\}", p))
    has_param = num_params > 0
    ends_with_param = p.endswith("}")
    if m == "DELETE":
        return (8, num_params)
    if m == "POST" and p.endswith("/cancel"):
        return (7, num_params)
    if not has_param and m == "GET":
        return (0, 0)
    if not has_param and m == "POST":
        return (1, 0)
    if (
        has_param
        and not ends_with_param
        and m == "GET"
        and not p.endswith("/latest")
        and not p.endswith("/count")
    ):
        return (2, num_params)
    if (
        has_param
        and not ends_with_param
        and m == "POST"
        and not _is_action_post(p, success_status)
    ):
        return (3, num_params)
    if has_param and m == "GET":
        return (4, num_params)
    if m in {"PUT", "PATCH"}:
        return (5, num_params)
    return (6, num_params)


def build_deterministic_fallback_scenarios(
    cluster_name: str,
    ops: list[dict[str, Any]],
    test_config: dict[str, Any] | None = None,
) -> list[TestScenario]:
    """Creates deterministic fallback test scenarios covering all operations in the cluster."""
    if not test_config:
        test_config = {}

    global_paths = test_config.get("global", {}).get(
        "path_parameters", test_config.get("path_parameters", {})
    )
    global_query = test_config.get("global", {}).get(
        "query_parameters", test_config.get("query_parameters", {})
    )

    happy_steps: list[TestStep] = []
    neg_step: TestStep | None = None
    ordered_ops = sorted(
        ops,
        key=lambda op: _step_crud_rank(
            str(op.get("method", "GET")),
            str(op.get("path", "/")),
            op.get("success_status"),
        ),
    )
    captured_by_stem: dict[str, str] = {}

    for idx, op in enumerate(ordered_ops):
        op_id = op["operation_id"]
        step_id = f"fallback_{cluster_name}_step_{idx + 1}_{op_id}"
        method = op["method"].upper()
        path = op["path"]

        endpoint_paths = (
            test_config.get("endpoints", {}).get(path, {}).get("path_parameters", {})
        )
        endpoint_query = (
            test_config.get("endpoints", {}).get(path, {}).get("query_parameters", {})
        )

        path_params: dict[str, Any] = {}
        fallback_path_params: dict[str, Any] = {}
        placeholders = re.findall(r"\{([^}]+)\}", path)
        param_list = op.get("parameters", [])

        param_map: dict[str, dict[str, Any]] = {}
        for p in param_list:
            if isinstance(p, dict):
                param_map[p.get("name", "")] = p
            elif hasattr(p, "name"):
                param_map[p.name] = {
                    "name": p.name,
                    "in": getattr(p, "in_location", "query"),
                    "type": getattr(p, "param_type", "string"),
                    "format": getattr(p, "format", None),
                    "pattern": getattr(p, "pattern", None),
                    "example": getattr(p, "example", None),
                    "default": getattr(p, "default", None),
                    "enum": getattr(p, "enum", None),
                }

        for ph in placeholders:
            if ph in endpoint_paths:
                seed_val = endpoint_paths[ph]
            elif ph in global_paths:
                seed_val = global_paths[ph]
            elif ph in param_map:
                p_info = param_map[ph]
                if p_info.get("example") is not None:
                    seed_val = p_info["example"]
                elif p_info.get("default") is not None:
                    seed_val = p_info["default"]
                elif p_info.get("enum"):
                    seed_val = p_info["enum"][0]
                elif p_info.get("pattern"):
                    pat_val = sample_from_regex(p_info["pattern"], ph)
                    seed_val = pat_val if pat_val else "1"
                else:
                    # Check if sibling operations in ops define enum or pattern for this parameter name
                    sibling_enum = None
                    sibling_pat = None
                    for other_op in ops:
                        for other_p in other_op.get("parameters", []):
                            op_pname = (
                                other_p.get("name")
                                if isinstance(other_p, dict)
                                else getattr(other_p, "name", None)
                            )
                            if op_pname == ph:
                                if not sibling_enum:
                                    sibling_enum = (
                                        other_p.get("enum")
                                        if isinstance(other_p, dict)
                                        else getattr(other_p, "enum", None)
                                    )
                                if not sibling_pat:
                                    sibling_pat = (
                                        other_p.get("pattern")
                                        if isinstance(other_p, dict)
                                        else getattr(other_p, "pattern", None)
                                    )
                    if sibling_enum:
                        cand = sibling_enum[0]
                        for opt in sibling_enum:
                            if str(opt).lower() in path.lower():
                                cand = opt
                                break
                        seed_val = cand
                    elif sibling_pat:
                        pat_val = sample_from_regex(sibling_pat, ph)
                        seed_val = pat_val if pat_val else "1"
                    else:
                        fmt = (p_info.get("format") or "").lower()
                        p_type = (p_info.get("type") or "string").lower()
                        if fmt == "uuid":
                            seed_val = "123e4567-e89b-12d3-a456-426614174000"
                        elif fmt in ["date", "date-time"]:
                            seed_val = "2026-01-01"
                        elif p_type in ["int", "integer"]:
                            seed_val = 1
                        elif p_type in ["number", "float"]:
                            seed_val = 1.0
                        elif "id" in ph.lower():
                            seed_val = "1"
                        else:
                            seed_val = f"sample_{ph}"
            elif "id" in ph.lower():
                seed_val = "1"
            elif any(coord in ph.lower() for coord in ["lat", "lon", "x", "y"]):
                seed_val = "0"
            else:
                seed_val = f"sample_{ph}"

            fallback_path_params[ph] = seed_val
            param_stem = _infer_param_resource_stem(path, ph, cluster_name)
            if param_stem and param_stem in captured_by_stem:
                path_params[ph] = f"{{{{{captured_by_stem[param_stem]}}}}}"
            else:
                path_params[ph] = seed_val

        query_params: dict[str, Any] = {}
        for p_name, p_info in param_map.items():
            if p_info.get("in") == "query":
                if p_name in endpoint_query:
                    query_params[p_name] = endpoint_query[p_name]
                elif p_name in global_query:
                    query_params[p_name] = global_query[p_name]
                elif p_info.get("required") or p_info.get("example") is not None:
                    query_params[p_name] = (
                        p_info.get("example")
                        if p_info.get("example") is not None
                        else (p_info.get("default") or 1)
                    )
                elif p_name.lower() in (
                    "limit",
                    "pagesize",
                    "page_size",
                    "per_page",
                    "count",
                ):
                    query_params[p_name] = min(p_info.get("default") or 10, 10)
                elif p_name.lower() == "type" and p_info.get("enum"):
                    cand = p_info["enum"][0]
                    for opt in p_info["enum"]:
                        if any(
                            f"/{str(opt).lower()}/"
                            in str(other_op.get("path", "")).lower()
                            for other_op in ops
                        ):
                            cand = opt
                            break
                    query_params[p_name] = cand

        request_body = None
        headers: dict[str, str] = {}
        raw_ep = op.get("_raw_ep")
        if method in ["POST", "PUT", "PATCH"]:
            if raw_ep and raw_ep.request_body:
                request_body = generate_mock_json_payload(
                    raw_ep.request_body.raw_schema, raw_ep.request_body.properties
                )
            elif op.get("request_body_properties"):
                request_body = {
                    prop: f"sample_{prop}" for prop in op["request_body_properties"]
                }
            elif isinstance(op.get("request_body"), dict):
                rb_schema = op["request_body"]
                request_body = generate_mock_json_payload(
                    rb_schema, rb_schema.get("properties")
                )
            if request_body:
                request_body = _inject_context_into_payload(
                    request_body, captured_by_stem
                )
                headers["Content-Type"] = "application/json"

        expected_status = op.get("success_status")
        if not expected_status:
            expected_status = (
                201 if method == "POST" else (204 if method == "DELETE" else 200)
            )

        captures = _infer_op_captures(op, cluster_name, ordered_ops)
        step_stem = _infer_step_resource_stem(path, cluster_name)
        for cap in captures:
            cap_stem = _stem_from_variable_name(cap.variable_name, step_stem)
            if cap_stem:
                captured_by_stem[cap_stem] = cap.variable_name

        happy_steps.append(
            TestStep(
                step_id=step_id,
                operation_id=op_id,
                method=HTTPMethod(method),
                path_template=path,
                path_params=path_params,
                query_params=query_params,
                headers=headers,
                request_body=request_body,
                captures=captures,
                expected_status=expected_status,
            )
        )

        if (
            neg_step is None
            and fallback_path_params
            and path.rstrip("/").endswith("}")
        ):
            bad_params = dict(fallback_path_params)
            last_key = list(bad_params.keys())[-1]
            bad_params[last_key] = "invalid_nonexistent_id_999999"
            neg_step = TestStep(
                step_id=f"neg_{cluster_name}_step_{op_id}",
                operation_id=op_id,
                method=HTTPMethod(method),
                path_template=path,
                path_params=bad_params,
                query_params=query_params,
                headers=headers,
                expected_status=404,
            )

    clean_name = cluster_name.replace("_", " ").title()
    scenarios: list[TestScenario] = []

    def _referenced_vars(steps_list: list[TestStep]) -> set[str]:
        refs: set[str] = set()
        for st in steps_list:
            for val in list((st.path_params or {}).values()) + list(
                (st.query_params or {}).values()
            ):
                if isinstance(val, str):
                    refs.update(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", val))
            if isinstance(st.request_body, dict):
                body_str = json.dumps(st.request_body)
                refs.update(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", body_str))
        return refs

    # Chunk happy steps into modular scenarios (max 6 steps each) if cluster is large
    step_chunks = (
        [happy_steps]
        if len(happy_steps) <= 8
        else [happy_steps[i : i + 6] for i in range(0, len(happy_steps), 6)]
    )
    for c_idx, chunk in enumerate(step_chunks):
        suffix = f"_{c_idx + 1}" if len(step_chunks) > 1 else ""
        flow_title = f" (Part {c_idx + 1})" if len(step_chunks) > 1 else ""
        chunk_setup_steps: list[TestStep] = []
        if c_idx > 0:
            prior_steps = happy_steps[: c_idx * 6]
            captured_in_chunk = {
                cap.variable_name for st in chunk for cap in (st.captures or [])
            }
            needed_vars = _referenced_vars(chunk) - captured_in_chunk
            selected_indices: set[int] = set()
            changed = True
            while changed and needed_vars:
                changed = False
                for p_idx, p_st in enumerate(prior_steps):
                    if p_idx in selected_indices:
                        continue
                    produced = {cap.variable_name for cap in (p_st.captures or [])}
                    if produced & needed_vars:
                        selected_indices.add(p_idx)
                        needed_vars -= produced
                        needed_vars |= _referenced_vars([p_st]) - captured_in_chunk
                        changed = True
            for p_idx in sorted(selected_indices):
                p_st = prior_steps[p_idx]
                chunk_setup_steps.append(
                    p_st.model_copy(
                        update={
                            "step_id": f"setup_{c_idx + 1}_{p_st.operation_id}",
                        },
                        deep=True,
                    )
                )

        scenarios.append(
            TestScenario(
                scenario_id=f"fallback_{cluster_name}{suffix}",
                name=f"Baseline Flow: {clean_name}{flow_title}",
                description=(
                    f"Deterministic baseline workflow covering {len(chunk)} "
                    f"endpoints in {cluster_name}."
                ),
                scenario_type=ScenarioType.HAPPY_PATH_E2E,
                setup_steps=chunk_setup_steps,
                test_steps=chunk,
            )
        )
    if neg_step:
        neg_scenario = TestScenario(
            scenario_id=f"neg_{cluster_name}",
            name=f"Boundary Negative: {clean_name} Invalid Parameters",
            description=(
                f"Exercises boundary rejection with non-existent identifiers "
                f"in {cluster_name}."
            ),
            scenario_type=ScenarioType.BOUNDARY_NEGATIVE,
            test_steps=[neg_step],
        )
        scenarios.append(neg_scenario)

    return scenarios


class TestGeneratorAgent:
    """Synthesizes domain-clustered OpenAPI integration test journeys using Gemini and DAG ordering."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Provenance of the most recent generation run: which domain clusters Gemini designed versus the
        # deterministic baseline, and what the guardrails changed. Persisted as generation_report.json.
        self.generation_report: dict[str, Any] = new_generation_report()

    def _record_cluster_provenance(
        self,
        cluster_name: str,
        cluster_ops: list[dict[str, Any]],
        source: str,
        final_scenarios: list[TestScenario],
        *,
        raw_scenarios: list[TestScenario] | None = None,
        test_config: dict[str, Any] | None = None,
        reason: str | None = None,
        model: str | None = None,
        latency_ms: int | None = None,
        attempts: int | None = None,
    ) -> None:
        """Records how one domain cluster's journeys were produced. Best-effort: never raises."""
        try:
            final_dicts = [s.model_dump(mode="json") for s in final_scenarios]
            entry: dict[str, Any] = {
                "source": source,
                "operations": [str(op.get("operation_id") or "") for op in cluster_ops],
                "model": model,
                "latency_ms": latency_ms,
                "attempts": attempts,
                "reason": reason,
                "scenario_ids": [str(sc.get("scenario_id")) for sc in final_dicts],
                "raw_scenario_count": len(raw_scenarios) if raw_scenarios is not None else None,
                "adjustments": {},
                "stats": aggregate_stats(final_dicts),
            }
            if source == "gemini":
                if raw_scenarios is not None:
                    entry["adjustments"] = diff_generated_scenarios(
                        [s.model_dump(mode="json") for s in raw_scenarios], final_dicts
                    )
                # What the deterministic builder would have produced for the same endpoints, so the
                # Visualize tests page can show Gemini's design against a like-for-like baseline.
                baseline = build_deterministic_fallback_scenarios(cluster_name, cluster_ops, test_config)
                entry["baseline_stats"] = aggregate_stats([s.model_dump(mode="json") for s in baseline])
            else:
                entry["baseline_stats"] = entry["stats"]
            self.generation_report["clusters"][cluster_name] = entry
        except Exception as err:
            logger.debug(
                "[%s] Could not record provenance for cluster '%s': %s",
                self.session_id,
                cluster_name,
                err,
            )

    def _finalize_generation_report(self, scenarios: list[TestScenario]) -> None:
        """Indexes per-journey provenance and summary counts once the whole suite is assembled."""
        try:
            report = self.generation_report
            by_scenario: dict[str, dict[str, Any]] = {}
            for cluster_name, entry in report["clusters"].items():
                for sc_id in entry.get("scenario_ids") or []:
                    by_scenario[sc_id] = {
                        "source": entry["source"],
                        "cluster": cluster_name,
                        "model": entry.get("model"),
                        "reason": entry.get("reason"),
                        "adjustments": (entry.get("adjustments") or {}).get(sc_id, []),
                    }
            summary: dict[str, int] = {
                "journeys": len(scenarios),
                "gemini": 0,
                "fallback": 0,
                "template": 0,
                "adjustments": 0,
            }
            for sc in scenarios:
                sc_dict = sc.model_dump(mode="json")
                sc_id = str(sc_dict.get("scenario_id"))
                info = by_scenario.get(sc_id) or {
                    "source": infer_scenario_source(sc_dict),
                    "cluster": None,
                    "model": None,
                    "reason": None,
                    "adjustments": [],
                }
                info["stats"] = scenario_stats(sc_dict)
                report["scenarios"][sc_id] = info
                summary[info["source"]] = summary.get(info["source"], 0) + 1
                summary["adjustments"] += len(info["adjustments"])
            report["summary"] = summary
            report["cluster_count"] = len(report["clusters"])
            report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        except Exception as err:
            logger.debug("[%s] Could not finalize generation report: %s", self.session_id, err)

    def _convert_to_operation_nodes(
        self, oas_metadata: OASMetadata
    ) -> list[OperationNode]:
        ops: list[OperationNode] = []
        for ep in oas_metadata.endpoints:
            method_upper = ep.method.upper()
            domain = extract_domain_cluster(ep.path, getattr(ep, "tags", []))
            stem = _infer_step_resource_stem(ep.path, domain)
            param_schemas: list[ParameterSchema] = []
            seen_p_names: set[str] = set()
            for p in ep.parameters:
                loc_str = (p.in_location or "query").lower()
                loc = (
                    ParameterLocation(loc_str)
                    if loc_str in {"path", "query", "header", "cookie"}
                    else ParameterLocation.QUERY
                )
                param_schemas.append(
                    ParameterSchema(
                        name=p.name,
                        location=loc,
                        required=p.required,
                    )
                )
                seen_p_names.add(p.name)
            for ph in re.findall(r"\{([^}]+)\}", ep.path):
                if ph not in seen_p_names:
                    param_schemas.append(
                        ParameterSchema(
                            name=ph,
                            location=ParameterLocation.PATH,
                            required=True,
                        )
                    )
                    seen_p_names.add(ph)

            produces_keys: list[str] = []
            is_creation_post = method_upper == "POST" and not _is_action_post(
                ep.path, 201 if "201" in ep.responses else None
            )
            is_collection_get = (
                method_upper == "GET" and not ep.path.rstrip("/").endswith("}")
            )
            if is_creation_post or is_collection_get:
                produces_keys = [
                    "id",
                    f"{stem}_id",
                    f"{stem}Id",
                    f"{domain}_id",
                ]

            ops.append(
                OperationNode(
                    operation_id=ep.operation_id,
                    method=HTTPMethod(method_upper),
                    path=ep.path,
                    parameters=param_schemas,
                    produces_keys=produces_keys,
                )
            )
        return ops

    @staticmethod
    def _sanitize_generated_scenarios(
        scenarios: list[TestScenario],
        cluster_ops: list[dict[str, Any]],
    ) -> list[TestScenario]:
        """Enforces post-generation guardrails on status codes, context variable chaining, and destructive step ordering."""
        ops_by_id = {op["operation_id"]: op for op in cluster_ops}
        ops_by_method_path = {
            (str(op.get("method", "")).upper(), str(op.get("path", ""))): op
            for op in cluster_ops
        }
        for scenario in scenarios:
            if scenario.scenario_type == ScenarioType.HAPPY_PATH_E2E:
                for step in scenario.test_steps:
                    if step.expected_status >= 400:
                        op_info = ops_by_id.get(step.operation_id, {})
                        step.expected_status = op_info.get("success_status", 200)
                scenario.test_steps.sort(
                    key=lambda st: (
                        2
                        if st.method.value == "DELETE"
                        else (
                            1
                            if st.path_template.rstrip("/").endswith("/cancel")
                            else 0
                        )
                    )
                )

                # Deterministic context-chaining pass across setup_steps + test_steps
                all_steps = list(scenario.setup_steps) + list(scenario.test_steps)
                captured_by_stem: dict[str, str] = {}

                # Backfill missing or incomplete request_body payloads on write steps first
                # so downstream foreign-key consumption is visible to upstream steps.
                for st in all_steps:
                    if st.method.value in {"POST", "PUT", "PATCH"}:
                        op_info = ops_by_id.get(st.operation_id) or ops_by_method_path.get(
                            (st.method.value.upper(), st.path_template), {}
                        )
                        default_payload: dict[str, Any] | None = None
                        raw_ep = op_info.get("_raw_ep") if op_info else None
                        if raw_ep and getattr(raw_ep, "request_body", None):
                            default_payload = generate_mock_json_payload(
                                raw_ep.request_body.raw_schema,
                                raw_ep.request_body.properties,
                            )
                        elif op_info and isinstance(op_info.get("request_body"), dict):
                            rb_schema = op_info["request_body"]
                            default_payload = generate_mock_json_payload(
                                rb_schema, rb_schema.get("properties")
                            )
                        elif op_info and op_info.get("request_body_properties"):
                            default_payload = {
                                prop: f"sample_{prop}"
                                for prop in op_info["request_body_properties"]
                            }

                        if default_payload:
                            if not isinstance(st.request_body, dict) or not st.request_body:
                                st.request_body = dict(default_payload)
                            elif st.method.value in {"POST", "PUT"}:
                                req_fields = (
                                    op_info.get("required_body_fields")
                                    or (
                                        (op_info.get("request_body") or {}).get("required")
                                        if isinstance(op_info.get("request_body"), dict)
                                        else []
                                    )
                                    or []
                                )
                                for rf in req_fields:
                                    if rf not in st.request_body and rf in default_payload:
                                        st.request_body[rf] = default_payload[rf]

                # Identify all resource stems needed by downstream steps in this scenario
                stems_consumed_from: list[set[str]] = []
                for st in all_steps:
                    step_consumed: set[str] = set()
                    for ph in re.findall(r"\{([^}]+)\}", st.path_template):
                        p_stem = _infer_param_resource_stem(st.path_template, ph)
                        if p_stem:
                            step_consumed.add(p_stem)
                    if isinstance(st.request_body, dict):
                        for bk in st.request_body:
                            bk_snake = _to_snake_case(bk)
                            if bk_snake != "id" and bk_snake.endswith("_id"):
                                step_consumed.add(_singularize(bk_snake[:-3]))
                    stems_consumed_from.append(step_consumed)

                for idx, step in enumerate(all_steps):
                    op_info = ops_by_id.get(step.operation_id) or ops_by_method_path.get(
                        (step.method.value.upper(), step.path_template), {}
                    )
                    step_stem = _infer_step_resource_stem(step.path_template)

                    # 1. Propagate already-captured context variables into path_params and request_body
                    for ph in re.findall(r"\{([^}]+)\}", step.path_template):
                        p_stem = _infer_param_resource_stem(step.path_template, ph)
                        if p_stem and p_stem in captured_by_stem:
                            cur_val = step.path_params.get(ph)
                            if not (
                                isinstance(cur_val, str)
                                and ("{{" in cur_val or cur_val.startswith("$"))
                            ):
                                step.path_params[ph] = (
                                    f"{{{{{captured_by_stem[p_stem]}}}}}"
                                )
                        elif p_stem and p_stem not in captured_by_stem:
                            cur_val = step.path_params.get(ph)
                            if isinstance(cur_val, str) and ("{{" in cur_val or cur_val.startswith("$")):
                                var_name = cur_val.strip("{}$")
                                var_stem = _stem_from_variable_name(var_name)
                                if var_stem and var_stem != p_stem:
                                    op_params = (op_info.get("parameters") or []) if op_info else []
                                    ph_example = None
                                    for p in op_params:
                                        p_name = p.get("name") if isinstance(p, dict) else getattr(p, "name", None)
                                        if p_name == ph:
                                            ph_example = p.get("example") if isinstance(p, dict) else getattr(p, "example", None)
                                            break
                                    step.path_params[ph] = ph_example or "1"

                    if (
                        step.method.value in {"POST", "PUT", "PATCH"}
                        and isinstance(step.request_body, dict)
                        and step.request_body
                    ):
                        step.headers.setdefault("Content-Type", "application/json")
                        if captured_by_stem:
                            step.request_body = _inject_context_into_payload(
                                step.request_body, captured_by_stem
                            )

                    # 2. Register existing captures on this step
                    existing_stems_on_step: set[str] = set()
                    for cap in step.captures:
                        c_stem = _stem_from_variable_name(
                            cap.variable_name, step_stem
                        )
                        if "." in cap.json_path:
                            tail_prop = cap.json_path.split(".")[-1]
                            tp_snake = _to_snake_case(tail_prop)
                            if tp_snake != "id" and tp_snake.endswith("_id"):
                                c_stem = _singularize(tp_snake[:-3])
                        if c_stem:
                            captured_by_stem[c_stem] = cap.variable_name
                            existing_stems_on_step.add(c_stem)

                    # 3. Auto-inject missing captures if this step produces IDs needed by downstream steps
                    downstream_needed: set[str] = set()
                    for later_set in stems_consumed_from[idx + 1:]:
                        downstream_needed.update(later_set)

                    if downstream_needed:
                        synth_op = dict(op_info) if op_info else {}
                        synth_op.setdefault("operation_id", step.operation_id)
                        synth_op.setdefault("method", step.method.value)
                        synth_op.setdefault("path", step.path_template)
                        synth_op.setdefault(
                            "success_status", step.expected_status
                        )
                        inferred_caps = _infer_op_captures(
                            synth_op,
                            step_stem,
                            cluster_ops
                            or [
                                {
                                    "path": s.path_template,
                                    "method": s.method.value,
                                    "request_body_properties": list(
                                        (s.request_body or {}).keys()
                                    ),
                                }
                                for s in all_steps
                            ],
                        )
                        existing_var_names = {
                            c.variable_name for c in step.captures
                        }
                        for icap in inferred_caps:
                            istem = _stem_from_variable_name(
                                icap.variable_name, step_stem
                            )
                            if (
                                istem in downstream_needed
                                and istem not in existing_stems_on_step
                                and icap.variable_name not in existing_var_names
                            ):
                                step.captures.append(icap)
                                existing_var_names.add(icap.variable_name)
                                existing_stems_on_step.add(istem)
                                captured_by_stem[istem] = icap.variable_name

            elif scenario.scenario_type == ScenarioType.BOUNDARY_NEGATIVE:
                filtered_steps: list[TestStep] = []
                for step in scenario.test_steps:
                    step.captures = []
                    step.semantic_assertions = []
                    op_info = ops_by_id.get(step.operation_id, {})
                    err_codes = op_info.get("error_statuses", [])
                    summary_lower = (op_info.get("summary") or "").lower()
                    is_collection_list = (
                        not step.path_template.rstrip("/").endswith("}")
                        or "list of " in summary_lower
                        or "returns a list" in summary_lower
                    )
                    if (
                        step.expected_status == 404
                        and is_collection_list
                        and 404 not in err_codes
                    ):
                        continue
                    filtered_steps.append(step)
                if filtered_steps:
                    scenario.test_steps = filtered_steps
        return [s for s in scenarios if s.test_steps]

    async def _generate_cluster_scenarios_async(
        self,
        client: Any,
        model_id: str,
        cluster_name: str,
        cluster_ops: list[dict[str, Any]],
        sorted_node_ids: list[str],
        test_config: dict[str, Any] | None,
        semaphore: asyncio.Semaphore,
        use_vertex: bool,
    ) -> list[TestScenario]:
        """Generates modular test scenarios for a single domain cluster with retries and graceful fallback."""
        schema_json = json.dumps(DomainScenarioBatch.model_json_schema(), indent=2)
        system_prompt = (
            "You are a Principal API Test Automation Architect.\n"
            "Generate modular, deterministic integration test scenarios for the target API domain.\n"
            "You must emit strictly valid JSON conforming to the following JSON schema:\n"
            f"{schema_json}\n\n"
            "CRITICAL GUARDRAILS:\n"
            "1. Follow the provided topological operation order (List GET / Create POST -> Get by ID -> PUT/PATCH -> Action POST -> DELETE).\n"
            "2. NEVER generate dynamic or speculative value assertions in `semantic_assertions`:\n"
            "   - Do NOT assert `EQUALS` or `CONTAINS` on dynamic, server-generated, or time-varying fields "
            "(such as timestamps, UUIDs, live weather/forecast values, pagination counts, or array lengths).\n"
            "   - Only use `NOT_NULL` on guaranteed top-level fields explicitly listed in `response_properties`, "
            "or `EQUALS` only when verifying a static field that was sent in the request body of the same CRUD flow.\n"
            "   - Leave `semantic_assertions` empty (`[]`) when `response_properties` is empty, on HTTP 204 responses, "
            "or on `BOUNDARY_NEGATIVE` steps.\n"
            "3. Keep `path_template` identical to the canonical OpenAPI path (e.g. `/v1/products/{id}` or `/products/{product_id}`). "
            "When referencing a captured variable from a prior step, put `\"{{captured_var}}\"` as the value inside "
            "`path_params` (e.g. `\"path_params\": {\"id\": \"{{created_id}}\"}`) and inside `request_body` foreign-key fields "
            "(e.g. `\"request_body\": {\"product_id\": \"{{product_id}}\", \"customer_id\": \"{{customer_id}}\"}`), NOT by altering the parameter name in `path_template`.\n"
            "   - When a collection `GET` step (e.g. `GET /products`, `GET /v1/products`, `GET /v1/orders`) or creation `POST` step runs before "
            "steps that require resource IDs or foreign-key IDs, ALWAYS capture the resource ID (e.g. `$.items[0].id`, `$.@graph[0].id`, or `$.id`) "
            "and any foreign-key IDs (`$.items[0].product_id`, `$.items[0].customer_id`, `$.product_id`) in `captures`.\n"
            "   - Whenever a step captures a variable, EVERY subsequent step in that scenario that takes that identifier in `path_params` OR `request_body` "
            "MUST use `\"{{variable_name}}\"` instead of a hardcoded or invented literal.\n"
            "4. Only use query parameter and path parameter names that are explicitly defined in the operation's `parameters` list.\n"
            "5. In `HAPPY_PATH_E2E` scenarios, every step MUST have a `2XX` `expected_status` (e.g. 200, 201, 204) — NEVER 400 or 404.\n"
            "6. In `BOUNDARY_NEGATIVE` scenarios, only expect `404` on single-resource lookup endpoints (where the path ends in a resource ID "
            "and returns a single item, not a list/collection). Never expect `404` on collection filter endpoints (e.g. `/products/types/{typeId}`, "
            "`/stations/{stationId}/observations`, `/radar/stations/{stationId}/alarms`) that return `200 OK` with an empty list `[]`."
        )

        config_hint = ""
        if test_config:
            config_hint = (
                f"\nKnown-Good Seed Parameters & Overrides (from test_config.json):\n"
                f"{json.dumps(test_config, indent=2)}\n"
                "Use these exact valid parameter values for path_params and query_params where applicable.\n"
            )

        clean_title = cluster_name.replace("_", " ").title()
        ops_prompt_data = [
            {k: v for k, v in op.items() if k != "_raw_ep"} for op in cluster_ops
        ]
        user_prompt = f"""Target Domain Cluster: {clean_title}
Operations in this domain ({len(cluster_ops)} endpoints):
{json.dumps(ops_prompt_data, indent=2)}
{config_hint}
Topological Operations Order:
{sorted_node_ids}

### Requirements for this Domain:
1. **MODULAR FLOWS (3 to 8 steps)**:
   - Generate 1 or 2 cohesive `HAPPY_PATH_E2E` scenarios covering these operations in logical sequence (e.g. List -> Create -> Get by ID -> Update -> Delete) with `2XX` `expected_status` on every step.
   - Generate 1 `BOUNDARY_NEGATIVE` scenario exercising single-resource lookup 404s or invalid format/enum 400s (with `captures: []` and `semantic_assertions: []`). Do NOT use collection/list filter endpoints for 404 negative tests.
   - Every single `operation_id` in the list above must appear in at least one `HAPPY_PATH_E2E` scenario.
2. **STATE CHAINING & PARAMETERS (PATH & REQUEST PAYLOAD)**:
   - For steps that consume IDs produced by prior `GET` list or `POST` create steps, set `captures` with JSONPath (e.g. `$.items[0].id`, `$.@graph[0].id`, or `$.id` -> `product_id`, plus foreign keys like `$.items[0].customer_id` -> `customer_id`) and reference `{{{{product_id}}}}` inside BOTH `path_params` (e.g. `GET /products/{{product_id}}`) and `request_body` foreign-key fields (e.g. `POST /v1/orders` with `"product_id": "{{{{product_id}}}}"`).
   - When downstream steps consume date, time, or sequence path parameters (such as /cwas/{{date}}/{{sequence}} or /tafs/{{date}}/{{time}}), capture them from the preceding list response (e.g. `$.features[0].properties.sequence`, `$.features[0].properties.date`, `$.@graph[0].date`) and wire them as `{{{{date}}}}` or `{{{{sequence}}}}` into `path_params`.
   - When calling collection/list GET endpoints with pagination or limit query parameters (e.g. `limit`), include a reasonable limit (e.g. `limit=10`) to prevent slow network responses or payload timeouts.
   - Populate any remaining uncaptured `path_params` and `query_params` with valid enum/example values from the operation specification or seeds from `test_config`.
3. **ASSERTION HYGIENE (NO DYNAMIC ASSERTIONS)**:
   - Avoid dynamic assertion setting: never assert `EQUALS` on live/computed server data, timestamps, or unseeded fields.
   - Prefer `expected_status` verification and at most 1 `NOT_NULL` semantic assertion on a guaranteed top-level key from `response_properties`.
"""

        async with semaphore:
            started_at = time.monotonic()
            degrade_reason = "no_response"
            for attempt in range(3):
                try:
                    # Do NOT pass `response_schema=DomainScenarioBatch` on Vertex AI because
                    # Vertex AI Controlled Generation (`responseSchema`) treats `dict[str, Any]`
                    # fields (`request_body`, `path_params`, `query_params`) as OBJECT schemas
                    # with no `properties` and constrains the model to emit `null` or `{}`.
                    config_kwargs: dict[str, Any] = {
                        "response_mime_type": "application/json",
                        "temperature": float(
                            os.environ.get("GEMINI_TEMPERATURE", "0.0")
                        ),
                        "automatic_function_calling": types.AutomaticFunctionCallingConfig(
                            disable=True
                        ),
                    }

                    config = types.GenerateContentConfig(**config_kwargs)
                    logger.info(
                        "[%s] Calling Gemini (%s) for cluster '%s' (%d endpoints)...",
                        self.session_id,
                        model_id,
                        cluster_name,
                        len(cluster_ops),
                    )
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(
                            model=model_id,
                            contents=[system_prompt, user_prompt],
                            config=config,
                        ),
                        timeout=90.0,
                    )
                    if response.text:
                        raw_text = response.text.strip()
                        if raw_text.startswith("```"):
                            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                            raw_text = re.sub(r"\s*```$", "", raw_text)

                        try:
                            parsed_data = json.loads(raw_text)
                        except json.JSONDecodeError:
                            json_match = re.search(
                                r"(\{.*\}|\[.*\])", raw_text, re.DOTALL
                            )
                            if json_match:
                                parsed_data = json.loads(json_match.group(1))
                            else:
                                raise

                        scenarios: list[TestScenario] = []
                        if isinstance(parsed_data, list):
                            scenarios = [
                                TestScenario.model_validate(s) for s in parsed_data
                            ]
                        elif isinstance(parsed_data, dict):
                            if "scenarios" in parsed_data:
                                scenarios = [
                                    TestScenario.model_validate(s)
                                    for s in parsed_data["scenarios"]
                                ]
                            else:
                                scenarios = [TestScenario.model_validate(parsed_data)]

                        # Keep Gemini's raw proposal so the provenance report can show exactly
                        # what the deterministic guardrails changed.
                        raw_scenarios = copy.deepcopy(scenarios)
                        scenarios = self._sanitize_generated_scenarios(
                            scenarios, cluster_ops
                        )
                        if scenarios:
                            logger.info(
                                "[%s] Cluster '%s' generated %d scenarios.",
                                self.session_id,
                                cluster_name,
                                len(scenarios),
                            )
                            self._record_cluster_provenance(
                                cluster_name,
                                cluster_ops,
                                "gemini",
                                scenarios,
                                raw_scenarios=raw_scenarios,
                                test_config=test_config,
                                model=model_id,
                                latency_ms=int((time.monotonic() - started_at) * 1000),
                                attempts=attempt + 1,
                            )
                            return scenarios
                        degrade_reason = "empty_after_guardrails"
                    else:
                        degrade_reason = "empty_response"
                except Exception as err:
                    err_str = str(err)
                    is_rate_limit = any(
                        term in err_str.lower()
                        for term in [
                            "429",
                            "quota",
                            "resourceexhausted",
                            "ratelimit",
                        ]
                    )
                    if isinstance(err, asyncio.TimeoutError):
                        degrade_reason = "timeout"
                    elif is_rate_limit:
                        degrade_reason = "rate_limited"
                    else:
                        degrade_reason = f"error: {err_str[:160] or type(err).__name__}"
                    if is_rate_limit and attempt < 2:
                        backoff = (attempt + 1) * 3.0
                        logger.warning(
                            "[%s] Quota / rate limit hit for cluster '%s'. Backing off for %.1f seconds...",
                            self.session_id,
                            cluster_name,
                            backoff,
                        )
                        await asyncio.sleep(backoff)
                    else:
                        logger.warning(
                            "[%s] LLM call failed for cluster '%s': %s",
                            self.session_id,
                            cluster_name,
                            err,
                        )
                        break

            logger.info(
                "[%s] Cluster '%s' gracefully degraded to deterministic baseline flow.",
                self.session_id,
                cluster_name,
            )
            fallback = build_deterministic_fallback_scenarios(
                cluster_name, cluster_ops, test_config
            )
            self._record_cluster_provenance(
                cluster_name,
                cluster_ops,
                "fallback",
                fallback,
                reason=degrade_reason,
                model=model_id,
                latency_ms=int((time.monotonic() - started_at) * 1000),
            )
            return fallback

    async def _execute_async(
        self,
        oas_metadata: OASMetadata,
        test_config: dict[str, Any] | None = None,
    ) -> TestSuite:
        logger.info(
            "[%s] TestGeneratorAgent executing with Domain-Chunked Multi-Call Architecture",
            self.session_id,
        )
        self.generation_report = new_generation_report()
        self.generation_report["endpoint_count"] = len(oas_metadata.endpoints)

        # 1. Build DAG and topological sort order
        ops = self._convert_to_operation_nodes(oas_metadata)
        graph = DependencyGraph()
        for op in ops:
            graph.add_operation(op)
        graph.build_heuristic_edges()
        sorted_node_ids = graph.topological_sort()

        # 2. Cluster operations generically
        raw_clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for ep in oas_metadata.endpoints:
            domain = extract_domain_cluster(ep.path, getattr(ep, "tags", []))

            resp_props: list[str] = []
            for status_key in ["200", "201", 200, 201]:
                if status_key in ep.responses:
                    resp_obj = ep.responses[status_key]
                    if isinstance(resp_obj, dict):
                        content = resp_obj.get("content", {})
                        for ct_val in content.values():
                            schema = ct_val.get("schema", {})
                            props = _extract_schema_props(schema)
                            for pk, pv in props.items():
                                resp_props.append(pk)
                                if (
                                    isinstance(pv, dict)
                                    and "items" in pv
                                    and isinstance(pv["items"], dict)
                                ):
                                    item_props = _extract_schema_props(pv["items"])
                                    for ipk in item_props:
                                        resp_props.append(f"{pk}[].{ipk}")
                            if (
                                not props
                                and isinstance(schema, dict)
                                and "items" in schema
                                and isinstance(schema["items"], dict)
                            ):
                                item_props = _extract_schema_props(schema["items"])
                                for ipk in item_props:
                                    resp_props.append(f"[].{ipk}")
                    break

            error_statuses = sorted(
                {
                    int(str(code))
                    for code in ep.responses
                    if str(code).isdigit() and int(str(code)) >= 400
                }
            )
            raw_clusters[domain].append(
                {
                    "operation_id": ep.operation_id,
                    "method": ep.method,
                    "path": ep.path,
                    "summary": ep.summary or "",
                    "parameters": [
                        {
                            "name": p.name,
                            "in": p.in_location,
                            "required": p.required,
                            "type": p.param_type,
                            "format": p.format,
                            "pattern": p.pattern,
                            "enum": p.enum,
                            "example": p.example or p.default,
                        }
                        for p in ep.parameters
                    ],
                    "response_properties": resp_props[:25],
                    "request_body_properties": (
                        list(ep.request_body.properties.keys())
                        if ep.request_body
                        else []
                    ),
                    "required_body_fields": (
                        ep.request_body.required_properties if ep.request_body else []
                    ),
                    "success_status": (
                        201
                        if "201" in ep.responses
                        else (204 if "204" in ep.responses else 200)
                    ),
                    "error_statuses": error_statuses,
                    "_raw_ep": ep,
                }
            )

        bundled_clusters = bundle_clusters(
            raw_clusters, max_clusters=12, min_independent_size=3
        )
        logger.info(
            "[%s] Partitioned %d endpoints across %d domain clusters: %s",
            self.session_id,
            len(oas_metadata.endpoints),
            len(bundled_clusters),
            list(bundled_clusters.keys()),
        )

        load_dotenv("../.env")
        load_dotenv(".env")
        api_key = os.environ.get("GOOGLE_API_KEY")
        use_vertex = (
            os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        )

        if api_key or use_vertex:
            if use_vertex:
                client = genai.Client(
                    vertexai=True,
                    project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
                )
            else:
                client = genai.Client(api_key=api_key)

            model_id = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
            semaphore = asyncio.Semaphore(3)
            self.generation_report.update(
                {
                    "model": model_id,
                    "provider": "vertex_ai" if use_vertex else "gemini_api",
                    "credentials_available": True,
                }
            )

            tasks = [
                self._generate_cluster_scenarios_async(
                    client=client,
                    model_id=model_id,
                    cluster_name=c_name,
                    cluster_ops=c_ops,
                    sorted_node_ids=sorted_node_ids,
                    test_config=test_config,
                    semaphore=semaphore,
                    use_vertex=use_vertex,
                )
                for c_name, c_ops in bundled_clusters.items()
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_scenarios: list[TestScenario] = []
            for i, res in enumerate(results):
                c_name = list(bundled_clusters.keys())[i]
                c_ops = list(bundled_clusters.values())[i]
                if isinstance(res, list) and res:
                    all_scenarios.extend(res)
                else:
                    logger.warning(
                        "[%s] Cluster '%s' returned invalid result (%s); using baseline fallback.",
                        self.session_id,
                        c_name,
                        res,
                    )
                    cluster_fallback = build_deterministic_fallback_scenarios(
                        c_name, c_ops, test_config
                    )
                    self._record_cluster_provenance(
                        c_name,
                        c_ops,
                        "fallback",
                        cluster_fallback,
                        reason=f"error: {str(res)[:160] or 'invalid result'}",
                        model=model_id,
                    )
                    all_scenarios.extend(cluster_fallback)

            if oas_metadata.endpoints:
                first_ep = oas_metadata.endpoints[0]
                all_scenarios.append(
                    TestScenario(
                        scenario_id="global_security_check",
                        name="Security: Verify Endpoint Authentication and Headers",
                        description=(
                            "Tests that requests with missing credentials or "
                            "malformed security headers are rejected appropriately."
                        ),
                        scenario_type=ScenarioType.SECURITY_PERMUTATION,
                        test_steps=[
                            TestStep(
                                step_id="sec_step_unauthorized_probe",
                                operation_id=first_ep.operation_id,
                                method=HTTPMethod(first_ep.method.upper()),
                                path_template=first_ep.path,
                                expected_status=401 if bool(first_ep.security) else 200,
                            )
                        ],
                    )
                )
                all_scenarios.append(
                    TestScenario(
                        scenario_id="global_idempotency_check",
                        name="Idempotency: Verify Repeated Safe Requests",
                        description=(
                            "Executes identical requests to verify that safe "
                            "operations produce identical, idempotent responses."
                        ),
                        scenario_type=ScenarioType.IDEMPOTENCY,
                        test_steps=[
                            TestStep(
                                step_id="idem_step_1",
                                operation_id=first_ep.operation_id,
                                method=HTTPMethod(first_ep.method.upper()),
                                path_template=first_ep.path,
                                expected_status=200,
                            ),
                            TestStep(
                                step_id="idem_step_2",
                                operation_id=first_ep.operation_id,
                                method=HTTPMethod(first_ep.method.upper()),
                                path_template=first_ep.path,
                                expected_status=200,
                            ),
                        ],
                    )
                )

            self._finalize_generation_report(all_scenarios)
            return TestSuite(
                suite_id=f"suite_{self.session_id}",
                target_base_url=oas_metadata.base_url or "http://localhost:8080",
                scenarios=all_scenarios,
            )

        fallback_scenarios: list[TestScenario] = []
        for c_name, c_ops in bundled_clusters.items():
            cluster_fallback = build_deterministic_fallback_scenarios(
                c_name, c_ops, test_config
            )
            self._record_cluster_provenance(
                c_name, c_ops, "fallback", cluster_fallback, reason="no_credentials"
            )
            fallback_scenarios.extend(cluster_fallback)
        self._finalize_generation_report(fallback_scenarios)
        return TestSuite(
            suite_id=f"suite_{self.session_id}",
            target_base_url=oas_metadata.base_url or "http://localhost:8080",
            scenarios=fallback_scenarios,
        )

    def execute(
        self,
        oas_metadata: OASMetadata,
        test_config: dict[str, Any] | None = None,
    ) -> TestSuite:
        """Executes scenario generation synchronously or within an active event loop."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(
                        asyncio.run, self._execute_async(oas_metadata, test_config)
                    ).result()
            return asyncio.run(self._execute_async(oas_metadata, test_config))
        except RuntimeError:
            return asyncio.run(self._execute_async(oas_metadata, test_config))

    def build_heuristic_test_plan(
        self,
        oas_metadata: OASMetadata,
        test_config: dict[str, Any] | None = None,
    ) -> TestPlan:
        """Constructs a deterministic baseline TestPlan with happy path and negative tests."""
        auth_header_name = "Authorization"
        auth_header_env_var = "API_AUTH_TOKEN"
        auth_prefix = "Bearer "

        for _, scheme in oas_metadata.security_schemes.items():
            if scheme.type == "apiKey" and scheme.name:
                auth_header_name = scheme.name
                auth_header_env_var = "API_KEY"
                auth_prefix = ""
                break
            if scheme.type == "http" and scheme.scheme == "bearer":
                auth_header_name = "Authorization"
                auth_header_env_var = "API_AUTH_TOKEN"
                auth_prefix = "Bearer "
                break

        test_cases: list[TestCasePlan] = []

        for ep in oas_metadata.endpoints:
            resolved_path = resolve_path_parameters(
                ep.path, ep.parameters, test_config
            )

            query_params: dict[str, Any] = {}
            for p in ep.parameters:
                if p.in_location == "query" and (p.required or p.default is not None):
                    query_params[p.name] = get_dummy_param_value(p)

            success_status = 200
            for code in ["200", "201", "204"]:
                if code in ep.responses:
                    success_status = int(code)
                    break

            is_auth_required = bool(ep.security)

            happy_payload = None
            if ep.method in ["POST", "PUT", "PATCH"] and ep.request_body:
                happy_payload = generate_mock_json_payload(
                    ep.request_body.raw_schema, ep.request_body.properties
                )

            happy_desc = (
                f"Verify {ep.method} {ep.path} returns {success_status} OK "
                "under valid conditions."
            )
            if ep.summary:
                happy_desc += f" ({ep.summary})"

            test_cases.append(
                TestCasePlan(
                    test_id=f"test_{ep.operation_id}_{success_status}",
                    operation_id=ep.operation_id,
                    method=ep.method,
                    resolved_path=resolved_path,
                    query_params=query_params,
                    headers=(
                        {"Content-Type": "application/json"} if happy_payload else {}
                    ),
                    request_body=happy_payload,
                    expected_status=success_status,
                    test_type="happy_path",
                    scenario_description=happy_desc,
                    auth_required=is_auth_required,
                )
            )

            if ep.method in ["POST", "PUT", "PATCH"] and ep.request_body:
                neg_payload = generate_invalid_json_payload(ep.request_body)
                neg_desc = (
                    f"Verify {ep.method} {ep.path} returns 400 Bad Request "
                    "when request body violates schema."
                )
                test_cases.append(
                    TestCasePlan(
                        test_id=f"test_{ep.operation_id}_400",
                        operation_id=ep.operation_id,
                        method=ep.method,
                        resolved_path=resolved_path,
                        query_params=query_params,
                        headers={"Content-Type": "application/json"},
                        request_body=neg_payload,
                        expected_status=400,
                        test_type="negative_400",
                        scenario_description=neg_desc,
                        auth_required=is_auth_required,
                    )
                )

        workflows: list[WorkflowPlan] = []
        endpoints_by_path: dict[str, dict[str, Any]] = {}
        for ep in oas_metadata.endpoints:
            endpoints_by_path.setdefault(ep.path, {})[ep.method] = ep

        for path, methods in endpoints_by_path.items():
            if "POST" in methods:
                post_ep = methods["POST"]
                resource_name = path.strip("/").split("/")[-1]
                item_path = None
                for candidate_path in endpoints_by_path:
                    if candidate_path.startswith(
                        f"{path}/{{"
                    ) and candidate_path.endswith("}"):
                        item_path = candidate_path
                        break

                if item_path and item_path in endpoints_by_path:
                    item_methods = endpoints_by_path[item_path]
                    steps = [
                        WorkflowStep(
                            step_name=f"create_{resource_name}",
                            method="POST",
                            path_template=path,
                            payload=(
                                generate_mock_json_payload(
                                    post_ep.request_body.raw_schema,
                                    post_ep.request_body.properties,
                                )
                                if post_ep.request_body
                                else None
                            ),
                            capture_var=f"{resource_name}_id",
                            capture_json_key="id",
                            expected_status=(
                                201 if "201" in post_ep.responses else 200
                            ),
                            auth_required=bool(post_ep.security),
                        )
                    ]
                    if "GET" in item_methods:
                        steps.append(
                            WorkflowStep(
                                step_name=f"get_{resource_name}_by_id",
                                method="GET",
                                path_template=f"{path}/{{{resource_name}_id}}",
                                expected_status=200,
                                auth_required=bool(item_methods["GET"].security),
                            )
                        )
                    if "DELETE" in item_methods:
                        steps.append(
                            WorkflowStep(
                                step_name=f"delete_{resource_name}",
                                method="DELETE",
                                path_template=f"{path}/{{{resource_name}_id}}",
                                expected_status=(
                                    204
                                    if "204" in item_methods["DELETE"].responses
                                    else 200
                                ),
                                auth_required=bool(item_methods["DELETE"].security),
                            )
                        )
                    if len(steps) >= 2:
                        workflows.append(
                            WorkflowPlan(
                                workflow_id=f"{resource_name}_crud_lifecycle",
                                title=f"{resource_name.capitalize()} Lifecycle",
                                description=(
                                    f"Stateful CRUD workflow: Create -> Read -> "
                                    f"Delete {resource_name}."
                                ),
                                steps=steps,
                            )
                        )

        return TestPlan(
            api_title=oas_metadata.title,
            base_url=oas_metadata.base_url,
            auth_header_name=auth_header_name,
            auth_header_env_var=auth_header_env_var,
            auth_prefix=auth_prefix,
            mode="deterministic",
            tests=test_cases,
            workflows=workflows,
        )
