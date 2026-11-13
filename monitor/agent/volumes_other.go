//go:build !windows

package main

import "github.com/shirou/gopsutil/v4/disk"

// isFixedVolume (Linux/macOS) descarta sistemas de arquivos virtuais ou temporários,
// que não são discos de verdade. O agente em produção roda no Windows; isto serve para testes.
func isFixedVolume(partition disk.PartitionStat) bool {
	switch partition.Fstype {
	case "", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs":
		return false
	}
	return true
}

// usagePath fora do Windows usa o ponto de montagem como está.
func usagePath(mountpoint string) string {
	return mountpoint
}
