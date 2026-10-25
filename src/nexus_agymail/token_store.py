"""Stockage du jeton OAuth : chiffré DPAPI, écrit de façon atomique sous verrou inter-processus.

Plusieurs processus (serveurs nexus-agymail, `nexus-agymail auth`) peuvent tourner en même temps : toute
lecture, migration ou réécriture du jeton passe par le verrou de token.lock. L'écriture va dans un fichier temporaire
du même dossier (droits réduits à l'utilisateur), puis os.replace : un lecteur voit l'ancien jeton entier ou le
nouveau, jamais un fichier coupé.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import dpapi

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

LOCK_TIMEOUT_S = 30.0


def _try_lock(fd: int) -> None:
    """Verrou exclusif non bloquant sur le premier octet (OSError si déjà pris)."""
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


class TokenError(Exception):
    """Erreur de jeton avec un code stable (unreadable, corrupt, locked, migration)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TokenStore:
    """Jeton chiffré token.bin dans `directory`, migré automatiquement depuis l'ancien token.json en clair."""

    def __init__(self, directory: Path, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        self.directory = directory
        self.path = directory / "token.bin"
        self.legacy_path = directory / "token.json"
        self.lock_path = directory / "token.lock"
        self.lock_timeout = lock_timeout
        self._local = threading.local()

    # --- verrou inter-processus (réentrant dans un même fil d'exécution)

    @contextmanager
    def lock(self) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        created = not self.lock_path.exists()
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if created:
                dpapi.restrict_to_current_user(self.lock_path)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TokenError(
                            "locked",
                            f"token held by another nexus-agymail server for {self.lock_timeout:.0f} s "
                            f"({self.lock_path}): try again",
                        ) from None
                    time.sleep(0.05)
            self._local.depth = 1
            try:
                yield
            finally:
                self._local.depth = 0
                _unlock(fd)
        finally:
            os.close(fd)

    # --- lecture / écriture

    def describe(self) -> str | None:
        """Où est le jeton, sans le lire."""
        if self.path.exists():
            return "token.bin (DPAPI-encrypted)" if dpapi.ENCRYPTED else "token.bin (readable by your account only)"
        if self.legacy_path.exists():
            return "token.json (plain text, migrated on first load)"
        return None

    def load(self) -> dict[str, Any] | None:
        """Renvoie le jeton (dict) ou None s'il n'y en a pas ; migre token.json si besoin."""
        with self.lock():
            if self.path.exists():
                return self._read()
            if self.legacy_path.exists():
                return self._migrate()
            return None

    def save(self, info: dict[str, Any]) -> None:
        with self.lock():
            self._write(info)

    def _read(self) -> dict[str, Any]:
        try:
            plain = dpapi.unprotect(self.path.read_bytes())
        except dpapi.DpapiError as exc:
            raise TokenError("unreadable", f"unreadable token ({exc}): sign in again") from exc
        return _parse(plain.decode("utf-8"), self.path)

    def _write(self, info: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        blob = dpapi.protect(json.dumps(info, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        tmp = self.directory / f"token.bin.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            with open(tmp, "xb") as handle:
                dpapi.restrict_to_current_user(tmp)
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def _migrate(self) -> dict[str, Any]:
        info = _parse(self.legacy_path.read_text(encoding="utf-8"), self.legacy_path)
        self._write(info)
        try:
            again = self._read()
        except TokenError as exc:
            raise TokenError("migration", f"migration to token.bin not read back ({exc}): token.json kept") from exc
        if again != info:
            raise TokenError("migration", "migration to token.bin read back different: token.json kept")
        self.legacy_path.unlink()
        return info


def _parse(text: str, source: Path) -> dict[str, Any]:
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TokenError("corrupt", f"corrupt token ({source.name}): sign in again") from exc
    if not isinstance(info, dict) or not info.get("refresh_token"):
        raise TokenError("corrupt", f"incomplete token ({source.name}, no refresh_token): sign in again")
    return info
