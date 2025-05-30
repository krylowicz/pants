# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
"""Function-as-a-service (FaaS) support like AWS Lambda and Google Cloud Functions."""

from __future__ import annotations

import importlib.resources
import logging
import os.path
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, cast

from pants.backend.python.dependency_inference.module_mapper import (
    PythonModuleOwnersRequest,
    map_module_to_address,
)
from pants.backend.python.dependency_inference.rules import import_rules
from pants.backend.python.dependency_inference.subsystem import (
    AmbiguityResolution,
    PythonInferSubsystem,
)
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import (
    PexCompletePlatformsField,
    PexLayout,
    PythonResolveField,
)
from pants.backend.python.util_rules.pex import (
    CompletePlatforms,
    create_pex,
    digest_complete_platform_addresses,
)
from pants.backend.python.util_rules.pex_from_targets import (
    InterpreterConstraintsRequest,
    PexFromTargetsRequest,
    interpreter_constraints_for_targets,
)
from pants.backend.python.util_rules.pex_from_targets import rules as pex_from_targets_rules
from pants.backend.python.util_rules.pex_venv import PexVenvLayout, PexVenvRequest
from pants.backend.python.util_rules.pex_venv import pex_venv as pex_venv_get
from pants.backend.python.util_rules.pex_venv import rules as pex_venv_rules
from pants.core.goals.package import BuiltPackage, BuiltPackageArtifact, OutputPathField
from pants.engine.addresses import Address
from pants.engine.fs import (
    EMPTY_DIGEST,
    CreateDigest,
    FileContent,
    GlobMatchErrorBehavior,
    PathGlobs,
)
from pants.engine.internals.graph import determine_explicitly_provided_dependencies
from pants.engine.intrinsics import create_digest, digest_to_snapshot, path_globs_to_paths
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.target import (
    AsyncFieldMixin,
    Dependencies,
    DependenciesRequest,
    FieldSet,
    InferDependenciesRequest,
    InferredDependencies,
    InvalidFieldException,
    InvalidTargetException,
    StringField,
    StringSequenceField,
)
from pants.engine.unions import UnionRule
from pants.source.source_root import SourceRootRequest, get_source_root
from pants.util.docutil import doc_url
from pants.util.ordered_set import FrozenOrderedSet
from pants.util.strutil import help_text, softwrap

logger = logging.getLogger(__name__)


class PythonFaaSLayoutField(StringField):
    alias = "layout"
    valid_choices = PexVenvLayout
    expected_type = str
    default = PexVenvLayout.FLAT_ZIPPED.value
    help = help_text(
        """
        Control the layout of the final artifact: `flat` creates a directory with the
        source and requirements at the top level, as recommended by cloud vendors,
        while `flat-zipped` (the default) wraps this up into a single zip file.
        """
    )


class PythonFaaSPex3VenvCreateExtraArgsField(StringSequenceField):
    alias = "pex3_venv_create_extra_args"
    default = ()
    help = help_text(
        """
        Any extra arguments to pass to the `pex3 venv create` invocation that is used to create the
        final zip file or directory.

        For example, `pex3_venv_create_extra_args=["--collisions-ok"]`, if using packages that have
        colliding files that aren't required at runtime (errors like "Encountered collisions
        populating ...").
        """
    )


class PythonFaaSPexBuildExtraArgs(StringSequenceField):
    alias = "pex_build_extra_args"
    default = ()
    help = help_text(
        """
        Additional arguments to pass to the `pex` invocation that is used to collect the requirements
        and sources for packaging.

        For example, `pex_build_extra_args=["--exclude=pypi-package-name"]` to force a package called
        `pypi-package-name` isn't included in the artifact.

        Note: Excluding dependencies currently causes Pex to throw an error. You can additionally pass
        the `--ignore-errors` flag.
        """
    )


class PythonFaaSHandlerField(StringField, AsyncFieldMixin):
    alias = "handler"
    required = True
    value: str
    help = help_text(
        """
        You can specify a full module like `'path.to.module:handler_func'` or use a shorthand to
        specify a file name, using the same syntax as the `sources` field, e.g.
        `'cloud_function.py:handler_func'`.
        """
    )

    @classmethod
    def compute_value(cls, raw_value: str | None, address: Address) -> str:
        value = cast(str, super().compute_value(raw_value, address))
        if ":" not in value:
            raise InvalidFieldException(
                f"The `{cls.alias}` field in target at {address} must end in the "
                f"format `:my_handler_func`, but was {value}."
            )
        return value


@dataclass(frozen=True)
class ResolvedPythonFaaSHandler:
    module: str
    func: str
    file_name_used: bool


@dataclass(frozen=True)
class ResolvePythonFaaSHandlerRequest:
    field: PythonFaaSHandlerField


@rule(desc="Determining the handler for a python FaaS target")
async def resolve_python_faas_handler(
    request: ResolvePythonFaaSHandlerRequest,
) -> ResolvedPythonFaaSHandler:
    handler_val = request.field.value
    field_alias = request.field.alias
    address = request.field.address
    path, _, func = handler_val.partition(":")

    # If it's already a module, simply use that. Otherwise, convert the file name into a module
    # path.
    if not path.endswith(".py"):
        return ResolvedPythonFaaSHandler(module=path, func=func, file_name_used=False)

    # Use the engine to validate that the file exists and that it resolves to only one file.
    full_glob = os.path.join(address.spec_path, path)
    handler_paths = await path_globs_to_paths(
        PathGlobs(
            [full_glob],
            glob_match_error_behavior=GlobMatchErrorBehavior.error,
            description_of_origin=f"{address}'s `{field_alias}` field",
        )
    )

    # We will have already raised if the glob did not match, i.e. if there were no files. But
    # we need to check if they used a file glob (`*` or `**`) that resolved to >1 file.
    if len(handler_paths.files) != 1:
        raise InvalidFieldException(
            f"Multiple files matched for the `{field_alias}` {repr(handler_val)} for the target "
            f"{address}, but only one file expected. Are you using a glob, rather than a file "
            f"name?\n\nAll matching files: {list(handler_paths.files)}."
        )
    handler_path = handler_paths.files[0]
    source_root = await get_source_root(SourceRootRequest.for_file(handler_path))
    stripped_source_path = os.path.relpath(handler_path, source_root.path)
    module_base, _ = os.path.splitext(stripped_source_path)
    normalized_path = module_base.replace(os.path.sep, ".")
    return ResolvedPythonFaaSHandler(module=normalized_path, func=func, file_name_used=True)


class PythonFaaSDependencies(Dependencies):
    supports_transitive_excludes = True


@dataclass(frozen=True)
class PythonFaaSHandlerInferenceFieldSet(FieldSet):
    required_fields = (
        PythonFaaSDependencies,
        PythonFaaSHandlerField,
        PythonResolveField,
    )

    dependencies: PythonFaaSDependencies
    handler: PythonFaaSHandlerField
    resolve: PythonResolveField


class InferPythonFaaSHandlerDependency(InferDependenciesRequest):
    infer_from = PythonFaaSHandlerInferenceFieldSet


@rule(desc="Inferring dependency from the python FaaS `handler` field")
async def infer_faas_handler_dependency(
    request: InferPythonFaaSHandlerDependency,
    python_infer_subsystem: PythonInferSubsystem,
    python_setup: PythonSetup,
) -> InferredDependencies:
    if not python_infer_subsystem.entry_points:
        return InferredDependencies([])

    explicitly_provided_deps, handler = await concurrently(
        determine_explicitly_provided_dependencies(
            **implicitly(DependenciesRequest(request.field_set.dependencies))
        ),
        resolve_python_faas_handler(ResolvePythonFaaSHandlerRequest(request.field_set.handler)),
    )

    # Only set locality if needed, to avoid unnecessary rule graph memoization misses.
    # When set, use the source root, which is useful in practice, but incurs fewer memoization
    # misses than using the full spec_path.
    locality = None
    if python_infer_subsystem.ambiguity_resolution == AmbiguityResolution.by_source_root:
        source_root = await get_source_root(
            SourceRootRequest.for_address(request.field_set.address)
        )
        locality = source_root.path

    owners = await map_module_to_address(
        PythonModuleOwnersRequest(
            handler.module,
            resolve=request.field_set.resolve.normalized_value(python_setup),
            locality=locality,
        ),
        **implicitly(),
    )
    address = request.field_set.address
    explicitly_provided_deps.maybe_warn_of_ambiguous_dependency_inference(
        owners.ambiguous,
        address,
        # If the handler was specified as a file, like `app.py`, we know the module must
        # live in the python_google_cloud_function's directory or subdirectory, so the owners must be ancestors.
        owners_must_be_ancestors=handler.file_name_used,
        import_reference="module",
        context=(
            f"The target {address} has the field "
            f"`handler={repr(request.field_set.handler.value)}`, which maps "
            f"to the Python module `{handler.module}`"
        ),
    )
    maybe_disambiguated = explicitly_provided_deps.disambiguated(
        owners.ambiguous, owners_must_be_ancestors=handler.file_name_used
    )
    unambiguous_owners = owners.unambiguous or (
        (maybe_disambiguated,) if maybe_disambiguated else ()
    )
    return InferredDependencies(unambiguous_owners)


class PythonFaaSCompletePlatforms(PexCompletePlatformsField):
    help = help_text(
        f"""
        {PexCompletePlatformsField.help}

        N.B.: only one of this and `runtime` can be set. If `runtime` is set, a default complete
        platform is chosen, if one is known for that runtime. Explicitly set this to `[]` to use the
        platform's ambient interpreter, such as when running in an docker environment.
        """
    )


class FaaSArchitecture(str, Enum):
    X86_64 = "x86_64"
    ARM64 = "arm64"


@dataclass(frozen=True)
class PythonFaaSKnownRuntime:
    name: str
    major: int
    minor: int
    docker_repo: str
    tag: str
    architecture: FaaSArchitecture

    def file_name(self) -> str:
        return f"complete_platform_{self.tag}.json"


class PythonFaaSRuntimeField(StringField, ABC):
    alias = "runtime"
    default = None

    known_runtimes: ClassVar[tuple[PythonFaaSKnownRuntime, ...]] = ()

    @classmethod
    def known_runtimes_complete_platforms_module(cls) -> str:
        # the runtime field subclasses are conventionally in a `target_types.py` file, and we want
        # to put the JSONs in a sibling file
        return cls.__module__.rsplit(".", 1)[0]

    @abstractmethod
    def to_interpreter_version(self) -> None | tuple[int, int]:
        """Returns the Python version implied by the runtime, as (major, minor)."""

    @classmethod
    @abstractmethod
    def from_interpreter_version(cls, py_major: int, py_minor: int) -> str:
        """Returns an appropriately-formatted runtime argument."""

    def to_platform_string(self) -> None | str:
        # We hardcode the platform value to the appropriate one for each FaaS runtime.
        # (Running the "hello world" cloud function in the example code will report the platform, and can be
        # used to verify correctness of these platform strings.)
        interpreter_version = self.to_interpreter_version()
        if interpreter_version is None:
            return None

        return _format_platform_from_major_minor(*interpreter_version)


def _format_platform_from_major_minor(py_major: int, py_minor: int) -> str:
    platform_str = f"linux_x86_64-cp-{py_major}{py_minor}-cp{py_major}{py_minor}"
    # set pymalloc ABI flag - this was removed in python 3.8 https://bugs.python.org/issue36707
    if py_major <= 3 and py_minor < 8:
        platform_str += "m"
    return platform_str


@rule
async def digest_complete_platforms(
    complete_platforms: PythonFaaSCompletePlatforms,
) -> CompletePlatforms:
    return await digest_complete_platform_addresses(complete_platforms.to_unparsed_address_inputs())


@dataclass(frozen=True)
class RuntimePlatformsRequest:
    address: Address
    target_name: str

    runtime: PythonFaaSRuntimeField
    complete_platforms: PythonFaaSCompletePlatforms
    architecture: FaaSArchitecture


@dataclass(frozen=True)
class RuntimePlatforms:
    interpreter_version: None | tuple[int, int]
    complete_platforms: CompletePlatforms = CompletePlatforms()


async def _infer_from_ics(request: RuntimePlatformsRequest) -> tuple[int, int]:
    ics = await interpreter_constraints_for_targets(
        InterpreterConstraintsRequest([request.address]), **implicitly()
    )

    # Future proofing: use naive non-universe-based IC requirement matching to determine if the
    # requirements cover exactly (and all patch versions of) one major.minor interpreter
    # version.
    #
    # Either reasonable option for a universe (`PythonSetup.interpreter_universe` or the FaaS's
    # known runtimes) can and will be expanded during a Pants upgrade: for instance, at the time of
    # writing, Pants only supports up to 3.11 but might soon add support for 3.12, or AWS Lambda
    # (and pants.backend.awslambda.python's known runtimes) only supports up to 3.10 but might soon
    # add support for 3.11.
    #
    # When this happens, some ranges (like `>=3.11`, if using `PythonSetup.interpreter_universe`)
    # will go from covering one major.minor interpreter version to covering more than one, and thus
    # inference starts breaking during the upgrade, requiring the user to do distracting changes
    # without deprecations/warnings to help.
    major_minor = ics.major_minor_version_when_single_and_entire()
    if major_minor is not None:
        return major_minor

    raise InvalidTargetException(
        softwrap(
            f"""
            The {request.target_name!r} target {request.address} cannot have its runtime platform
            inferred, because inference requires simple interpreter constraints covering exactly one
            minor release of Python, and all its patch version. The constraints for this target
            ({ics}) aren't understood.

            To fix, provide one of the following:

            - a value for the `{request.runtime.alias}` field, or

            - a value for the `{request.complete_platforms.alias}` field, or

            - simple and narrow interpreter constraints (for example, `==3.10.*` or `>=3.10,<3.11` are simple enough to imply Python 3.10)
            """
        )
    )


@rule
async def infer_runtime_platforms(request: RuntimePlatformsRequest) -> RuntimePlatforms:
    if request.complete_platforms.value is not None:
        # explicit complete platforms wins:

        complete_platforms = await digest_complete_platforms(request.complete_platforms)
        # Don't bother trying to infer the runtime version if the user has provided their own
        # complete platform; they probably know what they're doing.
        return RuntimePlatforms(interpreter_version=None, complete_platforms=complete_platforms)

    version = request.runtime.to_interpreter_version()
    inferred_from_ics = False
    if version is None:
        # if there's not a specified version, let's try to infer it from the interpreter constraints
        version = await _infer_from_ics(request)
        inferred_from_ics = True

    try:
        file_name = next(
            rt.file_name()
            for rt in request.runtime.known_runtimes
            if version == (rt.major, rt.minor) and request.architecture.value == rt.architecture
        )
    except StopIteration:
        # No known runtime, so prompt the user to specify
        version_modifier = "[inferred from interpreter constraints]" if inferred_from_ics else ""
        version_adjective = "inferred" if inferred_from_ics else "specified"
        known_runtimes_str = ", ".join(
            FrozenOrderedSet(r.name for r in request.runtime.known_runtimes)
        )
        raise InvalidTargetException(
            softwrap(
                f"""
                Could not find a known runtime for the {version_adjective} Python version and machine architecture!

                * Python version: {version} {version_modifier}
                * Machine architecture: {request.architecture.value}
                * Known runtime values: {known_runtimes_str}

                To fix, please generate a `complete_platforms` file for the given Python version and
                machine architecture, or specify a runtime that is known to Pants.

                You can follow the instructions at {doc_url("docs/python/overview/pex#generating-the-complete_platforms-file")}
                to generate a `complete_platforms` file for your Python version and machine
                architecture.
                """
            ),
            description_of_origin=f"In the {request.target_name!r} target",
        ) from None

    module = request.runtime.known_runtimes_complete_platforms_module()

    content = (importlib.resources.files(module) / file_name).read_bytes()
    snapshot = await digest_to_snapshot(
        **implicitly(CreateDigest([FileContent(file_name, content)]))
    )

    return RuntimePlatforms(
        interpreter_version=version, complete_platforms=CompletePlatforms.from_snapshot(snapshot)
    )


@dataclass(frozen=True)
class BuildPythonFaaSRequest:
    address: Address
    target_name: str

    complete_platforms: PythonFaaSCompletePlatforms
    handler: None | PythonFaaSHandlerField
    output_path: OutputPathField
    runtime: PythonFaaSRuntimeField
    architecture: FaaSArchitecture
    pex3_venv_create_extra_args: PythonFaaSPex3VenvCreateExtraArgsField
    pex_build_extra_args: PythonFaaSPexBuildExtraArgs
    layout: PythonFaaSLayoutField

    include_requirements: bool
    include_sources: bool

    reexported_handler_module: None | str
    log_only_reexported_handler_func: bool = False

    prefix_in_artifact: None | str = None


@rule
async def build_python_faas(
    request: BuildPythonFaaSRequest,
) -> BuiltPackage:
    additional_pex_args = (
        # Ensure we can resolve manylinux wheels in addition to any AMI-specific wheels.
        "--manylinux=manylinux2014",
        # When we're executing Pex on Linux, allow a local interpreter to be resolved if
        # available and matching the AMI platform.
        "--resolve-local-platforms",
        # Additional args from request
        *(request.pex_build_extra_args.value or ()),
    )

    platforms_get = infer_runtime_platforms(
        RuntimePlatformsRequest(
            address=request.address,
            target_name=request.target_name,
            runtime=request.runtime,
            architecture=request.architecture,
            complete_platforms=request.complete_platforms,
        ),
    )

    if request.handler:
        platforms, handler = await concurrently(
            platforms_get,
            resolve_python_faas_handler(ResolvePythonFaaSHandlerRequest(request.handler)),
        )
    else:
        platforms = await platforms_get
        handler = None

    # TODO: improve diagnostics if there's more than one platform/complete_platform

    if request.reexported_handler_module and handler:
        # synthesise a source file that gives a fixed handler path, no matter what the entry point is:
        # some platforms require a certain name (e.g. GCF), and even on others, giving a fixed name
        # means users don't need to duplicate the entry_point config in both the pants BUILD file and
        # infrastructure definitions (the latter can always use the same names, for every lambda).
        reexported_handler_file = f"{request.reexported_handler_module}.py"
        reexported_handler_func = "handler"
        reexported_handler_content = (
            f"from {handler.module} import {handler.func} as {reexported_handler_func}"
        )
        additional_sources = await create_digest(
            CreateDigest(
                [FileContent(reexported_handler_file, reexported_handler_content.encode())]
            )
        )
    else:
        additional_sources = EMPTY_DIGEST
        reexported_handler_func = None

    repository_filename = "faas_repository.pex"
    pex_request = PexFromTargetsRequest(
        addresses=[request.address],
        internal_only=False,
        include_requirements=request.include_requirements,
        include_source_files=request.include_sources,
        output_filename=repository_filename,
        complete_platforms=platforms.complete_platforms,
        layout=PexLayout.PACKED,
        additional_args=additional_pex_args,
        additional_lockfile_args=additional_pex_args,
        additional_sources=additional_sources,
        warn_for_transitive_files_targets=True,
    )

    pex_result = await create_pex(**implicitly({pex_request: PexFromTargetsRequest}))

    layout = PexVenvLayout(request.layout.value)

    output_filename = request.output_path.value_or_default(
        file_ending="zip" if layout is PexVenvLayout.FLAT_ZIPPED else None
    )

    result = await pex_venv_get(
        PexVenvRequest(
            pex=pex_result,
            layout=layout,
            complete_platforms=platforms.complete_platforms,
            extra_args=request.pex3_venv_create_extra_args.value or (),
            prefix=request.prefix_in_artifact,
            output_path=Path(output_filename),
            description=f"Build {request.target_name} artifact for {request.address}",
        ),
    )

    extra_log_lines = []

    if platforms.interpreter_version is not None:
        extra_log_lines.append(
            f"    Runtime: {request.runtime.from_interpreter_version(*platforms.interpreter_version)}"
        )

    if request.architecture is not None:
        extra_log_lines.append(f"    Architecture: {request.architecture.value}")

    if reexported_handler_func is not None:
        if request.log_only_reexported_handler_func:
            handler_text = reexported_handler_func
        else:
            handler_text = f"{request.reexported_handler_module}.{reexported_handler_func}"
        extra_log_lines.append(f"    Handler: {handler_text}")

    artifact = BuiltPackageArtifact(
        output_filename,
        extra_log_lines=tuple(extra_log_lines),
    )
    return BuiltPackage(digest=result.digest, artifacts=(artifact,))


def rules():
    return (
        *collect_rules(),
        *import_rules(),
        *pex_venv_rules(),
        *pex_from_targets_rules(),
        UnionRule(InferDependenciesRequest, InferPythonFaaSHandlerDependency),
    )
