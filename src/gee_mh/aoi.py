"""Límite del partido de Monte Hermoso y zona de estudio urbana.

Fuente del partido: datos.gob.ar / georef (IGN), capa nacional de
departamentos/partidos en WGS84:
https://infra.datos.gob.ar/georef/departamentos.geojson
Partido id georef: 06553 (Monte Hermoso, Buenos Aires).

La zona de estudio (casco urbano de Monte Hermoso, alrededores y Balneario
Sauce Grande) sale de la huella urbana actual más un margen, recortada al
partido (`build_urban_zone`).

Uso:
    PYTHONPATH=src python3 -m gee_mh.aoi          # límite del partido
    PYTHONPATH=src python3 -m gee_mh.aoi --zona   # zona urbana (Earth Engine)
"""

import argparse
import datetime
import json
import urllib.request

import geopandas as gpd
from shapely.geometry import Polygon, box, shape
from shapely import segmentize
from shapely.ops import unary_union

from gee_mh.config import PROJECT_ROOT, load_config

DEPARTAMENTOS_URL = "https://infra.datos.gob.ar/georef/departamentos.geojson"
MONTE_HERMOSO_ID = "06553"

ZONE_PATH = PROJECT_ROOT / "data" / "aoi" / "zona_urbana.geojson"
METRIC_CRS = "EPSG:32720"  # UTM 20 sur: metros para el margen y las áreas de la zona
MAPBIOMAS_URBAN_ASSET = "projects/mapbiomas-public/assets/argentina/lulc/collection3/mapbiomas_argentina_collection3_coverage_v1"
MAPBIOMAS_URBAN_BAND, MAPBIOMAS_URBAN_CODE = "classification_2025", 24
DYNAMIC_WORLD_BUILT_MIN = 0.35  # probabilidad media de 'built' (2024-01 a 2026-06) para contar como construido
CLOSING_M = 150  # el cierre une manzanas vecinas en una sola huella
MIN_CLUSTER_HA = 30  # manchas urbanas menores (caseríos, galpones) no abren una zona propia
MIN_HOLE_HA = 5  # los huecos menores que esto dentro de la zona se rellenan
SEA_WATER_MIN = 0.5  # probabilidad media de 'water' de Dynamic World (2024-2026): MapBiomas no mapea el mar
# Los Bosques (El Americano): parcela 1050c del plano de zonificación municipal, trazada del PDF y georreferenciada
# contra la laguna (error estimado de 250 m, por eso el buffer); el predio de 554 ha no tiene límites publicados.
PARCELS_PATH = PROJECT_ROOT / "data" / "reference" / "desarrollos_poligonos.geojson"
LOS_BOSQUES = ("El Americano - barrio Los Bosques", 250)  # (nombre en PARCELS_PATH, buffer en metros)
SOUTH_EXTENSION_DEG = 0.03  # el límite sur del partido (Georef) es una recta que corta tierra en Balneario Sauce Grande
MIN_SEA_HA = 300  # las masas de agua mayores que esto dentro de la zona se tratan como mar y se descuentan


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


def zone_from_clusters(clusters, partido, margin_m: float, sea=None, parcels=()) -> Polygon:
    """
    clusters: polígonos (WGS84) de las manchas urbanas; partido: polígono del
    partido (el de Georef llega varios cientos de metros mar adentro); sea:
    polígono del mar a descontar, si se da; parcels: [(polígono lon/lat, buffer en m)] predios que se suman
    con su propio buffer (sin el margen de las manchas). Return la zona: manchas + margen_m,
    simplificada (20 m), sin huecos menores que MIN_HOLE_HA, sin el mar y
    recortada al partido (el borde del partido queda exacto: se recorta después
    de simplificar).
    """
    # el partido de Georef tiene pocos vértices: sin densificarlo, reproyectar curva sus lados rectos (unos 45 m)
    metric = gpd.GeoSeries([unary_union(clusters), segmentize(partido, 0.001)], crs="EPSG:4326").to_crs(METRIC_CRS)
    zone = metric.iloc[0].buffer(margin_m)
    for polygon, buffer_m in parcels:
        zone = zone.union(gpd.GeoSeries([polygon], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0].buffer(buffer_m))
    zone = zone.simplify(20)
    parts = [zone] if zone.geom_type == "Polygon" else list(zone.geoms)
    parts = [Polygon(p.exterior, [h for h in p.interiors if Polygon(h).area >= MIN_HOLE_HA * 1e4]) for p in parts]
    zone = unary_union(parts).intersection(metric.iloc[1])
    if sea is not None:
        zone = zone.difference(gpd.GeoSeries([sea], crs="EPSG:4326").to_crs(METRIC_CRS).iloc[0].simplify(15))
    return gpd.GeoSeries([zone], crs=METRIC_CRS).to_crs("EPSG:4326").iloc[0]


def build_urban_zone(margin_m: float = 1000, out_path=None) -> gpd.GeoDataFrame:
    """
    Huella urbana actual = clase urbana de MapBiomas Argentina 2025 o 'built' de
    Dynamic World (2024-2026), cerrada CLOSING_M para unir manzanas; se queda con
    las manchas de más de MIN_CLUSTER_HA, suma margin_m y recorta al partido.
    Guarda data/aoi/zona_urbana.geojson. Necesita Earth Engine.
    """
    import ee

    out_path = out_path or ZONE_PATH
    ee.Initialize(project=load_config()["ee_project"])
    partido = gpd.read_file(PROJECT_ROOT / load_config()["aoi"]["partido_path"]).geometry.iloc[0]
    named = gpd.read_file(PARCELS_PATH).set_index("name").geometry
    parcels = [(named[LOS_BOSQUES[0]], LOS_BOSQUES[1])]
    # se amplía hacia el sur: la huella urbana y la zona deben seguir la costa real, no esa recta (el mar se descuenta después)
    west, south, east, _ = partido.bounds
    partido = unary_union([partido, box(west, south - SOUTH_EXTENSION_DEG, east, south + 0.005)])
    region = ee.Geometry(partido.__geo_interface__)

    urban = ee.Image(MAPBIOMAS_URBAN_ASSET).select(MAPBIOMAS_URBAN_BAND).eq(MAPBIOMAS_URBAN_CODE)
    built = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(region) \
        .filterDate("2024-01-01", "2026-06-30").select("built").mean().gt(DYNAMIC_WORLD_BUILT_MIN)
    footprint = urban.Or(built).selfMask().focalMax(CLOSING_M, "circle", "meters").focalMin(CLOSING_M, "circle", "meters").selfMask()
    vectors = footprint.reduceToVectors(geometry=region, scale=30, maxPixels=1e9, geometryType="polygon", eightConnected=True)
    vectors = vectors.map(lambda f: f.set("ha", f.geometry().area(10).divide(1e4))).filter(ee.Filter.gt("ha", MIN_CLUSTER_HA))
    clusters = [shape(f["geometry"]) for f in vectors.getInfo()["features"]]
    if not clusters:
        raise ValueError("No se encontró ninguna mancha urbana")

    # el polígono del partido incluye mar: se descuenta el agua grande (Dynamic World) dentro de la zona
    minx, miny, maxx, maxy = zone_from_clusters(clusters, partido, margin_m, parcels=parcels).bounds
    water = ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1").filterBounds(region).filterDate("2024-01-01", "2026-06-30") \
        .select("water").mean().gt(SEA_WATER_MIN).selfMask()
    water_vectors = water.reduceToVectors(geometry=ee.Geometry.Rectangle([minx - 0.01, miny - 0.02, maxx + 0.01, maxy + 0.01]), scale=30,
                                          maxPixels=1e9, geometryType="polygon", eightConnected=True)
    water_vectors = water_vectors.map(lambda f: f.set("ha", f.geometry().area(10).divide(1e4))).filter(ee.Filter.gt("ha", MIN_SEA_HA))
    sea = unary_union([shape(f["geometry"]) for f in water_vectors.getInfo()["features"]])
    zone = zone_from_clusters(clusters, partido, margin_m, sea=sea, parcels=parcels)
    gdf = gpd.GeoDataFrame({
        "nombre": ["Zona urbana de Monte Hermoso y Balneario Sauce Grande"],
        "margen_m": [margin_m],
        "fuente": [f"MapBiomas Argentina C3 clase {MAPBIOMAS_URBAN_CODE} (2025) o Dynamic World built > {DYNAMIC_WORLD_BUILT_MIN} (2024-2026), "
                   f"cierre {CLOSING_M} m, manchas > {MIN_CLUSTER_HA} ha, recortada al partido (IGN/Georef) y sin el mar (Dynamic World water > {SEA_WATER_MIN}); más la parcela 1050c de Los Bosques (buffer de 250 m)"],
        "creada": [datetime.date.today().isoformat()],
        "area_km2": [gpd.GeoSeries([zone], crs="EPSG:4326").to_crs(METRIC_CRS).area.iloc[0] / 1e6],
    }, geometry=[zone], crs="EPSG:4326")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="GeoJSON")
    return gdf


def write_kml(zone, points, out_path) -> None:
    """KML con el contorno de la zona y puntos (nombre, lat, lon) para abrirlo en Google Earth."""
    def ring(polygon):
        return " ".join(f"{x},{y},0" for x, y in polygon.exterior.coords)

    polygons = [zone] if zone.geom_type == "Polygon" else list(zone.geoms)
    marks = [
        f"<Placemark><name>Zona de estudio propuesta</name><Style><LineStyle><color>ff0000ff</color><width>3</width></LineStyle>"
        f"<PolyStyle><fill>0</fill></PolyStyle></Style><Polygon><outerBoundaryIs><LinearRing><coordinates>{ring(p)}"
        f"</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>" for p in polygons]
    marks += [f"<Placemark><name>{name}</name><Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>" for name, lat, lon in points]
    out_path.write_text('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                        + "".join(marks) + "</Document></kml>", encoding="utf-8")


def selftest():
    # el mar queda al sur de lat -39.00 (fuera); con vértices cada 0,005° para que reproyectar no curve los bordes
    partido = segmentize(box(-61.45, -39.00, -61.10, -38.92), 0.005)
    town = box(-61.31, -38.99, -61.29, -38.98)     # 1,7 x 1,1 km
    small = zone_from_clusters([town], partido, 500)
    large = zone_from_clusters([town], partido, 1000)
    assert large.area > small.area > town.area
    assert partido.contains(large.buffer(-1e-9)), "la zona se recorta al partido"
    near_edge = box(-61.31, -39.00, -61.29, -38.99)  # pegada al borde sur del partido
    assert zone_from_clusters([near_edge], partido, 1000).difference(partido).area < 1e-9, "la zona no pasa el borde del partido"
    ring = unary_union([box(-61.34, -38.99, -61.26, -38.98).difference(box(-61.32, -38.987, -61.31, -38.983))])
    assert zone_from_clusters([ring], partido, 0).geom_type == "Polygon"
    sea = segmentize(box(-61.5, -39.1, -61.0, -38.995), 0.005)
    assert zone_from_clusters([near_edge], partido, 1000, sea=sea).intersection(sea).area < 1e-5, "la zona no incluye el mar"
    assert zone_from_clusters([near_edge], partido, 1000, sea=sea).area < zone_from_clusters([near_edge], partido, 1000).area
    parcel = box(-61.31, -38.96, -61.29, -38.94)
    with_parcel = zone_from_clusters([town], partido, 500, parcels=[(parcel, 250)])
    assert with_parcel.bounds[3] > -38.94 > zone_from_clusters([town], partido, 500).bounds[3], "el predio llega hasta el norte"
    assert with_parcel.contains(parcel), "la zona cubre el predio entero"
    two = zone_from_clusters([town, box(-61.22, -38.995, -61.20, -38.985)], partido, 500)
    assert two.geom_type == "MultiPolygon" and len(two.geoms) == 2, "dos manchas lejanas = dos zonas"
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        write_kml(two, [("punto", -38.99, -61.29)], Path(tmp) / "z.kml")
        kml = (Path(tmp) / "z.kml").read_text(encoding="utf-8")
        assert kml.count("<Polygon>") == 2 and "<name>punto</name>" in kml
    print("aoi selftest OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zona", action="store_true", help="armar la zona urbana (Earth Engine) en vez de bajar el partido")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--margen", type=float, default=1000, help="margen alrededor de la huella urbana, en metros")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        raise SystemExit
    if args.zona:
        zona = build_urban_zone(args.margen)
        print(f"Zona OK: {zona.iloc[0]['area_km2']:.1f} km2, margen {args.margen:.0f} m -> {ZONE_PATH}")
        raise SystemExit
    aoi = fetch_monte_hermoso_aoi()
    assert len(aoi) == 1
    assert aoi.iloc[0]["nombre"] == "Monte Hermoso"
    assert aoi.iloc[0]["area_km2"] > 0
    print(f"AOI OK: {aoi.iloc[0]['nombre']}, area={aoi.iloc[0]['area_km2']:.1f} km2")
