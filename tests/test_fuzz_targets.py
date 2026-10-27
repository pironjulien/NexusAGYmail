"""Cibles de fuzzing (fuzz/targets.py) sans Atheris : octets pseudo-aléatoires à graine fixe et entrées structurées
(les octets aléatoires ne forment presque jamais une adresse valide, relecture de Codex), sur Windows aussi."""

import random
import sys
from pathlib import Path

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
