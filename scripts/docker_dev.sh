#!/usr/bin/env bash
# Run the local SPX Voice stack from locally built Docker images.

set -euo pipefail

COMMAND="${1:-up}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${BASE_DIR}"

COMPOSE_ARGS=(
  compose
  -f docker-compose.yaml
  -f docker-compose.dev.yaml
)

ensure_env_file() {
  local example="$1"
  local destination="$2"
  if [[ -f "${destination}" || ! -f "${example}" ]]; then
    return
  fi
  cp "${example}" "${destination}"
  echo "Created ${destination} from ${example}"
}

print_next_steps() {
  cat <<'EOF'

SPX Voice Docker dev stack is starting/running.
  UI:     http://localhost:3010
  API:    http://localhost:8000/api/v1/health
  MinIO:  http://localhost:9001

Useful commands:
  bash scripts/docker_dev.sh logs
  bash scripts/docker_dev.sh ps
  bash scripts/docker_dev.sh down

This dev stack bind-mounts local api/ and ui/ code; it does not build the heavy production images.
EOF
}

case "${COMMAND}" in
  up)
    ensure_env_file .env.example .env
    ensure_env_file api/.env.example api/.env
    ensure_env_file ui/.env.example ui/.env
    # `up` pulls the public images and builds the API image locally from
    # api/Dockerfile on first run when no published image is available. The
    # first build can take a few minutes; later starts reuse the built image.
    docker "${COMPOSE_ARGS[@]}" up -d
    print_next_steps
    ;;
  rebuild)
    docker "${COMPOSE_ARGS[@]}" build api
    docker "${COMPOSE_ARGS[@]}" up -d --force-recreate api ui
    print_next_steps
    ;;
  restart)
    docker "${COMPOSE_ARGS[@]}" up -d --force-recreate api ui
    print_next_steps
    ;;
  down)
    docker "${COMPOSE_ARGS[@]}" down
    ;;
  logs)
    docker "${COMPOSE_ARGS[@]}" logs -f --tail=150
    ;;
  ps)
    docker "${COMPOSE_ARGS[@]}" ps
    ;;
  *)
    echo "Usage: $0 [up|rebuild|restart|down|logs|ps]" >&2
    exit 2
    ;;
esac
