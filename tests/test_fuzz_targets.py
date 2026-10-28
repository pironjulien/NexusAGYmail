"""Cibles de fuzzing (fuzz/targets.py) sans Atheris : octets pseudo-aléatoires à graine fixe et entrées structurées
(les octets aléatoires ne forment presque jamais une adresse valide, relecture de Codex), sur Windows aussi."""

import random
import sys
from email import policy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fuzz"))

import targets  # noqa: E402  # pyright: ignore[reportMissingImports]

ADDRESSES = [
    "a@example.com",
    "Nom <a@example.com>",
    '"Nom, Prénom" <a.b+c@example.com>, d@example.org',
    "x@bücher.example",
    "=?utf-8?q?A?= <a@example.com>",
    "<a@example.com>",
    # refusées ou réparées, jamais rendues telles quelles
    "a..b@example.com",
    ".a@example.com",
    "a.@example.com",
    "a@-x.example.com",
    "Nom Bcc: x <a@example.net>",
    "Nom\x85Bcc: x <a@example.net>",
    "a@example.com\n",
    "a@example.com\r",
    # groupes « : » à la chaîne : le lecteur d'adresses de Python s'appelle lui-même par groupe (fuzzing du 28/10)
    "a:" * 2000,
    ":" * 2000,
]
NAMES = [
    "\U0001f600" * 128,  # 256 unités UTF-16 : trop long pour Windows
    "a" * 300 + ".\U0001f600" * 8,
    "." * 200 + ".pdf",
    "con.txt",
    " ../..\\x.exe. ",
    "",
]


def test_targets_hold_on_random_inputs() -> None:
    rng = random.Random(1618)
    for _ in range(20000):
        targets.target(rng.randbytes(rng.randrange(0, 200)))


def test_structured_inputs() -> None:
    for value in ADDRESSES:
        targets.addresses(value)
    for value in NAMES:
        targets.attachment_name(value)


def test_long_name_is_cut_in_linear_time() -> None:
    """Relecture de Codex : 200 000 caractères prenaient 3 s (troncature quadratique)."""
    import time

    from nexus_agymail import fichiers

    start = time.perf_counter()
    assert fichiers.safe_name("a" * 200_000 + ".pdf").endswith("a.pdf")
    assert time.perf_counter() - start < 0.5


def test_blank_display_name_is_dropped() -> None:
    """Fuzzing en CI : « ()Co.$@E.B() » rendait «   <Co.$@E.B> », qui ne se relisait pas à l'identique."""
    targets.addresses("()Co.$@E.B()")


def test_encoded_words_and_blanks_found_by_local_search() -> None:
    """Recherche locale : un mot encodé (RFC 2047) dans l'adresse ou entre guillemets, une tabulation dans le nom."""
    from nexus_agymail import client

    for value in [
        "=?utf-8?q?A?=Co$a@b.example",
        "B.<=?utf-8?q?A?=1a@b.example",
        "B( Co\t-)a@b.example ",
        '=?utf-8?q?A?=Bexample.comx.co-<x.coa@b.example a"',
        "=?utf-8?q?Ren=C3=A9?= <a@example.com>",
    ]:
        targets.addresses(value)
    out = client.normalize_addresses("=?utf-8?q?Ren=C3=A9?= <a@example.com>", "to")
    assert out.endswith(" <a@example.com>") and str(policy.default.header_factory("To", out)) == "René <a@example.com>"


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        # relecture de Codex : « =? » seul est du dot-atom, permis
        ("a=?b@example.com", "a=?b@example.com"),
        ("=?@example.com", "=?@example.com"),
        # nom qui est littéralement un mot encodé : gardé tel quel, jamais décodé deux fois (RFC 2047 § 5)
        ("=?utf-8?b?PT91dGYtOD9xP1Jlbj1DMz1BOT89?= <a@example.com>", "=?utf-8?q?Ren=C3=A9?= <a@example.com>"),
        # Unicode ordinaire autour d'un mot encodé : gardé
        ("René =?utf-8?q?Dupont?= <a@example.com>", "René Dupont <a@example.com>"),
        ("Jean René <jean.rene@example.com>", "Jean René <jean.rene@example.com>"),
    ],
)
def test_legitimate_addresses_are_kept(value: str, shown: str) -> None:
    from nexus_agymail import client

    out = client.normalize_addresses(value, "to")
    targets.addresses(value)
    assert str(policy.default.header_factory("To", out)) == shown


def test_long_name_like_an_encoded_word_has_no_line_break() -> None:
    """Relecture de Codex : un long nom réencodé était plié par un saut de ligne, refusé ensuite dans l'en-tête To."""
    from email.header import Header
    from email.message import EmailMessage

    from nexus_agymail import client

    literal = "=?utf-8?q?" + "A" * 48 + "?="
    value = " ".join(Header(literal, "utf-8", maxlinelen=75).encode().splitlines()) + " <a@example.com>"
    out = client.normalize_addresses(value, "to")
    targets.addresses(value)
    message = EmailMessage()
    message["To"] = out
    assert str(policy.default.header_factory("To", out)) == f"{literal} <a@example.com>"


def test_control_characters_in_display_name_found_by_the_fuzzer() -> None:
    import base64

    targets.target(base64.b64decode("AAAAAAAAAPoACQkmCS4JUAkJCVAJAEMyPEBAL5YAbzoJbwkuCVAJCS4JUAkuCVQJQAlQCS5U"))
