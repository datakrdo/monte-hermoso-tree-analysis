"""Self-check: EE conectado, AOI cargado, una composición trae las bandas
esperadas a la escala correcta. Sin frameworks.

Uso: PYTHONPATH=src python3 tests/test_smoke.py
"""

import geopandas as gpd

from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.preprocessing import Preprocessor, DataSource, LongLatBBox


def main():
    cfg = load_config()

    aoi_path = PROJECT_ROOT / cfg["aoi"]["path"]
    aoi = gpd.read_file(aoi_path)
    assert len(aoi) == 1, "el AOI debe tener exactamente un polígono"
    minx, miny, maxx, maxy = aoi.total_bounds
    print(f"AOI: {aoi.iloc[0]['nombre']} bounds={aoi.total_bounds}")

    # Recorte chico dentro del AOI para no bajar la imagen entera.
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    small_area = LongLatBBox(cx - 0.01, cx + 0.01, cy - 0.01, cy + 0.01)

    processor = Preprocessor(ee_project=cfg["ee_project"])
    processor.data_source = DataSource.SENTINEL2

    images = processor.get_satellite_data(["2023-02-01"], small_area)
    assert len(images) == 1, "se esperaba una imagen para la fecha pedida"
    image = images[0]

    expected_bands = {"red", "nir", "swir", "ndvi", "ndbi", "ndmi"}
    assert expected_bands.issubset(image.bands.keys()), f"faltan bandas: {expected_bands - image.bands.keys()}"
    assert image.resolution == 10.0, f"Sentinel-2 debería usar 10m, no {image.resolution}"
    assert image.width > 0 and image.height > 0, "la imagen no debería estar vacía"

    print(f"OK: {image}, mean_ndvi={image.mean_ndvi:.3f}")


if __name__ == "__main__":
    main()
