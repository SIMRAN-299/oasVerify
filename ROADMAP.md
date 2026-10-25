# 🗺️ Project Roadmap & Priority Specifications

This document defines the categorized priority tiers, feature backlog, and implementation specifications for **OASVerify**.

---

## 📊 Priority Matrix Overview

| Tier | Focus Area | Status | Key Features |
| :--- | :--- | :---: | :--- |
| **P0** | **Core Test Viability** | **✅ 100% Complete** | Dynamic Base URL, Parameter substitution, Request bodies, Auth tokens, Real 400 Bad Request assertions. |
| **P1** | **Organization, Execution & Deep Validation** | **🔄 In Progress** | **Test Execution Agent (`test_exec_agent`)**, UI "Run Tests" modal & report viewer, CLI execution utility, Commenting & Bucketing, CRUD state validation, `conftest.py` harness, Response schema verification. |
| **P2** | **Combinatorial Workflows & Advanced Auth** | **⏳ Next Up** | Method lifecycle combinations (CR, CU, CD, CRD, CUD, full CRUD), OAuth 2.0 client credentials flow, Header permutations. |
| **P3** | **Multi-Language, Metrics & Agent Evals** | **⏳ Backlog** | Multi-language generation (Python, TS, Java, Go), Sonar-style OAS coverage matrix, Agent eval benchmark suite. |

---

## 🔴 Tier 1: P0 — Core Test Viability (Completed)

1. **Configurable Base URL**: Extracted from OAS `servers` block with `API_BASE_URL` runtime environment override.
2. **Path & Query Parameter Substitution**: Concrete dummy values generated based on schema constraints (UUIDs for `format: uuid`, enums, integers).
3. **Request Body & Full HTTP Method Support**: Automatic schema-compliant JSON generation for `POST`, `PUT`, and `PATCH`.
4. **Auth Token Header Injection**: Automatic detection of `BearerAuth` and `apiKey` security schemes, generating `get_headers(auth_required=...)` reading from `API_AUTH_TOKEN` or `API_KEY`.
5. **Real 400 Bad Request Assertions**: Generates negative test cases by omitting required fields or passing boundary violations, asserting `status_code in [400, 422]`.

---

## 🟡 Tier 2: P1 — Usability, Test Organization & Test Execution

### 1. Test Execution Agent (`test_exec_agent`) & Automated Reporting *(P1 Priority)*
An autonomous agent responsible for executing generated test suites against live or mock target APIs, capturing runtime diagnostics, and synthesizing test execution reports:
* **UI "Run Tests" Action & Parameter Modal**:
  * After tests are generated and reviewed, the UI surfaces a prominent **"Run Tests"** button.
  * Clicking opens a runtime parameter configuration modal allowing the user to provide:
    * **Target Base URL**: Pre-filled with OAS `BASE_URL`, overridable for local/staging/prod.
    * **Authentication Credentials**: Input fields for `API_AUTH_TOKEN` (Bearer token) or `API_KEY` with secure masked inputs.
    * **Test Scope / Markers**: Checkboxes to run all tests or target specific buckets (e.g. `--smoke`, `--crud`, `--negative`).
    * **Custom Environment Variables / Headers**: Optional extra parameters.
  * Submitting triggers asynchronous background execution via `POST /execute/{session_id}`.
* **Interactive UI Test Report Viewer**:
  * Displays real-time test execution progress.
  * Renders interactive test run reports (pass/fail status, duration, assertion failures, HTTP status codes, and request/response payloads).
* **CLI Execution Utility**:
  * Flag support in main runner: `python main.py --oas-file <spec> --session-id <id> --run-tests --base-url <url> --auth-token <token>`
  * Dedicated standalone runner: `python run_tests.py --session-id <id> [--base-url <url>] [--auth-token <token>] [-m <marker>]`
  * Exports formatted terminal output + saves HTML/JSON reports to `session_workspaces/<session_id>/reports/`.
* **Failure Forensics & Tracebacks**:
  * Automatically isolates failing test cases with exact HTTP response bodies and assertion failure tracebacks for rapid debugging.

### 2. Commenting & Bucketing of Test Cases
* **PyTest Markers / Tagging**:
  * `@pytest.mark.smoke`: Basic happy-path endpoint checks.
  * `@pytest.mark.negative`: Schema violation, boundary, and error-handling tests.
  * `@pytest.mark.crud`: Single-resource and combinatorial lifecycle workflows.
  * `@pytest.mark.auth`: Security and unauthenticated negative checks.
  * *Benefit*: Allows selective execution in CI/CD and UI (e.g. `pytest -m smoke` or `pytest -m crud`).
* **Resource-Based Class Bucketing**:
  * Grouping tests into structured classes: e.g. `TestProductsHappyPath`, `TestProductsNegative`, `TestProductsWorkflows`.
* **Standardized Header & Inline Comments**:
  * Structured docstrings and inline step comments explaining:
    * *Objective*: What business rule is being exercised.
    * *Pre-conditions & Inputs*: Payloads and parameters passed.
    * *Expected Assertions*: Status codes, headers, and body structures expected.

### 3. Validate CRUD Operation Scenarios (Deep State Verification)
Moving beyond HTTP status codes (`200`, `204`) to verify **semantic state integrity**:
* **Post-Create Verification (C)**: Verify the response contains generated IDs and matches input fields.
* **Post-Read Verification (R)**: Assert that retrieved field values strictly equal the payload sent during creation (`assert data["name"] == payload["name"]`).
* **Post-Update Verification (U)**: Verify that updated fields changed while immutable/untouched fields remained intact.
* **Post-Delete Negative Verification (D)**: After `DELETE` returns 204/200, issue an immediate follow-up `GET /{id}` to verify that the resource returns `404 Not Found`.

### 4. Dedicated `conftest.py` & Test Harness
* Extract `BASE_URL`, `get_headers()`, and common authentication session fixtures into a shared `conftest.py` file per workspace.
* Provides fixtures for reusable authenticated sessions (`requests.Session`).

### 5. Response Schema Assertions
* Validate that response bodies strictly adhere to the OpenAPI `responses.200.content.application/json.schema` using `jsonschema` or Pydantic validation.

### 6. HITL (Human-In-The-Loop) Approval Guardrails
* Mandatory interactive review gate before any generated test files are deployed or committed to remote Git repositories.

---

## 🔵 Tier 3: P2 — Advanced Integrations & Combinatorial Workflows

### 1. Method Lifecycle Combinations for CRUD
Currently, the engine generates a single monolithic CRUD lifecycle per resource. P2 expands this to generate **combinatorial lifecycle permutations** based on supported HTTP methods on each endpoint:

* **2-Step Transitions**:
  * **CR (Create $\rightarrow$ Read)**: Validates immediate persistence and retrieval.
  * **CU (Create $\rightarrow$ Update)**: Validates state transition immediately following creation.
  * **CD (Create $\rightarrow$ Delete)**: Validates fast lifecycle teardown / ephemeral entity creation.
  * **RU (Read $\rightarrow$ Update)**: Validates updating an existing read entity.
* **3-Step Transitions**:
  * **CRD (Create $\rightarrow$ Read $\rightarrow$ Delete)**: Creation, verification, and cleanup.
  * **CUD (Create $\rightarrow$ Update $\rightarrow$ Delete)**: Creation, modification, and cleanup.
  * **CRU (Create $\rightarrow$ Read $\rightarrow$ Update)**: Multi-read before update.
* **Complete Lifecycle (4-Step + Deletion Confirmation)**:
  * **CRUD**: `POST` (Create) $\rightarrow$ `GET` (Verify) $\rightarrow$ `PUT` (Update) $\rightarrow$ `GET` (Verify Update) $\rightarrow$ `DELETE` (Remove) $\rightarrow$ `GET` (Assert 404).
* **Idempotency & Boundary Combinations**:
  * **DD (Double Delete)**: `DELETE` $\rightarrow$ `DELETE` (verifies second delete returns 404).
  * **UU (Double Update)**: Validates update idempotency.

### 2. OAuth 2.0 Client Credentials Flow
* Dynamic acquisition of Bearer tokens via `/oauth/token` exchange (`client_id` + `client_secret`) prior to test suite execution.

---

## ⚪ Tier 4: P3 — Governance, Multi-Language & Continuous Improvement

### 1. Multi-Language Test Generation (Polyglot Generator) *(Moved to P3)*
Extending the code generation engine beyond Python to support multiple target enterprise testing frameworks via modular Jinja2 templates:
* **Python**: `pytest` + `requests` (Default)
* **TypeScript / JavaScript**: `jest` / `mocha` + `axios`
* **Java**: `JUnit 5` + `REST Assured`
* **Go**: `testing` + `testify` + `net/http`
* *Configuration*: `--language [python|typescript|java|go]`

### 2. Sonar-Style OAS Coverage Dashboard
* Granular coverage metrics:
  * `% Endpoints Exercised` (e.g. 6/6 endpoints covered = 100%).
  * `% Status Codes Covered` (e.g. 200, 201, 204, 400, 401, 404).
  * `% Request Body Fields & Enums Tested`.
* Visual HTML coverage report exportable alongside test runs.

### 3. Automated Agent Benchmark & Evaluation Suite
* Benchmark evaluation runner measuring agent accuracy, syntax error rates, and self-healing success rates across 20+ standard OpenAPI specifications.

