"""Silent (no dialog) PDF printing, with two selectable backends, and
optional page-splitting so a multi-page PDF prints as separate jobs.

'shell'   - uses Windows' ShellExecute 'print' verb via the file's default
            PDF handler (Adobe Reader, Foxit, Edge, etc). Needs no extra
            download, but the handler app can stay open well past the
            print command returning, which blocks archiving until it
            closes - and on some systems (Edge in particular) that can be
            many seconds or more. Windows also only confirms the print
            *command* was issued, not that the page actually came out.

'sumatra' - shells out to SumatraPDF.exe with -print-to / -print-to-default
            and -silent. Exits deterministically as soon as the job is
            spooled, so the file is never left locked, and it's genuinely
            silent with no visible window. This is the recommended method
            for production use - see README for the one-time setup.
"""

import os
import shutil
import tempfile
import subprocess
import time

import pymupdf

try:
    import win32api
    import win32print
    # Attempt an explicit import to resolve pywin32's internal environment hooks early
    try:
        import win32timezone
    except ImportError:
        pass
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False

from logger_setup import get_logger

logger = get_logger()

_VIRTUAL_PRINTER_HINTS = (
    "microsoft print to pdf",
    "microsoft xps document writer",
    "onenote",
    "send to onenote",
    "adobe pdf",
    "fax",
)

_PRINT_TEMP_SUBDIR = "bin_scan_app_print_temp"
_PRINT_TEMP_MAX_AGE_SECONDS = 120  # generous upper bound - even a slow handler launch


def _get_print_temp_dir() -> str:
    temp_dir = os.path.join(tempfile.gettempdir(), _PRINT_TEMP_SUBDIR)
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir


def _cleanup_stale_print_temp_files(max_age_seconds: float = _PRINT_TEMP_MAX_AGE_SECONDS) -> None:
    """Removes isolated shell-print copies once they're old enough that no
    handler process could plausibly still need them. This is the safety
    mechanism that replaces "delete right after a fixed sleep" - see the
    comment in _print_via_shell for why that approach caused Adobe's
    "File cannot be found" error.
    """
    temp_dir = _get_print_temp_dir()
    try:
        entries = os.listdir(temp_dir)
    except OSError:
        return

    now = time.time()
    for name in entries:
        path = os.path.join(temp_dir, name)
        try:
            age_seconds = now - os.path.getmtime(path)
            if age_seconds > max_age_seconds:
                os.remove(path)
        except OSError:
            pass  # still in use, or already gone - fine, we'll try again next sweep


class PrintError(Exception):
    pass


def list_installed_printers():
    """Returns display names of every printer Windows knows about (real and
    virtual). Empty list if pywin32 isn't available."""
    if not _HAS_PYWIN32:
        return []
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception as e:
        logger.warning(f"Could not enumerate installed printers: {e}")
        return []


def get_default_printer():
    if not _HAS_PYWIN32:
        return None
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def _is_virtual_printer(name: str) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return any(hint in lowered for hint in _VIRTUAL_PRINTER_HINTS)


def resolve_and_validate_printer(cfg: dict) -> str:
    """Figures out which printer a job will actually go to, and raises
    PrintError early - before anything is sent to it - if that printer is a
    virtual one (Print to PDF, XPS, Fax, etc) that needs an interactive Save
    dialog and will never produce physical output, or if it can't be found
    among installed printers at all.
    """
    configured_name = cfg.get("printer_name", "").strip()

    if configured_name:
        if _is_virtual_printer(configured_name):
            raise PrintError(
                f"'printer_name' is set to '{configured_name}', which is a virtual printer - "
                "it needs an interactive Save dialog and will never produce physical output. "
                "Set 'printer_name' to your actual physical printer's exact name instead."
            )
        installed = list_installed_printers()
        if installed and configured_name not in installed:
            raise PrintError(
                f"Configured printer_name '{configured_name}' was not found among installed "
                f"printers on this PC: {', '.join(installed)}"
            )
        return configured_name

    default_name = get_default_printer()
    if not default_name:
        raise PrintError(
            "No 'printer_name' configured and the Windows default printer could not be "
            "determined. Set 'printer_name' in config.json to your physical printer's exact name."
        )
    if _is_virtual_printer(default_name):
        raise PrintError(
            f"The Windows default printer is '{default_name}', which is a virtual printer - "
            "it needs an interactive Save dialog and will never produce physical output, and can "
            "leave a stray dialog open on screen. Set 'printer_name' in config.json to your "
            "actual physical printer's exact name (check Settings > Printers & scanners)."
        )
    return default_name


def open_pdf_for_manual_print(file_path: str) -> None:
    """Opens a PDF in its default viewer (Adobe, etc) for the operator to
    review and print manually - choosing layout, binding, page range, etc
    themselves. Used when print_mode is 'manual' instead of the app
    silently auto-printing.
    """
    if not _HAS_PYWIN32:
        raise PrintError("pywin32 is required to open PDFs but is not installed")
    try:
        win32api.ShellExecute(0, "open", file_path, None, ".", 1)  # 1 = SW_SHOWNORMAL
    except Exception as e:
        raise PrintError(f"Failed to open '{file_path}' for manual printing: {e}")
    logger.info(f"Opened '{file_path}' in default viewer for manual printing.")


def print_pdf(file_path: str, cfg: dict) -> None:
    """Public entry point. Splits into per-page print jobs first if the
    file has more than one page and print_pages_separately is enabled."""
    if not cfg.get("print_pages_separately", True):
        _print_single_file(file_path, cfg)
        return

    try:
        with pymupdf.open(file_path) as doc:
            page_count = doc.page_count
    except Exception as e:
        raise PrintError(f"Could not open '{file_path}' to check page count: {e}")

    if page_count <= 1:
        _print_single_file(file_path, cfg)
        return

    logger.info(f"'{file_path}' has {page_count} pages - printing each as a separate job")
    _print_pages_separately(file_path, page_count, cfg)


def _print_pages_separately(file_path: str, page_count: int, cfg: dict) -> None:
    temp_paths = []
    try:
        with pymupdf.open(file_path) as doc:
            for page_index in range(page_count):
                single_page_doc = pymupdf.open()
                single_page_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
                fd, temp_path = tempfile.mkstemp(suffix=f"_page{page_index + 1}.pdf")
                os.close(fd)
                single_page_doc.save(temp_path)
                single_page_doc.close()
                temp_paths.append(temp_path)

        for i, temp_path in enumerate(temp_paths, start=1):
            logger.info(f"Printing page {i}/{page_count} of '{file_path}'")
            _print_single_file(temp_path, cfg)
    finally:
        for temp_path in temp_paths:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _print_single_file(file_path: str, cfg: dict) -> None:
    method = cfg["print_method"]
    if method == "shell":
        _print_via_shell(file_path, cfg)
    elif method == "sumatra":
        _print_via_sumatra(file_path, cfg)
    else:
        raise PrintError(f"Unknown print_method in config: '{method}'")


def _print_via_shell(file_path: str, cfg: dict) -> None:
    if not _HAS_PYWIN32:
        raise PrintError("pywin32 is required for print_method='shell' but is not installed")

    target_printer = resolve_and_validate_printer(cfg)
    if cfg.get("printer_name", "").strip():
        logger.warning(
            "'printer_name' is set but print_method='shell' always uses whatever Windows' "
            "default printer is - the configured printer_name has no effect on this method. "
            "Switch to print_method='sumatra' to target a specific printer directly."
        )

    # Opportunistic sweep: clears out any older isolated copies left behind
    # by previous prints, before we add a new one.
    _cleanup_stale_print_temp_files()

    # 1. Isolate the printing path by creating a safe temporary copy, in a
    # dedicated folder we manage ourselves (see _cleanup_stale_print_temp_files).
    temp_dir = _get_print_temp_dir()
    fd, temp_print_path = tempfile.mkstemp(suffix="_shell_print.pdf", dir=temp_dir)
    os.close(fd)

    try:
        shutil.copy2(file_path, temp_print_path)
    except Exception as e:
        try:
            os.remove(temp_print_path)
        except OSError:
            pass
        raise PrintError(f"Failed to isolate file for shell printing: {e}")

    # 2. Command the default PDF handler (Adobe, Edge, etc) to open and
    # print the isolated temporary file. ShellExecute is asynchronous - it
    # returns as soon as the handler process is *launched*, not once it has
    # actually finished opening/reading the file.
    try:
        win32api.ShellExecute(0, "print", temp_print_path, None, ".", 0)
    except Exception as e:
        try:
            os.remove(temp_print_path)
        except OSError:
            pass
        raise PrintError(f"ShellExecute print failed for '{file_path}': {e}")

    logger.info(f"Print command issued (shell verb) for isolated file - target printer: '{target_printer}'")

    # 3. This wait is just pacing between jobs, NOT a signal that the
    # handler has finished with the file - ShellExecute gives us no such
    # signal. A slow-to-launch handler (cold start, busy system) can easily
    # take longer than this, which is exactly why we do NOT delete the
    # temp copy based on this timer (see step 4).
    wait_time = cfg.get("print_wait_seconds", 3)
    time.sleep(wait_time)

    # Provide system diagnostics
    _log_print_queue_status(target_printer)

    # 4. Deliberately NOT deleting temp_print_path here. Deleting it on a
    # fixed timer caused exactly the "File cannot be found" error in Adobe:
    # if Adobe hadn't gotten around to actually opening the file yet, the
    # delete would silently succeed (nothing had it locked *yet*), and
    # Adobe would then fail moments later trying to open a file that was
    # already gone. Instead, isolated copies accumulate in a dedicated temp
    # folder and are swept up automatically once they're old enough that no
    # handler could plausibly still need them (see _cleanup_stale_print_temp_files,
    # called opportunistically at the top of this function on every print).


def _print_via_sumatra(file_path: str, cfg: dict) -> None:
    sumatra_path = cfg.get("sumatra_path", "")
    if not sumatra_path or not os.path.isfile(sumatra_path):
        raise PrintError(f"SumatraPDF executable not found at '{sumatra_path}'")

    target_printer = resolve_and_validate_printer(cfg)
    args = [sumatra_path, "-silent", "-print-to", target_printer, file_path]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise PrintError(f"SumatraPDF print timed out for '{file_path}'")
    except Exception as e:
        raise PrintError(f"Failed to launch SumatraPDF for '{file_path}': {e}")

    if result.returncode != 0:
        raise PrintError(
            f"SumatraPDF returned exit code {result.returncode} for '{file_path}'. "
            f"stderr: {result.stderr.strip()}"
        )

    logger.info(f"Printed via SumatraPDF to '{target_printer}': '{file_path}'")
    time.sleep(cfg.get("print_wait_seconds", 1))
    _log_print_queue_status(target_printer)


def _log_print_queue_status(printer_name: str, delay_seconds: float = 1.0) -> None:
    """Best-effort diagnostic: a clean SumatraPDF exit code only means the
    job was accepted by the Windows spooler - not that the physical printer
    produced anything. This peeks at the printer's queue shortly after
    submission and logs anything stuck there (paused, error, offline,
    out of paper), so a silent "nothing printed" has an actual reason
    attached to it in the log instead of just a success line.
    """
    if not _HAS_PYWIN32:
        return

    time.sleep(delay_seconds)
    try:
        handle = win32print.OpenPrinter(printer_name)
    except Exception as e:
        logger.warning(f"Could not open printer '{printer_name}' to check its queue: {e}")
        return

    try:
        jobs = win32print.EnumJobs(handle, 0, 20, 2)
        if not jobs:
            logger.info(f"Print queue for '{printer_name}' is empty - job likely completed normally.")
            return

        for job in jobs:
            status = job.get("Status", 0)
            flags = []
            if status & win32print.JOB_STATUS_PAUSED:
                flags.append("PAUSED")
            if status & win32print.JOB_STATUS_ERROR:
                flags.append("ERROR")
            if status & win32print.JOB_STATUS_OFFLINE:
                flags.append("PRINTER OFFLINE")
            if status & win32print.JOB_STATUS_PAPEROUT:
                flags.append("OUT OF PAPER")
            if status & win32print.JOB_STATUS_BLOCKED_DEVQ:
                flags.append("BLOCKED")
            if status & win32print.JOB_STATUS_PRINTING:
                flags.append("PRINTING")
            if status & win32print.JOB_STATUS_SPOOLING:
                flags.append("SPOOLING")
            logger.warning(
                f"Job still in queue for '{printer_name}': document='{job.get('pDocument')}', "
                f"status flags={flags or [f'UNKNOWN(raw={status})']}"
            )
    except Exception as e:
        logger.warning(f"Could not read job queue for '{printer_name}': {e}")
    finally:
        try:
            win32print.ClosePrinter(handle)
        except Exception:
            pass
