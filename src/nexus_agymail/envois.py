"""Tentatives d'envoi en cours ou à l'issue inconnue : la requête messages.send a pu partir sans que sa réponse
revienne (5xx, délai, réseau, arrêt du serveur). Le message a pu partir ou non ; rien n'est jamais relancé
automatiquement.

Une trace PAR TENTATIVE (identifiant unique), écrite avant messages.send et rangée sous l'empreinte SHA-256 des octets
bruts approuvés (envois.json du dossier d'état, verrouillé comme history.json), avec la date de la tentative. Un refus
net ou un envoi confirmé ne retire que la trace de SA tentative : deux tentatives simultanées sur les mêmes octets
(deux jetons valides) ne s'effacent jamais l'une l'autre. Tant qu'il reste une tentative non résolue pour ces octets,
le résumé et la fenêtre de confirmation du même brouillon avertissent qu'un envoi a peut-être déjà eu lieu (et le
cherchent dans Envoyés). Rien d'autre n'est gardé : ni adresse, ni objet, ni corps.
"""

from __future__ import annotations

import secrets
from typing import Any

from . import suivi

FILE = "envois.json"
KEPT = 34  # empreintes gardées au plus (suite de Fibonacci, ≈ φ⁷) ; les plus anciennes sortent d'abord


def _attempts(entry: Any) -> dict[str, dict[str, Any]]:
    """Tentatives d'une empreinte ; l'ancien format (0.11.3-0.11.4 : une seule trace) vaut une tentative."""
    if not isinstance(entry, dict):
        return {}
    if isinstance(entry.get("attempted_at"), int):
        return {"legacy": {"draftId": str(entry.get("draftId") or ""), "attempted_at": entry["attempted_at"]}}
    attempts = entry.get("attempts")
    if not isinstance(attempts, dict):
        return {}
    return {str(k): v for k, v in attempts.items() if isinstance(v, dict) and isinstance(v.get("attempted_at"), int)}


def mark_attempt(raw_sha256: str, draft_id: str, attempted_at: int) -> str:
    """Trace d'une tentative, écrite AVANT messages.send ; rend son identifiant (pour resolve)."""
    attempt = secrets.token_hex(8)
    with suivi.locked(FILE):
        data = suivi.read_json(FILE)
        attempts = _attempts(data.pop(raw_sha256, None))
        attempts[attempt] = {"draftId": str(draft_id), "attempted_at": int(attempted_at)}
        data[raw_sha256] = {"attempts": attempts}
        suivi.write_json(FILE, dict(list(data.items())[-KEPT:]))
    return attempt


def unknown(raw_sha256: str) -> dict[str, Any]:
    """{attempted_at (la plus ancienne), attempts (nombre)} s'il reste une tentative non résolue de ces octets exacts,
    {} sinon."""
    if not raw_sha256:
        return {}
    with suivi.locked(FILE):
        attempts = _attempts(suivi.read_json(FILE).get(raw_sha256))
    if not attempts:
        return {}
    return {"attempted_at": min(a["attempted_at"] for a in attempts.values()), "attempts": len(attempts)}


def resolve(raw_sha256: str, attempt: str) -> None:
    """Issue de CETTE tentative connue (refus net, ou envoi confirmé) : seule sa trace est retirée."""
    with suivi.locked(FILE):
        data = suivi.read_json(FILE)
        attempts = _attempts(data.get(raw_sha256))
        if attempts.pop(attempt, None) is None:
            return
        if attempts:
            data[raw_sha256] = {"attempts": attempts}
        else:
            data.pop(raw_sha256, None)
        suivi.write_json(FILE, data)
