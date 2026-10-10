"""Atribución de causa por evento de pérdida: desarrollo
inmobiliario vs. natural (incendio, erosión/inundación costera), degradando a
`desconocido`/`mixto` con score de confianza cuando la evidencia no alcanza -
sin forzar una clasificación binaria.
"""

from dataclasses import dataclass

import cv2
import ee
import numpy
from rasterio.features import rasterize
from rasterio.transform import from_bounds

from gee_mh.detection import resample_mask
from gee_mh.masks import built_mask, water_mask

BUILT_INCREASE_THRESHOLD = 0.3  # fracción del blob que pasa a "built" en t2
WATER_INCREASE_THRESHOLD = 0.3  # fracción del blob que pasa a "water" en t2
FIRE_BUFFER_M = 200  # buffer alrededor del blob para buscar detecciones FIRMS


@dataclass
class Attribution:
    cause: str  # 'desarrollo_inmobiliario' | 'natural_incendio' | 'natural_erosion_inundacion' | 'desconocido'
    confidence: float
    evidence: dict


def count_fire_detections(ee_geometry: ee.Geometry, date_from: str, date_to: str) -> int:
    """
    Cuenta detecciones de incendio (FIRMS) en un buffer alrededor del evento
    durante la ventana de fecha t1-t2. Requiere una llamada a EE por evento -
    aceptable para el volumen esperado de eventos anuales de un partido.
    """
    buffered = ee_geometry.buffer(FIRE_BUFFER_M)
    fires = (
        ee.ImageCollection("FIRMS")
        .filterBounds(buffered)
        .filterDate(date_from, date_to)
        .select("T21")
    )
    count = fires.reduce(ee.Reducer.count()).reduceRegion(
        reducer=ee.Reducer.max(), geometry=buffered, scale=1000, bestEffort=True
    ).get("T21_count")
    return int(count.getInfo() or 0)


def images_after(series: dict, year_to: int, extra_years: int = 2) -> list:
    """t2 y hasta extra_years años siguientes que existan en la serie."""
    return [series[year] for year in range(year_to, year_to + extra_years + 1) if year in series]


def attribute_events(polygons, area, shape, image_t1, images_after) -> list:
    """
    Una Attribution por polígono de pérdida (mask_to_polygons sobre una máscara
    de forma `shape`). La evidencia se mide dentro de cada polígono: la
    fracción construida (Dynamic World) en el mejor de los años de
    images_after (t2 y los siguientes: la obra puede terminar uno o dos años
    después de despejar el terreno), y la fracción de agua en el primero.
    NDBI sube entre t1 y el último año: queda en la evidencia, sin regla.
    Los incendios (FIRMS) no se consultan: eran una llamada a EE por evento.
    """
    if not polygons:
        return []
    transform = from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, shape[1], shape[0])
    # all_touched: los polígonos siguen los bordes de los píxeles
    labels = rasterize([(p, i + 1) for i, p in enumerate(polygons)], out_shape=shape, transform=transform,
                       fill=0, dtype="int32", all_touched=True).ravel()
    size = numpy.maximum(numpy.bincount(labels, minlength=len(polygons) + 1)[1:], 1)

    def fraction(mask: numpy.ndarray) -> numpy.ndarray:
        weights = resample_mask(mask, shape).ravel().astype(float)
        return numpy.bincount(labels, weights=weights, minlength=len(polygons) + 1)[1:] / size

    built = numpy.max([fraction(built_mask(image)) for image in images_after], axis=0)
    water = fraction(water_mask(images_after[0]))
    ndbi_rise = fraction_mean(labels, size, resample_float(images_after[-1].bands["ndbi"] - image_t1.bands["ndbi"], shape))
    # caída media de NDVI entre t1 y t2 (images_after[0]), sin el corrimiento regional: la usa el filtro de cva
    ndvi_drop = fraction_mean(labels, size, resample_float(image_t1.bands["ndvi"] - images_after[0].bands["ndvi"], shape))

    result = []
    for i in range(len(polygons)):
        evidence = {"built_fraction": float(built[i]), "water_fraction": float(water[i]), "ndbi_rise": float(ndbi_rise[i]), "ndvi_drop": float(ndvi_drop[i])}
        if built[i] >= BUILT_INCREASE_THRESHOLD:
            result.append(Attribution("desarrollo_inmobiliario", float(built[i]), evidence))
        elif water[i] >= WATER_INCREASE_THRESHOLD:
            result.append(Attribution("natural_erosion_inundacion", float(water[i]), evidence))
        else:
            # Ninguna evidencia es concluyente: no forzar la clasificación.
            result.append(Attribution("desconocido", 0.0, evidence))
    return result


def resample_float(values: numpy.ndarray, shape) -> numpy.ndarray:
    if values.shape == tuple(shape):
        return values
    return cv2.resize(values.astype(numpy.float32), (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def fraction_mean(labels: numpy.ndarray, size: numpy.ndarray, values: numpy.ndarray) -> numpy.ndarray:
    """Media de values dentro de cada etiqueta (1..n) de labels."""
    return numpy.bincount(labels, weights=values.ravel().astype(float), minlength=len(size) + 1)[1:] / size


def _selftest():
    from gee_mh.preprocessing import DYNAMIC_WORLD_CLASSES, LongLatBBox, SatelliteImage
    from shapely.geometry import Point

    from gee_mh.export import mask_to_polygons

    area = LongLatBBox(0.0, 1.0, 0.0, 1.0)
    shape = (40, 40)

    def make_image(built_block=None):
        image = SatelliteImage("d", area, 10.0, shape[1], shape[0])
        image.bands["ndvi"] = numpy.full(shape, 0.7, dtype=numpy.float32)
        image.bands["ndbi"] = numpy.full(shape, -0.2, dtype=numpy.float32)
        image.dw_label = numpy.full(shape, DYNAMIC_WORLD_CLASSES.index("trees"), dtype=numpy.uint8)
        for rows, cols, name in built_block or []:
            image.dw_label[rows, cols] = DYNAMIC_WORLD_CLASSES.index(name)
        return image

    mask = numpy.zeros(shape, dtype=bool)
    mask[2:8, 2:8] = True      # termina construido un año después
    mask[20:26, 20:26] = True  # sigue vegetación
    mask[30:36, 2:8] = True    # pasa a agua
    polygons = mask_to_polygons(area, mask)
    assert len(polygons) == 3
    t1 = make_image()
    t2 = make_image([(slice(30, 36), slice(2, 8), "water")])
    t3 = make_image([(slice(2, 8), slice(2, 8), "built")])
    t3.bands["ndbi"][2:8, 2:8] = 0.3
    attributions = attribute_events(polygons, area, shape, t1, [t2, t3])

    def at(row, col):  # atribución del polígono que contiene el píxel (row, col)
        point = Point((col + 0.5) / shape[1], 1 - (row + 0.5) / shape[0])
        return next(a for p, a in zip(polygons, attributions) if p.contains(point))

    built, green, water = at(4, 4), at(22, 22), at(32, 4)
    assert built.cause == "desarrollo_inmobiliario" and built.confidence == 1.0, built
    assert abs(built.evidence["ndbi_rise"] - 0.5) < 1e-6, built.evidence
    assert abs(built.evidence["ndvi_drop"]) < 1e-6, "t1 y t2 tienen el mismo NDVI"
    t2.bands["ndvi"][20:26, 20:26] = 0.4
    assert abs(attribute_events(polygons, area, shape, t1, [t2, t3])[[p.contains(Point(22.5 / 40, 1 - 22.5 / 40)) for p in polygons].index(True)].evidence["ndvi_drop"] - 0.3) < 1e-6
    assert green.cause == "desconocido", green
    assert water.cause == "natural_erosion_inundacion", water
    assert attribute_events([], area, shape, t1, [t2]) == []
    print("attribution selftest OK")


if __name__ == "__main__":
    _selftest()
