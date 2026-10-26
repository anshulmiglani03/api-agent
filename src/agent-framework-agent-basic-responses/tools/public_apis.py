"""Tools backed by free public APIs (no key needed), useful for testing the agent."""

from typing import Annotated, Any
from urllib.parse import quote

from pydantic import Field

from api_client import call_api


async def get_country_info(
    name: Annotated[str, Field(description="Country name in English, e.g. 'Germany'.")],
) -> Any:
    """Get facts about a country: capital, population, region, languages and currencies."""
    data = await call_api("GET", f"https://restcountries.com/v3.1/name/{quote(name, safe='')}", auth=False)
    if isinstance(data, dict):
        return data
    return [
        {
            "name": c["name"]["common"],
            "capital": c.get("capital"),
            "population": c.get("population"),
            "region": c.get("region"),
            "languages": list(c.get("languages", {}).values()),
            "currencies": list(c.get("currencies", {})),
        }
        for c in data[:3]
    ]


async def get_weather(
    latitude: Annotated[float, Field(description="Latitude of the location.", ge=-90, le=90)],
    longitude: Annotated[float, Field(description="Longitude of the location.", ge=-180, le=180)],
) -> Any:
    """Get the current weather (temperature in C, wind speed in km/h) for a latitude/longitude."""
    return await call_api(
        "GET",
        "https://api.open-meteo.com/v1/forecast",
        params={"latitude": latitude, "longitude": longitude, "current_weather": "true"},
        auth=False,
    )


async def geocode_city(
    city: Annotated[str, Field(description="City name, e.g. 'Paris'.")],
) -> Any:
    """Look up the latitude/longitude of a city. Use before get_weather when only a city name is known."""
    data = await call_api(
        "GET", "https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 3}, auth=False
    )
    if "error" in data:
        return data
    return [
        {"name": r["name"], "country": r.get("country"), "latitude": r["latitude"], "longitude": r["longitude"]}
        for r in data.get("results", [])
    ]
