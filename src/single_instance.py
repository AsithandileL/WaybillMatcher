"""Prevents two copies of the app running at once on the same kiosk PC,
using a named Windows mutex. If pywin32 isn't available (e.g. running the
source on a non-Windows dev machine for a quick test), it degrades to a
no-op rather than crashing."""

try:
    import win32event
    import win32api
    import winerror
    _HAS_PYWIN32 = True
except ImportError:
    _HAS_PYWIN32 = False

MUTEX_NAME = "Global\\BinScanApp_SingleInstance_Mutex"


class SingleInstanceGuard:
    def __init__(self):
        self._handle = None
        self.already_running = False

        if not _HAS_PYWIN32:
            return

        self._handle = win32event.CreateMutex(None, False, MUTEX_NAME)
        last_error = win32api.GetLastError()
        if last_error == winerror.ERROR_ALREADY_EXISTS:
            self.already_running = True

    def release(self):
        if self._handle is not None:
            try:
                win32api.CloseHandle(self._handle)
            except Exception:
                pass
