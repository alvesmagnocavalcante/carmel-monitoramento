//go:build !windows

package main

import (
	"errors"
	"net"
	"time"
)

// pingICMP não é suportado fora do Windows: o agente só roda como serviço Windows, e o
// modo "tcp" continua funcionando em qualquer sistema.
func pingICMP(_ net.IP, _ time.Duration) (float64, error) {
	return 0, errors.New("ICMP is only supported on Windows")
}
