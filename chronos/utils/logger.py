"""
chronos/utils/logger.py
========================
Système de journalisation structurée avec Loguru.

Chaque action est horodatée et categorisée.
Les logs sont stockés dans ~/.chronos-git/logs/
"""

import sys
from pathlib import Path
from loguru import logger as _loguru_logger


LOG_DIR = Path.home() / ".chronos-git" / "logs"
LOG_FILE = LOG_DIR / "chronos-{time:YYYY-MM-DD}.log"

_configured = False


def _configure_logger():
    global _configured
    if _configured:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Supprime le handler par défaut
    _loguru_logger.remove()

    # Console : uniquement INFO et plus (propre pour l'utilisateur)
    _loguru_logger.add(
        sys.stdout,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )

    # Fichier : DEBUG complet, rotation journalière, conservation 30 jours
    _loguru_logger.add(
        str(LOG_FILE),
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        rotation="1 day",
        retention="30 days",
        compression="zip",
        encoding="utf-8",
    )

    _configured = True


def get_logger(name: str = "chronos"):
    """Retourne un logger configuré pour le module appelant."""
    _configure_logger()
    return _loguru_logger.bind(name=name)
