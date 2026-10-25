# Automated Integration Tests: oas_api

- **Generated Timestamp:** 2026-10-01_16-15-08
- **Session ID:** `905c00c2-0473-4515-8331-a51eb699e1b6`
- **Target Base URL:** `https://35-244-241-176.nip.io/weather_oas`
- **Total Scenarios:** 19
- **Total Test Steps:** 80

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
