#
# Copyright (c) nexB Inc. and others. All rights reserved.
# ScanCode is a trademark of nexB Inc.
# SPDX-License-Identifier: Apache-2.0
# See http://www.apache.org/licenses/LICENSE-2.0 for the license text.
# See https://github.com/nexB/scancode-toolkit for support or download.
# See https://aboutcode.org for more information about nexB OSS projects.
#

import io

import saneyaml
from packageurl import PackageURL

from packagedcode import models
from packagedcode.pypi import BaseDependencyFileHandler
from dparse2.parser import parse_requirement_line

"""
Handle Conda manifests and metadata, see https://docs.conda.io/en/latest/
https://docs.conda.io/projects/conda-build/en/latest/resources/define-metadata.html

See https://repo.continuum.io/pkgs/free for examples.
"""

# TODO: there are likely other package data files for Conda

class CondaYamlHandler(BaseDependencyFileHandler):
    datasource_id = 'conda_yaml'
    path_patterns = ('*conda*.yaml', '*env*.yaml', '*environment*.yaml')
    default_package_type = 'conda'
    default_primary_language = 'Python'
    description = 'Conda yaml manifest'
    documentation_url = 'https://docs.conda.io/'

    @classmethod
    def parse(cls, location, package_only=False):
        with open(location) as fi:
            conda_data = saneyaml.load(fi.read())
        dependencies = get_conda_yaml_dependencies(conda_data=conda_data)
        name = conda_data.get('name')
        extra_data = {}
        channels = conda_data.get('channels')
        if channels:
            extra_data['channels'] = channels
        if name or dependencies:
            package_data = dict(
                datasource_id=cls.datasource_id,
                type=cls.default_package_type,
                name=name,
                primary_language=cls.default_primary_language,
                dependencies=dependencies,
                extra_data=extra_data,
                is_private=True,
            )
            yield models.PackageData.from_data(package_data, package_only)


class CondaMetaYamlHandler(models.DatafileHandler):
    datasource_id = 'conda_meta_yaml'
    default_package_type = 'conda'
    path_patterns = ('*/meta.yaml',)
    description = 'Conda meta.yml manifest'
    documentation_url = 'https://docs.conda.io/'

    @classmethod
    def get_conda_root(cls, resource, codebase):
        """
        Return a root Resource given a meta.yaml ``resource``.
        """
        # the root is either the parent or further up for yaml stored under
        # an "info" dir. We support extractcode extraction.
        # in a source repo it would be in <repo>/conda.recipe/meta.yaml
        paths = (
            'info/recipe.tar-extract/recipe/meta.yaml',
            'info/recipe/recipe/meta.yaml',
            'conda.recipe/meta.yaml',
        )
        res = resource
        for pth in paths:
            if not res.path.endswith(pth):
                continue
            for _seg in pth.split('/'):
                res = res.parent(codebase)
                if not res:
                    break

            return res

        return resource.parent(codebase)

    @classmethod
    def assign_package_to_resources(cls, package, resource, codebase, package_adder):
        return models.DatafileHandler.assign_package_to_resources(
            package=package,
            resource=cls.get_conda_root(resource, codebase),
            codebase=codebase,
            package_adder=package_adder,
        )

    @classmethod
    def parse(cls, location, package_only=False):
        metayaml = get_meta_yaml_data(location)
        package_element = metayaml.get('package') or {}
        package_name = package_element.get('name')
        package_version = package_element.get('version')

        # FIXME: source is source, not download
        source = metayaml.get('source') or {}
        download_url = source.get('url')
        sha256 = source.get('sha256')

        about = metayaml.get('about') or {}
        homepage_url = about.get('home')
        extracted_license_statement = about.get('license')
        description = about.get('summary')
        vcs_url = about.get('dev_url')

        dependencies = []
        extra_data = {}
        requirements = metayaml.get('requirements') or {}
        for scope, reqs in requirements.items():
            # requirements format is like:
            # (u'run', [u'mccortex ==1.0', u'nextflow ==19.01.0', u'cortexpy
            # ==0.45.7', u'kallisto ==0.44.0', u'bwa', u'pandas',
            # u'progressbar2', u'python >=3.6'])])
            for req in reqs:
                name, _, requirement = req.partition(" ")
                version = None
                if requirement.startswith("=="):
                    _, version = requirement.split("==") 

                # requirements may have namespace, version too
                # - conda-forge::numpy=1.15.4
                namespace = None
                if "::" in name:
                    namespace, name = name.split("::")

                is_pinned = False
                if "=" in name:
                    name, version = name.split("=")
                    is_pinned = True
                    requirement = f"={version}"

                if name in ('pip', 'python'):
                    if not scope in extra_data:
                        extra_data[scope] = [req]
                    else:
                        extra_data[scope].append(req)
                    continue

                purl = PackageURL(
                    type=cls.default_package_type,
                    name=name,
                    namespace=namespace,
                    version=version,
                )
                if "run" in scope:
                    is_runtime = True
                    is_optional = False
                else:
                    is_runtime = False
                    is_optional = True

                dependencies.append(
                    models.DependentPackage(
                        purl=purl.to_string(),
                        extracted_requirement=requirement,
                        scope=scope,
                        is_runtime=is_runtime,
                        is_optional=is_optional,
                        is_pinned=is_pinned,
                        is_direct=True,
                    )
                )

        package_data = dict(
            datasource_id=cls.datasource_id,
            type=cls.default_package_type,
            name=package_name,
            version=package_version,
            download_url=download_url,
            homepage_url=homepage_url,
            vcs_url=vcs_url,
            description=description,
            sha256=sha256,
            extracted_license_statement=extracted_license_statement,
            dependencies=dependencies,
            extra_data=extra_data,
        )
        yield models.PackageData.from_data(package_data, package_only)


def get_conda_yaml_dependencies(conda_data):
    """
    Return a list of DependentPackage mappins from conda and pypi
    dependencies present in a `conda_data` mapping.
    """
    dependencies = conda_data.get('dependencies') or []
    deps = []
    for dep in dependencies:
        if isinstance(dep, str):
            namespace = None
            specs = None
            is_pinned = False

            if "::" in dep:
                namespace, dep = dep.split("::")
                if "/" in namespace or ":" in namespace:
                    namespace = None

            req = parse_requirement_line(dep)
            if req:
                name = req.name
                version = None

                specs = str(req.specs)
                if '==' in specs:
                    version = specs.replace('==','')
                    is_pinned = True
                purl = PackageURL(type='pypi', name=name, version=version)
            else:
                if "=" in dep:
                    dep, version = dep.split("=")
                    is_pinned = True
                    specs = f"={version}"

                purl = PackageURL(
                    type='conda',
                    namespace=namespace,
                    name=dep,
                    version=version,
                )

            if purl.name in ('pip', 'python'):
                continue

            deps.append(
                models.DependentPackage(
                    purl=purl.to_string(),
                    extracted_requirement=specs,
                    scope='dependencies',
                    is_runtime=True,
                    is_optional=False,
                    is_pinned=is_pinned,
                    is_direct=True,
                ).to_dict()
            )

        elif isinstance(dep, dict):
            for line in dep.get('pip', []):
                req = parse_requirement_line(line)
                if req:
                    name = req.name
                    version = None
                    is_pinned = False
                    specs = str(req.specs)
                    if '==' in specs:
                        version = specs.replace('==','')
                        is_pinned = True
                    purl = PackageURL(type='pypi', name=name, version=version)
                    deps.append(
                        models.DependentPackage(
                            purl=purl.to_string(),
                            extracted_requirement=specs,
                            scope='dependencies',
                            is_runtime=True,
                            is_optional=False,
                            is_pinned=is_pinned,
                            is_direct=True,
                        ).to_dict()
                    )

    return deps


def get_meta_yaml_data(location):
    """
    Return a mapping of conda metadata loaded from a meta.yaml files. The format
    support Jinja-based templating and we try a crude resolution of variables
    before loading the data as YAML.
    """
    # FIXME: use Jinja to process these
    variables = get_variables(location)
    yaml_lines = []
    with io.open(location, encoding='utf-8') as metayaml:
        for line in metayaml:
            if not line:
                continue
            pure_line = line.strip()
            if (
                pure_line.startswith('{%')
                and pure_line.endswith('%}')
                and '=' in pure_line
            ):
                continue

            # Replace the variable with the value
            if '{{' in line and '}}' in line:
                for variable, value in variables.items():
                    if "|lower" in line:
                        line = line.replace('{{ ' + variable + '|lower' + ' }}', value.lower())
                    else:
                        line = line.replace('{{ ' + variable + ' }}', value)
            yaml_lines.append(line)

    # Cleanup any remaining complex jinja template lines
    # as the yaml load fails otherwise for unresolved jinja
    cleaned_yaml_lines = [
        line
        for line in yaml_lines
        if not "{{" in line
    ]

    return saneyaml.load(''.join(cleaned_yaml_lines))


def get_variables(location):
    """
    Conda yaml will have variables defined at the beginning of the file, the
    idea is to parse it and return a dictionary of the variable and value

    For example:
    {% set version = "0.45.0" %}
    {% set sha256 = "bc7512f2eef785b037d836f4cc6faded457ac277f75c6e34eccd12da7c85258f" %}
    """
    result = {}
    with io.open(location, encoding='utf-8') as loc:
        for line in loc.readlines():
            if not line:
                continue
            line = line.strip()
            if line.startswith('{%') and line.endswith('%}') and '=' in line:
                line = line.lstrip('{%').rstrip('%}').strip().lstrip('set').lstrip()
                parts = line.split('=')
                result[parts[0].strip()] = parts[-1].strip().strip('"')
    return result
