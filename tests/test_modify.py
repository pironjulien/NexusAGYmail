"""Libellés (label_* / unlabel_*, noms du serveur Gmail officiel) et corbeille : identifiants ou noms, batchModify,
limites, jamais de suppression."""

import json

import pytest
from fake_gmail import plain_message

from nexus_agymail import client, server


def fill(gmail, count: int = 3) -> None:
    for n in range(count):
        gmail.add(plain_message(f"m{n}", f"t{n}", f"message {n}"))


def labels_of(gmail, message_id: str) -> list[str]:
    return gmail.messages[message_id]["labelIds"]


def test_liste_des_libelles(gmail):
    data = json.loads(server.list_labels())
    assert data["status"] == "success"
    names = [label["name"] for label in data["labels"]]
    assert names.index("INBOX") < names.index("Clients/Acme") < names.index("Factures")  # système d'abord
    assert {"id": "Label_1", "name": "Factures", "type": "user"} in data["labels"]


@pytest.mark.parametrize(
    ("tool", "label", "present"),
    [
        ("unlabel_message", "INBOX", False),  # archiver
        ("label_message", "INBOX", True),
        ("unlabel_message", "UNREAD", False),  # lu
        ("label_message", "UNREAD", True),
        ("label_message", "STARRED", True),  # suivi
        ("unlabel_message", "STARRED", False),
        ("label_message", "IMPORTANT", True),
        ("unlabel_message", "IMPORTANT", False),
    ],
)
def test_libelles_systeme(gmail, tool, label, present):
    fill(gmail, 1)
    data = json.loads(getattr(server, tool)(messageId="m0", labelIds=[label]))
    assert data["status"] == "success", data
    assert (label in labels_of(gmail, "m0")) is present


def test_messages_en_un_seul_batch_modify(gmail):
    fill(gmail, 5)
    data = json.loads(server.unlabel_message(messageId=["m0", "m1", "m2", "m3", "m4", "m0"], labelIds=["INBOX"]))
    assert data["messages"] == ["m0", "m1", "m2", "m3", "m4"]  # doublon retiré
    assert gmail.kinds() == ["labels", "batchModify"]
    assert all("INBOX" not in labels_of(gmail, f"m{n}") for n in range(5))


def test_fils_en_requetes_groupees(gmail):
    fill(gmail, 3)
    gmail.add(plain_message("m9", "t0", "réponse"))
    data = json.loads(server.unlabel_thread(threadId=["t0", "t1"], labelIds=["UNREAD"]))
    assert data["status"] == "success" and data["threads"] == ["t0", "t1"]
    assert gmail.kinds().count("batch") == 1 and gmail.kinds().count("lot:thread-modify") == 2
    assert "UNREAD" not in labels_of(gmail, "m9") and "UNREAD" in labels_of(gmail, "m2")


def test_fil_introuvable_signale_sans_cacher_les_autres(gmail):
    fill(gmail, 1)
    data = json.loads(server.label_thread(threadId=["t0", "absent"], labelIds=["STARRED"]))
    assert data["status"] == "partial"
    assert data["threads"] == ["t0"]
    assert data["failed"][0]["id"] == "absent" and data["failed"][0]["code"] == "not_found"


def test_libelles_par_identifiant_ou_nom(gmail):
    fill(gmail, 1)
    data = json.loads(server.label_message(messageId="m0", labelIds=["Label_1", "clients/acme"]))
    assert data["status"] == "success", data
    assert data["addLabelIds"] == ["Label_1", "Label_2"]
    assert "Label_1" in labels_of(gmail, "m0") and "Label_2" in labels_of(gmail, "m0")
    data = json.loads(server.unlabel_message(messageId="m0", labelIds=["Factures"]))
    assert data["removeLabelIds"] == ["Label_1"] and "Label_1" not in labels_of(gmail, "m0")


def test_libelle_inconnu_erreur_claire_sans_rien_toucher(gmail):
    fill(gmail, 1)
    data = json.loads(server.label_message(messageId="m0", labelIds=["Factures", "Inconnu"]))
    assert data["status"] == "error" and data["code"] == "label"
    assert "Inconnu" in data["error"] and "createMissing" in data["error"] and "list_labels" in data["error"]
    assert "batchModify" not in gmail.kinds() and "label-create" not in gmail.kinds()


def test_creation_seulement_avec_create_missing(gmail):
    fill(gmail, 1)
    data = json.loads(server.label_message(messageId="m0", labelIds=["Projets/2026"], createMissing=True))
    assert data["status"] == "success", data
    assert data["created"] == [{"id": "Label_12", "name": "Projets/2026"}]
    assert "Label_12" in labels_of(gmail, "m0")
    removed = json.loads(server.unlabel_message(messageId="m0", labelIds=["Absent"]))
    assert removed["code"] == "label"  # jamais de création pour un retrait
    assert gmail.kinds().count("label-create") == 1


@pytest.mark.parametrize("name", ["TRASH", "spam", "SENT", "DRAFT"])
def test_libelles_reserves_refuses(gmail, name):
    fill(gmail, 1)
    data = json.loads(server.label_message(messageId="m0", labelIds=[name], createMissing=True))
    assert data["code"] == "label" and "reserved" in data["error"]
    assert "batchModify" not in gmail.kinds() and "label-create" not in gmail.kinds()


def test_limite_de_100_elements(gmail):
    data = json.loads(server.unlabel_message(messageId=[f"m{n}" for n in range(101)], labelIds=["INBOX"]))
    assert data["code"] == "invalid_argument" and "100" in data["error"]
    assert json.loads(server.trash_thread(threadId=[f"t{n}" for n in range(101)]))["code"] == "invalid_argument"
    assert gmail.calls == []
    fill(gmail, 1)
    ok = json.loads(server.unlabel_message(messageId=[f"m{n}" for n in range(100)], labelIds=["INBOX"]))
    assert ok["status"] == "success"


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("label_message", {"messageId": [], "labelIds": ["STARRED"]}),
        ("label_message", {"messageId": "m0", "labelIds": []}),
        ("unlabel_thread", {"threadId": "../t0", "labelIds": ["INBOX"]}),
        ("trash_message", {"messageId": ""}),
    ],
)
def test_parametres_invalides(gmail, tool, kwargs):
    data = json.loads(getattr(server, tool)(**kwargs))
    assert data["status"] == "error" and data["code"] == "invalid_argument", data
    assert "batchModify" not in gmail.kinds()


def test_relances_429_et_5xx(gmail, sleeps):
    fill(gmail, 2)
    gmail.faults = {"batchModify": [503], "batch-item": [429]}
    assert json.loads(server.unlabel_message(messageId="m0", labelIds=["INBOX"]))["status"] == "success"
    data = json.loads(server.unlabel_thread(threadId="t1", labelIds=["INBOX"]))
    assert data["status"] == "success", data
    assert "INBOX" not in labels_of(gmail, "m0") and "INBOX" not in labels_of(gmail, "m1")
    assert len(sleeps) >= 2


# --- corbeille


def test_corbeille_et_restauration(gmail):
    fill(gmail, 3)
    data = json.loads(server.trash_message(messageId="m0"))
    assert data["status"] == "success" and data["messages"] == ["m0"]
    assert "30 days" in data["note"] and "untrash_message" in data["note"]
    assert json.loads(server.trash_thread(threadId=["t1"]))["threads"] == ["t1"]
    assert "TRASH" in labels_of(gmail, "m0") and "TRASH" in labels_of(gmail, "m1")
    assert "TRASH" not in labels_of(gmail, "m2")
    back = json.loads(server.untrash_message(messageId=["m0"]))
    assert back["status"] == "success" and back["action"] == "untrash"
    assert json.loads(server.untrash_thread(threadId="t1"))["status"] == "success"
    assert "TRASH" not in labels_of(gmail, "m0") and "TRASH" not in labels_of(gmail, "m1")
    assert "suppression" not in gmail.kinds()


def test_corbeille_introuvable_partielle(gmail):
    fill(gmail, 1)
    data = json.loads(server.trash_message(messageId=["m0", "absent"]))
    assert data["status"] == "partial" and data["messages"] == ["m0"]
    assert data["failed"] == [{"id": "absent", "code": "not_found", "error": data["failed"][0]["error"]}]


def test_jamais_de_suppression_definitive():
    import inspect
    import re

    source = inspect.getsource(client)
    assert not re.search(r"\.(?:delete|batchDelete|emptyTrash)\(|messages\(\)\s*\.send\(", source)
    assert "mail.google.com" not in source
    names = {tool.name for tool in __import__("asyncio").run(server.mcp.list_tools())}
    assert not any("delete" in n or "empty" in n or "spam" in n for n in names)
    assert [n for n in names if "send" in n] == ["send_draft"]


def test_create_label_avec_parents(gmail):
    data = json.loads(server.create_label(displayName="Clients/Acme/2026"))
    assert data["status"] == "success", data
    assert [c["name"] for c in data["created"]] == ["Clients", "Clients/Acme/2026"]  # Clients/Acme existait
    assert data["name"] == "Clients/Acme/2026" and data["id"] == data["created"][-1]["id"]


def test_create_label_sans_parents_et_deja_present(gmail):
    data = json.loads(server.create_label(displayName="Projets/Alpha", autoCreateParentLabels=False))
    assert [c["name"] for c in data["created"]] == ["Projets/Alpha"]
    again = json.loads(server.create_label(displayName="factures"))
    assert again["status"] == "success" and again["created"] == [] and again["id"] == "Label_1"
    assert gmail.kinds().count("label-create") == 1


@pytest.mark.parametrize("name", ["", "  ", "Inbox", "TRASH", "a//b"])
def test_create_label_refuse(gmail, name):
    data = json.loads(server.create_label(displayName=name))
    assert data["status"] == "error", data
    assert "label-create" not in gmail.kinds()
