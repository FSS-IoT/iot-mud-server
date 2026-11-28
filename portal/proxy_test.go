package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func testPortal(t *testing.T, backendURL string) (*httptest.Server, *Portal) {
	t.Helper()
	cfg := &Config{
		Listen: "127.0.0.1:0",
		Servers: []Server{{
			ID: "fake", Name: "Fake", URL: backendURL,
			Lease: "isolated",
		}},
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	portal := NewPortal("", cfg, slog.New(slog.NewTextHandler(
		&strings.Builder{}, nil)))
	server := httptest.NewServer(portal.Handler())
	t.Cleanup(server.Close)
	return server, portal
}

func fakeBackend(t *testing.T) *httptest.Server {
	t.Helper()
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			OriginPatterns: []string{"*"},
		})
		if err != nil {
			return
		}
		defer ws.CloseNow()
		ctx := r.Context()
		_ = ws.Write(ctx, websocket.MessageText,
			[]byte(`{"type":"hello","data":{"protocol":[2]}}`))
		_, _, err = ws.Read(ctx)
		if err != nil {
			return
		}
		_ = ws.Write(ctx, websocket.MessageText,
			[]byte(`{"type":"auth.ok","data":{"player":{"name":"Ada"}}}`))
		for {
			typ, data, err := ws.Read(ctx)
			if err != nil {
				return
			}
			if err := ws.Write(ctx, typ, data); err != nil {
				return
			}
		}
	})
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	return server
}

func dialPortal(t *testing.T, server *httptest.Server) *websocket.Conn {
	t.Helper()
	url := "ws" + strings.TrimPrefix(server.URL, "http") + "/"
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ws, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ws.CloseNow() })
	return ws
}

func readText(t *testing.T, ws *websocket.Conn) map[string]any {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	typ, raw, err := ws.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if typ != websocket.MessageText {
		t.Fatalf("message type %v", typ)
	}
	var msg map[string]any
	if err := json.Unmarshal(raw, &msg); err != nil {
		t.Fatal(err)
	}
	return msg
}

func writeText(t *testing.T, ws *websocket.Conn, value string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ws.Write(ctx, websocket.MessageText, []byte(value)); err != nil {
		t.Fatal(err)
	}
}

func TestPortalListsAndRelays(t *testing.T) {
	backend := fakeBackend(t)
	server, _ := testPortal(t, "ws"+strings.TrimPrefix(backend.URL, "http"))
	ws := dialPortal(t, server)

	if msg := readText(t, ws); msg["type"] != "portal.hello" {
		t.Fatalf("hello: %v", msg)
	}
	writeText(t, ws, `{"type":"portal.servers","id":"1"}`)
	msg := readText(t, ws)
	if msg["type"] != "portal.servers" {
		t.Fatalf("servers: %v", msg)
	}
	data := msg["data"].(map[string]any)
	servers := data["servers"].([]any)
	if len(servers) != 1 || servers[0].(map[string]any)["id"] != "fake" {
		t.Fatalf("server list: %v", servers)
	}

	writeText(t, ws, `{"type":"portal.enter","id":"2","data":{"server":"fake"}}`)
	if msg := readText(t, ws); msg["type"] != "portal.enter.ok" {
		t.Fatalf("enter: %v", msg)
	}
	if msg := readText(t, ws); msg["type"] != "hello" {
		t.Fatalf("backend hello: %v", msg)
	}
	writeText(t, ws, `{"type":"auth","id":"auth","data":{"mode":"login"}}`)
	if msg := readText(t, ws); msg["type"] != "auth.ok" {
		t.Fatalf("auth: %v", msg)
	}
	writeText(t, ws, `{"type":"input","id":7,"data":{"text":"look"}}`)
	if msg := readText(t, ws); msg["type"] != "input" {
		t.Fatalf("relay: %v", msg)
	}
}

func TestUnknownServer(t *testing.T) {
	server, _ := testPortal(t, "ws://127.0.0.1:1")
	ws := dialPortal(t, server)
	_ = readText(t, ws)
	writeText(t, ws, `{"type":"portal.enter","id":"1","data":{"server":"nope"}}`)
	msg := readText(t, ws)
	if msg["type"] != "portal.error" {
		t.Fatalf("want portal.error, got %v", msg)
	}
	if msg["data"].(map[string]any)["code"] != "unknown_server" {
		t.Fatalf("code: %v", msg)
	}
}

func TestBackendDown(t *testing.T) {
	server, _ := testPortal(t, "ws://127.0.0.1:1")
	ws := dialPortal(t, server)
	_ = readText(t, ws)
	writeText(t, ws, `{"type":"portal.enter","id":"1","data":{"server":"fake"}}`)
	msg := readText(t, ws)
	if msg["type"] != "portal.error" {
		t.Fatalf("want portal.error, got %v", msg)
	}
	if msg["data"].(map[string]any)["code"] != "backend_down" {
		t.Fatalf("code: %v", msg)
	}
}

func TestConfigValidation(t *testing.T) {
	good := &Config{Listen: "127.0.0.1:0", Servers: []Server{{
		ID: "a", Name: "A", URL: "ws://127.0.0.1:1", Lease: "isolated",
	}}}
	if err := good.Validate(); err != nil {
		t.Fatal(err)
	}
	bad := *good
	bad.Servers = []Server{{ID: "a", Name: "A", URL: "http://x", Lease: "isolated"}}
	if err := bad.Validate(); err == nil {
		t.Fatal("expected bad url error")
	}
}

func testPortalDefault(t *testing.T, backendURL string) *httptest.Server {
	t.Helper()
	cfg := &Config{
		Listen:        "127.0.0.1:0",
		DefaultServer: "fake",
		Servers: []Server{{
			ID: "fake", Name: "Fake", URL: backendURL,
			Lease: "authoritative",
		}},
	}
	if err := cfg.Validate(); err != nil {
		t.Fatal(err)
	}
	portal := NewPortal("", cfg, slog.New(slog.NewTextHandler(
		&strings.Builder{}, nil)))
	server := httptest.NewServer(portal.Handler())
	t.Cleanup(server.Close)
	return server
}

func TestLoginAutoEntersDefault(t *testing.T) {
	backend := fakeBackend(t)
	server := testPortalDefault(t,
		"ws"+strings.TrimPrefix(backend.URL, "http"))
	ws := dialPortal(t, server)
	_ = readText(t, ws)
	writeText(t, ws, `{"type":"portal.login","id":"1","data":{"dev":false}}`)
	if msg := readText(t, ws); msg["type"] != "portal.enter.ok" {
		t.Fatalf("want auto enter, got %v", msg)
	}
	if msg := readText(t, ws); msg["type"] != "hello" {
		t.Fatalf("want backend hello, got %v", msg)
	}
}

func TestLoginDevShowsList(t *testing.T) {
	backend := fakeBackend(t)
	server := testPortalDefault(t,
		"ws"+strings.TrimPrefix(backend.URL, "http"))
	ws := dialPortal(t, server)
	_ = readText(t, ws)
	writeText(t, ws, `{"type":"portal.login","id":"1","data":{"dev":true}}`)
	msg := readText(t, ws)
	if msg["type"] != "portal.servers" {
		t.Fatalf("want server list, got %v", msg)
	}
	data := msg["data"].(map[string]any)
	if data["default_server"] != "fake" {
		t.Fatalf("default: %v", data)
	}
	servers := data["servers"].([]any)
	if servers[0].(map[string]any)["default"] != true {
		t.Fatalf("default flag: %v", servers)
	}
}
