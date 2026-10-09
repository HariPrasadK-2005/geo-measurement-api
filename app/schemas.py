"""Pydantic models: the shape of every API response (also drives the /docs page)."""
from typing import Any

from pydantic import BaseModel, Field


class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None = None
    status: str  # PROCESSING | COMPLETED | FAILED
    error: str | None = None
    created_at: str


class Measurement(BaseModel):
    status: str  # MEASURED | NOT_APPLICABLE | UNSUPPORTED | ERROR
    area_m2: float | None = None
    length_m: float | None = None
    measurement_crs: str | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FeatureMeasurement(BaseModel):
    index: int
    geometry_type: str | None = None
    measurement: Measurement


class MeasurementSummary(BaseModel):
    feature_count: int
    by_status: dict[str, int]
    total_area_m2: float
    total_length_m: float


class MeasurementsResponse(BaseModel):
    file_id: str
    filename: str
    crs: str | None = None
    summary: MeasurementSummary
    features: list[FeatureMeasurement]


class FeatureOut(BaseModel):
    index: int
    geometry_type: str | None = None
    geometry: dict[str, Any] | None = None
    crs: str | None = None
    properties: dict[str, Any]
    measurement: Measurement


class FeaturesResponse(BaseModel):
    file_id: str
    total: int
    limit: int
    offset: int
    features: list[FeatureOut]
