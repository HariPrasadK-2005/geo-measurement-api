"""Readers: turn an uploaded file into shapely geometries + attributes.

* KML        -> parsed with a small, safe XML parser (KML is always WGS84 / EPSG:4326).
* Shapefile  -> a .zip is extracted safely and read with GeoPandas (CRS comes from the .prj file).

Both readers return the same `ParsedFile` structure so the rest of the app does not care
which format was uploaded.
"""
from __future__ import annotations

import math
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
from defusedxml import ElementTree as ET  # hardened against XML bombs / entity attacks
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
)
from shapely.geometry.base import BaseGeometry

from app import config


class FileReadError(Exception):
    """The uploaded file is unreadable or invalid. The message is safe to show to the user."""


@dataclass
class ParsedFeature:
    index: int
    geometry: BaseGeometry | None
    properties: dict[str, Any]
    error: str | None = None  # set when this one feature could not be parsed


@dataclass
class ParsedFile:
    crs: CRS
    features: list[ParsedFeature]


def crs_label(crs: CRS) -> str:
    """Human-friendly CRS name, e.g. 'EPSG:4326'."""
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


# --------------------------------------------------------------------------- KML

_GEOMETRY_TAGS = {"Point", "LineString", "LinearRing", "Polygon", "MultiGeometry"}


def _local(tag: Any) -> str:
    """'{namespace}Placemark' -> 'Placemark'."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _find_text(element, path: str) -> str | None:
    node = element.find(path)
    return node.text if node is not None else None


def _parse_coordinates(text: str | None) -> list[tuple[float, float]]:
    """KML coordinates look like 'lon,lat,alt lon,lat,alt ...'. Altitude is ignored."""
    cleaned = re.sub(r"\s*,\s*", ",", (text or "").strip())
    coords: list[tuple[float, float]] = []
    for token in cleaned.split():
        parts = token.split(",")
        if len(parts) < 2:
            raise ValueError(f"bad coordinate '{token}'")
        lon, lat = float(parts[0]), float(parts[1])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            raise ValueError(f"non-finite coordinate '{token}'")
        coords.append((lon, lat))
    return coords


def _combine(parts: list[BaseGeometry]) -> BaseGeometry | None:
    """Merge the members of a <MultiGeometry> into the most specific shapely type."""
    flat: list[BaseGeometry] = []
    for part in parts:
        if hasattr(part, "geoms"):
            flat.extend(part.geoms)
        else:
            flat.append(part)
    if not flat:
        return None
    if len(flat) == 1:
        return flat[0]
    kinds = {g.geom_type for g in flat}
    if kinds == {"Point"}:
        return MultiPoint(flat)
    if kinds == {"LineString"}:
        return MultiLineString(flat)
    if kinds == {"Polygon"}:
        return MultiPolygon(flat)
    return GeometryCollection(flat)  # mixed types -> reported as unsupported later


def _parse_geometry(element) -> BaseGeometry | None:
    tag = _local(element.tag)
    if tag == "Point":
        coords = _parse_coordinates(_find_text(element, "./{*}coordinates"))
        if len(coords) != 1:
            raise ValueError("a Point needs exactly one coordinate")
        return Point(coords[0])
    if tag in ("LineString", "LinearRing"):
        coords = _parse_coordinates(_find_text(element, "./{*}coordinates"))
        if len(coords) < 2:
            raise ValueError("a LineString needs at least two coordinates")
        return LineString(coords)
    if tag == "Polygon":
        outer = _parse_coordinates(
            _find_text(element, "./{*}outerBoundaryIs/{*}LinearRing/{*}coordinates")
        )
        if len(outer) < 3:
            raise ValueError("a Polygon outer ring needs at least three coordinates")
        holes = [
            _parse_coordinates(node.text)
            for node in element.findall("./{*}innerBoundaryIs/{*}LinearRing/{*}coordinates")
        ]
        return Polygon(outer, [h for h in holes if len(h) >= 3])
    if tag == "MultiGeometry":
        parts = []
        for child in element:
            if _local(child.tag) in _GEOMETRY_TAGS:
                geometry = _parse_geometry(child)
                if geometry is not None:
                    parts.append(geometry)
        return _combine(parts)
    return None


def _geometry_from_placemark(placemark) -> BaseGeometry | None:
    parts = []
    for child in placemark:
        if _local(child.tag) in _GEOMETRY_TAGS:
            geometry = _parse_geometry(child)
            if geometry is not None:
                parts.append(geometry)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else GeometryCollection(parts)


def _extract_kml_properties(placemark) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for child in placemark:
        name = _local(child.tag)
        if name in ("name", "description"):
            props[name] = (child.text or "").strip() or None
    extended = placemark.find("./{*}ExtendedData")
    if extended is not None:
        for node in extended.iter():
            kind, key = _local(node.tag), node.get("name")
            if not key:
                continue
            if kind == "Data":
                value = node.find("./{*}value")
                props[key] = (value.text or "").strip() or None if value is not None else None
            elif kind == "SimpleData":
                props[key] = (node.text or "").strip() or None
    return props


def read_kml(path: Path) -> ParsedFile:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, ValueError) as exc:
        raise FileReadError(f"File is not valid KML/XML: {exc}") from exc

    placemarks = [el for el in root.iter() if _local(el.tag) == "Placemark"]
    if not placemarks:
        raise FileReadError("No Placemark features were found in the KML file.")

    features: list[ParsedFeature] = []
    for index, placemark in enumerate(placemarks):
        geometry, error = None, None
        try:
            geometry = _geometry_from_placemark(placemark)
            if geometry is None:
                error = "Placemark has no supported geometry."
        except Exception as exc:  # one bad feature must not break the whole file
            error = f"Invalid geometry: {exc}"
        features.append(
            ParsedFeature(
                index=index,
                geometry=geometry,
                properties=_extract_kml_properties(placemark),
                error=error,
            )
        )
    return ParsedFile(crs=CRS.from_epsg(4326), features=features)


# --------------------------------------------------------------------- Shapefile


def _json_safe(value: Any) -> Any:
    """Convert numpy / pandas / datetime values into plain JSON-friendly Python values."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):  # numpy scalar
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    return str(value)


def _safe_extract(zip_path: Path, target_dir: Path) -> None:
    """Extract a zip while defending against zip-slip and zip-bomb attacks."""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > config.MAX_ZIP_MEMBERS:
                raise FileReadError("The zip archive contains too many files.")
            if sum(m.file_size for m in members) > config.MAX_ZIP_UNCOMPRESSED_BYTES:
                raise FileReadError("The zip archive is too large when extracted.")
            root = target_dir.resolve()
            for member in members:
                destination = (root / member.filename).resolve()
                if destination != root and root not in destination.parents:
                    raise FileReadError("The zip archive contains an unsafe file path.")
            archive.extractall(root)
    except zipfile.BadZipFile as exc:
        raise FileReadError("The uploaded file is not a valid zip archive.") from exc


def read_shapefile_zip(zip_path: Path, work_dir: Path) -> ParsedFile:
    _safe_extract(zip_path, work_dir)

    shapefiles = sorted(
        p
        for p in work_dir.rglob("*.shp")
        if not p.name.startswith("._") and "__MACOSX" not in p.parts
    )
    if not shapefiles:
        raise FileReadError("No .shp file was found inside the zip archive.")

    try:
        gdf = gpd.read_file(shapefiles[0])
    except Exception as exc:
        raise FileReadError(
            f"Could not read the shapefile (are the .shp, .shx and .dbf files all included?): {exc}"
        ) from exc

    if gdf.crs is None:
        raise FileReadError(
            "The shapefile has no coordinate reference system (.prj file is missing), "
            "so measurements cannot be calculated reliably."
        )
    if len(gdf) == 0:
        raise FileReadError("The shapefile contains no features.")

    geometry_column = gdf.geometry.name
    attributes = gdf.drop(columns=geometry_column).to_dict("records")
    features = [
        ParsedFeature(
            index=index,
            geometry=geometry,
            properties={str(k): _json_safe(v) for k, v in props.items()},
            error=None if geometry is not None else "Feature has no geometry.",
        )
        for index, (geometry, props) in enumerate(zip(gdf.geometry, attributes))
    ]
    return ParsedFile(crs=CRS.from_user_input(gdf.crs), features=features)
