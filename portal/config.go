package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
)

// Server is one game backend. The client never sees URL; the portal dials it.
type Server struct {
	ID    string   `json:"id"`
	Name  string   `json:"name"`
	URL   string   `json:"url"`
	Lease string   `json:"lease"`
	Tags  []string `json:"tags,omitempty"`
}

// PublicServer is the part of a server the client may see.
type PublicServer struct {
	ID      string   `json:"id"`
	Name    string   `json:"name"`
	Lease   string   `json:"lease"`
	Tags    []string `json:"tags,omitempty"`
	Default bool     `json:"default,omitempty"`
}

type Config struct {
	Listen        string   `json:"listen"`
	DefaultServer string   `json:"default_server,omitempty"`
	Servers       []Server `json:"servers"`
}

func LoadConfig(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg Config
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return &cfg, nil
}

func (c *Config) Validate() error {
	if strings.TrimSpace(c.Listen) == "" {
		return errors.New("listen is required")
	}
	if len(c.Servers) == 0 {
		return errors.New("at least one server is required")
	}
	seen := make(map[string]bool, len(c.Servers))
	for i := range c.Servers {
		s := &c.Servers[i]
		s.ID = strings.TrimSpace(s.ID)
		s.Name = strings.TrimSpace(s.Name)
		if s.ID == "" || s.Name == "" {
			return fmt.Errorf("server %d needs id and name", i)
		}
		if seen[s.ID] {
			return fmt.Errorf("duplicate server id %q", s.ID)
		}
		seen[s.ID] = true
		switch s.Lease {
		case "authoritative", "isolated":
		default:
			return fmt.Errorf("server %q: lease must be authoritative or isolated", s.ID)
		}
		u, err := url.Parse(s.URL)
		if err != nil || (u.Scheme != "ws" && u.Scheme != "wss") || u.Host == "" {
			return fmt.Errorf("server %q: url must be ws:// or wss://", s.ID)
		}
	}
	if c.DefaultServer != "" {
		if !seen[c.DefaultServer] {
			return fmt.Errorf("default_server %q is not configured", c.DefaultServer)
		}
	}
	return nil
}

func (c *Config) Server(id string) (Server, bool) {
	for _, s := range c.Servers {
		if s.ID == id {
			return s, true
		}
	}
	return Server{}, false
}

func (c *Config) PublicServers() []PublicServer {
	out := make([]PublicServer, 0, len(c.Servers))
	for _, s := range c.Servers {
		out = append(out, PublicServer{
			ID: s.ID, Name: s.Name, Lease: s.Lease, Tags: s.Tags,
			Default: s.ID == c.DefaultServer,
		})
	}
	return out
}
