# BIN Scan - Search - Print - Archive

A kiosk-style Windows app: scan a BIN with a barcode scanner, it instantly
searches an in-memory index of configured PDF folders, silently prints the
matching PDF(s) (one page per print job), and moves them to an archive
folder. No mouse click or Enter key required during normal operation.

## What's included

```
bin_scan_app/
  config.json           <- edit this before running
  requirements.txt
  build_exe.bat          <- builds the .exe on Windows
  src/
    main.py              <- GUI + orchestration
    config_loader.py      <- loads/validates config.json
    indexer.py              <- in-memory BIN index, background refresh
    printer.py               <- silent printing, splits multi-page PDFs
    archiver.py                <- moves printed files to /archive, with retry
    single_instance.py          <- prevents two copies running at once
    logger_setup.py               <- rotating log file
```

## What changed in this revision

1. **Files weren't being removed from the search folder (#1) and the same
   error kept appearing (#5, #6).** Root cause, visible in your log:
   `[WinError 32] ... being used by another process`. The `"shell"` print
   method opens the PDF in your default viewer to print it, and that
   viewer can hold the file open well past when the archive-move happens.
   Fixed two ways: the config now defaults to `"sumatra"` as the print
   method, which exits cleanly the moment the job is spooled and never
   holds the file open (see setup step below - it needs a one-time
   install); and the archive move now **retries automatically** (5 times,
   1.5s apart by default) before giving up, as a safety net either way.

2. **Search was slow.** It was re-reading and re-extracting text from
   every PDF on every single scan. Replaced with `indexer.py`: an
   in-memory index of every BIN-like token found in every PDF, built once
   at startup and kept current by a background thread that only
   re-parses a file if its size/modified-time actually changed. A scan is
   now a dictionary lookup - effectively instant once the index is warm.
   If a file was dropped in seconds ago and hasn't been picked up by the
   background pass yet, a lookup miss triggers one immediate targeted
   refresh before reporting "no match", so nothing gets missed.

3. **Had to press Enter.** Since your BINs are always 16 characters, the
   app now auto-fires the search the instant the input reaches that
   length - no Enter key needed. Enter still works too, in case you ever
   scan something a different length. Configurable via `bin_length`.

4. **Multi-page PDFs should print as separate jobs.** `printer.py` now
   checks the page count and, if more than 1, splits the PDF into
   individual single-page temporary files and sends each as its own
   separate print job, then deletes the temp files. Toggle with
   `print_pages_separately` (default `true`). The original multi-page
   file is still archived as one file - only the *printing* is split.

5. **Log shows success but nothing physically prints.** This happens when
   the print job goes to a **virtual printer** - "Microsoft Print to PDF",
   "Adobe PDF", "Microsoft XPS Document Writer", a fax driver, etc. These
   report success but actually need someone to click through an
   interactive Save dialog, which never happens on a kiosk. The app now
   checks the target printer *before* sending anything: it looks up
   either your configured `printer_name` or the Windows default, refuses
   to print (with a clear log message) if it looks virtual or isn't found
   among installed printers at all, and logs every installed printer name
   at startup so you can copy the exact right one into config. `sumatra`
   mode now also always targets an explicit printer by name rather than
   "whatever the default is" - one less thing that can silently drift.

## 1. Configure

Edit `config.json`:

| Key | Meaning |
|---|---|
| `search_directories` | List of folders to search (network paths work). Add as many as you like. |
| `archive_directory` | Where printed PDFs get moved to. Sub-folder structure is preserved. |
| `file_extensions` | Which extensions to scan, e.g. `[".pdf"]`. |
| `bin_pattern` | Regex the scanned input must match before a search starts. Default now requires exactly 16 alphanumeric/dash characters. |
| `bin_length` | **New.** Exact BIN length - drives both the auto-search trigger and how the index extracts candidate tokens from PDF text. Set to your real BIN length if it's ever not 16. |
| `case_sensitive_match` | Usually leave `false`. |
| `print_method` | `"sumatra"` (recommended, default) or `"shell"` (no install needed, but can hold file locks - see above). |
| `printer_name` | Leave `""` to use the Windows default printer, or name a specific one. |
| `sumatra_path` | Full path to `SumatraPDF.exe`. Required if `print_method` is `"sumatra"`. |
| `print_pages_separately` | **New.** `true` (default) prints each page of a multi-page PDF as its own job. Set `false` to print the whole file as one job. |
| `multiple_match_behavior` | `"print_all"`, `"print_newest"`, or `"flag_only"` when more than one PDF matches the same BIN. |
| `index_refresh_seconds` | **New.** How often the background thread checks for new/changed files, in seconds. |
| `archive_retry_attempts` / `archive_retry_delay_seconds` | **New.** How many times (and how far apart) to retry moving a file to archive if it's briefly locked. |
| `log_directory` | Where the rotating log file is written. |
| `rescan_cooldown_seconds` | Ignores an identical scan repeated within this window (guards against a scanner double-firing). |

## 2. Set the physical printer explicitly (do this now)

Don't leave `printer_name` empty and rely on "whatever Windows' default
printer is" - on the machine in your log, the default resolves to
something that never produces physical output. Instead:

1. On the kiosk PC, open **Settings > Bluetooth & devices > Printers &
   scanners** and find your real physical printer's exact display name.
2. Set `"printer_name"` in `config.json` to that exact name.
3. Start the app and check the log - it now lists every installed
   printer at startup and confirms which one jobs will target, or tells
   you clearly if something's wrong (virtual printer, not found, etc)
   before anything is ever sent to it.

## 3. Install SumatraPDF (fixes the file-lock/archive error)

1. Download SumatraPDF **portable** from https://www.sumatrapdfreader.org/download-free-pdf-viewer (portable .zip, no installer needed).
2. Unzip it anywhere on the kiosk PC, e.g. `C:\Tools\SumatraPDF\`.
3. Confirm `sumatra_path` in `config.json` points at the real `SumatraPDF.exe` location.

`print_method` is already set to `"sumatra"` by default in the new
config - you just need the executable in place. (I can't download and
bundle this binary myself in this environment - it's a two-minute manual
step on your end.) If you skip this step and stick with `"shell"`, the
app still works, just slower to archive and occasionally needing a retry.

## 4. Run from source (for testing before building the exe)

On a Windows machine with Python 3.10+ installed:

```
pip install -r requirements.txt
python src\main.py
```

The window opens, builds its initial index (may take a few seconds the
first time if the folder already has many PDFs in it), then is ready.
Scan a test BIN (or type 16 characters) to confirm it auto-searches,
prints each page separately, and archives correctly - check the log for
detail on every step.

## 5. Build the standalone .exe

```
build_exe.bat
```

Produces `BinScanApp.exe` in the project folder. Copy `config.json` into
the same folder as the .exe - it looks for it right next to itself.

## 6. Deploy on the kiosk PC

1. Copy `BinScanApp.exe`, `config.json`, and (if using `sumatra`) the
   SumatraPDF folder to the kiosk PC.
2. Point `search_directories` / `archive_directory` at the real network paths.
3. Test with a real scanned barcode and a real PDF containing that BIN.
4. To start automatically at login: put a shortcut to `BinScanApp.exe` in
   `shell:startup` (Win+R -> `shell:startup` -> paste shortcut there).

## Safeguards built in

- **Auto-search at exact BIN length** - no Enter key needed, but Enter
  still works as a fallback.
- **Regex validation** rejects malformed scans before any search happens.
- **Duplicate-scan cooldown** ignores an identical scan fired twice in
  quick succession.
- **Single-instance lock** - a second launch is blocked with a warning.
- **Index freshness guard** - a lookup miss triggers one immediate
  refresh before reporting "no match", so a just-arrived file isn't missed.
- **File-settle check** - won't index a PDF that's still being copied in.
- **Corrupt/unreadable PDFs** are logged and skipped, never crash the app.
- **No-match / multiple-match** are both logged and shown on screen.
- **Archive collisions** get a timestamp suffix instead of overwriting.
- **Archive retry** rides out a file being briefly held open by the
  print handler instead of failing the whole scan on the first attempt.
- **Archive folder is excluded from the index** so archived files can
  never be "found" and reprocessed.
- **Config validated at startup** - the app refuses to start with a
  broken config rather than failing confusingly mid-shift.
- **Everything is logged** to a rotating file (5 x 5MB by default).

## Known limitations / things to confirm on your side

- Assumes your PDFs have a real text layer - scanned-image-only PDFs
  would need OCR added (let me know if you hit this).
- Even with `sumatra`, the archive retry exists as a safety net, but if a
  file is *still* locked after 5 attempts (~7.5s total by default),
  something else on the PC (antivirus scan, another program) is likely
  holding it - worth checking what's installed on the kiosk PC if that
  ever shows up in the log.
