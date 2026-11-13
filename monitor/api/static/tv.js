/* Modo apresentação: visão ampla e somente leitura para telas grandes (TV).
   Depende de app.js (state, groupAgents, fullVolumes, formatRate, meterBar, setTheme...).
   URL: ?tv=1  &grupo=Nome (só um grupo)  &rotacao=20 (alterna entre grupos a cada 20 s). */

// Tempo parado (em milissegundos) até o cursor e os botões sumirem da tela.
const TV_IDLE_MS = 3000;
// Limites do tamanho da fonte (em pixels): nunca menor que o mínimo (legibilidade)
// nem maior que o máximo (evita letras gigantes quando há poucas máquinas).
const TV_MIN_FONT = 10;
const TV_MAX_FONT = 26;
// Tempo padrão (segundos) de cada grupo na rotação; abaixo do mínimo a URL é ignorada
// para a tela não ficar trocando rápido demais.
const TV_ROTATION_SECONDS = 20;
const TV_MIN_ROTATION_SECONDS = 5;
// Medidas em "em" (1em = tamanho de fonte do modo), usadas para calcular o ajuste à tela.
// Largura e altura de cada cartão de máquina, altura do título do grupo e espaço entre cartões.
// Precisam ser iguais às medidas do CSS (.tv-grid, .tv-tile, .tv-group h3), senão o cálculo erra.
const TV_TILE_WIDTH = 21;
const TV_TILE_HEIGHT = 6.9;
const TV_GROUP_HEIGHT = 2.4;
const TV_GAP = 0.8;
// Folga máxima de altura que pode ser somada a cada linha de cartões (em em), para sobrar espaço sem esticar demais.
const TV_MAX_EXTRA_EM = 1.6;

// Lidos uma vez, antes de qualquer reescrita da URL.
const tvParams = new URLSearchParams(window.location.search);

// Referências aos elementos do modo apresentação (ids do index.html) e o estado do modo:
// timers ativos, tempo de rotação, grupo mostrado e o "wake lock" (mantém a tela acesa).
const tv = {
  root: document.querySelector("#tv"),
  body: document.querySelector("#tv-body"),
  toggle: document.querySelector("#tv-toggle"),
  exit: document.querySelector("#tv-exit"),
  rotate: document.querySelector("#tv-rotate"),
  themeToggle: document.querySelector("#tv-theme"),
  total: document.querySelector("#tv-total"),
  online: document.querySelector("#tv-online"),
  offline: document.querySelector("#tv-offline"),
  diskFull: document.querySelector("#tv-disk-full"),
  clock: document.querySelector("#tv-clock"),
  date: document.querySelector("#tv-date"),
  updated: document.querySelector("#tv-updated"),
  clockTimer: null,
  idleTimer: null,
  resizeTimer: null,
  rotationTimer: null,
  rotation: 0,
  rotationIndex: 0,
  wakeLock: null,
  groupApplied: false,
};

/* ---------- Conteúdo ---------- */

// 0 = offline, 1 = disco cheio, 2 = normal: o que precisa de atenção vem primeiro.
function tvPriority(agent) {
  if (agent.status === "offline") return 0;
  return needsAttention(agent) ? 1 : 2;
}

// Cria uma barrinha de uso (CPU, MEM ou Disco): nome, percentual e a barra de progresso.
// Sem dado válido mostra "—" em vez de 0%, para não parecer que está tudo bem.
function tvBar(label, percent) {
  const wrapper = document.createElement("div");
  wrapper.className = "tv-bar";
  const name = document.createElement("span");
  name.className = "tv-bar-label";
  name.textContent = label;
  const value = document.createElement("span");
  value.className = "tv-bar-value";
  value.textContent = Number.isFinite(percent) ? `${Math.round(percent)}%` : "—";
  wrapper.append(name, value, meterBar(percent, HIGH_USAGE_PERCENT));
  return wrapper;
}

// Monta o cartão de uma máquina: cabeçalho (nome e avisos), três barras e rodapé (rede e uptime).
// As classes online/offline/full/alert controlam a cor da borda e o pulso no CSS.
function tvTile(agent) {
  const full = fullVolumes(agent);
  const metric = agent.latest_metric;
  const tile = document.createElement("article");
  tile.className = `tv-tile ${agent.status}${full.length ? " full" : ""}${needsAttention(agent) ? " alert" : ""}`;

  // Cabeçalho: bolinha de status, nome (com dica ao passar o mouse) e etiquetas de aviso.
  const head = document.createElement("div");
  head.className = "tv-tile-head";
  const dot = document.createElement("span");
  dot.className = "tv-dot";
  const name = document.createElement("strong");
  name.textContent = agent.name;
  name.title = agent.name;
  head.append(dot, name);
  // Cada aviso é [texto da etiqueta, dica detalhada]; o disco cheio lista os volumes afetados.
  const flags = [];
  if (full.length) flags.push(["Disco cheio", full.map((volume) => `${volume.mount} ${Math.round(volume.percent)}%`).join(" · ")]);
  if (highMemory(agent)) flags.push(["Memória alta", `${Math.round(metric.memory_percent)}% em uso`]);
  flags.forEach(([text, title]) => {
    const flag = document.createElement("span");
    flag.className = "tv-flag";
    flag.textContent = text;
    flag.title = title;
    head.append(flag);
  });

  const bars = document.createElement("div");
  bars.className = "tv-bars";
  bars.append(
    tvBar("CPU", metric?.cpu_percent),
    tvBar("MEM", metric?.memory_percent),
    tvBar("Disco", metric?.disk_percent),
  );

  // Rodapé: velocidade de rede (↓ recebe, ↑ envia) e, à direita, uptime ou há quanto tempo está offline.
  const foot = document.createElement("div");
  foot.className = "tv-foot";
  const network = document.createElement("span");
  network.textContent = `↓ ${formatRate(agent.net_recv_mbps)}  ↑ ${formatRate(agent.net_sent_mbps)}`;
  const status = document.createElement("span");
  status.textContent = agent.status === "offline"
    ? `Offline · ${relativeTime(agent.last_seen)}`
    : (metric ? `Uptime ${formatDuration(metric.uptime_s)}` : "");
  foot.append(network, status);

  tile.append(head, bars, foot);
  return tile;
}

// Monta a seção de um grupo: título com a quantidade de máquinas, a posição na rotação (ex.: 2/5,
// só quando está rotacionando) e a grade de cartões.
function tvGroupSection(name, agents, position) {
  const section = document.createElement("section");
  section.className = "tv-group";
  const title = document.createElement("h3");
  title.textContent = name || "Sem grupo";
  const count = document.createElement("span");
  count.textContent = agents.length;
  title.append(count);
  if (position) {
    const page = document.createElement("em");
    page.textContent = position;
    title.append(page);
  }
  const grid = document.createElement("div");
  grid.className = "tv-grid";
  grid.append(...agents.map(tvTile));
  section.append(title, grid);
  return section;
}

// Maior fonte em que todos os grupos cabem na tela sem rolar, e quanto sobra de altura por linha.
// groupSizes = quantidade de máquinas de cada grupo visível. Testa fontes do maior para o menor
// (de 0,5 em 0,5 px): para cada uma calcula colunas, linhas e altura usada, e para na primeira que cabe.
function tvFit(groupSizes) {
  const width = tv.body.clientWidth;
  const height = tv.body.clientHeight;
  for (let font = TV_MAX_FONT; font >= TV_MIN_FONT; font -= 0.5) {
    const gap = TV_GAP * font;
    // Quantos cartões cabem lado a lado (o gap extra compensa o fato de haver um espaço a menos que cartões).
    const columns = Math.max(1, Math.floor((width + gap) / (TV_TILE_WIDTH * font + gap)));
    const rows = groupSizes.reduce((total, count) => total + Math.ceil(count / columns), 0);
    // Altura total: títulos dos grupos + todas as linhas de cartões (cada uma com seu espaçamento).
    const used = groupSizes.length * TV_GROUP_HEIGHT * font + rows * (TV_TILE_HEIGHT * font + gap);
    if (used <= height) {
      return { font, extra: Math.min((height - used) / rows, TV_MAX_EXTRA_EM * font) };
    }
  }
  // Nem a menor fonte cabe: usa o mínimo e aceita rolar (a barra de rolagem fica escondida no CSS).
  return { font: TV_MIN_FONT, extra: 0 };
}

// Redesenha a tela inteira do modo apresentação: contadores do topo, grupos ordenados e cartões.
// Chamada a cada atualização de dados, a cada troca de grupo da rotação e ao redimensionar a janela.
function renderTv() {
  if (!state.tv) return;
  // groupAgents() já respeita o grupo escolhido (inclusive o vindo de ?grupo= na URL).
  const agents = groupAgents();
  const online = agents.filter((agent) => agent.status === "online").length;
  tv.total.textContent = agents.length;
  tv.online.textContent = online;
  tv.offline.textContent = agents.length - online;
  tv.diskFull.textContent = agents.filter((agent) => fullVolumes(agent).length).length;
  tv.updated.textContent = elements.updatedAt.textContent;
  tv.updated.classList.toggle("error", elements.updatedAt.classList.contains("error"));

  // Agrupa por grupo, ordena os grupos por nome e, dentro de cada um, o que precisa de atenção vem primeiro.
  const groups = new Map();
  agents.forEach((agent) => groups.set(agent.group, [...(groups.get(agent.group) || []), agent]));
  const ordered = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, "pt-BR"));
  ordered.forEach(([, members]) => members.sort((a, b) =>
    tvPriority(a) - tvPriority(b) || a.name.localeCompare(b.name, "pt-BR")));

  if (!ordered.length) {
    const empty = document.createElement("p");
    empty.className = "tv-empty";
    empty.textContent = "Nenhuma máquina para exibir.";
    tv.body.replaceChildren(empty);
    return;
  }

  // Em rotação, mostra um grupo por vez (fonte bem maior); senão, todos juntos.
  const rotating = tv.rotation > 0 && ordered.length > 1;
  if (rotating) tv.rotationIndex %= ordered.length;
  const visible = rotating ? [ordered[tv.rotationIndex]] : ordered;
  tv.body.replaceChildren(...visible.map(([name, members]) =>
    tvGroupSection(name, members, rotating ? `${tv.rotationIndex + 1}/${ordered.length}` : "")));
  // Entrega ao CSS o tamanho da fonte e a folga de altura calculados (variáveis --tv-font e --tv-extra).
  // Precisa vir depois de inserir os grupos, pois tvFit lê o tamanho real da área.
  const { font, extra } = tvFit(visible.map(([, members]) => members.length));
  tv.body.style.setProperty("--tv-font", `${font}px`);
  tv.body.style.setProperty("--tv-extra", `${Math.round(extra)}px`);
}

/* ---------- Entrar, sair e rotação ---------- */

// Atualiza relógio e data do cabeçalho (data em português, com a primeira letra maiúscula).
function updateTvClock() {
  const now = new Date();
  const date = now.toLocaleDateString("pt-BR", { weekday: "long", day: "numeric", month: "long" });
  tv.clock.textContent = now.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  tv.date.textContent = date.charAt(0).toUpperCase() + date.slice(1);
}

// Espelha o modo na barra de endereço (?tv=1&grupo=...&rotacao=...) sem recarregar a página,
// assim dá para favoritar ou recarregar a TV e voltar ao mesmo estado. Ao sair, remove os parâmetros.
function setTvUrl(active) {
  const url = new URL(window.location.href);
  ["tv", "grupo", "rotacao"].forEach((key) => url.searchParams.delete(key));
  if (active) {
    url.searchParams.set("tv", "1");
    if (state.selectedGroup !== "all") url.searchParams.set("grupo", state.selectedGroup);
    if (tv.rotation) url.searchParams.set("rotacao", tv.rotation);
  }
  window.history.replaceState(null, "", url);
}

// Liga (seconds > 0) ou desliga (0) a rotação entre grupos. Reinicia o timer para o novo intervalo
// e volta ao primeiro grupo.
function setRotation(seconds) {
  tv.rotation = seconds;
  tv.rotationIndex = 0;
  window.clearInterval(tv.rotationTimer);
  if (seconds) {
    tv.rotationTimer = window.setInterval(() => {
      tv.rotationIndex += 1;
      renderTv();
    }, seconds * 1000);
  }
  tv.rotate.classList.toggle("active", seconds > 0);
  tv.rotate.setAttribute("aria-pressed", String(seconds > 0));
  setTvUrl(state.tv);
  renderTv();
}

// Evita que a tela apague enquanto o modo está ligado (quando o navegador permite).
// "Wake lock": pedido ao navegador para não apagar a tela (importante em TV sem ninguém mexendo).
async function keepScreenAwake() {
  try {
    tv.wakeLock = await navigator.wakeLock?.request("screen");
  } catch (error) { /* sem suporte ou negado: segue sem */ }
}

// Mostra cursor e botões e agenda o sumiço após TV_IDLE_MS sem movimento (a classe "awake" é tratada no CSS).
function wakeTvControls() {
  tv.root.classList.add("awake");
  window.clearTimeout(tv.idleTimer);
  tv.idleTimer = window.setTimeout(() => tv.root.classList.remove("awake"), TV_IDLE_MS);
}

// Entra no modo apresentação. fullscreen = pedir tela cheia (só funciona após um clique do usuário,
// por isso a abertura direta via URL passa false). rotation = segundos por grupo (0 = sem rotação).
async function enterTv({ fullscreen, rotation = 0 }) {
  if (state.tv) return;
  if (state.selectedAgentId) closeDetails();
  state.tv = true;
  tv.root.hidden = false;
  document.body.classList.add("tv-active");
  updateTvClock();
  tv.clockTimer = window.setInterval(updateTvClock, 15_000);
  setRotation(rotation);
  wakeTvControls();
  keepScreenAwake();
  if (fullscreen && document.fullscreenEnabled) {
    try { await document.documentElement.requestFullscreen(); } catch (error) { /* o navegador pode negar */ }
    renderTv(); // a área útil mudou
  }
}

// Sai do modo: esconde a tela, para todos os timers, limpa a URL e libera tela cheia e wake lock.
function exitTv() {
  if (!state.tv) return;
  state.tv = false;
  tv.root.hidden = true;
  document.body.classList.remove("tv-active");
  window.clearInterval(tv.clockTimer);
  window.clearInterval(tv.rotationTimer);
  window.clearTimeout(tv.idleTimer);
  tv.rotation = 0;
  setTvUrl(false);
  tv.wakeLock?.release().catch(() => {});
  tv.wakeLock = null;
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}

// ?grupo=Nome na URL mostra só aquele grupo (útil para uma TV por hotel).
function applyGroupFromUrl() {
  if (tv.groupApplied) return;
  tv.groupApplied = true;
  const wanted = tvParams.get("grupo");
  if (wanted && state.agents.some((agent) => agent.group === wanted)) {
    state.selectedGroup = wanted;
    renderGroups();
    renderHosts();
    setTvUrl(state.tv);
  }
}

/* ---------- Eventos ---------- */

// Botões do modo: abrir, sair, ligar/desligar rotação e alternar tema.
tv.toggle.addEventListener("click", () => enterTv({ fullscreen: true }));
tv.exit.addEventListener("click", exitTv);
tv.rotate.addEventListener("click", () => setRotation(tv.rotation ? 0 : TV_ROTATION_SECONDS));
tv.themeToggle.addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
// Qualquer interação reexibe os controles; "passive" avisa o navegador que não bloqueamos a rolagem.
["mousemove", "pointerdown", "keydown", "touchstart"].forEach((type) => {
  tv.root.addEventListener(type, wakeTvControls, { passive: true });
});
// Esc sai do modo (o navegador já sai da tela cheia sozinho com Esc).
document.addEventListener("keydown", (event) => {
  if (state.tv && event.key === "Escape") exitTv();
});
// Evento disparado pelo app.js a cada vez que chegam dados novos da API.
document.addEventListener("agents-updated", () => {
  applyGroupFromUrl();
  renderTv();
});
// O navegador solta o wake lock quando a aba fica oculta; ao voltar, pede de novo.
document.addEventListener("visibilitychange", () => {
  if (state.tv && document.visibilityState === "visible") keepScreenAwake();
});
// Ao redimensionar, recalcula o ajuste, mas só 150 ms após parar (debounce) para não recalcular o tempo todo.
window.addEventListener("resize", () => {
  window.clearTimeout(tv.resizeTimer);
  tv.resizeTimer = window.setTimeout(renderTv, 150);
});

// Abertura direta por URL (?tv=1): entra sem pedir tela cheia (o navegador só permite após clique).
// Rotação inválida ou menor que o mínimo vira 0 (desligada).
window.addEventListener("DOMContentLoaded", () => {
  if (tvParams.get("tv") !== "1") return;
  const seconds = Number(tvParams.get("rotacao"));
  enterTv({ fullscreen: false, rotation: seconds >= TV_MIN_ROTATION_SECONDS ? seconds : 0 });
});
