"""
chronos/gui/notifier.py
========================
Système de notifications desktop pour Chronos-Git.

Envoie des notifications natives sur :
  - Windows  : via plyer (toast notifications)
  - Linux    : via plyer ou notify-send
  - macOS    : via plyer

Notifications envoyées :
  - ✅ Commit réussi
  - ❌ Commit échoué (action requise)
  - ⚠️  Fichiers modifiés détectés (drift)
  - ⏰ Rattrapage terminé au démarrage
  - 📅 Merge planifié effectué

Usage :
    notifier = DesktopNotifier()
    notifier.commit_success("feat: day 05 automation.py")
    notifier.drift_detected(["script.py", "utils.py"])
"""

from chronos.utils.logger import get_logger

logger = get_logger(__name__)


class DesktopNotifier:
    """
    Interface unifiée pour les notifications desktop.

    Graceful degradation : si plyer n'est pas installé ou que les
    notifications ne sont pas supportées, les erreurs sont loguées
    silencieusement (pas de crash de l'application).
    """

    APP_NAME = "Chronos-Git"
    APP_ICON = ""  # Chemin vers une icône .ico ou .png (optionnel)

    def __init__(self):
        self._available = self._check_availability()

    def _check_availability(self) -> bool:
        """Vérifie si les notifications desktop sont disponibles."""
        try:
            from plyer import notification
            return True
        except ImportError:
            logger.debug(
                "plyer non installé. Notifications desktop désactivées. "
                "Installez avec : pip install plyer"
            )
            return False

    def _send(self, title: str, message: str, timeout: int = 8) -> None:
        """
        Envoie une notification desktop.

        Args:
            title   : titre de la notification (max ~50 chars sur Windows)
            message : corps de la notification
            timeout : durée d'affichage en secondes
        """
        if not self._available:
            # Fallback : log uniquement
            logger.info(f"[NOTIFICATION] {title} — {message}")
            return

        try:
            from plyer import notification
            notification.notify(
                title=f"{self.APP_NAME} — {title}",
                message=message,
                app_name=self.APP_NAME,
                app_icon=self.APP_ICON or None,
                timeout=timeout,
            )
        except Exception as e:
            # Ne jamais crasher à cause d'une notification
            logger.debug(f"Notification échouée (non-critique) : {e}")

    # ── Méthodes de notification spécifiques ──────────────────────────────

    def commit_success(self, commit_message: str, branch: str = "") -> None:
        """Notifie qu'un commit a été poussé avec succès."""
        branch_info = f" → {branch}" if branch else ""
        self._send(
            title="✅ Commit réussi",
            message=f"{commit_message}{branch_info}",
            timeout=5,
        )

    def commit_failed(self, commit_message: str, reason: str = "") -> None:
        """Notifie qu'un commit a échoué (action requise)."""
        self._send(
            title="❌ Commit échoué — Action requise",
            message=f"{commit_message}\n{reason}",
            timeout=12,
        )

    def catchup_complete(self, count: int, failed: int = 0) -> None:
        """Notifie la fin du rattrapage au démarrage du PC."""
        if count == 0:
            return  # Rien à notifier si aucun rattrapage

        status = "✅" if failed == 0 else "⚠️"
        self._send(
            title=f"{status} Rattrapage terminé",
            message=(
                f"{count} commit(s) rattrapé(s)."
                + (f"\n{failed} échec(s) à vérifier." if failed else "")
            ),
            timeout=8,
        )

    def drift_detected(self, files: list[str]) -> None:
        """Notifie qu'une dérive de fichiers a été détectée."""
        count = len(files)
        names = ", ".join(f.split("/")[-1] for f in files[:3])
        suffix = f" (+{count-3} autres)" if count > 3 else ""

        self._send(
            title=f"👁️  Dérive détectée — {count} fichier(s)",
            message=f"{names}{suffix}\nOuvrez Chronos-Git pour gérer.",
            timeout=10,
        )

    def merge_success(self, source: str, target: str) -> None:
        """Notifie qu'un merge planifié a été effectué."""
        self._send(
            title="🔀 Merge effectué",
            message=f"{source} → {target}",
            timeout=6,
        )

    def hash_mismatch(self, filename: str) -> None:
        """Notifie qu'un fichier a été modifié depuis sa planification."""
        self._send(
            title="🔒 Intégrité : fichier modifié",
            message=(
                f"'{filename}' a changé depuis sa planification.\n"
                "Commit bloqué. Ouvrez Chronos-Git pour décider."
            ),
            timeout=15,
        )

    def kill_switch_activated(self) -> None:
        """Notifie l'activation du Kill Switch."""
        self._send(
            title="🔴 KILL SWITCH ACTIVÉ",
            message="Toutes les opérations automatiques sont suspendues.",
            timeout=10,
        )
