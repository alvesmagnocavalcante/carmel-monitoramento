"""Autenticação da API.

Há dois segredos separados:
  - AGENT_TOKEN: usado pelos agentes, no cabeçalho "Authorization: Bearer <token>", para enviar heartbeats;
  - MANAGEMENT_PASSWORD: a senha do painel, enviada no cabeçalho "X-Management-Key", para editar ou excluir um host.
A consulta dos dados (lista de hosts e histórico) é pública e não passa por aqui.
"""

import hashlib
import hmac
import os

from fastapi import Header, HTTPException, status


def _digest(value: str) -> bytes:
    # Transforma o texto em um hash de tamanho fixo (32 bytes) antes de comparar.
    # Assim a comparação abaixo leva sempre o mesmo tempo, não importa o tamanho do valor enviado.
    return hashlib.sha256(value.encode("utf-8")).digest()


def _verify(
    provided: str | None, env_var: str, headers: dict[str, str] | None = None
) -> None:
    """Confere o valor recebido com o configurado em `env_var`; levanta erro HTTP se não bater.

    503 = a API foi publicada sem a variável (erro de configuração, não do cliente).
    401 = valor ausente ou errado. `headers` entra na resposta 401 (ex.: WWW-Authenticate).
    """
    configured = os.getenv(env_var)
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{env_var} is not configured",
        )
    # compare_digest evita "timing attacks": uma comparação comum termina mais cedo
    # quando as primeiras letras erram, o que permitiria descobrir a chave aos poucos.
    if not provided or not hmac.compare_digest(_digest(provided), _digest(configured)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers=headers,
        )


def require_agent_token(authorization: str | None = Header(default=None)) -> None:
    """Dependência das rotas dos agentes: exige "Authorization: Bearer <AGENT_TOKEN>"."""
    # O cabeçalho vem como "Bearer abc123": separa a palavra "Bearer" do token.
    scheme, _, token = (authorization or "").partition(" ")
    _verify(token if scheme.lower() == "bearer" else None, "AGENT_TOKEN", {"WWW-Authenticate": "Bearer"})


def require_management_key(x_management_key: str | None = Header(default=None)) -> None:
    """Dependência das rotas de gestão (editar/excluir host): exige a senha em X-Management-Key."""
    _verify(x_management_key, "MANAGEMENT_PASSWORD")
