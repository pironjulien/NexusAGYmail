"""Commande nexus-agymail : serveur MCP (sans argument), et sous-commandes pour l'utilisateur.

  nexus-agymail                       serveur MCP stdio (ce que lancent les IA)
  nexus-agymail auth [--client-secret FICHIER] [--no-browser]
                                      connexion Google (navigateur), jeton enregistré localement
  nexus-agymail setup                 déclaration chez Antigravity (plugin), puis l'étape suivante affichée
  nexus-agymail status                état du jeton (JSON), sans réseau ni secret affiché

Pack Nexus (NEXUS_PACK=1, setup) : aucune question, aucune attente, aucune fenêtre ; code 0 si aucune déclaration
n'a échoué, et une dernière ligne `NEXUS_PACK_RESULT Antigravity=…; sign-in=…`. La connexion Google reste un geste de
l'utilisateur (son propre client OAuth, son consentement)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, auth, register

USAGE = "nexus-agymail [auth|setup|status] (no argument: run the MCP server over stdio)"


def _short(result: str) -> str:
    """État d'un client pour la ligne NEXUS_PACK_RESULT."""
    for prefix, short in (("failed", "failed"), ("kept", "kept"), ("plugin", "plugin")):
        if result.startswith(prefix):
            return short
    return result


def _client_secret(path_text: str) -> Path:
    """Copie un client OAuth Desktop téléchargé dans le dossier d'état, après une vérification de forme."""
    source = Path(path_text).expanduser()
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"--client-secret: cannot read {source} ({exc})") from exc
    if not isinstance(data, dict) or "installed" not in data:
        raise SystemExit(
            f"--client-secret: {source.name} is not a Desktop app OAuth client (no 'installed' section); create one of "
            "type Desktop app in Google Cloud Console > APIs & Services > Credentials"
        )
    target = auth.client_secret_target()
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target


def cmd_auth(args: argparse.Namespace) -> int:
    if args.client_secret:
        print(f"OAuth client saved: {_client_secret(args.client_secret)}")
    try:
        result = auth.authenticate_interactive(open_browser=not args.no_browser)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    scopes = result["scopes"]
    print(f"Signed in as {result['account'] or 'the Google account you chose'}; token saved in {result['path']}.")
    if scopes.get("reauth_required"):
        print(f"Scopes: {scopes.get('message') or scopes}", file=sys.stderr)
        return 1
    if result.get("warning"):
        print(result["warning"], file=sys.stderr)
        return 1
    print("Restart your AI app (Antigravity) to use Gmail.")
    return 0


def cmd_setup(_args: argparse.Namespace) -> int:
    pack = os.environ.get("NEXUS_PACK") == "1"
    results = register.declare_all()
    for client, state in results.items():
        print(f"{client}: {state}")
    if not results:
        print(
            "Antigravity not found: install it, open it once, then run this again (or configure your MCP client "
            "by hand, see the README)."
        )
    report = auth.inspect_token()
    signed_in = bool(report.get("present")) and not report.get("error")
    if signed_in:
        print(f"Gmail: signed in ({report.get('account') or 'account recorded in the token'}).")
    else:
        print(
            f"Gmail: not signed in yet. Next step: {auth.REAUTH_HINT} --client-secret <client_secret_....json> "
            "(README: create your own Google OAuth client first)."
        )
    failed = any(r.startswith("failed") for r in results.values())
    if pack:  # ligne de synthèse lue par le pack Nexus (get-nexus.ps1)
        fields = [f"{client}={_short(state)}" for client, state in results.items()]
        fields.append(f"sign-in={'done' if signed_in else 'to do'}")
        print("NEXUS_PACK_RESULT " + "; ".join(fields), flush=True)
    return 1 if failed else 0


def cmd_status(_args: argparse.Namespace) -> int:
    report = auth.inspect_token()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("present") and not report.get("error") else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "serve":
        from . import server

        server.serve()
        return 0
    parser = argparse.ArgumentParser(prog="nexus-agymail", usage=USAGE, description="Gmail MCP server for AI agents.")
    parser.add_argument("--version", action="version", version=f"nexus-agymail {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p_auth = sub.add_parser("auth", help="sign in to Google in the browser and save the token locally")
    p_auth.add_argument("--client-secret", metavar="FILE", help="Desktop app OAuth client JSON downloaded from Google")
    p_auth.add_argument("--no-browser", action="store_true", help="print the sign-in URL instead of opening a browser")
    p_auth.set_defaults(run=cmd_auth)
    sub.add_parser("setup", help="declare the server to Antigravity (plugin)").set_defaults(run=cmd_setup)
    sub.add_parser("status", help="token status as JSON (no network, no secret shown)").set_defaults(run=cmd_status)
    args = parser.parse_args(argv)
    return int(args.run(args))
