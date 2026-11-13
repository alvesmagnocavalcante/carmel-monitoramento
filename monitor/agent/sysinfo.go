package main

import (
	"net"
	"regexp"
	"slices"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/host"
	gonet "github.com/shirou/gopsutil/v4/net"
)

// agentVersion é a versão do agente enviada à API. Definida no build com:
// -X main.agentVersion=1.2.0
var agentVersion = "1.2.0"

// systemInfoTTL é por quanto tempo os dados fixos do PC ficam em cache (1 hora);
// maxIPs limita quantos endereços IP vão no heartbeat.
const (
	systemInfoTTL = time.Hour
	maxIPs        = 8
)

// SystemInfo reúne o que raramente muda no computador. A API só grava quando mudar.
type SystemInfo struct {
	AgentVersion     string   `json:"agent_version"`
	Platform         string   `json:"platform"`
	PlatformVersion  string   `json:"platform_version"`
	Build            string   `json:"build"`
	Arch             string   `json:"arch"`
	CPUModel         string   `json:"cpu_model"`
	CPUCores         int      `json:"cpu_cores"`
	CPUThreads       int      `json:"cpu_threads"`
	MemoryTotalBytes uint64   `json:"memory_total_bytes"`
	IPs              []string `json:"ips"`
}

// systemInfoCache guarda a última coleta de SystemInfo e quando foi feita. O Mutex
// (trava) evita acesso simultâneo por mais de uma goroutine.
var systemInfoCache struct {
	sync.Mutex
	value       SystemInfo
	collectedAt time.Time
}

// buildPattern encontra o número da build do Windows no texto da versão do kernel.
// virtualInterfaces reconhece nomes de placas virtuais (VMs, Docker, VPNs, Bluetooth),
// cujos IPs não representam o endereço real do computador na rede.
var (
	buildPattern      = regexp.MustCompile(`Build\s+(\S+)`)
	virtualInterfaces = regexp.MustCompile(`(?i)vethernet|virtualbox|vmware|host-only|hyper-v|bluetooth|loopback|docker|wsl|tailscale|zerotier`)
)

// currentSystemInfo reaproveita a coleta por systemInfoTTL: CPU, SO e IPs mudam raramente.
func currentSystemInfo(memoryTotalBytes uint64) SystemInfo {
	cache := &systemInfoCache
	cache.Lock()
	defer cache.Unlock()
	if cache.collectedAt.IsZero() || time.Since(cache.collectedAt) >= systemInfoTTL {
		cache.value = collectSystemInfo()
		cache.collectedAt = time.Now()
	}
	info := cache.value
	// A memória total é sempre a leitura atual, pois já vem pronta da coleta de métricas.
	info.MemoryTotalBytes = memoryTotalBytes
	return info
}

// collectSystemInfo coleta de fato os dados do computador. Cada leitura que falhar deixa
// o campo vazio em vez de derrubar a coleta inteira.
func collectSystemInfo() SystemInfo {
	info := SystemInfo{AgentVersion: agentVersion, IPs: collectIPs()}
	if hostInfo, err := host.Info(); err == nil {
		info.Platform = hostInfo.Platform
		info.PlatformVersion = hostInfo.PlatformVersion
		info.Build = buildNumber(hostInfo.KernelVersion)
		info.Arch = hostInfo.KernelArch
	}
	if cpus, err := cpu.Info(); err == nil && len(cpus) > 0 {
		info.CPUModel = strings.TrimSpace(cpus[0].ModelName)
	}
	// Counts(false) conta núcleos físicos; Counts(true) conta threads (núcleos lógicos).
	if cores, err := cpu.Counts(false); err == nil {
		info.CPUCores = cores
	}
	if threads, err := cpu.Counts(true); err == nil {
		info.CPUThreads = threads
	}
	return info
}

// buildNumber extrai "26300.9711" de "10.0.26300.9711 Build 26300.9711".
func buildNumber(kernelVersion string) string {
	if match := buildPattern.FindStringSubmatch(kernelVersion); match != nil {
		return match[1]
	}
	return strings.TrimSpace(kernelVersion)
}

// collectIPs lista os IPv4 das placas de rede reais, com o IP principal primeiro e
// limitado a maxIPs. A ordenação alfabética deixa o resultado estável entre coletas,
// evitando que a API veja "mudança" só por troca de ordem.
func collectIPs() []string {
	interfaces, err := gonet.Interfaces()
	if err != nil {
		return nil
	}
	var ips []string
	for _, iface := range interfaces {
		if !isUsableInterface(iface.Name, iface.Flags) {
			continue
		}
		for _, addr := range iface.Addrs {
			if ip := usableIPv4(addr.Addr); ip != "" {
				ips = append(ips, ip)
			}
		}
	}
	sort.Strings(ips)
	ips = primaryFirst(ips, primaryIPv4())
	if len(ips) > maxIPs {
		ips = ips[:maxIPs]
	}
	return ips
}

// primaryIPv4 devolve o IP que o sistema usa para sair para a rede, conforme a tabela de
// rotas. Um UDP "conectado" não envia pacote algum; só faz o sistema escolher a origem.
// O endereço 192.0.2.1 é reservado para documentação e nunca existe de verdade.
func primaryIPv4() string {
	conn, err := net.Dial("udp4", "192.0.2.1:9")
	if err != nil {
		return ""
	}
	defer conn.Close()
	if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok {
		return usableIPv4(addr.IP.String() + "/32")
	}
	return ""
}

// primaryFirst coloca o IP principal na frente da lista, se ele estiver nela.
func primaryFirst(ips []string, primary string) []string {
	index := slices.Index(ips, primary)
	if primary == "" || index <= 0 {
		return ips
	}
	return append([]string{primary}, slices.Delete(slices.Clone(ips), index, index+1)...)
}

// isUsableInterface descarta placas desligadas, loopback e adaptadores virtuais.
func isUsableInterface(name string, flags []string) bool {
	return slices.Contains(flags, "up") &&
		!slices.Contains(flags, "loopback") &&
		!virtualInterfaces.MatchString(name)
}

// usableIPv4 devolve o IPv4 de um endereço "ip/máscara", ou "" se não servir.
// Descarta IPv6, loopback (127.x) e link-local (169.254.x, que o Windows se atribui
// quando não consegue um IP da rede).
func usableIPv4(cidr string) string {
	ip, _, err := net.ParseCIDR(cidr)
	if err != nil {
		return ""
	}
	v4 := ip.To4()
	if v4 == nil || v4.IsLoopback() || v4.IsLinkLocalUnicast() {
		return ""
	}
	return v4.String()
}
