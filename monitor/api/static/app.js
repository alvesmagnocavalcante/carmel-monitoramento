/*
  app.js - painel (dashboard) de monitoramento de hardware dos PCs dos hotéis.

  Como funciona, em resumo:
  - Este arquivo é carregado pela página HTML do painel (index.html) e roda no navegador.
  - A cada 30 s ele pede a lista de PCs (agentes) à API em /api/agents e redesenha a tabela
    (cards no celular), agrupada por hotel, com filtros, busca e ordenação.
  - Ao clicar num host abre um painel lateral com as informações da máquina e gráficos de
    histórico (biblioteca Chart.js, que fica local; nenhuma outra biblioteca é usada).
  - Os elementos da tela são localizados pelos "id" do HTML (ver o objeto elements abaixo).
    Se um id mudar no HTML, o nome correspondente aqui precisa mudar junto.
  - O modo apresentação (TV) está em outro arquivo, tv.js. Ele reaproveita o estado e várias
    funções daqui (state, elements, groupAgents, fullVolumes, needsAttention, highMemory,
    formatRate, formatDuration, relativeTime, meterBar, setTheme) e é avisado dos dados novos
    pelo evento "agents-updated", disparado em setUpdated().
  - Dispositivos sem agente (impressoras, antenas...) são pingados por um servidor do local e
    listados em /api/devices. Eles têm painel próprio ("Dispositivos"), abaixo da tabela de hosts,
    e são carregados no mesmo ciclo de refresh(), mas de forma independente: se esse carregamento
    falhar, a lista de hosts continua funcionando. O tv.js também lê state.devices.
*/

/* ---------- Constantes de configuração ---------- */

// Quantos hosts aparecem por página na tabela.
const PAGE_SIZE = 25;
// De quanto em quanto tempo (30 s) a lista de agentes é buscada de novo na API.
const AGENTS_REFRESH_MS = 30_000;
// Intervalo mínimo (3 min) para recarregar os gráficos do host aberto; evita sobrecarregar a API.
const METRICS_REFRESH_MS = 180_000;
// A partir de 85% a barra de CPU/memória/disco fica destacada (cor de aviso).
const HIGH_USAGE_PERCENT = 85;
// A partir de 90% um disco é considerado cheio (selo "Disco cheio" e linha pulsando).
const FULL_DISK_PERCENT = 90;
// A partir de 90% de memória em uso o host recebe o selo "Memória alta" e a linha pulsa.
const MEMORY_ALERT_PERCENT = 90;
// Duração (ms) da transição suave entre tema claro e escuro; deve acompanhar o CSS.
const THEME_FADE_MS = 350;
// Tempo (ms) da animação de saída do painel lateral; deve acompanhar o CSS.
const DETAILS_CLOSE_MS = 180;
// Duração (ms) da animação de desenho dos gráficos.
const CHART_ANIMATION_MS = 450;
// Quantas consultas de histórico (host + período) ficam guardadas na memória do navegador.
const METRICS_CACHE_LIMIT = 24;
// Bytes em 1 GB (1024 ao cubo), usado para converter tamanhos de memória e disco.
const BYTES_PER_GB = 1024 ** 3;
// Nomes amigáveis dos tipos de dispositivo, na forma em que a API os envia.
const DEVICE_KIND_LABELS = { impressora: "Impressora", camera: "Câmera", antena: "Antena", outro: "Outro" };
// Os mesmos tipos no plural, para os cabeçalhos de bloco da lista ("Magna · Câmeras"). A ordem das
// chaves é a ordem em que os blocos aparecem dentro de cada grupo.
const DEVICE_KIND_PLURALS = { impressora: "Impressoras", camera: "Câmeras", antena: "Antenas", outro: "Outros" };
// Texto do selo de cada status de dispositivo ("unknown" = ainda sem leitura recente).
const DEVICE_STATUS_LABELS = { up: "No ar", down: "Fora do ar", unknown: "Sem leitura" };
// Ordem na tabela: o que está fora do ar vem primeiro, depois o sem leitura e por fim o que está no ar.
const DEVICE_STATUS_ORDER = { down: 0, unknown: 1, up: 2 };
// Nomes dos campos da API exibidos nas mensagens de erro de validação (422).
const API_FIELD_LABELS = {
  name: "Nome", kind: "Tipo", address: "Endereço", group: "Grupo",
  probe_id: "Servidor", method: "Verificação", port: "Porta",
};

/* ---------- Estado e elementos da tela ---------- */

// Estado único da tela: tudo que muda (dados, filtros, seleção) fica aqui, e as funções
// render* redesenham a tela a partir dele. O tv.js também lê este objeto.
const state = {
  // Lista de hosts devolvida pela API na última atualização.
  agents: [],
  // Lista de dispositivos (impressoras, antenas...) devolvida por /api/devices; também lida pelo tv.js.
  devices: [],
  // Id do host aberto no painel lateral (null = painel fechado).
  selectedAgentId: null,
  // Grupo (hotel) escolhido no filtro; "all" = todos.
  selectedGroup: "all",
  // Filtro de status: "all", "online" ou "offline".
  status: "all",
  // Texto digitado na busca.
  search: "",
  // Coluna usada na ordenação e o sentido (asc = crescente, desc = decrescente).
  sortKey: "name",
  sortDirection: "asc",
  // Página atual da tabela (começa em 1).
  page: 1,
  // Gráficos Chart.js já criados (cpu, memory, disk, network).
  charts: {},
  // Trava para não rodar duas atualizações ao mesmo tempo.
  refreshing: false,
  // Momento (timestamp) em que o histórico foi carregado pela última vez.
  metricsLoadedAt: 0,
  // Cache do histórico por "host:período", para abrir o painel sem esperar a API.
  metricsCache: new Map(),
  // Indica se o modo apresentação (TV) está ativo; é controlado pelo tv.js.
  tv: false,
};

// Atalhos para os elementos do HTML, buscados uma única vez (por id do HTML) ao carregar o script.
const elements = {
  layout: document.querySelector("#layout"),
  tableBody: document.querySelector("#host-table-body"),
  tableWrap: document.querySelector("#host-table-wrap"),
  empty: document.querySelector("#empty-state"),
  details: document.querySelector("#details"),
  detailsTitle: document.querySelector("#details-title"),
  detailsHost: document.querySelector("#details-host"),
  closeDetails: document.querySelector("#close-details"),
  period: document.querySelector("#period-select"),
  updatedAt: document.querySelector("#updated-at"),
  total: document.querySelector("#total-agents"),
  online: document.querySelector("#online-agents"),
  onlineShare: document.querySelector("#online-share"),
  totalNote: document.querySelector("#total-note"),
  offlineShare: document.querySelector("#offline-share"),
  hostFacts: document.querySelector("#host-facts"),
  editHost: document.querySelector("#edit-host"),
  deleteHost: document.querySelector("#delete-host"),
  hostHint: document.querySelector("#host-hint"),
  offline: document.querySelector("#offline-agents"),
  group: document.querySelector("#group-select"),
  statusChips: document.querySelectorAll("#status-chips .chip"),
  search: document.querySelector("#host-search"),
  pagination: document.querySelector("#pagination"),
  paginationSummary: document.querySelector("#pagination-summary"),
  pageNumber: document.querySelector("#page-number"),
  previousPage: document.querySelector("#previous-page"),
  nextPage: document.querySelector("#next-page"),
  themeToggle: document.querySelector("#theme-toggle"),
  editDialog: document.querySelector("#edit-dialog"),
  editForm: document.querySelector("#edit-agent-form"),
  editId: document.querySelector("#edit-agent-id"),
  editName: document.querySelector("#edit-agent-name"),
  editGroup: document.querySelector("#edit-agent-group"),
  closeEdit: document.querySelector("#close-edit-dialog"),
  cancelEdit: document.querySelector("#cancel-edit-dialog"),
  deviceBody: document.querySelector("#device-table-body"),
  deviceTableWrap: document.querySelector("#device-table-wrap"),
  deviceEmpty: document.querySelector("#device-empty"),
  deviceSummary: document.querySelector("#device-summary"),
  addDevice: document.querySelector("#add-device"),
  deviceDialog: document.querySelector("#device-dialog"),
  deviceForm: document.querySelector("#device-form"),
  deviceDialogTitle: document.querySelector("#device-dialog-title"),
  deviceName: document.querySelector("#device-name"),
  deviceKind: document.querySelector("#device-kind"),
  deviceAddress: document.querySelector("#device-address"),
  deviceGroup: document.querySelector("#device-group"),
  deviceGroupList: document.querySelector("#device-group-list"),
  deviceProbe: document.querySelector("#device-probe"),
  deviceMethod: document.querySelector("#device-method"),
  devicePortField: document.querySelector("#device-port-field"),
  devicePort: document.querySelector("#device-port"),
  deviceDelete: document.querySelector("#delete-device"),
  deviceClose: document.querySelector("#close-device-dialog"),
  deviceCancel: document.querySelector("#cancel-device-dialog"),
};

/* ---------- Formatação ---------- */

// Número com exatamente 1 casa decimal no padrão brasileiro (vírgula): 9.4 vira "9,4".
function formatDecimal(value) {
  return value.toLocaleString("pt-BR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

// Porcentagem para exibir; se não há valor (host sem métrica ainda) mostra um traço em vez de "NaN%".
function formatPercent(value) {
  return Number.isFinite(value) ? `${formatDecimal(value)}%` : "—";
}

// Transforma uma data em texto relativo ("Há 5min", "Há 2h") para o "Último contato".
function relativeTime(value) {
  if (!value) return "Nunca recebido";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 5) return "Agora";
  if (seconds < 60) return `Há ${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `Há ${minutes}min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `Há ${hours}h`;
  return `Há ${Math.floor(hours / 24)}d`;
}

// Velocidade de rede: abaixo de 1 Mbps mostra em kbps (1 Mbps = 1000 kbps) para não aparecer "0,0".
function formatRate(mbps) {
  if (!Number.isFinite(mbps)) return "—";
  const kbps = Math.round(mbps * 1000);
  return kbps >= 1000 ? `${formatDecimal(mbps)} Mbps` : `${kbps} kbps`;
}

// Número em pt-BR: sem casas decimais a partir de 100, com 1 casa abaixo disso.
function formatNumber(value) {
  return value.toLocaleString("pt-BR", { maximumFractionDigits: value >= 100 ? 0 : 1 });
}

// Converte bytes em GB, ou em TB quando passa de 1024 GB.
function formatGb(bytes) {
  const gb = bytes / BYTES_PER_GB;
  return gb >= 1024 ? `${formatNumber(gb / 1024)} TB` : `${formatNumber(gb)} GB`;
}

// "9,4 / 15,7 GB": a unidade aparece uma vez só, a do total.
function formatUsage(usedBytes, totalBytes) {
  const unit = totalBytes >= 1024 * BYTES_PER_GB ? 1024 * BYTES_PER_GB : BYTES_PER_GB;
  return `${formatNumber(usedBytes / unit)} / ${formatNumber(totalBytes / unit)} ${unit === BYTES_PER_GB ? "GB" : "TB"}`;
}

// Duração em texto curto (uptime): "3 d 4 h", "5 h 20 min" ou "12 min", mostrando só as 2 maiores unidades.
function formatDuration(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

// Data e hora curtas no padrão brasileiro (ex.: usado no "Último boot").
function formatDateTime(date) {
  return new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" }).format(date);
}

// Apenas hora:minuto, usado nos rótulos do eixo horizontal dos gráficos.
function formatTime(value) {
  return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

// Atualiza o texto "Atualizado às ..." (ou o erro de conexão) e avisa o tv.js, pelo evento
// "agents-updated", que há dados novos (ou falha) para redesenhar o modo apresentação.
function setUpdated(ok) {
  elements.updatedAt.classList.toggle("error", !ok);
  elements.updatedAt.textContent = ok
    ? `Atualizado às ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
    : "Sem conexão com a API";
  document.dispatchEvent(new Event("agents-updated"));
}

/* ---------- Filtros e ordenação ---------- */

// Host atualmente aberto no painel lateral (undefined se nenhum).
function selectedAgent() {
  return state.agents.find((agent) => agent.id === state.selectedAgentId);
}

// Hosts do grupo (hotel) escolhido no filtro, antes de aplicar status e busca. Também usado pelo tv.js.
function groupAgents() {
  if (state.selectedGroup === "all") return state.agents;
  return state.agents.filter((agent) => agent.group === state.selectedGroup);
}

// Host offline não tem uptime confiável: o último valor conhecido ficou para trás.
// Uptime só para host online; null quando offline ou sem métrica.
function hostUptime(agent) {
  return agent.status === "online" ? agent.latest_metric?.uptime_s ?? null : null;
}

// Valor usado para ordenar por uma coluna. Quem não tem dado recebe -1 e vai para o fim
// na ordem decrescente (e para o início na crescente).
function metricForSort(agent, key) {
  if (key === "uptime") return hostUptime(agent) ?? -1;
  if (key === "name") return agent.name.toLocaleLowerCase("pt-BR");
  if (key === "status") return agent.status;
  if (key === "last_seen") return agent.last_seen ? new Date(agent.last_seen).getTime() : 0;
  if (key === "net") return (agent.net_recv_mbps ?? -1) + (agent.net_sent_mbps ?? 0);
  const fields = { cpu: "cpu_percent", memory: "memory_percent", disk: "disk_percent" };
  return agent.latest_metric?.[fields[key]] ?? -1;
}

// Lista final exibida: aplica grupo, status e busca (nome, hostname, SO ou grupo) e ordena.
// Com "todos os grupos" o hotel vem sempre primeiro na ordenação, para manter cada hotel junto.
function filteredAgents() {
  const query = state.search.toLocaleLowerCase("pt-BR");
  return groupAgents()
    .filter((agent) => state.status === "all" || agent.status === state.status)
    .filter((agent) => !query || [agent.name, agent.hostname, agent.os, agent.group]
      .filter(Boolean)
      .some((value) => value.toLocaleLowerCase("pt-BR").includes(query)))
    .sort((left, right) => {
      if (state.selectedGroup === "all" && left.group !== right.group) {
        return left.group.localeCompare(right.group, "pt-BR");
      }
      const a = metricForSort(left, state.sortKey);
      const b = metricForSort(right, state.sortKey);
      const result = typeof a === "string" ? a.localeCompare(b, "pt-BR") : a - b;
      return state.sortDirection === "asc" ? result : -result;
    });
}

// Preenche o seletor de grupos com os hotéis existentes. Se o grupo escolhido deixou de existir
// (ex.: foi renomeado), volta para "todos" para a tela não ficar vazia.
function renderGroups() {
  const groups = [...new Set(state.agents.map((agent) => agent.group))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  if (state.selectedGroup !== "all" && !groups.includes(state.selectedGroup)) state.selectedGroup = "all";
  elements.group.replaceChildren(
    new Option("Todos os grupos", "all"),
    ...groups.map((group) => new Option(group, group)),
  );
  elements.group.value = state.selectedGroup;
}

/* ---------- Tabela / cards ---------- */

// Cria uma célula <td>. O "label" vai no atributo data-label, que o CSS usa para mostrar o
// nome da coluna quando a tabela vira cards no celular.
function createCell(label, content, className) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  cell.className = className;
  if (content instanceof Node) cell.append(content);
  else cell.textContent = content;
  return cell;
}

// Barra de progresso (0 a 100%). A classe "high" muda a cor quando o valor chega a "highFrom".
// Valores fora da faixa são limitados e ausentes viram 0, para a barra nunca "estourar". Usada também pelo tv.js.
function meterBar(percent, highFrom) {
  const meter = document.createElement("div");
  meter.className = `meter${percent >= highFrom ? " high" : ""}`;
  const fill = document.createElement("span");
  fill.style.width = `${Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : 0}%`;
  meter.append(fill);
  return meter;
}

// Célula de CPU/memória/disco: percentual em texto mais a barra.
function metricCell(label, value, className) {
  const wrapper = document.createElement("div");
  wrapper.className = "metric";
  const text = document.createElement("span");
  text.className = "metric-value";
  text.textContent = formatPercent(value);
  wrapper.append(text, meterBar(value, HIGH_USAGE_PERCENT));
  return createCell(label, wrapper, className);
}

// Célula de rede: uma linha para download (↓) e outra para upload (↑).
function networkCell(agent) {
  const wrapper = document.createElement("div");
  wrapper.className = "net";
  [["↓", "in", agent.net_recv_mbps], ["↑", "out", agent.net_sent_mbps]].forEach(([arrow, direction, value]) => {
    const line = document.createElement("span");
    line.className = "net-line";
    const icon = document.createElement("b");
    icon.className = `net-${direction}`;
    icon.textContent = arrow;
    line.append(icon, formatRate(value));
    wrapper.append(line);
  });
  return createCell("Rede", wrapper, "c-net");
}

// Célula de uptime (tempo ligado); traço quando o host está offline (ver hostUptime).
function uptimeCell(agent) {
  const uptime = hostUptime(agent);
  const value = document.createElement("span");
  value.className = "uptime";
  value.textContent = uptime === null ? "—" : formatDuration(uptime);
  return createCell("Uptime", value, "c-uptime");
}

// Discos fixos com uso acima de FULL_DISK_PERCENT (ou o disco do sistema, para agentes antigos).
function fullVolumes(agent) {
  if (!agent.volumes?.length) {
    const percent = agent.latest_metric?.disk_percent;
    return percent >= FULL_DISK_PERCENT ? [{ mount: "Disco", percent }] : [];
  }
  return agent.volumes
    .filter((volume) => volume.total_bytes > 0)
    .map((volume) => ({ mount: volume.mount, percent: (volume.used_bytes / volume.total_bytes) * 100 }))
    .filter((volume) => volume.percent >= FULL_DISK_PERCENT);
}

// Memória em uso acima do limite. Só vale para host online, pois o valor de um offline é antigo.
function highMemory(agent) {
  return agent.status === "online" && agent.latest_metric?.memory_percent >= MEMORY_ALERT_PERCENT;
}

// Offline, disco cheio ou memória alta: o card do host pulsa para chamar atenção.
function needsAttention(agent) {
  return agent.status === "offline" || fullVolumes(agent).length > 0 || highMemory(agent);
}

// Selo de alerta; o "title" aparece como dica ao passar o mouse.
function warningBadge(text, title) {
  const warning = document.createElement("span");
  warning.className = "badge warn";
  warning.textContent = text;
  warning.title = title;
  return warning;
}

// Célula de status: selo online/offline e, se houver, os selos "Disco cheio" e "Memória alta".
function statusCell(agent) {
  const stack = document.createElement("div");
  stack.className = "status-stack";
  const badge = document.createElement("span");
  badge.className = `badge ${agent.status}`;
  badge.textContent = agent.status;
  stack.append(badge);

  const full = fullVolumes(agent);
  if (full.length) {
    stack.append(warningBadge("Disco cheio", full.map((volume) => `${volume.mount} ${Math.round(volume.percent)}%`).join(" · ")));
  }
  if (highMemory(agent)) {
    stack.append(warningBadge("Memória alta", `${Math.round(agent.latest_metric.memory_percent)}% em uso`));
  }
  return createCell("Status", stack, "c-status");
}

// Monta a linha (ou card, no celular) de um host. A classe "alert" faz a linha pulsar e
// "selected" marca o host aberto no painel lateral.
function hostRow(agent) {
  const row = document.createElement("tr");
  if (agent.id === state.selectedAgentId) row.classList.add("selected");
  if (needsAttention(agent)) row.classList.add("alert");

  const hostButton = document.createElement("button");
  hostButton.type = "button";
  hostButton.className = "host-name-button";
  const hostName = document.createElement("strong");
  hostName.textContent = agent.name;
  const hostMeta = document.createElement("span");
  hostMeta.textContent = [agent.os, agent.group].filter(Boolean).join(" · ");
  hostButton.append(hostName, hostMeta);
  hostButton.addEventListener("click", () => selectAgent(agent.id));

  const metric = agent.latest_metric;
  row.append(
    createCell("Host", hostButton, "c-host"),
    statusCell(agent),
    metricCell("CPU", metric?.cpu_percent, "c-cpu"),
    metricCell("Memória", metric?.memory_percent, "c-mem"),
    metricCell("Disco", metric?.disk_percent, "c-disk"),
    networkCell(agent),
    uptimeCell(agent),
    createCell("Último contato", relativeTime(agent.last_seen), "c-last last-contact"),
  );
  // A linha inteira é clicável; o botão do nome já tem o próprio clique, então é ignorado aqui
  // para não abrir e fechar o painel duas vezes.
  row.addEventListener("click", (event) => {
    if (!event.target.closest("button")) selectAgent(agent.id);
  });
  return row;
}

// Linha de cabeçalho de um hotel na tabela, com o total de hosts do grupo.
function groupHeader(group, count) {
  const row = document.createElement("tr");
  row.className = "group-header";
  const cell = document.createElement("td");
  // 8 = número de colunas da tabela; a célula ocupa a linha toda.
  cell.colSpan = 8;

  const content = document.createElement("div");
  content.className = "group-header-content";
  const name = document.createElement("strong");
  name.textContent = group || "Sem grupo";
  const total = document.createElement("span");
  total.textContent = count;
  content.append(name, total);
  cell.append(content);
  row.append(cell);
  return row;
}

// Cartões de resumo do topo (total, online, offline e percentuais). Respeita o filtro de grupo,
// mas não o de status nem a busca, para o resumo mostrar o quadro geral do grupo.
function renderSummary() {
  const grouped = groupAgents();
  const online = grouped.filter((agent) => agent.status === "online").length;
  const offline = grouped.length - online;
  const share = (count) => `${Math.round((count / grouped.length) * 100)}% do parque`;
  const groups = new Set(grouped.map((agent) => agent.group)).size;
  elements.total.textContent = grouped.length;
  elements.online.textContent = online;
  elements.offline.textContent = offline;
  elements.totalNote.textContent = groups ? `em ${groups} ${groups === 1 ? "grupo" : "grupos"}` : "";
  elements.onlineShare.textContent = grouped.length ? share(online) : "";
  elements.offlineShare.textContent = !grouped.length ? "" : offline ? share(offline) : "nenhuma parada";
}

// "Intel(R) Core(TM) i3-3220 CPU @ 3.30GHz" vira "Intel Core i3-3220 @ 3.30GHz".
// (Só para ficar legível no painel; o modelo original não é alterado nos dados.)
function cleanCpuName(name) {
  return name.replace(/\((R|TM)\)/gi, "").replace(/\bCPU\b/i, "").replace(/\s+/g, " ").trim();
}

// Bloco de informação do painel lateral: título, valor, detalhe opcional (sub), barra opcional
// (percent) e "wide" para ocupar a largura toda.
function factTile({ label, value, sub, percent, wide }) {
  const tile = document.createElement("div");
  tile.className = `fact${wide ? " wide" : ""}`;
  const title = document.createElement("span");
  title.className = "fact-label";
  title.textContent = label;
  const main = document.createElement("strong");
  main.className = "fact-value";
  main.textContent = value;
  tile.append(title);
  if (value) tile.append(main);
  if (sub) {
    const detail = document.createElement("span");
    detail.className = "fact-sub";
    detail.textContent = sub;
    tile.append(detail);
  }
  if (Number.isFinite(percent)) tile.append(meterBar(percent, HIGH_USAGE_PERCENT));
  return tile;
}

// Uma linha por disco/volume, com uso e barra (que muda de cor a partir de FULL_DISK_PERCENT).
function volumeRow(volume) {
  const percent = volume.total_bytes > 0 ? (volume.used_bytes / volume.total_bytes) * 100 : 0;
  const row = document.createElement("div");
  row.className = "volume-row";
  const head = document.createElement("div");
  head.className = "volume-head";
  const name = document.createElement("span");
  name.textContent = `${volume.mount} · ${formatUsage(volume.used_bytes, volume.total_bytes)}`;
  const share = document.createElement("span");
  share.textContent = formatPercent(percent);
  head.append(name, share);
  row.append(head, meterBar(percent, FULL_DISK_PERCENT));
  return row;
}

// Bloco "Discos" do painel lateral, listando todos os volumes do host.
function disksTile(volumes) {
  const tile = factTile({ label: "Discos", wide: true });
  const list = document.createElement("div");
  list.className = "volume-list";
  list.append(...volumes.map(volumeRow));
  tile.append(list);
  return tile;
}

// Preenche o painel lateral com as informações do host selecionado (sistema, CPU, memória,
// uptime, temperatura, IPs, versão do agente, discos). Cada bloco só aparece se o dado existir.
function renderHostInfo() {
  const agent = selectedAgent();
  if (!agent) return;
  const { system, latest_metric: metric } = agent;
  const tiles = [];

  if (system) {
    const platform = (system.platform || "").replace(/^Microsoft\s+/, "");
    if (platform) {
      tiles.push(factTile({
        label: "Sistema",
        value: platform,
        sub: [system.platform_version && `versão ${system.platform_version}`, system.build && `build ${system.build}`, system.arch]
          .filter(Boolean).join(" · "),
        wide: true,
      }));
    }
    if (system.cpu_model) {
      tiles.push(factTile({
        label: "CPU",
        value: cleanCpuName(system.cpu_model),
        sub: system.cpu_cores ? `${system.cpu_cores} núcleos · ${system.cpu_threads} threads` : "",
        wide: true,
      }));
    }
    if (system.memory_total_bytes) {
      tiles.push(factTile({
        label: "Memória",
        value: metric
          ? formatUsage((system.memory_total_bytes * metric.memory_percent) / 100, system.memory_total_bytes)
          : formatGb(system.memory_total_bytes),
        sub: metric ? `${formatPercent(metric.memory_percent)} em uso` : "",
        percent: metric?.memory_percent,
      }));
    }
  }
  if (metric) {
    // Uptime e boot são os do último contato do agente.
    const boot = new Date(new Date(metric.received_at) - metric.uptime_s * 1000);
    tiles.push(factTile({ label: "Uptime", value: formatDuration(metric.uptime_s), sub: `Último boot: ${formatDateTime(boot)}` }));
  }
  if (Number.isFinite(metric?.temp_c)) {
    tiles.push(factTile({ label: "Temperatura", value: `${formatDecimal(metric.temp_c)} °C` }));
  }
  if (system?.ips?.length) tiles.push(factTile({ label: "IPs", value: system.ips.join(" · ") }));
  if (system?.agent_version) tiles.push(factTile({ label: "Agente", value: `v${system.agent_version}` }));
  if (agent.volumes?.length) tiles.push(disksTile(agent.volumes));

  elements.hostFacts.replaceChildren(...tiles);
  // O aviso fica visível enquanto o agente ainda não enviou os dados do sistema (agente antigo ou recém-instalado).
  elements.hostHint.hidden = Boolean(system);
}

// Redesenha tudo que depende dos dados e filtros: resumo, painel lateral, tabela e paginação.
// É chamada a cada atualização da API e a cada mudança de filtro, busca, ordem ou página.
function renderHosts() {
  renderSummary();
  renderHostInfo();

  const agents = filteredAgents();
  const pages = Math.max(1, Math.ceil(agents.length / PAGE_SIZE));
  // Se a lista encolheu (filtro, host excluído), evita ficar numa página que não existe mais.
  state.page = Math.min(state.page, pages);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageAgents = agents.slice(start, start + PAGE_SIZE);
  const groupCounts = new Map();
  agents.forEach((agent) => groupCounts.set(agent.group, (groupCounts.get(agent.group) || 0) + 1));
  // Insere um cabeçalho de hotel sempre que o grupo muda (a lista já vem ordenada por grupo).
  const rows = [];
  let currentGroup = null;
  pageAgents.forEach((agent) => {
    if (agent.group !== currentGroup) {
      currentGroup = agent.group;
      rows.push(groupHeader(agent.group, groupCounts.get(agent.group)));
    }
    rows.push(hostRow(agent));
  });
  elements.tableBody.replaceChildren(...rows);
  elements.tableWrap.hidden = agents.length === 0;
  elements.empty.hidden = agents.length > 0;
  elements.pagination.hidden = agents.length === 0;
  elements.paginationSummary.textContent = agents.length
    ? `${start + 1}–${Math.min(start + PAGE_SIZE, agents.length)} de ${agents.length} hosts`
    : "0 hosts";
  elements.pageNumber.textContent = `Página ${state.page} de ${pages}`;
  elements.previousPage.disabled = state.page === 1;
  elements.nextPage.disabled = state.page === pages;
}

// Marca no cabeçalho qual coluna está ordenando e o sentido (seta e atributo aria-sort, para leitores de tela).
function updateSortIndicators() {
  document.querySelectorAll(".sort-button").forEach((button) => {
    const active = button.dataset.sort === state.sortKey;
    button.classList.toggle("active", active);
    button.classList.toggle("desc", active && state.sortDirection === "desc");
    button.closest("th").setAttribute("aria-sort", active
      ? (state.sortDirection === "asc" ? "ascending" : "descending")
      : "none");
  });
}

/* ---------- Tema e gráficos ---------- */

// Lê o valor atual de uma variável de cor do CSS (ex.: --muted), que muda conforme o tema.
// O Chart.js desenha num canvas e não enxerga o CSS, então as cores são passadas por aqui.
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Aplica no gráfico as cores do tema atual. Chamada na criação e a cada troca de tema.
function styleChart(chart) {
  const muted = cssVar("--muted");
  chart.data.datasets.forEach((dataset, index) => {
    const color = cssVar(chart.$colorVars[index]);
    dataset.borderColor = color;
    // Acrescentar "22" à cor hexadecimal a deixa quase transparente (usada no preenchimento sob a linha).
    dataset.backgroundColor = `${color}22`;
  });
  chart.options.scales.x.ticks.color = muted;
  chart.options.scales.y.ticks.color = muted;
  chart.options.scales.y.grid.color = cssVar("--line");
  chart.options.plugins.legend.labels.color = cssVar("--ink-soft");
  // "none" redesenha sem animação, para a troca de tema ser instantânea.
  chart.update("none");
}

// Cria um gráfico de linhas no canvas indicado. Cada nome em "labels" é uma série (linha) e
// "colorVars" traz a variável de cor CSS de cada uma. Os dados entram depois, em updateChart.
function createLineChart(canvasId, colorVars, labels) {
  const chart = new Chart(document.querySelector(`#${canvasId}`), {
    type: "line",
    data: { labels: [], datasets: labels.map((label) => ({
      label,
      data: [],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      // Só gráficos de uma série (CPU, memória, disco) têm área preenchida; o de rede tem duas linhas.
      fill: labels.length === 1,
    })) },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: CHART_ANIMATION_MS, easing: "easeOutQuart" },
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 5 } },
        // Gráficos de percentual vão até 100; o de rede ajusta a escala sozinho (Mbps varia muito).
        y: { beginAtZero: true, suggestedMax: labels.length === 1 ? 100 : undefined, grid: {}, ticks: {} },
      },
      plugins: { legend: { display: labels.length > 1, labels: { boxWidth: 10, boxHeight: 10 } } },
    },
  });
  // Guarda as variáveis de cor no próprio gráfico para styleChart reaplicá-las ao trocar de tema.
  chart.$colorVars = colorVars;
  styleChart(chart);
  return chart;
}

// Cria os quatro gráficos do painel lateral (CPU, memória, disco e rede). Roda uma vez, ao carregar a página.
function initializeCharts() {
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  state.charts.cpu = createLineChart("cpu-chart", ["--chart-cpu"], ["CPU"]);
  state.charts.memory = createLineChart("memory-chart", ["--chart-memory"], ["Memória"]);
  state.charts.disk = createLineChart("disk-chart", ["--chart-disk"], ["Disco"]);
  state.charts.network = createLineChart("network-chart", ["--chart-net-in", "--chart-net-out"], ["Recebido", "Enviado"]);
}

// Guarda o timer que remove a classe de transição do tema (ver setTheme).
let themeFadeTimer;

// Aplica o tema "light" ou "dark", lembra a escolha no navegador e repinta os gráficos. Também usada pelo tv.js.
// A classe theme-fading liga as transições de cor do CSS só durante a troca, para ela ser suave.
function setTheme(theme) {
  const root = document.documentElement;
  root.classList.add("theme-fading");
  root.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch (error) { /* sem armazenamento local */ }
  Object.values(state.charts).forEach(styleChart);
  // Cancela o timer anterior (cliques rápidos) e remove a classe só depois da transição terminar (+100 ms de folga).
  window.clearTimeout(themeFadeTimer);
  themeFadeTimer = window.setTimeout(() => root.classList.remove("theme-fading"), THEME_FADE_MS + 100);
}

// Troca os dados de um gráfico: "labels" é o eixo horizontal (horas) e "series" traz uma lista de valores por linha.
function updateChart(chart, labels, series) {
  chart.data.labels = labels;
  chart.data.datasets.forEach((dataset, index) => { dataset.data = series[index]; });
  chart.update();
}

// O agente envia contadores acumulados de bytes de rede, não a velocidade. Aqui calculamos a
// velocidade (Mbps) entre duas medições seguidas: diferença de bytes x 8 bits / segundos / 1 milhão.
function networkRates(metrics) {
  const labels = [];
  const received = [];
  const sent = [];
  for (let index = 1; index < metrics.length; index += 1) {
    const previous = metrics[index - 1];
    const current = metrics[index];
    const elapsed = (new Date(current.received_at) - new Date(previous.received_at)) / 1000;
    // Ignora medições com tempo repetido ou invertido, para não dividir por zero.
    if (elapsed <= 0) continue;
    labels.push(formatTime(current.received_at));
    // Math.max(0, ...) evita valor negativo quando o contador zera (ex.: o PC foi reiniciado).
    received.push(Math.max(0, current.net_bytes_recv - previous.net_bytes_recv) * 8 / elapsed / 1_000_000);
    sent.push(Math.max(0, current.net_bytes_sent - previous.net_bytes_sent) * 8 / elapsed / 1_000_000);
  }
  return { labels, received, sent };
}

// Desenha o histórico recebido nos quatro gráficos. Lista vazia limpa os gráficos.
function paintMetrics(metrics) {
  const labels = metrics.map((metric) => formatTime(metric.received_at));
  updateChart(state.charts.cpu, labels, [metrics.map((metric) => metric.cpu_percent)]);
  updateChart(state.charts.memory, labels, [metrics.map((metric) => metric.memory_percent)]);
  updateChart(state.charts.disk, labels, [metrics.map((metric) => metric.disk_percent)]);
  const network = networkRates(metrics);
  updateChart(state.charts.network, network.labels, [network.received, network.sent]);
}

// Chave do cache de histórico: host aberto + período escolhido (minutos).
const metricsKey = () => `${state.selectedAgentId}:${elements.period.value}`;

// Busca o histórico do host aberto na API, guarda no cache e desenha nos gráficos.
async function refreshMetrics() {
  if (!state.selectedAgentId) return;
  const selectedId = state.selectedAgentId;
  const key = metricsKey();
  const response = await fetch(`/api/agents/${encodeURIComponent(selectedId)}/metrics?minutes=${elements.period.value}`);
  if (!response.ok) throw new Error(`Falha ao buscar métricas: ${response.status}`);
  const metrics = await response.json();
  // Apaga e regrava a chave para ela ir ao fim do Map; assim o primeiro item é sempre o mais antigo.
  state.metricsCache.delete(key);
  state.metricsCache.set(key, metrics);
  // Passou do limite: descarta o mais antigo, para o cache não crescer sem fim.
  if (state.metricsCache.size > METRICS_CACHE_LIMIT) {
    state.metricsCache.delete(state.metricsCache.keys().next().value);
  }
  // A resposta demorou e o usuário já trocou de host ou de período: o resultado fica no cache,
  // mas não é desenhado, senão os gráficos mostrariam dados do host errado.
  if (selectedId !== state.selectedAgentId || key !== metricsKey()) return;
  state.metricsLoadedAt = Date.now();
  paintMetrics(metrics);
}

// Ao abrir um host (ou trocar o período): mostra na hora o que já se conhece, ou um brilho de
// carregamento, e atualiza quando a API responder.
async function loadMetrics() {
  const cached = state.metricsCache.get(metricsKey());
  if (cached) {
    paintMetrics(cached);
  } else {
    // Sem cache: limpa os gráficos para não aparecer o histórico do host anterior enquanto carrega.
    paintMetrics([]);
    elements.details.classList.add("loading");
  }
  try {
    await refreshMetrics();
  } finally {
    // Tira o brilho de carregamento mesmo se a API falhar (o erro segue para quem chamou).
    elements.details.classList.remove("loading");
  }
}

/* ---------- Histórico: abre ao clicar no host e recolhe ao clicar de novo ---------- */

// Guarda o timer do fechamento do painel, para poder cancelá-lo se o painel for reaberto antes do fim.
let detailsCloseTimer;

// Abre ou fecha o painel lateral de detalhes.
// Ao fechar, o painel sai deslizando (classe closing) e só depois some e devolve o espaço à tabela.
function setDetailsOpen(open) {
  // Cancela um fechamento pendente: sem isso, reabrir logo após fechar faria o timer antigo esconder o painel novo.
  window.clearTimeout(detailsCloseTimer);
  elements.details.classList.remove("closing");
  if (open) {
    elements.details.hidden = false;
    // "has-details" divide a tela entre tabela e painel (o CSS esconde colunas menos importantes
    // para caber); "sheet-open" trava a rolagem do fundo quando o painel cobre a tela no celular.
    elements.layout.classList.add("has-details");
    document.body.classList.add("sheet-open");
    return;
  }
  // setTimeout dá tempo para a animação de saída rodar (DETAILS_CLOSE_MS) antes de esconder o painel de fato.
  elements.details.classList.add("closing");
  detailsCloseTimer = window.setTimeout(() => {
    elements.details.hidden = true;
    elements.details.classList.remove("closing");
    elements.layout.classList.remove("has-details");
    document.body.classList.remove("sheet-open");
  }, DETAILS_CLOSE_MS);
}

// Fecha o painel e limpa a seleção; renderHosts tira o destaque da linha.
function closeDetails() {
  state.selectedAgentId = null;
  setDetailsOpen(false);
  renderHosts();
}

// Clique num host: abre o painel com os dados dele e carrega o histórico. Clicar no mesmo host
// de novo fecha o painel (comportamento de "alternar").
async function selectAgent(agentId) {
  if (state.selectedAgentId === agentId) {
    closeDetails();
    return;
  }
  state.selectedAgentId = agentId;
  const agent = state.agents.find((item) => item.id === agentId);
  elements.detailsTitle.textContent = agent?.name || agentId;
  elements.detailsHost.textContent = [agent?.hostname, agent?.os, agent?.group].filter(Boolean).join(" · ");
  setDetailsOpen(true);
  // Os dados básicos já estão na lista, então o painel aparece na hora; só o histórico espera a API.
  renderHosts();
  try {
    await loadMetrics();
  } catch (error) {
    console.error(error);
    setUpdated(false);
  }
}

/* ---------- Edição e exclusão ---------- */

// Transforma o corpo de erro da API em texto legível. O "detail" pode ser um texto simples ou, nos
// erros de validação (422), uma lista de objetos {loc, msg}; sem este tratamento a lista apareceria
// no alerta como "[object Object]". Cada item vira "Campo: mensagem", um por linha.
function apiErrorMessage(payload, status) {
  const detail = payload?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail.map((item) => {
      if (typeof item === "string") return item;
      // O último trecho textual de "loc" é o nome do campo (ex.: ["body", "port"]).
      const field = Array.isArray(item?.loc) ? item.loc.filter((part) => typeof part === "string" && part !== "body").pop() : null;
      const message = String(item?.msg || "Valor inválido").replace(/^Value error, /, "");
      const label = API_FIELD_LABELS[field] || field;
      return label ? `${label}: ${message}` : message;
    }).join("\n");
  }
  return `Falha na operação: ${status}`;
}

// Faz uma chamada à API que altera dados (editar/excluir), que exige a chave de gestão.
// A chave é pedida por prompt uma vez e guardada em sessionStorage (some ao fechar a aba).
// Devolve null se o usuário cancelou ou a chave foi recusada; lança erro nas demais falhas.
async function managementRequest(url, options) {
  let key = sessionStorage.getItem("managementKey");
  if (!key) {
    key = window.prompt(
      "Cole o token do config.json do agente. Ele será solicitado apenas uma vez neste navegador:",
    );
    if (!key) return null;
    key = key.trim();
    if (!key) return null;
    sessionStorage.setItem("managementKey", key);
  }

  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Management-Key": key,
      ...(options.headers || {}),
    },
  });
  // 401 = chave recusada: esquece a chave guardada para ela ser pedida de novo na próxima tentativa.
  if (response.status === 401) {
    sessionStorage.removeItem("managementKey");
    window.alert("Token inválido. Copie o campo token do config.json do agente.");
    return null;
  }
  if (!response.ok) {
    // Tenta ler a mensagem de erro da API; se a resposta não for JSON, usa um objeto vazio.
    const payload = await response.json().catch(() => ({}));
    throw new Error(apiErrorMessage(payload, response.status));
  }
  return response;
}

// Abre a janela de edição (nome e grupo) já preenchida com os dados do host.
function openEditDialog(agent) {
  elements.editId.value = agent.id;
  elements.editName.value = agent.name;
  elements.editGroup.value = agent.group;
  elements.editDialog.showModal();
  elements.editName.focus();
}

// Exclui o host e todo o histórico, depois de confirmação. Usa managementRequest (pede a chave).
async function deleteAgent(agent) {
  const confirmed = window.confirm(
    `Excluir ${agent.name} e todo o histórico de métricas?\n\n` +
    "Se o agent continuar em execução, ele será cadastrado novamente.",
  );
  if (!confirmed) return;
  try {
    const response = await managementRequest(`/api/agents/${encodeURIComponent(agent.id)}`, {
      method: "DELETE",
    });
    if (!response) return;
    if (state.selectedAgentId === agent.id) closeDetails();
    await refresh();
  } catch (error) {
    window.alert(error.message);
  }
}

/* ---------- Dispositivos (impressoras, antenas...) ---------- */

// Texto do endereço do dispositivo: no teste por porta TCP mostra "endereço:porta".
function deviceAddress(device) {
  return device.method === "tcp" && device.port ? `${device.address}:${device.port}` : device.address;
}

// "Fora do ar há 2 h" / "No ar há 3 d": há quanto tempo o dispositivo está no estado atual. Usa só a
// maior unidade (min, h ou d) para ficar curto na tabela; sem leitura ou sem data mostra um traço.
function deviceSinceText(device) {
  if (device.status === "unknown" || !device.status_since) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(device.status_since).getTime()) / 1000));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  let amount;
  if (minutes < 1) amount = "menos de 1 min";
  else if (hours < 1) amount = `${minutes} min`;
  else if (hours < 24) amount = `${hours} h`;
  else amount = `${Math.floor(hours / 24)} d`;
  return `${device.status === "down" ? "Fora do ar" : "No ar"} há ${amount}`;
}

// Monta a linha (ou card, no celular) de um dispositivo. Só "down" recebe a classe "alert", que
// reaproveita o pulso dos hosts em alerta; "unknown" (sem leitura) não pulsa de propósito.
function deviceRow(device) {
  const row = document.createElement("tr");
  if (device.status === "down") row.classList.add("alert");

  // O nome é um botão (como nos hosts) para o teclado também conseguir abrir a edição.
  const nameButton = document.createElement("button");
  nameButton.type = "button";
  nameButton.className = "host-name-button";
  const name = document.createElement("strong");
  name.textContent = device.name;
  const meta = document.createElement("span");
  // Tipo e grupo já aparecem no cabeçalho do bloco; aqui fica só o endereço.
  meta.textContent = deviceAddress(device);
  nameButton.append(name, meta);
  nameButton.addEventListener("click", () => openDeviceDialog(device));

  const badge = document.createElement("span");
  badge.className = `badge ${device.status}`;
  badge.textContent = DEVICE_STATUS_LABELS[device.status] || device.status;

  const latency = Number.isFinite(device.latency_ms) ? `${formatDecimal(device.latency_ms)} ms` : "—";
  row.append(
    createCell("Dispositivo", nameButton, "c-dev"),
    createCell("Status", badge, "c-dstatus"),
    createCell("Resposta", latency, "c-dresp device-value"),
    createCell("Desde", deviceSinceText(device), "c-dsince device-value"),
    createCell("Servidor", device.probe_name || "—", "c-dserver device-value"),
  );
  // A linha inteira abre a edição; o botão do nome já tem o próprio clique.
  row.addEventListener("click", (event) => {
    if (!event.target.closest("button")) openDeviceDialog(device);
  });
  return row;
}

// Cabeçalho de um bloco da lista: "Magna · Câmeras" e "3 de 4 no ar". Se algum do bloco está fora do ar,
// o contador ganha a cor de alerta para o problema aparecer sem precisar ler a tabela.
function deviceGroupRow(devices) {
  const first = devices[0];
  const up = devices.filter((device) => device.status === "up").length;
  const down = devices.filter((device) => device.status === "down").length;
  const row = document.createElement("tr");
  row.className = "device-group-row";
  const cell = document.createElement("td");
  cell.colSpan = 5;
  const title = document.createElement("strong");
  title.textContent = [first.group, DEVICE_KIND_PLURALS[first.kind] || first.kind].filter(Boolean).join(" · ");
  const total = document.createElement("span");
  total.textContent = `${up} de ${devices.length} no ar`;
  if (down) total.classList.add("has-down");
  cell.append(title, total);
  row.append(cell);
  return row;
}

// Redesenha o painel de dispositivos: resumo (no ar, fora do ar, sem leitura), tabela e estado vazio.
// O painel nunca é escondido, para o botão "Adicionar dispositivo" continuar acessível sem cadastros.
function renderDevices() {
  const kindOrder = (device) => {
    const index = Object.keys(DEVICE_KIND_PLURALS).indexOf(device.kind);
    return index === -1 ? 99 : index;
  };
  // Ordem: grupo, depois tipo (impressoras, câmeras, antenas...); dentro do bloco, o que está fora do ar
  // primeiro, depois sem leitura, depois no ar, e por fim o nome.
  const devices = [...state.devices].sort((left, right) =>
    (left.group || "").localeCompare(right.group || "", "pt-BR")
    || kindOrder(left) - kindOrder(right)
    || (DEVICE_STATUS_ORDER[left.status] ?? 1) - (DEVICE_STATUS_ORDER[right.status] ?? 1)
    || left.name.localeCompare(right.name, "pt-BR"));
  const count = (status) => devices.filter((device) => device.status === status).length;
  elements.deviceSummary.textContent = devices.length
    ? `${count("up")} no ar · ${count("down")} fora do ar · ${count("unknown")} sem leitura`
    : "";

  // Uma linha de cabeçalho a cada mudança de grupo ou de tipo, com quantos do bloco estão no ar.
  const rows = [];
  let blockKey = null;
  let block = [];
  const closeBlock = () => {
    if (block.length) rows.splice(rows.length - block.length, 0, deviceGroupRow(block));
    block = [];
  };
  for (const device of devices) {
    const key = `${device.group || ""}\u0000${device.kind}`;
    if (key !== blockKey) {
      closeBlock();
      blockKey = key;
    }
    block.push(device);
    rows.push(deviceRow(device));
  }
  closeBlock();
  elements.deviceBody.replaceChildren(...rows);
  elements.deviceTableWrap.hidden = devices.length === 0;
  elements.deviceEmpty.hidden = devices.length > 0;
}

// Busca a lista de dispositivos. Falhas são tratadas aqui mesmo e NÃO propagam: um problema nesta
// rota não pode marcar "Sem conexão com a API" nem derrubar a lista de hosts. Em caso de erro a
// lista anterior continua na tela.
async function loadDevices() {
  try {
    const response = await fetch("/api/devices", { cache: "no-store" });
    if (!response.ok) throw new Error(`Falha ao buscar dispositivos: ${response.status}`);
    state.devices = await response.json();
    renderDevices();
  } catch (error) {
    console.warn(error);
  }
}

// Dispositivo em edição (null = cadastro novo). Guarda o original para enviar só o que mudou.
let editingDevice = null;

// Mostra a porta só no teste por porta TCP, onde ela é obrigatória.
function syncDevicePort() {
  const tcp = elements.deviceMethod.value === "tcp";
  elements.devicePortField.hidden = !tcp;
  elements.devicePort.required = tcp;
}

// Preenche as opções do servidor que faz o ping (agentes de state.agents, por grupo e nome). Se o
// servidor do dispositivo editado não está mais na lista, ele é mantido para a edição não perdê-lo.
function fillDeviceProbes(selectedId) {
  const agents = [...state.agents].sort((left, right) =>
    left.group.localeCompare(right.group, "pt-BR") || left.name.localeCompare(right.name, "pt-BR"));
  const options = [new Option("Selecione o servidor", "")];
  if (selectedId && !agents.some((agent) => agent.id === selectedId)) {
    options.push(new Option(editingDevice?.probe_name || selectedId, selectedId));
  }
  agents.forEach((agent) => options.push(new Option(`${agent.name} (${agent.group})`, agent.id)));
  elements.deviceProbe.replaceChildren(...options);
  elements.deviceProbe.value = selectedId || "";
}

// Abre o diálogo para cadastrar (sem argumento) ou editar um dispositivo, já preenchido.
function openDeviceDialog(device = null) {
  editingDevice = device;
  elements.deviceDialogTitle.textContent = device ? "Editar dispositivo" : "Adicionar dispositivo";
  elements.deviceDelete.hidden = !device;
  elements.deviceName.value = device?.name ?? "";
  elements.deviceKind.value = device?.kind ?? "impressora";
  elements.deviceAddress.value = device?.address ?? "";
  elements.deviceGroup.value = device?.group ?? "";
  elements.deviceMethod.value = device?.method ?? "ping";
  elements.devicePort.value = device?.port ?? "";
  // Os grupos já existentes entram como sugestão no campo Grupo (o campo continua livre).
  const groups = [...new Set(state.agents.map((agent) => agent.group))].sort((a, b) => a.localeCompare(b, "pt-BR"));
  elements.deviceGroupList.replaceChildren(...groups.map((group) => new Option(group)));
  fillDeviceProbes(device?.probe_id ?? "");
  // No cadastro o servidor é obrigatório; na edição, um dispositivo sem servidor pode ser salvo sem escolher.
  elements.deviceProbe.required = !device;
  syncDevicePort();
  elements.deviceDialog.showModal();
  elements.deviceName.focus();
}

// Valores atuais do formulário, no formato da API. A porta só vale no TCP; no ping vai null.
function deviceFormValues() {
  const tcp = elements.deviceMethod.value === "tcp";
  return {
    name: elements.deviceName.value.trim(),
    kind: elements.deviceKind.value,
    address: elements.deviceAddress.value.trim(),
    group: elements.deviceGroup.value.trim(),
    probe_id: elements.deviceProbe.value,
    method: elements.deviceMethod.value,
    port: tcp ? Number(elements.devicePort.value) : null,
  };
}

// Salva o dispositivo: POST no cadastro; na edição, PATCH só com os campos que mudaram (e, se nada
// mudou, apenas fecha). Usa managementRequest (pede a chave de gestão) e recarrega a lista na hora.
async function saveDevice() {
  const values = deviceFormValues();
  let url = "/api/devices";
  let options;
  if (editingDevice) {
    const changes = {};
    Object.entries(values).forEach(([field, value]) => {
      // Servidor vazio na edição = nada escolhido: não envia, para não apagar o vínculo existente.
      if (field === "probe_id" && !value) return;
      if (value !== (editingDevice[field] ?? null)) changes[field] = value;
    });
    if (!Object.keys(changes).length) {
      elements.deviceDialog.close();
      return;
    }
    url = `/api/devices/${encodeURIComponent(editingDevice.id)}`;
    options = { method: "PATCH", body: JSON.stringify(changes) };
  } else {
    options = { method: "POST", body: JSON.stringify(values) };
  }
  try {
    const response = await managementRequest(url, options);
    if (!response) return;
    elements.deviceDialog.close();
    await loadDevices();
  } catch (error) {
    window.alert(error.message);
  }
}

// Exclui o dispositivo editado, depois de confirmação. Usa managementRequest (pede a chave).
async function deleteDevice(device) {
  const confirmed = window.confirm(`Excluir o dispositivo ${device.name}?\n\nEle deixará de ser monitorado.`);
  if (!confirmed) return;
  try {
    const response = await managementRequest(`/api/devices/${encodeURIComponent(device.id)}`, { method: "DELETE" });
    if (!response) return;
    elements.deviceDialog.close();
    await loadDevices();
  } catch (error) {
    window.alert(error.message);
  }
}

/* ---------- Atualização periódica ---------- */

// Busca a lista de agentes e redesenha a tela. Roda ao abrir a página, a cada 30 s e ao voltar à aba.
// Não faz nada se já há uma busca em andamento ou se a aba está escondida (poupa a API).
async function refresh() {
  if (state.refreshing || document.hidden) return;
  state.refreshing = true;
  // Os dispositivos começam a carregar em paralelo, mas com tratamento próprio (loadDevices nunca
  // lança erro): se falharem, os hosts continuam normais e a tela não diz "Sem conexão com a API".
  const devicesLoad = loadDevices();
  try {
    // "no-store" impede o navegador de reaproveitar uma resposta antiga do cache.
    const response = await fetch("/api/agents", { cache: "no-store" });
    if (!response.ok) throw new Error(`Falha ao buscar agents: ${response.status}`);
    state.agents = await response.json();
    renderGroups();
    // O host aberto sumiu da lista (foi excluído, por exemplo): fecha o painel.
    if (state.selectedAgentId && !state.agents.some((agent) => agent.id === state.selectedAgentId)) {
      closeDetails();
    }
    renderHosts();
    // Espera os dispositivos antes de avisar o tv.js, para o modo apresentação já ver a lista nova.
    await devicesLoad;
    setUpdated(true);
    // O histórico dos gráficos é atualizado com menos frequência (3 min) que a lista (30 s).
    if (state.selectedAgentId && Date.now() - state.metricsLoadedAt >= METRICS_REFRESH_MS) {
      await refreshMetrics();
    }
  } catch (error) {
    console.error(error);
    setUpdated(false);
  } finally {
    // Libera a trava mesmo em caso de erro, senão nunca mais atualizaria.
    state.refreshing = false;
  }
}

/* ---------- Eventos (ligam os botões e campos da tela às funções acima) ---------- */

// Mudar o período (ex.: 1 h, 24 h) recarrega o histórico; se falhar, só mostra o aviso de conexão.
elements.period.addEventListener("change", () => loadMetrics().catch(() => setUpdated(false)));
elements.closeDetails.addEventListener("click", closeDetails);
// Editar e excluir ficam no cartão do host selecionado.
elements.editHost.addEventListener("click", () => {
  const agent = selectedAgent();
  if (agent) openEditDialog(agent);
});
elements.deleteHost.addEventListener("click", () => {
  const agent = selectedAgent();
  if (agent) deleteAgent(agent);
});
// Esc fecha o painel, exceto com uma janela de edição aberta (ali o Esc fecha só a janela).
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.selectedAgentId && !elements.editDialog.open && !elements.deviceDialog.open) closeDetails();
});
// Botão de tema: alterna entre claro e escuro.
elements.themeToggle.addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
// Filtro de grupo: volta à página 1 e, se o host aberto não pertence ao novo grupo, fecha o painel.
// O return evita um segundo renderHosts, pois closeDetails já redesenha.
elements.group.addEventListener("change", () => {
  state.selectedGroup = elements.group.value;
  state.page = 1;
  if (state.selectedAgentId && !groupAgents().some((agent) => agent.id === state.selectedAgentId)) {
    closeDetails();
    return;
  }
  renderHosts();
});
// Botões de status (todos/online/offline): só um fica ativo por vez.
elements.statusChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    state.status = chip.dataset.status;
    state.page = 1;
    elements.statusChips.forEach((item) => item.classList.toggle("active", item === chip));
    renderHosts();
  });
});
// Busca: filtra a cada tecla digitada (a lista é pequena, então não precisa de atraso).
elements.search.addEventListener("input", () => {
  state.search = elements.search.value.trim();
  state.page = 1;
  renderHosts();
});
// Paginação. O limite superior da página é corrigido dentro de renderHosts.
elements.previousPage.addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  renderHosts();
});
elements.nextPage.addEventListener("click", () => {
  state.page += 1;
  renderHosts();
});
// Cabeçalhos clicáveis: clicar na coluna já ordenada inverte o sentido; numa coluna nova,
// começa crescente para o nome (A-Z) e decrescente para as métricas (maiores primeiro).
document.querySelectorAll(".sort-button").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.sort;
    if (state.sortKey === key) state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
    else {
      state.sortKey = key;
      state.sortDirection = key === "name" ? "asc" : "desc";
    }
    state.page = 1;
    updateSortIndicators();
    renderHosts();
  });
});

// Salvar a edição: envia nome e grupo à API (PATCH) e atualiza a lista. preventDefault impede o
// envio normal do formulário, que recarregaria a página.
elements.editForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await managementRequest(
      `/api/agents/${encodeURIComponent(elements.editId.value)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          name: elements.editName.value.trim(),
          group: elements.editGroup.value.trim(),
        }),
      },
    );
    if (!response) return;
    elements.editDialog.close();
    await refresh();
  } catch (error) {
    window.alert(error.message);
  }
});
// Botões "X" e "Cancelar" da janela de edição.
elements.closeEdit.addEventListener("click", () => elements.editDialog.close());
elements.cancelEdit.addEventListener("click", () => elements.editDialog.close());

// Dispositivos: botão de adicionar, troca do tipo de verificação (mostra/esconde a porta), envio do
// formulário (preventDefault evita recarregar a página), exclusão e botões de fechar do diálogo.
elements.addDevice.addEventListener("click", () => openDeviceDialog());
elements.deviceMethod.addEventListener("change", syncDevicePort);
elements.deviceForm.addEventListener("submit", (event) => {
  event.preventDefault();
  saveDevice();
});
elements.deviceDelete.addEventListener("click", () => {
  if (editingDevice) deleteDevice(editingDevice);
});
elements.deviceClose.addEventListener("click", () => elements.deviceDialog.close());
elements.deviceCancel.addEventListener("click", () => elements.deviceDialog.close());

// Inicialização, quando a página termina de carregar: cria os gráficos, marca a ordenação inicial,
// busca os dados e agenda a atualização periódica. Ao voltar para a aba (visibilitychange), atualiza
// na hora, pois refresh() ignora chamadas enquanto a aba está escondida.
window.addEventListener("DOMContentLoaded", () => {
  initializeCharts();
  updateSortIndicators();
  // Mostra já o estado vazio do painel de dispositivos (e não uma tabela sem linhas) enquanto a API não responde.
  renderDevices();
  refresh();
  window.setInterval(refresh, AGENTS_REFRESH_MS);
  document.addEventListener("visibilitychange", refresh);
});
