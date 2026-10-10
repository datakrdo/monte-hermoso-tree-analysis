"""Serie larga 1985-2026 a 30 m con Landsat 5/7/8/9 (Colección 2), un solo
método para todo el período: sirve para comparar antes y después de la
Ordenanza 1913 (2010), cuando Sentinel-2 todavía no existía.

Un compuesto de verano por año (mediana de +-60 días del 15 de febrero; una
ventana más ancha que la de Sentinel-2 porque antes de 2000 hay pocas escenas
Landsat) alimenta LandTrendr, que ajusta segmentos
lineales por píxel: una caída de un solo año que se recupera (sequía) se
suaviza, y un cambio que persiste (obra, tala) queda como escalón. Sobre los
valores ajustados se miden la pérdida y la ganancia de cobertura arbórea
que persisten (no se recuperan en PERSISTENCE_YEARS años: así una sequía no
cuenta como tala), descontando el corrimiento regional de NDVI de cada par
(ver detection.normalize_regional_shift). El construido sale de MapBiomas
Argentina (clase urbana): a 30 m, NDBI no separa bien el construido de la
arena (F1 0,12 contra Dynamic World).

Uso:
    PYTHONPATH=src python3 -m gee_mh.longterm --start-year 1985 --end-year 2026
"""

import argparse
import datetime

import cv2
import ee
import geopandas
import numpy
import pandas
from shapely.geometry import mapping as shapely_mapping

from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.detection import NDVI_DROP_THRESHOLD, NDVI_VEGETATION_THRESHOLD, filter_small_blobs
from gee_mh.export import mask_to_polygons
from gee_mh.external_validation import MAPBIOMAS_ASSET, URBAN_CODE, mapbiomas_years
from gee_mh.masks import aoi_mask
from gee_mh.pipeline import load_aoi_geometry, load_area, load_partido_area, load_partido_geometry
from gee_mh.preprocessing import DYNAMIC_WORLD_CLASSES, SOURCES, DataSource, Preprocessor, mask_landsat_clouds
from gee_mh.timeseries import build_annual_series

RESOLUTION_M = 30.0
WINDOW_DAYS = 60
MAX_CLOUD_PERCENT = 35.0
MIN_OBSERVATIONS = 2  # un píxel-año con una sola observación no resiste una nube o sombra sin detectar: queda sin dato
MIN_VALID_FRACTION = 0.8  # años con menos píxeles válidos (>= MIN_OBSERVATIONS) que esto se marcan como pobres en datos
MIN_EVENT_AREA_M2 = 2700.0  # 3 píxeles de 30 m: uno solo es ruido de píxel mezclado
ORDINANCE_YEAR = 2010  # Ordenanza 1913 de Monte Hermoso
PIXELS_PER_REQUEST = 100_000  # LandTrendr es pesado: pedidos chicos
# NDVI de Landsat 8/9 menos Landsat 7 sobre vegetación en los años en que ambos vuelan (2014-2021): la mediana de
# las medianas anuales es 0,039 (de -0,009 a 0,115 según el año). Se resta ese valor al NDVI de Landsat 8/9.
OLI_NDVI_OFFSET = -0.04
PERSISTENCE_YEARS = 3  # una pérdida (ganancia) cuenta si sigue así este número de años después
# Parámetros de ejemplo del LT-GEE oficial (github.com/eMapR/LT-GEE)
LANDTRENDR = dict(
    maxSegments=6, spikeThreshold=0.9, vertexCountOvershoot=3, preventOneYearRecovery=True,
    recoveryThreshold=0.25, pvalThreshold=0.05, bestModelProportion=0.75, minObservationsNeeded=6)
EXPORTS = PROJECT_ROOT / "data" / "exports"


def landsat_collection(area, center: ee.Date) -> ee.ImageCollection:
    """
    Landsat 5/7/8/9 sin nubes (QA_PIXEL) en la ventana, una banda 'ndvi' por escena. El NDVI de Landsat
    8/9 se corre OLI_NDVI_OFFSET para alinearlo con Landsat 5/7: sin eso, el cambio de sensor de 2013 en
    adelante suma cobertura "arbórea" de la nada.
    """
    collection = None
    for source, offset in ((DataSource.LANDSAT5, 0.0), (DataSource.LANDSAT7, 0.0), (DataSource.LANDSAT8, OLI_NDVI_OFFSET)):
        spec = SOURCES[source]
        part = ee.ImageCollection(spec["collections"][0])
        for extra in spec["collections"][1:]:
            part = part.merge(ee.ImageCollection(extra))
        part = part.filterBounds(area.to_ee_box()) \
            .filterDate(center.advance(-WINDOW_DAYS, "day"), center.advance(WINDOW_DAYS, "day")) \
            .filter(ee.Filter.lt(spec["cloud"], MAX_CLOUD_PERCENT)) \
            .map(mask_landsat_clouds) \
            .map(lambda image, spec=spec, offset=offset: image.select(spec["bands"], ["red", "nir", "swir"])
                 .multiply(spec["scale"]).add(spec["offset"]).normalizedDifference(["nir", "red"]).add(offset).toFloat().rename("ndvi"))
        collection = part if collection is None else collection.merge(part)
    return collection


def annual_image(area, year: int) -> ee.Image:
    """
    Bandas segidx (-NDVI x1000: la perturbación sube, como pide LandTrendr) y
    ndvi (x1000): mediana de los NDVI de las escenas, solo donde hay al menos MIN_OBSERVATIONS.
    Un año sin escenas es una imagen enmascarada.
    """
    center = ee.Date(f"{year}-02-15")
    collection = landsat_collection(area, center)

    def build(collection):
        collection = ee.ImageCollection(collection)
        ndvi = collection.median().updateMask(collection.count().gte(MIN_OBSERVATIONS))
        return ee.Image.cat([ndvi.multiply(-1000), ndvi.multiply(1000)]).rename(["segidx", "ndvi"]).toShort()

    empty = ee.Image.constant([0, 0]).rename(["segidx", "ndvi"]).toShort().updateMask(ee.Image.constant(0))
    return ee.Image(ee.Algorithms.If(collection.size().gt(0), build(collection), empty)).set("system:time_start", center.millis())


def scenes_per_year(area, years) -> dict:
    """Escenas de la ventana de cada año (una sola llamada a EE)."""
    counts = {str(y): landsat_collection(area, ee.Date(f"{y}-02-15")).size() for y in years}
    return {int(y): n for y, n in ee.Dictionary(counts).getInfo().items()}


def valid_fraction(processor: Preprocessor, area, aoi, years) -> dict:
    """Fracción del partido con al menos MIN_OBSERVATIONS observaciones sin nube en la ventana de cada año."""
    def download():
        bands = []
        for year in years:
            collection = landsat_collection(area, ee.Date(f"{year}-02-15"))
            count = ee.Image(ee.Algorithms.If(collection.size().gt(0), collection.count(), ee.Image.constant(0)))
            bands.append(count.gte(MIN_OBSERVATIONS).rename(f"y{year}"))
        values = ee.Image.cat(bands).reduceRegion(ee.Reducer.mean(), ee.Geometry(shapely_mapping(aoi)), 90, maxPixels=1e9).getInfo()
        return numpy.array([values[f"y{year}"] if values[f"y{year}"] is not None else 0.0 for year in years], dtype=float)
    # un año sin ninguna escena devuelve None en la reducción (la caché guarda NaN): sin dato = 0
    return dict(zip(years, numpy.nan_to_num(processor.cached(("valid_fraction", area, years[0], years[-1], WINDOW_DAYS, MAX_CLOUD_PERCENT, MIN_OBSERVATIONS, OLI_NDVI_OFFSET), download))))


def fitted_series(processor: Preprocessor, area, years) -> numpy.ndarray:
    """
    NDVI ajustado por LandTrendr, array (años, alto, ancho) float32. Un píxel
    sin ajuste (agua sin datos) queda en 0.
    """
    series = ee.ImageCollection([annual_image(area, y) for y in years])
    fitted = ee.Algorithms.TemporalSegmentation.LandTrendr(timeSeries=series, **LANDTRENDR)
    names = [str(y) for y in years]
    image = fitted.select("ndvi_fit").arrayFlatten([names]).round().toShort()
    raw = processor.cached(
        ("landtrendr", "ndvi", area, years[0], years[-1], tuple(sorted(LANDTRENDR.items())), WINDOW_DAYS, MAX_CLOUD_PERCENT, MIN_OBSERVATIONS, OLI_NDVI_OFFSET),
        lambda: processor.download_numpy_data(image, area, names, RESOLUTION_M, PIXELS_PER_REQUEST))
    return numpy.stack([raw[name] for name in names]).astype(numpy.float32) / 1000.0


def urban_area_ha(processor: Preprocessor, aoi, years) -> dict:
    """Hectáreas de clase urbana de MapBiomas Argentina dentro del partido (años posteriores al último se repiten)."""
    bands = list(dict.fromkeys(f"classification_{mapbiomas_years(year, year)[0]}" for year in years))

    def download():
        # una sola reducción con todas las bandas: pedir 42 por separado supera el límite de agregaciones simultáneas
        classes = ee.Image(MAPBIOMAS_ASSET).select(bands)
        hectares = classes.eq(URBAN_CODE).multiply(ee.Image.pixelArea().divide(10_000))
        values = hectares.reduceRegion(ee.Reducer.sum(), ee.Geometry(shapely_mapping(aoi)), 30, maxPixels=1e9).getInfo()
        return numpy.array([values[f"classification_{mapbiomas_years(year, year)[0]}"] for year in years], dtype=float)
    return dict(zip(years, processor.cached(("mapbiomas_urban", MAPBIOMAS_ASSET, years[0], years[-1]), download)))


def rainfall_mm(processor: Preprocessor, area, years) -> dict:
    """Lluvia CHIRPS del 1-sep al 15-mar previos al compuesto de cada año (mm sobre el área)."""
    def download():
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
        sums = {str(y): chirps.filterDate(f"{y - 1}-09-01", f"{y}-03-16").sum()
                .reduceRegion(ee.Reducer.mean(), area.to_ee_box(), 5000).get("precipitation") for y in years}
        values = ee.Dictionary(sums).getInfo()
        return numpy.array([values[str(y)] for y in years], dtype=float)
    return dict(zip(years, processor.cached(("chirps", area, years[0], years[-1]), download)))


def fraction_at_30m(mask: numpy.ndarray, shape: tuple) -> numpy.ndarray:
    """Fracción de cada píxel de 30 m cubierta por una máscara de 10 m."""
    return cv2.resize(mask.astype(numpy.float32), (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def best_threshold(values: numpy.ndarray, reference: numpy.ndarray, candidates) -> tuple:
    """Umbral (>=) de values que mejor reproduce reference (booleana) por F1. Return (umbral, F1)."""
    best = (None, -1.0)
    for threshold in candidates:
        predicted = values >= threshold
        tp = int((predicted & reference).sum())
        fp = int((predicted & ~reference).sum())
        fn = int((~predicted & reference).sum())
        f1 = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        if f1 > best[1]:
            best = (float(threshold), f1)
    return best


def regional_shift(ndvi_before: numpy.ndarray, ndvi_after: numpy.ndarray, reference: numpy.ndarray) -> float:
    """Caída mediana de NDVI de la vegetación de referencia (positiva = el verde bajó en toda la región)."""
    if reference.sum() < 100:
        return 0.0
    return float(numpy.median(ndvi_before[reference] - ndvi_after[reference]))


def year_transitions(ndvi, i, inside, tree_threshold, shift=None) -> dict:
    """
    Cambios de cobertura arbórea (NDVI >= tree_threshold) entre el año i-1 y el i sobre la serie
    ajustada (array años x alto x ancho). Una pérdida es una caída mayor que NDVI_DROP_THRESHOLD
    sobre el corrimiento regional del par que además sigue sin recuperarse PERSISTENCE_YEARS años
    después (o en el último año que hay); una ganancia es lo inverso. El corrimiento solo se
    descuenta en la dirección que se mide, igual que en detection.normalize_regional_shift. `shift`
    es el corrimiento medido en una región más grande (todo el partido); si es None se mide dentro
    de `inside`.
    """
    before, after = ndvi[i - 1], ndvi[i]
    later = ndvi[min(i + PERSISTENCE_YEARS, len(ndvi) - 1)]
    tree_before = before >= tree_threshold
    if shift is None:
        shift = regional_shift(before, after, tree_before & inside)
    loss = tree_before & (after < tree_threshold) & (before - after - max(shift, 0.0) > NDVI_DROP_THRESHOLD) & (later < tree_threshold)
    gain = ~tree_before & (after >= tree_threshold) & (after - before - max(-shift, 0.0) > NDVI_DROP_THRESHOLD) & (later >= tree_threshold)
    return {"loss": loss, "gain": gain, "shift": shift}


def calibrate(processor: Preprocessor, area, years, ndvi, inside) -> dict:
    """
    Umbral de árboles de la serie Landsat contra Dynamic World (Sentinel-2, 2017-2025, solo como
    referencia de calibración): NDVI >= tree_threshold reproduce la clase 'trees' a 30 m.
    """
    shape = ndvi.shape[1:]
    pairs = []
    for year, image in sorted(build_annual_series(processor, area, 2017, min(2025, years[-1])).items()):
        if image.dw_label is None or year not in years:
            continue
        reference = fraction_at_30m(image.to_dynamic_world_mask("trees"), shape) >= 0.5
        pairs.append((ndvi[years.index(year)][inside], reference[inside]))
    tree_threshold, tree_f1 = best_threshold(
        numpy.concatenate([v for v, _ in pairs]), numpy.concatenate([r for _, r in pairs]), numpy.arange(0.30, 0.86, 0.025))
    print(f"umbral de árboles NDVI >= {tree_threshold:.3f} (F1 {tree_f1:.2f} contra Dynamic World)")
    return {"tree_threshold": tree_threshold, "tree_f1": tree_f1}


def ordinance_summary(rates: pandas.DataFrame, metrics=("loss_ha", "gain_ha", "net_tree_ha", "urban_net_ha"), draws: int = 10000, seed: int = 0) -> pandas.DataFrame:
    """
    Antes (year_to <= ORDINANCE_YEAR) contra después (year_to >= ORDINANCE_YEAR + 2; el par 2010-2011
    cruza la sanción de la ordenanza y queda afuera). Por métrica: promedio anual de cada período,
    p de una prueba de permutación de la diferencia, y el efecto "después" ajustado por lluvia
    (regresión métrica ~ después + lluvia) con IC 95% por bootstrap de años. Se informa sobre todos los
    años y solo sobre los años con al menos MIN_VALID_FRACTION del partido con datos. No hay partido de control:
    un cambio entre períodos no prueba que la ordenanza lo causó.
    """
    rng = numpy.random.default_rng(seed)
    rows = []
    for subset, data in (("todos los años", rates), ("años con >= 80 % de datos", rates[rates["valid_fraction"] >= MIN_VALID_FRACTION])):
        before = data[data["year_to"] <= ORDINANCE_YEAR]
        after = data[data["year_to"] >= ORDINANCE_YEAR + 2]
        if len(before) < 3 or len(after) < 3:
            continue
        for metric in metrics:
            a, b = before[metric].to_numpy(float), after[metric].to_numpy(float)
            difference = b.mean() - a.mean()
            pooled = numpy.concatenate([a, b])
            permuted = numpy.array([
                (lambda q: q[len(a):].mean() - q[:len(a)].mean())(rng.permutation(pooled)) for _ in range(draws)])
            period = pandas.concat([before, after])
            design = numpy.column_stack([
                numpy.ones(len(period)), (period["year_to"] >= ORDINANCE_YEAR + 2).to_numpy(float), period["rain_mm"].to_numpy(float)])
            target = period[metric].to_numpy(float)

            def after_effect(index):
                return numpy.linalg.lstsq(design[index], target[index], rcond=None)[0][1]

            boot = [after_effect(rng.integers(0, len(period), len(period))) for _ in range(2000)]
            rows.append({
                "subset": subset, "metric": metric, "years_before": len(before), "years_after": len(after),
                "mean_before": a.mean(), "mean_after": b.mean(), "difference": difference,
                "p_permutation": float((numpy.abs(permuted) >= abs(difference)).mean()),
                "after_effect_rain_adjusted": after_effect(numpy.arange(len(period))),
                "ci95_low": float(numpy.percentile(boot, 2.5)), "ci95_high": float(numpy.percentile(boot, 97.5)),
            })
    return pandas.DataFrame(rows)


def run(start_year: int, end_year: int):
    cfg = load_config()
    area = load_area(small=False)
    aoi = load_aoi_geometry()
    partido_area, partido = load_partido_area(), load_partido_geometry()
    processor = Preprocessor(ee_project=cfg["ee_project"])
    years = list(range(start_year, end_year + 1))

    # zona de estudio (la de config.yaml) y partido completo: el corrimiento regional y el umbral de árboles se miden en el
    # partido, para que un cambio real y amplio de la zona no se reste a sí mismo como si fuera clima
    ndvi = fitted_series(processor, area, years)
    inside = aoi_mask(area, aoi, ndvi.shape[1:])
    ndvi_partido = fitted_series(processor, partido_area, years)
    inside_partido = aoi_mask(partido_area, partido, ndvi_partido.shape[1:])
    in_zone_partido = aoi_mask(partido_area, aoi, ndvi_partido.shape[1:])
    tree_threshold = calibrate(processor, partido_area, years, ndvi_partido, inside_partido)["tree_threshold"]
    shifts = [None] + [regional_shift(ndvi_partido[i - 1], ndvi_partido[i], (ndvi_partido[i - 1] >= tree_threshold) & inside_partido)
                       for i in range(1, len(years))]

    scenes = processor.cached(("scenes", area, years[0], years[-1], WINDOW_DAYS, MAX_CLOUD_PERCENT),
                              lambda: numpy.array([scenes_per_year(area, years)[y] for y in years]))
    rain = rainfall_mm(processor, area, years)
    urban = urban_area_ha(processor, aoi, years)
    valid = valid_fraction(processor, area, aoi, years)
    pixel_ha = RESOLUTION_M ** 2 / 10_000.0

    rows, events, comparison = [], [], []
    rest = inside_partido & ~in_zone_partido
    for i in range(1, len(years)):
        found = year_transitions(ndvi, i, inside, tree_threshold, shifts[i])
        row = {
            "year_to": years[i], "n_scenes": int(scenes[i]), "valid_fraction": valid[years[i]], "rain_mm": rain[years[i]], "ndvi_shift": shifts[i],
            "provisional": i + PERSISTENCE_YEARS > len(years) - 1,
            "tree_ha": float((ndvi[i] >= tree_threshold)[inside].sum() * pixel_ha),
            "vegetation_ha": float((ndvi[i] >= NDVI_VEGETATION_THRESHOLD)[inside].sum() * pixel_ha),
            "urban_ha_mapbiomas": urban[years[i]], "urban_net_ha": urban[years[i]] - urban[years[i - 1]],
        }
        row["net_tree_ha"] = row["tree_ha"] - float((ndvi[i - 1] >= tree_threshold)[inside].sum() * pixel_ha)
        for kind in ("loss", "gain"):
            mask = filter_small_blobs(found[kind] & inside, RESOLUTION_M, MIN_EVENT_AREA_M2)
            row[f"{kind}_ha"] = float(mask.sum() * pixel_ha)
            for polygon in mask_to_polygons(area, mask):
                events.append({"geometry": polygon, "kind": kind, "year_to": years[i], "provisional": row["provisional"]})
        rows.append(row)

        # el resto del partido, con los mismos umbrales y el mismo corrimiento: contexto, no control (la zona se eligió por tener construcción)
        found_partido = year_transitions(ndvi_partido, i, inside_partido, tree_threshold, shifts[i])
        comparison.append({
            "year_to": years[i], "provisional": row["provisional"],
            "zone_area_km2": float(inside.sum() * pixel_ha / 100), "rest_area_km2": float(rest.sum() * pixel_ha / 100),
            "zone_tree_ha": row["tree_ha"], "rest_tree_ha": float((ndvi_partido[i] >= tree_threshold)[rest].sum() * pixel_ha),
            "zone_loss_ha": row["loss_ha"],
            "rest_loss_ha": float(filter_small_blobs(found_partido["loss"] & rest, RESOLUTION_M, MIN_EVENT_AREA_M2).sum() * pixel_ha),
        })

    rates = pandas.DataFrame(rows)
    versus = pandas.DataFrame(comparison)
    for side in ("zone", "rest"):
        versus[f"{side}_loss_ha_per_km2"] = versus[f"{side}_loss_ha"] / versus[f"{side}_area_km2"]
        versus[f"{side}_tree_pct"] = 100 * versus[f"{side}_tree_ha"] / (versus[f"{side}_area_km2"] * 100)
    EXPORTS.mkdir(parents=True, exist_ok=True)
    rates.to_csv(EXPORTS / "longterm_rates.csv", index=False)
    versus.to_csv(EXPORTS / "longterm_zone_vs_rest.csv", index=False)
    gdf = geopandas.GeoDataFrame(events, crs="EPSG:4326")
    gdf["area_m2"] = gdf.to_crs("EPSG:6933").geometry.area
    gdf.to_file(EXPORTS / "longterm_events.geojson", driver="GeoJSON")
    summary = ordinance_summary(rates)
    summary.to_csv(EXPORTS / "ordinance_summary.csv", index=False)
    print(rates.round(2).to_string(index=False))
    print(summary.round(3).to_string(index=False))
    print(versus.round(2).to_string(index=False))
    print("correlación de Spearman, lluvia vs. cambio neto de cobertura arbórea: "
          f"{rates['rain_mm'].corr(rates['net_tree_ha'], method='spearman'):.2f}")
    print(f"Exportado: {EXPORTS / 'longterm_rates.csv'}, longterm_zone_vs_rest.csv, longterm_events.geojson, ordinance_summary.csv")


def selftest():
    shape = (30, 30)
    inside = numpy.ones(shape, dtype=bool)
    ndvi = numpy.full((6, *shape), 0.6, dtype=numpy.float32)
    ndvi[1:, 2:8, 2:8] = 0.2     # tala: árbol -> suelo desnudo desde el año 1, no se recupera
    ndvi[1:3, 12:18, 12:18] = 0.1  # sequía: baja 2 años y se recupera
    ndvi[:2, 22:28, 22:28] = 0.2   # replante: suelo -> árbol desde el año 2
    ndvi[2:, 22:28, 22:28] = 0.6
    result = year_transitions(ndvi, 1, inside, 0.5)
    assert result["loss"][2:8, 2:8].all() and result["loss"].sum() == 36, int(result["loss"].sum())  # la sequía no cuenta
    assert not result["gain"].any()
    assert year_transitions(ndvi, 2, inside, 0.5)["gain"][22:28, 22:28].all()
    # el corrimiento medido afuera (todo el partido) manda sobre el medido adentro
    uniform = numpy.stack([numpy.full(shape, 0.6), numpy.full(shape, 0.3)]).astype(numpy.float32)  # toda la zona cae 0.3
    assert year_transitions(uniform, 1, inside, 0.45)["loss"].sum() == 0, "medido adentro, la caída de toda la zona se resta a sí misma"
    assert year_transitions(uniform, 1, inside, 0.45, shift=0.05)["loss"].all(), "con el corrimiento del partido (0.05) se conserva"
    # año seco: todo baja 0.2; solo la tala real (0.6 -> 0.1) cuenta
    dry = numpy.stack([numpy.full(shape, 0.6), numpy.full(shape, 0.4)]).astype(numpy.float32)
    dry[1, 2:8, 2:8] = 0.1
    result = year_transitions(dry, 1, inside, 0.45)
    assert abs(result["shift"] - 0.2) < 1e-6 and result["loss"].sum() == 36, int(result["loss"].sum())
    # 12 años de pérdida baja y 12 de pérdida alta: el período "después" pierde más aun con la misma lluvia
    years = list(range(1999, 2023))
    rates = pandas.DataFrame({
        "year_to": years, "n_scenes": 4, "valid_fraction": 1.0, "rain_mm": [450.0 + 20 * (y % 3) for y in years],
        "loss_ha": [1.0 + 0.1 * (y % 3) if y <= 2010 else 3.0 + 0.1 * (y % 3) for y in years], "gain_ha": 1.0, "net_tree_ha": 0.0, "urban_net_ha": 0.0})
    result = ordinance_summary(rates, draws=500).set_index(["subset", "metric"]).loc["todos los años"]
    assert abs(result.loc["loss_ha", "difference"] - 2.0) < 1e-9 and result.loc["loss_ha", "p_permutation"] < 0.01
    assert result.loc["loss_ha", "years_before"] == 12 and result.loc["loss_ha", "years_after"] == 11  # 2011 queda afuera
    assert abs(result.loc["loss_ha", "after_effect_rain_adjusted"] - 2.0) < 0.2
    assert best_threshold(numpy.array([0.1, 0.4, 0.6, 0.9]), numpy.array([False, False, True, True]), [0.3, 0.5, 0.8])[0] == 0.5
    print("longterm selftest OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--start-year", type=int, default=1985)
    parser.add_argument("--end-year", type=int, default=datetime.date.today().year)
    args = parser.parse_args()
    if args.selftest:
        selftest()
    else:
        run(args.start_year, args.end_year)
