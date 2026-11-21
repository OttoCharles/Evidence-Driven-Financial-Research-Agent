"""Exercise browser model settings through an isolated app and a loopback mock."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ROUTER_KEY = "browser-test-router-key"
COMPANY_KEY = "browser-test-company-key"
FILE_KEY = "browser-test-file-key"
BASE_CONFIG = f'[openrouter]\napi_key = "{FILE_KEY}"\nmodel = "openai/file-default"\n'


class MockModel(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/v1/chat/completions":
            response = {
                "clarification": {
                    "message": f"Loopback model received {COMPANY_KEY}.",
                    "fields": [
                        {
                            "name": "year",
                            "label": "Fiscal year",
                            "control": "select",
                            "options": [{"value": "2024", "label": "FY2024"}],
                        }
                    ],
                }
            }
        else:
            response = {"out_of_scope": "New loopback endpoint received the clarification."}
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(response)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def open_settings(page):
    button = page.get_by_role("button", name="Model settings", exact=True)
    expect(button).to_be_enabled(timeout=20000)
    button.click()
    dialog = page.locator("#model-settings-dialog")
    expect(dialog).to_be_visible()
    return dialog


def check_model_settings(browser, app_url, mock_url, requests):
    page = browser.new_page(viewport={"width": 1440, "height": 960})
    received = []
    page.on("websocket", lambda ws: ws.on("framereceived", lambda frame: received.append(frame)))
    page.goto(app_url)
    dialog = open_settings(page)
    provider = dialog.locator("#model-provider")
    model = dialog.locator("#model-name")
    key = dialog.locator("#model-api-key")
    endpoint = dialog.locator("#model-base-url")
    token_limit = dialog.locator("#model-token-limit-parameter")
    save = dialog.get_by_role("button", name="Save settings")

    expect(provider).to_have_value("openrouter")
    expect(model).to_have_value("openai/file-default")
    expect(key).to_have_attribute("type", "password")
    expect(key).to_have_value("")
    model.fill("openai/browser-model")
    key.fill(ROUTER_KEY)
    save.click()
    expect(dialog).not_to_be_visible()

    open_settings(page)
    expect(model).to_have_value("openai/browser-model")
    expect(key).to_have_value("")
    provider.select_option("openai_compatible")
    expect(key).to_have_value("")
    model.fill("corp-model-one")
    endpoint.fill("http://company.example/v1")
    key.fill(COMPANY_KEY)
    save.click()
    expect(dialog.get_by_role("alert")).to_contain_text("HTTPS")
    expect(key).to_have_value("")

    endpoint.fill(f"{mock_url}/v1/")
    key.fill(COMPANY_KEY)
    token_limit.select_option("max_tokens")
    save.click()
    expect(dialog).not_to_be_visible()
    page.locator("#research-mode").select_option("live")
    page.locator("#research-question").fill("Did Microsoft’s profit increase?")
    page.get_by_role("button", name="Run investigation").click()
    expect(page.get_by_text("Loopback model received [redacted]", exact=False).first).to_be_visible(
        timeout=20000
    )
    expect(page.get_by_role("heading", name="Clarify the claim")).to_be_visible()

    open_settings(page)
    expect(provider).to_have_value("openai_compatible")
    expect(endpoint).to_have_value(f"{mock_url}/v1")
    expect(key).to_have_value("")
    model.fill("corp-model-two")
    endpoint.fill(f"{mock_url}/gateway/v1")
    token_limit.select_option("max_completion_tokens")
    save.click()
    expect(dialog).not_to_be_visible()
    page.get_by_role("combobox", name="Fiscal year").select_option("2024")
    page.get_by_role("button", name="Resume investigation").click()
    expect(
        page.get_by_text("New loopback endpoint received the clarification.", exact=True).first
    ).to_be_visible(timeout=20000)

    (first_path, first_auth, first_request), (second_path, second_auth, second_request) = requests
    assert first_path == "/v1/chat/completions"
    assert first_auth == f"Bearer {COMPANY_KEY}"
    assert first_request["model"] == "corp-model-one"
    assert first_request["max_tokens"] == 6500
    assert second_path == "/gateway/v1/chat/completions"
    assert second_auth is None
    assert second_request["model"] == "corp-model-two"
    assert second_request["max_completion_tokens"] == 6500
    assert json.loads(second_request["messages"][-1]["content"])["clarification_answers"] == {"year": "2024"}

    other = browser.new_page()
    other.goto(app_url)
    other_dialog = open_settings(other)
    expect(other_dialog.locator("#model-provider")).to_have_value("openrouter")
    expect(other_dialog.locator("#model-name")).to_have_value("openai/file-default")
    other.close()

    open_settings(page)
    dialog.get_by_role("button", name="Restore defaults").click()
    expect(dialog).not_to_be_visible()
    open_settings(page)
    expect(model).to_have_value("openai/file-default")
    expect(key).to_have_value("")
    expect(dialog.get_by_role("button", name="Restore defaults")).to_be_disabled()

    storage = page.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    assert received
    for secret in (ROUTER_KEY, COMPANY_KEY, FILE_KEY):
        assert secret not in storage
        assert all(
            secret.encode() not in (frame.encode() if isinstance(frame, str) else frame) for frame in received
        )
    page.close()


def main():
    with (
        TemporaryDirectory(prefix="research-settings-") as directory,
        ThreadingHTTPServer(("127.0.0.1", 0), MockModel) as mock,
    ):
        workspace = Path(directory)
        shutil.copy2(ROOT / "app.py", workspace)
        shutil.copytree(
            ROOT / "financial_research",
            workspace / "financial_research",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        config = workspace / "config.local.toml"
        config.write_text(BASE_CONFIG, encoding="utf-8")
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "OPENROUTER_API_KEY",
                "OPENAI_COMPATIBLE_API_KEY",
                "FRED_API_KEY",
                "TAVILY_API_KEY",
                "SEC_USER_AGENT",
            }
        }
        mock_url = f"http://127.0.0.1:{mock.server_port}"
        # Keep even a regressed provider selection from reaching external services.
        environment.update(
            {
                "FINANCIAL_RESEARCH_DB": str(workspace / "test.sqlite3"),
                "NO_PROXY": "127.0.0.1",
                "HTTP_PROXY": mock_url,
                "HTTPS_PROXY": mock_url,
                "ALL_PROXY": mock_url,
            }
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        mock.requests = []
        thread = Thread(target=mock.serve_forever, daemon=True)
        thread.start()
        log = workspace / "streamlit.log"
        command = [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.address",
            "127.0.0.1",
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ]
        try:
            with (
                log.open("w", encoding="utf-8") as output,
                subprocess.Popen(
                    command, cwd=workspace, env=environment, stdout=output, stderr=subprocess.STDOUT
                ) as app,
            ):
                try:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=1):
                                break
                        except (ConnectionRefusedError, TimeoutError):
                            if app.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError("The isolated test app did not start") from None
                            time.sleep(0.1)
                    with sync_playwright() as playwright:
                        options = {"headless": True}
                        edge = Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
                        if edge.exists():
                            options["executable_path"] = str(edge)
                        browser = playwright.chromium.launch(**options)
                        check_model_settings(browser, f"http://127.0.0.1:{port}/", mock_url, mock.requests)
                        browser.close()
                finally:
                    app.terminate()
        except Exception:
            print(log.read_text(encoding="utf-8", errors="replace")[-4000:], file=sys.stderr)
            raise
        finally:
            mock.shutdown()
            thread.join()

        assert config.read_text(encoding="utf-8") == BASE_CONFIG
        for path in [log, *workspace.glob("*.sqlite3*")]:
            contents = path.read_bytes()
            assert all(secret.encode() not in contents for secret in (ROUTER_KEY, COMPANY_KEY, FILE_KEY))
    print("Browser model settings passed.")


if __name__ == "__main__":
    main()
