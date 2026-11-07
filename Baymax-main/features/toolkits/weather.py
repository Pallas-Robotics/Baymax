"""Current model-based weather and today's forecast from Open-Meteo."""
import copy
import threading
import time
from datetime import datetime, timezone
import requests
from .location import Location, fetch_json


class Weather:
    def __init__(self, location, fetch=fetch_json, clock=time.monotonic):
        self.location, self.fetch, self.clock = location, fetch, clock
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, units="celsius", city="", country_code="", region=""):
        if units not in ("celsius", "fahrenheit"):
            return {"error": "Choose celsius or fahrenheit."}
        # A forecast for another city must not move the user's current location.
        place = (Location(self.location.fetch).set_city(city, country_code, region)
                 if city else self.location.get())
        if "error" in place or place.get("needs_clarification"):
            return place
        key = (place["latitude"], place["longitude"], units)
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() < cached[0]:
                return {**copy.deepcopy(cached[1]), "location": place, "cached": True}
            result, ttl = self._fetch(place, units)
            self._store(key, result, ttl)
            return copy.deepcopy(result)

    def refresh(self):
        """Fetch the user's weather again before the cached copy expires, in each unit
        asked for there (celsius at least), so a weather question never waits on
        Open-Meteo. The fetch runs outside the lock, so a question meanwhile gets the
        cached copy; a failed fetch keeps a copy that is still valid."""
        place = self.location.get()
        if "error" in place or place.get("needs_clarification"):
            return
        here = (place["latitude"], place["longitude"])
        with self.lock:
            asked = {key[2] for key in self.cache if key[:2] == here} | {"celsius"}
        for units in sorted(asked):
            result, ttl = self._fetch(place, units)
            with self.lock:
                cached = self.cache.get(here + (units,))
                if "error" in result and cached and "error" not in cached[1] and self.clock() < cached[0]:
                    continue
                self._store(here + (units,), result, ttl)

    def _fetch(self, place, units):
        """(result, seconds to keep it) from Open-Meteo for `place`."""
        try:
            data = self.fetch("https://api.open-meteo.com/v1/forecast", {
                "latitude": place["latitude"], "longitude": place["longitude"], "temperature_unit": units,
                "wind_speed_unit": "mph" if units == "fahrenheit" else "kmh",
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto", "forecast_days": 1})
            if not data.get("current") or not data.get("daily"):
                raise ValueError("Weather unavailable")
            return {"location": place, "current": data["current"], "current_units": data.get("current_units", {}),
                    "today": data["daily"], "daily_units": data.get("daily_units", {}),
                    "timezone": data.get("timezone"), "source": "Open-Meteo", "source_url": "https://open-meteo.com/",
                    "notice": "Model-based weather for these coordinates; IP-derived location may be wrong.",
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "cached": False}, 600
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {"error": "Weather service is unavailable. Do not invent current conditions.", "location": place}, 30

    def _store(self, key, result, ttl):
        # Caller holds the lock. Bound the cache when a user visits many cities.
        if len(self.cache) >= 32:
            self.cache.clear()
        self.cache[key] = (self.clock() + ttl, result)
