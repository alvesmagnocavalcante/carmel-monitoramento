import os
from contextlib import asynccontextmanager
from pathlib import Path

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
database = Database.from_environment(BASE_DIR / "monitor.db")


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    yield


app = FastAPI(
    title="Hardware Monitor API",
    version="0.1.0",
    lifespan=lifespan,
)

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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
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
    return HeartbeatAccepted(received_at=database.record_heartbeat(payload.model_dump()))


@app.get("/api/agents", response_model=list[AgentOut])
def list_agents() -> list[AgentOut]:
    return [AgentOut(**agent) for agent in database.list_agents()]


@app.patch(
    "/api/agents/{agent_id}",
    response_model=AgentOut,
    dependencies=[Depends(require_management_key)],
)
def update_agent(agent_id: str, changes: AgentUpdate) -> AgentOut:
    updated = database.update_agent(
        agent_id, name=changes.name, group=changes.group
    )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return AgentOut(**database.list_agents(agent_id)[0])


@app.delete(
    "/api/agents/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_management_key)],
)
def delete_agent(agent_id: str) -> Response:
    if not database.delete_agent(agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/agents/{agent_id}/metrics", response_model=list[MetricOut])
def get_metrics(
    agent_id: str,
    minutes: int = Query(default=60, ge=1, le=10_080),
) -> list[MetricOut]:
    metrics = database.get_metrics(agent_id, minutes)
    if metrics is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return [MetricOut(**metric) for metric in metrics]
