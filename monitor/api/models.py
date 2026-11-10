from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SystemInfo(BaseModel):
    """Informação do host que raramente muda."""

    model_config = ConfigDict(extra="ignore")

    agent_version: str | None = Field(default=None, max_length=32)
    platform: str | None = Field(default=None, max_length=128)
    platform_version: str | None = Field(default=None, max_length=64)
    build: str | None = Field(default=None, max_length=64)
    arch: str | None = Field(default=None, max_length=32)
    cpu_model: str | None = Field(default=None, max_length=128)
    cpu_cores: int | None = Field(default=None, ge=0, le=4096)
    cpu_threads: int | None = Field(default=None, ge=0, le=4096)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    ips: list[str] = Field(default_factory=list, max_length=8)


class Volume(BaseModel):
    mount: str = Field(min_length=1, max_length=16)
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)


class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    group: str = Field(min_length=1, max_length=100)
    timestamp: datetime | None = None
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    temp_c: float | None = Field(default=None, ge=-273.15)
    net_bytes_sent: int = Field(ge=0)
    net_bytes_recv: int = Field(ge=0)
    uptime_s: int = Field(ge=0)
    hostname: str = Field(min_length=1, max_length=255)
    os: str = Field(min_length=1, max_length=128)
    interval_seconds: int = Field(default=60, ge=1, le=86_400)
    system: SystemInfo | None = None
    volumes: list[Volume] | None = Field(default=None, max_length=16)


class MetricOut(BaseModel):
    received_at: datetime
    agent_timestamp: datetime | None
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    temp_c: float | None
    net_bytes_sent: int
    net_bytes_recv: int
    uptime_s: int


class AgentOut(BaseModel):
    id: str
    group: str
    name: str
    status: str
    last_seen: datetime | None
    hostname: str | None
    os: str | None
    expected_interval_s: int
    latest_metric: MetricOut | None
    system: SystemInfo | None = None
    volumes: list[Volume] | None = None
    net_recv_mbps: float | None = None
    net_sent_mbps: float | None = None


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    group: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> "AgentUpdate":
        if self.name is None and self.group is None:
            raise ValueError("name or group is required")
        return self


class HeartbeatAccepted(BaseModel):
    accepted: bool = True
    received_at: datetime
