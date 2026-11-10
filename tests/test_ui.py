import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from frontend import views


def test_draft_editor_ignores_blank_rows_but_keeps_pinned_rows(monkeypatch):
    descriptions = [{"text": f"Description {i}", "pin": ""} for i in range(1, 5)]
    pinned_without_text = {"text": "", "pin": "DESCRIPTION_1"}
    rows = descriptions + [{"text": "  ", "pin": None}, pinned_without_text]
    monkeypatch.setattr(views.st, "markdown", lambda *_args: None)
    monkeypatch.setattr(views.st, "data_editor", lambda *_args, **_kwargs: pd.DataFrame(rows))

    assert views.edit_assets("descriptions", {"descriptions": descriptions}, "test") == (
        descriptions + [pinned_without_text]
    )


@pytest.mark.skipif(os.getenv("TEST_LOCAL_UI") != "1", reason="requires local TCP access")
def test_demo_launch_and_all_pages(tmp_path, monkeypatch):
    backend = "http://127.0.0.1:18700"
    frontend = "http://127.0.0.1:18701"
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DASHBOARD_MODE": "demo", "BACKEND_URL": backend,
           "DEMO_DB": str(tmp_path / "ui.sqlite3")}
    monkeypatch.setenv("BACKEND_URL", backend)
    servers = []
    logs = []
    commands = [
        ["uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "18700"],
        ["streamlit", "run", "frontend/app.py", "--global.developmentMode", "false",
         "--server.address", "127.0.0.1", "--server.port", "18701", "--server.headless", "true"],
    ]
    try:
        for i, command in enumerate(commands):
            log = tmp_path / f"server-{i}.log"
            logs.append(log)
            with log.open("wb") as output:
                servers.append(subprocess.Popen([sys.executable, "-m", *command], cwd=root, env=env,
                                                stdout=output, stderr=subprocess.STDOUT))
        for server, endpoint in zip(servers, (backend + "/health", frontend + "/_stcore/health")):
            deadline = time.monotonic() + 25
            while server.poll() is None and time.monotonic() < deadline:
                try:
                    if httpx.get(endpoint, timeout=1, trust_env=False).is_success:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(.2)
            else:
                pytest.fail("\n".join(log.read_text(errors="replace") for log in logs))

        app = AppTest.from_file(str(root / "frontend" / "app.py"), default_timeout=20).run()
        assert not app.exception and not app.error
        assert app.title[0].value == "Marketing Performance"
        assert any(metric.label == "Shopify orders" and metric.value == "Not available" for metric in app.metric)
        app.sidebar.radio[0].set_value("Paid Advertising").run()
        assert app.title[0].value == "All Paid"
        next(widget for widget in app.radio if widget.label == "Paid platform").set_value("Meta Ads").run()
        assert any("not connected" in item.value for item in app.info)
        next(widget for widget in app.radio if widget.label == "Paid platform").set_value("Google Ads").run()
        assert len(app.metric) == 10

        for page, title in (
            ("Weekly Review", "Weekly performance review"),
            ("Campaigns", "Campaigns"), ("Ad Content", "Ad Content"),
        ):
            next(widget for widget in app.radio if widget.label == "Google Ads view").set_value(page).run()
            assert not app.exception and not app.error
            assert app.title[0].value == title
        for view, title in (("Keywords", "Keywords & search terms"), ("Drafts", "Draft revisions")):
            next(widget for widget in app.radio if widget.label == "Ad Content").set_value(view).run()
            assert not app.exception and not app.error
            assert app.title[0].value == title
        save = next(button for button in app.button if button.label == "Save draft")
        assert not save.disabled
        save.click().run()
        assert app.success[0].value == "Draft saved."

        app.sidebar.radio[0].set_value("Organic Marketing").run()
        assert app.title[0].value == "Organic Marketing"
        assert "Facebook analytics" in [tab.label for tab in app.tabs]
        next(widget for widget in app.radio if widget.label == "Organic channel").set_value("Shopify Blog").run()
        assert not app.exception and not app.error
        assert "UTM Link Builder" in [tab.label for tab in app.tabs]
        app.sidebar.radio[0].set_value("Reports & AI Insights").run()
        assert app.title[0].value == "Reports & AI Insights"
        app.sidebar.checkbox[0].check().run()
        assert not app.exception and not app.error
        assert app.title[0].value == "Connections & Sync"
    finally:
        for server in servers:
            if server.poll() is None:
                server.terminate()
            server.wait(timeout=10)
