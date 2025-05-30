# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules.source_files import SourceFiles
from pants.engine.fs import CreateDigest, Directory, FileContent
from pants.engine.internals.native_engine import AddPrefix, MergeDigests, RemovePrefix
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import (
    add_prefix,
    create_digest,
    execute_process,
    get_digest_contents,
    merge_digests,
    remove_prefix,
)
from pants.engine.process import (
    FallibleProcessResult,
    ProductDescription,
    fallible_to_exec_result_or_raise,
)
from pants.engine.rules import collect_rules, implicitly, rule
from pants.engine.unions import UnionRule
from pants.jvm.compile import ClasspathEntry
from pants.jvm.jdk_rules import InternalJdk, JdkRequest, JvmProcess, prepare_jdk_environment
from pants.jvm.resolve.common import ArtifactRequirements
from pants.jvm.resolve.coordinate import Coordinate
from pants.jvm.resolve.coursier_fetch import ToolClasspathRequest, materialize_classpath_for_tool
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, JvmToolBase
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.resources import read_resource

_PARSER_KOTLIN_VERSION = "1.6.20"


class KotlinParser(JvmToolBase):
    options_scope = "kotlin-parser"
    help = "Internal tool for parsing Kotlin sources to identify dependencies"

    default_version = _PARSER_KOTLIN_VERSION
    default_artifacts = (
        "org.jetbrains.kotlin:kotlin-compiler:{version}",
        "org.jetbrains.kotlin:kotlin-stdlib:{version}",
        "com.google.code.gson:gson:2.9.0",
    )
    default_lockfile_resource = (
        "pants.backend.kotlin.dependency_inference",
        "kotlin_parser.lock",
    )


@dataclass(frozen=True)
class KotlinImport:
    name: str
    alias: str | None
    is_wildcard: bool

    @classmethod
    def from_json_dict(cls, d: dict) -> KotlinImport:
        return cls(
            name=d["name"],
            alias=d.get("alias"),
            is_wildcard=d["isWildcard"],
        )

    def to_debug_json_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "alias": self.alias,
            "is_wildcard": self.is_wildcard,
        }


@dataclass(frozen=True)
class KotlinSourceDependencyAnalysis:
    package: str
    imports: frozenset[KotlinImport]
    named_declarations: frozenset[str]
    consumed_symbols_by_scope: FrozenDict[str, frozenset[str]]
    scopes: frozenset[str]

    def fully_qualified_consumed_symbols(self) -> Iterator[str]:
        """Consumed symbols qualified in various ways.

        This method _will_ introduce false-positives, because we will assume that the symbol could
        have been provided by any wildcard import in scope, as well as being declared in the current
        package.
        """

        def scope_and_parents(scope: str) -> Iterator[str]:
            while True:
                yield scope
                if scope == "":
                    break
                scope, _, _ = scope.rpartition(".")

        for consumption_scope, consumed_symbols in self.consumed_symbols_by_scope.items():
            parent_scopes = tuple(scope_and_parents(consumption_scope))
            for symbol in consumed_symbols:
                symbol_rel_prefix, dot_in_symbol, symbol_rel_suffix = symbol.partition(".")
                if not self.scopes or dot_in_symbol:
                    # TODO: Similar to #13545: we assume that a symbol containing a dot might already
                    # be fully qualified.
                    yield symbol
                for parent_scope in parent_scopes:
                    if parent_scope in self.scopes:
                        # A package declaration is a parent of this scope, and any of its symbols
                        # could be in scope.
                        yield f"{parent_scope}.{symbol}"

                    for imp in self.imports if parent_scope == self.package else ():
                        if imp.is_wildcard:
                            # There is a wildcard import in a parent scope.
                            yield f"{imp.name}.{symbol}"
                        if dot_in_symbol:
                            # If the parent scope has an import which defines the first token of the
                            # symbol, then it might be a relative usage of an import.
                            if imp.alias:
                                if imp.alias == symbol_rel_prefix:
                                    yield f"{imp.name}.{symbol_rel_suffix}"
                            elif imp.name.endswith(f".{symbol_rel_prefix}"):
                                yield f"{imp.name}.{symbol_rel_suffix}"

    @classmethod
    def from_json_dict(cls, d: dict) -> KotlinSourceDependencyAnalysis:
        return cls(
            package=d["package"],
            imports=frozenset(KotlinImport.from_json_dict(i) for i in d["imports"]),
            named_declarations=frozenset(d["namedDeclarations"]),
            consumed_symbols_by_scope=FrozenDict(
                {k: frozenset(v) for k, v in d["consumedSymbolsByScope"].items()}
            ),
            scopes=frozenset(d["scopes"]),
        )

    def to_debug_json_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "imports": [imp.to_debug_json_dict() for imp in self.imports],
            "named_declarations": list(self.named_declarations),
            "consumed_symbols_by_scope": {
                k: sorted(v) for k, v in self.consumed_symbols_by_scope.items()
            },
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class FallibleKotlinSourceDependencyAnalysisResult:
    process_result: FallibleProcessResult


class KotlinParserCompiledClassfiles(ClasspathEntry):
    pass


@rule(level=LogLevel.DEBUG)
async def analyze_kotlin_source_dependencies(
    processor_classfiles: KotlinParserCompiledClassfiles,
    source_files: SourceFiles,
    tool: KotlinParser,
) -> FallibleKotlinSourceDependencyAnalysisResult:
    # Use JDK 8 due to https://youtrack.jetbrains.com/issue/KTIJ-17192 and https://youtrack.jetbrains.com/issue/KT-37446.
    request = JdkRequest("zulu:8.0.392")
    env = await prepare_jdk_environment(**implicitly({request: JdkRequest}))
    jdk = InternalJdk.from_jdk_environment(env)

    if len(source_files.files) > 1:
        raise ValueError(
            f"analyze_kotlin_source_dependencies expects sources with exactly 1 source file, but found {len(source_files.snapshot.files)}."
        )
    elif len(source_files.files) == 0:
        raise ValueError(
            "analyze_kotlin_source_dependencies expects sources with exactly 1 source file, but found none."
        )
    source_prefix = "__source_to_analyze"
    source_path = os.path.join(source_prefix, source_files.files[0])
    processorcp_relpath = "__processorcp"
    toolcp_relpath = "__toolcp"

    (
        tool_classpath,
        prefixed_source_files_digest,
    ) = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(lockfile=(GenerateJvmLockfileFromTool.create(tool)))
        ),
        add_prefix(AddPrefix(source_files.snapshot.digest, source_prefix)),
    )

    extra_immutable_input_digests = {
        toolcp_relpath: tool_classpath.digest,
        processorcp_relpath: processor_classfiles.digest,
    }

    analysis_output_path = "__source_analysis.json"

    process_result = await execute_process(
        **implicitly(
            JvmProcess(
                jdk=jdk,
                classpath_entries=[
                    *tool_classpath.classpath_entries(toolcp_relpath),
                    processorcp_relpath,
                ],
                argv=[
                    "org.pantsbuild.backend.kotlin.dependency_inference.KotlinParserKt",
                    analysis_output_path,
                    source_path,
                ],
                input_digest=prefixed_source_files_digest,
                extra_immutable_input_digests=extra_immutable_input_digests,
                output_files=(analysis_output_path,),
                extra_nailgun_keys=extra_immutable_input_digests,
                description=f"Analyzing {source_files.files[0]}",
                level=LogLevel.DEBUG,
            )
        )
    )

    return FallibleKotlinSourceDependencyAnalysisResult(process_result=process_result)


@rule(level=LogLevel.DEBUG)
async def resolve_fallible_result_to_analysis(
    fallible_result: FallibleKotlinSourceDependencyAnalysisResult,
) -> KotlinSourceDependencyAnalysis:
    desc = ProductDescription("Kotlin source dependency analysis failed.")
    result = await fallible_to_exec_result_or_raise(
        **implicitly(
            {fallible_result.process_result: FallibleProcessResult, desc: ProductDescription}
        )
    )
    analysis_contents = await get_digest_contents(result.output_digest)
    analysis = json.loads(analysis_contents[0].content)
    return KotlinSourceDependencyAnalysis.from_json_dict(analysis)


@rule
async def setup_kotlin_parser_classfiles(
    jdk: InternalJdk, tool: KotlinParser
) -> KotlinParserCompiledClassfiles:
    dest_dir = "classfiles"

    parser_source_content = read_resource(
        "pants.backend.kotlin.dependency_inference", "KotlinParser.kt"
    )
    if not parser_source_content:
        raise AssertionError("Unable to find KotlinParser.kt resource.")

    parser_source = FileContent("KotlinParser.kt", parser_source_content)

    tool_classpath, parser_classpath, source_digest = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(
                prefix="__toolcp",
                artifact_requirements=ArtifactRequirements.from_coordinates(
                    [
                        Coordinate(
                            group="org.jetbrains.kotlin",
                            artifact="kotlin-compiler-embeddable",
                            version=tool.version,
                        ),
                    ]
                ),
            )
        ),
        materialize_classpath_for_tool(
            ToolClasspathRequest(
                prefix="__parsercp", lockfile=(GenerateJvmLockfileFromTool.create(tool))
            )
        ),
        create_digest(CreateDigest([parser_source, Directory(dest_dir)])),
    )

    merged_digest = await merge_digests(
        MergeDigests(
            (
                tool_classpath.digest,
                parser_classpath.digest,
                source_digest,
            )
        )
    )

    process_result = await fallible_to_exec_result_or_raise(
        **implicitly(
            JvmProcess(
                jdk=jdk,
                classpath_entries=tool_classpath.classpath_entries(),
                argv=[
                    "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
                    "-classpath",
                    ":".join(parser_classpath.classpath_entries()),
                    "-d",
                    dest_dir,
                    parser_source.path,
                ],
                input_digest=merged_digest,
                output_directories=(dest_dir,),
                description="Compile Kotlin parser for dependency inference with kotlinc",
                level=LogLevel.DEBUG,
                # NB: We do not use nailgun for this process, since it is launched exactly once.
                use_nailgun=False,
            )
        )
    )
    stripped_classfiles_digest = await remove_prefix(
        RemovePrefix(process_result.output_digest, dest_dir)
    )
    return KotlinParserCompiledClassfiles(digest=stripped_classfiles_digest)


def rules():
    return (
        *collect_rules(),
        UnionRule(ExportableTool, KotlinParser),
    )
