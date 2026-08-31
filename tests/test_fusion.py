import json

import pytest
from test_oracle import trace

from divergence.fusion import FEATURES, predict, train
from divergence.models import LinearFusion
from divergence.oracle import compare


def model(weight=1, intercept=0):
    return LinearFusion(
        feature_version=2,
        feature_names=FEATURES,
        means=[0.0] * len(FEATURES),
        scales=[1.0] * len(FEATURES),
        weights=[weight] * len(FEATURES),
        intercept=intercept,
    )


def test_json_weights_and_abstention():
    assert predict(dict.fromkeys(FEATURES, 0.0), model())[0] == "INCONCLUSIVE"
    assert predict(dict.fromkeys(FEATURES, 1.0), model())[0] == "BAD"
    assert predict(dict.fromkeys(FEATURES, 0.0), model(intercept=-4))[0] == "GOOD"
    with pytest.raises(ValueError):
        LinearFusion(
            feature_version=2, feature_names=["a"], means=[0], scales=[0], weights=[1], intercept=0
        )


def test_assertions_override_model_good(scenario, tmp_path):
    scenario.policy.mode = "learned"
    scenario.policy.fusion = model(weight=0, intercept=-4)
    assert compare(trace(), trace(False), scenario, tmp_path, tmp_path).verdict == "BAD"
    assert compare(trace(), trace(), scenario, tmp_path, tmp_path).verdict == "GOOD"


def test_unrelated_early_difference_is_not_bug_onset(scenario, tmp_path):
    a, b = trace(), trace()
    b.samples[0].state["x"] = 4.0
    b.samples[-1].state["door"]["open"] = False
    result = compare(a, b, scenario, tmp_path, tmp_path)
    assert result.first_observed_difference_ms == 0
    assert result.first_divergence_ms == 200


def test_train_uses_grouped_folds_and_portable_model(tmp_path):
    pytest.importorskip("sklearn")
    examples = []
    for group in ("a", "b", "c"):
        for variant in range(3):
            for label in (0, 1):
                examples.append(
                    {
                        "id": f"{group}-{variant}-{label}",
                        "group": group,
                        "label": label,
                        "features": dict.fromkeys(FEATURES, label + variant * 0.01),
                        "rule_verdict": "BAD" if label else "GOOD",
                    }
                )
    dataset = tmp_path / "data.json"
    dataset.write_text(json.dumps({"feature_version": 2, "examples": examples}))
    result = train(dataset, tmp_path / "model")
    assert len(result["folds"]) == 3
    assert all(fold["training_count"] == 12 and fold["test_count"] == 6 for fold in result["folds"])
    exported = LinearFusion.model_validate_json((tmp_path / "model/model.json").read_text())
    assert predict(dict.fromkeys(FEATURES, 1.0), exported)[0] == "BAD"
    assert predict(dict.fromkeys(FEATURES, 0.0), exported)[0] == "GOOD"
