"""Testes contra um PostgreSQL de verdade (heartbeat, listagem, gráficos e dispositivos).

Só rodam quando a variável TEST_DATABASE_URL aponta para um banco DE TESTE, por exemplo:

    docker run -d --name pg-test -e POSTGRES_PASSWORD=teste -e POSTGRES_DB=monitor_test -p 55432:5432 postgres:16-alpine
    set TEST_DATABASE_URL=postgresql://postgres:teste@localhost:55432/monitor_test
    python -m unittest test_postgres

ATENÇÃO: cada teste APAGA as tabelas agents, metrics e devices desse banco. Sem a variável, os testes são
ignorados (é o que acontece na rotina normal e no deploy).
"""

import os
import unittest
from datetime import datetime, timedelta, timezone

from db import Database, to_db_timestamp
from devices import FAIL_THRESHOLD, Devices

TEST_URL = os.getenv("TEST_DATABASE_URL", "").strip()

# Heartbeat completo, com os campos que um agente real envia (inclui contadores de rede acima de 2 bilhões,
# que não cabem em um INTEGER do PostgreSQL).
HEARTBEAT = {
    "agent_id": "pg-agent-1", "group": "Magna", "timestamp": datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc),
    "cpu_percent": 12.5, "memory_percent": 40.25, "disk_percent": 61.0, "temp_c": None,
    "net_bytes_sent": 5_000_000_000, "net_bytes_recv": 9_000_000_000, "uptime_s": 123456,
    "hostname": "SRV-TESTE", "os": "windows", "interval_seconds": 60,
    "system": {"os": "Windows 11", "cpu": "i5"}, "volumes": [{"mount": "C:", "percent": 61.0}],
}


@unittest.skipUnless(TEST_URL, "defina TEST_DATABASE_URL para rodar os testes de PostgreSQL")
class PostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Database("-", database_url=TEST_URL)
        with self.database.connect() as connection:
            connection.execute("DROP TABLE IF EXISTS metrics, devices, agents CASCADE")
        self.database.initialize()

    def test_backend_is_postgres(self) -> None:
        self.assertEqual(self.database.backend, "postgres")

    def test_heartbeat_list_and_dedup(self) -> None:
        self.database.record_heartbeat(HEARTBEAT)
        self.database.record_heartbeat(HEARTBEAT)  # reenvio idêntico: não duplica
        with self.database.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        self.assertEqual(count, 1, "o reenvio do mesmo heartbeat não pode gravar duas medições")

        agents = self.database.list_agents()
        self.assertEqual(len(agents), 1)
        agent = agents[0]
        self.assertEqual((agent["name"], agent["group"], agent["status"]), ("SRV-TESTE", "Magna", "online"))
        self.assertEqual(agent["latest_metric"]["net_bytes_recv"], 9_000_000_000)
        self.assertIsNone(agent["latest_metric"]["temp_c"])
        self.assertEqual(agent["system"], {"cpu": "i5", "os": "Windows 11"})
        self.assertEqual(agent["volumes"], [{"mount": "C:", "percent": 61.0}])

    def test_agents_sorted_ignoring_case_and_update_delete(self) -> None:
        for agent_id, hostname in (("a1", "zeta"), ("a2", "Alfa"), ("a3", "beta")):
            self.database.record_heartbeat(dict(HEARTBEAT, agent_id=agent_id, hostname=hostname, timestamp=None))
        self.assertEqual([a["name"] for a in self.database.list_agents()], ["Alfa", "beta", "zeta"])
        self.assertTrue(self.database.update_agent("a1", name="Novo nome", group="Taiba"))
        self.assertEqual(self.database.list_agents("a1")[0]["group"], "Taiba")
        self.assertTrue(self.database.delete_agent("a1"))
        self.assertFalse(self.database.delete_agent("a1"))
        self.assertIsNone(self.database.get_metrics("a1", 60))

    def test_metrics_history_is_bucketed(self) -> None:
        self.database.record_heartbeat(dict(HEARTBEAT, timestamp=None))
        # Insere medições antigas direto no banco: 120 pontos, um por minuto, na última hora e meia.
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            for minute in range(1, 121):
                connection.execute(
                    "INSERT INTO metrics (agent_id, received_at, agent_timestamp, cpu_percent, memory_percent, "
                    "disk_percent, temp_c, net_bytes_sent, net_bytes_recv, uptime_s, volumes) "
                    "VALUES (?, ?, NULL, ?, 50, 50, NULL, 1, 1, ?, NULL)",
                    ("pg-agent-1", to_db_timestamp(now - timedelta(minutes=minute)), float(minute), minute),
                )
        short = self.database.get_metrics("pg-agent-1", 150)
        self.assertGreater(len(short), 50)
        self.assertLessEqual(len(short), 300, "nunca passa do limite de pontos do gráfico")
        received = [point["received_at"] for point in short]
        self.assertEqual(received, sorted(received), "pontos em ordem de tempo")
        # Período de 7 dias: 121 medições são agrupadas em baldes de 34 min; menos pontos que medições.
        long = self.database.get_metrics("pg-agent-1", 10_080)
        self.assertLess(len(long), 121)
        self.assertIsNotNone(self.database.get_metrics("pg-agent-1", 60))

    def test_purge_old_metrics(self) -> None:
        self.database.record_heartbeat(dict(HEARTBEAT, timestamp=None))
        old = to_db_timestamp(datetime.now(timezone.utc) - timedelta(days=30))
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO metrics (agent_id, received_at, cpu_percent, memory_percent, disk_percent, "
                "net_bytes_sent, net_bytes_recv, uptime_s) VALUES (?, ?, 1, 1, 1, 1, 1, 1)",
                ("pg-agent-1", old),
            )
        self.database.purge_old_metrics(14)
        with self.database.connect() as connection:
            remaining = connection.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        self.assertEqual(remaining, 1, "só a medição recente fica")

    def test_devices_flow(self) -> None:
        self.database.record_heartbeat(dict(HEARTBEAT, timestamp=None))
        devices = Devices(self.database)
        devices.initialize()
        now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
        created = devices.create(
            {"name": "Câmera Piscina", "kind": "camera", "address": "10.0.0.9", "group": "Magna",
             "probe_id": "pg-agent-1", "method": "ping", "port": None},
            now,
        )
        self.assertEqual(created["probe_name"], "SRV-TESTE")
        # Lote só com falhas: latency_ms é NULL em todas as linhas (caso que exige o CAST no UPDATE ... FROM).
        for minute in range(FAIL_THRESHOLD):
            devices.record_results(
                "pg-agent-1", [{"device_id": created["id"], "ok": False, "latency_ms": None}],
                now + timedelta(minutes=minute),
            )
        down_at = now + timedelta(minutes=FAIL_THRESHOLD)
        devices.record_results("pg-agent-1", [{"device_id": created["id"], "ok": False, "latency_ms": None}], down_at)
        self.assertEqual(devices.get(created["id"], down_at)["status"], "down")
        # Depois de reiniciar a API, o estado "down" gravado no PostgreSQL continua.
        restarted = Devices(self.database)
        restarted.initialize()
        self.assertEqual(restarted.get(created["id"], down_at)["status"], "down")
        # Volta ao ar com latência decimal (DOUBLE PRECISION).
        back = down_at + timedelta(minutes=1)
        restarted.record_results("pg-agent-1", [{"device_id": created["id"], "ok": True, "latency_ms": 3.25}], back)
        reloaded = Devices(self.database)
        reloaded.initialize()
        self.assertEqual(reloaded.get(created["id"], back)["latency_ms"], 3.25)
        self.assertTrue(reloaded.delete(created["id"]))


if __name__ == "__main__":
    unittest.main()
