# OmniVec portal UI tests

These tests run the real legacy `web/static/classic.html` UI in headless Chromium with
network calls intercepted. They cover source and destination wizard behavior,
permission guidance, API request payloads, responsive layout, and parity with
the CLI connector contract.

`test_spa.py` covers the redesigned console (`web/static/index.html` and
`web/static/app/*.js`) through the `spa_app` fixture. It uses mock API
responses from `spa_mock.py` (override them per test with `overrides=`),
covering sign-in, Home, pipeline pagination and detail, connections, models,
recipes, search, metrics, the agent lock and the API path guard. Any uncaught
JavaScript error fails the test.

```powershell
pip install -r tests\ui\requirements.txt
python -m playwright install chromium
pytest tests\ui -v
```

The suite is hermetic: it serves the checked-in static app locally and does not
contact Azure or external web services.
