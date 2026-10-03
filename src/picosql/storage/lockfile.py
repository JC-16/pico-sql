"""Single-writer instance lock that dies with the process.

A lock file beside the database, locked at the OS level (msvcrt.locking on
Windows, fcntl.flock on POSIX). The OS releases the lock when the owning
process exits -- including when it is killed -- so a crashed writer can
never leave a stale lock behind, and a second writer can never silently
corrupt a database that is still open elsewhere.
"""

from __future__ import annotations

import os
from pathlib import Path

from .pages import PageError


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fh = None
        try:
            self._fh = open(self.path, "a+b")
            if self._fh.seek(0, os.SEEK_END) == 0:
                self._fh.write(b"\x00")  # the locked region needs one byte
                self._fh.flush()
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            raise PageError(
                f"database is locked by another instance ({self.path}): {exc}"
            ) from None

    def release(self) -> None:
        """Best-effort unlock; the OS also releases on handle close/exit."""
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None
