# Monitoramento Carmel

Monitoramento de hardware dos computadores do grupo. Dois componentes em `monitor/`:

- **`agent/`** (Go): roda em cada PC, coleta CPU, memória, disco, temperatura, rede e uptime e envia um heartbeat a cada 60 s.
- **`api/`** (FastAPI): recebe os heartbeats, guarda em SQLite (local) ou Turso (produção) e serve o dashboard em `/`.

## API

```bash
cd monitor/api
pip install -r requirements.txt
cp ../.env.example ../.env        # defina AGENT_TOKEN
fastapi dev main.py --port 8009   # dashboard em http://localhost:8009
```

| Variável | Descrição |
|---|---|
| `AGENT_TOKEN` | Token dos agentes. Também é a chave de gestão (editar/excluir no dashboard). |
| `DATABASE_PATH` | Arquivo SQLite local, usado só sem Turso (padrão `api/monitor.db`). |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Quando definidas, substituem o SQLite. Sempre juntas. |
| `METRICS_RETENTION_DAYS` | Dias de histórico mantidos (padrão 14, `0` desativa a limpeza). |
| `CORS_ORIGINS` | Origens permitidas para chamadas de outro site (padrão `localhost:8009`). O dashboard não precisa. |

Testes: `cd monitor/api && python -m unittest test_db`.
Deploy: `cd monitor/api && fastapi deploy`.

## Agente

```bash
cd monitor/agent
go build -ldflags "-X main.defaultAgentToken=<AGENT_TOKEN>" -o dist/monitor-agent.exe .
```

Na primeira execução o agente pergunta o grupo (ou use `--group "Nome"`) e grava `config.json` ao lado do exe.
Campos: `api_url`, `group`, `token`, `interval_seconds`. As variáveis `API_URL`, `AGENT_GROUP`, `AGENT_TOKEN`, `INTERVAL_SECONDS` e `CONFIG_PATH` têm precedência sobre o arquivo.

Se a API estiver fora do ar, os heartbeats ficam numa fila em memória (1000 itens) e são reenviados com backoff.

## Consumo do Turso

Cada heartbeat grava 1 linha. Com 50 PCs a 60 s são ~2,2M escritas/mês, mais a limpeza de retenção (limite do plano: 10M).
