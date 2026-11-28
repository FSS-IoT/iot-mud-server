package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/coder/websocket"
)

const (
	protocolVersion = 1
	// The v2 backend limits a WebSocket message to 64 KiB. Match it here so a
	// frame is rejected at the portal instead of killing the backend.
	maxMessageBytes = 65536
)

type Portal struct {
	mu  sync.RWMutex
	cfg *Config
	log *slog.Logger
}

func NewPortal(cfg *Config, log *slog.Logger) *Portal {
	return &Portal{cfg: cfg, log: log}
}

func (p *Portal) SetConfig(cfg *Config) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.cfg = cfg
}

func (p *Portal) Config() *Config {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.cfg
}

// Handler serves /healthz and the portal WebSocket on the same port.
func (p *Portal) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", p.health)
	mux.HandleFunc("/", p.portal)
	return mux
}

func (p *Portal) health(w http.ResponseWriter, _ *http.Request) {
	cfg := p.Config()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"ok":      true,
		"servers": len(cfg.Servers),
	})
}

func (p *Portal) portal(w http.ResponseWriter, r *http.Request) {
	ws, err := websocket.Accept(w, r, &websocket.AcceptOptions{})
	if err != nil {
		p.log.Warn("accept failed", "error", err)
		return
	}
	defer ws.CloseNow()
	ws.SetReadLimit(maxMessageBytes)

	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	if err := writeJSON(ctx, ws, map[string]any{
		"v":    protocolVersion,
		"type": "portal.hello",
		"data": map[string]any{"protocol": "portal/1"},
	}); err != nil {
		return
	}
	cancel()

	ctx = r.Context()
	for {
		typ, raw, err := ws.Read(ctx)
		if err != nil {
			return
		}
		if typ != websocket.MessageText {
			p.sendError(ctx, ws, nil, "bad_message", "Portal messages must be JSON text.")
			continue
		}
		var msg portalMessage
		if err := json.Unmarshal(raw, &msg); err != nil {
			p.sendError(ctx, ws, nil, "bad_message", "That is not a portal message.")
			continue
		}
		switch msg.Type {
		case "portal.login":
			var data struct {
				Dev bool `json:"dev"`
			}
			if err := json.Unmarshal(msg.Data, &data); err != nil {
				p.sendError(ctx, ws, msg.ID, "bad_args",
					"Login data must be an object with an optional dev flag.")
				continue
			}
			cfg := p.Config()
			if !data.Dev && cfg.DefaultServer != "" {
				if p.enterServer(ctx, ws, msg.ID, cfg.DefaultServer) {
					return
				}
				continue
			}
			if err := p.reply(ctx, ws, msg.ID, "portal.servers", map[string]any{
				"servers":        cfg.PublicServers(),
				"default_server": cfg.DefaultServer,
			}); err != nil {
				return
			}
		case "portal.servers":
			cfg := p.Config()
			if err := p.reply(ctx, ws, msg.ID, "portal.servers", map[string]any{
				"servers":        cfg.PublicServers(),
				"default_server": cfg.DefaultServer,
			}); err != nil {
				return
			}
		case "portal.enter":
			var data struct {
				Server string `json:"server"`
			}
			if err := json.Unmarshal(msg.Data, &data); err != nil ||
				data.Server == "" {
				p.sendError(ctx, ws, msg.ID, "bad_args",
					"Choose a server id.")
				continue
			}
			if p.enterServer(ctx, ws, msg.ID, data.Server) {
				return
			}
		case "portal.quit":
			return
		default:
			p.sendError(ctx, ws, msg.ID, "unknown_message",
				"Unknown portal message.")
		}
	}
}

func (p *Portal) enterServer(ctx context.Context, client *websocket.Conn,
	id json.RawMessage, serverID string) bool {
	cfg := p.Config()
	server, ok := cfg.Server(serverID)
	if !ok {
		p.sendError(ctx, client, id, "unknown_server", "No such server.")
		return false
	}

	dialCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	backend, _, err := websocket.Dial(dialCtx, server.URL, &websocket.DialOptions{
		HTTPHeader: http.Header{"User-Agent": []string{"iot-mud-portal/0.1"}},
	})
	if err != nil {
		p.log.Warn("backend down", "server", server.ID, "error", err)
		p.sendError(ctx, client, id, "backend_down",
			"That server is not available.")
		return false
	}
	defer backend.CloseNow()
	backend.SetReadLimit(maxMessageBytes)

	if err := p.reply(ctx, client, id, "portal.enter.ok", map[string]any{
		"server": server.ID,
	}); err != nil {
		return true
	}
	err = relay(ctx, client, backend)
	if err != nil && !errors.Is(err, context.Canceled) {
		p.log.Debug("relay ended", "server", server.ID, "error", err)
	}
	return true
}

func relay(ctx context.Context, client, backend *websocket.Conn) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	type relayEnd struct {
		err        error
		fromClient bool
	}
	errc := make(chan relayEnd, 2)
	go func() {
		for {
			typ, data, err := client.Read(ctx)
			if err != nil {
				errc <- relayEnd{err: err, fromClient: true}
				return
			}
			if err := backend.Write(ctx, typ, data); err != nil {
				errc <- relayEnd{err: err, fromClient: true}
				return
			}
		}
	}()
	go func() {
		for {
			typ, data, err := backend.Read(ctx)
			if err != nil {
				errc <- relayEnd{err: err, fromClient: false}
				return
			}
			if err := client.Write(ctx, typ, data); err != nil {
				errc <- relayEnd{err: err, fromClient: false}
				return
			}
		}
	}()
	end := <-errc
	cancel()
	if code := websocket.CloseStatus(end.err); code != -1 {
		reason := ""
		var closeErr *websocket.CloseError
		if errors.As(end.err, &closeErr) {
			reason = closeErr.Reason
		}
		peer := backend
		if !end.fromClient {
			peer = client
		}
		_ = peer.Close(code, reason)
	}
	return end.err
}

type portalMessage struct {
	Type string          `json:"type"`
	ID   json.RawMessage `json:"id,omitempty"`
	Data json.RawMessage `json:"data,omitempty"`
}

func (p *Portal) reply(ctx context.Context, ws *websocket.Conn, id json.RawMessage,
	typ string, data any) error {
	return writeJSON(ctx, ws, map[string]any{
		"v": protocolVersion, "type": typ, "id": id, "data": data,
	})
}

func (p *Portal) sendError(ctx context.Context, ws *websocket.Conn,
	id json.RawMessage, code, message string) {
	_ = p.reply(ctx, ws, id, "portal.error", map[string]any{
		"code": code, "message": message,
	})
}

func writeJSON(ctx context.Context, ws *websocket.Conn, value any) error {
	var buf bytes.Buffer
	if err := json.NewEncoder(&buf).Encode(value); err != nil {
		return err
	}
	return ws.Write(ctx, websocket.MessageText, buf.Bytes())
}
