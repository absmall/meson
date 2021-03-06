# Copyright 2013-2017 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This file contains the detection logic for external
# dependencies. Mostly just uses pkg-config but also contains
# custom logic for packages that don't provide them.

# Currently one file, should probably be split into a
# package before this gets too big.

import re
import sys
import os, stat, glob, shutil
import subprocess
import sysconfig
from collections import OrderedDict
from . mesonlib import MesonException, version_compare, version_compare_many, Popen_safe
from . import mlog
from . import mesonlib
from .environment import detect_cpu_family, for_windows

class DependencyException(MesonException):
    '''Exceptions raised while trying to find dependencies'''

class Dependency:
    def __init__(self, type_name, kwargs):
        self.name = "null"
        self.is_found = False
        self.type_name = type_name
        method = kwargs.get('method', 'auto')

        # Set the detection method. If the method is set to auto, use any available method.
        # If method is set to a specific string, allow only that detection method.
        if method == "auto":
            self.methods = self.get_methods()
        elif method in self.get_methods():
            self.methods = [method]
        else:
            raise MesonException('Unsupported detection method: {}, allowed methods are {}'.format(method, mlog.format_list(["auto"] + self.get_methods())))

    def __repr__(self):
        s = '<{0} {1}: {2}>'
        return s.format(self.__class__.__name__, self.name, self.is_found)

    def get_compile_args(self):
        return []

    def get_link_args(self):
        return []

    def found(self):
        return self.is_found

    def get_sources(self):
        """Source files that need to be added to the target.
        As an example, gtest-all.cc when using GTest."""
        return []

    def get_methods(self):
        return ['auto']

    def get_name(self):
        return self.name

    def get_exe_args(self, compiler):
        return []

    def need_threads(self):
        return False

    def get_pkgconfig_variable(self, variable_name):
        raise MesonException('Tried to get a pkg-config variable from a non-pkgconfig dependency.')

class InternalDependency(Dependency):
    def __init__(self, version, incdirs, compile_args, link_args, libraries, sources, ext_deps):
        super().__init__('internal', {})
        self.version = version
        self.include_directories = incdirs
        self.compile_args = compile_args
        self.link_args = link_args
        self.libraries = libraries
        self.sources = sources
        self.ext_deps = ext_deps

    def get_compile_args(self):
        return self.compile_args

    def get_link_args(self):
        return self.link_args

    def get_version(self):
        return self.version

class PkgConfigDependency(Dependency):
    # The class's copy of the pkg-config path. Avoids having to search for it
    # multiple times in the same Meson invocation.
    class_pkgbin = None

    def __init__(self, name, environment, kwargs):
        Dependency.__init__(self, 'pkgconfig', kwargs)
        self.is_libtool = False
        self.required = kwargs.get('required', True)
        self.static = kwargs.get('static', False)
        self.silent = kwargs.get('silent', False)
        if not isinstance(self.static, bool):
            raise DependencyException('Static keyword must be boolean')
        # Store a copy of the pkg-config path on the object itself so it is
        # stored in the pickled coredata and recovered.
        self.pkgbin = None
        self.cargs = []
        self.libs = []
        if 'native' in kwargs and environment.is_cross_build():
            self.want_cross = not kwargs['native']
        else:
            self.want_cross = environment.is_cross_build()
        self.name = name
        self.modversion = 'none'

        # When finding dependencies for cross-compiling, we don't care about
        # the 'native' pkg-config
        if self.want_cross:
            if 'pkgconfig' not in environment.cross_info.config['binaries']:
                if self.required:
                    raise DependencyException('Pkg-config binary missing from cross file')
            else:
                pkgname = environment.cross_info.config['binaries']['pkgconfig']
                potential_pkgbin = ExternalProgram(pkgname, silent=True)
                if potential_pkgbin.found():
                    # FIXME, we should store all pkg-configs in ExternalPrograms.
                    # However that is too destabilizing a change to do just before release.
                    self.pkgbin = potential_pkgbin.get_command()[0]
                    PkgConfigDependency.class_pkgbin = self.pkgbin
                else:
                    mlog.debug('Cross pkg-config %s not found.' % potential_pkgbin.name)
        # Only search for the native pkg-config the first time and
        # store the result in the class definition
        elif PkgConfigDependency.class_pkgbin is None:
            self.pkgbin = self.check_pkgconfig()
            PkgConfigDependency.class_pkgbin = self.pkgbin
        else:
            self.pkgbin = PkgConfigDependency.class_pkgbin

        self.is_found = False
        if not self.pkgbin:
            if self.required:
                raise DependencyException('Pkg-config not found.')
            return
        if self.want_cross:
            self.type_string = 'Cross'
        else:
            self.type_string = 'Native'

        mlog.debug('Determining dependency {!r} with pkg-config executable '
                   '{!r}'.format(name, self.pkgbin))
        ret, self.modversion = self._call_pkgbin(['--modversion', name])
        if ret != 0:
            if self.required:
                raise DependencyException('{} dependency {!r} not found'
                                          ''.format(self.type_string, name))
            return
        found_msg = [self.type_string + ' dependency', mlog.bold(name), 'found:']
        self.version_reqs = kwargs.get('version', None)
        if self.version_reqs is None:
            self.is_found = True
        else:
            if not isinstance(self.version_reqs, (str, list)):
                raise DependencyException('Version argument must be string or list.')
            if isinstance(self.version_reqs, str):
                self.version_reqs = [self.version_reqs]
            (self.is_found, not_found, found) = \
                version_compare_many(self.modversion, self.version_reqs)
            if not self.is_found:
                found_msg += [mlog.red('NO'),
                              'found {!r} but need:'.format(self.modversion),
                              ', '.join(["'{}'".format(e) for e in not_found])]
                if found:
                    found_msg += ['; matched:',
                                  ', '.join(["'{}'".format(e) for e in found])]
                if not self.silent:
                    mlog.log(*found_msg)
                if self.required:
                    m = 'Invalid version of dependency, need {!r} {!r} found {!r}.'
                    raise DependencyException(m.format(name, not_found, self.modversion))
                return
        found_msg += [mlog.green('YES'), self.modversion]
        # Fetch cargs to be used while using this dependency
        self._set_cargs()
        # Fetch the libraries and library paths needed for using this
        self._set_libs()
        # Print the found message only at the very end because fetching cflags
        # and libs can also fail if other needed pkg-config files aren't found.
        if not self.silent:
            mlog.log(*found_msg)

    def __repr__(self):
        s = '<{0} {1}: {2} {3}>'
        return s.format(self.__class__.__name__, self.name, self.is_found,
                        self.version_reqs)

    def _call_pkgbin(self, args):
        p, out = Popen_safe([self.pkgbin] + args, env=os.environ)[0:2]
        return p.returncode, out.strip()

    def _set_cargs(self):
        ret, out = self._call_pkgbin(['--cflags', self.name])
        if ret != 0:
            raise DependencyException('Could not generate cargs for %s:\n\n%s' %
                                      (self.name, out))
        self.cargs = out.split()

    def _set_libs(self):
        libcmd = [self.name, '--libs']
        if self.static:
            libcmd.append('--static')
        ret, out = self._call_pkgbin(libcmd)
        if ret != 0:
            raise DependencyException('Could not generate libs for %s:\n\n%s' %
                                      (self.name, out))
        self.libs = []
        for lib in out.split():
            if lib.endswith(".la"):
                shared_libname = self.extract_libtool_shlib(lib)
                shared_lib = os.path.join(os.path.dirname(lib), shared_libname)
                if not os.path.exists(shared_lib):
                    shared_lib = os.path.join(os.path.dirname(lib), ".libs", shared_libname)

                if not os.path.exists(shared_lib):
                    raise DependencyException('Got a libtools specific "%s" dependencies'
                                              'but we could not compute the actual shared'
                                              'library path' % lib)
                lib = shared_lib
                self.is_libtool = True
            self.libs.append(lib)

    def get_pkgconfig_variable(self, variable_name):
        ret, out = self._call_pkgbin(['--variable=' + variable_name, self.name])
        variable = ''
        if ret != 0:
            if self.required:
                raise DependencyException('%s dependency %s not found.' %
                                          (self.type_string, self.name))
        else:
            variable = out.strip()
        mlog.debug('Got pkgconfig variable %s : %s' % (variable_name, variable))
        return variable

    def get_modversion(self):
        return self.modversion

    def get_version(self):
        return self.modversion

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.libs

    def get_methods(self):
        return ['pkgconfig']

    def check_pkgconfig(self):
        evar = 'PKG_CONFIG'
        if evar in os.environ:
            pkgbin = os.environ[evar].strip()
        else:
            pkgbin = 'pkg-config'
        try:
            p, out = Popen_safe([pkgbin, '--version'])[0:2]
            if p.returncode != 0:
                # Set to False instead of None to signify that we've already
                # searched for it and not found it
                pkgbin = False
        except (FileNotFoundError, PermissionError):
            pkgbin = False
        if pkgbin and not os.path.isabs(pkgbin) and shutil.which(pkgbin):
            # Sometimes shutil.which fails where Popen succeeds, so
            # only find the abs path if it can be found by shutil.which
            pkgbin = shutil.which(pkgbin)
        if not self.silent:
            if pkgbin:
                mlog.log('Found pkg-config:', mlog.bold(pkgbin),
                         '(%s)' % out.strip())
            else:
                mlog.log('Found Pkg-config:', mlog.red('NO'))
        return pkgbin

    def found(self):
        return self.is_found

    def extract_field(self, la_file, fieldname):
        with open(la_file) as f:
            for line in f:
                arr = line.strip().split('=')
                if arr[0] == fieldname:
                    return arr[1][1:-1]
        return None

    def extract_dlname_field(self, la_file):
        return self.extract_field(la_file, 'dlname')

    def extract_libdir_field(self, la_file):
        return self.extract_field(la_file, 'libdir')

    def extract_libtool_shlib(self, la_file):
        '''
        Returns the path to the shared library
        corresponding to this .la file
        '''
        dlname = self.extract_dlname_field(la_file)
        if dlname is None:
            return None

        # Darwin uses absolute paths where possible; since the libtool files never
        # contain absolute paths, use the libdir field
        if mesonlib.is_osx():
            dlbasename = os.path.basename(dlname)
            libdir = self.extract_libdir_field(la_file)
            if libdir is None:
                return dlbasename
            return os.path.join(libdir, dlbasename)
        # From the comments in extract_libtool(), older libtools had
        # a path rather than the raw dlname
        return os.path.basename(dlname)

class WxDependency(Dependency):
    wx_found = None

    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'wx', kwargs)
        self.is_found = False
        self.modversion = 'none'
        if WxDependency.wx_found is None:
            self.check_wxconfig()
        if not WxDependency.wx_found:
            mlog.log("Neither wx-config-3.0 nor wx-config found; can't detect dependency")
            return

        p, out = Popen_safe([self.wxc, '--version'])[0:2]
        if p.returncode != 0:
            mlog.log('Dependency wxwidgets found:', mlog.red('NO'))
            self.cargs = []
            self.libs = []
        else:
            self.modversion = out.strip()
            version_req = kwargs.get('version', None)
            if version_req is not None:
                if not version_compare(self.modversion, version_req, strict=True):
                    mlog.log('Wxwidgets version %s does not fullfill requirement %s' %
                             (self.modversion, version_req))
                    return
            mlog.log('Dependency wxwidgets found:', mlog.green('YES'))
            self.is_found = True
            self.requested_modules = self.get_requested(kwargs)
            # wx-config seems to have a cflags as well but since it requires C++,
            # this should be good, at least for now.
            p, out = Popen_safe([self.wxc, '--cxxflags'])[0:2]
            if p.returncode != 0:
                raise DependencyException('Could not generate cargs for wxwidgets.')
            self.cargs = out.split()

            p, out = Popen_safe([self.wxc, '--libs'] + self.requested_modules)[0:2]
            if p.returncode != 0:
                raise DependencyException('Could not generate libs for wxwidgets.')
            self.libs = out.split()

    def get_requested(self, kwargs):
        modules = 'modules'
        if modules not in kwargs:
            return []
        candidates = kwargs[modules]
        if isinstance(candidates, str):
            return [candidates]
        for c in candidates:
            if not isinstance(c, str):
                raise DependencyException('wxwidgets module argument is not a string.')
        return candidates

    def get_modversion(self):
        return self.modversion

    def get_version(self):
        return self.modversion

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.libs

    def check_wxconfig(self):
        for wxc in ['wx-config-3.0', 'wx-config']:
            try:
                p, out = Popen_safe([wxc, '--version'])[0:2]
                if p.returncode == 0:
                    mlog.log('Found wx-config:', mlog.bold(shutil.which(wxc)),
                             '(%s)' % out.strip())
                    self.wxc = wxc
                    WxDependency.wx_found = True
                    return
            except (FileNotFoundError, PermissionError):
                pass
        WxDependency.wxconfig_found = False
        mlog.log('Found wx-config:', mlog.red('NO'))

    def found(self):
        return self.is_found

class ExternalProgram:
    windows_exts = ('exe', 'msc', 'com', 'bat')

    def __init__(self, name, command=None, silent=False, search_dir=None):
        self.name = name
        if command is not None:
            if not isinstance(command, list):
                self.command = [command]
            else:
                self.command = command
        else:
            self.command = self._search(name, search_dir)
        if not silent:
            if self.found():
                mlog.log('Program', mlog.bold(name), 'found:', mlog.green('YES'),
                         '(%s)' % ' '.join(self.command))
            else:
                mlog.log('Program', mlog.bold(name), 'found:', mlog.red('NO'))

    def __repr__(self):
        r = '<{} {!r} -> {!r}>'
        return r.format(self.__class__.__name__, self.name, self.command)

    @staticmethod
    def _shebang_to_cmd(script):
        """
        Windows does not understand shebangs, so we check if the file has a
        shebang and manually parse it to figure out the interpreter to use
        """
        try:
            with open(script) as f:
                first_line = f.readline().strip()
            if first_line.startswith('#!'):
                commands = first_line[2:].split('#')[0].strip().split()
                if mesonlib.is_windows():
                    # Windows does not have UNIX paths so remove them,
                    # but don't remove Windows paths
                    if commands[0].startswith('/'):
                        commands[0] = commands[0].split('/')[-1]
                    if len(commands) > 0 and commands[0] == 'env':
                        commands = commands[1:]
                    # Windows does not ship python3.exe, but we know the path to it
                    if len(commands) > 0 and commands[0] == 'python3':
                        commands[0] = sys.executable
                return commands + [script]
        except Exception:
            pass
        return False

    def _is_executable(self, path):
        suffix = os.path.splitext(path)[-1].lower()[1:]
        if mesonlib.is_windows():
            if suffix in self.windows_exts:
                return True
        elif os.access(path, os.X_OK):
            return not os.path.isdir(path)
        return False

    def _search_dir(self, name, search_dir):
        if search_dir is None:
            return False
        trial = os.path.join(search_dir, name)
        if os.path.exists(trial):
            if self._is_executable(trial):
                return [trial]
        else:
            for ext in self.windows_exts:
                trial_ext = '{}.{}'.format(trial, ext)
                if os.path.exists(trial_ext):
                    return [trial_ext]
            return False
        # Now getting desperate. Maybe it is a script file that is a) not chmodded
        # executable or b) we are on windows so they can't be directly executed.
        return self._shebang_to_cmd(trial)

    def _search(self, name, search_dir):
        '''
        Search in the specified dir for the specified executable by name
        and if not found search in PATH
        '''
        commands = self._search_dir(name, search_dir)
        if commands:
            return commands
        # Do a standard search in PATH
        command = shutil.which(name)
        if not mesonlib.is_windows():
            # On UNIX-like platforms, the standard PATH search is enough
            return [command]
        # HERE BEGINS THE TERROR OF WINDOWS
        if command:
            # On Windows, even if the PATH search returned a full path, we can't be
            # sure that it can be run directly if it's not a native executable.
            # For instance, interpreted scripts sometimes need to be run explicitly
            # with an interpreter if the file association is not done properly.
            name_ext = os.path.splitext(command)[1]
            if name_ext[1:].lower() in self.windows_exts:
                # Good, it can be directly executed
                return [command]
            # Try to extract the interpreter from the shebang
            commands = self._shebang_to_cmd(command)
            if commands:
                return commands
        else:
            # Maybe the name is an absolute path to a native Windows
            # executable, but without the extension. This is technically wrong,
            # but many people do it because it works in the MinGW shell.
            if os.path.isabs(name):
                for ext in self.windows_exts:
                    command = '{}.{}'.format(name, ext)
                    if os.path.exists(command):
                        return [command]
            # On Windows, interpreted scripts must have an extension otherwise they
            # cannot be found by a standard PATH search. So we do a custom search
            # where we manually search for a script with a shebang in PATH.
            search_dirs = os.environ.get('PATH', '').split(';')
            for search_dir in search_dirs:
                commands = self._search_dir(name, search_dir)
                if commands:
                    return commands
        return [None]

    def found(self):
        return self.command[0] is not None

    def get_command(self):
        return self.command[:]

    def get_path(self):
        # Assume that the last element is the full path to the script
        # If it's not a script, this will be an array of length 1
        if self.found():
            return self.command[-1]
        return None

    def get_name(self):
        return self.name

class ExternalLibrary(Dependency):
    # TODO: Add `lang` to the parent Dependency object so that dependencies can
    # be expressed for languages other than C-like
    def __init__(self, name, link_args=None, language=None, silent=False):
        super().__init__('external')
        self.name = name
        self.is_found = False
        self.link_args = []
        self.lang_args = []
        if link_args:
            self.is_found = True
            if not isinstance(link_args, list):
                link_args = [link_args]
            if language:
                self.lang_args = {language: link_args}
            else:
                self.link_args = link_args
        if not silent:
            if self.is_found:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.green('YES'))
            else:
                mlog.log('Library', mlog.bold(name), 'found:', mlog.red('NO'))

    def found(self):
        return self.is_found

    def get_name(self):
        return self.name

    def get_link_args(self):
        return self.link_args

    def get_lang_args(self, lang):
        if lang in self.lang_args:
            return self.lang_args[lang]
        return []

class BoostDependency(Dependency):
    # Some boost libraries have different names for
    # their sources and libraries. This dict maps
    # between the two.
    name2lib = {'test': 'unit_test_framework'}

    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'boost', kwargs)
        self.name = 'boost'
        self.environment = environment
        self.libdir = ''
        if 'native' in kwargs and environment.is_cross_build():
            self.want_cross = not kwargs['native']
        else:
            self.want_cross = environment.is_cross_build()
        try:
            self.boost_root = os.environ['BOOST_ROOT']
            if not os.path.isabs(self.boost_root):
                raise DependencyException('BOOST_ROOT must be an absolute path.')
        except KeyError:
            self.boost_root = None
        if self.boost_root is None:
            if self.want_cross:
                if 'BOOST_INCLUDEDIR' in os.environ:
                    self.incdir = os.environ['BOOST_INCLUDEDIR']
                else:
                    raise DependencyException('BOOST_ROOT or BOOST_INCLUDEDIR is needed while cross-compiling')
            if mesonlib.is_windows():
                self.boost_root = self.detect_win_root()
                self.incdir = self.boost_root
            else:
                if 'BOOST_INCLUDEDIR' in os.environ:
                    self.incdir = os.environ['BOOST_INCLUDEDIR']
                else:
                    self.incdir = '/usr/include'
        else:
            self.incdir = os.path.join(self.boost_root, 'include')
        self.boost_inc_subdir = os.path.join(self.incdir, 'boost')
        mlog.debug('Boost library root dir is', self.boost_root)
        self.src_modules = {}
        self.lib_modules = {}
        self.lib_modules_mt = {}
        self.detect_version()
        self.requested_modules = self.get_requested(kwargs)
        module_str = ', '.join(self.requested_modules)
        if self.version is not None:
            self.detect_src_modules()
            self.detect_lib_modules()
            self.validate_requested()
            if self.boost_root is not None:
                info = self.version + ', ' + self.boost_root
            else:
                info = self.version
            mlog.log('Dependency Boost (%s) found:' % module_str, mlog.green('YES'), info)
        else:
            mlog.log("Dependency Boost (%s) found:" % module_str, mlog.red('NO'))
        if 'cpp' not in self.environment.coredata.compilers:
            raise DependencyException('Tried to use Boost but a C++ compiler is not defined.')
        self.cpp_compiler = self.environment.coredata.compilers['cpp']

    def detect_win_root(self):
        globtext = 'c:\\local\\boost_*'
        files = glob.glob(globtext)
        if len(files) > 0:
            return files[0]
        return 'C:\\'

    def get_compile_args(self):
        args = []
        if self.boost_root is not None:
            if mesonlib.is_windows():
                args.append('-I' + self.boost_root)
            else:
                args.append('-I' + os.path.join(self.boost_root, 'include'))
        else:
            args.append('-I' + self.incdir)
        return args

    def get_requested(self, kwargs):
        candidates = kwargs.get('modules', [])
        if isinstance(candidates, str):
            return [candidates]
        for c in candidates:
            if not isinstance(c, str):
                raise DependencyException('Boost module argument is not a string.')
        return candidates

    def validate_requested(self):
        for m in self.requested_modules:
            if m not in self.src_modules:
                raise DependencyException('Requested Boost module "%s" not found.' % m)

    def found(self):
        return self.version is not None

    def get_version(self):
        return self.version

    def detect_version(self):
        try:
            ifile = open(os.path.join(self.boost_inc_subdir, 'version.hpp'))
        except FileNotFoundError:
            self.version = None
            return
        with ifile:
            for line in ifile:
                if line.startswith("#define") and 'BOOST_LIB_VERSION' in line:
                    ver = line.split()[-1]
                    ver = ver[1:-1]
                    self.version = ver.replace('_', '.')
                    return
        self.version = None

    def detect_src_modules(self):
        for entry in os.listdir(self.boost_inc_subdir):
            entry = os.path.join(self.boost_inc_subdir, entry)
            if stat.S_ISDIR(os.stat(entry).st_mode):
                self.src_modules[os.path.split(entry)[-1]] = True

    def detect_lib_modules(self):
        if mesonlib.is_windows():
            return self.detect_lib_modules_win()
        return self.detect_lib_modules_nix()

    def detect_lib_modules_win(self):
        arch = detect_cpu_family(self.environment.coredata.compilers)
        # Guess the libdir
        if arch == 'x86':
            gl = 'lib32*'
        elif arch == 'x86_64':
            gl = 'lib64*'
        else:
            # Does anyone do Boost cross-compiling to other archs on Windows?
            gl = None
        # See if the libdir is valid
        if gl:
            libdir = glob.glob(os.path.join(self.boost_root, gl))
        else:
            libdir = []
        # Can't find libdir, bail
        if len(libdir) == 0:
            return
        libdir = libdir[0]
        self.libdir = libdir
        globber = 'boost_*-gd-*.lib' # FIXME
        for entry in glob.glob(os.path.join(libdir, globber)):
            (_, fname) = os.path.split(entry)
            base = fname.split('_', 1)[1]
            modname = base.split('-', 1)[0]
            self.lib_modules_mt[modname] = fname

    def detect_lib_modules_nix(self):
        if mesonlib.is_osx():
            libsuffix = 'dylib'
        else:
            libsuffix = 'so'

        globber = 'libboost_*.{}'.format(libsuffix)
        if 'BOOST_LIBRARYDIR' in os.environ:
            libdirs = [os.environ['BOOST_LIBRARYDIR']]
        elif self.boost_root is None:
            libdirs = mesonlib.get_library_dirs()
        else:
            libdirs = [os.path.join(self.boost_root, 'lib')]
        for libdir in libdirs:
            for entry in glob.glob(os.path.join(libdir, globber)):
                lib = os.path.basename(entry)
                name = lib.split('.')[0].split('_', 1)[-1]
                # I'm not 100% sure what to do here. Some distros
                # have modules such as thread only as -mt versions.
                if entry.endswith('-mt.so'):
                    self.lib_modules_mt[name] = True
                else:
                    self.lib_modules[name] = True

    def get_win_link_args(self):
        args = []
        if self.boost_root:
            args.append('-L' + self.libdir)
        for module in self.requested_modules:
            module = BoostDependency.name2lib.get(module, module)
            if module in self.lib_modules_mt:
                args.append(self.lib_modules_mt[module])
        return args

    def get_link_args(self):
        if mesonlib.is_windows():
            return self.get_win_link_args()
        args = []
        if self.boost_root:
            args.append('-L' + os.path.join(self.boost_root, 'lib'))
        elif 'BOOST_LIBRARYDIR' in os.environ:
            args.append('-L' + os.environ['BOOST_LIBRARYDIR'])
        for module in self.requested_modules:
            module = BoostDependency.name2lib.get(module, module)
            libname = 'boost_' + module
            # The compiler's library detector is the most reliable so use that first.
            default_detect = self.cpp_compiler.find_library(libname, self.environment, [])
            if default_detect is not None:
                if module == 'unit_testing_framework':
                    emon_args = self.cpp_compiler.find_library('boost_test_exec_monitor')
                else:
                    emon_args = None
                args += default_detect
                if emon_args is not None:
                    args += emon_args
            elif module in self.lib_modules or module in self.lib_modules_mt:
                linkcmd = '-l' + libname
                args.append(linkcmd)
                # FIXME a hack, but Boost's testing framework has a lot of
                # different options and it's hard to determine what to do
                # without feedback from actual users. Update this
                # as we get more bug reports.
                if module == 'unit_testing_framework':
                    args.append('-lboost_test_exec_monitor')
            elif module + '-mt' in self.lib_modules_mt:
                linkcmd = '-lboost_' + module + '-mt'
                args.append(linkcmd)
                if module == 'unit_testing_framework':
                    args.append('-lboost_test_exec_monitor-mt')
        return args

    def get_sources(self):
        return []

    def need_threads(self):
        return 'thread' in self.requested_modules

class GTestDependency(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'gtest', kwargs)
        self.main = kwargs.get('main', False)
        self.name = 'gtest'
        self.libname = 'libgtest.so'
        self.libmain_name = 'libgtest_main.so'
        self.include_dir = '/usr/include'
        self.src_dirs = ['/usr/src/gtest/src', '/usr/src/googletest/googletest/src']
        self.detect()

    def found(self):
        return self.is_found

    def detect(self):
        trial_dirs = mesonlib.get_library_dirs()
        glib_found = False
        gmain_found = False
        for d in trial_dirs:
            if os.path.isfile(os.path.join(d, self.libname)):
                glib_found = True
            if os.path.isfile(os.path.join(d, self.libmain_name)):
                gmain_found = True
        if glib_found and gmain_found:
            self.is_found = True
            self.compile_args = []
            self.link_args = ['-lgtest']
            if self.main:
                self.link_args.append('-lgtest_main')
            self.sources = []
            mlog.log('Dependency GTest found:', mlog.green('YES'), '(prebuilt)')
        elif self.detect_srcdir():
            self.is_found = True
            self.compile_args = ['-I' + self.src_include_dir]
            self.link_args = []
            if self.main:
                self.sources = [self.all_src, self.main_src]
            else:
                self.sources = [self.all_src]
            mlog.log('Dependency GTest found:', mlog.green('YES'), '(building self)')
        else:
            mlog.log('Dependency GTest found:', mlog.red('NO'))
            self.is_found = False
        return self.is_found

    def detect_srcdir(self):
        for s in self.src_dirs:
            if os.path.exists(s):
                self.src_dir = s
                self.all_src = mesonlib.File.from_absolute_file(
                    os.path.join(self.src_dir, 'gtest-all.cc'))
                self.main_src = mesonlib.File.from_absolute_file(
                    os.path.join(self.src_dir, 'gtest_main.cc'))
                self.src_include_dir = os.path.normpath(os.path.join(self.src_dir, '..'))
                return True
        return False

    def get_compile_args(self):
        arr = []
        if self.include_dir != '/usr/include':
            arr.append('-I' + self.include_dir)
        if hasattr(self, 'src_include_dir'):
            arr.append('-I' + self.src_include_dir)
        return arr

    def get_link_args(self):
        return self.link_args

    def get_version(self):
        return '1.something_maybe'

    def get_sources(self):
        return self.sources

    def need_threads(self):
        return True

class GMockDependency(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'gmock', kwargs)
        # GMock may be a library or just source.
        # Work with both.
        self.name = 'gmock'
        self.libname = 'libgmock.so'
        trial_dirs = mesonlib.get_library_dirs()
        gmock_found = False
        for d in trial_dirs:
            if os.path.isfile(os.path.join(d, self.libname)):
                gmock_found = True
        if gmock_found:
            self.is_found = True
            self.compile_args = []
            self.link_args = ['-lgmock']
            self.sources = []
            mlog.log('Dependency GMock found:', mlog.green('YES'), '(prebuilt)')
            return

        for d in ['/usr/src/googletest/googlemock/src', '/usr/src/gmock/src', '/usr/src/gmock']:
            if os.path.exists(d):
                self.is_found = True
                # Yes, we need both because there are multiple
                # versions of gmock that do different things.
                d2 = os.path.normpath(os.path.join(d, '..'))
                self.compile_args = ['-I' + d, '-I' + d2]
                self.link_args = []
                all_src = mesonlib.File.from_absolute_file(os.path.join(d, 'gmock-all.cc'))
                main_src = mesonlib.File.from_absolute_file(os.path.join(d, 'gmock_main.cc'))
                if kwargs.get('main', False):
                    self.sources = [all_src, main_src]
                else:
                    self.sources = [all_src]
                mlog.log('Dependency GMock found:', mlog.green('YES'), '(building self)')
                return

        mlog.log('Dependency GMock found:', mlog.red('NO'))
        self.is_found = False

    def get_version(self):
        return '1.something_maybe'

    def get_compile_args(self):
        return self.compile_args

    def get_sources(self):
        return self.sources

    def get_link_args(self):
        return self.link_args

    def found(self):
        return self.is_found

class QtBaseDependency(Dependency):
    def __init__(self, name, env, kwargs):
        Dependency.__init__(self, name, kwargs)
        self.name = name
        self.qtname = name.capitalize()
        self.qtver = name[-1]
        if self.qtver == "4":
            self.qtpkgname = 'Qt'
        else:
            self.qtpkgname = self.qtname
        self.root = '/usr'
        self.bindir = None
        self.silent = kwargs.get('silent', False)
        # We store the value of required here instead of passing it on to
        # PkgConfigDependency etc because we want to try the qmake-based
        # fallback as well.
        self.required = kwargs.pop('required', True)
        kwargs['required'] = False
        mods = kwargs.get('modules', [])
        self.cargs = []
        self.largs = []
        self.is_found = False
        if isinstance(mods, str):
            mods = [mods]
        if len(mods) == 0:
            raise DependencyException('No ' + self.qtname + '  modules specified.')
        type_text = 'cross' if env.is_cross_build() else 'native'
        found_msg = '{} {} {{}} dependency (modules: {}) found:' \
                    ''.format(self.qtname, type_text, ', '.join(mods))
        from_text = 'pkg-config'

        # Keep track of the detection methods used, for logging purposes.
        methods = []
        # Prefer pkg-config, then fallback to `qmake -query`
        if 'pkgconfig' in self.methods:
            self._pkgconfig_detect(mods, env, kwargs)
            methods.append('pkgconfig')
        if not self.is_found and 'qmake' in self.methods:
            from_text = self._qmake_detect(mods, env, kwargs)
            methods.append('qmake-' + self.name)
            methods.append('qmake')
        if not self.is_found:
            # Reset compile args and link args
            self.cargs = []
            self.largs = []
            from_text = '(checked {})'.format(mlog.format_list(methods))
            self.version = 'none'
            if self.required:
                err_msg = '{} {} dependency not found {}' \
                          ''.format(self.qtname, type_text, from_text)
                raise DependencyException(err_msg)
            if not self.silent:
                mlog.log(found_msg.format(from_text), mlog.red('NO'))
            return
        from_text = '`{}`'.format(from_text)
        if not self.silent:
            mlog.log(found_msg.format(from_text), mlog.green('YES'))

    def compilers_detect(self):
        "Detect Qt (4 or 5) moc, uic, rcc in the specified bindir or in PATH"
        if self.bindir:
            moc = ExternalProgram(os.path.join(self.bindir, 'moc'), silent=True)
            uic = ExternalProgram(os.path.join(self.bindir, 'uic'), silent=True)
            rcc = ExternalProgram(os.path.join(self.bindir, 'rcc'), silent=True)
        else:
            # We don't accept unsuffixed 'moc', 'uic', and 'rcc' because they
            # are sometimes older, or newer versions.
            moc = ExternalProgram('moc-' + self.name, silent=True)
            uic = ExternalProgram('uic-' + self.name, silent=True)
            rcc = ExternalProgram('rcc-' + self.name, silent=True)
        return moc, uic, rcc

    def _pkgconfig_detect(self, mods, env, kwargs):
        modules = OrderedDict()
        for module in mods:
            modules[module] = PkgConfigDependency(self.qtpkgname + module, env, kwargs)
        self.is_found = True
        for m in modules.values():
            if not m.found():
                self.is_found = False
                return
            self.cargs += m.get_compile_args()
            self.largs += m.get_link_args()
        self.version = m.modversion
        # Try to detect moc, uic, rcc
        if 'Core' in modules:
            core = modules['Core']
        else:
            corekwargs = {'required': 'false', 'silent': 'true'}
            core = PkgConfigDependency(self.qtpkgname + 'Core', env, corekwargs)
        # Used by self.compilers_detect()
        self.bindir = self.get_pkgconfig_host_bins(core)
        if not self.bindir:
            # If exec_prefix is not defined, the pkg-config file is broken
            prefix = core.get_pkgconfig_variable('exec_prefix')
            if prefix:
                self.bindir = os.path.join(prefix, 'bin')

    def _find_qmake(self, qmake, env):
        # Even when cross-compiling, if we don't get a cross-info qmake, we
        # fallback to using the qmake in PATH because that's what we used to do
        if env.is_cross_build():
            qmake = env.cross_info.config['binaries'].get('qmake', qmake)
        return ExternalProgram(qmake, silent=True)

    def _qmake_detect(self, mods, env, kwargs):
        for qmake in ('qmake-' + self.name, 'qmake'):
            self.qmake = self._find_qmake(qmake, env)
            if not self.qmake.found():
                continue
            # Check that the qmake is for qt5
            pc, stdo = Popen_safe(self.qmake.get_command() + ['-v'])[0:2]
            if pc.returncode != 0:
                continue
            if not 'Qt version ' + self.qtver in stdo:
                mlog.log('QMake is not for ' + self.qtname)
                continue
            # Found qmake for Qt5!
            break
        else:
            # Didn't find qmake :(
            return
        self.version = re.search(self.qtver + '(\.\d+)+', stdo).group(0)
        # Query library path, header path, and binary path
        mlog.log("Found qmake:", mlog.bold(self.qmake.get_name()), '(%s)' % self.version)
        stdo = Popen_safe(self.qmake.get_command() + ['-query'])[1]
        qvars = {}
        for line in stdo.split('\n'):
            line = line.strip()
            if line == '':
                continue
            (k, v) = tuple(line.split(':', 1))
            qvars[k] = v
        if mesonlib.is_osx():
            return self._framework_detect(qvars, mods, kwargs)
        incdir = qvars['QT_INSTALL_HEADERS']
        self.cargs.append('-I' + incdir)
        libdir = qvars['QT_INSTALL_LIBS']
        # Used by self.compilers_detect()
        self.bindir = qvars['QT_INSTALL_BINS']
        self.is_found = True
        for module in mods:
            mincdir = os.path.join(incdir, 'Qt' + module)
            self.cargs.append('-I' + mincdir)
            if for_windows(env.is_cross_build(), env):
                libfile = os.path.join(libdir, self.qtpkgname + module + '.lib')
                if not os.path.isfile(libfile):
                    # MinGW can link directly to .dll
                    libfile = os.path.join(self.bindir, self.qtpkgname + module + '.dll')
                    if not os.path.isfile(libfile):
                        self.is_found = False
                        break
            else:
                libfile = os.path.join(libdir, 'lib{}{}.so'.format(self.qtpkgname, module))
                if not os.path.isfile(libfile):
                    self.is_found = False
                    break
            self.largs.append(libfile)
        return qmake

    def _framework_detect(self, qvars, modules, kwargs):
        libdir = qvars['QT_INSTALL_LIBS']
        for m in modules:
            fname = 'Qt' + m
            fwdep = ExtraFrameworkDependency(fname, kwargs.get('required', True), libdir, kwargs)
            self.cargs.append('-F' + libdir)
            if fwdep.found():
                self.is_found = True
                self.cargs += fwdep.get_compile_args()
                self.largs += fwdep.get_link_args()
        # Used by self.compilers_detect()
        self.bindir = qvars['QT_INSTALL_BINS']

    def get_version(self):
        return self.version

    def get_compile_args(self):
        return self.cargs

    def get_sources(self):
        return []

    def get_link_args(self):
        return self.largs

    def get_methods(self):
        return ['pkgconfig', 'qmake']

    def found(self):
        return self.is_found

    def get_exe_args(self, compiler):
        # Originally this was -fPIE but nowadays the default
        # for upstream and distros seems to be -reduce-relocations
        # which requires -fPIC. This may cause a performance
        # penalty when using self-built Qt or on platforms
        # where -fPIC is not required. If this is an issue
        # for you, patches are welcome.
        return compiler.get_pic_args()

class Qt5Dependency(QtBaseDependency):
    def __init__(self, env, kwargs):
        QtBaseDependency.__init__(self, 'qt5', env, kwargs)

    def get_pkgconfig_host_bins(self, core):
        return core.get_pkgconfig_variable('host_bins')

class Qt4Dependency(QtBaseDependency):
    def __init__(self, env, kwargs):
        QtBaseDependency.__init__(self, 'qt4', env, kwargs)

    def get_pkgconfig_host_bins(self, core):
        # Only return one bins dir, because the tools are generally all in one
        # directory for Qt4, in Qt5, they must all be in one directory. Return
        # the first one found among the bin variables, in case one tool is not
        # configured to be built.
        applications = ['moc', 'uic', 'rcc', 'lupdate', 'lrelease']
        for application in applications:
            try:
                return os.path.dirname(core.get_pkgconfig_variable('%s_location' % application))
            except MesonException:
                pass

class GnuStepDependency(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'gnustep', kwargs)
        self.required = kwargs.get('required', True)
        self.modules = kwargs.get('modules', [])
        self.detect()

    def detect(self):
        self.confprog = 'gnustep-config'
        try:
            gp = Popen_safe([self.confprog, '--help'])[0]
        except (FileNotFoundError, PermissionError):
            self.args = None
            mlog.log('Dependency GnuStep found:', mlog.red('NO'), '(no gnustep-config)')
            return
        if gp.returncode != 0:
            self.args = None
            mlog.log('Dependency GnuStep found:', mlog.red('NO'))
            return
        if 'gui' in self.modules:
            arg = '--gui-libs'
        else:
            arg = '--base-libs'
        fp, flagtxt, flagerr = Popen_safe([self.confprog, '--objc-flags'])
        if fp.returncode != 0:
            raise DependencyException('Error getting objc-args: %s %s' % (flagtxt, flagerr))
        args = flagtxt.split()
        self.args = self.filter_arsg(args)
        fp, libtxt, liberr = Popen_safe([self.confprog, arg])
        if fp.returncode != 0:
            raise DependencyException('Error getting objc-lib args: %s %s' % (libtxt, liberr))
        self.libs = self.weird_filter(libtxt.split())
        self.version = self.detect_version()
        mlog.log('Dependency', mlog.bold('GnuStep'), 'found:',
                 mlog.green('YES'), self.version)

    def weird_filter(self, elems):
        """When building packages, the output of the enclosing Make
is sometimes mixed among the subprocess output. I have no idea
why. As a hack filter out everything that is not a flag."""
        return [e for e in elems if e.startswith('-')]

    def filter_arsg(self, args):
        """gnustep-config returns a bunch of garbage args such
        as -O2 and so on. Drop everything that is not needed."""
        result = []
        for f in args:
            if f.startswith('-D') \
                    or f.startswith('-f') \
                    or f.startswith('-I') \
                    or f == '-pthread' \
                    or (f.startswith('-W') and not f == '-Wall'):
                result.append(f)
        return result

    def detect_version(self):
        gmake = self.get_variable('GNUMAKE')
        makefile_dir = self.get_variable('GNUSTEP_MAKEFILES')
        # This Makefile has the GNUStep version set
        base_make = os.path.join(makefile_dir, 'Additional', 'base.make')
        # Print the Makefile variable passed as the argument. For instance, if
        # you run the make target `print-SOME_VARIABLE`, this will print the
        # value of the variable `SOME_VARIABLE`.
        printver = "print-%:\n\t@echo '$($*)'"
        env = os.environ.copy()
        # See base.make to understand why this is set
        env['FOUNDATION_LIB'] = 'gnu'
        p, o, e = Popen_safe([gmake, '-f', '-', '-f', base_make,
                              'print-GNUSTEP_BASE_VERSION'],
                             env=env, write=printver, stdin=subprocess.PIPE)
        version = o.strip()
        if not version:
            mlog.debug("Couldn't detect GNUStep version, falling back to '1'")
            # Fallback to setting some 1.x version
            version = '1'
        return version

    def get_variable(self, var):
        p, o, e = Popen_safe([self.confprog, '--variable=' + var])
        if p.returncode != 0 and self.required:
            raise DependencyException('{!r} for variable {!r} failed to run'
                                      ''.format(self.confprog, var))
        return o.strip()

    def found(self):
        return self.args is not None

    def get_version(self):
        return self.version

    def get_compile_args(self):
        if self.args is None:
            return []
        return self.args

    def get_link_args(self):
        return self.libs

class AppleFrameworks(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'appleframeworks', kwargs)
        modules = kwargs.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]
        if len(modules) == 0:
            raise DependencyException("AppleFrameworks dependency requires at least one module.")
        self.frameworks = modules

    def get_link_args(self):
        args = []
        for f in self.frameworks:
            args.append('-framework')
            args.append(f)
        return args

    def found(self):
        return mesonlib.is_osx()

    def get_version(self):
        return 'unknown'

class GLDependency(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'gl', kwargs)
        self.is_found = False
        self.cargs = []
        self.linkargs = []
        if 'pkgconfig' in self.methods:
            try:
                pcdep = PkgConfigDependency('gl', environment, kwargs)
                if pcdep.found():
                    self.type_name = 'pkgconfig'
                    self.is_found = True
                    self.cargs = pcdep.get_compile_args()
                    self.linkargs = pcdep.get_link_args()
                    self.version = pcdep.get_version()
                    return
            except Exception:
                pass
        if 'system' in self.methods:
            if mesonlib.is_osx():
                self.is_found = True
                self.linkargs = ['-framework', 'OpenGL']
                self.version = '1' # FIXME
                return
            if mesonlib.is_windows():
                self.is_found = True
                self.linkargs = ['-lopengl32']
                self.version = '1' # FIXME: unfixable?
                return

    def get_link_args(self):
        return self.linkargs

    def get_version(self):
        return self.version

    def get_methods(self):
        if mesonlib.is_osx() or mesonlib.is_windows():
            return ['pkgconfig', 'system']
        else:
            return ['pkgconfig']

# There are three different ways of depending on SDL2:
# sdl2-config, pkg-config and OSX framework
class SDL2Dependency(Dependency):
    def __init__(self, environment, kwargs):
        Dependency.__init__(self, 'sdl2', kwargs)
        self.is_found = False
        self.cargs = []
        self.linkargs = []
        if 'pkgconfig' in self.methods:
            try:
                pcdep = PkgConfigDependency('sdl2', environment, kwargs)
                if pcdep.found():
                    self.type_name = 'pkgconfig'
                    self.is_found = True
                    self.cargs = pcdep.get_compile_args()
                    self.linkargs = pcdep.get_link_args()
                    self.version = pcdep.get_version()
                    return
            except Exception as e:
                mlog.debug('SDL 2 not found via pkgconfig. Trying next, error was:', str(e))
                pass
        if 'sdlconfig' in self.methods:
            sdlconf = shutil.which('sdl2-config')
            if sdlconf:
                stdo = Popen_safe(['sdl2-config', '--cflags'])[1]
                self.cargs = stdo.strip().split()
                stdo = Popen_safe(['sdl2-config', '--libs'])[1]
                self.linkargs = stdo.strip().split()
                stdo = Popen_safe(['sdl2-config', '--version'])[1]
                self.version = stdo.strip()
                self.is_found = True
                mlog.log('Dependency', mlog.bold('sdl2'), 'found:', mlog.green('YES'),
                         self.version, '(%s)' % sdlconf)
                return
            mlog.debug('Could not find sdl2-config binary, trying next.')
        if 'extraframework' in self.methods:
            if mesonlib.is_osx():
                fwdep = ExtraFrameworkDependency('sdl2', kwargs.get('required', True), None, kwargs)
                if fwdep.found():
                    self.is_found = True
                    self.cargs = fwdep.get_compile_args()
                    self.linkargs = fwdep.get_link_args()
                    self.version = '2' # FIXME
                    return
            mlog.log('Dependency', mlog.bold('sdl2'), 'found:', mlog.red('NO'))

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.linkargs

    def found(self):
        return self.is_found

    def get_version(self):
        return self.version

    def get_methods(self):
        if mesonlib.is_osx():
            return ['pkgconfig', 'sdlconfig', 'extraframework']
        else:
            return ['pkgconfig', 'sdlconfig']

class ExtraFrameworkDependency(Dependency):
    def __init__(self, name, required, path, kwargs):
        Dependency.__init__(self, 'extraframeworks', kwargs)
        self.name = None
        self.detect(name, path)
        if self.found():
            mlog.log('Dependency', mlog.bold(name), 'found:', mlog.green('YES'),
                     os.path.join(self.path, self.name))
        else:
            mlog.log('Dependency', name, 'found:', mlog.red('NO'))

    def detect(self, name, path):
        lname = name.lower()
        if path is None:
            paths = ['/Library/Frameworks']
        else:
            paths = [path]
        for p in paths:
            for d in os.listdir(p):
                fullpath = os.path.join(p, d)
                if lname != d.split('.')[0].lower():
                    continue
                if not stat.S_ISDIR(os.stat(fullpath).st_mode):
                    continue
                self.path = p
                self.name = d
                return

    def get_compile_args(self):
        if self.found():
            return ['-I' + os.path.join(self.path, self.name, 'Headers')]
        return []

    def get_link_args(self):
        if self.found():
            return ['-F' + self.path, '-framework', self.name.split('.')[0]]
        return []

    def found(self):
        return self.name is not None

    def get_version(self):
        return 'unknown'

class ThreadDependency(Dependency):
    def __init__(self, environment, kwargs):
        super().__init__('threads')
        self.name = 'threads'
        self.is_found = True
        mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.green('YES'))

    def need_threads(self):
        return True

    def get_version(self):
        return 'unknown'

class Python3Dependency(Dependency):
    def __init__(self, environment, kwargs):
        super().__init__('python3', kwargs)
        self.name = 'python3'
        self.is_found = False
        # We can only be sure that it is Python 3 at this point
        self.version = '3'
        if 'pkgconfig' in self.methods:
            try:
                pkgdep = PkgConfigDependency('python3', environment, kwargs)
                if pkgdep.found():
                    self.cargs = pkgdep.cargs
                    self.libs = pkgdep.libs
                    self.version = pkgdep.get_version()
                    self.is_found = True
                    return
            except Exception:
                pass
        if not self.is_found:
            if mesonlib.is_windows() and 'sysconfig' in self.methods:
                self._find_libpy3_windows(environment)
            elif mesonlib.is_osx() and 'extraframework' in self.methods:
                # In OSX the Python 3 framework does not have a version
                # number in its name.
                fw = ExtraFrameworkDependency('python', False, None, kwargs)
                if fw.found():
                    self.cargs = fw.get_compile_args()
                    self.libs = fw.get_link_args()
                    self.is_found = True
        if self.is_found:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.green('YES'))
        else:
            mlog.log('Dependency', mlog.bold(self.name), 'found:', mlog.red('NO'))

    def _find_libpy3_windows(self, env):
        '''
        Find python3 libraries on Windows and also verify that the arch matches
        what we are building for.
        '''
        pyarch = sysconfig.get_platform()
        arch = detect_cpu_family(env.coredata.compilers)
        if arch == 'x86':
            arch = '32'
        elif arch == 'x86_64':
            arch = '64'
        else:
            # We can't cross-compile Python 3 dependencies on Windows yet
            mlog.log('Unknown architecture {!r} for'.format(arch),
                     mlog.bold(self.name))
            self.is_found = False
            return
        # Pyarch ends in '32' or '64'
        if arch != pyarch[-2:]:
            mlog.log('Need', mlog.bold(self.name),
                     'for {}-bit, but found {}-bit'.format(arch, pyarch[-2:]))
            self.is_found = False
            return
        inc = sysconfig.get_path('include')
        platinc = sysconfig.get_path('platinclude')
        self.cargs = ['-I' + inc]
        if inc != platinc:
            self.cargs.append('-I' + platinc)
        # Nothing exposes this directly that I coulf find
        basedir = sysconfig.get_config_var('base')
        vernum = sysconfig.get_config_var('py_version_nodot')
        self.libs = ['-L{}/libs'.format(basedir),
                     '-lpython{}'.format(vernum)]
        self.version = sysconfig.get_config_var('py_version_short')
        self.is_found = True

    def get_compile_args(self):
        return self.cargs

    def get_link_args(self):
        return self.libs

    def get_methods(self):
        if mesonlib.is_windows():
            return ['pkgconfig', 'sysconfig']
        elif mesonlib.is_osx():
            return ['pkgconfig', 'extraframework']
        else:
            return ['pkgconfig']

    def get_version(self):
        return self.version

class ValgrindDependency(PkgConfigDependency):

    def __init__(self, environment, kwargs):
        PkgConfigDependency.__init__(self, 'valgrind', environment, kwargs)

    def get_link_args(self):
        return []

def get_dep_identifier(name, kwargs):
    elements = [name]
    modlist = kwargs.get('modules', [])
    if isinstance(modlist, str):
        modlist = [modlist]
    for module in modlist:
        elements.append(module)
    # We use a tuple because we need a non-mutable structure to use as the key
    # of a dictionary and a string has potential for name collisions
    identifier = tuple(elements)
    identifier += ('main', kwargs.get('main', False))
    identifier += ('static', kwargs.get('static', False))
    if 'fallback' in kwargs:
        f = kwargs.get('fallback')
        identifier += ('fallback', f[0], f[1])
    return identifier

def find_external_dependency(name, environment, kwargs):
    required = kwargs.get('required', True)
    if not isinstance(required, bool):
        raise DependencyException('Keyword "required" must be a boolean.')
    lname = name.lower()
    if lname in packages:
        dep = packages[lname](environment, kwargs)
        if required and not dep.found():
            raise DependencyException('Dependency "%s" not found' % name)
        return dep
    pkg_exc = None
    pkgdep = None
    try:
        pkgdep = PkgConfigDependency(name, environment, kwargs)
        if pkgdep.found():
            return pkgdep
    except Exception as e:
        pkg_exc = e
    if mesonlib.is_osx():
        fwdep = ExtraFrameworkDependency(name, required, None, kwargs)
        if required and not fwdep.found():
            m = 'Dependency {!r} not found, tried Extra Frameworks ' \
                'and Pkg-Config:\n\n' + str(pkg_exc)
            raise DependencyException(m.format(name))
        return fwdep
    if pkg_exc is not None:
        raise pkg_exc
    mlog.log('Dependency', mlog.bold(name), 'found:', mlog.red('NO'))
    return pkgdep

# This has to be at the end so the classes it references
# are defined.
packages = {'boost': BoostDependency,
            'gtest': GTestDependency,
            'gmock': GMockDependency,
            'qt5': Qt5Dependency,
            'qt4': Qt4Dependency,
            'gnustep': GnuStepDependency,
            'appleframeworks': AppleFrameworks,
            'wxwidgets': WxDependency,
            'sdl2': SDL2Dependency,
            'gl': GLDependency,
            'threads': ThreadDependency,
            'python3': Python3Dependency,
            'valgrind': ValgrindDependency,
            }
