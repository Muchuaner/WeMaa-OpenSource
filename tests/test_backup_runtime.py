import importlib.util
from pathlib import Path
import sqlite3
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("backup_runtime", Path(__file__).parents[1] / "scripts/backup_runtime.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_online_snapshot_includes_uncheckpointed_wal(tmp_path):
    root = tmp_path / "core"
    (root / "config").mkdir(parents=True)
    (root / "data").mkdir()
    with sqlite3.connect(root / "data/person.db") as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE person (name TEXT)")
        live.execute("INSERT INTO person VALUES ('verified')")
        live.commit()
        archive = tmp_path / "backup.tar.gz"
        manifest = backup.snapshot_core(root, archive)
        with tarfile.open(archive) as snapshot:
            assert not any(name.endswith(("-wal", "-shm")) for name in snapshot.getnames())
            snapshot.extractall(tmp_path / "restored", filter="data")
        with sqlite3.connect(tmp_path / "restored/data/person.db") as restored:
            assert restored.execute("SELECT name FROM person").fetchall() == [("verified",)]
        assert manifest["sqlite_databases"] == ["data/person.db"]
        assert archive.stat().st_mode & 0o777 == 0o600


def test_backup_does_not_overwrite_recovery_point(tmp_path):
    archive = tmp_path / "backup.tar.gz"
    archive.write_bytes(b"existing recovery point")
    with pytest.raises(FileExistsError):
        backup.snapshot_core(tmp_path, archive)
    assert archive.read_bytes() == b"existing recovery point"


def test_missing_runtime_does_not_publish_backup(tmp_path):
    archive = tmp_path / "backup.tar.gz"
    with pytest.raises(RuntimeError, match="directories are missing"):
        backup.snapshot_core(tmp_path, archive)
    assert not archive.exists()
