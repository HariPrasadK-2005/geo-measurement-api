"""Generate sample_files/survey_shapefile.zip (a polygon shapefile in EPSG:4326).

Run from the project root:   python scripts/make_sample_shapefile.py
"""
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import box

OUTPUT = Path(__file__).resolve().parent.parent / "sample_files" / "survey_shapefile.zip"

gdf = gpd.GeoDataFrame(
    {"name": ["Plot 1", "Plot 2"], "owner": ["Alice", "Bob"]},
    geometry=[box(77.59, 12.97, 77.60, 12.98), box(77.61, 12.97, 77.62, 12.99)],
    crs="EPSG:4326",
)

with tempfile.TemporaryDirectory() as tmp:
    gdf.to_file(Path(tmp) / "plots.shp")
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in Path(tmp).iterdir():
            archive.write(part, arcname=part.name)

print(f"Created {OUTPUT}")
