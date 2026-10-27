"""Troisième relecture de Codex (27/10/2026, 0.11.3) : chaque défaut reproduit ici avant d'être corrigé.

1. [P2] Le rapprochement dans Envoyés n'affirme jamais « ce brouillon exact a été envoyé » sur une simple
   ressemblance (objet, To, Cc, texte) : seulement « message semblable, doublon possible ». L'identité n'est affirmée
   que prouvée par le Message-ID des octets bruts.
2. [P2] Une panne de l'état local (disque, verrou) ne masque jamais l'issue chez Gmail (envoyé, ou inconnue) ; une
   trace « tentative en cours » est écrite AVANT messages.send et survit à un arrêt brutal.
3. [P3] Une réponse 2xx illisible après l'envoi est une issue inconnue, pas une erreur interne.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fake_gmail import FakeGmail, stored_draft

from nexus_agymail import client, envois, server


def summary(draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id))


def send(accord: str, draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id, confirm=True, accord=accord))


def old_sent_copy(gmail: FakeGmail, **headers: Any) -> None:
    """Message déjà présent dans Envoyés, semblable au brouillon d1 (même objet, To, Cc, texte) mais pas identique."""
    stored_draft(gmail, "d0", "Facture 42", **headers)
    msg = gmail.draft_store.pop("d0")["message"]
    gmail.add(dict(msg, id="old", labelIds=["SENT"], internalDate=str(int(time.time() * 1000))))


# --- 1. ressemblance n'est pas identité


@pytest.mark.parametrize("difference", [{"attachment": "autre.pdf"}, {"Bcc": "dave@example.com"}])
def test_message_semblable_jamais_affirme_comme_ce_brouillon(gmail: FakeGmail, difference: dict[str, str]):
    old_sent_copy(gmail, Cc="carol@example.com", **difference)
    stored_draft(gmail, "d1", "Facture 42", Cc="carol@example.com", attachment="devis.pdf")
    token = summary()["accord"]
    gmail.faults["message-send"] = [503]  # rien n'est parti cette fois
    assert send(token)["code"] == "send_outcome_unknown"
    previous = summary()["previous_send"]
    assert previous["status"] == "similar_in_sent", previous
    assert "possible duplicate" in previous["warning"] and "was already sent" not in previous["warning"]
    assert previous["similarId"] == "old"


def test_identite_prouvee_par_le_message_id(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Facture 42", Message_ID="<brouillon-42@example.com>")
    token = summary()["accord"]
    gmail.lost_sends = 1  # parti, réponse perdue
    assert send(token)["code"] == "send_outcome_unknown"
    previous = summary()["previous_send"]
    assert previous["status"] == "found_in_sent" and previous["messageId"] == "s1", previous
    assert "was already sent" in previous["warning"]


def test_sans_message_id_jamais_affirme(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Facture 42")
    token = summary()["accord"]
    gmail.lost_sends = 1
    send(token)
    previous = summary()["previous_send"]
    assert previous["status"] == "similar_in_sent" and "was already sent" not in previous["warning"]


# --- 2. l'issue chez Gmail survit à une panne de l'état local


def broken(*_: Any, **__: Any) -> None:
    raise OSError("disque plein")


def test_envoi_reussi_rendu_meme_si_l_etat_ne_s_ecrit_pas(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    monkeypatch.setattr(envois, "resolve", broken)
    data = send(token)
    assert data["status"] == "success" and data["sent"] is True, data
    assert "state_warning" in data and len(gmail.sent) == 1


def test_issue_inconnue_rendue_meme_si_l_etat_ne_s_ecrit_pas(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    calls: list[str] = []
    real = envois.mark_unknown

    def second_fails(*args: Any, **kwargs: Any) -> None:
        calls.append("mark")
        if len(calls) > 1:
            raise OSError("verrou indisponible")
        real(*args, **kwargs)

    monkeypatch.setattr(envois, "mark_unknown", second_fails)
    gmail.lost_sends = 1
    data = send(token)
    assert data["status"] == "unknown" and data["code"] == "send_outcome_unknown", data


def test_trace_ecrite_avant_l_envoi_survit_a_un_arret(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]

    def crash(approved: dict[str, Any], service: Any = None) -> dict[str, Any]:
        raise KeyboardInterrupt  # le serveur s'arrête pendant messages.send

    real = client.send_approved
    monkeypatch.setattr(client, "send_approved", crash)
    with pytest.raises(KeyboardInterrupt):
        send(token)
    monkeypatch.setattr(client, "send_approved", real)  # jamais undo() : il défairait aussi l'isolement des tests
    from nexus_agymail import garde_fous

    monkeypatch.setattr(server, "ACCORDS", garde_fous.Accords())  # redémarrage
    assert summary()["previous_send"]["status"] == "unknown"


def test_trace_impossible_rien_ne_part(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    monkeypatch.setattr(envois, "mark_unknown", broken)
    data = send(token)
    assert data["status"] == "error" and data["sent"] is False and "nothing was sent" in data["error"]
    assert gmail.sent == [] and "message-send" not in gmail.kinds()


def test_refus_net_efface_la_trace(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    gmail.faults["message-send"] = [400]
    assert send(token)["status"] == "error"
    assert "previous_send" not in summary()


# --- 3. réponse 2xx illisible


def test_reponse_2xx_illisible_issue_inconnue(gmail: FakeGmail):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    gmail.garbled_sends = 1
    data = send(token)
    assert data["status"] == "unknown" and data["code"] == "send_outcome_unknown", data
    assert len(gmail.sent) == 1 and gmail.kinds().count("message-send") == 1
