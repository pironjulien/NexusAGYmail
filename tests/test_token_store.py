"""Point 2 : jeton chiffré DPAPI, écriture atomique sous verrou inter-processus, migration, erreurs exactes."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fake_gmail import NEW_SCOPES, fake_token, write_legacy_token

from nexus_agymail import auth, server

# Modules nouveaux importés dans chaque test : sur l'ancien code, chaque test échoue seul (pas d'arrêt de collecte).


def test_migration_chiffre_puis_supprime_token_json(state_dir):
    from nexus_agymail import dpapi

    legacy = write_legacy_token(state_dir, fake_token(NEW_SCOPES))
    auth.get_credentials()
    encrypted = state_dir / "token.bin"
    assert encrypted.exists() and not legacy.exists()
    blob = encrypted.read_bytes()
    assert b"faux-jeton-renouvellement" not in blob and b"refresh_token" not in blob
    assert json.loads(dpapi.unprotect(blob))["refresh_token"] == "faux-jeton-renouvellement"


def test_migration_garde_token_json_si_relecture_echoue(state_dir, monkeypatch):
    from nexus_agymail import dpapi
    from nexus_agymail.token_store import TokenError, TokenStore

    legacy = write_legacy_token(state_dir, fake_token(NEW_SCOPES))

    def refuse(_data: bytes) -> bytes:
        raise dpapi.DpapiError(13, "relecture impossible (simulée)")

    monkeypatch.setattr(dpapi, "unprotect", refuse)
    with pytest.raises(TokenError) as error:
        TokenStore(state_dir).load()
    assert error.value.code == "migration"
    assert legacy.exists()


def test_droits_reserves_a_l_utilisateur(state_dir):
    from nexus_agymail.token_store import TokenStore

    store = TokenStore(state_dir)
    store.save(fake_token(NEW_SCOPES))
    listing = subprocess.run(["icacls", str(store.path)], capture_output=True, check=True).stdout.decode(
        "oem", errors="replace"
    )  # icacls écrit en page OEM
    aces = [line for line in listing.splitlines() if ":(" in line]
    assert len(aces) == 1, listing
    assert os.environ.get("USERNAME", "").lower() in aces[0].lower()
    assert "(I)" not in listing  # aucun droit hérité


def test_ecritures_concurrentes_atomiques(state_dir):
    from nexus_agymail.token_store import TokenStore

    errors: list[BaseException] = []

    def writer(n: int) -> None:
        store = TokenStore(state_dir)
        try:
            for i in range(10):
                info = fake_token(NEW_SCOPES)
                info["token"] = f"faux-{n}-{i}"
                store.save(info)
                assert store.load() is not None
        except BaseException as exc:  # remonté au fil principal
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert TokenStore(state_dir).load() is not None
    assert not list(state_dir.glob("token.bin.*.tmp"))


def test_verrou_inter_processus(state_dir):
    from nexus_agymail.token_store import TokenError, TokenStore

    script = (
        "import sys, time\nfrom pathlib import Path\nfrom nexus_agymail.token_store import TokenStore\n"
        "with TokenStore(Path(sys.argv[1])).lock():\n    print('pris', flush=True)\n    sys.stdin.readline()\n"
    )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    holder = subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(state_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "pris"
        with pytest.raises(TokenError) as error:
            TokenStore(state_dir, lock_timeout=0.3).save(fake_token(NEW_SCOPES))
        assert error.value.code == "locked"
    finally:
        holder.communicate("fin\n", timeout=30)
    TokenStore(state_dir, lock_timeout=5).save(fake_token(NEW_SCOPES))


class _Response:
    def __init__(self, status: int, data: dict) -> None:
        self.status = status
        self.headers = {"content-type": "application/json"}
        self.data = json.dumps(data).encode()


def test_invalid_grant_dit_jeton_revoque(state_dir, monkeypatch):
    write_legacy_token(state_dir, fake_token(NEW_SCOPES, expired=True))
    answer = _Response(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    monkeypatch.setattr(auth, "make_transport", lambda: lambda *args, **kwargs: answer, raising=False)
    status = json.loads(server.auth_status())
    assert status["status"] == "error"
    assert status["code"] == "revoked"
    assert "token revoked or expired: sign in again" in status["error"]
    assert status["reauth_required"] is True


def test_erreur_reseau_dite_reseau(state_dir):
    write_legacy_token(state_dir, fake_token(NEW_SCOPES, expired=True))
    status = json.loads(server.auth_status())  # le garde-fou des tests coupe le réseau
    assert status["status"] == "error"
    assert status["code"] == "network"
    assert status["error"].startswith("network")


def test_renouvellement_reecrit_le_jeton_chiffre(state_dir, monkeypatch):
    from nexus_agymail.token_store import TokenStore

    write_legacy_token(state_dir, fake_token(NEW_SCOPES, expired=True))
    answer = _Response(200, {"access_token": "faux-nouveau", "expires_in": 3600, "scope": " ".join(NEW_SCOPES)})
    monkeypatch.setattr(auth, "make_transport", lambda: lambda *args, **kwargs: answer)
    creds = auth.get_credentials()
    assert creds.token == "faux-nouveau"
    stored = TokenStore(Path(state_dir)).load()
    assert stored is not None and stored["token"] == "faux-nouveau" and stored["account"] == "test@example.com"
