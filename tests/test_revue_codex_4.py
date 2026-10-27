"""Quatrième relecture de Codex (27/10/2026, 0.11.4) : deux tentatives simultanées sur les mêmes octets (deux jetons
valides) ne partagent plus une seule trace. Chaque tentative a la sienne ; un refus net ou un envoi confirmé ne retire
que la sienne, et previous_send reste tant qu'une tentative de ces octets est en cours ou à l'issue inconnue."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fake_gmail import FakeGmail, stored_draft

from nexus_agymail import client, envois, server, suivi


def summary(draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id))


def send(accord: str, draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id, confirm=True, accord=accord))


def test_refus_de_a_n_efface_pas_la_trace_de_b(gmail: FakeGmail, monkeypatch: pytest.MonkeyPatch):
    stored_draft(gmail, "d1", "Facture 42")
    token_a, token_b = summary()["accord"], summary()["accord"]
    real = client.send_approved
    results: dict[str, Any] = {}

    def a_then_b(approved: dict[str, Any], service: Any = None) -> dict[str, Any]:
        # Pendant que A attend Gmail, B (même brouillon, autre jeton) part entièrement et perd sa réponse.
        monkeypatch.setattr(client, "send_approved", real)
        gmail.lost_sends = 1
        results["b"] = send(token_b)
        gmail.faults["message-send"] = [400]  # puis A est refusé net
        return real(approved, service)

    monkeypatch.setattr(client, "send_approved", a_then_b)
    results["a"] = send(token_a)
    assert results["b"]["code"] == "send_outcome_unknown" and results["a"]["status"] == "error"
    previous = summary().get("previous_send")
    assert previous and previous["status"] in ("unknown", "similar_in_sent"), previous


def test_une_trace_par_tentative_resolue_une_a_une(state_dir):
    first = envois.mark_attempt("abc", "d1", 100)
    second = envois.mark_attempt("abc", "d1", 200)
    assert first != second
    assert envois.unknown("abc") == {"attempted_at": 100, "attempts": 2}
    envois.resolve("abc", first)
    assert envois.unknown("abc") == {"attempted_at": 200, "attempts": 1}
    envois.resolve("abc", second)
    assert envois.unknown("abc") == {}


def test_ancien_format_reste_une_tentative_inconnue(state_dir):
    with suivi.locked(envois.FILE):
        suivi.write_json(envois.FILE, {"abc": {"draftId": "d1", "attempted_at": 50}})
    assert envois.unknown("abc") == {"attempted_at": 50, "attempts": 1}
    attempt = envois.mark_attempt("abc", "d1", 60)
    envois.resolve("abc", attempt)
    assert envois.unknown("abc")["attempted_at"] == 50
