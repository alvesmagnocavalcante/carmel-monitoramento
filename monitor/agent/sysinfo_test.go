package main

import "testing"

// Confere a extração do número da build a partir do texto de versão do kernel.
func TestBuildNumber(t *testing.T) {
	cases := map[string]string{
		"10.0.26300.9711 Build 26300.9711": "26300.9711",
		"6.8.0-45-generic":                 "6.8.0-45-generic",
		"  ":                               "",
	}
	for input, want := range cases {
		if got := buildNumber(input); got != want {
			t.Errorf("buildNumber(%q) = %q, want %q", input, got, want)
		}
	}
}

// Confere que só IPv4 de rede válido é aceito (sem link-local, loopback, IPv6 ou lixo).
func TestUsableIPv4(t *testing.T) {
	cases := map[string]string{
		"192.168.1.20/24":              "192.168.1.20",
		"169.254.249.242/16":           "",
		"127.0.0.1/8":                  "",
		"fe80::17cb:bd4a:fbf0:5360/64": "",
		"not-an-address":               "",
	}
	for input, want := range cases {
		if got := usableIPv4(input); got != want {
			t.Errorf("usableIPv4(%q) = %q, want %q", input, got, want)
		}
	}
}

// Confere o filtro de placas: aceita as reais ativas e rejeita desligadas, loopback e virtuais.
func TestIsUsableInterface(t *testing.T) {
	cases := []struct {
		name  string
		flags []string
		want  bool
	}{
		{"Ethernet", []string{"up", "broadcast"}, true},
		{"Wi-Fi", []string{"up"}, true},
		{"Ethernet", []string{"broadcast"}, false},
		{"Loopback Pseudo-Interface 1", []string{"up", "loopback"}, false},
		{"vEthernet (Default Switch)", []string{"up"}, false},
		{"VirtualBox Host-Only Network", []string{"up"}, false},
	}
	for _, tc := range cases {
		if got := isUsableInterface(tc.name, tc.flags); got != tc.want {
			t.Errorf("isUsableInterface(%q, %v) = %v, want %v", tc.name, tc.flags, got, tc.want)
		}
	}
}

// Confere a comparação de volumes, que ignora maiúsculas e a barra final.
func TestSameVolume(t *testing.T) {
	if !sameVolume("C:", `c:\`) {
		t.Error("C: e c:\\ devem ser o mesmo volume")
	}
	if sameVolume("C:", "D:") {
		t.Error("C: e D: são volumes diferentes")
	}
}

// Confere que o IP principal vai para a frente da lista e que, sem IP principal, nada muda.
func TestPrimaryFirst(t *testing.T) {
	got := primaryFirst([]string{"10.0.0.2", "192.168.56.1", "172.16.0.9"}, "192.168.56.1")
	want := []string{"192.168.56.1", "10.0.0.2", "172.16.0.9"}
	if len(got) != len(want) {
		t.Fatalf("primaryFirst() = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("primaryFirst() = %v, want %v", got, want)
		}
	}
	if same := primaryFirst([]string{"10.0.0.2"}, ""); len(same) != 1 || same[0] != "10.0.0.2" {
		t.Errorf("sem IP principal a lista não deve mudar, veio %v", same)
	}
}
