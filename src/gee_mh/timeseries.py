"""Serie temporal anual comparable.

Una imagen por año, tomada en la misma ventana estacional (verano austral,
menor nubosidad y mayor vigor de vegetación en la costa bonaerense). El modo
AUTO del preprocesador ya prioriza Sentinel-2 > Landsat 8 > Landsat 5, así
que para años sin Sentinel-2 (pre-2015) cae solo en Landsat sin configuración
extra.
"""

import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from gee_mh.preprocessing import Preprocessor, DataSource, SatelliteImage, LongLatBBox

# Día del año usado para todas las composiciones (15 de febrero = pleno
# verano austral). El preprocesador compone la mediana de las escenas en
# una ventana de +-30 días alrededor de esa fecha.
SEASONAL_MONTH_DAY = (2, 15)

MAX_WORKERS = 4


def year_to_date(year: int) -> str:
    month, day = SEASONAL_MONTH_DAY
    return f"{year}-{month:02d}-{day:02d}"


def build_annual_series(
    processor: Preprocessor,
    area: LongLatBBox,
    start_year: int,
    end_year: Optional[int] = None,
) -> Dict[int, SatelliteImage]:
    """
    Construye una composición estacional por año en [start_year, end_year].
    end_year=None usa el año actual.
    Return un dict {year: SatelliteImage}. Los años sin imagen disponible se
    omiten (no se agrega una imagen dummy) para no contaminar la serie con
    datos falsos - quedan registrados como huecos por el llamador.
    """
    end_year = end_year or datetime.date.today().year
    processor.data_source = DataSource.AUTO

    years = range(start_year, end_year + 1)
    # NOTE: 4 hilos fijos, prudente para la cuota Community de EE; subir si
    # el tier lo permite. Las descargas ya hechas salen del caché en disco.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        images = list(pool.map(lambda year: processor.get_satellite_data([year_to_date(year)], area)[0], years))

    series = {}
    for year, image in zip(years, images):
        # get_satellite_data devuelve una imagen dummy (todo ceros) cuando no
        # hay datos disponibles - se detecta por ancho/alto nulo de bandas.
        if image.bands["red"].max() == 0.0 and image.bands["red"].min() == 0.0:
            print(f"{year}: sin observación suficiente, se omite")
            continue
        series[year] = image
    return series


def build_series_with_context(
    processor: Preprocessor,
    area: LongLatBBox,
    start_year: int,
    end_year: int,
) -> Dict[int, SatelliteImage]:
    """
    build_annual_series con un año antes y uno después del rango: el previo
    sirve para la leñosa estable y el siguiente para confirmar la pérdida
    (ver detection.detect_pair). Quien itera pares debe quedarse con los de
    [start_year, end_year].
    """
    return build_annual_series(processor, area, start_year - 1, min(end_year + 1, datetime.date.today().year))


def missing_years(series: Dict[int, SatelliteImage], start_year: int, end_year: int) -> List[int]:
    return [y for y in range(start_year, end_year + 1) if y not in series]
