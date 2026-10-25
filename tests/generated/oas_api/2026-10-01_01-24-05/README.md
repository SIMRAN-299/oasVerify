# Automated Integration Tests: oas_api

- **Generated Timestamp:** 2026-10-01_01-24-05
- **Session ID:** `2b9e756f-a80d-40f1-bfa1-0491e800cbd5`
- **Target Base URL:** `https://35-244-241-176.nip.io/weather_oas`
- **Total Scenarios:** 12
- **Total Test Steps:** 64

## Files in this Directory
- `test_suite.py`: Standalone, executable PyTest integration test suite.
- `suite.json`: Declarative JSON defining all scenarios, parameters, and assertions.
- `test_api_runner.py`: Runner execution wrapper.

## Running Tests
Run natively with PyTest:
```bash
pytest test_suite.py -v
```

Override target API Base URL at runtime:
```bash
API_BASE_URL="https://35-244-241-176.nip.io/weather_oas" pytest test_suite.py -v
```
