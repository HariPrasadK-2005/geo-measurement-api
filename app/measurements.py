"""Area / length calculation.

Strategy (see README "CRS handling"):
  1. Find the feature's centre in longitude/latitude (WGS84).
  2. Pick the matching UTM zone (metre-based, low distortion) for that centre.
  3. Re-project the feature from its source CRS into that UTM zone.
  4. Measure with shapely in the projected plane -> square metres / metres.

Degrees are NEVER used for measuring.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

import numpy as np
import shapely
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry

WGS84 = CRS.from_epsg(4326)

AREA_TYPES = {"Polygon", "MultiPolygon"}
LENGTH_TYPES = {"LineString", "MultiLineString"}
NO_MEASUREMENT_TYPES = {"Point", "MultiPoint"}


@lru_cache(maxsize=128)
def _transformer(source: CRS, target: CRS) -> Transformer:
    # always_xy=True -> coordinates are always (x=longitude/easting, y=latitude/northing)
    return Transformer.from_crs(source, target, always_xy=True)


@lru_cache(maxsize=64)
def _crs_from_epsg(code: int) -> CRS:
    return CRS.from_epsg(code)


def _reproject(geometry: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    """Apply a pyproj transformer to every coordinate of a shapely geometry."""

    def convert(coords):  # coords is an (N, 2) array of x, y
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack((x, y))

    return shapely.transform(geometry, convert)


def utm_epsg_for(lon: float, lat: float) -> int:
    """EPSG code of the UTM zone containing (lon, lat); UPS for the poles."""
    if lat >= 84:
        return 32661  # WGS84 / UPS North
    if lat < -80:
        return 32761  # WGS84 / UPS South
    zone = max(1, min(60, int(math.floor((lon + 180.0) / 6.0)) + 1))
    return (32600 if lat >= 0 else 32700) + zone  # 326xx = north, 327xx = south


def measure_feature(geometry: BaseGeometry, source_crs: CRS) -> dict[str, Any]:
    """Return a measurement dict. Never raises: problems are reported in the result."""
    geometry_type = geometry.geom_type

    if geometry_type in NO_MEASUREMENT_TYPES:
        return {"status": "NOT_APPLICABLE", "message": "No measurement is required for points."}
    if geometry_type not in AREA_TYPES | LENGTH_TYPES:
        return {
            "status": "UNSUPPORTED",
            "message": f"Measurement is not supported for geometry type '{geometry_type}'.",
        }

    try:
        flat = shapely.force_2d(geometry)  # ignore altitude / Z values

        # Step 1: centre of the feature in lon/lat
        to_lonlat = _transformer(source_crs, WGS84)
        centre = _reproject(flat, to_lonlat).centroid
        if not (math.isfinite(centre.x) and math.isfinite(centre.y)) or abs(centre.y) > 90:
            raise ValueError("coordinates are outside the valid range for the file's CRS")

        # Steps 2-3: choose a UTM zone and project
        epsg = utm_epsg_for(centre.x, centre.y)
        projected = _reproject(flat, _transformer(source_crs, _crs_from_epsg(epsg)))

        # Step 4: measure
        result: dict[str, Any] = {"status": "MEASURED", "measurement_crs": f"EPSG:{epsg}"}
        value = projected.area if geometry_type in AREA_TYPES else projected.length
        if not math.isfinite(value):
            raise ValueError("calculation produced a non-finite value")
        result["area_m2" if geometry_type in AREA_TYPES else "length_m"] = round(value, 3)

        if not geometry.is_valid:
            result["warnings"] = ["Geometry is invalid (e.g. self-intersecting); result may be unreliable."]
        return result
    except Exception as exc:  # report, don't crash
        return {"status": "ERROR", "message": f"Measurement failed: {exc}"}