#!/usr/bin/env python3
"""Private live backup of WeMaa state; never restores or replays deliveries."""
from pathlib import Path
from contextlib import closing

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import uuid


def snapshot_core(root: Path, archive: Path) -> dict:
    """Each SQLite file is consistent; this is not a cross-service transaction."""
    if archive.exists():
        raise FileExistsError(archive)
    databases = []
    with tempfile.TemporaryDirectory(prefix="wemaa-snapshot-") as temporary:
        staging = Path(temporary)
        for name in ("config", "data", "depends-data", "plugins"):
            source = root / name
            if not source.is_dir():
                continue
            shutil.copytree(source, staging / name, symlinks=True,
                            ignore=shutil.ignore_patterns("*-wal", "*-shm", "__pycache__"))
            for database in source.rglob("*"):
                if database.is_symlink() or not database.is_file():
                    continue
                with database.open("rb") as stream:
                    is_sqlite = stream.read(16) == b"SQLite format 3\x00"
                if not is_sqlite:
                    continue
                relative = database.relative_to(root)
                copied = staging / relative
                copied.unlink()
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(copied)) as dst:
                        src.backup(dst)
                        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise RuntimeError("SQLite backup integrity check failed")
                databases.append(str(relative))
        if not (staging / "config").is_dir() or not (staging / "data").is_dir():
            raise RuntimeError("Required WeMaa config/data directories are missing")
        manifest = {"format": 1, "completed_at": time.time(), "sqlite_databases": databases,
                    "consistency": "SQLite online backup per database; do not replay delivery queues"}
        (staging / "_wemaa_snapshot.json").write_text(json.dumps(manifest, indent=2))
        temporary_archive = staging / "snapshot.tar.gz"
        with tarfile.open(temporary_archive, "w:gz") as output:
            for child in staging.iterdir():
                if child != temporary_archive:
                    output.add(child, arcname=child.name, recursive=True)
        # Exclusive creation prevents an existing recovery point being overwritten.
        with archive.open("xb") as output, temporary_archive.open("rb") as source:
            os.chmod(archive, 0o600)
            shutil.copyfileobj(source, output)
    return manifest


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def backup_runtime(destination: Path, container: str, environment: Path) -> dict:
    os.umask(0o077)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = ("wemaa.tar.gz", "wemaa-runtime.env", "wemaa-metadata.json")
    if any((destination / name).exists() for name in names):
        raise FileExistsError("Destination already contains a WeMaa backup")
    if not environment.is_file():
        raise RuntimeError("Protected runtime environment is missing")
    inspected = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
    if not inspected["State"]["Running"]:
        raise RuntimeError("WeMaa is not running; inspect its state before backing up")
    remote_archive = "/tmp/wemaa-backup-" + uuid.uuid4().hex + ".tar.gz"
    try:
        subprocess.run(["docker", "exec", "-i", container, "python", "-", "--inside", remote_archive],
                       input=Path(__file__).read_bytes(), check=True, timeout=600)
        with tempfile.TemporaryDirectory(prefix=".wemaa-backup-", dir=destination) as temporary:
            staging = Path(temporary)
            subprocess.run(["docker", "cp", container + ":" + remote_archive,
                            str(staging / names[0])], check=True, stdout=subprocess.DEVNULL)
            shutil.copyfile(environment, staging / names[1])
            metadata = {"format": 1, "completed_at": time.time(), "container": container,
                        "image_id": inspected["Image"], "image": inspected["Config"]["Image"],
                        "sha256": {name: digest(staging / name) for name in names[:2]}}
            (staging / names[2]).write_text(json.dumps(metadata, indent=2))
            for name in names:
                os.chmod(staging / name, 0o600)
                os.link(staging / name, destination / name)
        return {"backed_up": True, "files": list(names), "image_id": inspected["Image"]}
    finally:
        subprocess.run(["docker", "exec", container, "python", "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)",
                        remote_archive], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--container", default="wemaa-core")
    parser.add_argument("--environment", type=Path,
                        default=Path("/home/jinjie/wemaa-private/production.env"))
    parser.add_argument("--inside", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inside:
        snapshot_core(Path("/MaiMBot"), args.inside)
    elif args.destination:
        print(json.dumps(backup_runtime(args.destination, args.container, args.environment)))
    else:
        parser.error("destination is required")
