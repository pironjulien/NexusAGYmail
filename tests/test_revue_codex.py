"""Relecture de Codex du 27/10/2026 (0.11.1) : chaque défaut reproduit ici avant d'être corrigé.

1. Rôles des destinataires (To, Cc, Bcc), From, Reply-To et objet liés séparément à l'accord.
2. Envoi lié au contenu exact approuvé : un brouillon changé après la dernière relecture ne part pas à sa place.
3. Booléens stricts : seul true envoie (ni 1, ni "yes", ni "on").
4. list_new_messages : aucune perte quand la page est partielle puis que l'historique expire, ni depuis un ancien
   startHistoryId donné explicitement.
5. history.json : transaction verrouillée entre fils et entre processus, fichier temporaire unique.
6. Fausse API : messages.list filtre vraiment `q` (after:/before:) et labelIds.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from typing import Any

import anyio
import pytest
from fake_gmail import FakeGmail, plain_message, stored_draft
from mcp import Client
from mcp.types import ElicitResult

from nexus_agymail import client, server, suivi


def summary(draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id))


def send(accord: str, draft_id: str = "d1") -> dict[str, Any]:
    return json.loads(server.send_draft(draftId=draft_id, confirm=True, accord=accord))


def nothing_sent(gmail: FakeGmail) -> bool:
    return gmail.sent == [] and "message-send" not in gmail.kinds() and "draft-send" not in gmail.kinds()


# --- 1. rôles des destinataires


def test_bcc_passe_en_cc_annule_l_accord(gmail):
    stored_draft(gmail, "d1", "Bonjour Bob", Bcc="carol@example.com")
    token = summary()["accord"]
    stored_draft(gmail, "d1", "Bonjour Bob", Cc="carol@example.com")  # même adresse, rôle visible de tous
    data = send(token)
    assert data["code"] == "accord_refused" and "recipients" in data["reason"], data
    assert nothing_sent(gmail)


def test_to_passe_en_bcc_annule_l_accord(gmail):
    stored_draft(gmail, "d1", "Bonjour", To="bob@example.com", Cc="carol@example.com")
    token = summary()["accord"]
    stored_draft(gmail, "d1", "Bonjour", To="bob@example.com", Bcc="carol@example.com")
    assert send(token)["code"] == "accord_refused" and nothing_sent(gmail)


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"From": "Julien <julien@example.com>"}, {"From": "Julien <autre@example.com>"}),
        ({}, {"Reply_To": "pirate@example.net"}),
        ({"Reply_To": "julien@example.com"}, {"Reply_To": "pirate@example.net"}),
    ],
)
def test_from_et_reply_to_lies_a_l_accord(gmail, before, after):
    stored_draft(gmail, "d1", "Bonjour Bob", **before)
    token = summary()["accord"]
    stored_draft(gmail, "d1", "Bonjour Bob", **after)
    data = send(token)
    assert data["code"] == "accord_refused", data
    assert nothing_sent(gmail)


def test_resume_montre_from_et_reply_to(gmail):
    stored_draft(gmail, "d1", "Bonjour Bob", From="Julien <julien@example.com>", Reply_To="compta@example.com")
    draft = summary()["draft"]
    assert "julien@example.com" in draft["from"] and "compta@example.com" in draft["replyTo"]


# --- 2. course entre la dernière relecture et l'envoi


def test_brouillon_change_juste_apres_la_relecture_le_contenu_approuve_part(gmail):
    stored_draft(gmail, "d1", "Montant : 100 €", attachment="devis.pdf")
    token = summary()["accord"]
    approved = gmail.draft_store["d1"]["message"]["_raw"]
    # Le brouillon change entre la relecture de l'envoi et l'envoi lui-même.
    gmail.after["draft-get"] = lambda: stored_draft(gmail, "d1", "Montant : 9 999 €", To="pirate@example.net")
    data = send(token)
    assert data["status"] == "success" and data["sent"] is True, data
    assert len(gmail.sent) == 1 and gmail.sent[0]["_raw"] == approved  # octet pour octet
    sent = message_from_bytes(gmail.sent[0]["_raw"], policy=default)
    assert "pirate" not in str(sent["To"]) and gmail.sent[0]["threadId"] == "t-d1"
    assert "draft-send" not in gmail.kinds()
    # Le brouillon modifié entre-temps n'est pas supprimé : il reste à l'utilisateur, signalé.
    assert "d1" in gmail.draft_store and data["draft"] == "kept_changed"


def test_envoi_normal_retire_le_brouillon(gmail):
    stored_draft(gmail, "d1", "Bonjour Bob", Bcc="carol@example.com")
    approved = gmail.draft_store["d1"]["message"]["_raw"]
    data = send(summary()["accord"])
    assert data["sent"] is True and data["draft"] == "deleted" and "d1" not in gmail.draft_store
    assert gmail.sent[0]["_raw"] == approved and "Bcc: carol@example.com" in approved.decode()
    assert gmail.kinds().count("message-send") == 1 and gmail.kinds().count("draft-delete") == 1


def test_envoi_d_un_gros_brouillon_par_envoi_reprenable(gmail):
    stored_draft(gmail, "d1", "Ci-joint.", attachment="devis.pdf")
    data = send(summary()["accord"])
    assert data["sent"] is True and gmail.sent[0]["_raw"].count(b"devis.pdf") >= 1
    assert any(kind == "upload-session" and "/messages/send" in uri for kind, uri in gmail.calls)


# --- 3. booléens stricts


@pytest.mark.parametrize("value", [1, "yes", "on", "true", "1"])
def test_fenetre_oui_seulement_avec_true(value):
    assert server._answer(ElicitResult(action="accept", content={"send": value})) == "declined"
    assert server._answer(ElicitResult(action="accept", content={"send": True})) == "yes"


def call_tool(arguments: dict[str, Any]) -> Any:
    async def run() -> Any:
        async with Client(server.mcp, mode="legacy") as mcp_client:
            return await mcp_client.call_tool("send_draft", arguments)

    return anyio.run(run)


@pytest.mark.parametrize("value", [1, "yes", "on", "true"])
def test_confirm_non_booleen_n_envoie_pas(gmail, value):
    stored_draft(gmail, "d1", "Bonjour")
    token = summary()["accord"]
    result = call_tool({"draftId": "d1", "confirm": value, "accord": token})
    assert result.is_error and nothing_sent(gmail)
    assert call_tool({"draftId": "d1", "confirm": True, "accord": token}).is_error is False
    assert len(gmail.sent) == 1


# --- 4. list_new_messages sans perte


class Now:
    def __init__(self) -> None:
        self.value = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> Now:
    clock = Now()
    monkeypatch.setattr(time, "time", clock)
    return clock


def receive(gmail: FakeGmail, n: int) -> str:
    return gmail.receive(plain_message(f"m{n}", f"t{n}", f"Message {n}", subject=f"Sujet {n}"))


def new(**kwargs: Any) -> dict[str, Any]:
    return json.loads(server.list_new_messages(**kwargs))


def test_page_partielle_puis_historique_expire_rien_n_est_perdu(gmail, now):
    new()  # position posée à T0
    now.value += 100
    for n in (1, 2, 3):
        receive(gmail, n)
    now.value += 100
    first = new(pageSize=1)
    assert first["mode"] == "history" and [m["id"] for m in first["messages"]] == ["m1"] and first["more"] is True
    gmail.history_floor = gmail.history_id + 1  # le curseur partiel expire avant la suite
    now.value += 100
    rest = new(pageSize=10)
    assert rest["mode"] == "catch_up", rest
    assert {"m2", "m3"} <= {m["id"] for m in rest["messages"]}, rest


def test_ancien_start_history_id_explicite_repli_par_date(gmail, now):
    start = new()["historyId"]
    now.value += 100
    receive(gmail, 1)
    now.value += 100
    assert [m["id"] for m in new()["messages"]] == ["m1"]
    gmail.history_floor = gmail.history_id + 1
    data = new(startHistoryId=start, advance=False)
    assert data["mode"] == "catch_up" and [m["id"] for m in data["messages"]] == ["m1"], data


def test_position_inconnue_toujours_reset(gmail):
    gmail.history_floor = 500
    assert new(startHistoryId="10")["mode"] == "reset"


# --- 5. history.json sous verrou


def test_transaction_verrouillee_entre_fils(state_dir, monkeypatch):
    real = suivi._read
    barrier = threading.Barrier(2, timeout=0.5)

    def slow_read() -> dict[str, Any]:
        data = real()
        try:  # sans verrou, les deux fils lisent le même état avant d'écrire : une mise à jour est perdue
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return data

    monkeypatch.setattr(suivi, "_read", slow_read)
    errors: list[BaseException] = []

    def save(label: str) -> None:
        try:
            suivi.save("test@example.com", label, "1000", 1)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=save, args=(label,)) for label in ("INBOX", "Label_1")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert suivi.load("test@example.com", "INBOX") and suivi.load("test@example.com", "Label_1")
    assert not list(state_dir.glob("*.tmp"))


def test_transaction_verrouillee_entre_processus(state_dir):
    script = (
        "import sys\n"
        "from nexus_agymail import suivi\n"
        "for n in range(25):\n"
        "    suivi.save('test@example.com', f'{sys.argv[1]}-{n}', str(1000 + n), n)\n"
    )
    src = str(Path(server.__file__).resolve().parents[1])
    env = {**__import__("os").environ, "PYTHONPATH": src}
    processes = [
        subprocess.Popen([sys.executable, "-c", script, f"p{i}"], env=env, stderr=subprocess.PIPE) for i in range(4)
    ]
    for process in processes:
        _, err = process.communicate(timeout=120)
        assert process.returncode == 0, err.decode(errors="replace")
    labels = json.loads((state_dir / suivi.FILE).read_text(encoding="utf-8"))["test@example.com"]["labels"]
    assert len(labels) == 100
    assert not list(state_dir.glob("*.tmp"))


def test_fichiers_temporaires_uniques(state_dir, monkeypatch):
    names: list[str] = []
    real = suivi.os.replace

    def spy(source: Any, target: Any) -> None:
        names.append(str(source))
        real(source, target)

    monkeypatch.setattr(suivi.os, "replace", spy)
    suivi.save("test@example.com", "INBOX", "1", 1)
    suivi.save("test@example.com", "INBOX", "2", 2)
    assert len(set(names)) == 2


# --- 6. la fausse API filtre vraiment


def test_fausse_api_filtre_date_et_libelle(gmail, now):
    old = plain_message("m-old", "t1", "ancien")
    old["internalDate"] = str(int((now.value - 1000) * 1000))
    gmail.add(old)
    receive(gmail, 1)  # reçu maintenant, INBOX
    sent = plain_message("m-sent", "t2", "envoyé")
    sent["labelIds"] = ["SENT"]
    gmail.receive(sent)
    found = client.messages_since(int(now.value) - 10, "INBOX", 20)
    assert found["ids"] == ["m1"]
    assert client.messages_since(int(now.value) - 10, "", 20)["ids"] == ["m-sent", "m1"]
    with pytest.raises(AssertionError):
        gmail._matches(old, {"q": ["from:x"]})


def test_brouillon_brut_sert_de_resume(gmail):
    """Le résumé montré et l'accord viennent des octets mêmes qui partiraient (format raw)."""
    stored_draft(gmail, "d1", "Bonjour Bob")
    summary()
    assert any(kind == "draft-get" and "format=raw" in uri for kind, uri in gmail.calls)
    raw = gmail.draft_store["d1"]["message"]["_raw"]
    assert base64.urlsafe_b64encode(raw)  # garde le brut du faux magasin
