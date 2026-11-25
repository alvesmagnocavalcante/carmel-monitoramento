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
| `AGENT_TOKEN` | Token dos agentes (heartbeats). |
| `MANAGEMENT_PASSWORD` | Senha para editar/excluir no dashboard. |
| `DATABASE_URL` | PostgreSQL (`postgresql://usuario:senha@host:5432/banco`). Tem prioridade sobre as duas linhas abaixo. |
| `DATABASE_PATH` | Arquivo SQLite local, usado só sem PostgreSQL e sem Turso (padrão `api/monitor.db`). |
| `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` | Quando definidas (e sem `DATABASE_URL`), substituem o SQLite. Sempre juntas. |
| `METRICS_RETENTION_DAYS` | Dias de histórico mantidos (padrão 14, `0` desativa a limpeza). |
| `CORS_ORIGINS` | Origens permitidas para chamadas de outro site (padrão `localhost:8009`). O dashboard não precisa. |

Testes: `cd monitor/api && python -m unittest test_db test_devices`. Os testes de PostgreSQL (`test_postgres`) só rodam com `TEST_DATABASE_URL` apontando para um banco **de teste** (eles apagam as tabelas).
Deploy no FastAPI Cloud: `cd monitor/api && fastapi deploy` (sempre de dentro de `monitor/api`).

## Rodar em servidor próprio (Docker + PostgreSQL)

A API roda em um container, atrás do Caddy (HTTPS automático), e usa um PostgreSQL que já exista em outro container. Arquivos em `monitor/deploy/` e `monitor/api/Dockerfile`.

```
agentes --HTTPS--> Caddy (80/443) --> API (container) --> PostgreSQL (outro container)
```

1. **Banco**: no container do PostgreSQL crie um usuário e um banco só para este sistema:
   ```sql
   CREATE USER monitor WITH PASSWORD 'uma-senha-forte';
   CREATE DATABASE monitor OWNER monitor;
   ```
   Para organizar por **schema** (um banco compartilhado, um schema por aplicação), crie o schema e fixe-o como padrão do usuário; as tabelas vão para ele, sem mudar nada na API (testado):
   ```sql
   -- conectado ao banco compartilhado (ex.: \c apps)
   CREATE SCHEMA monitoramento AUTHORIZATION monitor;
   ALTER ROLE monitor SET search_path = monitoramento;
   ```
2. **Endereço HTTPS**: o token dos agentes não pode trafegar sem HTTPS. Com domínio, crie um registro DNS (ex.: `monitor.carmelhoteis.com.br`) apontando para o IP estático da instância. Sem domínio, use o IP com hífens e `.sslip.io` (IP `3.90.12.45` → `3-90-12-45.sslip.io`), que também recebe certificado válido.
3. **Firewall do Lightsail** (aba *Networking*): libere as portas **80** e **443** (e mantenha a 22 só para quem administra).
4. **Subir**: copie o projeto para a instância e, em `monitor/deploy`:
   ```bash
   cp .env.example .env        # preencha: tokens, DATABASE_URL, PG_NETWORK e SITE_ADDRESS
   docker compose up -d --build
   docker compose logs -f api
   ```
   `PG_NETWORK` é o nome da rede do Docker onde o PostgreSQL está (`docker inspect <container-do-postgres>`), e o host do `DATABASE_URL` é o nome desse container.
5. **Copiar os dados** do Turso (hosts, dispositivos e os últimos 2 dias de medições) para o PostgreSQL, uma única vez:
   ```bash
   docker compose run --rm -e TURSO_DATABASE_URL=... -e TURSO_AUTH_TOKEN=... api \
       python migrate_to_postgres.py --destino "$DATABASE_URL"
   ```
6. **Trocar os agentes**: depois de conferir o painel no endereço novo, mude `monitor_agent_api_url` no Semaphore e rode o template em todos os hosts. Mantenha a API antiga no ar até todos terem trocado.

A API roda com **um processo só** (o estado dos dispositivos e os caches ficam em memória). O backup é o do PostgreSQL (`pg_dump`).

## Agente

```bash
cd monitor/agent
go build -ldflags "-X main.defaultAgentToken=<AGENT_TOKEN>" -o dist/monitor-agent.exe .
```

O agente também envia informações do computador (Windows, CPU, memória, discos fixos, IPs, uptime e versão do agente, definida em `agentVersion`; para mudar no build: `-X main.agentVersion=1.2.0`). Ao publicar uma versão nova, faça o deploy da API **antes** de atualizar os agentes.

Na primeira execução o agente pergunta o grupo (ou use `--group "Nome"`) e grava `config.json` ao lado do exe.
Campos: `api_url`, `group`, `token`, `interval_seconds`, `probe`. As variáveis `API_URL`, `AGENT_GROUP`, `AGENT_TOKEN`, `INTERVAL_SECONDS`, `AGENT_PROBE` e `CONFIG_PATH` têm precedência sobre o arquivo.

Se a API estiver fora do ar, os heartbeats ficam numa fila em memória (1000 itens) e são reenviados com backoff.

### Compilar o agente para Windows

```powershell
cd monitor/agent
$env:GOOS = 'windows'
$env:GOARCH = 'amd64'
go build -trimpath -ldflags '-s -w' -o dist/monitor-agent.exe .
Get-FileHash -Algorithm SHA256 dist/monitor-agent.exe
```

Para publicar no GitHub Releases, crie e envie uma tag semântica. O workflow testa,
compila e publica o executável e seu SHA-256 automaticamente:

```bash
git tag agent-vX.Y.Z       # a mesma versão de agentVersion em sysinfo.go
git push origin agent-vX.Y.Z
```

O repositório ou, no mínimo, os assets da release precisam estar acessíveis pelos
computadores. O playbook baixa o `.exe` diretamente do GitHub e valida o SHA-256
antes da instalação.

## Dispositivos sem agente (impressoras, antenas)

Equipamentos que não aceitam o agente são monitorados por **ping** feito por um servidor do mesmo local, a **sonda**:

```
servidor do hotel (agente com "probe": true) --ping--> impressora / antena
        |
        +--- 1 envio por ciclo com todos os resultados ---> API ---> painel (seção "Dispositivos")
```

- **Ligar a sonda** (agente 1.2.0 ou mais novo): no servidor, `"probe": true` no `config.json` (ou `AGENT_PROBE=true`). No Ansible, defina `monitor_agent_probe: true` para o host. Por padrão fica **desligada** e agentes antigos ignoram o campo.
- **Cadastrar** dispositivos pelo painel (nome, tipo, IP, grupo e o servidor que faz o ping, por ping ou por porta TCP). A sonda busca a lista sozinha; não é preciso mexer no servidor para adicionar ou remover equipamentos.
- **Status**: "fora do ar" só depois de 3 falhas seguidas. Sem leitura recente (a sonda desligou) o status é "sem leitura", nunca "fora do ar".
- **Banco**: o estado fica em memória na API e é gravado só quando muda (ou a cada 5 min), em um comando por lote, para não pesar no Turso.
- Rotas: `GET/POST /api/devices`, `PATCH/DELETE /api/devices/{id}` (escrita com a chave de gestão) e, para a sonda, `GET /api/probe/targets` e `POST /api/probe/results` (token do agente).
- Testes: `cd monitor/api && python -m unittest test_devices`.

## Instalação remota com Ansible e Semaphore

O agente funciona como serviço nativo `CarmelMonitorAgent`, inicia automaticamente
com atraso e não abre janela na sessão do usuário. O serviço usa a conta restrita
`LocalService`; o token fica em `C:\Program Files\Carmel Monitor\config.json`, com
ACL limitada ao serviço, `SYSTEM` e administradores.

### Pré-requisitos

1. Garanta que o ambiente executor do Semaphore tenha `pywinrm>=0.5` e a collection
   `ansible.windows` versão 3.9.2 ou superior.
2. Habilite WinRM HTTPS (porta 5986) nos computadores por GPO ou ferramenta de RMM,
   usando certificado emitido por uma CA confiável pelo servidor Semaphore.
3. Crie uma conta administrativa dedicada ao Ansible. Não salve sua senha no Git.
4. Garanta conectividade do servidor Semaphore até a porta 5986 dos computadores e
   dos computadores até a URL da API e a URL HTTPS do binário.

### Configurar o projeto no Semaphore

1. Cadastre este repositório em **Repositories**.
2. Crie o inventário no Semaphore, definindo `monitor_agent_group` nas variáveis
   de cada grupo de computadores.
3. Associe ao inventário uma credencial **Login with password** com a conta WinRM.
4. Crie um **Variable Group/Environment** com estas variáveis YAML:

   ```yaml
   monitor_agent_release_tag: agent-v1.0.0
   ```

   Na aba **Secrets**, cadastre `MONITOR_AGENT_TOKEN` como **Environment variable**
   com o mesmo valor de `AGENT_TOKEN` da API. `monitor_agent_group` é definido por
   grupo no inventário e pode ser sobrescrito por host.
5. Crie um template apontando para `ansible/playbooks/install_agent.yml` e selecione
   o inventário, o repositório e o ambiente cadastrados.
6. Execute primeiro limitando a um PDV piloto. Depois remova o limite; o rollout é
   feito em lotes de 20% e interrompe no primeiro lote com falha.

Para validar a conectividade antes da instalação:

```bash
ansible windows -m ansible.windows.win_ping
```

O role é idempotente: baixa a versão indicada da release, valida seu hash, reinicia
somente quando binário ou configuração mudam, mantém reinício automático após
falha e remove a versão anterior somente depois que o serviço novo estiver ativo.

### Atualizar ou desinstalar o agente

Para atualizar, publique uma nova tag `agent-vX.Y.Z`, altere
`monitor_agent_release_tag` no Variable Group e execute novamente
`ansible/playbooks/install_agent.yml`. Não desinstale antes de atualizar.

Para remover completamente o serviço e `C:\Program Files\Carmel Monitor`, crie no
Semaphore um template separado para:

```text
ansible/playbooks/uninstall_agent.yml
```

Execute primeiro com limite para um equipamento. O playbook de desinstalação não
utiliza token nem tag de release.

## Consumo do Turso

Cada heartbeat grava 1 linha. Com 50 PCs a 60 s são ~2,2M escritas/mês, mais a limpeza de retenção (limite do plano: 10M).
