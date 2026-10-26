"""NexusAGYmail - serveur MCP Gmail (lecture, brouillons, rangement, envoi confirmé) pour Antigravity."""

import sys

# Aucun __pycache__ dans les sources (dossier synchronisé) : les commandes de lancement passent aussi -B et
# PYTHONDONTWRITEBYTECODE, ceci couvre les sous-modules si Python est lancé sans eux.
sys.dont_write_bytecode = True

__version__ = "0.9.0"
