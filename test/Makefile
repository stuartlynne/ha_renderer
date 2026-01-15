SHELL := /bin/bash

ENV_FILE := docker.env
TEMPLATE_SRC := ../python_render/templates/trmnl.svg.j2
TEMPLATE_DST := templates/trmnl.svg.j2 templates/trmnl_default.svg.j2 templates/trmnl_forecast.svg.j2
FONT_DST := fonts/TGL_0-1451Eng.ttf
DIRS_NEEDED := data templates fonts

.PHONY: all clean ha-states ha-weather-list ha-weather ha-forecast ha-ws-forecast ha-outside

all: $(DIRS_NEEDED) $(TEMPLATE_DST) $(FONT_DST)
	@set -a; source $(ENV_FILE); set +a; python ../python_render/app.py 2>&1 | tee stderr.txt

$(DIRS_NEEDED):
	mkdir -p $@

templates/trmnl.svg.j2: ../python_render/templates/trmnl.svg.j2 | templates
	cp ../python_render/templates/trmnl.svg.j2 templates/trmnl.svg.j2

templates/trmnl_default.svg.j2: ../python_render/templates/trmnl_default.svg.j2 | templates
	cp ../python_render/templates/trmnl_default.svg.j2 templates/trmnl_default.svg.j2

templates/trmnl_forecast.svg.j2: ../python_render/templates/trmnl_forecast.svg.j2 | templates
	cp ../python_render/templates/trmnl_forecast.svg.j2 templates/trmnl_forecast.svg.j2

fonts/TGL_0-1451Eng.ttf: ../python_render/fonts/TGL_0-1451Eng.ttf | fonts
	cp ../python_render/fonts/TGL_0-1451Eng.ttf fonts/TGL_0-1451Eng.ttf

clean:
	rm -rf data

ha-states:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states"

ha-weather-list:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	if command -v jq >/dev/null 2>&1; then \
		curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states" \
		| jq -r '.[].entity_id | select(startswith("weather."))'; \
	else \
		curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states" \
		| grep -o 'weather\\.[a-zA-Z0-9_]*' | sort -u; \
	fi

ha-weather:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	if [[ -z "$$WEATHER_ENTITY" ]]; then echo "WEATHER_ENTITY is empty"; exit 1; fi; \
	curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states/$$WEATHER_ENTITY" | json_pp
	#curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states/$$WEATHER_ENTITY" | jq -c -n 2

ha-forecast:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	if [[ -z "$$WEATHER_ENTITY" ]]; then echo "WEATHER_ENTITY is empty"; exit 1; fi; \
	curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/weather/forecast?entity_id=$$WEATHER_ENTITY" | json_pp

ha-ws-forecast:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	if [[ -z "$$WEATHER_ENTITY" ]]; then echo "WEATHER_ENTITY is empty"; exit 1; fi; \
	python ../python_render/ws_forecast_test.py "$$HA_URL" "$$HA_TOKEN" "$$WEATHER_ENTITY" "$$FORECAST_TYPE" | json_pp

ha-outside:
	@set -a; source $(ENV_FILE); set +a; \
	if [[ -z "$$HA_TOKEN" ]]; then echo "HA_TOKEN is empty"; exit 1; fi; \
	if [[ -z "$$OUTSIDE_ENTITY" ]]; then echo "OUTSIDE_ENTITY is empty"; exit 1; fi; \
	curl -s -H "Authorization: Bearer $$HA_TOKEN" "$$HA_URL/api/states/$$OUTSIDE_ENTITY" | json_pp
