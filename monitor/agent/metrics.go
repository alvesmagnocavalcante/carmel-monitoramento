package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/disk"
	"github.com/shirou/gopsutil/v4/host"
	"github.com/shirou/gopsutil/v4/mem"
	gonet "github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/sensors"
)

// Heartbeat é o "sinal de vida" enviado à API a cada ciclo, com as métricas do momento.
// Os nomes entre aspas são os campos do JSON esperados pela API.
// TempC é ponteiro porque pode não existir (vira null); System e Volumes são omitidos se vazios.
type Heartbeat struct {
	AgentID       string      `json:"agent_id"`
	Group         string      `json:"group"`
	Timestamp     string      `json:"timestamp"`
	CPUPercent    float64     `json:"cpu_percent"`
	MemoryPercent float64     `json:"memory_percent"`
	DiskPercent   float64     `json:"disk_percent"`
	TempC         *float64    `json:"temp_c"`
	NetBytesSent  uint64      `json:"net_bytes_sent"`
	NetBytesRecv  uint64      `json:"net_bytes_recv"`
	UptimeS       uint64      `json:"uptime_s"`
	Hostname      string      `json:"hostname"`
	OS            string      `json:"os"`
	IntervalS     int         `json:"interval_seconds"`
	System        *SystemInfo `json:"system,omitempty"`
	Volumes       []Volume    `json:"volumes,omitempty"`
}

// collectMetrics lê CPU, memória, disco, rede e uptime e monta um Heartbeat.
// Se qualquer leitura essencial falhar, devolve erro e nada é enviado nesse ciclo.
// Temperatura, informações do sistema e volumes são opcionais e não causam erro.
// O horário do heartbeat é gravado em UTC, para não depender do fuso do PC.
func collectMetrics(agentID, group string, intervalSeconds int) (Heartbeat, error) {
	// Mede o uso de CPU durante 1 s (por isso cada coleta leva ao menos 1 s);
	// false = um único valor para o computador todo, não um por núcleo.
	cpuValues, err := cpu.Percent(time.Second, false)
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect CPU: %w", err)
	}
	if len(cpuValues) == 0 {
		return Heartbeat{}, fmt.Errorf("collect CPU: no values returned")
	}

	memory, err := mem.VirtualMemory()
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect memory: %w", err)
	}

	// DiskPercent refere-se só ao disco principal; todos os discos vão em Volumes.
	diskStats, err := disk.Usage(mainPartition())
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect disk: %w", err)
	}

	// Contadores de rede somados de todas as placas (false). São totais acumulados
	// desde a inicialização do PC, não a velocidade atual.
	network, err := gonet.IOCounters(false)
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect network: %w", err)
	}
	if len(network) == 0 {
		return Heartbeat{}, fmt.Errorf("collect network: no values returned")
	}

	uptime, err := host.Uptime()
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect uptime: %w", err)
	}

	hostname, err := os.Hostname()
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect hostname: %w", err)
	}

	// Dados fixos do computador (modelo de CPU, IPs etc.) vêm de um cache de 1 hora.
	system := currentSystemInfo(memory.Total)
	return Heartbeat{
		System:        &system,
		Volumes:       collectVolumes(),
		AgentID:       agentID,
		Group:         group,
		Timestamp:     time.Now().UTC().Format(time.RFC3339Nano),
		CPUPercent:    cpuValues[0],
		MemoryPercent: memory.UsedPercent,
		DiskPercent:   diskStats.UsedPercent,
		TempC:         collectTemperature(),
		NetBytesSent:  network[0].BytesSent,
		NetBytesRecv:  network[0].BytesRecv,
		UptimeS:       uptime,
		Hostname:      hostname,
		OS:            runtime.GOOS,
		IntervalS:     intervalSeconds,
	}, nil
}

// mainPartition devolve a partição principal. No Windows é o drive onde o executável
// está instalado (cai para C:\ se não der para descobrir); nos demais sistemas, "/".
func mainPartition() string {
	if runtime.GOOS != "windows" {
		return "/"
	}
	executable, err := os.Executable()
	if err == nil {
		if volume := filepath.VolumeName(executable); volume != "" {
			return volume + `\`
		}
	}
	return `C:\`
}

// collectTemperature devolve a maior temperatura entre os sensores, ou nil se o computador
// não expuser sensores (comum em PCs e máquinas virtuais). Nunca causa erro de coleta.
func collectTemperature() *float64 {
	temperatures, err := sensors.SensorsTemperatures()
	if err != nil || len(temperatures) == 0 {
		return nil
	}

	var hottest *float64
	for _, sensor := range temperatures {
		// Abaixo do zero absoluto é leitura inválida do sensor; ignora.
		if sensor.Temperature < -273.15 {
			continue
		}
		if hottest == nil || sensor.Temperature > *hottest {
			value := sensor.Temperature
			hottest = &value
		}
	}
	return hottest
}
