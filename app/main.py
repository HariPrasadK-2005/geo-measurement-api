import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from app.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Geospatial File Measurement API",
    version="1.0.0",
    description=(
        "Upload a KML file or a zipped Shapefile, extract its features and get "
        "area (polygons) and length (lines) measurements calculated in a projected CRS."
    ),
)
app.include_router(router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
