"""Contraste de los eventos detectados con desarrollos inmobiliarios conocidos
(data/reference/desarrollos_inmobiliarios.csv, de prensa y fuentes municipales).

Por cada desarrollo con coordenadas cuenta los eventos a menos de RADIUS_M. No
es una validación de precisión: no hay límites de los predios, los puntos son
aproximados y el año de inicio muchas veces falta. Responde "¿hay cambios
detectados donde se sabe que hubo un desarrollo?" y deja afuera lo que no se
puede ubicar.

Uso:
    PYTHONPATH=src python3 -m gee_mh.developments
"""

import geopandas
import numpy
import pandas
import shapely

from gee_mh.config import PROJECT_ROOT

RADIUS_M = 300
YEAR_TOLERANCE = 2  # un evento "coincide en el tiempo" si year_to cae a +-2 años del inicio conocido
METRIC_CRS = "EPSG:32720"
REFERENCE = PROJECT_ROOT / "data" / "reference" / "desarrollos_inmobiliarios.csv"
POLYGONS = PROJECT_ROOT / "data" / "reference" / "desarrollos_poligonos.geojson"
EVENTS = PROJECT_ROOT / "data" / "exports" / "events_by_baseline.geojson"


def count_events_near(developments: pandas.DataFrame, events: geopandas.GeoDataFrame, radius_m: float = RADIUS_M,
                      polygons: geopandas.GeoDataFrame | None = None) -> pandas.DataFrame:
    """
    developments: name, lat, lon, year_start (texto o número, puede faltar). events: GeoDataFrame con baseline,
    year_to, confirmed y area_m2. polygons: límites con columna name; si el nombre coincide se mide contra el
    polígono (distancia 0 adentro) y no contra el punto. Return una fila por desarrollo con coordenadas: eventos a menos de radius_m,
    de ellos los confirmados y los de `veg_to_built`, hectáreas, y los que coinciden en el tiempo con el inicio.
    """
    located = developments.dropna(subset=["lat", "lon"]).copy()
    if located.empty:
        return pandas.DataFrame()
    points = geopandas.GeoDataFrame(located, geometry=geopandas.points_from_xy(located["lon"], located["lat"]), crs="EPSG:4326").to_crs(METRIC_CRS)
    if polygons is not None:
        shapes = polygons.to_crs(METRIC_CRS).set_index("name").geometry
        points["shape"] = [shapes.get(name) for name in points["name"]]
        points["geometry"] = [s if s is not None else p for s, p in zip(points["shape"], points.geometry)]
    metric_events = events.to_crs(METRIC_CRS)
    rows = []
    for _, dev in points.iterrows():
        near = metric_events[metric_events.geometry.distance(dev.geometry) <= radius_m]
        start = pandas.to_numeric(str(dev.get("year_start", "")).split("-")[0], errors="coerce")
        in_time = near[(near["year_to"] - start).abs() <= YEAR_TOLERANCE] if not numpy.isnan(start) else near.iloc[0:0]
        confirmed = near[near["confirmed"]]
        rows.append({
            "name": dev["name"], "lat": dev["lat"], "lon": dev["lon"], "year_start": dev.get("year_start", ""),
            "geometry": "polígono" if polygons is not None and dev["name"] in set(polygons["name"]) else "punto",
            "events": len(near), "confirmed": len(confirmed),
            "veg_to_built": int((near["baseline"] == "veg_to_built").sum()),
            "development_cause": int((near["cause"] == "desarrollo_inmobiliario").sum()),
            "area_ha": round(near["area_m2"].sum() / 1e4, 2),
            "events_within_2_years_of_start": len(in_time) if not numpy.isnan(start) else None,
            "years": ",".join(str(y) for y in sorted(near["year_to"].unique())),
        })
    return pandas.DataFrame(rows)


def selftest():
    events = geopandas.GeoDataFrame({
        "baseline": ["veg_to_built", "ndvi_diff", "ndvi_diff"], "year_to": [2022, 2018, 2022], "confirmed": [True, True, False],
        "cause": ["desarrollo_inmobiliario", "desconocido", "desconocido"], "area_m2": [10_000.0, 2_000.0, 500.0],
    }, geometry=geopandas.points_from_xy([-61.29, -61.29, -61.20], [-38.98, -38.9805, -38.98]), crs="EPSG:4326")  # el tercero, a ~8 km
    developments = pandas.DataFrame({
        "name": ["cerca", "sin coordenadas"], "lat": [-38.98, None], "lon": [-61.29, None], "year_start": ["2022-2025", ""]})
    result = count_events_near(developments, events).set_index("name")
    assert list(result.index) == ["cerca"], "sin coordenadas no se cuenta"
    row = result.loc["cerca"]
    assert row["events"] == 2 and row["confirmed"] == 2 and row["veg_to_built"] == 1 and row["development_cause"] == 1, row
    assert row["events_within_2_years_of_start"] == 1, "solo el de 2022 está a +-2 años de 2022"
    assert abs(row["area_ha"] - 1.2) < 1e-9
    assert count_events_near(developments.iloc[1:], events).empty
    big = geopandas.GeoDataFrame({"name": ["cerca"]}, geometry=[shapely.box(-61.2905, -38.9805, -61.2895, -38.9795)], crs="EPSG:4326")
    far = events.iloc[[0]].copy()
    far.geometry = geopandas.points_from_xy([-61.2850], [-38.98])  # a ~450 m del punto, pero a ~450 m del polígono también
    assert count_events_near(developments, far).iloc[0]["events"] == 0
    far.geometry = geopandas.points_from_xy([-61.2898], [-38.9797])  # dentro del polígono, lejos del punto del CSV (>300 m)
    moved = developments.assign(lat=[-38.9830, None], lon=[-61.2898, None])
    assert count_events_near(moved, far).iloc[0]["events"] == 0 and count_events_near(moved, far, polygons=big).iloc[0]["events"] == 1
    assert count_events_near(moved, far, polygons=big).iloc[0]["geometry"] == "polígono"
    print("developments selftest OK")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        selftest()
        raise SystemExit
    developments = pandas.read_csv(REFERENCE)
    result = count_events_near(developments, geopandas.read_file(EVENTS), polygons=geopandas.read_file(POLYGONS))
    out = PROJECT_ROOT / "data" / "exports" / "developments_check.csv"
    result.to_csv(out, index=False)
    unlocated = developments[developments["lat"].isna()]["name"].tolist()
    print(result.to_string(index=False))
    print(f"Sin coordenadas (no se pudieron contrastar): {unlocated}")
    print(f"Exportado: {out}")
