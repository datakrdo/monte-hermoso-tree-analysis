"""Atribución de causa por evento de pérdida (paso 6 del plan): desarrollo
inmobiliario vs. natural (incendio, erosión/inundación costera), degradando a
`desconocido`/`mixto` con score de confianza cuando la evidencia no alcanza -
sin forzar una clasificación binaria.
"""

from dataclasses import dataclass
from typing import Optional

import ee
import numpy

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


def _blob_fraction(mask: numpy.ndarray, blob_mask: numpy.ndarray) -> float:
    n = blob_mask.sum()
    if n == 0:
        return 0.0
    return float((mask & blob_mask).sum()) / float(n)


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


def attribute_event(
    blob_mask: numpy.ndarray,
    image_t1,
    image_t2,
    ee_geometry: Optional[ee.Geometry] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Attribution:
    """
    Clasifica un único evento de pérdida (blob_mask ya alineado a la grilla
    común de image_t1/image_t2, ver detection.align_pair).
    ee_geometry/date_from/date_to son opcionales: sin ellos se omite la
    verificación de incendios y esa evidencia queda ausente (no se asume
    'no hubo incendio').
    """
    # blob_mask está en la grilla común del par (la del sensor más grueso, ver
    # detection.align_pair): si t2 es el sensor fino (ej. Landsat -> Sentinel-2),
    # sus máscaras se bajan a esa grilla.
    built_t2 = resample_mask(built_mask(image_t2), blob_mask.shape)
    water_t2 = resample_mask(water_mask(image_t2), blob_mask.shape)

    built_fraction = _blob_fraction(built_t2, blob_mask)
    water_fraction = _blob_fraction(water_t2, blob_mask)

    fire_count = None
    if ee_geometry is not None and date_from and date_to:
        fire_count = count_fire_detections(ee_geometry, date_from, date_to)

    evidence = {
        "built_fraction_t2": built_fraction,
        "water_fraction_t2": water_fraction,
        "fire_detections": fire_count,
    }

    if built_fraction >= BUILT_INCREASE_THRESHOLD:
        return Attribution("desarrollo_inmobiliario", built_fraction, evidence)
    if fire_count:
        return Attribution("natural_incendio", min(1.0, fire_count / 5.0), evidence)
    if water_fraction >= WATER_INCREASE_THRESHOLD:
        return Attribution("natural_erosion_inundacion", water_fraction, evidence)

    # Ninguna evidencia es concluyente: no forzar la clasificación.
    return Attribution("desconocido", 0.0, evidence)
