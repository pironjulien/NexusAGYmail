"""Point 6 : recherche de fils (search_threads), pagination, pageSize borné, requêtes groupées, relances 429/5xx."""

import json

from fake_gmail import plain_message

from nexus_agymail import server


def fill(gmail, count: int) -> None:
    for n in range(count):
        gmail.add(plain_message(f"m{n:02d}", f"t{n:02d}", f"message {n}", subject=f"Objet {n}"))


def test_pagination(gmail):
    fill(gmail, 5)
    first = json.loads(server.search_threads(query="", pageSize=2))
    assert first["count"] == 2 and first["nextPageToken"] == "2"
    second = json.loads(server.search_threads(query="", pageSize=2, pageToken=first["nextPageToken"]))
    assert [t["id"] for t in second["threads"]] == ["t02", "t03"]
    last = json.loads(server.search_threads(query="", pageSize=2, pageToken=second["nextPageToken"]))
    assert last["count"] == 1 and last["nextPageToken"] is None


def test_fil_resume_ses_messages_les_plus_recents(gmail):
    for n in range(7):
        gmail.add(plain_message(f"m{n}", "t1", f"message {n}", subject="Fil"))
    thread = json.loads(server.search_threads(query=""))["threads"][0]
    assert thread["id"] == "t1" and thread["messageCount"] == 7
    assert [m["id"] for m in thread["messages"]] == ["m2", "m3", "m4", "m5", "m6"]
    assert "body" not in thread["messages"][0]  # jamais de corps : get_thread pour lire


def test_page_size_borne(gmail):
    fill(gmail, 3)
    assert json.loads(server.search_threads(query="", pageSize=500))["pageSize"] == 50
    assert json.loads(server.search_threads(query="", pageSize=0))["pageSize"] == 1
    assert [p["maxResults"] for p in gmail.list_params] == [["50"], ["1"]]


def test_corbeille_seulement_sur_demande_et_jamais_le_spam(gmail):
    fill(gmail, 1)
    server.search_threads(query="facture")
    server.search_threads(query="facture", includeTrash=True)
    plain, trash = gmail.list_params
    assert "includeSpamTrash" not in plain and plain["q"] == ["facture"]
    assert trash["includeSpamTrash"] == ["true"] and trash["q"] == ["facture -in:spam"]


def test_requetes_groupees(gmail):
    fill(gmail, 12)
    data = json.loads(server.search_threads(query="", pageSize=50))
    assert data["count"] == 12
    kinds = [kind for kind, _ in gmail.calls]
    assert kinds.count("thread-list") == 1 and kinds.count("batch") == 1
    assert kinds.count("thread") == 0  # aucun aller-retour par fil
    assert all("format=metadata" in uri for kind, uri in gmail.calls if kind == "lot:thread")


def test_relance_429_et_5xx(gmail, sleeps):
    fill(gmail, 4)
    gmail.faults = {"thread-list": [429, 503], "batch": [500], "batch-item": [429]}
    data = json.loads(server.search_threads(query="", pageSize=10))
    assert data["status"] == "success", data
    assert data["count"] == 4
    assert len(sleeps) >= 4 and all(s > 0 for s in sleeps)


def test_erreur_404_exacte(gmail):
    data = json.loads(server.get_message(messageId="absent"))
    assert data["status"] == "error" and data["code"] == "not_found"
