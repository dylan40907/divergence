# Contributing

Changes to classification, execution, persistence or security boundaries need tests.

```sh
python -m pip install -e ".[dev,ml,browser,pygame]"
python -m ruff check src tests
python -m pytest -q
```

Browser tests require Chromium and `RUN_BROWSER_TESTS=1`. Keep revision providers independent from
runtime adapters, and version contracts and features when their meaning changes.
