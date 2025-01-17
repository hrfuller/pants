# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os
import textwrap
import unittest
from builtins import object
from contextlib import contextmanager

from pants.java.distribution.distribution import Distribution
from pants.java.executor import Executor, SubprocessExecutor
from pants.util.contextutil import environment_as, temporary_dir
from pants.util.dirutil import chmod_plus_x, safe_open
from pants.util.process_handler import subprocess


class SubprocessExecutorTest(unittest.TestCase):
  @contextmanager
  def jre(self, env_var):
    with temporary_dir() as jre:
      path = os.path.join(jre, 'java')
      with safe_open(path, 'w') as fp:
        fp.write(textwrap.dedent("""
            #!/bin/sh
            echo ${env_var} >&2
            echo "java.home={java_home}"
          """.format(env_var=env_var, java_home=jre)).strip())
      chmod_plus_x(path)
      yield jre

  def do_test_jre_env_var(self, env_var, env_value, scrubbed=True):
    with self.jre(env_var=env_var) as jre:
      executor = SubprocessExecutor(Distribution(bin_path=jre))
      with environment_as(**{env_var: env_value}):
        self.assertEqual(env_value, os.getenv(env_var))
        process = executor.spawn(classpath=['dummy/classpath'],
                                 main='dummy.main',
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        _, stderr = process.communicate()
        self.assertEqual(0, process.returncode)
        self.assertEqual('' if scrubbed else env_value, stderr.decode('utf-8').strip())

  def test_not_scrubbed(self):
    self.do_test_jre_env_var('FRED', 'frog', scrubbed=False)

  def test_scrubbed_classpath(self):
    with temporary_dir() as cp:
      self.do_test_jre_env_var('CLASSPATH', cp)

  def test_scrubbed_java_options(self):
    self.do_test_jre_env_var('_JAVA_OPTIONS', '-target 6')

  def test_scrubbed_java_tool_options(self):
    self.do_test_jre_env_var('JAVA_TOOL_OPTIONS', '-Xmx1g')

  def test_fails_with_bad_distribution(self):

    class DefinitelyNotADistribution(object):
      pass

    with self.assertRaises(Executor.InvalidDistribution):
      SubprocessExecutor(DefinitelyNotADistribution())

  def test_fails_with_no_distribution(self):
    with self.assertRaises(Executor.InvalidDistribution):
      SubprocessExecutor(None)
