"""Journal commun (FORMAT.md, produit « agymail ») : une ligne par appel d'outil, issue et code d'erreur stable, jamais
d'adresse, d'objet, de corps ni d'identifiant ; rien quand l'IA est inconnue ou que le journal est coupé."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_gmail import plain_message, stored_draft
from mcp import Client

from nexus_agymail import journal, server


def call(name: str, arguments: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp, mode="legacy") as client:
            return await client.call_tool(name, arguments)

    return anyio.run(run)


def lignes(home: Path, agent: str = "antigravity") -> list[dict[str, Any]]:
    fichiers = list((home / "journal" / "agymail" / agent).glob("*.jsonl"))
    return [json.loads(line) for f in fichiers for line in f.read_text(encoding="utf-8").splitlines()]


def test_une_ligne_par_appel_sans_contenu(gmail, isolated_home, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_AGENT", "antigravity")
    gmail.add(plain_message("m1", "t1", "Corps secret", subject="Objet secret"))
    call("get_message", {"messageId": "m1"})
    call("get_message", {"messageId": "absent"})
    faites = lignes(isolated_home)
    assert [(x["action"], x["issue"], x["erreur"]) for x in faites] == [
        ("get_message", "✓", None),
        ("get_message", "✗", "not_found"),
    ]
    assert all(x["app"] == "gmail" and isinstance(x["ms"], int) and x["outil"] == server.__version__ for x in faites)
    texte = json.dumps(faites)
    assert "secret" not in texte and "m1" not in texte and "absent" not in texte


def test_envoi_sans_accord_compte_comme_refus(gmail, isolated_home, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_AGENT", "antigravity")
    stored_draft(gmail, "d1", "Brouillon")
    call("send_draft", {"draftId": "d1"})
    assert [(x["action"], x["issue"]) for x in lignes(isolated_home)] == [("send_draft", "refus")]


def test_ia_inconnue_ou_journal_coupe(gmail, isolated_home, monkeypatch: pytest.MonkeyPatch):
    call("list_labels", {})
    monkeypatch.setenv("NEXUS_AGENT", "antigravity")
    monkeypatch.setenv("NEXUS_JOURNAL", "0")
    call("list_labels", {})
    assert not (isolated_home / "journal").exists()


def test_codes_libres_ecartes():
    assert journal.issue_et_cause("error", "quota") == ("✗", "quota")
    assert journal.issue_et_cause("error", "Adresse alice@example.com") == ("✗", "error")
    assert journal.issue_et_cause("unauthenticated", None) == ("✗", "unauthenticated")
    assert journal.issue_et_cause("success", None) == ("✓", None)
