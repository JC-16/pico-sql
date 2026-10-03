"""The catalog: schemas + page lists, stored as JSON in page 0.

Real engines keep the catalog in dedicated binary pages with transactional
updates (PostgreSQL's pg_class, SQLite's sqlite_master). A JSON page is an
honest simplification: it keeps the data path fully page-based while making
the catalog human-inspectable. The 4 KB page caps how much schema a single
database can hold -- the error says so explicitly.
"""

from __future__ import annotations

import json

from .bufferpool import BufferPool
from .pages import PAGE_SIZE, PageError

CATALOG_PAGE_ID = 0
CATALOG_VERSION = 1

_EMPTY = {"version": CATALOG_VERSION, "tables": {}, "free_pages": []}


def load_catalog(pool: BufferPool) -> dict:
    if pool.page_file.num_pages == 0:
        return json.loads(json.dumps(_EMPTY))  # deep copy
    page = pool.get(CATALOG_PAGE_ID)
    text = bytes(page).rstrip(b"\x00").decode("utf-8")
    if not text:
        return json.loads(json.dumps(_EMPTY))
    catalog = json.loads(text)
    if catalog.get("version") != CATALOG_VERSION:
        raise PageError(f"unknown catalog version {catalog.get('version')!r}")
    return catalog


def save_catalog(pool: BufferPool, catalog: dict) -> None:
    payload = json.dumps(catalog, ensure_ascii=False).encode("utf-8")
    if len(payload) > PAGE_SIZE:
        raise PageError(
            f"catalog too large for one page ({len(payload)} > {PAGE_SIZE} bytes)"
        )
    if pool.page_file.num_pages == 0:
        pool.page_file.alloc_page()  # page 0 for the catalog
    page = pool.get(CATALOG_PAGE_ID)
    page[:] = payload + b"\x00" * (PAGE_SIZE - len(payload))
    pool.mark_dirty(CATALOG_PAGE_ID)
