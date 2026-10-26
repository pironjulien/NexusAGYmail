"""Assistant de première connexion : pages Google ouvertes dans l'ordre, client téléchargé détecté tout seul,
vérifications après la connexion (API Gmail coupée, appli restée en mode Test), sans client OAuth partagé."""

from __future__ import annotations

import json
import time

import pytest

from nexus_agymail import auth, guide

DESKTOP = {"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "y", "project_id": "nexus-1"}}
OK = {"account": "alice@example.com", "scopes": {"reauth_required": False}, "path": "token.bin"}


class FakeIo(guide.Io):
    def __init__(self, on_open=None) -> None:
        super().__init__()
        self.opened: list[str] = []
        self.lines: list[str] = []
        self.asked = 0
        self.on_open = on_open
        self.say = self.lines.append
        self.sleep = lambda _s: None

        def ask(_prompt: str) -> str:
            self.asked += 1
            return ""

        def open_url(url: str) -> bool:
            self.opened.append(url)
            if self.on_open:
                self.on_open(url)
            return True

        self.ask = ask
        self.open = open_url

    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def flows(monkeypatch, state_dir):
    """Résultats successifs de la connexion Google (faux navigateur)."""
    results: list[dict] = []
    calls: list[bool] = []

    def fake(open_browser=True):
        calls.append(open_browser)
        return results.pop(0)

    monkeypatch.setattr(auth, "authenticate_interactive", fake)
    return results, calls


def drop_client(folder, name="client_secret_123-abc.apps.googleusercontent.com.json", data=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(data or DESKTOP), encoding="utf-8")
    return path


def test_parcours_complet_pages_dans_l_ordre_et_json_detecte(isolated_home, state_dir, flows):
    results, _ = flows
    results.append(dict(OK))
    downloads = isolated_home / "Downloads"

    def on_open(url):
        if url == guide.CLIENT_CREATE:  # l'utilisateur crée le client puis clique « Download JSON »
            drop_client(downloads)

    io = FakeIo(on_open)
    assert guide.run(io) == 0
    assert io.opened == [guide.PROJECT_CREATE, guide.ENABLE_GMAIL, guide.BRANDING, guide.AUDIENCE, guide.CLIENT_CREATE]
    assert json.loads((state_dir / "client_secret.json").read_text(encoding="utf-8")) == DESKTOP
    assert "Publish app" in io.text() and "hasn't verified this app" in io.text()
    assert "signed in as alice@example.com" in io.text() and "Restart Antigravity" in io.text()


def test_un_vieux_json_ou_un_client_web_ne_compte_pas(isolated_home, state_dir, flows, monkeypatch):
    downloads = isolated_home / "Downloads"
    old = drop_client(downloads, "client_secret_old.json")
    past = time.time() - 3600
    import os

    os.utime(old, (past, past))
    drop_client(downloads, "client_secret_web.json", {"web": {"client_id": "w"}})
    monkeypatch.setattr(guide, "WAIT_S", 0)
    io = FakeIo()
    assert guide.wait_for_client(io, since=time.time() - 5, timeout=0) is None


def test_api_gmail_coupee_puis_activee(state_dir, flows, tmp_path):
    results, calls = flows
    results += [{**OK, "gmail_api_disabled": True, "profile_error": "accessNotConfigured"}, dict(OK)]
    io = FakeIo()
    assert guide.run(io, client_secret=str(drop_client(tmp_path))) == 0
    assert io.opened == [guide.ENABLE_GMAIL + "&project=nexus-1"] and len(calls) == 2


def test_appli_restee_en_mode_test_puis_publiee(state_dir, flows, tmp_path):
    results, calls = flows
    results += [{**OK, "testing_mode": True, "refresh_token_expires_in": 604799}, dict(OK)]
    io = FakeIo()
    assert guide.run(io, client_secret=str(drop_client(tmp_path))) == 0
    assert io.opened == [guide.AUDIENCE + "?project=nexus-1"] and "Testing" in io.text() and len(calls) == 2


def test_echecs_repetes_proposent_le_serveur_officiel(state_dir, flows, tmp_path):
    results, _ = flows
    results += [{**OK, "testing_mode": True}] * 3
    io = FakeIo()
    assert guide.run(io, client_secret=str(drop_client(tmp_path))) == 1
    assert guide.FALLBACK in io.text()


def test_client_deja_present_aucune_page_ouverte(state_dir, flows):
    results, _ = flows
    results.append(dict(OK))
    (state_dir / "client_secret.json").write_text(json.dumps(DESKTOP), encoding="utf-8")
    io = FakeIo()
    assert guide.run(io) == 0 and io.opened == []


def test_mode_test_detecte_par_la_duree_du_jeton():
    assert auth._short_lived(604799) and auth._short_lived("604799")
    assert not auth._short_lived(None) and not auth._short_lived("x") and not auth._short_lived(10**9)


def test_jamais_interactif_dans_le_pack(monkeypatch):
    monkeypatch.setenv("NEXUS_PACK", "1")
    assert guide.interactive() is False
