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


def last_summer_year(today: Optional[datetime.date] = None) -> int:
    """
    Último año cuyo verano ya se puede componer: la ventana de Landsat llega a
    mediados de abril y sus escenas tardan semanas en publicarse, y el caché se
    indexa por año, así que un compuesto incompleto quedaría guardado para
    siempre. Desde el 1 de mayo es el año actual; antes, el anterior.
    """
    today = today or datetime.date.today()
    return today.year if today.month >= 5 else today.year - 1


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
    end_year=None usa el último verano completo (last_summer_year).
    Return un dict {year: SatelliteImage}. Los años sin imagen disponible se
    omiten (no se agrega una imagen dummy) para no contaminar la serie con
    datos falsos - quedan registrados como huecos por el llamador.
    """
    end_year = end_year or last_summer_year()
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
    return build_annual_series(processor, area, start_year - 1, min(end_year + 1, last_summer_year()))


def missing_years(series: Dict[int, SatelliteImage], start_year: int, end_year: int) -> List[int]:
    return [y for y in range(start_year, end_year + 1) if y not in series]


if __name__ == "__main__":
    day = datetime.date
    assert last_summer_year(day(2027, 4, 30)) == 2026
    assert last_summer_year(day(2027, 5, 1)) == 2027
    assert last_summer_year(day(2027, 6, 1)) == 2027
    assert last_summer_year(day(2027, 1, 1)) == 2026
    print("timeseries selftest OK")
