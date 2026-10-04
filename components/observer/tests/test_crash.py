"""Actual SIGKILL tests around SQLite commit and atomic init publication."""

import contextlib
import multiprocessing
import os
from pathlib import Path
import signal
import tempfile
import unittest

from test_observer import FakeClient, envelope, reply
from technocore_observer.observer import Observer
from technocore_observer.protocol import ObserverError
from technocore_observer.storage import StateLock, Store, initialize

ROOT = Path(__file__).resolve().parents[1]
CTX = multiprocessing.get_context("fork")


def crash_worker(directory, point, init=False):
    def checkpoint(observed):
        if point == observed:
            os.kill(os.getpid(), signal.SIGKILL)
    with StateLock(directory):
        if init:
            initialize(directory, "test-room", envelope([100]), reply(envelope([100])), checkpoint)
        else:
            with contextlib.closing(Store(directory, "test-room")) as store:
                Observer(store, FakeClient(reply(envelope([200, 201]))), checkpoint).poll_once()


def lock_worker(directory, connection):
    try:
        with StateLock(directory):
            connection.send("locked")
            connection.recv()
    except BlockingIOError:
        connection.send("busy")
    finally:
        connection.close()


class CrashTests(unittest.TestCase):
    def kill_at(self, directory, point, init=False):
        process = CTX.Process(target=crash_worker, args=(directory, point, init))
        process.start()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join()
            self.fail("crash worker failed to stop")
        self.assertEqual(process.exitcode, -signal.SIGKILL, point)

    def test_runtime_kills(self):
        points = ("http_received", "message_inserted", "gap_inserted",
                  "before_cursor_update", "before_commit", "after_commit")
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory(dir=ROOT) as root:
                directory = Path(root)
                with StateLock(directory):
                    initialize(directory, "test-room", envelope([100]), reply(envelope([100])))
                self.kill_at(directory, point)
                with StateLock(directory), contextlib.closing(Store(directory, "test-room")) as store:
                    committed = point == "after_commit"
                    self.assertEqual(store.state()["poll_seq"], 201 if committed else 100)
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2 if committed else 0)
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM gaps").fetchone()[0], 1 if committed else 0)
                    observer = Observer(store, FakeClient(reply(envelope([] if committed else [200, 201]))))
                    observer.poll_once()
                    self.assertEqual(store.state()["poll_seq"], 201)
                    self.assertEqual(store.state()["resolved_seq"], 100)
                    self.assertEqual(store.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 2)

    def test_init_kills(self):
        points = ("init_temp_created", "init_schema", "init_state_inserted",
                  "init_committed", "init_before_rename", "init_after_rename")
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory(dir=ROOT) as root:
                directory = Path(root)
                self.kill_at(directory, point, init=True)
                if point == "init_after_rename":
                    with StateLock(directory), contextlib.closing(Store(directory, "test-room")) as store:
                        self.assertEqual(store.state()["poll_seq"], 100)
                else:
                    self.assertFalse((directory / "state.sqlite").exists())
                    self.assertTrue((directory / "state.sqlite.init").exists())
                    with self.assertRaises(ObserverError):
                        Store(directory, "test-room")
                with StateLock(directory), self.assertRaisesRegex(ObserverError, "INIT_PATH_ALREADY_EXISTS"):
                    initialize(directory, "test-room", envelope([200]), reply(envelope([200])))

    def test_single_instance_processes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as root:
            parent1, child1 = CTX.Pipe()
            parent2, child2 = CTX.Pipe()
            first = CTX.Process(target=lock_worker, args=(root, child1))
            second = CTX.Process(target=lock_worker, args=(root, child2))
            try:
                first.start()
                self.assertTrue(parent1.poll(5))
                self.assertEqual(parent1.recv(), "locked")
                second.start()
                self.assertTrue(parent2.poll(5))
                self.assertEqual(parent2.recv(), "busy")
                second.join(5)
                self.assertEqual(second.exitcode, 0)
                parent1.send("release")
                first.join(5)
                self.assertEqual(first.exitcode, 0)
            finally:
                for process in (first, second):
                    if process.is_alive():
                        process.kill()
                        process.join()
                for connection in (parent1, parent2, child1, child2):
                    connection.close()


if __name__ == "__main__":
    unittest.main()
