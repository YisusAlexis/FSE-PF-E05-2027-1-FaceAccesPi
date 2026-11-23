#!/usr/bin/env bash
# iniciar_vision.sh - Lo ejecuta systemd (faceaccess.service). Prepara la pantalla y
# arranca el servicio de reconocimiento. También se puede correr a mano para depurar.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

# shellcheck source=entorno_pantalla.sh
source "$DIR/scripts/entorno_pantalla.sh"

export PYTHONUNBUFFERED=1
exec python3 "$DIR/vision/faceaccess.py"
