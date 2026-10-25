"""État pour l'appli Nexus (~/.nexus/status/agymail.json) : contrat commun, PyPI au plus une fois par jour."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from nexus_agymail import __version__, status


@pytest.fixture
def pypi(isolated_home, monkeypatch):
    calls: list[int] = []
    answer = {"version": __version__}

    def fake() -> str:
        calls.append(1)
        if isinstance(answer["version"], Exception):
            raise answer["version"]
        return str(answer["version"])

    monkeypatch.setattr(status, "latest_from_pypi", fake)
    monkeypatch.delenv(status.CHECK_ENV, raising=False)
    return calls, answer


def read() -> dict:
    return json.loads(status.path().read_text(encoding="utf-8"))


def test_contrat_commun_a_jour(pypi, isolated_home):
    data = status.report()
    assert status.path() == isolated_home / ".nexus" / "status" / "agymail.json"
    assert read() == data
    assert set(data) == {"product", "version", "latest", "state", "checked_at", "message"}
    assert data["product"] == "agymail" and data["version"] == __version__ and data["latest"] == __version__
    assert data["state"] == "idle" and data["message"] == "Up to date." and data["checked_at"].endswith("Z")


def test_nouvelle_version_annoncee_pour_le_prochain_demarrage(pypi):
    pypi[1]["version"] = "99.0.0"
    data = status.report()
    assert data["latest"] == "99.0.0" and "next start of Antigravity" in data["message"]


def test_pypi_au_plus_une_fois_par_jour(pypi, monkeypatch):
    calls, _ = pypi
    status.report()
    status.report()
    assert len(calls) == 1
    later = status._now() + timedelta(days=1, minutes=1)
    monkeypatch.setattr(status, "_now", lambda: later)
    status.report()
    assert len(calls) == 2


def test_echec_garde_la_derniere_valeur_connue(pypi, monkeypatch):
    pypi[1]["version"] = "99.0.0"
    first = status.report()
    pypi[1]["version"] = OSError("hors ligne")
    later = status._now() + timedelta(days=2)
    monkeypatch.setattr(status, "_now", lambda: later)
    data = status.report()
    assert data["latest"] == "99.0.0" and data["checked_at"] == first["checked_at"]
    assert "check failed: OSError" in data["message"]


def test_verification_coupee(pypi, monkeypatch):
    monkeypatch.setenv(status.CHECK_ENV, "0")
    data = status.report()
    assert pypi[0] == [] and data["latest"] is None and data["version"] == __version__


def test_jamais_d_exception(pypi, monkeypatch):
    def refuse(_data):
        raise OSError("disque plein")

    monkeypatch.setattr(status, "_write", refuse)
    assert status.report()["product"] == "agymail"  # le serveur démarre quand même
