"""État pour l'appli Nexus : ~/.nexus/status/agymail.json, écrit par le serveur à son démarrage.

Contrat commun des produits Nexus : {product, version, latest, state, checked_at, message}. `latest` vient de PyPI
(une requête au plus une fois par jour, en arrière-plan, sans bloquer le serveur) ; sinon la valeur déjà connue est
gardée. Rien à déclencher : la déclaration Antigravity suit @latest, la mise à jour se fait au prochain démarrage.
NEXUS_AGYMAIL_UPDATE_CHECK=0 coupe la requête à PyPI (l'état est écrit quand même, sans `latest` neuf).
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import __version__

PRODUCT = "agymail"
PYPI_URL = "https://pypi.org/pypi/nexus-agymail/json"
CHECK_EVERY = timedelta(days=1)
TIMEOUT_S = 5
CHECK_ENV = "NEXUS_AGYMAIL_UPDATE_CHECK"


def path() -> Path:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / ".nexus" / "status" / f"{PRODUCT}.json"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def latest_from_pypi() -> str:
    request = urllib.request.Request(PYPI_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (URL fixe, https)
        return str(json.load(response)["info"]["version"])


def _message(version: str, latest: str | None) -> str:
    if not latest:
        return "Latest version not checked yet."
    if _key(latest) > _key(version):
        return f"Version {latest} is available: it is installed at the next start of Antigravity."
    return "Up to date."


def report(fetch: bool = True) -> dict[str, Any]:
    """Écrit l'état ; vérifie PyPI si la dernière vérification a plus d'un jour. Ne lève jamais."""
    previous = _read()
    latest = previous.get("latest") if isinstance(previous.get("latest"), str) else None
    checked_at = previous.get("checked_at") if isinstance(previous.get("checked_at"), str) else None
    note = ""
    due = True
    if checked_at:
        try:
            due = _now() - datetime.fromisoformat(checked_at.replace("Z", "+00:00")) >= CHECK_EVERY
        except ValueError:
            due = True
    if fetch and due and os.environ.get(CHECK_ENV, "1") != "0":
        try:
            latest = latest_from_pypi()
            checked_at = _now().isoformat().replace("+00:00", "Z")
        except Exception as exc:  # réseau, PyPI, JSON : l'ancienne valeur reste
            note = f" (check failed: {type(exc).__name__})"
    data = {
        "product": PRODUCT,
        "version": __version__,
        "latest": latest,
        "state": "idle",
        "checked_at": checked_at,
        "message": _message(__version__, latest) + note,
    }
    try:
        _write(data)
    except OSError:
        pass  # l'état est un confort : jamais une raison d'empêcher le serveur de démarrer
    return data


def report_in_background() -> threading.Thread:
    thread = threading.Thread(target=report, name="nexus-agymail-status", daemon=True)
    thread.start()
    return thread
