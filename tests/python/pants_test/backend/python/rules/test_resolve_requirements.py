# coding=utf-8
# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import json
import os.path
import zipfile

from pants.backend.python.rules.resolve_requirements import (ResolvedRequirementsPex,
                                                             ResolveRequirementsRequest,
                                                             resolve_requirements)
from pants.backend.python.subsystems.python_native_code import (PythonNativeCode,
                                                                create_pex_native_build_environment)
from pants.backend.python.subsystems.python_setup import PythonSetup
from pants.engine.fs import DirectoryToMaterialize
from pants.engine.rules import RootRule
from pants.engine.selectors import Params
from pants.util.collections import assert_single_element
from pants.util.contextutil import temporary_dir
from pants_test.subsystem.subsystem_util import init_subsystems
from pants_test.test_base import TestBase


class TestResolveRequirements(TestBase):

  @classmethod
  def rules(cls):
    return super(TestResolveRequirements, cls).rules() + [
      resolve_requirements,
      RootRule(ResolveRequirementsRequest),
      RootRule(PythonSetup),
      RootRule(PythonNativeCode),
      create_pex_native_build_environment,
    ]

  def setUp(self):
    super(TestResolveRequirements, self).setUp()
    init_subsystems([PythonSetup, PythonNativeCode])

  def create_pex_and_get_pex_info(
    self, requirements=None, entry_point=None, interpreter_constraints=None
  ):
    def hashify_optional_collection(iterable):
      return tuple(sorted(iterable)) if iterable is not None else tuple()

    request = ResolveRequirementsRequest(
      output_filename="test.pex",
      requirements=hashify_optional_collection(requirements),
      interpreter_constraints=hashify_optional_collection(interpreter_constraints),
      entry_point=entry_point,
    )
    requirements_pex = assert_single_element(
      self.scheduler.product_request(ResolvedRequirementsPex, [Params(
        request,
        PythonSetup.global_instance(),
        PythonNativeCode.global_instance()
      )])
    )
    with temporary_dir() as tmp_dir:
      self.scheduler.materialize_directories((
        DirectoryToMaterialize(path=tmp_dir, directory_digest=requirements_pex.directory_digest),
      ))
      with zipfile.ZipFile(os.path.join(tmp_dir, "test.pex"), "r") as pex:
        with pex.open("PEX-INFO", "r") as pex_info:
          pex_info_content = pex_info.readline().decode("utf-8")
    return json.loads(pex_info_content)

  def test_resolves_dependencies(self):
    requirements = {"six==1.12.0", "jsonschema==2.6.0", "requests==2.22.0"}
    pex_info = self.create_pex_and_get_pex_info(requirements=requirements)
    # NB: We do not check for transitive dependencies, which PEX-INFO will include. We only check
    # that at least the dependencies we requested are included.
    self.assertTrue(requirements.issubset(pex_info["requirements"]))

  def test_entry_point(self):
    entry_point = "pydoc"
    pex_info = self.create_pex_and_get_pex_info(entry_point=entry_point)
    self.assertEqual(pex_info["entry_point"], entry_point)

  def test_interpreter_constraints(self):
    constraints = {"CPython>=2.7,<3", "CPython>=3.6,<4"}
    pex_info = self.create_pex_and_get_pex_info(interpreter_constraints=constraints)
    self.assertEqual(set(pex_info["interpreter_constraints"]), constraints)
