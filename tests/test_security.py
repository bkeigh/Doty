"""The security pass: who may talk to the game-data listener, where API keys are kept on Windows, and what
the updater will agree to run."""

import hashlib
import sys
import uuid
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from doty import authenticode, keystore, update
from doty.gsi.server import GsiServer, from_a_browser, host_name

OK_BODY = {"auth": {"token": "secret"}, "map": {"clock_time": 5}}


async def test_the_game_data_listener_refuses_web_pages_and_other_host_names():
    got = []
    server = GsiServer("127.0.0.1", 0, "secret", lambda p, t: got.append(p))
    async with TestClient(TestServer(server.build_app())) as client:
        assert (await client.post("/", json=OK_BODY)).status == 200  # Dota: no Origin, Host is loopback
        # A page in the player's browser, knowing the token (the default is public):
        page = await client.post("/", json=OK_BODY, headers={"Origin": "https://evil.example"})
        simple = await client.post("/", data=b'{"auth": {"token": "secret"}}',
                                   headers={"Content-Type": "text/plain", "Sec-Fetch-Site": "cross-site"})
        # DNS rebinding: the page's own name now points at 127.0.0.1, so the browser calls it same-site.
        rebound = await client.post("/", json=OK_BODY, headers={"Host": "evil.example:53000"})
        health = await client.get("/health", headers={"Origin": "https://evil.example"})
        assert [page.status, simple.status, rebound.status, health.status] == [403, 403, 403, 403]
        assert (await client.post("/", json=OK_BODY, headers={"Host": "localhost:53000"})).status == 200
        assert (await client.get("/health")).status == 200
    assert len(got) == 2 and server.rejected == 4 and server.received == 2


async def test_a_listener_opened_to_the_network_on_purpose_still_refuses_browsers():
    server = GsiServer("0.0.0.0", 0, "secret", lambda p, t: None)  # noqa: S104 (the player set this host)
    async with TestClient(TestServer(server.build_app())) as client:
        assert (await client.post("/", json=OK_BODY, headers={"Host": "gaming-pc:53000"})).status == 200
        assert (await client.post("/", json=OK_BODY, headers={"Origin": "http://gaming-pc"})).status == 403


def test_host_and_browser_helpers():
    assert host_name("127.0.0.1:53000") == "127.0.0.1" and host_name("LOCALHOST") == "localhost"
    assert host_name("[::1]:53000") == "[::1]" and host_name("") == ""
    assert from_a_browser({"sec-fetch-mode": "no-cors"}) and not from_a_browser({"User-Agent": "Valve/Steam"})


class FakeCred:
    """win32cred's shape, in memory."""

    CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2

    def __init__(self):
        self.vault = {}

    def CredWrite(self, cred, flags):  # noqa: N802
        self.vault[cred["TargetName"]] = cred["CredentialBlob"].encode("utf-16-le")

    def CredRead(self, target, kind, flags):  # noqa: N802
        return {"CredentialBlob": self.vault[target]}  # KeyError when missing, as CredRead raises

    def CredDelete(self, target, kind, flags):  # noqa: N802
        del self.vault[target]


def test_windows_keeps_api_keys_in_credential_manager_not_the_environment(tmp_path):
    cred, path = FakeCred(), tmp_path / "keys.env"
    assert keystore.save("ANTHROPIC_API_KEY", " sk-ant-new ", cred=cred, path=path) == "credential manager"
    assert list(cred.vault) == ["Doty/ANTHROPIC_API_KEY"] and not path.exists()
    # An older Doty left a key in the user environment: the one saved in Settings since wins.
    env = {"ANTHROPIC_API_KEY": "sk-ant-old", "OPENAI_API_KEY": "from-the-environment"}
    assert keystore.load_into_environ(env, ring=False, path=path, cred=cred) == ["ANTHROPIC_API_KEY"]
    assert env == {"ANTHROPIC_API_KEY": "sk-ant-new", "OPENAI_API_KEY": "from-the-environment"}
    keystore.save("ANTHROPIC_API_KEY", "", cred=cred, path=path)  # forgetting it
    keystore.save("ANTHROPIC_API_KEY", "", cred=cred, path=path)  # ...twice is fine
    assert cred.vault == {} and keystore.load_into_environ({}, ring=False, path=path, cred=cred) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_a_key_round_trips_through_the_real_credential_manager(real_wincred):
    name = f"DOTY_TEST_{uuid.uuid4().hex[:12]}"  # a throwaway entry, removed again below
    try:
        keystore._cred_write(real_wincred, name, "sk-test-ключ-123")
        assert keystore._cred_read(real_wincred, name) == "sk-test-ключ-123"
    finally:
        keystore._cred_write(real_wincred, name, "")
    assert keystore._cred_read(real_wincred, name) == ""


def test_settings_save_keys_through_the_key_store(monkeypatch):
    from doty.ui import controller

    saved = []
    monkeypatch.setattr(keystore, "save", lambda name, key: saved.append((name, key)))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    controller.save_api_key(" g-key ", "GEMINI_API_KEY")
    assert saved == [("GEMINI_API_KEY", "g-key")]
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _release(tmp_path, monkeypatch, payload=b"MZ pretend installer"):
    exe = tmp_path / "src" / "DotySetup-9.9.9.exe"
    exe.parent.mkdir()
    exe.write_bytes(payload)
    sha = tmp_path / "src" / "sha"
    sha.write_text(hashlib.sha256(payload).hexdigest() + "  DotySetup-9.9.9.exe\n", encoding="utf-8")
    return update.Release("9.9.9", "", "", exe.as_uri(), exe.name, sha.as_uri())


def test_a_signed_doty_only_installs_an_update_from_the_same_publisher(tmp_path, monkeypatch):
    rel, out = _release(tmp_path, monkeypatch), tmp_path / "updates"
    signed_by = {"name": ""}
    monkeypatch.setattr(authenticode, "publisher", lambda path: signed_by["name"])
    # Unsigned Doty (today): the checksum is all there is.
    assert update.download(rel, out, publisher="").is_file()
    # Signed Doty, unsigned update: refused and deleted.
    with pytest.raises(update.UpdateError, match="isn't signed"):
        update.download(rel, out, publisher="Doty Publisher")
    assert not (out / rel.installer_name).exists()
    # Signed by someone else: refused, and the message says who.
    signed_by["name"] = "Somebody Else LLC"
    with pytest.raises(update.UpdateError, match="Somebody Else LLC"):
        update.download(rel, out, publisher="Doty Publisher")
    assert not (out / rel.installer_name).exists()
    signed_by["name"] = "Doty Publisher"
    assert update.download(rel, out, publisher="Doty Publisher").is_file()


def test_running_from_source_asks_no_publisher(monkeypatch):
    monkeypatch.setattr(authenticode, "publisher", lambda path: "Python Software Foundation")
    assert update.own_publisher() == ""  # python.exe's signature isn't Doty's
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert update.own_publisher() == "Python Software Foundation"


def test_an_absurdly_large_download_is_dropped(tmp_path, monkeypatch):
    rel = _release(tmp_path, monkeypatch, payload=b"x" * 300_000)
    monkeypatch.setattr(update, "MAX_INSTALLER_BYTES", 100_000)
    with pytest.raises(update.UpdateError, match="bigger"):
        update.download(rel, tmp_path / "updates", publisher="")
    assert not (tmp_path / "updates" / rel.installer_name).exists()


def test_signature_check_on_real_files(tmp_path):
    plain = tmp_path / "not-signed.exe"
    plain.write_bytes(b"MZ" + b"\0" * 200)
    assert authenticode.publisher(plain) == ""
    assert authenticode.publisher(tmp_path / "missing.exe") == "" and authenticode.publisher(tmp_path) == ""
    python = Path(sys.base_prefix) / "python.exe"
    if sys.platform == "win32" and python.is_file():
        name = authenticode.publisher(python)
        if name:  # python.org builds are signed; a self-built or Store Python needn't be
            assert "Python" in name
