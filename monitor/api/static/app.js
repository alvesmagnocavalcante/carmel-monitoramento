const PAGE_SIZE = 25;
const AGENTS_REFRESH_MS = 30_000;
const METRICS_REFRESH_MS = 180_000;
const HIGH_USAGE_PERCENT = 85;

const state = {
  agents: [],
  selectedAgentId: null,
  selectedGroup: "all",
  status: "all",
  search: "",
  sortKey: "name",
  sortDirection: "asc",
  page: 1,
  charts: {},
  refreshing: false,
  metricsLoadedAt: 0,
};

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
};

/* ---------- Formatação ---------- */

function formatPercent(value) {
  return Number.isFinite(value) ? `${value.toFixed(1)}%` : "—";
}

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

function formatTime(value) {
  return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function setUpdated(ok) {
  elements.updatedAt.classList.toggle("error", !ok);
  elements.updatedAt.textContent = ok
    ? `Atualizado às ${new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`
    : "Sem conexão com a API";
}

/* ---------- Filtros e ordenação ---------- */

function groupAgents() {
  if (state.selectedGroup === "all") return state.agents;
  return state.agents.filter((agent) => agent.group === state.selectedGroup);
}

function metricForSort(agent, key) {
  if (key === "name") return agent.name.toLocaleLowerCase("pt-BR");
  if (key === "status") return agent.status;
  if (key === "last_seen") return agent.last_seen ? new Date(agent.last_seen).getTime() : 0;
  const fields = { cpu: "cpu_percent", memory: "memory_percent", disk: "disk_percent" };
  return agent.latest_metric?.[fields[key]] ?? -1;
}

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

function createCell(label, content, className) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  cell.className = className;
  if (content instanceof Node) cell.append(content);
  else cell.textContent = content;
  return cell;
}

function metricCell(label, value, className) {
  const wrapper = document.createElement("div");
  wrapper.className = "metric";
  const text = document.createElement("span");
  text.className = "metric-value";
  text.textContent = formatPercent(value);
  const meter = document.createElement("div");
  meter.className = `meter${value >= HIGH_USAGE_PERCENT ? " high" : ""}`;
  const fill = document.createElement("span");
  fill.style.width = `${Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0}%`;
  meter.append(fill);
  wrapper.append(text, meter);
  return createCell(label, wrapper, className);
}

function actionButton(label, className, onClick) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function hostRow(agent) {
  const row = document.createElement("tr");
  if (agent.id === state.selectedAgentId) row.classList.add("selected");

  const hostButton = document.createElement("button");
  hostButton.type = "button";
  hostButton.className = "host-name-button";
  const hostName = document.createElement("strong");
  hostName.textContent = agent.name;
  const hostMeta = document.createElement("span");
  hostMeta.textContent = [agent.os, agent.group].filter(Boolean).join(" · ");
  hostButton.append(hostName, hostMeta);
  hostButton.addEventListener("click", () => selectAgent(agent.id));

  const badge = document.createElement("span");
  badge.className = `badge ${agent.status}`;
  badge.textContent = agent.status;

  const actions = document.createElement("div");
  actions.className = "row-actions";
  actions.append(
    actionButton("Editar", "icon-button", () => openEditDialog(agent)),
    actionButton("Excluir", "icon-button danger", () => deleteAgent(agent)),
  );

  const metric = agent.latest_metric;
  row.append(
    createCell("Host", hostButton, "c-host"),
    createCell("Status", badge, "c-status"),
    metricCell("CPU", metric?.cpu_percent, "c-cpu"),
    metricCell("Memória", metric?.memory_percent, "c-mem"),
    metricCell("Disco", metric?.disk_percent, "c-disk"),
    createCell("Último contato", relativeTime(agent.last_seen), "c-last last-contact"),
    createCell("Ações", actions, "c-act"),
  );
  row.addEventListener("click", (event) => {
    if (!event.target.closest("button")) selectAgent(agent.id);
  });
  return row;
}

function groupHeader(group, count) {
  const row = document.createElement("tr");
  row.className = "group-header";
  const cell = document.createElement("td");
  cell.colSpan = 7;

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

function renderSummary() {
  const grouped = groupAgents();
  const online = grouped.filter((agent) => agent.status === "online").length;
  elements.total.textContent = grouped.length;
  elements.online.textContent = online;
  elements.offline.textContent = grouped.length - online;
  elements.onlineShare.textContent = grouped.length
    ? `${Math.round((online / grouped.length) * 100)}% do parque`
    : "";
}

function renderHosts() {
  renderSummary();

  const agents = filteredAgents();
  const pages = Math.max(1, Math.ceil(agents.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const start = (state.page - 1) * PAGE_SIZE;
  const pageAgents = agents.slice(start, start + PAGE_SIZE);
  const groupCounts = new Map();
  agents.forEach((agent) => groupCounts.set(agent.group, (groupCounts.get(agent.group) || 0) + 1));
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

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function styleChart(chart) {
  const muted = cssVar("--muted");
  chart.data.datasets.forEach((dataset, index) => {
    const color = cssVar(chart.$colorVars[index]);
    dataset.borderColor = color;
    dataset.backgroundColor = `${color}22`;
  });
  chart.options.scales.x.ticks.color = muted;
  chart.options.scales.y.ticks.color = muted;
  chart.options.scales.y.grid.color = cssVar("--line");
  chart.options.plugins.legend.labels.color = cssVar("--ink-soft");
  chart.update("none");
}

function createLineChart(canvasId, colorVars, labels) {
  const chart = new Chart(document.querySelector(`#${canvasId}`), {
    type: "line",
    data: { labels: [], datasets: labels.map((label) => ({
      label,
      data: [],
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.3,
      fill: labels.length === 1,
    })) },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false, mode: "index" },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 5 } },
        y: { beginAtZero: true, suggestedMax: labels.length === 1 ? 100 : undefined, grid: {}, ticks: {} },
      },
      plugins: { legend: { display: labels.length > 1, labels: { boxWidth: 10, boxHeight: 10 } } },
    },
  });
  chart.$colorVars = colorVars;
  styleChart(chart);
  return chart;
}

function initializeCharts() {
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  state.charts.cpu = createLineChart("cpu-chart", ["--chart-cpu"], ["CPU"]);
  state.charts.memory = createLineChart("memory-chart", ["--chart-memory"], ["Memória"]);
  state.charts.disk = createLineChart("disk-chart", ["--chart-disk"], ["Disco"]);
  state.charts.network = createLineChart("network-chart", ["--chart-net-in", "--chart-net-out"], ["Recebido", "Enviado"]);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch (error) { /* sem armazenamento local */ }
  Object.values(state.charts).forEach(styleChart);
}

function updateChart(chart, labels, series) {
  chart.data.labels = labels;
  chart.data.datasets.forEach((dataset, index) => { dataset.data = series[index]; });
  chart.update("none");
}

function networkRates(metrics) {
  const labels = [];
  const received = [];
  const sent = [];
  for (let index = 1; index < metrics.length; index += 1) {
    const previous = metrics[index - 1];
    const current = metrics[index];
    const elapsed = (new Date(current.received_at) - new Date(previous.received_at)) / 1000;
    if (elapsed <= 0) continue;
    labels.push(formatTime(current.received_at));
    received.push(Math.max(0, current.net_bytes_recv - previous.net_bytes_recv) * 8 / elapsed / 1_000_000);
    sent.push(Math.max(0, current.net_bytes_sent - previous.net_bytes_sent) * 8 / elapsed / 1_000_000);
  }
  return { labels, received, sent };
}

async function refreshMetrics() {
  if (!state.selectedAgentId) return;
  const selectedId = state.selectedAgentId;
  const response = await fetch(`/api/agents/${encodeURIComponent(selectedId)}/metrics?minutes=${elements.period.value}`);
  if (!response.ok) throw new Error(`Falha ao buscar métricas: ${response.status}`);
  const metrics = await response.json();
  if (selectedId !== state.selectedAgentId) return;
  state.metricsLoadedAt = Date.now();
  const labels = metrics.map((metric) => formatTime(metric.received_at));
  updateChart(state.charts.cpu, labels, [metrics.map((metric) => metric.cpu_percent)]);
  updateChart(state.charts.memory, labels, [metrics.map((metric) => metric.memory_percent)]);
  updateChart(state.charts.disk, labels, [metrics.map((metric) => metric.disk_percent)]);
  const network = networkRates(metrics);
  updateChart(state.charts.network, network.labels, [network.received, network.sent]);
}

/* ---------- Histórico: abre ao clicar no host e recolhe ao clicar de novo ---------- */

function setDetailsOpen(open) {
  elements.details.hidden = !open;
  elements.layout.classList.toggle("has-details", open);
  document.body.classList.toggle("sheet-open", open);
}

function closeDetails() {
  state.selectedAgentId = null;
  setDetailsOpen(false);
  renderHosts();
}

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
  renderHosts();
  try {
    await refreshMetrics();
  } catch (error) {
    console.error(error);
    setUpdated(false);
  }
}

/* ---------- Edição e exclusão ---------- */

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
  if (response.status === 401) {
    sessionStorage.removeItem("managementKey");
    window.alert("Token inválido. Copie o campo token do config.json do agente.");
    return null;
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Falha na operação: ${response.status}`);
  }
  return response;
}

function openEditDialog(agent) {
  elements.editId.value = agent.id;
  elements.editName.value = agent.name;
  elements.editGroup.value = agent.group;
  elements.editDialog.showModal();
  elements.editName.focus();
}

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

/* ---------- Atualização periódica ---------- */

async function refresh() {
  if (state.refreshing || document.hidden) return;
  state.refreshing = true;
  try {
    const response = await fetch("/api/agents", { cache: "no-store" });
    if (!response.ok) throw new Error(`Falha ao buscar agents: ${response.status}`);
    state.agents = await response.json();
    renderGroups();
    if (state.selectedAgentId && !state.agents.some((agent) => agent.id === state.selectedAgentId)) {
      closeDetails();
    }
    renderHosts();
    setUpdated(true);
    if (state.selectedAgentId && Date.now() - state.metricsLoadedAt >= METRICS_REFRESH_MS) {
      await refreshMetrics();
    }
  } catch (error) {
    console.error(error);
    setUpdated(false);
  } finally {
    state.refreshing = false;
  }
}

/* ---------- Eventos ---------- */

elements.period.addEventListener("change", () => refreshMetrics().catch(() => setUpdated(false)));
elements.closeDetails.addEventListener("click", closeDetails);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.selectedAgentId && !elements.editDialog.open) closeDetails();
});
elements.themeToggle.addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
elements.group.addEventListener("change", () => {
  state.selectedGroup = elements.group.value;
  state.page = 1;
  if (state.selectedAgentId && !groupAgents().some((agent) => agent.id === state.selectedAgentId)) {
    closeDetails();
    return;
  }
  renderHosts();
});
elements.statusChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    state.status = chip.dataset.status;
    state.page = 1;
    elements.statusChips.forEach((item) => item.classList.toggle("active", item === chip));
    renderHosts();
  });
});
elements.search.addEventListener("input", () => {
  state.search = elements.search.value.trim();
  state.page = 1;
  renderHosts();
});
elements.previousPage.addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  renderHosts();
});
elements.nextPage.addEventListener("click", () => {
  state.page += 1;
  renderHosts();
});
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
elements.closeEdit.addEventListener("click", () => elements.editDialog.close());
elements.cancelEdit.addEventListener("click", () => elements.editDialog.close());

window.addEventListener("DOMContentLoaded", () => {
  initializeCharts();
  updateSortIndicators();
  refresh();
  window.setInterval(refresh, AGENTS_REFRESH_MS);
  document.addEventListener("visibilitychange", refresh);
});
