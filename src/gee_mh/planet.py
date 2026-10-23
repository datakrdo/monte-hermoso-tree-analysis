"""Acceso a Planet (PlanetScope 3 m, RapidEye 5 m): búsqueda, pedido recortado a
la zona de estudio y compuesto de verano por año. Los rasters quedan en
data/planet/ (ignorado por git): el repo solo lleva derivados.

Compuesto del año Y: mediana de los píxeles despejados de las mejores escenas
del 15-ene a 15-mar de Y, la misma ventana (±30 días de mediados de febrero)
que los compuestos de Sentinel-2 en timeseries.py.

© Planet Labs PBC en todo lo derivado de estas imágenes.
"""

import argparse
import base64
import json
import os
import re
import time
import urllib.request
from pathlib import Path

import numpy
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import Resampling, reproject
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

from gee_mh.config import PROJECT_ROOT, load_config

PLANET_DIR = PROJECT_ROOT / "data" / "planet"
GRID_CRS = "EPSG:32720"  # UTM 20S: Monte Hermoso
RESOLUTION_M = {"PSScene": 3.0, "REOrthoTile": 5.0}
# bundle, índice (0-based) de la banda NIR en el raster de reflectancia, tipo de máscara
SENSORS = {
    "PSScene": {"bundle": "analytic_sr_udm2", "nir": 3, "short": "ps"},
    "REOrthoTile": {"bundle": "analytic_sr", "nir": 4, "short": "re"},
}
SCENES_PER_YEAR = 3
GEE_COLLECTION = "planet_ps_summer"  # projects/<ee_project>/assets/planet_ps_summer
MAX_CLOUD = 0.20  # de la escena entera: la zona es chica y el UDM2 enmascara píxel a píxel
MIN_COVER = 0.99  # las escenas elegidas deben cubrir juntas la zona
DATA_API = "https://api.planet.com/data/v1"
ORDERS_API = "https://api.planet.com/compute/ops/orders/v2"
QUOTA_API = "https://api.planet.com/auth/v1/experimental/public/my/subscriptions"
# El plan Education and Research Basic cobra mínimo 100 km² por escena recortada, aunque el recorte sea menor
# (https://support.planet.com/hc/en-us/articles/4425683189533). La cuota es de la cuenta, no del repo.
MIN_KM2_SCENE = float(os.environ.get("PL_MIN_KM2_SCENE", 100))
MAX_KM2_RUN = float(os.environ.get("PL_MAX_KM2_RUN", 300))  # tope por corrida, sin flag para saltearlo
_spent = 0.0  # km² ya pedidos en esta corrida


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value and (PROJECT_ROOT / ".env").exists():
        for line in (PROJECT_ROOT / ".env").read_text().splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    if not value:
        raise RuntimeError(f"Falta {name} (variable de entorno o .env)")
    return value


def api_key() -> str:
    return _env("PL_API_KEY")


def _request(url: str, body: dict = None, auth: bool = True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{api_key()}:".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers)) as response:
        return json.load(response)


def zone():
    """Geometría de la zona de estudio (WGS84), levemente simplificada para el pedido."""
    path = PROJECT_ROOT / load_config()["aoi"]["path"]
    data = json.load(open(path))
    geometry = data["features"][0]["geometry"] if "features" in data else data
    return shape(geometry).simplify(5e-5)


def zone_area_km2() -> float:
    import pyproj

    project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True).transform
    return shp_transform(project, zone()).area / 1e6


def grid(item_type: str):
    """(transform, ancho, alto) de la grilla común en UTM 20S para el sensor."""
    import pyproj

    res = RESOLUTION_M[item_type]
    project = pyproj.Transformer.from_crs("EPSG:4326", GRID_CRS, always_xy=True).transform
    minx, miny, maxx, maxy = shp_transform(project, zone()).bounds
    minx, miny = numpy.floor(minx / res) * res, numpy.floor(miny / res) * res
    maxy = numpy.ceil(maxy / res) * res
    return from_origin(minx, maxy, res, res), int(numpy.ceil((maxx - minx) / res)), int(numpy.ceil((maxy - miny) / res))


def search(item_type: str, year: int) -> list:
    """Escenas del verano del año que tocan la zona, de menos a más nubes."""
    z = zone()
    body = {
        "item_types": [item_type],
        "filter": {"type": "AndFilter", "config": [
            {"type": "GeometryFilter", "field_name": "geometry", "config": mapping(z)},
            {"type": "DateRangeFilter", "field_name": "acquired",
             "config": {"gte": f"{year}-01-15T00:00:00Z", "lte": f"{year}-03-15T23:59:59Z"}},
            {"type": "RangeFilter", "field_name": "cloud_cover", "config": {"lte": MAX_CLOUD}},
        ]},
    }
    result = _request(f"{DATA_API}/quick-search?_page_size=250", body)
    features = result["features"]
    while result["_links"].get("_next"):
        result = _request(result["_links"]["_next"])
        features += result["features"]
    features = [f for f in features if shape(f["geometry"]).intersects(z)]
    return sorted(features, key=lambda f: f["properties"]["cloud_cover"])


def select(scenes: list, n: int = SCENES_PER_YEAR) -> list:
    """
    Primero las escenas con menos nubes que sumen cobertura de la zona (RapidEye cubre solo
    parte por baldosa), hasta MIN_COVER; después las mejores que queden hasta n.
    """
    z, covered, chosen = zone(), None, []
    for scene in scenes:
        footprint = shape(scene["geometry"]).intersection(z)
        gain = footprint if covered is None else footprint.difference(covered)
        if gain.area / z.area > 0.01:
            chosen.append(scene)
            covered = footprint if covered is None else covered.union(footprint)
        if covered is not None and covered.area / z.area >= MIN_COVER:
            break
    return chosen + [s for s in scenes if s not in chosen][: max(0, n - len(chosen))]


def quota() -> float:
    """Km² que quedan este mes. Falla cerrado: si no se puede leer, aborta y no se ordena."""
    try:
        sub = _request(QUOTA_API)
        sub = sub[0] if isinstance(sub, list) else sub
        return float(sub["quota_sqkm"]) - float(sub["quota_used"])
    except Exception as error:
        raise RuntimeError(f"No se pudo leer la cuota de Planet; no se ordena nada ({error!r})") from error


def order_cost_km2(n_scenes: int, area_km2: float = None) -> float:
    """Costo de un pedido: Planet cobra al menos MIN_KM2_SCENE por escena."""
    return n_scenes * max(MIN_KM2_SCENE, zone_area_km2() if area_km2 is None else area_km2)


def check_budget(cost: float, left: float, spent: float = 0.0, label: str = ""):
    """Aborta (con el desglose) si el pedido supera la cuota restante o el tope de la corrida."""
    if cost > left:
        raise RuntimeError(f"Cupo insuficiente {label}: {cost:.0f} km² necesarios, quedan {left:.0f} km² este mes")
    if spent + cost > MAX_KM2_RUN:
        raise RuntimeError(f"Tope por corrida superado {label}: {cost:.0f} km² de este pedido + {spent:.0f} km² ya pedidos "
                           f"> {MAX_KM2_RUN:.0f} km² (PL_MAX_KM2_RUN)")


def gee_delivery(project: str) -> dict:
    """Bloque `delivery` de la Orders API con la cuenta de servicio propia (PL_GEE_SA_KEY = ruta al JSON)."""
    key = Path(_env("PL_GEE_SA_KEY")).expanduser().read_bytes()
    return {"google_earth_engine": {"project": project, "collection": GEE_COLLECTION,
                                    "credentials": base64.b64encode(key).decode()}}


def collection_ids(project: str) -> set:
    """Ids de escenas ya entregadas a la colección de GEE; aborta si la colección no existe."""
    import ee

    parent = f"projects/{project}/assets/{GEE_COLLECTION}"
    try:
        ee.data.getAsset(parent)
    except ee.EEException as error:
        raise RuntimeError(f"No existe {parent}: crearla con `earthengine create collection {parent}`") from error
    return {Path(a["name"]).name for a in ee.data.listAssets({"parent": parent}).get("assets", [])}


def order(item_type: str, year: int, ids: list, folder: Path = None, delivery: dict = None):
    """
    Pide las escenas recortadas a la zona y espera. Con `delivery` (GEE) las
    escenas se armonizan con Sentinel-2 y quedan en la colección; sin él, se
    descargan a folder.
    """
    tools = [{"clip": {"aoi": mapping(zone())}}]
    body = {
        "name": f"mh-{SENSORS[item_type]['short']}-{year}",
        "products": [{"item_ids": ids, "item_type": item_type, "product_bundle": SENSORS[item_type]["bundle"]}],
        "tools": tools + ([{"harmonize": {"target_sensor": "Sentinel-2"}}] if delivery else []),
    }
    if delivery:
        body["delivery"] = delivery
    created = _request(ORDERS_API, body)
    url = f"{ORDERS_API}/{created['id']}"
    while True:
        status = _request(url)
        if status["state"] in ("success", "failed", "partial", "cancelled"):
            break
        time.sleep(15)
    if status["state"] != "success":
        raise RuntimeError(f"Pedido {created['id']} terminó en estado {status['state']}")
    if delivery:
        return
    folder.mkdir(parents=True, exist_ok=True)
    for result in status["_links"]["results"]:
        name = Path(result["name"]).name
        if name.endswith(".tif"):
            urllib.request.urlretrieve(result["location"], folder / name)


def build_composite(item_type: str, year: int) -> Path:
    """Mediana de píxeles despejados de las escenas descargadas, en la grilla común."""
    folder = PLANET_DIR / item_type / str(year)
    transform, width, height = grid(item_type)
    nir_index = SENSORS[item_type]["nir"]
    stacks, clear = [], []
    for sr_path in sorted(folder.glob("*_SR*clip.tif")):
        udm_path = next(folder.glob(re.split(r"_Analytic", sr_path.name)[0] + "*udm*clip.tif"), None)  # ponytail: sin udm, todo cuenta como despejado
        bands = numpy.full((4, height, width), numpy.nan, dtype=numpy.float32)
        mask = numpy.full((1, height, width), 255, dtype=numpy.uint8)  # 255 = fuera de la huella del udm
        with rasterio.open(sr_path) as src:
            indexes = (1, 2, 3, nir_index + 1)  # B, G, R, NIR
            for out, index in zip(bands, indexes):
                reproject(rasterio.band(src, index), out, dst_transform=transform, dst_crs=GRID_CRS,
                          resampling=Resampling.bilinear, dst_nodata=numpy.nan)
        if udm_path:
            with rasterio.open(udm_path) as src:
                reproject(rasterio.band(src, 1), mask, dst_transform=transform, dst_crs=GRID_CRS,
                          resampling=Resampling.nearest, dst_nodata=255)
            good = (mask[0] == 1) if item_type == "PSScene" else (mask[0] == 0)
        else:
            good = numpy.ones((height, width), dtype=bool)
        bands[:, ~good] = numpy.nan
        stacks.append(bands / 1e4)  # reflectancia
    if not stacks:
        raise RuntimeError(f"Sin escenas descargadas en {folder}")
    composite = numpy.nanmedian(numpy.stack(stacks), axis=0).astype(numpy.float32)
    count = numpy.sum(~numpy.isnan(numpy.stack(stacks)[:, 0]), axis=0).astype(numpy.float32)
    out_path = PLANET_DIR / item_type / f"{year}_composite.tif"
    with rasterio.open(out_path, "w", driver="GTiff", height=height, width=width, count=5, dtype="float32",
                       crs=GRID_CRS, transform=transform, nodata=numpy.nan) as dst:
        dst.write(numpy.concatenate([composite, count[None]]))
    return out_path


def read_composite(item_type: str, year: int):
    """(dict blue/green/red/nir/count, transform) del compuesto, o None si no existe."""
    path = PLANET_DIR / item_type / f"{year}_composite.tif"
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        data = src.read()
        transform = src.transform
    return dict(zip(("blue", "green", "red", "nir", "count"), data)), transform


def download(item_type: str, year: int, dry_run: bool = False):
    """PlanetScope va a la colección de GEE; RapidEye (sin entrega a GEE) se baja y compone en local."""
    to_gee = item_type == "PSScene"
    if not to_gee and (PLANET_DIR / item_type / f"{year}_composite.tif").exists():
        return
    scenes = select(search(item_type, year))
    if to_gee:
        project = load_config()["ee_project"]
        from gee_mh.config import init_earth_engine

        init_earth_engine(project)
        done = collection_ids(project)
        scenes = [s for s in scenes if not any(s["id"] in name for name in done)]
    if not scenes:
        print(f"{item_type} {year}: nada para pedir (sin escenas despejadas que cubran la zona, o ya entregadas)")
        return
    global _spent
    needed, left = order_cost_km2(len(scenes)), quota()
    label = f"{item_type} {year}: {len(scenes)} escenas x {max(MIN_KM2_SCENE, zone_area_km2()):.0f} km²"
    print(f"{label} = {needed:.0f} km² estimados; cuota restante {left:.0f} km²; ya pedidos en esta corrida {_spent:.0f} km²")
    check_budget(needed, left, _spent, label)
    if dry_run:
        print("--dry-run: no se ordena")
        return
    _spent += needed
    ids = [s["id"] for s in scenes]
    if to_gee:
        order(item_type, year, ids, delivery=gee_delivery(project))
        print(f"{item_type} {year}: {len(ids)} escenas entregadas a projects/{project}/assets/{GEE_COLLECTION}")
    else:
        order(item_type, year, ids, PLANET_DIR / item_type / str(year))
        print(f"{item_type} {year}: {build_composite(item_type, year)} ({len(ids)} escenas)")


def _selftest():
    z = zone()
    assert 40 < zone_area_km2() < 50, zone_area_km2()
    transform, width, height = grid("PSScene")
    assert transform.a == 3.0 and width > 1000 and height > 1000
    import pyproj

    miny = shp_transform(pyproj.Transformer.from_crs("EPSG:4326", GRID_CRS, always_xy=True).transform, z).bounds[1]
    assert transform.f - height * 3.0 <= miny, "la grilla debe cubrir el borde sur"
    assert shape(mapping(z)).equals(z)
    assert SENSORS["PSScene"]["nir"] == 3 and SENSORS["REOrthoTile"]["nir"] == 4
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json") as key:
        key.write('{"client_email": "x"}')
        key.flush()
        os.environ["PL_GEE_SA_KEY"] = key.name
        block = gee_delivery("p")["google_earth_engine"]
        assert block["collection"] == GEE_COLLECTION and base64.b64decode(block["credentials"]) == b'{"client_email": "x"}'
    del os.environ["PL_GEE_SA_KEY"]
    area = 45.0  # zona de ~45 km²: se cobran 100 km² por escena, no 45
    assert order_cost_km2(3, area) == 300 and order_cost_km2(3, area) != 3 * area
    check_budget(300, left=3000, spent=0)
    for args in ({"cost": 400, "left": 3000}, {"cost": 100, "left": 3000, "spent": 250}, {"cost": 100, "left": 50}):
        try:
            check_budget(**args)
        except RuntimeError:
            continue
        raise AssertionError(f"debía abortar: {args}")
    global _request
    real, _request = _request, lambda *a, **k: (_ for _ in ()).throw(OSError("sin red"))
    try:
        quota()
        raise AssertionError("quota() debe fallar cerrado")
    except RuntimeError:
        pass
    finally:
        _request = real
    print("planet selftest OK")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--sensor", choices=["ps", "re"], default="ps")
    parser.add_argument("--years", type=int, nargs="+")
    parser.add_argument("--dry-run", action="store_true", help="todo salvo el POST a la Orders API")
    args = parser.parse_args()
    if args.selftest:
        _selftest()
    if args.download:
        item_type = "PSScene" if args.sensor == "ps" else "REOrthoTile"
        default_years = range(2017, 2027) if args.sensor == "ps" else range(2011, 2020)
        for y in args.years or default_years:
            download(item_type, y, args.dry_run)
