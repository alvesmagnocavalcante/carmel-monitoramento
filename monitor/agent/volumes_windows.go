package main

import (
	"github.com/shirou/gopsutil/v4/disk"
	"golang.org/x/sys/windows"
)

// isFixedVolume exclui pen drives, unidades de rede mapeadas e leitores de CD.
func isFixedVolume(partition disk.PartitionStat) bool {
	root := windows.StringToUTF16Ptr(partition.Mountpoint + `\`)
	return windows.GetDriveType(root) == windows.DRIVE_FIXED
}

// usagePath acrescenta a barra final: no Windows, "C:" sozinho significa "pasta atual
// do drive C", e "C:\" significa a raiz do disco, que é o que se quer medir.
func usagePath(mountpoint string) string {
	return mountpoint + `\`
}
