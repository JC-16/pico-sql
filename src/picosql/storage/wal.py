"""Write-ahead log: the guarantee that a committed statement survives a crash.

Rule (write-ahead): before any dirty page may reach the data file, the log
record describing that page's committed image must reach stable storage.

Design, honestly scoped:

* **Physical full-page-image redo logging.** Each record stores the complete
  4 KB image of every dirty page at commit time. Redo = "write these images
  back". This is deliberately simple and impossible to get subtly wrong --
  the tradeoff (large records, base64 +33%) is documented in design.md
  section 3.14. Real engines log per-record deltas with LSN-chained pages.
* **One record per committed statement** (autocommit): the engine appends
  + fsyncs right after a statement finishes applying, so anything that
  returns to the caller is durable. A statement that never finished wrote
  no record, so its effects vanish on recovery -- exactly the semantics
  transactions should have.
* **Framing**: ``[u32 payload_len][u32 crc32][u32 lsn][payload]``. A torn
  tail (crash mid-append) or a CRC mismatch stops replay at that record and
  the file is truncated there -- bytes after the last valid record never
  happened.
* **Checkpoint**: after the buffer pool flushes and the data file fsyncs,
  the log is redundant and is truncated to zero. Checkpoints happen every
  32 commits and on close.

Payload is JSON with base64 page images: human-inspectable, easy to parse
correctly, and honest about its cost.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Iterator

from .pages import PAGE_SIZE

_HEADER = struct.Struct(">III")  # payload_len, crc32, lsn


class WalError(Exception):
    """The log file itself is unusable (cannot be opened/appended)."""


class WriteAheadLog:
    def __init__(self, path):
        self.path = Path(path)
        self.lsn = 0
        self._valid_end = 0  # byte offset after the last valid record

    # ----------------------------------------------------------------- append

    def append_commit(self, pages: list) -> int:
        """Append one commit record (list of (page_id, 4096-byte image)) and
        fsync it. Returns the record's LSN."""
        payload_obj = {
            "type": "commit",
            "lsn": self.lsn,
            "pages": [
                [page_id, base64.b64encode(image).decode("ascii")]
                for page_id, image in pages
            ],
        }
        payload = json.dumps(payload_obj, separators=(",", ":")).encode("utf-8")
        header = _HEADER.pack(len(payload), zlib.crc32(payload) & 0xFFFFFFFF, self.lsn)
        try:
            with open(self.path, "ab") as handle:
                handle.write(header)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise WalError(f"cannot append to WAL {self.path}: {exc}") from None
        self.lsn += 1
        return self.lsn - 1

    # ----------------------------------------------------------------- replay

    def replay(self) -> Iterator:
        """Yield (lsn, pages) for every intact commit record, oldest first.

        Stops silently at the first torn/corrupt record (that record and
        everything after it never reached stable storage). ``valid_end``
        then marks where the file should be truncated.
        """
        self._valid_end = 0
        if not self.path.exists():
            return
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise WalError(f"cannot read WAL {self.path}: {exc}") from None
        pos = 0
        total = len(data)
        while pos + _HEADER.size <= total:
            payload_len, crc, lsn = _HEADER.unpack_from(data, pos)
            if payload_len > total - pos - _HEADER.size:
                break  # torn tail: header promises more bytes than exist
            start = pos + _HEADER.size
            payload = data[start : start + payload_len]
            if zlib.crc32(payload) & 0xFFFFFFFF != crc:
                break  # corrupt payload never fully reached disk
            try:
                obj = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                break
            pages = []
            valid = obj.get("type") == "commit"
            for entry in obj.get("pages", []) if valid else []:
                if not (isinstance(entry, list) and len(entry) == 2):
                    valid = False
                    break
                page_id, b64 = entry
                if not isinstance(page_id, int) or not isinstance(b64, str):
                    valid = False
                    break
                try:
                    image = base64.b64decode(b64, validate=True)
                except Exception:
                    valid = False
                    break
                if len(image) != PAGE_SIZE:
                    valid = False
                    break
                pages.append((page_id, image))
            if not valid:
                break
            yield lsn, pages
            pos = start + payload_len
            self._valid_end = pos
            self.lsn = lsn + 1

    # -------------------------------------------------------------- lifecycle

    def truncate(self) -> None:
        """Checkpoint: the log's contents are now redundant; reset it."""
        try:
            with open(self.path, "wb") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise WalError(f"cannot truncate WAL {self.path}: {exc}") from None
        self.lsn = 0
        self._valid_end = 0

    def discard_torn_tail(self) -> None:
        """Cut the file at the last valid record (called after replay)."""
        if not self.path.exists():
            return
        if self._valid_end < self.path.stat().st_size:
            with open(self.path, "r+b") as handle:
                handle.truncate(self._valid_end)
                handle.flush()
                os.fsync(handle.fileno())

    def close(self) -> None:
        pass  # each append opens/writes/fsyncs/closes; nothing to hold
