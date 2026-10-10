"""Corrida del pipeline completo año a año sobre el AOI (o un recorte, para
pruebas). Ata los pasos: serie temporal -> detección (3
baselines) -> filtrado -> atribución -> exportes.

Uso:
    PYTHONPATH=src python3 -m gee_mh.pipeline --start-year 2020 --end-year 2021 --small
"""

import argparse
import datetime

import geopandas as gpd

from gee_mh.attribution import attribute_events, images_after
from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.detection import detect_loss_ndvi_diff, detect_pair, pair_regional_shift, regional_shifts
from gee_mh.export import export_annual_summary_csv, export_events_geojson, mask_to_polygons
from gee_mh.preprocessing import Preprocessor, LongLatBBox
from gee_mh.timeseries import build_series_with_context
from gee_mh.trees import estimate_trees


def load_area(small: bool) -> LongLatBBox:
    cfg = load_config()
    aoi = gpd.read_file(PROJECT_ROOT / cfg["aoi"]["path"])
    minx, miny, maxx, maxy = aoi.total_bounds
    if not small:
        return LongLatBBox(minx, maxx, miny, maxy)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    return LongLatBBox(cx - 0.01, cx + 0.01, cy - 0.01, cy + 0.01)


def load_aoi_geometry():
    cfg = load_config()
    return gpd.read_file(PROJECT_ROOT / cfg["aoi"]["path"]).geometry.iloc[0]


def load_partido_geometry():
    """Polígono del partido completo (aoi.partido_path): referencia regional y contexto de la zona de estudio."""
    return gpd.read_file(PROJECT_ROOT / load_config()["aoi"]["partido_path"]).geometry.iloc[0]


def load_partido_area() -> LongLatBBox:
    minx, miny, maxx, maxy = load_partido_geometry().bounds
    return LongLatBBox(minx, maxx, miny, maxy)


def load_regional_shifts(processor, start_year: int, end_year: int):
    """
    Corrimientos regionales de NDVI de cada par de años (ver detection.regional_shifts) medidos en todo el
    partido. Con la zona de estudio chica, medirlos dentro de ella restaría un cambio real de la zona como
    si fuera clima. None si la zona de estudio es el partido mismo (se miden adentro).
    """
    cfg = load_config()
    if cfg["aoi"]["path"] == cfg["aoi"]["partido_path"]:
        return None
    area, aoi = load_partido_area(), load_partido_geometry()
    series = build_series_with_context(processor, area, start_year, end_year)
    years = sorted(series)
    return regional_shifts(series, area, aoi, [(y, y + k) for y in years for k in (1, 2)])


def run(start_year: int, end_year: int, small: bool):
    cfg = load_config()
    area = load_area(small)
    aoi = load_aoi_geometry()
    min_area_m2 = cfg["detection"]["min_event_area_m2"]
    min_persistence = cfg["detection"]["min_persistence_years"]

    processor = Preprocessor(ee_project=cfg["ee_project"])
    series = build_series_with_context(processor, area, start_year, end_year)
    years = sorted(series)
    shifts = load_regional_shifts(processor, start_year, end_year)

    events = []
    for year_from, year_to in zip(years, years[1:]):
        if year_from < start_year or year_to > end_year:
            continue  # años de contexto (previo/siguiente), no son pares a analizar
        image_t1, image_t2 = series[year_from], series[year_to]
        # NDVI no es directamente comparable entre sensores (Landsat vs
        # Sentinel-2 miden reflectancia distinto): un cambio de sensor entre
        # t1 y t2 puede generar una caída de NDVI artificial en áreas enormes
        # que no perdieron vegetación real. No se corrige (upgrade: normalizar
        # radiometría entre sensores), se marca para no mezclarlo sin avisar
        # con pares del mismo sensor.
        sensor_change = image_t1.source != image_t2.source

        if image_t1.resolution != image_t2.resolution:
            print(f"{year_from}->{year_to}: se omite (cambio de resolución {image_t1.resolution}m -> {image_t2.resolution}m, NDVI no comparable)")
            continue

        # Baseline por defecto: NDVI-diff, el único disponible en todo el
        # rango 2000-presente. dynamic_world y cva se comparan en
        # compare_baselines.py.
        loss_mask, confirmed = detect_pair(
            detect_loss_ndvi_diff, series, year_from, year_to, area, aoi, min_area_m2, min_persistence, shifts=shifts)

        print(
            f"{year_from}->{year_to}: ndvi_diff filtrado={loss_mask.sum()}px confirmado={confirmed}"
            f"{' [CAMBIO DE SENSOR: ' + str(image_t1.source) + ' -> ' + str(image_t2.source) + ']' if sensor_change else ''}"
        )

        if not loss_mask.any():
            continue

        polygons = mask_to_polygons(area, loss_mask)
        ndvi_shift = (pair_regional_shift(shifts, series, year_from, year_to, area, aoi) or {}).get("ndvi", 0.0)
        attributions = attribute_events(polygons, area, loss_mask.shape, image_t1, images_after(series, year_to))
        for polygon, attribution in zip(polygons, attributions):
            events.append(
                {
                    "geometry": polygon,
                    "year_from": year_from,
                    "year_to": year_to,
                    "cause": attribution.cause,
                    "confidence": attribution.confidence,
                    "built_fraction": attribution.evidence["built_fraction"],
                    "ndvi_shift": ndvi_shift,
                    "sensor_change": sensor_change,
                    "confirmed": confirmed,
                }
            )

    if not events:
        print("Sin eventos detectados en el rango/área pedidos.")
        return

    events_path = PROJECT_ROOT / "data" / "exports" / "events.geojson"
    csv_path = PROJECT_ROOT / "data" / "exports" / "annual_summary.csv"
    gdf = export_events_geojson(events, events_path)
    gdf["area_m2"] = gdf.to_crs("EPSG:6933").geometry.area
    # Árboles estimados como escenario (ver trees.py) - se asume vegetación
    # urbana/costera dispersa por defecto; sin calibración local todavía.
    trees = gdf["area_m2"].apply(lambda a: estimate_trees(a, is_urban=True))
    gdf["trees_low"] = [t.trees_low for t in trees]
    gdf["trees_high"] = [t.trees_high for t in trees]
    gdf.to_file(events_path, driver="GeoJSON")
    export_annual_summary_csv(gdf, csv_path)
    print(f"Exportado: {events_path}, {csv_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=datetime.date.today().year)
    parser.add_argument("--small", action="store_true", help="usar un recorte chico del AOI para pruebas")
    args = parser.parse_args()
    run(args.start_year, args.end_year, args.small)
