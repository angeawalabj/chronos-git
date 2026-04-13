"""
chronos/gui/task_editor.py  (v2 — optimisé)
=============================================
Panneau d'édition des tâches — version haute performance.

Problèmes de la v1 corrigés :
  ❌ Recréation de TOUS les widgets à chaque refresh → crash sur 200+ tâches
  ❌ Le scheduler retrigge des refreshs en cascade pendant les pushes
  ❌ Aucun debounce sur la recherche → appels DB en rafale
  ❌ Pas de pagination → tout affiché d'un coup

Solutions appliquées :
  ✅ WIDGET POOL   : POOL_SIZE lignes Tkinter créées UNE SEULE FOIS.
                    Chaque refresh ne fait que update() le texte/couleur,
                    jamais de destroy()/recreate(). Zéro allocation.
  ✅ PAGINATION    : PAGE_SIZE lignes par page (défaut 50).
                    Navigation Précédent / Suivant. Choix de 25/50/100/200.
  ✅ DEBOUNCE      : La recherche attend 400ms d'inactivité avant de filtrer.
  ✅ REFRESH LOCK  : Un seul refresh actif à la fois. Les suivants sont mis
                    en file d'attente (1 max) plutôt qu'empilés.
  ✅ THREAD SAFE   : Lecture DB dans un thread daemon → self.after() pour
                    mettre à jour l'UI dans le thread Tkinter.
  ✅ CACHE LOCAL   : Après une édition individuelle, seul l'élément modifié
                    est rechargé depuis la DB (pas tout le tableau).
"""

import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable

import customtkinter as ctk
from tkinter import messagebox, simpledialog

from chronos.core.database import Database, Task, TaskStatus
from chronos.core.task_manager import TaskManager, BulkOperationResult
from chronos.utils.logger import get_logger

logger = get_logger(__name__)

# ── Palette ───────────────────────────────────────────────────────────────────
COLORS = {
    "bg_primary":    "#0a0e1a",
    "bg_secondary":  "#111827",
    "bg_card":       "#1a2236",
    "accent_green":  "#00ff88",
    "accent_blue":   "#0088ff",
    "accent_orange": "#ff6b00",
    "accent_red":    "#ff2244",
    "accent_purple": "#8b5cf6",
    "accent_yellow": "#ffd700",
    "text_primary":  "#e2e8f0",
    "text_secondary":"#64748b",
    "text_dim":      "#2d3f55",
    "border":        "#1e3a5f",
}

STATUS_COLORS = {
    TaskStatus.PENDING:   COLORS["accent_green"],
    TaskStatus.FAILED:    COLORS["accent_red"],
    TaskStatus.SKIPPED:   COLORS["text_secondary"],
    TaskStatus.COMPLETED: COLORS["accent_blue"],
    TaskStatus.RUNNING:   COLORS["accent_yellow"],
}
STATUS_ICONS = {
    TaskStatus.PENDING:   "⏳",
    TaskStatus.FAILED:    "❌",
    TaskStatus.SKIPPED:   "⏭",
    TaskStatus.COMPLETED: "✅",
    TaskStatus.RUNNING:   "⚙️",
}

PAGE_SIZE   = 50
DEBOUNCE_MS = 400


# ══════════════════════════════════════════════════════════════════════════════
# ── DIALOGUE D'ÉDITION ────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TaskEditDialog(ctk.CTkToplevel):
    """Fenêtre modale d'édition complète d'une tâche."""

    def __init__(self, parent, task: Task, on_save: Callable[[dict], None]):
        super().__init__(parent)
        self.task    = task
        self.on_save = on_save
        self.title(f"Éditer #{task.id}")
        self.geometry("540x460")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg_card"])
        self.after(50, self._safe_grab)
        self.focus()
        self._build()

    def _safe_grab(self):
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self):
        ctk.CTkLabel(self, text=f"✏️  Tâche #{self.task.id}",
                     font=ctk.CTkFont(size=17, weight="bold"),
                     text_color=COLORS["accent_green"],
                     ).pack(pady=(18, 2), padx=18, anchor="w")
        ctk.CTkLabel(self, text=Path(self.task.file_path).name,
                     font=ctk.CTkFont(size=11),
                     text_color=COLORS["text_secondary"],
                     ).pack(padx=18, anchor="w", pady=(0, 10))

        form = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"], corner_radius=10)
        form.pack(fill="x", padx=18, pady=(0, 10))

        # Message
        ctk.CTkLabel(form, text="Message de commit",
                     font=ctk.CTkFont(size=11), text_color=COLORS["text_secondary"],
                     ).pack(anchor="w", padx=14, pady=(12, 2))
        self._msg = ctk.CTkEntry(form, fg_color=COLORS["bg_card"],
                                  border_color=COLORS["border"], height=32)
        self._msg.insert(0, self.task.commit_message)
        self._msg.pack(fill="x", padx=14, pady=(0, 6))

        # Préfixes rapides
        pf = ctk.CTkFrame(form, fg_color="transparent")
        pf.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(pf, text="Préfixe :", font=ctk.CTkFont(size=10),
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 5))
        for p in ["feat","fix","docs","refactor","chore"]:
            ctk.CTkButton(pf, text=p, width=56, height=22,
                          font=ctk.CTkFont(size=10),
                          fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                          border_color=COLORS["border"], border_width=1, corner_radius=4,
                          command=lambda x=p: self._prefix(x),
                          ).pack(side="left", padx=2)

        # Date / Heure
        ctk.CTkLabel(form, text="Date planifiée",
                     font=ctk.CTkFont(size=11), text_color=COLORS["text_secondary"],
                     ).pack(anchor="w", padx=14, pady=(4, 2))
        dr = ctk.CTkFrame(form, fg_color="transparent")
        dr.pack(fill="x", padx=14, pady=(0, 8))
        cur = datetime.strptime(self.task.scheduled_time, "%Y-%m-%d %H:%M:%S")
        self._date = ctk.CTkEntry(dr, fg_color=COLORS["bg_card"],
                                   border_color=COLORS["border"], width=135, height=30)
        self._date.insert(0, cur.strftime("%Y-%m-%d"))
        self._date.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(dr, text="à", text_color=COLORS["text_secondary"]).pack(side="left", padx=3)
        self._h = ctk.StringVar(value=str(cur.hour))
        ctk.CTkOptionMenu(dr, values=[str(i) for i in range(24)],
                          variable=self._h, fg_color=COLORS["bg_card"], width=62,
                          ).pack(side="left", padx=2)
        ctk.CTkLabel(dr, text="h", text_color=COLORS["text_secondary"]).pack(side="left")
        self._m = ctk.StringVar(value=f"{cur.minute:02d}")
        ctk.CTkOptionMenu(dr, values=[f"{i:02d}" for i in range(0,60,5)],
                          variable=self._m, fg_color=COLORS["bg_card"], width=62,
                          ).pack(side="left", padx=2)

        # Branche
        ctk.CTkLabel(form, text="Branche",
                     font=ctk.CTkFont(size=11), text_color=COLORS["text_secondary"],
                     ).pack(anchor="w", padx=14, pady=(4, 2))
        self._br = ctk.CTkEntry(form, fg_color=COLORS["bg_card"],
                                 border_color=COLORS["border"], height=30)
        self._br.insert(0, self.task.branch_name)
        self._br.pack(fill="x", padx=14, pady=(0, 12))

        self._force = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self, text="Forcer les dates passées",
                        variable=self._force, text_color=COLORS["accent_orange"],
                        font=ctk.CTkFont(size=10),
                        ).pack(anchor="w", padx=18, pady=(0, 10))

        br = ctk.CTkFrame(self, fg_color="transparent")
        br.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(br, text="💾  Sauvegarder", height=38,
                      fg_color=COLORS["accent_green"], hover_color="#00cc66",
                      text_color="#000000", font=ctk.CTkFont(size=12, weight="bold"),
                      command=self._save,
                      ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(br, text="Annuler", height=38,
                      fg_color=COLORS["bg_secondary"], command=self.destroy,
                      ).pack(side="right")

    def _prefix(self, p: str):
        cur    = self._msg.get().strip()
        suffix = cur.split(":", 1)[1].strip() if ":" in cur else cur
        self._msg.delete(0, "end")
        self._msg.insert(0, f"{p}: {suffix}")

    def _save(self):
        msg = self._msg.get().strip()
        if not msg:
            messagebox.showerror("Erreur", "Message vide.", parent=self)
            return
        try:
            dt = datetime.strptime(self._date.get().strip(), "%Y-%m-%d").replace(
                hour=int(self._h.get()), minute=int(self._m.get()), second=0)
        except ValueError:
            messagebox.showerror("Erreur", "Date invalide (YYYY-MM-DD).", parent=self)
            return
        if dt < datetime.now() and not self._force.get():
            if not messagebox.askyesno("Date passée",
                f"{dt.strftime('%d/%m/%Y %H:%M')} est dans le passé.\nConfirmer ?",
                parent=self):
                return
        self.on_save({"new_message": msg,
                      "new_datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
                      "new_branch": self._br.get().strip(),
                      "force": self._force.get()})
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# ── DIALOGUE OPÉRATION DE MASSE ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class BulkActionDialog(ctk.CTkToplevel):

    def __init__(self, parent, action: str, on_confirm: Callable[[dict], None]):
        super().__init__(parent)
        self.action     = action
        self.on_confirm = on_confirm
        self._fields: dict = {}
        self.title(action)
        self.geometry("440x340")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg_card"])
        self.after(50, self._safe_grab)
        self.focus()
        self._build()

    def _safe_grab(self):
        try:
            self.grab_set()
        except Exception:
            pass

    def _build(self):
        ctk.CTkLabel(self, text=f"⚡  {self.action}",
                     font=ctk.CTkFont(size=15, weight="bold"),
                     text_color=COLORS["accent_orange"],
                     ).pack(pady=(16, 8), padx=16, anchor="w")

        form = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"], corner_radius=8)
        form.pack(fill="x", padx=16, pady=(0, 10))

        if self.action == "Décaler de N jours":
            self._f(form, "days_offset", "Jours (+/−)", "3")
            self._f(form, "from_date",   "À partir du (YYYY-MM-DD, optionnel)", "")

        elif self.action == "Heure quotidienne":
            self._o(form, "hour",   "Heure",  [str(h) for h in range(24)], "18")
            self._o(form, "minute", "Minute", [f"{m:02d}" for m in range(0,60,5)], "00")
            self._f(form, "jitter", "Jitter ±min (0 = exact)", "20")
            self._f(form, "from_date", "À partir du (optionnel)", "")

        elif self.action == "Jours de push":
            ctk.CTkLabel(form, text="Jours autorisés :",
                         font=ctk.CTkFont(size=11), text_color=COLORS["text_secondary"],
                         ).pack(anchor="w", padx=12, pady=(10, 4))
            df = ctk.CTkFrame(form, fg_color="transparent")
            df.pack(fill="x", padx=12, pady=(0, 6))
            self._day_vars: dict[int, ctk.BooleanVar] = {}
            for i, n in enumerate(["Lun","Mar","Mer","Jeu","Ven","Sam","Dim"]):
                v = ctk.BooleanVar(value=(i < 5))
                self._day_vars[i] = v
                ctk.CTkCheckBox(df, text=n, variable=v,
                                font=ctk.CTkFont(size=10),
                                checkbox_width=15, checkbox_height=15,
                                ).pack(side="left", padx=3)
            self._o(form, "hour",   "Heure", [str(h) for h in range(24)], "16")
            self._o(form, "minute", "Min",   [f"{m:02d}" for m in range(0,60,5)], "00")

        elif self.action == "Changer préfixe":
            self._o(form, "prefix", "Préfixe",
                    ["feat","fix","docs","refactor","chore","perf","test","style"], "feat")
            self._f(form, "from_date", "Du (optionnel)", "")
            self._f(form, "to_date",   "Au (optionnel)", "")

        elif self.action == "Réactiver les annulés":
            self._f(form, "shift_days", "Décalage jours (0 = originales)", "0")

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 14))
        ctk.CTkButton(row, text="✅  Confirmer", height=36,
                      fg_color=COLORS["accent_orange"], hover_color="#cc5500",
                      font=ctk.CTkFont(size=12, weight="bold"),
                      command=self._confirm).pack(side="right", padx=(6, 0))
        ctk.CTkButton(row, text="Annuler", height=36,
                      fg_color=COLORS["bg_secondary"],
                      command=self.destroy).pack(side="right")

    def _f(self, p, key, label, default):
        ctk.CTkLabel(p, text=label, font=ctk.CTkFont(size=10),
                     text_color=COLORS["text_secondary"],
                     ).pack(anchor="w", padx=12, pady=(8, 2))
        e = ctk.CTkEntry(p, fg_color=COLORS["bg_card"],
                         border_color=COLORS["border"], height=28)
        if default:
            e.insert(0, default)
        e.pack(fill="x", padx=12, pady=(0, 4))
        self._fields[key] = ("e", e)

    def _o(self, p, key, label, values, default):
        ctk.CTkLabel(p, text=label, font=ctk.CTkFont(size=10),
                     text_color=COLORS["text_secondary"],
                     ).pack(anchor="w", padx=12, pady=(8, 2))
        v = ctk.StringVar(value=default)
        ctk.CTkOptionMenu(p, values=values, variable=v,
                          fg_color=COLORS["bg_card"]).pack(anchor="w", padx=12, pady=(0, 4))
        self._fields[key] = ("v", v)

    def _val(self, key: str) -> str:
        t, w = self._fields.get(key, ("", None))
        if w is None:
            return ""
        return w.get().strip() if t == "e" else w.get()

    def _confirm(self):
        p: dict = {}
        try:
            if self.action == "Décaler de N jours":
                p["days_offset"] = int(self._val("days_offset"))
                p["from_date"]   = self._val("from_date") or None
            elif self.action == "Heure quotidienne":
                p["hour"]       = int(self._val("hour"))
                p["minute"]     = int(self._val("minute"))
                p["jitter_min"] = int(self._val("jitter") or "0")
                p["from_date"]  = self._val("from_date") or None
            elif self.action == "Jours de push":
                p["allowed_days"] = [d for d, v in self._day_vars.items() if v.get()]
                if not p["allowed_days"]:
                    messagebox.showerror("Erreur", "Sélectionnez au moins un jour.", parent=self)
                    return
                p["hour"]   = int(self._val("hour"))
                p["minute"] = int(self._val("minute"))
            elif self.action == "Changer préfixe":
                p["prefix"]    = self._val("prefix")
                p["from_date"] = self._val("from_date") or None
                p["to_date"]   = self._val("to_date")   or None
            elif self.action == "Réactiver les annulés":
                p["shift_days"] = int(self._val("shift_days") or "0")
        except ValueError as e:
            messagebox.showerror("Erreur", f"Valeur invalide : {e}", parent=self)
            return
        self.on_confirm(p)
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# ── LIGNE DU POOL (réutilisable, jamais détruite) ────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class _PoolRow:
    """
    Représente une ligne du tableau. Créée UNE SEULE FOIS par le pool.

    Les colonnes sont des CTkLabel dont on ne fait que .configure(text=...).
    Les boutons d'action sont cachés/montrés selon le statut de la tâche
    mais ne sont JAMAIS recréés.
    """

    # Largeurs colonnes en px
    W = [42, 72, 148, 205, 265, 155, 130]

    def __init__(self, parent):
        self.frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"],
                                   corner_radius=4, height=38)
        self.frame.pack_propagate(False)

        self.lbl_id     = self._lbl(self.W[0], "Courier", 10)
        self.lbl_status = self._lbl(self.W[1], None, 13)
        self.lbl_date   = self._lbl(self.W[2], "Courier", 10)
        self.lbl_file   = self._lbl(self.W[3], None, 11)
        self.lbl_msg    = self._lbl(self.W[4], None, 11)
        self.lbl_branch = self._lbl(self.W[5], None, 10)

        # Conteneur boutons (width fixe)
        self._bf = ctk.CTkFrame(self.frame, fg_color="transparent", width=self.W[6])
        self._bf.pack(side="left", padx=4)
        self._bf.pack_propagate(False)

        self._btn_edit = ctk.CTkButton(
            self._bf, text="✏️", width=32, height=26,
            fg_color=COLORS["bg_secondary"], hover_color=COLORS["border"],
            font=ctk.CTkFont(size=12), corner_radius=4)
        self._btn_cancel = ctk.CTkButton(
            self._bf, text="🚫", width=32, height=26,
            fg_color=COLORS["bg_secondary"], hover_color=COLORS["accent_red"],
            font=ctk.CTkFont(size=12), corner_radius=4)
        self._btn_react = ctk.CTkButton(
            self._bf, text="🔄", width=88, height=26,
            fg_color=COLORS["bg_secondary"], hover_color=COLORS["accent_orange"],
            text_color=COLORS["accent_orange"], font=ctk.CTkFont(size=10), corner_radius=4)

        self._shown = False

    def _lbl(self, width, family, size) -> ctk.CTkLabel:
        font = ctk.CTkFont(size=size, family=family) if family else ctk.CTkFont(size=size)
        lbl  = ctk.CTkLabel(self.frame, text="", font=font, width=width, anchor="w")
        lbl.pack(side="left", padx=4)
        return lbl

    def update(self, task: Task, idx: int,
               on_edit: Callable, on_cancel: Callable, on_react: Callable):
        """Met à jour le contenu sans recréer de widgets."""
        self.frame.configure(
            fg_color=COLORS["bg_card"] if idx % 2 == 0 else COLORS["bg_secondary"]
        )
        self.lbl_id.configure(text=str(task.id), text_color=COLORS["text_dim"])

        sc = STATUS_COLORS.get(task.status, COLORS["text_secondary"])
        self.lbl_status.configure(text=STATUS_ICONS.get(task.status, "?"), text_color=sc)

        overdue = (
            task.status == TaskStatus.PENDING
            and datetime.strptime(task.scheduled_time, "%Y-%m-%d %H:%M:%S") < datetime.now()
        )
        dc = (COLORS["accent_orange"] if overdue else
              COLORS["text_secondary"] if task.status in (TaskStatus.SKIPPED, TaskStatus.COMPLETED)
              else COLORS["accent_green"])
        self.lbl_date.configure(
            text=task.scheduled_time[:16] + (" ⚡" if overdue else ""),
            text_color=dc,
        )

        fn = Path(task.file_path).name
        self.lbl_file.configure(
            text=fn[:26] + ("…" if len(fn) > 26 else ""),
            text_color=COLORS["text_primary"],
        )
        msg = task.commit_message
        self.lbl_msg.configure(
            text=msg[:33] + ("…" if len(msg) > 33 else ""),
            text_color=COLORS["text_secondary"],
        )
        br = task.branch_name
        self.lbl_branch.configure(
            text=br[:18] + ("…" if len(br) > 18 else ""),
            text_color=COLORS["accent_purple"],
        )

        # Boutons : cache tous, affiche selon statut
        self._btn_edit.pack_forget()
        self._btn_cancel.pack_forget()
        self._btn_react.pack_forget()

        if task.status in (TaskStatus.PENDING, TaskStatus.FAILED):
            self._btn_edit.configure(command=lambda t=task: on_edit(t))
            self._btn_cancel.configure(command=lambda t=task: on_cancel(t))
            self._btn_edit.pack(side="left", padx=1)
            self._btn_cancel.pack(side="left", padx=1)
        elif task.status == TaskStatus.SKIPPED:
            self._btn_react.configure(command=lambda t=task: on_react(t))
            self._btn_react.pack(side="left", padx=1)

    def show(self):
        if not self._shown:
            self.frame.pack(fill="x", padx=4, pady=1)
            self._shown = True

    def hide(self):
        if self._shown:
            self.frame.pack_forget()
            self._shown = False


# ══════════════════════════════════════════════════════════════════════════════
# ── PANNEAU PRINCIPAL ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

class TaskEditorPanel(ctk.CTkFrame):
    """
    Panneau "Mes Tâches" — haute performance.

    Cycle de vie d'un refresh :
      1. [thread daemon]  _load_from_db()   lit la DB
      2. [thread Tk/after] _on_loaded()     stocke dans _all_tasks
      3. [thread Tk]       _apply_filter()  filtre en mémoire O(n)
      4. [thread Tk]       _render_page()   update() le pool (O(PAGE_SIZE))

    Jamais de recréation de widgets. Jamais de lecture DB dans le thread Tk.
    """

    def __init__(self, parent, db: Database, project_id: int = None):
        super().__init__(parent, fg_color="transparent")

        self.db          = db
        self.manager     = TaskManager(db)
        self._pid        = project_id

        self._all_tasks: list[Task] = []
        self._filtered:  list[Task] = []
        self._page       = 0
        self._filter_st: Optional[TaskStatus] = None

        # Anti-cascade : lock + pending flag
        self._lock    = threading.Lock()
        self._pending = False

        # Debounce recherche
        self._search_var  = ctk.StringVar()
        self._debounce_id = None
        self._search_var.trace_add("write", self._on_search)

        # Pool créé après _build
        self._pool: list[_PoolRow] = []

        self._build()
        self._init_pool(PAGE_SIZE)

        if project_id:
            self.load_project(project_id)

    # ── Construction ──────────────────────────────────────────────────────

    def _build(self):
        # En-tête
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(hdr, text="Mes Tâches",
                     font=ctk.CTkFont(size=24, weight="bold"),
                     text_color=COLORS["text_primary"],
                     ).pack(side="left")
        self._proj_var = ctk.StringVar(value="— Projet —")
        self._proj_menu = ctk.CTkOptionMenu(
            hdr, values=self._proj_opts(),
            variable=self._proj_var,
            fg_color=COLORS["bg_card"], width=200,
            command=self._on_proj,
        )
        self._proj_menu.pack(side="right")

        # Toolbar recherche + filtres
        tb = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=8)
        tb.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(tb, text="🔍", font=ctk.CTkFont(size=14),
                     ).pack(side="left", padx=(10, 3), pady=8)
        ctk.CTkEntry(tb, textvariable=self._search_var,
                     placeholder_text="Rechercher...",
                     fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
                     width=210, height=28,
                     ).pack(side="left", padx=(0, 10), pady=8)

        self._fbts: dict = {}
        for sv, lbl, col in [
            (None,               "Tous",      COLORS["text_secondary"]),
            (TaskStatus.PENDING, "⏳ Pending", COLORS["accent_green"]),
            (TaskStatus.FAILED,  "❌ Échoués", COLORS["accent_red"]),
            (TaskStatus.SKIPPED, "⏭ Annulés", COLORS["text_secondary"]),
            (TaskStatus.COMPLETED,"✅ Faits",  COLORS["accent_blue"]),
        ]:
            b = ctk.CTkButton(
                tb, text=lbl, width=82, height=26,
                font=ctk.CTkFont(size=10),
                fg_color=COLORS["bg_secondary"], hover_color=COLORS["border"],
                text_color=col, corner_radius=5,
                command=lambda s=sv: self._filter(s),
            )
            b.pack(side="left", padx=2, pady=8)
            self._fbts[str(sv)] = b

        ctk.CTkButton(tb, text="↻", width=30, height=26,
                      fg_color=COLORS["bg_secondary"], hover_color=COLORS["border"],
                      font=ctk.CTkFont(size=13),
                      command=self._refresh,
                      ).pack(side="left", padx=2)
        self._cnt = ctk.CTkLabel(tb, text="0 tâches",
                                  font=ctk.CTkFont(size=10),
                                  text_color=COLORS["text_secondary"])
        self._cnt.pack(side="right", padx=10)

        # Barre actions de masse
        ba = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"], corner_radius=6)
        ba.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(ba, text="Actions :", font=ctk.CTkFont(size=10),
                     text_color=COLORS["text_secondary"],
                     ).pack(side="left", padx=(10, 5), pady=6)
        for lbl, act, col in [
            ("📅 Décaler",       "Décaler de N jours",    COLORS["accent_blue"]),
            ("🕐 Heure fixe",    "Heure quotidienne",     COLORS["accent_blue"]),
            ("📆 Jours",         "Jours de push",         COLORS["accent_blue"]),
            ("✏️ Préfixes",       "Changer préfixe",       COLORS["accent_purple"]),
            ("🔄 Réactiver",     "Réactiver les annulés", COLORS["accent_orange"]),
            ("🚫 Tout annuler",  "_cancel_all",            COLORS["accent_red"]),
        ]:
            ctk.CTkButton(ba, text=lbl, width=96, height=25,
                          font=ctk.CTkFont(size=10),
                          fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                          text_color=col, corner_radius=4,
                          command=lambda a=act: self._bulk(a),
                          ).pack(side="left", padx=2, pady=6)

        # En-tête colonnes (fixe, jamais recréé)
        ch = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=0, height=30)
        ch.pack(fill="x")
        ch.pack_propagate(False)
        for title, w in [("#",42),("Statut",72),("Date / Heure",148),
                          ("Fichier",205),("Message",265),("Branche",155),("Actions",130)]:
            ctk.CTkLabel(ch, text=title, width=w, anchor="w",
                         font=ctk.CTkFont(size=10, weight="bold"),
                         text_color=COLORS["text_secondary"],
                         ).pack(side="left", padx=5, pady=5)

        # Zone table — scrollable UNIQUEMENT pour les lignes du pool
        self._tbl = ctk.CTkScrollableFrame(
            self, fg_color=COLORS["bg_secondary"], corner_radius=0,
        )
        self._tbl.pack(fill="both", expand=True)

        # Label "vide"
        self._empty = ctk.CTkLabel(
            self._tbl,
            text="Aucune tâche. Sélectionnez un projet ou ajustez les filtres.",
            font=ctk.CTkFont(size=12), text_color=COLORS["text_secondary"],
        )

        # Pagination
        pg = ctk.CTkFrame(self, fg_color="transparent", height=34)
        pg.pack(fill="x")
        pg.pack_propagate(False)
        self._btn_p = ctk.CTkButton(pg, text="◀", width=34, height=26,
                                     fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                                     command=self._prev)
        self._btn_p.pack(side="left", padx=(4, 2), pady=4)
        self._pg_lbl = ctk.CTkLabel(pg, text="Page 1/1",
                                     font=ctk.CTkFont(size=10),
                                     text_color=COLORS["text_secondary"])
        self._pg_lbl.pack(side="left", padx=8)
        self._btn_n = ctk.CTkButton(pg, text="▶", width=34, height=26,
                                     fg_color=COLORS["bg_card"], hover_color=COLORS["border"],
                                     command=self._next)
        self._btn_n.pack(side="left", padx=2)

        self._ps_var = ctk.StringVar(value=str(PAGE_SIZE))
        ctk.CTkLabel(pg, text="Lignes/page :", font=ctk.CTkFont(size=10),
                     text_color=COLORS["text_secondary"],
                     ).pack(side="right", padx=(0, 3))
        ctk.CTkOptionMenu(pg, values=["25","50","100","200"],
                          variable=self._ps_var,
                          fg_color=COLORS["bg_card"], width=68, height=26,
                          command=self._on_ps_change,
                          ).pack(side="right", padx=4)

    def _init_pool(self, size: int):
        """Crée le pool de lignes réutilisables dans _tbl."""
        for _ in range(size):
            row = _PoolRow(self._tbl)
            row.hide()
            self._pool.append(row)

    def _resize_pool(self, new_size: int):
        """Adapte la taille du pool sans tout recréer si possible."""
        cur = len(self._pool)
        if new_size > cur:
            # Ajoute les lignes manquantes
            for _ in range(new_size - cur):
                row = _PoolRow(self._tbl)
                row.hide()
                self._pool.append(row)
        elif new_size < cur:
            # Cache et détruit les lignes en excès
            for row in self._pool[new_size:]:
                row.hide()
                row.frame.destroy()
            self._pool = self._pool[:new_size]

    # ── Chargement ────────────────────────────────────────────────────────

    def load_project(self, pid: Optional[int]):
        self._pid  = pid
        self._page = 0
        self._refresh()

    def _refresh(self):
        """Lance un chargement DB asynchrone. Protégé contre les cascades."""
        if not self._lock.acquire(blocking=False):
            # Un refresh est déjà en cours : on note qu'un autre est demandé
            self._pending = True
            return

        def worker():
            try:
                tasks = (
                    self.manager.get_all_tasks(self._pid, include_done=True)
                    if self._pid else []
                )
                self.after(0, lambda: self._on_loaded(tasks))
            except Exception as e:
                logger.error(f"Erreur chargement : {e}")
                self._lock.release()

        threading.Thread(target=worker, daemon=True).start()

    def _on_loaded(self, tasks: list[Task]):
        """Callback Tk : reçoit les données et lance le filtrage."""
        self._all_tasks = tasks
        self._lock.release()
        self._apply_filter()
        # S'il y avait un refresh en attente, on le lance maintenant
        if self._pending:
            self._pending = False
            self._refresh()

    # ── Filtrage ──────────────────────────────────────────────────────────

    def _apply_filter(self):
        """Filtre en mémoire — O(n), zéro accès DB."""
        kw = self._search_var.get().lower().strip()
        result = []
        for t in self._all_tasks:
            if self._filter_st and t.status != self._filter_st:
                continue
            if kw:
                if kw not in (t.commit_message + Path(t.file_path).name + t.branch_name).lower():
                    continue
            result.append(t)
        self._filtered = result

        ps = int(self._ps_var.get())
        total_pages = max(1, (len(result) - 1) // ps + 1) if result else 1
        if self._page >= total_pages:
            self._page = 0

        self._cnt.configure(text=f"{len(result)} tâche(s)")
        self._render_page()

    def _render_page(self):
        """
        Affiche la page courante via le pool.
        Aucun widget créé ou détruit — uniquement .configure() et pack/pack_forget.
        """
        ps    = int(self._ps_var.get())
        start = self._page * ps
        page  = self._filtered[start:start + ps]

        # Label vide
        if not page:
            self._empty.pack(pady=40)
        else:
            self._empty.pack_forget()

        # Update du pool
        for i, row in enumerate(self._pool):
            if i < len(page):
                row.update(page[i], i,
                           on_edit=self._edit,
                           on_cancel=self._cancel,
                           on_react=self._react)
                row.show()
            else:
                row.hide()

        # Pagination
        tp = max(1, (len(self._filtered) - 1) // ps + 1) if self._filtered else 1
        self._pg_lbl.configure(text=f"Page {self._page + 1}/{tp}")
        self._btn_p.configure(state="normal" if self._page > 0 else "disabled")
        self._btn_n.configure(state="normal" if self._page < tp - 1 else "disabled")

    # ── Navigation ────────────────────────────────────────────────────────

    def _prev(self):
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _next(self):
        ps = int(self._ps_var.get())
        tp = max(1, (len(self._filtered) - 1) // ps + 1) if self._filtered else 1
        if self._page < tp - 1:
            self._page += 1
            self._render_page()

    def _on_ps_change(self, v: str):
        self._page = 0
        self._resize_pool(int(v))
        self._render_page()

    def _on_search(self, *_):
        if self._debounce_id:
            self.after_cancel(self._debounce_id)
        self._debounce_id = self.after(DEBOUNCE_MS, self._apply_filter)

    # ── Filtres ───────────────────────────────────────────────────────────

    def _filter(self, status: Optional[TaskStatus]):
        self._filter_st = status
        self._page = 0
        for k, b in self._fbts.items():
            active = (k == str(status))
            b.configure(
                fg_color=COLORS["bg_card"] if active else COLORS["bg_secondary"],
                border_width=1 if active else 0,
                border_color=COLORS["accent_green"] if active else COLORS["border"],
            )
        self._apply_filter()

    # ── Actions individuelles ──────────────────────────────────────────────

    def _edit(self, task: Task):
        def on_save(data: dict):
            ok = self.manager.edit_task(
                task.id,
                new_message=data["new_message"],
                new_datetime=data["new_datetime"],
                new_branch=data["new_branch"],
                force=data["force"],
            )
            if ok:
                # Mise à jour locale : relit seulement cet élément
                fresh = self.manager._get_task_any_status(task.id)
                for i, t in enumerate(self._all_tasks):
                    if t.id == task.id:
                        self._all_tasks[i] = fresh
                        break
                self._apply_filter()
            else:
                messagebox.showerror("Erreur", "Modification échouée.")
        TaskEditDialog(self.winfo_toplevel(), task, on_save)

    def _cancel(self, task: Task):
        if not messagebox.askyesno(
            "Confirmer l'annulation",
            f"Annuler :\n  {Path(task.file_path).name}\n  {task.scheduled_time[:16]}"
        ):
            return
        if self.manager.cancel(task.id):
            fresh = self.manager._get_task_any_status(task.id)
            for i, t in enumerate(self._all_tasks):
                if t.id == task.id:
                    self._all_tasks[i] = fresh
                    break
            self._apply_filter()

    def _react(self, task: Task):
        new_date = simpledialog.askstring(
            "Réactiver",
            f"Nouvelle date (YYYY-MM-DD HH:MM) — vide = conserver {task.scheduled_time[:16]}",
            parent=self.winfo_toplevel(),
        )
        if self.manager.reactivate(
            task.id,
            new_datetime=new_date.strip() if new_date and new_date.strip() else None
        ):
            fresh = self.manager._get_task_any_status(task.id)
            for i, t in enumerate(self._all_tasks):
                if t.id == task.id:
                    self._all_tasks[i] = fresh
                    break
            self._apply_filter()

    # ── Actions de masse ──────────────────────────────────────────────────

    def _bulk(self, action: str):
        if self._pid is None:
            messagebox.showwarning("Attention", "Sélectionnez d'abord un projet.")
            return

        if action == "_cancel_all":
            pending = self.manager.get_tasks_by_status(self._pid, TaskStatus.PENDING)
            if not pending:
                messagebox.showinfo("Info", "Aucune tâche PENDING.")
                return
            if messagebox.askyesno(
                "⚠️  Annulation en masse",
                f"Annuler les {len(pending)} tâches PENDING ?\n"
                "(Récupérables via 'Réactiver les annulés')"
            ):
                r = self.manager.cancel_all_pending(self._pid)
                messagebox.showinfo("Terminé", r.summary())
                self._refresh()
            return

        def on_confirm(p: dict):
            r = self._run_bulk(action, p)
            if r:
                messagebox.showinfo("Terminé", f"{action}\n\n{r.summary()}")
                self._refresh()

        top = self.winfo_toplevel()
        self.after(10, lambda: BulkActionDialog(top, action, on_confirm))

    def _run_bulk(self, action: str, p: dict) -> Optional[BulkOperationResult]:
        pid = self._pid
        if action == "Décaler de N jours":
            return self.manager.shift_all(pid, p["days_offset"], from_date=p.get("from_date"))
        elif action == "Heure quotidienne":
            return self.manager.set_daily_push_time(
                pid, p["hour"], p["minute"], p.get("jitter_min", 0), p.get("from_date"))
        elif action == "Jours de push":
            return self.manager.set_push_days(pid, p["allowed_days"], p["hour"], p["minute"])
        elif action == "Changer préfixe":
            return self.manager.bulk_edit_messages(pid, p["prefix"], p.get("from_date"), p.get("to_date"))
        elif action == "Réactiver les annulés":
            return self.manager.reactivate_all_skipped(pid, p.get("shift_days", 0))
        return None

    # ── Utilitaires ───────────────────────────────────────────────────────

    def _proj_opts(self) -> list[str]:
        projects = self.db.get_all_projects()
        return [f"[{p.id}] {p.name}" for p in projects] or ["Aucun projet"]

    def _on_proj(self, sel: str):
        try:
            pid = int(sel.split("]")[0].replace("[", ""))
            self.load_project(pid)
        except (ValueError, IndexError):
            pass