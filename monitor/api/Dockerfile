# Imagem da API de monitoramento (FastAPI). Montada pelo docker-compose de monitor/deploy.
FROM python:3.12-slim

# Sem arquivos .pyc e com a saída dos logs sem atraso (aparece na hora em "docker logs").
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# As dependências vêm antes do código: enquanto o requirements.txt não mudar, o Docker reaproveita
# esta camada e a reconstrução da imagem é rápida.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# A API roda sem privilégios de administrador dentro do container.
RUN useradd --system --no-create-home --uid 10001 api && chown -R api /app
USER api

EXPOSE 8000

# Confere de tempos em tempos se a API responde; o Docker reinicia o container se ela travar.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/agents', timeout=4)"

# UM processo só (--workers 1) de propósito: a API guarda em memória o estado dos dispositivos e
# caches, que não são compartilhados entre processos. A carga (50 PCs a cada 60 s) é pequena.
# --proxy-headers faz a API enxergar o IP real do cliente atrás do proxy (Caddy).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
