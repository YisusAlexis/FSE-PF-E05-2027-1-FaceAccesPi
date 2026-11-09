#!/usr/bin/env bash
# descargar_modelos.sh - Descarga YuNet (detección) y SFace (reconocimiento) del OpenCV Zoo
# y verifica su SHA-256. Uso: scripts/descargar_modelos.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
DESTINO="$DIR/modelos"
mkdir -p "$DESTINO"

# nombre | sha256 | ruta en opencv_zoo
MODELOS=(
  "face_detection_yunet_2023mar.onnx|8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4|models/face_detection_yunet"
  "face_recognition_sface_2021dec.onnx|0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79|models/face_recognition_sface"
)

for m in "${MODELOS[@]}"; do
  IFS='|' read -r nombre sha ruta <<< "$m"
  archivo="$DESTINO/$nombre"
  if [[ -f "$archivo" ]] && echo "$sha  $archivo" | sha256sum -c --quiet 2>/dev/null; then
    echo "[OK] $nombre ya está descargado y verificado"
    continue
  fi
  ok=0
  # Los modelos están en Git LFS: media.githubusercontent.com entrega el archivo real
  for url in "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/$ruta/$nombre" \
             "https://github.com/opencv/opencv_zoo/raw/main/$ruta/$nombre"; do
    echo "[..] Descargando $nombre"
    if curl -fsSL --retry 3 -o "$archivo.tmp" "$url" && \
       echo "$sha  $archivo.tmp" | sha256sum -c --quiet 2>/dev/null; then
      mv "$archivo.tmp" "$archivo"
      echo "[OK] $nombre ($(du -h "$archivo" | cut -f1)) verificado"
      ok=1
      break
    fi
  done
  rm -f "$archivo.tmp"
  if [[ $ok -ne 1 ]]; then
    echo "[ERROR] No se pudo descargar o verificar $nombre" >&2
    exit 1
  fi
done
