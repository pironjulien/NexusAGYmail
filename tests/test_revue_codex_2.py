"""Seconde relecture de Codex (27/10/2026, 0.11.2) : chaque défaut reproduit ici avant d'être corrigé.

1. [P2] Après l'envoi, le brouillon n'est jamais supprimé définitivement : une modification enregistrée entre la
   relecture et la suppression serait perdue (drafts.delete ne passe pas par la corbeille). Il est gardé, et dit.
2. [P2] Issue inconnue (réponse perdue après messages.send) : mémorisée par l'empreinte des octets bruts ; le résumé
   suivant avertit qu'un envoi a peut-être déjà eu lieu, et le retrouve dans Envoyés quand il est parti. Jamais de
   relance automatique.
3. [P3] Journal : un nom d'outil inconnu n'est jamais écrit tel quel (il pourrait contenir une adresse).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_gmail import FakeGmail, stored_draft
from mcp import Client

from nexus_agymail import server


def summary(draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id))


def send(accord: str, draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id, confirm=True, accord=accord))


# --- 1. aucune suppression définitive après l'envoi


def test_modification_enregistree_pendant_le_nettoyage_jamais_perdue(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Version approuvée")
    token = summary()["accord"]
    # Lecture de l'envoi, puis (s'il y en a une) relecture de nettoyage : la modification arrive juste après.
    gmail.after["draft-get"] = lambda: None
    edits = iter([lambda: None, lambda: stored_draft(gmail, "d1", "Version retouchée après l'envoi")])

    def arm() -> None:
        next(edits, lambda: None)()
        gmail.after["draft-get"] = arm

    gmail.after["draft-get"] = arm
    data = send(token)
    assert data["sent"] is True, data
    assert "draft-delete" not in gmail.kinds() and "suppression" not in gmail.kinds()
    assert "d1" in gmail.draft_store  # rien n'est supprimé : la retouche éventuelle reste récupérable
    assert data["draft"] == "kept" and "delete it" in data["note"]


def test_envoi_garde_toujours_le_brouillon(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Bonjour")
    data = send(summary()["accord"])
    assert data["sent"] is True and data["draft"] == "kept" and "d1" in gmail.draft_store
    assert gmail.kinds().count("message-send") == 1 and "draft-delete" not in gmail.kinds()


# --- 2. issue inconnue


def test_reponse_perdue_issue_inconnue_puis_avertie_et_retrouvee(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Facture 42", Cc="carol@example.com")
    token = summary()["accord"]
    gmail.lost_sends = 1  # Gmail envoie, la réponse se perd
    data = send(token)
    assert data["status"] == "unknown" and data["code"] == "send_outcome_unknown" and data["sent"] is None, data
    assert "do not send it again" in data["message"].lower()
    assert len(gmail.sent) == 1 and gmail.kinds().count("message-send") == 1  # jamais relancé
    again = summary()
    # Sans Message-ID dans le brouillon, la ressemblance n'est qu'un doublon possible (0.11.4).
    assert again["code"] == "confirmation" and "possible duplicate" in again["previous_send"]["warning"], again
    assert again["previous_send"]["status"] == "similar_in_sent" and again["previous_send"]["similarId"] == "s1"
    assert gmail.kinds().count("message-send") == 1


def test_echec_ambigu_non_retrouve_reste_averti(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Facture 43")
    token = summary()["accord"]
    gmail.faults["message-send"] = [503]  # rien n'est parti, mais le client ne peut pas le savoir
    data = send(token)
    assert data["code"] == "send_outcome_unknown" and gmail.sent == []
    again = summary()
    assert again["previous_send"]["status"] == "unknown"
    assert "may already have been sent" in again["previous_send"]["warning"]
    assert "Nothing was sent" not in json.dumps(again)
    # L'utilisateur, averti, dit oui à nouveau : l'envoi part et résout SA tentative ; la première, à l'issue
    # toujours inconnue, reste signalée (0.11.5 : une trace par tentative).
    assert send(again["accord"])["sent"] is True
    assert summary()["previous_send"]["status"] in ("unknown", "similar_in_sent")


def test_issue_inconnue_survit_au_redemarrage(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Facture 44")
    token = summary()["accord"]
    gmail.faults["message-send"] = [503]
    send(token)
    from nexus_agymail import garde_fous

    monkeypatch.setattr(server, "ACCORDS", garde_fous.Accords())  # nouveau processus : accords en mémoire perdus
    assert summary()["previous_send"]["status"] == "unknown"


def test_issue_inconnue_liee_aux_octets(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Facture 45")
    token = summary()["accord"]
    gmail.faults["message-send"] = [503]
    send(token)
    stored_draft(gmail, "d1", "Facture 45, corrigée")  # autre contenu : autre envoi
    assert "previous_send" not in summary()


def test_refus_net_n_est_pas_une_issue_inconnue(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    gmail.faults["message-send"] = [400]
    data = send(token)
    assert data["status"] == "error" and data["code"] != "send_outcome_unknown"
    assert "previous_send" not in summary()


# --- 3. journal sans nom d'outil inconnu


def test_nom_d_outil_inconnu_neutralise(gmail, isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEXUS_AGENT", "antigravity")

    async def run() -> Any:
        async with Client(server.mcp, mode="legacy") as client:
            return await client.call_tool("alice@example.com", {})

    anyio.run(run)
    files = list((isolated_home / "journal" / "agymail" / "antigravity").glob("*.jsonl"))
    text = "".join(f.read_text(encoding="utf-8") for f in files)
    assert "alice" not in text
    assert [json.loads(line)["action"] for line in text.splitlines()] == ["unknown"]
