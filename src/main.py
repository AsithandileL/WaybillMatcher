"""WaybillMatcher - scan-search-print-archive kiosk app.

Flow: operator scans a BIN -> barcode scanner types it into the always-
focused textbox -> once the input reaches the configured BIN length, the
app auto-fires the search (no Enter key needed) -> looks the BIN up in the
in-memory index (instant - no re-reading every PDF on every scan) ->
prints match(es) (as separate per-page jobs if the PDF has multiple pages)
-> moves the printed file(s) to /archive -> logs everything.

No mouse click required at any point during normal scanning. A "Fetch New
Waybills" button lets the operator force an immediate re-check of the
search folder for newly-arrived PDFs without waiting for the automatic
background refresh interval - it's fast because the index only ever
re-reads files that are new or changed since the last pass. Scanning is
locked out (input disabled, spinner shown) during the initial startup
index build and during any manual fetch, so a scan can never race against
an incomplete index.
"""

import os
import re
import sys
import time
import threading
import queue
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logger_setup import setup_logger, get_logger
from config_loader import load_config, resolve_config_path, ConfigError
from single_instance import SingleInstanceGuard
from indexer import PDFIndex
import printer
import archiver

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def group_matches_into_waybills(matches, companion_suffix: str = "_InvoiceFile"):
    """Groups index matches into logical waybills.

    A waybill is normally ONE pdf (page 1 shipment info, page 2 commercial
    invoice), but some are split into two files: the main waybill file and
    a companion invoice file whose name is the same plus a suffix, e.g.
    "<name>.pdf" and "<name>_InvoiceFile.pdf". Those two files are one
    waybill, not a duplicate.

    Returns a list of groups. Each group is a list of matches with the main
    file first and any companion file after it. More than one group means
    the BIN genuinely matched different waybills (a real duplicate, such as
    a stray "- Copy.pdf", or the same name in two different folders).
    """
    suffix = (companion_suffix or "").strip()
    suffix_lower = suffix.lower()
    groups = {}  # key -> {"main": [...], "companion": [...]}

    for match in matches:
        directory, filename = os.path.split(match.file_path)
        stem, _ext = os.path.splitext(filename)
        is_companion = bool(suffix) and stem.lower().endswith(suffix_lower)
        base = stem[: -len(suffix)] if is_companion else stem
        key = (os.path.normcase(directory), base.lower())
        bucket = groups.setdefault(key, {"main": [], "companion": []})
        bucket["companion" if is_companion else "main"].append(match)

    return [bucket["main"] + bucket["companion"] for bucket in groups.values()]


def format_multi_match_message(bin_value: str, matches, max_shown: int = 8) -> str:
    """On-screen text for a multi-match hit. Pulled out as a plain function
    so it's unit-testable without a real Tk window."""
    filenames = [os.path.basename(m.file_path) for m in matches]
    shown = filenames[:max_shown]
    remainder = len(filenames) - len(shown)
    file_list = ", ".join(shown)
    if remainder > 0:
        file_list += f", (+{remainder} more - see log)"
    return f"BIN: {bin_value} found in multiple files: {file_list}"


class BinScanApp:
    def __init__(self, root: tk.Tk, cfg: dict, index: PDFIndex):
        self.root = root
        self.cfg = cfg
        self.index = index
        self.logger = get_logger()
        self.ui_queue = queue.Queue()
        self.last_scan_value = None
        self.last_scan_time = 0.0
        self._processing = False
        self._busy = False
        self._busy_message = ""
        self._spinner_index = 0
        self._spinner_after_id = None

        self._build_ui()
        self.root.after(100, self._drain_ui_queue)

    # ---------------------------------------------------------------- UI --

    def _build_ui(self):
        self.root.title("WaybillMatcher")
        self.root.attributes("-topmost", True)
        self.root.geometry("620x320")
        self.root.configure(bg="#1e1e1e")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        title_font = tkfont.Font(size=14, weight="bold")
        status_font = tkfont.Font(size=12)
        entry_font = tkfont.Font(size=20)
        button_font = tkfont.Font(size=11)

        tk.Label(
            self.root, text="Scan a BIN to search and print",
            font=title_font, bg="#1e1e1e", fg="white"
        ).pack(pady=(18, 6))

        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(
            self.root, textvariable=self.entry_var, font=entry_font,
            justify="center", relief="flat", bg="#2d2d2d", fg="white",
            insertbackground="white"
        )
        self.entry.pack(fill="x", padx=40, pady=10, ipady=8)
        self.entry.bind("<Return>", self._on_scan)
        self.entry_var.trace_add("write", self._on_entry_changed)
        self.entry.focus_set()

        self.status_var = tk.StringVar(value="Ready.")
        self.status_label = tk.Label(
            self.root, textvariable=self.status_var, font=status_font,
            bg="#1e1e1e", fg="#8fd694", wraplength=560, justify="center"
        )
        self.status_label.pack(pady=(10, 4))

        self.fetch_button = tk.Button(
            self.root, text="Fetch New Waybills", font=button_font,
            command=self._on_fetch_new_waybills, relief="flat",
            bg="#2d2d2d", fg="white", activebackground="#3d3d3d",
            activeforeground="white", padx=12, pady=6,
        )
        self.fetch_button.pack(pady=(6, 10))

        # Re-focus the entry any time the window (or anything in it) is
        # clicked or loses focus, so the operator never has to click the box.
        self.root.bind_all("<FocusOut>", lambda e: self.root.after(50, self._refocus))
        self.root.bind_all("<Button-1>", lambda e: self.root.after(50, self._refocus))

    def _refocus(self):
        if self._busy:
            return
        try:
            self.entry.focus_set()
        except tk.TclError:
            pass

    def _set_status(self, message: str, color: str = "#8fd694"):
        self.ui_queue.put(("status", message, color))

    def _signal_busy_done(self, message: str, color: str = "#8fd694"):
        """Thread-safe: call this from a background worker thread once
        indexing work is finished. queue.Queue.put is safe from any thread;
        the actual widget changes happen on the main thread inside
        _drain_ui_queue, which is the only place Tk widgets are touched
        from outside the main thread's own callbacks."""
        self.ui_queue.put(("busy_done", message, color))

    def _drain_ui_queue(self):
        try:
            while True:
                kind, message, color = self.ui_queue.get_nowait()
                if kind == "busy_done":
                    self._set_busy(False)
                self.status_var.set(message)
                self.status_label.configure(fg=color)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _on_close(self):
        self.logger.info("Application closed by operator (window close).")
        self.root.destroy()

    # --------------------------------------------------------- Busy/UI --

    def _set_busy(self, busy: bool, message: str = ""):
        """Locks out scanning (disables the input box) and shows a spinner
        while the initial index build or a manual fetch is running, so a
        scan can never land while the index is incomplete. Safe to call
        with busy=True only from the main thread (it touches widgets
        directly); busy=False is reached via the ui_queue from
        _drain_ui_queue, which also runs on the main thread."""
        self._busy = busy
        if busy:
            self._busy_message = message
            self.entry.config(state="disabled")
            self.fetch_button.config(state="disabled")
            self._spinner_index = 0
            self._spin_step()
        else:
            self.entry.config(state="normal")
            self.fetch_button.config(state="normal")
            if self._spinner_after_id is not None:
                try:
                    self.root.after_cancel(self._spinner_after_id)
                except Exception:
                    pass
                self._spinner_after_id = None
            self._refocus()

    def _spin_step(self):
        if not self._busy:
            return
        frame = _SPINNER_FRAMES[self._spinner_index % len(_SPINNER_FRAMES)]
        self._spinner_index += 1
        self.status_var.set(f"{frame} {self._busy_message}")
        self.status_label.configure(fg="#e0b84a")
        self._spinner_after_id = self.root.after(120, self._spin_step)

    def _on_fetch_new_waybills(self):
        if self._busy or self._processing:
            return
        self._set_busy(True, "Fetching new waybills...")
        threading.Thread(target=self._do_fetch_new_waybills, daemon=True).start()

    def _do_fetch_new_waybills(self):
        try:
            result = self.index.refresh()
            new_count = result.get("new", 0)
            if new_count:
                message = f"Found {new_count} new waybill(s)."
            else:
                message = "No new waybills found."
            color = "#8fd694"
            self.logger.info(f"Manual fetch: {message}")
        except Exception:
            self.logger.exception("Manual fetch of new waybills failed")
            message = "Error fetching new waybills - see log."
            color = "#e05c5c"
        self._signal_busy_done(message, color)

    # ------------------------------------------------------------ Scan --

    def _on_entry_changed(self, *_args):
        """Auto-fires the search the instant the input reaches the
        configured BIN length - no Enter key needed."""
        if self._processing or self._busy:
            return
        value = self.entry_var.get()
        target_len = self.cfg.get("bin_length", 16)
        if len(value) >= target_len:
            self._on_scan()

    def _on_scan(self, _event=None):
        if self._processing or self._busy:
            return
        raw_value = self.entry_var.get().strip()
        self.entry_var.set("")
        self._refocus()

        if not raw_value:
            return

        now = time.monotonic()
        cooldown = self.cfg.get("rescan_cooldown_seconds", 5)
        if raw_value == self.last_scan_value and (now - self.last_scan_time) < cooldown:
            self.logger.info(f"Ignored duplicate scan within cooldown window: '{raw_value}'")
            self._set_status(f"Duplicate scan ignored: {raw_value}", "#e0b84a")
            return

        self.last_scan_value = raw_value
        self.last_scan_time = now

        pattern = self.cfg["bin_pattern"]
        if not re.match(pattern, raw_value):
            self.logger.warning(f"Rejected scan - does not match expected BIN format: '{raw_value}'")
            self._set_status(f"Invalid BIN format: {raw_value}", "#e05c5c")
            return

        self.logger.info(f"Scan received: '{raw_value}'")
        self._set_status(f"Searching for {raw_value} ...", "#e0b84a")

        self._processing = True
        threading.Thread(target=self._process_scan, args=(raw_value,), daemon=True).start()

    def _process_scan(self, bin_value: str):
        try:
            matches = self.index.lookup(bin_value)
            if not matches:
                # File may have just arrived and the background pass hasn't
                # caught it yet - do one immediate targeted refresh before
                # declaring "no match".
                self.index.refresh()
                matches = self.index.lookup(bin_value)
        except Exception:
            self.logger.exception(f"Unexpected error while looking up '{bin_value}'")
            self._set_status(f"Search error for {bin_value} - see log", "#e05c5c")
            self._processing = False
            return

        if not matches:
            self.logger.info(f"No match found for BIN '{bin_value}'")
            self._set_status(f"No match found for {bin_value}", "#e05c5c")
            self._processing = False
            return

        groups = group_matches_into_waybills(
            matches, self.cfg.get("companion_file_suffix", "_InvoiceFile")
        )

        if len(groups) > 1:
            self.logger.warning(
                f"BIN '{bin_value}' found in {len(matches)} files belonging to "
                f"{len(groups)} different waybills - not printing. Files: "
                + ", ".join(m.file_path for m in matches)
            )
            self._set_status(format_multi_match_message(bin_value, matches), "#e05c5c")
            self._processing = False
            return

        to_process = groups[0]
        if len(to_process) > 1:
            self.logger.info(
                f"BIN '{bin_value}': waybill is split across {len(to_process)} files "
                "(waybill + invoice) - treating as one waybill: "
                + ", ".join(m.file_path for m in to_process)
            )

        print_mode = self.cfg.get("print_mode", "auto")

        if print_mode == "manual":
            opened_count = 0
            for match in to_process:
                try:
                    printer.open_pdf_for_manual_print(match.file_path)
                    opened_count += 1
                    self.logger.info(
                        f"Opened '{match.file_path}' for manual printing (BIN '{bin_value}'). "
                        "Not auto-archived - archive it manually once printed."
                    )
                except printer.PrintError as e:
                    self.logger.error(f"Failed to open '{match.file_path}' for BIN '{bin_value}': {e}")
                except Exception:
                    self.logger.exception(f"Unexpected error opening '{match.file_path}'")

            if opened_count == len(to_process):
                self._set_status(
                    f"Found {bin_value} - opened {opened_count} file(s) for manual printing. "
                    "Choose layout and print from the viewer.",
                    "#8fd694",
                )
            elif opened_count:
                self._set_status(
                    f"{bin_value}: only opened {opened_count} of {len(to_process)} file(s) - see log",
                    "#e05c5c",
                )
            else:
                self._set_status(f"Error opening match for {bin_value} - see log", "#e05c5c")
            self._processing = False
            return

        printed_count = 0
        failed_count = 0
        for match in to_process:
            try:
                printer.print_pdf(match.file_path, self.cfg)
                archiver.archive_file(
                    match.file_path,
                    match.search_root,
                    self.cfg["archive_directory"],
                    retry_attempts=self.cfg.get("archive_retry_attempts", 5),
                    retry_delay_seconds=self.cfg.get("archive_retry_delay_seconds", 1.5),
                )
                printed_count += 1
            except (printer.PrintError, archiver.ArchiveError) as e:
                failed_count += 1
                self.logger.error(f"Failed processing '{match.file_path}' for BIN '{bin_value}': {e}")
            except Exception:
                failed_count += 1
                self.logger.exception(f"Unexpected error handling match '{match.file_path}'")

        # One final status, decided after every file has been tried - so a
        # failure on the second file of a split waybill can never be hidden
        # behind a green "success" message from the first.
        if failed_count == 0:
            self.logger.info(f"Completed BIN '{bin_value}': {printed_count} file(s) printed and archived.")
            self._set_status(f"Printed and archived {printed_count} file(s) for {bin_value}", "#8fd694")
        elif printed_count == 0:
            self._set_status(f"Error printing/archiving match for {bin_value} - see log", "#e05c5c")
        else:
            self.logger.warning(
                f"BIN '{bin_value}': only {printed_count} of {len(to_process)} file(s) printed and archived."
            )
            self._set_status(
                f"{bin_value}: only {printed_count} of {len(to_process)} file(s) printed and archived - see log",
                "#e05c5c",
            )

        self._processing = False


def main():
    config_path = resolve_config_path()

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Configuration error", str(e))
        sys.exit(1)

    logger = setup_logger(
        cfg["log_directory"], cfg["log_max_bytes"], cfg["log_backup_count"]
    )
    logger.info("=" * 60)
    logger.info("WaybillMatcher starting")

    if cfg["print_method"] == "shell":
        logger.warning(
            "print_method is 'shell' - the default PDF handler can hold the file open "
            "after printing, delaying (or failing) the archive move. 'sumatra' is "
            "recommended for production - see README."
        )

    installed_printers = printer.list_installed_printers()
    if installed_printers:
        logger.info(f"Installed printers detected: {', '.join(installed_printers)}")
    try:
        target_printer = printer.resolve_and_validate_printer(cfg)
        logger.info(f"Printer check passed - print jobs will target: '{target_printer}'")
    except printer.PrintError as e:
        logger.error(f"Printer configuration problem detected at startup: {e}")

    guard = SingleInstanceGuard()
    if guard.already_running:
        logger.warning("Startup blocked - another instance is already running.")
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("Already running", "WaybillMatcher is already running.")
        sys.exit(0)

    index = PDFIndex(
        search_directories=cfg["search_directories"],
        archive_directory=cfg["archive_directory"],
        file_extensions=cfg["file_extensions"],
        bin_length=cfg["bin_length"],
        case_sensitive=cfg["case_sensitive_match"],
        recent_threshold_seconds=cfg.get("file_settle_threshold_seconds", 3.0),
    )

    root = tk.Tk()
    app = BinScanApp(root, cfg, index)
    app._set_busy(True, "Building initial index - this can take a moment on a large folder...")

    def build_initial_index_then_start_background_refresh():
        logger.info("Building initial PDF index...")
        try:
            result = index.refresh()
            logger.info(f"Initial index built ({result.get('new', 0)} file(s) indexed).")
        except Exception:
            logger.exception("Initial index build failed")
        app._signal_busy_done("Ready for scanning.", "#8fd694")
        index.start_background_refresh(cfg.get("index_refresh_seconds", 5))

    # Runs in the background so the window is usable immediately, even if
    # the folder has a large backlog of existing files to index first.
    threading.Thread(target=build_initial_index_then_start_background_refresh, daemon=True).start()

    root.mainloop()

    guard.release()
    logger.info("WaybillMatcher stopped.")


if __name__ == "__main__":
    main()