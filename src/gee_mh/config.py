"""Carga de config.yaml. Un solo punto de configuración para todo el pipeline."""

import os
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def init_earth_engine(project: str):
    """
    ee.Initialize con las credenciales de `earthengine authenticate` o, si
    GOOGLE_APPLICATION_CREDENTIALS está definida (GitHub Actions), con esas:
    ee.Initialize no las lee por su cuenta.
    """
    import ee

    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        import google.auth

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/earthengine"])
        ee.Initialize(credentials, project=project)
    else:
        ee.Initialize(project=project)


if __name__ == "__main__":
    cfg = load_config()
    assert cfg["ee_project"] == "gee-mh"
    assert cfg["aoi"]["path"]
    print("config OK:", cfg["ee_project"])
