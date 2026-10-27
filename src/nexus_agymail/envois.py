"""Envois à l'issue inconnue : la requête messages.send est partie mais sa réponse n'est pas revenue (5xx, délai,
réseau). Le message a pu partir ou non ; rien n'est jamais relancé automatiquement.

Chaque cas est retenu par l'empreinte SHA-256 des octets bruts approuvés (envois.json du dossier d'état, verrouillé
comme history.json), avec la date de la tentative : tant qu'il n'est pas résolu par un envoi confirmé de ces mêmes
octets, le résumé et la fenêtre de confirmation du même brouillon avertissent qu'un envoi a peut-être déjà eu lieu
(et le cherchent dans Envoyés). Rien d'autre n'est gardé : ni adresse, ni objet, ni corps.
"""

from __future__ import annotations

from typing import Any

from . import suivi

FILE = "envois.json"
KEPT = 34  # cas gardés au plus (suite de Fibonacci, ≈ φ⁷) ; les plus anciens sortent d'abord


def mark_unknown(raw_sha256: str, draft_id: str, attempted_at: int) -> bool:
    """Trace écrite AVANT messages.send (« tentative en cours ») : elle reste si l'issue est inconnue ou si le
    serveur s'arrête pendant l'envoi. Une trace plus ancienne des mêmes octets est gardée telle quelle (date de la
    première tentative). Rend True si la trace est nouvelle."""
    with suivi.locked(FILE):
        data = suivi.read_json(FILE)
        if isinstance(data.get(raw_sha256), dict):
            return False
        data[raw_sha256] = {"draftId": str(draft_id), "attempted_at": int(attempted_at)}
        suivi.write_json(FILE, dict(list(data.items())[-KEPT:]))
    return True


def unknown(raw_sha256: str) -> dict[str, Any]:
    """{draftId, attempted_at} si un envoi de ces octets exacts a une issue inconnue, {} sinon."""
    if not raw_sha256:
        return {}
    with suivi.locked(FILE):
        entry = suivi.read_json(FILE).get(raw_sha256)
    return entry if isinstance(entry, dict) and isinstance(entry.get("attempted_at"), int) else {}


def resolve(raw_sha256: str) -> None:
    """Envoi de ces octets confirmé par Gmail : l'avertissement n'a plus lieu d'être."""
    with suivi.locked(FILE):
        data = suivi.read_json(FILE)
        if data.pop(raw_sha256, None) is not None:
            suivi.write_json(FILE, data)
