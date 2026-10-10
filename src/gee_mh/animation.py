"""Animación 1985-2026 para el usuario final: un cuadro por año en color
natural (Sentinel-2 a 10 m desde 2017, Landsat a 30 m antes), con la pérdida
y la ganancia persistentes de cobertura arbórea acumuladas desde 1986, y la
curva de cobertura arbórea de la zona de estudio debajo.

Dos paneles a la misma escala (Monte Hermoso y alrededores, Balneario Sauce
Grande). Entradas: data/exports/longterm_rates.csv y longterm_events.geojson
(ver longterm.py). Salidas: docs/monte_hermoso.gif y docs/frames/{mh,sg}/AAAA.jpg
(las mismas imágenes alimentan el mapa web docs/index.html).

Uso:
    PYTHONPATH=src python3 -m gee_mh.animation
"""

import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import ee
import geopandas
import matplotlib
import matplotlib.colors

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy
import pandas
from PIL import Image
from shapely.geometry import box
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from gee_mh.config import PROJECT_ROOT, load_config
from gee_mh.longterm import EXPORTS, ORDINANCE_YEAR, scenes_per_year
from gee_mh.pipeline import load_area
from gee_mh.preprocessing import CLOUD_SCORE_MIN, CLOUD_SCORE_PLUS, SOURCES, DataSource, LongLatBBox, Preprocessor, mask_landsat_clouds

DOCS = PROJECT_ROOT / "docs"
FRAME_WIDTH = 1200
SENTINEL_FIRST_YEAR = 2017  # 2016 es una sola escena (ver README)
CUT_LON = -61.25  # meridiano que separa los dos paneles (el tramo entre ambas localidades casi no tiene construcción)
MAP_WIDTH = 600  # ancho en píxeles de cada panel del GIF
PANEL_NAMES = {"mh": "Monte Hermoso y alrededores", "sg": "Balneario Sauce Grande"}
S2_WINDOW_DAYS = 30
LANDSAT_WINDOW_DAYS, WIDE_WINDOW_DAYS, WIDE_MAX_CLOUD = 30, 75, 60.0  # ventana normal y ancha de los cuadros Landsat
# Colores de la paleta de referencia del skill dataviz (superficie clara)
INK, INK_SECONDARY, SURFACE = "#0b0b0b", "#52514e", "#fcfcfb"
KIND_COLORS = {"loss": "#e34948", "gain": "#1baf7a"}
KIND_LABELS = {"loss": "Pérdida de árboles", "gain": "Ganancia de árboles"}
BUILT_COLOR = "#eb6834"  # vegetación → construido (eventos Sentinel-2 del mapa web)
LINE_COLOR = "#2a78d6"


def frame_size(area, width: int = FRAME_WIDTH) -> tuple:
    """Ancho y alto del cuadro con la proporción real del terreno (la grilla es lineal en grados)."""
    meters_x = (area.long_to - area.long_from) * 111_320.0 * numpy.cos(numpy.radians((area.lat_from + area.lat_to) / 2))
    meters_y = (area.lat_to - area.lat_from) * 111_320.0
    return width, round(width * meters_y / meters_x)


def natural_color(area, year: int, wide: bool) -> ee.Image:
    """Compuesto de verano en color natural, reflectancia 0-1: Sentinel-2 desde 2017, Landsat antes."""
    center = ee.Date(f"{year}-02-15")
    if year >= SENTINEL_FIRST_YEAR:
        collection = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").filterBounds(area.to_ee_box()) \
            .filterDate(center.advance(-S2_WINDOW_DAYS, "day"), center.advance(S2_WINDOW_DAYS, "day")) \
            .linkCollection(ee.ImageCollection(CLOUD_SCORE_PLUS), ["cs_cdf"]) \
            .map(lambda image: image.updateMask(image.select("cs_cdf").gte(CLOUD_SCORE_MIN)))
        return collection.median().select(["B4", "B3", "B2"]).multiply(1e-4)
    def landsat(year_, days, cloud):
        middle = ee.Date(f"{year_}-02-15")
        parts = []
        for source, bands in ((DataSource.LANDSAT5, ["SR_B3", "SR_B2", "SR_B1"]), (DataSource.LANDSAT7, ["SR_B3", "SR_B2", "SR_B1"]),
                              (DataSource.LANDSAT8, ["SR_B4", "SR_B3", "SR_B2"])):
            spec = SOURCES[source]
            part = ee.ImageCollection(spec["collections"][0])
            for extra in spec["collections"][1:]:
                part = part.merge(ee.ImageCollection(extra))
            parts.append(part.filterBounds(area.to_ee_box())
                         .filterDate(middle.advance(-days, "day"), middle.advance(days, "day"))
                         .filter(ee.Filter.lt(spec["cloud"], cloud)).map(mask_landsat_clouds)
                         .map(lambda image, bands=bands, spec=spec: image.select(bands, ["red", "green", "blue"])
                              .multiply(spec["scale"]).add(spec["offset"])))
        merged = parts[0].merge(parts[1]).merge(parts[2])
        empty = ee.Image.constant([0, 0, 0]).rename(["red", "green", "blue"]).updateMask(ee.Image.constant(0))
        return ee.Image(ee.Algorithms.If(merged.size().gt(0), merged.median(), empty))

    # píxeles sin dato (nubes enmascaradas): se rellenan con la ventana ancha y después con los años vecinos
    # (siempre con la máscara de nubes: nunca se muestran nubes ni huecos negros)
    image = landsat(year, LANDSAT_WINDOW_DAYS, 35.0).unmask(landsat(year, WIDE_WINDOW_DAYS, WIDE_MAX_CLOUD))
    for neighbor in (year - 1, year + 1, year - 2, year + 2):
        image = image.unmask(landsat(neighbor, WIDE_WINDOW_DAYS, WIDE_MAX_CLOUD))
    return image


def download_frame(area, year: int, wide: bool, path, width: int = FRAME_WIDTH) -> None:
    width, height = frame_size(area, width)
    image = natural_color(area, year, wide).visualize(min=0.02, max=0.22, gamma=1.3)
    url = image.getThumbURL({"region": area.to_ee_box(), "dimensions": f"{width}x{height}", "format": "jpg", "crs": "EPSG:4326"})
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=180) as response:
        path.write_bytes(response.read())


def frame_sensor(year: int) -> str:
    return "Sentinel-2, 10 m" if year >= SENTINEL_FIRST_YEAR else "Landsat, 30 m"


def cumulative_overlays(events: geopandas.GeoDataFrame, area, years, size) -> dict:
    """{año: RGBA (alto, ancho, 4)} con los eventos acumulados hasta ese año; el más reciente va encima."""
    width, height = size
    transform = from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, width, height)
    layers = {}
    for kind in KIND_COLORS:
        for year in years:
            subset = events[(events["kind"] == kind) & (events["year_to"] == year)]
            layers[(kind, year)] = rasterize([(g, 1) for g in subset.geometry], out_shape=(height, width), transform=transform,
                                             fill=0, dtype="uint8", all_touched=True).astype(bool) if len(subset) else None
    overlays = {}
    accumulated = {kind: numpy.zeros((height, width), dtype=bool) for kind in KIND_COLORS}
    for year in years:
        for kind in KIND_COLORS:
            if layers[(kind, year)] is not None:
                accumulated[kind] |= layers[(kind, year)]
        rgba = numpy.zeros((height, width, 4), dtype=numpy.float32)
        for kind, color in KIND_COLORS.items():  # un orden fijo: el construido va al final y gana
            rgba[accumulated[kind]] = matplotlib.colors.to_rgba(color, 0.85)
        overlays[year] = rgba
    return overlays


def meters_size(area) -> tuple:
    """Ancho y alto del bbox en metros (la grilla es lineal en grados)."""
    meters_x = (area.long_to - area.long_from) * 111_320.0 * numpy.cos(numpy.radians((area.lat_from + area.lat_to) / 2))
    return meters_x, (area.lat_to - area.lat_from) * 111_320.0


def panel_areas(zone) -> dict:
    """Bbox de cada panel (Monte Hermoso al oeste de CUT_LON, Balneario Sauce Grande al este), con un margen chico."""
    areas = {}
    for key, side in (("mh", box(-180, -90, CUT_LON, 90)), ("sg", box(CUT_LON, -90, 180, 90))):
        minx, miny, maxx, maxy = zone.intersection(side).bounds
        areas[key] = LongLatBBox(minx - 0.002, maxx + 0.002, miny - 0.002, maxy + 0.002)
    return areas


def render_frame(bases: dict, overlays: dict, rates: pandas.DataFrame, year: int, areas: dict, zone):
    """Un cuadro: dos mapas apilados a la misma escala (Monte Hermoso arriba, Sauce Grande abajo), leyenda y curva de cobertura."""
    margin, title_h, gap, curve_h, bottom_h, map_w = 18, 42, 12, 132, 36, MAP_WIDTH
    scale = map_w / meters_size(areas["mh"])[0]  # píxeles por metro, igual en los dos paneles
    sizes = {key: tuple(round(m * scale) for m in meters_size(area)) for key, area in areas.items()}
    width = map_w + 2 * margin
    height = margin + title_h + sizes["mh"][1] + gap + sizes["sg"][1] + gap + 12 + curve_h + bottom_h
    fig = plt.figure(figsize=(width / 100, height / 100), dpi=100, facecolor=SURFACE)

    def axes(x, y_top, w, h):
        return fig.add_axes([x / width, 1 - (y_top + h) / height, w / width, h / height])

    y = margin + title_h
    side_note = []
    for key in ("mh", "sg"):
        w, h = sizes[key]
        ax = axes(margin, y, w, h)
        area = areas[key]
        extent = (area.long_from, area.long_to, area.lat_from, area.lat_to)
        ax.imshow(bases[key], extent=extent, aspect="auto")
        ax.imshow(overlays[key], extent=extent, aspect="auto", interpolation="nearest")
        for polygon in ([zone] if zone.geom_type == "Polygon" else list(zone.geoms)):
            ax.plot(*polygon.exterior.xy, color="white", linewidth=0.7, alpha=0.7)
        ax.set_xlim(area.long_from, area.long_to)
        ax.set_ylim(area.lat_from, area.lat_to)
        ax.set_axis_off()
        ax.text(0.012, 0.965, PANEL_NAMES[key], transform=ax.transAxes, fontsize=9, color="white", va="top", fontweight="bold",
                bbox=dict(boxstyle="round,pad=0.25", fc="black", alpha=0.55, lw=0))
        y += h + gap
        if key == "sg" and map_w - w > 120:  # el hueco a la derecha del panel angosto aloja la leyenda
            side = axes(margin + w + 10, y - gap - h, map_w - w - 10, h)
            side.set_axis_off()
            handles = [plt.Rectangle((0, 0), 1, 1, color=color) for color in KIND_COLORS.values()]
            side.legend(handles, list(KIND_LABELS.values()), loc="upper left", fontsize=8,
                        frameon=False, labelcolor=INK_SECONDARY, handlelength=1.0, labelspacing=0.9, borderaxespad=0)
            side.text(0, 0.62, "acumulado desde 1986,\nsolo si persiste 3 años", transform=side.transAxes, fontsize=7.5, color=INK_SECONDARY, va="top")
            side.text(0, 0.02, "Línea blanca:\nzona de estudio", transform=side.transAxes, fontsize=8, color=INK_SECONDARY, va="bottom")

    fig.text(margin / width, 1 - (margin + title_h / 2) / height, f"Monte Hermoso y Sauce Grande, {year}", fontsize=13, fontweight="bold", color=INK, va="center")
    note = frame_sensor(year)
    fig.text(1 - margin / width, 1 - (margin + title_h / 2) / height, note, fontsize=9, color=INK_SECONDARY, ha="right", va="center")

    ax_line = axes(margin + 36, y + 12, map_w - 36, curve_h)
    ax_line.set_facecolor(SURFACE)
    ax_line.plot(rates["year_to"], rates["tree_ha"], color=LINE_COLOR, linewidth=1.6)
    current = rates[rates["year_to"] == year]
    if len(current):
        ax_line.plot(current["year_to"], current["tree_ha"], "o", color=LINE_COLOR, markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5)
        ax_line.annotate(f"{current['tree_ha'].iloc[0]:,.0f} ha".replace(",", "."), (year, current["tree_ha"].iloc[0]),
                         textcoords="offset points", xytext=(0, 9), ha="center" if year < 2020 else "right", fontsize=9, color=INK)
    ax_line.axvline(ORDINANCE_YEAR, color=INK_SECONDARY, linewidth=0.8, linestyle=(0, (3, 3)))
    ax_line.text(ORDINANCE_YEAR + 0.4, ax_line.get_ylim()[1], "Ordenanza 1913 (2010)", fontsize=8, color=INK_SECONDARY, va="top")
    ax_line.set_xlim(rates["year_to"].min() - 0.5, rates["year_to"].max() + 0.5)
    ax_line.set_ylabel("Cobertura arbórea (ha)", fontsize=8, color=INK_SECONDARY)
    ax_line.tick_params(colors=INK_SECONDARY, labelsize=8, length=0)
    ax_line.grid(axis="y", color="#e4e3df", linewidth=0.6)
    for side_name in ("top", "right", "left", "bottom"):
        ax_line.spines[side_name].set_visible(False)
    fig.canvas.draw()
    frame = Image.fromarray(numpy.asarray(fig.canvas.buffer_rgba())).convert("RGB")
    plt.close(fig)
    return frame


def shared_palette(frames) -> Image.Image:
    """
    Una paleta para todo el GIF: colores de las imágenes más los de la leyenda y el gráfico, que por ser
    pocos píxeles se perderían con una paleta por cuadro.
    """
    fixed = [KIND_COLORS["loss"], KIND_COLORS["gain"], BUILT_COLOR, INK, INK_SECONDARY, SURFACE, LINE_COLOR, "#e4e3df", "#ffffff"]
    sample = frames[:: max(1, len(frames) // 8)]
    mosaic = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for k, frame in enumerate(sample):
        mosaic.paste(frame, (0, k * frame.height))
    colors = 256 - len(fixed)
    values = mosaic.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).getpalette()[: colors * 3]
    for color in fixed:
        values += [round(c * 255) for c in matplotlib.colors.to_rgb(color)]
    palette = Image.new("P", (1, 1))
    palette.putpalette(values + [0] * (768 - len(values)))
    return palette


def render_zone_preview(out_path=None):
    """
    Cuadro Sentinel-2 de 2026 con el contorno de la zona de estudio, las localidades (Georef) y los lugares
    de OpenStreetMap por confirmar, con grilla de coordenadas para pedir ajustes. Escribe también el KML.
    """
    from gee_mh.aoi import ZONE_PATH, write_kml

    cfg = load_config()
    Preprocessor(ee_project=cfg["ee_project"])  # solo para ee.Initialize
    zone = geopandas.read_file(ZONE_PATH).geometry.iloc[0]
    minx, miny, maxx, maxy = zone.bounds
    area = LongLatBBox(minx - 0.004, maxx + 0.004, miny - 0.004, maxy + 0.006)
    developments = pandas.read_csv(PROJECT_ROOT / "data" / "reference" / "desarrollos_inmobiliarios.csv").dropna(subset=["lat", "lon"])
    out_path = out_path or ZONE_PATH.with_name("zona_urbana_preview.png")
    frame_path = PROJECT_ROOT / "data" / "cache" / "frames" / "preview_2026.jpg"
    download_frame(area, 2026, False, frame_path, width=2400)
    places = pandas.read_csv(PROJECT_ROOT / "data" / "reference" / "lugares_osm.csv")
    localities = [("Monte Hermoso", -38.9867, -61.2909), ("Balneario Sauce Grande", -38.9938, -61.2080)]

    fig, ax = plt.subplots(figsize=(18, 6.4), dpi=130)
    ax.imshow(numpy.asarray(Image.open(frame_path).convert("RGB")), extent=(area.long_from, area.long_to, area.lat_from, area.lat_to), aspect="auto")
    for polygon in ([zone] if zone.geom_type == "Polygon" else list(zone.geoms)):
        ax.plot(*polygon.exterior.xy, color="#ff2d2d", linewidth=2)
    for name, lat, lon in localities:
        ax.plot(lon, lat, "*", color="white", markeredgecolor="black", markersize=15)
        ax.annotate(name, (lon, lat), textcoords="offset points", xytext=(0, 12), ha="center", fontsize=10, color="white", fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.2", fc="black", alpha=0.55, lw=0))
    for k, row in enumerate(places.itertuples()):
        ax.plot(row.lon, row.lat, "o", color="#ffd400", markeredgecolor="black", markersize=8)
        ax.annotate(row.name, (row.lon, row.lat), textcoords="offset points", xytext=(0, -16 - 14 * (k % 2)), ha="center", fontsize=8, color="#ffd400",
                    bbox=dict(boxstyle="round,pad=0.15", fc="black", alpha=0.55, lw=0))
    for row in developments.itertuples():
        ax.plot(row.lon, row.lat, "s", color="#ff7a1a", markeredgecolor="black", markersize=9)
        ax.annotate(row.name.split(" (")[0].split(" - ")[0] + " (aprox.)" if "aproximado" in str(row.notes) else row.name.split(" (")[0].split(" - ")[0],
                    (row.lon, row.lat), textcoords="offset points", xytext=(0, 12), ha="center", fontsize=8, color="#ff9d4d",
                    bbox=dict(boxstyle="round,pad=0.15", fc="black", alpha=0.55, lw=0))
    ax.text(area.long_from + 0.002, area.lat_to - 0.002, "Habitar, Pinar del Golf y Los Bosques (parcela 1050c, error de unos 250 m) ubicados con fuentes públicas",
            fontsize=9, color="white", va="top", bbox=dict(boxstyle="round,pad=0.3", fc="black", alpha=0.6, lw=0))
    ax.set_xlim(area.long_from, area.long_to)
    ax.set_ylim(area.lat_from, area.lat_to)
    ax.set_xticks(numpy.arange(round(area.long_from, 2), area.long_to, 0.02))
    ax.set_yticks(numpy.arange(round(area.lat_from, 2), area.lat_to, 0.01))
    ax.grid(color="white", alpha=0.25, linewidth=0.5)
    ax.tick_params(labelsize=8)
    ax.set_title(f"Zona de estudio propuesta ({geopandas.read_file(ZONE_PATH)['area_km2'].iloc[0]:.1f} km², margen de 1 km sobre la huella urbana); fondo Sentinel-2, verano 2026", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    write_kml(zone, [("Monte Hermoso (Georef)", *localities[0][1:]), ("Balneario Sauce Grande (Georef)", *localities[1][1:])]
              + [(r.name, r.lat, r.lon) for r in places.itertuples()], ZONE_PATH.with_suffix(".kml"))
    print(f"Vista previa: {out_path} y {ZONE_PATH.with_suffix('.kml')}")


def run(years):
    from gee_mh.aoi import ZONE_PATH

    cfg = load_config()
    zone_area = load_area(small=False)
    zone = geopandas.read_file(ZONE_PATH).geometry.iloc[0]
    areas = panel_areas(zone)
    Preprocessor(ee_project=cfg["ee_project"])  # solo para ee.Initialize
    rates = pandas.read_csv(EXPORTS / "longterm_rates.csv")
    events = geopandas.read_file(EXPORTS / "longterm_events.geojson")
    frames_dir = DOCS / "frames"
    cache_dir = PROJECT_ROOT / "data" / "cache" / "frames"

    scenes = scenes_per_year(zone_area, [y for y in years if y < SENTINEL_FIRST_YEAR])
    wide = {y: scenes.get(y, 1) == 0 for y in years}

    def fetch(job):
        key, year = job
        path = cache_dir / key / f"{year}{'w' if wide[year] else ''}.jpg"
        if not path.exists():
            download_frame(areas[key], year, wide[year], path, width=round(meters_size(areas[key])[0] / 10))  # 10 m por píxel
        return job, path

    jobs = [(key, year) for key in areas for year in years]
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = dict(pool.map(fetch, jobs))

    scale = MAP_WIDTH / meters_size(areas["mh"])[0]
    sizes = {key: tuple(round(m * scale) for m in meters_size(area)) for key, area in areas.items()}
    overlays = {key: cumulative_overlays(events, areas[key], years, sizes[key]) for key in areas}
    for old in frames_dir.glob("*.jpg"):  # cuadros del partido completo, de una versión anterior
        old.unlink()
    gif_frames = []
    for year in years:
        bases = {}
        for key in areas:
            image = Image.open(paths[(key, year)]).convert("RGB")
            bases[key] = numpy.asarray(image)
            web = image.resize((round(meters_size(areas[key])[0] / 12), round(meters_size(areas[key])[1] / 12)), Image.LANCZOS)  # 12 m por píxel
            (frames_dir / key).mkdir(parents=True, exist_ok=True)
            web.save(frames_dir / key / f"{year}.jpg", quality=80, optimize=True)
        gif_frames.append(render_frame(bases, {key: overlays[key][year] for key in areas}, rates, year, areas, zone))
    palette = shared_palette(gif_frames)
    quantized = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in gif_frames]
    durations = [350] * (len(quantized) - 1) + [2500]
    quantized[0].save(DOCS / "monte_hermoso.gif", save_all=True, append_images=quantized[1:], duration=durations, loop=0, optimize=True)
    export_web_data(areas, zone, rates, events, years)
    print(f"Exportado: {DOCS / 'monte_hermoso.gif'} ({len(years)} cuadros), {frames_dir} y {DOCS / 'data'}")


def export_web_data(areas, zone, rates, events, years):
    """Datos del mapa web (docs/index.html): contorno, eventos de 30 m, eventos de 10 m confirmados y metadatos."""
    import json

    data_dir = DOCS / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    def save(frame, name):
        frame = frame.copy()
        frame["geometry"] = frame.geometry.simplify(0.00002).set_precision(1e-5)
        frame.to_file(data_dir / name, driver="GeoJSON")

    save(events[["kind", "year_to", "area_m2", "geometry"]], "longterm_events.geojson")
    sentinel = geopandas.read_file(EXPORTS / "events_by_baseline.geojson")
    sentinel = sentinel[sentinel["confirmed"] & sentinel["baseline"].isin(["ndvi_diff", "veg_to_built"])]
    save(sentinel[["baseline", "year_to", "area_m2", "cause", "geometry"]], "sentinel_events.geojson")
    geopandas.GeoDataFrame(geometry=[zone], crs="EPSG:4326").to_file(data_dir / "zona.geojson", driver="GeoJSON")
    meta = {
        "panels": {key: {"name": PANEL_NAMES[key], "bounds": [[a.lat_from, a.long_from], [a.lat_to, a.long_to]]} for key, a in areas.items()},
        "ordinance_year": ORDINANCE_YEAR,
        "frames": [{"year": y, "sensor": frame_sensor(y)} for y in years],
        "tree_ha": {int(r.year_to): round(float(r.tree_ha), 1) for r in rates.itertuples()},
    }
    (data_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=1986)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--zona-preview", action="store_true", help="solo la vista previa de la zona de estudio (aoi/zona_urbana_preview.png)")
    args = parser.parse_args()
    if args.zona_preview:
        render_zone_preview()
    else:
        run(list(range(args.start_year, args.end_year + 1)))
