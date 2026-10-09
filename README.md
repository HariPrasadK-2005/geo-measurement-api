# Geospatial File Measurement API

A backend service (FastAPI) that accepts a **KML** file or a **zipped Shapefile**, extracts every
feature (ID, geometry type, geometry, CRS, properties) and calculates **area** for polygons and
**length** for lines. Measurements are always computed in a **projected, metre-based CRS** - never in
latitude/longitude degrees.

## Contents
- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Learnings](#learnings)
- [Future scope](#future-scope)

---

## Setup

Requires **Python 3.10+**.

```bash
# 1. (optional but recommended) create a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. install dependencies
pip install -r requirements.txt

# 3. run the server
uvicorn app.main:app --reload
```

The API is now at **http://localhost:8000** and interactive Swagger docs are at
**http://localhost:8000/docs** (you can upload files straight from that page).

Generate the sample Shapefile (the sample KML is already included):

```bash
python scripts/make_sample_shapefile.py   # creates sample_files/survey_shapefile.zip
```

Run with Docker instead:

```bash
docker build -t geo-api .
docker run -p 8000:8000 geo-api
```

Configuration (optional environment variables): `GEO_API_DATA_DIR` (where uploads and the SQLite
database are stored, default `./data`), `GEO_API_DB_PATH`, `GEO_API_MAX_UPLOAD_MB` (default 50).

---

## API

| Method | Endpoint                          | Purpose                                   |
|--------|-----------------------------------|-------------------------------------------|
| POST   | `/api/files/`                     | Upload and process a `.kml` or `.zip`     |
| GET    | `/api/files/{id}/`                | File information                          |
| GET    | `/api/files/{id}/measurements/`   | Measurements for every feature            |
| GET    | `/api/files/{id}/features/`       | Extracted geometry + properties (paginated) |
| GET    | `/health`                         | Health check                              |

### Upload - `POST /api/files/`

```bash
curl -F "file=@sample_files/survey.kml" http://localhost:8000/api/files/
```

`201 Created`
```json
{
  "id": "3f6c1b7e0c9a4d3f8e2b5a7d9c1e4f60",
  "filename": "survey.kml",
  "feature_count": 4,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null,
  "created_at": "2026-10-09T10:15:30+00:00"
}
```

Error responses:

| Code | When |
|------|------|
| 400  | Wrong file type (not `.kml` / `.zip`) or empty file |
| 413  | File larger than the size limit |
| 422  | File could not be read (corrupt zip, invalid KML, no `.shp`, missing `.prj`, ...). Body: `{"detail": {"id": "...", "status": "FAILED", "error": "..."}}` |

### File information - `GET /api/files/{id}/`

```bash
curl http://localhost:8000/api/files/3f6c1b7e0c9a4d3f8e2b5a7d9c1e4f60/
```
Returns the same object as the upload response. `404` if the id does not exist.

### Measurements - `GET /api/files/{id}/measurements/`

`200 OK`
```json
{
  "file_id": "3f6c1b7e0c9a4d3f8e2b5a7d9c1e4f60",
  "filename": "survey.kml",
  "crs": "EPSG:4326",
  "summary": {
    "feature_count": 4,
    "by_status": {"MEASURED": 2, "NOT_APPLICABLE": 1, "UNSUPPORTED": 1},
    "total_area_m2": 1197553.9,
    "total_length_m": 3053.1
  },
  "features": [
    {"index": 0, "geometry_type": "Polygon",
     "measurement": {"status": "MEASURED", "area_m2": 1197553.9, "measurement_crs": "EPSG:32643", "warnings": []}},
    {"index": 1, "geometry_type": "LineString",
     "measurement": {"status": "MEASURED", "length_m": 3053.1, "measurement_crs": "EPSG:32643", "warnings": []}},
    {"index": 2, "geometry_type": "Point",
     "measurement": {"status": "NOT_APPLICABLE", "message": "No measurement is required for points.", "warnings": []}},
    {"index": 3, "geometry_type": "GeometryCollection",
     "measurement": {"status": "UNSUPPORTED", "message": "Measurement is not supported for geometry type 'GeometryCollection'.", "warnings": []}}
  ]
}
```
*(Numbers above are illustrative; run the sample file to see exact values.)*

Measurement `status` values: `MEASURED`, `NOT_APPLICABLE` (points), `UNSUPPORTED` (e.g. mixed
geometry collections), `ERROR` (a single feature could not be parsed/measured).
A bad feature never fails the whole file.

### Features - `GET /api/files/{id}/features/?limit=100&offset=0`

Returns every extracted feature with its `index`, `geometry_type`, `geometry` (GeoJSON, in the file's own
CRS), `crs`, `properties` and `measurement`.

---

## Architecture

```
app/
├── main.py          FastAPI app creation
├── routes.py        HTTP endpoints (validation only, no business logic)
├── services.py      orchestration: read -> build features -> measure -> store
├── readers.py       KML parser + Shapefile (zip) reader  -> common ParsedFile structure
├── measurements.py  CRS selection + area/length calculation
├── database.py      SQLite persistence
├── schemas.py       Pydantic response models
└── config.py        settings / limits
```

**File-processing flow**
1. `POST /api/files/` validates the extension and streams the upload to disk in 1 MB chunks
   (enforcing the size limit).
2. A `files` row is created with status `PROCESSING`.
3. `services.process_file` picks a reader: KML parser, or safe zip extraction + GeoPandas for Shapefiles.
4. Each feature becomes a record: index, geometry type, GeoJSON geometry, CRS, properties.
5. Features are measured (below) and saved together with the `COMPLETED` status in **one database
   transaction**. Any read failure sets status `FAILED` with a readable error message.

**Measurement flow** (per feature, `measurements.py`)
1. Point / MultiPoint -> `NOT_APPLICABLE`. Other types that are not polygon or line -> `UNSUPPORTED`.
2. Convert the feature to WGS84 and take its centre.
3. Choose the UTM zone for that centre; re-project the feature into it.
4. Polygon / MultiPolygon -> `area_m2`; LineString / MultiLineString -> `length_m`.
5. Anything that goes wrong is caught and returned as `status: ERROR` for that feature.

**CRS handling**
- KML is always WGS84 (EPSG:4326) by specification.
- Shapefile CRS is read from the `.prj` file. If it is missing the upload is rejected with a clear
  message, because guessing a CRS would silently produce wrong numbers.
- Whatever the source CRS is (degrees *or* metres), each feature is re-projected to the **local UTM zone**
  (EPSG:326xx north / 327xx south, UPS at the poles) before measuring. Axis order is forced to
  `(x=lon, y=lat)` with `always_xy=True` to avoid the classic lat/lon swap bug.

---

## Design decisions

| Decision | Why | Alternatives considered |
|----------|-----|-------------------------|
| **FastAPI** | Automatic request validation and interactive `/docs`, little boilerplate | Django + DRF (heavier for a 3-endpoint service) |
| **Always re-project to local UTM** (even if the file is already projected) | A metre-based CRS is not necessarily area-accurate (e.g. Web Mercator, EPSG:3857, inflates areas); UTM keeps distortion small | Trust the file's own projected CRS; one UTM zone for the whole file (wrong for files spanning zones); a single equal-area CRS |
| **Pick the UTM zone per feature** | Accurate even when one file spans several zones | One zone per file (faster, less accurate) |
| **Own KML parser** (`defusedxml`) instead of a GDAL driver | KML driver availability varies between GDAL builds; a small parser is predictable, testable, and safe against XML attacks | GeoPandas/Fiona KML driver |
| **SQLite via stdlib `sqlite3`** | Zero setup, enough for this scope, single-file DB | PostgreSQL + PostGIS + SQLAlchemy (better for production scale) |
| **Process synchronously inside the upload request** | Simple; result is available immediately | Background worker (Celery/RQ) with polling - see future scope |
| **Per-feature error handling** | One broken placemark should not discard a 10,000-feature file | Fail the whole upload |
| **Safe zip extraction** | Rejects path traversal ("zip-slip") and zip bombs | Plain `extractall` |

**Known accuracy trade-off:** UTM is a map projection, so areas/lengths differ slightly from true
ellipsoidal values (typically well under 0.2 % inside a zone). Using `pyproj.Geod` (geodesic
calculation) would be the most exact alternative; the tests cross-check the results against `Geod`.

---

## Testing

```bash
pytest -v
```

Tests cover: KML and Shapefile upload, polygon area / line length (cross-checked against geodesic
calculations), points, unsupported geometry, projected vs geographic CRS, invalid / corrupt / empty
files, and 404 handling.

---

## Learnings

> *Replace this section with your own words before submitting.*

- How coordinate reference systems work, and why measuring in degrees gives meaningless results.
- Structuring a FastAPI project into routes, services and data layers.
- Handling untrusted file uploads safely (size limits, zip-slip, XML attacks).
- Designing APIs that degrade gracefully: one bad feature should not break a whole file.
- Writing tests that verify results against an independent calculation (geodesic area/length).

## Future scope

- Background processing (Celery / RQ) with progress polling for very large files.
- PostgreSQL + PostGIS, plus spatial queries (features within a bounding box).
- More formats: KMZ, GeoJSON, GeoPackage.
- Perimeter, centroid and bounding box per feature; unit options (hectares, acres, km).
- Geodesic measurement option (`pyproj.Geod`) for the highest accuracy.
- Authentication, rate limiting, and automatic clean-up of old uploads.
- CI pipeline (GitHub Actions) running tests and linting on every push.
