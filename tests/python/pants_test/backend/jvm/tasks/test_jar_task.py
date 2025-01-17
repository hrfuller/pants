# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, division, print_function, unicode_literals

import os
import re
from builtins import open, range
from contextlib import contextmanager
from textwrap import dedent

from twitter.common.collections import maybe_list

from pants.backend.jvm.targets.java_agent import JavaAgent
from pants.backend.jvm.targets.jvm_binary import JvmBinary
from pants.backend.jvm.tasks.jar_task import JarBuilderTask, JarTask
from pants.build_graph.build_file_aliases import BuildFileAliases
from pants.util.contextutil import open_zip, temporary_dir, temporary_file
from pants.util.dirutil import safe_mkdir, safe_mkdtemp, safe_open, safe_rmtree
from pants_test.jvm.jar_task_test_base import JarTaskTestBase


class BaseJarTaskTest(JarTaskTestBase):

  @classmethod
  def alias_groups(cls):
    return super(BaseJarTaskTest, cls).alias_groups().merge(BuildFileAliases(
      targets={
        'java_agent': JavaAgent,
        'jvm_binary': JvmBinary,
      },
    ))

  def setUp(self):
    super(BaseJarTaskTest, self).setUp()

    self.workdir = safe_mkdtemp()
    self.jar_task = self.prepare_execute(self.context())

  def tearDown(self):
    super(BaseJarTaskTest, self).tearDown()

    if self.workdir:
      safe_rmtree(self.workdir)

  @contextmanager
  def jarfile(self):
    with temporary_file(root_dir=self.workdir, suffix='.jar') as fd:
      fd.close()
      yield fd.name

  def assert_listing(self, jar, *expected_items):
    self.assertEqual({'META-INF/', 'META-INF/MANIFEST.MF'} | set(expected_items),
                      set(jar.namelist()))


class JarTaskTest(BaseJarTaskTest):
  MAX_SUBPROC_ARGS = 50

  class TestJarTask(JarTask):
    def execute(self):
      pass

  @classmethod
  def task_type(cls):
    return cls.TestJarTask

  def setUp(self):
    super(JarTaskTest, self).setUp()
    self.set_options(max_subprocess_args=self.MAX_SUBPROC_ARGS)
    self.jar_task = self.prepare_execute(self.context())

  def test_update_write(self):
    with temporary_dir() as chroot:
      _path = os.path.join(chroot, 'a/b/c')
      safe_mkdir(_path)
      data_file = os.path.join(_path, 'd.txt')
      with open(data_file, 'w') as fd:
        fd.write('e')

      with self.jarfile() as existing_jarfile:
        with self.jar_task.open_jar(existing_jarfile) as jar:
          jar.write(data_file, 'f/g/h')

        with open_zip(existing_jarfile) as jar:
          self.assert_listing(jar, 'f/', 'f/g/', 'f/g/h')
          self.assertEqual(b'e', jar.read('f/g/h'))

  def test_update_writestr(self):
    def assert_writestr(path, contents, *entries):
      with self.jarfile() as existing_jarfile:
        with self.jar_task.open_jar(existing_jarfile) as jar:
          jar.writestr(path, contents)

        with open_zip(existing_jarfile) as jar:
          self.assert_listing(jar, *entries)
          self.assertEqual(contents, jar.read(path))

    assert_writestr('a.txt', b'b', 'a.txt')
    assert_writestr('a/b/c.txt', b'd', 'a/', 'a/b/', 'a/b/c.txt')

  def test_overwrite_write(self):
    with temporary_dir() as chroot:
      _path = os.path.join(chroot, 'a/b/c')
      safe_mkdir(_path)
      data_file = os.path.join(_path, 'd.txt')
      with open(data_file, 'w') as fd:
        fd.write('e')

      with self.jarfile() as existing_jarfile:
        with self.jar_task.open_jar(existing_jarfile, overwrite=True) as jar:
          jar.write(data_file, 'f/g/h')

        with open_zip(existing_jarfile) as jar:
          self.assert_listing(jar, 'f/', 'f/g/', 'f/g/h')
          self.assertEqual(b'e', jar.read('f/g/h'))

  def test_overwrite_writestr(self):
    with self.jarfile() as existing_jarfile:
      with self.jar_task.open_jar(existing_jarfile, overwrite=True) as jar:
        jar.writestr('README', b'42')

      with open_zip(existing_jarfile) as jar:
        self.assert_listing(jar, 'README')
        self.assertEqual(b'42', jar.read('README'))

  @contextmanager
  def _test_custom_manifest(self):
    manifest_contents = b'Manifest-Version: 1.0\r\nCreated-By: test\r\n\r\n'

    with self.jarfile() as existing_jarfile:
      with self.jar_task.open_jar(existing_jarfile, overwrite=True) as jar:
        jar.writestr('README', b'42')

      with open_zip(existing_jarfile) as jar:
        self.assert_listing(jar, 'README')
        self.assertEqual(b'42', jar.read('README'))
        self.assertNotEqual(manifest_contents, jar.read('META-INF/MANIFEST.MF'))

      with self.jar_task.open_jar(existing_jarfile, overwrite=False) as jar:
        yield jar, manifest_contents

      with open_zip(existing_jarfile) as jar:
        self.assert_listing(jar, 'README')
        self.assertEqual(b'42', jar.read('README'))
        self.assertEqual(manifest_contents, jar.read('META-INF/MANIFEST.MF'))

  def test_custom_manifest_str(self):
    with self._test_custom_manifest() as (jar, manifest_contents):
      jar.writestr('META-INF/MANIFEST.MF', manifest_contents)

  def test_custom_manifest_file(self):
    with self._test_custom_manifest() as (jar, manifest_contents):
      with safe_open(os.path.join(safe_mkdtemp(), 'any_source_file'), 'wb') as fp:
        fp.write(manifest_contents)
      jar.write(fp.name, dest='META-INF/MANIFEST.MF')

  def test_custom_manifest_dir(self):
    with self._test_custom_manifest() as (jar, manifest_contents):
      basedir = safe_mkdtemp()
      with safe_open(os.path.join(basedir, 'META-INF/MANIFEST.MF'), 'wb') as fp:
        fp.write(manifest_contents)
      jar.write(basedir)

  def test_custom_manifest_dir_custom_dest(self):
    with self._test_custom_manifest() as (jar, manifest_contents):
      basedir = safe_mkdtemp()
      with safe_open(os.path.join(basedir, 'MANIFEST.MF'), 'wb') as fp:
        fp.write(manifest_contents)
      jar.write(basedir, dest='META-INF')

  def test_classpath(self):
    def manifest_content(classpath):
      return ('Manifest-Version: 1.0\r\n' +
              'Class-Path: {}\r\n' +
              'Created-By: org.pantsbuild.tools.jar.JarBuilder\r\n\r\n').format(
                ' '.join(maybe_list(classpath))).encode('utf-8')

    def assert_classpath(classpath):
      with self.jarfile() as existing_jarfile:
        # Note for -classpath, there is no update, it's already overwriting.
        # To verify this, first add a random classpath, and verify it's overwritten by
        # the supplied classpath value.
        with self.jar_task.open_jar(existing_jarfile) as jar:
          # prefix with workdir since Class-Path is relative to jarfile.path
          jar.append_classpath(os.path.join(self.workdir, 'something_should_be_overwritten.jar'))

        with self.jar_task.open_jar(existing_jarfile) as jar:
          jar.append_classpath([os.path.join(self.workdir, jar_path) for jar_path in classpath])

        with open_zip(existing_jarfile) as jar:
          self.assertEqual(manifest_content(classpath), jar.read('META-INF/MANIFEST.MF'))

    assert_classpath(['a.jar'])
    assert_classpath(['a.jar', 'b.jar'])

  def test_update_jars(self):
    with self.jarfile() as main_jar:
      with self.jarfile() as included_jar:
        with self.jar_task.open_jar(main_jar) as jar:
          jar.writestr('a/b', b'c')

        with self.jar_task.open_jar(included_jar) as jar:
          jar.writestr('e/f', b'g')

        with self.jar_task.open_jar(main_jar) as jar:
          jar.writejar(included_jar)

        with open_zip(main_jar) as jar:
          self.assert_listing(jar, 'a/', 'a/b', 'e/', 'e/f')

  def test_overwrite_jars(self):
    with self.jarfile() as main_jar:
      with self.jarfile() as included_jar:
        with self.jar_task.open_jar(main_jar) as jar:
          jar.writestr('a/b', b'c')

        with self.jar_task.open_jar(included_jar) as jar:
          jar.writestr('e/f', b'g')

        # Create lots of included jars (even though they're all the same)
        # so the -jars argument to jar-tool will exceed max_args limit thus
        # switch to @argfile calling style.
        with self.jar_task.open_jar(main_jar, overwrite=True) as jar:
          for i in range(self.MAX_SUBPROC_ARGS + 1):
            jar.writejar(included_jar)

        with open_zip(main_jar) as jar:
          self.assert_listing(jar, 'e/', 'e/f')


class JarBuilderTest(BaseJarTaskTest):

  class TestJarBuilderTask(JarBuilderTask):
    def execute(self):
      pass

  @classmethod
  def task_type(cls):
    return cls.TestJarBuilderTask

  def setUp(self):
    super(JarBuilderTest, self).setUp()
    self.set_options(max_subprocess_args=100)

  def test_agent_manifest(self):
    self.add_to_build_file('src/java/pants/agents', dedent("""
        java_agent(
          name='fake_agent',
          premain='bob',
          agent_class='fred',
          can_redefine=True,
          can_retransform=True,
          can_set_native_method_prefix=True
        )""").strip())
    java_agent = self.target('src/java/pants/agents:fake_agent')

    context = self.context(target_roots=[java_agent])
    jar_builder_task = self.prepare_execute(context)

    self.add_to_runtime_classpath(context, java_agent, {'FakeAgent.class': '0xCAFEBABE'})
    with self.jarfile() as existing_jarfile:
      with jar_builder_task.open_jar(existing_jarfile) as jar:
        with jar_builder_task.create_jar_builder(jar) as jar_builder:
          jar_builder.add_target(java_agent)

      with open_zip(existing_jarfile) as jar:
        self.assert_listing(jar, 'FakeAgent.class')
        self.assertEqual(b'0xCAFEBABE', jar.read('FakeAgent.class'))

        manifest = jar.read('META-INF/MANIFEST.MF').decode('utf-8').strip()
        all_entries = dict(tuple(re.split(r'\s*:\s*', line, 1)) for line in manifest.splitlines())
        expected_entries = {
            'Agent-Class': 'fred',
            'Premain-Class': 'bob',
            'Can-Redefine-Classes': 'true',
            'Can-Retransform-Classes': 'true',
            'Can-Set-Native-Method-Prefix': 'true',
        }
        self.assertEqual(set(expected_entries.items()),
                          set(expected_entries.items()).intersection(set(all_entries.items())))

  def test_manifest_items(self):
    self.add_to_build_file('src/java/hello', dedent("""
        jvm_binary(
          name='hello',
          main='hello.Hello',
          manifest_entries = {
            'Foo': 'foo-value',
            'Implementation-Version': '1.2.3',
          },
        )""").strip())
    binary_target = self.target('src/java/hello:hello')
    context = self.context(target_roots=[binary_target])

    self.add_to_runtime_classpath(context, binary_target, {'Hello.class': '0xDEADBEEF'})

    jar_builder_task = self.prepare_execute(context)

    with self.jarfile() as existing_jarfile:
      with jar_builder_task.open_jar(existing_jarfile) as jar:
        with jar_builder_task.create_jar_builder(jar) as jar_builder:
          jar_builder.add_target(binary_target)

      with open_zip(existing_jarfile) as jar:
        manifest = jar.read('META-INF/MANIFEST.MF').decode('utf-8').strip()
        all_entries = dict(tuple(re.split(r'\s*:\s*', line, 1)) for line in manifest.splitlines())
        expected_entries = {
          'Foo': 'foo-value',
          'Implementation-Version': '1.2.3',
          }
        self.assertEqual(set(expected_entries.items()),
                          set(expected_entries.items()).intersection(set(all_entries.items())))
