"""Unit tests for the OASVerify pipeline components."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from fastapi.testclient import TestClient

# Ensure repo root is on sys.path
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from manager.agents.test_generator_agent import (  # noqa: E402
    TestGeneratorAgent,
    build_deterministic_fallback_scenarios,
    bundle_clusters,
    extract_domain_cluster,
    generate_mock_json_payload,
    get_dummy_param_value,
    resolve_path_parameters,
)
from manager.agents.test_validator_agent import TestValidatorAgent  # noqa: E402
from manager.core.github_deployer import GitHubDeployer  # noqa: E402
from manager.core.orchestrator import OASTestOrchestrator  # noqa: E402
from manager.core.runtime import (  # noqa: E402
    AssertionEngine,
    TestRunner,
    VariableResolver,
    are_values_equivalent,
)
from manager.core.schemas import (  # noqa: E402
    AssertionOperator,
    HTTPMethod,
    ScenarioType,
    SemanticAssertion,
    TestScenario,
    TestStep,
)
from manager.models import (  # noqa: E402
    EdgeCaseSuggestions,
    EndpointMetadata,
    OASMetadata,
    ParameterMetadata,
    RequestBodyMetadata,
    ReviewResult,
    SecurityScheme,
    TestCasePlan,
    TestPlan,
)
from manager.core.codegen import generate_python_test_suite  # noqa: E402
from manager.tools.generation_tools import render_test_code_from_plan  # noqa: E402
from manager.tools.github_tools import (  # noqa: E402
    commit_and_push_local_git,
    make_timestamped_folder,
)
from manager.tools.review_tools import run_linter  # noqa: E402
from server import app  # noqa: E402


class TestOASPipeline(unittest.TestCase):
    """Verifies Pydantic schema validation, parameter resolution, and code generation."""

    def test_parameter_dummy_generation(self):
        """Tests dummy value generation for different parameter types and formats."""
        uuid_param = ParameterMetadata(
            name="userId", in_location="path", required=True, format="uuid"
        )
        self.assertEqual(
            get_dummy_param_value(uuid_param),
            "123e4567-e89b-12d3-a456-426614174000",
        )

        enum_param = ParameterMetadata(
            name="status", in_location="query", enum=["active", "pending"]
        )
        self.assertEqual(get_dummy_param_value(enum_param), "active")

        int_param = ParameterMetadata(
            name="limit", in_location="query", param_type="integer"
        )
        self.assertEqual(get_dummy_param_value(int_param), 1)

    def test_path_parameter_resolution(self):
        """Verifies that {path_param} placeholders are substituted with concrete dummy values."""
        params = [
            ParameterMetadata(
                name="alertId", in_location="path", required=True, format="uuid"
            ),
            ParameterMetadata(name="filter", in_location="query"),
        ]
        resolved = resolve_path_parameters("/alerts/{alertId}", params)
        self.assertEqual(
            resolved, "/alerts/123e4567-e89b-12d3-a456-426614174000"
        )
        self.assertNotIn("{alertId}", resolved)

    def test_mock_json_payload_generation(self):
        """Verifies schema-compliant mock payload generation."""
        schema = {
            "type": "object",
            "properties": {
                "username": {"type": "string"},
                "email": {"type": "string", "format": "email"},
                "age": {"type": "integer", "minimum": 18},
                "isActive": {"type": "boolean"},
            },
            "required": ["username", "email"],
        }
        payload = generate_mock_json_payload(schema, schema["properties"])
        self.assertEqual(payload["username"], "sample_username")
        self.assertEqual(payload["email"], "user@example.com")
        self.assertEqual(payload["age"], 18)
        self.assertTrue(payload["isActive"])

    def test_planner_creates_happy_and_negative_tests(self):
        """Verifies that TestGeneratorAgent generates both 200 and 400 test cases."""
        ep = EndpointMetadata(
            path="/items/{itemId}",
            method="POST",
            operation_id="create_item",
            summary="Create a new item",
            parameters=[
                ParameterMetadata(
                    name="itemId", in_location="path", required=True
                )
            ],
            request_body=RequestBodyMetadata(
                required=True,
                properties={"name": {"type": "string"}},
                required_properties=["name"],
            ),
            responses={"201": {"description": "Created"}},
            security=[{"BearerAuth": []}],
        )
        oas_meta = OASMetadata(
            title="Item_Service",
            version="1.0.0",
            base_url="https://api.example.com",
            security_schemes={
                "BearerAuth": SecurityScheme(
                    scheme_name="BearerAuth", type="http", scheme="bearer"
                )
            },
            endpoints=[ep],
        )

        generator = TestGeneratorAgent(session_id="unit_test_session")
        plan = generator.build_heuristic_test_plan(oas_meta)

        self.assertEqual(len(plan.tests), 2)
        happy_test = plan.tests[0]
        neg_test = plan.tests[1]

        self.assertEqual(happy_test.expected_status, 201)
        self.assertEqual(happy_test.test_type, "happy_path")
        self.assertTrue(happy_test.auth_required)
        self.assertNotIn("{itemId}", happy_test.resolved_path)

        self.assertEqual(neg_test.expected_status, 400)
        self.assertEqual(neg_test.test_type, "negative_400")

    def test_generated_pytest_code_is_syntactically_valid(self):
        """Verifies that the generated PyTest file compiles cleanly with Python standard compile()."""
        test_case = TestCasePlan(
            test_id="test_get_alerts_200",
            operation_id="get_alerts",
            method="GET",
            resolved_path="/alerts/sample_id",
            query_params={"active": True},
            headers={},
            request_body=None,
            expected_status=200,
            test_type="happy_path",
            scenario_description="Test GET alerts returns 200 OK under valid conditions.",
            auth_required=True,
        )
        plan = TestPlan(
            api_title="Alert_API",
            base_url="https://api.weather.gov",
            tests=[test_case],
        )

        code = render_test_code_from_plan(plan)

        # Check for docstrings, env base URL, and auth
        self.assertIn("API_BASE_URL", code)
        self.assertIn("https://api.weather.gov", code)
        self.assertIn("API_AUTH_TOKEN", code)
        self.assertIn(
            '"""Test GET alerts returns 200 OK under valid conditions."""', code
        )

        # Verify it compiles cleanly with zero syntax errors!
        compiled = compile(code, "<test_code>", "exec")
        self.assertIsNotNone(compiled)

    def test_crud_workflow_generation_and_compilation(self):
        """Verifies stateful CRUD workflow discovery, variable capturing, and compilation."""
        post_ep = EndpointMetadata(
            path="/products",
            method="POST",
            operation_id="create_product",
            request_body=RequestBodyMetadata(
                required=True,
                properties={
                    "name": {"type": "string"},
                    "price": {"type": "number"},
                },
                required_properties=["name"],
            ),
            responses={"201": {"description": "Created"}},
        )
        get_ep = EndpointMetadata(
            path="/products/{productId}",
            method="GET",
            operation_id="get_product",
            parameters=[
                ParameterMetadata(
                    name="productId", in_location="path", required=True
                )
            ],
            responses={"200": {"description": "OK"}},
        )
        del_ep = EndpointMetadata(
            path="/products/{productId}",
            method="DELETE",
            operation_id="delete_product",
            parameters=[
                ParameterMetadata(
                    name="productId", in_location="path", required=True
                )
            ],
            responses={"204": {"description": "Deleted"}},
        )
        oas_meta = OASMetadata(
            title="Product_Store",
            version="1.0.0",
            endpoints=[post_ep, get_ep, del_ep],
        )

        generator = TestGeneratorAgent(session_id="wf_test")
        plan = generator.build_heuristic_test_plan(oas_meta)

        self.assertEqual(len(plan.workflows), 1)
        wf = plan.workflows[0]
        self.assertEqual(wf.workflow_id, "products_crud_lifecycle")
        self.assertEqual(len(wf.steps), 3)
        self.assertEqual(wf.steps[0].method, "POST")
        self.assertEqual(wf.steps[1].method, "GET")
        self.assertEqual(wf.steps[2].method, "DELETE")

        # Render and compile the workflow test code
        code = render_test_code_from_plan(plan)
        self.assertIn("def test_workflow_products_crud_lifecycle():", code)
        self.assertIn("captured_vars", code)
        self.assertIn("requests.post", code)
        self.assertIn("requests.delete", code)

        compiled = compile(code, "<workflow_code>", "exec")
        self.assertIsNotNone(compiled)

    def test_edge_case_suggestions_model_and_rendering(self):
        """Verifies EdgeCaseSuggestions structured output validation and code generation."""
        raw_json = """{
            "tests": [
                {
                    "test_id": "test_create_product_negative_price",
                    "operation_id": "create_product",
                    "method": "POST",
                    "resolved_path": "/products",
                    "request_body_json": "{\\"name\\": \\"Item\\", \\"price\\": -5.0}",
                    "expected_status": 400,
                    "scenario_description": "Verify POST /products returns 400 when price is negative"
                }
            ]
        }"""
        suggestions = EdgeCaseSuggestions.model_validate_json(raw_json)
        self.assertEqual(len(suggestions.tests), 1)
        gt = suggestions.tests[0]
        self.assertEqual(gt.test_id, "test_create_product_negative_price")

        payload = json.loads(gt.request_body_json)
        plan = TestPlan(
            api_title="Test API",
            base_url="https://api.example.com",
            tests=[
                TestCasePlan(
                    test_id=gt.test_id,
                    operation_id=gt.operation_id,
                    method=gt.method,
                    resolved_path=gt.resolved_path,
                    request_body=payload,
                    expected_status=gt.expected_status,
                    test_type="negative_400",
                    scenario_description=gt.scenario_description,
                )
            ],
        )
        code = render_test_code_from_plan(plan)
        self.assertIn("def test_create_product_negative_price():", code)
        self.assertIn("payload = {'name': 'Item', 'price': -5.0}", code)
        self.assertIn("assert response.status_code in [400, 422]", code)
        compiled = compile(code, "<edge_case_code>", "exec")
        self.assertIsNotNone(compiled)

    def test_generic_domain_clustering_across_diverse_apis(self):
        """Verifies domain clustering works generically across diverse API types without hardcoding."""
        # Tag-based extraction
        self.assertEqual(
            extract_domain_cluster("/pet/findByStatus", ["Pet Store"]),
            "pet_store",
        )
        self.assertEqual(
            extract_domain_cluster("/store/order", ["Store"]), "store"
        )

        # Path-segment fallback (stripping API prefixes like /v1, /api)
        self.assertEqual(
            extract_domain_cluster("/api/v2/customers/{id}/invoices"),
            "customers",
        )
        self.assertEqual(
            extract_domain_cluster("/v3/inventory/warehouses"), "inventory"
        )
        self.assertEqual(
            extract_domain_cluster("/rest/orders/{orderId}/pay"), "orders"
        )

        # Weather / geospatial paths
        self.assertEqual(extract_domain_cluster("/points/{point}"), "points")
        self.assertEqual(
            extract_domain_cluster("/gridpoints/{wfo}/{x},{y}/forecast"),
            "gridpoints",
        )

        # Root route fallback
        self.assertEqual(extract_domain_cluster("/{id}"), "general")

    def test_bundle_clusters_limits_parallel_calls_without_dropping_endpoints(
        self,
    ):
        """Verifies that large cluster sets are cleanly packed into <= max_clusters without losing endpoints."""
        raw = {
            f"domain_{i}": [{"op": f"op_{i}_{j}"} for j in range(i + 1)]
            for i in range(10)
        }
        total_ops_before = sum(len(ops) for ops in raw.values())

        bundled = bundle_clusters(raw, max_clusters=6)
        self.assertLessEqual(len(bundled), 6)

        total_ops_after = sum(len(ops) for ops in bundled.values())
        self.assertEqual(
            total_ops_before,
            total_ops_after,
            "No operations should be lost during bundling",
        )
        self.assertIn("auxiliary_endpoints", bundled)

    def test_build_deterministic_fallback_scenarios_coverage_and_defaults(self):
        """Verifies fallback scenarios generate both happy path and negative tests with resolved parameters."""
        ops = [
            {
                "operation_id": "create_widget",
                "method": "POST",
                "path": "/widgets",
                "parameters": [],
                "request_body_properties": ["name", "sku"],
                "required_body_fields": ["name"],
                "success_status": 201,
            },
            {
                "operation_id": "get_widget",
                "method": "GET",
                "path": "/widgets/{widgetId}",
                "parameters": [
                    {
                        "name": "widgetId",
                        "in": "path",
                        "required": True,
                        "type": "string",
                        "format": "uuid",
                    },
                    {
                        "name": "verbose",
                        "in": "query",
                        "required": False,
                        "type": "boolean",
                    },
                ],
                "success_status": 200,
            },
        ]

        scenarios = build_deterministic_fallback_scenarios("widgets", ops)
        self.assertEqual(len(scenarios), 2)  # Happy path + boundary negative

        happy = scenarios[0]
        self.assertEqual(happy.name, "Baseline Flow: Widgets")
        self.assertEqual(len(happy.test_steps), 2)

        # POST step checks
        post_step = happy.test_steps[0]
        self.assertEqual(post_step.expected_status, 201)
        self.assertIsNotNone(post_step.request_body)
        self.assertIn("name", post_step.request_body)
        self.assertEqual(
            post_step.headers.get("Content-Type"), "application/json"
        )
        self.assertEqual(len(post_step.captures), 1)

        # GET step checks (camelCase widgetId is chained from POST /widgets capture)
        get_step = happy.test_steps[1]
        self.assertEqual(
            get_step.path_params.get("widgetId"),
            "{{widgets_id}}",
        )

        # Boundary negative checks
        neg = scenarios[1]
        self.assertEqual(neg.scenario_type.value, "BOUNDARY_NEGATIVE")
        self.assertEqual(neg.test_steps[0].expected_status, 404)
        self.assertEqual(
            neg.test_steps[0].path_params.get("widgetId"),
            "invalid_nonexistent_id_999999",
        )

    def test_jsonpath_extraction_and_variable_resolver(self):
        """Verifies pure Python dot/bracket JSONPath and variable string resolution."""
        payload = {
            "store": {
                "books": [
                    {"title": "Book A", "price": 10},
                    {"title": "Book B", "price": 20},
                ],
                "metadata": {"total": 2},
            }
        }

        self.assertEqual(
            VariableResolver.extract_jsonpath(
                payload, "$.store.books[0].title"
            ),
            "Book A",
        )
        self.assertEqual(
            VariableResolver.extract_jsonpath(payload, "store.books[1].price"),
            20,
        )
        self.assertEqual(
            VariableResolver.extract_jsonpath(
                payload, "$.store.metadata.total"
            ),
            2,
        )
        self.assertIsNone(
            VariableResolver.extract_jsonpath(payload, "$.store.nonexistent")
        )

        # Variable template replacement
        ctx = {"book_id": "99", "user": "alice"}
        self.assertEqual(
            VariableResolver.resolve_string(
                "/books/{book_id}/authors/{{user}}", ctx
            ),
            "/books/99/authors/alice",
        )

    def test_assertion_engine_contextual_evaluation(self):
        """Verifies semantic assertion engine with context variable expansion and loose equality."""
        data = {"id": 123, "status": "ACTIVE", "tags": ["a", "b"]}
        ctx = {"expected_id": "123", "target_status": "ACTIVE"}

        # Loose equality string comparison
        eq_assert = SemanticAssertion(
            target_expression="$.id",
            operator=AssertionOperator.EQUALS,
            expected_value="{expected_id}",
        )
        AssertionEngine.evaluate_semantic(eq_assert, data, ctx)

        # Contains assertion
        contains_assert = SemanticAssertion(
            target_expression="$.status",
            operator=AssertionOperator.CONTAINS,
            expected_value="ACT",
        )
        AssertionEngine.evaluate_semantic(contains_assert, data, ctx)

    def test_scenario_step_progress_callback(self):
        """Verifies on_step_progress emits STEP_START and STEP_PROGRESS events."""
        step1 = TestStep(
            step_id="step_mock_1",
            operation_id="get_items",
            method=HTTPMethod.GET,
            path_template="/items",
            expected_status=200,
        )
        scenario = TestScenario(
            scenario_id="mock_sc",
            name="Mock Scenario",
            description="Testing progress callback",
            scenario_type=ScenarioType.HAPPY_PATH_E2E,
            test_steps=[step1],
        )

        events = []

        def on_progress(event_info):
            events.append(event_info)

        runner = TestRunner(base_url="http://mock.test.local")
        asyncio.run(
            runner.execute_scenario(scenario, on_step_progress=on_progress)
        )

        self.assertGreaterEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "STEP_START")
        self.assertEqual(events[0]["step_id"], "step_mock_1")
        self.assertEqual(events[1]["event"], "STEP_PROGRESS")
        self.assertEqual(events[1]["step_id"], "step_mock_1")

    def test_stream_scenario_and_suite_endpoints(self):
        """Verifies SSE streaming endpoints for scenario, suite, and dashboard HTML rendering."""
        test_sess_id = "test_stream_session_verification"
        mock_suite = {
            "suite_id": f"suite_{test_sess_id}",
            "target_base_url": "http://127.0.0.1:9",
            "scenarios": [
                {
                    "scenario_id": "SC-STREAM-01",
                    "name": "Stream Journey 1",
                    "description": "First mock journey for streaming verification",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_1",
                            "operation_id": "get_mock",
                            "method": "GET",
                            "path_template": "/mock1",
                            "path_params": {},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "expected_status": 200,
                        }
                    ],
                    "teardown_steps": [],
                },
                {
                    "scenario_id": "SC-STREAM-02",
                    "name": "Stream Journey 2",
                    "description": "Second mock journey for streaming verification",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_2",
                            "operation_id": "get_mock2",
                            "method": "GET",
                            "path_template": "/mock2",
                            "path_params": {},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "expected_status": 200,
                        }
                    ],
                    "teardown_steps": [],
                },
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("server.WORKSPACE_DIR", tmpdir):
                test_dir = os.path.join(tmpdir, test_sess_id, "tests")
                os.makedirs(test_dir, exist_ok=True)
                with open(
                    os.path.join(test_dir, "suite.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(mock_suite, f, indent=2)

                client = TestClient(app)

                # 1. Test Dashboard HTML rendering & UI workflow note
                dash_resp = client.get(f"/execution_dashboard/{test_sess_id}")
                self.assertEqual(dash_resp.status_code, 200)
                html = dash_resp.text
                self.assertIn("suite-progress-container", html)
                self.assertIn("suite-progress-bar", html)
                self.assertIn("progress_box_SC-STREAM-01", html)
                self.assertIn("healingPulse", html)
                self.assertIn("consumeSSE", html)
                self.assertIn("runAll()", html)
                self.assertIn("runScenario('SC-STREAM-01')", html)
                self.assertIn("ui-workflow-note", html)

                # 2. Test Stream Scenario endpoint (healing=False to avoid external LLM call)
                sc_resp = client.get(
                    f"/stream_scenario/{test_sess_id}/SC-STREAM-01?healing=false"
                )
                self.assertEqual(sc_resp.status_code, 200)
                self.assertIn(
                    "text/event-stream",
                    sc_resp.headers.get("content-type", ""),
                )

                sc_events = []
                for line in sc_resp.text.split("\n"):
                    if line.startswith("data: "):
                        sc_events.append(json.loads(line[6:]))

                event_types = [e.get("event") for e in sc_events]
                self.assertIn("STEP_START", event_types)
                self.assertIn("STEP_PROGRESS", event_types)
                self.assertIn("JOURNEY_COMPLETE", event_types)

                # 3. Test Stream Suite endpoint with parallel execution
                suite_resp = client.get(
                    f"/stream_suite/{test_sess_id}?healing=false&concurrency=2"
                )
                self.assertEqual(suite_resp.status_code, 200)
                self.assertIn(
                    "text/event-stream",
                    suite_resp.headers.get("content-type", ""),
                )

                suite_events = []
                for line in suite_resp.text.split("\n"):
                    if line.startswith("data: "):
                        suite_events.append(json.loads(line[6:]))

                suite_event_types = [e.get("event") for e in suite_events]
                self.assertIn("JOURNEY_START", suite_event_types)
                self.assertIn("STEP_START", suite_event_types)
                self.assertIn("STEP_PROGRESS", suite_event_types)
                self.assertIn("JOURNEY_COMPLETE", suite_event_types)
                self.assertIn("SUITE_COMPLETE", suite_event_types)

                # Check final SUITE_COMPLETE event
                last_event = suite_events[-1]
                self.assertEqual(last_event["event"], "SUITE_COMPLETE")
                self.assertEqual(last_event["total"], 2)

    def test_self_healing_deterministic_and_retry_pass(self):
        """Verifies self-healing diagnoses parameter and status errors, updates suite.json, and passes on retry."""
        validator = TestValidatorAgent()
        # Test status mismatch diagnosis
        mock_sc = {
            "scenario_id": "SC-HEAL-01",
            "setup_steps": [],
            "test_steps": [
                {"step_id": "step_status_mismatch", "expected_status": 404}
            ],
            "teardown_steps": [],
        }
        fix_status = asyncio.run(
            validator.heal_scenario_async(
                mock_sc,
                "Step step_status_mismatch failed: Expected HTTP 404, got 200. Response: {}",
            )
        )
        self.assertIsNotNone(fix_status)
        self.assertEqual(fix_status.fixed_expected_status, 200)

        # Test enum mismatch diagnosis
        mock_sc_enum = {
            "scenario_id": "SC-HEAL-02",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "step_enum_mismatch",
                    "path_params": {"area": "INVALID"},
                }
            ],
            "teardown_steps": [],
        }
        fix_enum = asyncio.run(
            validator.heal_scenario_async(
                mock_sc_enum,
                'Step step_enum_mismatch failed: Expected HTTP 200, got 404. Response: {"parameterErrors": [{"parameter": "path.area", "message": "Does not have a value in the enumeration [\\"a\\", \\"b\\"]"}]}',
            )
        )
        self.assertIsNotNone(fix_enum)
        self.assertEqual(fix_enum.fixed_path_params, {"area": "a"})

        # Test stream_scenario with healing=True and verify HEALING_APPLIED carries fixed_step
        test_sess_id = "test_stream_healing_verification"
        mock_suite = {
            "suite_id": f"suite_{test_sess_id}",
            "target_base_url": "http://127.0.0.1:9",
            "scenarios": [
                {
                    "scenario_id": "SC-HEAL-FLOW",
                    "name": "Heal Flow Journey",
                    "description": "Verifying healing emission",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_heal_target",
                            "operation_id": "op_heal",
                            "method": "GET",
                            "path_template": "/test/{area}",
                            "path_params": {"area": "wrong"},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "expected_status": 200,
                        }
                    ],
                    "teardown_steps": [],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("server.WORKSPACE_DIR", tmpdir):
                test_dir = os.path.join(tmpdir, test_sess_id, "tests")
                os.makedirs(test_dir, exist_ok=True)
                suite_path = os.path.join(test_dir, "suite.json")
                with open(suite_path, "w", encoding="utf-8") as f:
                    json.dump(mock_suite, f, indent=2)

                client = TestClient(app)
                resp = client.get(
                    f"/stream_scenario/{test_sess_id}/SC-HEAL-FLOW?healing=true"
                )
                self.assertEqual(resp.status_code, 200)

                events = []
                for line in resp.text.split("\n"):
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))

                ev_types = [e.get("event") for e in events]
                self.assertIn("HEALING_START", ev_types)
                complete_ev = next(
                    e for e in events if e.get("event") == "JOURNEY_COMPLETE"
                )
                self.assertIsNotNone(complete_ev)

                # Test download_suite endpoint
                dl_resp = client.get(f"/download_suite/{test_sess_id}")
                self.assertEqual(dl_resp.status_code, 200)
                self.assertIn(
                    "application/json",
                    dl_resp.headers.get("content-type", ""),
                )

                # Test download_tests endpoint (returns standalone Python PyTest script)
                py_resp = client.get(f"/download_tests/{test_sess_id}")
                self.assertEqual(py_resp.status_code, 200)
                self.assertIn(
                    "text/x-python", py_resp.headers.get("content-type", "")
                )
                self.assertIn(
                    "async def test_sc_heal_flow_heal_flow_journey():",
                    py_resp.text,
                )
                compiled = compile(py_resp.text, "<test_suite>", "exec")
                self.assertIsNotNone(compiled)

                # Test dashboard HTML contains Python download button and scoped step ID
                dash_resp = client.get(f"/execution_dashboard/{test_sess_id}")
                self.assertEqual(dash_resp.status_code, 200)
                self.assertIn(
                    "Download Python Tests (test_suite.py)", dash_resp.text
                )
                self.assertIn(
                    "step_card_SC-HEAL-FLOW_step_heal_target", dash_resp.text
                )

        # Test boolean and string case equivalence
        self.assertTrue(are_values_equivalent(False, "false"))
        self.assertTrue(are_values_equivalent(True, "true"))
        self.assertTrue(are_values_equivalent(10, "10"))
        self.assertFalse(are_values_equivalent(False, "true"))

    def test_github_folder_and_branch_naming(self):
        """Verifies tests are saved under tests/oas-tests-agent/{timestamp}/."""
        folder_path, ts = make_timestamped_folder()
        self.assertTrue(folder_path.startswith("tests/oas-tests-agent/"))
        self.assertEqual(folder_path, f"tests/oas-tests-agent/{ts}")

    def test_github_cli_push_safety_guard(self):
        """Verifies that commit_and_push_local_git raises RuntimeError on mismatched repo."""
        with self.assertRaises(RuntimeError) as ctx:
            commit_and_push_local_git(
                dest_folder="tests/oas-tests-agent/dummy_test",
                file_dict={"dummy.txt": "hello"},
                commit_message="test",
                repo_name="external-org/different-repo",
            )
        self.assertIn("does not match local git remote", str(ctx.exception))

    def test_orchestrator_does_not_sync_when_disabled_even_with_token(self):
        """Verifies that GITHUB_TOKEN presence does not force auto-sync when sync_github=False."""
        with mock.patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "ghp_fake_token_12345", "GITHUB_AUTO_SYNC": "false"},
        ):
            orch = OASTestOrchestrator(session_id="test_no_sync_sess")
            self.assertNotEqual(orch.repo_name, "your-org/your-repo")
            with (
                mock.patch.object(orch.oas_parser, "execute") as mock_parse,
                mock.patch.object(orch.test_generator, "execute") as mock_plan,
                mock.patch.object(orch.suite_scaffolder, "execute") as mock_gen,
                mock.patch.object(orch.quality_gate, "execute") as mock_rev,
                mock.patch.object(
                    orch.github_deployer, "push_session_tests"
                ) as mock_gh_push,
            ):
                mock_parse.return_value = mock.MagicMock(
                    title="MockAPI", base_url="http://test", endpoints=[]
                )
                mock_suite = mock.MagicMock(
                    target_base_url="http://test", scenarios=[]
                )
                mock_plan.return_value = mock_suite
                mock_gen.return_value = ["test.py"]
                mock_rev.return_value = ReviewResult(passed=True)

                res = orch.run(oas_path="dummy.yaml", sync_github=False)
                self.assertEqual(res["status"], "success")
                mock_gh_push.assert_not_called()
                self.assertIsNone(res["github_sync"])

    def test_commit_message_formats_with_spec_file(self):
        """Verifies commit message follows: OAS tests for {filename.yaml}: {total_steps} tests across {endpoint_count} endpoints."""
        deployer = GitHubDeployer(session_id="test_sess_commit_msg")

        with tempfile.TemporaryDirectory() as tmp_ws:
            sess_test_dir = os.path.join(
                tmp_ws, "test_sess_commit_msg", "tests"
            )
            os.makedirs(sess_test_dir, exist_ok=True)
            suite_data = {
                "metadata": {
                    "title": "Weather Service",
                    "spec_file": "custom_weather.yaml",
                },
                "target_base_url": "http://api.weather.com",
                "scenarios": [
                    {
                        "scenario_id": "SC-1",
                        "name": "S1",
                        "test_steps": [
                            {"step_id": "st-1", "path_template": "/alerts"},
                            {"step_id": "st-2", "path_template": "/stations"},
                        ],
                    }
                ],
            }
            with open(
                os.path.join(sess_test_dir, "suite.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(suite_data, f)

            with mock.patch(
                "manager.core.github_deployer.commit_and_push_local_git"
            ) as mock_git:
                mock_git.return_value = {
                    "sha": "123456",
                    "url": "http://commit/123",
                    "branch": "oas-tests-agent/test",
                }
                res = deployer.push_session_tests(
                    session_id="test_sess_commit_msg",
                    workspace_dir=tmp_ws,
                    spec_file="my_custom_api.yaml",
                )
                self.assertEqual(res["status"], "success")
                call_args = mock_git.call_args[1]
                commit_msg = call_args.get("commit_message", "")
                self.assertTrue(
                    commit_msg.startswith(
                        "OAS tests for my_custom_api.yaml: 2 tests across 2 endpoints"
                    )
                )

    def test_codegen_boolean_and_null_literals_pass_linter(self):
        """Verifies generate_python_test_suite emits valid Python True/False/None instead of JSON true/false/null."""
        suite_data = {
            "suite_id": "test_bool_suite",
            "target_base_url": "http://localhost:8080",
            "scenarios": [
                {
                    "scenario_id": "sc_bool",
                    "name": "Boolean Query and Null Body Test",
                    "description": "Tests boolean query params and None body fields.",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_bool_query",
                            "operation_id": "query_items",
                            "method": "POST",
                            "path_template": "/items",
                            "path_params": {},
                            "query_params": {"active": True, "require_qc": False},
                            "headers": {},
                            "request_body": {"enabled": True, "optional_field": None},
                            "captures": [
                                {"variable_name": "item_id", "json_path": "$.servers[0].id"}
                            ],
                            "expected_status": 200,
                            "semantic_assertions": [
                                {
                                    "target_expression": "$.@context",
                                    "operator": "NOT_NULL",
                                    "expected_value": None,
                                }
                            ],
                            "timeout_seconds": 10.0,
                        }
                    ],
                    "teardown_steps": [],
                }
            ],
        }
        code = generate_python_test_suite(suite_data)
        self.assertIn("'active': True", code)
        self.assertIn("'require_qc': False", code)
        self.assertIn("'optional_field': None", code)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp_f:
            tmp_f.write(code)
            tmp_path = tmp_f.name

        try:
            passed, errs = run_linter(tmp_path)
            self.assertTrue(passed, f"Expected generated code to pass linter, got: {errs}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_extract_jsonpath_collection_unwrap_and_uri_tail(self):
        """Verifies extract_jsonpath unwraps @graph on arbitrary collection names and strips URI prefixes on id."""
        payload = {
            "@context": {"@version": "1.1"},
            "@graph": [
                {
                    "@id": "https://api.weather.gov/radar/servers/ldm1",
                    "id": "https://api.weather.gov/radar/servers/ldm1",
                }
            ],
        }
        self.assertEqual(
            VariableResolver.extract_jsonpath(payload, "$.servers[0].id"),
            "ldm1",
        )
        self.assertEqual(
            VariableResolver.extract_jsonpath(payload, "$.@graph[0].id"),
            "ldm1",
        )

    def test_sanitize_generated_scenarios_filters_collection_404_and_enforces_2xx(self):
        """Verifies post-generation sanitizer drops collection 404 negative steps and resets 4XX in HAPPY_PATH_E2E."""
        cluster_ops = [
            {
                "operation_id": "product",
                "method": "GET",
                "path": "/products/{productId}",
                "summary": "Returns a specific text product",
                "success_status": 200,
                "error_statuses": [],
            },
            {
                "operation_id": "products_type",
                "method": "GET",
                "path": "/products/types/{typeId}",
                "summary": "Returns a list of text products of a given type",
                "success_status": 200,
                "error_statuses": [],
            },
        ]
        happy_sc = TestScenario(
            scenario_id="happy_1",
            name="Happy Flow",
            description="Desc",
            scenario_type=ScenarioType.HAPPY_PATH_E2E,
            test_steps=[
                TestStep(
                    step_id="s1",
                    operation_id="product",
                    method=HTTPMethod.GET,
                    path_template="/products/{productId}",
                    expected_status=404,
                )
            ],
        )
        neg_sc = TestScenario(
            scenario_id="neg_1",
            name="Negative Flow",
            description="Desc",
            scenario_type=ScenarioType.BOUNDARY_NEGATIVE,
            test_steps=[
                TestStep(
                    step_id="n1",
                    operation_id="product",
                    method=HTTPMethod.GET,
                    path_template="/products/{productId}",
                    path_params={"productId": "NONEXISTENT"},
                    expected_status=404,
                ),
                TestStep(
                    step_id="n2",
                    operation_id="products_type",
                    method=HTTPMethod.GET,
                    path_template="/products/types/{typeId}",
                    path_params={"typeId": "INVALID"},
                    expected_status=404,
                ),
            ],
        )
        sanitized = TestGeneratorAgent._sanitize_generated_scenarios(
            [happy_sc, neg_sc], cluster_ops
        )
        self.assertEqual(sanitized[0].test_steps[0].expected_status, 200)
        self.assertEqual(len(sanitized[1].test_steps), 1)
        self.assertEqual(sanitized[1].test_steps[0].step_id, "n1")

    def test_mock_crud_server_expanded_endpoints_and_oas_alignment(self):
        """Verifies the expanded mock CRUD server endpoints, state transitions, and OpenAPI spec alignment."""
        from manager.core.oas_parser import OASParser
        import mock_crud_server.app as crud_app

        spec_path = os.path.join(REPO_ROOT, "sample_oas", "ecommerce.yaml")
        parser = OASParser(session_id="test_crud_oas")
        oas_meta = parser.execute(spec_path)
        self.assertEqual(len(oas_meta.endpoints), 32)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
            tmp_db_path = tmp_db.name

        old_db_path = crud_app.DB_PATH
        try:
            crud_app.DB_PATH = tmp_db_path
            crud_app.init_db(reset=True)
            client = TestClient(crud_app.app)

            # 1. Products & Restock + Price filter validation
            bad_filter = client.get("/v1/products?min_price=500&max_price=10")
            self.assertEqual(bad_filter.status_code, 400)

            prod_resp = client.post(
                "/v1/products",
                json={
                    "name": "UltraWide Studio Monitor",
                    "price": 499.99,
                    "category": "electronics",
                    "in_stock": True,
                    "stock_quantity": 10,
                    "sku": "SKU-MON-500",
                    "tags": ["monitor", "display"],
                },
            )
            self.assertEqual(prod_resp.status_code, 201)
            prod_id = prod_resp.json()["id"]

            restock_resp = client.post(
                f"/v1/products/{prod_id}/restock",
                json={"quantity": 15, "warehouse_code": "WH-US-WEST"},
            )
            self.assertEqual(restock_resp.status_code, 200)
            self.assertEqual(restock_resp.json()["stock_quantity"], 25)

            # 2. Reviews sub-resource & rating aggregation
            rev_resp = client.post(
                f"/v1/products/{prod_id}/reviews",
                json={
                    "reviewer_email": "alex.shopper@example.com",
                    "rating": 4,
                    "title": "Crisp 4K panel",
                    "comment": "Colors are accurate right out of the box.",
                    "verified_purchase": True,
                },
            )
            self.assertEqual(rev_resp.status_code, 201)
            rev_id = rev_resp.json()["id"]

            rev_list = client.get(f"/v1/products/{prod_id}/reviews?min_rating=4")
            self.assertEqual(rev_list.status_code, 200)
            self.assertEqual(rev_list.json()["total"], 1)
            self.assertEqual(rev_list.json()["average_rating"], 4.0)

            rev_patch = client.patch(
                f"/v1/reviews/{rev_id}",
                json={"rating": 5, "title": "Updated: 5 stars"},
            )
            self.assertEqual(rev_patch.status_code, 200)
            self.assertEqual(rev_patch.json()["rating"], 5)

            # 3. Customers CRUD & Customer Orders sub-resource
            cust_resp = client.post(
                "/v1/customers",
                json={
                    "email": "jamie.dev@example.com",
                    "full_name": "Jamie Developer",
                    "tier": "PLATINUM",
                    "loyalty_points": 1200,
                    "active": True,
                    "shipping_address": {
                        "street": "500 Tech Way",
                        "city": "Seattle",
                        "state": "WA",
                        "postal_code": "98101",
                        "country": "US",
                    },
                },
            )
            self.assertEqual(cust_resp.status_code, 201)
            cust_id = cust_resp.json()["id"]

            # 4. Promotions CRUD & percentage cap validation (> 90% rejected)
            bad_promo = client.post(
                "/v1/promotions",
                json={
                    "code": "TOO_HIGH",
                    "discount_type": "PERCENTAGE",
                    "discount_value": 95.0,
                },
            )
            self.assertEqual(bad_promo.status_code, 400)

            promo_resp = client.post(
                "/v1/promotions",
                json={
                    "code": "VIP25",
                    "description": "25% VIP discount",
                    "discount_type": "PERCENTAGE",
                    "discount_value": 25.0,
                    "min_order_amount": 100.0,
                    "active": True,
                },
            )
            self.assertEqual(promo_resp.status_code, 201)

            # 5. Orders + Apply Promotion + Cancel & Restock + State Transition Guard
            order_resp = client.post(
                "/v1/orders",
                json={
                    "customer_email": "jamie.dev@example.com",
                    "customer_id": cust_id,
                    "product_id": prod_id,
                    "quantity": 2,
                    "notes": "Deliver to reception",
                },
            )
            self.assertEqual(order_resp.status_code, 201)
            order_id = order_resp.json()["id"]
            self.assertAlmostEqual(order_resp.json()["total_price"], 999.98, places=2)

            cust_orders = client.get(f"/v1/customers/{cust_id}/orders")
            self.assertEqual(cust_orders.status_code, 200)
            self.assertEqual(cust_orders.json()["total"], 1)

            apply_resp = client.post(
                f"/v1/orders/{order_id}/apply-promotion",
                json={"promo_code": "VIP25"},
            )
            self.assertEqual(apply_resp.status_code, 200)
            self.assertEqual(apply_resp.json()["promo_code"], "VIP25")
            self.assertAlmostEqual(apply_resp.json()["discount_amount"], 250.0, places=2)
            self.assertAlmostEqual(apply_resp.json()["total_price"], 749.98, places=2)

            cancel_resp = client.post(
                f"/v1/orders/{order_id}/cancel",
                json={"reason": "Ordered wrong model", "restock_items": True},
            )
            self.assertEqual(cancel_resp.status_code, 200)
            self.assertEqual(cancel_resp.json()["status"], "CANCELLED")

            # Verify illegal transition from CANCELLED -> DELIVERED is blocked with 400
            illegal_patch = client.patch(
                f"/v1/orders/{order_id}",
                json={"status": "DELIVERED"},
            )
            self.assertEqual(illegal_patch.status_code, 400)

            # Verify deleting a newly created product removes it (404 afterwards),
            # whereas deleting a shared seed product preserves the seed record for concurrent journeys.
            del_created = client.delete(f"/v1/products/{prod_id}")
            self.assertEqual(del_created.status_code, 204)
            get_deleted = client.get(f"/v1/products/{prod_id}")
            self.assertEqual(get_deleted.status_code, 404)

            seed_id = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
            del_seed = client.delete(f"/v1/products/{seed_id}")
            self.assertEqual(del_seed.status_code, 204)
            get_seed = client.get(f"/v1/products/{seed_id}")
            self.assertEqual(get_seed.status_code, 200)
        finally:
            crud_app.DB_PATH = old_db_path
            if os.path.exists(tmp_db_path):
                os.remove(tmp_db_path)

    def test_step_pacing_404_retry_and_delete_last_ordering(self):
        """Verifies inter-step pacing, read-after-write 404 retry, and DELETE/cancel last ordering."""
        ops = [
            {
                "operation_id": "delete_product",
                "method": "DELETE",
                "path": "/v1/products/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True}],
                "success_status": 204,
            },
            {
                "operation_id": "restock_product",
                "method": "POST",
                "path": "/v1/products/{id}/restock",
                "parameters": [{"name": "id", "in": "path", "required": True}],
                "success_status": 200,
            },
            {
                "operation_id": "create_product",
                "method": "POST",
                "path": "/v1/products",
                "parameters": [],
                "success_status": 201,
            },
            {
                "operation_id": "get_product_by_id",
                "method": "GET",
                "path": "/v1/products/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True}],
                "success_status": 200,
            },
        ]
        fallback_scenarios = build_deterministic_fallback_scenarios("products", ops)
        happy_steps = fallback_scenarios[0].test_steps
        self.assertEqual(
            [s.operation_id for s in happy_steps],
            [
                "create_product",
                "get_product_by_id",
                "restock_product",
                "delete_product",
            ],
        )
        # Verify root POST captured ID is chained into downstream steps
        self.assertEqual(happy_steps[1].path_params["id"], "{{products_id}}")
        self.assertEqual(happy_steps[2].path_params["id"], "{{products_id}}")
        self.assertEqual(happy_steps[3].path_params["id"], "{{products_id}}")

        # Verify _sanitize_generated_scenarios moves DELETE and /cancel to the end
        unsorted_scenario = TestScenario(
            scenario_id="order_e2e",
            name="Order Lifecycle",
            description="Desc",
            scenario_type=ScenarioType.HAPPY_PATH_E2E,
            test_steps=[
                TestStep(
                    step_id="s1",
                    operation_id="create_order",
                    method=HTTPMethod.POST,
                    path_template="/v1/orders",
                    expected_status=201,
                ),
                TestStep(
                    step_id="s2",
                    operation_id="delete_order",
                    method=HTTPMethod.DELETE,
                    path_template="/v1/orders/{id}",
                    expected_status=204,
                ),
                TestStep(
                    step_id="s3",
                    operation_id="cancel_order",
                    method=HTTPMethod.POST,
                    path_template="/v1/orders/{id}/cancel",
                    expected_status=200,
                ),
                TestStep(
                    step_id="s4",
                    operation_id="apply_order_promotion",
                    method=HTTPMethod.POST,
                    path_template="/v1/orders/{id}/apply-promotion",
                    expected_status=200,
                ),
            ],
        )
        sanitized = TestGeneratorAgent._sanitize_generated_scenarios(
            [unsorted_scenario], []
        )
        self.assertEqual(
            [s.operation_id for s in sanitized[0].test_steps],
            [
                "create_order",
                "apply_order_promotion",
                "cancel_order",
                "delete_order",
            ],
        )

        # Verify read-after-write 404 retry in TestRunner when context is populated
        call_count = 0

        class FlakyReadResponse:
            def __init__(self, status_code: int, body: dict):
                self.status_code = status_code
                self._body = body
                self.text = json.dumps(body)
                self.headers = {"content-type": "application/json"}

            def json(self):
                return self._body

        async def fake_request(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FlakyReadResponse(404, {"detail": "Not yet visible"})
            return FlakyReadResponse(200, {"id": "prod-1", "name": "Headphones"})

        runner = TestRunner(
            base_url="http://localhost:8090", step_delay_seconds=0.01
        )
        runner.context["products_id"] = "prod-1"
        step = TestStep(
            step_id="read_after_write",
            operation_id="get_product_by_id",
            method=HTTPMethod.GET,
            path_template="/v1/products/{id}",
            path_params={"id": "{{products_id}}"},
            expected_status=200,
        )
        mock_client = mock.MagicMock()
        mock_client.request = fake_request
        res = asyncio.run(runner._execute_step(mock_client, step))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(call_count, 2)

    def test_github_create_pr_modes_and_token_persistence(self):
        """Verifies create_pr=True opens an API PR when token/gh is available or returns a pre-filled PR compare URL on Git SSH."""
        deployer = GitHubDeployer(session_id="test_sess_pr_modes")

        with tempfile.TemporaryDirectory() as tmp_ws:
            sess_test_dir = os.path.join(tmp_ws, "test_sess_pr_modes", "tests")
            os.makedirs(sess_test_dir, exist_ok=True)
            suite_data = {
                "metadata": {
                    "title": "Store Inventory API",
                    "spec_file": "ecommerce.yaml",
                },
                "target_base_url": "http://localhost:8090",
                "scenarios": [
                    {
                        "scenario_id": "SC-PR-1",
                        "name": "Products CRUD",
                        "test_steps": [
                            {"step_id": "st-1", "path_template": "/v1/products"},
                        ],
                    }
                ],
            }
            with open(
                os.path.join(sess_test_dir, "suite.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(suite_data, f)

            # 1. Local CLI mode (require_token=False) without token -> returns type="pr_compare" with ?expand=1 pre-filled PR URL
            with (
                mock.patch(
                    "manager.core.github_deployer.commit_and_push_local_git"
                ) as mock_git,
                mock.patch(
                    "manager.core.github_deployer.open_pr_via_gh_cli",
                    return_value="",
                ),
            ):
                mock_git.return_value = {
                    "sha": "ceb29a065c38",
                    "url": "https://github.com/apigee-agentic-offering/oas-spec-test/commit/ceb29a065c38",
                    "branch_url": "https://github.com/apigee-agentic-offering/oas-spec-test/tree/oas-tests-agent/2026-10-03_09-14-30",
                    "branch": "oas-tests-agent/2026-10-03_09-14-30",
                    "folder": "tests/oas-tests-agent/2026-10-03_09-14-30",
                    "repo": "apigee-agentic-offering/oas-spec-test",
                }
                res_ssh = deployer.push_session_tests(
                    session_id="test_sess_pr_modes",
                    workspace_dir=tmp_ws,
                    branch="oas-tests-agent/2026-10-03_09-14-30",
                    create_pr=True,
                )
                self.assertEqual(res_ssh["status"], "success")
                self.assertEqual(res_ssh["type"], "pr_compare")
                self.assertFalse(res_ssh["pr_created"])
                self.assertIn(
                    f"https://github.com/{deployer.repo_name}/compare/main...oas-tests-agent/2026-10-03_09-14-30?expand=1",
                    res_ssh["url"],
                )
                self.assertEqual(
                    res_ssh["commit_url"],
                    "https://github.com/apigee-agentic-offering/oas-spec-test/commit/ceb29a065c38",
                )

            # 2. PyGithub API push + token available -> calls open_pull_request and returns type="pr"
            mock_repo = mock.MagicMock()
            mock_repo.default_branch = "main"
            mock_gh_cls = mock.MagicMock()
            mock_gh_cls.return_value.get_repo.return_value = mock_repo
            with (
                mock.patch(
                    "manager.core.github_deployer.Github", mock_gh_cls
                ),
                mock.patch(
                    "manager.core.github_deployer.create_branch",
                    return_value=True,
                ),
                mock.patch(
                    "manager.core.github_deployer.commit_files",
                    return_value={
                        "sha": "ceb29a065c38",
                        "url": "https://github.com/apigee-agentic-offering/oas-spec-test/commit/ceb29a065c38",
                    },
                ),
                mock.patch(
                    "manager.core.github_deployer.open_pull_request",
                    return_value="https://github.com/apigee-agentic-offering/oas-spec-test/pull/42",
                ) as mock_open_pr,
            ):
                res_pr = deployer.push_session_tests(
                    session_id="test_sess_pr_modes",
                    workspace_dir=tmp_ws,
                    branch="oas-tests-agent/2026-10-03_09-14-30",
                    create_pr=True,
                    token="ghp_test_token_val",
                    require_token=True,
                )
                self.assertEqual(res_pr["status"], "success")
                self.assertEqual(res_pr["type"], "pr")
                self.assertTrue(res_pr["pr_created"])
                self.assertEqual(
                    res_pr["url"],
                    "https://github.com/apigee-agentic-offering/oas-spec-test/pull/42",
                )
                mock_open_pr.assert_called_once()

            # 3. Cloud Run mode (require_token=True): never invokes local git CLI even when PyGithub raises an error
            with mock.patch(
                "manager.core.github_deployer.commit_and_push_local_git"
            ) as mock_git_cli:
                res_no_token = deployer.push_session_tests(
                    session_id="test_sess_pr_modes",
                    workspace_dir=tmp_ws,
                    branch="oas-tests-agent/2026-10-03_09-14-30",
                    token="",
                    require_token=True,
                )
                self.assertEqual(res_no_token["status"], "error")
                self.assertIn("is required", res_no_token["message"])
                mock_git_cli.assert_not_called()

                from github import GithubException

                mock_gh_err_cls = mock.MagicMock()
                mock_gh_err_cls.return_value.get_repo.side_effect = GithubException(
                    403, {"message": "Resource not accessible by personal access token"}, None
                )
                with mock.patch(
                    "manager.core.github_deployer.Github", mock_gh_err_cls
                ):
                    res_api_err = deployer.push_session_tests(
                        session_id="test_sess_pr_modes",
                        workspace_dir=tmp_ws,
                        branch="oas-tests-agent/2026-10-03_09-14-30",
                        token="github_pat_readonly",
                        require_token=True,
                    )
                    self.assertEqual(res_api_err["status"], "error")
                    self.assertIn("403 Forbidden", res_api_err["message"])
                    self.assertNotIn("No such file or directory: 'git'", res_api_err["message"])
                    mock_git_cli.assert_not_called()

    def test_per_session_token_isolation_and_ui_sync_toggle_removed(self):
        """Verifies tokens are isolated per session_id in memory, never written to disk, and Sync with GitHub toggle is removed."""
        from fastapi.testclient import TestClient
        import server

        client = TestClient(server.app)

        # 1. Home page should not contain Sync with GitHub toggle
        home_res = client.get("/")
        self.assertEqual(home_res.status_code, 200)
        self.assertNotIn("syncGithubOnGenerate", home_res.text)
        self.assertNotIn("Sync with GitHub", home_res.text)

        # 2. Token isolation between Session A and Session B
        server.session_github_tokens.clear()
        with tempfile.TemporaryDirectory() as tmp_ws:
            for sid in ("session_user_a", "session_user_b"):
                tdir = os.path.join(tmp_ws, sid, "tests")
                os.makedirs(tdir, exist_ok=True)
                with open(os.path.join(tdir, "suite.json"), "w", encoding="utf-8") as f:
                    json.dump({"metadata": {"title": "API"}, "scenarios": []}, f)

            with mock.patch.object(server, "WORKSPACE_DIR", tmp_ws):
                # Missing token -> HTTP 400
                missing_res = client.post(
                    "/github/push/session_user_a",
                    json={"repo": "org/repo", "branch": "main", "token": ""},
                )
                self.assertEqual(missing_res.status_code, 400)
                self.assertIn("is required", missing_res.json()["message"])

                # Push with token in session_user_a
                with mock.patch(
                    "manager.core.github_deployer.GitHubDeployer.push_session_tests",
                    return_value={
                        "status": "success",
                        "type": "commit",
                        "url": "https://github.com/org/repo/commit/123",
                        "branch": "main",
                        "folder": "tests/oas-tests-agent/ts",
                    },
                ):
                    ok_res = client.post(
                        "/github/push/session_user_a",
                        json={
                            "repo": "org/repo",
                            "branch": "main",
                            "token": "ghp_secret_user_a",
                        },
                    )
                    self.assertEqual(ok_res.status_code, 200)

                # Session A has token in memory; Session B does NOT
                cfg_a = client.get("/github/config?session_id=session_user_a").json()
                cfg_b = client.get("/github/config?session_id=session_user_b").json()
                self.assertTrue(cfg_a["has_token"])
                self.assertFalse(cfg_b["has_token"])

                # Disk config file must NEVER contain any token
                cfg_file = os.path.join(tmp_ws, "github_config.json")
                if os.path.exists(cfg_file):
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        disk_cfg = json.load(f)
                    self.assertNotIn("token", disk_cfg)

    def test_context_chaining_get_collection_subresources_and_request_body(self):
        """Verifies context variable capture & propagation across GET collections, camelCase/snake_case path params, sub-resources, and request_body foreign keys."""
        # 1. GET /products -> GET /products/{product_id} & GET /products/{productId} (collection GET without POST)
        product_ops = [
            {
                "operation_id": "get_product_camel",
                "method": "GET",
                "path": "/products/{productId}",
                "parameters": [
                    {"name": "productId", "in": "path", "required": True, "type": "string"}
                ],
                "success_status": 200,
            },
            {
                "operation_id": "list_products",
                "method": "GET",
                "path": "/products",
                "parameters": [],
                "success_status": 200,
            },
            {
                "operation_id": "get_product_snake",
                "method": "GET",
                "path": "/products/{product_id}",
                "parameters": [
                    {"name": "product_id", "in": "path", "required": True, "type": "string"}
                ],
                "success_status": 200,
            },
        ]
        prod_scenarios = build_deterministic_fallback_scenarios("products", product_ops)
        prod_steps = prod_scenarios[0].test_steps
        self.assertEqual(prod_steps[0].operation_id, "list_products")
        self.assertTrue(
            any(c.variable_name == "products_id" for c in prod_steps[0].captures)
        )
        self.assertEqual(prod_steps[1].path_params.get("productId"), "{{products_id}}")
        self.assertEqual(prod_steps[2].path_params.get("product_id"), "{{products_id}}")

        # Verify top-level JSON array response works with $.items[0].id
        self.assertEqual(
            VariableResolver.extract_jsonpath([{"id": "prod-top-level-1"}], "$.items[0].id"),
            "prod-top-level-1",
        )

        # 2. Sub-resource cluster: /v1/products/{id}/reviews + /v1/reviews/{id}
        review_ops = [
            {
                "operation_id": "get_review_by_id",
                "method": "GET",
                "path": "/v1/reviews/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "success_status": 200,
            },
            {
                "operation_id": "delete_review",
                "method": "DELETE",
                "path": "/v1/reviews/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "success_status": 204,
            },
            {
                "operation_id": "patch_review",
                "method": "PATCH",
                "path": "/v1/reviews/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "request_body": {"properties": {"rating": {"type": "integer"}}},
                "success_status": 200,
            },
            {
                "operation_id": "create_product_review",
                "method": "POST",
                "path": "/v1/products/{id}/reviews",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "type": "string",
                        "example": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                    }
                ],
                "request_body": {
                    "properties": {
                        "reviewer_email": {"type": "string", "format": "email"},
                        "rating": {"type": "integer"},
                    }
                },
                "response_properties": ["id", "product_id"],
                "success_status": 201,
            },
            {
                "operation_id": "list_product_reviews",
                "method": "GET",
                "path": "/v1/products/{id}/reviews",
                "parameters": [
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "type": "string",
                        "example": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                    }
                ],
                "response_properties": ["items[].id", "items[].product_id"],
                "success_status": 200,
            },
        ]
        rev_scenarios = build_deterministic_fallback_scenarios("reviews", review_ops)
        rev_steps = rev_scenarios[0].test_steps
        self.assertEqual(
            [s.operation_id for s in rev_steps],
            [
                "list_product_reviews",
                "create_product_review",
                "get_review_by_id",
                "patch_review",
                "delete_review",
            ],
        )
        # list_product_reviews uses seed product ID and captures product_id; create_product_review chains {{product_id}}
        self.assertEqual(
            rev_steps[0].path_params["id"],
            "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        )
        self.assertEqual(rev_steps[1].path_params["id"], "{{product_id}}")
        # Downstream /v1/reviews/{id} steps receive {{reviews_id}} (not {{product_id}})
        self.assertEqual(rev_steps[2].path_params["id"], "{{reviews_id}}")
        self.assertEqual(rev_steps[3].path_params["id"], "{{reviews_id}}")
        self.assertEqual(rev_steps[4].path_params["id"], "{{reviews_id}}")

        # 3. Foreign-key propagation in request_body payloads (orders cluster)
        order_ops = [
            {
                "operation_id": "list_orders",
                "method": "GET",
                "path": "/v1/orders",
                "parameters": [],
                "success_status": 200,
            },
            {
                "operation_id": "create_order",
                "method": "POST",
                "path": "/v1/orders",
                "parameters": [],
                "request_body": {
                    "required": ["customer_email", "product_id", "quantity"],
                    "properties": {
                        "customer_email": {"type": "string", "format": "email"},
                        "customer_id": {"type": "string", "format": "uuid"},
                        "product_id": {"type": "string", "format": "uuid"},
                        "quantity": {"type": "integer"},
                    },
                },
                "success_status": 201,
            },
            {
                "operation_id": "replace_order",
                "method": "PUT",
                "path": "/v1/orders/{id}",
                "parameters": [{"name": "id", "in": "path", "required": True, "type": "string"}],
                "request_body": {
                    "required": ["customer_email", "product_id", "quantity"],
                    "properties": {
                        "customer_email": {"type": "string", "format": "email"},
                        "customer_id": {"type": "string", "format": "uuid"},
                        "product_id": {"type": "string", "format": "uuid"},
                        "quantity": {"type": "integer"},
                    },
                },
                "success_status": 200,
            },
        ]
        order_scenarios = build_deterministic_fallback_scenarios("orders", order_ops)
        order_steps = order_scenarios[0].test_steps
        self.assertEqual(order_steps[0].operation_id, "list_orders")
        captured_vars_on_list = {c.variable_name for c in order_steps[0].captures}
        self.assertIn("orders_id", captured_vars_on_list)
        self.assertIn("customer_id", captured_vars_on_list)
        self.assertIn("product_id", captured_vars_on_list)
        self.assertEqual(order_steps[1].request_body["customer_id"], "{{customer_id}}")
        self.assertEqual(order_steps[1].request_body["product_id"], "{{product_id}}")
        self.assertEqual(order_steps[2].path_params["id"], "{{orders_id}}")
        self.assertEqual(order_steps[2].request_body["customer_id"], "{{customer_id}}")
        self.assertEqual(order_steps[2].request_body["product_id"], "{{product_id}}")

        # 4. _sanitize_generated_scenarios chains context variables when LLM leaves hardcoded literals
        llm_unchained = TestScenario(
            scenario_id="llm_unchained_1",
            name="LLM Unchained Products & Orders",
            description="Verifies sanitizer injects captures and replaces hardcoded path/body IDs",
            scenario_type=ScenarioType.HAPPY_PATH_E2E,
            test_steps=[
                TestStep(
                    step_id="s1",
                    operation_id="list_orders",
                    method=HTTPMethod.GET,
                    path_template="/v1/orders",
                    captures=[],
                    expected_status=200,
                ),
                TestStep(
                    step_id="s2",
                    operation_id="create_order",
                    method=HTTPMethod.POST,
                    path_template="/v1/orders",
                    request_body={
                        "customer_email": "buyer@example.com",
                        "customer_id": "c1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c",
                        "product_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                        "quantity": 1,
                    },
                    captures=[],
                    expected_status=201,
                ),
                TestStep(
                    step_id="s3",
                    operation_id="replace_order",
                    method=HTTPMethod.PUT,
                    path_template="/v1/orders/{id}",
                    path_params={"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"},
                    request_body={
                        "customer_email": "buyer@example.com",
                        "customer_id": "c1a2b3c4-d5e6-4f7a-8b9c-0d1e2f3a4b5c",
                        "product_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                        "quantity": 2,
                    },
                    expected_status=200,
                ),
            ],
        )
        sanitized_llm = TestGeneratorAgent._sanitize_generated_scenarios(
            [llm_unchained], order_ops
        )[0]
        self.assertTrue(len(sanitized_llm.test_steps[0].captures) > 0)
        self.assertEqual(
            sanitized_llm.test_steps[1].request_body["customer_id"], "{{customer_id}}"
        )
        self.assertEqual(
            sanitized_llm.test_steps[1].request_body["product_id"], "{{product_id}}"
        )
        self.assertIn(
            sanitized_llm.test_steps[2].path_params["id"],
            ("{{order_id}}", "{{orders_id}}"),
        )
        self.assertEqual(
            sanitized_llm.test_steps[2].request_body["customer_id"], "{{customer_id}}"
        )
        self.assertEqual(
            sanitized_llm.test_steps[2].request_body["product_id"], "{{product_id}}"
        )

        # 5. _sanitize_generated_scenarios populates missing/null request_body on HAPPY_PATH_E2E POST/PUT steps
        llm_null_body = TestScenario(
            scenario_id="llm_null_body_1",
            name="Vertex AI Null Request Body Recovery",
            description="Verifies sanitizer synthesizes request_body when LLM outputs None",
            scenario_type=ScenarioType.HAPPY_PATH_E2E,
            test_steps=[
                TestStep(
                    step_id="s1",
                    operation_id="list_orders",
                    method=HTTPMethod.GET,
                    path_template="/v1/orders",
                    captures=[],
                    expected_status=200,
                ),
                TestStep(
                    step_id="s2",
                    operation_id="create_order",
                    method=HTTPMethod.POST,
                    path_template="/v1/orders",
                    request_body=None,
                    captures=[],
                    expected_status=201,
                ),
            ],
        )
        sanitized_null_body = TestGeneratorAgent._sanitize_generated_scenarios(
            [llm_null_body], order_ops
        )[0]
        self.assertIsNotNone(sanitized_null_body.test_steps[1].request_body)
        self.assertIn("customer_email", sanitized_null_body.test_steps[1].request_body)
        self.assertEqual(
            sanitized_null_body.test_steps[1].request_body["customer_id"], "{{customer_id}}"
        )
        self.assertEqual(
            sanitized_null_body.test_steps[1].request_body["product_id"], "{{product_id}}"
        )

    def test_validator_never_weakens_happy_path_to_400_or_clears_assertions_on_validation_error(self):
        """Verifies TestValidatorAgent repairs missing request_body from OpenAPI schema and never weakens HAPPY_PATH_E2E status to 400 or clears semantic assertions on 400 errors."""
        from manager.agents.test_validator_agent import TestFix, apply_test_fix_to_step

        crud_spec_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sample_oas",
            "ecommerce.yaml",
        )
        validator = TestValidatorAgent()

        # Case 1: HAPPY_PATH_E2E POST /v1/products fails with 400 Validation error: body: Field required
        failed_sc = {
            "scenario_id": "sc_products_happy",
            "name": "Products Happy Path",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "step_create_product",
                    "operation_id": "create_product",
                    "method": "POST",
                    "path_template": "/v1/products",
                    "path_params": {},
                    "query_params": {},
                    "headers": {},
                    "request_body": None,
                    "captures": [{"variable_name": "product_id", "json_path": "$.id"}],
                    "expected_status": 201,
                    "semantic_assertions": [
                        {"json_path": "$.id", "condition": "NOT_NULL"}
                    ],
                }
            ],
            "teardown_steps": [],
        }
        err_400 = (
            'Step step_create_product failed: Expected HTTP 201, got 400. '
            'Body: {"error": "BAD_REQUEST", "message": "Validation error: body: Field required", '
            '"status_code": 400, "details": [{"loc": ["body"], "msg": "Field required", "type": "missing"}]}'
        )
        fix1 = validator.diagnose_and_heal_deterministic(
            failed_sc, err_400, spec_path=crud_spec_path
        )
        self.assertIsNotNone(fix1)
        self.assertIsNotNone(fix1.fixed_request_body)
        self.assertIn("name", fix1.fixed_request_body)
        self.assertIn("category", fix1.fixed_request_body)
        self.assertIn("price", fix1.fixed_request_body)
        self.assertIsNone(fix1.fixed_expected_status)
        self.assertFalse(bool(fix1.clear_semantic_assertions))

        # Case 2: Step previously corrupted to expected_status=400 fails semantic_assertion on 400 BAD_REQUEST body
        corrupted_sc = {
            "scenario_id": "sc_products_corrupted",
            "name": "Products Happy Path Corrupted",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "step_create_product",
                    "operation_id": "create_product",
                    "method": "POST",
                    "path_template": "/v1/products",
                    "path_params": {},
                    "query_params": {},
                    "headers": {},
                    "request_body": None,
                    "captures": [{"variable_name": "product_id", "json_path": "$.id"}],
                    "expected_status": 400,
                    "semantic_assertions": [
                        {"json_path": "$.id", "condition": "NOT_NULL"}
                    ],
                }
            ],
            "teardown_steps": [],
        }
        err_sem_on_400 = (
            'Step step_create_product failed: Step step_create_product semantic assertion failed: '
            'Expected $.id to be NOT NULL | Response: {"error": "BAD_REQUEST", '
            '"message": "Validation error: body: Field required", "status_code": 400}'
        )
        fix2 = validator.diagnose_and_heal_deterministic(
            corrupted_sc, err_sem_on_400, spec_path=crud_spec_path
        )
        self.assertIsNotNone(fix2)
        self.assertIsNotNone(fix2.fixed_request_body)
        self.assertEqual(fix2.fixed_expected_status, 201)
        self.assertFalse(bool(fix2.clear_semantic_assertions))

        step_to_patch = corrupted_sc["test_steps"][0]
        apply_test_fix_to_step(step_to_patch, fix2, scenario_type="HAPPY_PATH_E2E")
        self.assertEqual(step_to_patch["expected_status"], 201)
        self.assertIsNotNone(step_to_patch["request_body"])
        self.assertEqual(len(step_to_patch["semantic_assertions"]), 1)

        # Case 3: apply_test_fix_to_step refuses to weaken HAPPY_PATH_E2E expected_status to 400
        bad_fix = TestFix(
            reasoning="Bad status weakening",
            fixed_step_id="step_create_product",
            fixed_expected_status=400,
        )
        apply_test_fix_to_step(step_to_patch, bad_fix, scenario_type="HAPPY_PATH_E2E")
        self.assertEqual(step_to_patch["expected_status"], 201)

    def test_download_all_vs_successful_tests_and_suite(self):
        """Verifies /download_tests and /download_suite support 'all' and 'successful' scopes."""
        import server as srv

        sess_id = "test_dl_scope_sess"
        suite_data = {
            "suite_id": "suite_dl_test",
            "metadata": {"title": "Sample API"},
            "target_base_url": "http://localhost:8080",
            "scenarios": [
                {
                    "scenario_id": "sc_passed",
                    "name": "Passed Journey",
                    "description": "Journey with passing and failing steps",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_ok_1",
                            "operation_id": "get_items",
                            "method": "GET",
                            "path_template": "/v1/items",
                            "path_params": {},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "captures": [],
                            "expected_status": 200,
                            "semantic_assertions": [],
                        },
                        {
                            "step_id": "step_fail_2",
                            "operation_id": "get_broken",
                            "method": "GET",
                            "path_template": "/v1/broken",
                            "path_params": {},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "captures": [],
                            "expected_status": 200,
                            "semantic_assertions": [],
                        },
                    ],
                    "teardown_steps": [],
                },
                {
                    "scenario_id": "sc_unrun",
                    "name": "Unrun Journey",
                    "description": "Never executed",
                    "scenario_type": "BOUNDARY_NEGATIVE",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_unrun_1",
                            "operation_id": "delete_item",
                            "method": "DELETE",
                            "path_template": "/v1/items/999",
                            "path_params": {},
                            "query_params": {},
                            "headers": {},
                            "request_body": None,
                            "captures": [],
                            "expected_status": 404,
                            "semantic_assertions": [],
                        }
                    ],
                    "teardown_steps": [],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_ws:
            with mock.patch.object(srv, "WORKSPACE_DIR", tmp_ws):
                sess_test_dir = os.path.join(tmp_ws, sess_id, "tests")
                os.makedirs(sess_test_dir, exist_ok=True)
                with open(
                    os.path.join(sess_test_dir, "suite.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(suite_data, f, indent=2)

                client = TestClient(srv.app)

                # 1. Before any tests pass, scope=successful returns 400
                res_empty = client.get(
                    f"/download_tests/{sess_id}?scope=successful"
                )
                self.assertEqual(res_empty.status_code, 400)
                self.assertIn(
                    "No successful tests found", res_empty.json()["error"]
                )

                # 2. scope=all returns all scenarios and steps
                res_all = client.get(f"/download_tests/{sess_id}?scope=all")
                self.assertEqual(res_all.status_code, 200)
                self.assertIn("/v1/items", res_all.text)
                self.assertIn("/v1/broken", res_all.text)
                self.assertIn("/v1/items/999", res_all.text)

                # 3. Record execution results where step_ok_1 passed and step_fail_2 failed
                srv.record_scenario_execution_results(
                    sess_id,
                    "sc_passed",
                    {
                        "step_ok_1": {"status": "SUCCESS"},
                        "step_fail_2": {"status": "FAILED"},
                    },
                    "FAILED",
                )

                # 4. GET /download_tests?scope=successful returns only step_ok_1
                res_succ = client.get(
                    f"/download_tests/{sess_id}?scope=successful"
                )
                self.assertEqual(res_succ.status_code, 200)
                self.assertIn(
                    f'filename="test_suite_{sess_id}_passed.py"',
                    res_succ.headers.get("content-disposition", ""),
                )
                self.assertIn("/v1/items", res_succ.text)
                self.assertNotIn("/v1/broken", res_succ.text)
                self.assertNotIn("/v1/items/999", res_succ.text)

                # 5. POST /download_tests with live passed_steps override
                res_post_succ = client.post(
                    f"/download_tests/{sess_id}",
                    json={
                        "scope": "successful",
                        "passed_steps": {"sc_unrun": ["step_unrun_1"]},
                    },
                )
                self.assertEqual(res_post_succ.status_code, 200)
                self.assertIn("/v1/items/999", res_post_succ.text)
                self.assertNotIn("/v1/broken", res_post_succ.text)

                # 6. GET /download_suite?scope=successful returns filtered suite JSON
                res_suite_succ = client.get(
                    f"/download_suite/{sess_id}?scope=successful"
                )
                self.assertEqual(res_suite_succ.status_code, 200)
                dl_suite = res_suite_succ.json()
                self.assertEqual(len(dl_suite["scenarios"]), 1)
                self.assertEqual(
                    len(dl_suite["scenarios"][0]["test_steps"]), 1
                )
                self.assertEqual(
                    dl_suite["scenarios"][0]["test_steps"][0]["step_id"],
                    "step_ok_1",
                )

                # 7. GET /tests/{session_id} (Generated Code tab) renders only test_suite.py (not suite.json or test_api_runner.py)
                res_code_tab = client.get(f"/tests/{sess_id}")
                self.assertEqual(res_code_tab.status_code, 200)
                self.assertIn("test_suite.py", res_code_tab.text)
                self.assertNotIn("test_api_runner.py", res_code_tab.text)
                self.assertNotIn("Declarative suite", res_code_tab.text)
                self.assertIn("/v1/items", res_code_tab.text)
                self.assertIn("/v1/broken", res_code_tab.text)

                res_code_tab_succ = client.get(
                    f"/tests/{sess_id}?scope=successful"
                )
                self.assertEqual(res_code_tab_succ.status_code, 200)
                self.assertIn("test_suite_passed.py", res_code_tab_succ.text)
                self.assertIn("/v1/items", res_code_tab_succ.text)
                self.assertNotIn("/v1/broken", res_code_tab_succ.text)

    def test_github_push_all_vs_successful_tests(self):
        """Verifies GitHubDeployer.push_session_tests supports committing all vs only successful tests and commits only test_suite.py."""
        deployer = GitHubDeployer(session_id="test_sess_gh_scope")

        with tempfile.TemporaryDirectory() as tmp_ws:
            sess_test_dir = os.path.join(
                tmp_ws, "test_sess_gh_scope", "tests"
            )
            os.makedirs(sess_test_dir, exist_ok=True)
            suite_data = {
                "metadata": {
                    "title": "Commerce API",
                    "spec_file": "commerce.yaml",
                },
                "target_base_url": "http://api.commerce.local",
                "scenarios": [
                    {
                        "scenario_id": "SC-PASS",
                        "name": "Products Flow",
                        "setup_steps": [],
                        "test_steps": [
                            {
                                "step_id": "st-pass-1",
                                "method": "GET",
                                "path_template": "/v1/products",
                                "expected_status": 200,
                            },
                            {
                                "step_id": "st-fail-2",
                                "method": "POST",
                                "path_template": "/v1/products",
                                "expected_status": 201,
                            },
                        ],
                        "teardown_steps": [],
                    },
                    {
                        "scenario_id": "SC-FAIL",
                        "name": "Orders Flow",
                        "setup_steps": [],
                        "test_steps": [
                            {
                                "step_id": "st-fail-3",
                                "method": "GET",
                                "path_template": "/v1/orders",
                                "expected_status": 200,
                            }
                        ],
                        "teardown_steps": [],
                    },
                ],
            }
            suite_file_path = os.path.join(sess_test_dir, "suite.json")
            with open(suite_file_path, "w", encoding="utf-8") as f:
                json.dump(suite_data, f, indent=2)

            # 1. only_successful=True with no passed steps returns an error
            err_res = deployer.push_session_tests(
                session_id="test_sess_gh_scope",
                workspace_dir=tmp_ws,
                only_successful=True,
            )
            self.assertEqual(err_res["status"], "error")
            self.assertIn("No successful tests found", err_res["message"])

            # 2. Write execution_results.json with 1 passed step and 2 failed steps
            exec_results = {
                "scenarios": {
                    "SC-PASS": {
                        "status": "FAILED",
                        "steps": {
                            "st-pass-1": "SUCCESS",
                            "st-fail-2": "FAILED",
                        },
                    },
                    "SC-FAIL": {
                        "status": "FAILED",
                        "steps": {"st-fail-3": "FAILED"},
                    },
                }
            }
            with open(
                os.path.join(sess_test_dir, "execution_results.json"),
                "w",
                encoding="utf-8",
            ) as ef:
                json.dump(exec_results, ef, indent=2)

            with mock.patch(
                "manager.core.github_deployer.commit_and_push_local_git"
            ) as mock_git:
                mock_git.return_value = {
                    "sha": "abcdef",
                    "url": "http://commit/abcdef",
                    "branch": "oas-tests-agent/test",
                }
                succ_res = deployer.push_session_tests(
                    session_id="test_sess_gh_scope",
                    workspace_dir=tmp_ws,
                    spec_file="commerce.yaml",
                    only_successful=True,
                )
                self.assertEqual(succ_res["status"], "success")
                self.assertEqual(succ_res["test_scope"], "successful")
                self.assertEqual(succ_res["tests_committed"], 1)
                self.assertEqual(succ_res["scenarios_committed"], 1)

                call_kwargs = mock_git.call_args[1]
                committed_files = call_kwargs["file_dict"]
                self.assertEqual(list(committed_files.keys()), ["test_suite.py"])
                self.assertNotIn("suite.json", committed_files)
                self.assertNotIn("test_api_runner.py", committed_files)
                self.assertIn("/v1/products", committed_files["test_suite.py"])
                self.assertNotIn("/v1/orders", committed_files["test_suite.py"])

                # Verify on-disk suite.json still retains all scenarios/steps for future runs
                with open(suite_file_path, "r", encoding="utf-8") as f:
                    disk_suite = json.load(f)
                self.assertEqual(len(disk_suite["scenarios"]), 2)

    def test_bundled_sample_chooser_and_download_route(self):
        """Verifies the home page offers both bundled samples with download links and /sample_oas only serves whitelisted files."""
        from fastapi.testclient import TestClient
        import server

        client = TestClient(server.app)

        # 1. Home page renders a radio + download link per bundled sample (e-commerce pre-selected)
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn('id="sampleChooser"', home.text)
        self.assertIn('name="sample_spec" value="ecommerce.yaml" checked', home.text)
        self.assertIn('name="sample_spec" value="weather.yaml">', home.text)
        self.assertEqual(server.DEFAULT_OAS_PATH, os.path.join(server.SAMPLE_OAS_DIR, "ecommerce.yaml"))
        self.assertIn('href="/sample_oas/weather.yaml" download', home.text)
        self.assertIn('href="/sample_oas/ecommerce.yaml" download', home.text)
        # The note tells users that simply clicking Generate uses the pre-selected sample
        self.assertIn("No file? Simply click Generate tests", home.text)
        self.assertIn('id="sampleChooserHint">The pre-selected <code>ecommerce.yaml</code> sample is used by default', home.text)

        # 2. Whitelisted samples download as attachments
        for fname in ("weather.yaml", "ecommerce.yaml"):
            res = client.get(f"/sample_oas/{fname}")
            self.assertEqual(res.status_code, 200, fname)
            self.assertIn(f'filename="{fname}"', res.headers.get("content-disposition", ""))
            self.assertIn("openapi:", res.text)

        # 3. Anything outside the whitelist is a 404 (removed/renamed samples, path traversal, unknown files)
        for bad in ("cloud_run_crud_api.yaml", "simple_api.yaml", "..%2Fserver.py", "nope.yaml"):
            self.assertEqual(client.get(f"/sample_oas/{bad}").status_code, 404, bad)

        # 4. /upload without a file uses the selected bundled sample; unknown values fall back to the default
        with mock.patch.object(server, "run_agent_task"), mock.patch.object(server, "save_session_status"):
            picked = client.post("/upload", data={"sample_spec": "weather.yaml"}).json()
            self.assertEqual(picked["spec_used"], "weather.yaml (sample)")
            self.assertEqual(
                server.session_status_map[picked["session_id"]]["spec_path"],
                os.path.join(server.SAMPLE_OAS_DIR, "weather.yaml"),
            )
            fallback = client.post("/upload", data={"sample_spec": "../secrets.yaml"}).json()
            self.assertEqual(fallback["spec_used"], "ecommerce.yaml (sample)")
            self.assertEqual(server.session_status_map[fallback["session_id"]]["spec_path"], server.DEFAULT_OAS_PATH)

    def test_provenance_diff_stats_and_data_flow(self):
        """Verifies the provenance helpers describe guardrail adjustments, journey stats and captured-value data flow."""
        from manager.core.provenance import (
            data_flow_edges,
            diff_generated_scenarios,
            diff_step_fields,
            infer_scenario_source,
            scenario_stats,
        )

        raw = {
            "scenario_id": "products_crud",
            "scenario_type": "HAPPY_PATH_E2E",
            "test_steps": [
                {"step_id": "s1", "method": "POST", "path_template": "/products", "expected_status": 201, "request_body": {"name": "Widget"}, "captures": []},
                {"step_id": "s3", "method": "DELETE", "path_template": "/products/{id}", "expected_status": 204, "path_params": {"id": "abc"}},
                {"step_id": "s2", "method": "GET", "path_template": "/products/{id}", "expected_status": 404, "path_params": {"id": "abc"}},
                {"step_id": "s4", "method": "GET", "path_template": "/products", "expected_status": 404},
            ],
        }
        final = {
            "scenario_id": "products_crud",
            "scenario_type": "HAPPY_PATH_E2E",
            "test_steps": [
                {"step_id": "s1", "method": "POST", "path_template": "/products", "expected_status": 201, "request_body": {"name": "Widget", "price": 9.99}, "captures": [{"variable_name": "product_id", "json_path": "$.id"}]},
                {"step_id": "s2", "method": "GET", "path_template": "/products/{id}", "expected_status": 200, "path_params": {"id": "{{product_id}}"}},
                {"step_id": "s3", "method": "DELETE", "path_template": "/products/{id}", "expected_status": 204, "path_params": {"id": "{{product_id}}"}},
            ],
        }
        adjustments = diff_generated_scenarios([raw], [final])["products_crud"]
        kinds = {(a["kind"], a["step_id"]) for a in adjustments}
        for expected in (
            ("steps_reordered", None),
            ("step_dropped", "s4"),
            ("status_forced", "s2"),
            ("required_fields_added", "s1"),
            ("captures_added", "s1"),
            ("path_chained", "s2"),
            ("path_chained", "s3"),
        ):
            self.assertIn(expected, kinds)

        stats = scenario_stats(final)
        self.assertEqual((stats["steps"], stats["captures"], stats["chained_refs"], stats["negative_steps"]), (3, 1, 2, 0))
        self.assertEqual(stats["resources"], ["products"])
        self.assertEqual(data_flow_edges(final["test_steps"]), [(0, 1, "product_id"), (0, 2, "product_id")])

        self.assertEqual(infer_scenario_source({"scenario_id": "fallback_products"}), "fallback")
        self.assertEqual(infer_scenario_source({"scenario_id": "global_security_check"}), "template")
        self.assertEqual(infer_scenario_source({"scenario_id": "products_crud"}), "gemini")
        changes = diff_step_fields(
            {"path_params": {"id": "abc"}, "expected_status": 404},
            {"path_params": {"id": "{{product_id}}"}, "expected_status": 200},
        )
        self.assertEqual([c["field"] for c in changes], ["path_params.id", "expected_status"])

    def test_generator_records_fallback_provenance_without_credentials(self):
        """Verifies the generator records per-cluster provenance (deterministic baseline + reason) when Gemini is unavailable."""
        ep_post = EndpointMetadata(
            path="/widgets",
            method="POST",
            operation_id="create_widget",
            summary="Create a widget",
            request_body=RequestBodyMetadata(required=True, properties={"name": {"type": "string"}}, required_properties=["name"]),
            responses={"201": {"description": "Created"}},
        )
        ep_get = EndpointMetadata(
            path="/widgets/{widgetId}",
            method="GET",
            operation_id="get_widget",
            summary="Get a widget",
            parameters=[ParameterMetadata(name="widgetId", in_location="path", required=True)],
            responses={"200": {"description": "OK"}},
        )
        oas_meta = OASMetadata(title="Widget_API", version="1.0.0", base_url="https://api.example.com", endpoints=[ep_post, ep_get])

        env = {k: v for k, v in os.environ.items() if k not in {"GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"}}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("manager.agents.test_generator_agent.load_dotenv"):
            generator = TestGeneratorAgent(session_id="prov_test")
            suite = generator.execute(oas_meta)

        report = generator.generation_report
        self.assertFalse(report["credentials_available"])
        self.assertEqual(report["endpoint_count"], 2)
        cluster = report["clusters"]["widgets"]
        self.assertEqual(cluster["source"], "fallback")
        self.assertEqual(cluster["reason"], "no_credentials")
        self.assertEqual(cluster["operations"], ["create_widget", "get_widget"])
        self.assertEqual(cluster["baseline_stats"], cluster["stats"])
        self.assertEqual(sorted(report["scenarios"]), sorted(sc.scenario_id for sc in suite.scenarios))
        self.assertEqual(report["scenarios"]["fallback_widgets"]["source"], "fallback")
        self.assertEqual(report["summary"]["fallback"], len(suite.scenarios))
        self.assertEqual(report["summary"]["gemini"], 0)
        self.assertIsNotNone(report["generated_at"])

    def test_scaffolder_writes_generation_report_and_visualizer_snapshot(self):
        """Verifies the scaffolder persists generation_report.json and renders visualize.html through the shared visualizer."""
        from manager.core.schemas import TestSuite
        from manager.core.suite_scaffolder import TestSuiteScaffolder

        suite = TestSuite(
            suite_id="suite_scaffold_test",
            target_base_url="https://api.example.com",
            scenarios=build_deterministic_fallback_scenarios(
                "widgets",
                [{"operation_id": "list_widgets", "method": "GET", "path": "/widgets", "parameters": [], "success_status": 200}],
            ),
        )
        report = {
            "version": 1,
            "model": None,
            "clusters": {"widgets": {"source": "fallback", "reason": "no_credentials", "operations": ["list_widgets"], "scenario_ids": ["fallback_widgets"], "adjustments": {}, "stats": {"journeys": 1, "steps": 1}, "baseline_stats": {"journeys": 1, "steps": 1}}},
            "scenarios": {"fallback_widgets": {"source": "fallback", "cluster": "widgets", "model": None, "reason": "no_credentials", "adjustments": []}},
            "summary": {"journeys": 1, "gemini": 0, "fallback": 1, "template": 0, "adjustments": 0},
        }
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                files = TestSuiteScaffolder("scaffold_sess").execute(suite, generation_report=report)
            finally:
                os.chdir(cwd)
            sess_dir = os.path.join(tmp, "session_workspaces", "scaffold_sess")
            self.assertTrue(files and all(os.path.exists(os.path.join(tmp, f)) for f in files))
            with open(os.path.join(sess_dir, "tests", "generation_report.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["clusters"]["widgets"]["source"], "fallback")
            with open(os.path.join(sess_dir, "visualize.html"), encoding="utf-8") as f:
                html_out = f.read()
        self.assertIn("Visualize tests", html_out)
        self.assertIn("graph TD", html_out)
        self.assertIn("Deterministic baseline", html_out)
        self.assertIn('integrity="sha384-', html_out)
        self.assertNotIn("tailwindcss", html_out)

    def test_visualize_route_renders_live_provenance_page(self):
        """Verifies /visualize renders live from suite.json + provenance + healing log, escapes untrusted text and rejects bad ids."""
        import server

        client = TestClient(server.app)
        adjustment = {"step_id": "p2", "kind": "path_chained", "detail": "Path parameter(s) re-pointed at captured values: id → {{product_id}}"}
        suite = {
            "suite_id": "suite_vz",
            "target_base_url": "https://api.example.com",
            "scenarios": [
                {
                    "scenario_id": "products_crud",
                    "name": "Product lifecycle <script>alert(1)</script>",
                    "description": "Create, read and delete a product",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "test_steps": [
                        {"step_id": "p1", "operation_id": "create_product", "method": "POST", "path_template": "/products", "expected_status": 201, "request_body": {"name": "Widget"}, "captures": [{"variable_name": "product_id", "json_path": "$.id"}]},
                        {"step_id": "p2", "operation_id": "get_product", "method": "GET", "path_template": "/products/{id}", "expected_status": 200, "path_params": {"id": "{{product_id}}"}},
                    ],
                },
                {
                    "scenario_id": "fallback_orders",
                    "name": "Baseline Flow: Orders",
                    "description": "Deterministic baseline",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "test_steps": [{"step_id": "fallback_orders_step_1_list_orders", "operation_id": "list_orders", "method": "GET", "path_template": "/orders", "expected_status": 200}],
                },
            ],
        }
        report = {
            "version": 1,
            "model": "gemini-test",
            "generated_at": "2026-10-08T10:00:00+00:00",
            "clusters": {
                "products": {"source": "gemini", "model": "gemini-test", "latency_ms": 1800, "attempts": 1, "operations": ["create_product", "get_product"], "scenario_ids": ["products_crud"], "adjustments": {"products_crud": [adjustment]}, "stats": {"journeys": 1, "steps": 2, "captures": 1, "chained_refs": 1, "negative_steps": 0, "assertions": 0}, "baseline_stats": {"journeys": 2, "steps": 3, "captures": 1, "chained_refs": 1, "negative_steps": 1, "assertions": 0}},
                "orders": {"source": "fallback", "reason": "timeout", "operations": ["list_orders"], "scenario_ids": ["fallback_orders"], "adjustments": {}, "stats": {"journeys": 1, "steps": 1, "captures": 0, "chained_refs": 0, "negative_steps": 0, "assertions": 0}, "baseline_stats": {"journeys": 1, "steps": 1, "captures": 0, "chained_refs": 0, "negative_steps": 0, "assertions": 0}},
            },
            "scenarios": {
                "products_crud": {"source": "gemini", "cluster": "products", "model": "gemini-test", "reason": None, "adjustments": [adjustment]},
                "fallback_orders": {"source": "fallback", "cluster": "orders", "model": None, "reason": "timeout", "adjustments": []},
            },
            "summary": {"journeys": 2, "gemini": 1, "fallback": 1, "template": 0, "adjustments": 1},
        }
        healing = [{
            "recorded_at": "2026-10-08T10:05:00+00:00",
            "scenario_id": "products_crud",
            "step_id": "p2",
            "source": "gemini",
            "model": "gemini-test",
            "error": "HTTP 404: not found",
            "reasoning": "Chain the created product id into the lookup.",
            "changes": [{"field": "path_params.id", "before": "abc", "after": "{{product_id}}"}],
            "re_run_success": True,
        }]

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "WORKSPACE_DIR", tmp):
            sid = "vz-session-1"
            tests_dir = os.path.join(tmp, sid, "tests")
            os.makedirs(tests_dir)
            for name, payload in (("suite.json", suite), ("generation_report.json", report), ("healing_log.json", healing)):
                with open(os.path.join(tests_dir, name), "w", encoding="utf-8") as f:
                    json.dump(payload, f)
            with open(os.path.join(tmp, sid, "status.json"), "w", encoding="utf-8") as f:
                json.dump({"result": {"api_title": "Store API"}}, f)
            server.record_scenario_execution_results(sid, "products_crud", {"p1": {"status": "SUCCESS"}, "p2": {"status": "SUCCESS"}}, "SUCCESS")

            res = client.get(f"/visualize/{sid}")
            self.assertEqual(res.status_code, 200)
            page = res.text
            for expected in (
                "Visualize tests",
                "Store API",
                "Designed by Gemini",
                "Deterministic baseline",
                "Healed by Gemini",
                "graph TD",
                "Gemini vs. deterministic baseline, per API domain",
                "the Gemini call timed out",
                "Re-run passed",
                "Last run passed",
                "S0 -. product_id .-&gt; S1",
                'integrity="sha384-',
            ):
                self.assertIn(expected, page, expected)
            # Untrusted suite text is escaped, never emitted as markup
            self.assertNotIn("<script>alert(1)</script>", page)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
            self.assertNotIn("inferred from journey naming", page)

            # Dashboard + code viewer link to the renamed page
            dash = client.get(f"/execution_dashboard/{sid}")
            self.assertEqual(dash.status_code, 200)
            self.assertIn(f'href="/visualize/{sid}"', dash.text)
            self.assertIn("Visualize tests", dash.text)
            self.assertNotIn("Flowcharts", dash.text)

            # Sessions generated before provenance tracking still render, with inferred provenance flagged
            legacy_sid = "vz-session-legacy"
            os.makedirs(os.path.join(tmp, legacy_sid, "tests"))
            with open(os.path.join(tmp, legacy_sid, "tests", "suite.json"), "w", encoding="utf-8") as f:
                json.dump(suite, f)
            legacy = client.get(f"/visualize/{legacy_sid}")
            self.assertEqual(legacy.status_code, 200)
            self.assertIn("inferred from journey naming", legacy.text)
            self.assertIn("Deterministic baseline", legacy.text)

            # Unknown or malformed session ids are rejected before touching the filesystem
            self.assertEqual(client.get("/visualize/does-not-exist").status_code, 404)
            self.assertEqual(client.get("/visualize/bad%20id").status_code, 404)
            self.assertIsNone(server.resolve_session_dir("../etc"))
            self.assertIsNone(server.resolve_session_dir(""))
            self.assertEqual(server.resolve_session_dir(sid), os.path.realpath(os.path.join(tmp, sid)))

    def test_record_healing_event_and_validator_source_tracking(self):
        """Verifies self-healing events persist field-level diffs and the validator reports who produced each fix."""
        import server
        from manager.agents.test_validator_agent import TestFix

        fix = TestFix(reasoning="Use the captured id", fixed_step_id="p2", fixed_path_params={"id": "{{product_id}}"})
        before = {"step_id": "p2", "path_params": {"id": "abc"}, "expected_status": 404}
        after = {"step_id": "p2", "path_params": {"id": "{{product_id}}"}, "expected_status": 200}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server, "WORKSPACE_DIR", tmp):
            server.record_healing_event("sess-heal", "products_crud", "p2", fix, "gemini", "gemini-test", "HTTP 404", before, after, True)
            server.record_healing_event("sess-heal", "products_crud", "p2", fix, "deterministic", "gemini-test", "HTTP 404", before, after, False)
            log = server.load_healing_log("sess-heal")
            self.assertEqual(server.load_healing_log("never-healed"), [])
        self.assertEqual(len(log), 2)
        self.assertEqual((log[0]["source"], log[0]["model"], log[0]["re_run_success"]), ("gemini", "gemini-test", True))
        self.assertEqual([c["field"] for c in log[0]["changes"]], ["path_params.id", "expected_status"])
        self.assertEqual(log[0]["reasoning"], "Use the captured id")
        self.assertEqual((log[1]["source"], log[1]["model"], log[1]["re_run_success"]), ("deterministic", None, False))

        with mock.patch("manager.agents.test_validator_agent.load_dotenv"), mock.patch.dict(os.environ, {}, clear=True):
            validator = TestValidatorAgent()
        self.assertIsNone(validator.client)
        self.assertIsNone(validator.last_fix_source)
        scenario = {
            "scenario_id": "products_crud",
            "scenario_type": "HAPPY_PATH_E2E",
            "test_steps": [{"step_id": "p2", "operation_id": "get_product", "method": "GET", "path_template": "/products/{id}", "path_params": {"id": "abc"}, "expected_status": 200}],
        }
        # Deterministic analyzer path
        with mock.patch.object(validator, "diagnose_and_heal_deterministic", return_value=fix):
            got = asyncio.run(validator.heal_scenario_async(scenario, "Step p2 failed: HTTP 404"))
        self.assertIs(got, fix)
        self.assertEqual(validator.last_fix_source, "deterministic")
        # Gemini path
        gemini_client = mock.MagicMock()
        gemini_client.aio.models.generate_content = mock.AsyncMock(
            return_value=mock.MagicMock(text=json.dumps({"reasoning": "Chain id", "fixed_step_id": "p2", "fixed_path_params": {"id": "{{product_id}}"}}))
        )
        validator.client = gemini_client
        got = asyncio.run(validator.heal_scenario_async(scenario, "Step p2 failed: HTTP 404"))
        self.assertEqual(got.fixed_path_params, {"id": "{{product_id}}"})
        self.assertEqual(validator.last_fix_source, "gemini")

    def test_explain_unhealed_failure_provides_descriptive_diagnosis(self):
        """Verifies explain_unhealed_failure produces clear, specific diagnostics when no safe patch can be applied."""
        from manager.agents.test_validator_agent import TestValidatorAgent

        validator = TestValidatorAgent()
        scenario_dict = {
            "scenario_id": "SCENARIO-PRODUCTS-E2E-01",
            "name": "Text Products Lifecycle and Issuance Verification",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "step_1",
                    "method": "GET",
                    "path_template": "/products/types",
                    "expected_status": 200,
                },
                {
                    "step_id": "step_2",
                    "method": "GET",
                    "path_template": "/products/locations",
                    "expected_status": 200,
                },
                {
                    "step_id": "step_3",
                    "method": "GET",
                    "path_template": "/products/types/{typeId}",
                    "path_params": {"typeId": "{{captured_type_id}}"},
                    "expected_status": 200,
                },
                {
                    "step_id": "step_4",
                    "method": "GET",
                    "path_template": "/products/types/{typeId}/locations",
                    "path_params": {"typeId": "{{captured_type_id}}"},
                    "expected_status": 200,
                },
                {
                    "step_id": "step_5",
                    "method": "GET",
                    "path_template": "/products/types/{typeId}/locations/{locationId}/latest",
                    "path_params": {
                        "typeId": "{{captured_type_id}}",
                        "locationId": "{{captured_loc_id}}",
                    },
                    "expected_status": 200,
                },
            ],
            "teardown_steps": [],
        }
        err_500 = (
            "Expected HTTP 200, got 500: "
            '{"title": "Unexpected Problem", "status": 500, "detail": "An unexpected problem has occurred."} '
            "| Request: GET https://35-244-241-176.nip.io/weather_oas/products/types/MAN/locations/KREV/latest "
            '| Captured Context: {"captured_type_id": "MAN", "captured_loc_id": "KREV"}'
        )

        diag_500 = validator.explain_unhealed_failure(
            scenario_dict, err_500, failing_step_id="step_5"
        )
        self.assertEqual(
            diag_500,
            "Server returned HTTP 500 (Unexpected Problem) instead of 200 — "
            "upstream server error cannot be fixed by modifying test parameters.",
        )

    def test_sample_from_regex_generates_valid_strings(self):
        """Verifies sample_from_regex generates valid matching strings across complex regex patterns."""
        import re
        from manager.agents.test_generator_agent import sample_from_regex

        patterns = [
            r"^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\d{3}$",
            r"([01][0-9]|2[0-3])[0-5][0-9]",
            r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
            r"^[A-Z]{3,4}$",
            r"^\d{6}$",
        ]
        for pat in patterns:
            sampled = sample_from_regex(pat)
            self.assertIsNotNone(sampled, f"Failed to sample for {pat}")
            self.assertTrue(
                bool(re.search(pat, sampled)),
                f"Sample '{sampled}' did not match pattern '{pat}'",
            )

        # Test zone-specific Z preference
        zone_pat = r"^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\d{3}$"
        zone_sampled = sample_from_regex(zone_pat, param_name="zoneId")
        self.assertTrue(
            bool(re.search(zone_pat, zone_sampled)),
            f"Zone sample '{zone_sampled}' did not match pattern",
        )
        self.assertIn("Z", zone_sampled)

    def test_parameter_regex_404_self_healing_heals_path_and_sibling_params(self):
        """Verifies self-healing heals parameter regex mismatch on HTTP 404 with parameterErrors."""
        import re
        from manager.agents.test_validator_agent import TestValidatorAgent

        validator = TestValidatorAgent()
        scenario = {
            "scenario_id": "fallback_auxiliary_endpoints",
            "name": "Auxiliary Endpoints Fallback Suite",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "fallback_auxiliary_endpoints_step_4_zone_forecast",
                    "method": "GET",
                    "path_template": "/zones/{type}/{zoneId}/forecast",
                    "path_params": {"type": "sample_type", "zoneId": "1"},
                    "expected_status": 200,
                }
            ],
            "teardown_steps": [],
        }

        err_msg = (
            'Step fallback_auxiliary_endpoints_step_4_zone_forecast failed: '
            'Expected HTTP 200, got 404: {"correlationId": "d65b4684", "parameterErrors": [{"parameter": "path.zoneId", "message": "Does not match the regex pattern ^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\\d{3}$"}], "title": "Not Found", "type": "https://api.weather.gov/problems/NotFound", "status": 404, "detail": "Not Found", "instance": "https://api.weather.gov/requests/d65b4684"} '
            '| Response: {"correlationId": "d65b4684", "parameterErrors": [{"parameter": "path.zoneId", "message": "Does not match the regex pattern ^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\\d{3}$"}], "title": "Not Found", "type": "https://api.weather.gov/problems/NotFound", "status": 404, "detail": "Not Found", "instance": "https://api.weather.gov/requests/d65b4684"} '
            '| Request: GET https://35-244-241-176.nip.io/weather_oas/zones/sample_type/1/forecast'
        )

        fix = validator.diagnose_and_heal_deterministic(
            scenario, err_msg, spec_path="sample_oas/weather.yaml"
        )
        self.assertIsNotNone(fix)
        self.assertEqual(
            fix.fixed_step_id, "fallback_auxiliary_endpoints_step_4_zone_forecast"
        )
        self.assertIsNotNone(fix.fixed_path_params)
        self.assertIn("zoneId", fix.fixed_path_params)
        self.assertIn("type", fix.fixed_path_params)

        zone_pat = r"^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\d{3}$"
        self.assertTrue(
            bool(re.match(zone_pat, fix.fixed_path_params["zoneId"])),
            f"Healed zoneId '{fix.fixed_path_params['zoneId']}' does not match regex pattern",
        )
        self.assertEqual(fix.fixed_path_params["type"], "forecast")

        # Verify explain_unhealed_failure distinguishes parameterErrors 404 from generic not found
        unhealed_param = validator.explain_unhealed_failure(
            scenario, err_msg, failing_step_id="fallback_auxiliary_endpoints_step_4_zone_forecast"
        )
        self.assertIn("request parameter failed validation constraints", unhealed_param)
        self.assertNotIn("resource not found", unhealed_param)

    def test_boundary_negative_status_code_alignment_does_not_mutate_invalid_inputs(self):
        """Verifies BOUNDARY_NEGATIVE tests expecting 400 that receive 404 align status without mutating negative inputs."""
        from manager.agents.test_validator_agent import TestValidatorAgent

        validator = TestValidatorAgent()
        scenario = {
            "scenario_id": "SCENARIO-ALERTS-03",
            "name": "Alerts Validation and Boundary Negative Checks",
            "scenario_type": "BOUNDARY_NEGATIVE",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "STEP-01-GET-ACTIVE-INVALID-REGION",
                    "method": "GET",
                    "path_template": "/alerts/active/region/{region}",
                    "path_params": {"region": "INVALID_REGION"},
                    "expected_status": 400,
                }
            ],
            "teardown_steps": [],
        }
        err_msg = (
            'Step STEP-01-GET-ACTIVE-INVALID-REGION failed: Expected HTTP 400, got 404. Response: {"correlationId": "d66f3f6f", "parameterErrors": [{"parameter": "path.region", "message": "Does not have a value in the enumeration [\\"AL\\",\\"AT\\",\\"GL\\",\\"GM\\",\\"PA\\\",\\"PI\\"]"}], "title": "Not Found", "type": "https://api.weather.gov/problems/NotFound", "status": 404, "detail": "Not Found", "instance": "https://api.weather.gov/requests/d66f3f6f"}'
        )

        # 1. Prompt build should not raise ValueError
        prompt = validator._build_validation_prompt(scenario, err_msg)
        self.assertIn("Validation & Repair Rules", prompt)

        # 2. Deterministic fix should align expected_status to 404, not mutate region to 'AL'
        fix = validator.diagnose_and_heal_deterministic(
            scenario, err_msg, spec_path="sample_oas/weather.yaml"
        )
        self.assertIsNotNone(fix)
        self.assertEqual(fix.fixed_step_id, "STEP-01-GET-ACTIVE-INVALID-REGION")
        self.assertEqual(fix.fixed_expected_status, 404)
        self.assertIsNone(fix.fixed_path_params)

    def test_multi_failure_journey_diagnoses_and_events(self):
        """Verifies that journeys with multiple failing steps produce distinct diagnoses for each failed step."""
        from server import _execute_and_heal_journey, format_step_results

        target_sc = {
            "scenario_id": "SCENARIO-RADAR-01",
            "name": "Radar Stations, Alarms, and Profilers End-to-End Flow",
            "description": "Validates radar profilers and station endpoints.",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "step_1",
                    "operation_id": "getRadarServers",
                    "method": "GET",
                    "path_template": "/radar/servers",
                    "expected_status": 200,
                },
                {
                    "step_id": "step_2",
                    "operation_id": "getRadarProfiler",
                    "method": "GET",
                    "path_template": "/radar/profilers/{stationId}",
                    "path_params": {"stationId": "STATION1"},
                    "expected_status": 200,
                },
                {
                    "step_id": "step_3",
                    "operation_id": "getRadarStation",
                    "method": "GET",
                    "path_template": "/radar/stations/{stationId}",
                    "path_params": {"stationId": "STATION1"},
                    "expected_status": 200,
                },
            ],
            "teardown_steps": [],
        }

        suite = {
            "target_base_url": "https://api.weather.gov",
            "scenarios": [target_sc],
        }

        async def run_test():
            event_queue: asyncio.Queue = asyncio.Queue()

            async def fake_execute_scenario(sc_obj, step_results, on_step_progress=None):
                step_results["step_1"] = {
                    "status": "SUCCESS",
                    "request_url": "https://api.weather.gov/radar/servers",
                    "request_method": "GET",
                }
                step_results["step_2"] = {
                    "status": "FAILED",
                    "error": 'Expected HTTP 200, got 404: {"title": "Not Found", "status": 404, "detail": "Profiler STATION1 not found"}',
                    "request_url": "https://api.weather.gov/radar/profilers/STATION1",
                    "request_method": "GET",
                }
                step_results["step_3"] = {
                    "status": "FAILED",
                    "error": 'Expected HTTP 200, got 404: {"title": "Not Found", "status": 404, "detail": "Station STATION1 not found"}',
                    "request_url": "https://api.weather.gov/radar/stations/STATION1",
                    "request_method": "GET",
                }
                return False

            with mock.patch("server.TestRunner.execute_scenario", side_effect=fake_execute_scenario), \
                 mock.patch("server.TestValidatorAgent.heal_scenario_async", return_value=None), \
                 tempfile.NamedTemporaryFile("w+", suffix=".json") as tmp_suite:
                json.dump(suite, tmp_suite)
                tmp_suite.flush()

                success, healing_info, step_results = await _execute_and_heal_journey(
                    session_id="test-session-123",
                    target_sc=target_sc,
                    suite=suite,
                    suite_path=tmp_suite.name,
                    healing=True,
                    step_callback=None,
                    event_queue=event_queue,
                    lock=asyncio.Lock(),
                )

                self.assertFalse(success)
                self.assertIsNotNone(healing_info)
                self.assertIn("diagnoses", healing_info)
                diags = healing_info["diagnoses"]
                self.assertEqual(len(diags), 2)
                self.assertEqual(diags[0]["step_id"], "step_2")
                self.assertEqual(diags[1]["step_id"], "step_3")
                self.assertTrue(len(diags[0]["reasoning"]) > 0)
                self.assertTrue(len(diags[1]["reasoning"]) > 0)

                # Check step_results formatting and individual diagnoses
                self.assertIn("diagnosis", step_results["step_2"])
                self.assertIn("diagnosis", step_results["step_3"])
                self.assertEqual(step_results["step_2"]["diagnosis"], diags[0]["reasoning"])
                self.assertEqual(step_results["step_3"]["diagnosis"], diags[1]["reasoning"])

                formatted = format_step_results(step_results)
                self.assertEqual(formatted["step_2"]["diagnosis"], diags[0]["reasoning"])
                self.assertEqual(formatted["step_3"]["diagnosis"], diags[1]["reasoning"])

                # Check SSE events emitted
                events = []
                while not event_queue.empty():
                    events.append(await event_queue.get())

                healing_failed_events = [e for e in events if e.get("event") == "HEALING_FAILED"]
                self.assertEqual(len(healing_failed_events), 1)
                hf_event = healing_failed_events[0]
                self.assertEqual(hf_event["target_steps"], ["step_2", "step_3"])
                self.assertIsNone(hf_event["target_step"])
                self.assertEqual(len(hf_event["diagnoses"]), 2)
                self.assertEqual(hf_event["diagnoses"][0]["step_id"], "step_2")
                self.assertEqual(hf_event["diagnoses"][1]["step_id"], "step_3")

        asyncio.run(run_test())

    def test_bundle_clusters_preserves_cohesive_domains_and_safe_fallback_limits(self):
        """Verifies smart domain bundling, safe collection limits, and fallback chunking."""
        # 1. Verify bundle_clusters keeps cohesive domains (>= 3 ops) independent
        raw = {
            "products": [{"op": f"p_{i}"} for i in range(9)],
            "alerts": [{"op": f"a_{i}"} for i in range(8)],
            "aviation": [{"op": f"av_{i}"} for i in range(7)],
            "stations": [{"op": f"st_{i}"} for i in range(7)],
            "radar": [{"op": f"r_{i}"} for i in range(7)],
            "zones": [{"op": f"z_{i}"} for i in range(6)],
            "gridpoints": [{"op": f"gp_{i}"} for i in range(4)],
            "icons": [{"op": f"ic_{i}"} for i in range(3)],
            "offices": [{"op": f"of_{i}"} for i in range(3)],
            "points": [{"op": "pt_1"}, {"op": "pt_2"}],
            "glossary": [{"op": "gl_1"}],
            "thumbnails": [{"op": "th_1"}],
        }
        bundled = bundle_clusters(raw, max_clusters=12, min_independent_size=3)
        self.assertEqual(len(bundled), 10)
        self.assertIn("zones", bundled)
        self.assertIn("gridpoints", bundled)
        self.assertIn("icons", bundled)
        self.assertIn("offices", bundled)
        self.assertIn("auxiliary_endpoints", bundled)
        self.assertEqual(len(bundled["auxiliary_endpoints"]), 4)

        # 2. Verify fallback chunking when a cluster has > 8 endpoints
        bulk_ops = [
            {
                "operation_id": f"bulk_op_{i}",
                "method": "GET",
                "path": f"/items/{i}" if i % 2 == 1 else "/items",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "type": "integer",
                        "default": 500,
                    }
                ],
                "success_status": 200,
            }
            for i in range(14)
        ]
        scenarios = build_deterministic_fallback_scenarios("bulk", bulk_ops)
        # Should be chunked into modular scenarios (max 6 ops each) + boundary negative
        self.assertGreater(len(scenarios), 2)
        happy_scenarios = [s for s in scenarios if s.scenario_type == ScenarioType.HAPPY_PATH_E2E]
        for hs in happy_scenarios:
            self.assertLessEqual(len(hs.test_steps), 6)

        # 3. Verify safe default limit parameter is injected for collection query
        first_step = happy_scenarios[0].test_steps[0]
        self.assertEqual(first_step.query_params.get("limit"), 10)

        # 4. Verify codegen replaces unpopulated context variables with safe dummies instead of raw curlies
        dummy_suite = {
            "suite_id": "test_suite",
            "target_base_url": "https://api.example.com",
            "scenarios": [
                {
                    "scenario_id": "scen_1",
                    "name": "Test Unresolved Chaining",
                    "description": "Tests that missing captured variables do not generate raw curly braces in URLs",
                    "scenario_type": "HAPPY_PATH_E2E",
                    "setup_steps": [],
                    "test_steps": [
                        {
                            "step_id": "step_1",
                            "operation_id": "get_item",
                            "method": "GET",
                            "path_template": "/items/{itemId}",
                            "path_params": {"itemId": "{{missing_item_id}}"},
                            "expected_status": 200,
                        }
                    ],
                    "teardown_steps": [],
                }
            ],
        }
        code = generate_python_test_suite(dummy_suite)
        self.assertIn("pv = '1' if 'id' in pv_clean.lower() else f'sample_{pv_clean}'", code)
        self.assertIn("return '1' if 'id' in k.lower() else f'sample_{k}'", code)

    def test_404_self_healing_with_captured_context_and_chunked_setup_steps(self):
        """Verifies 404 self-healing when Captured Context is present, county zone 404 healing, dict-key ID extraction, and chunked setup_steps."""
        from manager.agents.test_generator_agent import _infer_param_resource_stem
        from manager.agents.test_validator_agent import TestValidatorAgent

        validator = TestValidatorAgent()

        # 1. HTTP 404 with parameterErrors + appended "| Captured Context: {...}" must not truncate regex at \d{3}$
        zones_scenario = {
            "scenario_id": "fallback_zones",
            "name": "Baseline Flow: Zones",
            "scenario_type": "HAPPY_PATH_E2E",
            "setup_steps": [],
            "test_steps": [
                {
                    "step_id": "fallback_zones_step_2_zone_forecast",
                    "operation_id": "zone_forecast",
                    "method": "GET",
                    "path_template": "/zones/{type}/{zoneId}/forecast",
                    "path_params": {"type": "forecast", "zoneId": "{{zones_id}}"},
                    "expected_status": 200,
                },
                {
                    "step_id": "fallback_zones_step_4_zone_stations",
                    "operation_id": "zone_stations",
                    "method": "GET",
                    "path_template": "/zones/forecast/{zoneId}/stations",
                    "path_params": {"zoneId": "{{zones_id}}"},
                    "expected_status": 200,
                },
            ],
            "teardown_steps": [],
        }

        err_with_ctx = (
            "Step 'fallback_zones_step_2_zone_forecast' failed: Expected HTTP 200, got 404. "
            'Body: {"correlationId": "abc", "parameterErrors": [{"parameter": "path.zoneId", '
            '"message": "Does not match the regex pattern ^(A[KLMNRSZ]|C[AOT]|D[CE]|F[LM]|G[AMU]|I[ADLN]|K[SY]|L[ACEHMOS]|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[AHKMRSWZ]|S[CDL]|T[NX]|UT|V[AIT]|W[AIVY]|[HR]I)[CZ]\\\\d{3}$"}], '
            '"status": 404, "detail": "Not Found"} '
            "| Request: GET https://api.weather.gov/zones/forecast/ALC001/forecast params=None "
            '| Captured Context: {"zones_id": "https://api.weather.gov/zones/county/ALC001"}'
        )
        fix_step_2 = validator.diagnose_and_heal_deterministic(
            zones_scenario, err_with_ctx, spec_path="sample_oas/weather.yaml"
        )
        self.assertIsNotNone(fix_step_2)
        self.assertEqual(fix_step_2.fixed_step_id, "fallback_zones_step_2_zone_forecast")
        self.assertEqual(fix_step_2.fixed_path_params.get("zoneId"), "AZZ111")

        # 2. Plain HTTP 404 (no parameterErrors) on /zones/forecast/{zoneId}/stations when zones_id is county zone ALC001
        err_plain_404 = (
            "Step 'fallback_zones_step_4_zone_stations' failed: Expected HTTP 200, got 404. "
            'Body: {"title": "Not Found", "status": 404, "detail": "Not Found"} '
            "| Request: GET https://api.weather.gov/zones/forecast/ALC001/stations params={'limit': 10} "
            '| Captured Context: {"zones_id": "https://api.weather.gov/zones/county/ALC001"}'
        )
        fix_step_4 = validator.diagnose_and_heal_deterministic(
            zones_scenario, err_plain_404, spec_path="sample_oas/weather.yaml"
        )
        self.assertIsNotNone(fix_step_4)
        self.assertEqual(fix_step_4.fixed_step_id, "fallback_zones_step_4_zone_stations")
        self.assertEqual(fix_step_4.fixed_path_params.get("zoneId"), "AZZ111")

        # 3. Sub-resource stem disambiguation: /radar/profilers/{stationId} -> 'profiler' (not 'station')
        self.assertEqual(
            _infer_param_resource_stem("/radar/profilers/{stationId}", "stationId", "radar"),
            "profiler",
        )
        self.assertEqual(
            _infer_param_resource_stem("/radar/stations/{stationId}/alarms", "stationId", "radar"),
            "station",
        )

        # 4. Dict-key ID map extraction (e.g. /products/locations returning {"locations": {"ABQ": "Albuquerque, NM"}})
        loc_payload = {
            "@context": {"@version": "1.1"},
            "locations": {"ABQ": "Albuquerque, NM", "AKQ": "Wakefield, VA"},
        }
        self.assertEqual(
            VariableResolver.extract_jsonpath(loc_payload, "$.locations[0].id"),
            "ABQ",
        )


if __name__ == "__main__":
    unittest.main()
