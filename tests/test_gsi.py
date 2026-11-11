from aiohttp.test_utils import TestClient, TestServer

from doty.config import Config
from doty.gsi import install
from doty.gsi.server import GsiServer


async def test_server_accepts_token_and_rejects_others():
    got = []
    server = GsiServer("127.0.0.1", 0, "secret", lambda p, t: got.append(p))
    async with TestClient(TestServer(server.build_app())) as client:
        ok = await client.post("/", json={"auth": {"token": "secret"}, "map": {"clock_time": 5}})
        bad = await client.post("/", json={"auth": {"token": "nope"}})
        junk = await client.post("/", data=b"not json", headers={"Content-Type": "application/json"})
        health = await (await client.get("/health")).json()
    assert ok.status == 200 and bad.status == 401 and junk.status == 400
    assert len(got) == 1 and got[0]["map"]["clock_time"] == 5
    assert health["received"] == 1 and health["rejected"] == 1


def test_render_cfg_is_valid_keyvalues():
    cfg = Config()
    text = install.render_cfg(cfg)
    assert text.startswith('"Doty"')
    assert f'"uri"           "http://127.0.0.1:{cfg.gsi.port}/"' in text
    assert f'"token"     "{cfg.gsi.auth_token}"' in text
    assert text.count("{") == text.count("}")
    for section in ("provider", "map", "player", "hero", "abilities", "items", "events"):
        assert f'"{section}"' in text


def test_install_writes_into_dota_tree(tmp_path):
    dota = tmp_path / "dota 2 beta"
    (dota / "game" / "dota").mkdir(parents=True)
    path = install.install(Config(), dota)
    assert path.name == install.CFG_NAME and path.is_file()
    assert install.uninstall(dota) == path and not path.exists()


def test_find_dota_prefers_the_real_install_over_a_leftover_folder(tmp_path):
    from doty.gsi import install

    steam, lib = tmp_path / "Steam", tmp_path / "SteamLibrary"
    leftover = steam / "steamapps" / "common" / "dota 2 beta"
    real = lib / "steamapps" / "common" / "dota 2 beta"
    (leftover / "game" / "dota" / "cfg").mkdir(parents=True)  # old cfg files, no game
    (real / "game" / "dota").mkdir(parents=True)
    (real / "game" / "bin" / "win64").mkdir(parents=True)
    (real / "game" / "bin" / "win64" / "dota2.exe").write_bytes(b"")
    (lib / "steamapps" / "appmanifest_570.acf").write_text('"AppState" {}')
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders" { "0" { "path" "%s" } "1" { "path" "%s" } }'
        % (str(steam).replace("\\", "\\\\"), str(lib).replace("\\", "\\\\")))
    assert install.dota_installs(steam) == [real, leftover]
