#!/usr/bin/env bash
set -euo pipefail

set -x
PUBLICIP=$(ip route get 1 | sed -e 's/.*src //' -e 's/ .*//g')
export PUBLICIP
if [[ -f /entry/ha_renderer.env ]]; then
  # shellcheck disable=SC1091
  source /entry/ha_renderer.env
fi

echo PUBLIC_URL: ${PUBLIC_URL}


echo exec /usr/local/bin/ha_renderer
