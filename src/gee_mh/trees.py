"""Conversión de pérdida de copa a árboles estimados.

La métrica primaria y defendible es m2/ha de copa perdida. La conversión a
"número de árboles" solo tiene sentido con una densidad calibrada
localmente (inventario municipal, parcelas de campo, o conteo en imagen de
alta resolución). Sin esa calibración, se entrega como escenario/rango
usando densidades de referencia bibliográficas, nunca como conteo exacto.
"""

from dataclasses import dataclass

# NOTE: densidades de referencia (árboles/ha) sin calibrar a Monte
# Hermoso todavía - documentan un rango plausible, no una medición local.
# Upgrade: reemplazar por densidad medida en parcelas de campo o inventario
# municipal y marcar is_calibrated=True.
REFERENCE_DENSITY_URBAN = (40, 120)  # árboles/ha, arbolado urbano/costero disperso
REFERENCE_DENSITY_NATURAL = (150, 400)  # árboles/ha, monte/matorral natural denso


@dataclass
class TreeEstimate:
    area_m2: float
    area_ha: float
    trees_low: float
    trees_high: float
    is_calibrated: bool

    def __str__(self):
        calibration = "calibrado" if self.is_calibrated else "ESCENARIO, no calibrado localmente"
        return (
            f"{self.area_ha:.2f} ha perdidas -> {self.trees_low:.0f}-{self.trees_high:.0f} "
            f"árboles estimados ({calibration})"
        )


def estimate_trees(
    area_m2: float,
    is_urban: bool,
    density_range_trees_per_ha: tuple = None,
    is_calibrated: bool = False,
) -> TreeEstimate:
    """
    area_m2: superficie de copa perdida
    is_urban: separa vegetación urbana de vegetación natural (densidades muy
    distintas)
    density_range_trees_per_ha: (low, high) árboles/ha; si no se da, usa la
    densidad de referencia sin calibrar y is_calibrated se fuerza a False
    """
    if density_range_trees_per_ha is None:
        density_range_trees_per_ha = REFERENCE_DENSITY_URBAN if is_urban else REFERENCE_DENSITY_NATURAL
        is_calibrated = False

    area_ha = area_m2 / 10_000.0
    low, high = density_range_trees_per_ha
    return TreeEstimate(
        area_m2=area_m2,
        area_ha=area_ha,
        trees_low=area_ha * low,
        trees_high=area_ha * high,
        is_calibrated=is_calibrated,
    )


if __name__ == "__main__":
    estimate = estimate_trees(area_m2=5000, is_urban=True)
    assert not estimate.is_calibrated
    assert estimate.trees_low < estimate.trees_high
    print(estimate)
