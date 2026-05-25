"""Optional fastText-based quality classifier.

Wraps a fastText model (e.g. kenhktsui/llm-data-textbook-quality-fasttext-classifier-v2)
for CPU-only inference at ~2000 pages/sec. Gracefully returns None when the model
or fasttext library is unavailable.
"""

from __future__ import annotations

from pathlib import Path

_model = None
_model_loaded = False


class ClassifierUnavailable(Exception):
    pass


def _get_model(model_path: str | Path | None = None):
    """Load the fastText model. Raises ClassifierUnavailable if deps/model missing."""
    global _model, _model_loaded
    if _model_loaded:
        if _model is None:
            raise ClassifierUnavailable("fasttext model not available")
        return _model

    _model_loaded = True

    try:
        import fasttext  # noqa: F401
    except ImportError:
        raise ClassifierUnavailable(
            "fasttext not installed (pip install fasttext-wheel)"
        )

    if model_path is None:
        from platformdirs import user_data_dir

        model_path = Path(user_data_dir("vasco")) / "models" / "quality.bin"

    model_path = Path(model_path)
    if not model_path.is_file():
        raise ClassifierUnavailable(f"model not found at {model_path}")

    _model = fasttext.load_model(str(model_path))
    return _model


def reset() -> None:
    """Clear cached model (for testing)."""
    global _model, _model_loaded
    _model = None
    _model_loaded = False


def classify(text: str, *, model_path: str | Path | None = None) -> float | None:
    """Return a quality score from the classifier, or None if unavailable.

    Returns a float in [0, 1] where higher = higher quality (inverted from slop_score).
    Returns None if the classifier is not available (missing deps or model file).
    """
    try:
        model = _get_model(model_path)
    except ClassifierUnavailable:
        return None

    # fastText expects single-line input; collapse whitespace.
    line = " ".join(text.split())[:4096]
    predictions = model.predict(line)
    if not predictions or not predictions[0]:
        return None

    label = predictions[0][0]
    confidence = predictions[1][0] if len(predictions) > 1 else 0.5

    # Map labels to quality scores.
    # Common label schemes: __label__High, __label__Mid, __label__Low
    label_map = {
        "__label__High": 1.0,
        "__label__Mid": 0.5,
        "__label__Low": 0.0,
        "__label__cc": 0.3,
        "__label__wiki": 0.8,
        "__label__textbook": 1.0,
    }

    base = label_map.get(label, 0.5)
    # Weight by confidence: push toward 0.5 when confidence is low.
    return round(base * confidence + 0.5 * (1 - confidence), 4)
