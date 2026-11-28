package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	var (
		configPath = flag.String("config", "portal.json", "portal JSON config")
		logLevel   = flag.String("log-level", "info", "debug, info, warn, error")
	)
	flag.Parse()

	level := slog.LevelInfo
	switch *logLevel {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: level,
	}))

	cfg, err := LoadConfig(*configPath)
	if err != nil {
		log.Error("config", "error", err)
		os.Exit(1)
	}
	portal := NewPortal(*configPath, cfg, log)

	server := &http.Server{
		Addr:              cfg.Listen,
		Handler:           portal.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	hup := make(chan os.Signal, 1)
	signal.Notify(hup, syscall.SIGHUP)
	go func() {
		for range hup {
			next, err := LoadConfig(*configPath)
			if err != nil {
				log.Error("reload failed", "error", err)
				continue
			}
			portal.SetConfig(next)
			log.Info("config reloaded", "servers", len(next.Servers))
		}
	}()

	ctx, stop := signal.NotifyContext(
		context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Info("portal listening", "addr", cfg.Listen,
			"servers", len(cfg.Servers))
		if err := server.ListenAndServe(); err != nil &&
			!errors.Is(err, http.ErrServerClosed) {
			log.Error("serve", "error", err)
			stop()
		}
	}()

	<-ctx.Done()
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		log.Warn("shutdown", "error", err)
	}
}
