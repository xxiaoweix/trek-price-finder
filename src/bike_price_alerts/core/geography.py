"""Small, brand-neutral geography helpers for continental-US discovery."""

from __future__ import annotations

import math

MILES_PER_LATITUDE_DEGREE = 69.0

# Conservative polygon used to place query centres. It intentionally excludes
# Alaska, Hawaii, and territories; edge coverage comes from nearby centres.
CONUS_OUTLINE = (
    (-124.8, 48.5), (-123.0, 46.0), (-124.5, 42.0), (-123.0, 38.0),
    (-120.0, 34.5), (-117.2, 32.5), (-111.0, 31.3), (-106.5, 31.8),
    (-103.0, 29.5), (-97.0, 25.8), (-82.0, 24.4), (-80.0, 26.0),
    (-80.0, 31.0), (-78.0, 33.5), (-75.0, 35.5), (-70.0, 41.0),
    (-67.0, 44.8), (-69.5, 47.0), (-82.0, 45.0), (-89.0, 48.8),
    (-95.0, 49.0), (-104.0, 49.0), (-111.0, 49.0), (-117.0, 49.0),
)

CONUS_STATE_CODES = frozenset({
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA",
    "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM",
    "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
})


def inside_conus_outline(lat: float, lng: float) -> bool:
    """Return whether a query centre belongs inside the CONUS seed polygon."""
    inside = False
    previous = CONUS_OUTLINE[-1]
    for current in CONUS_OUTLINE:
        x1, y1 = previous
        x2, y2 = current
        if (y1 > lat) != (y2 > lat):
            crossing_lng = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lng < crossing_lng:
                inside = not inside
        previous = current
    return inside


def conus_grid_points(spacing_miles: float) -> tuple[tuple[str, float, float], ...]:
    """Create deterministic, staggered query centres across the lower 48/DC."""
    if not 40 <= spacing_miles <= 500:
        raise ValueError("CONUS grid spacing must be between 40 and 500 miles")
    lat_step = spacing_miles / MILES_PER_LATITUDE_DEGREE
    points: list[tuple[str, float, float]] = []
    row = 0
    lat = 24.4 + lat_step / 2
    while lat <= 49.0:
        miles_per_lng_degree = MILES_PER_LATITUDE_DEGREE * math.cos(math.radians(lat))
        lng_step = spacing_miles / miles_per_lng_degree
        lng = -124.8 + lng_step / 2 + (lng_step / 2 if row % 2 else 0)
        while lng <= -67.0:
            if inside_conus_outline(lat, lng):
                points.append((f"CONUS {lat:.3f},{lng:.3f}", round(lat, 6), round(lng, 6)))
            lng += lng_step
        row += 1
        lat += lat_step
    return tuple(points)
