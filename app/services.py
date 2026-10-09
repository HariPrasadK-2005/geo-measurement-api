"""Orchestrates: read file -> build features -> measure -> store."""
import logging
import tempfile
from pathlib import Path
from typing import Any

from pyproj import CRS
from shapely.geometry import mapping

from app import database as db
from app import readers
from app.measurements import measure_feature
from app.readers import FileReadError, ParsedFeature

logger = logging.getLogger(__name__)


def _read(stored_path: Path, extension: str) -> readers.ParsedFile:
    if extension == ".kml":
        return readers.read_kml(stored_path)
    with tempfile.TemporaryDirectory(prefix="shp_") as work_dir:
        return readers.read_shapefile_zip(stored_path, Path(work_dir))


def _build_feature(parsed: ParsedFeature, crs: CRS, crs_name: str) -> dict[str, Any]:
    geometry = parsed.geometry
    if geometry is None or geometry.is_empty:
        return {
            "index": parsed.index,
            "geometry_type": None,
            "geometry": None,
            "crs": crs_name,
            "properties": parsed.properties,
            "measurement": {
                "status": "ERROR" if parsed.error else "UNSUPPORTED",
                "message": parsed.error or "Feature has no geometry.",
            },
        }
    return {
        "index": parsed.index,
        "geometry_type": geometry.geom_type,
        "geometry": mapping(geometry),  # GeoJSON-style dict, in the file's own CRS
        "crs": crs_name,
        "properties": parsed.properties,
        "measurement": measure_feature(geometry, crs),
    }


def process_file(file_id: str, stored_path: Path, extension: str) -> dict[str, Any]:
    """Process an uploaded file and return its final database record."""
    try:
        parsed = _read(stored_path, extension)
        crs_name = readers.crs_label(parsed.crs)
        features = [_build_feature(f, parsed.crs, crs_name) for f in parsed.features]
        db.save_result(file_id, crs_name, features)
    except FileReadError as exc:
        db.mark_failed(file_id, str(exc))
    except Exception:  # unexpected: log details, show a generic message
        logger.exception("Unexpected error while processing file %s", file_id)
        db.mark_failed(file_id, "Unexpected error while processing the file.")
    return db.get_file(file_id)  # type: ignore[return-value]
