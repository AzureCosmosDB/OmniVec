# OmniVec portal UI tests

These tests run the real legacy `web/static/classic.html` UI in headless Chromium with
network calls intercepted. They cover source and destination wizard behavior,
permission guidance, API request payloads, responsive layout, and parity with
the CLI connector contract.

```powershell
pip install -r tests\ui\requirements.txt
python -m playwright install chromium
pytest tests\ui -v
```

The suite is hermetic: it serves the checked-in static app locally and does not
contact Azure or external web services.
