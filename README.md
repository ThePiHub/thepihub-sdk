# ThePiHub SDK

The Python SDK for [ThePiHub](https://thepihub.com): connect a Raspberry Pi (or any Linux machine) to a web dashboard.

```bash
pip install thepihub
```

The package is called `thepihub`, you `import pihub`, and the command is `pihub`. **Start with [the SDK guide](sdk/pihub/README.md)**: what it does,
how to install it, your first run and the concepts, with code.

| Folder | What is in it |
|---|---|
| [`sdk/pihub/`](sdk/pihub/) | the SDK itself |
| [`examples/`](examples/) | small programs to copy, each connecting to the real platform; start with `widget_showcase.py`, which shows every widget |
| [`apps/`](apps/) | complete programs to run as they are: a live bird-box camera, a GPIO panel, a motion-triggered camera trap |
| [`tests/`](tests/) | the SDK's tests |

## Working on the SDK

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[gpio]" pytest
python -m pytest tests -q
```

Use `python -m pytest`, not bare `pytest`: the tests import the programs in `examples/`.

This repository is a copy of the SDK published from ThePiHub's main repository.

## Licence

Apache License 2.0; see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
