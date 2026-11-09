# Feedback task starting environment

Upstream: https://github.com/zopefoundation/RestrictedPython
Upstream commit: `9d7aeb267e598c3eb270850c223b343ca5a5d2cb`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= tests
```

Observed baseline (2026-11-09): `302 passed, 10 skipped, 2 warnings in 0.31s`
Whole-command wall time: 0.731 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
