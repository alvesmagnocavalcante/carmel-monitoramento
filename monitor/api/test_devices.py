"""Testes dos dispositivos monitorados por ping (devices.py e as rotas /api/devices e /api/probe).

Rodar: python -m unittest test_devices   (dentro de monitor/api). Não usam rede nem o Turso.
"""

import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from db import Database
from devices import FAIL_THRESHOLD, PERSIST_EVERY, UNKNOWN_AFTER, Devices
from models import DeviceIn, DeviceUpdate

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)

# Heartbeat mínimo só para cadastrar o servidor (a sonda) no banco.
PROBE_HEARTBEAT = {
    "agent_id": "probe1", "group": "Taiba", "timestamp": None, "cpu_percent": 1.0, "memory_percent": 1.0,
    "disk_percent": 1.0, "temp_c": None, "net_bytes_sent": 1, "net_bytes_recv": 1, "uptime_s": 1,
    "hostname": "INTERFACES", "os": "windows", "interval_seconds": 60,
}


def new_device(**changes):
    data = {
        "name": "Impressora recepção", "kind": "impressora", "address": "10.0.0.50", "group": "Taiba",
        "probe_id": "probe1", "method": "ping", "port": None,
    }
    return {**data, **changes}


def ok(device_id, latency=3.5):
    return {"device_id": device_id, "ok": True, "latency_ms": latency}


def fail(device_id):
    return {"device_id": device_id, "ok": False, "latency_ms": None}


class DevicesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.statements: list[str] = []
        path = str(Path(self.directory.name) / "devices.db")

        def connect() -> sqlite3.Connection:
            connection = sqlite3.connect(path, timeout=10)
            connection.set_trace_callback(
                lambda sql: self.statements.append(sql.strip())
                if sql.lstrip().upper().startswith(("SELECT", "INSERT", "UPDATE", "DELETE", "WITH"))
                else None
            )
            return connection

        self.database = Database(path, connection_factory=connect)
        self.database.initialize()
        self.database.record_heartbeat(PROBE_HEARTBEAT)
        self.devices = Devices(self.database)
        self.devices.initialize()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_create_list_update_delete(self) -> None:
        created = self.devices.create(new_device(), NOW)
        self.assertEqual(created["status"], "unknown")
        self.assertEqual(created["probe_name"], "INTERFACES", "mostra o nome do servidor que faz o ping")
        self.assertEqual([d["id"] for d in self.devices.list_all(NOW)], [created["id"]])

        renamed = self.devices.update(created["id"], {"name": "Impressora RH"}, NOW)
        self.assertEqual(renamed["name"], "Impressora RH")
        self.assertIsNone(self.devices.update("dev_inexistente", {"name": "x"}, NOW))

        self.assertTrue(self.devices.delete(created["id"]))
        self.assertFalse(self.devices.delete(created["id"]))
        self.assertEqual(self.devices.list_all(NOW), [])

    def test_goes_down_only_after_consecutive_failures(self) -> None:
        device = self.devices.create(new_device(), NOW)
        device_id = device["id"]
        self.devices.record_results("probe1", [ok(device_id)], NOW)
        self.assertEqual(self.devices.get(device_id, NOW)["status"], "up")

        for attempt in range(1, FAIL_THRESHOLD):
            self.devices.record_results("probe1", [fail(device_id)], NOW + timedelta(minutes=attempt))
            self.assertEqual(self.devices.get(device_id, NOW)["status"], "up", f"falha {attempt}: ainda no ar")

        down_at = NOW + timedelta(minutes=FAIL_THRESHOLD)
        self.devices.record_results("probe1", [fail(device_id)], down_at)
        view = self.devices.get(device_id, down_at)
        self.assertEqual(view["status"], "down")
        self.assertEqual(view["status_since"], down_at, "mostra desde quando está fora do ar")

        # Uma resposta basta para voltar ao ar.
        back = down_at + timedelta(minutes=1)
        self.devices.record_results("probe1", [ok(device_id, 8.0)], back)
        view = self.devices.get(device_id, back)
        self.assertEqual((view["status"], view["latency_ms"], view["status_since"]), ("up", 8.0, back))

    def test_stale_reading_is_unknown_not_down(self) -> None:
        device_id = self.devices.create(new_device(), NOW)["id"]
        self.devices.record_results("probe1", [ok(device_id)], NOW)
        later = NOW + UNKNOWN_AFTER + timedelta(minutes=1)
        self.assertEqual(self.devices.get(device_id, later)["status"], "unknown")

    def test_ignores_results_from_other_probe_or_unknown_device(self) -> None:
        device_id = self.devices.create(new_device(), NOW)["id"]
        self.assertEqual(self.devices.record_results("outra-sonda", [ok(device_id)], NOW), 0)
        self.assertEqual(self.devices.record_results("probe1", [ok("dev_inexistente")], NOW), 0)
        self.assertEqual(self.devices.get(device_id, NOW)["status"], "unknown")

    def test_targets_are_only_the_probes_own_devices(self) -> None:
        mine = self.devices.create(new_device(), NOW)["id"]
        self.database.record_heartbeat(dict(PROBE_HEARTBEAT, agent_id="probe2", hostname="OUTRO"))
        self.devices.create(new_device(name="Antena", probe_id="probe2", address="10.0.1.9"), NOW)
        targets = self.devices.targets("probe1", NOW)
        self.assertEqual([t["id"] for t in targets], [mine])
        self.assertEqual(targets[0], {"id": mine, "address": "10.0.0.50", "method": "ping", "port": None})

    def test_batch_is_written_in_one_statement_and_only_when_needed(self) -> None:
        ids = [self.devices.create(new_device(name=f"Disp {n}", address=f"10.0.0.{n}"), NOW)["id"] for n in range(5)]

        self.statements.clear()
        self.devices.record_results("probe1", [ok(i) for i in ids], NOW)
        writes = [s for s in self.statements if s.upper().startswith(("WITH", "UPDATE"))]
        self.assertEqual(len(writes), 1, "os 5 dispositivos vão em 1 comando só")

        # Nada mudou e ainda não é hora de gravar a leitura: não escreve no banco.
        self.statements.clear()
        self.devices.record_results("probe1", [ok(i) for i in ids], NOW + timedelta(minutes=1))
        self.assertEqual(self.statements, [])

        # Passado o intervalo, grava a leitura (tempo de resposta e hora) de novo, também em 1 comando.
        self.statements.clear()
        self.devices.record_results("probe1", [ok(i) for i in ids], NOW + PERSIST_EVERY + timedelta(seconds=1))
        self.assertEqual(len([s for s in self.statements if s.upper().startswith("WITH")]), 1)

    def test_down_status_survives_a_restart(self) -> None:
        device_id = self.devices.create(new_device(), NOW)["id"]
        for minute in range(FAIL_THRESHOLD):
            self.devices.record_results("probe1", [fail(device_id)], NOW + timedelta(minutes=minute))

        restarted = Devices(self.database)
        restarted.initialize()
        later = NOW + timedelta(minutes=FAIL_THRESHOLD)
        restarted.record_results("probe1", [fail(device_id)], later)
        self.assertEqual(restarted.get(device_id, later)["status"], "down", "não precisa contar 3 falhas de novo")

    def test_changing_the_ping_target_resets_the_status(self) -> None:
        device_id = self.devices.create(new_device(), NOW)["id"]
        self.devices.record_results("probe1", [ok(device_id)], NOW)
        self.devices.update(device_id, {"name": "Só o nome"}, NOW)
        self.assertEqual(self.devices.get(device_id, NOW)["status"], "up", "renomear não perde o estado")
        self.devices.update(device_id, {"address": "10.0.0.99"}, NOW)
        self.assertEqual(self.devices.get(device_id, NOW)["status"], "unknown", "endereço novo = sem leitura")

    def test_data_validation(self) -> None:
        with self.assertRaises(ValidationError):
            DeviceIn(**new_device(address="10.0.0.1; rm -rf"))
        with self.assertRaises(ValidationError):
            DeviceIn(**new_device(method="tcp"))  # tcp exige a porta
        self.assertEqual(DeviceIn(**new_device(method="tcp", port=9100)).port, 9100)
        with self.assertRaises(ValidationError):
            DeviceUpdate()  # alteração vazia
        self.assertEqual(DeviceUpdate(name="x").model_dump(exclude_unset=True), {"name": "x"})


class DeviceRoutesTests(unittest.TestCase):
    """Ponta a ponta pelas rotas HTTP: cadastro, lista da sonda, resultados e status."""

    def test_full_flow(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            environment = {
                "DATABASE_PATH": str(Path(directory) / "api.db"),
                "AGENT_TOKEN": "token-de-teste",
                "TURSO_DATABASE_URL": "",
                "TURSO_AUTH_TOKEN": "",
            }
            with patch.dict(os.environ, environment):
                sys.modules.pop("main", None)
                main = importlib.import_module("main")
                agent = {"Authorization": "Bearer token-de-teste"}
                manager = {"X-Management-Key": "token-de-teste"}
                with TestClient(main.app) as client:
                    main.database.record_heartbeat(PROBE_HEARTBEAT)
                    body = new_device()

                    self.assertEqual(client.post("/api/devices", json=body).status_code, 401, "exige a chave de gestão")
                    self.assertEqual(
                        client.post("/api/devices", json=dict(body, probe_id="nao-existe"), headers=manager).status_code,
                        422, "a sonda precisa ser um agente cadastrado",
                    )
                    created = client.post("/api/devices", json=body, headers=manager)
                    self.assertEqual(created.status_code, 201)
                    device_id = created.json()["id"]

                    self.assertEqual(client.get("/api/probe/targets", params={"agent_id": "probe1"}).status_code, 401)
                    targets = client.get("/api/probe/targets", params={"agent_id": "probe1"}, headers=agent).json()
                    self.assertEqual([t["id"] for t in targets], [device_id])

                    for _ in range(FAIL_THRESHOLD):
                        posted = client.post(
                            "/api/probe/results", headers=agent,
                            json={"probe_id": "probe1", "results": [{"device_id": device_id, "ok": False}]},
                        )
                        self.assertEqual(posted.json(), {"accepted": 1})
                    listed = client.get("/api/devices").json()
                    self.assertEqual((listed[0]["status"], listed[0]["probe_name"]), ("down", "INTERFACES"))

                    patched = client.patch(f"/api/devices/{device_id}", json={"name": "Nova"}, headers=manager)
                    self.assertEqual(patched.json()["name"], "Nova")
                    self.assertEqual(client.patch("/api/devices/dev_x", json={"name": "x"}, headers=manager).status_code, 404)
                    self.assertEqual(client.delete(f"/api/devices/{device_id}", headers=manager).status_code, 204)
                    self.assertEqual(client.get("/api/devices").json(), [])
            sys.modules.pop("main", None)


if __name__ == "__main__":
    unittest.main()
