# ha_renderer
# Wed Jan 14 01:32:32 PST 2026

Local Home Assistant data-to-PNG renderer intended for TRMNL BYOS.

## Layout
- python_render: Python renderer + HTTP server
- docker: docker-compose.yml for the renderer
- espfirmware: placeholder for ESP firmware tooling

## Quick start
1) Set environment variables for HA access:
   - HA_TOKEN: long-lived access token
   - ENTITIES: comma-separated entity IDs
   - INSIDE_ENTITY: entity ID for inside temp
   - INSIDE_ENTITY_UNITS: override inside unit (F or C)
   - OUTSIDE_ENTITY: entity ID for outside temp
   - OUTSIDE_ENTITY_UNITS: override outside unit (F or C)
   - WEATHER_ENTITY: weather entity ID (optional)
   - HA_WS_URL: optional WebSocket URL (defaults derived from HA_URL)
   - FORECAST_MODE: websocket to fetch forecast via HA WebSocket API
   - FORECAST_TYPE: hourly or daily (defaults to daily)
   - PUBLIC_URL: base URL clients should use to fetch the PNG (optional)
   - LOG_PATH: path to append device logs (default /data/log.txt)
   - DEVICE_STATE_PATH: path to store last seen device headers (default /data/device_state.json)
   - MQTT_ENABLE: enable MQTT publishing (true/false)
   - MQTT_HOST/MQTT_PORT: broker connection (default mosquitto:1883)
   - MQTT_USERNAME/MQTT_PASSWORD: optional auth
   - MQTT_PREFIX: state topic prefix (default trmnl)
   - MQTT_DISCOVERY_PREFIX: Home Assistant discovery prefix (default homeassistant)
   - DEVICE_CONFIG_PATH: JSON file mapping device IDs to per-device settings (optional)
   - DISPLAY_REFRESH_RATE: device poll interval returned in /api/display (defaults to REFRESH_SECONDS)
   - REFRESH_SECONDS: when set, renderer pre-renders on a loop; when empty, render on /api/display
   - MAX_CACHE_PER_DEVICE: number of recent images cached per device (default 3)
   - EARLY_DISPLAY_THRESHOLD: seconds; if /api/display arrives sooner, advance to next screen (default 5)
   - LATE_DISPLAY_THRESHOLD: seconds; if /api/display arrives later, reset to first screen (default 2x DISPLAY_REFRESH_RATE)

2) Launch the renderer:
   - cd docker
   - HA_TOKEN=... ENTITIES=sensor.temp,sensor.humidity docker compose up --build

3) Fetch the image:
   - http://<host>:9088/trmnl.bmp

The renderer also exposes:
- /health
- /render (forces immediate refresh)

TRMNL compatibility endpoints:
- /api/setup (returns a local api_key + image_url)
- /api/display (records headers + returns image_url)
- /api/log (records headers/body)

## Per-device templates
Set `DEVICE_CONFIG_PATH` to a JSON file that maps device IDs (MAC without colons) to config overrides.
Example: `python_render/devices.json`

When a new device connects (not present in the config), the renderer will add a stub entry
pointing to `templates/trmnl_default.svg.j2` so you can edit it in place.

Supported keys per device:
- template: template path (relative to `python_render/` or absolute)
- output_path: output image path (can include `{device_id}` and `{screen_index}`)
- entities: list or comma-separated string
- inside_entity / outside_entity / weather_entity
- inside_entity_units / outside_entity_units (override units, F or C)
- screens: list of per-screen overrides (each entry can be a template string or a dict override)
- display_refresh_rate: override the per-device refresh rate returned to the device

## Template
The renderer uses a Jinja2 SVG template and converts it to PNG with CairoSVG.
Default template path: `python_render/templates/trmnl.svg.j2`

Additional template: `python_render/templates/trmnl_forecast.svg.j2` (current + hourly + daily)
