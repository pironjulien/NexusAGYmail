"""Commande nexus-agymail : serveur MCP (sans argument), et sous-commandes pour l'utilisateur.

  nexus-agymail                       serveur MCP stdio (ce que lancent les IA)
  nexus-agymail auth [--client-secret FICHIER] [--no-browser]
                                      connexion Google guidée (guide.py) : client OAuth créé pas à pas si absent
  nexus-agymail setup                 déclaration chez Antigravity (plugin), puis l'assistant si pas connecté
  nexus-agymail status                état du jeton (JSON), sans réseau ni secret affiché

Pack Nexus (NEXUS_PACK=1, setup) : aucune question, aucune attente, aucune fenêtre ; code 0 si aucune déclaration
n'a échoué, et une dernière ligne `NEXUS_PACK_RESULT Antigravity=…; sign-in=…`. La connexion Google reste un geste de
l'utilisateur (son propre client OAuth, son consentement)."""

from __future__ import annotations

import argparse
import json
import os

from . import __version__, auth, guide, register

USAGE = "nexus-agymail [auth|setup|status] (no argument: run the MCP server over stdio)"


def _short(result: str) -> str:
    """État d'un client pour la ligne NEXUS_PACK_RESULT."""
    for prefix, short in (("failed", "failed"), ("kept", "kept"), ("plugin", "plugin")):
        if result.startswith(prefix):
            return short
    return result


def cmd_auth(args: argparse.Namespace) -> int:
    return guide.run(client_secret=args.client_secret or "", open_browser=not args.no_browser)


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
            f"Gmail: not signed in yet. Next step: {auth.REAUTH_HINT} (guided: it opens each Google page, then checks)."
        )
    failed = any(r.startswith("failed") for r in results.values())
    if not signed_in and guide.interactive():
        answer = input("Connect Gmail now (guided, about 5 minutes)? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes", "o", "oui"):
            return guide.run() or (1 if failed else 0)
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
