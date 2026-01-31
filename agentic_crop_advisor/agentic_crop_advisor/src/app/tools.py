from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .models import WeatherSnapshot, WebContext


class ToolError(RuntimeError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_LAT_LON_RE = re.compile(
    r"""
    (?:
        lat\s*[:=]?\s*(?P<lat1>-?\d+(?:\.\d+)?)\s*[,\s]+lon\s*[:=]?\s*(?P<lon1>-?\d+(?:\.\d+)?)
    )
    |
    (?:
        (?P<lat2>-?\d+(?:\.\d+)?)\s*,\s*(?P<lon2>-?\d+(?:\.\d+)?)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def extract_lat_lon(text: str) -> tuple[Optional[float], Optional[float]]:
    """
    Extract lat/lon from user text:
      - "lat 19.07 lon 72.87"
      - "19.07,72.87"
    """
    m = _LAT_LON_RE.search(text or "")
    if not m:
        return None, None

    lat_s = m.group("lat1") or m.group("lat2")
    lon_s = m.group("lon1") or m.group("lon2")
    try:
        lat = float(lat_s) if lat_s is not None else None
        lon = float(lon_s) if lon_s is not None else None
    except ValueError:
        return None, None

    if lat is None or lon is None:
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=4),
    stop=stop_after_attempt(3),
)
async def _http_get_json(url: str, params: dict[str, Any], headers: Optional[dict[str, str]] = None) -> Any:
    timeout = httpx.Timeout(connect=5.0, read=12.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()


@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.TransportError)),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=4),
    stop=stop_after_attempt(3),
)
async def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def geocode_place_openweather(
    api_key: str,
    place: str,
    *,
    limit: int = 1,
) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """
    Direct geocoding using OpenWeather Geocoding API.
    Returns (lat, lon, resolved_name).
    """
    place = (place or "").strip()
    if not place:
        return None, None, None

    url = "https://api.openweathermap.org/geo/1.0/direct"
    params = {"q": place, "limit": max(1, min(limit, 5)), "appid": api_key}

    try:
        data = await _http_get_json(url, params=params)
    except httpx.HTTPStatusError as e:
        raise ToolError(f"Geocoding failed (HTTP {e.response.status_code}).") from e
    except httpx.HTTPError as e:
        raise ToolError("Geocoding failed (network error).") from e

    if not isinstance(data, list) or not data:
        return None, None, None

    top = data[0] or {}
    lat = top.get("lat")
    lon = top.get("lon")
    name = top.get("name")
    country = top.get("country")
    state = top.get("state")

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None, None, None

    resolved = ", ".join([x for x in [name, state, country] if x])
    return lat_f, lon_f, resolved or None


def _summarize_openweather(onecall: dict[str, Any], units: str) -> tuple[str, list[str]]:
    """
    Build a short, Telegram-friendly weather summary + alert lines.
    """
    current = onecall.get("current") or {}
    weather_arr = current.get("weather") or []
    main = weather_arr[0].get("main") if weather_arr else None
    desc = weather_arr[0].get("description") if weather_arr else None

    temp = current.get("temp")
    humidity = current.get("humidity")

    unit_symbol = "°C" if units == "metric" else ("°F" if units == "imperial" else "K")

    parts: list[str] = []
    if main or desc:
        parts.append(f"{(main or '').strip()}: {(desc or '').strip()}".strip(": ").strip())
    if isinstance(temp, (int, float)):
        parts.append(f"Temp {round(float(temp), 1)}{unit_symbol}")
    if isinstance(humidity, (int, float)):
        parts.append(f"Humidity {int(humidity)}%")

    alerts_in = onecall.get("alerts") or []
    alert_lines: list[str] = []
    for a in alerts_in[:3]:
        event = (a or {}).get("event") or "Alert"
        alert_lines.append(str(event))

    summary = " | ".join([p for p in parts if p]) if parts else "Weather fetched."
    return summary, alert_lines


async def fetch_weather_onecall(
    api_key: str,
    lat: float,
    lon: float,
    *,
    units: str = "metric",
    exclude: str = "minutely",
    lang: str = "en",
) -> WeatherSnapshot:
    """
    OpenWeather One Call API 3.0.
    Returns a compact snapshot with limited daily/hourly arrays for LLM context.
    """
    url = "https://api.openweathermap.org/data/3.0/onecall"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": api_key,
        "units": units,
        "exclude": exclude,
        "lang": lang,
    }

    try:
        onecall = await _http_get_json(url, params=params)
    except httpx.HTTPStatusError as e:
        # OpenWeather returns useful JSON errors; avoid leaking full payload
        raise ToolError(f"Weather fetch failed (HTTP {e.response.status_code}).") from e
    except httpx.HTTPError as e:
        raise ToolError("Weather fetch failed (network error).") from e

    if not isinstance(onecall, dict):
        raise ToolError("Weather fetch failed (invalid response).")

    summary, alert_lines = _summarize_openweather(onecall, units=units)

    # Keep only what we need to avoid blowing context window
    daily = (onecall.get("daily") or [])[:5]
    hourly = (onecall.get("hourly") or [])[:12]

    return WeatherSnapshot(
        fetched_at_utc=_utc_now_iso(),
        summary=summary,
        alerts=alert_lines,
        daily=daily if isinstance(daily, list) else [],
        hourly=hourly if isinstance(hourly, list) else [],
    )


async def tavily_search(
    api_key: str,
    query: str,
    *,
    max_results: int = 5,
    search_depth: str = "basic",
    topic: str = "general",
    include_answer: bool = False,
    time_range: Optional[str] = None,
) -> WebContext:
    """
    Tavily /search endpoint.
    Returns a compact WebContext with snippets + urls.
    """
    q = (query or "").strip()
    if not q:
        raise ToolError("Tavily search query is empty.")

    url = "https://api.tavily.com/search"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    payload: dict[str, Any] = {
        "query": q,
        "max_results": max(1, min(int(max_results), 10)),
        "search_depth": search_depth,
        "topic": topic,
        "include_answer": include_answer,
        "include_raw_content": False,
        "include_images": False,
    }
    if time_range:
        payload["time_range"] = time_range  # e.g., "week", "month"

    try:
        data = await _http_post_json(url, payload=payload, headers=headers)
    except httpx.HTTPStatusError as e:
        raise ToolError(f"Tavily search failed (HTTP {e.response.status_code}).") from e
    except httpx.HTTPError as e:
        raise ToolError("Tavily search failed (network error).") from e

    if not isinstance(data, dict):
        raise ToolError("Tavily search failed (invalid response).")

    results = data.get("results") or []
    snippets: list[str] = []
    urls: list[str] = []

    if isinstance(results, list):
        for r in results[: payload["max_results"]]:
            if not isinstance(r, dict):
                continue
            u = r.get("url")
            c = r.get("content")
            t = r.get("title")
            if isinstance(u, str) and u:
                urls.append(u)
            # Build short snippet
            line_parts = []
            if isinstance(t, str) and t.strip():
                line_parts.append(t.strip())
            if isinstance(c, str) and c.strip():
                line_parts.append(c.strip())
            if line_parts:
                snippets.append(" — ".join(line_parts)[:700])

    return WebContext(
        fetched_at_utc=_utc_now_iso(),
        query=q,
        snippets=snippets[:8],
        urls=urls[:8],
    )


@dataclass(frozen=True)
class ToolBundle:
    """
    Convenience wrapper so graph nodes can pass one object around.
    Keep it tiny.
    """
    openweather_api_key: str
    openweather_units: str
    tavily_api_key: str
    tavily_max_results: int

    async def geocode(self, place: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
        return await geocode_place_openweather(self.openweather_api_key, place)

    async def weather(self, lat: float, lon: float) -> WeatherSnapshot:
        return await fetch_weather_onecall(
            api_key=self.openweather_api_key,
            lat=lat,
            lon=lon,
            units=self.openweather_units,
        )

    async def web(self, query: str, *, time_range: Optional[str] = None) -> WebContext:
        return await tavily_search(
            api_key=self.tavily_api_key,
            query=query,
            max_results=self.tavily_max_results,
            time_range=time_range,
        )
