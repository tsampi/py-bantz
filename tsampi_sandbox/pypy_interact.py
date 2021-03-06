#! /usr/bin/env python

"""Interacts with a PyPy subprocess translated with --sandbox.

Usage:
    pypy-sandbox [options] <args...>

Options:
    --tmp=DIR     the real directory that corresponds to the virtual /tmp,
                  which is the virtual current dir (always read-only for now)
    --heapsize=N  limit memory usage to N bytes, or kilo- mega- giga-bytes
                  with the 'k', 'm' or 'g' suffix respectively.
    --timeout=N   limit execution time to N (real-time) seconds.
    --log=FILE    log all user input into the FILE.
    --verbose     log all proxied system calls.

Note that you can get readline-like behavior with a tool like 'ledit',
provided you use enough -u options:

    ledit python -u /usr/bin/pypy-sandbox -u
"""

import sys
import os
import posixpath
import errno
import stat
import time
import socket
import urllib

sys.path.insert(0, os.path.realpath(
    os.path.join(os.path.dirname(__file__), '..', '..')))
from rpython.translator.sandbox.sandlib import SimpleIOSandboxedProc
from rpython.translator.sandbox.sandlib import VirtualizedSandboxedProc, RESULTTYPE_STATRESULT
from rpython.translator.sandbox.vfs import Dir, RealDir, RealFile

LIB_ROOT = '/code/repos/tsampi-0/tsampi/pypy/'

# Maps flag constants to the string eg. 'a+'
# https://docs.python.org/2/library/os.html#open-constants
OPEN_FLAGS = {0: 'r', 2: 'r+', 65: 'a', 66: 'a+', 577: 'w', 578: 'w+'}

class RealWritableFile(RealFile):
    read_only = False

    def __repr__(self):
        return '<RealWritableFile %s>' % (self.path,)

    def stat(self):
        # print('stat', self)
        return super(RealWritableFile, self).stat()

    def access(self, mode):
        # print('access', mode)
        s = self.stat()
        e_mode = s.st_mode & stat.S_IRWXO
        if UID == s.st_uid:
            e_mode |= (s.st_mode & stat.S_IRWXU) >> 6
        if GID == s.st_gid:
            e_mode |= (s.st_mode & stat.S_IRWXG) >> 3
        return (e_mode & mode) == mode

    def open(self, flags):
        # print('opening', self)
        try:
            return open(self.path, OPEN_FLAGS[flags])
        except IOError as e:
            raise OSError(e.errno, "open failed")


class RealWritableDir(RealDir):
    # If show_dotfiles=False, we pretend that all files whose name starts
    # with '.' simply don't exist.  If follow_links=True, then symlinks are
    # transparently followed (they look like a regular file or directory to
    # the sandboxed process).  If follow_links=False, the subprocess is
    # not allowed to access them at all.  Finally, exclude is a list of
    # file endings that we filter out (note that we also filter out files
    # with the same ending but a different case, to be safe).
    read_only = False

    def __repr__(self):
        return '<RealWritableDir %s>' % (self.path,)

    def stat(self):
        # print('stat')
        return super(RealWritableDir, self).stat()

    def access(self, mode):
        # print('access', mode)
        s = self.stat()
        e_mode = s.st_mode & stat.S_IRWXO
        if UID == s.st_uid:
            e_mode |= (s.st_mode & stat.S_IRWXU) >> 6
        if GID == s.st_gid:
            e_mode |= (s.st_mode & stat.S_IRWXG) >> 3
        return (e_mode & mode) == mode

    def join(self, name):
        if name.startswith('.') and not self.show_dotfiles:
            raise OSError(errno.ENOENT, name)
        for excl in self.exclude:
            if name.lower().endswith(excl):
                raise OSError(errno.ENOENT, name)
        path = os.path.join(self.path, name)
        # print('self.path', self.path)
        try:
            if self.follow_links:
                st = os.stat(path)
            else:
                st = os.lstat(path)
        except Exception as e:
            # print(e)
            raise
        if stat.S_ISDIR(st.st_mode):
            # print('ISDIR')
            return RealWritableDir(path, show_dotfiles=self.show_dotfiles,
                                   follow_links=self.follow_links,
                                   exclude=self.exclude)
        elif stat.S_ISREG(st.st_mode):
            # print('ISREG')
            return RealWritableFile(path)
        else:
            # print('IS SOMETHING ELSE')
            # don't allow access to symlinks and other special files
            raise OSError(errno.EACCES, path)


class PyPySandboxedProc(VirtualizedSandboxedProc, SimpleIOSandboxedProc):
    argv0 = '/bin/pypy-c'
    virtual_cwd = '/tmp'
    virtual_env = {}
    virtual_console_isatty = True

    def __init__(self, executable, arguments, tmpdir=None, debug=True, lib_root=None, user='sybil'):
        self.lib_root = lib_root
        self.executable = executable = os.path.abspath(executable)
        self.tmpdir = tmpdir
        self.debug = debug
        self.sockets = {}
        self.virtual_env['user'] = user
        # print(executable)
        super(PyPySandboxedProc, self).__init__([self.argv0] + arguments,
                                                executable=executable)

    def translate_path(self, vpath):
        # print('translate_path', vpath)
        # XXX this assumes posix vpaths for now, but os-specific real paths
        vpath = posixpath.normpath(posixpath.join(self.virtual_cwd, vpath))
        dirnode = self.virtual_root
        components = [component for component in vpath.split('/')]
        for component in components[:-1]:
            if component:
                dirnode = dirnode.join(component)
                if dirnode.kind != stat.S_IFDIR:
                    raise OSError(errno.ENOTDIR, component)
        return dirnode, components[-1]


    def do_ll_os__ll_os_read(self, fd, size):
        if fd in self.sockets:
           return self.get_file(fd).recv(size)

        f = self.get_file(fd, throw=False)
        if f is None:
            return super(VirtualizedSandboxedProc, self).do_ll_os__ll_os_read(
                fd, size)
        else:
            if not (0 <= size <= sys.maxint):
                raise OSError(errno.EINVAL, "invalid read size")
            # don't try to read more than 256KB at once here
            return f.read(min(size, 256 * 1024))

    def do_ll_os__ll_os_write(self, fd, data):
        if fd in self.sockets:
            return self.get_file(fd).send(data)

        if fd in [1, 2]:
            return super(VirtualizedSandboxedProc, self).do_ll_os__ll_os_write(
                fd, data)

        f = self.get_file(fd, throw=False)  # Really? False?
        f.write(data)
        return len(data)

    def check_path(self, vpathname, flags):
        pass

    def do_ll_os__ll_os_fstat(self, fd):
        if fd in self.sockets:
            s = self.get_file(fd)
            st = os.fstat(s.fileno())
            return st
        f, node = self.get_fd(fd)
        return node.stat()
    do_ll_os__ll_os_fstat.resulttype = RESULTTYPE_STATRESULT

    def do_ll_os__ll_os_open(self, vpathname, flags, mode):

        ##
        # No, child. You are not yet ready.
        ##
        #if vpathname.startswith("tcp://"):
        #    host, port = vpathname[6:].split(":")
        #    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        #    sock.connect((host, int(port)))
        #    sock.setblocking(0)
        #    fd = self.allocate_fd(sock)
        #    self.sockets[fd] = vpathname
        #    return fd
        #
        #if vpathname.startswith("http://"):
        #    u = urllib.urlopen(vpathname)
        #    sock = u.fp
        #    fd = self.allocate_fd(sock)
        #    self.sockets[fd] = True
        #    return fd

        # Normalize the pathname within the sandbox fs
        # print('os_open', vpathname, flags, mode, self.tmpdir)
        absvfilename = os.path.normpath(
            os.path.join(self.virtual_cwd, vpathname))
        # print('absvfilename', absvfilename)
        # If this is readonly, don't attempt to creat path and file
        if flags != os.O_RDONLY:
            # print('write')
            if absvfilename.startswith('/tmp/'):
                filename = self.tmpdir + absvfilename[4:]
                #print('filename', filename)
                # make the file and parent dirs before opening it.
                if not os.path.exists(os.path.dirname(filename)):
                    try:
                        #print('making path', os.path.dirname(filename))
                        os.makedirs(os.path.dirname(filename))
                    except OSError as exc:  # Guard against race condition
                        if exc.errno != errno.EEXIST:
                            raise
                # Hack to ensure the file exists in the real fs
                #print('touching file', filename)
                open(filename, "a").close()
        else:
            #print('read only')
            pass

        node = self.get_node(vpathname)
        # all other flags are ignored
        if type(node) is RealWritableFile:
            f = node.open(flags)
        else:
            if flags & (os.O_RDONLY|os.O_WRONLY|os.O_RDWR) != os.O_RDONLY:
               raise OSError(errno.EPERM, "write access denied")
            f = node.open()
        return self.allocate_fd(f, node)

    def build_virtual_root(self):
        # build a virtual file system:
        # * can access its own executable
        # * can access the pure Python libraries
        # * can access the temporary usession directory as /tmp
        exclude = ['.pyc', '.pyo']
        if self.tmpdir is None:
            tmpdirnode = Dir({})
        else:
            tmpdirnode = RealWritableDir(self.tmpdir, exclude=exclude)
            #tmpdirnode = RealDir(self.tmpdir, exclude=exclude)
        libroot = self.lib_root

        return Dir({
            'bin': Dir({
                'pypy-c': RealFile(self.executable, mode=0111),
                'lib-python': RealDir(os.path.join(libroot, 'lib-python'),
                                      exclude=exclude),
                'lib_pypy': RealDir(os.path.join(libroot, 'lib_pypy'),
                                    exclude=exclude),
            }),
            'tmp': tmpdirnode,
        })


def main():
    from getopt import getopt      # and not gnu_getopt!
    options, arguments = getopt(sys.argv[1:], 't:hv',
                                ['lib_root=', 'tmp=', 'heapsize=', 'timeout=', 'log=',
                                 'user=', 'verbose', 'help'])
    tmpdir = None
    timeout = None
    logfile = None
    debug = False
    lib_root=None
    extraoptions = []

    def help():
        print >> sys.stderr, __doc__
        sys.exit(2)

    for option, value in options:
        if option in ['-t', '--tmp']:
            value = os.path.abspath(value)
            if not os.path.isdir(value):
                raise OSError("%r is not a directory" % (value,))
            tmpdir = value
        elif option == '--heapsize':
            value = value.lower()
            if value.endswith('k'):
                bytes = int(value[:-1]) * 1024
            elif value.endswith('m'):
                bytes = int(value[:-1]) * 1024 * 1024
            elif value.endswith('g'):
                bytes = int(value[:-1]) * 1024 * 1024 * 1024
            else:
                bytes = int(value)
            if bytes <= 0:
                raise ValueError
            if bytes > sys.maxint:
                raise OverflowError("--heapsize maximum is %d" % sys.maxint)
            extraoptions[:0] = ['--heapsize', str(bytes)]
        elif option == '--timeout':
            timeout = int(value)
        elif option == '--user':
            user = value
        elif option == '--lib_root':
            lib_root = value
        elif option == '--log':
            logfile = value
        elif option in ['-v', '--verbose']:
            debug = True
        elif option in ['-h', '--help']:
            help()
        else:
            raise ValueError(option)

    sys_implementation = getattr(sys, 'implementation', sys)
    multiarch_triple = getattr(sys_implementation, '_multiarch', None)
    if not multiarch_triple:
        from subprocess import check_output
        multiarch_triple = check_output(('gcc', '--print-multiarch')).strip()

    sandproc = PyPySandboxedProc('/usr/lib/pypy-sandbox/pypy-c-sandbox',
                                 extraoptions + arguments,
                                 tmpdir=tmpdir, debug=debug, lib_root=lib_root, user=user)
    if timeout is not None:
        sandproc.settimeout(timeout, interrupt_main=True)
    if logfile is not None:
        sandproc.setlogfile(logfile)
    try:
        sandproc.interact()
    finally:
        sandproc.kill()

if __name__ == '__main__':
    main()
