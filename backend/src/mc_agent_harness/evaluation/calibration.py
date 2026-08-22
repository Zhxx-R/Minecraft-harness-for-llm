from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class CreativeCalibrationResult:
    """Threshold and diagnostics produced by deterministic one-dimensional K-means."""

    threshold: float
    lower_centroid: float
    upper_centroid: float
    sample_count: int
    iterations: int
    f1: float | None = None
    accuracy: float | None = None

    def to_json(self) -> dict[str, Any]:
        """Convert calibration diagnostics into a JSON-safe payload."""

        return asdict(self)


def calibrate_creative_threshold(
    scores: Iterable[float],
    *,
    human_labels: Iterable[bool] | None = None,
    max_iterations: int = 100,
) -> CreativeCalibrationResult:
    """Fit K=2 to trajectory scores and use the centroid midpoint as decision boundary."""

    values = [float(score) for score in scores]
    if len(values) < 2 or min(values) == max(values):
        raise ValueError("Calibration requires at least two distinct trajectory scores.")
    labels = list(human_labels) if human_labels is not None else None
    if labels is not None and len(labels) != len(values):
        raise ValueError("human_labels length must match scores length.")
    lower = min(values)
    upper = max(values)
    iterations = 0
    for iterations in range(1, max_iterations + 1):
        lower_cluster: list[float] = []
        upper_cluster: list[float] = []
        for score in values:
            destination = lower_cluster if abs(score - lower) <= abs(score - upper) else upper_cluster
            destination.append(score)
        if not lower_cluster or not upper_cluster:
            raise ValueError("K-means produced an empty cluster; add more varied calibration runs.")
        next_lower = sum(lower_cluster) / len(lower_cluster)
        next_upper = sum(upper_cluster) / len(upper_cluster)
        if abs(next_lower - lower) < 1e-12 and abs(next_upper - upper) < 1e-12:
            lower, upper = next_lower, next_upper
            break
        lower, upper = next_lower, next_upper
    lower, upper = sorted((lower, upper))
    threshold = (lower + upper) / 2
    f1, accuracy = _classification_metrics(values, labels, threshold)
    return CreativeCalibrationResult(
        threshold=threshold,
        lower_centroid=lower,
        upper_centroid=upper,
        sample_count=len(values),
        iterations=iterations,
        f1=f1,
        accuracy=accuracy,
    )


def _classification_metrics(
    scores: list[float],
    labels: list[bool] | None,
    threshold: float,
) -> tuple[float | None, float | None]:
    """Compute optional F1 and accuracy against human trajectory labels."""

    if labels is None:
        return None, None
    predictions = [score > threshold for score in scores]
    true_positive = sum(prediction and label for prediction, label in zip(predictions, labels))
    false_positive = sum(prediction and not label for prediction, label in zip(predictions, labels))
    false_negative = sum(not prediction and label for prediction, label in zip(predictions, labels))
    denominator = 2 * true_positive + false_positive + false_negative
    f1 = (2 * true_positive / denominator) if denominator else 0.0
    accuracy = sum(prediction == label for prediction, label in zip(predictions, labels)) / len(labels)
    return f1, accuracy
