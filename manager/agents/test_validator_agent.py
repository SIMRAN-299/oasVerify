"""LLM-driven TestValidatorAgent that diagnoses live API test failures and repairs test definitions."""

import asyncio
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from manager.agents.test_generator_agent import (
    _infer_step_resource_stem,
    _inject_context_into_payload,
    _singularize,
    _stem_from_variable_name,
    _to_snake_case,
    generate_mock_json_payload,
    sample_from_regex,
)
from manager.core.oas_parser import OASParser

logger = logging.getLogger(__name__)

_SPEC_CACHE: dict[str, Any] = {}


def safe_json_loads(s: str) -> Any:
    """Parses JSON safely even if the payload contains unescaped regex backslashes (e.g. \\d, \\w)."""
    try:
        return json.loads(s)
    except Exception:
        s_fixed = re.sub(r'\\(?![/"\\bfnrtu])', r'\\\\', s)
        try:
            return json.loads(s_fixed)
        except Exception:
            return None


class TestFix(BaseModel):
    """Structured repair patch for a failing test step."""

    __test__ = False

    reasoning: str = Field(
        ...,
        description="Explain why the server rejected the request or assertion and how this patch fixes it.",
    )
    fixed_step_id: str | None = Field(
        default=None,
        description="Specific step_id being fixed. If omitted, applies to the failing step.",
    )
    fixed_path_params: dict[str, Any] | None = Field(
        default=None,
        description="Corrected path parameters (only set if path_params need changing; otherwise null).",
    )
    fixed_query_params: dict[str, Any] | None = Field(
        default=None,
        description="Corrected query parameters (only set if query_params need changing; otherwise null).",
    )
    fixed_request_body: dict[str, Any] | None = Field(
        default=None,
        description="Corrected JSON request body payload (only set if request_body needs changing; otherwise null).",
    )
    fixed_expected_status: int | None = Field(
        default=None,
        description=(
            "Corrected expected HTTP status code ONLY if the test's status expectation "
            "was misaligned with the server contract (never set to 4XX on HAPPY_PATH_E2E or 5XX)."
        ),
    )
    clear_semantic_assertions: bool | None = Field(
        default=None,
        description="Set to true ONLY if the HTTP status was 2XX and a dynamic/brittle semantic_assertion failed.",
    )


def apply_test_fix_to_step(
    step: dict[str, Any],
    fix: TestFix,
    scenario_type: str | None = None,
) -> None:
    """Applies non-null fields from a TestFix patch onto a step dictionary in place."""
    is_happy_path = (scenario_type or "").upper() == "HAPPY_PATH_E2E"
    if fix.fixed_path_params:
        step.setdefault("path_params", {}).update(fix.fixed_path_params)
    if fix.fixed_query_params is not None:
        step["query_params"] = fix.fixed_query_params
    if fix.fixed_request_body is not None:
        step["request_body"] = fix.fixed_request_body
        step.setdefault("headers", {}).setdefault("Content-Type", "application/json")
    if fix.fixed_expected_status is not None and fix.fixed_expected_status < 500:
        if not (is_happy_path and fix.fixed_expected_status >= 400):
            step["expected_status"] = fix.fixed_expected_status
    if fix.clear_semantic_assertions:
        # Never clear semantic assertions if the step is still expecting an error status on HAPPY_PATH_E2E
        if not (is_happy_path and step.get("expected_status", 200) >= 400):
            step["semantic_assertions"] = []


class TestValidatorAgent:
    """Agent that validates live API test failures and synthesizes targeted TestFix patches."""

    __test__ = False

    def __init__(self) -> None:
        load_dotenv("../.env")
        load_dotenv(".env")

        self.use_vertex = (
            os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "false").lower() == "true"
        )
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")

        self.client = None
        try:
            if self.use_vertex:
                self.client = genai.Client(
                    vertexai=True,
                    project=os.environ.get("GOOGLE_CLOUD_PROJECT", "apigee-x-payg"),
                    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
                )
            elif api_key:
                self.client = genai.Client(api_key=api_key)
        except Exception as err:
            logger.warning("Could not initialize Gemini genai client: %s", err)

        self.model_id = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        self.semaphore = asyncio.Semaphore(3)
        # Who produced the most recent fix returned by heal_scenario_async: "gemini" or "deterministic".
        self.last_fix_source: str | None = None
        self.last_unhealed_reasoning: str | None = None

    @staticmethod
    def _extract_endpoint_context(
        spec_path: str | None,
        failed_scenario: dict[str, Any],
        target_step: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Extracts the OpenAPI endpoint schema and default payload for the failing step."""
        all_steps = (
            failed_scenario.get("setup_steps", [])
            + failed_scenario.get("test_steps", [])
            + failed_scenario.get("teardown_steps", [])
        )
        target_st_id = target_step.get("step_id")
        captured_by_stem: dict[str, str] = {}
        for st in all_steps:
            if st.get("step_id") == target_st_id:
                break
            st_stem = _infer_step_resource_stem(st.get("path_template", ""))
            for cap in st.get("captures", []) or []:
                var_name = cap.get("variable_name", "")
                j_path = cap.get("json_path", "")
                c_stem = _stem_from_variable_name(var_name, st_stem)
                if "." in j_path:
                    tail_prop = j_path.split(".")[-1]
                    tp_snake = _to_snake_case(tail_prop)
                    if tp_snake != "id" and tp_snake.endswith("_id"):
                        c_stem = _singularize(tp_snake[:-3])
                if c_stem and var_name:
                    captured_by_stem[c_stem] = var_name

        if spec_path and not os.path.exists(spec_path):
            repo_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            alt_path = os.path.join(repo_root, spec_path)
            if os.path.exists(alt_path):
                spec_path = alt_path

        if not spec_path or not os.path.exists(spec_path):
            return None

        try:
            parsed = _SPEC_CACHE.get(spec_path)
            if parsed is None:
                parsed = OASParser(session_id="validator").execute(spec_path)
                _SPEC_CACHE[spec_path] = parsed

            op_id = target_step.get("operation_id")
            method = str(target_step.get("method", "")).upper()
            path_tmpl = target_step.get("path_template", "")

            matched_ep = None
            for ep in parsed.endpoints:
                if op_id and ep.operation_id == op_id:
                    matched_ep = ep
                    break
                if ep.method.upper() == method and ep.path == path_tmpl:
                    matched_ep = ep
                    break

            if not matched_ep:
                return None

            success_status = 201 if method == "POST" else 200
            if matched_ep.responses:
                two_xx = [
                    int(code)
                    for code in matched_ep.responses.keys()
                    if str(code).isdigit() and 200 <= int(code) < 300
                ]
                if two_xx:
                    success_status = sorted(two_xx)[0]

            default_payload: dict[str, Any] | None = None
            req_body_schema: dict[str, Any] | None = None
            if matched_ep.request_body:
                req_body_schema = matched_ep.request_body.raw_schema or {
                    "required": matched_ep.request_body.required_fields,
                    "properties": matched_ep.request_body.properties,
                }
                default_payload = generate_mock_json_payload(
                    matched_ep.request_body.raw_schema,
                    matched_ep.request_body.properties,
                )
                if default_payload and captured_by_stem:
                    default_payload = _inject_context_into_payload(
                        default_payload, captured_by_stem
                    )

            params_info = [
                {
                    "name": p.name,
                    "in": p.in_location,
                    "required": p.required,
                    "type": getattr(p, "param_type", "string"),
                    "format": p.format,
                    "enum": p.enum,
                    "pattern": getattr(p, "pattern", None),
                    "example": p.example,
                    "default": getattr(p, "default", None),
                }
                for p in (matched_ep.parameters or [])
            ]

            # Cross-endpoint enrichment for parameters with identical names in same spec
            for p_info in params_info:
                p_name = p_info["name"]
                if not p_info.get("enum") or not p_info.get("pattern"):
                    for other_ep in parsed.endpoints:
                        for other_p in other_ep.parameters or []:
                            if other_p.name == p_name:
                                if not p_info.get("enum") and getattr(other_p, "enum", None):
                                    p_info["enum"] = other_p.enum
                                if not p_info.get("pattern") and getattr(other_p, "pattern", None):
                                    p_info["pattern"] = other_p.pattern

            return {
                "operation_id": matched_ep.operation_id,
                "method": matched_ep.method,
                "path": matched_ep.path,
                "success_status": success_status,
                "parameters": params_info,
                "request_body_schema": req_body_schema,
                "default_payload": default_payload,
                "captured_by_stem": captured_by_stem,
            }
        except Exception as err:
            logger.debug("Could not extract endpoint context from %s: %s", spec_path, err)
            return None

    def _build_validation_prompt(
        self,
        failed_scenario: dict[str, Any],
        error_message: str,
        endpoint_context: dict[str, Any] | None = None,
    ) -> str:
        schema_json = json.dumps(TestFix.model_json_schema(), indent=2)
        ep_section = ""
        if endpoint_context:
            ep_summary = {
                k: v
                for k, v in endpoint_context.items()
                if k in (
                    "operation_id",
                    "method",
                    "path",
                    "success_status",
                    "parameters",
                    "request_body_schema",
                    "default_payload",
                )
            }
            ep_section = f"""
### OpenAPI Endpoint Schema & Synthesized Default Payload:
```json
{json.dumps(ep_summary, indent=2)}
```
"""
        return f"""You are a Principal API Test Validator and Self-Healing Agent.
A test scenario was executed against a live API and failed.

### Failed Scenario Definition (from suite.json):
```json
{json.dumps(failed_scenario, indent=2)}
```
{ep_section}
### Live Execution Error / Server Rejection:
```
{error_message}
```

### Validation & Repair Rules (STRICT):
1. **Fix Inputs First, NEVER Weaken `HAPPY_PATH_E2E` Status to `4XX`**:
   - If `scenario_type` is `"HAPPY_PATH_E2E"` and the server returned `400`, `404`, or `422` (e.g., `"Validation error: body: Field required"`, missing fields, enum mismatch, regex/date format error, or invalid/unresolvable path/query parameter), you MUST fix `fixed_request_body`, `fixed_path_params`, or `fixed_query_params` so the request succeeds with `2XX`.
   - NOTE ON HTTP 404 WITH PARAMETER ERRORS: When an API (such as weather.gov or RFC 7807 problem details) returns HTTP 404 with `parameterErrors` (e.g. `{{"parameter": "path.zoneId", "message": "Does not match the regex pattern..."}}`) on a `HAPPY_PATH_E2E` test, the 404 is a parameter validation rejection, NOT a missing resource. You MUST extract the parameter name, strip any `path.` or `query.` prefix, and set `fixed_path_params` or `fixed_query_params` with a valid value matching the specified regex pattern or schema constraints, and correct any other dummy/placeholder parameters in the failing step (e.g. `sample_type` -> valid enum) so the request succeeds with 200.
   - NOTE ON HTTP 404 FROM DUMMY, UNRESOLVED, OR MISMATCHED PARAMETERS: Even when `parameterErrors` is absent on a `HAPPY_PATH_E2E` `404` failure, inspect the failing step's `path_params` and `query_params` alongside the `Request:` URL and `Captured Context:`. If any parameter holds a dummy placeholder (`"1"`, `"sample_*"`, or a truncated code like `"AKZ"`), an unresolved template variable (`"{{var}}"` missing from `Captured Context` or sent as `%7B%7B...%7D%7D`), or a captured ID of the wrong subtype (e.g. a county zone `ALC001` on a `/zones/forecast/` route), replace it in `fixed_path_params` or `fixed_query_params` with a valid schema `example`, `enum`, `pattern`-matching value, or known valid domain identifier.
   - For `BOUNDARY_NEGATIVE` scenarios where an intentionally invalid payload or parameter was sent: if the test expected `400` but the server rejected with `404` or `422`, DO NOT fix the inputs to valid values. Instead, set `fixed_expected_status` to match the server's actual 4XX rejection code (`404` or `422`).
   - NEVER set `fixed_expected_status` to `400`, `404`, `422`, or any `4XX`/`5XX` code on a `HAPPY_PATH_E2E` scenario!
   - If a `HAPPY_PATH_E2E` step currently has `"expected_status": 400` (from a prior misconfiguration), restore `"fixed_expected_status"` to its `2XX` `success_status` (`201` for `POST`, `200` for `GET`/`PUT`/`PATCH`) alongside fixing `fixed_request_body` / parameters.
   - NEVER set `fixed_expected_status` to a `5XX` server error (`500`, `502`, `503`, `504`).
2. **Dynamic Semantic Assertion Failures (ONLY on Valid `2XX` Responses)**:
   - ONLY set `"clear_semantic_assertions": true` if the HTTP response status was `2XX` (`200`/`201`) and a brittle or dynamic `semantic_assertion` failed on a valid success payload.
   - NEVER set `"clear_semantic_assertions": true` when the response body is a `400`/`404`/`422`/`500` error response (such as `"BAD_REQUEST"` or `"Validation error"`). Fix the request payload/parameters and restore `2XX` status instead!
3. **Minimal Targeted Patch (Null for Unchanged Fields)**:
   - Set ONLY the `fixed_*` fields that actually need to change.
   - Set all unchanged fields to `null`. NEVER output placeholder dummy maps like `{{"key": "value"}}` or empty `{{}}` for fields you are not fixing.
4. **When No Safe Patch Exists**:
   - If the failure is an upstream `5XX` server error (`500`, `502`, `503`, `504`), an authentication/permission rejection (`401`/`403`), or an unresolvable backend fault, leave all `fixed_*` fields as `null` and `clear_semantic_assertions` as `null`.
   - Keep `reasoning` to one short sentence (under 20 words) summarizing the root cause.

Return a single JSON object conforming to this schema:
{schema_json}
"""

    @staticmethod
    def _parse_fix_response(raw_text: str) -> TestFix:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        fix_data = json.loads(cleaned)
        # Guard against placeholder dummy values from LLM
        if fix_data.get("fixed_path_params") == {"key": "value"}:
            fix_data["fixed_path_params"] = None
        if fix_data.get("fixed_query_params") == {"key": "value"}:
            fix_data["fixed_query_params"] = None
        if fix_data.get("fixed_request_body") == {}:
            fix_data["fixed_request_body"] = None
        if (
            isinstance(fix_data.get("fixed_expected_status"), int)
            and fix_data["fixed_expected_status"] >= 500
        ):
            fix_data["fixed_expected_status"] = None
        return TestFix(**fix_data)

    def diagnose_and_heal_deterministic(
        self,
        failed_scenario: dict[str, Any],
        error_message: str,
        spec_path: str | None = None,
    ) -> TestFix | None:
        """Deterministic analyzer that repairs missing request_body payloads, parameter errors, regexes, enums, and status mismatches."""
        is_happy_path = (
            str(failed_scenario.get("scenario_type", "")).upper() == "HAPPY_PATH_E2E"
        )
        all_steps = (
            failed_scenario.get("setup_steps", [])
            + failed_scenario.get("test_steps", [])
            + failed_scenario.get("teardown_steps", [])
        )
        failing_st_id = None
        m_step = re.search(r"Step\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?\s+", error_message)
        if m_step:
            failing_st_id = m_step.group(1)
        elif all_steps:
            failing_st_id = all_steps[0].get("step_id")

        target_step = next(
            (s for s in all_steps if s.get("step_id") == failing_st_id),
            (all_steps[0] if all_steps else {}),
        )
        ep_ctx = self._extract_endpoint_context(
            spec_path, failed_scenario, target_step
        )

        # 0. Missing or invalid request_body on HAPPY_PATH_E2E POST/PUT/PATCH step (or step previously corrupted to 4XX)
        method_upper = str(target_step.get("method", "")).upper()
        cur_body = target_step.get("request_body")
        cur_expected = int(target_step.get("expected_status", 200))
        is_body_validation_err = (
            "Validation error" in error_message
            or "Field required" in error_message
            or '"loc":' in error_message
            or "BAD_REQUEST" in error_message
        )
        if is_happy_path and method_upper in {"POST", "PUT", "PATCH"}:
            if not isinstance(cur_body, dict) or not cur_body or is_body_validation_err:
                repaired_body: dict[str, Any] = (
                    dict(cur_body) if isinstance(cur_body, dict) else {}
                )
                if ep_ctx and isinstance(ep_ctx.get("default_payload"), dict):
                    for k, v in ep_ctx["default_payload"].items():
                        if k not in repaired_body or repaired_body[k] is None:
                            repaired_body[k] = v

                # Also inspect FastAPI/Pydantic validation error details for missing body fields if spec wasn't loaded
                for field_name in re.findall(
                    r'"loc"\s*:\s*\[\s*"body"\s*,\s*"([a-zA-Z0-9_]+)"\s*\]',
                    error_message,
                ):
                    if field_name not in repaired_body:
                        fn_lower = field_name.lower()
                        if "email" in fn_lower:
                            repaired_body[field_name] = "customer@example.com"
                        elif fn_lower.endswith("_id"):
                            stem = _singularize(fn_lower[:-3])
                            cap_map = (ep_ctx or {}).get("captured_by_stem") or {}
                            repaired_body[field_name] = (
                                f"{{{{{cap_map[stem]}}}}}"
                                if stem in cap_map
                                else "123e4567-e89b-12d3-a456-426614174000"
                            )
                        elif any(
                            w in fn_lower
                            for w in ("quantity", "count", "stock", "rating", "percent")
                        ):
                            repaired_body[field_name] = 10
                        elif any(w in fn_lower for w in ("price", "amount", "total")):
                            repaired_body[field_name] = 29.99
                        else:
                            repaired_body[field_name] = f"sample_{field_name}"

                if repaired_body:
                    restored_status = None
                    if cur_expected >= 400:
                        restored_status = (
                            ep_ctx.get("success_status")
                            if ep_ctx
                            else (201 if method_upper == "POST" else 200)
                        )
                    return TestFix(
                        reasoning=(
                            f"Synthesized valid request_body payload for {method_upper} "
                            f"{target_step.get('path_template')} from OpenAPI schema"
                            + (
                                f" and restored expected_status to {restored_status}."
                                if restored_status
                                else "."
                            )
                        ),
                        fixed_step_id=failing_st_id,
                        fixed_request_body=repaired_body,
                        fixed_expected_status=restored_status,
                    )

        # 1. Parameter validation errors (regex pattern mismatch, enum mismatch, date/time format, parameterErrors array)
        # ONLY repair parameters on HAPPY_PATH_E2E tests that are supposed to succeed with 2XX.
        # NEVER mutate intentionally invalid inputs on BOUNDARY_NEGATIVE tests!
        repaired_path_params: dict[str, Any] = {}
        repaired_query_params: dict[str, Any] = {}

        # Strip appended "| Request: ..." and "| Captured Context: {...}" before extracting the HTTP error JSON body
        body_part = error_message
        captured_ctx: dict[str, Any] = {}
        if " | Captured Context:" in body_part:
            body_part, ctx_str = body_part.split(" | Captured Context:", 1)
            ctx_parsed = safe_json_loads(ctx_str.strip())
            if isinstance(ctx_parsed, dict):
                captured_ctx = ctx_parsed
        if " | Request:" in body_part:
            body_part = body_part.split(" | Request:", 1)[0]

        # 1a. Inspect structured JSON problem details or error body (e.g. RFC 7807 parameterErrors, details, loc)
        parsed_body = None
        if "{" in body_part:
            start_brace = body_part.find("{")
            end_brace = body_part.rfind("}")
            if start_brace != -1 and end_brace > start_brace:
                parsed_body = safe_json_loads(body_part[start_brace : end_brace + 1])

        if isinstance(parsed_body, dict):
            raw_err_list = (
                parsed_body.get("parameterErrors")
                or parsed_body.get("invalid-params")
                or parsed_body.get("errors")
                or parsed_body.get("details")
                or parsed_body.get("detail")
            )
            if isinstance(raw_err_list, list):
                for pe in raw_err_list:
                    if not isinstance(pe, dict):
                        continue
                    raw_p = pe.get("parameter") or pe.get("name")
                    if not raw_p and pe.get("loc"):
                        raw_p = ".".join(str(x) for x in pe["loc"])
                    if not raw_p:
                        continue
                    p_clean = str(raw_p).replace("path.", "").replace("query.", "").strip()
                    is_path = (
                        str(raw_p).startswith("path.")
                        or p_clean in target_step.get("path_params", {})
                        or f"{{{p_clean}}}" in target_step.get("path_template", "")
                    )
                    pe_msg = str(pe.get("message") or pe.get("msg") or pe.get("reason") or "")

                    # Regex pattern mismatch
                    m_pe_pat = re.search(
                        r"(?:regex pattern|matches pattern|match pattern|pattern)\s+['\"]?(\^?[^'\"\r\n]+\$?)['\"]?",
                        pe_msg,
                        re.IGNORECASE,
                    )
                    if m_pe_pat:
                        pat_str = m_pe_pat.group(1).strip()
                        pat_val = sample_from_regex(pat_str, p_clean)
                        if pat_val:
                            if is_path:
                                repaired_path_params[p_clean] = pat_val
                            else:
                                repaired_query_params[p_clean] = pat_val
                            continue

                    # Enum mismatch
                    m_pe_enum = re.search(
                        r"(?:enumeration|must be one of)\s*(\[[^\]]+\])",
                        pe_msg,
                        re.IGNORECASE,
                    )
                    if m_pe_enum:
                        valid_vals = [
                            v
                            for v in re.findall(r'["\']?([a-zA-Z0-9_\-]+)["\']?', m_pe_enum.group(1))
                            if v
                        ]
                        if valid_vals:
                            if is_path:
                                repaired_path_params[p_clean] = valid_vals[0]
                            else:
                                repaired_query_params[p_clean] = valid_vals[0]
                            continue

                    # Date/Time format
                    if "date-time" in pe_msg or "YYYY-MM-DDThh" in pe_msg:
                        if is_path:
                            repaired_path_params[p_clean] = "2023-10-25T12:00:00Z"
                        else:
                            repaired_query_params[p_clean] = "2023-10-25T12:00:00Z"
                    elif "format YYYY-MM-DD" in pe_msg:
                        if is_path:
                            repaired_path_params[p_clean] = "2023-10-25"
                        else:
                            repaired_query_params[p_clean] = "2023-10-25"

        # 1b. Generic regex pattern mismatch in text error_message
        m_regex = re.search(
            r"(?:Does not match the regex pattern|string does not match regex|String should match pattern)\s+['\"]?([^'\"\r\n]+)['\"]?",
            body_part,
            re.IGNORECASE,
        )
        if m_regex:
            m_p = re.search(
                r'parameter"?:\s*"(?:path\.|query\.)?([a-zA-Z0-9_\-]+)"',
                body_part,
            )
            param_name = m_p.group(1) if m_p else None
            if not param_name and target_step.get("path_params"):
                for pk in target_step["path_params"].keys():
                    if pk.lower() in body_part.lower():
                        param_name = pk
                        break
            if param_name and param_name not in repaired_path_params and param_name not in repaired_query_params:
                pat_str = m_regex.group(1).strip().replace("\\\\", "\\")
                pat_val = sample_from_regex(pat_str, param_name)
                if pat_val:
                    is_path = (
                        param_name in target_step.get("path_params", {})
                        or "path." in body_part
                        or f"{{{param_name}}}" in target_step.get("path_template", "")
                    )
                    if is_path:
                        repaired_path_params[param_name] = pat_val
                    else:
                        repaired_query_params[param_name] = pat_val

        # 1c. Parameter enumeration mismatch in text error_message
        m_enum = re.search(
            r'parameter"?:\s*"(?:path\.|query\.)?([a-zA-Z0-9_\-]+)".*?enumeration\s*(\[[^\]]+\])',
            body_part,
            re.DOTALL | re.IGNORECASE,
        )
        if not m_enum:
            m_enum_match = re.search(
                r"enumeration\s*(\[[^\]]+\])",
                body_part,
                re.DOTALL | re.IGNORECASE,
            )
            m_p = re.search(
                r'parameter"?:\s*"(?:path\.|query\.)?([a-zA-Z0-9_\-]+)"',
                body_part,
            )
            if m_enum_match and m_p:
                param_name = m_p.group(1)
                enum_str = m_enum_match.group(1)
            else:
                param_name = None
                enum_str = None
        else:
            param_name = m_enum.group(1)
            enum_str = m_enum.group(2)

        if param_name and enum_str and param_name not in repaired_path_params and param_name not in repaired_query_params:
            try:
                valid_vals = [
                    v
                    for v in re.findall(r'["\']?([a-zA-Z0-9_\-]+)["\']?', enum_str)
                    if v
                ]
                if valid_vals:
                    val = valid_vals[0]
                    is_path = (
                        param_name in target_step.get("path_params", {})
                        or "path." in body_part
                        or f"{{{param_name}}}" in target_step.get("path_template", "")
                    )
                    if is_path:
                        repaired_path_params[param_name] = val
                    else:
                        repaired_query_params[param_name] = val
            except (ValueError, KeyError, TypeError) as err:
                logger.debug("Enumeration extraction failed: %s", err)

        # 1d. Date and Time format errors in text error_message
        if (
            "Invalid date" in body_part
            or "Invalid date-time" in body_part
            or "expected format YYYY" in body_part
        ):
            if "date-time" in body_part or "YYYY-MM-DDThh" in body_part:
                m_dt = re.search(
                    r'parameter"?:\s*"(?:path\.)?([a-zA-Z0-9_\-]+)"', body_part
                )
                dt_param = m_dt.group(1) if m_dt else "time"
                if dt_param not in repaired_path_params:
                    repaired_path_params[dt_param] = "2023-10-25T12:00:00Z"
            elif "format YYYY-MM-DD" in body_part:
                m_d = re.search(
                    r'parameter"?:\s*"(?:path\.)?([a-zA-Z0-9_\-]+)"', body_part
                )
                d_param = m_d.group(1) if m_d else "date"
                if d_param not in repaired_path_params:
                    repaired_path_params[d_param] = "2023-10-25"

        # 1e. Time regex pattern ^([01][0-9]|2[0-3])[0-5][0-9]$
        if "([01][0-9]|2[0-3])[0-5][0-9]" in body_part:
            m_t = re.search(
                r'parameter"?:\s*"(?:path\.)?([a-zA-Z0-9_\-]+)"', body_part
            )
            t_param = m_t.group(1) if m_t else "time"
            if t_param not in repaired_path_params:
                repaired_path_params[t_param] = "1200"

        # 1f. Multi-parameter healing for dummy placeholders, unresolved {{vars}}, or mismatched captured IDs
        is_4xx_rejection = bool(
            re.search(r"Expected HTTP 2\d\d,\s*got 4(?:00|04|22)", error_message)
        )
        if ep_ctx and isinstance(ep_ctx.get("parameters"), list):
            path_tmpl = target_step.get("path_template", "")
            step_path_params = target_step.get("path_params") or {}
            for p_info in ep_ctx["parameters"]:
                p_name = p_info.get("name")
                if not p_name:
                    continue
                is_p_in_path = p_name in step_path_params or f"{{{p_name}}}" in path_tmpl
                cur_val = step_path_params.get(p_name)
                if is_p_in_path and p_name not in repaired_path_params:
                    cur_str = str(cur_val) if cur_val is not None else ""
                    is_unresolved_var = False
                    is_mismatched_var = False
                    if "{{" in cur_str and "}}" in cur_str:
                        var_key = cur_str.strip("{}$ ")
                        if var_key not in captured_ctx or "%7B%7B" in error_message:
                            is_unresolved_var = True
                        elif is_4xx_rejection:
                            resolved_v = str(captured_ctx.get(var_key, ""))
                            if "/" in resolved_v:
                                resolved_v = resolved_v.rstrip("/").split("/")[-1]
                            if p_info.get("pattern") and not re.search(
                                p_info["pattern"], resolved_v
                            ):
                                is_mismatched_var = True
                            elif (
                                "zone" in p_name.lower()
                                and "/forecast/" in path_tmpl.lower()
                                and len(resolved_v) >= 3
                                and resolved_v[2].upper() == "C"
                            ):
                                is_mismatched_var = True
                            elif p_info.get("example") is not None and resolved_v != str(
                                p_info["example"]
                            ):
                                is_mismatched_var = True

                    pattern_mismatch_literal = bool(
                        p_info.get("pattern")
                        and cur_val is not None
                        and "{{" not in cur_str
                        and not re.search(p_info["pattern"], cur_str)
                    )
                    is_dummy = (
                        cur_val is None
                        or cur_str.startswith("sample_")
                        or cur_val in ("1", 1)
                        or (p_info.get("enum") and cur_val not in p_info["enum"])
                        or is_unresolved_var
                        or is_mismatched_var
                        or pattern_mismatch_literal
                    )
                    if is_dummy:
                        if p_info.get("enum"):
                            cand = p_info["enum"][0]
                            for opt in p_info["enum"]:
                                if str(opt).lower() in path_tmpl.lower():
                                    cand = opt
                                    break
                            repaired_path_params[p_name] = cand
                        elif p_info.get("example") is not None:
                            repaired_path_params[p_name] = p_info["example"]
                        elif p_info.get("pattern"):
                            pat_val = sample_from_regex(p_info["pattern"], p_name)
                            if pat_val:
                                repaired_path_params[p_name] = pat_val
                        elif p_info.get("default") is not None and str(
                            p_info["default"]
                        ) != str(cur_val):
                            repaired_path_params[p_name] = p_info["default"]

        # If any path or query parameters were repaired:
        # Note: Do NOT mutate intentionally invalid inputs on negative scenarios (e.g. BOUNDARY_NEGATIVE expecting 4XX).
        # Allow Section 5 to update expected_status to match server's 4XX code on negative tests.
        should_repair_params = not (cur_expected >= 400 and not is_happy_path)
        if should_repair_params and (repaired_path_params or repaired_query_params):
            restored_status = None
            if is_happy_path and cur_expected >= 400:
                restored_status = (
                    ep_ctx.get("success_status")
                    if ep_ctx
                    else (201 if method_upper == "POST" else 200)
                )
            repaired_names = list(repaired_path_params.keys()) + list(repaired_query_params.keys())
            return TestFix(
                reasoning=(
                    f"Server rejected parameter validation; synthesized valid values "
                    f"for {repaired_names} matching schema constraints."
                ),
                fixed_step_id=failing_st_id,
                fixed_path_params=repaired_path_params or None,
                fixed_query_params=repaired_query_params or None,
                fixed_expected_status=restored_status,
            )

        # 4. Semantic assertion failure (ONLY on valid 2XX responses, never on 4XX/5XX error bodies)
        if (
            "semantic assertion failed" in error_message.lower()
            or "to be NOT NULL" in error_message
            or "Expected $." in error_message
        ):
            if not is_body_validation_err and not (is_happy_path and cur_expected >= 400):
                return TestFix(
                    reasoning=(
                        "Semantic assertion failed on dynamic or optional response "
                        "property; cleared brittle semantic assertion."
                    ),
                    fixed_step_id=failing_st_id,
                    clear_semantic_assertions=True,
                )

        # 5. Status code mismatch (never weaken HAPPY_PATH_E2E to 4XX, and never weaken any step to 5XX)
        m_status = re.search(r"Expected HTTP (\d+),\s*got (\d+)", error_message)
        if m_status:
            exp_code = int(m_status.group(1))
            act_code = int(m_status.group(2))
            if act_code < 500 and not (is_happy_path and act_code >= 400):
                return TestFix(
                    reasoning=(
                        f"API returned HTTP {act_code} (test expected {exp_code}); "
                        "updating assertion to match server contract."
                    ),
                    fixed_step_id=failing_st_id,
                    fixed_expected_status=act_code,
                )

        return None

    @staticmethod
    def _extract_server_error_summary(error_message: str) -> str:
        """Extracts a short, concise error label (max ~60 chars) from the server response."""
        base_err = error_message
        if " | Request:" in base_err:
            base_err = base_err.split(" | Request:", 1)[0].strip()
        if " | Captured Context:" in base_err:
            base_err = base_err.split(" | Captured Context:", 1)[0].strip()

        raw_body = ""
        for marker in ("Response:", "Body:"):
            idx = base_err.find(marker)
            if idx != -1:
                raw_body = base_err[idx + len(marker) :].strip()
                break
        if not raw_body:
            m_http = re.search(r"(?:got|HTTP)\s+\d+\s*:\s*(.+)$", base_err, re.DOTALL)
            if m_http:
                raw_body = m_http.group(1).strip()

        if raw_body.startswith("{"):
            try:
                parsed = safe_json_loads(raw_body)
                if isinstance(parsed, dict):
                    param_errs = (
                        parsed.get("parameterErrors")
                        or parsed.get("invalid-params")
                        or parsed.get("errors")
                        or parsed.get("details")
                        or parsed.get("detail")
                    )
                    if isinstance(param_errs, list) and param_errs:
                        first_pe = param_errs[0]
                        if isinstance(first_pe, dict):
                            p_name = first_pe.get("parameter") or first_pe.get("name") or ".".join(
                                str(x) for x in (first_pe.get("loc") or [])
                            )
                            p_msg = first_pe.get("message") or first_pe.get("msg") or first_pe.get("reason")
                            if p_name and p_msg:
                                summary = f"{p_name}: {p_msg}"
                                return summary[:70] + ("…" if len(summary) > 70 else "")

                    title = parsed.get("title") or parsed.get("error")
                    detail = parsed.get("detail") or parsed.get("message")
                    if title:
                        return str(title).strip()[:60]
                    if detail:
                        first_sentence = str(detail).split(".")[0].strip()
                        return first_sentence[:60] + ("…" if len(first_sentence) > 60 else "")
            except Exception:
                pass

        cleaned = re.sub(r"\s+", " ", raw_body).strip()
        if cleaned:
            return cleaned[:60] + ("…" if len(cleaned) > 60 else "")
        return ""

    def explain_unhealed_failure(
        self,
        failed_scenario: dict[str, Any],
        error_message: str,
        failing_step_id: str | None = None,
    ) -> str:
        """Builds a single concise sentence explaining why a failing step could not be self-healed."""
        sc_type = str(failed_scenario.get("scenario_type", "")).upper()
        is_happy_path = sc_type == "HAPPY_PATH_E2E"

        m_status = re.search(r"Expected HTTP (\d+),\s*got (\d+)", error_message)
        server_summary = self._extract_server_error_summary(error_message)
        detail_suffix = f" ({server_summary})" if server_summary else ""

        if m_status:
            exp_code = int(m_status.group(1))
            act_code = int(m_status.group(2))
            if act_code >= 500:
                return (
                    f"Server returned HTTP {act_code}{detail_suffix} instead of {exp_code} — "
                    "upstream server error cannot be fixed by modifying test parameters."
                )
            if act_code in (401, 403):
                return (
                    f"Server returned HTTP {act_code}{detail_suffix} instead of {exp_code} — "
                    "authentication or permission failure cannot be auto-healed."
                )
            if act_code == 404 and is_happy_path:
                if (
                    "parameterErrors" in error_message
                    or "Does not match the regex" in error_message
                    or "regex pattern" in error_message
                    or "invalid-params" in error_message
                ):
                    return (
                        f"Server returned HTTP 404{detail_suffix} instead of {exp_code} — "
                        "request parameter failed validation constraints."
                    )
                return (
                    f"Server returned HTTP 404{detail_suffix} instead of {exp_code} — "
                    "resource not found, and Happy Path tests cannot be weakened to 404."
                )
            if act_code >= 400 and is_happy_path:
                return (
                    f"Server returned HTTP {act_code}{detail_suffix} instead of {exp_code} — "
                    "response did not specify a repairable parameter constraint."
                )
            return (
                f"Server returned HTTP {act_code}{detail_suffix} instead of {exp_code} — "
                "no safe parameter or status adjustment found."
            )

        if (
            "semantic assertion failed" in error_message.lower()
            or "to be NOT NULL" in error_message
            or "Expected $." in error_message
        ):
            core_err = error_message.split("|")[0].strip()
            if len(core_err) > 80:
                core_err = core_err[:80] + "…"
            return f"{core_err} — required response property could not be verified."

        core_err = error_message.split("|")[0].strip()
        if len(core_err) > 80:
            core_err = core_err[:80] + "…"
        return f"Request failed ({core_err}) — network or server transport error cannot be auto-healed."

    async def heal_scenario_async(
        self,
        failed_scenario: dict[str, Any],
        error_message: str,
        spec_path: str | None = None,
    ) -> TestFix | None:
        """Asynchronously diagnoses a failing scenario using Gemini with deterministic fallback."""
        logger.info(
            "Triggering Async Self-Healing for scenario: %s",
            failed_scenario.get("name"),
        )
        self.last_fix_source = None
        self.last_unhealed_reasoning = None
        is_happy_path = (
            str(failed_scenario.get("scenario_type", "")).upper() == "HAPPY_PATH_E2E"
        )
        all_steps = (
            failed_scenario.get("setup_steps", [])
            + failed_scenario.get("test_steps", [])
            + failed_scenario.get("teardown_steps", [])
        )
        failing_st_id = None
        m_step = re.search(r"Step\s+['\"]?([a-zA-Z0-9_\-]+)['\"]?\s+", error_message)
        if m_step:
            failing_st_id = m_step.group(1)
        elif all_steps:
            failing_st_id = all_steps[0].get("step_id")
        target_step = next(
            (s for s in all_steps if s.get("step_id") == failing_st_id),
            (all_steps[0] if all_steps else {}),
        )
        ep_ctx = self._extract_endpoint_context(
            spec_path, failed_scenario, target_step
        )

        if self.client:
            try:
                prompt = self._build_validation_prompt(
                    failed_scenario, error_message, endpoint_context=ep_ctx
                )
                config_kwargs: dict[str, Any] = {
                    "response_mime_type": "application/json",
                    "temperature": float(
                        os.environ.get("GEMINI_TEMPERATURE", "0.0")
                    ),
                }

                async with self.semaphore:
                    response = await asyncio.wait_for(
                        self.client.aio.models.generate_content(
                            model=self.model_id,
                            contents=prompt,
                            config=types.GenerateContentConfig(**config_kwargs),
                        ),
                        timeout=30.0,
                    )
                if response.text:
                    raw_obj = json.loads(
                        re.sub(
                            r"^```(?:json)?\s*|\s*```$",
                            "",
                            response.text.strip(),
                        )
                    )
                    had_blocked_status = isinstance(
                        raw_obj.get("fixed_expected_status"), int
                    ) and (
                        raw_obj["fixed_expected_status"] >= 500
                        or (
                            is_happy_path
                            and raw_obj["fixed_expected_status"] >= 400
                        )
                    )
                    fix = self._parse_fix_response(response.text)
                    # Enforce HAPPY_PATH_E2E guardrails on LLM output
                    if is_happy_path:
                        if (
                            fix.fixed_expected_status is not None
                            and fix.fixed_expected_status >= 400
                        ):
                            fix.fixed_expected_status = None
                        cur_expected = int(target_step.get("expected_status", 200))
                        if cur_expected >= 400 and fix.fixed_expected_status is None:
                            method_upper = str(target_step.get("method", "")).upper()
                            fix.fixed_expected_status = (
                                ep_ctx.get("success_status")
                                if ep_ctx
                                else (201 if method_upper == "POST" else 200)
                            )
                        if (
                            "Validation error" in error_message
                            or "BAD_REQUEST" in error_message
                            or cur_expected >= 400
                        ):
                            fix.clear_semantic_assertions = None
                        # If step is missing request_body and LLM didn't supply one, backfill from ep_ctx
                        if (
                            str(target_step.get("method", "")).upper()
                            in {"POST", "PUT", "PATCH"}
                            and not target_step.get("request_body")
                            and fix.fixed_request_body is None
                            and ep_ctx
                            and isinstance(ep_ctx.get("default_payload"), dict)
                        ):
                            fix.fixed_request_body = dict(ep_ctx["default_payload"])

                    if (
                        fix.fixed_path_params
                        or fix.fixed_query_params is not None
                        or fix.fixed_request_body is not None
                        or fix.fixed_expected_status is not None
                        or fix.clear_semantic_assertions
                    ):
                        self.last_fix_source = "gemini"
                        return fix
                    if fix.reasoning and not had_blocked_status:
                        self.last_unhealed_reasoning = fix.reasoning.strip()
            except Exception as err:
                logger.warning(
                    "Async LLM healing failed (%s); falling back to deterministic analyzer.",
                    err,
                )

        fix = self.diagnose_and_heal_deterministic(
            failed_scenario, error_message, spec_path=spec_path
        )
        self.last_fix_source = "deterministic" if fix else None
        return fix
