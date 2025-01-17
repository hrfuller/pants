# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os
import sys
from builtins import filter, object
from collections import defaultdict
from contextlib import contextmanager

from future.utils import PY3
from twitter.common.collections import OrderedSet

from pants.base.build_environment import get_buildroot, get_scm
from pants.base.deprecated import deprecated
from pants.base.worker_pool import SubprocPool
from pants.base.workunit import WorkUnit, WorkUnitLabel
from pants.build_graph.target import Target
from pants.engine.isolated_process import (FallibleExecuteProcessResult,
                                           fallible_to_exec_result_or_raise)
from pants.goal.products import Products
from pants.goal.workspace import ScmWorkspace
from pants.process.lock import OwnerPrintingInterProcessFileLock
from pants.source.source_root import SourceRootConfig


class Context(object):
  """Contains the context for a single run of pants.

  Task implementations can access configuration data from pants.ini and any flags they have exposed
  here as well as information about the targets involved in the run.

  Advanced uses of the context include adding new targets to it for upstream or downstream goals to
  operate on and mapping of products a goal creates to the targets the products are associated with.

  :API: public
  """

  # TODO: Figure out a more structured way to construct and use context than this big flat
  # repository of attributes?
  def __init__(self, options, run_tracker, target_roots,
               requested_goals=None, target_base=None, build_graph=None,
               build_file_parser=None, build_configuration=None,
               address_mapper=None, console_outstream=None, scm=None,
               workspace=None, invalidation_report=None, scheduler=None):
    self._options = options

    # We register a callback that will cause build graph edits to invalidate our caches, and we hold
    # a handle directly to the callback function to ensure that it is not GC'd until the context is.
    self.build_graph = build_graph
    self._clear_target_cache_handle = self._clear_target_cache
    self._targets_cache = dict()
    self.build_graph.add_invalidation_callback(self._clear_target_cache_handle)

    self._build_file_parser = build_file_parser
    self.build_configuration = build_configuration
    self.address_mapper = address_mapper
    self.run_tracker = run_tracker
    self._log = run_tracker.logger
    self._target_base = target_base or Target
    self._products = Products()
    self._buildroot = get_buildroot()
    self._source_roots = SourceRootConfig.global_instance().get_source_roots()
    self._lock = OwnerPrintingInterProcessFileLock(os.path.join(self._buildroot, '.pants.workdir.file_lock'))
    self._java_sysprops = None  # Computed lazily.
    self.requested_goals = requested_goals or []
    self._console_outstream = console_outstream or (sys.stdout.buffer if PY3 else sys.stdout)
    self._scm = scm or get_scm()
    self._workspace = workspace or (ScmWorkspace(self._scm) if self._scm else None)
    self._replace_targets(target_roots)
    self._invalidation_report = invalidation_report
    self._scheduler = scheduler

  @property
  @deprecated('1.17.0.dev2', hint_message='Use the build_configuration property.')
  def build_file_parser(self):
    return self._build_file_parser

  @property
  def options(self):
    """Returns the new-style options.

    :API: public
    """
    return self._options

  @property
  def log(self):
    """Returns the preferred logger for goals to use.

    :API: public
    """
    return self._log

  @property
  def products(self):
    """Returns the Products manager for the current run.

    :API: public
    """
    return self._products

  @property
  def source_roots(self):
    """Returns the :class:`pants.source.source_root.SourceRoots` instance for the current run.

    :API: public
    """
    return self._source_roots

  @property
  def target_roots(self):
    """Returns the targets specified on the command line.

    This set is strictly a subset of all targets in play for the run as returned by self.targets().
    Note that for a command line invocation that uses wildcard selectors : or ::, the targets
    globbed by the wildcards are considered to be target roots.

    :API: public
    """
    return self._target_roots

  @property
  def console_outstream(self):
    """Returns the output stream to write console messages to.

    :API: public
    """
    return self._console_outstream

  @property
  def scm(self):
    """Returns the current workspace's scm, if any.

    :API: public
    """
    return self._scm

  @property
  def workspace(self):
    """Returns the current workspace, if any."""
    return self._workspace

  @property
  def invalidation_report(self):
    return self._invalidation_report

  def __str__(self):
    ident = Target.identify(self.targets())
    return 'Context(id:{}, targets:{})'.format(ident, self.targets())

  @contextmanager
  def executing(self):
    """A contextmanager that sets metrics in the context of a (v1) engine execution."""
    self._set_target_root_count_in_runtracker()
    yield
    metrics = self._scheduler.metrics()
    self.run_tracker.pantsd_stats.set_scheduler_metrics(metrics)
    engine_workunits = self._scheduler.engine_workunits(metrics)
    if engine_workunits:
      self.run_tracker.report.bulk_record_workunits(engine_workunits)
    self._set_affected_target_count_in_runtracker()

  def _set_target_root_count_in_runtracker(self):
    """Sets the target root count in the run tracker's daemon stats object."""
    # N.B. `self._target_roots` is always an expanded list of `Target` objects as
    # provided by `GoalRunner`.
    target_count = len(self._target_roots)
    self.run_tracker.pantsd_stats.set_target_root_size(target_count)
    return target_count

  def _set_affected_target_count_in_runtracker(self):
    """Sets the realized target count in the run tracker's daemon stats object."""
    target_count = len(self.build_graph)
    self.run_tracker.pantsd_stats.set_affected_targets_size(target_count)
    return target_count

  def submit_background_work_chain(self, work_chain, parent_workunit_name=None):
    """
    :API: public
    """
    background_root_workunit = self.run_tracker.get_background_root_workunit()
    if parent_workunit_name:
      # We have to keep this workunit alive until all its child work is done, so
      # we manipulate the context manually instead of using it as a contextmanager.
      # This is slightly funky, but the with-context usage is so pervasive and
      # useful elsewhere that it's worth the funkiness in this one place.
      workunit_parent_ctx = self.run_tracker.new_workunit_under_parent(
        name=parent_workunit_name, labels=[WorkUnitLabel.MULTITOOL], parent=background_root_workunit)
      workunit_parent = workunit_parent_ctx.__enter__()
      done_hook = lambda: workunit_parent_ctx.__exit__(None, None, None)
    else:
      workunit_parent = background_root_workunit  # Run directly under the root.
      done_hook = None
    self.run_tracker.background_worker_pool().submit_async_work_chain(
      work_chain, workunit_parent=workunit_parent, done_hook=done_hook)

  def background_worker_pool(self):
    """Returns the pool to which tasks can submit background work.

    :API: public
    """
    return self.run_tracker.background_worker_pool()

  def subproc_map(self, f, items):
    """Map function `f` over `items` in subprocesses and return the result.

      :API: public

      :param f: A multiproc-friendly (importable) work function.
      :param items: A iterable of pickleable arguments to f.
    """
    try:
      # Pool.map (and async_map().get() w/o timeout) can miss SIGINT.
      # See: http://stackoverflow.com/a/1408476, http://bugs.python.org/issue8844
      # Instead, we map_async(...), wait *with a timeout* until ready, then .get()
      # NB: in 2.x, wait() with timeout wakes up often to check, burning CPU. Oh well.
      res = SubprocPool.foreground().map_async(f, items)
      while not res.ready():
        res.wait(60)  # Repeatedly wait for up to a minute.
        if not res.ready():
          self.log.debug('subproc_map result still not ready...')
      return res.get()
    except KeyboardInterrupt:
      SubprocPool.shutdown(True)
      raise

  @contextmanager
  def new_workunit(self, name, labels=None, cmd='', log_config=None):
    """Create a new workunit under the calling thread's current workunit.

    :API: public
    """
    with self.run_tracker.new_workunit(name=name, labels=labels, cmd=cmd, log_config=log_config) as workunit:
      yield workunit

  def acquire_lock(self):
    """ Acquire the global lock for the root directory associated with this context. When
    a goal requires serialization, it will call this to acquire the lock.

    :API: public
    """
    if self.options.for_global_scope().lock:
      if not self._lock.acquired:
        self._lock.acquire()

  def release_lock(self):
    """Release the global lock if it's held.
    Returns True if the lock was held before this call.

    :API: public
    """
    if not self._lock.acquired:
      return False
    else:
      self._lock.release()
      return True

  def is_unlocked(self):
    """Whether the global lock object is actively holding the lock.

    :API: public
    """
    return not self._lock.acquired

  def _replace_targets(self, target_roots):
    # Replaces all targets in the context with the given roots and their transitive dependencies.
    #
    # If another task has already retrieved the current targets, mutable state may have been
    # initialized somewhere, making it now unsafe to replace targets. Thus callers of this method
    # must know what they're doing!
    #
    # TODO(John Sirois): This currently has only 1 use (outside ContextTest) in pantsbuild/pants and
    # only 1 remaining known use case in the Foursquare codebase that will be able to go away with
    # the post RoundEngine engine - kill the method at that time.
    self._target_roots = list(target_roots)
    self._clear_target_cache()

  def _clear_target_cache(self):
    """A callback for cases where the graph or target roots have been mutated.

    See BuildGraph.add_invalidation_callback.
    """
    self._targets_cache.clear()

  def add_new_target(self, address, target_type, target_base=None, dependencies=None,
                     derived_from=None, **kwargs):
    """Creates a new target, adds it to the context and returns it.

    This method ensures the target resolves files against the given target_base, creating the
    directory if needed and registering a source root.

    :API: public
    """
    self._clear_target_cache()
    rel_target_base = target_base or address.spec_path
    abs_target_base = os.path.join(get_buildroot(), rel_target_base)
    if not os.path.exists(abs_target_base):
      os.makedirs(abs_target_base)
      # TODO: Adding source roots on the fly like this is yucky, but hopefully this
      # method will go away entirely under the new engine. It's primarily used for injecting
      # synthetic codegen targets, and that isn't how codegen will work in the future.
    if not self.source_roots.find_by_path(rel_target_base):
      # TODO: Set the lang and root category (source/test/thirdparty) based on the target type?
      self.source_roots.add_source_root(rel_target_base)
    if dependencies:
      dependencies = [dep.address for dep in dependencies]

    self.build_graph.inject_synthetic_target(address=address,
                                             target_type=target_type,
                                             dependencies=dependencies,
                                             derived_from=derived_from,
                                             **kwargs)
    new_target = self.build_graph.get_target(address)

    return new_target

  def targets(self, predicate=None, **kwargs):
    """Selects targets in-play in this run from the target roots and their transitive dependencies.

    Also includes any new synthetic targets created from the target roots or their transitive
    dependencies during the course of the run.

    See Target.closure_for_targets for remaining parameters.

    :API: public

    :param predicate: If specified, the predicate will be used to narrow the scope of targets
                      returned.
    :param bool postorder: `True` to gather transitive dependencies with a postorder traversal;
                          `False` or preorder by default.
    :returns: A list of matching targets.
    """
    targets_cache_key = tuple(sorted(kwargs))
    targets = self._targets_cache.get(targets_cache_key)
    if targets is None:
      self._targets_cache[targets_cache_key] = targets = self._unfiltered_targets(**kwargs)
    return list(filter(predicate, targets))

  def _unfiltered_targets(self, **kwargs):
    def _collect_targets(root_targets, **kwargs):
      return Target.closure_for_targets(
        target_roots=root_targets,
        **kwargs
      )

    target_set = _collect_targets(self.target_roots, **kwargs)

    synthetics = OrderedSet()
    for synthetic_address in self.build_graph.synthetic_addresses:
      if self.build_graph.get_concrete_derived_from(synthetic_address) in target_set:
        synthetics.add(self.build_graph.get_target(synthetic_address))
    target_set.update(_collect_targets(synthetics, **kwargs))

    return target_set

  def dependents(self, on_predicate=None, from_predicate=None):
    """Returns  a map from targets that satisfy the from_predicate to targets they depend on that
      satisfy the on_predicate.

    :API: public
    """
    core = set(self.targets(on_predicate))
    dependees = defaultdict(set)
    for target in self.targets(from_predicate):
      for dependency in target.dependencies:
        if dependency in core:
          dependees[target].add(dependency)
    return dependees

  def resolve(self, spec):
    """Returns an iterator over the target(s) the given address points to.

    :API: public
    """
    return self.build_graph.resolve(spec)

  def scan(self, root=None):
    """Scans and parses all BUILD files found under ``root``.

    Only BUILD files found under ``root`` are parsed as roots in the graph, but any dependencies of
    targets parsed in the root tree's BUILD files will be followed and this may lead to BUILD files
    outside of ``root`` being parsed and included in the returned build graph.

    :API: public

    :param string root: The path to scan; by default, the build root.
    :returns: A new build graph encapsulating the targets found.
    """
    build_graph = self.build_graph.clone_new()
    for address in self.address_mapper.scan_addresses(root):
      build_graph.inject_address_closure(address)
    return build_graph

  def execute_process_synchronously_without_raising(self, execute_process_request, name, labels=None):
    """Executes a process (possibly remotely), and returns information about its output.

    :param execute_process_request: The ExecuteProcessRequest to run.
    :param name: A descriptive name representing the process being executed.
    :param labels: A tuple of WorkUnitLabels.
    :return: An ExecuteProcessResult with information about the execution.

    Note that this is an unstable, experimental API, which is subject to change with no notice.
    """
    with self.new_workunit(
      name=name,
      labels=labels,
      cmd=' '.join(execute_process_request.argv),
    ) as workunit:
      result = self._scheduler.product_request(FallibleExecuteProcessResult, [execute_process_request])[0]
      workunit.output("stdout").write(result.stdout)
      workunit.output("stderr").write(result.stderr)
      workunit.set_outcome(WorkUnit.FAILURE if result.exit_code else WorkUnit.SUCCESS)
      return result

  def execute_process_synchronously_or_raise(self, execute_process_request, name, labels=None):
    """Execute process synchronously, and throw if the return code is not 0.

    See execute_process_synchronously for the api docs.
    """
    fallible_result = self.execute_process_synchronously_without_raising(execute_process_request, name, labels)
    return fallible_to_exec_result_or_raise(
      fallible_result,
      execute_process_request
    )
