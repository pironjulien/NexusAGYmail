"""Position du suivi incrémental (« nouveaux messages depuis la dernière fois ») : historyId Gmail et date du dernier
relevé, par compte et par libellé, dans le dossier d'état local (history.json, jamais synchronisé).

Écrit par remplacement atomique (os.replace) : un lecteur voit l'ancien fichier entier ou le nouveau. Un fichier
illisible vaut « aucune position » : le relevé suivant repart de maintenant, sans erreur.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import auth

FILE = "history.json"


def path() -> Path:
    return auth.get_nexus_dir() / FILE


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load(account: str, label: str) -> dict[str, Any]:
    """{historyId, checked_at} de ce compte et de ce libellé, {} si aucune position connue."""
    entry = ((_read().get(account.casefold()) or {}).get("labels") or {}).get(label or "*")
    if not isinstance(entry, dict) or not str(entry.get("historyId") or "").isdigit():
        return {}
    return {"historyId": str(entry["historyId"]), "checked_at": int(entry.get("checked_at") or 0)}


def save(account: str, label: str, history_id: str, checked_at: int) -> None:
    data = _read()
    labels = data.setdefault(account.casefold(), {}).setdefault("labels", {})
    labels[label or "*"] = {"historyId": str(history_id), "checked_at": int(checked_at)}
    target = path()
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
