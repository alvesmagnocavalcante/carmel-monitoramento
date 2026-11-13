package main

import (
	"strings"

	"github.com/shirou/gopsutil/v4/disk"
)

// maxVolumes limita quantos discos entram no heartbeat, para o envio não crescer demais.
const maxVolumes = 16

// Volume é um disco fixo do computador, com uso em bytes.
type Volume struct {
	Mount      string `json:"mount"`
	TotalBytes uint64 `json:"total_bytes"`
	UsedBytes  uint64 `json:"used_bytes"`
}

// collectVolumes lista os discos fixos, com o do sistema (onde o agente roda) primeiro.
func collectVolumes() []Volume {
	// false = só partições físicas (sem sistemas de arquivos virtuais).
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
		// Ignora volumes sem leitura de uso ou de tamanho zero (ex.: leitor vazio).
		if err != nil || usage == nil || usage.Total == 0 {
			continue
		}
		volume := Volume{Mount: partition.Mountpoint, TotalBytes: usage.Total, UsedBytes: usage.Used}
		// O disco do sistema vai para o início da lista; os demais ficam na ordem encontrada.
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

// sameVolume compara dois caminhos de disco ignorando maiúsculas e a barra final,
// pois "C:", "c:\" e "C:/" são o mesmo volume.
func sameVolume(a, b string) bool {
	return strings.EqualFold(strings.TrimRight(a, `\/`), strings.TrimRight(b, `\/`))
}
