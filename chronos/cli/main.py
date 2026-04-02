"""
chronos/cli/main.py
====================
Interface CLI complète de Chronos-Git, construite avec Typer + Rich.

Commandes disponibles :
  plan      — Planifier un dossier (auto ou via YAML)
  status    — Afficher l'état d'un projet
  catchup   — Forcer le rattrapage manuel
  security  — Gestion des secrets (token, GPG)
  projects  — Lister les projets enregistrés
  gui       — Lancer l'interface graphique
  kill      — Activer/désactiver le Kill Switch
"""

from pathlib import Path
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich import print as rprint

from chronos.core.database import Database, Project, MergeFrequency
from chronos.core.scanner import FolderScanner
from chronos.core.executor import GitExecutor
from chronos.core.catchup import CatchupEngine
from chronos.security.keyring_manager import KeyringManager
from chronos.utils.config import ChronosConfig
from chronos.utils.logger import get_logger

logger = get_logger(__name__)

# ── Application Typer principale ──────────────────────────────────────────────

app     = typer.Typer(
    name="chronos-git",
    help="⏱️  Chronos-Git — Orchestrateur de Workflow Git. Solution à l'oubli.",
    rich_markup_mode="rich",
    add_completion=True,
)
console = Console()

# ── Dépendances globales (initialisées une seule fois) ────────────────────────

_db      = Database()
_keyring = KeyringManager()
_scanner = FolderScanner(_db)
_executor = GitExecutor(_db, _keyring)
_catchup  = CatchupEngine(_db, _executor)


def get_db() -> Database:
    _db.initialize()
    return _db


# ── Commande : plan ──────────────────────────────────────────────────────────

@app.command()
def plan(
    folder: Optional[str] = typer.Option(
        None, "--folder", "-f",
        help="Dossier source à planifier"
    ),
    repo: Optional[str] = typer.Option(
        None, "--repo", "-r",
        help="Chemin du dépôt Git local"
    ),
    days: int = typer.Option(
        30, "--days", "-d",
        help="Nombre de jours sur lesquels répartir les commits"
    ),
    start: Optional[str] = typer.Option(
        None, "--start", "-s",
        help="Date de début (format: YYYY-MM-DD). Défaut: aujourd'hui"
    ),
    branch: str = typer.Option(
        "main", "--branch", "-b",
        help="Branche Git cible (ex: feat/mon-challenge)"
    ),
    config_file: Optional[str] = typer.Option(
        None, "--config", "-c",
        help="Fichier YAML de configuration (mode personnalisation absolue)"
    ),
    project_name: Optional[str] = typer.Option(
        None, "--name", "-n",
        help="Nom du projet Chronos-Git"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Simule la planification sans écrire en base de données"
    ),
    recursive: bool = typer.Option(
        False, "--recursive",
        help="Inclure les sous-dossiers dans le scan"
    ),
):
    """
    📅 Planifie un dossier de fichiers sur N jours.

    [bold]Mode Auto[/bold] (--folder + --days) :
      Répartition intelligente par date de création, avec jitter naturel.

    [bold]Mode Config[/bold] (--config plan.yaml) :
      Personnalisation absolue via fichier YAML.

    Exemples :
      chronos plan --folder ./30-days --days 30 --repo ./my-repo
      chronos plan --config plan.yaml --dry-run
    """
    db = get_db()

    # ── Mode YAML ────────────────────────────────────────────────────────
    if config_file:
        try:
            cfg = ChronosConfig(config_file)
            rprint(f"\n[bold cyan]📋 Configuration chargée :[/bold cyan]")
            for k, v in cfg.to_dict().items():
                rprint(f"  [dim]{k}[/dim] : [white]{v}[/white]")

            folder      = cfg.source_folder or folder
            repo        = cfg.repo_path
            days        = cfg.days_count
            start       = cfg.start_date.strftime("%Y-%m-%d")
            branch      = cfg.feature_branch
            project_name = cfg.project_name
            overrides   = cfg.overrides
            recursive   = cfg.recursive

        except (FileNotFoundError, ValueError) as e:
            rprint(f"[bold red]❌ Erreur de configuration :[/bold red] {e}")
            raise typer.Exit(1)
    else:
        overrides = {}

    # Validation des paramètres obligatoires
    if not folder:
        rprint("[bold red]❌ --folder est requis (ou utilisez --config)[/bold red]")
        raise typer.Exit(1)

    if not repo:
        rprint("[bold red]❌ --repo est requis (ou utilisez --config)[/bold red]")
        raise typer.Exit(1)

    # Parse de la date de début
    start_date = datetime.strptime(start, "%Y-%m-%d") if start else datetime.now()

    # Nom du projet par défaut
    name = project_name or Path(folder).name

    rprint(Panel(
        f"[bold]Projet[/bold]    : {name}\n"
        f"[bold]Dossier[/bold]  : {folder}\n"
        f"[bold]Dépôt[/bold]    : {repo}\n"
        f"[bold]Durée[/bold]    : {days} jours\n"
        f"[bold]Début[/bold]    : {start_date.strftime('%d/%m/%Y')}\n"
        f"[bold]Branche[/bold]  : {branch}\n"
        f"[bold]Récursif[/bold] : {'Oui' if recursive else 'Non'}",
        title="⏱️  [bold cyan]Chronos-Git — Planification[/bold cyan]",
        border_style="cyan"
    ))

    # Confirmation utilisateur
    if not dry_run:
        confirm = typer.confirm(
            "\n🚀 Lancer la planification ?",
            default=True
        )
        if not confirm:
            rprint("[yellow]Planification annulée.[/yellow]")
            raise typer.Exit(0)

    # ── Création du projet en base ────────────────────────────────────────
    project = Project(
        name=name,
        repo_path=repo,
        source_folder=folder,
        feature_branch=branch,
        target_branch="main",
        merge_frequency=MergeFrequency.MANUAL,
    )

    if not dry_run:
        project_id = db.insert_project(project)
    else:
        project_id = 0
        rprint("[bold yellow]🔍 Mode DRY RUN — Aucune modification en base de données[/bold yellow]")

    # ── Scan et planification ─────────────────────────────────────────────
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        transient=True,
    ) as progress:
        task_progress = progress.add_task("📂 Scan du dossier...", total=None)

        tasks = _scanner.build_plan(
            folder_path=folder,
            project_id=project_id,
            start_date=start_date,
            days_count=days,
            branch_name=branch,
            recursive=recursive,
            overrides=overrides,
        )
        progress.update(task_progress, description="💾 Insertion en base de données...")

        if not dry_run and tasks:
            db.insert_tasks_bulk(tasks)
            # Met à jour le total du projet
            db.update_project_progress(project_id, 0)

    # ── Affichage du résultat ─────────────────────────────────────────────
    if not tasks:
        rprint("[yellow]⚠️  Aucun fichier planifiable trouvé dans ce dossier.[/yellow]")
        raise typer.Exit(0)

    table = Table(
        title=f"📅 Plan généré : {len(tasks)} commits sur {days} jours",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Date prévue",     style="green",  width=20)
    table.add_column("Fichier",         style="white",  width=35)
    table.add_column("Message commit",  style="yellow", width=40)
    table.add_column("Branche",         style="cyan",   width=25)

    for t in tasks[:20]:  # Affiche les 20 premières pour ne pas surcharger
        table.add_row(
            t.scheduled_time,
            Path(t.file_path).name,
            t.commit_message,
            t.branch_name,
        )

    if len(tasks) > 20:
        table.add_row(
            f"... +{len(tasks) - 20} autres",
            "", "", "", style="dim"
        )

    console.print(table)

    if not dry_run:
        rprint(f"\n[bold green]✅ {len(tasks)} tâches planifiées avec succès ![/bold green]")
        rprint(f"   ID Projet : [cyan]{project_id}[/cyan]")
        rprint(f"   Utilisez [bold]chronos status --project {project_id}[/bold] pour suivre l'avancement.")
    else:
        rprint(f"\n[bold yellow]🔍 DRY RUN : {len(tasks)} tâches seraient créées.[/bold yellow]")


# ── Commande : status ────────────────────────────────────────────────────────

@app.command()
def status(
    project_id: Optional[int] = typer.Option(
        None, "--project", "-p",
        help="ID du projet à afficher. Affiche tous les projets si omis."
    ),
):
    """
    📊 Affiche l'état d'avancement des projets et de la file d'attente.
    """
    db = get_db()
    projects = db.get_all_projects()

    if not projects:
        rprint("[yellow]Aucun projet enregistré. Utilisez 'chronos plan' pour commencer.[/yellow]")
        return

    if project_id:
        projects = [p for p in projects if p.id == project_id]

    for project in projects:
        stats = db.get_project_stats(project.id)
        total = stats.get("total", 0)
        completed = stats.get("completed", 0)
        pending = stats.get("pending", 0)
        failed = stats.get("failed", 0)

        progress_pct = (completed / total * 100) if total > 0 else 0

        console.print(Panel(
            f"[bold]ID[/bold]        : {project.id}\n"
            f"[bold]Projet[/bold]    : {project.name}\n"
            f"[bold]Dépôt[/bold]     : {project.repo_path}\n"
            f"[bold]Branche[/bold]   : {project.feature_branch} → {project.target_branch}\n"
            f"[bold]Créé le[/bold]   : {project.created_at[:10]}\n"
            f"\n"
            f"[bold green]✅ Complétés[/bold green]  : {completed}/{total} ({progress_pct:.1f}%)\n"
            f"[bold yellow]⏳ En attente[/bold yellow] : {pending}\n"
            f"[bold red]❌ Échoués[/bold red]    : {failed}",
            title=f"[bold cyan]📁 {project.name}[/bold cyan]",
            border_style="cyan"
        ))

        # Prochaines tâches
        upcoming = db.get_upcoming_tasks(limit=5)
        if upcoming:
            table = Table(
                title="📅 Prochains commits planifiés",
                show_header=True,
                header_style="bold dim",
                border_style="dim",
            )
            table.add_column("Date",    style="green", width=20)
            table.add_column("Fichier", style="white", width=35)
            table.add_column("Message", style="dim",   width=35)

            for t in upcoming:
                table.add_row(
                    t.scheduled_time,
                    Path(t.file_path).name,
                    t.commit_message,
                )
            console.print(table)


# ── Commande : catchup ───────────────────────────────────────────────────────

@app.command()
def catchup(
    project_id: Optional[int] = typer.Option(
        None, "--project", "-p",
        help="Limiter le rattrapage à un projet spécifique."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Simule le rattrapage sans exécuter les commits."
    ),
):
    """
    ⏰ Rattrape manuellement toutes les tâches en retard.

    Normalement exécuté automatiquement au démarrage.
    Utile pour forcer un rattrapage à tout moment.
    """
    db = get_db()

    rprint("\n[bold cyan]🔍 Recherche de tâches en retard...[/bold cyan]")
    overdue = db.get_overdue_tasks()

    if not overdue:
        rprint("[bold green]✅ Aucune tâche en retard. Vous êtes parfaitement à jour ![/bold green]")
        return

    rprint(f"[bold yellow]⏱️  {len(overdue)} tâche(s) en retard détectée(s).[/bold yellow]")

    if not dry_run:
        confirm = typer.confirm("Démarrer le rattrapage ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
    ) as progress:
        prog_task = progress.add_task(
            "⏱️  Rattrapage en cours...",
            total=len(overdue)
        )

        def on_progress(done, total, task):
            progress.update(prog_task, completed=done)

        report = _catchup.run(
            project_id=project_id,
            progress_callback=on_progress,
            dry_run=dry_run,
        )

    console.print(Panel(
        report.summary(),
        title="[bold]Rapport de Rattrapage[/bold]",
        border_style="green" if report.failed == 0 else "red"
    ))


# ── Commande : security ──────────────────────────────────────────────────────

@app.command()
def security(
    action: str = typer.Argument(
        help="Action : setup-token | show-token | delete-token | audit"
    ),
):
    """
    🔒 Gestion sécurisée des secrets et de l'intégrité.

    Actions :
      setup-token  — Configure le token GitHub dans le Keyring OS
      show-token   — Affiche le token (masqué)
      delete-token — Supprime le token
      audit        — Lance Bandit sur le code source
    """
    if action == "setup-token":
        token = typer.prompt(
            "🔑 Collez votre GitHub Personal Access Token",
            hide_input=True
        )
        success = _keyring.store_token(token)
        if success:
            rprint("[bold green]✅ Token stocké en sécurité dans le gestionnaire OS.[/bold green]")
        else:
            rprint("[bold red]❌ Échec du stockage. Vérifiez le format du token.[/bold red]")

    elif action == "show-token":
        rprint(f"🔑 Token configuré : [cyan]{_keyring.get_token_preview()}[/cyan]")

    elif action == "delete-token":
        confirm = typer.confirm("⚠️  Supprimer le token GitHub ?", default=False)
        if confirm:
            _keyring.delete_token()
            rprint("[yellow]Token supprimé.[/yellow]")

    elif action == "audit":
        import subprocess
        rprint("[bold]🔍 Lancement de Bandit (analyse sécurité du code)...[/bold]")
        result = subprocess.run(
            ["bandit", "-r", "chronos/", "-ll"],
            capture_output=False
        )
        if result.returncode == 0:
            rprint("[bold green]✅ Aucune vulnérabilité détectée.[/bold green]")

    else:
        rprint(f"[red]Action inconnue : {action}[/red]")


# ── Commande : projects ──────────────────────────────────────────────────────

@app.command()
def projects():
    """📁 Liste tous les projets Chronos-Git enregistrés."""
    db = get_db()
    all_projects = db.get_all_projects()

    if not all_projects:
        rprint("[yellow]Aucun projet. Utilisez 'chronos plan' pour commencer.[/yellow]")
        return

    table = Table(
        title="📁 Projets Chronos-Git",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("ID",      style="cyan",   width=5)
    table.add_column("Nom",     style="white",  width=25)
    table.add_column("Dépôt",   style="dim",    width=40)
    table.add_column("Branche", style="green",  width=20)
    table.add_column("Créé le", style="yellow", width=12)

    for p in all_projects:
        table.add_row(
            str(p.id),
            p.name,
            p.repo_path,
            f"{p.feature_branch} → {p.target_branch}",
            p.created_at[:10],
        )

    console.print(table)


# ── Commande : drift ─────────────────────────────────────────────────────────

@app.command()
def drift(
    project_id: int = typer.Argument(help="ID du projet à analyser"),
):
    """
    👁️  Analyse la dérive entre les fichiers locaux et la file d'attente.

    Détecte :
    🔵 Nouveaux fichiers non planifiés
    🟡 Fichiers modifiés depuis leur planification
    🟢 Fichiers correctement planifiés
    """
    db = get_db()
    project = db.get_project(project_id)

    if not project:
        rprint(f"[red]Projet {project_id} introuvable.[/red]")
        raise typer.Exit(1)

    if not project.source_folder:
        rprint("[red]Ce projet n'a pas de dossier source configuré.[/red]")
        raise typer.Exit(1)

    rprint(f"\n[bold cyan]🔍 Analyse de dérive : {project.name}[/bold cyan]")

    result = _scanner.analyze_drift(project.source_folder, project_id)

    console.print(Panel(
        result.summary(),
        title="[bold]Rapport de Dérive[/bold]",
        border_style="yellow" if result.total_actionable > 0 else "green"
    ))

    if result.new_files:
        rprint("\n[bold blue]🔵 Nouveaux fichiers détectés :[/bold blue]")
        for f in result.new_files:
            rprint(f"  {Path(f).name}")

        if typer.confirm("\nAjouter ces fichiers à la file d'attente ?", default=True):
            new_tasks = _scanner.append_new_files_to_plan(
                file_paths=result.new_files,
                project_id=project_id,
                branch_name=project.feature_branch,
            )
            db.insert_tasks_bulk(new_tasks)
            rprint(f"[green]✅ {len(new_tasks)} fichiers ajoutés au plan.[/green]")

    if result.modified_files:
        rprint("\n[bold yellow]🟡 Fichiers modifiés :[/bold yellow]")
        for f in result.modified_files:
            rprint(f"  {Path(f).name}")
        rprint(
            "[dim]Action suggérée : Replanifier ces fichiers "
            "avec 'chronos plan' ou via la GUI.[/dim]"
        )


# ── Point d'entrée ────────────────────────────────────────────────────────────

def run_cli():
    """Point d'entrée pour la CLI."""
    get_db()  # Initialise la DB au démarrage
    app()
