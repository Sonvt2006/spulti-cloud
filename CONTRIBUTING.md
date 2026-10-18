# Contributing to Spulti-cloud

## Development setup

The project uses two config files for code quality:

- `pyproject.toml` configures [Ruff](https://docs.astral.sh/ruff/) (linting and import sorting) and [mypy](https://mypy.readthedocs.io/) (strict type checking).
- `.pre-commit-config.yaml` configures [pre-commit](https://pre-commit.com/), which runs those checks automatically before every commit.


### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install the tools

```bash
pip install ruff mypy pre-commit
```

### 3. Enable the pre-commit hooks

```bash
pre-commit install
```

This reads `.pre-commit-config.yaml` and installs a git hook, so the checks run on every `git commit`. To run them once against the whole repository:

```bash
pre-commit run --all-files
```

## Before opening a pull request

Make sure `pre-commit run --all-files` passes with no errors.
