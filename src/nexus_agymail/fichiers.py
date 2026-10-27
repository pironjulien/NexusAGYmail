"""Pièces jointes côté disque : enregistrer un fichier reçu sans danger, lire un fichier local à joindre.

Enregistrer : nom de fichier venu d'un mail nettoyé (séparateurs, caractères interdits ou invisibles, noms réservés
de Windows, inversion de sens « fdp.exe » → « exe.pdf »), jamais d'écrasement, écriture atomique, et marque « venu
d'Internet » (Zone.Identifier) pour que Windows et Office ouvrent le fichier avec leurs protections habituelles.

Joindre : fichier ordinaire seulement, taille bornée, et jamais depuis un emplacement de secrets (clés SSH, jetons des
agents, dossiers de secrets de l'utilisateur listés dans NEXUS_AGYMAIL_PROTECTED) : un mail lu ne doit pas pouvoir
faire partir un secret, même dans un brouillon.
"""

from __future__ import annotations

import mimetypes
import os
import re
import tempfile
from pathlib import Path

MAX_ATTACH_BYTES = 25 * 1024 * 1024  # limite de Gmail pour les pièces jointes d'un message
MAX_NAME_CHARS = 150
PROTECTED_ENV = "NEXUS_AGYMAIL_PROTECTED"  # dossiers de secrets en plus, séparés par os.pathsep
ZONE_IDENTIFIER = "[ZoneTransfer]\r\nZoneId=3\r\nHostUrl=https://mail.google.com/\r\n"

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)
# Emplacements de secrets (relatifs au dossier personnel) et noms de fichiers de secrets, jamais joints.
_SECRET_DIRS = (
    ".ssh", ".gnupg", ".nexus", ".aws", ".azure", ".kube", ".docker", ".config/gcloud", ".config/gh", ".codex",
    ".gemini", ".claude", ".antigravity", ".cursor", ".vscode-server", ".password-store",
)  # fmt: skip
_SECRET_NAMES = frozenset(
    {
        ".credentials.json", "credentials.json", "client_secret.json", "token.json", "token.bin", ".claude.json",
        ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".dockercfg", "id_rsa", "id_ed25519", "id_ecdsa",
    }
)  # fmt: skip
_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".kdbx", ".keystore", ".jks", ".ppk")
_ENV_EXAMPLES = (".example", ".sample", ".template", ".dist")


def _secret_name(name: str) -> bool:
    """Nom de fichier de secrets : .env et sa famille (.env.local, prod.env... sauf les modèles .env.example),
    clés privées, magasins de mots de passe, jetons et configurations d'agents."""
    lower = name.lower()
    if lower in _SECRET_NAMES or lower.endswith(_SECRET_SUFFIXES):
        return True
    if lower == ".env" or lower.startswith(".env.") or lower.endswith(".env"):
        return not lower.endswith(_ENV_EXAMPLES)
    return False


def default_directory() -> Path:
    return Path.home() / "Downloads" / "NexusAGYmail"


def safe_name(name: str, fallback: str = "attachment") -> str:
    """Nom de fichier Windows sûr tiré d'un nom reçu : sans chemin, sans caractère interdit ni invisible."""
    text = _INVISIBLE.sub("", str(name or ""))
    text = text.replace("/", "\\").rsplit("\\", 1)[-1]
    text = _FORBIDDEN.sub("_", text).strip().rstrip(". ")
    text = text.lstrip(".") or fallback
    if _RESERVED.match(text):
        text = f"_{text}"
    if _units(text) > MAX_NAME_CHARS:
        stem, dot, ext = text.rpartition(".")
        suffix = f".{ext}" if dot and len(ext) <= 16 else ""
        # chaque caractère vaut au moins une unité : borné d'abord, l'ajustement reste court (relecture de Codex)
        head = (stem if suffix else text)[:MAX_NAME_CHARS]
        while head and _units(head) + _units(suffix) > MAX_NAME_CHARS:
            head = head[:-1]
        text = (head.rstrip(". ") or fallback) + suffix
    return text


def _units(text: str) -> int:
    """Longueur comptée comme Windows (unités UTF-16) : un émoji en vaut deux (trouvé par le fuzzing : 128 émojis
    passaient la limite en caractères mais pas CreateFileW)."""
    return len(text.encode("utf-16-le")) // 2


def _free_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = candidate.stem, candidate.suffix
    number = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({number}){suffix}"
        number += 1
    return candidate


def target_directory(directory: str) -> Path:
    """Dossier d'enregistrement : celui donné (chemin absolu), sinon Downloads/NexusAGYmail."""
    if not (directory or "").strip():
        return default_directory()
    path = Path(directory.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("directory: absolute path expected (or empty for Downloads/NexusAGYmail)")
    return path


def save(data: bytes, name: str, directory: Path) -> tuple[Path, bool]:
    """Écrit `data` sous un nom sûr et libre (jamais d'écrasement), de façon atomique. Renvoie (chemin, marque
    Zone.Identifier posée)."""
    directory.mkdir(parents=True, exist_ok=True)
    clean = safe_name(name)
    handle, temp = tempfile.mkstemp(prefix=".nexus-agymail-", suffix=".part", dir=directory)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        while True:
            target = _free_path(directory, clean)
            try:
                os.link(temp, target)  # échoue si le nom vient d'être pris : jamais d'écrasement
                break
            except FileExistsError:
                continue
            except OSError as exc:  # système de fichiers sans liens physiques
                if os.name != "nt":
                    raise ValueError(
                        f"directory: this disk cannot save without risking an overwrite ({exc}); choose another folder"
                    ) from exc
                try:
                    os.rename(temp, target)  # Windows : refuse d'écraser un fichier existant
                    break
                except FileExistsError:
                    continue
    finally:
        Path(temp).unlink(missing_ok=True)
    return target, mark_of_the_web(target)


def mark_of_the_web(path: Path) -> bool:
    """Pose la marque « venu d'Internet » (flux NTFS Zone.Identifier), comme un navigateur. False si impossible."""
    if os.name != "nt":
        return False
    try:
        with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as stream:
            stream.write(ZONE_IDENTIFIER)
        return True
    except OSError:
        return False


def _secret_roots() -> list[Path]:
    home = Path.home()
    roots = [home / d for d in _SECRET_DIRS]
    roots += [Path(p).expanduser() for p in os.environ.get(PROTECTED_ENV, "").split(os.pathsep) if p.strip()]
    if state := os.environ.get("NEXUS_AGYMAIL_HOME"):
        roots.append(Path(state))
    return [r.resolve() for r in roots]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def load(path_text: str) -> tuple[str, str, bytes]:
    """Fichier local à joindre : (nom, type MIME, contenu). ValueError s'il manque, n'est pas un fichier ordinaire,
    est trop gros, ou se trouve dans un emplacement de secrets."""
    raw = (path_text or "").strip().strip('"')
    if not raw:
        raise ValueError("attachments: empty path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"attachments: absolute path expected ({raw!r})")
    real = path.resolve()
    if any(_is_within(real, root) for root in _secret_roots()) or _secret_name(real.name) or _secret_name(path.name):
        raise ValueError(
            f"attachments: {real.name!r} is in a secrets location (vault, keys, tokens): never attached by an "
            "agent; the user can attach it themselves in Gmail"
        )
    if not real.is_file():
        raise ValueError(f"attachments: file not found or not a regular file ({raw!r})")
    size = real.stat().st_size
    if size > MAX_ATTACH_BYTES:
        raise ValueError(f"attachments: {real.name!r} is {size} bytes, more than the Gmail limit (25 MB)")
    mime, encoding = mimetypes.guess_type(real.name)
    if not mime or encoding:
        mime = "application/octet-stream"
    return real.name, mime, real.read_bytes()
