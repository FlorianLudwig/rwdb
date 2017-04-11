# -*- coding: utf-8 -*-
import os
import sys

from distutils.command.sdist import sdist
from setuptools import setup, find_packages
import setuptools.command.test

BASE_PATH = os.path.abspath(os.path.dirname(__file__))
version_suffix = ''


class TestCommand(setuptools.command.test.test):
    def finalize_options(self):
        setuptools.command.test.test.finalize_options(self)
        self.test_args = []
        self.test_suite = True

    def run_tests(self):
        fails = []
        from tox.config import parseconfig
        from tox.session import Session

        config = parseconfig(self.test_args)
        retcode = Session(config).runcommand()
        if retcode != 0:
            fails.append('tox returned errors')

        import pep8
        style_guide = pep8.StyleGuide(config_file=BASE_PATH + '/.pep8')
        style_guide.input_dir(BASE_PATH + '/rwdb')
        if style_guide.options.report.get_count() != 0:
            fails.append('pep8 returned errros for rwdb/')

        style_guide = pep8.StyleGuide(config_file=BASE_PATH + '/.pep8')
        style_guide.input_dir(BASE_PATH + '/test')
        if style_guide.options.report.get_count() != 0:
            fails.append('pep8 returned errros for test/')

        if fails:
            print('\n'.join(fails))
            sys.exit(1)


setup(
    name="rwdb",
    version="0.0.4",
    url='https://github.com/FlorianLudwig/rwdb',
    description='tornado based webframework',
    author='Florian Ludwig',
    author_email='vierzigundzwei@gmail.com',
    install_requires=['motor>=1.0.0'],
    extras_requires={
        'test': ['tox', 'pytest', 'pep8'],
        'docs': ['sphinx_rtd_theme']
    },
    packages=['rwdb'],
    include_package_data=True,
    cmdclass={
        'test': TestCommand
    }
)
