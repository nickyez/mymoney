#!/usr/bin/env python3
"""Exercise MyMoney's Docker volume backup archive format in a temp directory."""
import json
import pathlib
import sqlite3
import tarfile
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    source = root / "source"
    restored = root / "restored"
    source.mkdir()
    restored.mkdir()

    db_path = source / "transactions.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE transactions (id INTEGER PRIMARY KEY, description TEXT NOT NULL)")
    db.execute("INSERT INTO transactions(description) VALUES (?)", ("backup fixture",))
    db.commit()
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    db.close()
    config = {"gmail_user": "fixture@example.test", "senders": []}
    (source / "config.json").write_text(json.dumps(config))

    archive = root / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(db_path, arcname="transactions.db")
        tar.add(source / "config.json", arcname="config.json")

    with tarfile.open(archive, "r:gz") as tar:
        members = {member.name: member for member in tar.getmembers() if member.isfile()}
        assert set(members) == {"transactions.db", "config.json"}
        for name, member in members.items():
            extracted = tar.extractfile(member)
            assert extracted is not None
            with extracted:
                (restored / name).write_bytes(extracted.read())

    restored_config = json.loads((restored / "config.json").read_text())
    assert restored_config == config
    restored_db = sqlite3.connect(restored / "transactions.db")
    assert restored_db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored_db.execute("SELECT description FROM transactions").fetchone()[0] == "backup fixture"
    restored_db.close()

print("Disposable backup archive/restore round-trip passed; SQLite data and config verified.")
