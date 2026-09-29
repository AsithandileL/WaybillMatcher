"""Loads and validates config.json. Fails loudly and early if the config
is broken, rather than letting the app start in a half-configured state."""

import json
import os
import re
import sys

REQUIRED_KEYS = [
    "search_directories",
    "archive_directory",
    "file_extensions",
    "bin_pattern",
    "log_directory",
]

DEFAULTS = {
    "case_sensitive_match": False,
    "print_method": "shell",
    "printer_name": "",
    "sumatra_path": "",
    "print_wait_seconds": 3,
    "print_pages_separately": True,
    "print_mode": "auto",
    "sumatra_print_settings": "",
    "log_max_bytes": 5_242_880,
    "log_backup_count": 5,
    "rescan_cooldown_seconds": 5,
    "bin_length": 16,
    "index_refresh_seconds": 5,
    "file_settle_threshold_seconds": 3.0,
    "archive_retry_attempts": 5,
    "archive_retry_delay_seconds": 1.5,
}


class ConfigError(Exception):
    pass


# A real UNC path is \\server\share\... (two leading backslashes). In JSON,
# writing a literal backslash requires "\\", so the correct UNC path needs
# FOUR backslashes at the start of the JSON string. Two backslashes at the
# start collapses to a single literal backslash - a very common mistake
# that produces an unreachable path with no obvious explanation in the log.
_MALFORMED_UNC_PATTERN = re.compile(r"^\\[^\\]")


def _check_for_malformed_unc(path: str, label: str) -> None:
    if _MALFORMED_UNC_PATTERN.match(path):
        raise ConfigError(
            f"'{label}' is set to '{path}', which looks like a malformed network path - "
            "it starts with a single backslash. A UNC path \\\\server\\share must be "
            "written in config.json with FOUR backslashes at the start and two for every "
            'separator after that, e.g. "\\\\\\\\server\\\\share\\\\folder". '
            "Fix the escaping and try again."
        )


def load_config(config_path: str) -> dict:
    if not os.path.isfile(config_path):
        raise ConfigError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"config.json is not valid JSON: {e}")

    for key in REQUIRED_KEYS:
        if key not in cfg:
            raise ConfigError(f"config.json is missing required key: '{key}'")

    # Fill in optional keys with sane defaults.
    for key, default in DEFAULTS.items():
        cfg.setdefault(key, default)

    if not isinstance(cfg["search_directories"], list) or not cfg["search_directories"]:
        raise ConfigError("'search_directories' must be a non-empty list")

    for path in cfg["search_directories"]:
        _check_for_malformed_unc(path, "search_directories entry")
    _check_for_malformed_unc(cfg["archive_directory"], "archive_directory")

    if cfg["print_method"] not in ("shell", "sumatra"):
        raise ConfigError("'print_method' must be 'shell' or 'sumatra'")

    if cfg["print_mode"] not in ("auto", "manual"):
        raise ConfigError("'print_mode' must be 'auto' or 'manual'")

    if cfg["print_method"] == "sumatra" and not cfg.get("sumatra_path"):
        raise ConfigError("'print_method' is 'sumatra' but 'sumatra_path' is empty")

    # Validate the regex compiles - fail at startup, not on first scan.
    try:
        re.compile(cfg["bin_pattern"])
    except re.error as e:
        raise ConfigError(f"'bin_pattern' is not a valid regex: {e}")

    if not isinstance(cfg["bin_length"], int) or cfg["bin_length"] <= 0:
        raise ConfigError("'bin_length' must be a positive integer")

    return cfg


def resolve_config_path() -> str:
    """config.json is expected next to the executable (or next to this
    script when running from source)."""
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "config.json")
