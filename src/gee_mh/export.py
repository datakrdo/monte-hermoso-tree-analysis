"""Exportes finales: GeoTIFF de pérdida, GeoJSON de
eventos y CSV anual por causa. Sin API/frontend en este entregable.
"""

from pathlib import Path
from typing import List

import geopandas
import numpy
import pandas
import rasterio
from rasterio.features import shapes as rasterio_shapes
from rasterio.transform import from_bounds
from shapely.geometry import shape

from gee_mh.preprocessing import LongLatBBox


def mask_to_polygons(area: LongLatBBox, mask: numpy.ndarray):
    """
    Convierte una máscara booleana (en la grilla común de un par de años, ver
    detection.align_pair) en polígonos shapely en WGS84, indexando por la forma
    real de la máscara y no por el tamaño de la imagen, que puede no coincidir
    tras el remuestreo entre sensores. La fila 0 es el borde norte. Los
    polígonos siguen los bordes de los píxeles (un bloque de 5x5 píxeles da
    25 píxeles de área, con sus huecos), y cada región conectada en 8
    direcciones es un solo polígono.
    """
    height, width = mask.shape
    transform = from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, width, height)
    return [shape(geometry) for geometry, _ in rasterio_shapes(
        mask.astype(numpy.uint8), mask=mask.astype(bool), connectivity=8, transform=transform)]


def export_events_geojson(events: List[dict], out_path: Path) -> geopandas.GeoDataFrame:
    """
    events: lista de dicts con al menos 'geometry' (shapely), 'year_from',
    'year_to', 'area_m2', 'cause', 'confidence'.
    """
    gdf = geopandas.GeoDataFrame(events, crs="EPSG:4326")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_path, driver="GeoJSON")
    return gdf


def export_annual_summary_csv(events_gdf: geopandas.GeoDataFrame, out_path: Path) -> pandas.DataFrame:
    """
    events_gdf: salida de export_events_geojson (o equivalente), con
    columnas 'year_to', 'cause', 'area_m2', y opcionalmente 'trees_low'/
    'trees_high' (ver trees.py - siempre un rango, no un conteo).
    """
    agg = {"area_m2": ["sum", "count"]}
    if "trees_low" in events_gdf and "trees_high" in events_gdf:
        agg["trees_low"] = ["sum"]
        agg["trees_high"] = ["sum"]

    group_cols = ["year_to", "cause"]
    if "sensor_change" in events_gdf:
        # Separa los pares año con cambio de sensor: su área no es
        # comparable con pares del mismo sensor (ver pipeline.py).
        group_cols.append("sensor_change")

    summary = events_gdf.groupby(group_cols).agg(agg)
    summary.columns = ["_".join(c) for c in summary.columns]
    summary = summary.rename(columns={"area_m2_sum": "area_m2_total", "area_m2_count": "n_eventos"}).reset_index()
    summary["area_ha_total"] = summary["area_m2_total"] / 10_000.0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out_path, index=False)
    return summary


def export_loss_geotiff(mask: numpy.ndarray, area: LongLatBBox, out_path: Path) -> None:
    """
    Exporta la máscara de pérdida como GeoTIFF de una banda (uint8, 0/1) en
    WGS84. Para un COG real, agregar `driver="COG"` si la versión de GDAL
    instalada lo soporta; con GTiff simple + overviews alcanza para revisión.
    """
    height, width = mask.shape
    transform = from_bounds(area.long_from, area.lat_from, area.long_to, area.lat_to, width, height)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(mask.astype(numpy.uint8), 1)


if __name__ == "__main__":
    import tempfile

    area = LongLatBBox(-61.3, -61.29, -38.96, -38.95)
    mask = numpy.zeros((20, 20), dtype=bool)
    mask[5:10, 5:10] = True

    polygons = mask_to_polygons(area, mask)
    assert len(polygons) == 1
    pixel_area = (area.long_to - area.long_from) / 20 * (area.lat_to - area.lat_from) / 20
    assert abs(polygons[0].area - 25 * pixel_area) < 1e-12, polygons[0].area / pixel_area  # 5x5 píxeles, no 4x4
    ring = mask.copy()
    ring[7, 7] = False  # un hueco no es otro evento
    assert len(mask_to_polygons(area, ring)) == 1
    diagonal = numpy.zeros((20, 20), dtype=bool)
    diagonal[1, 1] = diagonal[2, 2] = True  # conectados en diagonal: un solo evento
    assert len(mask_to_polygons(area, diagonal)) == 1
    north = numpy.zeros((20, 20), dtype=bool)
    north[0:5, :] = True  # fila 0 = borde norte
    assert mask_to_polygons(area, north)[0].centroid.y > area.lat_from + 0.5 * (area.lat_to - area.lat_from)

    with tempfile.TemporaryDirectory() as tmp:
        tif_path = Path(tmp) / "loss.tif"
        export_loss_geotiff(mask, area, tif_path)
        with rasterio.open(tif_path) as src:
            assert src.read(1).sum() == mask.sum()

        events = [{"geometry": polygons[0], "year_from": 2020, "year_to": 2021, "area_m2": 2500.0, "cause": "desconocido", "confidence": 0.0}]
        geojson_path = Path(tmp) / "events.geojson"
        gdf = export_events_geojson(events, geojson_path)
        assert geojson_path.exists()

        csv_path = Path(tmp) / "summary.csv"
        summary = export_annual_summary_csv(gdf, csv_path)
        assert csv_path.exists()
        assert summary.iloc[0]["n_eventos"] == 1

    print("export OK")
