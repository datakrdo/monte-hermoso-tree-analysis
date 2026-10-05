"""Validación local (paso 8 del plan): métricas de detección y muestreo
estratificado de eventos para revisión manual contra imágenes de alta
resolución. Los umbrales se calibran solo con muestras de Monte Hermoso, no
se importan de otras regiones.
"""

import argparse
import math
from dataclasses import dataclass

import numpy
import pandas


@dataclass
class DetectionMetrics:
    precision: float
    recall: float
    f1: float
    iou: float
    tp: int
    fp: int
    fn: int
    tn: int


def compute_metrics(predicted_mask: numpy.ndarray, ground_truth_mask: numpy.ndarray) -> DetectionMetrics:
    """
    predicted_mask, ground_truth_mask: máscaras booleanas de la misma forma.
    """
    if predicted_mask.shape != ground_truth_mask.shape:
        raise ValueError("predicted_mask y ground_truth_mask deben tener la misma forma")

    tp = int((predicted_mask & ground_truth_mask).sum())
    fp = int((predicted_mask & ~ground_truth_mask).sum())
    fn = int((~predicted_mask & ground_truth_mask).sum())
    tn = int((~predicted_mask & ~ground_truth_mask).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    union = tp + fp + fn
    iou = tp / union if union > 0 else 0.0

    return DetectionMetrics(precision, recall, f1, iou, tp, fp, fn, tn)


def stratified_sample(
    events: pandas.DataFrame,
    strata_columns: list,
    n_per_stratum: int,
    random_state: int = 42,
) -> pandas.DataFrame:
    """
    events: una fila por evento detectado, con al menos las columnas en
    strata_columns (ej. ['year', 'cause', 'size_class']).
    Return una muestra para revisión manual, con hasta n_per_stratum eventos
    por combinación de estratos (menos si el estrato tiene pocos eventos).
    """
    # NOTE: no usar groupby().apply() acá - pandas >=2.2 descarta
    # silenciosamente las columnas de agrupamiento del resultado cuando la
    # función aplicada no las referencia (confirmado en pandas 3.0.5).
    samples = [g.sample(min(len(g), n_per_stratum), random_state=random_state) for _, g in events.groupby(strata_columns)]
    return pandas.concat(samples).reset_index(drop=True)


def size_class(area_m2: float) -> str:
    if area_m2 < 1_000:
        return "pequeño"
    if area_m2 < 10_000:
        return "mediano"
    return "grande"


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return float("nan"), float("nan")
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return center - half, center + half


def precision_by_stratum(labeled: pandas.DataFrame, strata=("baseline", "size_class")) -> pandas.DataFrame:
    """
    labeled: muestra de revisión manual con label_true_positive (1 = pérdida
    real, 0 = falso positivo; las filas sin etiquetar se ignoran). Return n,
    precisión e intervalo de Wilson 95% por estrato (n chico = intervalo ancho).
    """
    done = labeled[labeled["label_true_positive"].isin([0, 1])]
    rows = []
    for key, group in done.groupby(list(strata)):
        key = key if isinstance(key, tuple) else (key,)
        hits, n = int(group["label_true_positive"].sum()), len(group)
        low, high = _wilson_interval(hits, n)
        rows.append({**dict(zip(strata, key)), "n": n, "precision": hits / n, "ci95_low": low, "ci95_high": high})
    return pandas.DataFrame(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--labels", help="CSV de revisión manual etiquetado (label_true_positive 1/0): imprime la precisión por estrato")
    args = parser.parse_args()
    if args.labels:
        labeled = pandas.read_csv(args.labels)
        print(f"etiquetadas: {labeled['label_true_positive'].isin([0, 1]).sum()} de {len(labeled)}")
        print(precision_by_stratum(labeled).round(3).to_string(index=False))
        raise SystemExit

    pred = numpy.array([[True, True, False], [False, True, False]])
    gt = numpy.array([[True, False, False], [False, True, True]])
    m = compute_metrics(pred, gt)
    assert m.tp == 2 and m.fp == 1 and m.fn == 1 and m.tn == 2
    assert abs(m.iou - 2 / 4) < 1e-9
    print(m)

    events = pandas.DataFrame({"stratum": ["a", "a", "a", "b"], "val": [1, 2, 3, 4]})
    sample = stratified_sample(events, ["stratum"], n_per_stratum=2)
    assert "stratum" in sample.columns, "las columnas de estrato no deben perderse"
    assert len(sample) == 3  # 2 de 'a' (de 3 disponibles) + 1 de 'b' (de 1 disponible)
    print("stratified_sample OK")

    labeled = pandas.DataFrame({
        "baseline": ["a"] * 4 + ["b"] * 2,
        "size_class": ["x"] * 6,
        "label_true_positive": [1, 1, 0, numpy.nan, 0, 0],  # NaN = sin etiquetar
    })
    result = precision_by_stratum(labeled).set_index("baseline")
    assert result.loc["a", "n"] == 3 and abs(result.loc["a", "precision"] - 2 / 3) < 1e-9
    assert result.loc["b", "precision"] == 0.0 and result.loc["b", "ci95_high"] < 0.9  # Wilson, n=2
    print("precision_by_stratum OK")
