"""Day 4: write-ahead log, crash recovery, and commit durability."""

import subprocess
import sys

import pytest

from picosql.engine import Database, EngineError
from picosql.storage.pages import PAGE_SIZE, PageError
from picosql.storage.wal import WalError, WriteAheadLog


# ------------------------------------------------------------ WAL unit tests


def test_append_and_replay_roundtrip(tmp_path):
    wal = WriteAheadLog(tmp_path / "db.pico.wal")
    page_a = bytearray(PAGE_SIZE)
    page_a[0:4] = (1).to_bytes(4, "big")
    page_b = bytearray(PAGE_SIZE)
    page_b[0:4] = (2).to_bytes(4, "big")
    wal.append_commit([(0, bytes(page_a))])
    wal.append_commit([(1, bytes(page_b))])

    records = list(wal.replay())
    assert len(records) == 2
    lsn1, pages1 = records[0]
    assert lsn1 == 0 and pages1[0][0] == 0
    assert pages1[0][1][0:4] == (1).to_bytes(4, "big")


def test_replay_stops_at_torn_tail(tmp_path):
    wal = WriteAheadLog(tmp_path / "db.pico.wal")
    image = bytes(PAGE_SIZE)
    wal.append_commit([(0, image)])
    wal.append_commit([(1, image)])
    raw = wal.path.read_bytes()
    # simulate a crash halfway through the second record
    wal.path.write_bytes(raw[: len(raw) - 100])
    wal2 = WriteAheadLog(wal.path)
    records = list(wal2.replay())
    assert len(records) == 1  # the torn record never happened
    wal2.discard_torn_tail()
    assert wal2.path.stat().st_size == wal2._valid_end


def test_replay_stops_at_crc_corruption(tmp_path):
    wal = WriteAheadLog(tmp_path / "db.pico.wal")
    wal.append_commit([(0, bytes(PAGE_SIZE))])
    wal.append_commit([(1, bytes(PAGE_SIZE))])
    raw = bytearray(wal.path.read_bytes())
    header = 12
    raw[header + 5] ^= 0xFF  # flip one bit inside the FIRST payload
    wal.path.write_bytes(bytes(raw))
    wal2 = WriteAheadLog(wal.path)
    assert list(wal2.replay()) == []  # first record invalid: nothing after counts


def test_truncate_resets_the_log(tmp_path):
    wal = WriteAheadLog(tmp_path / "db.pico.wal")
    wal.append_commit([(0, bytes(PAGE_SIZE))])
    wal.truncate()
    assert wal.path.stat().st_size == 0
    assert list(wal.replay()) == []


# --------------------------------------------------- engine-level durability


def test_committed_statements_survive_without_close(tmp_path):
    """The Day 2 gap, closed: another instance sees committed rows even
    though the writer never called close()."""
    path = tmp_path / "live.pico"
    writer = Database(path)
    writer.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT);")
    writer.execute_sql("INSERT INTO t VALUES (1, 10);")
    reader = Database(path)  # opens while the writer is still open
    (res,) = reader.execute_sql("SELECT id, v FROM t")
    assert res.rows == [[1, 10]]
    reader.close()
    writer.execute_sql("INSERT INTO t VALUES (2, 20);")
    reader2 = Database(path)
    (res,) = reader2.execute_sql("SELECT id FROM t ORDER BY id")
    assert [r[0] for r in res.rows] == [1, 2]
    reader2.close()
    writer.close()


def test_checkpoint_truncates_wal_and_data_remains(tmp_path):
    path = tmp_path / "ckpt.pico"
    db = Database(path)
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT);")
    db.execute_sql("INSERT INTO t VALUES (1, 1);")
    wal_path = path.with_suffix(".pico.wal")
    assert wal_path.stat().st_size > 0  # at least one commit record
    db.checkpoint()
    assert wal_path.stat().st_size == 0  # flushed + fsynced -> log redundant
    (res,) = db.execute_sql("SELECT id FROM t")
    assert res.rows == [[1]]
    db.close()


def test_recovery_replays_after_simulated_crash(tmp_path):
    """Simulate: commits reached the WAL; the data file never got flushed
    (no close, no checkpoint). Recovery must rebuild the committed state."""
    path = tmp_path / "reco.pico"
    writer = Database(path)
    writer.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT);")
    writer.execute_sql("INSERT INTO t VALUES (1, 11), (2, 22);")
    # discard the writer WITHOUT flushing: buffers die, WAL survives
    writer._closed = True  # pretend the process vanished
    writer.pool.discard()

    db = Database(path)  # opens -> replays WAL -> state restored
    (res,) = db.execute_sql("SELECT id, v FROM t ORDER BY id")
    assert res.rows == [[1, 11], [2, 22]]
    db.close()


def test_kill9_preserves_committed_statements(tmp_path):
    """The kill -9 demo, as a test: a child process commits three
    statements, then is killed hard. Every committed statement must
    survive; nothing else may appear."""
    path = tmp_path / "crash.pico"
    child_code = (
        "import time\n"
        "from picosql.engine import Database\n"
        f"db = Database(r'{path}')\n"
        "db.execute_sql('CREATE TABLE t (id INT PRIMARY KEY, v INT);')\n"
        "print('C1', flush=True)\n"
        "db.execute_sql('INSERT INTO t VALUES (1, 10);')\n"
        "print('C2', flush=True)\n"
        "db.execute_sql('INSERT INTO t VALUES (2, 20);')\n"
        "db.execute_sql('INSERT INTO t VALUES (3, 30);')\n"
        "print('C3', flush=True)\n"
        "time.sleep(30)\n"  # never reaches close(): the kill is the crash
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        marks = [proc.stdout.readline().strip() for _ in range(3)]
    finally:
        proc.kill()  # SIGKILL on POSIX / TerminateProcess on Windows
        proc.wait(timeout=10)
    assert marks == ["C1", "C2", "C3"]

    db = Database(path)  # reopen -> recovery replays the WAL
    (res,) = db.execute_sql("SELECT id, v FROM t ORDER BY id")
    assert res.rows == [[1, 10], [2, 20], [3, 30]]
    db.close()


# ------------------------------------------------------- poisoned instances


def test_uncommitted_statement_vanishes_after_mid_apply_failure(
    tmp_path, monkeypatch
):
    path = tmp_path / "poison.pico"
    db = Database(path)
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT);")
    db.execute_sql("INSERT INTO t VALUES (1, 10);")  # committed and durable

    calls = {"n": 0}
    original = db.tables["t"].store.insert

    def flaky_insert(row):
        calls["n"] += 1
        if calls["n"] == 2:
            raise PageError("simulated storage failure mid-statement")
        return original(row)

    monkeypatch.setattr(db.tables["t"].store, "insert", flaky_insert)
    with pytest.raises(EngineError, match="simulated storage failure"):
        db.execute_sql("INSERT INTO t VALUES (2, 20), (3, 30);")
    assert db.tables["t"].store.insert is not original  # monkeypatch active
    db.close()  # poisoned: discards uncommitted dirty pages

    db2 = Database(path)  # recovery restores the last COMMITTED state
    (res,) = db2.execute_sql("SELECT id FROM t")
    assert [r[0] for r in res.rows] == [1]
    db2.close()


def test_execute_after_poison_is_refused(tmp_path):
    db = Database(tmp_path / "refused.pico")
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    db._poisoned = True
    with pytest.raises(EngineError, match="failed state"):
        db.execute_sql("SELECT * FROM t")
    db.close()


def test_wal_commit_failure_poisons_instance(tmp_path, monkeypatch):
    path = tmp_path / "walfail.pico"
    db = Database(path)
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    monkeypatch.setattr(
        db._wal,
        "append_commit",
        lambda pages: (_ for _ in ()).throw(WalError("disk full")),
    )
    with pytest.raises(EngineError, match="commit failed"):
        db.execute_sql("INSERT INTO t VALUES (1);")
    db.close()
    db2 = Database(path)
    (res,) = db2.execute_sql("SELECT id FROM t")
    assert res.rows == []  # the failed commit is not durable
    db2.close()


def test_memory_mode_has_no_wal():
    db = Database()  # in-memory: ephemeral by design
    assert db._wal is None
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    db.execute_sql("INSERT INTO t VALUES (1);")
    db.close()
