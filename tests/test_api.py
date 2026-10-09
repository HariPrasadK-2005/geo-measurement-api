import io
import zipfile

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from pyproj import Geod
from shapely.geometry import LineString, Polygon, box

from app.main import app

client = TestClient(app)
GEOD = Geod(ellps="WGS84")

# ~1 km x 1.1 km square near Bengaluru (lon, lat)
SQUARE_COORDS = [(77.590, 12.970), (77.600, 12.970), (77.600, 12.980), (77.590, 12.980), (77.590, 12.970)]
LINE_COORDS = [(77.585, 12.965), (77.595, 12.975), (77.600, 12.985)]


# ------------------------------------------------------------------ helpers
def coords_to_kml(coords) -> str:
    return " ".join(f"{x},{y},0" for x, y in coords)


def make_kml(*placemarks: str) -> bytes:
    body = "".join(placemarks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>'
    ).encode()


def polygon_placemark(coords=SQUARE_COORDS, name="Plot") -> str:
    return (
        f"<Placemark><name>{name}</name><Polygon><outerBoundaryIs><LinearRing>"
        f"<coordinates>{coords_to_kml(coords)}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
    )


def line_placemark(coords=LINE_COORDS) -> str:
    return f"<Placemark><name>Road</name><LineString><coordinates>{coords_to_kml(coords)}</coordinates></LineString></Placemark>"


def point_placemark() -> str:
    return "<Placemark><name>Marker</name><Point><coordinates>77.595,12.975,0</coordinates></Point></Placemark>"


def upload(name: str, content: bytes):
    return client.post("/api/files/", files={"file": (name, content, "application/octet-stream")})


def shapefile_zip(tmp_path, geometries, crs) -> bytes:
    gdf = gpd.GeoDataFrame({"name": [f"f{i}" for i in range(len(geometries))]}, geometry=geometries, crs=crs)
    folder = tmp_path / "shp"
    folder.mkdir()
    gdf.to_file(folder / "test.shp")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for part in folder.iterdir():
            archive.write(part, arcname=part.name)
    return buffer.getvalue()


def measurements_of(file_id: str) -> dict:
    response = client.get(f"/api/files/{file_id}/measurements/")
    assert response.status_code == 200
    return response.json()


# -------------------------------------------------------------------- tests
def test_kml_polygon_and_info_endpoint():
    response = upload("survey.kml", make_kml(polygon_placemark()))
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["feature_count"] == 1
    assert body["crs"] == "EPSG:4326"
    assert body["filename"] == "survey.kml"

    info = client.get(f"/api/files/{body['id']}/")
    assert info.status_code == 200
    assert info.json()["id"] == body["id"]


def test_polygon_area_is_in_square_metres_not_degrees():
    file_id = upload("a.kml", make_kml(polygon_placemark())).json()["id"]
    feature = measurements_of(file_id)["features"][0]["measurement"]

    expected, _ = GEOD.geometry_area_perimeter(Polygon(SQUARE_COORDS))
    assert feature["status"] == "MEASURED"
    assert feature["measurement_crs"] == "EPSG:32643"  # UTM zone 43N covers Bengaluru
    assert feature["area_m2"] == pytest.approx(abs(expected), rel=0.005)  # within 0.5 %
    assert feature["area_m2"] > 1_000_000  # ~1.2 km2; a degree-based answer would be ~0.0001


def test_linestring_length_in_metres():
    file_id = upload("l.kml", make_kml(line_placemark())).json()["id"]
    feature = measurements_of(file_id)["features"][0]["measurement"]

    expected = GEOD.geometry_length(LineString(LINE_COORDS))
    assert feature["status"] == "MEASURED"
    assert feature["length_m"] == pytest.approx(expected, rel=0.005)


def test_point_has_no_measurement():
    file_id = upload("p.kml", make_kml(point_placemark())).json()["id"]
    feature = measurements_of(file_id)["features"][0]
    assert feature["geometry_type"] == "Point"
    assert feature["measurement"]["status"] == "NOT_APPLICABLE"
    assert "area_m2" not in feature["measurement"]


def test_unsupported_geometry_is_handled_gracefully():
    mixed = (
        "<Placemark><name>Mixed</name><MultiGeometry>"
        "<Point><coordinates>77.6,12.99,0</coordinates></Point>"
        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords_to_kml(SQUARE_COORDS)}</coordinates>"
        "</LinearRing></outerBoundaryIs></Polygon></MultiGeometry></Placemark>"
    )
    response = upload("mixed.kml", make_kml(mixed, polygon_placemark()))
    assert response.status_code == 201  # whole file still succeeds

    data = measurements_of(response.json()["id"])
    statuses = [f["measurement"]["status"] for f in data["features"]]
    assert statuses == ["UNSUPPORTED", "MEASURED"]
    assert data["summary"]["by_status"] == {"UNSUPPORTED": 1, "MEASURED": 1}


def test_features_endpoint_returns_geometry_properties_and_crs():
    file_id = upload("f.kml", make_kml(polygon_placemark(name="Plot A"))).json()["id"]
    body = client.get(f"/api/files/{file_id}/features/").json()
    assert body["total"] == 1
    feature = body["features"][0]
    assert feature["index"] == 0
    assert feature["geometry_type"] == "Polygon"
    assert feature["geometry"]["type"] == "Polygon"
    assert feature["crs"] == "EPSG:4326"
    assert feature["properties"]["name"] == "Plot A"


def test_shapefile_in_projected_crs(tmp_path):
    # 100 m x 100 m square already in UTM 43N -> area must be exactly 10,000 m2
    content = shapefile_zip(tmp_path, [box(500000, 1435000, 500100, 1435100)], "EPSG:32643")
    response = upload("plots.zip", content)
    assert response.status_code == 201
    assert "32643" in response.json()["crs"] or "UTM zone 43N" in response.json()["crs"]

    feature = measurements_of(response.json()["id"])["features"][0]["measurement"]
    assert feature["area_m2"] == pytest.approx(10_000, rel=1e-4)


def test_shapefile_in_geographic_crs(tmp_path):
    content = shapefile_zip(tmp_path, [Polygon(SQUARE_COORDS)], "EPSG:4326")
    response = upload("geo.zip", content)
    assert response.status_code == 201

    feature = measurements_of(response.json()["id"])["features"][0]["measurement"]
    expected, _ = GEOD.geometry_area_perimeter(Polygon(SQUARE_COORDS))
    assert feature["area_m2"] == pytest.approx(abs(expected), rel=0.005)


def test_rejects_unsupported_extension():
    response = upload("notes.txt", b"hello")
    assert response.status_code == 400


def test_rejects_empty_file():
    assert upload("empty.kml", b"").status_code == 400


def test_corrupt_zip_is_reported_as_failed():
    response = upload("broken.zip", b"this is not a zip file")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "FAILED"

    info = client.get(f"/api/files/{detail['id']}/").json()
    assert info["status"] == "FAILED"
    assert info["error"]


def test_invalid_kml_is_reported_as_failed():
    assert upload("bad.kml", b"<kml><oops>").status_code == 422


def test_unknown_file_id_returns_404():
    assert client.get("/api/files/does-not-exist/").status_code == 404
    assert client.get("/api/files/does-not-exist/measurements/").status_code == 404
