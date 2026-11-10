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

O agente também envia informações do computador (Windows, CPU, memória, discos fixos, IPs, uptime e versão do agente, definida em `agentVersion`; para mudar no build: `-X main.agentVersion=1.2.0`). Ao publicar uma versão nova, faça o deploy da API **antes** de atualizar os agentes.

Na primeira execução o agente pergunta o grupo (ou use `--group "Nome"`) e grava `config.json` ao lado do exe.
Campos: `api_url`, `group`, `token`, `interval_seconds`. As variáveis `API_URL`, `AGENT_GROUP`, `AGENT_TOKEN`, `INTERVAL_SECONDS` e `CONFIG_PATH` têm precedência sobre o arquivo.

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
git tag agent-v1.0.0
git push origin agent-v1.0.0
```

O repositório ou, no mínimo, os assets da release precisam estar acessíveis pelos
computadores. O playbook baixa o `.exe` diretamente do GitHub e valida o SHA-256
antes da instalação.

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
