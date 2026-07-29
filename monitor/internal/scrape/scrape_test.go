package scrape

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFetchParsesLiveHTTPResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(realScrape))
	}))
	defer server.Close()

	snap, err := Fetch(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	if !snap.HasCacheItems {
		t.Error("expected cache gauge from live fetch")
	}
	if len(snap.Latency) != 2 {
		t.Errorf("expected 2 routes, got %d", len(snap.Latency))
	}
}

func TestFetchNonOKStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not ready", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	if _, err := Fetch(server.URL); err == nil {
		t.Error("expected an error on a non-200 response")
	}
}

func TestFetchConnectionRefusedIsAnError(t *testing.T) {
	// A closed server: the app hasn't started listening yet, which the
	// caller (the monitor's scrape loop) is expected to tolerate by
	// skipping the cycle, not by Fetch itself hiding the failure.
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := server.URL
	server.Close()

	_, err := Fetch(url)
	if err == nil {
		t.Fatal("expected an error when the target isn't listening")
	}
	if !strings.Contains(err.Error(), url) {
		t.Errorf("error should reference the scrape URL for debuggability: %v", err)
	}
}
