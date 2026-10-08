"""Validación cruzada externa (ver plan `twinkling-mapping-quokka`): compara
nuestras detecciones contra MapBiomas Pampa (referencia independiente,
misma región/período) y, como chequeo secundario de bajo peso, contra Hansen
Global Forest Change. No modifica preprocessing.py ni el pipeline propio -
sólo lee sus salidas y consulta ee.Image adicionales sobre el mismo AOI.

Uso:
    # rápido: enriquece una muestra ya exportada (ej. manual_review_sample.csv)
    # con la clase MapBiomas/Hansen en el centroide de cada evento.
    PYTHONPATH=src python3 -m gee_mh.external_validation --enrich data/exports/manual_review_sample.csv

    # más caro: corre los 3 baselines sobre un rango de años y calcula
    # precision/recall/F1/IoU contra la máscara de pérdida leñosa de MapBiomas.
    PYTHONPATH=src python3 -m gee_mh.external_validation --full --start-year 2016 --end-year 2020
"""

import argparse
import dataclasses

import ee
import numpy
import pandas

from gee_mh.compare_baselines import BASELINES
from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.detection import detect_pair, resample_mask
from gee_mh.masks import aoi_mask
from gee_mh.pipeline import load_aoi_geometry, load_area
from gee_mh.preprocessing import Preprocessor
from gee_mh.timeseries import build_series_with_context
from gee_mh.validation import compute_metrics

MAPBIOMAS_ASSET = "projects/mapbiomas-public/assets/pampa/collection4/mapbiomas_pampa_collection4_integration_v1"
MAPBIOMAS_LAST_YEAR = 2023  # último año de la colección 4; años posteriores se capan a este.
# Clases "leñosas" según la leyenda oficial de la Colección 4 (pampa.mapbiomas.org/en/legend-codes,
# PDF MBPampa_Col4_LegendCode): 3 = Bosque y arbustal cerrados, 4 = Bosque abierto,
# 9 = Silvicultura (forestación) - se cuenta como leñosa porque es cobertura arbórea real,
# aunque la leyenda la ubique bajo "Agropecuaria" en vez de "Vegetación natural leñosa".
WOODY_CODES = {3, 4, 9}
MAPBIOMAS_RESOLUTION_M = 30.0
HANSEN_ASSET = "UMD/hansen/global_forest_change_2025_v1_13"


def mapbiomas_years(year_from: int, year_to: int) -> tuple:
    """MapBiomas Pampa collection4 llega hasta MAPBIOMAS_LAST_YEAR; capar sin
    romper si el par pedido es más reciente (ej. 2023->2024)."""
    return min(year_from, MAPBIOMAS_LAST_YEAR), min(year_to, MAPBIOMAS_LAST_YEAR)


def enrich_events_with_external_reference(events: pandas.DataFrame) -> pandas.DataFrame:
    """
    events: filas con al menos year_from, year_to, centroid_lon, centroid_lat
    (salida de compare_baselines.run()/pipeline.run(), ver manual_review_sample.csv).
    Agrega, por evento, la clase MapBiomas en el centroide para year_from/
    year_to, si esa transición corresponde a pérdida de vegetación leñosa
    según MapBiomas, y el treecover2000/lossyear de Hansen en el mismo punto.
    """
    mapbiomas = ee.Image(MAPBIOMAS_ASSET)
    hansen = ee.Image(HANSEN_ASSET)

    classes_from, classes_to, woody_loss, treecover, lossyear = [], [], [], [], []
    for _, row in events.iterrows():
        mb_year_from, mb_year_to = mapbiomas_years(int(row["year_from"]), int(row["year_to"]))
        point = ee.Geometry.Point([row["centroid_lon"], row["centroid_lat"]])

        # años capados iguales (ej. 2024->2025 -> 2023, 2023): select no admite bandas repetidas
        mb_bands = list(dict.fromkeys([f"classification_{mb_year_from}", f"classification_{mb_year_to}"]))
        mb_values = mapbiomas.select(mb_bands).reduceRegion(ee.Reducer.first(), point, scale=30).getInfo()
        cls_from = mb_values.get(f"classification_{mb_year_from}")
        cls_to = mb_values.get(f"classification_{mb_year_to}")
        classes_from.append(cls_from)
        classes_to.append(cls_to)
        woody_loss.append(
            None if cls_from is None or cls_to is None
            else cls_from in WOODY_CODES and cls_to not in WOODY_CODES
        )

        hansen_values = hansen.select(["treecover2000", "lossyear"]) \
            .reduceRegion(ee.Reducer.first(), point, scale=30).getInfo()
        treecover.append(hansen_values.get("treecover2000"))
        lossyear.append(hansen_values.get("lossyear"))

    out = events.copy()
    out["mapbiomas_class_from"] = classes_from
    out["mapbiomas_class_to"] = classes_to
    out["mapbiomas_woody_loss"] = woody_loss
    out["hansen_treecover2000"] = treecover
    out["hansen_lossyear"] = lossyear
    return out


def validate_year_pair(processor: Preprocessor, area, aoi, min_area_m2: float, min_persistence: int, series: dict, year_from: int, year_to: int) -> dict:
    """
    Corre los tres baselines sobre (year_from, year_to) y los compara,
    píxel a píxel, contra la máscara de pérdida de vegetación leñosa
    derivada de MapBiomas para el mismo par (años capados a
    MAPBIOMAS_LAST_YEAR). series: salida de build_series_with_context que cubre
    ambos años. Return {baseline_name: (DetectionMetrics, confirmed)}.
    """
    if year_from not in series or year_to not in series:
        return {}
    image_t1, image_t2 = series[year_from], series[year_to]
    if image_t1.resolution != image_t2.resolution:
        print(f"{year_from}->{year_to}: se omite (cambio de resolución, NDVI no comparable)")
        return {}

    mb_year_from, mb_year_to = mapbiomas_years(year_from, year_to)
    mapbiomas = ee.Image(MAPBIOMAS_ASSET)
    # años capados iguales (ej. 2024->2025 -> 2023, 2023): no repetir la banda
    bands = list(dict.fromkeys([f"classification_{mb_year_from}", f"classification_{mb_year_to}"]))
    # MapBiomas es un producto de 30 m: bajarlo a 10 m solo repite píxeles.
    class_bands = processor.cached(
        ("mapbiomas", MAPBIOMAS_ASSET, *bands, area),
        lambda: processor.download_numpy_data(mapbiomas, area, bands, MAPBIOMAS_RESOLUTION_M))
    class_t1 = class_bands[f"classification_{mb_year_from}"]
    class_t2 = class_bands[f"classification_{mb_year_to}"]
    ground_truth = woody_loss_mask(class_t1, class_t2)

    results = {}
    for baseline_name, detect_fn in BASELINES.items():
        mask, confirmed = detect_pair(detect_fn, series, year_from, year_to, area, aoi, min_area_m2, min_persistence)
        if mask is None:
            continue
        gt = resample_mask(ground_truth, mask.shape) & aoi_mask(area, aoi, mask.shape)
        results[baseline_name] = (compute_metrics(mask, gt), confirmed)
    return results


def woody_loss_mask(class_t1: numpy.ndarray, class_t2: numpy.ndarray) -> numpy.ndarray:
    """True donde MapBiomas marca clase leñosa (WOODY_CODES) en t1 y no leñosa en t2."""
    return numpy.isin(class_t1, list(WOODY_CODES)) & ~numpy.isin(class_t2, list(WOODY_CODES))


def run_enrich(events_path, out_path):
    cfg = load_config()
    Preprocessor(ee_project=cfg["ee_project"])  # NOTE: sólo para disparar ee.Initialize, ver preprocessing.py
    events = pandas.read_csv(events_path)
    enriched = enrich_events_with_external_reference(events)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(out_path, index=False)
    print(f"Exportado: {out_path}")


def run_full(start_year: int, end_year: int, out_path, small: bool = False):
    cfg = load_config()
    area = load_area(small=small)
    aoi = load_aoi_geometry()
    min_area_m2 = cfg["detection"]["min_event_area_m2"]
    min_persistence = cfg["detection"]["min_persistence_years"]
    processor = Preprocessor(ee_project=cfg["ee_project"])

    out_path.parent.mkdir(parents=True, exist_ok=True)

    series = build_series_with_context(processor, area, start_year, end_year)

    rows = []
    for year_from, year_to in zip(range(start_year, end_year), range(start_year + 1, end_year + 1)):
        metrics_by_baseline = validate_year_pair(processor, area, aoi, min_area_m2, min_persistence, series, year_from, year_to)
        for baseline_name, (metrics, confirmed) in metrics_by_baseline.items():
            row = {"year_from": year_from, "year_to": year_to, "baseline": baseline_name, "confirmed": confirmed}
            row.update(dataclasses.asdict(metrics))
            rows.append(row)
        print(f"{year_from}->{year_to}: listo")

        # NOTE: checkpoint por año procesado - si el proceso crashea más
        # adelante, no se pierde la descarga/validación ya hecha.
        if rows:
            pandas.DataFrame(rows).to_csv(out_path, index=False)

    if not rows:
        print("Sin resultados (¿ningún par de años con datos en el rango pedido?).")
        return
    print(f"Exportado: {out_path}")


def selftest():
    class_t1 = numpy.array([[3, 12], [15, 9]])  # bosque cerrado, pastizal / pastura, silvicultura
    class_t2 = numpy.array([[12, 12], [15, 22]])  # bosque cerrado -> pastizal (pérdida); silvicultura -> área sin vegetación (pérdida)
    mask = woody_loss_mask(class_t1, class_t2)
    assert mask.tolist() == [[True, False], [False, True]], mask
    print("woody_loss_mask OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enrich", type=str, help="CSV de eventos a enriquecer (ej. manual_review_sample.csv)")
    parser.add_argument("--full", action="store_true", help="correr los 3 baselines contra MapBiomas en un rango de años")
    parser.add_argument("--selftest", action="store_true", help="correr el self-check con datos sintéticos, sin llamar a EE")
    parser.add_argument("--small", action="store_true", help="usar el recorte chico del AOI (0.02x0.02 grados) en vez del AOI completo")
    parser.add_argument("--start-year", type=int, default=2017)
    parser.add_argument("--end-year", type=int, default=2018)
    args = parser.parse_args()

    if args.selftest:
        selftest()
    if args.enrich:
        in_path = PROJECT_ROOT / args.enrich if not args.enrich.startswith("/") else args.enrich
        run_enrich(in_path, PROJECT_ROOT / "data" / "exports" / "external_validation_sample.csv")
    if args.full:
        run_full(args.start_year, args.end_year, PROJECT_ROOT / "data" / "exports" / "external_validation_summary.csv", small=args.small)
    if not args.enrich and not args.full and not args.selftest:
        parser.print_help()
