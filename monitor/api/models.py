"""Modelos (formatos de dados) da API.

Usam o Pydantic: cada classe descreve os campos que a API aceita ou devolve, e o Pydantic valida
os tipos e limites automaticamente. Se um dado de entrada não cabe, a API responde 422.

  - Heartbeat*  : o que o agente envia a cada minuto.
  - *Out        : o que a API devolve ao painel.
  - AgentUpdate : o que o painel envia para renomear/mover um host.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _fit_text(value, limit):
    """Informação descritiva nunca deve fazer o heartbeat ser recusado: apara o excesso."""
    return value[:limit] if isinstance(value, str) else value


class SystemInfo(BaseModel):
    """Informação do host que raramente muda."""

    # Campos desconhecidos são ignorados (e não recusados), para um agente mais novo
    # poder enviar dados extras sem quebrar a API.
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

    @field_validator("agent_version", "platform", "platform_version", "build", "arch", "cpu_model", mode="before")
    @classmethod
    def _trim_text(cls, value, info):
        # Corta o texto no limite do próprio campo (o mesmo max_length declarado acima),
        # antes da validação, para que um texto longo não cause erro 422.
        return _fit_text(value, cls.model_fields[info.field_name].metadata[0].max_length)

    @field_validator("ips", mode="before")
    @classmethod
    def _limit_ips(cls, value):
        # Mais de 8 IPs (muitas placas virtuais) é cortado, não recusado.
        return value[:8] if isinstance(value, list) else value


class Volume(BaseModel):
    """Um disco fixo do computador, com o uso em bytes."""

    mount: str = Field(min_length=1, max_length=16)
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)

    @field_validator("mount", mode="before")
    @classmethod
    def _trim_mount(cls, value):
        return _fit_text(value, 16)


class HeartbeatIn(BaseModel):
    """Um "sinal de vida" do agente: métricas do momento e dados do computador."""

    # Aqui, ao contrário de SystemInfo, campo desconhecido é recusado: pega erro de digitação
    # e versões incompatíveis do agente logo no início.
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1, max_length=128)
    group: str = Field(min_length=1, max_length=100)
    timestamp: datetime | None = None
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    disk_percent: float = Field(ge=0, le=100)
    temp_c: float | None = Field(default=None, ge=-273.15)
    # Contadores acumulados de bytes (desde que o computador ligou); a velocidade é calculada depois.
    net_bytes_sent: int = Field(ge=0)
    net_bytes_recv: int = Field(ge=0)
    uptime_s: int = Field(ge=0)
    hostname: str = Field(min_length=1, max_length=255)
    os: str = Field(min_length=1, max_length=128)
    # Intervalo (em segundos) com que o agente promete enviar; a API marca o host offline
    # quando passam 3 intervalos sem contato.
    interval_seconds: int = Field(default=60, ge=1, le=86_400)
    system: SystemInfo | None = None
    volumes: list[Volume] | None = Field(default=None, max_length=16)

    @field_validator("cpu_percent", "memory_percent", "disk_percent", mode="before")
    @classmethod
    def _clamp_percent(cls, value):
        # O Windows às vezes devolve 100,01 ou -0,0: é arredondamento, não erro.
        return min(max(value, 0), 100) if isinstance(value, (int, float)) else value

    @field_validator("hostname", "os", mode="before")
    @classmethod
    def _trim_names(cls, value, info):
        return _fit_text(value, 255 if info.field_name == "hostname" else 128)

    @field_validator("volumes", mode="before")
    @classmethod
    def _limit_volumes(cls, value):
        return value[:16] if isinstance(value, list) else value


class MetricOut(BaseModel):
    """Uma medição devolvida ao painel (última medição do host ou ponto do gráfico)."""

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
    """Um host como o painel o vê: cadastro, status e a última medição."""

    id: str
    group: str
    name: str
    # "online" ou "offline" (calculado na hora pela API, não fica guardado no banco).
    status: str
    last_seen: datetime | None
    hostname: str | None
    os: str | None
    expected_interval_s: int
    latest_metric: MetricOut | None
    system: SystemInfo | None = None
    volumes: list[Volume] | None = None
    # Velocidade de rede em Mbps, calculada entre as duas últimas medições (None se não der).
    net_recv_mbps: float | None = None
    net_sent_mbps: float | None = None


class AgentUpdate(BaseModel):
    """Pedido do painel para renomear um host e/ou mudar o grupo dele."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    group: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def require_change(self) -> "AgentUpdate":
        # Um pedido vazio não faz sentido: exige pelo menos um dos dois campos.
        if self.name is None and self.group is None:
            raise ValueError("name or group is required")
        return self


class HeartbeatAccepted(BaseModel):
    """Resposta ao agente: confirma o recebimento e devolve a hora em que a API recebeu."""

    accepted: bool = True
    received_at: datetime
