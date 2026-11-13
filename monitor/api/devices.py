"""Dispositivos monitorados por ping (impressoras, antenas...).

Como funciona:
  - Um dispositivo não tem agente. Um servidor do mesmo local (a "sonda", um agente já cadastrado)
    busca a lista dos dispositivos dele, faz o ping e envia os resultados em lote a cada ciclo.
  - A API decide o status: "down" só depois de FAIL_THRESHOLD falhas seguidas (evita alarme falso
    por uma resposta perdida); uma resposta já volta o dispositivo para "up".
  - Sem resultado recente (a sonda desligou), o status mostrado é "unknown" (sem leitura),
    nunca "down": não sabemos se o equipamento caiu ou se foi a sonda.

Regra de ouro (a mesma do db.py): no Turso cada comando SQL é uma ida à rede, e as idas se
enfileiram. Por isso o estado fica em memória: o painel e a sonda leem da memória, e o banco só é
escrito quando o status muda ou de tempos em tempos, e sempre em UM comando por lote.
"""

import secrets
import threading
from datetime import datetime, timedelta
from typing import Any

from db import Database, rows_as_dicts, to_db_timestamp, from_db_timestamp, utc_now

# Falhas seguidas para considerar o dispositivo fora do ar.
FAIL_THRESHOLD = 3
# Sem resultado há mais que isto => "sem leitura" (a sonda envia a cada ~1 minuto).
UNKNOWN_AFTER = timedelta(minutes=15)
# Mesmo sem mudança de status, grava a leitura (tempo de resposta e hora) no banco neste intervalo.
PERSIST_EVERY = timedelta(minutes=5)
# De quanto em quanto tempo a lista é relida do banco (pega mudanças feitas por outra instância
# da API e renomeações de servidores).
RELOAD_EVERY = timedelta(seconds=60)

DEVICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    address TEXT NOT NULL,
    group_name TEXT NOT NULL,
    probe_id TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'ping',
    port INTEGER,
    status TEXT NOT NULL DEFAULT 'unknown',
    latency_ms REAL,
    last_checked_at TEXT,
    status_since TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_probe ON devices(probe_id);
"""

# Campos que, se mudarem, fazem o dispositivo voltar a "sem leitura" (o alvo do ping é outro).
_TARGET_FIELDS = {"address", "method", "port", "probe_id"}


class Devices:
    """Cadastro e estado dos dispositivos. Usa o banco do Database, mas guarda o estado em memória."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = threading.Lock()
        # id -> dispositivo (campos do banco + probe_name) com o estado atual.
        self._devices: dict[str, dict[str, Any]] = {}
        # id -> falhas seguidas. Só em memória: depois de reiniciar, um dispositivo "down" continua "down".
        self._failures: dict[str, int] = {}
        # id -> quando o estado foi gravado no banco pela última vez.
        self._persisted_at: dict[str, datetime] = {}
        self._loaded_at: datetime | None = None

    # ---------- Inicialização e leitura ----------

    def initialize(self) -> None:
        """Cria a tabela (se não existir) e carrega os dispositivos. Chamado quando a API sobe."""
        with self.database.connect() as connection:
            connection.executescript(DEVICES_SCHEMA)
        self._reload()

    def _reload(self, now: datetime | None = None) -> None:
        """Relê os dispositivos do banco (1 consulta), preservando o estado mais novo que está em memória."""
        now = now or utc_now()
        with self.database.connect() as connection:
            rows = rows_as_dicts(
                connection.execute(
                    """
                    SELECT d.id, d.name, d.kind, d.address, d.group_name, d.probe_id,
                           a.name AS probe_name, d.method, d.port, d.status, d.latency_ms,
                           d.last_checked_at, d.status_since, d.created_at
                    FROM devices d
                    LEFT JOIN agents a ON a.id = d.probe_id
                    """
                )
            )
        with self._lock:
            fresh: dict[str, dict[str, Any]] = {}
            for row in rows:
                current = self._devices.get(row["id"])
                # Se a memória tem uma leitura mais nova que a do banco, ela vence.
                if current and (current["last_checked_at"] or "") > (row["last_checked_at"] or ""):
                    for field in ("status", "latency_ms", "last_checked_at", "status_since"):
                        row[field] = current[field]
                fresh[row["id"]] = row
                # Dispositivo que já estava "down" no banco continua contando como "down".
                self._failures.setdefault(row["id"], FAIL_THRESHOLD if row["status"] == "down" else 0)
            self._devices = fresh
            self._failures = {key: value for key, value in self._failures.items() if key in fresh}
            self._loaded_at = now

    def _ensure_fresh(self, now: datetime) -> None:
        if self._loaded_at is None or now - self._loaded_at >= RELOAD_EVERY:
            self._reload(now)

    @staticmethod
    def _view(device: dict[str, Any], now: datetime) -> dict[str, Any]:
        """Dispositivo no formato da API, com o status "unknown" quando a leitura é velha demais."""
        checked = from_db_timestamp(device["last_checked_at"])
        stale = checked is None or now - checked > UNKNOWN_AFTER
        return {
            "id": device["id"],
            "name": device["name"],
            "kind": device["kind"],
            "address": device["address"],
            "group": device["group_name"],
            "probe_id": device["probe_id"],
            "probe_name": device["probe_name"],
            "method": device["method"],
            "port": device["port"],
            "status": "unknown" if stale else device["status"],
            "latency_ms": device["latency_ms"],
            "status_since": from_db_timestamp(device["status_since"]),
            "last_checked_at": checked,
        }

    def list_all(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Todos os dispositivos (para o painel), por grupo e nome."""
        now = now or utc_now()
        self._ensure_fresh(now)
        with self._lock:
            devices = [self._view(device, now) for device in self._devices.values()]
        return sorted(devices, key=lambda device: (device["group"].lower(), device["name"].lower()))

    def get(self, device_id: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or utc_now()
        with self._lock:
            device = self._devices.get(device_id)
            return self._view(device, now) if device else None

    def targets(self, probe_id: str, now: datetime | None = None) -> list[dict[str, Any]]:
        """O que a sonda deve pingar: os dispositivos dela (lido da memória, sem ir ao banco)."""
        now = now or utc_now()
        self._ensure_fresh(now)
        with self._lock:
            return [
                {"id": d["id"], "address": d["address"], "method": d["method"], "port": d["port"]}
                for d in self._devices.values()
                if d["probe_id"] == probe_id
            ]

    # ---------- Cadastro ----------

    def create(self, data: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        device = {
            "id": "dev_" + secrets.token_hex(6),
            "name": data["name"],
            "kind": data["kind"],
            "address": data["address"],
            "group_name": data["group"],
            "probe_id": data["probe_id"],
            "method": data["method"],
            "port": data["port"],
            "status": "unknown",
            "latency_ms": None,
            "last_checked_at": None,
            "status_since": None,
            "created_at": to_db_timestamp(now),
        }
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO devices (id, name, kind, address, group_name, probe_id, method, port,
                                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?)
                """,
                (
                    device["id"], device["name"], device["kind"], device["address"],
                    device["group_name"], device["probe_id"], device["method"], device["port"],
                    device["created_at"],
                ),
            )
        with self._lock:
            device["probe_name"] = None
            self._devices[device["id"]] = device
            self._failures[device["id"]] = 0
        self._reload(now)  # traz o nome do servidor (probe_name)
        return self.get(device["id"], now)

    def update(self, device_id: str, changes: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
        """Altera um dispositivo. Devolve None se ele não existe."""
        now = now or utc_now()
        with self._lock:
            device = self._devices.get(device_id)
        if device is None:
            return None
        columns = {"group": "group_name"}
        updated = {columns.get(key, key): value for key, value in changes.items()}
        # Se o alvo do ping mudou, o estado anterior não vale mais: volta a "sem leitura".
        retarget = bool(_TARGET_FIELDS & set(changes)) and any(
            device[columns.get(key, key)] != value for key, value in changes.items() if key in _TARGET_FIELDS
        )
        if retarget:
            updated.update(status="unknown", latency_ms=None, last_checked_at=None, status_since=None)
        assignments = ", ".join(f"{column} = ?" for column in updated)
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE devices SET {assignments} WHERE id = ?", (*updated.values(), device_id)
            )
        with self._lock:
            device.update(updated)
            if retarget:
                self._failures[device_id] = 0
        self._reload(now)
        return self.get(device_id, now)

    def delete(self, device_id: str) -> bool:
        with self._lock:
            if device_id not in self._devices:
                return False
        with self.database.connect() as connection:
            connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        with self._lock:
            self._devices.pop(device_id, None)
            self._failures.pop(device_id, None)
            self._persisted_at.pop(device_id, None)
        return True

    # ---------- Resultados da sonda ----------

    def record_results(self, probe_id: str, results: list[dict[str, Any]], now: datetime | None = None) -> int:
        """Aplica os resultados de um lote da sonda. Devolve quantos foram aceitos.

        Só aceita resultado de dispositivo que existe e que pertence a esta sonda. O banco é escrito
        em UM comando, e só para o que mudou de status ou está na hora de gravar a leitura."""
        now = now or utc_now()
        if any(result["device_id"] not in self._devices for result in results):
            self._reload(now)  # cadastrado por outra instância? confere uma vez
        accepted = 0
        to_persist: list[dict[str, Any]] = []
        stamp = to_db_timestamp(now)
        with self._lock:
            for result in results:
                device = self._devices.get(result["device_id"])
                if device is None or device["probe_id"] != probe_id:
                    continue
                accepted += 1
                device_id = device["id"]
                previous = device["status"]
                if result["ok"]:
                    self._failures[device_id] = 0
                    device["status"] = "up"
                    device["latency_ms"] = result.get("latency_ms")
                else:
                    self._failures[device_id] = self._failures.get(device_id, 0) + 1
                    # Só vira "down" depois de várias falhas seguidas; antes mantém o estado anterior.
                    if self._failures[device_id] >= FAIL_THRESHOLD:
                        device["status"] = "down"
                    device["latency_ms"] = None
                device["last_checked_at"] = stamp
                if device["status"] != previous or device["status_since"] is None:
                    device["status_since"] = stamp
                due = now - self._persisted_at.get(device_id, datetime.min.replace(tzinfo=now.tzinfo)) >= PERSIST_EVERY
                if device["status"] != previous or due:
                    self._persisted_at[device_id] = now
                    to_persist.append(device.copy())
        if to_persist:
            self._persist(to_persist)
        return accepted

    def _persist(self, devices: list[dict[str, Any]]) -> None:
        """Grava o estado de vários dispositivos em UM comando SQL (UPDATE ... FROM uma lista de valores)."""
        values = ", ".join(["(?, ?, ?, ?, ?)"] * len(devices))
        parameters: list[Any] = []
        for device in devices:
            parameters += [
                device["id"], device["status"], device["latency_ms"],
                device["last_checked_at"], device["status_since"],
            ]
        with self.database.connect() as connection:
            connection.execute(
                f"""
                WITH v(id, status, latency_ms, last_checked_at, status_since) AS (VALUES {values})
                UPDATE devices
                SET status = v.status, latency_ms = v.latency_ms,
                    last_checked_at = v.last_checked_at, status_since = v.status_since
                FROM v WHERE devices.id = v.id
                """,
                parameters,
            )
