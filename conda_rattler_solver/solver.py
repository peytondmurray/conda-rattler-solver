from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from functools import cache
from pprint import pformat
from typing import TYPE_CHECKING

import rattler
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
    @cache
    def user_agent() -> str:
        """Expose this identifier to allow conda to extend its user agent if required."""
        return f"conda-rattler-solver/{__version__} py-rattler/{rattler_version}"

    def solve_final_state(
        self,
        update_modifier: UpdateModifier | _Null = NULL,
        deps_modifier: DepsModifier | _Null = NULL,
        prune: bool | _Null = NULL,
        ignore_pinned: bool | _Null = NULL,
        force_remove: bool | _Null = NULL,
        _should_retry_solve: bool = False,
    ) -> IndexedSet:
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
        for job_type, job_specs in jobs.items():
            # Convert all specs to MatchSpec
            specs: list[MatchSpec] = []
            for spec in job_specs:
                if isinstance(spec, str):
                    spec = MatchSpec(spec)
                specs.append(spec)

            if job_type == Request.Freeze:
                plan.freeze = specs
            elif job_type == Request.Install:
                plan.install = specs
            elif job_type == Request.Keep:
                plan.keep = specs
            elif job_type == Request.Pin:
                plan.pin = specs
                for idx, spec in enumerate(specs, 1):
                    out_state.pins[f"pin-{idx}"] = MatchSpec(spec)
            elif job_type == Request.Remove:
                plan.remove = specs
            elif job_type == Request.Update:
                plan.update = specs
            else:
                raise ValueError(f"Unknown job type: {job_type.__name__} for specs {specs}")

        return plan

    def _solve_attempt(
        self,
        in_state: SolverInputState,
        out_state: SolverOutputState,
        index: RattlerIndexHelper,
    ) -> list[rattler.RepoDataRecord]:
        out_state.check_for_pin_conflicts(index)
        logger.debug("Current conflicts (including learnt ones): %r", out_state.conflicts)
        plan = self._specs_to_plan(in_state, out_state)

        # TODO: convert MatchSpec objects to RepoDataRecord objects for `solve`
        specs = interop.convert_spec(list(plan))

        # Flags are copied from conda_libmamba_solver.solver.Solver._solver_flags
        return asyncio.run(
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

    def _export_solved_records(
        self,
        records: list[rattler.RepoDataRecord],
        out_state: SolverOutputState,
    ) -> None:
        out_state.records.update(
            interop.rattler_repodatarecords_to_conda(records)
        )
