"""Asynchronous HTTPX test execution engine, variable resolver, and semantic assertion evaluator."""

import asyncio
import logging
import os
import re
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from manager.core.schemas import (
    AssertionOperator,
    SemanticAssertion,
    TestScenario,
    TestStep,
)

logger = logging.getLogger(__name__)


class VariableResolver:
    """Resolves {{var}}, {var}, and $var placeholders and extracts JSONPath expressions."""

    TEMPLATE_PATTERN = re.compile(r"\{\{?([a-zA-Z0-9_]+)\}?\}|\$([a-zA-Z0-9_]+)")

    @classmethod
    def resolve_string(cls, template_str: str, context: dict[str, Any]) -> str:
        """Replaces variable placeholders in template_str with values from context."""

        def replace(match: re.Match[str]) -> str:
            var_name = match.group(1) or match.group(2)
            if var_name in context:
                return str(context[var_name])
            return match.group(0)

        return cls.TEMPLATE_PATTERN.sub(replace, template_str)

    @classmethod
    def resolve_deep(cls, data: Any, context: dict[str, Any]) -> Any:
        """Recursively resolves variable templates in strings, dicts, and lists."""
        if isinstance(data, str):
            return cls.resolve_string(data, context)
        if isinstance(data, dict):
            return {k: cls.resolve_deep(v, context) for k, v in data.items()}
        if isinstance(data, list):
            return [cls.resolve_deep(item, context) for item in data]
        return data

    @classmethod
    def extract_jsonpath(cls, payload: Any, path_expression: str) -> Any:
        """Pure-Python JSONPath extractor supporting dot and bracket notations."""
        if not isinstance(payload, (dict, list)) or not path_expression:
            return None
        expr = path_expression.strip()
        if expr.startswith("$."):
            expr = expr[2:]
        elif expr.startswith("$"):
            expr = expr[1:]

        tokens = re.findall(r"([^.\[\]]+)|\[(\d+)\]", expr)
        curr = payload
        for pos, (token, idx) in enumerate(tokens):
            next_is_index = (pos + 1 < len(tokens)) and bool(tokens[pos + 1][1])
            if token:
                token = token.strip("'\"")
                if isinstance(curr, dict) and token in curr:
                    curr = curr[token]
                elif (
                    isinstance(curr, dict)
                    and token == "@graph"
                    and "features" in curr
                ):
                    curr = curr["features"]
                elif (
                    isinstance(curr, dict)
                    and token == "features"
                    and "@graph" in curr
                ):
                    curr = curr["@graph"]
                elif (
                    isinstance(curr, dict)
                    and token == "@graph"
                    and (
                        "id" in curr
                        or "@id" in curr
                        or "type" in curr
                        or "properties" in curr
                    )
                ):
                    curr = [curr]
                elif (
                    isinstance(curr, dict)
                    and "properties" in curr
                    and isinstance(curr["properties"], dict)
                    and token in curr["properties"]
                ):
                    curr = curr["properties"][token]
                elif (
                    isinstance(curr, dict)
                    and token in ["@id", "id"]
                    and ("@id" in curr or "id" in curr)
                ):
                    curr = curr.get("@id") or curr.get("id")
                elif isinstance(curr, str) and token in ["@id", "id"]:
                    pass
                elif isinstance(curr, dict) and (
                    token
                    in [
                        "entry",
                        "items",
                        "data",
                        "@graph",
                        "features",
                        "results",
                        "records",
                        "locations",
                    ]
                    or next_is_index
                ):
                    matched = False
                    for alt in [
                        "features",
                        "@graph",
                        "items",
                        "entry",
                        "data",
                        "results",
                        "records",
                        "locations",
                    ]:
                        if alt in curr and isinstance(curr[alt], (list, dict)):
                            curr = curr[alt]
                            matched = True
                            break
                    if not matched and next_is_index:
                        list_vals = [
                            v for v in curr.values() if isinstance(v, list)
                        ]
                        if len(list_vals) == 1:
                            curr = list_vals[0]
                            matched = True
                        else:
                            dict_vals = [
                                v
                                for k, v in curr.items()
                                if not str(k).startswith("@")
                                and isinstance(v, dict)
                            ]
                            if len(dict_vals) == 1:
                                curr = dict_vals[0]
                                matched = True
                    if not matched:
                        return None
                elif isinstance(curr, list) and (
                    token
                    in [
                        "entry",
                        "items",
                        "data",
                        "@graph",
                        "features",
                        "results",
                        "records",
                        "locations",
                    ]
                    or next_is_index
                ):
                    pass
                else:
                    return None
                if (
                    token in ["@id", "id"]
                    and isinstance(curr, str)
                    and "/" in curr
                ):
                    curr = curr.rstrip("/").split("/")[-1]
            elif idx:
                try:
                    i = int(idx)
                    if isinstance(curr, dict):
                        for col in [
                            "@graph",
                            "features",
                            "items",
                            "data",
                            "results",
                            "records",
                            "entry",
                            "locations",
                        ]:
                            if col in curr and isinstance(curr[col], list):
                                curr = curr[col]
                                break
                    if isinstance(curr, list) and 0 <= i < len(curr):
                        curr = curr[i]
                    elif isinstance(curr, dict) and 0 <= i < len(curr):
                        non_meta = [
                            (k, v)
                            for k, v in curr.items()
                            if not str(k).startswith("@")
                        ]
                        if 0 <= i < len(non_meta):
                            k_i, v_i = non_meta[i]
                            curr = (
                                v_i
                                if isinstance(v_i, (dict, list))
                                else k_i
                            )
                        else:
                            curr = list(curr.values())[i]
                    else:
                        return None
                except (ValueError, IndexError):
                    return None
        return curr


def are_values_equivalent(val1: Any, val2: Any) -> bool:
    """Compares two values with case-insensitive string/boolean and numeric equivalence."""
    if val1 == val2:
        return True
    if val1 is None or val2 is None:
        return False
    s1 = str(val1).strip().lower()
    s2 = str(val2).strip().lower()
    if s1 == s2:
        return True
    try:
        if float(val1) == float(val2):
            return True
    except (ValueError, TypeError):
        pass
    return False


class AssertionEngine:
    """Evaluates declarative SemanticAssertion rules against JSON response payloads."""

    @staticmethod
    def evaluate_semantic(
        assertion: SemanticAssertion,
        response_data: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> None:
        """Evaluates a single SemanticAssertion and raises AssertionError on mismatch."""
        actual_val = VariableResolver.extract_jsonpath(
            response_data, assertion.target_expression
        )
        op = assertion.operator
        exp_val = assertion.expected_value
        if exp_val is not None and context:
            exp_val = VariableResolver.resolve_string(str(exp_val), context)

        if op == AssertionOperator.EQUALS:
            if not are_values_equivalent(actual_val, exp_val):
                raise AssertionError(
                    f"Expected {assertion.target_expression} == {exp_val}, got {actual_val}"
                )
        elif op == AssertionOperator.NOT_EQUALS:
            if are_values_equivalent(actual_val, exp_val):
                raise AssertionError(
                    f"Expected {assertion.target_expression} != {exp_val}, got {actual_val}"
                )
        elif op == AssertionOperator.CONTAINS:
            if (
                actual_val is None
                or str(exp_val).lower() not in str(actual_val).lower()
            ):
                raise AssertionError(f"Expected {actual_val} to contain {exp_val}")
        elif op == AssertionOperator.GREATER_THAN:
            try:
                if actual_val is None or float(actual_val) <= float(exp_val or 0):
                    raise AssertionError(
                        f"Expected {assertion.target_expression} > {exp_val}, got {actual_val}"
                    )
            except (ValueError, TypeError) as err:
                raise AssertionError(
                    f"Cannot compare non-numeric values for GREATER_THAN: {actual_val} vs {exp_val}"
                ) from err
        elif op == AssertionOperator.LESS_THAN:
            try:
                if actual_val is None or float(actual_val) >= float(exp_val or 0):
                    raise AssertionError(
                        f"Expected {assertion.target_expression} < {exp_val}, got {actual_val}"
                    )
            except (ValueError, TypeError) as err:
                raise AssertionError(
                    f"Cannot compare non-numeric values for LESS_THAN: {actual_val} vs {exp_val}"
                ) from err
        elif op == AssertionOperator.MATCHES_REGEX:
            if actual_val is None or not re.search(str(exp_val or ""), str(actual_val)):
                raise AssertionError(
                    f"Expected {actual_val} to match regex {exp_val}"
                )
        elif op == AssertionOperator.NOT_NULL:
            if actual_val is None:
                raise AssertionError(
                    f"Expected {assertion.target_expression} to be NOT NULL"
                )


class TestRunner:
    """Asynchronous HTTPX runner for executing stateful TestScenario journeys."""

    def __init__(
        self, base_url: str, step_delay_seconds: float | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.context: dict[str, Any] = {}
        if step_delay_seconds is None:
            self.step_delay_seconds = float(
                os.environ.get("OAS_STEP_DELAY_SECONDS", "0.25")
            )
        else:
            self.step_delay_seconds = max(0.0, float(step_delay_seconds))

    async def execute_scenario(
        self,
        scenario: TestScenario,
        step_results: dict[str, Any] | None = None,
        on_step_progress: Callable[[dict[str, Any]], Any] | None = None,
    ) -> bool:
        """Executes setup, test, and teardown steps in order, streaming step progress."""
        overall_success = True
        all_ordered_steps = (
            list(scenario.setup_steps)
            + list(scenario.test_steps)
            + list(scenario.teardown_steps)
        )
        total_steps = len(all_ordered_steps)
        completed_steps = 0

        async def emit_progress(info: dict[str, Any]) -> None:
            if on_step_progress:
                try:
                    res = on_step_progress(info)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as cb_err:
                    logger.warning("Error in on_step_progress callback: %s", cb_err)

        async def run_single_step(
            client: httpx.AsyncClient, step: TestStep
        ) -> bool:
            nonlocal completed_steps, overall_success
            if completed_steps > 0 and self.step_delay_seconds > 0:
                await asyncio.sleep(self.step_delay_seconds)
            step_idx = completed_steps + 1
            await emit_progress(
                {
                    "event": "STEP_START",
                    "step_id": step.step_id,
                    "step_index": step_idx,
                    "total_steps": total_steps,
                    "method": step.method.value,
                    "path_template": step.path_template,
                }
            )

            step_success = True
            resp = None
            err = None
            try:
                resp = await self._execute_step(client, step)
                if step_results is not None:
                    step_results[step.step_id] = {
                        "status": "SUCCESS",
                        "response": resp,
                    }
            except Exception as exc:
                step_success = False
                err = exc
                overall_success = False
                if step_results is not None:
                    if hasattr(exc, "response"):
                        step_results[step.step_id] = {
                            "status": "FAILED",
                            "response": getattr(exc, "response"),
                            "error": str(exc),
                        }
                    else:
                        step_results[step.step_id] = {
                            "status": "FAILED",
                            "error": str(exc),
                        }
            finally:
                completed_steps += 1
                body = None
                if resp is not None:
                    try:
                        body = resp.json()
                    except ValueError:
                        body = resp.text
                elif err and hasattr(err, "response"):
                    err_resp = getattr(err, "response")
                    if err_resp is not None:
                        try:
                            body = err_resp.json()
                        except ValueError:
                            body = err_resp.text

                req_url = ""
                req_method = step.method.value
                if resp and hasattr(resp, "request"):
                    req_url = str(resp.request.url)
                    req_method = resp.request.method
                elif (
                    err
                    and hasattr(err, "response")
                    and getattr(err, "response") is not None
                    and hasattr(getattr(err, "response"), "request")
                ):
                    req_url = str(getattr(err, "response").request.url)
                    req_method = getattr(err, "response").request.method

                status_code = (
                    resp.status_code
                    if resp
                    else (
                        getattr(getattr(err, "response", None), "status_code", "ERROR")
                        if err
                        else "ERROR"
                    )
                )

                await emit_progress(
                    {
                        "event": "STEP_PROGRESS",
                        "step_id": step.step_id,
                        "step_index": completed_steps,
                        "total_steps": total_steps,
                        "method": step.method.value,
                        "path_template": step.path_template,
                        "status": "SUCCESS" if step_success else "FAILED",
                        "status_code": status_code,
                        "request_url": req_url,
                        "request_method": req_method,
                        "body": body,
                        "error": str(err) if err else None,
                    }
                )

            return step_success

        base_parsed = urlparse(self.base_url)
        base_prefix = base_parsed.path.rstrip("/")

        async def rewrite_redirect(response: httpx.Response) -> None:
            if response.is_redirect and "location" in response.headers:
                loc = response.headers["location"]
                if (
                    base_prefix
                    and loc.startswith("/")
                    and not loc.startswith(base_prefix + "/")
                    and loc != base_prefix
                ):
                    response.headers["location"] = f"{base_prefix}{loc}"

        async with httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=True,
            event_hooks={"response": [rewrite_redirect]},
        ) as client:
            try:
                for step in scenario.setup_steps:
                    await run_single_step(client, step)

                for step in scenario.test_steps:
                    await run_single_step(client, step)

                return overall_success
            finally:
                for step in scenario.teardown_steps:
                    try:
                        await run_single_step(client, step)
                    except Exception as td_err:
                        logger.warning(
                            "Teardown step %s failed: %s", step.step_id, td_err
                        )

    async def _execute_step(
        self, client: httpx.AsyncClient, step: TestStep
    ) -> httpx.Response:
        # 1. Inject path_params directly into the template
        path = step.path_template
        if step.path_params:
            for k, v in step.path_params.items():
                if isinstance(v, str):
                    v_clean = v.strip("{}$")
                    if v_clean in self.context:
                        v = self.context[v_clean]
                    elif (v.startswith("{") and v.endswith("}")) or v.startswith("$"):
                        v = "1" if "id" in v_clean.lower() else f"sample_{v_clean}"
                path = path.replace(f"{{{k}}}", str(v))
                pattern = re.compile(rf"\{{{re.escape(k)}\}}", re.IGNORECASE)
                path = pattern.sub(str(v), path)

        # 2. Resolve any captured variables
        resolved_path = VariableResolver.resolve_string(path, self.context)

        # 3. Safe fallback for any unreplaced {placeholder} or $placeholder
        def dummy_sub(match: re.Match[str]) -> str:
            ph = (match.group(1) or match.group(2) or "").strip("{}$")
            if ph in self.context:
                return str(self.context[ph])
            return "1" if "id" in ph.lower() else f"sample_{ph}"

        resolved_path = re.sub(
            r"\{([^}]+)\}|\$([a-zA-Z0-9_]+)", dummy_sub, resolved_path
        )

        # 4. Resolve query params
        resolved_params: dict[str, Any] = {}
        if step.query_params:
            resolved_params = VariableResolver.resolve_deep(
                step.query_params, self.context
            )

        resolved_body = (
            VariableResolver.resolve_deep(step.request_body, self.context)
            if step.request_body
            else None
        )
        resolved_headers = (
            VariableResolver.resolve_deep(step.headers, self.context) or {}
        )
        resolved_headers.setdefault(
            "User-Agent",
            "OASTestAutomation/1.0 (test-automation; contact@example.com)",
        )

        response = await client.request(
            method=step.method.value,
            url=resolved_path,
            params=resolved_params,
            headers=resolved_headers,
            json=resolved_body,
            timeout=step.timeout_seconds,
        )

        # Retry once on transient 404 read-after-write lag when a step expects 2XX
        if (
            response.status_code == 404
            and 200 <= step.expected_status < 300
            and self.context
        ):
            await asyncio.sleep(max(self.step_delay_seconds, 0.35))
            response = await client.request(
                method=step.method.value,
                url=resolved_path,
                params=resolved_params,
                headers=resolved_headers,
                json=resolved_body,
                timeout=step.timeout_seconds,
            )

        if response.status_code != step.expected_status:
            err = AssertionError(
                f"Step {step.step_id} failed: Expected HTTP {step.expected_status}, "
                f"got {response.status_code}. Response: {response.text}"
            )
            setattr(err, "response", response)
            raise err

        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type:
            try:
                resp_json = response.json()
            except ValueError:
                resp_json = {}

            for capture in step.captures:
                val = VariableResolver.extract_jsonpath(resp_json, capture.json_path)
                if val is not None:
                    self.context[capture.variable_name] = val

            for assertion in step.semantic_assertions:
                try:
                    AssertionEngine.evaluate_semantic(
                        assertion, resp_json, self.context
                    )
                except AssertionError as a_err:
                    setattr(a_err, "response", response)
                    raise a_err

        return response
