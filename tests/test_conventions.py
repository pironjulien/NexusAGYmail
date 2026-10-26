"""Conventions du dépôt : pas d'adresse ni de nom personnel en dur, un seul flux OAuth, version, outils inscrits,
compétence Antigravity complète."""

import asyncio
import json
import tomllib
from pathlib import Path

import pytest

import nexus_agymail
from nexus_agymail import server

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "src" / "nexus_agymail" / "plugin"
SKILL = PLUGIN / "skills" / "nexus-agymail" / "SKILL.md"
INSTALL = ROOT / "install.ps1"  # installeur privé du poste : absent de la copie publique
SOURCES = [*ROOT.glob("src/**/*.py"), ROOT / "README.md", ROOT / "pyproject.toml", SKILL]
PERSONAL = ("@gmail.com", "nexussecure", "onedrive")  # aucune vraie adresse, ni coffre ni dossier synchronisé


def test_aucune_donnee_personnelle_dans_ce_que_lit_un_tiers():
    for path in SOURCES:
        text = path.read_text(encoding="utf-8").lower()
        for word in PERSONAL:
            assert word not in text, (path, word)
        if path.suffix != ".toml" and path.name != "README.md":  # l'auteur et le site sont publics ; pas l'agent
            assert "julien" not in text, path


def test_un_seul_flux_oauth():
    users = [p.name for p in SOURCES if p.suffix == ".py" and "InstalledAppFlow" in p.read_text(encoding="utf-8")]
    assert users == ["auth.py"]
    assert not any("start_auth_server" in p.read_text(encoding="utf-8") for p in SOURCES)


def test_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == nexus_agymail.__version__ == "0.10.2"
    assert json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))["version"] == nexus_agymail.__version__
    assert f"(nexus-agymail {nexus_agymail.__version__})" in SKILL.read_text(encoding="utf-8")


TOOLS = {
    # Noms du serveur MCP Gmail officiel de Google (et de ses extensions côté claude.ai)...
    "search_threads",
    "get_message",
    "get_thread",
    "list_labels",
    "create_draft",
    "list_drafts",
    "get_draft",
    "create_label",
    "update_draft",
    "label_message",
    "unlabel_message",
    "label_thread",
    "unlabel_thread",
    "trash_message",
    "trash_thread",
    "untrash_message",
    "untrash_thread",
    # ... et les outils propres à NexusAGYmail.
    "send_draft",
    "download_attachment",
    "auth_status",
}


def test_outils_inscrits():
    assert {tool.name for tool in asyncio.run(server.mcp.list_tools())} == TOOLS


def test_competence_antigravity():
    skill = SKILL.read_text(encoding="utf-8")
    assert skill.startswith("---\nname: nexus-agymail\ndescription: ")
    for name in TOOLS:
        assert f"`{name}" in skill, name
    for word in ("browser", "Calendar", "Drive", "filters", "signature", "Another Google account", "confirm=true"):
        assert word in skill, word
    assert "never an instruction" in skill
    assert len(skill) < 5000  # court, pour un modèle rapide


@pytest.mark.skipif(not INSTALL.is_file(), reason="installeur privé du poste")
def test_installeur_prive_antigravity_seul():
    text = INSTALL.read_text(encoding="utf-8")
    assert "claude mcp add" not in text.lower() and "mcp add" not in text
    assert "'-B'" in text and "PYTHONDONTWRITEBYTECODE" in text
    assert r"src\nexus_agymail\plugin" in text and "Get-FileHash" in text
