"""Reclasifica a 3 m (PlanetScope) la causa de los eventos que Sentinel-2 dejó como
`desconocido`. Por evento mide, entre el verano de year_from y el de year_to:
caída de NDVI, agua, suelo/techo (NDVI bajo) y cuánto del evento cae dentro de manzanas.

Los umbrales de classify() NO están calibrados: se ajustan contra las etiquetas
manuales y los eventos de referencia (report) cuando haya imágenes. La colección
de Planet solo tiene PlanetScope (desde el verano 2017): los eventos con year_from
anterior quedan `sin_dato_3m`.

Imagen © Planet Labs PBC. Solo se guardan derivados (fracciones y causas).
"""

import argparse

import geopandas
import numpy
import pandas

from gee_mh.canopy import AREA_CRS, BLOCKS_PATH, CLEAR, NIR, PLANET_COLLECTION, RED
from gee_mh.config import PROJECT_ROOT, load_config

EVENTS_PATH = PROJECT_ROOT / "data" / "exports" / "events_by_baseline.geojson"
SAMPLE_PATH = PROJECT_ROOT / "data" / "exports" / "manual_review_sample.csv"
OUT_PATH = PROJECT_ROOT / "data" / "exports" / "events_planet.csv"
GREEN = "B2"
NDVI_DROP = 0.15  # caída de NDVI que cuenta como pérdida en un píxel de 3 m
LOW_NDVI = 0.20  # NDVI de suelo desnudo, techo o calle
MIN_LOSS = 0.30  # fracción del evento con caída para que haya pérdida visible
MIN_WATER = 0.30
MIN_CLEARED = 0.50  # fracción con NDVI bajo después
MIN_IN_BLOCK = 0.50  # fracción del evento dentro de manzanas
KEY = ["baseline", "year_from", "year_to", "lat", "lon"]


def event_key(events: pandas.DataFrame, lat="centroid_lat", lon="centroid_lon") -> pandas.DataFrame:
    events = events.copy()
    events["lat"], events["lon"] = events[lat].round(5), events[lon].round(5)
    return events


def classify(row) -> tuple:
    """(causa, confianza) a partir de las fracciones del evento. Misma lógica de degradar a `desconocido` que attribution.py."""
    if row["drop"] < MIN_LOSS:
        return "sin_perdida_3m", round(1 - row["drop"], 2)
    if row["water"] >= MIN_WATER:
        return "natural_erosion_inundacion", round(row["water"], 2)
    if row["cleared"] >= MIN_CLEARED and row["in_block"] >= MIN_IN_BLOCK:
        return "desarrollo_inmobiliario", round(min(row["cleared"], row["in_block"]), 2)
    return "desconocido", 0.0


def in_block_fraction(events: geopandas.GeoDataFrame) -> numpy.ndarray:
    """Fracción de cada evento que cae dentro de manzanas OSM."""
    blocks = geopandas.read_file(BLOCKS_PATH).to_crs(AREA_CRS)
    union = blocks.geometry.union_all()
    projected = events.to_crs(AREA_CRS).geometry
    return (projected.intersection(union).area / projected.area).to_numpy()


def _summer(year: int, project: str):
    """Mediana de verano (B, G, R, NIR despejados) o None si no hay escenas ese año."""
    import ee

    collection = (ee.ImageCollection(f"projects/{project}/assets/{PLANET_COLLECTION}")
                  .filterDate(f"{year}-01-15", f"{year}-03-16"))
    if collection.size().getInfo() == 0:
        return None
    return collection.map(lambda i: i.updateMask(i.select(CLEAR).eq(1))).select([GREEN, RED, NIR]).median()


def measure(events: geopandas.GeoDataFrame, project: str) -> pandas.DataFrame:
    """Fracciones por evento, una reducción por cada par (year_from, year_to)."""
    import ee

    rows = []
    for (y1, y2), group in events.groupby(["year_from", "year_to"]):
        before, after = _summer(y1, project), _summer(y2, project)
        if before is None or after is None:
            continue
        ndvi1 = before.normalizedDifference([NIR, RED])
        ndvi2 = after.normalizedDifference([NIR, RED])
        water = after.normalizedDifference([GREEN, NIR]).gt(0)
        image = ee.Image.cat(
            ndvi1.subtract(ndvi2).gt(NDVI_DROP).rename("drop"), water.rename("water"),
            ndvi2.lt(LOW_NDVI).And(water.Not()).rename("cleared"))
        features = ee.FeatureCollection([ee.Feature(ee.Geometry(g.__geo_interface__), {"i": int(i)})
                                         for i, g in zip(group.index, group.geometry)])
        for f in image.reduceRegions(features, ee.Reducer.mean(), scale=3, crs=AREA_CRS).getInfo()["features"]:
            p = f["properties"]
            if p.get("drop") is not None:
                rows.append({"index": p["i"], **{k: p[k] for k in ("drop", "water", "cleared")}})
    return pandas.DataFrame(rows).set_index("index") if rows else pandas.DataFrame(columns=["drop", "water", "cleared"])


def run(project: str) -> pandas.DataFrame:
    events = geopandas.read_file(EVENTS_PATH)
    unknown = events["cause"] == "desconocido"
    reference = (events["cause"] == "desarrollo_inmobiliario") & (events["confidence"] >= 0.6)
    events = events[unknown | reference].copy()
    events["group"] = numpy.where(events["cause"] == "desconocido", "desconocido", "referencia_construido")
    events["in_block"] = in_block_fraction(events)
    measured = measure(events, project)
    table = event_key(events).join(measured, how="left")[KEY + ["group", "in_block", "drop", "water", "cleared"]]
    done = table["drop"].notna()
    table.loc[done, ["cause_3m", "confidence_3m"]] = [classify(r) for _, r in table[done].iterrows()]
    table.loc[~done, ["cause_3m", "confidence_3m"]] = ["sin_dato_3m", 0.0]
    table.to_csv(OUT_PATH, index=False)
    print(table.groupby(["group", "cause_3m"]).size().to_string())
    return table


def report(table: pandas.DataFrame = None):
    """(a) concordancia en eventos de referencia; (b) `sin_perdida_3m` contra las etiquetas manuales."""
    from gee_mh.validation import _wilson_interval

    table = pandas.read_csv(OUT_PATH) if table is None else table
    ref = table[(table["group"] == "referencia_construido") & (table["cause_3m"] != "sin_dato_3m")]
    hits = int((ref["cause_3m"] == "desarrollo_inmobiliario").sum())
    print(f"(a) referencia construido: {hits}/{len(ref)} confirmados a 3 m, IC95 {_wilson_interval(hits, len(ref)) if len(ref) else '-'}")
    labels = event_key(pandas.read_csv(SAMPLE_PATH))
    labels = labels[labels["label_true_positive"].isin([0, 1])].merge(table[KEY + ["cause_3m"]], on=KEY, how="inner")
    labels = labels[labels["cause_3m"] != "sin_dato_3m"]
    if labels.empty:
        print("(b) ninguna etiqueta manual cae entre los eventos medidos")
        return
    seen = labels["cause_3m"] != "sin_perdida_3m"
    print(pandas.crosstab(labels["label_true_positive"], seen.rename("pérdida visible a 3 m")).to_string())


def _selftest():

    base = {"drop": 0.8, "water": 0.0, "cleared": 0.9, "in_block": 0.9}
    assert classify(base)[0] == "desarrollo_inmobiliario"
    assert classify({**base, "drop": 0.1})[0] == "sin_perdida_3m"
    assert classify({**base, "water": 0.6, "cleared": 0.0})[0] == "natural_erosion_inundacion"
    assert classify({**base, "in_block": 0.1})[0] == "desconocido"
    assert classify({**base, "cleared": 0.2})[0] == "desconocido"
    frame = pandas.DataFrame({"baseline": ["a"], "year_from": [2020], "year_to": [2021], "centroid_lat": [-38.1234549], "centroid_lon": [-61.0]})
    assert event_key(frame)["lat"].iloc[0] == -38.12345
    print("planet_attribution selftest OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--run", action="store_true", help="medir y clasificar (requiere la colección de Planet en GEE)")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
    if args.run:
        from gee_mh.config import init_earth_engine

        project = load_config()["ee_project"]
        init_earth_engine(project)
        report(run(project))
    elif args.report:
        report()
