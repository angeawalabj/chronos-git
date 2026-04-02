"""
chronos/security/gpg_signer.py
================================
Module de signature GPG pour les commits Chronos-Git.

Les commits GPG-signés affichent le badge "Verified" sur GitHub.
C'est un signal fort de professionnalisme et d'authenticité.

Ce module configure et vérifie la signature GPG pour Git,
sans exposer ni stocker la clé privée dans l'application.

Usage :
    signer = GPGSigner()
    if signer.is_configured():
        signer.configure_repo(repo_path)
    key_id = signer.get_signing_key_id()
"""

import subprocess
from pathlib import Path
from typing import Optional

from chronos.utils.logger import get_logger

logger = get_logger(__name__)


class GPGSigner:
    """
    Gestionnaire de la configuration GPG pour les commits signés.

    Principe : Ce module ne GÈRE PAS les clés GPG (c'est le rôle de gpg).
    Il configure Git pour utiliser une clé GPG existante et vérifie
    que la configuration est correcte avant chaque opération.

    Pré-requis :
      1. GPG installé sur le système (gpg ou gpg2)
      2. Une clé GPG générée et associée à l'email GitHub
      3. La clé publique exportée vers GitHub Settings > SSH and GPG keys
    """

    def __init__(self):
        self._gpg_available = self._check_gpg()

    def _check_gpg(self) -> bool:
        """Vérifie si GPG est installé sur le système."""
        for cmd in ["gpg2", "gpg"]:
            try:
                result = subprocess.run(
                    [cmd, "--version"],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    self._gpg_cmd = cmd
                    logger.debug(f"GPG trouvé : {cmd}")
                    return True
            except FileNotFoundError:
                continue
        logger.warning(
            "GPG non trouvé. Les commits ne seront pas signés.\n"
            "Installez GPG : https://gnupg.org/download/"
        )
        return False

    def is_configured(self) -> bool:
        """
        Vérifie si une clé de signature Git est configurée globalement.

        Retourne True si git config user.signingkey est défini ET
        que commit.gpgsign est activé.
        """
        if not self._gpg_available:
            return False

        try:
            key = subprocess.run(
                ["git", "config", "--global", "user.signingkey"],
                capture_output=True, text=True
            ).stdout.strip()

            sign = subprocess.run(
                ["git", "config", "--global", "commit.gpgsign"],
                capture_output=True, text=True
            ).stdout.strip()

            return bool(key) and sign.lower() == "true"
        except Exception:
            return False

    def get_signing_key_id(self) -> Optional[str]:
        """Retourne l'ID de la clé de signature Git configurée."""
        try:
            result = subprocess.run(
                ["git", "config", "--global", "user.signingkey"],
                capture_output=True, text=True
            )
            return result.stdout.strip() or None
        except Exception:
            return None

    def list_available_keys(self) -> list[dict]:
        """
        Liste les clés GPG disponibles sur le système.

        Retourne une liste de dicts avec :
          - key_id  : identifiant court de la clé
          - email   : email associé
          - name    : nom associé
          - expires : date d'expiration (ou "n/a")
        """
        if not self._gpg_available:
            return []

        try:
            result = subprocess.run(
                [self._gpg_cmd, "--list-secret-keys", "--keyid-format=long"],
                capture_output=True, text=True
            )

            keys = []
            lines = result.stdout.splitlines()
            current_key = {}

            for line in lines:
                if line.startswith("sec"):
                    # Format : sec   rsa4096/KEYID DATE [SC]
                    parts = line.split("/")
                    if len(parts) >= 2:
                        key_info = parts[1].split(" ")
                        current_key = {
                            "key_id": key_info[0] if key_info else "unknown",
                            "email":  "",
                            "name":   "",
                            "expires": "n/a",
                        }
                elif line.strip().startswith("uid") and current_key:
                    # Format : uid    [ultimate] Nom Prénom <email@example.com>
                    uid_part = line.split("]")[-1].strip() if "]" in line else line.strip()
                    if "<" in uid_part and ">" in uid_part:
                        current_key["name"]  = uid_part.split("<")[0].strip()
                        current_key["email"] = uid_part.split("<")[1].rstrip(">").strip()
                    if current_key not in keys and current_key.get("key_id"):
                        keys.append(current_key.copy())

            return keys

        except Exception as e:
            logger.error(f"Impossible de lister les clés GPG : {e}")
            return []

    def configure_signing(self, key_id: str, enable: bool = True) -> bool:
        """
        Configure Git pour signer les commits avec la clé spécifiée.

        Modifie la config Git GLOBALE (pas juste pour un dépôt).

        Args:
            key_id : ID de la clé GPG (ex: "3AA5C34371567BD2")
            enable : True pour activer, False pour désactiver

        Returns:
            True si la configuration a réussi
        """
        try:
            subprocess.run(
                ["git", "config", "--global", "user.signingkey", key_id],
                check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "--global",
                 "commit.gpgsign", "true" if enable else "false"],
                check=True, capture_output=True
            )

            # Configure le programme GPG pour Git
            subprocess.run(
                ["git", "config", "--global",
                 "gpg.program", self._gpg_cmd],
                check=True, capture_output=True
            )

            status = "activée" if enable else "désactivée"
            logger.info(f"✅ Signature GPG {status} pour la clé : {key_id}")
            return True

        except subprocess.CalledProcessError as e:
            logger.error(f"Impossible de configurer GPG : {e}")
            return False

    def export_public_key(self, key_id: str) -> Optional[str]:
        """
        Exporte la clé publique au format armored (pour GitHub).

        Le résultat peut être copié dans :
        GitHub → Settings → SSH and GPG keys → New GPG key

        Returns:
            Clé publique armored (commence par "-----BEGIN PGP PUBLIC KEY BLOCK-----")
        """
        if not self._gpg_available:
            return None

        try:
            result = subprocess.run(
                [self._gpg_cmd, "--armor", "--export", key_id],
                capture_output=True, text=True
            )
            if result.stdout:
                return result.stdout
            logger.error(f"Clé {key_id} introuvable dans le trousseau GPG.")
            return None
        except Exception as e:
            logger.error(f"Export GPG échoué : {e}")
            return None

    def get_setup_instructions(self) -> str:
        """
        Retourne les instructions complètes pour configurer GPG.
        Affiché dans la GUI pour les utilisateurs non configurés.
        """
        return """
🔐 Configuration GPG pour les commits "Verified"

Étape 1 : Générer une clé GPG
─────────────────────────────
gpg --full-generate-key
→ Choisissez : RSA and RSA (option 1)
→ Taille : 4096 bits
→ Durée : 1 an (recommandé)
→ Utilisez votre email GitHub

Étape 2 : Récupérer l'ID de votre clé
───────────────────────────────────────
gpg --list-secret-keys --keyid-format=long
→ Notez l'ID après "rsa4096/" (ex: 3AA5C34371567BD2)

Étape 3 : Exporter vers GitHub
───────────────────────────────
gpg --armor --export VOTRE_ID_CLE
→ Copiez la sortie
→ GitHub → Settings → SSH and GPG keys → New GPG key

Étape 4 : Configurer via Chronos-Git
──────────────────────────────────────
Sélectionnez votre clé dans l'onglet Paramètres > GPG

Vos commits afficheront désormais le badge ✅ Verified sur GitHub !
"""
