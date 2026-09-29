"""Moves a printed PDF into the archive directory, preserving its
sub-folder structure relative to whichever search directory it came from,
and never overwriting an existing file."""

import os
import shutil
import time

from logger_setup import get_logger

logger = get_logger()


class ArchiveError(Exception):
    pass


def archive_file(
    file_path: str,
    search_root: str,
    archive_directory: str,
    retry_attempts: int = 5,
    retry_delay_seconds: float = 1.5,
) -> str:
    """Returns the final path the file was moved to.

    Retries the move a few times before giving up: a file that was just
    printed can briefly still be held open by the viewer/handler that
    printed it, and a short retry loop rides that out instead of failing
    the whole scan.
    """
    try:
        rel_path = os.path.relpath(file_path, search_root)
    except ValueError:
        # file_path and search_root on different drives, etc. - fall back to just the filename.
        rel_path = os.path.basename(file_path)

    destination = os.path.join(archive_directory, rel_path)
    dest_dir = os.path.dirname(destination)

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise ArchiveError(f"Could not create archive folder '{dest_dir}': {e}")

    destination = _resolve_collision(destination)

    last_error = None
    for attempt in range(1, retry_attempts + 1):
        try:
            shutil.move(file_path, destination)
            logger.info(f"Archived '{file_path}' -> '{destination}'")
            return destination
        except OSError as e:
            last_error = e
            logger.warning(
                f"Archive move attempt {attempt}/{retry_attempts} failed for "
                f"'{file_path}' (likely still held open by the print handler): {e}"
            )
            time.sleep(retry_delay_seconds)

    raise ArchiveError(
        f"Could not move '{file_path}' to '{destination}' after {retry_attempts} attempts: {last_error}"
    )


def _resolve_collision(destination: str) -> str:
    """If a file with this name already exists in the archive, append a
    timestamp rather than overwrite or fail."""
    if not os.path.exists(destination):
        return destination

    base, ext = os.path.splitext(destination)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{base}_{timestamp}{ext}"
