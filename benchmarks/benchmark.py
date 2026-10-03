"""pico-sql performance benchmark.

Usage (from the repo root, with the package installed):
    python benchmarks/benchmark.py [rows] [queries]

Measures, on the SAME database and machine:
  1. insert throughput (WAL commit per multi-row statement)
  2. primary-key point query latency -- B+ tree index (O(log n) descent +
     one page fetch)
  3. the same lookup FORCED through a full table scan (``id + 0 = k`` defeats
     the planner: the predicate is no longer ``pk = literal``), showing what
     the index saves at this scale

Writes docs/benchmark.png (matplotlib) and prints the raw numbers.
Run it yourself -- absolute numbers vary by machine; the RATIO is the point.
"""

import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from picosql.engine import Database  # noqa: E402

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
QUERIES = int(sys.argv[2]) if len(sys.argv[2:3]) else 300


def bench() -> dict:
    rng = random.Random(2026)
    results = {}
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "bench.pico")
        db.execute_sql(
            "CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(40), grp INT);"
        )
        # inserts: batches of 100 -> one WAL commit (fsync) per statement
        t0 = time.perf_counter()
        for start in range(0, ROWS, 100):
            values = ", ".join(
                f"({i}, 'name-{i}', {i % 50})" for i in range(start, min(start + 100, ROWS))
            )
            db.execute_sql(f"INSERT INTO t VALUES {values};")
        results["insert_seconds"] = time.perf_counter() - t0
        results["rows"] = ROWS

        keys = rng.sample(range(ROWS), QUERIES)

        # warm the buffer pool once so we measure the index, not the disk
        db.execute_sql("SELECT id FROM t WHERE id = 0")

        t0 = time.perf_counter()
        for k in keys:
            db.execute_sql(f"SELECT id, name FROM t WHERE id = {k}")
        results["index_us"] = (time.perf_counter() - t0) / QUERIES * 1e6

        # force a full scan: "id + 0 = k" is not "pk = literal", so the
        # planner cannot push it down and FilterOperator reads every row
        t0 = time.perf_counter()
        for k in keys:
            db.execute_sql(f"SELECT id, name FROM t WHERE id + 0 = {k}")
        results["fullscan_us"] = (time.perf_counter() - t0) / QUERIES * 1e6

        results["speedup"] = results["fullscan_us"] / results["index_us"]
        results["index_hits"] = db.last_scan_used_index
        db.close()
    return results


def render_chart(results: dict, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    labels = ["PK point query\n(B+ tree index)", "same predicate\n(full table scan)"]
    values = [results["index_us"], results["fullscan_us"]]
    bars = ax.bar(labels, values, color=["#2a7f62", "#b0553a"], width=0.5)
    for bar, value in zip(bars, values):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value * 1.02,
            f"{value:.0f} µs",
            ha="center",
            va="bottom",
            fontsize=11,
        )
    ax.set_ylabel("average latency per query (µs)")
    ax.set_title(
        f"pico-sql: index vs full scan "
        f"({results['rows']:,} rows, {results['speedup']:.0f}× speedup)"
    )
    ax.set_ylim(0, max(values) * 1.2)
    fig.text(
        0.99,
        0.01,
        "measured by benchmarks/benchmark.py on the author's machine;\n"
        "absolute values vary, the ratio is the point",
        ha="right",
        fontsize=7,
        color="gray",
    )
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


if __name__ == "__main__":
    res = bench()
    print(f"rows inserted          : {res['rows']}")
    print(f"insert wall time       : {res['insert_seconds']:.2f}s "
          f"(WAL fsync per 100-row statement)")
    print(f"PK point query (index) : {res['index_us']:.1f} µs/query")
    print(f"same query, full scan  : {res['fullscan_us']:.1f} µs/query")
    print(f"speedup                : {res['speedup']:.0f}x")
    out = Path(__file__).resolve().parents[1] / "docs" / "benchmark.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    render_chart(res, out)
    print(f"chart written to       : {out}")
