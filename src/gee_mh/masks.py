"""Máscaras contextuales para excluir océano, agua, dunas/arena y superficie
ya urbanizada del análisis de pérdida de copa.

Dynamic World es la fuente preferida (ya viene en SatelliteImage.classes desde
2015-06-27 en adelante). Antes de esa fecha no hay Dynamic World, así que se
degrada a un umbral simple sobre NDVI/NDBI - más ruidoso, documentado como
tal.
"""

import numpy
from rasterio.features import rasterize
from rasterio.transform import from_bounds


def aoi_mask(area, aoi_geometry, shape) -> numpy.ndarray:
    """
    True dentro del polígono del partido, sobre la grilla (norte arriba) de
    `shape` que cubre `area` (el bbox incluye mar y partidos vecinos).
    """
    height, width = shape
    transform = from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, width, height)
    return rasterize([(aoi_geometry, 1)], out_shape=shape, transform=transform, fill=0, dtype="uint8").astype(bool)


def has_dynamic_world(image) -> bool:
    return image.dw_label is not None


def water_mask(image) -> numpy.ndarray:
    if has_dynamic_world(image):
        return image.to_dynamic_world_mask("water")
    # NOTE: sin Dynamic World, NDVI<0 como proxy de agua (más ruidoso
    # en costa/espuma de olas); upgrade: JRC Global Surface Water histórico.
    return image.bands["ndvi"] < 0.0


def built_mask(image) -> numpy.ndarray:
    if has_dynamic_world(image):
        return image.to_dynamic_world_mask("built")
    # NOTE: sin Dynamic World, NDBI>0 como proxy de superficie construida;
    # upgrade: clasificador urbano calibrado localmente.
    return image.bands["ndbi"] > 0.0


def bare_or_dune_mask(image) -> numpy.ndarray:
    if has_dynamic_world(image):
        return image.to_dynamic_world_mask("bare")
    # NOTE: sin proxy confiable de médanos/arena pre-2015 con las bandas
    # actuales; upgrade: ESA WorldCover o límites de duna digitalizados.
    return numpy.zeros(image.bands["ndvi"].shape, dtype=bool)


def vegetation_candidate_mask(image) -> numpy.ndarray:
    """
    Área candidata a contener vegetación leñosa: no agua, no ya construida,
    no médano/arena.
    """
    return ~water_mask(image) & ~built_mask(image) & ~bare_or_dune_mask(image)
