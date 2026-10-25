"""Déclaration du serveur chez Antigravity (`nexus-agymail setup`, pack Nexus), comme NexusAndroid : le plugin
~/.gemini/config/plugins/nexus-agymail (compétence et serveur, entrée stdio `uvx --python 3.14 nexus-agymail@latest` :
la dernière version au démarrage d'Antigravity),
jamais d'entrée dans le mcp_config.json global (une ancienne entrée à nous y est retirée).

Claude et Codex ont leur connecteur Gmail officiel : rien n'y est déclaré (le README donne la configuration à la main).
Une entrée à nous (uvx … nexus-agymail) est remplacée ; un plugin écrit autrement par l'utilisateur est gardé.
Chaque fichier est sauvegardé dans ~/.nexus/nexus-agymail/backups puis écrit de façon atomique."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

NAME = "nexus-agymail"
PLUGIN_SOURCE = Path(__file__).resolve().parent / "plugin"


def home() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def backups() -> Path:
    return home() / ".nexus" / NAME / "backups"


def uvx() -> str:
    """Chemin absolu de uvx : un client déjà ouvert garde l'ancien PATH (uv installé après lui)."""
    return shutil.which("uvx") or "uvx"


def entry() -> dict:
    # @latest : uvx reprend la dernière version publiée à chaque démarrage d'Antigravity (mise à jour sans installeur).
    return {"command": uvx(), "args": ["--python", "3.14", f"{NAME}@latest"]}


def ours(existing: object) -> bool:
    """Entrée écrite par cet installeur (uvx … nexus-agymail, ==x ou @latest), ou aucune."""
    if existing is None:
        return True
    if not isinstance(existing, dict):
        return False
    command = Path(str(existing.get("command", ""))).name.lower()
    args = [str(a) for a in existing.get("args", [])]
    return command in {"uvx", "uvx.exe"} and any(re.fullmatch(rf"{NAME}(==[\w.]+|@latest)?", a) for a in args)


def _backup(path: Path) -> None:
    if path.exists():
        folder = backups()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, folder / f"{path.parent.name}-{path.name}-{stamp}")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".nexus-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() and path.stat().st_size else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def antigravity() -> str | None:
    """État de la déclaration chez Antigravity ; None si Antigravity est absent."""
    gemini = home() / ".gemini"
    if not (gemini / "antigravity").is_dir() and not (gemini / "config").is_dir():
        return None
    root = gemini / "config"
    target = root / "plugins" / NAME
    mcp_file = target / "mcp_config.json"
    current = _read_json(mcp_file).get("mcpServers", {}).get(NAME) if mcp_file.is_file() else None
    if mcp_file.is_file() and not ours(current):
        return "kept (a nexus-agymail plugin of yours already exists)"
    changed = False
    for file in sorted(PLUGIN_SOURCE.rglob("*")):
        if not file.is_file() or "__pycache__" in file.parts:
            continue
        destination = target / file.relative_to(PLUGIN_SOURCE)
        if not destination.is_file() or destination.read_bytes() != file.read_bytes():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, destination)
            changed = True
    if current != entry():
        _write(mcp_file, json.dumps({"mcpServers": {NAME: entry()}}, indent=2) + "\n")
        changed = True
    global_mcp = root / "mcp_config.json"  # ancienne entrée globale à nous : outils en double avec le plugin
    if global_mcp.is_file():
        data = _read_json(global_mcp)
        if NAME in data.get("mcpServers", {}) and ours(data["mcpServers"][NAME]):
            _backup(global_mcp)
            del data["mcpServers"][NAME]
            _write(global_mcp, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:  # un plugin découvert est actif par défaut ; un choix explicite de l'utilisateur est seulement signalé
        disabled = _read_json(root / "config.json").get("plugins", {}).get(NAME, {}).get("enabled") is False
    except OSError, ValueError, AttributeError:
        disabled = False
    state = "plugin" if changed else "plugin (already installed)"
    return state + (", turned off in Antigravity's settings" if disabled else "")


def declare_all() -> dict[str, str]:
    """Résultat par client trouvé (les absents ne figurent pas)."""
    try:
        result = antigravity()
    except (OSError, ValueError) as error:  # fichier illisible : laissé tel quel
        result = f"failed: {error}"
    return {} if result is None else {"Antigravity": result}
