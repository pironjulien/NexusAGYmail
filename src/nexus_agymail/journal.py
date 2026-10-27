"""Journal commun des outils Nexus (NexusHarness/NexusShared/journal/FORMAT.md, produit « agymail ») : une ligne par
appel d'outil dans %LOCALAPPDATA%\\NexusJournal\\agymail\\<agent>\\<AAAA-MM>.jsonl, pour la revue des outils.

Jamais d'adresse, d'objet, de corps, de libellé, d'identifiant de message ni de requête de recherche : `app` vaut
toujours « gmail », `action` le nom de l'outil, `erreur` le code stable de l'erreur (client.describe_error : revoked,
quota, network…), jamais son message. Jamais bloquant : une ligne qui ne peut pas être écrite est perdue."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

AGENTS = ("claude", "codex", "antigravity")
# Codes rendus par client.describe_error et par les outils ; tout autre texte devient « error ».
CODES = frozenset({"revoked", "refused", "not_found", "forbidden", "quota", "api", "label", "network",
                   "invalid_argument", "internal", "unauthenticated", "confirmation_required", "accord"})  # fmt: skip


def agent_of(name: str | None) -> str | None:
    """IA d'après un indice (nom du client MCP ou NEXUS_AGENT) ; None si inconnue."""
    seen = (name or "").casefold()
    if "codex" in seen:
        return "codex"
    if "antigravity" in seen or "jetski" in seen or "gemini" in seen:
        return "antigravity"
    if "claude" in seen:
        return "claude"
    return None


def actif() -> bool:
    """Faux si NEXUS_JOURNAL=0 (ou off, false, no) : aucune ligne écrite, aucun dossier créé."""
    return os.environ.get("NEXUS_JOURNAL", "").strip().lower() not in ("0", "off", "false", "no")


def racine() -> Path:
    base = os.environ.get("NEXUS_JOURNAL_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA", str(Path.home())), "NexusJournal"
    )
    return Path(base) / "agymail"


def issue_et_cause(status: str | None, code: str | None) -> tuple[str, str | None]:
    """Issue du journal commun d'après le statut rendu par l'outil, et la cause courte (code stable seulement)."""
    if status in (None, "success"):
        return "✓", None
    if status == "confirmation_required":
        return "refus", "confirmation_required"  # rien envoyé : la garde attend le oui de l'utilisateur
    cause = code if code in CODES else ("unauthenticated" if status == "unauthenticated" else "error")
    return ("refus" if cause in ("refused", "accord") else "✗"), cause


def ecrire(agent: str | None, action: str, issue: str, ms: float | None, erreur: str | None = None) -> None:
    """Ajoute une ligne ; agent inconnu : rien (le journal est rangé par IA). Jamais d'exception vers l'outil."""
    try:
        if agent not in AGENTS or not actif():
            return
        maintenant = datetime.now(UTC)
        ligne = {
            "v": 1,
            "ts": maintenant.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "agent": agent,
            "outil": __version__,
            "appel": None,
            "app": "gmail",
            "cadre": None,
            "action": action,
            "technique": "api",
            "issue": issue,
            "ms": round(ms) if ms is not None else None,
            "premier_plan": False,
            "erreur": erreur if erreur is None or erreur in CODES else "error",
        }
        chemin = racine() / agent / f"{maintenant:%Y-%m}.jsonl"
        chemin.parent.mkdir(parents=True, exist_ok=True)
        with chemin.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ligne) + "\n")  # échappé : jamais d'erreur d'encodage
    except Exception:  # noqa: BLE001, S110 - journal : jamais bloquant
        pass


def resultat(result: Any) -> tuple[str | None, str | None]:
    """(status, code) lus dans le résultat d'un appel d'outil (CallToolResult, dict ou texte JSON) ; jamais le reste."""
    data: Any = result
    if hasattr(data, "model_dump"):
        data = data.model_dump(by_alias=True, exclude_none=True)
    if isinstance(data, dict):
        structured = data.get("structuredContent") or data.get("structured_content")
        if isinstance(structured, dict):
            data = structured
        else:
            texts = [
                c["text"] for c in data.get("content") or [] if isinstance(c, dict) and isinstance(c.get("text"), str)
            ]
            try:
                parsed = json.loads(texts[0]) if texts else None
            except TypeError, ValueError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
            elif data.get("isError"):
                return "error", None
            else:
                return None, None
    if not isinstance(data, dict):
        return None, None
    status, code = data.get("status"), data.get("code")
    return (status if isinstance(status, str) else None), (code if isinstance(code, str) else None)
