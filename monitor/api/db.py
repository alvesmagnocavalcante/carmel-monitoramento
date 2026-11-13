"""Acesso ao banco de dados.

Guarda dois tipos de dado:
  - agents  : um registro por computador monitorado (nome, grupo, sistema...).
  - metrics : uma linha por heartbeat recebido (CPU, memória, disco, rede, uptime...).

Funciona com dois bancos, escolhidos pelas variáveis de ambiente (ver Database.from_environment):
  - SQLite local (arquivo), usado em desenvolvimento;
  - Turso (SQLite na nuvem), usado em produção.

Regra de ouro deste arquivo: no Turso cada comando SQL é uma ida à rede, e as idas se enfileiram.
Por isso tudo aqui tenta fazer o MÍNIMO de comandos por requisição.
"""

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Quantos dias de histórico (metrics) são mantidos; o que for mais antigo é apagado.
DEFAULT_RETENTION_DAYS = 14
# De quanto em quanto tempo a limpeza do histórico pode rodar.
PURGE_EVERY = timedelta(hours=6)
# Máximo de pontos devolvidos por gráfico (períodos longos são agrupados em baldes).
MAX_CHART_POINTS = 300
# Janela em que um reenvio do agente ainda é reconhecido como duplicado.
DEDUP_WINDOW = timedelta(minutes=10)
# Por quanto tempo o que se sabe de um agente (hostname, SO...) vale sem reconferir no banco.
KNOWN_AGENT_TTL = timedelta(hours=1)


# Estrutura do banco. "IF NOT EXISTS" permite rodar a cada início da API sem recriar nada.
# O índice (agent_id, received_at) é o que torna rápidas as buscas "últimas medições deste host"
# e a limpeza do histórico antigo.
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT 'Sem grupo',
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    hostname TEXT,
    os TEXT,
    expected_interval_s INTEGER NOT NULL DEFAULT 30 CHECK (expected_interval_s > 0),
    system_info TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    agent_timestamp TEXT,
    cpu_percent REAL NOT NULL,
    memory_percent REAL NOT NULL,
    disk_percent REAL NOT NULL,
    temp_c REAL,
    net_bytes_sent INTEGER NOT NULL,
    net_bytes_recv INTEGER NOT NULL,
    uptime_s INTEGER NOT NULL,
    volumes TEXT,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_agent_received_at
ON metrics(agent_id, received_at);
"""


# ---------- Funções auxiliares (datas e linhas do banco) ----------


def utc_now() -> datetime:
    # Todas as datas do sistema são guardadas e comparadas em UTC, para não depender de fuso horário.
    return datetime.now(timezone.utc)


def to_db_timestamp(value: datetime) -> str:
    # Data -> texto ISO 8601 em UTC (ex.: 2026-11-12T21:19:44+00:00). Datas sem fuso são tratadas como UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_db_timestamp(value: str | None) -> datetime | None:
    # Texto ISO guardado no banco -> data (ou None se vazio).
    return datetime.fromisoformat(value) if value else None


def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    # O banco devolve as linhas como tuplas; aqui viram dicionários {coluna: valor},
    # que são mais fáceis de ler no resto do código.
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def row_as_dict(cursor: Any) -> dict[str, Any] | None:
    # Igual a rows_as_dicts, mas para uma linha só (None se não houver resultado).
    columns = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row, strict=True)) if row else None


class Database:
    """Tudo o que a API faz no banco. Cada método abre sua própria conexão curta."""

    def __init__(
        self,
        path: str | Path,
        *,
        turso_url: str | None = None,
        turso_auth_token: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.path = str(path)
        self.turso_url = turso_url
        self.turso_auth_token = turso_auth_token
        # Permite injetar uma conexão pronta (usado nos testes).
        self.connection_factory = connection_factory
        # "turso" quando há URL do Turso; senão "sqlite". Alguns comandos só valem num dos dois.
        self.backend = "turso" if turso_url else "sqlite"
        self.retention_days = retention_days
        # Quando a limpeza do histórico rodou pela última vez (None = ainda não rodou).
        self._last_purge: datetime | None = None
        # agent_id -> (hostname, os, intervalo e system_info como estão no banco, quando foi conferido).
        # Cada ida ao banco custa caro (as chamadas ao Turso se enfileiram), então o heartbeat
        # comum não consulta o agente: só grava a métrica.
        self._known_agents: dict[str, tuple[dict[str, Any], datetime]] = {}

    @classmethod
    def from_environment(cls, default_path: str | Path) -> "Database":
        """Monta o Database a partir das variáveis de ambiente (ver README)."""
        turso_url = os.getenv("TURSO_DATABASE_URL", "").strip()
        turso_auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        # URL e token do Turso só fazem sentido juntos; um sem o outro é erro de configuração.
        if bool(turso_url) != bool(turso_auth_token):
            raise RuntimeError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured together"
            )
        return cls(
            os.getenv("DATABASE_PATH", str(default_path)),
            turso_url=turso_url or None,
            turso_auth_token=turso_auth_token or None,
            retention_days=int(
                os.getenv("METRICS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
            ),
        )

    def _open_connection(self) -> Any:
        # Abre a conexão certa: a injetada (testes), a do Turso (produção) ou um arquivo SQLite.
        if self.connection_factory:
            return self.connection_factory()
        if self.turso_url:
            # Importado só aqui: o libsql não é necessário em desenvolvimento com SQLite.
            import libsql

            return libsql.connect(
                database=self.turso_url,
                auth_token=self.turso_auth_token or "",
                timeout=10,
                autocommit=True,
            )
        return sqlite3.connect(self.path, timeout=10)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """Uso: `with self.connect() as connection:`. Abre, confirma (commit) ao sair sem erro,
        desfaz (rollback) se der erro e sempre fecha a conexão."""
        connection = self._open_connection()
        if self.backend == "sqlite":  # no Turso cada comando é uma ida à rede: só o necessário
            connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            # O Turso já confirma cada comando sozinho (autocommit); só o SQLite precisa do commit.
            if self.backend == "sqlite":
                connection.commit()
        except Exception:
            if self.backend == "sqlite":
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Prepara o banco quando a API sobe: cria as tabelas, atualiza bancos antigos e
        carrega o que se sabe dos agentes (para o heartbeat não precisar consultar)."""
        if self.backend == "sqlite":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if self.backend == "sqlite":
                # WAL permite ler enquanto se grava; NORMAL reduz esperas de disco sem risco real aqui.
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(SCHEMA)
            # Migração: bancos criados por versões antigas não têm algumas colunas.
            agent_columns = self._columns(connection, "agents")
            if "token_hash" in agent_columns:  # coluna legada, nunca usada
                connection.execute("ALTER TABLE agents DROP COLUMN token_hash")
            if "system_info" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN system_info TEXT")
            if "volumes" not in self._columns(connection, "metrics"):
                connection.execute("ALTER TABLE metrics ADD COLUMN volumes TEXT")
            # Carrega de uma vez só o que já se sabe de cada agente (1 consulta no total).
            now = utc_now()
            for row in rows_as_dicts(
                connection.execute(
                    "SELECT id, hostname, os, expected_interval_s, system_info FROM agents"
                )
            ):
                self._known_agents[row.pop("id")] = (row, now)

    @staticmethod
    def _columns(connection: Any, table: str) -> set[str]:
        # Nomes das colunas de uma tabela (usado nas migrações).
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _agent_exists(connection: Any, agent_id: str) -> bool:
        return connection.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone() is not None

    def agent_exists(self, agent_id: str) -> bool:
        """Confere se um agente (host) está cadastrado. Usado ao escolher a sonda de um dispositivo."""
        with self.connect() as connection:
            return self._agent_exists(connection, agent_id)

    def _known_agent(self, connection: Any, agent_id: str, now: datetime) -> dict[str, Any] | None:
        """Hostname, SO, intervalo e system_info gravados; só consulta o banco se não souber ou se for velho."""
        entry = self._known_agents.get(agent_id)
        if entry and now - entry[1] < KNOWN_AGENT_TTL:
            return entry[0]
        # Não sabia ou a informação passou de 1 hora: confere no banco e guarda.
        row = row_as_dict(
            connection.execute(
                "SELECT hostname, os, expected_interval_s, system_info FROM agents WHERE id = ?",
                (agent_id,),
            )
        )
        if row:
            self._known_agents[agent_id] = (row, now)
        else:
            self._known_agents.pop(agent_id, None)
        return row

    def _remember_agent(self, payload: dict[str, Any], system_json: str | None, now: datetime) -> None:
        # Guarda na memória o que acabou de ser gravado no banco, para os próximos heartbeats
        # do mesmo agente saberem que não há nada novo a gravar.
        self._known_agents[payload["agent_id"]] = (
            {
                "hostname": payload["hostname"],
                "os": payload["os"],
                "expected_interval_s": payload["interval_seconds"],
                "system_info": system_json,
            },
            now,
        )

    def record_heartbeat(self, payload: dict[str, Any]) -> datetime:
        """Grava um heartbeat recebido de um agente. Devolve a hora de recebimento.

        Passos:
          1. Se o agente é novo, cadastra; se mudou hostname/SO/intervalo/sistema, atualiza.
             (No caso comum, nada mudou e este passo não faz nenhuma consulta.)
          2. Grava a medição (metrics), ignorando reenvios duplicados.
          3. De tempos em tempos, apaga o histórico antigo.
        """
        received_at = utc_now()
        agent_timestamp = payload.get("timestamp")
        agent_timestamp_db = (
            to_db_timestamp(agent_timestamp) if agent_timestamp else None
        )
        # Informação fixa do host (SO, CPU, RAM...): gravada só quando muda.
        # sort_keys deixa o texto sempre igual para os mesmos dados, para a comparação funcionar.
        system = payload.get("system")
        system_json = json.dumps(system, sort_keys=True) if system else None
        volumes = payload.get("volumes")
        volumes_json = json.dumps(volumes) if volumes else None
        with self.connect() as connection:
            # Escrita do agente só quando ele é novo ou mudou hostname/os/intervalo/sistema.
            # O last_seen vem da última métrica (ver list_agents).
            known = self._known_agent(connection, payload["agent_id"], received_at)
            if known is None:
                # Agente novo: cadastra. O nome inicial é o hostname (o painel permite renomear).
                connection.execute(
                    """
                    INSERT INTO agents (
                        id, group_name, name, created_at, last_seen,
                        hostname, os, expected_interval_s, system_info
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["agent_id"],
                        payload["group"],
                        payload["hostname"],
                        to_db_timestamp(received_at),
                        to_db_timestamp(received_at),
                        payload["hostname"],
                        payload["os"],
                        payload["interval_seconds"],
                        system_json,
                    ),
                )
                self._remember_agent(payload, system_json, received_at)
            elif (
                known["hostname"] != payload["hostname"]
                or known["os"] != payload["os"]
                or known["expected_interval_s"] != payload["interval_seconds"]
                or (system_json is not None and known["system_info"] != system_json)
            ):
                # Algo mudou. COALESCE mantém o system_info antigo quando este heartbeat não
                # trouxe o campo (agentes antigos não enviam).
                connection.execute(
                    """
                    UPDATE agents SET hostname = ?, os = ?, expected_interval_s = ?,
                        system_info = COALESCE(?, system_info), last_seen = ?
                    WHERE id = ?
                    """,
                    (
                        payload["hostname"],
                        payload["os"],
                        payload["interval_seconds"],
                        system_json,
                        to_db_timestamp(received_at),
                        payload["agent_id"],
                    ),
                )
                self._remember_agent(payload, system_json or known["system_info"], received_at)
            # Grava a medição. O "WHERE NOT EXISTS" evita duplicar quando o agente reenvia o mesmo
            # heartbeat (ex.: a resposta se perdeu): mesma hora do agente dentro da janela de 10 min.
            connection.execute(
                """
                INSERT INTO metrics (
                    agent_id, received_at, agent_timestamp, cpu_percent,
                    memory_percent, disk_percent, temp_c, net_bytes_sent,
                    net_bytes_recv, uptime_s, volumes
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM metrics
                    WHERE agent_id = ? AND received_at >= ? AND agent_timestamp IS ?
                )
                """,
                (
                    payload["agent_id"],
                    to_db_timestamp(received_at),
                    agent_timestamp_db,
                    payload["cpu_percent"],
                    payload["memory_percent"],
                    payload["disk_percent"],
                    payload["temp_c"],
                    payload["net_bytes_sent"],
                    payload["net_bytes_recv"],
                    payload["uptime_s"],
                    volumes_json,
                    payload["agent_id"],
                    to_db_timestamp(received_at - DEDUP_WINDOW),
                    agent_timestamp_db,
                ),
            )
        self._purge_if_due(received_at)
        return received_at

    def _purge_if_due(self, now: datetime) -> None:
        # Roda a limpeza do histórico no máximo a cada PURGE_EVERY (6 h). retention_days = 0 desliga.
        if self.retention_days <= 0:
            return
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        self.purge_old_metrics(self.retention_days)

    def purge_old_metrics(self, days: int) -> None:
        """Apaga as medições mais antigas que `days` dias (limita o tamanho do banco)."""
        cutoff = to_db_timestamp(utc_now() - timedelta(days=days))
        with self.connect() as connection:
            # Um comando só (cada ida ao banco custa caro). Filtrar por agente deixa o SQLite
            # usar o índice (agent_id, received_at) em vez de varrer a tabela inteira.
            connection.execute(
                "DELETE FROM metrics WHERE agent_id IN (SELECT id FROM agents) AND received_at < ?",
                (cutoff,),
            )

    def list_agents(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Lista os hosts (ou só um, se agent_id for informado) com a última medição de cada um
        e o status online/offline."""
        where, params = ("WHERE a.id = ?", (agent_id,)) if agent_id else ("", ())
        with self.connect() as connection:
            # Uma consulta só traz tudo: o cadastro (a), a última medição (m) e a anterior (p).
            # A anterior serve para calcular a velocidade de rede (diferença entre as duas).
            rows = rows_as_dicts(
                connection.execute(
                    f"""
                    SELECT a.id, a.group_name, a.name,
                           COALESCE(m.received_at, a.last_seen) AS last_seen, a.hostname, a.os,
                           a.expected_interval_s, a.system_info, m.volumes,
                           m.received_at, m.agent_timestamp, m.cpu_percent,
                           m.memory_percent, m.disk_percent, m.temp_c,
                           m.net_bytes_sent, m.net_bytes_recv, m.uptime_s,
                           p.received_at AS prev_received_at,
                           p.net_bytes_sent AS prev_net_sent,
                           p.net_bytes_recv AS prev_net_recv
                    FROM agents a
                    LEFT JOIN metrics m ON m.id = (
                        SELECT id FROM metrics
                        WHERE agent_id = a.id
                        ORDER BY received_at DESC, id DESC
                        LIMIT 1
                    )
                    LEFT JOIN metrics p ON p.id = (
                        SELECT id FROM metrics
                        WHERE agent_id = a.id AND received_at < m.received_at
                        ORDER BY received_at DESC, id DESC
                        LIMIT 1
                    )
                    {where}
                    ORDER BY a.name COLLATE NOCASE
                    """,
                    params,
                )
            )

        now = utc_now()
        agents: list[dict[str, Any]] = []
        for row in rows:
            last_seen = from_db_timestamp(row["last_seen"])
            # Online = houve contato há menos de 3 intervalos do agente (ex.: 3 x 60 s = 3 min).
            online = bool(
                last_seen
                and now - last_seen
                <= timedelta(seconds=3 * row["expected_interval_s"])
            )
            latest_metric = self._metric_from_row(row) if row["received_at"] else None
            agents.append(
                {
                    "id": row["id"],
                    "group": row["group_name"],
                    "name": row["name"],
                    "status": "online" if online else "offline",
                    "last_seen": last_seen,
                    "hostname": row["hostname"],
                    "os": row["os"],
                    "expected_interval_s": row["expected_interval_s"],
                    "latest_metric": latest_metric,
                    "system": json.loads(row["system_info"]) if row["system_info"] else None,
                    "volumes": json.loads(row["volumes"]) if row["volumes"] else None,
                    **self._network_rates(row),
                }
            )
        return agents

    def update_agent(
        self, agent_id: str, *, name: str | None, group: str | None
    ) -> bool:
        """Renomeia e/ou muda o grupo de um host. Devolve False se o host não existe
        (ou se nada foi pedido)."""
        # Monta o UPDATE só com os campos informados.
        assignments: list[str] = []
        values: list[Any] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if group is not None:
            assignments.append("group_name = ?")
            values.append(group)
        if not assignments:
            return False

        with self.connect() as connection:
            if not self._agent_exists(connection, agent_id):
                return False
            values.append(agent_id)
            connection.execute(
                f"UPDATE agents SET {', '.join(assignments)} WHERE id = ?", values
            )
        return True

    def delete_agent(self, agent_id: str) -> bool:
        """Exclui um host e todo o histórico dele. Devolve False se o host não existe."""
        with self.connect() as connection:
            if not self._agent_exists(connection, agent_id):
                return False
            connection.execute("DELETE FROM metrics WHERE agent_id = ?", (agent_id,))
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        # Esquece o host na memória: se o agente voltar a enviar, ele é cadastrado de novo.
        self._known_agents.pop(agent_id, None)
        return True

    def get_metrics(self, agent_id: str, minutes: int) -> list[dict[str, Any]] | None:
        """Histórico dos últimos `minutes` minutos para os gráficos. Devolve None se o host não existe.

        Para períodos longos há mais medições do que o gráfico comporta, então elas são agrupadas
        em "baldes" de tempo (no máximo MAX_CHART_POINTS pontos): percentuais viram a média
        do balde e contadores (bytes, uptime) ficam com o maior valor."""
        cutoff = to_db_timestamp(utc_now() - timedelta(minutes=minutes))
        with self.connect() as connection:
            # Tamanho do balde em segundos (divisão para cima).
            bucket_s = max(1, -(-minutes * 60 // MAX_CHART_POINTS))
            rows = rows_as_dicts(
                connection.execute(
                    """
                    SELECT MAX(received_at) AS received_at,
                           MAX(agent_timestamp) AS agent_timestamp,
                           AVG(cpu_percent) AS cpu_percent,
                           AVG(memory_percent) AS memory_percent,
                           AVG(disk_percent) AS disk_percent,
                           AVG(temp_c) AS temp_c,
                           MAX(net_bytes_sent) AS net_bytes_sent,
                           MAX(net_bytes_recv) AS net_bytes_recv,
                           MAX(uptime_s) AS uptime_s
                    FROM metrics
                    WHERE agent_id = ? AND received_at >= ?
                    GROUP BY CAST(strftime('%s', received_at) AS INTEGER) / ?
                    ORDER BY received_at ASC
                    """,
                    (agent_id, cutoff, bucket_s),
                )
            )
            # Caso comum: uma só ida ao banco. Sem pontos, confere se o agente existe.
            if not rows and not self._agent_exists(connection, agent_id):
                return None
        return [self._metric_from_row(row) for row in rows]

    @staticmethod
    def _network_rates(row: dict[str, Any]) -> dict[str, float | None]:
        """Velocidade de rede (Mbps) entre as duas últimas medições do agente.

        Os contadores de bytes são acumulados; se diminuíram (reinício do
        computador) ou não há medição anterior, a velocidade fica indefinida.
        """
        rates: dict[str, float | None] = {"net_recv_mbps": None, "net_sent_mbps": None}
        if not row["received_at"] or not row["prev_received_at"]:
            return rates
        elapsed = (
            from_db_timestamp(row["received_at"]) - from_db_timestamp(row["prev_received_at"])
        ).total_seconds()
        if elapsed <= 0:
            return rates
        for key, now_bytes, before_bytes in (
            ("net_recv_mbps", row["net_bytes_recv"], row["prev_net_recv"]),
            ("net_sent_mbps", row["net_bytes_sent"], row["prev_net_sent"]),
        ):
            if now_bytes >= before_bytes:
                # bytes -> bits (x 8), por segundo, em megabits (/ 1.000.000).
                rates[key] = (now_bytes - before_bytes) * 8 / elapsed / 1_000_000
        return rates

    @staticmethod
    def _metric_from_row(row: dict[str, Any]) -> dict[str, Any]:
        # Extrai de uma linha do banco só os campos de uma medição, convertendo as datas.
        return {
            "received_at": from_db_timestamp(row["received_at"]),
            "agent_timestamp": from_db_timestamp(row["agent_timestamp"]),
            "cpu_percent": row["cpu_percent"],
            "memory_percent": row["memory_percent"],
            "disk_percent": row["disk_percent"],
            "temp_c": row["temp_c"],
            "net_bytes_sent": row["net_bytes_sent"],
            "net_bytes_recv": row["net_bytes_recv"],
            "uptime_s": row["uptime_s"],
        }
