#!/usr/bin/env bash
set -euo pipefail

if [[ -f /entry/ha_renderer.env ]]; then
  # shellcheck disable=SC1091
  source /entry/ha_renderer.env
fi


exec /usr/local/bin/ha_renderer
