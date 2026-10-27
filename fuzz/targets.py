"""
Cible de fuzzing de NexusAGYmail : ce qui reçoit des données choisies par un agent ou un expéditeur et finit dans un
en-tête de mail ou sur le disque.

- Adresses (`client.normalize_addresses`) : une liste acceptée ne contient aucun saut de ligne d'aucune sorte (sinon un
  en-tête de plus, donc un destinataire caché) et chaque adresse rendue est bien formée ; la normaliser de nouveau ne
  la change pas.
- Noms de pièces jointes (`fichiers.safe_name`) : le nom rendu reste dans le dossier choisi et Windows peut le créer
  tel quel (ni chemin, ni caractère interdit ou invisible, ni nom réservé, ni point ou espace final).

Lancée sous Atheris par .github/workflows/fuzz.yml (Linux) et, sans Atheris, par tests/test_fuzz_targets.py sur des
octets pseudo-aléatoires (test.ps1, Windows).
"""

from __future__ import annotations

import sys
from email import policy
from email.utils import getaddresses
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_agymail import client, fichiers  # noqa: E402


def addresses(text: str) -> None:
    try:
        out = client.normalize_addresses(text, "to")
    except ValueError:
        return
    if not out:  # liste vide permise (champ facultatif)
        return
    if out.splitlines() != [out] or "\x00" in out:
        raise AssertionError(f"line break in {out!r} (from {text!r})")
    header = policy.default.header_factory("To", out)  # analyse indépendante du validateur (RFC 5322)
    if header.defects or len(header.addresses) != len(getaddresses([out])):
        raise AssertionError(f"malformed {out!r}: {header.defects} (from {text!r})")
    if client.normalize_addresses(out, "to") != out:
        raise AssertionError(f"not stable: {out!r} (from {text!r})")


def attachment_name(text: str) -> None:
    out = fichiers.safe_name(text)
    bad = (
        not out
        or fichiers._FORBIDDEN.search(out)
        or fichiers._INVISIBLE.search(out)
        or fichiers._RESERVED.match(out)
        or out.startswith(".")
        or out.endswith((".", " "))
        or len(out.encode("utf-16-le")) // 2 > fichiers.MAX_NAME_CHARS  # unités UTF-16, comme Windows
    )
    if bad:
        raise AssertionError(f"unsafe name {out!r} (from {text!r})")


def target(data: bytes) -> None:
    text = data[1:].decode("utf-8", "replace")
    (addresses, attachment_name)[data[0] % 2 if data else 0](text)
