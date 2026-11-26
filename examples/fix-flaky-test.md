# Fix the flaky test

`tests/test_api.py::test_retry` fails about one run in five on CI.

1. Reproduce it locally (run it 50 times).
2. Find the cause; fix the code, not the test, unless the test is wrong.
3. Run the full suite once at the end.

Do not touch anything under `migrations/`.
