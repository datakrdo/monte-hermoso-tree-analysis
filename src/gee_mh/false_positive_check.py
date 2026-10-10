"""Prueba de qué separa la pérdida real de la climática, con lo revisado en Google Earth Pro.

Etiquetas: las de data/exports/manual_review_sample.csv (1 = pérdida real, 0 = falso positivo) más la
revisión de 2020: el sitio 1 (el mayor evento ndvi_diff, Los Bosques) es real; los ndvi_diff del este
de Balneario Sauce Grande y la mancha cva mayor de 2020 son estacionales. Solo se usa lo que se miró.
No cambia el detector: mide variables por evento (compuestos en caché + NDVI de primavera), calcula el
AUC de cada una y evalúa reglas de "umbral que conserva todo lo real".

Uso:
    PYTHONPATH=src python3 -m gee_mh.false_positive_check
"""

import numpy
import pandas
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from gee_mh.config import PROJECT_ROOT
from gee_mh.preprocessing import DYNAMIC_WORLD_CLASSES

EXPORTS = PROJECT_ROOT / "data" / "exports"
REVIEWS = PROJECT_ROOT / "data" / "reference" / "revisiones_google_earth.csv"
EAST_LON = -61.25  # al este de este meridiano está Balneario Sauce Grande
MIN_FALSE_REMOVED = 0.8  # una regla solo se propone si elimina al menos esta fracción de los falsos de 2020 sin perder reales


def label_events(events, sample: pandas.DataFrame) -> pandas.DataFrame:
    """
    events: GeoDataFrame de events_by_baseline (índice 0..n-1; baseline, year_to, area_m2, geometría en WGS84);
    sample: la muestra manual con label_true_positive. Return DataFrame indexado como events con `label`
    (1/0) y `source` ("muestra" o "2020"). Un evento de la muestra gana sobre la revisión de 2020 y los
    desacuerdos se imprimen.
    """
    labels = {}
    metric = events.to_crs(32720)
    lon = metric.geometry.centroid.to_crs(4326).x
    y2020 = events[events["year_to"] == 2020]
    nd = y2020[y2020["baseline"] == "ndvi_diff"]
    site1 = nd["area_m2"].idxmax()
    labels[site1] = (1, "2020")
    for i in nd.index[lon[nd.index] >= EAST_LON]:
        labels[i] = (0, "2020")
    labels[y2020[y2020["baseline"] == "cva"]["area_m2"].idxmax()] = (0, "2020")
    reviews = REVIEWS
    if reviews.exists():
        from shapely.geometry import Point
        import geopandas
        sites = pandas.read_csv(reviews)
        sites = sites[sites["radius_m"].notna()]
        centroids = metric.geometry.centroid
        for site in sites.itertuples():
            c = geopandas.GeoSeries([Point(site.lon, site.lat)], crs=4326).to_crs(32720).iloc[0]
            inside = y2020.index[(centroids[y2020.index].distance(c) <= site.radius_m)]
            for i in inside:
                if i not in labels:  # lo ya etiquetado (sitio 1, este) no se pisa
                    labels[i] = (1 if site.verdict == "real" else 0, "2020")
    cent = events.geometry.centroid
    for row in sample[sample["label_true_positive"].isin([0, 1])].itertuples():
        near = events[(events["baseline"] == row.baseline) & (events["year_to"] == row.year_to) & ((events["area_m2"] - row.area_m2).abs() < 1)]
        near = near[((cent[near.index].y - row.centroid_lat) ** 2 + (cent[near.index].x - row.centroid_lon) ** 2) ** 0.5 < 1e-4]
        for i in near.index:
            if i in labels and labels[i][0] != int(row.label_true_positive):
                print(f"AVISO: el evento {i} está en la muestra con {int(row.label_true_positive)} y en 2020 con {labels[i][0]}")
            labels[i] = (int(row.label_true_positive), "muestra")
    out = pandas.DataFrame.from_dict(labels, orient="index", columns=["label", "source"])
    return out.sort_index()


def event_features(events, series: dict, area) -> pandas.DataFrame:
    """Variables por evento a 10 m: NDVI antes/después/siguiente, caída y recuperación, cobertura de Dynamic World antes
    (árbol, arbusto, pasto, suelo desnudo, construido) y el árbol que queda, más área y compacidad."""
    metric = events.to_crs(32720)
    rows = []
    for i, ev in events.iterrows():
        y0, y1 = int(ev["year_from"]), int(ev["year_to"])
        before, after = series[y0], series[y1]
        shape = before.bands["ndvi"].shape
        mask = rasterize([(ev.geometry, 1)], out_shape=shape, transform=from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, shape[1], shape[0]), fill=0).astype(bool)
        if not mask.any():
            continue

        def mean(values):
            return float(numpy.nanmean(values[mask]))

        def frac(image, *names):
            return float(numpy.mean(numpy.isin(image.dw_label[mask], [DYNAMIC_WORLD_CLASSES.index(n) for n in names])))

        row = {"event": i, "ndvi_before": mean(before.bands["ndvi"]), "ndvi_after": mean(after.bands["ndvi"])}
        row["ndvi_drop"] = row["ndvi_before"] - row["ndvi_after"]
        nxt = series.get(y1 + 1)
        row["ndvi_recovery"] = mean(nxt.bands["ndvi"]) - row["ndvi_after"] if nxt is not None else numpy.nan
        if before.dw_label is not None:
            row.update({f"dw_{n}": frac(before, n) for n in ("trees", "shrub_and_scrub", "grass", "bare", "built")})
            row["dw_woody_after"] = frac(after, "trees", "shrub_and_scrub")
            row["dw_trees_p"] = mean(before.dw_trees_p)
        geom = metric.geometry[i]
        row["area_m2"] = ev["area_m2"]
        row["compactness"] = 4 * numpy.pi * geom.area / geom.length ** 2
        rows.append(row)
    return pandas.DataFrame(rows).set_index("event")


def spring_features(events) -> pandas.DataFrame:
    """NDVI mediano de octubre-noviembre (primavera) del año de partida y del de llegada: la pérdida real sigue baja,
    lo estacional vuelve. Necesita Earth Engine."""
    import ee

    from gee_mh.config import load_config

    ee.Initialize(project=load_config()["ee_project"])
    cs = ee.ImageCollection("GOOGLE/CLOUD_SCORE_PLUS/V1/S2_HARMONIZED")

    def spring(year):
        col = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterDate(f"{year}-10-01", f"{year}-11-30").linkCollection(cs, ["cs"])
        return col.map(lambda i: i.normalizedDifference(["B8", "B4"]).updateMask(i.select("cs").gte(0.6)).rename("n")).median()

    out = {}
    for (y0, y1), group in events.groupby(["year_from", "year_to"]):
        image = ee.Image.cat([spring(int(y0)).rename("spring0"), spring(int(y1)).rename("spring1")])
        fc = ee.FeatureCollection([ee.Feature(ee.Geometry(g.__geo_interface__), {"i": int(i)}) for i, g in group.geometry.items()])
        for f in image.reduceRegions(fc, ee.Reducer.mean(), scale=10).getInfo()["features"]:
            out[f["properties"]["i"]] = {"spring_between": f["properties"].get("spring0"), "spring_after": f["properties"].get("spring1")}
    return pandas.DataFrame.from_dict(out, orient="index")


def auc(values: numpy.ndarray, label: numpy.ndarray) -> float:
    """P(valor real > valor falso) por rangos (Mann-Whitney), 0,5 = no separa."""
    ok = ~numpy.isnan(values)
    values, label = values[ok], label[ok]
    pos, neg = values[label == 1], values[label == 0]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    return float((((pos[:, None] > neg[None, :]).sum()) + 0.5 * (pos[:, None] == neg[None, :]).sum()) / (len(pos) * len(neg)))


def bootstrap_auc(values, label, n=500, seed=0):
    rng = numpy.random.default_rng(seed)
    pos, neg = numpy.flatnonzero(label == 1), numpy.flatnonzero(label == 0)
    draws = [auc(values[idx := numpy.concatenate([rng.choice(pos, len(pos)), rng.choice(neg, len(neg))])], label[idx]) for _ in range(n)]
    return float(numpy.nanpercentile(draws, 2.5)), float(numpy.nanpercentile(draws, 97.5))


def keep_all_rule(values: numpy.ndarray, label: numpy.ndarray, higher_is_real: bool) -> dict:
    """Umbral que conserva todos los reales (el mínimo de los reales si lo real es alto, el máximo si es bajo): fracción de
    falsos eliminados, y en validación dejando uno fuera, cuántos reales habría perdido (el umbral se arma sin él)."""
    ok = ~numpy.isnan(values)
    values, label = values[ok], label[ok]
    sign = 1 if higher_is_real else -1
    real, false = sign * values[label == 1], sign * values[label == 0]
    removed = float(numpy.mean(false < real.min()))
    lost = sum(real[j] < numpy.delete(real, j).min() for j in range(len(real))) if len(real) > 1 else numpy.nan
    return {"removed_false": removed, "loo_reals_lost": lost, "n_real": len(real), "n_false": len(false)}


def analyze(table: pandas.DataFrame, variables: list) -> pandas.DataFrame:
    """table: variables + label (1/0) + source. Return una fila por variable con AUC (IC 95 % bootstrap), la regla de
    umbral y la misma prueba solo sobre 2020 (entrenando únicamente con lo que no es de 2020 no alcanza: los reales de
    2020 son uno)."""
    rows = []
    label = table["label"].to_numpy()
    for name in variables:
        values = table[name].to_numpy(dtype=float)
        a = auc(values, label)
        lo, hi = bootstrap_auc(values, label)
        rule = keep_all_rule(values, label, higher_is_real=a >= 0.5)
        rows.append({"variable": name, "auc": a, "ci_low": lo, "ci_high": hi, "direction": "alto=real" if a >= 0.5 else "bajo=real", **rule})
    return pandas.DataFrame(rows).sort_values("auc", key=lambda s: (s - 0.5).abs(), ascending=False)


def selftest():
    rng = numpy.random.default_rng(1)
    label = numpy.array([1] * 30 + [0] * 30)
    table = pandas.DataFrame({"label": label, "separa": numpy.where(label == 1, rng.normal(1, 0.3, 60), rng.normal(0, 0.3, 60)),
                              "ruido": rng.normal(0, 1, 60), "inversa": numpy.where(label == 1, rng.normal(-1, 0.3, 60), rng.normal(0, 0.3, 60))})
    result = analyze(table, ["separa", "ruido", "inversa"]).set_index("variable")
    assert result.loc["separa", "auc"] > 0.95 and result.loc["inversa", "auc"] < 0.05, result
    assert abs(result.loc["ruido", "auc"] - 0.5) < 0.2 and result.loc["ruido", "ci_low"] < 0.5 < result.loc["ruido", "ci_high"], result
    assert result.loc["inversa", "direction"] == "bajo=real" and result.loc["separa", "removed_false"] > 0.5
    assert auc(numpy.array([1.0, 1.0]), numpy.array([1, 0])) == 0.5
    rule = keep_all_rule(numpy.array([5.0, 6.0, 1.0, 2.0, 5.5]), numpy.array([1, 1, 0, 0, 0]), True)
    assert rule["removed_false"] == 2 / 3 and rule["loo_reals_lost"] == 1  # el real de 5 queda bajo el mínimo del otro (6)
    events = _labels_events()
    sample = pandas.DataFrame({"baseline": ["ndvi_diff"], "year_to": [2020], "area_m2": [100.0], "centroid_lat": [-38.9899], "centroid_lon": [-61.1999], "label_true_positive": [1]})
    got = label_events(events, sample)
    assert got.loc[0, "label"] == 1 and got.loc[1, "label"] == 0 and got.loc[2, "label"] == 0 and got.loc[3, "label"] == 0 and got.loc[4, "label"] == 1, got
    print("false_positive_check selftest OK")


def _labels_events():
    import geopandas
    from shapely.geometry import box

    def sq(lon, lat):
        return box(lon, lat, lon + 0.0002, lat + 0.0002)

    # 0: el mayor ndvi_diff (sitio 1, oeste); 1 y 2: ndvi_diff del este; 3: el cva mayor; 4: ndvi_diff del este, pero en la muestra con 1
    return geopandas.GeoDataFrame({
        "baseline": ["ndvi_diff", "ndvi_diff", "ndvi_diff", "cva", "ndvi_diff"], "year_to": [2020] * 5,
        "area_m2": [25000.0, 1500.0, 1400.0, 40000.0, 100.0],
    }, geometry=[sq(-61.35, -38.97), sq(-61.18, -38.99), sq(-61.19, -38.99), sq(-61.30, -38.98), sq(-61.20, -38.99)], crs="EPSG:4326")


def run():
    import geopandas

    from gee_mh.config import load_config
    from gee_mh.pipeline import load_area
    from gee_mh.preprocessing import Preprocessor
    from gee_mh.timeseries import build_series_with_context

    events = geopandas.read_file(EXPORTS / "events_by_baseline.geojson").reset_index(drop=True)
    sample = pandas.read_csv(EXPORTS / "manual_review_sample.csv")
    labels = label_events(events, sample)
    print(f"etiquetados: {len(labels)} (reales {int((labels['label'] == 1).sum())}, falsos {int((labels['label'] == 0).sum())}); "
          f"de 2020: {int((labels['source'] == '2020').sum())}, de la muestra: {int((labels['source'] == 'muestra').sum())}")
    labeled = events.loc[labels.index]
    area = load_area(small=False)
    series = build_series_with_context(Preprocessor(ee_project=load_config()["ee_project"]), area, 2016, 2026)
    table = labels.join(event_features(labeled, series, area)).join(spring_features(labeled))
    table["spring_ratio"] = table["spring_after"] / table["ndvi_before"]
    table.to_csv(EXPORTS / "features_labeled_events.csv")
    variables = [c for c in table.columns if c not in ("label", "source")]
    result = analyze(table, variables)
    print(result.round(3).to_string(index=False))
    print(f"\nmuestra sola: {len(table[table['source'] == 'muestra'])} etiquetas; falsos de 2020: {int(((table['source'] == '2020') & (table['label'] == 0)).sum())}")
    print(f"criterio: eliminar al menos {MIN_FALSE_REMOVED:.0%} de los falsos sin perder reales -> variables que lo cumplen:",
          result[(result["removed_false"] >= MIN_FALSE_REMOVED) & (result["loo_reals_lost"] == 0)]["variable"].tolist() or "ninguna")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
