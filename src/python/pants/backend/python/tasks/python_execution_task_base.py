# coding=utf-8
# Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os
from builtins import str

from future.utils import binary_type, text_type
from pex.interpreter import PythonInterpreter
from pex.pex import PEX

from pants.backend.python.subsystems.python_setup import PythonSetup
from pants.backend.python.targets.python_distribution import PythonDistribution
from pants.backend.python.targets.python_requirement_library import PythonRequirementLibrary
from pants.backend.python.targets.python_target import PythonTarget
from pants.backend.python.tasks.gather_sources import GatherSources
from pants.backend.python.tasks.resolve_requirements import ResolveRequirements
from pants.backend.python.tasks.resolve_requirements_task_base import ResolveRequirementsTaskBase
from pants.build_graph.files import Files
from pants.invalidation.cache_manager import VersionedTargetSet
from pants.util.contextutil import temporary_file
from pants.util.objects import datatype


def ensure_interpreter_search_path_env(interpreter):
  """Produces an environment dict that ensures that the given interpreter is discovered at runtime.

  At build time, a pex contains constraints on which interpreter version ranges are legal, but
  at runtime it will apply those constraints to the current (PEX_PYTHON_)PATH to locate a
  relevant interpreter.

  This environment is for use at runtime to ensure that a particular interpreter (which should
  match the constraints that were provided at build time) is locatable. PEX no longer allows
  for forcing use of a particular interpreter (the PEX_PYTHON_PATH is additive), so use
  of this method does not guarantee that the given interpreter is used: rather, that it is
  definitely considered for use.

  Subclasses of PythonExecutionTaskBase can use `self.ensure_interpreter_search_path_env`
  to get the relevant interpreter, but this method is exposed as static for cases where
  the building of the pex is separated from the execution of the pex.
  """
  chosen_interpreter_binary_path = interpreter.binary
  return {
    'PEX_IGNORE_RCFILES': '1',
    'PEX_PYTHON': chosen_interpreter_binary_path,
    'PEX_PYTHON_PATH': chosen_interpreter_binary_path,
  }


class PythonExecutionTaskBase(ResolveRequirementsTaskBase):
  """Base class for tasks that execute user Python code in a PEX environment.

  Note: Extends ResolveRequirementsTaskBase because it may need to resolve
  extra requirements in order to execute the code.
  """

  @classmethod
  def prepare(cls, options, round_manager):
    super(PythonExecutionTaskBase, cls).prepare(options, round_manager)
    round_manager.require_data(PythonInterpreter)
    round_manager.require_data(ResolveRequirements.REQUIREMENTS_PEX)
    round_manager.require_data(GatherSources.PYTHON_SOURCES)

  def extra_requirements(self):
    """Override to provide extra requirements needed for execution.

    :returns: An iterable of pip-style requirement strings.
    :rtype: :class:`collections.Iterable` of str
    """
    return ()

  class ExtraFile(datatype([('path', text_type), ('content', binary_type)])):
    """Models an extra file to place in a PEX."""

    @classmethod
    def empty(cls, path):
      """Creates an empty file with the given PEX path.

      :param str path: The path this extra file should have when added to a PEX.
      :rtype: :class:`ExtraFile`
      """
      return cls(path=path, content=b'')

    def add_to(self, builder):
      """Adds this extra file to a PEX builder.

      :param builder: The PEX builder to add this extra file to.
      :type builder: :class:`pex.pex_builder.PEXBuilder`
      """
      with temporary_file() as fp:
        fp.write(self.content)
        fp.close()
        add = builder.add_source if self.path.endswith('.py') else builder.add_resource
        add(fp.name, self.path)

  @classmethod
  def subsystem_dependencies(cls):
    return super(PythonExecutionTaskBase, cls).subsystem_dependencies() + (PythonSetup,)

  def extra_files(self):
    """Override to provide extra files needed for execution.

    :returns: An iterable of extra files to add to the PEX.
    :rtype: :class:`collections.Iterable` of :class:`PythonExecutionTaskBase.ExtraFile`
    """
    return ()

  def ensure_interpreter_search_path_env(self):
    """See ensure_interpreter_search_path_env."""
    return ensure_interpreter_search_path_env(self.context.products.get_data(PythonInterpreter))

  def create_pex(self, pex_info=None):
    """Returns a wrapped pex that "merges" other pexes produced in previous tasks via PEX_PATH.

    This method always creates a PEX to run locally on the current platform and selected
    interpreter: to create a pex that is distributable to other environments, use the pex_build_util
    Subsystem.

    The returned pex will have the pexes from the ResolveRequirements and GatherSources tasks mixed
    into it via PEX_PATH. Any 3rdparty requirements declared with self.extra_requirements() will
    also be resolved for the global interpreter, and added to the returned pex via PEX_PATH.

    :param pex_info: An optional PexInfo instance to provide to self.merged_pex().
    :type pex_info: :class:`pex.pex_info.PexInfo`, or None
    task. Otherwise, all of the interpreter constraints from all python targets will applied.
    :rtype: :class:`pex.pex.PEX`
    """
    relevant_targets = self.context.targets(
      lambda tgt: isinstance(tgt, (
        PythonDistribution, PythonRequirementLibrary, PythonTarget, Files)))
    with self.invalidated(relevant_targets) as invalidation_check:

      # If there are no relevant targets, we still go through the motions of resolving
      # an empty set of requirements, to prevent downstream tasks from having to check
      # for this special case.
      if invalidation_check.all_vts:
        target_set_id = VersionedTargetSet.from_versioned_targets(
          invalidation_check.all_vts).cache_key.hash
      else:
        target_set_id = 'no_targets'

      interpreter = self.context.products.get_data(PythonInterpreter)
      path = os.path.realpath(os.path.join(self.workdir, str(interpreter.identity), target_set_id))

      # Note that we check for the existence of the directory, instead of for invalid_vts,
      # to cover the empty case.
      if not os.path.isdir(path):
        pexes = [
          self.context.products.get_data(ResolveRequirements.REQUIREMENTS_PEX),
          self.context.products.get_data(GatherSources.PYTHON_SOURCES)
        ]

        if self.extra_requirements():
          extra_requirements_pex = self.resolve_requirement_strings(
            interpreter, self.extra_requirements())
          # Add the extra requirements first, so they take precedence over any colliding version
          # in the target set's dependency closure.
          pexes = [extra_requirements_pex] + pexes

        # NB: See docstring. We always use the previous selected interpreter.
        constraints = {str(interpreter.identity.requirement)}

        with self.merged_pex(path, pex_info, interpreter, pexes, constraints) as builder:
          for extra_file in self.extra_files():
            extra_file.add_to(builder)
          builder.freeze(bytecode_compile=False)

    return PEX(path, interpreter)
