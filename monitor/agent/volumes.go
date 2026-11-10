package main

import (
	"strings"

	"github.com/shirou/gopsutil/v4/disk"
)

const maxVolumes = 16

// Volume é um disco fixo do computador, com uso em bytes.
type Volume struct {
	Mount      string `json:"mount"`
	TotalBytes uint64 `json:"total_bytes"`
	UsedBytes  uint64 `json:"used_bytes"`
}

// collectVolumes lista os discos fixos, com o do sistema (onde o agente roda) primeiro.
func collectVolumes() []Volume {
	partitions, err := disk.Partitions(false)
	if err != nil {
		return nil
	}
	system := mainPartition()
	var volumes []Volume
	for _, partition := range partitions {
		if partition.Mountpoint == "" || !isFixedVolume(partition) {
			continue
		}
		usage, err := disk.Usage(usagePath(partition.Mountpoint))
		if err != nil || usage == nil || usage.Total == 0 {
			continue
		}
		volume := Volume{Mount: partition.Mountpoint, TotalBytes: usage.Total, UsedBytes: usage.Used}
		if sameVolume(partition.Mountpoint, system) {
			volumes = append([]Volume{volume}, volumes...)
		} else {
			volumes = append(volumes, volume)
		}
	}
	if len(volumes) > maxVolumes {
		volumes = volumes[:maxVolumes]
	}
	return volumes
}

func sameVolume(a, b string) bool {
	return strings.EqualFold(strings.TrimRight(a, `\/`), strings.TrimRight(b, `\/`))
}
