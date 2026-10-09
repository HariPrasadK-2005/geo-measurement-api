"""Point the app at a throw-away data folder BEFORE the app is imported."""
import os
import tempfile

os.environ["GEO_API_DATA_DIR"] = tempfile.mkdtemp(prefix="geo_api_tests_")
