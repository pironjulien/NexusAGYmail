"""Portées : gmail.readonly + gmail.compose + gmail.modify, jamais https://mail.google.com/ (suppression définitive).

Un jeton 0.2.0 sans gmail.modify continue de lire ; il est signalé (« sign in again pour les nouveaux
outils ») et les outils de rangement le disent sans appeler Gmail.
"""

import json

from fake_gmail import NEW_SCOPES, V02_SCOPES, WIDE_SCOPES, fake_token, plain_message, write_legacy_token

from nexus_agymail import auth, server

MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def test_portees_avec_modify_sans_acces_complet():
    assert set(auth.SCOPES) == set(NEW_SCOPES)
    assert MODIFY in auth.SCOPES
    assert "https://mail.google.com/" not in auth.SCOPES


def test_jeton_sans_modify_signale_nouveaux_outils(state_dir):
    write_legacy_token(state_dir, fake_token(V02_SCOPES))
    raw = server.auth_status()
    status = json.loads(raw)
    assert status["status"] == "authenticated"  # la lecture marche toujours
    assert status["reauth_required"] is True
    assert status["scopes"]["missing"] == [MODIFY] and status["scopes"]["excess"] == []
    assert "sign in again" in status["scopes"]["message"]
    assert "labelling and trash tools" in status["scopes"]["message"]
    assert status["scopes"]["blocked_tools"][:2] == ["label_message", "unlabel_message"]
    assert len(status["scopes"]["blocked_tools"]) == 8
    assert "faux-jeton" not in raw and "faux-secret" not in raw  # jamais le jeton


def test_jeton_sans_modify_lit_toujours(state_dir, gmail):
    write_legacy_token(state_dir, fake_token(V02_SCOPES))
    gmail.add(plain_message("m1", "t1", "Bonjour"))
    assert json.loads(server.get_message(messageId="m1"))["status"] == "success"
    assert json.loads(server.list_labels())["status"] == "success"


def test_rangement_bloque_sans_modify_sans_appel(state_dir, gmail):
    write_legacy_token(state_dir, fake_token(V02_SCOPES))
    gmail.add(plain_message("m1", "t1", "Bonjour"))
    for raw in (
        server.unlabel_message(messageId="m1", labelIds=["INBOX"]),
        server.label_thread(threadId="t1", labelIds=["STARRED"]),
        server.trash_message(messageId="m1"),
        server.untrash_thread(threadId="t1"),
    ):
        data = json.loads(raw)
        assert data["status"] == "error" and data["code"] == "scopes", data
        assert "signs in again" in data["error"]
    assert gmail.calls == []
    assert gmail.messages["m1"]["labelIds"] == ["INBOX", "UNREAD"]


def test_auth_status_propre_avec_bonnes_portees(state_dir):
    write_legacy_token(state_dir, fake_token(NEW_SCOPES))
    status = json.loads(server.auth_status())
    assert status["status"] == "authenticated"
    assert status["reauth_required"] is False
    assert status["scopes"]["excess"] == [] and status["scopes"]["missing"] == []
    assert "message" not in status["scopes"]
    assert status["account"] == "test@example.com"


def test_acces_complet_signale_en_trop(state_dir):
    write_legacy_token(state_dir, fake_token(WIDE_SCOPES))
    status = json.loads(server.auth_status())
    assert status["reauth_required"] is True
    assert status["scopes"]["excess"] == ["https://mail.google.com/"]


def test_verification_au_demarrage(state_dir, capsys):
    write_legacy_token(state_dir, fake_token(V02_SCOPES))
    report = server.startup_check()
    assert report["scopes"]["reauth_required"] is True
    assert "gmail.modify" in capsys.readouterr().err
    assert json.loads(server.auth_status())["startup"]["scopes"]["reauth_required"] is True


def test_jeton_ancien_reste_utilisable(state_dir):
    write_legacy_token(state_dir, fake_token(V02_SCOPES))
    creds = auth.get_credentials()
    assert creds.valid
    assert MODIFY not in (creds.scopes or [])


def test_auth_status_sans_jeton(state_dir):
    status = json.loads(server.auth_status())
    assert status["status"] == "unauthenticated"
    assert "nexus-agymail auth" in status["message"]
