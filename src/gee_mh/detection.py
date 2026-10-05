"""Detección de pérdida de cobertura arbórea entre dos años, comparando tres
baselines (paso 5 del plan): transición de clase Dynamic World, CVA sobre
índices, y una diferencia simple de NDVI. Se corren los tres sobre el mismo
par de años y se comparan con IoU/F1 contra una muestra etiquetada (paso 7,
en validation.py) antes de elegir el detector final.
"""

from typing import Optional

import cv2
import numpy

from gee_mh.masks import aoi_mask, bare_or_dune_mask, built_mask, vegetation_candidate_mask, water_mask

NDVI_VEGETATION_THRESHOLD = 0.3
NDVI_DROP_THRESHOLD = 0.15
TREES_P_MIN = 50   # probabilidad (0-100) mínima de 'trees' en t1
TREES_P_DROP = 40  # caída mínima de esa probabilidad en t2
CVA_SIGMAS = 2.0   # umbral del CVA: media + CVA_SIGMAS * desvío de la magnitud


def align_pair(image_t1, image_t2):
    """
    Dos años consecutivos pueden venir de sensores distintos (10m Sentinel-2
    vs 30m Landsat) y por lo tanto tener distinta grilla/dimensiones. Para
    poder comparar píxel a píxel, se remuestrea la imagen más fina a la
    grilla de la más gruesa.
    NOTE: remuestreo por área (cv2.INTER_AREA), no reproyección exacta -
    alcanza para detectar eventos de pérdida a escala de parcela/manzana.
    Upgrade: reproyectar ambas al mismo grid EE antes de descargar si se
    necesita precisión sub-parcela en los años de transición de sensor.
    Return (bands_t1, bands_t2, shape) con bandas comunes remuestreadas al
    tamaño más chico de los dos.
    """
    shape_t1 = image_t1.bands["ndvi"].shape
    shape_t2 = image_t2.bands["ndvi"].shape
    target_shape = min(shape_t1, shape_t2, key=lambda s: s[0] * s[1])
    target_wh = (target_shape[1], target_shape[0])  # cv2 quiere (width, height)

    common_bands = set(image_t1.bands) & set(image_t2.bands)

    def resample(image, shape):
        if shape == target_shape:
            return {b: image.bands[b] for b in common_bands}
        return {
            b: cv2.resize(image.bands[b].astype(numpy.float32), target_wh, interpolation=cv2.INTER_AREA)
            for b in common_bands
        }

    return resample(image_t1, shape_t1), resample(image_t2, shape_t2), target_shape


def resample_mask(mask: numpy.ndarray, target_shape) -> numpy.ndarray:
    if mask.shape == target_shape:
        return mask
    target_wh = (target_shape[1], target_shape[0])
    return cv2.resize(mask.astype(numpy.uint8), target_wh, interpolation=cv2.INTER_NEAREST).astype(bool)


def detect_loss_ndvi_diff(image_t1, image_t2) -> numpy.ndarray:
    """
    Baseline 1: caída de NDVI por debajo de un umbral en áreas que eran
    vegetación en t1. Es el único baseline que funciona en todo el rango
    2000-presente porque no depende de Dynamic World.
    """
    bands_t1, bands_t2, shape = align_pair(image_t1, image_t2)
    veg_t1 = bands_t1["ndvi"] > NDVI_VEGETATION_THRESHOLD
    ndvi_drop = bands_t1["ndvi"] - bands_t2["ndvi"]
    candidate_t1 = resample_mask(vegetation_candidate_mask(image_t1), shape)
    return veg_t1 & candidate_t1 & (ndvi_drop > NDVI_DROP_THRESHOLD)


def detect_loss_dynamic_world(image_t1, image_t2) -> Optional[numpy.ndarray]:
    """
    Baseline 2: caída de la probabilidad media de 'trees' de Dynamic World
    (de >= TREES_P_MIN en t1 a una caída de >= TREES_P_DROP puntos en t2).
    Usa la probabilidad y no la transición de etiqueta: la etiqueta de una
    ventana parpadea entre trees/shrub/grass y marcaba ~38% de los árboles
    como pérdida en un año. Solo disponible con Dynamic World en ambos años
    (2015-06-27 en adelante); devuelve None si no aplica.
    """
    if image_t1.dw_trees_p is None or image_t2.dw_trees_p is None:
        return None
    p1 = image_t1.dw_trees_p.astype(numpy.int16)
    p2 = image_t2.dw_trees_p.astype(numpy.int16)
    mask = (p1 >= TREES_P_MIN) & (p1 - p2 >= TREES_P_DROP)
    # ndvi_diff ya excluye agua/construido/médano en t1 (paso 4 del plan);
    # sin esto pasan transiciones espurias en costa/duna/borde urbano.
    return mask & resample_mask(vegetation_candidate_mask(image_t1), mask.shape)


def detect_loss_cva(image_t1, image_t2) -> Optional[numpy.ndarray]:
    """
    Baseline 3: Change Vector Analysis. Magnitud (norma euclídea) del vector de
    cambio de NDVI/NDMI/NDBI entre t1 y t2, sobre la leñosa estable de t1 y
    solo donde el NDVI cae. Umbral estadístico: media + CVA_SIGMAS desvíos de
    esa magnitud dentro de la leñosa estable.
    NOTE: umbral sin calibrar contra ninguna referencia; upgrade: aprenderlo
    de las etiquetas manuales. Requiere Dynamic World en ambos años (para la
    leñosa estable); devuelve None si no aplica.
    """
    if image_t1.dw_label is None or image_t2.dw_label is None:
        return None
    bands_t1, bands_t2, shape = align_pair(image_t1, image_t2)
    names = ("ndvi", "ndmi", "ndbi")
    change = numpy.dstack([bands_t2[n] - bands_t1[n] for n in names])
    magnitude = numpy.linalg.norm(change, axis=2)

    candidate = stable_woody(image_t1, None, shape) & resample_mask(vegetation_candidate_mask(image_t1), shape)
    if not candidate.any():
        return numpy.zeros(shape, dtype=bool)
    values = magnitude[candidate]
    threshold = values.mean() + CVA_SIGMAS * values.std()
    return candidate & (magnitude >= threshold) & (change[:, :, 0] < 0)


def filter_small_blobs(mask: numpy.ndarray, resolution_m: float, min_area_m2: float) -> numpy.ndarray:
    """
    Elimina blobs conectados más chicos que min_area_m2 (paso 5 del plan:
    filtrar falsos positivos por área mínima y conectividad).
    """
    min_pixels = max(1, int(min_area_m2 / (resolution_m ** 2)))
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(numpy.uint8), connectivity=8)
    filtered = numpy.zeros_like(mask, dtype=bool)
    for label in range(1, num_labels):  # 0 es el fondo
        if stats[label, cv2.CC_STAT_AREA] >= min_pixels:
            filtered[labels == label] = True
    return filtered


def _woody(image) -> Optional[numpy.ndarray]:
    """Leñosa (árboles + arbustal) según Dynamic World; None si no hay DW."""
    if image.dw_label is None:
        return None
    return image.to_dynamic_world_mask("trees") | image.to_dynamic_world_mask("shrub_and_scrub")


def stable_woody(image_t1, image_prev, shape) -> numpy.ndarray:
    """
    Leñosa en t1 y en el año previo: descarta el parpadeo de clase de Dynamic
    World y el pasto/cultivo que solo cambia de NDVI por fenología. Sin
    Dynamic World (pre-2015) no restringe nada.
    NOTE: ese caso queda con el ruido de NDVI de antes; upgrade: máscara
    leñosa de MapBiomas (hasta 2023) o WorldCover.
    """
    woody_t1 = _woody(image_t1)
    if woody_t1 is None:
        return numpy.ones(shape, dtype=bool)
    stable = resample_mask(woody_t1, shape)
    woody_prev = _woody(image_prev) if image_prev is not None else None
    if woody_prev is not None:
        stable = stable & resample_mask(woody_prev, shape)
    return stable


def detect_pair(detect_fn, series, year_from, year_to, area, aoi_geometry, min_area_m2, min_persistence_years=2):
    """
    Corre un baseline sobre (year_from, year_to) y lo refina: leñosa estable
    (año previo), confirmación en el año siguiente (si min_persistence_years
    >= 2 y hay un año siguiente de la misma resolución), recorte al partido y
    área mínima. series: {año: imagen} con años de contexto (year_from - 1,
    year_to + 1) si existen. Return (mask, confirmed), o (None, False) si el
    baseline no aplica al par.
    """
    image_t1, image_t2 = series[year_from], series[year_to]
    mask = detect_fn(image_t1, image_t2)
    if mask is None:
        return None, False

    mask = mask & stable_woody(image_t1, series.get(year_from - 1), mask.shape)

    image_next = series.get(year_to + 1)
    confirmed = min_persistence_years >= 2 and image_next is not None and image_next.resolution == image_t1.resolution
    if confirmed:
        # el NDVI sigue por debajo del de t1: la pérdida no se recuperó (cosecha, estacionalidad)
        still_lost = (image_t1.bands["ndvi"] - image_next.bands["ndvi"]) > NDVI_DROP_THRESHOLD
        mask = mask & resample_mask(still_lost, mask.shape)

    mask = mask & aoi_mask(area, aoi_geometry, mask.shape)
    return filter_small_blobs(mask, image_t2.resolution, min_area_m2), confirmed


def _selftest():
    from shapely.geometry import box

    from gee_mh.preprocessing import LongLatBBox, SatelliteImage, DYNAMIC_WORLD_CLASSES

    area = LongLatBBox(0.0, 1.0, 0.0, 1.0)
    shape = (40, 40)

    def make_image(ndvi, woody=True):
        image = SatelliteImage("d", area, 10.0, shape[1], shape[0])
        image.bands["ndvi"] = numpy.full(shape, ndvi, dtype=numpy.float32)
        image.dw_label = numpy.full(shape, DYNAMIC_WORLD_CLASSES.index("trees" if woody else "grass"), dtype=numpy.uint8)
        image.dw_trees_p = numpy.full(shape, 90 if woody else 5, dtype=numpy.uint8)
        return image

    prev, t1, t2, nxt = make_image(0.7), make_image(0.7), make_image(0.3), make_image(0.3)
    t1.dw_label[14:18, 32:36] = DYNAMIC_WORLD_CLASSES.index("grass")  # pasto en t1: no es leñosa
    prev.dw_label[30:34, 32:36] = DYNAMIC_WORLD_CLASSES.index("grass")  # parpadeo: no era leñosa antes
    nxt.bands["ndvi"][20:30, 20:30] = 0.7  # se recuperó: fenología, no pérdida
    series = {2019: prev, 2020: t1, 2021: t2, 2022: nxt}

    def detect_all(_t1, _t2):
        mask = numpy.zeros(shape, dtype=bool)
        mask[14:18, 32:36] = True  # pasto en t1 -> descartado
        mask[30:34, 32:36] = True  # leñosa solo en t1 -> descartado
        mask[20:30, 20:30] = True  # recuperada en t+1 -> descartado
        mask[2:12, 20:30] = True   # pérdida real, dentro del partido
        mask[2:12, 5:15] = True    # pérdida fuera del partido -> recortada
        mask[36:38, 36:38] = True  # 4 px < 10 px mínimos -> descartado
        return mask

    aoi = box(0.4, 0.0, 1.0, 1.0)  # mitad derecha del bbox
    mask, confirmed = detect_pair(detect_all, series, 2020, 2021, area, aoi, 1000)
    assert confirmed
    expected = numpy.zeros(shape, dtype=bool)
    expected[2:12, 20:30] = True
    assert (mask == expected).all(), int(mask.sum())

    mask, confirmed = detect_pair(detect_all, {k: v for k, v in series.items() if k != 2022}, 2020, 2021, area, aoi, 1000)
    assert not confirmed and mask[20:30, 20:30].all(), "sin año siguiente: no confirmado y sin filtrar recuperación"
    assert detect_pair(lambda *_: None, series, 2020, 2021, area, aoi, 1000) == (None, False)

    a, b = make_image(0.7), make_image(0.7)
    b.dw_trees_p[5:9, 5:9] = 20    # cae 70 puntos: pérdida
    b.dw_trees_p[10:14, 5:9] = 70  # cae 20 puntos: parpadeo, no cuenta
    loss = detect_loss_dynamic_world(a, b)
    assert loss[5:9, 5:9].all() and loss.sum() == 16, int(loss.sum())
    a.dw_trees_p = None
    assert detect_loss_dynamic_world(a, b) is None

    c1, c2 = make_image(0.7), make_image(0.7)
    for image in (c1, c2):
        image.bands["ndmi"] = numpy.zeros(shape, dtype=numpy.float32)
        image.bands["ndbi"] = numpy.zeros(shape, dtype=numpy.float32)
    c2.bands["ndvi"][5:9, 5:9] = 0.1   # cambio grande con caída de NDVI: pérdida
    c2.bands["ndvi"][20:24, 5:9] = 0.9  # cambio grande pero NDVI sube: no es pérdida
    loss = detect_loss_cva(c1, c2)
    assert loss[5:9, 5:9].all() and not loss[20:24, 5:9].any() and loss.sum() == 16, int(loss.sum())
    c1.dw_label = None
    assert detect_loss_cva(c1, c2) is None
    print("detection selftest OK")


if __name__ == "__main__":
    _selftest()
