"""
chronos/security/keyring_manager.py
====================================
Gestionnaire sécurisé des secrets (tokens GitHub, clés GPG).

Principe fondamental : ZÉRO secret en clair dans le code ou les fichiers.

Ce module utilise le coffre-fort natif de l'OS :
  - Windows  : Windows Credential Manager
  - macOS    : macOS Keychain
  - Linux    : GNOME Keyring / KWallet / Secret Service API

Fallback : Variables d'environnement (fichier .env avec .gitignore strict).

Usage :
    keyring = KeyringManager()
    keyring.store_token("ghp_votre_token_ici")
    token = keyring.get_token()
"""

import os
from typing import Optional
from pathlib import Path

try:
    import keyring as _keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False

from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Constantes ───────────────────────────────────────────────────────────────

# Identifiants dans le gestionnaire de secrets de l'OS
KEYRING_SERVICE = "chronos-git"
KEYRING_USERNAME = "github-token"

# Chemin du .env (fallback uniquement)
ENV_FILE = Path.home() / ".chronos-git" / ".env"
ENV_VAR_NAME = "CHRONOS_GITHUB_TOKEN"


# ── Gestionnaire de secrets ──────────────────────────────────────────────────

class KeyringManager:
    """
    Interface unifiée pour la gestion des secrets.

    Stratégie de récupération (par ordre de priorité) :
    1. OS Keyring    — Méthode recommandée (crypté par l'OS)
    2. Variable d'environnement CHRONOS_GITHUB_TOKEN
    3. Fichier .env  — Dernier recours (doit être dans .gitignore !)

    Avertissement : Si le token est dans un .env, Chronos-Git affiche
    un avertissement car ce fichier pourrait être accidentellement commité.
    """

    def __init__(self):
        # Charge le .env si disponible (fallback silencieux)
        if DOTENV_AVAILABLE and ENV_FILE.exists():
            load_dotenv(ENV_FILE)

    # ── Stockage du token ──────────────────────────────────────────────────

    def store_token(self, token: str) -> bool:
        """
        Stocke le token GitHub dans le coffre-fort sécurisé de l'OS.

        Vérifie d'abord que le token commence bien par "ghp_" ou "github_pat_"
        (formats valides des Personal Access Tokens GitHub).

        Args:
            token : Le Personal Access Token GitHub (Fine-grained recommandé)

        Returns:
            True si le stockage a réussi
        """
        # Validation basique du format
        if not self._is_valid_token_format(token):
            logger.error(
                "Format de token invalide. "
                "Un PAT GitHub commence par 'ghp_' ou 'github_pat_'."
            )
            return False

        if KEYRING_AVAILABLE:
            try:
                _keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
                logger.info("✅ Token stocké dans le gestionnaire de secrets OS.")
                return True
            except Exception as e:
                logger.warning(f"Keyring OS indisponible : {e}. Fallback .env.")

        # Fallback : fichier .env sécurisé
        return self._store_in_env_file(token)

    def get_token(self) -> Optional[str]:
        """
        Récupère le token GitHub depuis la source disponible.

        Retourne None si aucun token n'est configuré (l'appelant doit
        alors afficher un message d'erreur à l'utilisateur).

        Returns:
            Le token en clair, ou None si introuvable
        """
        # Priorité 1 : Keyring OS
        if KEYRING_AVAILABLE:
            try:
                token = _keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                if token:
                    return token
            except Exception as e:
                logger.debug(f"Keyring OS non disponible : {e}")

        # Priorité 2 : Variable d'environnement
        token = os.environ.get(ENV_VAR_NAME)
        if token:
            logger.warning(
                "⚠️  Token récupéré depuis variable d'environnement. "
                "Utilisez 'python main.py security setup-token' pour "
                "le stocker dans le gestionnaire de secrets OS."
            )
            return token

        # Aucun token trouvé
        logger.error(
            "❌ Aucun token GitHub configuré.\n"
            "   Exécutez : python main.py security setup-token"
        )
        return None

    def delete_token(self) -> bool:
        """Supprime le token du gestionnaire de secrets (révocation locale)."""
        if KEYRING_AVAILABLE:
            try:
                _keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
                logger.info("✅ Token supprimé du gestionnaire de secrets.")
                return True
            except Exception:
                pass

        # Supprime du .env si présent
        if ENV_FILE.exists():
            lines = ENV_FILE.read_text().splitlines()
            filtered = [l for l in lines if not l.startswith(f"{ENV_VAR_NAME}=")]
            ENV_FILE.write_text("\n".join(filtered))
            logger.info("✅ Token supprimé du fichier .env.")

        return True

    def is_configured(self) -> bool:
        """Vérifie si un token est disponible sans le retourner."""
        return self.get_token() is not None

    def get_token_preview(self) -> str:
        """
        Retourne une version masquée du token pour l'affichage dans l'UI.
        Ex: "ghp_****...****Ab3x"
        """
        token = self.get_token()
        if not token:
            return "❌ Non configuré"
        if len(token) <= 8:
            return "****"
        return f"{token[:4]}****...****{token[-4:]}"

    # ── Méthodes privées ──────────────────────────────────────────────────

    @staticmethod
    def _is_valid_token_format(token: str) -> bool:
        """Vérifie que le token a un format GitHub PAT valide."""
        return (
            isinstance(token, str)
            and len(token) >= 20
            and (
                token.startswith("ghp_")
                or token.startswith("github_pat_")
                or token.startswith("gho_")   # OAuth token
                or token.startswith("ghu_")   # User token
            )
        )

    @staticmethod
    def _store_in_env_file(token: str) -> bool:
        """
        Dernier recours : stocke dans ~/.chronos-git/.env

        Ce fichier est HORS du dépôt Git (dans le home directory),
        donc moins risqué qu'un .env dans le projet.
        """
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Lit le contenu existant et remplace/ajoute la variable
        existing_lines = []
        if ENV_FILE.exists():
            existing_lines = [
                line for line in ENV_FILE.read_text().splitlines()
                if not line.startswith(f"{ENV_VAR_NAME}=")
            ]

        existing_lines.append(f"{ENV_VAR_NAME}={token}")
        ENV_FILE.write_text("\n".join(existing_lines) + "\n")

        # Permissions restrictives : lecture seule par le propriétaire
        ENV_FILE.chmod(0o600)

        logger.warning(
            f"⚠️  Token stocké dans : {ENV_FILE}\n"
            "   Ce fichier est protégé (chmod 600) mais moins sécurisé "
            "que le gestionnaire de secrets OS."
        )
        return True
