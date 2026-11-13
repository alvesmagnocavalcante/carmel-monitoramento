//go:build windows

package main

import (
	"errors"
	"fmt"
	"net"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Usa a iphlpapi.dll do Windows em vez de um socket ICMP "raw": o socket raw exige
// administrador, e o serviço roda como LocalService. As funções IcmpSendEcho da
// iphlpapi funcionam sem privilégios elevados.
var (
	iphlpapi         = windows.NewLazySystemDLL("iphlpapi.dll")
	procIcmpCreate   = iphlpapi.NewProc("IcmpCreateFile")
	procIcmpSendEcho = iphlpapi.NewProc("IcmpSendEcho")
	procIcmpClose    = iphlpapi.NewProc("IcmpCloseHandle")
)

// Tamanho dos dados do ping e do buffer de resposta. O ICMP_ECHO_REPLY ocupa 40 bytes
// em 64 bits (a documentação manda reservar sizeof + dados + 8); usamos 64 para ter
// folga também em 32 bits e com respostas de erro ICMP.
const (
	icmpDataSize   = 32
	icmpReplyBytes = 64
)

// pingICMP envia um ICMP echo para um IPv4 e devolve o tempo de ida e volta em ms.
// Erro significa sem resposta (timeout), resposta de erro ICMP ou falha da chamada.
func pingICMP(ip net.IP, timeout time.Duration) (float64, error) {
	v4 := ip.To4()
	if v4 == nil {
		return 0, errors.New("ICMP requires an IPv4 address")
	}
	if err := iphlpapi.Load(); err != nil {
		return 0, fmt.Errorf("load iphlpapi: %w", err)
	}

	handle, _, callErr := procIcmpCreate.Call()
	// IcmpCreateFile devolve INVALID_HANDLE_VALUE (todos os bits 1) em caso de erro.
	if handle == 0 || handle == ^uintptr(0) {
		return 0, fmt.Errorf("IcmpCreateFile: %w", callErr)
	}
	// O handle é sempre fechado, mesmo em erro, para não vazar recursos no serviço.
	defer procIcmpClose.Call(handle)

	// O endereço vai na ordem de bytes de rede, que na memória little-endian é
	// exatamente a leitura dos 4 bytes do IP como uint32.
	destination := uint32(v4[0]) | uint32(v4[1])<<8 | uint32(v4[2])<<16 | uint32(v4[3])<<24
	data := make([]byte, icmpDataSize)
	for index := range data {
		data[index] = byte('a' + index%26)
	}
	reply := make([]byte, icmpReplyBytes+icmpDataSize+8)

	replies, _, callErr := procIcmpSendEcho.Call(
		handle,
		uintptr(destination),
		uintptr(unsafe.Pointer(&data[0])),
		uintptr(len(data)),
		0,
		uintptr(unsafe.Pointer(&reply[0])),
		uintptr(len(reply)),
		uintptr(timeout.Milliseconds()),
	)
	// Zero respostas: timeout ou erro; o motivo (se houver) vem no último erro do Windows.
	if replies == 0 {
		return 0, fmt.Errorf("no ICMP reply: %w", callErr)
	}
	// Status no deslocamento 4 (0 = IP_SUCCESS) e tempo de ida e volta no 8, em ms.
	status := *(*uint32)(unsafe.Pointer(&reply[4]))
	if status != 0 {
		return 0, fmt.Errorf("ICMP status %d", status)
	}
	rtt := *(*uint32)(unsafe.Pointer(&reply[8]))
	return float64(rtt), nil
}
