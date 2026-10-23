from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Thread
from urllib.request import Request

import pytest

from scripts.qualification.verify_documentation_identity import (
    deployed_identity_failures,
    expected_identity,
    html_identity_failures,
    site_identity_failures,
)

COMMIT = "a" * 40


def rendered_page(library: str = "live", version: str = "1.2.3", commit: str = COMMIT) -> str:
    return (
        "<html><head>"
        f'<meta name="ml4t-library" content="{library}">'
        f'<meta name="ml4t-version" content="{version}">'
        f'<meta name="ml4t-commit" content="{commit}">'
        "</head></html>"
    )


def test_expected_identity_requires_full_commit() -> None:
    with pytest.raises(ValueError, match="full lowercase hexadecimal SHA"):
        expected_identity("live", "1.2.3", "abc")


def test_html_identity_failures_detect_stale_version() -> None:
    expected = expected_identity("live", "1.2.4", COMMIT)

    failures = html_identity_failures(rendered_page(), expected, "index.html")

    assert failures == ["index.html: ml4t-version is '1.2.3', expected '1.2.4'"]


def test_site_identity_checks_every_page_and_manifest(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(rendered_page())
    nested = tmp_path / "api"
    nested.mkdir()
    (nested / "index.html").write_text(rendered_page())
    (tmp_path / "release.json").write_text(
        json.dumps({"library": "live", "version": "1.2.3", "commit": COMMIT})
    )

    assert site_identity_failures(tmp_path, library="live", version="1.2.3", commit=COMMIT) == []

    (nested / "index.html").write_text(rendered_page(commit="b" * 40))
    failures = site_identity_failures(tmp_path, library="live", version="1.2.3", commit=COMMIT)
    assert any("api/index.html: ml4t-commit" in failure for failure in failures)


@pytest.mark.parametrize("served_version", ["1.2.3", "1.2.4"])
def test_deployed_identity_identifies_client_without_accepting_stale_pages(
    served_version: str,
) -> None:
    requests: list[tuple[str, str]] = []

    class Edge(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            agent = self.headers.get("User-Agent", "")
            requests.append((self.path, agent))
            if not agent or agent.startswith("Python-urllib/"):
                self.send_error(403, "Browser signature rejected")
                return
            body = rendered_page(version=served_version).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Edge) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/docs/live/"
        try:
            failures = deployed_identity_failures(
                base_url, library="live", version="1.2.3", commit=COMMIT, attempts=1
            )
        finally:
            server.shutdown()
            thread.join()

    paths = ["/docs/live/", "/docs/live/getting-started/quickstart/", "/docs/live/api/"]
    expected = (
        []
        if served_version == "1.2.3"
        else [
            f"{base_url}{path.removeprefix('/docs/live/')}: "
            "ml4t-version is '1.2.4', expected '1.2.3'"
            for path in paths
        ]
    )
    assert failures == expected
    assert [path for path, _ in requests] == paths
    assert all(agent.startswith("ml4t-live-release-verifier/") for _, agent in requests)


def test_deployed_identity_default_allows_four_minutes_for_propagation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[str] = []
    sleeps: list[float] = []

    def open_url(request: Request, *, timeout: int) -> BytesIO:
        requests.append(request.full_url)
        assert timeout == 20
        version = "1.2.3" if len(requests) <= 23 * 3 else "1.2.4"
        return BytesIO(rendered_page(version=version).encode())

    monkeypatch.setattr(
        "scripts.qualification.verify_documentation_identity.urllib.request.urlopen", open_url
    )
    monkeypatch.setattr(
        "scripts.qualification.verify_documentation_identity.time.sleep", sleeps.append
    )

    assert (
        deployed_identity_failures(
            "https://example.test/docs/live/",
            library="live",
            version="1.2.4",
            commit=COMMIT,
        )
        == []
    )
    assert len(requests) == 24 * 3
    assert sleeps == [10.0] * 23
