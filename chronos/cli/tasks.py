"""
chronos/cli/tasks.py
=====================
Sous-application Typer pour la gestion fine des tâches planifiées.

Toutes les commandes nécessaires pour éditer, annuler, reprogrammer
et contrôler les tâches depuis le terminal.

Commandes :
  list        — Lister les tâches d'un projet (filtrable)
  show        — Détail complet d'une tâche
  edit        — Modifier message / date / branche d'une tâche
  cancel      — Annuler une ou plusieurs tâches
  cancel-all  — Annuler toutes les tâches PENDING
  cancel-range— Annuler les tâches dans une plage de dates
  reactivate  — Réactiver une tâche annulée
  reactivate-all — Réactiver toutes les tâches annulées
  reschedule  — Reprogrammer une tâche (raccourci de edit)
  shift       — Décaler tout un projet de N jours
  set-hour    — Fixer l'heure quotidienne de push
  set-days    — Choisir les jours de push (lun/mer/ven...)
  set-prefix  — Changer le préfixe de tous les messages
  swap        — Échanger les dates de deux tâches
  calendar    — Afficher le calendrier des tâches

Intégration dans main.py :
    from chronos.cli.tasks import tasks_app
    app.add_typer(tasks_app, name="task")
    # Puis : python main.py cli task list --project 1
"""

from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import print as rprint

from chronos.core.database import Database, TaskStatus
from chronos.core.task_manager import TaskManager

console = Console()
tasks_app = typer.Typer(
    name="task",
    help="🗂️  Gestion fine des tâches planifiées.",
    rich_markup_mode="rich",
)

# ── Dépendances ────────────────────────────────────────────────────────────────

_db  = Database()
_mgr : Optional[TaskManager] = None

def get_manager() -> TaskManager:
    global _mgr
    _db.initialize()
    if _mgr is None:
        _mgr = TaskManager(_db)
    return _mgr


# ── Helpers d'affichage ───────────────────────────────────────────────────────

STATUS_STYLE = {
    "PENDING":   "bold green",
    "FAILED":    "bold red",
    "SKIPPED":   "dim",
    "COMPLETED": "bold blue",
    "RUNNING":   "bold yellow",
}

STATUS_ICON = {
    "PENDING":   "⏳",
    "FAILED":    "❌",
    "SKIPPED":   "⏭",
    "COMPLETED": "✅",
    "RUNNING":   "⚙️",
}

def _status_badge(status: str) -> str:
    return f"{STATUS_ICON.get(status, '?')} {status}"

def _is_overdue(scheduled_time: str, status: str) -> bool:
    """True si la tâche est en attente et sa date est passée."""
    if status != "PENDING":
        return False
    try:
        dt = datetime.strptime(scheduled_time, "%Y-%m-%d %H:%M:%S")
        return dt < datetime.now()
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# ── LISTE ET CONSULTATION ────────────────════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

@tasks_app.command("list")
def list_tasks(
    project_id: int = typer.Argument(help="ID du projet"),
    status: Optional[str] = typer.Option(
        None, "--status", "-s",
        help="Filtre : PENDING | FAILED | SKIPPED | COMPLETED | RUNNING"
    ),
    search: Optional[str] = typer.Option(
        None, "--search", "-q",
        help="Mot-clé dans le message ou le nom de fichier"
    ),
    show_done: bool = typer.Option(
        False, "--all", "-a",
        help="Inclure les tâches COMPLETED et SKIPPED"
    ),
    limit: int = typer.Option(
        50, "--limit", "-l",
        help="Nombre maximum de tâches à afficher"
    ),
    day: Optional[str] = typer.Option(
        None, "--day",
        help="Filtrer par jour (YYYY-MM-DD)"
    ),
):
    """
    📋 Liste les tâches planifiées d'un projet.

    Exemples :
      task list 1
      task list 1 --status PENDING
      task list 1 --search "day 05"
      task list 1 --day 2026-04-15
      task list 1 --all
    """
    mgr = get_manager()

    if search:
        tasks = mgr.search_tasks(project_id, search)
    elif status:
        try:
            status_enum = TaskStatus(status.upper())
            tasks = mgr.get_tasks_by_status(project_id, status_enum)
        except ValueError:
            rprint(f"[red]Statut invalide : {status}[/red]")
            rprint(f"Valeurs acceptées : PENDING, FAILED, SKIPPED, COMPLETED, RUNNING")
            raise typer.Exit(1)
    elif day:
        tasks = mgr.get_tasks_for_day(project_id, day)
    else:
        tasks = mgr.get_all_tasks(project_id, include_done=show_done)

    if not tasks:
        rprint("[yellow]Aucune tâche trouvée avec ces critères.[/yellow]")
        return

    # Applique la limite
    total = len(tasks)
    tasks = tasks[:limit]

    # Tableau
    table = Table(
        title=f"📋 Tâches — Projet #{project_id}",
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
    )
    table.add_column("ID",       style="dim",   width=6)
    table.add_column("Statut",   width=16)
    table.add_column("Date / Heure",      width=18)
    table.add_column("Fichier",           width=30)
    table.add_column("Message de commit", width=38)
    table.add_column("Branche",           width=22)

    for task in tasks:
        overdue = _is_overdue(task.scheduled_time, task.status.value)
        date_style = "bold yellow" if overdue else "green"

        table.add_row(
            str(task.id),
            _status_badge(task.status.value),
            f"[{date_style}]{task.scheduled_time[:16]}[/{date_style}]"
            + (" ⚡" if overdue else ""),
            Path(task.file_path).name[:28],
            task.commit_message[:36],
            f"[purple]{task.branch_name[:20]}[/purple]",
        )

    console.print(table)

    if total > limit:
        rprint(f"[dim]... et {total - limit} autres tâches. Utilisez --limit pour en voir plus.[/dim]")

    # Résumé
    stats = _db.get_project_stats(project_id)
    rprint(
        f"\n[dim]Total : {stats.get('total',0)} | "
        f"✅ {stats.get('completed',0)} | "
        f"⏳ {stats.get('pending',0)} | "
        f"❌ {stats.get('failed',0)} | "
        f"⏭ {stats.get('skipped',0)}[/dim]"
    )


@tasks_app.command("show")
def show_task(
    task_id: int = typer.Argument(help="ID de la tâche"),
):
    """🔍 Affiche le détail complet d'une tâche."""
    mgr = get_manager()
    task = mgr._get_task_any_status(task_id)

    if task is None:
        rprint(f"[red]Tâche #{task_id} introuvable.[/red]")
        raise typer.Exit(1)

    overdue = _is_overdue(task.scheduled_time, task.status.value)
    overdue_tag = " [bold yellow]⚡ EN RETARD[/bold yellow]" if overdue else ""

    project = _db.get_project(task.project_id)
    project_name = project.name if project else "?"

    console.print(Panel(
        f"[bold]ID[/bold]          : {task.id}\n"
        f"[bold]Projet[/bold]      : [{task.project_id}] {project_name}\n"
        f"[bold]Statut[/bold]      : {_status_badge(task.status.value)}{overdue_tag}\n"
        f"\n"
        f"[bold]Fichier[/bold]     : {task.file_path}\n"
        f"[bold]Message[/bold]     : [yellow]{task.commit_message}[/yellow]\n"
        f"[bold]Branche[/bold]     : [purple]{task.branch_name}[/purple]\n"
        f"\n"
        f"[bold]Date prévue[/bold] : [green]{task.scheduled_time}[/green]\n"
        f"[bold]Créé le[/bold]     : {task.created_at[:16]}\n"
        f"[bold]Exécuté le[/bold]  : {task.executed_at or '—'}\n"
        f"[bold]Tentatives[/bold]  : {task.retry_count}/{_db.MAX_RETRY}\n"
        f"[bold]Hash SHA-256[/bold]: {(task.file_hash[:32] + '...') if task.file_hash else '—'}\n"
        + (f"\n[bold red]Erreur[/bold red] : {task.error_message}[/red]"
           if task.error_message else ""),
        title=f"[bold cyan]Tâche #{task.id}[/bold cyan]",
        border_style="cyan",
    ))


@tasks_app.command("calendar")
def show_calendar(
    project_id: int = typer.Argument(help="ID du projet"),
    weeks: int = typer.Option(4, "--weeks", "-w", help="Nombre de semaines à afficher"),
):
    """
    📅 Affiche le calendrier des tâches planifiées sous forme visuelle.

    Chaque jour avec des tâches est affiché avec le nombre de commits prévus.
    Les jours en retard sont mis en évidence.
    """
    mgr = get_manager()
    calendar = mgr.get_calendar_summary(project_id)

    if not calendar:
        rprint("[yellow]Aucune tâche planifiée pour ce projet.[/yellow]")
        return

    rprint(f"\n[bold cyan]📅 Calendrier — Projet #{project_id}[/bold cyan]\n")

    today = datetime.now().date()
    start = today
    end   = today + timedelta(weeks=weeks)

    current = start
    while current <= end:
        day_str = current.strftime("%Y-%m-%d")
        tasks   = calendar.get(day_str, [])

        if tasks or current == today:
            day_name = current.strftime("%a %d/%m")
            is_today = (current == today)
            is_past  = (current < today)

            if is_today:
                day_label = f"[bold white]→ {day_name}[/bold white]"
            elif is_past:
                day_label = f"[dim]{day_name}[/dim]"
            else:
                day_label = f"  {day_name}"

            if tasks:
                task_summary = ", ".join(
                    f"[{'green' if t.status == TaskStatus.PENDING else 'dim'}]"
                    f"{Path(t.file_path).name[:20]}[/]"
                    for t in tasks[:3]
                )
                suffix = f" +{len(tasks)-3}" if len(tasks) > 3 else ""
                overdue_marker = " [bold red]⚡[/bold red]" if is_past and any(
                    t.status == TaskStatus.PENDING for t in tasks
                ) else ""
                rprint(f"  {day_label}  [{len(tasks)} commit(s)]{overdue_marker}  {task_summary}{suffix}")
            elif is_today:
                rprint(f"  {day_label}  [dim]— Aucun commit[/dim]")

        current += timedelta(days=1)

    rprint()


# ══════════════════════════════════════════════════════════════════════════════
# ── ÉDITION INDIVIDUELLE ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@tasks_app.command("edit")
def edit_task(
    task_id: int = typer.Argument(help="ID de la tâche à modifier"),
    message: Optional[str] = typer.Option(
        None, "--message", "-m",
        help="Nouveau message de commit"
    ),
    date: Optional[str] = typer.Option(
        None, "--date", "-d",
        help="Nouvelle date planifiée (YYYY-MM-DD)"
    ),
    time: Optional[str] = typer.Option(
        None, "--time", "-t",
        help="Nouvelle heure (HH:MM)"
    ),
    branch: Optional[str] = typer.Option(
        None, "--branch", "-b",
        help="Nouvelle branche Git"
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Autoriser les dates dans le passé"
    ),
):
    """
    ✏️  Modifie une tâche planifiée.

    Seuls les champs fournis sont modifiés. Les autres restent inchangés.

    Exemples :
      task edit 5 --message "fix: corrected auth bug"
      task edit 5 --date 2026-04-20 --time 16:30
      task edit 5 --branch feat/hotfix --force
      task edit 5 -m "feat: complete" -d 2026-04-30 -t 23:00
    """
    mgr = get_manager()

    # Montre la tâche actuelle avant modification
    task = mgr._get_task_any_status(task_id)
    if task is None:
        raise typer.Exit(1)

    rprint(f"\n[dim]Avant :[/dim]")
    rprint(f"  Message : [yellow]{task.commit_message}[/yellow]")
    rprint(f"  Date    : [green]{task.scheduled_time}[/green]")
    rprint(f"  Branche : [purple]{task.branch_name}[/purple]")

    # Construit la nouvelle datetime si date ou heure fournie
    new_datetime = None
    if date or time:
        try:
            current_dt = datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S")
            base_date  = date or current_dt.strftime("%Y-%m-%d")
            base_time  = time or current_dt.strftime("%H:%M")
            # Accepte HH:MM ou HH:MM:SS
            if len(base_time) == 5:
                base_time += ":00"
            new_datetime = f"{base_date} {base_time}"
        except ValueError as e:
            rprint(f"[red]Format invalide : {e}[/red]")
            raise typer.Exit(1)

    success = mgr.edit_task(
        task_id=task_id,
        new_message=message,
        new_datetime=new_datetime,
        new_branch=branch,
        force=force,
    )

    if success:
        # Affiche le résultat
        updated = mgr._get_task_any_status(task_id)
        rprint(f"\n[dim]Après :[/dim]")
        rprint(f"  Message : [bold yellow]{updated.commit_message}[/bold yellow]")
        rprint(f"  Date    : [bold green]{updated.scheduled_time}[/bold green]")
        rprint(f"  Branche : [bold purple]{updated.branch_name}[/bold purple]")
        rprint(f"\n[bold green]✅ Tâche #{task_id} modifiée avec succès.[/bold green]")
    else:
        rprint(f"\n[red]❌ Modification échouée. Vérifiez le statut de la tâche.[/red]")


@tasks_app.command("reschedule")
def reschedule_task(
    task_id: int  = typer.Argument(help="ID de la tâche"),
    new_date: str = typer.Argument(help="Nouvelle date (YYYY-MM-DD ou YYYY-MM-DD HH:MM)"),
    force: bool   = typer.Option(False, "--force", help="Autoriser les dates passées"),
):
    """
    📅 Reprogramme une tâche à une nouvelle date/heure.

    Raccourci de 'task edit --date'.

    Exemples :
      task reschedule 5 2026-04-20
      task reschedule 5 "2026-04-20 16:30"
      task reschedule 5 "2026-04-01 09:00" --force
    """
    mgr = get_manager()
    success = mgr.reschedule(task_id, new_date, force=force)
    if success:
        rprint(f"[bold green]✅ Tâche #{task_id} reprogrammée → {new_date}[/bold green]")
    else:
        rprint("[red]❌ Reprogrammation échouée.[/red]")


@tasks_app.command("swap")
def swap_tasks(
    task_id_a: int = typer.Argument(help="ID de la première tâche"),
    task_id_b: int = typer.Argument(help="ID de la deuxième tâche"),
):
    """
    🔀 Échange les dates planifiées de deux tâches.

    Utile pour réordonner manuellement deux fichiers dans la timeline.

    Exemple :
      task swap 3 7   # La tâche 3 prend la date de 7 et vice versa
    """
    mgr = get_manager()

    t_a = mgr._get_task_any_status(task_id_a)
    t_b = mgr._get_task_any_status(task_id_b)

    if t_a is None or t_b is None:
        raise typer.Exit(1)

    rprint(
        f"\nSwap :\n"
        f"  #{task_id_a} [{t_a.scheduled_time[:16]}] {Path(t_a.file_path).name}\n"
        f"  #{task_id_b} [{t_b.scheduled_time[:16]}] {Path(t_b.file_path).name}\n"
    )
    confirm = typer.confirm("Confirmer l'échange ?", default=True)
    if not confirm:
        raise typer.Exit(0)

    success = mgr.swap_schedule(task_id_a, task_id_b)
    if success:
        rprint(f"[bold green]✅ Swap effectué.[/bold green]")


# ══════════════════════════════════════════════════════════════════════════════
# ── ANNULATION ───────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@tasks_app.command("cancel")
def cancel_task(
    task_id: int = typer.Argument(help="ID de la tâche à annuler"),
    reason: Optional[str] = typer.Option(
        None, "--reason", "-r",
        help="Raison de l'annulation (pour les logs)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirmer sans demander"),
):
    """
    🚫 Annule une tâche planifiée individuelle.

    La tâche passe en SKIPPED et ne sera plus jamais exécutée
    (même après un redémarrage). Peut être réactivée avec 'task reactivate'.

    Exemples :
      task cancel 5
      task cancel 5 --reason "Fichier supprimé"
      task cancel 5 --yes
    """
    mgr = get_manager()
    task = mgr._get_task_any_status(task_id)

    if task is None:
        raise typer.Exit(1)

    rprint(
        f"\n[yellow]Tâche à annuler :[/yellow]\n"
        f"  #{task_id} | {task.scheduled_time[:16]} | "
        f"{Path(task.file_path).name} | {task.commit_message}"
    )

    if not yes:
        confirm = typer.confirm("\nConfirmer l'annulation ?", default=False)
        if not confirm:
            rprint("[dim]Annulé.[/dim]")
            raise typer.Exit(0)

    success = mgr.cancel(task_id, reason=reason or "")
    if success:
        rprint(f"[bold green]✅ Tâche #{task_id} annulée.[/bold green]")


@tasks_app.command("cancel-all")
def cancel_all(
    project_id: int = typer.Argument(help="ID du projet"),
    reason: Optional[str] = typer.Option(
        None, "--reason", "-r",
        help="Raison de l'annulation"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirmer sans demander"),
):
    """
    🚫 Annule TOUTES les tâches PENDING d'un projet.

    Utile pour réinitialiser un projet ou changer de stratégie.
    Les tâches COMPLETED ne sont pas affectées.

    Exemple :
      task cancel-all 1
      task cancel-all 1 --reason "Nouveau planning" --yes
    """
    mgr = get_manager()
    pending = mgr.get_tasks_by_status(project_id, TaskStatus.PENDING)

    if not pending:
        rprint("[yellow]Aucune tâche PENDING à annuler.[/yellow]")
        return

    rprint(f"\n[bold yellow]⚠️  {len(pending)} tâche(s) PENDING seront annulées.[/bold yellow]")

    if not yes:
        confirm = typer.confirm("\nConfirmer l'annulation en masse ?", default=False)
        if not confirm:
            rprint("[dim]Annulé.[/dim]")
            raise typer.Exit(0)

    result = mgr.cancel_all_pending(project_id, reason=reason or "cancel-all CLI")
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


@tasks_app.command("cancel-range")
def cancel_range(
    project_id: int = typer.Argument(help="ID du projet"),
    from_date:  str = typer.Argument(help="Date de début (YYYY-MM-DD)"),
    to_date:    str = typer.Argument(help="Date de fin (YYYY-MM-DD)"),
    reason: Optional[str] = typer.Option(None, "--reason"),
    yes:    bool = typer.Option(False, "--yes", "-y"),
):
    """
    🚫 Annule les tâches dans une plage de dates.

    Utile pour supprimer les commits d'une semaine de vacances.

    Exemple :
      task cancel-range 1 2026-04-14 2026-04-18
      task cancel-range 1 2026-04-14 2026-04-18 --reason "Vacances" --yes
    """
    mgr = get_manager()

    # Compte les tâches affectées pour informer l'utilisateur
    tasks_in_range = mgr.get_all_tasks(project_id)
    try:
        dt_from = datetime.strptime(from_date, "%Y-%m-%d")
        dt_to   = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59)
    except ValueError:
        rprint("[red]Format de date invalide. Utilisez YYYY-MM-DD.[/red]")
        raise typer.Exit(1)

    affected = [
        t for t in tasks_in_range
        if t.status == TaskStatus.PENDING
        and dt_from <= datetime.strptime(t.scheduled_time, "%Y-%m-%d %H:%M:%S") <= dt_to
    ]

    if not affected:
        rprint(f"[yellow]Aucune tâche PENDING entre {from_date} et {to_date}.[/yellow]")
        return

    rprint(f"\n[yellow]⚠️  {len(affected)} tâche(s) seront annulées ({from_date} → {to_date})[/yellow]")

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=False)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.cancel_range(project_id, from_date, to_date, reason=reason or "")
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


# ══════════════════════════════════════════════════════════════════════════════
# ── RÉACTIVATION ─────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@tasks_app.command("reactivate")
def reactivate_task(
    task_id: int = typer.Argument(help="ID de la tâche à réactiver"),
    new_date: Optional[str] = typer.Option(
        None, "--date", "-d",
        help="Nouvelle date (optionnel, sinon conserve l'originale)"
    ),
):
    """
    🔄 Réactive une tâche SKIPPED ou FAILED.

    La tâche repasse en PENDING et sera exécutée normalement.
    Si elle est dans le passé, le moteur de rattrapage l'exécutera au prochain démarrage.

    Exemples :
      task reactivate 5
      task reactivate 5 --date "2026-04-25 16:00"
    """
    mgr = get_manager()
    success = mgr.reactivate(task_id, new_datetime=new_date)
    if success:
        rprint(f"[bold green]✅ Tâche #{task_id} réactivée.[/bold green]")


@tasks_app.command("reactivate-all")
def reactivate_all_skipped(
    project_id: int = typer.Argument(help="ID du projet"),
    shift_days: int = typer.Option(
        0, "--shift", "-s",
        help="Décaler les tâches réactivées de N jours"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """
    🔄 Réactive toutes les tâches SKIPPED d'un projet.

    Utile après une annulation accidentelle ou une longue pause.

    Exemples :
      task reactivate-all 1
      task reactivate-all 1 --shift 7     # Reprend dans 7 jours
      task reactivate-all 1 --shift 0 --yes
    """
    mgr = get_manager()
    skipped = mgr.get_tasks_by_status(project_id, TaskStatus.SKIPPED)

    if not skipped:
        rprint("[yellow]Aucune tâche SKIPPED à réactiver.[/yellow]")
        return

    shift_msg = f" (décalées de +{shift_days} jours)" if shift_days else ""
    rprint(f"\n[cyan]{len(skipped)} tâche(s) SKIPPED seront réactivées{shift_msg}.[/cyan]")

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.reactivate_all_skipped(project_id, shift_days=shift_days)
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


# ══════════════════════════════════════════════════════════════════════════════
# ── OPÉRATIONS DE MASSE ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

@tasks_app.command("shift")
def shift_project(
    project_id:  int = typer.Argument(help="ID du projet"),
    days:        int = typer.Argument(help="Jours à ajouter (négatif pour reculer)"),
    from_date: Optional[str] = typer.Option(
        None, "--from",
        help="N'appliquer qu'à partir de cette date (YYYY-MM-DD)"
    ),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """
    📅 Décale TOUTES les tâches PENDING d'un projet de N jours.

    Utile quand un challenge prend du retard ou qu'on veut avancer le planning.

    Exemples :
      task shift 1 3          # Décale tout de +3 jours
      task shift 1 -2         # Avance tout de 2 jours
      task shift 1 7 --from 2026-04-15   # Décale seulement à partir du 15/04
    """
    mgr = get_manager()
    pending = mgr.get_tasks_by_status(project_id, TaskStatus.PENDING)

    direction = "+" if days >= 0 else ""
    rprint(
        f"\n[cyan]{len(pending)} tâche(s) PENDING seront décalées de {direction}{days} jour(s).[/cyan]"
        + (f"\n[dim]À partir du {from_date}[/dim]" if from_date else "")
    )

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.shift_all(project_id, days_offset=days, from_date=from_date)
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


@tasks_app.command("set-hour")
def set_push_hour(
    project_id: int = typer.Argument(help="ID du projet"),
    hour:       int = typer.Argument(help="Heure de push (0-23)"),
    minute:     int = typer.Option(0,  "--minute", "-m", help="Minute (0-59)"),
    jitter:     int = typer.Option(20, "--jitter", "-j", help="Jitter ±minutes"),
    from_date: Optional[str] = typer.Option(None, "--from", help="À partir du (YYYY-MM-DD)"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """
    🕐 Fixe l'heure quotidienne de push pour toutes les tâches PENDING.

    Exemples :
      task set-hour 1 18              # Tous les commits à 18h00
      task set-hour 1 22 --minute 30  # À 22h30
      task set-hour 1 16 --jitter 30  # Entre 15h30 et 16h30 (naturel)
      task set-hour 1 9 --from 2026-04-20
    """
    mgr = get_manager()

    rprint(
        f"\n[cyan]Heure de push → {hour:02d}:{minute:02d} (jitter ±{jitter}min)[/cyan]"
        + (f"\n[dim]À partir du {from_date}[/dim]" if from_date else "")
    )

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.set_daily_push_time(
        project_id, hour=hour, minute=minute,
        jitter_min=jitter, from_date=from_date
    )
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


@tasks_app.command("set-days")
def set_push_days(
    project_id: int = typer.Argument(help="ID du projet"),
    days: str = typer.Argument(
        help="Jours autorisés séparés par virgule (0=Lun, 1=Mar, ..., 6=Dim)\nEx: '0,2,4' = Lun/Mer/Ven"
    ),
    hour:   int = typer.Option(16, "--hour",   "-H", help="Heure de push"),
    minute: int = typer.Option(0,  "--minute", "-m", help="Minute"),
    yes:    bool = typer.Option(False, "--yes", "-y"),
):
    """
    📆 Redistribue les tâches sur certains jours de la semaine uniquement.

    Exemples :
      task set-days 1 "0,2,4"        # Lun/Mer/Ven à 16h00
      task set-days 1 "1,3" --hour 20   # Mar/Jeu à 20h00
      task set-days 1 "0,1,2,3,4"   # Tous les jours ouvrés
    """
    mgr = get_manager()

    try:
        allowed = [int(d.strip()) for d in days.split(",") if d.strip()]
        if not all(0 <= d <= 6 for d in allowed):
            raise ValueError("Jours invalides")
    except ValueError:
        rprint("[red]Format invalide. Utilisez des entiers 0-6 séparés par virgule.[/red]")
        raise typer.Exit(1)

    day_names = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    days_str  = " / ".join(day_names[d] for d in sorted(set(allowed)))
    rprint(f"\n[cyan]Jours de push → [{days_str}] à {hour:02d}:{minute:02d}[/cyan]")

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.set_push_days(project_id, allowed_days=allowed, hour=hour, minute=minute)
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")


@tasks_app.command("set-prefix")
def set_commit_prefix(
    project_id: int = typer.Argument(help="ID du projet"),
    prefix:     str = typer.Argument(
        help="Préfixe : feat | fix | docs | refactor | chore | perf | test | style"
    ),
    from_date: Optional[str] = typer.Option(None, "--from"),
    to_date:   Optional[str] = typer.Option(None, "--to"),
    yes:       bool = typer.Option(False, "--yes", "-y"),
):
    """
    ✏️  Change le préfixe de commit de toutes les tâches PENDING.

    Conserve le texte après le ':' et remplace seulement le préfixe.

    Exemples :
      task set-prefix 1 docs
      task set-prefix 1 feat --from 2026-04-15 --to 2026-04-30
    """
    mgr = get_manager()

    date_range = ""
    if from_date or to_date:
        date_range = f" ({from_date or '...'} → {to_date or '...'})"

    rprint(f"\n[cyan]Préfixe → '{prefix}'{date_range}[/cyan]")

    if not yes:
        confirm = typer.confirm("Confirmer ?", default=True)
        if not confirm:
            raise typer.Exit(0)

    result = mgr.bulk_edit_messages(
        project_id, prefix=prefix,
        from_date=from_date, to_date=to_date
    )
    rprint(f"\n[bold green]✅ {result.summary()}[/bold green]")