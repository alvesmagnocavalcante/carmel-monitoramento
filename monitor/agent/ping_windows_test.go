//go:build windows

package main

import (
	"testing"
	"time"
)

// O ICMP real do Windows: loopback responde; 192.0.2.1 (TEST-NET, sem dono) não responde
// e a verificação termina dentro do prazo de ~2 s do ICMP.
func TestPingICMPWindows(t *testing.T) {
	latency, err := checkPing("127.0.0.1")
	if err != nil {
		t.Fatalf("loopback ping failed: %v", err)
	}
	if latency < 0 {
		t.Fatalf("invalid latency %v", latency)
	}

	start := time.Now()
	if _, err := checkPing("192.0.2.1"); err == nil {
		t.Fatal("TEST-NET address should not answer")
	}
	if elapsed := time.Since(start); elapsed > 3*time.Second {
		t.Fatalf("unreachable ping took too long: %s", elapsed)
	}
}
