"""
chronos/core/scanner.py
=======================
Moteur de scan et de planification intelligente.

Ce module transforme un dossier local en une séquence de tâches planifiées
dans la base de données. Il respecte l'ordre de création réel des fichiers
(date système) et permet une personnalisation absolue via des overrides.

Deux modes :
  - Mode Auto    : répartition automatique sur N jours avec jitter
  - Mode Manuel  : l'utilisateur édite chaque entrée avant validation
"""

import os
import random
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

from chronos.core.database import (
    Database, Task, Project, TaskStatus,
    MergeFrequency, compute_file_hash
)
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Constantes de distribution ───────────────────────────────────────────────

# Plage horaire "naturelle" pour les commits (9h - 21h)
COMMIT_HOUR_MIN  = 9
COMMIT_HOUR_MAX  = 21

# Jitter maximal en minutes (±60 min par rapport à l'heure calculée)
JITTER_MINUTES = 60

# Préfixes conventionnels pour varier les messages de commit
COMMIT_PREFIXES = [
    "feat", "fix", "docs", "refactor", "chore", "style", "perf", "test"
]


# ── Résultat du scan ────────────────────────────────────────────────────────

class ScanResult:
    """
    Résultat d'une analyse de dossier avant planification.

    Contient les 3 catégories de fichiers détectées :
      - new_files      : fichiers jamais vus (pas dans Git, pas dans la queue)
      - modified_files : fichiers déjà trackés mais modifiés localement
      - planned_files  : fichiers déjà dans la queue (statut PENDING)
    """

    def __init__(self):
        self.new_files:      list[str] = []
        self.modified_files: list[str] = []
        self.planned_files:  list[str] = []
        self.skipped_files:  list[str] = []

    @property
    def total_actionable(self) -> int:
        return len(self.new_files) + len(self.modified_files)

    def summary(self) -> str:
        return (
            f"🔵 {len(self.new_files)} nouveaux fichiers détectés\n"
            f"🟡 {len(self.modified_files)} fichiers modifiés localement\n"
            f"🟢 {len(self.planned_files)} fichiers déjà planifiés\n"
            f"⚪ {len(self.skipped_files)} fichiers ignorés"
        )


# ── Scanner principal ────────────────────────────────────────────────────────

class FolderScanner:
    """
    Analyse un dossier et génère un plan de commits.

    Fonctionnement :
    1. Liste les fichiers triés par DATE DE CRÉATION (st_ctime)
    2. Calcule les dates de commit avec répartition + jitter
    3. Calcule le hash SHA-256 de chaque fichier (intégrité)
    4. Génère les objets Task prêts pour l'insertion en base

    Usage :
        scanner = FolderScanner(db)
        plan = scanner.build_plan(
            folder_path="./30-days-scripting",
            project_id=1,
            start_date=datetime(2026, 4, 1),
            days_count=30,
            branch_name="feat/30-days-challenge"
        )
        # plan est une list[Task] à valider par l'utilisateur
        db.insert_tasks_bulk(plan)
    """

    # Extensions ignorées par défaut (fichiers système/cache/base de données)
    IGNORED_EXTENSIONS = {
        # Python bytecode
        ".pyc", ".pyo", ".pyd",
        # Bibliothèques compilées
        ".so", ".dll", ".dylib",
        # OS
        ".DS_Store", ".Thumbs.db",
        # Temporaires
        ".log", ".tmp", ".temp", ".bak", ".swp", ".swo",
        # SQLite (tous les fichiers WAL/SHM générés automatiquement)
        ".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3",
        # Archives
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
        # Binaires
        ".exe", ".bin", ".obj", ".o",
    }

    # Noms de fichiers ignorés par défaut
    IGNORED_NAMES = {
        "__pycache__", ".git", ".env", ".venv",
        "node_modules", ".idea", ".vscode",
        "chronos.db", ".gitignore_bak",
    }

    def __init__(self, db: Database):
        self.db = db

    # ── Scan d'un dossier ─────────────────────────────────────────────────

    def list_files_by_creation_date(
        self,
        folder_path: str,
        recursive: bool = False
    ) -> list[Path]:
        """
        Liste les fichiers d'un dossier triés par date de création (ASC).

        Le tri par date de création (st_ctime) respecte l'ordre dans lequel
        tu as réellement créé tes scripts, indépendamment de leur nom.

        Args:
            folder_path : chemin du dossier à scanner
            recursive   : si True, inclut les sous-dossiers

        Returns:
            Liste de Path triés du plus ancien au plus récent
        """
        root = Path(folder_path).resolve()

        if not root.exists():
            raise FileNotFoundError(f"Dossier introuvable : {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Ce chemin n'est pas un dossier : {root}")

        # Sécurité : vérification que le dossier est bien accessible
        logger.info(f"Scan du dossier : {root}")

        files: list[Path] = []

        if recursive:
            iterator = root.rglob("*")
        else:
            iterator = root.iterdir()

        for entry in iterator:
            # Ignorer les dossiers (on ne committe que des fichiers)
            if not entry.is_file():
                continue

            # Ignorer les extensions système (.db, .pyc, .log, etc.)
            if entry.suffix.lower() in self.IGNORED_EXTENSIONS:
                continue

            # Ignorer les fichiers dont le NOM EXACT est dans la liste noire
            # (.DS_Store, __pycache__, etc.) — on teste entry.name, pas les parts du chemin
            if entry.name in self.IGNORED_NAMES:
                continue

            # Ignorer les fichiers dans des sous-dossiers systèmes
            # (ex: un fichier dans .git/ ou node_modules/)
            if any(part in self.IGNORED_NAMES for part in entry.parts[:-1]):
                continue

            # Ignorer les fichiers cachés (commençant par un point) sauf .gitignore etc.
            # On ignore les fichiers dont le nom commence par '.' et qui ne sont pas
            # des fichiers de config courants utiles
            KEEP_DOTFILES = {".gitignore", ".gitattributes", ".editorconfig",
                             ".prettierrc", ".eslintrc", ".env.example",
                             ".htaccess", ".dockerignore"}
            if entry.name.startswith(".") and entry.name not in KEEP_DOTFILES:
                continue

            files.append(entry)

        # Tri par date de création (timestamp Unix, du plus ancien au plus récent)
        files.sort(key=lambda p: p.stat().st_ctime)

        logger.info(f"  → {len(files)} fichiers trouvés et triés par date de création")
        return files

    # ── Construction du plan automatique ─────────────────────────────────

    def build_plan(
        self,
        folder_path:  str,
        project_id:   int,
        start_date:   datetime,
        days_count:   int,
        branch_name:  str = "main",
        recursive:    bool = False,
        overrides:    Optional[dict] = None,  # {filename: {date, message, action}}
        commit_prefix: Optional[str] = None,  # Forcer un préfixe (feat, fix...)
    ) -> list[Task]:
        """
        Génère la liste des tâches planifiées (à valider avant insertion en DB).

        Algorithme de distribution :
          - interval = days_count / nb_fichiers
          - Pour chaque fichier i : date = start_date + i * interval jours
          - Heure : aléatoire dans [COMMIT_HOUR_MIN, COMMIT_HOUR_MAX]
          - Jitter : ±JITTER_MINUTES minutes supplémentaires

        Args:
            folder_path   : dossier contenant les fichiers à planifier
            project_id    : ID du projet dans la DB
            start_date    : premier jour de la séquence
            days_count    : durée totale en jours
            branch_name   : branche Git cible
            recursive     : inclure les sous-dossiers
            overrides     : personnalisations par fichier
            commit_prefix : forcer un préfixe ("feat", "fix"...)

        Returns:
            list[Task] — La liste des tâches générées (non encore insérées en DB)
        """
        overrides = overrides or {}
        files = self.list_files_by_creation_date(folder_path, recursive)

        if not files:
            logger.warning("Aucun fichier trouvé dans le dossier.")
            return []

        # Intervalle en jours entre chaque fichier
        # Ex: 30 fichiers sur 30 jours = 1.0 jour d'intervalle
        # Ex: 15 fichiers sur 30 jours = 2.0 jours d'intervalle
        interval = days_count / len(files)

        tasks: list[Task] = []

        for i, file_path in enumerate(files):
            file_name = file_path.name

            # Vérification des overrides par nom de fichier
            override = overrides.get(file_name, {})

            # Override "skip" : fichier volontairement ignoré
            if override.get("action") == "skip":
                logger.info(f"  ⏭  Ignoré (override) : {file_name}")
                continue

            # ── Calcul de la date planifiée ──────────────────────────────
            if override.get("date"):
                # Date entièrement personnalisée par l'utilisateur
                planned_dt = datetime.fromisoformat(override["date"])
            else:
                # Distribution automatique + heure aléatoire + jitter
                days_offset = i * interval
                planned_dt = start_date + timedelta(days=days_offset)

                # Heure naturelle (entre 9h et 21h)
                random_hour   = random.randint(COMMIT_HOUR_MIN, COMMIT_HOUR_MAX)
                random_minute = random.randint(0, 59)
                planned_dt = planned_dt.replace(
                    hour=random_hour,
                    minute=random_minute,
                    second=random.randint(0, 59)
                )

                # Jitter : petite variation pour ne pas paraître robotique
                jitter = timedelta(minutes=random.randint(-JITTER_MINUTES, JITTER_MINUTES))
                planned_dt += jitter

                # Contrainte : l'heure ne sort pas de la plage naturelle
                if planned_dt.hour < COMMIT_HOUR_MIN:
                    planned_dt = planned_dt.replace(hour=COMMIT_HOUR_MIN, minute=0)
                if planned_dt.hour > COMMIT_HOUR_MAX:
                    planned_dt = planned_dt.replace(hour=COMMIT_HOUR_MAX, minute=0)

            # ── Génération du message de commit ──────────────────────────
            if override.get("message"):
                commit_msg = override["message"]
            else:
                commit_msg = self._generate_commit_message(
                    file_name=file_name,
                    index=i,
                    total=len(files),
                    prefix=commit_prefix
                )

            # ── Calcul du hash SHA-256 (intégrité) ───────────────────────
            try:
                file_hash = compute_file_hash(str(file_path))
            except Exception as e:
                logger.warning(f"Impossible de hasher {file_name}: {e}")
                file_hash = ""

            # ── Construction de la Task ───────────────────────────────────
            task = Task(
                project_id=project_id,
                file_path=str(file_path),
                commit_message=commit_msg,
                branch_name=override.get("branch", branch_name),
                scheduled_time=planned_dt.strftime("%Y-%m-%d %H:%M:%S"),
                status=TaskStatus.PENDING,
                retry_count=0,
                file_hash=file_hash,
            )
            tasks.append(task)

            logger.debug(f"  📅 {planned_dt.strftime('%Y-%m-%d %H:%M')} | {file_name}")

        logger.info(f"Plan généré : {len(tasks)} tâches sur {days_count} jours")
        return tasks

    # ── Messages de commit ────────────────────────────────────────────────

    def _generate_commit_message(
        self,
        file_name: str,
        index:     int,
        total:     int,
        prefix:    Optional[str] = None
    ) -> str:
        """
        Génère un message de commit naturel et professionnel.

        Pour éviter le style robotique, les préfixes sont alternés selon
        la position du fichier dans la séquence. Les fichiers au début
        sont souvent des "feat:", ceux du milieu des "refactor:", etc.

        Args:
            file_name : nom du fichier (ex: "web_scraper.py")
            index     : position dans la séquence (0-based)
            total     : nombre total de fichiers
            prefix    : forcer un préfixe spécifique

        Returns:
            Message de commit formaté
        """
        stem = Path(file_name).stem.replace("_", " ").replace("-", " ")

        if prefix:
            chosen_prefix = prefix
        else:
            # Distribution naturelle des types de commits
            position_ratio = index / max(total - 1, 1)
            if position_ratio < 0.3:
                chosen_prefix = "feat"
            elif position_ratio < 0.6:
                chosen_prefix = random.choice(["refactor", "chore", "style"])
            elif position_ratio < 0.9:
                chosen_prefix = random.choice(["fix", "perf", "docs"])
            else:
                chosen_prefix = "feat"  # Le dernier fichier marque une milestone

        return f"{chosen_prefix}: {stem}"

    # ── Analyse de dérive (Drift Detection) ───────────────────────────────

    def analyze_drift(
        self,
        folder_path: str,
        project_id:  int
    ) -> ScanResult:
        """
        Compare l'état du dossier local avec la base de données.

        Détecte 4 catégories de fichiers :
        1. 🔵 Nouveaux : présents en local, absents de la queue
        2. 🟡 Modifiés : hash actuel ≠ hash au moment de la planification
        3. 🟢 Planifiés : dans la queue avec hash inchangé
        4. ⚪ Ignorés : extensions/noms dans la liste noire

        Args:
            folder_path : dossier à analyser
            project_id  : ID du projet pour récupérer les tâches existantes

        Returns:
            ScanResult avec les 4 catégories remplies
        """
        result = ScanResult()

        # Récupère toutes les tâches PENDING du projet (fichiers planifiés)
        pending_tasks = self.db.get_pending_tasks(project_id)
        planned_paths = {t.file_path: t for t in pending_tasks}

        try:
            local_files = self.list_files_by_creation_date(folder_path)
        except (FileNotFoundError, NotADirectoryError) as e:
            logger.error(f"Erreur de scan : {e}")
            return result

        for local_file in local_files:
            file_str = str(local_file)

            if file_str in planned_paths:
                task = planned_paths[file_str]
                # Recalcule le hash pour détecter une modification
                try:
                    current_hash = compute_file_hash(file_str)
                    if current_hash != task.file_hash and task.file_hash:
                        result.modified_files.append(file_str)
                        logger.warning(f"🟡 Fichier modifié détecté : {local_file.name}")
                    else:
                        result.planned_files.append(file_str)
                except Exception:
                    result.planned_files.append(file_str)
            else:
                result.new_files.append(file_str)
                logger.info(f"🔵 Nouveau fichier détecté : {local_file.name}")

        logger.info(f"Drift Analysis :\n{result.summary()}")
        return result

    # ── Ajout de fichiers détectés à la queue ─────────────────────────────

    def append_new_files_to_plan(
        self,
        file_paths:  list[str],
        project_id:  int,
        branch_name: str,
        start_after: Optional[datetime] = None
    ) -> list[Task]:
        """
        Ajoute de nouveaux fichiers détectés à la fin du plan existant.

        Utilisé après une Drift Detection pour intégrer les fichiers
        nouvellement créés sans briser la cohérence du calendrier.

        Args:
            file_paths  : liste des chemins des nouveaux fichiers
            project_id  : ID du projet
            branch_name : branche Git cible
            start_after : date de départ (par défaut: demain)

        Returns:
            list[Task] nouvelles tâches générées
        """
        if not file_paths:
            return []

        start = start_after or (datetime.now() + timedelta(days=1))
        tasks = []

        for i, file_path in enumerate(file_paths):
            planned_dt = start + timedelta(days=i)
            planned_dt = planned_dt.replace(
                hour=random.randint(COMMIT_HOUR_MIN, COMMIT_HOUR_MAX),
                minute=random.randint(0, 59),
                second=random.randint(0, 59)
            )

            file_name = Path(file_path).name
            try:
                file_hash = compute_file_hash(file_path)
            except Exception:
                file_hash = ""

            task = Task(
                project_id=project_id,
                file_path=file_path,
                commit_message=self._generate_commit_message(file_name, i, len(file_paths)),
                branch_name=branch_name,
                scheduled_time=planned_dt.strftime("%Y-%m-%d %H:%M:%S"),
                status=TaskStatus.PENDING,
                file_hash=file_hash,
            )
            tasks.append(task)

        logger.info(f"{len(tasks)} nouveaux fichiers ajoutés au plan.")
        return tasks