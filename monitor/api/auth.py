import hashlib
import hmac
import os

from fastapi import Header, HTTPException, status


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _verify(provided: str | None, headers: dict[str, str] | None = None) -> None:
    configured = os.getenv("AGENT_TOKEN")
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AGENT_TOKEN is not configured",
        )
    if not provided or not hmac.compare_digest(_digest(provided), _digest(configured)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers=headers,
        )


def require_agent_token(authorization: str | None = Header(default=None)) -> None:
    scheme, _, token = (authorization or "").partition(" ")
    _verify(token if scheme.lower() == "bearer" else None, {"WWW-Authenticate": "Bearer"})


def require_management_key(x_management_key: str | None = Header(default=None)) -> None:
    _verify(x_management_key)
