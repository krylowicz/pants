# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pants.backend.rust.target_types import RustPackageTarget
from pants.core.goals.tailor import PutativeTarget, PutativeTargets, PutativeTargetsRequest
from pants.engine.intrinsics import path_globs_to_paths
from pants.engine.rules import Rule, collect_rules, rule
from pants.engine.unions import UnionRule
from pants.util.dirutil import group_by_dir
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class PutativeRustTargetsRequest(PutativeTargetsRequest):
    pass


@rule(level=LogLevel.DEBUG, desc="Determine candidate Rust targets to create")
async def find_putative_rust_targets(request: PutativeRustTargetsRequest) -> PutativeTargets:
    all_cargo_toml_files = await path_globs_to_paths(request.path_globs("Cargo.toml"))
    putative_targets = [
        PutativeTarget.for_target_type(
            RustPackageTarget,
            path=dirname,
            name=None,
            triggering_sources=sorted(filenames),
        )
        for dirname, filenames in group_by_dir(all_cargo_toml_files.files).items()
    ]
    return PutativeTargets(putative_targets)


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        UnionRule(PutativeTargetsRequest, PutativeRustTargetsRequest),
    )
