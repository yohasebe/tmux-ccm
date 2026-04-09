#!/usr/bin/env bash
# Create demo projects for ccm GIF recording.
# Usage: ./scripts/setup-demo.sh [target_dir]
# Default target: ../ccm-demo (sibling of ccm repo)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CCM_ROOT="$(dirname "$SCRIPT_DIR")"
DEMO_DIR="${1:-$(dirname "$CCM_ROOT")/ccm-demo}"

echo "Creating demo projects in: $DEMO_DIR"

mkdir -p "$DEMO_DIR"/{auth-service/cmd/server,dashboard-ui,data-pipeline,sdk-python/src}

# ─── auth-service (Go) ───
cat > "$DEMO_DIR/auth-service/README.md" << 'EOF'
# auth-service

Authentication microservice built with Go. Handles JWT token issuance, validation, and refresh.

## Stack
- Go 1.22
- PostgreSQL
- Redis (session cache)
EOF

cat > "$DEMO_DIR/auth-service/cmd/server/main.go" << 'EOF'
package main

import (
	"fmt"
	"net/http"
)

func main() {
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"status": "ok"}`)
	})
	fmt.Println("auth-service listening on :8080")
	http.ListenAndServe(":8080", nil)
}
EOF

# ─── dashboard-ui (React) ───
cat > "$DEMO_DIR/dashboard-ui/README.md" << 'EOF'
# dashboard-ui

Internal dashboard built with React + TypeScript. Displays real-time metrics and user management.

## Stack
- React 18
- TypeScript
- Vite
- TailwindCSS
EOF

cat > "$DEMO_DIR/dashboard-ui/package.json" << 'EOF'
{
  "name": "dashboard-ui",
  "version": "0.1.0",
  "scripts": {
    "dev": "vite",
    "build": "tsc && vite build"
  }
}
EOF

# ─── data-pipeline (Python) ───
cat > "$DEMO_DIR/data-pipeline/README.md" << 'EOF'
# data-pipeline

ETL pipeline for processing event data. Runs on scheduled intervals via Airflow.

## Stack
- Python 3.12
- Apache Airflow
- pandas / polars
- PostgreSQL
EOF

cat > "$DEMO_DIR/data-pipeline/pipeline.py" << 'EOF'
"""Main ETL pipeline for event data processing."""

import logging

logger = logging.getLogger(__name__)


def extract(source: str) -> list[dict]:
    """Extract raw events from source."""
    logger.info(f"Extracting from {source}")
    return []


def transform(events: list[dict]) -> list[dict]:
    """Clean and transform raw events."""
    return [e for e in events if e.get("valid")]


def load(events: list[dict], target: str) -> int:
    """Load transformed events to target."""
    logger.info(f"Loading {len(events)} events to {target}")
    return len(events)
EOF

# ─── sdk-python ───
cat > "$DEMO_DIR/sdk-python/README.md" << 'EOF'
# sdk-python

Official Python SDK for the platform API. Published to PyPI.

## Stack
- Python 3.10+
- httpx
- pydantic
EOF

cat > "$DEMO_DIR/sdk-python/src/client.py" << 'EOF'
"""Platform API client."""

import httpx


class PlatformClient:
    def __init__(self, api_key: str, base_url: str = "https://api.example.com"):
        self.base_url = base_url
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def get_user(self, user_id: str) -> dict:
        resp = self._client.get(f"/users/{user_id}")
        resp.raise_for_status()
        return resp.json()
EOF

echo "Demo projects created:"
find "$DEMO_DIR" -type f | sort
echo ""
echo "To add to ccm:"
echo "  ccm add $DEMO_DIR/auth-service auth-service"
echo "  ccm add $DEMO_DIR/dashboard-ui dashboard-ui"
echo "  ccm add $DEMO_DIR/data-pipeline data-pipeline"
echo "  ccm add $DEMO_DIR/sdk-python sdk-python"
