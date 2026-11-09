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

type Heartbeat struct {
	AgentID       string   `json:"agent_id"`
	Group         string   `json:"group"`
	Timestamp     string   `json:"timestamp"`
	CPUPercent    float64  `json:"cpu_percent"`
	MemoryPercent float64  `json:"memory_percent"`
	DiskPercent   float64  `json:"disk_percent"`
	TempC         *float64 `json:"temp_c"`
	NetBytesSent  uint64   `json:"net_bytes_sent"`
	NetBytesRecv  uint64   `json:"net_bytes_recv"`
	UptimeS       uint64   `json:"uptime_s"`
	Hostname      string   `json:"hostname"`
	OS            string   `json:"os"`
	IntervalS     int      `json:"interval_seconds"`
}

func collectMetrics(agentID, group string, intervalSeconds int) (Heartbeat, error) {
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

	diskStats, err := disk.Usage(mainPartition())
	if err != nil {
		return Heartbeat{}, fmt.Errorf("collect disk: %w", err)
	}

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

	return Heartbeat{
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

func collectTemperature() *float64 {
	temperatures, err := sensors.SensorsTemperatures()
	if err != nil || len(temperatures) == 0 {
		return nil
	}

	var hottest *float64
	for _, sensor := range temperatures {
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
