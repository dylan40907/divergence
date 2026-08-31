# Evaluation protocol

The benchmark executes actual versions of the bundled simulation, captures images and traces,
and extracts the same features used by the application. Labels come from documented fixture
mutations, never from the model's own verdict. It is **synthetic application data**, not evidence
of generalization to arbitrary projects.

```sh
divergence benchmark --output .divergence/benchmark
divergence train .divergence/benchmark/dataset.json --output .divergence/model
```

Six mutation families are held out one at a time: collision, slow motion, delayed switch,
missing event, added error log, and missing player appearance. Each has four variants and benign
controls. Preprocessing fits only on each training fold. The comparison reports precision,
recall, F1, confusion matrices, and every held-out fold alongside state-only, visual-only, and
combined-rule baselines. A final model fitted to all benchmark rows is exported separately.
Do not evaluate that fitted model on its own training rows and call the result generalization.

The initial 48-example evaluation (before improving discrete-event handling and local visual
pooling) produced learned recall 0.50/F1 0.667 and rule recall 0.667/F1 0.80, with no false positives
on these limited benign controls. That exposed the limitation of learning modalities from only
one family and the weakness of global pixel averaging. Re-run the benchmark after feature changes;
do not compare feature-incompatible cached datasets. Current results are in the generated
`evaluation.json`; they must not be represented as real-world performance claims.

Feature version 2, after those corrections, yielded the following 48-example results:

| Classifier | Precision | Recall | F1 |
| --- | --- | --- | --- |
| State-only | 1.000 | 0.500 | 0.667 |
| Visual-only (default threshold) | 0.000 | 0.000 | 0.000 |
| Combined rules | 1.000 | 0.833 | 0.909 |
| Learned fusion, held-out families | 1.000 | 0.667 | 0.800 |

The default visual threshold missed small-object changes; zero precision is the zero-division
convention when nothing was predicted positive, not evidence of many false alarms. Learned fusion
still missed the held-out missing-event and error-log families. These are reasons to keep explicit
assertions/default rules and gather representative independent data before promoting a learned oracle.
The fixture controls are narrow. No parameter tuning on this fixture should be presented as an
unbiased final benchmark. Dataset and model artifacts carry a feature version to reject incompatible
cached features.

The model is optional and experimental. Runtime predictions use an abstention band around the
linear decision boundary. Values are margins, not calibrated confidence probabilities. Explicit
assertion violations still force BAD. A successful self-comparison and stable repeated captures
remain required before bisection can use the model.

```sh
divergence bisect --repo demo-workspace/repository --good known-good --bad known-bad \
  --scenario demo-workspace/scenario.yaml --model .divergence/model/model.json
```

The model artifact is plain JSON (feature names, scaling, coefficients, intercept, provenance),
not executable pickle. Its complete content is embedded in the run's scenario and fingerprint.
