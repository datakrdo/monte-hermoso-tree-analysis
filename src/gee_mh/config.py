"""Carga de config.yaml. Un solo punto de configuración para todo el pipeline."""

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    cfg = load_config()
    assert cfg["ee_project"] == "gee-mh"
    assert cfg["aoi"]["path"]
    print("config OK:", cfg["ee_project"])
