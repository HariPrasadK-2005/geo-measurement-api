"""Central configuration. Every value can be overridden with an environment variable."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.getenv("GEO_API_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("GEO_API_DB_PATH", DATA_DIR / "geo_api.sqlite3"))

ALLOWED_EXTENSIONS = {".kml", ".zip"}

# Upload size limit (megabytes -> bytes)
MAX_UPLOAD_BYTES = int(os.getenv("GEO_API_MAX_UPLOAD_MB", "50")) * 1024 * 1024

# Protection against "zip bombs": limits on what a zip may expand to.
MAX_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
MAX_ZIP_MEMBERS = 1000
