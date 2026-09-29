"""Keeps an in-memory index of BIN-like tokens found inside PDFs, so a scan
is an instant dictionary lookup instead of re-reading every PDF every time.

A background thread periodically re-scans the configured directories, but
only re-parses a file if its size or modified-time has changed since it was
last indexed - unchanged files are never re-read. New files dropped into
the folder get picked up on the next background pass; a scan that misses
the index also triggers one immediate targeted refresh, so a file that
arrived seconds ago is never a false "no match".
"""

import os
import re
import threading
import time
from dataclasses import dataclass

import pymupdf

from logger_setup import get_logger

logger = get_logger()


@dataclass
class MatchResult:
    file_path: str
    search_root: str
    modified_time: float


class PDFIndex:
    def __init__(self, search_directories, archive_directory, file_extensions,
                 bin_length: int, case_sensitive: bool = False,
                 recent_threshold_seconds: float = 3.0):
        self.search_directories = search_directories
        self.archive_directory = os.path.abspath(archive_directory) if archive_directory else None
        self.extensions = tuple(ext.lower() for ext in file_extensions)
        self.case_sensitive = case_sensitive
        self.recent_threshold_seconds = recent_threshold_seconds

        # Token-extraction pattern: exactly bin_length alnum/dash characters,
        # not glued onto a longer run of the same characters either side.
        self._token_pattern = re.compile(
            rf"(?<![A-Za-z0-9]{{1}})[A-Za-z0-9\-]{{{bin_length}}}(?![A-Za-z0-9])",
            0 if case_sensitive else re.IGNORECASE,
        )

        self._lock = threading.Lock()
        self._file_cache = {}   # file_path -> (size, mtime)
        self._index = {}        # normalized token -> list[MatchResult]
        self._unreachable_dirs = set()  # avoids re-logging the same error every few seconds
        self._zero_byte_warned = set()  # avoids re-logging the same 0-byte file every refresh

    def _normalize(self, token: str) -> str:
        return token if self.case_sensitive else token.lower()

    def _in_archive(self, dirpath: str) -> bool:
        return bool(self.archive_directory) and os.path.abspath(dirpath).startswith(self.archive_directory)

    def refresh(self) -> dict:
        """One full pass: index new/changed files, drop entries for files
        that no longer exist (e.g. already archived). Returns a small
        summary dict {"new": N, "removed": M} so callers (the manual
        "fetch new waybills" button, the startup build) can report
        something meaningful instead of a bare "done"."""
        seen_paths = set()
        new_count = 0

        for root_dir in self.search_directories:
            if not os.path.isdir(root_dir):
                if root_dir not in self._unreachable_dirs:
                    logger.error(f"Search directory not reachable, skipping: '{root_dir}'")
                    self._unreachable_dirs.add(root_dir)
                continue
            if root_dir in self._unreachable_dirs:
                logger.info(f"Search directory is reachable again: '{root_dir}'")
                self._unreachable_dirs.discard(root_dir)

            for dirpath, _dirnames, filenames in os.walk(root_dir):
                if self._in_archive(dirpath):
                    continue
                for filename in filenames:
                    if not filename.lower().endswith(self.extensions):
                        continue
                    full_path = os.path.join(dirpath, filename)
                    seen_paths.add(full_path)
                    if self._maybe_index_file(full_path, root_dir):
                        new_count += 1

        removed_count = self._drop_missing(seen_paths)
        return {"new": new_count, "removed": removed_count}

    def _maybe_index_file(self, full_path, root_dir) -> bool:
        """Returns True only when this file was actually (re)indexed this
        call - False for unchanged/zero-byte/not-ready files, so the caller
        can count real new/changed files rather than every file touched."""
        try:
            stat = os.stat(full_path)
        except OSError:
            return False
        fingerprint = (stat.st_size, stat.st_mtime)

        with self._lock:
            cached = self._file_cache.get(full_path)
        if cached == fingerprint:
            return False  # unchanged - already indexed, skip re-parsing

        if stat.st_size == 0:
            if full_path not in self._zero_byte_warned:
                logger.warning(
                    f"'{full_path}' is 0 bytes - skipping. If this persists, check whether the "
                    "source system finished writing it, and check file-content read permissions "
                    "and antivirus/EDR interference for the account running this app."
                )
                self._zero_byte_warned.add(full_path)
            return False

        if not self._is_file_ready(full_path, recent_threshold_seconds=self.recent_threshold_seconds):
            return False  # still being written into the folder - try again next pass

        tokens = set(self._extract_tokens(full_path))  # dedupe - a BIN can appear on multiple pages

        with self._lock:
            self._remove_file_from_index(full_path)
            self._file_cache[full_path] = fingerprint
            for token in tokens:
                self._index.setdefault(self._normalize(token), []).append(
                    MatchResult(file_path=full_path, search_root=root_dir, modified_time=stat.st_mtime)
                )
        return True

    def _extract_tokens(self, full_path):
        try:
            with pymupdf.open(full_path) as doc:
                text = "".join(page.get_text() for page in doc)
        except Exception as e:
            logger.warning(f"Could not read '{full_path}' while indexing - skipping. Reason: {e}")
            return []
        return self._token_pattern.findall(text)

    def _remove_file_from_index(self, full_path):
        for token, matches in list(self._index.items()):
            remaining = [m for m in matches if m.file_path != full_path]
            if remaining:
                self._index[token] = remaining
            else:
                del self._index[token]
        self._file_cache.pop(full_path, None)

    def _drop_missing(self, seen_paths) -> int:
        with self._lock:
            missing = [p for p in self._file_cache if p not in seen_paths]
        for path in missing:
            with self._lock:
                self._remove_file_from_index(path)
        return len(missing)

    @staticmethod
    def _is_file_ready(file_path, settle_seconds=0.4, recent_threshold_seconds=3):
        """A file that hasn't been touched recently is extremely unlikely to
        still be mid-write, so skip the settle-check delay entirely for it -
        this is the difference between an initial scan of a large backlog
        taking seconds instead of minutes. Only files modified within the
        last `recent_threshold_seconds` get the full before/after size check.
        """
        try:
            stat = os.stat(file_path)
        except OSError:
            return False

        age_seconds = time.time() - stat.st_mtime
        if age_seconds > recent_threshold_seconds:
            return True

        try:
            size_before = stat.st_size
            time.sleep(settle_seconds)
            size_after = os.path.getsize(file_path)
            return size_before == size_after
        except OSError:
            return False

    def lookup(self, bin_value: str):
        key = self._normalize(bin_value)
        with self._lock:
            matches = list(self._index.get(key, []))
        matches.sort(key=lambda m: m.modified_time, reverse=True)
        return matches

    def start_background_refresh(self, interval_seconds: float):
        def _loop():
            while True:
                try:
                    result = self.refresh()
                    if result.get("new") or result.get("removed"):
                        logger.info(
                            f"Background refresh: {result.get('new', 0)} new, "
                            f"{result.get('removed', 0)} removed"
                        )
                except Exception:
                    logger.exception("Background index refresh failed")
                time.sleep(interval_seconds)

        thread = threading.Thread(target=_loop, daemon=True)
        thread.start()
        return thread
