"""The SDK is published as `thepihub` (`pihub` on PyPI belongs to someone else) while `import pihub` and the `pihub` command stay."""

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which the SDK supports, has no tomllib: `tomli` is the same thing
    import tomli as tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(path):
    return tomllib.loads((REPO / path).read_text())


def test_the_distribution_is_thepihub_and_the_import_name_is_unchanged():
    config = load("pyproject.toml")
    assert config["project"]["name"] == "thepihub"
    assert config["project"]["scripts"] == {"pihub": "pihub.cli:main"}
    assert config["tool"]["setuptools"]["packages"]["find"]["where"] == ["sdk"]
    assert (REPO / "sdk" / "pihub" / "__init__.py").is_file()


def test_the_extras_named_in_install_hints_exist():
    extras = load("pyproject.toml")["project"]["optional-dependencies"]
    assert {"webrtc", "gpio"} <= set(extras)


def test_the_device_reads_its_version_from_the_distribution_name():
    from pihub import device_information

    source = Path(device_information.__file__).read_text()
    assert 'version("thepihub")' in source
    assert 'version("pihub")' not in source


def test_the_readme_pypi_shows_is_the_sdk_readme_and_exists():
    readme = load("pyproject.toml")["project"]["readme"]
    assert readme == {"file": "sdk/pihub/README.md", "content-type": "text/markdown"}
    assert (REPO / readme["file"]).is_file()
