//go:build !windows

package main

import "github.com/shirou/gopsutil/v4/disk"

func isFixedVolume(partition disk.PartitionStat) bool {
	switch partition.Fstype {
	case "", "tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs":
		return false
	}
	return true
}

func usagePath(mountpoint string) string {
	return mountpoint
}
