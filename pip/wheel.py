"""
Support for installing and building the "wheel" binary package format.
"""
from __future__ import with_statement

import csv
import functools
import hashlib
import os
import pkg_resources
import re
import shutil
import sys
from base64 import urlsafe_b64encode

from pip.backwardcompat import ConfigParser
from pip.locations import distutils_scheme
from pip.log import logger
from pip import pep425tags
from pip.util import call_subprocess, normalize_path, make_path_relative
from pip._vendor.distlib.scripts import ScriptMaker

wheel_ext = '.whl'

def wheel_setuptools_support():
    """
    Return True if we have a setuptools that supports wheel.
    """
    fulfilled = hasattr(pkg_resources, 'DistInfoDistribution')
    if not fulfilled:
        logger.warn("Wheel installs require setuptools >= 0.8 for dist-info support.")
    return fulfilled

def rehash(path, algo='sha256', blocksize=1<<20):
    """Return (hash, length) for path using hashlib.new(algo)"""
    h = hashlib.new(algo)
    length = 0
    with open(path, 'rb') as f:
        block = f.read(blocksize)
        while block:
            length += len(block)
            h.update(block)
            block = f.read(blocksize)
    digest = 'sha256='+urlsafe_b64encode(h.digest()).decode('latin1').rstrip('=')
    return (digest, length)

try:
    unicode
    def binary(s):
        if isinstance(s, unicode):
            return s.encode('ascii')
        return s
except NameError:
    def binary(s):
        if isinstance(s, str):
            return s.encode('ascii')

def open_for_csv(name, mode):
    if sys.version_info[0] < 3:
        nl = {}
        bin = 'b'
    else:
        nl = { 'newline': '' }
        bin = ''
    return open(name, mode + bin, **nl)

def fix_script(path):
    """Replace #!python with #!/path/to/python
    Return True if file was changed."""
    # XXX RECORD hashes will need to be updated
    if os.path.isfile(path):
        script = open(path, 'rb')
        try:
            firstline = script.readline()
            if not firstline.startswith(binary('#!python')):
                return False
            exename = sys.executable.encode(sys.getfilesystemencoding())
            firstline = binary('#!') + exename + binary(os.linesep)
            rest = script.read()
        finally:
            script.close()
        script = open(path, 'wb')
        try:
            script.write(firstline)
            script.write(rest)
        finally:
            script.close()
        return True

dist_info_re = re.compile(r"""^(?P<namever>(?P<name>.+?)(-(?P<ver>\d.+?))?)
                                \.dist-info$""", re.VERBOSE)

def root_is_purelib(name, wheeldir):
    """
    Return True if the extracted wheel in wheeldir should go into purelib.
    """
    name_folded = name.replace("-", "_")
    for item in os.listdir(wheeldir):
        match = dist_info_re.match(item)
        if match and match.group('name') == name_folded:
            with open(os.path.join(wheeldir, item, 'WHEEL')) as wheel:
                for line in wheel:
                    line = line.lower().rstrip()
                    if line == "root-is-purelib: true":
                        return True
    return False

def get_entrypoints(filename):
    if not os.path.exists(filename):
        return {}, {}
    cp = ConfigParser.RawConfigParser()
    cp.read(filename)
    console = {}
    gui = {}
    if cp.has_section('console_scripts'):
        console = dict(cp.items('console_scripts'))
    if cp.has_section('gui_scripts'):
        gui = dict(cp.items('gui_scripts'))
    return console, gui

def move_wheel_files(name, req, wheeldir, user=False, home=None, root=None):
    """Install a wheel"""

    scheme = distutils_scheme(name, user=user, home=home, root=root)

    if root_is_purelib(name, wheeldir):
        lib_dir = scheme['purelib']
    else:
        lib_dir = scheme['platlib']

    info_dir = []
    data_dirs = []
    source = wheeldir.rstrip(os.path.sep) + os.path.sep

    # Record details of the files moved
    #   installed = files copied from the wheel to the destination
    #   changed = files changed while installing (scripts #! line typically)
    #   generated = files newly generated during the install (script wrappers)
    installed = {}
    changed = set()
    generated = []

    def normpath(src, p):
        return make_path_relative(src, p).replace(os.path.sep, '/')

    def record_installed(srcfile, destfile, modified=False):
        """Map archive RECORD paths to installation RECORD paths."""
        oldpath = normpath(srcfile, wheeldir)
        newpath = normpath(destfile, lib_dir)
        installed[oldpath] = newpath
        if modified:
            changed.add(destfile)

    def clobber(source, dest, is_base, fixer=None, filter=None):
        if not os.path.exists(dest): # common for the 'include' path
            os.makedirs(dest)

        for dir, subdirs, files in os.walk(source):
            basedir = dir[len(source):].lstrip(os.path.sep)
            if is_base and basedir.split(os.path.sep, 1)[0].endswith('.data'):
                continue
            for s in subdirs:
                destsubdir = os.path.join(dest, basedir, s)
                if is_base and basedir == '' and destsubdir.endswith('.data'):
                    data_dirs.append(s)
                    continue
                elif (is_base
                    and s.endswith('.dist-info')
                    # is self.req.project_name case preserving?
                    and s.lower().startswith(req.project_name.replace('-', '_').lower())):
                    assert not info_dir, 'Multiple .dist-info directories'
                    info_dir.append(destsubdir)
                if not os.path.exists(destsubdir):
                    os.makedirs(destsubdir)
            for f in files:
                # Skip unwanted files
                if filter and filter(f):
                    continue
                srcfile = os.path.join(dir, f)
                destfile = os.path.join(dest, basedir, f)
                shutil.move(srcfile, destfile)
                changed = False
                if fixer:
                    changed = fixer(destfile)
                record_installed(srcfile, destfile, changed)

    clobber(source, lib_dir, True)

    assert info_dir, "%s .dist-info directory not found" % req

    # Get the defined entry points
    ep_file = os.path.join(info_dir[0], 'entry_points.txt')
    console, gui = get_entrypoints(ep_file)

    def is_entrypoint_wrapper(name):
        # EP, EP.exe and EP-script.py are scripts generated for
        # entry point EP by setuptools
        if name.lower().endswith('.exe'):
            matchname = name[:-4]
        elif name.lower().endswith('-script.py'):
            matchname = name[:-10]
        elif name.lower().endswith(".pya"):
            matchname = name[:-4]
        else:
            matchname = name
        # Ignore setuptools-generated scripts
        return (matchname in console or matchname in gui)

    for datadir in data_dirs:
        fixer = None
        filter = None
        for subdir in os.listdir(os.path.join(wheeldir, datadir)):
            fixer = None
            if subdir == 'scripts':
                fixer = fix_script
                filter = is_entrypoint_wrapper
            source = os.path.join(wheeldir, datadir, subdir)
            dest = scheme[subdir]
            clobber(source, dest, False, fixer=fixer, filter=filter)

    maker = ScriptMaker(None, scheme['scripts'])
    maker.variants = set(('', ))

    # This is required because otherwise distlib creates scripts that are not
    # executable.
    # See https://bitbucket.org/pypa/distlib/issue/32/
    maker.set_mode = True

    # Special case pip and setuptools to generate versioned wrappers
    #
    # The issue is that some projects (specifically, pip and setuptools) use
    # code in setup.py to create "versioned" entry points - pip2.7 on Python
    # 2.7, pip3.3 on Python 3.3, etc. But these entry points are baked into
    # the wheel metadata at build time, and so if the wheel is installed with
    # a *different* version of Python the entry points will be wrong. The
    # correct fix for this is to enhance the metadata to be able to describe
    # such versioned entry points, but that won't happen till Metadata 2.0 is
    # available.
    # In the meantime, projects using versioned entry points will either have
    # incorrect versioned entry points, or they will not be able to distribute
    # "universal" wheels (i.e., they will need a wheel per Python version).
    #
    # Because setuptools and pip are bundled with _ensurepip and virtualenv,
    # we need to use universal wheels. So, as a stopgap until Metadata 2.0, we
    # override the versioned entry points in the wheel and generate the
    # correct ones. This code is purely a short-term measure until Metadat 2.0
    # is available.
    pip_script = console.pop('pip', None)
    if pip_script:
        spec = 'pip = ' + pip_script
        generated.extend(maker.make(spec))
        spec = 'pip%s = %s' % (sys.version[:1], pip_script)
        generated.extend(maker.make(spec))
        spec = 'pip%s = %s' % (sys.version[:3], pip_script)
        generated.extend(maker.make(spec))
        # Delete any other versioned pip entry points
        pip_ep = [k for k in console if re.match(r'pip(\d(\.\d)?)?$', k)]
        for k in pip_ep:
            del console[k]
    easy_install_script = console.pop('easy_install', None)
    if easy_install_script:
        spec = 'easy_install = ' + easy_install_script
        generated.extend(maker.make(spec))
        spec = 'easy_install-%s = %s' % (sys.version[:3], easy_install_script)
        generated.extend(maker.make(spec))
        # Delete any other versioned easy_install entry points
        easy_install_ep = [k for k in console
                if re.match(r'easy_install(-\d\.\d)?$', k)]
        for k in easy_install_ep:
            del console[k]

    # Generate the console and GUI entry points specified in the wheel
    if len(console) > 0:
        generated.extend(maker.make_multiple(['%s = %s' % kv for kv in console.items()]))
    if len(gui) > 0:
        generated.extend(maker.make_multiple(['%s = %s' % kv for kv in gui.items()], {'gui': True}))

    record = os.path.join(info_dir[0], 'RECORD')
    temp_record = os.path.join(info_dir[0], 'RECORD.pip')
    with open_for_csv(record, 'r') as record_in:
        with open_for_csv(temp_record, 'w+') as record_out:
            reader = csv.reader(record_in)
            writer = csv.writer(record_out)
            for row in reader:
                row[0] = installed.pop(row[0], row[0])
                if row[0] in changed:
                    row[1], row[2] = rehash(row[0])
                writer.writerow(row)
            for f in generated:
                h, l = rehash(f)
                writer.writerow((f, h, l))
            for f in installed:
                writer.writerow((installed[f], '', ''))
    shutil.move(temp_record, record)

def _unique(fn):
    @functools.wraps(fn)
    def unique(*args, **kw):
        seen = set()
        for item in fn(*args, **kw):
            if item not in seen:
                seen.add(item)
                yield item
    return unique

# TODO: this goes somewhere besides the wheel module
@_unique
def uninstallation_paths(dist):
    """
    Yield all the uninstallation paths for dist based on RECORD-without-.pyc

    Yield paths to all the files in RECORD. For each .py file in RECORD, add
    the .pyc in the same directory.

    UninstallPathSet.add() takes care of the __pycache__ .pyc.
    """
    from pip.req import FakeFile # circular import
    r = csv.reader(FakeFile(dist.get_metadata_lines('RECORD')))
    for row in r:
        path = os.path.join(dist.location, row[0])
        yield path
        if path.endswith('.py'):
            dn, fn = os.path.split(path)
            base = fn[:-3]
            path = os.path.join(dn, base+'.pyc')
            yield path


class Wheel(object):
    """A wheel file"""

    # TODO: maybe move the install code into this class

    wheel_file_re = re.compile(
                r"""^(?P<namever>(?P<name>.+?)(-(?P<ver>\d.+?))?)
                ((-(?P<build>\d.*?))?-(?P<pyver>.+?)-(?P<abi>.+?)-(?P<plat>.+?)
                \.whl|\.dist-info)$""",
                re.VERBOSE)

    def __init__(self, filename):
        wheel_info = self.wheel_file_re.match(filename)
        self.filename = filename
        self.name = wheel_info.group('name').replace('_', '-')
        # we'll assume "_" means "-" due to wheel naming scheme
        # (https://github.com/pypa/pip/issues/1150)
        self.version = wheel_info.group('ver').replace('_', '-')
        self.pyversions = wheel_info.group('pyver').split('.')
        self.abis = wheel_info.group('abi').split('.')
        self.plats = wheel_info.group('plat').split('.')

        # All the tag combinations from this file
        self.file_tags = set((x, y, z) for x in self.pyversions for y
                            in self.abis for z in self.plats)

    def support_index_min(self, tags=None):
        """
        Return the lowest index that a file_tag achieves in the supported_tags list
        e.g. if there are 8 supported tags, and one of the file tags is first in the
        list, then return 0.
        """
        if tags is None: # for mock
            tags = pep425tags.supported_tags
        indexes = [tags.index(c) for c in self.file_tags if c in tags]
        return min(indexes) if indexes else None

    def supported(self, tags=None):
        """Is this wheel supported on this system?"""
        if tags is None: # for mock
            tags = pep425tags.supported_tags
        return bool(set(tags).intersection(self.file_tags))


class WheelBuilder(object):
    """Build wheels from a RequirementSet."""

    def __init__(self, requirement_set, finder, wheel_dir, build_options=[], global_options=[]):
        self.requirement_set = requirement_set
        self.finder = finder
        self.wheel_dir = normalize_path(wheel_dir)
        self.build_options = build_options
        self.global_options = global_options

    def _build_one(self, req):
        """Build one wheel."""

        base_args = [
            sys.executable, '-c',
            "import setuptools;__file__=%r;"\
            "exec(compile(open(__file__).read().replace('\\r\\n', '\\n'), __file__, 'exec'))" % req.setup_py] + \
            list(self.global_options)

        logger.notify('Running setup.py bdist_wheel for %s' % req.name)
        logger.notify('Destination directory: %s' % self.wheel_dir)
        wheel_args = base_args + ['bdist_wheel', '-d', self.wheel_dir] + self.build_options
        try:
            call_subprocess(wheel_args, cwd=req.source_dir, show_stdout=False)
            return True
        except:
            logger.error('Failed building wheel for %s' % req.name)
            return False

    def build(self):
        """Build wheels."""

        #unpack and constructs req set
        self.requirement_set.prepare_files(self.finder)

        reqset = self.requirement_set.requirements.values()

        #make the wheelhouse
        if not os.path.exists(self.wheel_dir):
            os.makedirs(self.wheel_dir)

        #build the wheels
        logger.notify('Building wheels for collected packages: %s' % ', '.join([req.name for req in reqset]))
        logger.indent += 2
        build_success, build_failure = [], []
        for req in reqset:
            if req.is_wheel:
                logger.notify("Skipping building wheel: %s", req.url)
                continue
            if self._build_one(req):
                build_success.append(req)
            else:
                build_failure.append(req)
        logger.indent -= 2

        #notify sucess/failure
        if build_success:
            logger.notify('Successfully built %s' % ' '.join([req.name for req in build_success]))
        if build_failure:
            logger.notify('Failed to build %s' % ' '.join([req.name for req in build_failure]))
