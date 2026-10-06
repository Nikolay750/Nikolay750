"""Тонкий мост к smeta_check.pipeline — чтобы checker.py не завязывался на
то, где физически лежит модуль движка, и чтобы в тестах его можно было
подменить одной точкой (mock.patch("app.checker.run_pipeline_for_package"))
без необходимости знать внутренности smeta_check."""

from __future__ import annotations

from smeta_check.pipeline import run_package as _run_package


def run_pipeline_for_package(paths: list[str], out_dir: str) -> dict:
    return _run_package(paths, out_dir)
