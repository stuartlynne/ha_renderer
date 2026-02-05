#!/usr/bin/env python3
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import time
import asyncio
from datetime import datetime, time as dt_time, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree
import unicodedata
import urllib.parse

import cairosvg
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image
import websockets
from paho.mqtt import client as mqtt

VERSION = "0.1"


def _coerce_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _battery_percent(voltage: float | None) -> int | None:
    if voltage is None:
        return None
    min_v = 3.2
    max_v = 4.2
    clamped = max(min_v, min(max_v, voltage))
    pct = int(round(((clamped - min_v) / (max_v - min_v)) * 100))
    return pct


def _battery_percent_float(voltage: float | None) -> float | None:
    if voltage is None:
        return None
    min_v = 3.2
    max_v = 4.2
    clamped = max(min_v, min(max_v, voltage))
    return ((clamped - min_v) / (max_v - min_v)) * 100.0


def _derive_device_state(raw_headers: dict[str, str]) -> dict:
    battery_voltage = None
    rssi = None
    fw = None
    device_id = None

    for key, value in raw_headers.items():
        normalized = key.lower().replace("_", "-")
        if normalized in ("battery-voltage", "battery", "battery-v"):
            battery_voltage = _coerce_float(value)
        elif normalized in ("rssi", "wifi-rssi", "signal"):
            rssi = _coerce_float(value)
        elif normalized in ("fw-version", "firmware", "firmware-version"):
            fw = value
        elif normalized in ("id", "device-id", "mac"):
            device_id = value

    percent = _battery_percent(battery_voltage)
    level = None
    if percent is not None:
        if percent >= 80:
            level = "full"
        elif percent >= 50:
            level = "high"
        elif percent >= 20:
            level = "low"
        else:
            level = "empty"

    return {
        "battery_voltage": battery_voltage,
        "battery_percent": percent,
        "battery_level": level,
        "rssi": rssi,
        "fw_version": fw,
        "device_id": device_id,
        "ip": raw_headers.get("ip"),
        "host": raw_headers.get("host"),
    }


def _parse_iso(dt: str | None) -> datetime | None:
    if not dt:
        return None
    value = dt.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo:
        return parsed.astimezone()
    return parsed


def _format_hour_label(dt: str | None) -> str:
    parsed = _parse_iso(dt)
    if not parsed:
        return ""
    return parsed.strftime("%-I%p").lower()


def _format_day_label(dt: str | None) -> str:
    parsed = _parse_iso(dt)
    if not parsed:
        return ""
    return parsed.strftime("%a")


def _format_day_date(dt: str | None) -> str:
    parsed = _parse_iso(dt)
    if not parsed:
        return ""
    return parsed.strftime("%-d")


def _format_condition_label(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().replace("_", " ").replace("-", " ")
    return " ".join(word.capitalize() for word in text.split())


def _coerce_float_value(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _condition_icon(condition: str | None) -> str:
    if not condition:
        return "•"
    key = condition.strip().lower().replace("_", "-")
    mapping = {
        "sunny": "☀",
        "clear": "☀",
        "clear-night": "◐",
        "partlycloudy": "☁",
        "partly-cloudy": "☁",
        "cloudy": "☁",
        "overcast": "☁",
        "rainy": "≋",
        "pouring": "≋",
        "snowy": "❄",
        "snowy-rainy": "❄",
        "fog": "≋",
        "windy": "≋",
        "hail": "☂",
        "lightning": "⚡",
        "lightning-rainy": "⚡",
        
    }
    if key in mapping:
        print(f"_condition_icon: raw condition={condition!r} {mapping[key]}", file=sys.stderr, flush=True)
        return mapping[key]
    key_no_hyphen = key.replace("-", "")
    if key_no_hyphen in mapping:
        return mapping[key_no_hyphen]
    if "-" in key:
        first = key.split("-", 1)[0]
        if first in mapping:
            return mapping[first]
    return "•"


def _strip_variation_selectors(value: str) -> str:
    return value.replace("\ufe0f", "").replace("\ufe0e", "")


def _map_envcan_condition(text: str, is_night: bool = False) -> str:
    if not text:
        return "clear-night" if is_night else "sunny"
    lowered = text.lower()
    if "thunder" in lowered or "lightning" in lowered:
        return "lightning-rainy"
    if "snow" in lowered or "flurr" in lowered:
        return "snowy"
    if "rain" in lowered or "shower" in lowered or "drizzle" in lowered:
        return "rainy"
    if "fog" in lowered or "mist" in lowered:
        return "fog"
    if "cloud" in lowered or "overcast" in lowered:
        if "partly" in lowered or "mix" in lowered:
            return "partlycloudy"
        return "cloudy"
    if "clear" in lowered or "sun" in lowered:
        return "clear-night" if is_night else "sunny"
    return "clear-night" if is_night else "sunny"


def _icon_offset(condition: str | None) -> tuple[int, int]:
    if not condition:
        return (0, 0)
    key = condition.strip().lower().replace("_", "-")
    if key in ("partlycloudy", "partly-cloudy"):
        return (0, -1)
    if key in ("cloudy", "overcast"):
        return (0, -1)
    if key in ("fog",):
        return (0, -2)
    return (0, 0)


def _icon_scale(condition: str | None) -> float:
    if not condition:
        return 1.0
    key = condition.strip().lower().replace("_", "-")
    if key in ("cloudy", "overcast"):
        return 0.85
    if key in ("partlycloudy", "partly-cloudy"):
        return 0.9
    return 1.0


def _convert_temp(value: float, from_unit: str | None, to_unit: str | None) -> float:
    if not from_unit or not to_unit or from_unit == to_unit:
        return value
    from_unit = from_unit.lower().replace("°", "")
    to_unit = to_unit.lower().replace("°", "")
    if from_unit == "c" and to_unit == "f":
        return (value * 9 / 5) + 32
    if from_unit == "f" and to_unit == "c":
        return (value - 32) * 5 / 9
    return value


def _build_graph_series(
    now: datetime,
    history_days: int,
    forecast_days: int,
    history: list[dict],
    forecast_hourly: list[dict],
    forecast_daily: list[dict],
    width: int,
    height: int,
    desired_unit: str | None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[float], float | None, float | None]:
    graph_left = 40
    graph_right = width - 40
    graph_top = 135
    graph_bottom = height - 90
    start_day = (now - timedelta(days=history_days)).date()
    total_days = history_days + forecast_days + 1
    start = datetime.combine(start_day, dt_time(0, 0), tzinfo=now.tzinfo)
    end = start + timedelta(days=total_days)
    span = (end - start).total_seconds()
    if span <= 0:
        return [], [], [], [], [], None, None

    def x_for(ts: datetime) -> float:
        ratio = (ts - start).total_seconds() / span
        ratio = min(max(ratio, 0.0), 1.0)
        return graph_left + ratio * (graph_right - graph_left)

    day_ticks = []
    day_lines = []
    for i in range(total_days):
        day = start_day + timedelta(days=i)
        midnight_dt = datetime.combine(day, dt_time(0, 0), tzinfo=now.tzinfo)
        day_lines.append(round(x_for(midnight_dt)))
        tick_dt = datetime.combine(day, dt_time(12, 0), tzinfo=now.tzinfo)
        day_ticks.append({"x": round(x_for(tick_dt)), "label": tick_dt.strftime("%a")})
    day_lines.append(round(x_for(end)))

    history_points = []
    temps = []
    history_by_date: dict[datetime.date, list[float]] = {}
    for entry in history:
        dt = _parse_iso(entry.get("last_updated")) or _parse_iso(entry.get("last_changed"))
        value = _coerce_float_value(entry.get("state"))
        if dt is None or value is None:
            continue
        unit = _normalize_temp_unit(entry.get("attributes", {}).get("unit_of_measurement"))
        value = _convert_temp(value, unit, desired_unit)
        history_points.append({"ts": dt, "value": value})
        temps.append(value)
        history_by_date.setdefault(dt.date(), []).append(value)

    forecast_high = []
    forecast_low = []
    forecast_by_date: dict[datetime.date, dict[str, float]] = {}
    for item in forecast_daily:
        dt = _parse_iso(item.get("datetime"))
        if dt is None:
            continue
        high = _coerce_float_value(item.get("temperature"))
        low = _coerce_float_value(item.get("templow"))
        if high is not None:
            high = _convert_temp(high, desired_unit, desired_unit)
        if low is not None:
            low = _convert_temp(low, desired_unit, desired_unit)
        if high is None and low is None:
            continue
        forecast_by_date[dt.date()] = {
            "high": high if high is not None else low,
            "low": low if low is not None else high,
        }

    for i in range(total_days):
        day = start_day + timedelta(days=i)
        day_start = datetime.combine(day, dt_time(0, 0), tzinfo=now.tzinfo)
        day_end = day_start + timedelta(days=1)
        if day_end > end:
            day_end = end
        if day in forecast_by_date and day >= now.date():
            high = forecast_by_date[day]["high"]
            low = forecast_by_date[day]["low"]
            if high is not None:
                forecast_high.append({"ts": day_start, "value": high})
                forecast_high.append({"ts": day_end, "value": high})
                temps.append(high)
            if low is not None:
                forecast_low.append({"ts": day_start, "value": low})
                forecast_low.append({"ts": day_end, "value": low})
                temps.append(low)
        elif day in history_by_date:
            day_values = history_by_date[day]
            day_min = min(day_values)
            day_max = max(day_values)
            forecast_high.append({"ts": day_start, "value": day_max})
            forecast_high.append({"ts": day_end, "value": day_max})
            forecast_low.append({"ts": day_start, "value": day_min})
            forecast_low.append({"ts": day_end, "value": day_min})
            temps.extend([day_min, day_max])

    if not temps:
        return [], [], [], day_ticks, day_lines, None, None
    min_temp = min(temps)
    max_temp = max(temps)
    if min_temp == max_temp:
        min_temp -= 1
        max_temp += 1
    pad = max(1.0, (max_temp - min_temp) * 0.1)
    min_temp -= pad
    max_temp += pad

    def y_for(value: float) -> float:
        ratio = (value - min_temp) / (max_temp - min_temp)
        ratio = min(max(ratio, 0.0), 1.0)
        return graph_bottom - ratio * (graph_bottom - graph_top)

    history_points_xy = [{"x": x_for(p["ts"]), "y": y_for(p["value"])} for p in history_points]
    forecast_high_xy = [{"x": x_for(p["ts"]), "y": y_for(p["value"])} for p in forecast_high]
    forecast_low_xy = [{"x": x_for(p["ts"]), "y": y_for(p["value"])} for p in forecast_low]
    if history_points_xy:
        first = history_points_xy[0]
        history_points_xy.insert(0, {"x": graph_left - 10, "y": first["y"]})
    return (
        history_points_xy,
        forecast_high_xy,
        forecast_low_xy,
        day_ticks,
        day_lines,
        round(min_temp, 1),
        round(max_temp, 1),
    )


def _format_temp_pair(temp: float | int | None, unit: str | None, sep: str = "/") -> dict:
    if temp is None:
        return {"c": "--", "f": "--"}
    try:
        value = float(temp)
    except (TypeError, ValueError):
        return {"c": "--", "f": "--"}
    unit = (unit or "").replace("°", "").upper()
    if unit == "C":
        c = round(value)
        f = round((value * 9 / 5) + 32)
    elif unit == "F":
        f = round(value)
        c = round((value - 32) * 5 / 9)
    else:
        return {"c": f"{round(value)}°", "f": ""}
    return {"c": f"{c}°", "f": f"{f}F", "c_value": f"{c}", "c_deg": "°"}


def _normalize_temp_unit(value: str | None, fallback: str | None = None) -> str:
    if value:
        upper = value.strip().upper()
        if "F" in upper:
            return "°F"
        if "C" in upper:
            return "°C"
    if fallback:
        upper = fallback.strip().upper()
        if upper.startswith("F"):
            return "°F"
        if upper.startswith("C"):
            return "°C"
    return ""


def _public_image_url(renderer: "HARenderer") -> str:
    if renderer.public_url:
        return f"{renderer.public_url}/trmnl.{renderer.output_format}"
    return f"/trmnl.{renderer.output_format}"


def _normalize_device_id(value: str | None) -> str:
    if not value:
        return ""
    return str(value).lower().replace(":", "").strip()


def _public_image_url_for(renderer: "HARenderer", device_id: str | None) -> str:
    normalized = _normalize_device_id(device_id)
    base = renderer.public_url or ""
    if normalized:
        suffix = f"/image/{normalized}.{renderer.output_format}"
        return f"{base}{suffix}" if base else suffix
    return _public_image_url(renderer)


def _image_url_for_config(
    renderer: "HARenderer",
    device_id: str | None,
    config: dict,
    image_hash: str | None = None,
) -> str:
    normalized = _normalize_device_id(device_id)
    if image_hash and normalized:
        base = renderer.public_url or ""
        suffix = f"/image/{normalized}/{image_hash}.{renderer.output_format}"
        return f"{base}{suffix}" if base else suffix
    return _public_image_url_for(renderer, device_id)


def _filename_for_image(renderer: "HARenderer", image_hash: str | None) -> str:
    if image_hash:
        return f"{image_hash}.{renderer.output_format}"
    return f"trmnl.{renderer.output_format}"


def _resolve_path(value: str | None, default: Path, use_cwd_if_relative: bool = False) -> Path:
    if value:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate
        if use_cwd_if_relative:
            return (Path.cwd() / candidate).resolve()
        return candidate
    return default


def _resolve_optional_path(value: str | None, use_cwd_if_relative: bool = False) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    if use_cwd_if_relative:
        return (Path.cwd() / candidate).resolve()
    return candidate


def _png_to_bmp(png_bytes: bytes) -> bytes:
    with Image.open(io.BytesIO(png_bytes)) as image:
        gray_levels = _env_int("GRAYSCALE_LEVELS", 0)
        if gray_levels and gray_levels > 2:
            image = _quantize_grayscale(image, gray_levels)
        else:
            dither_mode = os.getenv("BMP_DITHER", "fs").strip().lower()
            if dither_mode in ("none", "off", "0"):
                threshold = _env_int("BMP_THRESHOLD", 140)
                image = image.convert("L").point(
                    lambda p: 255 if p >= threshold else 0, mode="1"
                )
            else:
                image = image.convert("1")
        output = io.BytesIO()
        image.save(output, format="BMP")
        return output.getvalue()


def _content_type(fmt: str) -> str:
    if fmt == "bmp":
        return "image/bmp"
    return "image/png"


def _copy_templates_if_missing(target_dir: Path, source_dir: Path) -> None:
    if not source_dir.exists():
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    for template in source_dir.glob("*.j2"):
        destination = target_dir / template.name
        if not destination.exists():
            shutil.copy2(template, destination)


def _describe_icon(value: str | None) -> str:
    if not value:
        return "empty"
    parts = []
    for char in value:
        codepoint = f"U+{ord(char):04X}"
        name = unicodedata.name(char, "UNKNOWN")
        parts.append(f"{codepoint}:{name}")
    return ",".join(parts)


def _resolve_template_fallback(base_dir: Path, template_path: Path) -> Path:
    if template_path.exists():
        return template_path
    if template_path.name:
        candidate = base_dir / "templates" / template_path.name
        if candidate.exists():
            return candidate
    return template_path


def _template_stem(path: Path) -> str:
    name = path.name
    if name.endswith(".svg.j2"):
        return name[:-7]
    if name.endswith(".j2"):
        return name[:-3]
    return path.stem


def _select_alert_summary(entities_by_id: dict[str, dict], alert_entities: list[str]) -> str:
    for entity_id in alert_entities:
        entity = entities_by_id.get(entity_id)
        if not entity:
            continue
        state = str(entity.get("state", "")).strip().lower()
        if state in ("", "0", "none", "unknown", "unavailable"):
            continue
        attrs = entity.get("attributes", {}) or {}
        alert_text = attrs.get("alert_1") or attrs.get("alert") or attrs.get("summary")
        if alert_text:
            return str(alert_text)
        if state and state not in ("0", "unknown", "unavailable"):
            return str(state)
    return ""


def _clean_summary(text: str) -> str:
    if not text:
        return ""
    prefixes = ("grey", "gray", "yellow", "orange", "red")
    cleaned = text.strip()
    lowered = cleaned.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip(" :-")
            break
    return cleaned


def _wrap_text(text: str, max_chars: int, max_lines: int) -> list[str]:
    if not text:
        return []
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = ""
    for word in words:
        if not current:
            current = word
            continue
        if len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            if len(lines) >= max_lines - 1:
                # Fill the last line as much as possible, then truncate with ellipsis.
                remaining = max_chars - len(current)
                if remaining > 1:
                    snippet = word[: max(0, remaining - 4)]
                    if snippet:
                        current = f"{current} {snippet}..."
                    else:
                        current = f"{current}..."
                else:
                    current = f"{current}..."
                lines.append(current)
                return lines
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        return lines[:max_lines]
    return lines


def _extract_alert_text(entity: dict) -> str:
    if not entity:
        return ""
    state = str(entity.get("state", "")).strip()
    lowered = state.lower()
    if lowered in ("", "0", "none", "unknown", "unavailable"):
        state = ""
    attrs = entity.get("attributes", {}) or {}
    return str(
        attrs.get("alert_1")
        or attrs.get("alert")
        or attrs.get("summary")
        or state
        or ""
    )


def _content_hash(context: dict) -> str:
    payload = {
        k: v
        for k, v in context.items()
        if k not in ("now", "meta", "line_height", "start_y", "device")
    }
    dumped = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _entities_hash(entities: list[dict]) -> str:
    cleaned = []
    drop_attr = {"last_updated", "last_changed", "last_reported", "context"}
    drop_forecast_keys = {"datetime", "time", "last_updated", "last_changed", "last_reported"}
    for entity in entities:
        attrs = entity.get("attributes", {})
        filtered_attrs = {k: v for k, v in attrs.items() if k not in drop_attr}
        for key in ("forecast", "forecast_daily", "forecast_hourly"):
            value = filtered_attrs.get(key)
            if isinstance(value, list):
                normalized_list = []
                for item in value:
                    if isinstance(item, dict):
                        normalized_item = {k: v for k, v in item.items() if k not in drop_forecast_keys}
                        normalized_list.append(normalized_item)
                    else:
                        normalized_list.append(item)
                filtered_attrs[key] = normalized_list
        cleaned.append(
            {
                "entity_id": entity.get("entity_id"),
                "state": entity.get("state"),
                "attributes": filtered_attrs,
            }
        )
    dumped = json.dumps(cleaned, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _quantize_grayscale(image: Image.Image, levels: int) -> Image.Image:
    levels = max(2, min(levels, 16))
    gray = image.convert("L")
    step = 255 / (levels - 1)
    return gray.point(lambda p: int(round(p / step) * step))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    if value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value == "":
        return default
    return value in ("1", "true", "yes", "on")


class HARenderer:
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parent
        self.data_dir = Path("/data") if Path("/data").exists() else self.base_dir / "data"
        default_template = Path("/app/templates/trmnl.svg.j2")
        if not default_template.exists():
            default_template = self.base_dir / "templates" / "trmnl.svg.j2"

        self.ha_url = os.getenv("HA_URL", "http://homeassistant:8123").rstrip("/")
        self.ha_ws_url = os.getenv("HA_WS_URL", "")
        self.ha_token = os.getenv("HA_TOKEN", "").strip()
        self.entities = [e.strip() for e in os.getenv("ENTITIES", "").split(",") if e.strip()]
        self.width = _env_int("WIDTH", 800)
        self.height = _env_int("HEIGHT", 480)
        self.render_scale = max(1, _env_int("RENDER_SCALE", 1))
        self.refresh_seconds = _env_optional_int("REFRESH_SECONDS")
        self.display_refresh_rate = _env_int(
            "DISPLAY_REFRESH_RATE",
            self.refresh_seconds if self.refresh_seconds is not None else 60,
        )
        self.lazy_refresh_max = _env_int("LAZY_REFRESH_MAX", 0)
        print(f"lazy_refresh_max={self.lazy_refresh_max}", file=sys.stderr, flush=True)    
        self.output_format = os.getenv("OUTPUT_FORMAT", "bmp").strip().lower()
        self.save_last_bmp = os.getenv("SAVE_LAST_BMP", "false").lower() in ("1", "true", "yes", "on")
        self.early_display_threshold = _env_int("EARLY_DISPLAY_THRESHOLD", 5)
        self.late_display_threshold = _env_optional_int("LATE_DISPLAY_THRESHOLD")
        self.output_path = _resolve_path(
            os.getenv("OUTPUT_PATH"),
            self.data_dir / f"trmnl.{self.output_format}",
            use_cwd_if_relative=True,
        )
        self.template_path = _resolve_path(
            os.getenv("TEMPLATE_PATH"),
            default_template,
            use_cwd_if_relative=True,
        )
        self.inside_entity = os.getenv("INSIDE_ENTITY", "").strip()
        self.outside_entity = os.getenv("OUTSIDE_ENTITY", "").strip()
        self.weather_entity = os.getenv("WEATHER_ENTITY", "").strip()
        self.inside_entity_units = os.getenv("INSIDE_ENTITY_UNITS", "").strip()
        self.outside_entity_units = os.getenv("OUTSIDE_ENTITY_UNITS", "").strip()
        self.forecast_type = os.getenv("FORECAST_TYPE", "daily").strip().lower()
        self.forecast_hourly_limit = _env_int("FORECAST_HOURLY_LIMIT", 8)
        self.forecast_daily_limit = _env_int("FORECAST_DAILY_LIMIT", 6)
        self.history_days = _env_int("HISTORY_DAYS", 4)
        self.forecast_days = _env_int("FORECAST_DAYS", 3)
        self.render_all_devices_on_refresh = os.getenv(
            "RENDER_ALL_DEVICES_ON_REFRESH", "false"
        ).lower() in ("1", "true", "yes", "on")
        self.icon_offset_scale = _env_int("ICON_OFFSET_SCALE", 10)
        self.icon_offset_scale_big = _env_int("ICON_OFFSET_SCALE_BIG", 30)
        self.forecast_mode = os.getenv("FORECAST_MODE", "websocket").strip().lower()
        self.alert_entities = [
            e.strip()
            for e in os.getenv("ALERT_ENTITIES", "").split()
            if e.strip()
        ]
        self.summary_entity = os.getenv("SUMMARY_ENTITY", "").strip()
        self.alert_cycle_timeout = _env_int("ALERT_CYCLE_TIMEOUT", 120)
        self.battery_capacity_mah = _env_int("BATTERY_CAPACITY", 0)
        self.envcan_enabled = _env_bool("ENABLE_MSC_GEOMET") or _env_bool("ENABLE_MSC_GEONET") or _env_bool("ENVCAN_ENABLE")
        self.envcan_forecast_url = os.getenv("ENVCAN_FORECAST_URL", "").strip()
        self.envcan_geomet_base = os.getenv("ENVCAN_GEOMET_BASE", "https://api.weather.gc.ca").rstrip("/")
        self.envcan_geomet_collection = os.getenv("ENVCAN_GEOMET_COLLECTION", "citypageweather-realtime").strip()
        self.envcan_geomet_item_id = os.getenv("ENVCAN_GEOMET_ITEM_ID", "").strip()
        self.envcan_query_minutes = _env_int(
            "ENVCAN_QUERY_MINUTES",
            _env_int("ENVCAN_QUERY_MINUES", 30),
        )
        self.envcan_enable_amqp = _env_bool("ENVCAN_ENABLE_AMQP")
        self.envcan_amqp_url = os.getenv("ENVCAN_AMQP_URL", "").strip()
        self.envcan_amqp_queue = os.getenv("ENVCAN_AMQP_QUEUE", "").strip()
        self.envcan_last_fetch = 0.0
        self.envcan_forecast: list[dict] = []
        self.envcan_forecast_unit = "C"
        self.envcan_summary = ""
        self.envcan_alerts: list[str] = []
        self.envcan_alert_lock = threading.Lock()
        if not self.envcan_forecast_url and self.envcan_geomet_item_id:
            self.envcan_forecast_url = (
                f"{self.envcan_geomet_base}/collections/{self.envcan_geomet_collection}"
                f"/items/{self.envcan_geomet_item_id}?f=json&lang=en"
            )
        self.public_url = os.getenv("PUBLIC_URL", "").rstrip("/")
        self.log_path = _resolve_path(
            os.getenv("LOG_PATH"),
            self.data_dir / "log.txt",
            use_cwd_if_relative=True,
        )
        self.device_state_path = _resolve_path(
            os.getenv("DEVICE_STATE_PATH"),
            self.data_dir / "device_state.json",
            use_cwd_if_relative=True,
        )
        raw_device_config = os.getenv("DEVICE_CONFIG_PATH")
        self.device_config_path = _resolve_path(
            raw_device_config,
            self.data_dir / "devices.json",
            use_cwd_if_relative=True,
        )
        # Keep any extra artifacts alongside the device config/logs by default.
        self.storage_dir = self.device_config_path.parent
        self.last_bmp_path = _resolve_optional_path(
            os.getenv("SAVE_LAST_BMP_PATH", "").strip(),
            use_cwd_if_relative=True,
        )
        self._bootstrap_storage()
        self.mqtt_enabled = os.getenv("MQTT_ENABLE", "false").lower() in ("1", "true", "yes", "on")
        self.mqtt_host = os.getenv("MQTT_HOST", "mosquitto")
        self.mqtt_port = _env_int("MQTT_PORT", 1883)
        self.mqtt_username = os.getenv("MQTT_USERNAME", "")
        self.mqtt_password = os.getenv("MQTT_PASSWORD", "")
        self.mqtt_prefix = os.getenv("MQTT_PREFIX", "trmnl")
        self.discovery_prefix = os.getenv("MQTT_DISCOVERY_PREFIX", "homeassistant")
        self.mqtt_device_id_suffix_len = _env_optional_int("MQTT_DEVICE_ID_SUFFIX_LEN")
        self.mqtt_device_name_prefix = os.getenv("MQTT_DEVICE_NAME_PREFIX", "TRMNL").strip()
        self.lock = threading.Lock()
        self.last_error = ""
        self.device_state: dict[str, str] = {}
        self.device_config: dict[str, dict] = {}
        self.last_image_hash: dict[str, str] = {}
        self.image_cache: dict[str, bytes] = {}
        self.device_image_keys: dict[str, list[str]] = {}
        self.device_screen_index: dict[str, int] = {}
        self.device_last_display: dict[str, float] = {}
        self.device_last_display_delta: dict[str, float] = {}
        self.device_last_early: dict[str, bool] = {}
        self.last_content_hash: dict[str, str] = {}
        self.lazy_request_count: dict[str, int] = {}
        self.display_lazy_count: dict[str, int] = {}
        self.display_full_count: dict[str, int] = {}
        self.battery_start_percent: dict[str, int] = {}
        self.battery_last_percent: dict[str, int] = {}
        self.battery_start_voltage: dict[str, float] = {}
        self.battery_last_voltage: dict[str, float] = {}
        self.alert_cycle_index: dict[str, int] = {}
        self.alert_cycle_ts: dict[str, float] = {}
        self.last_alert_count: dict[str, int] = {}
        self.max_cache_per_device = _env_int("MAX_CACHE_PER_DEVICE", 3)
        print(
            f"[config] cwd={Path.cwd()} device_config_env={raw_device_config!r} "
            f"device_config_path={self.device_config_path}",
            file=sys.stderr,
            flush=True,
        )
        print(f"[version] {VERSION}", file=sys.stderr, flush=True)
        self._load_device_state()
        self._load_device_config()
        if self.envcan_enable_amqp:
            self._start_envcan_amqp()

    def _load_device_state(self) -> None:
        if not self.device_state_path.exists():
            return
        try:
            data = json.loads(self.device_state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.device_state.update({str(k): str(v) for k, v in data.items()})
        except (OSError, json.JSONDecodeError):
            return

    def _bootstrap_storage(self) -> None:
        try:
            self.device_config_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.device_config_path.exists():
                self.device_config_path.write_text("{}", encoding="utf-8")
        except OSError as exc:
            print(f"[config] unable to initialize {self.device_config_path}: {exc}", file=sys.stderr)

        copy_templates = os.getenv("COPY_TEMPLATES_IF_MISSING", "false").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if copy_templates:
            try:
                template_dir = self.template_path.parent
                _copy_templates_if_missing(template_dir, self.base_dir / "templates")
            except OSError as exc:
                print(f"[config] unable to populate templates: {exc}", file=sys.stderr)

    def _load_device_config(self) -> None:
        self.device_config = {}
        if not self.device_config_path.exists():
            return
        try:
            data = json.loads(self.device_config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                normalized = _normalize_device_id(key)
                self.device_config[normalized] = value
        except (OSError, json.JSONDecodeError):
            return

    def _ensure_device_config(self, device_id: str | None) -> None:
        if not device_id:
            return
        normalized = _normalize_device_id(device_id)
        if not normalized:
            return
        self._load_device_config()
        if normalized in self.device_config:
            return

        sample = {
            "output_path": "trmnl_{device_id}_{screen_index}.bmp",
            "entities": [],
            "screens": [
                "templates/trmnl_default.svg.j2"
            ],
        }

        raw = {}
        if self.device_config_path.exists():
            try:
                raw = json.loads(self.device_config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                print(
                    f"[config] failed to read {self.device_config_path}, recreating",
                    file=sys.stderr,
                    flush=True,
                )
                raw = {}

        if not isinstance(raw, dict):
            raw = {}
        if normalized in raw:
            self.device_config[normalized] = raw.get(normalized, sample)
            return
        raw[normalized] = sample
        self.device_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.device_config_path.write_text(json.dumps(raw, sort_keys=True, indent=2), encoding="utf-8")
        self.device_config[normalized] = sample
        print(
            f"[config] added device {normalized} to {self.device_config_path} (keys={len(raw)})",
            file=sys.stderr,
            flush=True,
        )

    def _fetch_entity(self, entity_id: str) -> dict:
        if not self.ha_token:
            raise RuntimeError("HA_TOKEN is not set")
        headers = {"Authorization": f"Bearer {self.ha_token}"}
        url = f"{self.ha_url}/api/states/{entity_id}"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()

    def _fetch_entities(self, entities: list[str]) -> list[dict]:
        results = []
        fetch_list = list(entities)
        if self.weather_entity and self.weather_entity not in fetch_list:
            fetch_list.append(self.weather_entity)
        for entity_id in fetch_list:
            data = self._fetch_entity(entity_id)
            results.append(data)
        if self.weather_entity:
            for item in results:
                if item.get("entity_id") == self.weather_entity:
                    attrs = item.setdefault("attributes", {})
                    if "forecast" not in attrs:
                        forecast_daily = self._fetch_forecast_websocket("daily")
                        forecast_hourly = self._fetch_forecast_websocket("hourly")
                        if forecast_daily is not None:
                            attrs["forecast_daily"] = forecast_daily
                            attrs["forecast"] = forecast_daily
                        if forecast_hourly is not None:
                            attrs["forecast_hourly"] = forecast_hourly
        return results

    def _fetch_history(self, entity_id: str, days: int) -> list[dict]:
        if not self.ha_token:
            raise RuntimeError("HA_TOKEN is not set")
        if days <= 0:
            return []
        end_time = datetime.now().astimezone()
        start_time = end_time - timedelta(days=days)
        start_iso = start_time.astimezone().isoformat()
        end_iso = end_time.astimezone().isoformat()
        headers = {"Authorization": f"Bearer {self.ha_token}"}
        url = (
            f"{self.ha_url}/api/history/period/{start_iso}"
            f"?filter_entity_id={entity_id}&end_time={end_iso}"
        )
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list) and payload:
            if isinstance(payload[0], list):
                return payload[0]
            if isinstance(payload[0], dict):
                return payload
        return []

    def _fetch_forecast_websocket(self, forecast_type: str) -> list[dict] | None:
        if self.forecast_mode != "websocket":
            return None
        if not self.ha_token or not self.weather_entity:
            return None

        ws_url = self.ha_ws_url
        if not ws_url:
            if self.ha_url.startswith("https://"):
                ws_url = "wss://" + self.ha_url.removeprefix("https://")
            elif self.ha_url.startswith("http://"):
                ws_url = "ws://" + self.ha_url.removeprefix("http://")
            else:
                ws_url = "ws://" + self.ha_url
            ws_url = ws_url.rstrip("/") + "/api/websocket"

        async def _run() -> list[dict] | None:
            async with websockets.connect(ws_url, open_timeout=10) as websocket:
                auth_required = json.loads(await websocket.recv())
                if auth_required.get("type") != "auth_required":
                    return None
                await websocket.send(json.dumps({"type": "auth", "access_token": self.ha_token}))
                auth_resp = json.loads(await websocket.recv())
                if auth_resp.get("type") != "auth_ok":
                    return None
                request_id = 1
                await websocket.send(
                    json.dumps(
                        {
                            "id": request_id,
                            "type": "call_service",
                            "domain": "weather",
                            "service": "get_forecasts",
                            "service_data": {
                                "entity_id": self.weather_entity,
                                "type": forecast_type,
                            },
                            "return_response": True,
                        }
                    )
                )
                while True:
                    message = json.loads(await websocket.recv())
                    if message.get("id") == request_id:
                        if message.get("type") == "result" and message.get("success"):
                            result = message.get("result", {})
                            response = result.get("response", {})
                            if isinstance(response, dict):
                                entity_payload = response.get(self.weather_entity)
                                if isinstance(entity_payload, dict) and isinstance(entity_payload.get("forecast"), list):
                                    return entity_payload["forecast"]
                                if isinstance(response.get("forecast"), list):
                                    return response["forecast"]
                        return None

        try:
            return asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"forecast_ws: {exc}"
            return None

    def _start_envcan_amqp(self) -> None:
        if not self.envcan_amqp_url:
            print(
                "[envcan] AMQP enabled but ENVCAN_AMQP_URL not set",
                file=sys.stderr,
                flush=True,
            )
            return
        try:
            import pika  # type: ignore
        except Exception as exc:  # noqa: BLE001
            print(
                f"[envcan] AMQP enabled but pika is not available: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return

        def _run() -> None:
            while True:
                try:
                    params = pika.URLParameters(self.envcan_amqp_url)
                    connection = pika.BlockingConnection(params)
                    channel = connection.channel()
                    if self.envcan_amqp_queue:
                        channel.queue_declare(queue=self.envcan_amqp_queue, durable=True)
                        channel.basic_qos(prefetch_count=1)

                        def _on_message(ch, method, properties, body) -> None:  # type: ignore[no-untyped-def]
                            text = body.decode("utf-8", errors="replace").strip()
                            if text:
                                with self.envcan_alert_lock:
                                    self.envcan_alerts = [text]
                            ch.basic_ack(delivery_tag=method.delivery_tag)

                        channel.basic_consume(queue=self.envcan_amqp_queue, on_message_callback=_on_message)
                        channel.start_consuming()
                    else:
                        print(
                            "[envcan] AMQP enabled but ENVCAN_AMQP_QUEUE not set",
                            file=sys.stderr,
                            flush=True,
                        )
                        connection.close()
                        return
                except Exception as exc:  # noqa: BLE001
                    print(f"[envcan] AMQP error: {exc}", file=sys.stderr, flush=True)
                    time.sleep(5)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def _get_envcan_alerts(self) -> list[str]:
        with self.envcan_alert_lock:
            return list(self.envcan_alerts)

    def _maybe_refresh_envcan(self) -> None:
        if not self.envcan_enabled or not self.envcan_forecast_url:
            return
        now = time.time()
        refresh_after = self.envcan_query_minutes * 60
        if refresh_after <= 0:
            refresh_after = 1800
        if now - self.envcan_last_fetch < refresh_after and self.envcan_forecast:
            return
        forecast, unit, summary = self._fetch_envcan_forecast_http()
        if forecast:
            self.envcan_forecast = forecast
            self.envcan_forecast_unit = unit or self.envcan_forecast_unit
            if summary:
                self.envcan_summary = summary
            self.envcan_last_fetch = now

    def _fetch_envcan_forecast_http(self) -> tuple[list[dict], str, str]:
        if not self.envcan_forecast_url:
            return ([], "", "")
        try:
            response = requests.get(self.envcan_forecast_url, timeout=20)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"envcan_http: {exc}"
            return ([], "", "")

        content_type = response.headers.get("content-type", "").lower()
        text = response.text
        if "json" in content_type or text.lstrip().startswith("{") or text.lstrip().startswith("["):
            try:
                payload = response.json()
            except ValueError:
                return ([], "", "")
            return self._parse_envcan_json(payload)
        return self._parse_envcan_xml(text)

    def _parse_envcan_json(self, payload: object) -> tuple[list[dict], str, str]:
        def _find_forecast_list(node: object) -> list[dict]:
            if isinstance(node, list) and node and isinstance(node[0], dict):
                if any(key in node[0] for key in ("temperature", "templow", "condition", "period")):
                    return node
            if isinstance(node, dict):
                if isinstance(node.get("forecast"), list):
                    return node["forecast"]  # type: ignore[return-value]
                for value in node.values():
                    found = _find_forecast_list(value)
                    if found:
                        return found
            return []

        node = payload
        if isinstance(payload, dict) and isinstance(payload.get("features"), list) and payload["features"]:
            feature = payload["features"][0]
            if isinstance(feature, dict) and isinstance(feature.get("properties"), dict):
                node = feature["properties"]
            else:
                node = feature

        forecast = _find_forecast_list(node)
        unit = ""
        if isinstance(node, dict):
            unit = str(
                node.get("temperature_unit")
                or node.get("temperatureUnit")
                or node.get("unit")
                or ""
            ).upper()

        summary_long = ""
        if isinstance(node, dict) and isinstance(node.get("forecastGroup"), dict):
            group = node["forecastGroup"]
            forecast = group.get("forecasts") or []
            unit = "C"
            if forecast:
                first = forecast[0]
                if isinstance(first, dict):
                    text_summary = first.get("textSummary")
                    if isinstance(text_summary, dict):
                        summary_long = str(text_summary.get("en") or "")
                    elif text_summary:
                        summary_long = str(text_summary)

        if forecast and isinstance(forecast[0], dict) and "period" in forecast[0]:
            items: list[dict] = []
            now = datetime.now().astimezone()
            day_index = -1
            for fc in forecast:
                period_node = fc.get("period") or {}
                period = (
                    str(period_node.get("textForecastName", {}).get("en"))
                    if isinstance(period_node, dict)
                    else str(period_node)
                )
                summary = ""
                abbr = fc.get("abbreviatedForecast")
                if isinstance(abbr, dict) and isinstance(abbr.get("textSummary"), dict):
                    summary = str(abbr.get("textSummary", {}).get("en") or "")
                if not summary:
                    if isinstance(fc.get("textSummary"), dict):
                        summary = str(fc.get("textSummary", {}).get("en") or "")
                    else:
                        summary = str(fc.get("textSummary") or fc.get("summary") or fc.get("text") or "")

                temp_value = None
                temp_class = ""
                temps = fc.get("temperatures", {}).get("temperature") if isinstance(fc.get("temperatures"), dict) else None
                if isinstance(temps, list) and temps:
                    temp_entry = temps[0]
                    if isinstance(temp_entry, dict):
                        temp_value = _coerce_float_value(temp_entry.get("value", {}).get("en") if isinstance(temp_entry.get("value"), dict) else temp_entry.get("value"))
                        temp_class = str(temp_entry.get("class", {}).get("en") if isinstance(temp_entry.get("class"), dict) else temp_entry.get("class") or "").lower()
                if temp_value is None:
                    temp_value = _coerce_float_value(fc.get("temperature") or fc.get("value"))
                if not temp_class:
                    temp_class = str(fc.get("temperatureClass") or fc.get("class") or "").lower()
                if os.getenv("DEBUG_ENVCAN", "").strip():
                    print(
                        "[envcan] period="
                        f"{period!r} temp={temp_value!r} class={temp_class!r} summary={summary!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                is_night = any(token in period.lower() for token in ("night", "overnight", "tonight"))
                condition = _map_envcan_condition(summary or period, is_night)
                if not is_night:
                    day_index += 1
                    items.append(
                        {
                            "datetime": (now + timedelta(days=max(day_index, 0))).isoformat(),
                            "condition": condition,
                            "night_condition": "",
                            "summary": summary or period,
                            "night_summary": "",
                            "temperature": temp_value,
                            "templow": None,
                        }
                    )
                else:
                    if not items:
                        day_index += 1
                        items.append(
                            {
                                "datetime": (now + timedelta(days=max(day_index, 0))).isoformat(),
                                "condition": condition,
                                "night_condition": condition,
                                "summary": summary or period,
                                "night_summary": summary or period,
                                "temperature": temp_value,
                                "templow": temp_value,
                            }
                        )
                    else:
                        items[-1]["night_condition"] = condition
                        items[-1]["night_summary"] = summary or period
                        if items[-1].get("templow") is None:
                            items[-1]["templow"] = temp_value
                        if temp_class == "low" and items[-1].get("temperature") is None:
                            items[-1]["temperature"] = temp_value
            return (items, unit, summary_long)

        return (forecast, unit, summary_long)

    def _parse_envcan_xml(self, text: str) -> tuple[list[dict], str, str]:
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError:
            return ([], "", "")

        forecasts = root.findall(".//forecast")
        items: list[dict] = []
        summary_long = ""
        now = datetime.now().astimezone()
        day_index = -1
        for fc in forecasts:
            period = (fc.findtext("period") or "").strip()
            summary = (fc.findtext("textSummary") or "").strip()
            if not summary_long and summary:
                summary_long = summary
            temp_node = fc.find(".//temperature")
            temp_value = None
            temp_class = ""
            if temp_node is not None and temp_node.text:
                try:
                    temp_value = float(temp_node.text)
                except ValueError:
                    temp_value = None
                temp_class = temp_node.attrib.get("class", "").lower()
            is_night = any(token in period.lower() for token in ("night", "overnight", "tonight"))
            condition = _map_envcan_condition(summary or period, is_night)
            if not is_night:
                day_index += 1
                items.append(
                    {
                        "datetime": (now + timedelta(days=max(day_index, 0))).isoformat(),
                        "condition": condition,
                        "temperature": temp_value,
                        "templow": None,
                    }
                )
            else:
                if not items:
                    day_index += 1
                    items.append(
                        {
                            "datetime": (now + timedelta(days=max(day_index, 0))).isoformat(),
                            "condition": condition,
                            "temperature": temp_value,
                            "templow": temp_value,
                        }
                    )
                else:
                    if items[-1].get("templow") is None:
                        items[-1]["templow"] = temp_value
                    if temp_class == "low" and items[-1].get("temperature") is None:
                        items[-1]["temperature"] = temp_value

        unit = "C"
        return (items, unit, summary_long)

    def _build_context(self, entities: list[dict], config: dict) -> dict:
        now_dt = datetime.now().astimezone()
        now = now_dt.strftime("%Y-%m-%d %H:%M")
        prepared = []
        by_id: dict[str, dict] = {}
        for entity in entities:
            attrs = entity.get("attributes", {})
            name = attrs.get("friendly_name", entity.get("entity_id", "unknown"))
            state = entity.get("state", "unknown")
            unit = attrs.get("unit_of_measurement", "")
            prepared.append({"name": name, "state": state, "unit": unit})
            if entity.get("entity_id"):
                by_id[entity["entity_id"]] = {
                    "name": name,
                    "state": state,
                    "unit": unit,
                    "attributes": attrs,
                }

        inside = by_id.get(config.get("inside_entity", ""), {}) if config.get("inside_entity") else {}
        outside = by_id.get(config.get("outside_entity", ""), {}) if config.get("outside_entity") else {}
        alert_candidates = []
        if self.envcan_enable_amqp:
            for text in self._get_envcan_alerts():
                if text:
                    alert_candidates.append({"text": text, "kind": "alert"})
        for entity_id in config.get("alert_entities", []):
            text = _extract_alert_text(by_id.get(entity_id, {}))
            if text:
                alert_candidates.append({"text": text, "kind": "alert"})
        summary_entity = config.get("summary_entity")
        if summary_entity:
            summary_text = _extract_alert_text(by_id.get(summary_entity, {}))
            if summary_text:
                alert_candidates.append({"text": summary_text, "kind": "summary"})
        elif self.envcan_enabled and self.envcan_summary:
            alert_candidates.append({"text": self.envcan_summary, "kind": "summary"})

        device_id = ""
        if isinstance(config.get("meta"), dict):
            device_id = str(config["meta"].get("device_id", ""))
        alert_index = self._current_alert_index(device_id, len(alert_candidates))
        normalized = _normalize_device_id(device_id or "") or "default"
        self.last_alert_count[normalized] = len(alert_candidates)
        selected = alert_candidates[alert_index] if alert_candidates else {}
        selected_text = _clean_summary(str(selected.get("text", "")))
        selected_kind = str(selected.get("kind", "")) if selected else ""
        if selected_kind == "summary":
            max_chars = 55 if self.width <= 800 else 66
            alert_summary_lines = _wrap_text(selected_text, max_chars=max_chars, max_lines=3)
        else:
            max_chars = 44 if self.width <= 800 else 54
            alert_summary_lines = _wrap_text(selected_text, max_chars=max_chars, max_lines=2)

        if inside:
            inside_unit_override = config.get("inside_entity_units") or self.inside_entity_units
            inside["unit"] = _normalize_temp_unit(inside.get("unit"), inside_unit_override)
        if outside:
            outside_unit_override = config.get("outside_entity_units") or self.outside_entity_units
            outside["unit"] = _normalize_temp_unit(outside.get("unit"), outside_unit_override)
            outside["temp_pair"] = _format_temp_pair(outside.get("state"), outside.get("unit"))

        weather = {}
        if config.get("weather_entity"):
            weather = by_id.get(config["weather_entity"], {})
        else:
            for entity in entities:
                attrs = entity.get("attributes", {})
                if isinstance(attrs.get("forecast"), list):
                    weather = {
                        "name": attrs.get("friendly_name", entity.get("entity_id", "weather")),
                        "state": entity.get("state", "unknown"),
                        "attributes": attrs,
                    }
                    break
        forecast = []
        temp_unit = ""
        if weather.get("attributes"):
            forecast = weather["attributes"].get("forecast", [])[:5]
            temp_unit = weather["attributes"].get("temperature_unit", "")

        meta = config.get("meta", {})
        if meta.get("hide_device_state"):
            device = {}
        else:
            device = _derive_device_state(self.device_state)

        attrs = weather.get("attributes", {}) if weather else {}
        forecast_daily = attrs.get("forecast_daily", [])
        forecast_hourly = attrs.get("forecast_hourly", [])
        if not forecast_daily:
            forecast_daily = attrs.get("forecast", [])
        if self.envcan_enabled and self.envcan_forecast_url:
            self._maybe_refresh_envcan()
            if self.envcan_forecast:
                forecast_daily = self.envcan_forecast
                temp_unit = self.envcan_forecast_unit or temp_unit

        hourly_items = []
        for item in forecast_hourly[: self.forecast_hourly_limit]:
            icon_dx, icon_dy = _icon_offset(item.get("condition"))
            icon_dx *= self.icon_offset_scale
            icon_dy *= self.icon_offset_scale
            icon_scale = _icon_scale(item.get("condition"))
            icon = _strip_variation_selectors(_condition_icon(item.get("condition")))
            hourly_items.append(
                {
                    "label": _format_hour_label(item.get("datetime")),
                    "temperature": _format_temp_pair(item.get("temperature"), temp_unit),
                    "condition": item.get("condition"),
                    "icon": icon,
                    "icon_dx": icon_dx,
                    "icon_dy": icon_dy,
                    "icon_scale": icon_scale,
                }
            )
            if os.getenv("DEBUG_ICONS", "").strip():
                print(
                    "[icon] hourly "
                    f"label={_format_hour_label(item.get('datetime'))!r} "
                    f"condition={item.get('condition')!r} icon={icon!r} "
                    f"glyphs={_describe_icon(icon)}",
                    file=sys.stderr,
                    flush=True,
                )

        daily_items = []
        for item in forecast_daily[: self.forecast_daily_limit]:
            icon_dx, icon_dy = _icon_offset(item.get("condition"))
            icon_dx *= self.icon_offset_scale
            icon_dy *= self.icon_offset_scale
            icon_scale = _icon_scale(item.get("condition"))
            icon = _strip_variation_selectors(_condition_icon(item.get("condition")))
            night_condition = item.get("night_condition") or item.get("condition")
            night_icon_dx, night_icon_dy = _icon_offset(night_condition)
            night_icon_dx *= self.icon_offset_scale
            night_icon_dy *= self.icon_offset_scale
            night_icon_scale = _icon_scale(night_condition)
            daily_items.append(
                {
                    "label": _format_day_label(item.get("datetime")),
                    "date_label": _format_day_date(item.get("datetime")),
                    "condition_label": item.get("summary") or _format_condition_label(item.get("condition")),
                    "night_condition_label": item.get("night_summary") or item.get("summary") or _format_condition_label(item.get("condition")),
                    "night_label": "Tonight" if len(daily_items) == 0 else "Night",
                    "temperature": _format_temp_pair(item.get("temperature"), temp_unit),
                    "templow": _format_temp_pair(item.get("templow"), temp_unit),
                    "condition": item.get("condition"),
                    "night_condition": item.get("night_condition") or item.get("condition"),
                    "icon": icon,
                    "icon_dx": icon_dx,
                    "icon_dy": icon_dy,
                    "icon_scale": icon_scale,
                    "night_icon": _strip_variation_selectors(_condition_icon(item.get("night_condition") or item.get("condition"))),
                    "night_icon_dx": night_icon_dx,
                    "night_icon_dy": night_icon_dy,
                    "night_icon_scale": night_icon_scale,
                }
            )
            if os.getenv("DEBUG_ICONS", "").strip():
                print(
                    "[icon] daily "
                    f"label={_format_day_label(item.get('datetime'))!r} "
                    f"condition={item.get('condition')!r} icon={icon!r} "
                    f"glyphs={_describe_icon(icon)}",
                    file=sys.stderr,
                    flush=True,
                )

        temp_value = attrs.get("temperature")
        if temp_value is None and forecast_daily:
            first = forecast_daily[0]
            if isinstance(first, dict):
                temp_value = first.get("temperature")

        current = {
            "condition": weather.get("state"),
            "icon": _strip_variation_selectors(_condition_icon(weather.get("state"))),
            "temperature": temp_value,
            "humidity": attrs.get("humidity"),
            "pressure": attrs.get("pressure"),
            "wind_speed": attrs.get("wind_speed"),
            "wind_bearing": attrs.get("wind_bearing"),
        }
        icon_dx, icon_dy = _icon_offset(weather.get("state"))
        icon_scale = _icon_scale(weather.get("state"))
        current["icon_dx"] = icon_dx
        current["icon_dy"] = icon_dy
        current["icon_dx_big"] = icon_dx * self.icon_offset_scale_big
        current["icon_dy_big"] = icon_dy * self.icon_offset_scale_big
        current["icon_scale"] = icon_scale
        current["icon_scale_big"] = icon_scale
        current["temp_pair"] = _format_temp_pair(current.get("temperature"), temp_unit)

        history_points = []
        forecast_high_points = []
        forecast_low_points = []
        day_ticks = []
        day_lines = []
        graph_min = None
        graph_max = None
        if config.get("outside_entity") and self.history_days > 0:
            try:
                history = self._fetch_history(config.get("outside_entity"), self.history_days)
                (
                    history_points,
                    forecast_high_points,
                    forecast_low_points,
                    day_ticks,
                    day_lines,
                    graph_min,
                    graph_max,
                ) = _build_graph_series(
                    now=now_dt,
                    history_days=self.history_days,
                    forecast_days=self.forecast_days,
                    history=history,
                    forecast_hourly=forecast_hourly,
                    forecast_daily=forecast_daily,
                    width=self.width,
                    height=self.height,
                    desired_unit=_normalize_temp_unit(temp_unit),
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"history: {exc}"

        return {
            "width": self.width,
            "height": self.height,
            "now": now,
            "entities": prepared,
            "entities_by_id": by_id,
            "inside": inside,
            "outside": outside,
            "weather": weather,
            "forecast": forecast,
            "temp_unit": temp_unit,
            "forecast_hourly": hourly_items,
            "forecast_daily": daily_items,
            "current_weather": current,
            "alert_summary": alert_summary_lines,
            "alert_summary_kind": selected_kind,
            "history_points": history_points,
            "forecast_high_points": forecast_high_points,
            "forecast_low_points": forecast_low_points,
            "day_ticks": day_ticks,
            "day_lines": day_lines,
            "graph_min": graph_min,
            "graph_max": graph_max,
            "device": device,
            "meta": meta,
            "last_error": self.last_error,
            "line_height": 40,
            "start_y": 120,
        }

    def _render(self, entities: list[dict], config: dict) -> tuple[bytes, str]:
        template_path = _resolve_template_fallback(self.base_dir, config["template_path"])
        if not template_path.exists():
            raise FileNotFoundError(f"template not found: {template_path}")

        env = Environment(
            loader=FileSystemLoader(template_path.parent),
            autoescape=select_autoescape(),
        )
        template = env.get_template(template_path.name)

        content_hash = self._compute_content_hash(entities, config)
        context = self._build_context(entities, config)
        svg = template.render(**context)
        if self.render_scale > 1:
            png_bytes = cairosvg.svg2png(
                bytestring=svg.encode("utf-8"),
                url=str(template_path),
                output_width=self.width * self.render_scale,
                output_height=self.height * self.render_scale,
            )
            with Image.open(io.BytesIO(png_bytes)) as image:
                image = image.resize((self.width, self.height), resample=Image.LANCZOS)
                resized = io.BytesIO()
                image.save(resized, format="PNG")
                png_bytes = resized.getvalue()
        else:
            png_bytes = cairosvg.svg2png(bytestring=svg.encode("utf-8"), url=str(template_path))
        if self.output_format == "png":
            gray_levels = _env_int("GRAYSCALE_LEVELS", 0)
            if gray_levels and gray_levels > 2:
                with Image.open(io.BytesIO(png_bytes)) as image:
                    image = _quantize_grayscale(image, gray_levels)
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    return output.getvalue()
            return png_bytes, content_hash
        return _png_to_bmp(png_bytes), content_hash

    def render_once(self) -> None:
        self.render_for_device(None, self._effective_config_for_screen(None, 0, 1))

    def render_all_devices(self) -> None:
        self._load_device_config()
        device_ids = sorted(self.device_config.keys())
        for device_id in device_ids:
            screen_count = self._screen_count(device_id)
            config = self._effective_config_for_screen(device_id, 0, screen_count)
            self.render_for_device(device_id, config)

    def _content_key(self, device_id: str | None, config: dict) -> str:
        normalized = _normalize_device_id(device_id)
        screen = _template_stem(config["template_path"])
        return f"{normalized or 'default'}:{screen}"

    def _compute_content_hash(self, entities: list[dict], config: dict) -> str:
        return _entities_hash(entities)

    def _log_entity_hashes(self, entities: list[dict]) -> None:
        if not os.getenv("DEBUG_LAZY_ENTITIES", "").strip():
            return
        drop_attr = {"last_updated", "last_changed", "last_reported", "context"}
        drop_forecast_keys = {"datetime", "time", "last_updated", "last_changed", "last_reported"}
        for entity in entities:
            entity_id = entity.get("entity_id")
            attrs = entity.get("attributes", {})
            filtered_attrs = {k: v for k, v in attrs.items() if k not in drop_attr}
            for key in ("forecast", "forecast_daily", "forecast_hourly"):
                value = filtered_attrs.get(key)
                if isinstance(value, list):
                    normalized_list = []
                    for item in value:
                        if isinstance(item, dict):
                            normalized_item = {k: v for k, v in item.items() if k not in drop_forecast_keys}
                            normalized_list.append(normalized_item)
                        else:
                            normalized_list.append(item)
                    filtered_attrs[key] = normalized_list
            payload = {
                "entity_id": entity_id,
                "state": entity.get("state"),
                "attributes": filtered_attrs,
            }
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
            print(f"[lazy-entity] id={entity_id} hash={digest}", file=sys.stderr, flush=True)

    def _current_alert_index(self, device_id: str | None, count: int) -> int:
        if count <= 1:
            return 0
        normalized = _normalize_device_id(device_id or "") or "default"
        last_ts = self.alert_cycle_ts.get(normalized)
        if last_ts and (time.time() - last_ts) > self.alert_cycle_timeout:
            self.alert_cycle_index[normalized] = 0
        return self.alert_cycle_index.get(normalized, 0) % count

    def _advance_alert_cycle(self, device_id: str | None, count: int) -> None:
        if count <= 1:
            return
        normalized = _normalize_device_id(device_id or "") or "default"
        idx = (self.alert_cycle_index.get(normalized, 0) + 1) % count
        self.alert_cycle_index[normalized] = idx
        self.alert_cycle_ts[normalized] = time.time()

    def _update_battery_stats(self, device_id: str | None, headers: dict[str, str]) -> None:
        if not os.getenv("DEBUG_BATTERY", "").strip():
            return
        normalized = _normalize_device_id(device_id) or "default"
        device = _derive_device_state(headers)
        voltage = device.get("battery_voltage")
        if voltage is None:
            return
        percent_f = _battery_percent_float(voltage)
        if percent_f is None:
            return
        if normalized not in self.battery_start_voltage:
            self.battery_start_voltage[normalized] = voltage
            self.battery_last_voltage[normalized] = voltage
            self.battery_start_percent[normalized] = percent_f
            self.battery_last_percent[normalized] = percent_f
            print(
                f"[battery] device={normalized} start={voltage:.2f}V ({percent_f:.1f}%)",
                file=sys.stderr,
                flush=True,
            )
            return
        last_voltage = self.battery_last_voltage.get(normalized)
        if last_voltage is not None and abs(last_voltage - voltage) < 0.001:
            return
        self.battery_last_voltage[normalized] = voltage
        self.battery_last_percent[normalized] = percent_f
        start_v = self.battery_start_voltage.get(normalized, voltage)
        start_pct = self.battery_start_percent.get(normalized, percent_f)
        lazy = self.display_lazy_count.get(normalized, 0)
        full = self.display_full_count.get(normalized, 0)
        total = lazy + full
        delta_v = start_v - voltage
        delta_pct = start_pct - percent_f
        rate_pct = (delta_pct / total) if total else 0.0
        rate_v = (delta_v / total) if total else 0.0
        lazy_pct = (lazy / total * 100.0) if total else 0.0
        mah_per_req = None
        if self.battery_capacity_mah > 0 and total:
            used_mah = (delta_pct / 100.0) * self.battery_capacity_mah
            mah_per_req = used_mah / total
        print(
            f"[battery] device={normalized} current={voltage:.2f}V ({percent_f:.1f}%) "
            f"start={start_v:.2f}V ({start_pct:.1f}%) delta={delta_v:.3f}V ({delta_pct:.2f}%) "
            f"total={total} lazy={lazy} full={full} lazy_pct={lazy_pct:.1f}% "
            f"delta_per_request={rate_v:.4f}V ({rate_pct:.3f}%)"
            f"{'' if mah_per_req is None else f' mah_per_request={mah_per_req:.4f}'}",
            file=sys.stderr,
            flush=True,
        )

    def render_for_device(
        self,
        device_id: str | None,
        config: dict,
        entities_override: list[dict] | None = None,
    ) -> None:
        with self.lock:
            try:
                if entities_override is not None:
                    entities = entities_override
                else:
                    entities_list = config.get("entities", [])
                    if not entities_list:
                        derived = [
                            config.get("inside_entity", ""),
                            config.get("outside_entity", ""),
                            config.get("weather_entity", ""),
                        ]
                        entities_list = [e for e in derived if e]
                        if not entities_list:
                            print(
                                "[render] no entities configured "
                                f"(inside={config.get('inside_entity')}, "
                                f"outside={config.get('outside_entity')}, "
                                f"weather={config.get('weather_entity')})",
                                file=sys.stderr,
                                flush=True,
                            )
                    alert_entities = config.get("alert_entities", [])
                    if alert_entities:
                        for entity_id in alert_entities:
                            if entity_id and entity_id not in entities_list:
                                entities_list.append(entity_id)
                    summary_entity = config.get("summary_entity")
                    if summary_entity and summary_entity not in entities_list:
                        entities_list.append(summary_entity)
                    entities = self._fetch_entities(entities_list) if entities_list else []
                self.last_error = ""
                self._log_entity_snapshot(entities)
                png_bytes, content_hash = self._render(entities, config)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                png_bytes, content_hash = self._render([], config)
            digest = hashlib.sha256(png_bytes).hexdigest()
            normalized = _normalize_device_id(device_id)
            self.last_image_hash[normalized or "default"] = digest
            content_key = self._content_key(device_id, config)
            self.last_content_hash[content_key] = content_hash
            self.lazy_request_count[content_key] = 0
            cache_key = f"{normalized or 'default'}:{digest}"
            self.image_cache[cache_key] = png_bytes
            key_list = self.device_image_keys.setdefault(normalized or "default", [])
            if cache_key not in key_list:
                key_list.append(cache_key)
            while len(key_list) > max(1, self.max_cache_per_device):
                old_key = key_list.pop(0)
                self.image_cache.pop(old_key, None)
            if self.save_last_bmp and self.output_format == "bmp":
                suffix = (normalized or "default")[-6:] or "device"
                screen = _template_stem(config["template_path"])
                if self.last_bmp_path:
                    if "{device_id}" in str(self.last_bmp_path):
                        save_path = Path(
                            str(self.last_bmp_path).format(device_id=normalized or "default")
                        ).resolve()
                    elif self.last_bmp_path.suffix.lower() == ".bmp":
                        save_path = self.last_bmp_path.resolve()
                    else:
                        save_path = (self.last_bmp_path / f"{suffix}-{screen}.bmp").resolve()
                else:
                    save_path = (self.storage_dir / f"{suffix}-{screen}.bmp").resolve()
                print(f"[render] saving last BMP to {save_path}", file=sys.stderr, flush=True)
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(png_bytes)

    def render_loop(self) -> None:
        if self.refresh_seconds is None:
            return
        while True:
            self.render_once()
            if self.render_all_devices_on_refresh:
                self.render_all_devices()
            time.sleep(self.refresh_seconds)

    def _log_entity_snapshot(self, entities: list[dict]) -> None:
        summary = []
        for entity in entities:
            attrs = entity.get("attributes", {})
            name = attrs.get("friendly_name", entity.get("entity_id", "unknown"))
            state = entity.get("state", "unknown")
            unit = attrs.get("unit_of_measurement", "")
            summary.append(f"{name}={state}{unit}")
        if summary:
            print(f"[render] entities: {', '.join(summary)}", file=sys.stderr, flush=True)
        else:
            print("[render] no entities fetched", file=sys.stderr, flush=True)

    def record_request(
        self,
        kind: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: str | None = None,
        client_ip: str | None = None,
        host: str | None = None,
        response: dict | None = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "method": method,
            "path": path,
            "headers": headers,
        }
        if body is not None:
            payload["body"] = body
        if response is not None:
            payload["response"] = response
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")
        self.device_state.update({k.lower(): v for k, v in headers.items()})
        if client_ip:
            self.device_state["ip"] = client_ip
        if host:
            self.device_state["host"] = host
        self.device_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.device_state_path.write_text(
            json.dumps(self.device_state, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        if self.mqtt_enabled:
            self._publish_mqtt()

        if kind in ("display", "setup", "log"):
            device = _derive_device_state(self.device_state)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"[{ts}] [request] {method} {path} "
                f"id={device.get('device_id')} "
                f"fw={device.get('fw_version')} "
                f"battery={device.get('battery_voltage')}V "
                f"rssi={device.get('rssi')}",
                file=sys.stderr,
                flush=True,
            )
            if response is not None:
                print(f"[{ts}] [response] {kind} {response}", file=sys.stderr, flush=True)

    def _effective_config(self, device_id: str | None) -> dict:
        return self._effective_config_for_screen(device_id, 0, total_screens=1)

    def _effective_config_for_screen(self, device_id: str | None, screen_index: int, total_screens: int) -> dict:
        self._load_device_config()
        normalized = _normalize_device_id(device_id or "")
        default_cfg = self.device_config.get("default", {})
        specific_cfg = self.device_config.get(normalized, {})
        has_specific = bool(specific_cfg)

        def merge_dicts(base: dict, overlay: dict) -> dict:
            merged = dict(base)
            merged.update({k: v for k, v in overlay.items() if v is not None})
            return merged

        base_cfg = merge_dicts(default_cfg, {k: v for k, v in specific_cfg.items() if k != "screens"})
        screens_raw = specific_cfg.get("screens") or default_cfg.get("screens") or []
        if screens_raw:
            if screen_index >= len(screens_raw):
                screen_index = 0
            screen_entry = screens_raw[screen_index]
            if isinstance(screen_entry, str):
                raw_cfg = merge_dicts(base_cfg, {"template": screen_entry})
            elif isinstance(screen_entry, dict):
                raw_cfg = merge_dicts(base_cfg, screen_entry)
            else:
                raw_cfg = base_cfg
        else:
            raw_cfg = base_cfg

        def get_value(key: str, fallback: str) -> str:
            return str(raw_cfg.get(key) or fallback)

        def get_list(key: str, fallback: list[str]) -> list[str]:
            raw = raw_cfg.get(key)
            if raw is None:
                return fallback
            if isinstance(raw, list):
                return [str(v) for v in raw if str(v).strip()]
            if isinstance(raw, str):
                return [v.strip() for v in raw.split(",") if v.strip()]
            return fallback

        template_path = Path(get_value("template", str(self.template_path)))
        if not template_path.is_absolute():
            template_path = (Path.cwd() / template_path).resolve()
        template_path = _resolve_template_fallback(self.base_dir, template_path)

        output_template = get_value("output_path", str(self.output_path))
        output_template = output_template.format(
            device_id=normalized or "unknown",
            screen_index=screen_index,
        )
        output_path = Path(output_template)
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()

        meta = {
            "app_name": "TRMNL HA Renderer",
            "device_id": device_id or "unknown",
            "device_id_normalized": normalized or "unknown",
            "config_path": str(self.device_config_path),
            "public_url": self.public_url,
            "host": self.device_state.get("host", ""),
            "screen_index": screen_index,
            "screen_count": total_screens,
            "last_display_delta": self.device_last_display_delta.get(normalized, 0.0),
        }

        if device_id and not has_specific:
            error_template = self.base_dir / "templates" / "trmnl_default.svg.j2"
            if error_template.exists():
                template_path = error_template
            meta["no_setup"] = True

        return {
            "template_path": template_path,
            "output_path": output_path,
            "entities": get_list("entities", self.entities),
            "alert_entities": get_list("alert_entities", self.alert_entities),
            "summary_entity": get_value("summary_entity", self.summary_entity),
            "inside_entity": get_value("inside_entity", self.inside_entity),
            "outside_entity": get_value("outside_entity", self.outside_entity),
            "weather_entity": get_value("weather_entity", self.weather_entity),
            "display_refresh_rate": raw_cfg.get("display_refresh_rate"),
            "inside_entity_units": raw_cfg.get("inside_entity_units"),
            "outside_entity_units": raw_cfg.get("outside_entity_units"),
            "meta": meta,
        }

    def _screen_count(self, device_id: str | None) -> int:
        self._load_device_config()
        normalized = _normalize_device_id(device_id or "")
        default_cfg = self.device_config.get("default", {})
        specific_cfg = self.device_config.get(normalized, {})
        screens = specific_cfg.get("screens") or default_cfg.get("screens") or []
        if isinstance(screens, list) and screens:
            return len(screens)
        return 1

    def _next_screen_index(self, device_id: str | None) -> int:
        normalized = _normalize_device_id(device_id or "default") or "default"
        now = time.time()
        last_ts = self.device_last_display.get(normalized)
        screen_count = self._screen_count(device_id)
        index = self.device_screen_index.get(normalized, 0)

        advance = False
        delta = 0.0
        if last_ts is not None:
            delta = now - last_ts
            if delta < self.early_display_threshold:
                advance = True
            else:
                late_threshold = self.late_display_threshold
                if late_threshold is None:
                    late_threshold = max(1, self.display_refresh_rate * 2)
                if delta > late_threshold:
                    index = 0
        if advance:
            index = (index + 1) % screen_count

        if os.getenv("DEBUG_SCREENS", "").strip():
            late_threshold = self.late_display_threshold
            if late_threshold is None:
                late_threshold = max(1, self.display_refresh_rate * 2)
            print(
                "[screen] "
                f"device={normalized} delta={delta:.2f}s "
                f"early={self.early_display_threshold}s "
                f"late={late_threshold}s "
                f"advance={advance} index={index}/{screen_count}",
                file=sys.stderr,
                flush=True,
            )

        self.device_last_display_delta[normalized] = delta if last_ts is not None else 0.0
        self.device_last_display[normalized] = now
        self.device_screen_index[normalized] = index
        self.device_last_early[normalized] = advance
        return index

    def _mqtt_client(self) -> mqtt.Client:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.mqtt_username:
            client.username_pw_set(self.mqtt_username, self.mqtt_password or None)
        client.connect(self.mqtt_host, self.mqtt_port, keepalive=30)
        return client

    def _publish_mqtt(self) -> None:
        client = None
        try:
            device = _derive_device_state(self.device_state)
            if not device.get("device_id"):
                device_id_full = "unknown"
            else:
                device_id_full = str(device["device_id"]).lower().replace(":", "")

            device_id = device_id_full
            if self.mqtt_device_id_suffix_len:
                device_id = device_id_full[-self.mqtt_device_id_suffix_len :] or device_id_full

            base_topic = f"{self.mqtt_prefix}/{device_id}"
            discovery_id = f"trmnl_{device_id}"
            client = self._mqtt_client()

            def publish_state(suffix: str, payload: dict) -> None:
                client.publish(f"{base_topic}/{suffix}", json.dumps(payload), retain=True)

            def publish_discovery(
                kind: str,
                name: str,
                unit: str | None,
                icon: str,
                state_topic: str,
                value_template: str,
            ) -> None:
                config_topic = f"{self.discovery_prefix}/sensor/{discovery_id}_{kind}/config"
                payload = {
                    "name": name,
                    "state_topic": state_topic,
                    "value_template": value_template,
                    "unique_id": f"{discovery_id}_{kind}",
                    "device": {
                        "identifiers": [discovery_id, device_id_full],
                        "name": f"{self.mqtt_device_name_prefix} {device_id}",
                        "manufacturer": "TRMNL",
                    },
                }
                if unit:
                    payload["unit_of_measurement"] = unit
                if icon:
                    payload["icon"] = icon
                client.publish(config_topic, json.dumps(payload), retain=True)

            publish_state("state", device)

            state_topic = f"{base_topic}/state"
            publish_discovery(
                "battery_voltage",
                "TRMNL Battery Voltage",
                "V",
                "mdi:battery",
                state_topic,
                "{{ value_json.battery_voltage }}",
            )
            publish_discovery(
                "battery_percent",
                "TRMNL Battery Percent",
                "%",
                "mdi:battery",
                state_topic,
                "{{ value_json.battery_percent }}",
            )
            publish_discovery(
                "rssi",
                "TRMNL WiFi RSSI",
                "dBm",
                "mdi:wifi",
                state_topic,
                "{{ value_json.rssi }}",
            )
            publish_discovery(
                "firmware",
                "TRMNL Firmware",
                None,
                "mdi:chip",
                state_topic,
                "{{ value_json.fw_version }}",
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"mqtt: {exc}"
        finally:
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass


def make_handler(renderer: HARenderer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _read_body(self) -> str:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return ""
            return self.rfile.read(length).decode("utf-8", errors="replace")

        def _send_json(self, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/setup"):
                headers = dict(self.headers)
                device_id = headers.get("ID") or headers.get("Id") or headers.get("id")
                renderer._ensure_device_config(device_id)
                config = renderer._effective_config(device_id)
                response = {
                    "status": 200,
                    "api_key": "local",
                    "friendly_id": "LOCAL",
                    "image_url": _image_url_for_config(renderer, device_id, config),
                    "filename": Path(config["output_path"]).name,
                    "image_name": "setup",
                    "message": "Welcome to local renderer!",
                }
                renderer.record_request(
                    "setup",
                    "GET",
                    self.path,
                    headers,
                    client_ip=self.client_address[0],
                    host=headers.get("Host"),
                    response=response,
                )
                self._send_json(response)
                return

            if self.path.startswith("/api/display"):
                headers = dict(self.headers)
                device_id = headers.get("ID") or headers.get("Id") or headers.get("id")
                renderer._ensure_device_config(device_id)
                screen_index = renderer._next_screen_index(device_id)
                screen_count = renderer._screen_count(device_id)
                config = renderer._effective_config_for_screen(device_id, screen_index, screen_count)
                normalized = _normalize_device_id(device_id)
                if renderer.device_last_early.get(normalized or "default", False):
                    alert_count = renderer.last_alert_count.get(normalized or "default", 0)
                    renderer._advance_alert_cycle(device_id, alert_count)
                lazy_max = renderer.lazy_refresh_max
                early_raw = renderer.device_last_early.get(normalized or "default", False)
                ignore_early = os.getenv("LAZY_IGNORE_EARLY", "true").lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
                early_effective = False if ignore_early else early_raw
                entities = None
                if lazy_max > 0 and not early_effective:
                    entities_list = config.get("entities", [])
                    if not entities_list:
                        derived = [
                            config.get("inside_entity", ""),
                            config.get("outside_entity", ""),
                            config.get("weather_entity", ""),
                        ]
                        entities_list = [e for e in derived if e]
                    alert_entities = config.get("alert_entities", [])
                    if alert_entities:
                        for entity_id in alert_entities:
                            if entity_id and entity_id not in entities_list:
                                entities_list.append(entity_id)
                    summary_entity = config.get("summary_entity")
                    if summary_entity and summary_entity not in entities_list:
                        entities_list.append(summary_entity)
                    entities = renderer._fetch_entities(entities_list) if entities_list else []
                    renderer._log_entity_hashes(entities)
                    content_hash = renderer._compute_content_hash(entities, config)
                    content_key = renderer._content_key(device_id, config)
                    last_hash = renderer.last_content_hash.get(content_key)
                    count = renderer.lazy_request_count.get(content_key, 0)
                    if os.getenv("DEBUG_LAZY", "").strip():
                        print(
                            "[lazy] "
                            f"device={normalized or 'default'} "
                            f"key={content_key} "
                            f"count={count} "
                            f"max={lazy_max} "
                            f"last_hash={last_hash or 'none'} "
                            f"content_hash={content_hash} "
                            f"hash_match={bool(last_hash and content_hash == last_hash)} "
                            f"early_raw={early_raw} early_effective={early_effective}",
                            file=sys.stderr,
                            flush=True,
                        )
                    if last_hash and content_hash == last_hash and count < lazy_max:
                        renderer.lazy_request_count[content_key] = count + 1
                        renderer.display_lazy_count[normalized or "default"] = (
                            renderer.display_lazy_count.get(normalized or "default", 0) + 1
                        )
                        image_hash = renderer.last_image_hash.get(normalized or "default", "")
                        display_refresh = config.get("display_refresh_rate")
                        if display_refresh is None or str(display_refresh).strip() == "":
                            display_refresh = renderer.display_refresh_rate
                        response = {
                            "status": 0,
                            "image_url": _image_url_for_config(renderer, device_id, config, image_hash=image_hash),
                            "filename": _filename_for_image(renderer, image_hash),
                            "image_name": f"local-{image_hash[:12] if image_hash else 'unknown'}",
                            "update_firmware": False,
                            "firmware_url": "",
                            "refresh_rate": str(display_refresh),
                            "reset_firmware": False,
                            "image_hash": image_hash,
                        }
                        renderer.record_request(
                            "display",
                            "GET",
                            self.path,
                            headers,
                            client_ip=self.client_address[0],
                            host=headers.get("Host"),
                            response=response,
                        )
                        renderer._update_battery_stats(device_id, headers)
                        self._send_json(response)
                        return
                rendered_now = False
                if renderer.refresh_seconds is None or not renderer.render_all_devices_on_refresh:
                    renderer.render_for_device(device_id, config, entities_override=entities)
                    rendered_now = True
                else:
                    image_hash = renderer.last_image_hash.get(normalized or "default", "")
                    cache_key = f"{normalized or 'default'}:{image_hash}" if image_hash else None
                    if not image_hash or (cache_key and cache_key not in renderer.image_cache):
                        renderer.render_for_device(device_id, config)
                        rendered_now = True
                image_hash = renderer.last_image_hash.get(normalized or "default", "")
                if not image_hash:
                    print(
                        "[image] display missing hash "
                        f"device={normalized or 'default'} rendered={rendered_now} "
                        f"refresh_seconds={renderer.refresh_seconds}",
                        file=sys.stderr,
                        flush=True,
                    )
                display_refresh = config.get("display_refresh_rate")
                if display_refresh is None or str(display_refresh).strip() == "":
                    display_refresh = renderer.display_refresh_rate
                response = {
                    "status": 0,
                    "image_url": _image_url_for_config(renderer, device_id, config, image_hash=image_hash),
                    "filename": _filename_for_image(renderer, image_hash),
                    "image_name": f"local-{image_hash[:12] if image_hash else 'unknown'}",
                    "update_firmware": False,
                    "firmware_url": "",
                    "refresh_rate": str(display_refresh),
                    "reset_firmware": False,
                    "image_hash": image_hash,
                }
                renderer.record_request(
                    "display",
                    "GET",
                    self.path,
                    headers,
                    client_ip=self.client_address[0],
                    host=headers.get("Host"),
                    response=response,
                )
                renderer.display_full_count[normalized or "default"] = (
                    renderer.display_full_count.get(normalized or "default", 0) + 1
                )
                renderer._update_battery_stats(device_id, headers)
                self._send_json(response)
                return

            if self.path.split("?", 1)[0] in ("/", f"/trmnl.{renderer.output_format}"):
                key_list = renderer.device_image_keys.get("default", [])
                if key_list:
                    data = renderer.image_cache.get(key_list[-1])
                    if data is None:
                        self.send_error(HTTPStatus.NOT_FOUND, "render not ready")
                        return
                    print(f"[image] GET {self.path} bytes={len(data)}", file=sys.stderr, flush=True)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", _content_type(renderer.output_format))
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                print(f"[image] MISS {self.path} cache=default", file=sys.stderr, flush=True)
                self.send_error(HTTPStatus.NOT_FOUND, "render not ready")
                return

            if self.path.split("?", 1)[0].startswith("/image/") and self.path.split("?", 1)[0].endswith(
                f".{renderer.output_format}"
            ):
                path_only = self.path.split("?", 1)[0]
                parts = path_only.strip("/").split("/")
                cache_key = None
                if len(parts) == 3:
                    _, device_id, filename = parts
                    image_hash = filename.removesuffix(f".{renderer.output_format}")
                    cache_key = f"{device_id}:{image_hash}"
                    data = renderer.image_cache.get(cache_key)
                    if data:
                        print(f"[image] GET {self.path} bytes={len(data)}", file=sys.stderr, flush=True)
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", _content_type(renderer.output_format))
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-store, max-age=0")
                        self.send_header("Pragma", "no-cache")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                print(
                    f"[image] MISS {self.path} cache_key={cache_key}",
                    file=sys.stderr,
                    flush=True,
                )
                self.send_error(HTTPStatus.NOT_FOUND, "render not ready")
                return

            if self.path == "/health":
                self._send_json(
                    {
                        "status": "ok",
                        "last_error": renderer.last_error,
                        "device": _derive_device_state(renderer.device_state),
                    }
                )
                return

            if self.path == "/render":
                renderer.render_once()
                self._send_json({"status": "rendered"})
                return

            if self.path.split("?", 1)[0] == "/view":
                params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                device_id = None
                if "device" in params:
                    device_id = params["device"][0]
                if device_id is None:
                    device_id = _derive_device_state(renderer.device_state).get("device_id")
                if device_id is None:
                    device_id = "default"
                screen_index = 1
                if "screen" in params:
                    try:
                        screen_index = int(params["screen"][0])
                    except ValueError:
                        screen_index = 0
                refresh = params.get("refresh", [str(renderer.display_refresh_rate)])[0]
                normalized = _normalize_device_id(device_id)
                if normalized:
                    screen_count = renderer._screen_count(device_id)
                    config = renderer._effective_config_for_screen(
                        device_id, screen_index, screen_count
                    )
                    config = dict(config)
                    config_meta = dict(config.get("meta", {}))
                    config_meta["hide_device_state"] = True
                    config["meta"] = config_meta
                    renderer.render_for_device(device_id, config)
                image_hash = ""
                if normalized:
                    image_hash = renderer.last_image_hash.get(normalized, "")
                image_url = "/trmnl.bmp"
                if normalized and image_hash:
                    image_url = f"/image/{normalized}/{image_hash}.{renderer.output_format}"
                html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta http-equiv="refresh" content="{refresh}" />
    <title>TRMNL View</title>
    <style>
      html, body {{ margin: 0; padding: 0; background: #fff; height: 100%; }}
      body {{ display: flex; align-items: center; justify-content: center; }}
      img {{ max-width: 100%; max-height: 100%; image-rendering: pixelated; }}
    </style>
  </head>
  <body>
    <img src="{image_url}" alt="TRMNL" />
  </body>
</html>
"""
                data = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            if self.path.startswith("/api/log"):
                body = self._read_body()
                headers = dict(self.headers)
                response = {"status": "ok"}
                renderer.record_request(
                    "log",
                    "POST",
                    self.path,
                    headers,
                    body,
                    client_ip=self.client_address[0],
                    host=headers.get("Host"),
                    response=response,
                )
                self._send_json(response)
                return

            self.send_error(HTTPStatus.NOT_FOUND, "not found")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return Handler


def main() -> None:
    renderer = HARenderer()
    renderer.render_once()

    thread = threading.Thread(target=renderer.render_loop, daemon=True)
    thread.start()

    host = os.getenv("HOST", "0.0.0.0")
    port = _env_int("PORT", 9080)
    try:
        server = ThreadingHTTPServer((host, port), make_handler(renderer))
    except OSError as exc:
        raise OSError(f"Failed to bind HTTP server on {host}:{port} ({exc})") from exc
    server.serve_forever()


if __name__ == "__main__":
    main()
