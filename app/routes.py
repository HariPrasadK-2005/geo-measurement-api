"""HTTP endpoints. Kept thin: validation + calling services/database."""
import uuid
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from app import config, services
from app import database as db
from app.schemas import (
    FeaturesResponse,
    FileInfo,
    MeasurementsResponse,
    MeasurementSummary,
)

router = APIRouter(prefix="/api/files", tags=["files"])

_CHUNK_SIZE = 1024 * 1024  # read uploads 1 MB at a time


def _get_file_or_404(file_id: str) -> dict:
    record = db.get_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found.")
    return record


def _require_completed(record: dict) -> None:
    if record["status"] != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail={
                "id": record["id"],
                "status": record["status"],
                "error": record["error"] or "File has not finished processing.",
            },
        )


@router.post(
    "/",
    response_model=FileInfo,
    status_code=201,
    summary="Upload a .kml file or a .zip containing a Shapefile",
)
def upload_file(file: UploadFile = File(...)):
    filename = Path(file.filename or "").name
    extension = Path(filename).suffix.lower()
    if extension not in config.ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a .kml file or a .zip containing a Shapefile.",
        )

    file_id = uuid.uuid4().hex
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = config.UPLOAD_DIR / f"{file_id}{extension}"

    size, too_large = 0, False
    with stored_path.open("wb") as out:
        while chunk := file.file.read(_CHUNK_SIZE):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        stored_path.unlink(missing_ok=True)
        limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File is larger than the {limit_mb} MB limit.")
    if size == 0:
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    db.create_file(file_id, filename)
    record = services.process_file(file_id, stored_path, extension)

    if record["status"] != "COMPLETED":
        raise HTTPException(
            status_code=422,
            detail={"id": file_id, "status": record["status"], "error": record["error"]},
        )
    return record


@router.get("/{file_id}/", response_model=FileInfo, summary="Information about an uploaded file")
def get_file_info(file_id: str):
    return _get_file_or_404(file_id)


@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    response_model_exclude_none=True,
    summary="Area / length measurements for every feature",
)
def get_measurements(file_id: str):
    record = _get_file_or_404(file_id)
    _require_completed(record)
    rows = db.get_measurements(file_id)

    total_area = sum(r["measurement"].get("area_m2") or 0 for r in rows)
    total_length = sum(r["measurement"].get("length_m") or 0 for r in rows)
    summary = MeasurementSummary(
        feature_count=len(rows),
        by_status=dict(Counter(r["measurement"]["status"] for r in rows)),
        total_area_m2=round(total_area, 3),
        total_length_m=round(total_length, 3),
    )
    return {
        "file_id": file_id,
        "filename": record["filename"],
        "crs": record["crs"],
        "summary": summary,
        "features": rows,
    }


@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    summary="Extracted features: geometry, CRS, properties (paginated)",
)
def get_features(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    record = _get_file_or_404(file_id)
    _require_completed(record)
    return {
        "file_id": file_id,
        "total": record["feature_count"],
        "limit": limit,
        "offset": offset,
        "features": db.get_features(file_id, limit, offset),
    }
