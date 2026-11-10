import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import libsql

from db import Database


PAYLOAD = {
    "agent_id": "agt_test",
    "group": "Hotel Carmel",
    "timestamp": datetime.now(timezone.utc),
    "cpu_percent": 20.5,
    "memory_percent": 50.0,
    "disk_percent": 60.0,
    "temp_c": None,
    "net_bytes_sent": 100,
    "net_bytes_recv": 200,
    "uptime_s": 300,
    "hostname": "RECEPCAO-01",
    "os": "windows",
    "interval_seconds": 10,
}


class DatabaseContractTests(unittest.TestCase):
    def exercise_database(self, database: Database) -> None:
        database.initialize()
        database.record_heartbeat(PAYLOAD)
        database.record_heartbeat(PAYLOAD)

        agents = database.list_agents()
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["group"], "Hotel Carmel")
        self.assertEqual(agents[0]["hostname"], "RECEPCAO-01")
        self.assertEqual(agents[0]["latest_metric"]["cpu_percent"], 20.5)

        metrics = database.get_metrics("agt_test", 60)
        self.assertIsNotNone(metrics)
        self.assertEqual(len(metrics), 1, "retries must not duplicate metrics")
        self.assertEqual(metrics[0]["net_bytes_recv"], 200)
        self.assertIsNone(database.get_metrics("missing", 60))

        self.assertTrue(
            database.update_agent(
                "agt_test", name="Recepção", group="Hotel Editado"
            )
        )
        database.record_heartbeat(PAYLOAD)
        edited = database.list_agents()[0]
        self.assertEqual(edited["name"], "Recepção")
        self.assertEqual(edited["group"], "Hotel Editado")

        self.assertTrue(database.delete_agent("agt_test"))
        self.assertEqual(database.list_agents(), [])
        self.assertIsNone(database.get_metrics("agt_test", 60))

    def test_sqlite_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.exercise_database(Database(Path(directory) / "monitor.db"))

    def test_libsql_compatible_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "libsql.db")
            database = Database(
                "unused.db",
                turso_url="libsql://test.invalid",
                turso_auth_token="test-token",
                connection_factory=lambda: libsql.connect(path, autocommit=True),
            )
            self.exercise_database(database)

    def test_purge_removes_only_old_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "monitor.db")
            database.initialize()
            database.record_heartbeat(PAYLOAD)
            old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
            with database.connect() as connection:
                connection.execute("UPDATE metrics SET received_at = ?", (old,))
            database.record_heartbeat(dict(PAYLOAD, timestamp=datetime.now(timezone.utc)))

            database.purge_old_metrics(days=14)

            self.assertEqual(len(database.get_metrics("agt_test", 1440)), 1)
            self.assertEqual(database.list_agents()[0]["status"], "online")

    def test_initialize_drops_legacy_token_hash_column(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "monitor.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute("ALTER TABLE agents ADD COLUMN token_hash TEXT")
            database.initialize()
            with database.connect() as connection:
                columns = [r[1] for r in connection.execute("PRAGMA table_info(agents)").fetchall()]
            self.assertNotIn("token_hash", columns)

    def test_network_rate_uses_last_two_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "monitor.db")
            database.initialize()
            now = datetime.now(timezone.utc)
            database.record_heartbeat(
                dict(PAYLOAD, timestamp=now - timedelta(seconds=60), net_bytes_recv=0, net_bytes_sent=0)
            )
            self.assertIsNone(database.list_agents()[0]["net_recv_mbps"], "sem medição anterior")
            before = (now - timedelta(seconds=60)).isoformat()
            with database.connect() as connection:
                connection.execute("UPDATE metrics SET received_at = ?", (before,))
            database.record_heartbeat(
                dict(PAYLOAD, timestamp=now, net_bytes_recv=7_500_000, net_bytes_sent=750_000)
            )
            agent = database.list_agents()[0]
            self.assertAlmostEqual(agent["net_recv_mbps"], 1.0, delta=0.05)
            self.assertAlmostEqual(agent["net_sent_mbps"], 0.1, delta=0.01)

            # contador menor que o anterior (computador reiniciou): sem velocidade
            with database.connect() as connection:
                connection.execute(
                    "UPDATE metrics SET net_bytes_recv = 99999999 WHERE received_at = ?", (before,)
                )
            self.assertIsNone(database.list_agents()[0]["net_recv_mbps"])

    def test_turso_environment_requires_url_and_token(self) -> None:
        environment = {
            "TURSO_DATABASE_URL": "libsql://test.invalid",
            "TURSO_AUTH_TOKEN": "",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                Database.from_environment("monitor.db")


if __name__ == "__main__":
    unittest.main()
