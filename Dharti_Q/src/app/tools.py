from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import httpx

from .models import WeatherSnapshot, WebContext

log = logging.getLogger("tools")


class ToolError(RuntimeError):
    pass


_LATLON_RE = re.compile(
    r"(?P<lat>-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(?P<lon>-?\d{1,3}(?:\.\d+)?)"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_lat_lon(text: str) -> Tuple[Optional[float], Optional[float]]:
    if not text:
        return None, None
    m = _LATLON_RE.search(text.strip())
    if not m:
        return None, None
    try:
        lat = float(m.group("lat"))
        lon = float(m.group("lon"))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None, None
        return lat, lon
    except Exception:
        return None, None


def _clean_snippet(title: str, content: str) -> str:
    title = (title or "").strip()
    content = (content or "").strip()
    if title and content:
        return f"{title} — {content}"
    return title or content or ""


def _coerce_alerts(data: dict[str, Any]) -> list[str]:
    alerts_out: list[str] = []
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if isinstance(alerts, list):
        for a in alerts[:3]:
            if not isinstance(a, dict):
                continue
            event = str(a.get("event") or "").strip()
            sender = str(a.get("sender_name") or "").strip()
            if event:
                alerts_out.append(f"{event}" + (f" ({sender})" if sender else ""))
    return alerts_out


def _summary_from_onecall(data: dict[str, Any]) -> tuple[str, Optional[float], Optional[int], Optional[float], list[str]]:
    current = data.get("current") if isinstance(data, dict) else {}
    wlist = (current or {}).get("weather") or []
    desc = ""
    if isinstance(wlist, list) and wlist:
        desc = str((wlist[0] or {}).get("description") or "").strip()

    temp = (current or {}).get("temp")
    humidity = (current or {}).get("humidity")
    wind = (current or {}).get("wind_speed")

    daily = data.get("daily") if isinstance(data, dict) else None
    day_hint = ""
    if isinstance(daily, list) and daily:
        d0 = daily[0] if isinstance(daily[0], dict) else {}
        dweather = d0.get("weather") or []
        if isinstance(dweather, list) and dweather:
            day_hint = str((dweather[0] or {}).get("description") or "").strip()

    parts: list[str] = []
    if desc:
        parts.append(desc.capitalize())
    if isinstance(temp, (int, float)):
        parts.append(f"{temp:.0f}°")
    if day_hint and day_hint.lower() != desc.lower():
        parts.append(f"Today: {day_hint}")

    summary = " • ".join([p for p in parts if p]).strip() or "Weather data available."
    alerts_out = _coerce_alerts(data)

    return (
        summary,
        float(temp) if isinstance(temp, (int, float)) else None,
        int(humidity) if isinstance(humidity, (int, float, int)) else None,
        float(wind) if isinstance(wind, (int, float)) else None,
        alerts_out,
    )


def _summary_from_current(data: dict[str, Any]) -> tuple[str, Optional[float], Optional[int], Optional[float], list[str]]:
    main = data.get("main") if isinstance(data, dict) else {}
    wlist = data.get("weather") if isinstance(data, dict) else []
    wind_obj = data.get("wind") if isinstance(data, dict) else {}

    desc = ""
    if isinstance(wlist, list) and wlist:
        desc = str((wlist[0] or {}).get("description") or "").strip()

    temp = main.get("temp")
    humidity = main.get("humidity")
    wind = wind_obj.get("speed")

    parts: list[str] = []
    if desc:
        parts.append(desc.capitalize())
    if isinstance(temp, (int, float)):
        parts.append(f"{temp:.0f}°")

    summary = " • ".join([p for p in parts if p]).strip() or "Weather data available."
    return (
        summary,
        float(temp) if isinstance(temp, (int, float)) else None,
        int(humidity) if isinstance(humidity, (int, float, int)) else None,
        float(wind) if isinstance(wind, (int, float)) else None,
        [],
    )


@dataclass
class ToolBundle:
    openweather_api_key: str
    openweather_units: str = "metric"
    tavily_api_key: str = ""
    tavily_max_results: int = 5

    async def geocode(self, location_text: str) -> tuple[Optional[float], Optional[float], str]:
        q = (location_text or "").strip()
        if not q:
            return None, None, ""

        if not self.openweather_api_key:
            raise ToolError("Missing OPENWEATHER_API_KEY")

        url = "https://api.openweathermap.org/geo/1.0/direct"
        params = {"q": q, "limit": 1, "appid": self.openweather_api_key}

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            raise ToolError(f"Geocoding failed: {e}") from e

        if not isinstance(data, list) or not data:
            return None, None, q

        item = data[0] if isinstance(data[0], dict) else {}
        lat = item.get("lat")
        lon = item.get("lon")
        name = item.get("name") or q
        state = item.get("state") or ""
        country = item.get("country") or ""
        resolved = ", ".join([x for x in [name, state, country] if x]).strip()

        try:
            return float(lat), float(lon), resolved or q
        except Exception:
            return None, None, resolved or q

    async def weather(self, lat: float, lon: float) -> WeatherSnapshot:
        """
        Robust weather:
        - Try One Call 3.0
        - If 401/403 (not authorized), fallback to One Call 2.5
        - If still fails, fallback to Current Weather 2.5
        """
        if not self.openweather_api_key:
            raise ToolError("Missing OPENWEATHER_API_KEY")

        units = self.openweather_units or "metric"

        async def _get_json(url: str, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(url, params=params)
                status = r.status_code
                try:
                    data = r.json()
                except Exception:
                    data = {}
                if status >= 400:
                    # keep the reason for logs
                    try:
                        r.raise_for_status()
                    except Exception as e:
                        raise httpx.HTTPStatusError(str(e), request=r.request, response=r)
                return status, data

        # 1) One Call 3.0
        url_30 = "https://api.openweathermap.org/data/3.0/onecall"
        params_onecall = {
            "lat": lat,
            "lon": lon,
            "appid": self.openweather_api_key,
            "units": units,
            "exclude": "minutely,hourly",
        }

        try:
            _, data = await _get_json(url_30, params_onecall)
            summary, t, h, w, alerts = _summary_from_onecall(data)
            return WeatherSnapshot(
                fetched_at_utc=_utc_now_iso(),
                summary=summary,
                alerts=alerts,
                temp=t,
                humidity=h,
                wind=w,
            )
        except httpx.HTTPStatusError as e:
            status = getattr(e.response, "status_code", None)
            if status in (401, 403):
                log.warning("OpenWeather One Call 3.0 not authorized (status=%s). Falling back to 2.5.", status)
            else:
                # Non-auth error (e.g., 429/5xx) — still try fallback once
                log.warning("OpenWeather 3.0 error (status=%s). Trying fallback 2.5.", status)
        except Exception as e:
            log.warning("OpenWeather 3.0 failed (%s). Trying fallback 2.5.", e)

        # 2) One Call 2.5 fallback
        url_25 = "https://api.openweathermap.org/data/2.5/onecall"
        try:
            _, data = await _get_json(url_25, params_onecall)
            summary, t, h, w, alerts = _summary_from_onecall(data)
            return WeatherSnapshot(
                fetched_at_utc=_utc_now_iso(),
                summary=summary,
                alerts=alerts,
                temp=t,
                humidity=h,
                wind=w,
            )
        except Exception as e:
            log.warning("OpenWeather One Call 2.5 failed (%s). Falling back to /data/2.5/weather", e)

        # 3) Current Weather 2.5 fallback
        url_curr = "https://api.openweathermap.org/data/2.5/weather"
        params_curr = {"lat": lat, "lon": lon, "appid": self.openweather_api_key, "units": units}
        try:
            _, data = await _get_json(url_curr, params_curr)
            summary, t, h, w, alerts = _summary_from_current(data)
            return WeatherSnapshot(
                fetched_at_utc=_utc_now_iso(),
                summary=summary,
                alerts=alerts,
                temp=t,
                humidity=h,
                wind=w,
            )
        except Exception as e:
            raise ToolError(f"Weather failed: {e}") from e

    async def web(self, query: str, *, time_range: str = "month") -> WebContext:
        q = (query or "").strip()
        if not q:
            return WebContext(query="", snippets=[], urls=[], fetched_at_utc=_utc_now_iso())

        if not self.tavily_api_key:
            raise ToolError("Missing TAVILY_API_KEY")

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.tavily_api_key,
            "query": q,
            "max_results": int(self.tavily_max_results or 5),
            "time_range": time_range,
            "search_depth": "basic",
            "include_images": False,
            "include_answer": False,
        }

        try:
            async with httpx.AsyncClient(timeout=25) as client:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            raise ToolError(f"Tavily web search failed: {e}") from e

        snippets: list[str] = []
        urls: list[str] = []

        results = (data or {}).get("results")
        if isinstance(results, list):
            for item in results[: int(self.tavily_max_results or 5)]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                content = str(item.get("content") or "").strip()
                u = str(item.get("url") or "").strip()
                sn = _clean_snippet(title, content)
                if sn:
                    snippets.append(sn)
                if u:
                    urls.append(u)

        seen = set()
        urls_dedup: list[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                urls_dedup.append(u)

        return WebContext(
            query=q,
            snippets=snippets[:6],
            urls=urls_dedup[:6],
            fetched_at_utc=_utc_now_iso(),
        )

    async def schemes(self, location_text: str, crop: Optional[str]) -> WebContext:
        loc = (location_text or "").strip()
        c = (crop or "").strip().lower()
        q = f"site:gov.in farmer scheme {c} {loc}".strip()
        return await self.web(q, time_range="year")

    async def market_prices(self, location_text: str, crop: Optional[str]) -> WebContext:
        loc = (location_text or "").strip()
        c = (crop or "").strip().lower() or "crop"
        q = f"{c} mandi price today {loc} APMC".strip()
        return await self.web(q, time_range="week")

    async def buy_inputs(self, location_text: str, crop: str) -> WebContext:
        loc = (location_text or "").strip()
        c = (crop or "").strip().lower()
        if not c:
            raise ToolError("Crop is required for buy_inputs()")

        queries = [
            f"buy {c} seeds online India {loc}".strip(),
            f"best fertilizer for {c} buy online India {loc}".strip(),
            f"bio pesticide for {c} buy online India {loc}".strip(),
        ]

        merged_snips: list[str] = []
        merged_urls: list[str] = []

        for q in queries:
            try:
                ctx = await self.web(q, time_range="month")
                merged_snips.extend(ctx.snippets[:3])
                merged_urls.extend(ctx.urls[:3])
            except ToolError:
                log.exception("Buy inputs search failed for query=%s", q)

        seen_u = set()
        urls_dedup: list[str] = []
        for u in merged_urls:
            if u and u not in seen_u:
                seen_u.add(u)
                urls_dedup.append(u)

        seen_s = set()
        snips_dedup: list[str] = []
        for s in merged_snips:
            key = s.lower().strip()
            if key and key not in seen_s:
                seen_s.add(key)
                snips_dedup.append(s)

        return WebContext(
            query=f"buy inputs {c} {loc}".strip(),
            snippets=snips_dedup[:8],
            urls=urls_dedup[:8],
            fetched_at_utc=_utc_now_iso(),
        )
