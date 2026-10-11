"""Imágenes SPOT 5 (CNES, SPOT World Heritage, nivel 1A) para el mapa web: solo para mirar, la detección no las usa.

Cada fecha tiene una escena multiespectral J (10 m; en el TIF las bandas vienen en el orden infrarrojo cercano, rojo, verde,
SWIR: se comprobó con el mar y el campo, aunque METADATA.DIM las nombra XS1 a XS3) y una
pancromática A (5 m). No hay banda azul, así que se genera un color simulado fusionado a 5 m (el azul se aproxima con el
verde); el falso color (IR-R-V) se descartó por poco legible. Las escenas vienen sin corrección geométrica y solo con los 4 puntos de
las esquinas: se georreferencian con ellos y el desfase que queda se mide por correlación de bordes contra Sentinel-2
2017 (cuadros de data/cache/frames), por panel.

Entradas: carpetas descomprimidas del portal GEODES, <dir>/spot5_AAAA-MM-DD_{J,A}/SCENE01/IMAGERY.TIF.
Salidas: docs/frames/spot/AAAA-MM-DD_nat_{mh,sg}.jpg

Uso:
    PYTHONPATH=src python3 -m gee_mh.spot [--dir ~/spot/extract]
"""

import argparse
from pathlib import Path

import geopandas
import numpy
import rasterio
from PIL import Image
from rasterio.warp import Resampling, reproject
from scipy import ndimage

from gee_mh.animation import DOCS, SPOT_SCENES, meters_size, panel_areas
from gee_mh.config import PROJECT_ROOT

PIXEL_M = 5  # resolución de la pancromática
MAX_SHIFT_M = 3000  # un desfase mayor se considera una correlación fallida


def read_scene(directory: Path, date: str, kind: str):
    """(matriz (bandas, alto, ancho), puntos de control, crs) de una escena sin georreferenciar."""
    with rasterio.open(directory / f"spot5_{date}_{kind}" / "SCENE01" / "IMAGERY.TIF") as source:
        gcps, crs = source.gcps
        return source.read(), gcps, crs


def warp(array, gcps, crs, area, width, height, shift=(0.0, 0.0)):
    """Lleva la escena a la grilla del panel (EPSG:4326), corrida shift=(este, norte) metros; fuera de la escena vale 0."""
    from rasterio.transform import from_bounds

    dlon = shift[0] / (111_320.0 * numpy.cos(numpy.radians((area.lat_from + area.lat_to) / 2)))
    dlat = shift[1] / 111_320.0
    out = numpy.zeros((array.shape[0], height, width), dtype=numpy.float32)
    reproject(array.astype(numpy.float32), out, gcps=gcps, src_crs=crs, src_nodata=0, dst_nodata=0, dst_crs="EPSG:4326",
              dst_transform=from_bounds(area.long_from - dlon, area.lat_from - dlat, area.long_to - dlon, area.lat_to - dlat, width, height),
              resampling=Resampling.bilinear)
    return out


def edges(gray):
    smooth = ndimage.gaussian_filter(gray, 1.5)
    magnitude = numpy.hypot(ndimage.sobel(smooth, 0), ndimage.sobel(smooth, 1))
    return numpy.minimum(magnitude, numpy.percentile(magnitude, 99)) - magnitude.mean()


def estimate_shift(pan, gcps, crs, area, reference_path):
    """Desfase (este, norte) en metros de la escena respecto del cuadro Sentinel-2 de referencia (10 m por píxel) y su calidad."""
    reference = numpy.asarray(Image.open(reference_path).convert("L"), dtype=numpy.float32)
    height, width = reference.shape
    moving = warp(pan, gcps, crs, area, width, height)[0]
    valid = moving > 0
    a, b = edges(reference), edges(moving) * valid
    correlation = numpy.fft.fftshift(numpy.fft.ifft2(numpy.fft.fft2(a) * numpy.conj(numpy.fft.fft2(b))).real)
    iy, ix = numpy.unravel_index(correlation.argmax(), correlation.shape)
    dx, dy = ix - width // 2, iy - height // 2
    mx, my = meters_size(area)
    return (dx * mx / width, -dy * my / height), correlation.max() / numpy.abs(correlation).mean()


def render(date: str, directory: Path, areas: dict) -> None:
    color, gcps_j, crs_j = read_scene(directory, date, "J")
    pan, gcps_a, crs_a = read_scene(directory, date, "A")
    out_dir = DOCS / "frames" / "spot"
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, area in areas.items():
        reference = PROJECT_ROOT / "data" / "cache" / "frames" / key / "2017.jpg"
        shift, quality = estimate_shift(pan, gcps_a, crs_a, area, reference)
        print(f"{date} {key}: desfase este {shift[0]:.0f} m, norte {shift[1]:.0f} m (pico/media {quality:.1f})")
        assert max(abs(shift[0]), abs(shift[1])) < MAX_SHIFT_M, "desfase demasiado grande: revisar la correlación"
        width, height = (round(m / PIXEL_M) for m in meters_size(area))
        p = warp(pan, gcps_a, crs_a, area, width, height, shift)[0]
        c = warp(color, gcps_j, crs_j, area, width, height, shift)  # IR cercano, rojo, verde, SWIR
        valid = (p > 0) & (c[:3].min(axis=0) > 0)
        # fusión por cociente: el color de 10 m por el detalle de la pancromática (pan / pan suavizada a 10 m)
        detail = numpy.clip(p / numpy.maximum(ndimage.gaussian_filter(p, 2.0), 1), 0.5, 2) * valid + (~valid)
        nir, red, green = (c[0] * detail), (c[1] * detail), (c[2] * detail)
        bands = (red, 0.85 * green + 0.15 * nir, 0.9 * green)  # ponytail: azul aproximado con el verde, no es color real
        low, high = numpy.percentile(numpy.stack(bands)[:, valid], (2, 98))  # un solo estiramiento: si no, se pierde el balance
        rgb = numpy.dstack([(numpy.clip((b - low) / (high - low), 0, 1) ** 0.9 * 255).astype(numpy.uint8) for b in bands])
        rgb[~valid] = 0
        Image.fromarray(rgb).save(out_dir / f"{date}_nat_{key}.jpg", quality=82, optimize=True)


if __name__ == "__main__":
    from gee_mh.aoi import ZONE_PATH

    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, default=Path.home() / "spot" / "extract")
    args = parser.parse_args()
    areas = panel_areas(geopandas.read_file(ZONE_PATH).geometry.iloc[0])
    for date in SPOT_SCENES:
        render(date, args.dir, areas)
