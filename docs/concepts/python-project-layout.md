# Why the code lives in `src/`

## In one sentence

Putting your package inside `src/` forces your tests to run against the
*installed* package, which is the only way to find out your packaging is broken
before your users do.

## The problem it solves

Python decides where to import from by searching a list of directories called
`sys.path`. The first entry is usually **the directory you ran the command
from**.

Now consider the common layout:

```
myproject/
├── myproject/          ← the package
│   └── __init__.py
└── tests/
```

You run `pytest` from `myproject/`. Python looks in the current directory,
finds the folder `myproject/`, and imports it. Your tests pass.

But they passed for the wrong reason: they imported a **folder that happened to
be sitting there**, not the package as it would be installed. If your
`pyproject.toml` forgets to include a subpackage, or a data file, or gets the
package name wrong — your tests will never notice. Your users will.

## How it works, step by step

With the `src/` layout:

```
patchpilot/
├── src/
│   └── patchpilot/
│       └── __init__.py
└── tests/
```

Running from the project root, Python looks in the current directory and finds…
`src/`, which is not a package. There is no `patchpilot/` folder to accidentally
pick up.

So the import only works if the package has been **installed** — which
`poetry install` does, in editable mode. Now:

```
import patchpilot   →   src/patchpilot/__init__.py, via the install
```

The path goes through your packaging configuration. If that configuration is
wrong, the import fails immediately, in your editor, instead of silently
working locally and failing on PyPI.

You can check which one you got:

```bash
poetry run python -c "import patchpilot; print(patchpilot.__file__)"
# /Users/.../patchpilot/src/patchpilot/__init__.py
```

## In PatchPilot

- [`pyproject.toml`](../../pyproject.toml) —
  `packages = [{ include = "patchpilot", from = "src" }]` is what tells Poetry
  where to look.
- [`tests/unit/test_config.py`](../../tests/unit/test_config.py) — its docstring
  notes that the import succeeding at all is itself evidence the layout works.

## The lock file, briefly

`poetry.lock` records the **exact** version of every package, including the
dependencies of your dependencies. It is committed.

Without it, `poetry install` resolves versions fresh each time. A library
releases a new version next month, someone clones your repo, and it breaks —
with no change in your code and nothing in the git history to explain it.

## What goes wrong

**Forgetting to install.** With `src/`, `import patchpilot` fails until
`poetry install` has run. That is the feature, but it surprises people once.

**Two Pythons.** `python3` on this machine is 3.10; the project uses 3.13. Always
`poetry run <command>`, which uses the project's virtual environment.

**Adding a dependency by hand.** `pip install X` inside the venv does not update
`pyproject.toml` or the lock file, so it works for you and nobody else. Use
`poetry add X`.

## Interview questions

**Q: Why `src/` layout?**

Without it, `import mypackage` resolves to a directory in the current working
directory, so tests pass whether or not the package is correctly packaged. With
`src/`, the import must go through the installed package, so packaging bugs
surface at test time rather than at release.

**Q: Why commit the lock file for an application?**

Reproducibility. Without it, two installs a month apart can resolve different
versions and behave differently with no code change. For a *library* you would
not constrain consumers this way, but an application wants exact, repeatable
environments. (The lock file pins; `pyproject.toml` still declares the flexible
ranges.)

**Q: Poetry, uv, or pip + venv?**

Any of them, as long as there is a lock file. Poetry here because it was already
installed and the project is not large enough for install speed to matter. `uv`
is substantially faster and a reasonable default for new projects; plain
`pip` + `venv` has no lock file, which is the thing that actually matters.

## Official documentation

- [Python Packaging User Guide — src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) — the canonical explanation.
- [Poetry — Basic usage](https://python-poetry.org/docs/basic-usage/) — `add`, `install`, and the lock file.
