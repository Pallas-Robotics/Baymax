"""Approximate IP location and user-confirmed city selection; memory-only cache."""
import copy
import math
import threading
import time
from datetime import datetime, timezone

import requests


def fetch_json(url, params=None):
    response = requests.get(url, params=params, timeout=(3, 5))
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("Invalid service response")
    return value


def coordinates(data):
    lat, lon = float(data["latitude"]), float(data["longitude"])
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Invalid coordinates")
    return lat, lon


class Location:
    def __init__(self, fetch=fetch_json, clock=time.monotonic):
        self.fetch, self.clock = fetch, clock
        self.lock = threading.RLock()
        self.cached, self.expires, self.confirmed = None, 0, None

    def get(self):
        with self.lock:
            if self.confirmed:
                return copy.deepcopy(self.confirmed)
            if self.cached and self.clock() < self.expires:
                return copy.deepcopy(self.cached)
            result, ttl = self._lookup()
            self.cached, self.expires = result, self.clock() + ttl
            return copy.deepcopy(result)

    def refresh(self):
        """Look the IP location up again before the hour's copy expires, so a weather
        question never waits on IPWho. A confirmed city needs no lookup. The lookup runs
        outside the lock; a failed one keeps a copy that is still valid."""
        with self.lock:
            if self.confirmed:
                return
        result, ttl = self._lookup()
        with self.lock:
            if ("error" in result and self.cached and "error" not in self.cached
                    and self.clock() < self.expires):
                return
            self.cached, self.expires = result, self.clock() + ttl

    def _lookup(self):
        """(result, seconds to keep it) from IPWho."""
        try:
            data = self.fetch("https://ipwho.is/", {"fields": "success,city,region,country,country_code,latitude,longitude"})
            if data.get("success") is not True:
                raise ValueError("Location unavailable")
            lat, lon = coordinates(data)
            return {"city": data.get("city", ""), "region": data.get("region", ""),
                    "country": data.get("country", ""), "country_code": data.get("country_code", ""),
                    "latitude": lat, "longitude": lon, "source": "ipwho.is",
                    "approximate": True, "accuracy": "city/region estimate; not GPS or a street address",
                    "notice": "VPNs, mobile networks and ISP routing can place you elsewhere. Say the place is approximate; the user can correct it.",
                    "fetched_at": datetime.now(timezone.utc).isoformat()}, 3600
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {"error": "Could not estimate location. Ask which city, region and country the user is in."}, 30

    def set_city(self, city, country_code="", region=""):
        if not isinstance(city, str) or not city.strip() or len(city) > 120:
            return {"error": "Ask for a city name."}
        if not isinstance(country_code, str) or (country_code and (len(country_code) != 2 or not country_code.isalpha())):
            return {"error": "Use a two-letter country code."}
        if not isinstance(region, str) or len(region) > 120:
            return {"error": "Ask for a state or region."}
        try:
            params = {"name": city.strip(), "count": 10, "language": "en", "format": "json"}
            if country_code:
                params["countryCode"] = country_code.upper()
            data = self.fetch("https://geocoding-api.open-meteo.com/v1/search", params)
            choices = []
            for entry in data.get("results", []):
                if region and region.casefold() != entry.get("admin1", "").casefold():
                    continue
                lat, lon = coordinates(entry)
                choices.append({"city": entry["name"], "region": entry.get("admin1", ""),
                                "country": entry.get("country", ""), "country_code": entry.get("country_code", ""),
                                "latitude": lat, "longitude": lon})
            if len(choices) != 1:
                return {"needs_clarification": True, "choices": choices,
                        "message": "Ask the user to specify the city, full state/region and country. Location has not changed."}
            result = {**choices[0], "source": "user-confirmed city via Open-Meteo geocoding",
                      "approximate": True, "accuracy": "city centre; not exact user position"}
            with self.lock:
                self.confirmed = result
            return copy.deepcopy(result)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return {"error": "City lookup failed. Location has not changed; ask the user to try again."}
