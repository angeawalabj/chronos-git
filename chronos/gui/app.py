"""
chronos/gui/app.py
==================
Interface graphique principale de Chronos-Git.

Thème : Dark Cyberpunk — noir profond, vert émeraude, bleu électrique.
Framework : CustomTkinter (modern Tkinter wrapper).

Fenêtres :
  - Dashboard    : Vue d'ensemble, statuts, prochains commits
  - Planner      : Tableau de planification interactif
  - Drift Panel  : Gestion des fichiers modifiés/nouveaux
  - Settings     : Configuration de la sécurité et des préférences

Navigation : Sidebar à gauche avec icônes, contenu à droite.
"""

import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox

from chronos.core.database import Database, Project, MergeFrequency, Task, TaskStatus
from chronos.core.scanner import FolderScanner
from chronos.core.executor import GitExecutor
from chronos.core.catchup import CatchupEngine, CatchupReport
from chronos.security.keyring_manager import KeyringManager
from chronos.utils.logger import get_logger

logger = get_logger(__name__)


# ── Configuration du thème ───────────────────────────────────────────────────

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# Palette de couleurs Chronos-Git (cyberpunk)
COLORS = {
    "bg_primary":    "#0a0e1a",   # Noir profond
    "bg_secondary":  "#111827",   # Gris très sombre
    "bg_card":       "#1a2236",   # Bleu nuit (cartes)
    "bg_sidebar":    "#0d1321",   # Sidebar
    "accent_green":  "#00ff88",   # Vert émeraude (succès, actif)
    "accent_blue":   "#0088ff",   # Bleu électrique (info)
    "accent_orange": "#ff6b00",   # Orange (avertissement)
    "accent_red":    "#ff2244",   # Rouge (erreur, kill switch)
    "accent_purple": "#8b5cf6",   # Violet (branches)
    "text_primary":  "#e2e8f0",   # Blanc cassé
    "text_secondary":"#64748b",   # Gris moyen
    "text_dim":      "#334155",   # Gris foncé
    "border":        "#1e3a5f",   # Bordure bleue subtile
}


# ── Application principale ───────────────────────────────────────────────────

class ChronosApp(ctk.CTk):
    """
    Fenêtre principale de Chronos-Git.

    Layout :
    ┌────────────┬─────────────────────────────────┐
    │  Sidebar   │         Content Area            │
    │  (180px)   │         (dynamique)             │
    │            │                                  │
    │  • Dash    │                                  │
    │  • Plan    │                                  │
    │  • Drift   │                                  │
    │  • Logs    │                                  │
    │  • Settings│                                  │
    └────────────┴─────────────────────────────────┘
    """

    def __init__(self, db: Database, keyring: KeyringManager):
        super().__init__()

        self.db       = db
        self.keyring  = keyring
        self.scanner  = FolderScanner(db)
        self.executor = GitExecutor(db, keyring)
        self.catchup  = CatchupEngine(db, self.executor)

        self._current_project: Optional[Project] = None
        self._frames: dict[str, ctk.CTkFrame] = {}

        self._configure_window()
        self._build_sidebar()
        self._build_content_area()
        self._show_frame("dashboard")

        # Lancement du rattrapage automatique en arrière-plan
        self._start_catchup_thread()

    # ── Configuration de la fenêtre ───────────────────────────────────────

    def _configure_window(self):
        self.title("⏱️  Chronos-Git — Git Lifecycle Orchestrator")
        self.geometry("1200x780")
        self.minsize(900, 600)
        self.configure(fg_color=COLORS["bg_primary"])

        # Icône de la fenêtre (si disponible)
        try:
            self.iconbitmap("assets/icon.ico")
        except Exception:
            pass

    # ── Sidebar ───────────────────────────────────────────────────────────

    def _build_sidebar(self):
        """Construit la barre de navigation latérale."""
        self.sidebar = ctk.CTkFrame(
            self,
            width=200,
            corner_radius=0,
            fg_color=COLORS["bg_sidebar"],
        )
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        # Logo / Titre
        logo_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo_frame.pack(pady=(30, 10), padx=15, fill="x")

        ctk.CTkLabel(
            logo_frame,
            text="⏱️",
            font=ctk.CTkFont(size=32),
        ).pack()

        ctk.CTkLabel(
            logo_frame,
            text="CHRONOS",
            font=ctk.CTkFont(size=18, weight="bold"),
            text_color=COLORS["accent_green"],
        ).pack()

        ctk.CTkLabel(
            logo_frame,
            text="Git Orchestrator",
            font=ctk.CTkFont(size=10),
            text_color=COLORS["text_secondary"],
        ).pack()

        # Séparateur
        ctk.CTkFrame(
            self.sidebar,
            height=1,
            fg_color=COLORS["border"]
        ).pack(fill="x", padx=15, pady=15)

        # Boutons de navigation
        nav_items = [
            ("dashboard", "📊  Dashboard",    "dashboard"),
            ("planner",   "📅  Planifier",     "planner"),
            ("drift",     "👁️   Dérive",        "drift"),
            ("logs",      "📋  Journaux",      "logs"),
            ("settings",  "⚙️   Paramètres",    "settings"),
        ]

        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for key, label, frame_name in nav_items:
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                anchor="w",
                font=ctk.CTkFont(size=13),
                fg_color="transparent",
                text_color=COLORS["text_secondary"],
                hover_color=COLORS["bg_card"],
                corner_radius=8,
                command=lambda fn=frame_name: self._show_frame(fn),
            )
            btn.pack(fill="x", padx=10, pady=3)
            self._nav_buttons[key] = btn

        # ── Kill Switch en bas ─────────────────────────────────────────────
        ctk.CTkFrame(
            self.sidebar,
            height=1,
            fg_color=COLORS["border"]
        ).pack(fill="x", padx=15, pady=(15, 5), side="bottom")

        self._kill_btn = ctk.CTkButton(
            self.sidebar,
            text="🔴  Kill Switch",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=COLORS["accent_red"],
            hover_color="#cc0033",
            corner_radius=8,
            command=self._toggle_kill_switch,
        )
        self._kill_btn.pack(fill="x", padx=10, pady=10, side="bottom")

        # Indicateur de statut
        self._status_label = ctk.CTkLabel(
            self.sidebar,
            text="● Système actif",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["accent_green"],
        )
        self._status_label.pack(pady=5, side="bottom")

    # ── Zone de contenu ───────────────────────────────────────────────────

    def _build_content_area(self):
        """Construit la zone de contenu principale (droite)."""
        self.content = ctk.CTkFrame(
            self,
            corner_radius=0,
            fg_color=COLORS["bg_primary"],
        )
        self.content.pack(side="right", fill="both", expand=True)

        # Construit tous les frames
        self._frames["dashboard"] = self._build_dashboard()
        self._frames["planner"]   = self._build_planner()
        self._frames["drift"]     = self._build_drift()
        self._frames["logs"]      = self._build_logs()
        self._frames["settings"]  = self._build_settings()

    def _show_frame(self, name: str):
        """Affiche un frame et met à jour la navigation."""
        # Cache tous les frames
        for frame in self._frames.values():
            frame.pack_forget()

        # Affiche le frame demandé
        self._frames[name].pack(fill="both", expand=True, padx=20, pady=20)

        # Met à jour les boutons de navigation
        for key, btn in self._nav_buttons.items():
            if key == name:
                btn.configure(
                    fg_color=COLORS["bg_card"],
                    text_color=COLORS["accent_green"],
                )
            else:
                btn.configure(
                    fg_color="transparent",
                    text_color=COLORS["text_secondary"],
                )

        # Rafraîchit les données si nécessaire
        if name == "dashboard":
            self._refresh_dashboard()
        elif name == "logs":
            self._refresh_logs()

    # ── Dashboard ─────────────────────────────────────────────────────────

    def _build_dashboard(self) -> ctk.CTkFrame:
        """Construit le tableau de bord principal."""
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        # En-tête
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 20))

        ctk.CTkLabel(
            header,
            text="Dashboard",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(side="left")

        self._refresh_btn = ctk.CTkButton(
            header,
            text="↻  Actualiser",
            width=120,
            fg_color=COLORS["bg_card"],
            hover_color=COLORS["border"],
            command=self._refresh_dashboard,
        )
        self._refresh_btn.pack(side="right")

        # ── Cartes de statistiques ─────────────────────────────────────────
        self._stats_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self._stats_frame.pack(fill="x", pady=(0, 20))

        # Placeholder des cartes (remplies au refresh)
        self._stat_cards = {}
        stats_config = [
            ("total",     "Total",      "📋", COLORS["accent_blue"]),
            ("completed", "Complétés",  "✅", COLORS["accent_green"]),
            ("pending",   "En attente", "⏳", COLORS["accent_orange"]),
            ("failed",    "Échoués",    "❌", COLORS["accent_red"]),
        ]

        for i, (key, label, icon, color) in enumerate(stats_config):
            card = self._make_stat_card(
                self._stats_frame, label, "0", icon, color
            )
            card.grid(row=0, column=i, padx=8, sticky="ew")
            self._stats_frame.columnconfigure(i, weight=1)
            self._stat_cards[key] = card

        # ── Liste des prochains commits ────────────────────────────────────
        ctk.CTkLabel(
            frame,
            text="Prochains commits planifiés",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(10, 5))

        self._upcoming_list = ctk.CTkScrollableFrame(
            frame,
            fg_color=COLORS["bg_secondary"],
            corner_radius=12,
            height=300,
        )
        self._upcoming_list.pack(fill="both", expand=True)

        return frame

    def _make_stat_card(
        self,
        parent,
        title:  str,
        value:  str,
        icon:   str,
        color:  str
    ) -> ctk.CTkFrame:
        """Crée une carte de statistique avec icône et valeur."""
        card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )

        ctk.CTkLabel(
            card,
            text=icon,
            font=ctk.CTkFont(size=24),
        ).pack(pady=(15, 5))

        # Stocker la référence pour mise à jour
        val_label = ctk.CTkLabel(
            card,
            text=value,
            font=ctk.CTkFont(size=32, weight="bold"),
            text_color=color,
        )
        val_label.pack()
        card._value_label = val_label

        ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_secondary"],
        ).pack(pady=(0, 15))

        return card

    def _refresh_dashboard(self):
        """Rafraîchit les données du dashboard."""
        projects = self.db.get_all_projects()

        if not projects:
            return

        # Agrège les stats de tous les projets
        total = completed = pending = failed = 0
        for p in projects:
            stats = self.db.get_project_stats(p.id)
            total     += stats.get("total", 0)
            completed += stats.get("completed", 0)
            pending   += stats.get("pending", 0)
            failed    += stats.get("failed", 0)

        # Met à jour les cartes
        self._stat_cards["total"]._value_label.configure(text=str(total))
        self._stat_cards["completed"]._value_label.configure(text=str(completed))
        self._stat_cards["pending"]._value_label.configure(text=str(pending))
        self._stat_cards["failed"]._value_label.configure(text=str(failed))

        # Met à jour la liste des prochains commits
        for widget in self._upcoming_list.winfo_children():
            widget.destroy()

        upcoming = self.db.get_upcoming_tasks(limit=15)
        if not upcoming:
            ctk.CTkLabel(
                self._upcoming_list,
                text="Aucun commit planifié à venir.",
                text_color=COLORS["text_secondary"],
            ).pack(pady=20)
        else:
            for task in upcoming:
                self._make_task_row(self._upcoming_list, task)

    def _make_task_row(self, parent, task: Task):
        """Crée une ligne de tâche dans la liste."""
        row = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_card"],
            corner_radius=8,
        )
        row.pack(fill="x", padx=5, pady=3)
        row.columnconfigure(1, weight=1)

        # Date
        ctk.CTkLabel(
            row,
            text=task.scheduled_time[:16],
            font=ctk.CTkFont(size=11, family="Courier"),
            text_color=COLORS["accent_green"],
            width=130,
        ).grid(row=0, column=0, padx=(10, 5), pady=8, sticky="w")

        # Nom du fichier
        ctk.CTkLabel(
            row,
            text=Path(task.file_path).name,
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_primary"],
            anchor="w",
        ).grid(row=0, column=1, padx=5, pady=8, sticky="ew")

        # Branche
        ctk.CTkLabel(
            row,
            text=task.branch_name,
            font=ctk.CTkFont(size=10),
            text_color=COLORS["accent_purple"],
            width=150,
        ).grid(row=0, column=2, padx=(5, 10), pady=8, sticky="e")

    # ── Planner ───────────────────────────────────────────────────────────

    def _build_planner(self) -> ctk.CTkFrame:
        """Construit l'interface de planification."""
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        ctk.CTkLabel(
            frame,
            text="Planifier un projet",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(0, 20))

        # ── Formulaire de planification ────────────────────────────────────
        form = ctk.CTkFrame(
            frame,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        form.pack(fill="x", pady=(0, 20))
        form.columnconfigure(1, weight=1)

        # Champs du formulaire
        fields = [
            ("Nom du projet",      "_inp_name",       "Mon-Challenge-30J"),
            ("Dossier source",     "_inp_folder",      "/chemin/vers/dossier"),
            ("Dépôt Git (local)",  "_inp_repo",        "/chemin/vers/repo"),
            ("Branche cible",      "_inp_branch",      "feat/mon-challenge"),
            ("Branche destination","_inp_target",      "main"),
            ("Jours total",        "_inp_days",        "30"),
            ("Date de début",      "_inp_start",       datetime.now().strftime("%Y-%m-%d")),
        ]

        for i, (label, attr, placeholder) in enumerate(fields):
            ctk.CTkLabel(
                form,
                text=label,
                font=ctk.CTkFont(size=12),
                text_color=COLORS["text_secondary"],
            ).grid(row=i, column=0, padx=(15, 10), pady=8, sticky="w")

            entry = ctk.CTkEntry(
                form,
                placeholder_text=placeholder,
                fg_color=COLORS["bg_secondary"],
                border_color=COLORS["border"],
            )
            entry.grid(row=i, column=1, padx=(0, 10), pady=8, sticky="ew")
            setattr(self, attr, entry)

            # Boutons de sélection de fichiers pour dossier et repo
            if attr in ("_inp_folder", "_inp_repo"):
                browse_btn = ctk.CTkButton(
                    form,
                    text="📂",
                    width=40,
                    fg_color=COLORS["bg_secondary"],
                    hover_color=COLORS["border"],
                    command=lambda e=entry: self._browse_folder(e),
                )
                browse_btn.grid(row=i, column=2, padx=(0, 15), pady=8)

        # ── Options avancées ───────────────────────────────────────────────
        options_frame = ctk.CTkFrame(form, fg_color="transparent")
        options_frame.grid(
            row=len(fields), column=0, columnspan=3,
            padx=15, pady=(5, 15), sticky="ew"
        )

        self._var_recursive = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options_frame,
            text="Scan récursif des sous-dossiers",
            variable=self._var_recursive,
            text_color=COLORS["text_secondary"],
        ).pack(side="left", padx=10)

        self._var_dry_run = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            options_frame,
            text="Mode simulation (Dry Run)",
            variable=self._var_dry_run,
            text_color=COLORS["text_secondary"],
        ).pack(side="left", padx=10)

        # ── Bouton principal ───────────────────────────────────────────────
        ctk.CTkButton(
            frame,
            text="⚡  Armer le système de planification",
            font=ctk.CTkFont(size=14, weight="bold"),
            height=50,
            fg_color=COLORS["accent_green"],
            hover_color="#00cc66",
            text_color="#000000",
            corner_radius=10,
            command=self._execute_plan,
        ).pack(fill="x", pady=(0, 15))

        # Zone de résultat
        self._plan_result = ctk.CTkTextbox(
            frame,
            fg_color=COLORS["bg_secondary"],
            text_color=COLORS["accent_green"],
            font=ctk.CTkFont(family="Courier", size=11),
            corner_radius=8,
            height=200,
        )
        self._plan_result.pack(fill="both", expand=True)

        return frame

    def _browse_folder(self, entry: ctk.CTkEntry):
        """Ouvre un dialogue de sélection de dossier."""
        folder = filedialog.askdirectory(title="Sélectionner un dossier")
        if folder:
            entry.delete(0, "end")
            entry.insert(0, folder)

    def _execute_plan(self):
        """Lance la planification depuis le formulaire GUI."""
        # Récupère les valeurs
        name    = self._inp_name.get().strip()
        folder  = self._inp_folder.get().strip()
        repo    = self._inp_repo.get().strip()
        branch  = self._inp_branch.get().strip() or "main"
        target  = self._inp_target.get().strip() or "main"
        dry_run = self._var_dry_run.get()
        recursive = self._var_recursive.get()

        try:
            days = int(self._inp_days.get().strip())
        except ValueError:
            messagebox.showerror("Erreur", "Le nombre de jours doit être un entier.")
            return

        try:
            start_date = datetime.strptime(
                self._inp_start.get().strip(), "%Y-%m-%d"
            )
        except ValueError:
            messagebox.showerror("Erreur", "Format de date invalide. Utilisez YYYY-MM-DD.")
            return

        if not name or not folder or not repo:
            messagebox.showerror("Erreur", "Nom, Dossier et Dépôt sont obligatoires.")
            return

        # Lance en arrière-plan pour ne pas bloquer l'UI
        def run_in_background():
            self._plan_result.delete("1.0", "end")
            self._log_result(f"⏱️  Démarrage de la planification : {name}\n")
            self._log_result(f"   Dossier : {folder}\n")
            self._log_result(f"   Jours   : {days}\n\n")

            try:
                if not dry_run:
                    project = Project(
                        name=name,
                        repo_path=repo,
                        source_folder=folder,
                        feature_branch=branch,
                        target_branch=target,
                    )
                    project_id = self.db.insert_project(project)
                else:
                    project_id = 0

                tasks = self.scanner.build_plan(
                    folder_path=folder,
                    project_id=project_id,
                    start_date=start_date,
                    days_count=days,
                    branch_name=branch,
                    recursive=recursive,
                )

                if not dry_run and tasks:
                    self.db.insert_tasks_bulk(tasks)

                self._log_result(f"✅ {len(tasks)} tâches {'simulées' if dry_run else 'planifiées'}.\n\n")

                for i, t in enumerate(tasks[:15]):
                    self._log_result(
                        f"  [{t.scheduled_time[:16]}] "
                        f"{Path(t.file_path).name} — {t.commit_message}\n"
                    )

                if len(tasks) > 15:
                    self._log_result(f"\n  ... et {len(tasks) - 15} autres tâches.\n")

                self._log_result(f"\n🎯 Système {'simulé' if dry_run else 'armé'} avec succès !")

            except Exception as e:
                self._log_result(f"\n❌ Erreur : {e}\n")
                logger.exception(f"Erreur de planification GUI : {e}")

        threading.Thread(target=run_in_background, daemon=True).start()

    def _log_result(self, text: str):
        """Ajoute du texte à la zone de résultat (thread-safe)."""
        self.after(0, lambda: self._plan_result.insert("end", text))

    # ── Drift Detection ───────────────────────────────────────────────────

    def _build_drift(self) -> ctk.CTkFrame:
        """Construit l'interface de détection de dérive."""
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        ctk.CTkLabel(
            frame,
            text="Détection de Dérive",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(0, 5))

        ctk.CTkLabel(
            frame,
            text="Compare vos fichiers locaux avec la file d'attente planifiée.",
            font=ctk.CTkFont(size=13),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 20))

        # Sélection du projet
        projects = self.db.get_all_projects()
        project_names = [f"[{p.id}] {p.name}" for p in projects] or ["Aucun projet"]

        self._drift_project_var = ctk.StringVar(value=project_names[0])
        ctk.CTkOptionMenu(
            frame,
            values=project_names,
            variable=self._drift_project_var,
            fg_color=COLORS["bg_card"],
        ).pack(anchor="w", pady=(0, 15))

        ctk.CTkButton(
            frame,
            text="🔍  Analyser la dérive",
            height=45,
            fg_color=COLORS["accent_blue"],
            hover_color="#006acc",
            command=self._run_drift_analysis,
        ).pack(fill="x", pady=(0, 15))

        # Zone de résultats
        self._drift_result = ctk.CTkScrollableFrame(
            frame,
            fg_color=COLORS["bg_secondary"],
            corner_radius=12,
        )
        self._drift_result.pack(fill="both", expand=True)

        return frame

    def _run_drift_analysis(self):
        """Lance l'analyse de dérive pour le projet sélectionné."""
        selection = self._drift_project_var.get()
        if "Aucun projet" in selection:
            messagebox.showinfo("Info", "Aucun projet à analyser.")
            return

        # Extrait l'ID depuis "[ID] Nom"
        try:
            project_id = int(selection.split("]")[0].replace("[", ""))
        except ValueError:
            return

        project = self.db.get_project(project_id)
        if not project or not project.source_folder:
            messagebox.showerror("Erreur", "Projet sans dossier source configuré.")
            return

        def analyze():
            result = self.scanner.analyze_drift(project.source_folder, project_id)

            def update_ui():
                # Nettoie les résultats précédents
                for w in self._drift_result.winfo_children():
                    w.destroy()

                # Résumé
                ctk.CTkLabel(
                    self._drift_result,
                    text=result.summary(),
                    font=ctk.CTkFont(family="Courier", size=12),
                    text_color=COLORS["accent_green"],
                    justify="left",
                ).pack(anchor="w", padx=10, pady=10)

                # Nouveaux fichiers avec bouton d'action
                if result.new_files:
                    ctk.CTkLabel(
                        self._drift_result,
                        text="🔵 Nouveaux fichiers :",
                        font=ctk.CTkFont(size=13, weight="bold"),
                        text_color=COLORS["accent_blue"],
                    ).pack(anchor="w", padx=10, pady=(10, 5))

                    for f in result.new_files:
                        row = ctk.CTkFrame(
                            self._drift_result,
                            fg_color=COLORS["bg_card"],
                            corner_radius=6,
                        )
                        row.pack(fill="x", padx=5, pady=2)

                        ctk.CTkLabel(
                            row,
                            text=Path(f).name,
                            font=ctk.CTkFont(size=12),
                        ).pack(side="left", padx=10, pady=6)

                        ctk.CTkButton(
                            row,
                            text="+ Planifier",
                            width=90,
                            height=28,
                            fg_color=COLORS["accent_blue"],
                            hover_color="#006acc",
                            font=ctk.CTkFont(size=11),
                            command=lambda fp=f: self._add_single_file(fp, project),
                        ).pack(side="right", padx=10, pady=5)

                # Fichiers modifiés avec actions
                if result.modified_files:
                    ctk.CTkLabel(
                        self._drift_result,
                        text="🟡 Fichiers modifiés localement :",
                        font=ctk.CTkFont(size=13, weight="bold"),
                        text_color=COLORS["accent_orange"],
                    ).pack(anchor="w", padx=10, pady=(15, 5))

                    for f in result.modified_files:
                        row = ctk.CTkFrame(
                            self._drift_result,
                            fg_color=COLORS["bg_card"],
                            corner_radius=6,
                        )
                        row.pack(fill="x", padx=5, pady=2)

                        ctk.CTkLabel(
                            row,
                            text=Path(f).name,
                            font=ctk.CTkFont(size=12),
                            text_color=COLORS["accent_orange"],
                        ).pack(side="left", padx=10, pady=6)

                        # Boutons d'action
                        btn_frame = ctk.CTkFrame(row, fg_color="transparent")
                        btn_frame.pack(side="right", padx=10)

                        ctk.CTkButton(
                            btn_frame,
                            text="Replanifier",
                            width=90,
                            height=28,
                            fg_color=COLORS["accent_orange"],
                            hover_color="#cc5500",
                            font=ctk.CTkFont(size=11),
                        ).pack(side="right", padx=3)

                        ctk.CTkButton(
                            btn_frame,
                            text="Ignorer",
                            width=70,
                            height=28,
                            fg_color=COLORS["bg_secondary"],
                            font=ctk.CTkFont(size=11),
                        ).pack(side="right", padx=3)

            self.after(0, update_ui)

        threading.Thread(target=analyze, daemon=True).start()

    def _add_single_file(self, file_path: str, project: Project):
        """Ajoute un seul fichier à la file d'attente."""
        new_tasks = self.scanner.append_new_files_to_plan(
            file_paths=[file_path],
            project_id=project.id,
            branch_name=project.feature_branch,
        )
        if new_tasks:
            self.db.insert_tasks_bulk(new_tasks)
            messagebox.showinfo(
                "Succès",
                f"✅ {Path(file_path).name} ajouté au plan pour le {new_tasks[0].scheduled_time[:10]}"
            )

    # ── Logs ──────────────────────────────────────────────────────────────

    def _build_logs(self) -> ctk.CTkFrame:
        """Construit l'interface des journaux d'exécution."""
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        ctk.CTkLabel(
            frame,
            text="Journaux d'exécution",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(0, 20))

        self._logs_textbox = ctk.CTkTextbox(
            frame,
            fg_color=COLORS["bg_secondary"],
            text_color=COLORS["accent_green"],
            font=ctk.CTkFont(family="Courier", size=11),
            corner_radius=8,
        )
        self._logs_textbox.pack(fill="both", expand=True)

        return frame

    def _refresh_logs(self):
        """Charge les logs récents depuis la DB."""
        projects = self.db.get_all_projects()
        self._logs_textbox.delete("1.0", "end")

        for project in projects:
            logs = self.db.get_recent_logs(project.id, limit=20)
            for log in logs:
                icon = "✅" if log.success else "❌"
                line = (
                    f"{icon} [{log.timestamp[:19]}] "
                    f"{log.action.upper():20} | {log.detail[:80]}\n"
                )
                self._logs_textbox.insert("end", line)

    # ── Settings ──────────────────────────────────────────────────────────

    def _build_settings(self) -> ctk.CTkFrame:
        """Construit l'interface des paramètres."""
        frame = ctk.CTkFrame(self.content, fg_color="transparent")

        ctk.CTkLabel(
            frame,
            text="Paramètres & Sécurité",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(0, 20))

        # ── Section Token ──────────────────────────────────────────────────
        token_card = ctk.CTkFrame(
            frame,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        token_card.pack(fill="x", pady=(0, 15))

        ctk.CTkLabel(
            token_card,
            text="🔑  Token GitHub",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=15, pady=(15, 5))

        token_status = self.keyring.get_token_preview()
        ctk.CTkLabel(
            token_card,
            text=f"Statut : {token_status}",
            font=ctk.CTkFont(size=12, family="Courier"),
            text_color=COLORS["accent_green"] if "****" in token_status else COLORS["accent_red"],
        ).pack(anchor="w", padx=15, pady=(0, 10))

        btn_row = ctk.CTkFrame(token_card, fg_color="transparent")
        btn_row.pack(anchor="w", padx=15, pady=(0, 15))

        ctk.CTkButton(
            btn_row,
            text="Configurer le token",
            fg_color=COLORS["accent_blue"],
            command=self._configure_token,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row,
            text="Supprimer",
            fg_color=COLORS["accent_red"],
            command=lambda: self.keyring.delete_token(),
        ).pack(side="left")

        # ── Section Info ───────────────────────────────────────────────────
        info_card = ctk.CTkFrame(
            frame,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        info_card.pack(fill="x")

        ctk.CTkLabel(
            info_card,
            text="ℹ️  À propos",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=15, pady=(15, 5))

        ctk.CTkLabel(
            info_card,
            text=(
                "Chronos-Git v1.0.0\n"
                "Git Lifecycle Orchestrator — Solution à l'oubli.\n"
                "Construit avec Python, GitPython, CustomTkinter et SQLite."
            ),
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", padx=15, pady=(0, 15))

        return frame

    def _configure_token(self):
        """Dialogue de configuration du token GitHub."""
        dialog = ctk.CTkInputDialog(
            text="Collez votre GitHub Personal Access Token :",
            title="Configuration du Token"
        )
        token = dialog.get_input()
        if token:
            success = self.keyring.store_token(token.strip())
            if success:
                messagebox.showinfo("Succès", "✅ Token stocké en sécurité dans le gestionnaire OS.")
            else:
                messagebox.showerror("Erreur", "Format de token invalide. Vérifiez votre PAT GitHub.")

    # ── Kill Switch ───────────────────────────────────────────────────────

    def _toggle_kill_switch(self):
        """Active ou désactive le Kill Switch d'urgence."""
        if self.executor.is_active:
            self.executor.activate_kill_switch()
            self._kill_btn.configure(
                text="🟢  Réactiver le système",
                fg_color=COLORS["accent_green"],
                hover_color="#00cc66",
                text_color="#000000",
            )
            self._status_label.configure(
                text="● ARRÊT D'URGENCE",
                text_color=COLORS["accent_red"],
            )
            messagebox.showwarning(
                "Kill Switch Activé",
                "🔴 Toutes les opérations automatiques sont suspendues.\n\n"
                "Cliquez à nouveau pour réactiver le système."
            )
        else:
            self.executor.deactivate_kill_switch()
            self._kill_btn.configure(
                text="🔴  Kill Switch",
                fg_color=COLORS["accent_red"],
                hover_color="#cc0033",
                text_color=COLORS["text_primary"],
            )
            self._status_label.configure(
                text="● Système actif",
                text_color=COLORS["accent_green"],
            )

    # ── Rattrapage automatique au démarrage ───────────────────────────────

    def _start_catchup_thread(self):
        """
        Lance le moteur de rattrapage en arrière-plan au démarrage.
        Ne bloque pas l'interface graphique.
        """
        def run_catchup():
            report = self.catchup.run()
            if report.has_work:
                # Notifie l'utilisateur via l'interface (thread-safe)
                self.after(1000, lambda: self._show_catchup_notification(report))

        threading.Thread(target=run_catchup, daemon=True).start()

    def _show_catchup_notification(self, report: CatchupReport):
        """Affiche une notification de rattrapage dans l'UI."""
        messagebox.showinfo(
            "⏱️  Rattrapage Automatique",
            f"Chronos-Git a rattrapé vos commits manquants :\n\n{report.summary()}"
        )
        self._refresh_dashboard()


# ── Point d'entrée GUI ────────────────────────────────────────────────────────

def launch_gui():
    """Lance l'interface graphique Chronos-Git."""
    db = Database()
    db.initialize()

    keyring = KeyringManager()

    app = ChronosApp(db, keyring)
    app.mainloop()
