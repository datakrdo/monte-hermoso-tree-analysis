"""Copa de árboles por manzana. Las manzanas se arman con la red de calles de
OpenStreetMap (© colaboradores de OpenStreetMap, ODbL): polígonos cerrados por
calles, recortados a la zona de estudio.
"""

import argparse
import json
import urllib.parse
import urllib.request

import geopandas
import numpy
import pandas
from shapely.geometry import LineString, box
from shapely.ops import polygonize, unary_union

from gee_mh.config import PROJECT_ROOT

BLOCKS_PATH = PROJECT_ROOT / "data" / "reference" / "manzanas_osm.geojson"
OVERPASS = "https://overpass-api.de/api/interpreter"
AREA_CRS = "EPSG:32720"  # UTM 20S
MIN_BLOCK_M2 = 500  # 0,05 ha
MAX_BLOCK_M2 = 100_000  # 10 ha: por encima es campo o médano, no manzana
PLANET_COLLECTION = "planet_ps_summer"
# Bandas de la colección entregada (PSScene analytic_sr_udm2, 4 bandas): B1..B4 = B, G, R, NIR y la banda
# udm2 "clear". ponytail: sin verificar hasta la primera entrega; `--bands` imprime los nombres reales.
RED, NIR, CLEAR = "B3", "B4", "Q1"
EXPORT_PATH = PROJECT_ROOT / "data" / "exports" / "canopy_by_block.csv"
THRESHOLDS = numpy.round(numpy.arange(0.20, 0.71, 0.05), 2)
NOT_STREETS = "footway|path|cycleway|steps|track|bridleway|pedestrian|corridor"


def fetch_streets(bounds) -> list:
    """LineStrings (lon, lat) de las calles de OSM dentro de bounds = (minx, miny, maxx, maxy)."""
    minx, miny, maxx, maxy = bounds
    query = f'[out:json][timeout:90];way["highway"]["highway"!~"{NOT_STREETS}"]({miny},{minx},{maxy},{maxx});out geom;'
    data = urllib.parse.urlencode({"data": query}).encode()
    with urllib.request.urlopen(urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": "monte-hermoso-tree-analysis"}), timeout=120) as response:
        elements = json.load(response)["elements"]
    return [LineString([(p["lon"], p["lat"]) for p in e["geometry"]]) for e in elements if len(e.get("geometry", [])) > 1]


def blocks_from_streets(streets: list, zone) -> geopandas.GeoDataFrame:
    """Manzanas = caras cerradas por las calles, dentro de la zona y con tamaño de manzana."""
    faces = list(polygonize(unary_union(streets)))
    blocks = geopandas.GeoDataFrame(geometry=faces, crs="EPSG:4326").clip(zone)
    blocks = blocks.explode(index_parts=False)[lambda b: b.geom_type == "Polygon"]  # el recorte puede partir una manzana
    blocks["area_m2"] = blocks.to_crs(AREA_CRS).area.round(0)
    blocks = blocks[blocks["area_m2"].between(MIN_BLOCK_M2, MAX_BLOCK_M2)].reset_index(drop=True)
    blocks["block_id"] = blocks.index
    return blocks


def build_blocks() -> geopandas.GeoDataFrame:
    from gee_mh.planet import zone

    z = zone()
    blocks = blocks_from_streets(fetch_streets(z.bounds), z)
    BLOCKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    blocks.to_file(BLOCKS_PATH, driver="GeoJSON")
    return blocks


def f1_by_threshold(fractions: numpy.ndarray, reference: numpy.ndarray) -> numpy.ndarray:
    """F1 de (fraction >= 0.5) contra reference (bool) para cada columna de fractions (muestras x umbrales)."""
    predicted = fractions >= 0.5
    tp = (predicted & reference[:, None]).sum(0)
    fp = (predicted & ~reference[:, None]).sum(0)
    fn = (~predicted & reference[:, None]).sum(0)
    return numpy.where(tp > 0, 2 * tp / numpy.maximum(2 * tp + fp + fn, 1), 0.0)


def _ndvi_composite(year: int, project: str):
    """Mediana del NDVI de verano de los píxeles despejados de la colección de Planet."""
    import ee

    def ndvi(image):
        return image.updateMask(image.select(CLEAR).eq(1)).normalizedDifference([NIR, RED]).rename("ndvi")

    collection = ee.ImageCollection(f"projects/{project}/assets/{PLANET_COLLECTION}")
    return collection.filterDate(f"{year}-01-15", f"{year}-03-16").map(ndvi).median()


def calibrate_threshold(year: int, project: str, samples: int = 20000) -> tuple:
    """
    Umbral de NDVI de copa a 3 m: el que mejor reproduce la clase 'trees' de Dynamic World a 10 m
    (un píxel de 10 m es árbol si >= 50 % de sus píxeles de 3 m superan el umbral). Return (umbral, F1).
    """
    import ee
    from gee_mh.planet import zone

    region = ee.Geometry(json.loads(geopandas.GeoSeries([zone()]).to_json())["features"][0]["geometry"])
    ndvi = _ndvi_composite(year, project)
    bands = [ndvi.gt(float(t)).rename(f"t{i}") for i, t in enumerate(THRESHOLDS)]
    fractions = ee.Image.cat(bands).reduceResolution(ee.Reducer.mean(), maxPixels=256).reproject(AREA_CRS, None, 10)
    dw = (ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(region).filterDate(f"{year}-01-15", f"{year}-03-16")
          .select("label").mode().eq(1).rename("dw"))  # 1 = trees
    rows = fractions.addBands(dw).sample(region, scale=10, numPixels=samples, seed=0, dropNulls=True).getInfo()["features"]
    table = pandas.DataFrame([r["properties"] for r in rows])
    f1 = f1_by_threshold(table[[f"t{i}" for i in range(len(THRESHOLDS))]].to_numpy(), table["dw"].to_numpy() == 1)
    best = int(f1.argmax())
    print(f"umbral de copa NDVI > {THRESHOLDS[best]} (F1 {f1[best]:.2f} contra Dynamic World, {len(table)} píxeles de 10 m, {year})")
    return float(THRESHOLDS[best]), float(f1[best])


def canopy_by_block(years, threshold: float, project: str) -> pandas.DataFrame:
    """% de copa (NDVI > threshold) y % de la manzana con dato, por manzana y año."""
    import ee

    blocks = geopandas.read_file(BLOCKS_PATH)
    features = ee.FeatureCollection([ee.Feature(ee.Geometry(g.__geo_interface__), {"block_id": int(i)})
                                     for i, g in zip(blocks["block_id"], blocks.geometry)])
    rows = []
    for year in years:
        ndvi = _ndvi_composite(year, project)
        image = ndvi.gt(threshold).rename("canopy").addBands(ndvi.mask().rename("valid"))
        result = image.reduceRegions(features, ee.Reducer.mean(), scale=3, crs=AREA_CRS).getInfo()["features"]
        rows += [{"block_id": f["properties"]["block_id"], "year": year, "sensor": "PSScene",
                  "canopy_pct": round(100 * f["properties"]["canopy"], 1) if f["properties"].get("canopy") is not None else None,
                  "valid_pct": round(100 * f["properties"]["valid"], 1)} for f in result]
    return pandas.DataFrame(rows)


def local_canopy_by_block(item_type: str, years, threshold: float) -> pandas.DataFrame:
    """% de copa por manzana desde los compuestos locales de planet.py (RapidEye); años sin compuesto se saltean."""
    from rasterio.features import rasterize

    from gee_mh.planet import GRID_CRS, read_composite

    blocks = geopandas.read_file(BLOCKS_PATH).to_crs(GRID_CRS)
    rows = []
    for year in years:
        composite = read_composite(item_type, year)
        if composite is None:
            continue
        bands, transform = composite
        ids = rasterize(zip(blocks.geometry, blocks["block_id"] + 1), out_shape=bands["nir"].shape, transform=transform, dtype="int32")
        ndvi = (bands["nir"] - bands["red"]) / (bands["nir"] + bands["red"])
        rows += canopy_rows(ids, ndvi, threshold, len(blocks), year, item_type)
    return pandas.DataFrame(rows)


def canopy_rows(ids: numpy.ndarray, ndvi: numpy.ndarray, threshold: float, n_blocks: int, year: int, sensor: str) -> list:
    """Por manzana (ids 1..n, 0 = fuera): % de copa entre los píxeles con dato y % de píxeles con dato."""
    inside = ids > 0
    valid = inside & ~numpy.isnan(ndvi)
    size = numpy.bincount(ids[inside], minlength=n_blocks + 1)[1:]
    good = numpy.bincount(ids[valid], minlength=n_blocks + 1)[1:]
    trees = numpy.bincount(ids[valid & (ndvi > threshold)], minlength=n_blocks + 1)[1:]
    return [{"block_id": i, "year": year, "sensor": sensor,
             "canopy_pct": round(100 * trees[i] / good[i], 1) if good[i] else None,
             "valid_pct": round(100 * good[i] / size[i], 1) if size[i] else 0.0} for i in range(n_blocks)]


def bridge(table: pandas.DataFrame) -> pandas.DataFrame:
    """
    Pasa la copa de RapidEye a la escala de PlanetScope con un ajuste lineal sobre los años en que
    hay ambos sensores, por manzana (como la corrección OLI de Landsat en longterm.py). Agrega
    `canopy_raw` (sin ajustar). Sin años de solapamiento no se ajusta nada y RapidEye se descarta.
    """
    table = table.assign(canopy_raw=table["canopy_pct"])
    ps = table[table["sensor"] == "PSScene"].set_index(["block_id", "year"])["canopy_pct"]
    re = table[table["sensor"] == "REOrthoTile"].set_index(["block_id", "year"])["canopy_pct"]
    both = pandas.concat([re.rename("re"), ps.rename("ps")], axis=1, join="inner").dropna()
    if len(both) < 30:
        print("sin solapamiento RapidEye / PlanetScope: RapidEye queda fuera de la serie")
        return table[table["sensor"] == "PSScene"]
    slope, intercept = numpy.polyfit(both["re"], both["ps"], 1)
    print(f"ajuste RapidEye -> PlanetScope: ps = {intercept:.1f} + {slope:.2f} * re (r = {both.corr().iloc[0, 1]:.2f}, {len(both)} manzanas-año)")
    is_re = table["sensor"] == "REOrthoTile"
    table.loc[is_re, "canopy_pct"] = (intercept + slope * table.loc[is_re, "canopy_raw"]).clip(0, 100).round(1)
    return table


def export_web(table: pandas.DataFrame, path=PROJECT_ROOT / "docs" / "data" / "manzanas.geojson"):
    """Manzanas con una propiedad c<año> = % de copa; solo derivados, sin píxeles de Planet."""
    wide = table.pivot_table(index="block_id", columns="year", values="canopy_pct")
    wide.columns = [f"c{y}" for y in wide.columns]
    blocks = geopandas.read_file(BLOCKS_PATH)[["block_id", "geometry"]].merge(wide, left_on="block_id", right_index=True, how="left")
    blocks["geometry"] = blocks.geometry.make_valid().set_precision(1e-5)
    blocks = blocks.sort_values("block_id")
    blocks.to_file(path, driver="GeoJSON")


def _selftest():
    # grilla de 3 x 3 calles cada 100 m -> 4 manzanas de 100 x 100 m (10.000 m²)
    step = 0.001  # ~100 m
    lat0, lon0 = -38.98, -61.30
    streets = [LineString([(lon0 + i * step, lat0), (lon0 + i * step, lat0 + 2 * step)]) for i in range(3)]
    streets += [LineString([(lon0, lat0 + j * step), (lon0 + 2 * step, lat0 + j * step)]) for j in range(3)]
    blocks = blocks_from_streets(streets, box(lon0 - step, lat0 - step, lon0 + 3 * step, lat0 + 3 * step))
    assert len(blocks) == 4, len(blocks)
    assert all(7000 < a < 10000 for a in blocks["area_m2"]), blocks["area_m2"].tolist()
    # el recorte a la zona descarta lo que queda fuera
    half = blocks_from_streets(streets, box(lon0 - step, lat0 - step, lon0 + step * 1.0, lat0 + 3 * step))
    assert len(half) == 2, len(half)
    # F1: umbral bajo marca todo como árbol (fracción 1), umbral alto ninguno; solo el medio acierta
    fractions = numpy.array([[1, 1, 0], [1, 1, 0], [1, 0, 0], [1, 0, 0]], dtype=float)
    reference = numpy.array([True, True, False, False])
    f1 = f1_by_threshold(fractions, reference)
    assert f1.argmax() == 1 and abs(f1[1] - 1.0) < 1e-9 and f1[2] == 0.0, f1
    # zonal: manzana 1 = 4 píxeles, 3 con árboles; manzana 2 = 2 píxeles, 1 sin dato
    ids = numpy.array([[1, 1, 2], [1, 1, 2], [0, 0, 0]])
    ndvi = numpy.array([[0.6, 0.7, 0.1], [0.6, 0.1, numpy.nan], [0.9, 0.9, 0.9]])
    first, second = canopy_rows(ids, ndvi, 0.4, 2, 2020, "PSScene")
    assert first["canopy_pct"] == 75.0 and first["valid_pct"] == 100.0, first
    assert second["canopy_pct"] == 0.0 and second["valid_pct"] == 50.0, second
    # puente: RapidEye lee el doble que PlanetScope en 3 años en común -> el ajuste lo baja a la mitad
    rng = numpy.random.default_rng(0)
    truth = rng.uniform(5, 40, 40).round(1)
    rows = [{"block_id": b, "year": y, "sensor": "PSScene", "canopy_pct": t} for y in (2017, 2018) for b, t in enumerate(truth)]
    rows += [{"block_id": b, "year": y, "sensor": "REOrthoTile", "canopy_pct": 2 * t} for y in (2017, 2018, 2012) for b, t in enumerate(truth)]
    joined = bridge(pandas.DataFrame(rows))
    old = joined[(joined["sensor"] == "REOrthoTile") & (joined["year"] == 2012)]
    assert numpy.allclose(old["canopy_pct"].to_numpy(), truth, atol=0.2) and (old["canopy_raw"] == 2 * truth).all()
    assert len(bridge(pandas.DataFrame(rows[:10] + rows[80:90]))) == 10  # sin solapamiento: solo PlanetScope
    print("canopy selftest OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--blocks", action="store_true", help="armar manzanas_osm.geojson desde Overpass")
    parser.add_argument("--bands", action="store_true", help="imprimir las bandas reales de la colección de Planet")
    parser.add_argument("--canopy", action="store_true", help="calibrar el umbral y calcular la copa por manzana")
    parser.add_argument("--years", type=int, nargs="+", default=list(range(2017, 2027)))
    args = parser.parse_args()
    if args.selftest:
        _selftest()
    if args.blocks:
        print(f"{len(build_blocks())} manzanas en {BLOCKS_PATH}")
    if args.bands or args.canopy:
        from gee_mh.config import init_earth_engine

        project = load_config()["ee_project"]
        init_earth_engine(project)
    if args.bands:
        import ee

        print(ee.ImageCollection(f"projects/{project}/assets/{PLANET_COLLECTION}").first().bandNames().getInfo())
    if args.canopy:
        threshold, _ = calibrate_threshold(args.years[-1], project)
        table = canopy_by_block(args.years, threshold, project)
        table = pandas.concat([table, local_canopy_by_block("REOrthoTile", range(2010, 2020), threshold)], ignore_index=True)
        table = bridge(table).sort_values(["block_id", "year"])
        table.to_csv(EXPORT_PATH, index=False)
        export_web(table)
        print(f"{len(table)} filas en {EXPORT_PATH}")
