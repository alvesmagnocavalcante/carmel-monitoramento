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

func usagePath(mountpoint string) string {
	return mountpoint + `\`
}
