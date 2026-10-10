"""Detección de pérdida de cobertura arbórea entre dos años, comparando tres
baselines: transición de clase Dynamic World, CVA sobre
índices, y una diferencia simple de NDVI. Se corren los tres sobre el mismo
par de años y se comparan con IoU/F1 contra una muestra etiquetada (paso 7,
en validation.py) antes de elegir el detector final.
"""

import copy
from typing import Optional

import cv2
import numpy

from gee_mh.masks import aoi_mask, bare_or_dune_mask, built_mask, vegetation_candidate_mask, water_mask

NDVI_VEGETATION_THRESHOLD = 0.3
NDVI_DROP_THRESHOLD = 0.15
TREES_P_MIN = 50   # probabilidad (0-100) mínima de 'trees' en t1
TREES_P_DROP = 40  # caída mínima de esa probabilidad en t2
CVA_SIGMAS = 2.0   # umbral del CVA: media + CVA_SIGMAS * desvío de la magnitud
CVA_MIN_NDVI_DROP = 0.20  # un evento cva con menos caída media de NDVI se descarta (compare_baselines): en 31 eventos revisados
                          # a ojo elimina los 22 falsos (estacionales) y pierde 3 de 9 reales, los tres menores de 1.100 m2


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
    # ndvi_diff ya excluye agua/construido/médano en t1;
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


DW_BUILT_BEFORE_MAX = 30  # probabilidad (0-100) máxima de 'built' en t1
DW_BUILT_AFTER_MIN = 40   # probabilidad mínima de 'built' en t2


def detect_loss_veg_to_built(image_t1, image_t2) -> Optional[numpy.ndarray]:
    """
    Baseline 4: cualquier vegetación (no solo leñosa: césped, pastizal) que
    pasa a construido. NDVI > umbral en t1 y probabilidad media de 'built' de
    Dynamic World por debajo de DW_BUILT_BEFORE_MAX en t1 y por encima de
    DW_BUILT_AFTER_MIN en t2. Es la señal de desarrollo inmobiliario sobre
    espacio verde. Requiere Dynamic World en ambos años; None si no aplica.
    """
    if image_t1.dw_built_p is None or image_t2.dw_built_p is None:
        return None
    bands_t1, _, shape = align_pair(image_t1, image_t2)
    veg_t1 = bands_t1["ndvi"] > NDVI_VEGETATION_THRESHOLD
    built_before = resample_mask(image_t1.dw_built_p < DW_BUILT_BEFORE_MAX, shape)
    built_after = resample_mask(image_t2.dw_built_p > DW_BUILT_AFTER_MIN, shape)
    return veg_t1 & built_before & built_after & resample_mask(vegetation_candidate_mask(image_t1), shape)


# La vegetación que se construye no es leñosa estable: detect_pair no le aplica esa máscara.
detect_loss_veg_to_built.woody_only = False


def filter_small_blobs(mask: numpy.ndarray, resolution_m: float, min_area_m2: float) -> numpy.ndarray:
    """
    Elimina blobs conectados más chicos que min_area_m2 (filtra falsos
    positivos por área mínima y conectividad).
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


SHIFT_BANDS = ("ndvi", "ndmi", "ndbi")


def measure_regional_shift(image_ref, image, inside=None):
    """
    Un año seco (o uno con otra fenología) baja el NDVI de toda la vegetación
    a la vez, y eso se leería como miles de pérdidas. La población de
    referencia es la leñosa estable de ref e image (Dynamic World; sin él,
    NDVI de ref sobre el umbral de vegetación), dentro de `inside` si se da.
    Return {banda: mediana de (ref - image)} para ndvi/ndmi/ndbi, o None si el
    NDVI no cayó (un aumento regional no se corrige: quitarlo marcaría como
    pérdida lo que no cambió), si hay menos de 100 píxeles de referencia o si
    los pares tienen distinta grilla.
    """
    ndvi_ref, ndvi = image_ref.bands["ndvi"], image.bands["ndvi"]
    if ndvi_ref.shape != ndvi.shape:
        return None
    woody_ref, woody = _woody(image_ref), _woody(image)
    if woody_ref is not None and woody is not None:
        reference = woody_ref & woody
    else:
        reference = vegetation_candidate_mask(image_ref)
    reference = reference & (ndvi_ref > NDVI_VEGETATION_THRESHOLD)
    if inside is not None:
        reference = reference & inside
    if reference.sum() < 100:
        return None
    shifts = {name: float(numpy.median(image_ref.bands[name][reference] - image.bands[name][reference])) for name in SHIFT_BANDS}
    return shifts if shifts["ndvi"] > 0 else None


def apply_regional_shift(image, shifts):
    """Copia de image con ndvi/ndmi/ndbi corridos por `shifts` (None = sin corregir)."""
    if shifts is None:
        return image
    corrected = copy.copy(image)
    corrected.bands = dict(image.bands)
    for name in SHIFT_BANDS:
        corrected.bands[name] = numpy.clip(image.bands[name] + shifts[name], -1.0, 1.0)
    return corrected


def normalize_regional_shift(image_ref, image, inside=None):
    """Return (image corregida, caída mediana de NDVI >= 0; 0 si no se corrigió). Ver measure_regional_shift."""
    shifts = measure_regional_shift(image_ref, image, inside)
    return (apply_regional_shift(image, shifts), shifts["ndvi"]) if shifts else (image, 0.0)


def regional_shifts(series, area, aoi_geometry, pairs) -> dict:
    """
    {(año_ref, año): corrimientos} para los pares de `pairs` que existen en series. Sirve para medir el
    corrimiento en una región más grande que la zona de estudio (todo el partido) y pasárselo a detect_pair:
    un cambio real y amplio de la zona no puede restarse a sí mismo como si fuera clima.
    """
    result = {}
    for year_ref, year in pairs:
        if year_ref not in series or year not in series:
            continue
        image = series[year]
        shifts = measure_regional_shift(series[year_ref], image, aoi_mask(area, aoi_geometry, image.bands["ndvi"].shape))
        if shifts:
            result[(year_ref, year)] = shifts
    return result


def pair_regional_shift(shifts, series, year_ref, year, area, aoi_geometry):
    """Corrimiento del par: el dado (de una región más grande) o, si shifts es None, el medido dentro de aoi_geometry."""
    if shifts is not None:
        return shifts.get((year_ref, year))
    image = series[year]
    return measure_regional_shift(series[year_ref], image, aoi_mask(area, aoi_geometry, image.bands["ndvi"].shape))


def detect_pair(detect_fn, series, year_from, year_to, area, aoi_geometry, min_area_m2, min_persistence_years=2, shifts=None):
    """
    Corre un baseline sobre (year_from, year_to) y lo refina: corrimiento
    regional de NDVI (`shifts` de una región más grande, ver regional_shifts;
    si es None se mide dentro de aoi_geometry), leñosa estable (año previo;
    no para baselines con woody_only = False), confirmación en el año
    siguiente (si min_persistence_years >= 2 y hay un año siguiente de la
    misma resolución), recorte a la zona y área mínima. series: {año: imagen}
    con años de contexto (year_from - 1, year_to + 1) si existen. Return
    (mask, confirmed), o (None, False) si el baseline no aplica al par.
    """
    image_t1, image_t2 = series[year_from], series[year_to]
    image_t2 = apply_regional_shift(image_t2, pair_regional_shift(shifts, series, year_from, year_to, area, aoi_geometry))
    mask = detect_fn(image_t1, image_t2)
    if mask is None:
        return None, False

    if getattr(detect_fn, "woody_only", True):
        mask = mask & stable_woody(image_t1, series.get(year_from - 1), mask.shape)

    image_next = series.get(year_to + 1)
    confirmed = min_persistence_years >= 2 and image_next is not None and image_next.resolution == image_t1.resolution
    if confirmed:
        # el NDVI sigue por debajo del de t1: la pérdida no se recuperó (cosecha, estacionalidad)
        image_next = apply_regional_shift(image_next, pair_regional_shift(shifts, series, year_from, year_to + 1, area, aoi_geometry))
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
        image.bands["ndmi"] = numpy.zeros(shape, dtype=numpy.float32)
        image.bands["ndbi"] = numpy.zeros(shape, dtype=numpy.float32)
        return image

    prev, t1, t2, nxt = make_image(0.7), make_image(0.7), make_image(0.7), make_image(0.7)
    t1.dw_label[14:18, 32:36] = DYNAMIC_WORLD_CLASSES.index("grass")  # pasto en t1: no es leñosa
    prev.dw_label[30:34, 32:36] = DYNAMIC_WORLD_CLASSES.index("grass")  # parpadeo: no era leñosa antes
    for rows, cols in [(slice(14, 18), slice(32, 36)), (slice(30, 34), slice(32, 36)), (slice(20, 30), slice(20, 30)),
                       (slice(2, 12), slice(20, 30)), (slice(2, 12), slice(5, 15)), (slice(36, 38), slice(36, 38))]:
        t2.bands["ndvi"][rows, cols] = 0.3  # el NDVI cae solo en los bloques que detect_all marca
        nxt.bands["ndvi"][rows, cols] = 0.3
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

    # año seco: todo el NDVI baja 0.2 y un bloque cae de verdad a 0.1
    dry = make_image(0.5)
    dry.bands["ndvi"][2:12, 20:30] = 0.1
    assert detect_loss_ndvi_diff(t1, dry).all(), "sin corregir, la caída regional marca todo"
    corrected, shift = normalize_regional_shift(t1, dry)
    assert abs(shift - 0.2) < 1e-6, shift
    loss = detect_loss_ndvi_diff(t1, corrected)
    assert loss[2:12, 20:30].all() and loss.sum() == 100, int(loss.sum())
    wet = make_image(0.9)  # aumento regional: no se corrige
    assert normalize_regional_shift(t1, wet) == (wet, 0.0)

    # el corrimiento medido en una región más grande: un cambio zonal real no se resta a sí mismo
    before, drop = make_image(0.7), make_image(0.7)
    drop.bands["ndvi"][:] = 0.4  # toda la zona cae 0.3 (cambio real y amplio de la zona)
    zonal_series = {2019: before, 2020: before, 2021: drop, 2022: drop}
    zone_aoi = box(0.0, 0.0, 1.0, 1.0)
    local, _ = detect_pair(detect_loss_ndvi_diff, zonal_series, 2020, 2021, area, zone_aoi, 1000)
    assert not local.any(), "medido dentro de la zona, la caída se resta a sí misma"
    mask, _ = detect_pair(detect_loss_ndvi_diff, zonal_series, 2020, 2021, area, zone_aoi, 1000, shifts={(2020, 2021): {"ndvi": 0.05, "ndmi": 0.0, "ndbi": 0.0}})
    assert mask.all(), "con un corrimiento regional chico (0.05) la pérdida de la zona se conserva"
    assert regional_shifts({2020: before, 2021: drop}, area, zone_aoi, [(2020, 2021), (2020, 2030)]).keys() == {(2020, 2021)}

    # vegetación (césped, no leñosa) que pasa a construido: no pasa por la máscara leñosa
    g1, g2, g3 = make_image(0.7), make_image(0.7), make_image(0.7)
    for image in (g1, g2, g3):
        image.dw_built_p = numpy.full(shape, 2, dtype=numpy.uint8)
    g1.dw_label[2:6, 20:24] = DYNAMIC_WORLD_CLASSES.index("grass")
    for image in (g2, g3):
        image.bands["ndvi"][2:6, 20:24] = 0.1
        image.dw_built_p[2:6, 20:24] = 90
    built_series = {2019: g1, 2020: g1, 2021: g2, 2022: g3}
    mask, confirmed = detect_pair(detect_loss_veg_to_built, built_series, 2020, 2021, area, aoi, 1000)
    assert confirmed and mask[2:6, 20:24].all() and mask.sum() == 16, int(mask.sum())
    g1.dw_built_p = None
    assert detect_loss_veg_to_built(g1, g2) is None
    print("detection selftest OK")


if __name__ == "__main__":
    _selftest()
