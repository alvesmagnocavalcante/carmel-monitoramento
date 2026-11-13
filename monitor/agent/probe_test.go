package main

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeProbeAPI é uma API falsa que serve os alvos e guarda os POSTs de resultado recebidos.
type fakeProbeAPI struct {
	server     *httptest.Server
	mutex      sync.Mutex
	posts      []probeReport
	authHeader string
	targetsRaw string
	failGet    bool
}

// newFakeProbeAPI sobe o servidor falso; targetsRaw é o JSON devolvido em /api/probe/targets.
func newFakeProbeAPI(t *testing.T, targetsRaw string, failGet bool) *fakeProbeAPI {
	t.Helper()
	api := &fakeProbeAPI{targetsRaw: targetsRaw, failGet: failGet}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/probe/targets", func(writer http.ResponseWriter, request *http.Request) {
		if api.failGet {
			http.Error(writer, "boom", http.StatusInternalServerError)
			return
		}
		if request.URL.Query().Get("agent_id") != "agt_test" {
			http.Error(writer, "bad agent_id", http.StatusBadRequest)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(api.targetsRaw))
	})
	mux.HandleFunc("/api/probe/results", func(writer http.ResponseWriter, request *http.Request) {
		var report probeReport
		if err := json.NewDecoder(request.Body).Decode(&report); err != nil {
			http.Error(writer, err.Error(), http.StatusBadRequest)
			return
		}
		api.mutex.Lock()
		api.posts = append(api.posts, report)
		api.authHeader = request.Header.Get("Authorization")
		api.mutex.Unlock()
		_, _ = writer.Write([]byte(`{"accepted": ` + strconv.Itoa(len(report.Results)) + `}`))
	})
	api.server = httptest.NewServer(mux)
	t.Cleanup(api.server.Close)
	return api
}

// postCount devolve quantos POSTs de resultado chegaram.
func (api *fakeProbeAPI) postCount() int {
	api.mutex.Lock()
	defer api.mutex.Unlock()
	return len(api.posts)
}

// Ciclo completo: um alvo tcp aberto (ok=true) e um fechado (ok=false), com token e probe_id.
func TestProbeCycleReportsOpenAndClosedPorts(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	openPort := listener.Addr().(*net.TCPAddr).Port

	// Abre e fecha um listener para obter uma porta que sabemos estar livre.
	closedListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closedPort := closedListener.Addr().(*net.TCPAddr).Port
	closedListener.Close()

	targets := `[{"id":"dev_open","address":"127.0.0.1","method":"tcp","port":` + strconv.Itoa(openPort) + `},` +
		`{"id":"dev_closed","address":"127.0.0.1","method":"tcp","port":` + strconv.Itoa(closedPort) + `}]`
	api := newFakeProbeAPI(t, targets, false)

	runProbeCycle(context.Background(), newAPIClient(api.server.URL, "tok-123"), "agt_test")

	if api.postCount() != 1 {
		t.Fatalf("expected 1 POST, got %d", api.postCount())
	}
	report := api.posts[0]
	if api.authHeader != "Bearer tok-123" {
		t.Fatalf("unexpected Authorization header: %q", api.authHeader)
	}
	if report.ProbeID != "agt_test" {
		t.Fatalf("unexpected probe_id: %q", report.ProbeID)
	}
	if len(report.Results) != 2 {
		t.Fatalf("expected 2 results, got %d", len(report.Results))
	}
	byID := map[string]probeResult{}
	for _, result := range report.Results {
		byID[result.DeviceID] = result
	}
	if !byID["dev_open"].OK || byID["dev_open"].LatencyMS == nil {
		t.Fatalf("open port should be ok with latency: %+v", byID["dev_open"])
	}
	if byID["dev_closed"].OK || byID["dev_closed"].LatencyMS != nil {
		t.Fatalf("closed port should be down without latency: %+v", byID["dev_closed"])
	}
}

// Lista de alvos vazia: não há nada a verificar e nenhum POST é feito.
func TestProbeCycleEmptyTargetsDoesNotPost(t *testing.T) {
	api := newFakeProbeAPI(t, `[]`, false)
	runProbeCycle(context.Background(), newAPIClient(api.server.URL, "tok"), "agt_test")
	if api.postCount() != 0 {
		t.Fatalf("expected no POST, got %d", api.postCount())
	}
}

// Falha ao buscar os alvos: sem pânico e sem POST.
func TestProbeCycleFetchFailureDoesNotPost(t *testing.T) {
	api := newFakeProbeAPI(t, `[]`, true)
	runProbeCycle(context.Background(), newAPIClient(api.server.URL, "tok"), "agt_test")
	if api.postCount() != 0 {
		t.Fatalf("expected no POST, got %d", api.postCount())
	}
}

// Nunca mais de 10 verificações ao mesmo tempo, e todos os alvos são verificados.
func TestCheckAllRespectsConcurrencyLimit(t *testing.T) {
	previous := checkTarget
	t.Cleanup(func() { checkTarget = previous })

	var running, peak, total atomic.Int32
	checkTarget = func(_ context.Context, target probeTarget) probeResult {
		current := running.Add(1)
		for {
			seen := peak.Load()
			if current <= seen || peak.CompareAndSwap(seen, current) {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
		running.Add(-1)
		total.Add(1)
		return probeResult{DeviceID: target.ID, OK: true}
	}

	targets := make([]probeTarget, 60)
	for index := range targets {
		targets[index] = probeTarget{ID: "dev_" + strconv.Itoa(index), Address: "127.0.0.1", Method: "ping"}
	}
	results := checkAll(context.Background(), targets)

	if len(results) != 60 || total.Load() != 60 {
		t.Fatalf("expected 60 checks, got results=%d checks=%d", len(results), total.Load())
	}
	if peak.Load() > probeConcurrency {
		t.Fatalf("concurrency peak %d exceeds limit %d", peak.Load(), probeConcurrency)
	}
	if peak.Load() < 2 {
		t.Fatalf("checks did not run in parallel (peak %d)", peak.Load())
	}
	for index, result := range results {
		if result.DeviceID != targets[index].ID || !result.OK {
			t.Fatalf("result %d out of order or not ok: %+v", index, result)
		}
	}
}

// Com o prazo do ciclo esgotado, os alvos que não rodaram voltam como ok=false.
func TestCheckAllExpiredDeadlineMarksRemainingDown(t *testing.T) {
	previous := checkTarget
	t.Cleanup(func() { checkTarget = previous })
	checkTarget = func(_ context.Context, target probeTarget) probeResult {
		return probeResult{DeviceID: target.ID, OK: true}
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	targets := make([]probeTarget, 30)
	for index := range targets {
		targets[index] = probeTarget{ID: "dev_" + strconv.Itoa(index)}
	}
	results := checkAll(ctx, targets)
	if len(results) != 30 {
		t.Fatalf("expected one result per target, got %d", len(results))
	}
	for _, result := range results {
		if result.DeviceID == "" {
			t.Fatal("every result must carry its device id")
		}
	}
}
