//go:build !windows

package main

import "testing"

// Fora do Windows o ICMP não existe: o teste de ping real é pulado.
func TestPingICMPWindows(t *testing.T) {
	t.Skip("ICMP is only supported on Windows")
}
