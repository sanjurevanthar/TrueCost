from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Optional


class GeoService:
    EARTH_RADIUS_MILES = 3958.8

    def distance_miles(self, lat1: Optional[float], lng1: Optional[float],
                       lat2: Optional[float], lng2: Optional[float]) -> Optional[float]:
        if None in (lat1, lng1, lat2, lng2):
            return None
        d_lat, d_lng = radians(lat2 - lat1), radians(lng2 - lng1)
        a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lng / 2) ** 2
        return round(2 * self.EARTH_RADIUS_MILES * asin(sqrt(a)), 1)
