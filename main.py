"""
main.py
=======
Point d'entrée principal de Chronos-Git.

Gère deux modes de démarrage :
  - GUI  : Interface graphique CustomTkinter (par défaut)
  - CLI  : Interface ligne de commande Typer

Usage :
  python main.py          → Lance la GUI
  python main.py gui      → Lance la GUI
  python main.py cli      → Lance la CLI (aide)
  python main.py cli plan --folder ./30-days --days 30

Startup sequence :
  1. Initialisation de la base de données SQLite
  2. Rattrapage automatique des tâches en retard (en arrière-plan)
  3. Lancement de l'interface choisie
"""

import sys
from chronos.core.database import Database
from chronos.utils.logger import get_logger

logger = get_logger("main")


def main():
    # ── Initialisation de la base de données ─────────────────────────────
    db = Database()
    db.initialize()
    logger.info("⏱️  Chronos-Git démarré.")

    # ── Routage CLI / GUI ─────────────────────────────────────────────────
    # Si des arguments sont passés et que le premier est "cli", lance la CLI
    args = sys.argv[1:]

    if args and args[0] in ("cli", "security"):
        # Mode CLI — retire "cli" des args pour Typer
        sys.argv = [sys.argv[0]] + args[1:]
        from chronos.cli.main import run_cli
        run_cli()

    elif args and args[0] == "gui":
        # Mode GUI explicite
        _launch_gui()

    elif not args:
        # Mode par défaut : GUI
        _launch_gui()

    else:
        # Passe directement à la CLI si la commande est reconnue
        from chronos.cli.main import run_cli
        run_cli()


def _launch_gui():
    """Lance l'interface graphique."""
    try:
        from chronos.gui.app import launch_gui
        logger.info("Lancement de l'interface graphique.")
        launch_gui()
    except ImportError as e:
        logger.error(
            f"Impossible de lancer la GUI : {e}\n"
            "Vérifiez que customtkinter est installé : pip install customtkinter\n"
            "Pour utiliser la CLI : python main.py cli --help"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
