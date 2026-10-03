"""Storage layer: pages, records, buffer pool, heap tables, catalog.

Pipeline: engine talks to HeapTable; HeapTable talks to BufferPool;
BufferPool talks to a PageFile (memory or disk). Pages are fixed-size
byte buffers formatted by pages.py; rows are encoded by record.py.
"""
