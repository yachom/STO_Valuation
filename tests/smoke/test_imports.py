"""Smoke test: every module imports without error.

This validates the dependency graph (no cycles, no missing symbols) and the
basic structural invariants of the skeleton.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def _iter_module_names(package_name: str) -> list[str]:
    pkg = importlib.import_module(package_name)
    paths = getattr(pkg, "__path__", None)
    if paths is None:
        return [package_name]
    out = [package_name]
    for info in pkgutil.walk_packages(paths, prefix=f"{package_name}."):
        out.append(info.name)
    return out


@pytest.mark.parametrize(
    "package",
    [
        "common",
        "projects.commercial",
        "projects.music",
        "airflow.scripts",
    ],
)
def test_package_imports(package: str) -> None:
    for module_name in _iter_module_names(package):
        # Skip airflow DAG files — they expect an Airflow runtime, not pytest.
        if module_name.startswith("airflow.dags"):
            continue
        importlib.import_module(module_name)


def test_composition_registry_keys() -> None:
    import composition

    assert set(composition.PROFILE_REGISTRY) == {"commercial", "music"}


def test_commercial_profile_loads() -> None:
    from projects.commercial.profile import COMMERCIAL_PROFILE

    assert COMMERCIAL_PROFILE.asset_class.value == "commercial"


def test_music_profile_loads() -> None:
    from projects.music.profile import MUSIC_PROFILE

    assert MUSIC_PROFILE.asset_class.value == "music"


def test_commercial_factors_nonempty() -> None:
    from projects.commercial.domain.factors import COMMERCIAL_EVALUATION_FACTORS

    assert "vacancy_rate" in COMMERCIAL_EVALUATION_FACTORS
    assert "rental_yield" in COMMERCIAL_EVALUATION_FACTORS
