"""

Not supported:

 - Diamond inheritance

"""

from pprint import pprint
import tempfile
import os
import shutil
from os.path import join as pjoin
import re
import glob

from ..formats.marked_yaml import load_yaml_from_file, is_null
from .utils import substitute_profile_parameters
from .. import core
from .exceptions import ProfileError

class Profile(object):
    """
    Profiles acts as nodes in a tree, with `extends` containing the
    parent profiles (which are child nodes in a DAG).
    """
    def __init__(self, doc, checkouts_manager):
        self.doc = doc
        self.parameters = dict(doc.get('parameters', {}))
        self.file_resolver = FileResolver(checkouts_manager, doc.get('package_dirs', []))
        self.hook_import_dirs = doc.get('hook_import_dirs', [])
        self.packages = doc['packages']
        self._yaml_cache = {}

    def load_package_yaml(self, pkgname):
        """
        Loads mypkg.yaml from either $pkgs/mypkg.yaml or $pkgs/mypkg/mypkg.yaml, and
        caches the result.
        """
        doc = self._yaml_cache.get(('package', pkgname), None)
        if doc is None:
            p = self.find_package_file(pkgname, pkgname + '.yaml')
            doc = load_yaml_from_file(p) if p is not None else None
            self._yaml_cache['package', pkgname] = doc
        return doc

    def glob_package_specs(self, pkgname):
        self.file_resolver.find_file([pkgname + '.yaml', pjoin(pkgname, pkgname + '.yaml'),
                                      pjoin(pkgname, pkgname + '-*.yaml')], glob=True)

    def find_package_file(self, pkgname, filename):
        """
        Attempts to find a package resource file at $pkgs/$filename or $pkgs/$pkgname/$filename.
        """
        return self.file_resolver.find_file([filename, pjoin(pkgname, filename)])

    def __repr__(self):
        return '<Profile %s>' % self.filename


class TemporarySourceCheckouts(object):
    """
    A context that holds a number of sources checked out to temporary directories
    until it is released.
    """
    REPO_NAME_PATTERN = re.compile(r'^<([^>]+)>(.*)')

    def __init__(self, source_cache):
        self.repos = {}  # name : (key, tmpdir)
        self.source_cache = source_cache

    def checkout(self, name, key, urls):
        if name in self.repos:
            existing_key, path = self.repos[name]
            if existing_key != key:
                raise ProfileError(name, 'Name "%s" used for two different commits within a profile' % name)
        else:
            if len(urls) != 1:
                raise ProfileError(urls, 'Only a single url currently supported')
            self.source_cache.fetch(urls[0], key, 'profile-%s' % name)
            path = tempfile.mkdtemp()
            try:
                self.source_cache.unpack(key, path)
            except:
                shutil.rmtree(path)
                raise
            else:
                self.repos[name] = (key, path)
        return path

    def close(self):
        for key, tmpdir in self.repos.values():
            shutil.rmtree(tmpdir)
        self.repos.clear()

    def resolve(self, path):
        """
        Expand path-names of the form ``<repo_name>/foo/bar``,
        replacing the ``<repo_name>`` part (where ``repo_name`` is
        given to `checkout`, and the ``<`` and ``>`` are literals)
        with the temporary checkout of the given directory.
        """
        m = self.REPO_NAME_PATTERN.match(path)
        if m:
            name = m.group(1)
            if name not in self.repos:
                raise ProfileError(path, 'No temporary checkouts are named "%s"' % name)
            key, tmpdir = self.repos[name]
            return tmpdir + m.group(2)
        else:
            return path

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

class FileResolver(object):
    """
    Find spec files in an overlay-based filesystem, consulting many
    search paths in order.  Supports the
    ``<repo_name>/some/path``-convention.
    """
    def __init__(self, checkouts_manager, search_dirs):
        self.checkouts_manager = checkouts_manager
        self.search_dirs = search_dirs

    def find_file(self, filenames, glob=False):
        """
        Search for a file with the given filename/path relative to the root
        of each of `self.search_dirs`. If `filenames` is a list, the entire
        list will be searched before moving on to the next layer/overlay.

        Returns the found file (in the ``<repo_name>/some/path``-convention),
        or None if no file was found.
        """
        if isinstance(filenames, basestring):
            filenames = [filenames]
        for overlay in self.search_dirs:
            for p in filenames:
                filename = pjoin(overlay, p)
                if os.path.exists(self.checkouts_manager.resolve(filename)):
                    return filename
        return None

    def glob_files(self, patterns):
        """
        Like ``find_file``, but uses a set of patterns and tries to match each
        pattern against the filesystem using ``glob.glob``. The result is
        a dict mapping the 'matched name' (path relative to root of overlay;
        this is required to be unique) to the physical path.
        """
        if isinstance(patterns, basestring):
            patterns = [patterns]
        result = {}
        # iterate from bottom and up, so that newer matches overwrites older ones in dict
        for overlay in self.search_dirs[::-1]:
            basedir = self.checkouts_manager.resolve(overlay)
            for p in patterns:
                for match in glob.glob(pjoin(basedir, p)):
                    assert match.startswith(basedir)
                    match_relname = match[len(basedir) + 1:]
                    result[match_relname] = match
        return result


def load_and_inherit_profile(checkouts, include_doc, cwd=None):
    """
    Loads a Profile given an include document fragment, e.g.::

        file: ../foo/profile.yaml

    or::

        file: linux/profile.yaml
        urls: [git://github.com/hashdist/hashstack.git]
        key: git:5aeba2c06ed1458ae4dc5d2c56bcf7092827347e

    The load happens recursively, including fetching any remote
    dependencies, and merging the result into this document.

    `cwd` is where to interpret `file` in `include_doc` relative to
    (if it is not in a temporary checked out source).  It can use the
    format of TemporarySourceCheckouts, ``<repo_name>/some/path``.
    """
    if cwd is None:
        cwd = os.getcwd()

    def resolve_path(cwd, p):
        if not os.path.isabs(p):
            p = pjoin(cwd, p)
        return p

    if isinstance(include_doc, str):
        include_doc = {'file': include_doc}

    if 'key' in include_doc:
        # This profile is included through a source cache
        # reference/"git import".  We check out sources to a temporary
        # directory and set the repo name expansion pattern as `cwd`.
        # (The purpose of this is to give understandable error
        # messages.)
        checkouts.checkout(include_doc['name'], include_doc['key'], include_doc['urls'])
        cwd = '<%s>' % include_doc['name']

    profile_file = resolve_path(cwd, include_doc['file'])
    new_cwd = resolve_path(cwd, os.path.dirname(include_doc['file']))

    doc = load_yaml_from_file(checkouts.resolve(profile_file))
    if doc is None:
        doc = {}

    if 'extends' in doc:
        parents = [load_and_inherit_profile(checkouts, parent_include_doc, cwd=new_cwd)
                   for parent_include_doc in doc['extends']]
        del doc['extends']
    else:
        parents = []

    for section in ['package_dirs', 'hook_import_dirs']:
        lst = doc.get(section, [])
        doc[section] = [resolve_path(new_cwd, p) for p in lst]

    # Merge package_dirs, hook_import_dirs with those of parents
    for section in ['package_dirs', 'hook_import_dirs']:
        for parent_doc in parents:
            doc[section].extend(parent_doc.get(section, []))

    # Merge parameters. Can't have the same parameter from more than one parent
    # *unless* it's overriden by this document, in which case it's OK.
    parameters = doc.setdefault('parameters', {})
    overridden = parameters.keys()
    for parent_doc in parents:
        for k, v in parent_doc.get('parameters', {}).iteritems():
            if k not in overridden:
                if k in parameters:
                    raise ProfileError(doc, 'two base profiles set same parameter %s, please set it '
                                       'explicitly in descendant profile')
                parameters[k] = v

    # Merge packages section
    packages = {}
    for parent_doc in parents:
        for pkgname, settings in parent_doc.get('packages', {}).iteritems():
            packages.setdefault(pkgname, {}).update(settings)

    for pkgname, settings in doc.get('packages', {}).iteritems():
        if is_null(settings):
            settings = {}
        packages.setdefault(pkgname, {}).update(settings)

    for pkgname, settings in list(packages.items()):  # copy to avoid changes during iteration
        if settings.get('skip', False):
            del packages[pkgname]

    doc['packages'] = packages
    return doc

def load_profile(checkout_manager, profile_file):
    doc = load_and_inherit_profile(checkout_manager, profile_file)
    return Profile(doc, checkout_manager)
