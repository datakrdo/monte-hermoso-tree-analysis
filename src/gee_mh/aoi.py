"""Descarga y guarda el límite del partido de Monte Hermoso (paso 2 del plan).

Fuente: datos.gob.ar / georef (IGN), capa nacional de departamentos/partidos
en WGS84: https://infra.datos.gob.ar/georef/departamentos.geojson
Partido id georef: 06553 (Monte Hermoso, Buenos Aires).
"""

import datetime
import json
import urllib.request

import geopandas as gpd

from gee_mh.config import PROJECT_ROOT

DEPARTAMENTOS_URL = "https://infra.datos.gob.ar/georef/departamentos.geojson"
MONTE_HERMOSO_ID = "06553"


def fetch_monte_hermoso_aoi(out_path=None) -> gpd.GeoDataFrame:
    out_path = out_path or PROJECT_ROOT / "data" / "aoi" / "monte_hermoso.geojson"

    # El servidor rechaza el User-Agent por defecto de urllib.
    req = urllib.request.Request(DEPARTAMENTOS_URL, headers={"User-Agent": "gee-mh/1.0"})
    with urllib.request.urlopen(req) as resp:
        departamentos = json.load(resp)

    gdf = gpd.GeoDataFrame.from_features(departamentos["features"], crs="EPSG:4326")
    aoi = gdf[gdf["id"] == MONTE_HERMOSO_ID].copy()
    if aoi.empty:
        raise ValueError(f"No se encontró el partido {MONTE_HERMOSO_ID} en {DEPARTAMENTOS_URL}")

    aoi["source"] = "datos.gob.ar/georef (IGN)"
    aoi["source_date"] = datetime.date.today().isoformat()
    # EPSG:6933 (World Cylindrical Equal Area) para un área confiable
    # independiente de la faja Gauss-Kruger local.
    aoi["area_km2"] = aoi.to_crs("EPSG:6933").geometry.area / 1e6

    out_path.parent.mkdir(parents=True, exist_ok=True)
    aoi.to_file(out_path, driver="GeoJSON")
    return aoi


if __name__ == "__main__":
    aoi = fetch_monte_hermoso_aoi()
    assert len(aoi) == 1
    assert aoi.iloc[0]["nombre"] == "Monte Hermoso"
    assert aoi.iloc[0]["area_km2"] > 0
    print(f"AOI OK: {aoi.iloc[0]['nombre']}, area={aoi.iloc[0]['area_km2']:.1f} km2")
