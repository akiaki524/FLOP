"""Build-time compatibility probe on a disposable old-SQLite database."""

import sqlite3
import sys
from contextlib import closing


def make(path):
    assert sqlite3.sqlite_version_info < (3, 51, 3)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        conn.execute("create table records (kind text primary key, value text not null)")
        conn.executemany("insert into records values (?, ?)",
                         [("message", "m1"), ("gap", "g1"), ("cursor", "42"),
                          ("epoch", "e1"), ("archive_provenance", "a1")])
        conn.commit()


def check(path):
    assert sqlite3.sqlite_version == "3.53.4"
    with closing(sqlite3.connect(path)) as capture, closing(sqlite3.connect(path)) as archive:
        expected = [("archive_provenance", "a1"), ("cursor", "42"), ("epoch", "e1"),
                    ("gap", "g1"), ("message", "m1")]
        assert capture.execute("select kind, value from records order by kind").fetchall() == expected
        assert capture.execute("pragma journal_mode").fetchone()[0] == "wal"
        capture.execute("insert into records values ('message2', 'm2')")
        capture.commit()
        assert archive.execute("select value from records where kind='message2'").fetchone()[0] == "m2"
        archive.execute("insert into records values ('archive2', 'a2')")
        archive.commit()
        assert capture.execute("pragma wal_checkpoint(passive)").fetchone()[0] == 0
        assert capture.execute("pragma integrity_check").fetchone()[0] == "ok"
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("select count(*) from records").fetchone()[0] == 7


if __name__ == "__main__":
    {"make": make, "check": check}[sys.argv[1]](sys.argv[2])
