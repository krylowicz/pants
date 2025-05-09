# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from dataclasses import dataclass

from pants.backend.codegen.thrift.apache.python import subsystem
from pants.backend.codegen.thrift.apache.python.additional_fields import ThriftPythonResolveField
from pants.backend.codegen.thrift.apache.python.subsystem import ThriftPythonSubsystem
from pants.backend.codegen.thrift.apache.rules import (
    GenerateThriftSourcesRequest,
    generate_apache_thrift_sources,
)
from pants.backend.codegen.thrift.target_types import ThriftDependenciesField, ThriftSourceField
from pants.backend.codegen.utils import find_python_runtime_library_or_raise_error
from pants.backend.python.dependency_inference.module_mapper import (
    PythonModuleOwnersRequest,
    map_module_to_address,
)
from pants.backend.python.dependency_inference.subsystem import (
    AmbiguityResolution,
    PythonInferSubsystem,
)
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import PythonSourceField
from pants.engine.fs import AddPrefix
from pants.engine.intrinsics import digest_to_snapshot
from pants.engine.rules import collect_rules, implicitly, rule
from pants.engine.target import (
    FieldSet,
    GeneratedSources,
    GenerateSourcesRequest,
    InferDependenciesRequest,
    InferredDependencies,
)
from pants.engine.unions import UnionRule
from pants.source.source_root import SourceRootRequest, get_source_root
from pants.util.logging import LogLevel


class GeneratePythonFromThriftRequest(GenerateSourcesRequest):
    input = ThriftSourceField
    output = PythonSourceField


@rule(desc="Generate Python from Thrift", level=LogLevel.DEBUG)
async def generate_python_from_thrift(
    request: GeneratePythonFromThriftRequest,
    thrift_python: ThriftPythonSubsystem,
) -> GeneratedSources:
    result = await generate_apache_thrift_sources(
        GenerateThriftSourcesRequest(
            thrift_source_field=request.protocol_target[ThriftSourceField],
            lang_id="py",
            lang_options=thrift_python.gen_options,
            lang_name="Python",
        ),
        **implicitly(),
    )

    # We must add back the source root for Python imports to work properly. Note that the file
    # paths will be different depending on whether `namespace py` was used. See the tests for
    # examples.
    source_root = await get_source_root(SourceRootRequest.for_target(request.protocol_target))
    source_root_restored = (
        await digest_to_snapshot(**implicitly(AddPrefix(result.snapshot.digest, source_root.path)))
        if source_root.path != "."
        else await digest_to_snapshot(result.snapshot.digest)
    )
    return GeneratedSources(source_root_restored)


@dataclass(frozen=True)
class ApacheThriftPythonDependenciesInferenceFieldSet(FieldSet):
    required_fields = (ThriftDependenciesField, ThriftPythonResolveField)

    dependencies: ThriftDependenciesField
    python_resolve: ThriftPythonResolveField


class InferApacheThriftPythonDependencies(InferDependenciesRequest):
    infer_from = ApacheThriftPythonDependenciesInferenceFieldSet


@rule
async def find_apache_thrift_python_requirement(
    request: InferApacheThriftPythonDependencies,
    thrift_python: ThriftPythonSubsystem,
    python_setup: PythonSetup,
    python_infer_subsystem: PythonInferSubsystem,
) -> InferredDependencies:
    if not thrift_python.infer_runtime_dependency:
        return InferredDependencies([])

    resolve = request.field_set.python_resolve.normalized_value(python_setup)

    locality = None
    if python_infer_subsystem.ambiguity_resolution == AmbiguityResolution.by_source_root:
        source_root = await get_source_root(
            SourceRootRequest.for_address(request.field_set.address)
        )
        locality = source_root.path

    addresses_for_thrift = await map_module_to_address(
        PythonModuleOwnersRequest(
            "thrift",
            resolve=resolve,
            locality=locality,
        ),
        **implicitly(),
    )

    addr = find_python_runtime_library_or_raise_error(
        addresses_for_thrift,
        request.field_set.address,
        "thrift",
        resolve=resolve,
        resolves_enabled=python_setup.enable_resolves,
        recommended_requirement_name="thrift",
        recommended_requirement_url="https://pypi.org/project/thrift/",
        disable_inference_option=f"[{thrift_python.options_scope}].infer_runtime_dependency",
    )
    return InferredDependencies([addr])


def rules():
    return (
        *collect_rules(),
        *subsystem.rules(),
        UnionRule(GenerateSourcesRequest, GeneratePythonFromThriftRequest),
        UnionRule(InferDependenciesRequest, InferApacheThriftPythonDependencies),
    )
