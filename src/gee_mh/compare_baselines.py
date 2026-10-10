"""Comparación de los baselines de detección: corre
ndvi_diff, dynamic_world, cva (pérdida leñosa) y veg_to_built (vegetación que
pasa a construido) sobre el AOI real, para el período en que existen todos
(Dynamic World arranca 2015-06-27), y arma una muestra estratificada para
revisión manual con imágenes de alta resolución.

Uso:
    PYTHONPATH=src python3 -m gee_mh.compare_baselines --start-year 2016 --end-year 2026 --sample-per-stratum 5
"""

import argparse
import datetime

import geopandas as gpd
import pandas

from gee_mh.attribution import attribute_events, images_after
from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.detection import (
    CVA_MIN_NDVI_DROP, detect_loss_cva, detect_loss_dynamic_world, detect_loss_ndvi_diff, detect_loss_veg_to_built, detect_pair,
    pair_regional_shift)
from gee_mh.export import mask_to_polygons
from gee_mh.pipeline import load_aoi_geometry, load_area, load_regional_shifts
from gee_mh.preprocessing import Preprocessor
from gee_mh.timeseries import build_series_with_context
from gee_mh.validation import size_class, stratified_sample

BASELINES = {
    "ndvi_diff": detect_loss_ndvi_diff,
    "dynamic_world": detect_loss_dynamic_world,
    "cva": detect_loss_cva,
    "veg_to_built": detect_loss_veg_to_built,
}
# NOTE: una versión anterior de cva (umbral buscado por calibración contra
# la transición de Dynamic World) no detectó ningún evento en el AOI completo
# 2016-2023, porque esa calibración solo funciona sobre una ventana chica
# alrededor de los píxeles de cambio. La versión actual usa un umbral
# estadístico simple (ver detection.detect_loss_cva) y sí aporta eventos, pero
# no está calibrada contra ninguna referencia: esperar su precisión recién con
# las etiquetas manuales.


def run(start_year: int, end_year: int, sample_per_stratum: int):
    cfg = load_config()
    area = load_area(small=False)
    aoi = load_aoi_geometry()
    min_area_m2 = cfg["detection"]["min_event_area_m2"]
    min_persistence = cfg["detection"]["min_persistence_years"]

    processor = Preprocessor(ee_project=cfg["ee_project"])
    series = build_series_with_context(processor, area, start_year, end_year)
    years = sorted(series)
    shifts = load_regional_shifts(processor, start_year, end_year)

    out_path = PROJECT_ROOT / "data" / "exports" / "events_by_baseline.geojson"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    events = []
    for year_from, year_to in zip(years, years[1:]):
        if year_from < start_year or year_to > end_year:
            continue  # años de contexto (previo/siguiente), no son pares a analizar
        image_t1, image_t2 = series[year_from], series[year_to]
        sensor_change = image_t1.source != image_t2.source  # ver nota en pipeline.py
        if image_t1.resolution != image_t2.resolution:
            print(f"{year_from}->{year_to}: se omite (cambio de resolución {image_t1.resolution}m -> {image_t2.resolution}m, NDVI no comparable)")
            continue
        # caída regional de NDVI del par (0 si no hubo), medida en todo el partido: ya descontada por detect_pair
        ndvi_shift = (pair_regional_shift(shifts, series, year_from, year_to, area, aoi) or {}).get("ndvi", 0.0)
        for baseline_name, detect_fn in BASELINES.items():
            mask, confirmed = detect_pair(detect_fn, series, year_from, year_to, area, aoi, min_area_m2, min_persistence, shifts=shifts)
            if mask is None or not mask.any():
                continue
            polygons = mask_to_polygons(area, mask)
            attributions = attribute_events(polygons, area, mask.shape, image_t1, images_after(series, year_to))
            for polygon, attribution in zip(polygons, attributions):
                if baseline_name == "cva" and attribution.evidence["ndvi_drop"] < CVA_MIN_NDVI_DROP:
                    continue
                events.append(
                    {
                        "geometry": polygon,
                        "baseline": baseline_name,
                        "year_from": year_from,
                        "year_to": year_to,
                        "cause": attribution.cause,
                        "confidence": attribution.confidence,
                        "built_fraction": attribution.evidence["built_fraction"],
                        "ndbi_rise": attribution.evidence["ndbi_rise"],
                        "ndvi_shift": ndvi_shift,
                        "sensor_change": sensor_change,
                        "confirmed": confirmed,
                    }
                )
        print(f"{year_from}->{year_to}: listo")

        # NOTE: checkpoint por año procesado - si el proceso crashea más
        # adelante, no se pierde la descarga/detección ya hecha.
        if events:
            checkpoint = gpd.GeoDataFrame(events, crs="EPSG:4326")
            checkpoint["area_m2"] = checkpoint.to_crs("EPSG:6933").geometry.area
            checkpoint["size_class"] = checkpoint["area_m2"].apply(size_class)
            centroids = checkpoint.geometry.centroid
            checkpoint["centroid_lat"] = centroids.y
            checkpoint["centroid_lon"] = centroids.x
            checkpoint.to_file(out_path, driver="GeoJSON")

    if not events:
        print("Sin eventos detectados en el rango/área pedidos.")
        return

    gdf = checkpoint

    # La muestra sale solo de eventos confirmados (el último par no tiene año siguiente).
    confirmed_events = gdf[gdf["confirmed"]].drop(columns="geometry")
    if confirmed_events.empty:
        print("Ningún evento confirmado: sin muestra para revisión manual.")
        return
    sample = stratified_sample(confirmed_events, ["baseline", "size_class"], sample_per_stratum)
    sample["review_url"] = [
        f"https://earth.google.com/web/@{lat},{lon},0a,300d,35y,0h,0t,0r"
        for lat, lon in zip(sample["centroid_lat"], sample["centroid_lon"])
    ]
    # A completar a mano viendo la imagen de alta resolución en centroid_lat/
    # centroid_lon (o review_url): 1 si hubo pérdida real de copa arbórea, 0
    # si es falso positivo (sombra, nube residual, cambio estacional, etc).
    sample["label_true_positive"] = ""
    sample_path = PROJECT_ROOT / "data" / "exports" / "manual_review_sample.csv"
    if sample_path.exists() and pandas.read_csv(sample_path)["label_true_positive"].notna().any():
        # no pisar etiquetas hechas a mano
        sample_path = sample_path.with_name("manual_review_sample_new.csv")
        print(f"manual_review_sample.csv ya tiene etiquetas: la muestra nueva va a {sample_path.name}")
    sample.to_csv(sample_path, index=False)

    print(f"Exportado: {out_path}")
    print(f"Muestra para revisión manual ({len(sample)} eventos): {sample_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year", type=int, default=datetime.date.today().year)
    parser.add_argument("--sample-per-stratum", type=int, default=5)
    args = parser.parse_args()
    run(args.start_year, args.end_year, args.sample_per_stratum)
