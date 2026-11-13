"""API de monitoramento de hardware (FastAPI).

Faz três coisas:
  1. Recebe os heartbeats dos agentes (POST /api/heartbeat) e grava no banco.
  2. Entrega ao painel a lista de hosts e o histórico (GET /api/agents e /api/agents/{id}/metrics),
     e permite renomear/excluir hosts (PATCH/DELETE, protegidos pela chave de gestão).
  3. Serve o painel em si (página e arquivos de static/) na raiz "/".

Como rodar localmente: fastapi dev main.py --port 8009   (veja o README).
"""

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from auth import require_agent_token, require_management_key
from db import Database
from models import (
    AgentOut,
    AgentUpdate,
    HeartbeatAccepted,
    HeartbeatIn,
    MetricOut,
)


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
# Escolhe o banco pelas variáveis de ambiente: Turso (produção) se TURSO_* estiverem
# definidas, senão um arquivo SQLite local (monitor.db, ao lado deste arquivo).
database = Database.from_environment(BASE_DIR / "monitor.db")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Roda uma vez quando a API sobe: cria as tabelas (se não existirem) e carrega o
    # que já se sabe dos agentes. O "yield" é o momento em que a API fica atendendo.
    database.initialize()
    yield


app = FastAPI(
    title="Hardware Monitor API",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS: de quais endereços um navegador pode chamar esta API. O painel servido pela própria
# API não precisa disso; só serve para um painel hospedado em outro endereço.
# Lista separada por vírgula na variável CORS_ORIGINS.
allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS", "http://localhost:8009,http://127.0.0.1:8009"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Management-Key"],
)
# Arquivos do painel (JavaScript, CSS, imagens) ficam em /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    # A página inicial nunca é guardada em cache: assim, depois de uma atualização do painel,
    # o navegador sempre recebe a versão nova (que aponta para os arquivos novos em /static).
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.post(
    "/api/heartbeat",
    response_model=HeartbeatAccepted,
    dependencies=[Depends(require_agent_token)],
)
def heartbeat(payload: HeartbeatIn) -> HeartbeatAccepted:
    # Rota chamada pelos agentes (exige o token). A validação dos dados já foi feita
    # pelo modelo HeartbeatIn; aqui só grava e confirma o recebimento.
    return HeartbeatAccepted(received_at=database.record_heartbeat(payload.model_dump()))


# O painel pede a lista a cada 30 s em cada tela aberta; as chamadas ao banco se enfileiram,
# então vários pedidos dentro de alguns segundos compartilham a mesma consulta.
AGENTS_CACHE_SECONDS = 10
_agents_cache: dict[str, Any] = {"expires": 0.0, "value": []}
_agents_lock = threading.Lock()


def _forget_agents() -> None:
    # Descarta a lista guardada: chamada depois de editar ou excluir um host,
    # para o painel ver a mudança na hora e não daqui a até 10 s.
    _agents_cache["expires"] = 0.0


@app.get("/api/agents", response_model=list[AgentOut])
def list_agents() -> list[AgentOut]:
    # O lock faz quem chegar enquanto a lista está sendo buscada esperar e reaproveitar
    # o resultado, em vez de disparar uma consulta repetida ao banco.
    with _agents_lock:
        if time.monotonic() >= _agents_cache["expires"]:
            _agents_cache["value"] = [AgentOut(**agent) for agent in database.list_agents()]
            _agents_cache["expires"] = time.monotonic() + AGENTS_CACHE_SECONDS
        return _agents_cache["value"]


@app.patch(
    "/api/agents/{agent_id}",
    response_model=AgentOut,
    dependencies=[Depends(require_management_key)],
)
def update_agent(agent_id: str, changes: AgentUpdate) -> AgentOut:
    # Renomeia o host e/ou muda o grupo dele (botão "Editar host" do painel).
    updated = database.update_agent(
        agent_id, name=changes.name, group=changes.group
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _forget_agents()
    return AgentOut(**database.list_agents(agent_id)[0])


@app.delete(
    "/api/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_management_key)],
)
def delete_agent(agent_id: str) -> Response:
    # Exclui o host e todo o histórico dele. Se o agente continuar rodando no PC,
    # o próximo heartbeat cadastra o host de novo.
    if not database.delete_agent(agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    _forget_agents()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/agents/{agent_id}/metrics", response_model=list[MetricOut])
def get_metrics(
    agent_id: str,
    minutes: int = Query(default=60, ge=1, le=10_080),
) -> list[MetricOut]:
    # Histórico para os gráficos: as últimas `minutes` (de 1 minuto até 7 dias).
    metrics = database.get_metrics(agent_id, minutes)
    if metrics is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return [MetricOut(**metric) for metric in metrics]
