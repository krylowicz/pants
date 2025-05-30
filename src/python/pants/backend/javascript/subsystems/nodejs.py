# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import itertools
import logging
import os.path
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import groupby
from typing import ClassVar

from nodesemver import min_satisfying

from pants.core.util_rules import asdf, search_paths, system_binaries
from pants.core.util_rules.asdf import AsdfPathString, AsdfToolPathsResult
from pants.core.util_rules.environments import EnvironmentTarget
from pants.core.util_rules.external_tool import (
    DownloadedExternalTool,
    ExternalToolRequest,
    ExternalToolVersion,
    TemplatedExternalToolOptionsMixin,
    download_external_tool,
)
from pants.core.util_rules.external_tool import rules as external_tool_rules
from pants.core.util_rules.search_paths import (
    ExecutableSearchPathsOptionMixin,
    ValidateSearchPathsRequest,
    VersionManagerSearchPathsRequest,
    get_un_cachable_version_manager_paths,
    validate_search_paths,
)
from pants.core.util_rules.system_binaries import (
    BinaryNotFoundError,
    BinaryPath,
    BinaryPathRequest,
    BinaryPathTest,
    BinaryShims,
    BinaryShimsRequest,
    create_binary_shims,
    find_binary,
)
from pants.engine.env_vars import EXTRA_ENV_VARS_USAGE_HELP, EnvironmentVars, EnvironmentVarsRequest
from pants.engine.fs import EMPTY_DIGEST, CreateDigest, Digest, Directory, DownloadFile
from pants.engine.internals.native_engine import FileDigest, MergeDigests
from pants.engine.internals.platform_rules import environment_path_variable
from pants.engine.internals.platform_rules import environment_vars_subset
from pants.engine.internals.platform_rules import (
    environment_vars_subset as environment_vars_subset_get,
)
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import create_digest, merge_digests
from pants.engine.platform import Platform
from pants.engine.process import Process, fallible_to_exec_result_or_raise
from pants.engine.rules import Rule, collect_rules, implicitly, rule
from pants.engine.unions import UnionRule
from pants.option.option_types import DictOption, ShellStrListOption, StrListOption, StrOption
from pants.option.subsystem import Subsystem
from pants.util.docutil import bin_name
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet
from pants.util.strutil import help_text, softwrap

_logger = logging.getLogger(__name__)


class NodeJS(Subsystem, TemplatedExternalToolOptionsMixin):
    options_scope = "nodejs"
    help = "The Node.js Javascript runtime (including Corepack)."

    default_version = "v22.14.0"
    default_known_versions = [
        "v22.14.0|macos_arm64|e9404633bc02a5162c5c573b1e2490f5fb44648345d64a958b17e325729a5e42|47035396",
        "v22.14.0|macos_x86_64|6698587713ab565a94a360e091df9f6d91c8fadda6d00f0cf6526e9b40bed250|48656392",
        "v22.14.0|linux_arm64|08bfbf538bad0e8cbb0269f0173cca28d705874a67a22f60b57d99dc99e30050|28636440",
        "v22.14.0|linux_x86_64|69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec|29893360",
    ]

    default_url_template = "https://nodejs.org/dist/{version}/node-{version}-{platform}.tar"
    default_url_platform_mapping = {
        "macos_arm64": "darwin-arm64",
        "macos_x86_64": "darwin-x64",
        "linux_arm64": "linux-arm64",
        "linux_x86_64": "linux-x64",
    }

    resolves = DictOption[str](
        default={},
        help=softwrap(
            f"""
            A mapping of names to lockfile paths used in your project.

            Specifying a resolve name is optional. If unspecified,
            the default resolve name is calculated by taking the path
            from the source root to the directory containing the lockfile
            and replacing '{os.path.sep}' with '.' in that path.

            Example:
            An npm lockfile located at `src/js/package/package-lock.json`
            will result in a resolve named `js.package`, assuming src/
            is a source root.

            Run `{bin_name()} generate-lockfiles` to
            generate the lockfile(s).
            """
        ),
        advanced=True,
    )

    def generate_url(self, version: str, plat: Platform) -> str:
        """NodeJS binaries are compressed as .gz for Mac, .xz for Linux."""
        platform = self.url_platform_mapping.get(plat.value, "")
        url = self.url_template.format(version=version, platform=platform)
        extension = "gz" if plat.is_macos else "xz"
        return f"{url}.{extension}"

    def generate_exe(self, version: str, plat: Platform) -> str:
        assert self.default_url_platform_mapping is not None
        plat_str = self.default_url_platform_mapping[plat.value]
        return f"./node-{version}-{plat_str}/bin/node"

    async def download_known_version(
        self, known_version: ExternalToolVersion, platform: Platform
    ) -> DownloadedExternalTool:
        exe = self.generate_exe(known_version.version, platform)
        url = self.generate_url(known_version.version, platform)
        download_file = DownloadFile(url, FileDigest(known_version.sha256, known_version.filesize))
        return await download_external_tool(ExternalToolRequest(download_file, exe))

    package_manager = StrOption(
        default="npm",
        help=softwrap(
            """
            Default Node.js package manager to use.

            You can either rely on this default together with the [nodejs].package_managers
            option, or specify the `package.json#packageManager` tool and version
            in the package.json of your project.

            Specifying conflicting package manager versions within a multi-package
            workspace is an error.
            """
        ),
    )

    package_managers = DictOption[str](
        default={"npm": "10.9.2", "yarn": "1.22.22", "pnpm": "9.15.6"},
        help=help_text(
            """
            A mapping of package manager versions to semver releases.

            Many organizations only need a single version of a package manager, which is
            a good default and often the simplest thing to do.

            The version download is managed by Corepack. This mapping corresponds to
            the https://github.com/nodejs/corepack#known-good-releases setting, using
            the `--activate` flag.
            """
        ),
    )

    extra_env_vars = StrListOption(
        help=softwrap(
            f"""
            Environment variables to set during package manager operations.

            {EXTRA_ENV_VARS_USAGE_HELP}
            """
        ),
        advanced=True,
    )

    @property
    def default_package_manager(self) -> str | None:
        if self.package_manager in self.package_managers:
            return f"{self.package_manager}@{self.package_managers[self.package_manager]}"
        return self.package_manager

    _tools = StrListOption(
        default=[],
        help=softwrap(
            """
            List any additional executable tools required for node processes to work. The paths to
            these tools will be included in the PATH used in the execution sandbox, so that
            they may be used by nodejs processes execution.
            """
        ),
        advanced=True,
    )

    _optional_tools = StrListOption(
        default=[],
        help=softwrap(
            """
            List any additional executable which are not mandatory for node processes to work, but
            which should be included if available. The paths to these tools will be included in the
            PATH used in the execution sandbox, so that they may be used by nodejs processes execution.
            """
        ),
        advanced=True,
    )

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._tools)))

    @property
    def optional_tools(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._optional_tools)))

    class EnvironmentAware(ExecutableSearchPathsOptionMixin, Subsystem.EnvironmentAware):
        search_path = StrListOption(
            default=["<PATH>"],
            help=lambda cls: help_text(
                f"""
                A list of paths to search for Node.js distributions.

                This option is only used if a templated url download
                specified via [{cls.subsystem.options_scope}].known_versions
                does not contain a version matching the configured
                [{cls.subsystem.options_scope}].version range.

                You can specify absolute paths to binaries
                and/or to directories containing binaries. The order of entries does
                not matter.

                The following special strings are supported:

                For all runtime environment types:

                * `<PATH>`, the contents of the PATH env var

                When the environment is a `local_environment` target:

                * `{AsdfPathString.STANDARD}`, {AsdfPathString.STANDARD.description("Node.js")}
                * `{AsdfPathString.LOCAL}`, {AsdfPathString.LOCAL.description("binaries")}
                * `<NVM>`, all NodeJS versions under $NVM_DIR/versions/node
                * `<NVM_LOCAL>`, the nvm installation with the version in BUILD_ROOT/.nvmrc
                Note that the version in the .nvmrc file has to be on the form "vX.Y.Z".
                """
            ),
            advanced=True,
            metavar="<binary-paths>",
        )

        executable_search_paths_help = softwrap(
            """
            The PATH value that will be used to find any tools required to run nodejs processes.
            """
        )

        _corepack_env_vars = ShellStrListOption(
            help=softwrap(
                """
                Environment variables to set for `corepack` invocations.

                Entries are either strings in the form `ENV_VAR=value` to set an explicit value;
                or just `ENV_VAR` to copy the value from Pants's own environment.

                Review https://github.com/nodejs/corepack#environment-variables
                for available variables.
                """
            ),
            advanced=True,
        )

        @property
        def corepack_env_vars(self) -> tuple[str, ...]:
            return tuple(sorted(set(self._corepack_env_vars)))


@dataclass(frozen=True)
class NodeJSToolProcess:
    """A request for a tool installed with NodeJS."""

    tool: str
    tool_version: str | None
    args: tuple[str, ...]
    description: str
    level: LogLevel = LogLevel.INFO
    input_digest: Digest = EMPTY_DIGEST
    output_files: tuple[str, ...] = ()
    output_directories: tuple[str, ...] = ()
    working_directory: str | None = None
    append_only_caches: FrozenDict[str, str] = field(default_factory=FrozenDict)
    timeout_seconds: int | None = None
    extra_env: Mapping[str, str] = field(default_factory=FrozenDict)
    project_digest: Digest | None = None

    @classmethod
    def npm(
        cls,
        args: Iterable[str],
        description: str,
        level: LogLevel = LogLevel.INFO,
        input_digest: Digest = EMPTY_DIGEST,
        output_files: tuple[str, ...] = (),
        output_directories: tuple[str, ...] = (),
        working_directory: str | None = None,
        append_only_caches: FrozenDict[str, str] | None = None,
        timeout_seconds: int | None = None,
        extra_env: Mapping[str, str] | None = None,
        tool_version: str | None = None,
        project_digest: Digest | None = None,
    ) -> NodeJSToolProcess:
        return cls(
            tool="npm",
            tool_version=tool_version,
            args=tuple(args),
            description=description,
            level=level,
            input_digest=input_digest,
            output_files=output_files,
            output_directories=output_directories,
            working_directory=working_directory,
            append_only_caches=append_only_caches or FrozenDict(),
            timeout_seconds=timeout_seconds,
            extra_env=extra_env or FrozenDict(),
            project_digest=project_digest,
        )


@dataclass(frozen=True)
class NodeJSBinaries:
    binary_dir: str
    digest: Digest | None = None


@dataclass(frozen=True)
class NodeJSProcessEnvironment:
    binaries: NodeJSBinaries
    npm_config_cache: str
    tool_binaries: BinaryShims
    corepack_home: str
    corepack_shims: str
    corepack_env_vars: EnvironmentVars

    base_bin_dir: ClassVar[str] = "__node"

    def to_env_dict(self, extras: Mapping[str, str] | None = None) -> dict[str, str]:
        extras = extras or {}
        extra_path = extras.get("PATH", "")
        path = [self.tool_binaries.path_component, self.corepack_shims, self.binary_directory]
        if extra_path:
            path.append(extra_path)

        return {
            **extras,
            "PATH": os.pathsep.join(path),
            "npm_config_cache": self.npm_config_cache,  # Normally stored at ~/.npm,
            "COREPACK_HOME": os.path.join("{chroot}", self.corepack_home),
            **self.corepack_env_vars,
        }

    @property
    def append_only_caches(self) -> Mapping[str, str]:
        return {"npm": self.npm_config_cache}

    @property
    def binary_directory(self) -> str:
        return self.binaries.binary_dir

    def immutable_digest(self) -> dict[str, Digest]:
        return (
            {self.base_bin_dir: self.binaries.digest, **self.tool_binaries.immutable_input_digests}
            if self.binaries.digest
            else {**self.tool_binaries.immutable_input_digests}
        )


async def add_corepack_shims_to_digest(
    binaries: NodeJSBinaries, tool_shims: BinaryShims, corepack_env_vars: EnvironmentVars
) -> Digest:
    directory_digest = await create_digest(CreateDigest([Directory("._corepack")]))
    binary_digest = binaries.digest if binaries.digest else EMPTY_DIGEST
    input_digest = await merge_digests(MergeDigests((directory_digest, binary_digest)))

    none_immutable_binary_path = binaries.binary_dir.replace(
        f"/{NodeJSProcessEnvironment.base_bin_dir}", ""
    )
    enable_corepack_result = await fallible_to_exec_result_or_raise(
        **implicitly(
            Process(
                argv=(
                    "corepack",
                    "enable",
                    "npm",
                    "pnpm",
                    "yarn",
                    "--install-directory",
                    "._corepack",
                ),
                input_digest=input_digest,
                immutable_input_digests={**tool_shims.immutable_input_digests},
                output_directories=["._corepack"],
                description="Enabling corepack shims",
                level=LogLevel.DEBUG,
                env={
                    "PATH": f"{tool_shims.path_component}:{none_immutable_binary_path}",
                    "COREPACK_HOME": "._corepack_home",
                    **corepack_env_vars,
                },
            )
        )
    )
    return await merge_digests(MergeDigests((binary_digest, enable_corepack_result.output_digest)))


async def get_nodejs_process_tools_shims(
    *,
    tools: Sequence[str],
    optional_tools: Sequence[str],
    search_path: Sequence[str],
    rationale: str,
) -> BinaryShims:
    requests = [
        BinaryPathRequest(binary_name=binary_name, search_path=search_path)
        for binary_name in (*tools, *optional_tools)
    ]
    paths = await concurrently(find_binary(request, **implicitly()) for request in requests)
    required_tools_paths = [
        path.first_path_or_raise(request, rationale=rationale)
        for request, path in zip(requests, paths)
        if request.binary_name in tools
    ]
    optional_tools_paths = [
        path.first_path
        for request, path in zip(requests, paths)
        if request.binary_name in optional_tools and path.first_path
    ]

    tools_shims = await create_binary_shims(
        BinaryShimsRequest.for_paths(
            *required_tools_paths,
            *optional_tools_paths,
            rationale=rationale,
        ),
        **implicitly(),
    )

    return tools_shims


@rule(level=LogLevel.DEBUG)
async def node_process_environment(
    binaries: NodeJSBinaries,
    nodejs: NodeJS,
    nodejs_environment: NodeJS.EnvironmentAware,
) -> NodeJSProcessEnvironment:
    default_required_tools = ["sh", "bash"]
    tools_used_by_setup_scripts = ["mkdir", "rm", "touch", "which"]
    pnpm_shim_tools = ["sed", "dirname"]

    binary_shims = await get_nodejs_process_tools_shims(
        tools=[
            *default_required_tools,
            *tools_used_by_setup_scripts,
            *pnpm_shim_tools,
            *nodejs.tools,
        ],
        optional_tools=nodejs.optional_tools,
        search_path=nodejs_environment.executable_search_path,
        rationale="execute a nodejs process",
    )
    corepack_env_vars = await environment_vars_subset_get(
        EnvironmentVarsRequest(nodejs_environment.corepack_env_vars), **implicitly()
    )
    binary_digest_with_shims = await add_corepack_shims_to_digest(
        binaries, binary_shims, corepack_env_vars
    )
    binaries = NodeJSBinaries(binaries.binary_dir, binary_digest_with_shims)

    return NodeJSProcessEnvironment(
        binaries=binaries,
        npm_config_cache="._npm",
        tool_binaries=binary_shims,
        corepack_home="._corepack_home",
        corepack_shims=os.path.join(
            "{chroot}", NodeJSProcessEnvironment.base_bin_dir, "._corepack"
        ),
        corepack_env_vars=corepack_env_vars,
    )


@dataclass(frozen=True)
class NodeJSBootstrap:
    nodejs_search_paths: tuple[str, ...]


async def _get_nvm_root() -> str | None:
    """See https://github.com/nvm-sh/nvm#installing-and-updating."""

    env = await environment_vars_subset(
        EnvironmentVarsRequest(("NVM_DIR", "XDG_CONFIG_HOME", "HOME")), **implicitly()
    )
    nvm_dir = env.get("NVM_DIR")
    default_dir = env.get("XDG_CONFIG_HOME", env.get("HOME"))
    if nvm_dir:
        return nvm_dir
    elif default_dir:
        return os.path.join(default_dir, ".nvm")
    return None


async def _nodejs_search_paths(
    env_tgt: EnvironmentTarget, paths: Collection[str]
) -> tuple[str, ...]:
    asdf_result = await AsdfToolPathsResult.get_un_cachable_search_paths(
        paths,
        env_tgt=env_tgt,
        tool_name="nodejs",
        tool_description="Node.js distribution",
        paths_option_name=f"[{NodeJS.options_scope}].search_path",
    )
    asdf_standard_tool_paths = asdf_result.standard_tool_paths
    asdf_local_tool_paths = asdf_result.local_tool_paths
    special_strings: dict[str, Iterable[str]] = {
        AsdfPathString.STANDARD: asdf_standard_tool_paths,
        AsdfPathString.LOCAL: asdf_local_tool_paths,
    }
    nvm_dir = await _get_nvm_root()
    expanded: list[str] = []
    nvm_path_results = await concurrently(
        get_un_cachable_version_manager_paths(
            VersionManagerSearchPathsRequest(
                env_tgt,
                nvm_dir,
                "versions/node",
                f"[{NodeJS.options_scope}].search_path",
                (".nvmrc",),
                s if s == "<NVM_LOCAL>" else None,
            ),
        )
        for s in paths
        if s == "<NVM>" or s == "<NVM_LOCAL>"
    )
    for nvm_path in FrozenOrderedSet(itertools.chain.from_iterable(nvm_path_results)):
        expanded.append(nvm_path)
    for s in paths:
        if s == "<PATH>":
            expanded.extend(await environment_path_variable(**implicitly()))  # noqa: PNT30: Linear search
        elif s in special_strings:
            expanded.extend(special_strings[s])
        elif s == "<NVM>" or s == "<NVM_LOCAL>":
            continue
        else:
            expanded.append(s)
    return tuple(expanded)


@rule
async def nodejs_bootstrap(nodejs_env_aware: NodeJS.EnvironmentAware) -> NodeJSBootstrap:
    search_paths = await validate_search_paths(
        ValidateSearchPathsRequest(
            env_tgt=nodejs_env_aware.env_tgt,
            search_paths=tuple(nodejs_env_aware.search_path),
            option_origin=f"[{NodeJS.options_scope}].search_path",
            environment_key="nodejs_search_path",
            is_default=nodejs_env_aware._is_default("search_path"),
            local_only=FrozenOrderedSet(
                (AsdfPathString.STANDARD, AsdfPathString.LOCAL, "<NVM>", "<NVM_LOCAL>")
            ),
        )
    )

    expanded_paths = await _nodejs_search_paths(nodejs_env_aware.env_tgt, search_paths)

    return NodeJSBootstrap(nodejs_search_paths=expanded_paths)


class _BinaryPathsPerVersion(FrozenDict[str, Sequence[BinaryPath]]):
    pass


@rule(level=LogLevel.DEBUG, desc="Testing for Node.js binaries.")
async def get_valid_nodejs_paths_by_version(bootstrap: NodeJSBootstrap) -> _BinaryPathsPerVersion:
    paths = await find_binary(
        BinaryPathRequest(
            search_path=bootstrap.nodejs_search_paths,
            binary_name="node",
            test=BinaryPathTest(
                ["--version"], fingerprint_stdout=False
            ),  # Hack to retain version info
        ),
        **implicitly(),
    )

    group_by_version = groupby((path for path in paths.paths), key=lambda path: path.fingerprint)
    return _BinaryPathsPerVersion({version: tuple(paths) for version, paths in group_by_version})


@rule(level=LogLevel.DEBUG, desc="Finding Node.js distribution binaries.")
async def determine_nodejs_binaries(
    nodejs: NodeJS, platform: Platform, paths_per_version: _BinaryPathsPerVersion
) -> NodeJSBinaries:
    decoded_versions = groupby(
        (ExternalToolVersion.decode(unparsed) for unparsed in nodejs.known_versions),
        lambda v: v.version,
    )

    decoded_per_version = {
        version: tuple(
            known_version
            for known_version in known_versions
            if known_version.platform == platform.value
        )
        for version, known_versions in decoded_versions
    }

    satisfying_version = min_satisfying(decoded_per_version.keys(), nodejs.version)
    if satisfying_version:
        known_version = decoded_per_version[satisfying_version][0]
        downloaded = await nodejs.download_known_version(known_version, platform)
        nodejs_bin_dir = os.path.join(
            "{chroot}",
            NodeJSProcessEnvironment.base_bin_dir,
            os.path.dirname(downloaded.exe),
        )

        return NodeJSBinaries(nodejs_bin_dir, downloaded.digest)

    satisfying_version = min_satisfying(paths_per_version.keys(), nodejs.version)
    if not satisfying_version:
        raise BinaryNotFoundError(
            softwrap(
                f"""
                Cannot find any `node` binaries satisfying the range '{nodejs.version}'.

                To fix, either list a `[{NodeJS.options_scope}].known_versions` version that satisfies the range,
                or ensure `[{NodeJS.options_scope}].search_path` contains a path to binaries that satisfy the range.
                """
            )
        )
    return NodeJSBinaries(os.path.dirname(paths_per_version[satisfying_version][0].path))


@dataclass(frozen=True)
class CorepackToolRequest:
    tool: str
    version: str | None = None


@dataclass(frozen=True)
class CorepackToolDigest:
    digest: Digest


@rule(desc="Preparing Corepack managed tool.")
async def prepare_corepack_tool(
    request: CorepackToolRequest, environment: NodeJSProcessEnvironment, nodejs: NodeJS
) -> CorepackToolDigest:
    version = request.version or nodejs.package_managers.get(request.tool)
    tool_spec = f"{request.tool}@{version}" if version else request.tool
    tool_description = tool_spec if version else f"default {tool_spec} version"
    result = await fallible_to_exec_result_or_raise(
        **implicitly(
            Process(
                argv=filter(
                    None, ("corepack", "prepare", tool_spec if version else "--all", "--activate")
                ),
                description=f"Preparing configured {tool_description}.",
                immutable_input_digests=environment.immutable_digest(),
                level=LogLevel.DEBUG,
                env=environment.to_env_dict(),
                append_only_caches={**environment.append_only_caches},
                output_directories=[environment.corepack_home],
            )
        )
    )
    return CorepackToolDigest(result.output_digest)


@rule(level=LogLevel.DEBUG)
async def setup_node_tool_process(
    request: NodeJSToolProcess, environment: NodeJSProcessEnvironment
) -> Process:
    if request.tool in ("npm", "npx", "pnpm", "yarn"):
        tool_name = request.tool.replace("npx", "npm")
        corepack_tool = await prepare_corepack_tool(
            CorepackToolRequest(tool_name, request.tool_version), **implicitly()
        )
        input_digest = await merge_digests(
            MergeDigests([request.input_digest, corepack_tool.digest])
        )
    else:
        input_digest = request.input_digest
    return Process(
        argv=list(filter(None, (request.tool, *request.args))),
        input_digest=input_digest,
        output_files=request.output_files,
        immutable_input_digests=environment.immutable_digest(),
        output_directories=request.output_directories,
        description=request.description,
        level=request.level,
        env=environment.to_env_dict(request.extra_env),
        working_directory=request.working_directory,
        append_only_caches={**request.append_only_caches, **environment.append_only_caches},
        timeout_seconds=request.timeout_seconds,
    )


class UserChosenNodeJSResolveAliases(FrozenDict[str, str]):
    pass


@rule(level=LogLevel.DEBUG)
async def user_chosen_resolve_aliases(nodejs: NodeJS) -> UserChosenNodeJSResolveAliases:
    return UserChosenNodeJSResolveAliases((value, key) for key, value in nodejs.resolves.items())


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        *external_tool_rules(),
        *asdf.rules(),
        *system_binaries.rules(),
        *search_paths.rules(),
    )
