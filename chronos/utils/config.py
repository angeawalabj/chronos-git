"""
chronos/utils/config.py
========================
Chargeur de configuration YAML pour le mode "personnalisation absolue".

Permet à l'utilisateur de définir son plan complet dans un fichier YAML
au lieu de passer par l'interface graphique.

Format supporté : voir plan.yaml.example à la racine du projet.
"""

from pathlib import Path
from datetime import datetime
from typing import Optional
import yaml

from chronos.core.database import MergeFrequency
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


class ChronosConfig:
    """
    Configuration d'un projet Chronos-Git chargée depuis un fichier YAML.

    Attributs correspondant au fichier plan.yaml :
      project        : nom du projet
      repo_path      : chemin local du dépôt Git
      remote         : nom du remote (défaut: "origin")
      strategy       : "daily" | "weekly" | "custom"
      start_date     : date de début (ISO 8601)
      branch         : branche feature (défaut: "feat/{project}")
      merge_into     : branche de destination (défaut: "main")
      merge_every    : fréquence de merge
      overrides      : personnalisations par fichier
    """

    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self._raw: dict = {}
        self._load()

    def _load(self):
        """Charge et valide le fichier YAML."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config introuvable : {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f) or {}

        logger.info(f"Configuration chargée : {self.config_path}")
        self._validate()

    def _validate(self):
        """Vérifie les champs obligatoires."""
        required = ["project", "repo_path"]
        missing = [k for k in required if k not in self._raw]
        if missing:
            raise ValueError(f"Champs obligatoires manquants dans le YAML : {missing}")

    # ── Accesseurs ────────────────────────────────────────────────────────

    @property
    def project_name(self) -> str:
        return self._raw["project"]

    @property
    def repo_path(self) -> str:
        return str(Path(self._raw["repo_path"]).expanduser().resolve())

    @property
    def source_folder(self) -> Optional[str]:
        folder = self._raw.get("source_folder")
        return str(Path(folder).expanduser().resolve()) if folder else None

    @property
    def remote(self) -> str:
        return self._raw.get("remote", "origin")

    @property
    def strategy(self) -> str:
        return self._raw.get("strategy", "daily")

    @property
    def start_date(self) -> datetime:
        raw = self._raw.get("start_date", datetime.now().strftime("%Y-%m-%d"))
        if isinstance(raw, datetime):
            return raw
        return datetime.fromisoformat(str(raw))

    @property
    def days_count(self) -> int:
        return int(self._raw.get("days", 30))

    @property
    def feature_branch(self) -> str:
        return self._raw.get("branch", f"feat/{self.project_name}")

    @property
    def target_branch(self) -> str:
        return self._raw.get("merge_into", "main")

    @property
    def merge_frequency(self) -> MergeFrequency:
        raw = self._raw.get("merge_every", "manual")
        mapping = {
            "friday":      MergeFrequency.FRIDAY,
            "monday":      MergeFrequency.MONDAY,
            "6days":       MergeFrequency.EVERY_6DAYS,
            "on_complete": MergeFrequency.ON_COMPLETE,
            "manual":      MergeFrequency.MANUAL,
        }
        return mapping.get(raw, MergeFrequency.MANUAL)

    @property
    def overrides(self) -> dict:
        """
        Retourne les overrides sous forme de dict indexé par nom de fichier.

        Format YAML :
          overrides:
            - file: "secret.py"
              action: skip
            - file: "final.py"
              date: "2026-04-30 23:59:00"
              message: "🚀 feat: DONE"
        """
        raw_overrides = self._raw.get("overrides", [])
        result = {}
        for item in raw_overrides:
            filename = item.get("file")
            if filename:
                result[filename] = {k: v for k, v in item.items() if k != "file"}
        return result

    @property
    def recursive(self) -> bool:
        return bool(self._raw.get("recursive", False))

    def to_dict(self) -> dict:
        """Sérialise la config pour l'affichage ou le debug."""
        return {
            "project":        self.project_name,
            "repo_path":      self.repo_path,
            "start_date":     self.start_date.isoformat(),
            "days_count":     self.days_count,
            "feature_branch": self.feature_branch,
            "target_branch":  self.target_branch,
            "merge_frequency": self.merge_frequency.value,
            "strategy":       self.strategy,
            "overrides_count": len(self.overrides),
        }
