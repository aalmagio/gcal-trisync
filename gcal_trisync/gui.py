"""
Tkinter GUI for gcal_trisync.

The GUI keeps the sync engine in the existing CLI process and focuses on
making configuration and day-to-day execution approachable on Windows.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import (
    END,
    BooleanVar,
    IntVar,
    Listbox,
    StringVar,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    ttk,
)
from tkinter.scrolledtext import ScrolledText
from typing import Any

import yaml

from .config import ConfigValidationError, load_config, validate_config
from .models import VALID_VISIBILITIES

TASK_NAME = "gcal-trisync Sync"


def get_user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "gcal-trisync"
    return Path.home() / ".gcal-trisync"


def default_config(data_dir: Path) -> dict[str, Any]:
    return {
        "window_days_past": 30,
        "window_days_future": 365,
        "prefix_origin_in_title": True,
        "sync_tag_in_description": "Sincronizzato da gcal_trisync",
        "ignore_if_summary_contains": ["compleanno"],
        "ignore_event_types": [],
        "skip_if_title_has_known_prefix": True,
        "sync_delete": True,
        "default_copy_visibility": "private",
        "calendars": [
            {
                "name": "WORK",
                "calendar_id": "primary",
                "credentials_file": str(data_dir / "creds" / "work_oauth_client.json"),
                "token_file": str(data_dir / "tokens" / "work.token.json"),
            },
            {
                "name": "PERS",
                "calendar_id": "primary",
                "credentials_file": str(data_dir / "creds" / "personal_oauth_client.json"),
                "token_file": str(data_dir / "tokens" / "personal.token.json"),
            },
        ],
    }


class CalendarDialog:
    """Small modal dialog used to add or edit one calendar row."""

    def __init__(self, parent: Tk, calendar: dict[str, Any] | None = None) -> None:
        self.result: dict[str, Any] | None = None
        self.top = Toplevel(parent)
        self.top.title("Calendario")
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.top.grab_set()

        values = calendar or {}
        self.name = StringVar(value=values.get("name", ""))
        self.calendar_id = StringVar(value=values.get("calendar_id", "primary"))
        self.credentials_file = StringVar(value=values.get("credentials_file", ""))
        self.token_file = StringVar(value=values.get("token_file", ""))
        self.copy_visibility = StringVar(value=values.get("copy_visibility", ""))

        body = ttk.Frame(self.top, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        self._entry(body, 0, "Nome", self.name)
        self._entry(body, 1, "Calendar ID", self.calendar_id)
        self._path_entry(body, 2, "Credenziali JSON", self.credentials_file, save=False)
        self._path_entry(body, 3, "Token locale", self.token_file, save=True)

        ttk.Label(body, text="Visibilita copie").grid(row=4, column=0, sticky="w", pady=4)
        visibility = ttk.Combobox(
            body,
            textvariable=self.copy_visibility,
            values=[""] + sorted(VALID_VISIBILITIES),
            state="readonly",
            width=28,
        )
        visibility.grid(row=4, column=1, sticky="ew", pady=4)

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Annulla", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Salva", command=self._save).grid(row=0, column=1, padx=4)

        self.top.bind("<Escape>", lambda _event: self._cancel())
        self.top.bind("<Return>", lambda _event: self._save())
        self.top.wait_window()

    def _entry(self, parent: ttk.Frame, row: int, label: str, variable: StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, width=48).grid(row=row, column=1, sticky="ew", pady=4)

    def _path_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: StringVar,
        *,
        save: bool,
    ) -> None:
        self._entry(parent, row, label, variable)

        def choose_path() -> None:
            self._choose_path(variable, save=save)

        ttk.Button(parent, text="Sfoglia", command=choose_path).grid(row=row, column=2, padx=(8, 0))

    def _choose_path(self, variable: StringVar, *, save: bool) -> None:
        if save:
            path = filedialog.asksaveasfilename(
                parent=self.top,
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("Tutti i file", "*.*")],
            )
        else:
            path = filedialog.askopenfilename(
                parent=self.top,
                filetypes=[("JSON", "*.json"), ("Tutti i file", "*.*")],
            )
        if path:
            variable.set(path)

    def _save(self) -> None:
        name = self.name.get().strip().upper()
        calendar_id = self.calendar_id.get().strip()
        credentials_file = self.credentials_file.get().strip()
        token_file = self.token_file.get().strip()
        if not all([name, calendar_id, credentials_file, token_file]):
            messagebox.showerror("Dati mancanti", "Compila nome, Calendar ID, credenziali e token.")
            return

        result: dict[str, Any] = {
            "name": name,
            "calendar_id": calendar_id,
            "credentials_file": credentials_file,
            "token_file": token_file,
        }
        visibility = self.copy_visibility.get().strip()
        if visibility:
            result["copy_visibility"] = visibility
        self.result = result
        self.top.destroy()

    def _cancel(self) -> None:
        self.top.destroy()


class TrisyncGui:
    """Main application window."""

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("gcal-trisync")
        self.root.minsize(940, 680)

        self.data_dir = get_user_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "creds").mkdir(exist_ok=True)
        (self.data_dir / "tokens").mkdir(exist_ok=True)
        self.default_cfg = default_config(self.data_dir)

        self.config_path = StringVar(value=str(self.data_dir / "config.yaml"))
        self.state_file = StringVar(value=str(self.data_dir / ".trisync_state.json"))
        self.auth_method = StringVar(value="local")
        self.login_hint = StringVar(value="")
        self.port = IntVar(value=0)

        self.window_days_past = IntVar(value=self.default_cfg["window_days_past"])
        self.window_days_future = IntVar(value=self.default_cfg["window_days_future"])
        self.prefix_origin = BooleanVar(value=self.default_cfg["prefix_origin_in_title"])
        self.sync_delete = BooleanVar(value=self.default_cfg["sync_delete"])
        self.skip_prefixed = BooleanVar(value=self.default_cfg["skip_if_title_has_known_prefix"])
        self.default_visibility = StringVar(value=self.default_cfg["default_copy_visibility"])
        self.sync_tag = StringVar(value=self.default_cfg["sync_tag_in_description"])
        self.ignore_words = list(self.default_cfg["ignore_if_summary_contains"])
        self.ignore_word_input = StringVar(value="")
        self.ignore_event_types = StringVar(value="")

        self.incremental = BooleanVar(value=True)
        self.force_full = BooleanVar(value=False)
        self.dry_run = BooleanVar(value=False)
        self.metrics = BooleanVar(value=True)
        self.verbose = BooleanVar(value=False)
        self.schedule_every_minutes = IntVar(value=15)
        self.schedule_force_full = BooleanVar(value=False)
        self.schedule_status = StringVar(value="Non verificato")

        self.calendars: list[dict[str, Any]] = [dict(cal) for cal in self.default_cfg["calendars"]]
        self.process: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str | None] = queue.Queue()

        self._build_ui()
        self._refresh_calendar_table()
        self._load_if_exists()
        self._poll_output()

    def _build_ui(self) -> None:
        root = ttk.Frame(self.root, padding=14)
        root.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Configurazione").grid(row=0, column=0, sticky="w")
        ttk.Entry(header, textvariable=self.config_path).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(header, text="Apri", command=self.open_config).grid(row=0, column=2, padx=2)
        ttk.Button(header, text="Salva", command=self.save_config).grid(row=0, column=3, padx=2)

        tabs = ttk.Notebook(root)
        tabs.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

        calendars_tab = ttk.Frame(tabs, padding=12)
        options_tab = ttk.Frame(tabs, padding=12)
        ignore_tab = ttk.Frame(tabs, padding=12)
        run_tab = ttk.Frame(tabs, padding=12)
        schedule_tab = ttk.Frame(tabs, padding=12)

        tabs.add(calendars_tab, text="Calendari")
        tabs.add(options_tab, text="Opzioni")
        tabs.add(ignore_tab, text="Parole ignorate")
        tabs.add(run_tab, text="Esecuzione")
        tabs.add(schedule_tab, text="Scheduler")

        self._build_calendars(calendars_tab)
        self._build_options(options_tab)
        self._build_ignore_words(ignore_tab)
        self._build_actions(run_tab)
        self._build_scheduler(schedule_tab)

    def _build_options(self, parent: ttk.Frame) -> None:
        options = ttk.Frame(parent)
        options.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        for column in range(6):
            options.columnconfigure(column, weight=1)

        ttk.Label(options, text="Giorni passati").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(options, from_=0, to=3650, textvariable=self.window_days_past, width=8).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Label(options, text="Giorni futuri").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(options, from_=0, to=3650, textvariable=self.window_days_future, width=8).grid(
            row=0, column=3, sticky="w"
        )
        ttk.Label(options, text="Visibilita").grid(row=0, column=4, sticky="w")
        ttk.Combobox(
            options,
            textvariable=self.default_visibility,
            values=sorted(VALID_VISIBILITIES),
            state="readonly",
            width=14,
        ).grid(row=0, column=5, sticky="w")

        ttk.Checkbutton(options, text="Prefisso origine nel titolo", variable=self.prefix_origin).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(options, text="Cancellazione sicura", variable=self.sync_delete).grid(
            row=1, column=2, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(options, text="Salta titoli gia prefissati", variable=self.skip_prefixed).grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(10, 0)
        )

        ttk.Label(options, text="Tag descrizione").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(options, textvariable=self.sync_tag).grid(
            row=2, column=1, columnspan=5, sticky="ew", pady=(10, 0)
        )
        ttk.Label(options, text="Ignora tipi evento").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(options, textvariable=self.ignore_event_types).grid(
            row=3, column=1, columnspan=5, sticky="ew", pady=(8, 0)
        )

    def _build_calendars(self, parent: ttk.Frame) -> None:
        section = ttk.Frame(parent)
        section.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        section.columnconfigure(0, weight=1)
        section.rowconfigure(0, weight=1)

        columns = ("name", "calendar_id", "credentials_file", "token_file", "copy_visibility")
        self.calendar_table = ttk.Treeview(section, columns=columns, show="headings", height=5)
        headings = {
            "name": "Nome",
            "calendar_id": "Calendar ID",
            "credentials_file": "Credenziali",
            "token_file": "Token",
            "copy_visibility": "Visibilita",
        }
        widths = {
            "name": 90,
            "calendar_id": 160,
            "credentials_file": 260,
            "token_file": 220,
            "copy_visibility": 90,
        }
        for column in columns:
            self.calendar_table.heading(column, text=headings[column])
            self.calendar_table.column(column, width=widths[column], stretch=True)
        self.calendar_table.grid(row=0, column=0, sticky="nsew")

        buttons = ttk.Frame(section)
        buttons.grid(row=0, column=1, sticky="ns", padx=(10, 0))
        ttk.Button(buttons, text="Aggiungi", command=self.add_calendar).grid(row=0, column=0, sticky="ew", pady=2)
        ttk.Button(buttons, text="Modifica", command=self.edit_calendar).grid(row=1, column=0, sticky="ew", pady=2)
        ttk.Button(buttons, text="Rimuovi", command=self.remove_calendar).grid(row=2, column=0, sticky="ew", pady=2)

    def _build_ignore_words(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)

        entry_row = ttk.Frame(parent)
        entry_row.grid(row=0, column=0, sticky="ew")
        entry_row.columnconfigure(0, weight=1)
        ttk.Entry(entry_row, textvariable=self.ignore_word_input).grid(row=0, column=0, sticky="ew")
        ttk.Button(entry_row, text="Aggiungi", command=self.add_ignore_word).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(entry_row, text="Rimuovi selezionata", command=self.remove_ignore_word).grid(row=0, column=2, padx=(8, 0))

        self.ignore_words_list = Listbox(parent, height=14)
        self.ignore_words_list.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self._refresh_ignore_words()

    def _build_actions(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent)
        actions.grid(row=0, column=0, sticky="nsew")
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        actions.columnconfigure(1, weight=1)
        actions.rowconfigure(4, weight=1)

        ttk.Checkbutton(actions, text="Incrementale", variable=self.incremental).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(actions, text="Forza sync completo", variable=self.force_full).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(actions, text="Dry run", variable=self.dry_run).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(actions, text="Metriche", variable=self.metrics).grid(row=0, column=3, sticky="w")
        ttk.Checkbutton(actions, text="Log dettagliato", variable=self.verbose).grid(row=0, column=4, sticky="w")

        ttk.Label(actions, text="Auth").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(actions, textvariable=self.auth_method, values=["local", "console"], state="readonly", width=10).grid(
            row=1, column=1, sticky="w", pady=(8, 0)
        )
        ttk.Label(actions, text="Email login").grid(row=1, column=2, sticky="e", pady=(8, 0))
        ttk.Entry(actions, textvariable=self.login_hint).grid(row=1, column=3, sticky="ew", pady=(8, 0))
        ttk.Label(actions, text="Porta").grid(row=1, column=4, sticky="e", pady=(8, 0))
        ttk.Spinbox(actions, from_=0, to=65535, textvariable=self.port, width=8).grid(
            row=1, column=5, sticky="w", pady=(8, 0)
        )

        ttk.Label(actions, text="File stato").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(actions, textvariable=self.state_file).grid(row=2, column=1, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Sfoglia", command=self.choose_state_file).grid(row=2, column=5, sticky="ew", pady=(8, 0))

        buttons = ttk.Frame(actions)
        buttons.grid(row=3, column=0, columnspan=6, sticky="w", pady=(12, 0))
        self.sync_button = ttk.Button(buttons, text="Avvia sync", command=self.run_sync)
        self.sync_button.grid(row=0, column=0, padx=2)
        ttk.Button(buttons, text="Controlla auth", command=self.check_auth).grid(row=0, column=1, padx=2)
        ttk.Button(buttons, text="Re-auth", command=self.reauth).grid(row=0, column=2, padx=2)
        ttk.Button(buttons, text="Reset stato", command=self.clear_state).grid(row=0, column=3, padx=2)
        self.stop_button = ttk.Button(buttons, text="Interrompi", command=self.stop_process, state="disabled")
        self.stop_button.grid(row=0, column=4, padx=2)

        self.log = ScrolledText(actions, height=16, wrap="word", state="disabled")
        self.log.grid(row=4, column=0, columnspan=6, sticky="nsew", pady=(12, 0))

    def _build_scheduler(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(parent, text="Ogni minuti").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(parent, from_=5, to=1440, textvariable=self.schedule_every_minutes, width=8).grid(
            row=0, column=1, sticky="w"
        )
        ttk.Checkbutton(parent, text="Forza sync completo", variable=self.schedule_force_full).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        buttons = ttk.Frame(parent)
        buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(14, 0))
        ttk.Button(buttons, text="Installa/aggiorna", command=self.install_scheduler).grid(row=0, column=0, padx=2)
        ttk.Button(buttons, text="Rimuovi", command=self.remove_scheduler).grid(row=0, column=1, padx=2)
        ttk.Button(buttons, text="Verifica", command=self.check_scheduler).grid(row=0, column=2, padx=2)

        ttk.Label(parent, text="Stato").grid(row=3, column=0, sticky="w", pady=(14, 0))
        ttk.Label(parent, textvariable=self.schedule_status).grid(row=3, column=1, sticky="w", pady=(14, 0))

    def _load_if_exists(self) -> None:
        if Path(self.config_path.get()).exists():
            self.load_config_from_path(self.config_path.get(), show_errors=False)

    def open_config(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            initialdir=str(Path.cwd()),
            filetypes=[("YAML/JSON", "*.yaml *.yml *.json"), ("Tutti i file", "*.*")],
        )
        if path:
            self.config_path.set(path)
            self.load_config_from_path(path)

    def load_config_from_path(self, path: str, *, show_errors: bool = True) -> None:
        try:
            cfg = load_config(path)
        except Exception as exc:
            if show_errors:
                messagebox.showerror("Configurazione non valida", str(exc))
            return
        self._apply_config(cfg)
        self._append_log(f"Configurazione caricata: {path}\n")

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        self.window_days_past.set(int(cfg.get("window_days_past", 30)))
        self.window_days_future.set(int(cfg.get("window_days_future", 365)))
        self.prefix_origin.set(bool(cfg.get("prefix_origin_in_title", True)))
        self.sync_delete.set(bool(cfg.get("sync_delete", True)))
        self.skip_prefixed.set(bool(cfg.get("skip_if_title_has_known_prefix", True)))
        self.default_visibility.set(cfg.get("default_copy_visibility", "private"))
        self.sync_tag.set(cfg.get("sync_tag_in_description", ""))
        self.ignore_words = list(cfg.get("ignore_if_summary_contains", []))
        self.ignore_event_types.set(", ".join(cfg.get("ignore_event_types", [])))
        self.calendars = [dict(cal) for cal in cfg.get("calendars", [])]
        self._refresh_calendar_table()
        self._refresh_ignore_words()

    def save_config(self) -> bool:
        cfg = self._collect_config()
        try:
            validate_config(cfg, check_files=False)
        except ConfigValidationError as exc:
            messagebox.showerror("Configurazione incompleta", str(exc))
            return False

        path = Path(self.config_path.get()).expanduser()
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=False)
        self._append_log(f"Configurazione salvata: {path}\n")
        return True

    def _collect_config(self) -> dict[str, Any]:
        return {
            "window_days_past": self.window_days_past.get(),
            "window_days_future": self.window_days_future.get(),
            "prefix_origin_in_title": self.prefix_origin.get(),
            "sync_tag_in_description": self.sync_tag.get(),
            "ignore_if_summary_contains": list(self.ignore_words),
            "ignore_event_types": self._split_csv(self.ignore_event_types.get()),
            "skip_if_title_has_known_prefix": self.skip_prefixed.get(),
            "sync_delete": self.sync_delete.get(),
            "default_copy_visibility": self.default_visibility.get(),
            "calendars": [dict(cal) for cal in self.calendars],
        }

    def _split_csv(self, value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    def _refresh_ignore_words(self) -> None:
        if not hasattr(self, "ignore_words_list"):
            return
        self.ignore_words_list.delete(0, END)
        for word in self.ignore_words:
            self.ignore_words_list.insert(END, word)

    def add_ignore_word(self) -> None:
        word = self.ignore_word_input.get().strip()
        if not word:
            return
        existing = {item.lower() for item in self.ignore_words}
        if word.lower() not in existing:
            self.ignore_words.append(word)
            self.ignore_words.sort(key=str.lower)
            self._refresh_ignore_words()
        self.ignore_word_input.set("")

    def remove_ignore_word(self) -> None:
        selected = list(self.ignore_words_list.curselection())
        if not selected:
            messagebox.showinfo("Seleziona parola", "Seleziona una parola da rimuovere.")
            return
        for index in reversed(selected):
            del self.ignore_words[index]
        self._refresh_ignore_words()

    def _refresh_calendar_table(self) -> None:
        for row in self.calendar_table.get_children():
            self.calendar_table.delete(row)
        for index, cal in enumerate(self.calendars):
            self.calendar_table.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    cal.get("name", ""),
                    cal.get("calendar_id", ""),
                    cal.get("credentials_file", ""),
                    cal.get("token_file", ""),
                    cal.get("copy_visibility", ""),
                ),
            )

    def add_calendar(self) -> None:
        dialog = CalendarDialog(self.root)
        if dialog.result:
            self.calendars.append(dialog.result)
            self._refresh_calendar_table()

    def edit_calendar(self) -> None:
        selected = self.calendar_table.selection()
        if not selected:
            messagebox.showinfo("Seleziona calendario", "Seleziona una riga da modificare.")
            return
        index = int(selected[0])
        dialog = CalendarDialog(self.root, self.calendars[index])
        if dialog.result:
            self.calendars[index] = dialog.result
            self._refresh_calendar_table()

    def remove_calendar(self) -> None:
        selected = self.calendar_table.selection()
        if not selected:
            messagebox.showinfo("Seleziona calendario", "Seleziona una riga da rimuovere.")
            return
        index = int(selected[0])
        del self.calendars[index]
        self._refresh_calendar_table()

    def choose_state_file(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            defaultextension=".json",
            initialfile=Path(self.state_file.get()).name,
            filetypes=[("JSON", "*.json"), ("Tutti i file", "*.*")],
        )
        if path:
            self.state_file.set(path)

    def run_sync(self) -> None:
        args = []
        if self.incremental.get():
            args.append("--incremental")
        if self.force_full.get():
            args.append("--full-sync")
        if self.dry_run.get():
            args.append("--dry-run")
        if self.metrics.get():
            args.append("--metrics")
        self._run_cli(args)

    def check_auth(self) -> None:
        self._run_cli(["--check-auth"])

    def reauth(self) -> None:
        self._run_cli(["--reauth", "--check-auth"])

    def clear_state(self) -> None:
        if messagebox.askyesno("Reset stato", "Cancellare i token di sync incrementale salvati?"):
            self._run_cli(["--clear-state"])

    def install_scheduler(self) -> None:
        if os.name != "nt":
            messagebox.showerror("Scheduler non disponibile", "Lo scheduler Windows e disponibile solo su Windows.")
            return
        if not self.save_config():
            return
        minutes = max(5, int(self.schedule_every_minutes.get()))
        command = self._scheduled_cli_command()
        task_command = subprocess.list2cmdline(command)
        args = [
            "schtasks",
            "/Create",
            "/F",
            "/TN",
            TASK_NAME,
            "/SC",
            "MINUTE",
            "/MO",
            str(minutes),
            "/TR",
            task_command,
            "/RL",
            "LIMITED",
        ]
        result = self._run_schtasks(args)
        if result.returncode == 0:
            self.schedule_status.set(f"Attivo: ogni {minutes} minuti")
        else:
            self.schedule_status.set("Errore installazione")
        self._append_log(result.stdout)

    def remove_scheduler(self) -> None:
        if os.name != "nt":
            messagebox.showerror("Scheduler non disponibile", "Lo scheduler Windows e disponibile solo su Windows.")
            return
        args = ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
        result = self._run_schtasks(args)
        if result.returncode == 0:
            self.schedule_status.set("Rimosso")
        else:
            self.schedule_status.set("Non trovato o errore")
        self._append_log(result.stdout)

    def check_scheduler(self) -> None:
        if os.name != "nt":
            messagebox.showerror("Scheduler non disponibile", "Lo scheduler Windows e disponibile solo su Windows.")
            return
        result = self._run_schtasks(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
        if result.returncode == 0:
            self.schedule_status.set("Attivo")
        else:
            self.schedule_status.set("Non installato")
        self._append_log(result.stdout)

    def _scheduled_cli_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--cli"]
        else:
            cmd = [sys.executable, "-m", "gcal_trisync"]
        cmd.extend(
            [
                "--config",
                self.config_path.get(),
                "--auth",
                "local",
                "--state-file",
                self.state_file.get(),
                "--incremental",
                "--metrics",
            ]
        )
        if self.schedule_force_full.get():
            cmd.append("--full-sync")
        return cmd

    def _run_schtasks(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(Path(self.config_path.get()).expanduser().parent),
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return subprocess.CompletedProcess(args, result.returncode, stdout=output)
        except Exception as exc:
            return subprocess.CompletedProcess(args, returncode=1, stdout=f"Errore scheduler: {exc}\n")

    def _run_cli(self, extra_args: list[str]) -> None:
        if self.process:
            messagebox.showinfo("Operazione in corso", "Attendi la fine del comando corrente.")
            return
        if not self.save_config():
            return

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--cli"]
        else:
            cmd = [sys.executable, "-m", "gcal_trisync"]

        cmd.extend(
            [
                "--config",
                self.config_path.get(),
                "--auth",
                self.auth_method.get(),
                "--state-file",
                self.state_file.get(),
            ]
        )
        if self.login_hint.get().strip():
            cmd.extend(["--login-hint", self.login_hint.get().strip()])
        if self.port.get():
            cmd.extend(["--port", str(self.port.get())])
        if self.verbose.get():
            cmd.append("--verbose")
        cmd.extend(extra_args)

        self._append_log("\n$ " + " ".join(f'"{part}"' if " " in part else part for part in cmd) + "\n")
        self.sync_button.configure(state="disabled")
        self.stop_button.configure(state="normal")

        thread = threading.Thread(target=self._process_worker, args=(cmd,), daemon=True)
        thread.start()

    def _process_worker(self, cmd: list[str]) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(Path(self.config_path.get()).expanduser().parent),
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                self.output_queue.put(line)
            return_code = self.process.wait()
            self.output_queue.put(f"\nComando concluso con codice {return_code}.\n")
        except Exception as exc:
            self.output_queue.put(f"\nErrore: {exc}\n")
        finally:
            self.process = None
            self.output_queue.put(None)

    def stop_process(self) -> None:
        if self.process and messagebox.askyesno("Interrompi", "Interrompere il comando in corso?"):
            self.process.terminate()

    def _poll_output(self) -> None:
        while True:
            try:
                item = self.output_queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.sync_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
            else:
                self._append_log(item)
        self.root.after(120, self._poll_output)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")


def main() -> int:
    root = Tk()
    try:
        root.call("tk", "scaling", 1.2)
    except Exception:
        pass
    TrisyncGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
