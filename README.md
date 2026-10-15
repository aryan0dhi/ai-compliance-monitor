# ai-compliance-monitor

## CI

`.github/workflows/ci.yml` runs on every push, every pull request and manual dispatch. Each job skips with a notice until the files it needs are tracked in git, then enforces normally.

| Job | Checks | Runs once |
|-----|--------|-----------|
| `python` | `ruff check`, `ruff format --check`, `mypy`, `pytest` with an 80% coverage floor, `alembic upgrade head` + `alembic check` against a Postgres service, `pip-audit` on `requirements.txt` | `requirements-dev.txt` is tracked (tests need `test_*.py`/`*_test.py`, Alembic needs `alembic.ini`). Tracked `.py` files without `requirements-dev.txt` fail. |
| `security` | gitleaks over the commits new in the push or PR (full history on manual dispatch), configured by `.gitleaks.toml` | always |
| `osv` | OSV-Scanner over tracked `requirements*.txt`, `uv.lock`, `poetry.lock`, `Pipfile.lock`; fails on any known vulnerability | a lockfile is tracked |
| `codeql` | CodeQL for `python` and `actions`; fails on any finding. The SARIF is uploaded as an artifact, and to the Security tab only if the repo is public | `actions` always, `python` once a `.py` file is tracked |
| `actionlint` | lints the workflow files | always |
| `container` | `docker build`, then Trivy on the Dockerfile and the image (fixable HIGH/CRITICAL fail) | a root `Dockerfile` is tracked |

Python dependencies are expected as hashed pip-tools lockfiles: `requirements.txt` (runtime) and `requirements-dev.txt` (dev, includes runtime via `-r`), generated with `pip-compile --generate-hashes`.

### Fixing a finding

- Secret (gitleaks): remove it and rotate it. Do not allowlist a real secret.
- Vulnerable package (OSV, pip-audit, Trivy): upgrade it.
- CodeQL: fix the flagged code; the rule and location are in the job log.

### Running the checks locally

```sh
pip install --require-hashes -r requirements-dev.txt
ruff check . && ruff format --check . && mypy .
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/app_test
pytest --cov --cov-fail-under=80
pip-audit -r requirements.txt --require-hashes --disable-pip --no-deps
gitleaks git --redact --verbose .
osv-scanner scan source -L requirements.txt -L requirements-dev.txt
```
