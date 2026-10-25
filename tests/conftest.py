"""Garde-fous communs : dossier personnel temporaire (jamais le vrai jeton), aucun réseau, attentes instantanées."""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_gmail import FakeGmail  # noqa: E402

_LOCAL = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("NEXUS_AGYMAIL_HOME", str(home / ".nexus" / "nexus-agymail"))
    monkeypatch.delenv("NEXUS_AGYMAIL_ACCOUNT", raising=False)
    return home


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toute connexion hors boucle locale échoue (asyncio garde sa paire de sockets locale)."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and str(address[0]) in _LOCAL:
            return real_connect(self, address)
        raise OSError("réseau interdit dans les tests")

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if str(host) in _LOCAL:
            return real_getaddrinfo(host, *args, **kwargs)
        raise OSError("réseau interdit dans les tests")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Attentes (relances) enregistrées au lieu d'être subies."""
    recorded: list[float] = []
    monkeypatch.setattr(time, "sleep", recorded.append)
    return recorded


class Clock:
    """Horloge des accords, avancée à la main (expiration)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
def accords(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Any:
    """Registre neuf des accords et des mails lus pour chaque test (rien ne passe d'un test à l'autre)."""
    from nexus_agymail import garde_fous, server

    fresh = garde_fous.Accords(clock=clock)
    monkeypatch.setattr(server, "ACCORDS", fresh)
    return fresh


@pytest.fixture
def state_dir(isolated_home: Path) -> Path:
    path = isolated_home / ".nexus" / "nexus-agymail"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def gmail(monkeypatch: pytest.MonkeyPatch) -> FakeGmail:
    """Fausse API branchée à la place du vrai service (le jeton n'est jamais chargé)."""
    import nexus_agymail.client as client

    fake = FakeGmail()
    service = fake.service()
    monkeypatch.setattr(client, "get_service", lambda *args, **kwargs: service)
    return fake
