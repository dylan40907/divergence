"""Optional inspectable linear fusion. JSON weights only; no pickle deserialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from divergence.models import LinearFusion
from divergence.storage import atomic_json

FEATURES = [
    f"{channel}_{stat}"
    for channel in ("state", "visual", "events", "logs", "metrics", "motion")
    for stat in ("mean", "max")
]


def predict(features: dict[str, float], model: LinearFusion) -> tuple[str, float]:
    if set(model.feature_names) != set(FEATURES):
        raise ValueError("unsupported fusion feature schema")
    vector = np.asarray([features[name] for name in model.feature_names], dtype=float)
    margin = float(np.dot((vector - model.means) / model.scales, model.weights) + model.intercept)
    if not np.isfinite(margin):
        raise ValueError("fusion model returned a nonfinite score")
    verdict = (
        "BAD"
        if margin > model.abstain_margin
        else "GOOD"
        if margin < -model.abstain_margin
        else "INCONCLUSIVE"
    )
    return verdict, margin


def train(dataset: Path, output: Path) -> dict:
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import confusion_matrix, f1_score, precision_score, recall_score
        from sklearn.model_selection import LeaveOneGroupOut
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError("Install divergence-hunter[ml] to train fusion models") from exc
    data = json.loads(dataset.read_text(encoding="utf-8"))
    if data.get("feature_version") != 2:
        raise ValueError("dataset feature version is unsupported; regenerate with this runner")
    rows = data["examples"]
    if len(rows) < 12 or len({row["group"] for row in rows}) < 3:
        raise ValueError("need at least 12 examples across at least 3 independent groups")
    if any(type(row["label"]) is not int or row["label"] not in {0, 1} for row in rows):
        raise ValueError("labels must be integer 0 or 1")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("example IDs must be unique")
    x = np.asarray(
        [[row["features"][feature] for feature in FEATURES] for row in rows], dtype=float
    )
    y = np.asarray([row["label"] for row in rows], dtype=int)
    groups = np.asarray([row["group"] for row in rows])
    if not np.isfinite(x).all() or set(y.tolist()) != {0, 1}:
        raise ValueError("features must be finite and labels must include both 0 and 1")
    predicted = np.zeros(len(rows), dtype=int)
    folds = []
    for fit_indices, test_indices in LeaveOneGroupOut().split(x, y, groups):
        if len(set(y[fit_indices])) != 2:
            raise ValueError("each training fold must contain both labels")
        model = make_pipeline(
            StandardScaler(),
            LogisticRegression(C=1.0, class_weight="balanced", random_state=42, max_iter=2000),
        )
        model.fit(x[fit_indices], y[fit_indices])
        predicted[test_indices] = model.predict(x[test_indices])
        folds.append(
            {
                "held_out_group": str(groups[test_indices[0]]),
                "training_count": len(fit_indices),
                "test_count": len(test_indices),
                "correct": int((predicted[test_indices] == y[test_indices]).sum()),
            }
        )

    def metrics(labels):
        return {
            "precision": float(precision_score(y, labels, zero_division=0)),
            "recall": float(recall_score(y, labels, zero_division=0)),
            "f1": float(f1_score(y, labels, zero_division=0)),
            "confusion_matrix_true_rows_predicted_columns": confusion_matrix(
                y, labels, labels=[0, 1]
            ).tolist(),
        }

    baselines = {
        "state_only": [int(row["features"]["state_max"] > 0) for row in rows],
        "visual_only": [int(row["features"]["visual_max"] > 0.08) for row in rows],
        "any_signal": [int(row["rule_verdict"] == "BAD") for row in rows],
    }
    evaluation = {
        "schema_version": 1,
        "examples": len(rows),
        "evaluation": "leave-one-mutation-family-out",
        "learned_fusion": metrics(predicted),
        "baselines": {name: metrics(labels) for name, labels in baselines.items()},
        "folds": folds,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "limitations": [
            "Generated simulation fixtures, not a representative real-world benchmark.",
            "Scores are uncalibrated decision margins, not probabilities.",
            "Cross-validation metrics use zero margin; runtime also abstains near zero.",
            "Same-family variations and controls stay together within each evaluation fold.",
        ],
    }
    final = make_pipeline(
        StandardScaler(),
        LogisticRegression(C=1.0, class_weight="balanced", random_state=42, max_iter=2000),
    )
    final.fit(x, y)
    scaler, classifier = final.steps[0][1], final.steps[1][1]
    artifact = LinearFusion(
        feature_version=2,
        feature_names=FEATURES,
        means=scaler.mean_.tolist(),
        scales=scaler.scale_.tolist(),
        weights=classifier.coef_[0].tolist(),
        intercept=float(classifier.intercept_[0]),
        provenance={
            "dataset_sha256": evaluation["dataset_sha256"],
            "examples": len(rows),
            "warning": "Experimental model trained on synthetic simulation fixtures.",
        },
    )
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output / "model.json", artifact.model_dump())
    atomic_json(output / "evaluation.json", evaluation)
    return evaluation
