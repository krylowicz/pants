# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from pants.backend.scala.subsystems.scala import ScalaSubsystem
from pants.backend.scala.target_types import (
    ScalaJunitTestsGeneratorSourcesField,
    ScalaJunitTestsGeneratorTarget,
    ScalaSourcesGeneratorTarget,
    ScalatestTestsGeneratorSourcesField,
    ScalatestTestsGeneratorTarget,
)
from pants.core.goals.tailor import (
    AllOwnedSources,
    PutativeTarget,
    PutativeTargets,
    PutativeTargetsRequest,
)
from pants.engine.intrinsics import path_globs_to_paths
from pants.engine.rules import collect_rules, rule
from pants.engine.target import Target
from pants.engine.unions import UnionRule
from pants.source.filespec import FilespecMatcher
from pants.util.dirutil import group_by_dir
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class PutativeScalaTargetsRequest(PutativeTargetsRequest):
    pass


def classify_source_files(paths: Iterable[str]) -> dict[type[Target], set[str]]:
    """Returns a dict of target type -> files that belong to targets of that type."""
    scalatest_filespec_matcher = FilespecMatcher(ScalatestTestsGeneratorSourcesField.default, ())
    junit_filespec_matcher = FilespecMatcher(ScalaJunitTestsGeneratorSourcesField.default, ())
    scalatest_files = {
        path
        for path in paths
        if os.path.basename(path)
        in set(scalatest_filespec_matcher.matches([os.path.basename(path) for path in paths]))
    }
    junit_files = {
        path
        for path in paths
        if os.path.basename(path)
        in set(junit_filespec_matcher.matches([os.path.basename(path) for path in paths]))
    }
    sources_files = set(paths) - scalatest_files - junit_files
    return {
        ScalaJunitTestsGeneratorTarget: junit_files,
        ScalaSourcesGeneratorTarget: sources_files,
        ScalatestTestsGeneratorTarget: scalatest_files,
    }


@rule(level=LogLevel.DEBUG, desc="Determine candidate Scala targets to create")
async def find_putative_targets(
    req: PutativeScalaTargetsRequest,
    all_owned_sources: AllOwnedSources,
    scala_subsystem: ScalaSubsystem,
) -> PutativeTargets:
    putative_targets = []

    if scala_subsystem.tailor_source_targets:
        all_scala_files_globs = req.path_globs("*.scala")
        all_scala_files = await path_globs_to_paths(all_scala_files_globs)
        unowned_scala_files = set(all_scala_files.files) - set(all_owned_sources)
        classified_unowned_scala_files = classify_source_files(unowned_scala_files)
        for tgt_type, paths in classified_unowned_scala_files.items():
            for dirname, filenames in group_by_dir(paths).items():
                putative_targets.append(
                    PutativeTarget.for_target_type(
                        tgt_type, path=dirname, name=None, triggering_sources=sorted(filenames)
                    )
                )

    return PutativeTargets(putative_targets)


def rules():
    return [
        *collect_rules(),
        UnionRule(PutativeTargetsRequest, PutativeScalaTargetsRequest),
    ]
