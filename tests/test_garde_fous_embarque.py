"""La politique commune est embarquée à l'identique (copie de NexusHarness/NexusWindows/commun) et chargée par le serveur."""

from pathlib import Path

import pytest

from nexus_agymail import garde_fous

PACKAGE = Path(garde_fous.__file__).resolve().parent
COMMUN = Path(__file__).resolve().parents[2] / "NexusWindows" / "commun"  # politique commune, à côté du dépôt
FILES = ("garde_fous.py", "garde-fous.json")


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")  # fins de ligne selon Git, contenu identique


@pytest.mark.parametrize("name", FILES)
def test_copie_identique_a_commun(name):
    if not (COMMUN / name).is_file():
        pytest.skip("NexusHarness/NexusWindows/commun absent sur ce poste")
    assert _text(PACKAGE / name) == _text(COMMUN / name), f"{name} : recopier depuis {COMMUN}"


def test_politique_embarquee_lisible():
    policy = garde_fous.load()
    assert garde_fous.POLICY_FILE == PACKAGE / "garde-fous.json"
    assert policy["accord"]["duree_s"] > 0 and policy["injection"]["motifs"]
