"""Assistant de première connexion (`nexus-agymail setup` ou `auth`, en interactif) : il ouvre chaque page de la
console Google au bon endroit, dit quoi cliquer, détecte tout seul le client OAuth téléchargé, puis vérifie ce qui se
vérifie après la connexion (API Gmail activée, appli publiée et non restée en mode Test à 7 jours).

Aucun client OAuth partagé : l'utilisateur crée le sien dans son propre projet Google Cloud. Rien n'est cliqué à sa
place ; l'assistant ouvre, attend et vérifie. Textes en anglais (sortie de la commande).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import auth

CONSOLE = "https://console.cloud.google.com"
PROJECT_CREATE = f"{CONSOLE}/projectcreate"
ENABLE_GMAIL = f"{CONSOLE}/flows/enableapi?apiid=gmail.googleapis.com"
BRANDING = f"{CONSOLE}/auth/branding"
AUDIENCE = f"{CONSOLE}/auth/audience"
CLIENT_CREATE = f"{CONSOLE}/auth/clients/create"
FALLBACK = "https://developers.google.com/workspace/gmail/api/reference/mcp"
WAIT_S = 900  # attente du JSON téléchargé : 15 minutes


class Io:
    """Entrées et sorties de l'assistant (remplacées par des faux dans les tests)."""

    def __init__(self) -> None:
        self.say: Callable[[str], None] = print
        self.ask: Callable[[str], str] = input
        self.open: Callable[[str], Any] = webbrowser.open
        self.sleep: Callable[[float], None] = time.sleep


def interactive() -> bool:
    return os.environ.get("NEXUS_PACK") != "1" and sys.stdin.isatty() and sys.stdout.isatty()


def with_project(url: str, project: str) -> str:
    if not project:
        return url
    return f"{url}{'&' if '?' in url else '?'}project={project}"


def downloads() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads"


def desktop_client(path: Path) -> dict[str, Any] | None:
    """Contenu du client OAuth s'il est de type « Application de bureau », sinon None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError, ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("installed"), dict) and data["installed"].get("client_id"):
        return data
    return None


def save_client(source: Path) -> tuple[Path, str]:
    """Copie le client dans le dossier d'état (droits réduits) ; rend (chemin, projet Google)."""
    data = desktop_client(source)
    if data is None:
        raise ValueError(
            f"{source.name} is not a Desktop app OAuth client (no 'installed' section): create a client of type "
            "Desktop app"
        )
    target = auth.client_secret_target()
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target, str(data["installed"].get("project_id") or "")


def _newest_client(folder: Path, since: float) -> Path | None:
    try:
        candidates = [p for p in folder.glob("client_secret*.json") if p.stat().st_mtime >= since]
    except OSError:
        return None
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        if desktop_client(path) is not None:
            return path
    return None


def wait_for_client(io: Io, since: float, timeout: float = WAIT_S) -> Path | None:
    """Attend le client_secret_….json dans Téléchargements ; Ctrl+C pour coller son chemin à la main."""
    folder = downloads()
    io.say(f"   Waiting for client_secret_….json in {folder} (Ctrl+C to type its path instead)...")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if found := _newest_client(folder, since):
                return found
            io.sleep(1)
    except KeyboardInterrupt:
        text = io.ask("   Path of the downloaded client JSON: ").strip().strip('"')
        path = Path(text).expanduser()
        if text and desktop_client(path) is not None:
            return path
        io.say("   That is not a Desktop app client JSON.")
    return None


def _step(io: Io, number: int, title: str, url: str, lines: list[str]) -> None:
    io.say(f"\n{number}/5  {title}")
    io.open(url)
    io.say(f"   Opened in your browser: {url}")
    for line in lines:
        io.say(f"   - {line}")


def _pause(io: Io) -> None:
    io.ask("   Press Enter when it is done... ")


def create_client(io: Io) -> tuple[Path, str] | None:
    """Étapes 1 à 4 : projet, API Gmail, écran de consentement publié, client de bureau téléchargé."""
    started = time.time()
    io.say(
        "Gmail needs your own Google OAuth client (free, about 5 minutes, once): your mail never goes through anyone "
        "else's app. Each page opens in your browser; do what it says, then come back here."
    )
    _step(io, 1, "Create a Google Cloud project", PROJECT_CREATE, ["Project name: NexusAGYmail", "Click Create."])
    _pause(io)
    _step(
        io,
        2,
        "Turn on the Gmail API",
        ENABLE_GMAIL,
        ["Check that the project at the top is NexusAGYmail (switch to it if needed)", "Click Next, then Enable."],
    )
    _pause(io)
    _step(
        io,
        3,
        "Consent screen",
        BRANDING,
        [
            "Click Get started. App name: NexusAGYmail; support email: yours. Next.",
            "Audience: External. Next. Contact email: yours. Next. Accept, then Create.",
        ],
    )
    _pause(io)
    io.open(AUDIENCE)
    io.say(f"   Now on {AUDIENCE}: click Publish app, then Confirm (in Testing, Google signs you out every 7 days).")
    _pause(io)
    _step(
        io,
        4,
        "Create the Desktop client",
        CLIENT_CREATE,
        [
            "Application type: Desktop app; name: NexusAGYmail. Click Create.",
            "In the window that opens: Download JSON.",
        ],
    )
    found = wait_for_client(io, started - 5)
    if found is None:
        io.say("   No client JSON found. Run `nexus-agymail auth --client-secret <file>` once you have it.")
        return None
    target, project = save_client(found)
    io.say(f"   Found {found.name}: saved as {target}.")
    return target, project


def sign_in(io: Io, project: str, open_browser: bool = True) -> int:
    """Étape 5 : connexion, puis vérifications (API Gmail, mode Test) avec le geste qui corrige."""
    for _attempt in range(3):
        io.say("\n5/5  Sign in to Gmail")
        io.say(
            '   Google shows "Google hasn\'t verified this app": it is your own app. Click Advanced, then Go to '
            "NexusAGYmail (unsafe), and allow."
        )
        try:
            result = auth.authenticate_interactive(open_browser=open_browser)
        except FileNotFoundError as exc:
            io.say(str(exc))
            return 1
        if result.get("gmail_api_disabled"):
            url = with_project(ENABLE_GMAIL, project)
            io.open(url)
            io.say(f"   The Gmail API is not on for this project yet. Click Enable on {url}, wait a minute.")
            _pause(io)
            continue
        if result.get("testing_mode"):
            url = with_project(AUDIENCE, project)
            io.open(url)
            io.say(f"   The app is still in Testing (sign-in valid 7 days only). Click Publish app on {url}.")
            _pause(io)
            continue
        if result["scopes"].get("reauth_required"):
            io.say(f"   Scopes: {result['scopes'].get('message')}. Allow every Gmail permission and sign in again.")
            continue
        io.say(f"\nDone: signed in as {result.get('account') or 'your account'}. Restart Antigravity to use Gmail.")
        return 0
    io.say(
        f"\nNot finished. If you would rather not create a client, see Google's official Gmail MCP server: {FALLBACK}"
    )
    return 1


def run(io: Io | None = None, client_secret: str = "", open_browser: bool = True) -> int:
    """Assistant complet : client OAuth (créé, donné ou déjà là), puis connexion vérifiée."""
    io = io or Io()
    project = ""
    if client_secret:
        try:
            _, project = save_client(Path(client_secret).expanduser())
        except ValueError as exc:
            io.say(str(exc))
            return 1
    else:
        try:
            existing = auth.get_client_secret_path()
        except FileNotFoundError:
            created = create_client(io)
            if created is None:
                return 1
            project = created[1]
        else:
            project = str(((desktop_client(existing) or {}).get("installed") or {}).get("project_id") or "")
    return sign_in(io, project, open_browser=open_browser)
