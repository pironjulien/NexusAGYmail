"""Commande nexus-agymail : setup (déclaration Antigravity, contrat du pack Nexus), auth (client OAuth), status."""

from __future__ import annotations

import json

import pytest

from nexus_agymail import cli, register


@pytest.fixture
def gemini(isolated_home):
    root = isolated_home / ".gemini" / "config"
    root.mkdir(parents=True)
    return root


def test_setup_declare_le_plugin_antigravity(gemini, capsys):
    assert cli.main(["setup"]) == 0
    plugin = gemini / "plugins" / "nexus-agymail"
    server = json.loads((plugin / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]["nexus-agymail"]
    assert server["args"] == ["--python", "3.14", "nexus-agymail@latest"]  # dernière version à chaque démarrage
    assert (plugin / "skills" / "nexus-agymail" / "SKILL.md").is_file() and (plugin / "plugin.json").is_file()
    out = capsys.readouterr().out
    assert "Antigravity: plugin" in out and "not signed in yet" in out and "nexus-agymail auth" in out
    assert "NEXUS_PACK_RESULT" not in out
    assert cli.main(["setup"]) == 0
    assert "plugin (already installed)" in capsys.readouterr().out


def test_setup_pack_sans_question_avec_ligne_de_synthese(gemini, capsys, monkeypatch):
    monkeypatch.setenv("NEXUS_PACK", "1")
    assert cli.main(["setup"]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last == "NEXUS_PACK_RESULT Antigravity=plugin; sign-in=to do"


def test_setup_garde_un_plugin_ecrit_par_l_utilisateur(gemini, capsys, monkeypatch):
    mine = gemini / "plugins" / "nexus-agymail" / "mcp_config.json"
    mine.parent.mkdir(parents=True)
    own = {"mcpServers": {"nexus-agymail": {"command": "python.exe", "args": ["-m", "nexus_agymail"]}}}
    mine.write_text(json.dumps(own), encoding="utf-8")
    monkeypatch.setenv("NEXUS_PACK", "1")
    assert cli.main(["setup"]) == 0
    assert json.loads(mine.read_text(encoding="utf-8")) == own
    assert capsys.readouterr().out.strip().endswith("Antigravity=kept; sign-in=to do")


def test_setup_retire_une_ancienne_entree_globale_a_nous(gemini):
    global_mcp = gemini / "mcp_config.json"
    entry = {"command": "uvx", "args": ["--python", "3.14", "nexus-agymail==0.7.0"]}
    other = {"command": "node", "args": ["autre.js"]}
    global_mcp.write_text(json.dumps({"mcpServers": {"nexus-agymail": entry, "autre": other}}), encoding="utf-8")
    assert cli.main(["setup"]) == 0
    assert json.loads(global_mcp.read_text(encoding="utf-8")) == {"mcpServers": {"autre": other}}
    assert list(register.backups().glob("config-mcp_config.json-*"))


def test_setup_sans_antigravity(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("NEXUS_PACK", "1")
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "Antigravity not found" in out and out.strip().endswith("NEXUS_PACK_RESULT sign-in=to do")


def test_auth_refuse_un_client_qui_n_est_pas_de_bureau(tmp_path, state_dir, capsys):
    web = tmp_path / "client_secret_web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    assert cli.main(["auth", "--client-secret", str(web)]) == 1
    assert "Desktop app" in capsys.readouterr().out


def test_auth_copie_le_client_puis_lance_la_connexion(tmp_path, state_dir, monkeypatch):
    desktop = tmp_path / "client_secret_desktop.json"
    desktop.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
    calls = []

    def fake_flow(open_browser=True, login_hint=""):
        calls.append(open_browser)
        scopes = {"reauth_required": False}
        return {"account": "alice@example.com", "scopes": scopes, "path": str(state_dir / "token.bin")}

    monkeypatch.setattr(cli.auth, "authenticate_interactive", fake_flow)
    assert cli.main(["auth", "--client-secret", str(desktop), "--no-browser"]) == 0
    assert calls == [False]
    assert json.loads((state_dir / "client_secret.json").read_text(encoding="utf-8"))["installed"]["client_id"] == "x"


def test_status_sans_jeton(isolated_home, capsys):
    assert cli.main(["status"]) == 1
    assert json.loads(capsys.readouterr().out)["present"] is False
