"""Updates from GitHub Releases, against a local fake: version checks, checksum-verified downloads."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from doty import update

PAYLOAD = b"MZ fake installer bytes"


@pytest.fixture
def server():
    state = {"tag": "v0.2.0", "sha": hashlib.sha256(PAYLOAD).hexdigest()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/repos/me/doty/releases/latest":
                body = json.dumps({"tag_name": state["tag"], "html_url": f"{base}/page", "body": "Faster coach.",
                                   "assets": [{"name": "DotySetup-0.2.0.exe", "browser_download_url": f"{base}/exe"},
                                              {"name": "DotySetup-0.2.0.exe.sha256",
                                               "browser_download_url": f"{base}/sha"}]}).encode()
            elif self.path == "/exe":
                body = PAYLOAD
            elif self.path == "/sha":
                body = f"{state['sha']}  DotySetup-0.2.0.exe\n".encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", state
    srv.shutdown()


def test_versions_compare_as_numbers():
    assert update.newer("v0.10.0", "0.9.9") and not update.newer("0.1.0", "0.1.0")
    assert update.parse_version("v1.2.3-beta") == (1, 2, 3)


def test_finds_a_newer_release_and_verifies_the_download(server, tmp_path):
    api, state = server
    rel = update.check("me/doty", current="0.1.0", api=api)
    assert rel.version == "0.2.0" and rel.installer_name == "DotySetup-0.2.0.exe"
    assert update.check("me/doty", current="0.2.0", api=api) is None  # already up to date
    seen = []
    path = update.download(rel, tmp_path, lambda done, total: seen.append(done))
    assert path.read_bytes() == PAYLOAD and seen[-1] == len(PAYLOAD)
    state["sha"] = "0" * 64  # a tampered or broken download is deleted, never run
    with pytest.raises(update.UpdateError):
        update.download(rel, tmp_path)
    assert not (tmp_path / "DotySetup-0.2.0.exe").exists()


def test_no_source_set_is_a_clear_error():
    with pytest.raises(update.UpdateError):
        update.check("")


def test_pre_releases_come_before_their_final_release():
    assert update.newer("v1.2.3", "1.2.3rc2") and update.newer("1.2.3", "v1.2.3-rc.2")
    assert not update.newer("v1.2.3-rc.2", "1.2.3") and update.newer("1.2.3rc3", "1.2.3rc2")
    assert update.newer("0.10.0", "0.9.9") and not update.newer("1.2", "1.2.0")
