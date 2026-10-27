"""Position du suivi incrémental (« nouveaux messages depuis la dernière fois ») : historyId Gmail et borne de date,
par compte et par libellé, dans le dossier d'état local (history.json, jamais synchronisé).

`checked_at` est une borne de date SÛRE : tout message pas encore listé depuis cette position est arrivé après elle.
Elle n'avance que quand le curseur a rattrapé la boîte (page complète) ; tant qu'il reste une suite (`more`), elle
garde la date d'où vient cette suite, pour que le repli par date (historique expiré) ne saute rien. Les positions
rendues récemment gardent aussi leur borne (`positions`) : un ancien startHistoryId donné explicitement retrouve la
sienne.

Chaque lecture et chaque transaction (lire, modifier, remplacer) se fait sous verrou : verrou de fil, plus verrou de
fichier (history.json.lock) entre processus ; le nouvel état est écrit dans un fichier temporaire au nom unique puis
mis en place par remplacement atomique (os.replace). Un fichier illisible vaut « aucune position » : le relevé suivant
repart de maintenant, sans erreur.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import auth

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

FILE = "history.json"
POSITIONS_KEPT = 34  # positions rendues dont la borne de date est gardée, par libellé (suite de Fibonacci, ≈ φ⁷)
LOCK_TIMEOUT_S = 30.0
_THREAD_LOCK = threading.Lock()


def path() -> Path:
    return auth.get_nexus_dir() / FILE


def _try_lock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _locked() -> Iterator[None]:
    """Section exclusive entre fils et entre processus (verrou du premier octet de history.json.lock)."""
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        fd = os.open(target.with_name(FILE + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{FILE} is locked by another process") from None
                    threading.Event().wait(0.01)  # pas time.sleep : attente réelle même quand il est simulé
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)


def _read() -> dict[str, Any]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _entry(data: dict[str, Any], account: str, label: str) -> dict[str, Any]:
    entry = ((data.get(account.casefold()) or {}).get("labels") or {}).get(label or "*")
    return entry if isinstance(entry, dict) else {}


def load(account: str, label: str) -> dict[str, Any]:
    """{historyId, checked_at, positions} de ce compte et de ce libellé ; historyId et checked_at absents si aucune
    position courante n'est connue."""
    with _locked():
        entry = _entry(_read(), account, label)
    result: dict[str, Any] = {"positions": _positions(entry)}
    if str(entry.get("historyId") or "").isdigit():
        result.update(historyId=str(entry["historyId"]), checked_at=int(entry.get("checked_at") or 0))
    return result


def _positions(entry: dict[str, Any]) -> dict[str, int]:
    positions = entry.get("positions")
    if not isinstance(positions, dict):
        return {}
    kept: dict[str, int] = {}
    for key, value in positions.items():
        try:
            if str(key).isdigit():
                kept[str(key)] = int(value)
        except TypeError, ValueError:
            continue
    return kept


def bound(saved: dict[str, Any], history_id: str) -> int:
    """Borne de date sûre d'une position : la sienne si elle a été rendue récemment, 0 si inconnue."""
    if history_id == saved.get("historyId"):
        return int(saved.get("checked_at") or 0)
    if history_id in (saved.get("positions") or {}):
        return int(saved["positions"][history_id])
    return 0


def save(account: str, label: str, history_id: str, checked_at: int, advance: bool = True) -> None:
    """Retient la borne de date de `history_id` ; avec advance, en fait aussi la position courante. Toute la
    transaction (lire, modifier, remplacer) est verrouillée."""
    with _locked():
        data = _read()
        account_entry = data.get(account.casefold())
        if not isinstance(account_entry, dict):
            account_entry = data[account.casefold()] = {}
        labels = account_entry.get("labels")
        if not isinstance(labels, dict):
            labels = account_entry["labels"] = {}
        entry = _entry(data, account, label)
        positions = _positions(entry)
        positions.pop(str(history_id), None)
        positions[str(history_id)] = int(checked_at)
        positions = dict(list(positions.items())[-POSITIONS_KEPT:])
        current = {k: entry[k] for k in ("historyId", "checked_at") if k in entry}
        if advance:
            current = {"historyId": str(history_id), "checked_at": int(checked_at)}
        labels[label or "*"] = {**current, "positions": positions}
        target = path()
        fd, temporary = tempfile.mkstemp(prefix=f"{FILE}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
