"""Authentification OAuth 2.0 Gmail : seul module qui parle à Google pour le jeton.

Les secrets restent dans le dossier d'état local (~/.nexus/nexus-agymail), jamais dans le dépôt :
- client OAuth : client_secret.json (copié par `nexus-agymail auth --client-secret FICHIER`), ou le fichier désigné
  par NEXUS_AGYMAIL_CLIENT_SECRET ;
- jeton : token.bin, chiffré DPAPI sous Windows, fichier 0600 ailleurs (voir token_store.py).
NEXUS_AGYMAIL_HOME remplace le dossier (tests) ; NEXUS_AGYMAIL_ACCOUNT donne l'adresse affichée si le jeton ne la porte pas.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .token_store import TokenError, TokenStore

# Lecture, brouillons (et leur envoi après confirmation), rangement, libellés et corbeille. Jamais
# https://mail.google.com/ : c'est la seule portée qui permet la suppression définitive.
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    MODIFY_SCOPE,
)
FULL_ACCESS_SCOPE = "https://mail.google.com/"
MODIFY_TOOLS = (
    "label_message, unlabel_message, label_thread, unlabel_thread, trash_message, trash_thread, untrash_message, "
    "untrash_thread"
)
REAUTH_HINT = "uvx --python 3.14 nexus-agymail auth"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
REFRESH_TIMEOUT_S = 20
STATE_ENV = "NEXUS_AGYMAIL_HOME"
ACCOUNT_ENV = "NEXUS_AGYMAIL_ACCOUNT"
CLIENT_SECRET_ENV = "NEXUS_AGYMAIL_CLIENT_SECRET"


class GmailAuthError(Exception):
    """Erreur d'authentification avec un code stable (absent, revoque, reseau, refus, illisible, corrompu...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Transport(Request):
    """Transport google-auth borné dans le temps (120 s par défaut sinon)."""

    def __call__(
        self, url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: Any = None, **kwargs: Any
    ) -> Any:
        return super().__call__(url, method, body, headers, timeout or REFRESH_TIMEOUT_S, **kwargs)


def make_transport() -> Any:
    """Transport HTTP des échanges de jeton (remplacé par un faux dans les tests)."""
    return _Transport()


def get_nexus_dir() -> Path:
    """Dossier local (jamais synchronisé) des données de session NexusAGYmail."""
    override = os.environ.get(STATE_ENV)
    path = Path(override) if override else Path.home() / ".nexus" / "nexus-agymail"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_store() -> TokenStore:
    return TokenStore(get_nexus_dir())


def get_client_secret_path() -> Path:
    """Localise le client OAuth Desktop (NEXUS_AGYMAIL_CLIENT_SECRET, sinon le dossier d'état) sans le lire."""
    if chosen := os.environ.get(CLIENT_SECRET_ENV):
        path = Path(chosen).expanduser()
        if path.is_file():
            return path
        raise FileNotFoundError(f"{CLIENT_SECRET_ENV} points to a missing file: {path}")
    local_path = client_secret_target()
    if local_path.exists():
        return local_path
    raise FileNotFoundError(
        f"no OAuth client ({local_path}): create a Google OAuth client of type Desktop app (see the README), then run "
        f"{REAUTH_HINT} --client-secret <downloaded client_secret_....json>"
    )


def client_secret_target() -> Path:
    """Où `nexus-agymail auth --client-secret` copie le client OAuth."""
    return get_nexus_dir() / "client_secret.json"


# --- portées


def granted_scopes(info: dict[str, Any]) -> list[str]:
    """Portées inscrites dans le jeton (champ scopes), en liste."""
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return sorted(str(s) for s in scopes)


def scope_report(info: dict[str, Any]) -> dict[str, Any]:
    """Compare les portées du jeton à celles voulues : manque (gmail.modify d'un jeton 0.2.0) ou portée en trop."""
    granted = granted_scopes(info)
    missing = [s for s in SCOPES if s not in granted]
    excess = [s for s in granted if s not in SCOPES]
    report: dict[str, Any] = {
        "granted": granted,
        "required": list(SCOPES),
        "missing": missing,
        "excess": excess,
        "reauth_required": bool(missing or excess),
    }
    if MODIFY_SCOPE in missing:
        report["blocked_tools"] = MODIFY_TOOLS.split(", ")
    if excess:
        report["message"] = (
            f"the token carries extra scopes ({', '.join(excess)}): sign in again ({REAUTH_HINT}); "
            "meanwhile the tools keep working"
        )
    elif missing == [MODIFY_SCOPE]:
        report["message"] = (
            f"token without gmail.modify: sign in again ({REAUTH_HINT}) for the labelling and trash tools "
            f"({MODIFY_TOOLS}); reading, drafts and sending keep working"
        )
    elif missing:
        report["message"] = f"missing scopes ({', '.join(missing)}): sign in again ({REAUTH_HINT})"
    return report


def modify_scope_problem() -> str | None:
    """Message si le jeton enregistré n'a pas gmail.modify (outils de rangement bloqués) ; None sinon.

    Jeton absent, illisible ou sans liste de portées : None, l'appel lui-même rendra l'erreur exacte.
    """
    try:
        info = token_store().load()
    except TokenError:
        return None
    if not info or not granted_scopes(info) or MODIFY_SCOPE in granted_scopes(info):
        return None
    return (
        f"the token lacks the gmail.modify scope: the user signs in again ({REAUTH_HINT}) for {MODIFY_TOOLS}; "
        "reading and drafts still work"
    )


def account_of(info: dict[str, Any] | None) -> str:
    return os.environ.get(ACCOUNT_ENV) or (info or {}).get("account") or ""


def inspect_token() -> dict[str, Any]:
    """État local du jeton (sans réseau, sans jamais l'afficher) : présence, stockage, portées, compte."""
    store = token_store()
    storage = store.describe()
    if storage is None:
        return {
            "present": False,
            "message": f"no token ({store.path}): sign in first ({REAUTH_HINT})",
        }
    try:
        info = store.load()
    except TokenError as exc:
        return {"present": True, "storage": storage, "error": str(exc), "code": exc.code}
    if info is None:
        return {"present": False, "message": f"no token ({store.path})"}
    return {
        "present": True,
        "storage": store.describe(),
        "account": account_of(info),
        "scopes": scope_report(info),
    }


# --- identifiants


def _to_info(creds: Credentials, previous: dict[str, Any]) -> dict[str, Any]:
    info = json.loads(creds.to_json())
    for key in ("account", "scopes"):
        if previous.get(key) and not info.get(key):
            info[key] = previous[key]
    return info


def get_credentials() -> Credentials:
    """Identifiants valides, renouvelés si besoin (jamais de flux navigateur ici). Erreurs exactes."""
    store = token_store()
    try:
        with store.lock():
            info = store.load()
            if info is None:
                raise GmailAuthError("no_token", f"no token ({store.path}): sign in first ({REAUTH_HINT})")
            try:
                creds = Credentials.from_authorized_user_info(info, granted_scopes(info) or list(SCOPES))
            except (ValueError, KeyError) as exc:
                raise GmailAuthError("corrupt", f"incomplete token ({exc}): sign in again ({REAUTH_HINT})") from exc
            if creds.valid:
                return creds
            try:
                creds.refresh(make_transport())
            except RefreshError as exc:
                if "invalid_grant" in str(exc):
                    raise GmailAuthError("revoked", f"token revoked or expired: sign in again ({REAUTH_HINT})") from exc
                raise GmailAuthError("refused", f"token refresh refused by Google: {exc}") from exc
            except TransportError as exc:
                raise GmailAuthError("network", f"network: the token could not be refreshed ({exc})") from exc
            store.save(_to_info(creds, info))
            return creds
    except TokenError as exc:
        raise GmailAuthError(exc.code, str(exc)) from exc


# --- authentification initiale (lancée seulement par `nexus-agymail auth`, à la demande de l'utilisateur)


def revoke(refresh_token: str) -> bool:
    """Révoque un jeton chez Google ; True si révoqué ou déjà invalide."""
    response = make_transport()(
        url=REVOKE_URL,
        method="POST",
        body=urlencode({"token": refresh_token}),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return response.status in (200, 400)


def authenticate_interactive(port: int = 0, open_browser: bool = True) -> dict[str, Any]:
    """Flux OAuth navigateur (port local), puis jeton chiffré.

    Un ancien jeton aux portées en trop n'est révoqué qu'APRÈS l'obtention et l'enregistrement du nouveau : un flux
    annulé ou en échec laisse l'ancien jeton intact et utilisable.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    store = token_store()
    try:
        old = store.load()
    except TokenError:
        old = None

    flow = InstalledAppFlow.from_client_secrets_file(str(get_client_secret_path()), list(SCOPES))
    creds = flow.run_local_server(
        port=port,
        prompt="consent",
        access_type="offline",
        open_browser=open_browser,
        authorization_prompt_message="Sign in to Google in your browser (if it did not open, visit this URL):\n{url}",
        success_message="NexusAGYmail is signed in. You can close this tab.",
    )
    info = json.loads(creds.to_json())
    # Les portées réellement accordées (consentement granulaire), pas celles demandées.
    granted = getattr(creds, "granted_scopes", None) or creds.scopes or []
    info["scopes"] = sorted(granted)
    try:
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        info["account"] = service.users().getProfile(userId="me").execute(num_retries=3).get("emailAddress", "")
    except Exception as exc:  # l'adresse est un confort : le jeton reste bon sans elle
        print(f"Account address not read: {exc}", file=sys.stderr)
    store.save(info)
    result: dict[str, Any] = {"account": info.get("account", ""), "scopes": scope_report(info), "path": str(store.path)}
    if old and scope_report(old)["excess"] and old.get("refresh_token") != info.get("refresh_token"):
        # Sans révocation, l'ancienne autorisation (portées en trop) resterait valable chez Google.
        revoked = revoke(str(old["refresh_token"]))
        result["old_token_revoked"] = revoked
        print(
            f"Old token with extra scopes: {'revoked' if revoked else 'revocation not confirmed'}.",
            file=sys.stderr,
        )
        # Google peut retirer toute l'autorisation de l'application avec l'ancien jeton : on vérifie le nouveau.
        if not _refresh_works(info):
            result["warning"] = (
                f"revoking the old token also cut the new one: run {REAUTH_HINT} again "
                "(the old one is gone, the second sign-in gives a clean token)"
            )
    return result


def _refresh_works(info: dict[str, Any]) -> bool:
    """True si le jeton se renouvelle encore chez Google (False seulement sur invalid_grant)."""
    creds = Credentials.from_authorized_user_info(info, granted_scopes(info) or list(SCOPES))
    try:
        creds.refresh(make_transport())
    except RefreshError as exc:
        return "invalid_grant" not in str(exc)
    except TransportError:
        return True  # réseau : rien de prouvé, le jeton reste en place
    return True
