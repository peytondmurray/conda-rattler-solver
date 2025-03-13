from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pprint import pformat
from typing import TYPE_CHECKING

from boltons.setutils import IndexedSet
from conda.base.constants import ChannelPriority
from conda.base.context import context
from conda.common.constants import NULL

from conda.common.io import Spinner
from conda.models.match_spec import MatchSpec
from conda.models.records import PackageRecord
from conda_libmamba_solver.solver import LibMambaSolver
from conda_libmamba_solver.state import SolverInputState, SolverOutputState
from libmambapy.solver import Request
from rattler import (
    Gateway,
    solve,
)
from rattler import VirtualPackage as RattlerVirtualPackage
from rattler import __version__ as rattler_version
from rattler.exceptions import SolverError as RattlerSolverError

from . import __version__, interop
from .exceptions import RattlerUnsatisfiableError
from .index import RattlerIndexHelper

if TYPE_CHECKING:
    from boltons.setutils import IndexedSet
    from conda.auxlib import _Null
    from conda.base.constants import (
        DepsModifier,
        UpdateModifier,
    )
    from libmambapy.solver.libsolv import Solution, UnSolvable


logger = logging.getLogger(f"conda.{__name__}")

@dataclass
class CondaPlan:
    """A plan for a list of changes to packages in an environment."""
    freeze: list[MatchSpec] = field(default_factory=list)
    install: list[MatchSpec] = field(default_factory=list)
    keep: list[MatchSpec] = field(default_factory=list)
    pin: list[MatchSpec] = field(default_factory=list)
    remove: list[MatchSpec] = field(default_factory=list)
    update: list[MatchSpec] = field(default_factory=list)

    def __iter__(self):
        yield from itertools.chain(
            self.freeze,
            self.install,
            self.keep,
            self.pin,
            self.remove,
            self.update,
        )


class RattlerSolver(LibMambaSolver):
    @staticmethod
    @lru_cache(maxsize=None)
    def user_agent() -> str:
        """
        Expose this identifier to allow conda to extend its user agent if required
        """
        return f"conda-rattler-solver/{__version__} py-rattler/{rattler_version}"

    def solve_final_state(
        self,
        update_modifier: UpdateModifier | _Null = NULL,
        deps_modifier: DepsModifier | _Null = NULL,
        prune: bool | _Null = NULL,
        ignore_pinned: bool | _Null = NULL,
        force_remove: bool | _Null = NULL,
        should_retry_solve: bool = False,
    ) -> IndexedSet[PackageRecord]:
        in_state = SolverInputState(
            prefix=self.prefix,
            requested=self.specs_to_add or self.specs_to_remove,
            update_modifier=update_modifier,
            deps_modifier=deps_modifier,
            prune=prune,
            ignore_pinned=ignore_pinned,
            force_remove=force_remove,
            command=self._command,
        )

        out_state = SolverOutputState(solver_input_state=in_state)

        # These tasks do _not_ require a solver...
        # TODO: Abstract away in the base class?
        none_or_final_state = out_state.early_exit()
        if none_or_final_state is not None:
            return none_or_final_state

        all_channels = [
            *self.channels,
            *in_state.channels_from_specs(),
            *in_state.maybe_free_channel(),
        ]
        logger.info("Channels: %s", pformat(all_channels))

        with Spinner(
            self._collect_all_metadata_spinner_message(all_channels),
            enabled=not context.verbosity and not context.quiet,
            json=context.json,
        ):
            index = RattlerIndexHelper(all_channels, self.subdirs, self._repodata_fn)

        with Spinner(
            "Solving environment",
            enabled=not context.verbosity and not context.quiet,
            json=context.json,
        ):
            try:
                records = self._solve_attempt(in_state, out_state, index)
                self._export_solved_records(records, out_state)
            except RattlerSolverError as exc:
                exc2 = RattlerUnsatisfiableError(str(exc))
                exc2.allow_retry = False
                raise exc2 from exc

        # Run post-solve tasks
        out_state.post_solve(solver=self)
        self.neutered_specs = tuple(out_state.neutered.values())

        return out_state.current_solution

    def _specs_to_plan(
        self,
        in_state: SolverInputState,
        out_state: SolverOutputState,
    ) -> CondaPlan:
        if in_state.is_removing:
            jobs = self._specs_to_request_jobs_remove(in_state, out_state)
        elif self._called_from_conda_build():
            jobs = self._specs_to_request_jobs_conda_build(in_state, out_state)
        else:
            jobs = self._specs_to_request_jobs_add(in_state, out_state)

        plan = CondaPlan()
        for JobType, job_specs in jobs.items():
            # Convert all specs to MatchSpec
            specs: MatchSpec = []
            for spec in job_specs:
                if isinstance(spec, str):
                    spec = MatchSpec(spec)
                specs.append(spec)

            if JobType == Request.Freeze:
                plan.freeze = specs
            elif JobType == Request.Install:
                plan.install = specs
            elif JobType == Request.Keep:
                plan.keep = specs
            elif JobType == Request.Pin:
                plan.pin = specs
                for idx, spec in enumerate(specs, 1):
                    out_state.pins[f"pin-{idx}"] = MatchSpec(spec)
            elif JobType == Request.Remove:
                plan.remove = specs
            elif JobType == Request.Update:
                plan.update = specs
            else:
                raise ValueError(f"Unknown job type: {JobType.__name__} for specs {specs}")

        return plan

    def _solve_attempt(
        self,
        in_state: SolverInputState,
        out_state: SolverOutputState,
        index: RattlerIndexHelper,
    ) -> tuple[bool, Solution | UnSolvable]:
        out_state.check_for_pin_conflicts(index)
        logger.debug("Current conflicts (including learnt ones): %r", out_state.conflicts)
        plan = self._specs_to_plan(in_state, out_state)

        # TODO: convert MatchSpec objects to RepoDataRecord objects for `solve`
        specs = interop.convert_spec(list(plan))

        # Flags are copied from conda_libmamba_solver.solver.Solver._solver_flags
        result = asyncio.run(
            solve(
                channels=interop.convert_channels(index._channels),
                specs=specs,
                # specs=conda_to_rattler_spec(plan.install),
                gateway=Gateway(),
                platforms=None,
                # locked_packages=conda_to_rattler_record(plan.freeze),
                # pinned_packages=conda_to_rattler_record(plan.pin),
                virtual_packages=RattlerVirtualPackage.detect(),
                timeout=None,
                channel_priority=interop.convert_channel_priority(
                    context.channel_priority if context.channel_priority else ChannelPriority.STRICT
                ),
                exclude_newer=None,
                strategy="highest",
                constraints=None,
            )
        )

        # # TODO: This is a hack to get the installed packages into the solver
        # # but rattler doesn't allow PrefixRecords to be passed in yet
        # rattler_installed = {}
        # for json_path in Path(self.prefix).glob("conda-meta/*.json"):
        #     name = json_path.stem.rsplit("-", 2)[0]
        #     record = RattlerPrefixRecord.from_path(json_path)
        #     rattler_installed[name] = record

        # specs = []
        # pins = []
        # locked = []
        # for (task_name, _), task_specs in tasks.items():
        #     if task_name in ("INSTALL", "UPDATE"):
        #         specs.extend(task_specs)
        #     # TODO
        #     elif task_name in ("ADD_PIN", "USERINSTALLED"):
        #         for spec in task_specs:
        #             for record in in_state.installed.values():
        #                 if MatchSpec(spec).match(record):
        #                     pins.append(rattler_installed[record.name])
        #     elif task_name == "LOCK":
        #         for spec in task_specs:
        #             for record in in_state.installed.values():
        #                 if MatchSpec(spec).match(record):
        #                     locked.append(rattler_installed[record.name])

        return result

    def _export_solved_records(self, records, out_state):
        for record in records:
            out_state.records[record.name] = PackageRecord(
                arch=record.arch,
                build=record.build,
                build_number=record.build_number,
                channel=record.channel,
                constrains=record.constrains or (),
                # date=record.date, #! TODO: MISSING
                depends=record.depends or (),
                features=record.features or (),
                fn=record.file_name,
                legacy_bz2_md5=record.legacy_bz2_md5.hex() if record.legacy_bz2_md5 else None,
                legacy_bz2_size=record.legacy_bz2_size,
                license=record.license,
                license_family=record.license_family,
                md5=record.md5.hex(),
                name=record.name.source,
                # noarch=record.noarch,  #! TODO: MISSING
                # package_type=record.package_type, #! TODO: MISSING
                platform=record.platform,
                # preferred_env=record.preferred_env, #! TODO: MISSING
                sha256=record.sha256.hex(),
                size=record.size or 0,
                subdir=record.subdir,
                timestamp=record.timestamp.toordinal() or 0,
                track_features=record.track_features or (),
                url=record.url,
                version=str(record.version),
            )
