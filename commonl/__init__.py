#! /usr/bin/python2
#
# Copyright (c) 2017 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

"""
Common timo infrastructure and code
Command line and logging helpers

.. moduleauthor:: FIXME <fixme@domain.com>

.. admonition:: FIXMEs

  - This is still leaking temporary files (subpython's stdout and
    stderr) when running top level tests.

"""
import argparse
import base64
import bisect
import contextlib
import errno
import fcntl
import fnmatch
import glob
import hashlib
import imp
import importlib
import io
import inspect
import logging
import numbers
import os
import random
import re
import signal
import socket
import string
import struct
import subprocess
import sys
import tempfile
import termios
import thread
import threading
import time
import traceback
import types

import keyring
import requests

from . import expr_parser

logging.addLevelName(50, "C")
logging.addLevelName(40, "E")
logging.addLevelName(30, "W")
logging.addLevelName(20, "I")
logging.addLevelName(10, "D")
logging.addLevelName(9, "D2")
logging.addLevelName(8, "D3")
logging.addLevelName(7, "D4")
logging.addLevelName(6, "D5")

def config_import_file(filename, namespace = "__main__",
                       raise_on_fail = True):
    """Import a Python [configuration] file.

    Any symbol available to the current namespace is available to the
    configuration file.

    :param filename: path and file name to load.

    :param namespace: namespace where to insert the configuration file

    :param bool raise_on_fail: (optional) raise an exception if the
      importing of the config file fails.

    >>> timo.config_import_file("some/path/file.py", "__main__")

    """

    logging.log(9, "%s: configuration file being loaded", filename)
    try:
        imp.load_source(namespace, filename)
        sys.stdout.flush()
        sys.stderr.flush()
        logging.debug("%s: configuration file imported", filename)
    except Exception as e:	# pylint: disable = W0703
        # throw a wide net to catch any errors in filename
        logging.exception("%s: can't load config file: %s", filename, e)
        if raise_on_fail:
            raise

def path_expand(path_list):
    # Compose the path list
    _list = []
    for _paths in path_list:
        paths = _paths.split(":")
        for path in paths:
            if path == "":
                _list = []
            else:
                _list.append(os.path.expanduser(path))
    return _list

def config_import(path_list, file_regex, namespace = "__main__",
                  raise_on_fail = True):
    """Import Python [configuration] files that match file_regex in any of
    the list of given paths into the given namespace.

    Any symbol available to the current namespace is available to the
    configuration file.

    :param paths: list of paths where to import from; each item can be
      a list of colon separated paths and thus the list would be further
      expanded. If an element is the empty list, it removes the
      current list.

    :param file_regex: a compiled regular expression to match the file
      name against.

    :param namespace: namespace where to insert the configuration file

    :param bool raise_on_fail: (optional) raise an exception if the
      importing of the config file fails.

    >>> timo.config_import([ ".config:/etc/config" ],
    >>>                    re.compile("conf[_-].*.py"), "__main__")

    """

    # Compose the path list
    _list = path_expand(path_list)
    paths_done = set()
    # Bring in config files
    # FIXME: expand ~ -> $HOME
    for path in _list:
        abs_path = os.path.abspath(os.path.normpath(path))
        if abs_path in paths_done:
            # Skip what we have done already
            continue
        logging.log(8, "%s: loading configuration files %s",
                    path, file_regex.pattern)
        try:
            if not os.path.isdir(path):
                logging.log(7, "%s: ignoring non-directory", path)
                continue
            for filename in sorted(os.listdir(path)):
                if not file_regex.match(filename):
                    logging.log(6, "%s/%s: ignored", path, filename)
                    continue
                config_import_file(path + "/" + filename, namespace)
        except Exception:	# pylint: disable = W0703
            # throw a wide net to catch any errors in filename
            logging.error("%s: can't load config files", path)
            if raise_on_fail:
                raise
        else:
            logging.log(9, "%s: loaded configuration files %s",
                        path, file_regex.pattern)
        paths_done.add(abs_path)

def logging_verbosity_inc(level):
    if level == 0:
        return
    if level > logging.DEBUG:
        delta = 10
    else:
        delta = 1
    return level - delta


def logfile_open(tag, cls = None, delete = True, bufsize = 0,
                 suffix = ".log", who = None, directory = None):
    assert isinstance(tag, basestring)
    if who == None:
        frame = inspect.stack(0)[1][0]
        who = frame.f_code.co_name + ":%d" % frame.f_lineno
    if tag != "":
        tag += "-"
    if cls != None:
        clstag = cls.__name__ + "."
    else:
        clstag = ''
    return tempfile.NamedTemporaryFile(
        prefix = os.path.basename(sys.argv[0]) + ":"
        + clstag + who + "-" + tag,
        suffix = suffix, delete = delete, bufsize = bufsize, dir = directory)

class _Action_increase_level(argparse.Action):
    def __init__(self, option_strings, dest, default = None, required = False,
                 nargs = None, **kwargs):
        super(_Action_increase_level, self).__init__(
            option_strings, dest, nargs = 0, required = required,
            **kwargs)

    #
    # Python levels are 50, 40, 30, 20, 10 ... (debug) 9 8 7 6 5 ... :)
    def __call__(self, parser, namespace, values, option_string = None):
        if namespace.level == None:
            namespace.level = logging.ERROR
        namespace.level = logging_verbosity_inc(namespace.level)

def log_format_compose(log_format, log_pid, log_time = False):
    if log_pid == True:
        log_format = log_format.replace(
            "%(levelname)s",
            "%(levelname)s[%(process)d]", 1)
    if log_time == True:
        log_format = log_format.replace(
            "%(levelname)s",
            "%(levelname)s/%(asctime)s", 1)
    return log_format

def cmdline_log_options(parser):
    """Initializes a parser with the standard command line options to
    control verbosity when using the logging module

    :param python:argparse.ArgParser parser: command line argument parser

    -v|--verbose to increase verbosity (defaults to print/log errors only)

    Note that after processing the command line options, you need to
    initialize logging with:

    >>> import logging, argparse, timo.core
    >>> arg_parser = argparse.ArgumentParser()
    >>> timo.core.cmdline_log_options(arg_parser)
    >>> args = arg_parser.parse_args()
    >>> logging.basicConfig(format = args.log_format, level = args.level)

    """
    if not isinstance(parser, argparse.ArgumentParser):
        raise TypeError("parser argument has to be an argparse.ArgumentParser")

    parser.add_argument("-v", "--verbose",
                        dest = "level",
                        action = _Action_increase_level, nargs = 0,
                        help = "Increase verbosity")
    parser.add_argument("--log-pid-tid", action = "store_true",
                        default = False,
                        help = "Print PID and TID in the logs")
    parser.add_argument("--log-time", action = "store_true",
                        default = False,
                        help = "Print Date and time in the logs")


def mkid(something, l = 10):
    """
    Generate a 10 character base32 ID out of an iterable object

    :param something: anything from which an id has to be generate
      (anything iterable)
    """
    h = hashlib.sha512(something)
    return base64.b32encode(h.digest())[:l].lower()



def trim_trailing(s, trailer):
    """
    Trim *trailer* from the end of *s* (if present) and return it.

    :param str s: string to trim from
    :param str trailer: string to trim
    """
    tl = len(trailer)
    if s[-tl:] == trailer:
        return s[:-tl]
    else:
        return s


def name_make_safe(name, safe_chars = None):
    """
    Given a filename, return the same filename will all characters not
    in the set [-_.0-9a-zA-Z] replaced with _.

    :param str name: name to make *safe*
    :param set safe_chars: (potional) set of characters that are
      considered safe. Defaults to ASCII letters and digits plus - and
      _.
    """
    if safe_chars == None:
        safe_chars = set('-_' + string.ascii_letters + string.digits)
    # We don't use string.translate()'s deletions because it doesn't
    # take them for Unicode strings.
    r = ""
    for c in name:
        if c not in safe_chars:
            c = '_'
        r += c
    return r


def file_name_make_safe(file_name, extra_chars = ":/"):
    """
    Given a filename, return the same filename will all characters not
    in the set [-_.0-9a-zA-Z] removed.

    This is useful to kinda make a URL into a file name, but it's not
    bidirectional (as it is destructive) and not very fool proof.
    """
    # We don't use string.translate()'s deletions because it doesn't
    # take them for Unicode strings.
    r = ""
    for c in file_name:
        if c in set(extra_chars + string.whitespace):
            continue
        r += c
    return r

def hash_file(hash_object, filepath, blk_size = 8192):
    """
    Run a the contents of a file though a hash generator.

    :param hash_object: hash object (from :py:mod:`hashlib`)
    :param str filepath: path to the file to feed
    :param int blk_size: read the file in chunks of that size (in bytes)
    """
    assert hasattr(hash_object, "update")
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(blk_size), b''):
            hash_object.update(chunk)
    return hash_object

def request_response_maybe_raise(response):
    if not response:
        try:
            json = response.json()
            if json != None and 'message' in json:
                message = json['message']
            else:
                message = "no specific error text available"
        except ValueError as e:
            message = "no specific error text available"
        logging.debug("HTTP Error: %s", response.text)
        e = requests.HTTPError(
            "%d: %s" % (response.status_code, message))
        e.status_code = response.status_code
        raise e

def _os_path_split_full(path):
    """
    Split an absolute path in all the directory components
    """
    t = os.path.split(path)
    if t[0] == "/":
        l = [ t[1] ]
    else:
        l = _os_path_split_full(t[0])
        l.append(t[1])
    return l

def os_path_split_full(path):
    """
    Split an absolute path in all the directory components
    """
    parts =  _os_path_split_full(os.path.abspath(path))
    return parts

def progress(msg):
    """
    Print some sort of progress information banner to standard error
    output that will be overriden with real information.

    This only works when stdout or stderr are not redirected to files
    and is intended to give humans a feel of what's going on.
    """
    if not sys.stderr.isatty() or not sys.stdout.isatty():
        return

    _h, w, _hp, _wp = struct.unpack(
        'HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ,
                            struct.pack('HHHH', 0, 0, 0, 0)))
    if len(msg) < w:
        w_len = w - len(msg)
        msg += w_len * " "
    sys.stderr.write(msg + "\r")
    sys.stderr.flush()


def digits_in_base(number, base):
    """
    Convert a number to a list of the digits it would have if written
    in base @base.

    For example:
     - (16, 2) -> [1, 6] as 1*10 + 6 = 16
     - (44, 4) -> [2, 3, 0] as 2*4*4 + 3*4 + 0 = 44
    """
    if number == 0:
        return [ 0 ]
    digits = []
    while number != 0:
        digit = int(number % base)
        number = int(number / base)
        digits.append(digit)
    digits.reverse()
    return digits

def rm_f(filename):
    """
    Remove a file (not a directory) unconditionally, ignore errors if
    it does not exist.
    """
    try:
        os.unlink(filename)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def makedirs_p(dirname, mode = None):
    """
    Create a directory tree, ignoring an error if it already exists

    :param str pathname: directory tree to crate
    :param int mode: mode set the directory to
    """
    try:
        os.makedirs(dirname)
        # yes, this is a race condition--but setting the umask so
        # os.makedirs() gets the right mode would interfere with other
        # threads and processes.
        if mode:
            os.chmod(dirname, mode)
    except OSError:
        if not os.path.isdir(dirname):
            raise

def symlink_f(source, dest):
    """
    Create a symlink, ignoring an error if it already exists

    """
    try:
        os.symlink(source, dest)
    except OSError as e:
        if e.errno != errno.EEXIST or not os.path.islink(dest):
            raise

def _pid_grok(pid):
    if pid == None:
        return None, None
    if isinstance(pid, basestring):
        # Is it a PID encoded as string?
        try:
            return int(pid), None
        except ValueError:
            pass
        # Mite be a pidfile
        try:
            with open(pid) as f:
                pids = f.read()
        except IOError:
            return None, pid
        try:
            return int(pids), pid
        except ValueError:
            return None, pid
    elif isinstance(pid, int):
        # fugly
        return pid, None
    else:
        assert True, "don't know how to convert %s to a PID" % pid

def process_alive(pidfile, path = None):
    """
    Return if a process path/PID combination is alive from the
    standpoint of the calling context (in terms of UID permissions,
    etc).

    :param str pidfile: path to pid file (or)
    :param str pidfile: PID of the process to check (in str form) (or)
    :param int pidfile: PID of the process to check
    :param str path: path binary that runs the process

    :returns: PID number if alive, *None* otherwise (might be running as a
      separate user, etc)
    """
    if path:
        paths = path + ": "
    else:
        paths = ""
    pid, _pidfile = _pid_grok(pidfile)
    if pid == None:
        return None
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:	# Not running
            return None
        if e.errno == errno.EPERM:	# Running, but not our user?
            return None
        raise RuntimeError("%scan't signal pid %d to test if running: %s"
                           % (paths, pid, e))
    if not path:
        return pid
    # Thing is running, let's see what it is
    try:
        _path = os.readlink("/proc/%d/exe" % pid)
    except OSError as e:
        # Usually this means it has died while we checked
        return None
    if path == _path:
        return pid
    else:
        return None

def process_terminate(pid, pidfile = None, tag = None,
                      path = None, wait_to_kill = 0.25):
    """Terminate a process (TERM and KILL after 0.25s)

    :param pid: PID of the process to kill; this can be an
      integer, a string representation of an integer or a path to a
      PIDfile.

    :param str pidfile: (optional) pidfile to remove [deprecated]

    :param str path: (optional) path to the binary

    :param str tag: (optional) prefix to error messages
    """
    if tag == None:
        if path:
            _tag = path
        else:
            _tag = ""
    else:
        _tag = tag + ": "
    _pid, _pidfile = _pid_grok(pid)
    if _pid == None:
        # Nothing to kill
        return
    if path:
        # Thing is running, let's see what it is
        try:
            _path = os.readlink("/proc/%d/exe" % _pid)
        except OSError as e:
            # Usually this means it has died while we checked
            return None
        if os.path.abspath(_path) != os.path.abspath(path):
            return None	            # Not our binary
    try:
        signal_name = "SIGTERM"
        os.kill(_pid, signal.SIGTERM)
        time.sleep(wait_to_kill)
        signal_name = "SIGKILL"
        os.kill(_pid, signal.SIGKILL)
    except OSError as e:
        if e.errno == errno.ESRCH:	# killed already
            return
        else:
            raise RuntimeError("%scan't %s: %s"
                               % (tag, signal_name, e.message))
    finally:
        if _pidfile:
            rm_f(_pidfile)
        if pidfile:	# Extra pidfile to remove, kinda deprecated
            rm_f(pidfile)

def process_started(pidfile, path,
                    tag = None, log = None,
                    verification_f = None,
                    verification_f_args = None,
                    timeout = 5, poll_period = 0.3):
    if log == None:
        log = logging
    if tag == None:
        tag = path
    t0 = time.time()		# Verify it came up
    while True:
        t = time.time()
        if t - t0 > timeout:
            log.error("%s: timed out (%ss) starting process", tag, timeout)
            return None
        time.sleep(poll_period)		# Give it .1s to come up
        pid = process_alive(pidfile, path)
        if pid == None:
            log.debug("%s: no PID yet (+%.2f/%ss), re-checking", tag,
                      t - t0, timeout)
            continue
        # PID found, if there is a verification function, let's run it
        break
    if verification_f:
        log.debug("%s: pid %d found at +%.2f/%ss), verifying",
                  tag, pid, t - t0, timeout)
        while True:
            if t - t0 > timeout:
                log.error("%s: timed out (%ss) verifying process pid %d",
                          tag, timeout, pid)
                return None
            if verification_f(*verification_f_args):
                log.debug("%s: started (pid %d) and verified at +%.2f/%ss",
                          tag, pid, t - t0, timeout)
                return pid
            time.sleep(poll_period)		# Give it .1s to come up
            t = time.time()
    else:
        log.debug("%s: started (pid %d) at +%.2f/%ss)",
                  tag, pid, t - t0, timeout)
        return pid

def origin_get(depth = 1):
    """
    Return the name of the file and line from which this was called
    """
    o = inspect.stack()[depth]
    return "%s:%s" % (o[1], o[2])

def origin_fn_get(depth = 1, sep = ":"):
    """
    Return the name of the function and line from which this was called
    """
    frame = inspect.stack()[depth][0]
    return frame.f_code.co_name + sep + "%d" % frame.f_lineno

def kws_update_type_string(kws, rt, kws_origin = None, origin = None,
                           prefix = ""):
    # FIXME: rename this to _scalar
    # FIXME: make this replace subfields as .
    #        ['bsps']['x86']['zephyr_board'] = 'arduino_101' becomes
    #        'bsps.x86.zephyr_board' = 'arduino_101'
    """
    Given a dictionary, update the second only using those keys with
    string values

    :param dict kws: destination dictionary
    :param dict d: source dictionary
    """
    assert isinstance(kws, dict)
    if not isinstance(rt, dict):
        # FIXME: this comes from the remote server...
        return
    for key, value in rt.iteritems():
        if value == None:
            kws[prefix + key] = ""
            if kws_origin and origin:
                kws_origin[prefix + key] = origin
        elif isinstance(value, basestring) \
           or isinstance(value, numbers.Integral):
            kws[prefix + key] = value
            if kws_origin and origin:
                kws_origin[prefix + key] = origin
        elif isinstance(value, bool):
            kws[prefix + key] = value

def _kws_update(kws, rt, kws_origin = None, origin = None,
                prefix = ""):
    """
    Given a dictionary, update the second only using those keys from
    the first string values

    :param dict kws: destination dictionary
    :param dict d: source dictionary
    """
    assert isinstance(kws, dict)
    if not isinstance(rt, dict):
        return
    for key, value in rt.iteritems():
        if value == None:
            kws[prefix + key] = ""
            if kws_origin and origin:
                kws_origin[prefix + key] = origin
        else:
            kws[prefix + key] = value
            if kws_origin and origin:
                kws_origin[prefix + key] = origin

def kws_update_from_rt(kws, rt, kws_origin = None, origin = None,
                       prefix = ""):
    """
    Given a target's tags, update the keywords valid for exporting and
    evaluation

    This means filtering out things that are not strings and maybe
    others, decided in a case by case basis.

    We make sure we fix the type and 'target' as the fullid.
    """
    # WARNING!!! This is used by both the client and server code
    assert isinstance(kws, dict)
    assert isinstance(rt, dict)
    if origin == None and 'url' in rt:
        origin = rt['url']
    if origin == None:
        origin = origin_get(2)
    else:
        assert isinstance(origin, basestring)

    _kws_update(kws, rt, kws_origin = kws_origin,
                origin = origin, prefix = prefix)
    if 'fullid' in rt:
        # Clients have full id in the target tags (as it includes the
        # server AKA')
        kws[prefix + 'target'] = file_name_make_safe(rt['fullid'])
    else:
        # Said concept does not exist in the server...
        kws[prefix + 'target'] = file_name_make_safe(rt['id'])
    kws[prefix + 'type'] = rt.get('type', 'n/a')
    if kws_origin:
        assert isinstance(kws_origin, dict)
        kws_origin[prefix + 'target'] = origin
        kws_origin[prefix + 'type'] = origin
    # Interconnects need to be exported manually
    kws['interconnects'] = {}
    if 'interconnects' in rt:
        _kws_update(kws['interconnects'], rt['interconnects'],
                    kws_origin = kws_origin,
                    origin = origin, prefix = prefix)


def if_present(ifname):
    """
    Return if network interface *ifname* is present in the system

    :param str ifname: name of the network interface to remove
    :returns: True if interface exists, False otherwise
    """
    return os.path.exists("/sys/class/net/" + ifname)

def if_index(ifname):
    """
    Return the interface index for *ifname* is present in the system

    :param str ifname: name of the network interface
    :returns: index of the interface, or None if not present
    """
    try:
        with open("/sys/class/net/" + ifname + "/ifindex") as f:
            index = f.read().strip()
            return int(index)
    except IOError:
        raise IndexError("%s: network interface does not exist" % ifname)

def if_find_by_mac(mac, physical = True):
    """
    Return the name of the physical network interface whose MAC
    address matches *mac*.

    Note the comparison is made at the string level, case
    insensitive.

    :param str mac: MAC address of the network interface to find
    :param bool physical: True if only look for physical devices (eg:
      not vlans); this means there a *device* symlink in
      */sys/class/net/DEVICE/*
    :returns: Name of the interface if it exists, None otherwise
    """
    assert isinstance(mac, basestring)
    for path in glob.glob("/sys/class/net/*/address"):
        if physical and not os.path.exists(os.path.dirname(path) + "/device"):
            continue
        with open(path) as f:
            path_mac = f.read().strip()
            if path_mac.lower() == mac.lower():
                return os.path.basename(os.path.dirname(path))
    return None

def if_remove(ifname):
    """
    Remove from the system a network interface using
    *ip link del*.

    :param str ifname: name of the network interface to remove
    :returns: nothing
    """
    subprocess.check_call("ip link del " + ifname, shell = True)

def if_remove_maybe(ifname):
    """
    Remove from the system a network interface (if it exists) using
    *ip link del*.

    :param str ifname: name of the network interface to remove
    :returns: nothing
    """
    if if_present(ifname):
        if_remove(ifname)

def ps_children_list(pid):
    """
    List all the PIDs that are children of a give process

    :param int pid: PID whose children we are looking for
    :return: set of PIDs children of *PID* (if any)
    """
    cl = set()
    try:
        for task_s in os.listdir("/proc/%d/task/" % pid):
            task = int(task_s)
            with open("/proc/%d/task/%d/children" % (pid, task)) as childrenf:
                children = childrenf.read()
                for child in children.split():
                    if child != pid:
                        cl.add(int(child))
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

    f = set()
    for child_pid in cl:
        f.update(ps_children_list(child_pid))
    f.update(cl)
    return f

def ps_zombies_list(pids):
    """
    Given a list of PIDs, return which are zombies

    :param pids: iterable list of numeric PIDs
    :return: set of PIDs which are zombies
    """
    zombies = set()
    for pid in pids:
        try:
            with open("/proc/%d/stat" % pid) as statf:
                stat = statf.read()
                if ") Z " in stat:
                    zombies.add(pid)
        except IOError as e:
            if e.errno != errno.ENOENT:
                raise
            # If the PID doesn't exist, ignore it
    return zombies

def version_get(module, name):
    try:
        # Try version module created during installation by
        # {,ttbd}/setup.py into {ttbd,tcfl}/version.py.
        #
        # We use two different version modules to catch be able to
        # catch mismatched installations
        importlib.import_module(module.__name__ + ".version")
        return module.version.version_string
    except ImportError as _e:
        pass
    # Nay? Maybe a git tree because we are running from the source
    # tree during development work?
    _src = os.path.abspath(module.__file__)
    _srcdir = os.path.dirname(_src)
    try:
        git_version = subprocess.check_output(
            "git describe --tags --always --abbrev=7 --dirty".split(),
            cwd = _srcdir, stderr = subprocess.STDOUT)
        return git_version.strip()
    except subprocess.CalledProcessError as _e:
        # At this point, logging is still not initialized
        raise RuntimeError("Unable to determine %s (%s) version: %s"
                           % (name, _srcdir, _e.output))

def tcp_port_busy(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.close()
        del s
        return False
    except socket.error as e:
        if e.errno == errno.EADDRINUSE:
            return True
        raise

# FIXME: this thing sucks, it is obviously racy, but I can't figure
# out a better way -- we can't bind to (0) because we have plenty of
# daemons that need to get assigned more than one port and then it is
# impossible to get from them where did they bind (assuming they can
# do it)
def tcp_port_assigner(ports = 1, port_range = (1025, 65530)):
    assert isinstance(port_range, tuple) and len(port_range) == 2 \
        and port_range[0] > 0 and port_range[1] < 65536 \
        and port_range[0] + 10 < port_range[1], \
        "port range has to be (A, B) with A > 0 and B < 65536, A << B; " \
        "got " + str(port_range)
    max_tries = 1000
    while max_tries > 0:
        port_base = random.randrange(port_range[0], port_range[1])
        for port_cnt in range(ports):
            if tcp_port_busy(port_base + port_cnt):
                continue
            else:
                return port_base
        max_tries -= 1
    raise RuntimeError("Cannot assign %d ports" % ports)

def tcp_port_connectable(hostname, port):
    """
    Return true if we can connect to a TCP port
    """
    try:
        with contextlib.closing(socket.socket(socket.AF_INET,
                                              socket.SOCK_STREAM)) as sk:
            sk.settimeout(5)
            sk.connect((hostname, port))
            return True
    except socket.error:
        return False

def conditional_eval(tag, kw, conditional, origin,
                     kind = "conditional"):
    """
    Evaluate an action's conditional string to determine if it
    should be considered or not.

    :returns bool: True if the action must be considered, False
      otherwise.
    """
    if conditional == None:
        return True
    try:
        return expr_parser.parse(conditional, kw)
    except Exception as e:
        raise Exception("error evaluating %s %s "
                        "'%s' from '%s': %s"
                        % (tag, kind, conditional, origin, e))

def check_dir(path, what):
    if not os.path.isdir(path):
        raise RuntimeError("%s: path for %s is not a directory" % (path, what))

def check_dir_writeable(path, what):
    check_dir(path, what)
    if not os.access(path, os.W_OK):
        raise RuntimeError("%s: path for %s does not allow writes"
                           % (path, what))

def prctl_cap_get_effective():
    """
    Return an integer describing the effective capabilities of this process
    """
    # FIXME: linux only
    # CAP_NET_ADMIN is 12 (from /usr/include/linux/prctl.h
    with open("/proc/self/status") as f:
        s = f.read()
        r = re.compile(r"^CapEff:\s(?P<cap_eff>[0-9a-z]+)$", re.MULTILINE)
        m = r.search(s)
        if not m or not 'cap_eff' in m.groupdict():
            raise RuntimeError("Cannot find effective capabilities "
                               "in /proc/self/status: %s",
                               m.groupdict() if m else None)
        return int(m.groupdict()['cap_eff'], 16)


def which(cmd, mode = os.F_OK | os.X_OK, path = None):
    """Given a command, mode, and a PATH string, return the path which
    conforms to the given mode on the PATH, or None if there is no such
    file.

    `mode` defaults to os.F_OK | os.X_OK. `path` defaults to the result
    of os.environ.get("PATH"), or can be overridden with a custom search
    path.

    .. note: Lifted from Python 3.6
    """
    # Check that a given file can be accessed with the correct mode.
    # Additionally check that `file` is not a directory, as on Windows
    # directories pass the os.access check.
    def _access_check(fn, mode):
        return (os.path.exists(fn) and os.access(fn, mode)
                and not os.path.isdir(fn))

    # If we're given a path with a directory part, look it up directly
    # rather than referring to PATH directories. This includes
    # checking relative to the current directory, e.g. ./script
    if os.path.dirname(cmd):
        if _access_check(cmd, mode):
            return cmd
        return None

    if path is None:
        path = os.environ.get("PATH", os.defpath)
    if not path:
        return None
    path = path.split(os.pathsep)

    # On other platforms you don't have things like PATHEXT to tell you
    # what file suffixes are executable, so just pass on cmd as-is.
    files = [cmd]

    seen = set()
    for _dir in path:
        normdir = os.path.normcase(_dir)
        if not normdir in seen:
            seen.add(normdir)
            for thefile in files:
                name = os.path.join(_dir, thefile)
                if _access_check(name, mode):
                    return name
    return None

def ttbd_locate_helper(filename, log = logging, relsrcpath = ""):
    """
    Find the path to a TTBD file, depending on we running from source
    or installed system wide.

    :param str filename: name of the TTBD file we are looking for.
    :param str relsrcpath: path relative to the running binary in the source
    """
    # Simics needs an image with a bootloader, we use grub2 and we
    # share the setup-efi-grub2-elf.sh implementation from grub2elf.
    _src = os.path.abspath(sys.argv[0])
    _srcdir = os.path.dirname(_src)
    # Running from source tree
    cmd_path = os.path.join(_srcdir, relsrcpath, filename)
    if os.path.exists(cmd_path):
        return cmd_path
    # System-wide install in the same prefix -> ../share/tcf
    cmd_path = os.path.join(_srcdir, "..", "share", "tcf", filename)
    log.debug("looking for %s" % cmd_path)
    if os.path.exists(cmd_path):
        return cmd_path
    raise RuntimeError("Can't find util %s" % filename)

def raise_from(what, cause):
    """
    Forward compath to Python 3's raise X from Y
    """
    setattr(what, "__cause__", cause)
    raise what

#: Regex to filter out ANSI characters from text, to ease up debug printing
#:
#: Use as:
#:
#: >>> data = commonl.ansi_regex.sub('', source_data)
#:
# FIXME: this is deleting more stuff than it should
ansi_regex = re.compile(r'\x1b(\[[0-9]*J|\[[0-9;]*H|\[[0-9=]*h|\[[0-9]*m|\[B)')


class dict_missing_c(dict):
    """
    A dictionary that returns as a value a string KEY_UNDEFINED_SYMBOL
    if KEY is not in the dictionary.

    This is useful for things like

    >>> "%(idonthavethis)" % dict_missing_c({"ihavethis": True"}

    to print "idonthavethis_UNDEFINED_SYMBOL" intead of raising KeyError
    """
    def __init__(self, d, missing = None):
        assert isinstance(d, dict)
        assert missing == None or isinstance(missing, basestring)
        dict.__init__(self, d)
        self.missing = missing

    def __getitem__(self, key):
        if self.__contains__(key):
            return dict.__getitem__(self, key)
        if self.missing:
            return self.missing
        return "%s_UNDEFINED_SYMBOL.%s" % (key, origin_fn_get(2, "."))

def ipv4_len_to_netmask_ascii(length):
    return socket.inet_ntoa(struct.pack('>I', 0xffffffff ^ ((1 << (32 - length) ) - 1)))

def password_get(domain, user, password):
    """
    Get the password for a domain and user

    This returns a password obtained from a configuration file, maybe
    accessing secure password storage services to get the real
    password. This is intended to be use as a service to translate
    passwords specified in config files, which in some time might be
    cleartext, in others obtained from services.

    >>> real_password = password_get("somearea", "rtmorris", "KEYRING")

    will query the *keyring* service for the password to use for user
    *rtmorris* on domain *somearea*.

    >>> real_password = password_get("somearea", "rtmorris", "KEYRING:Area51")

    would do the same, but *keyring*'s domain would be *Area51*
    instead.

    >>> real_password = password_get(None, "rtmorris",
    >>>                              "FILE:/etc/config/some.key")

    would obtain the password from the contents of file
    */etc/config/some.key*.

    >>> real_password = password_get("somearea", "rtmorris", "sikrit")

    would just return *sikrit* as a password.

    :param str domain: a domain to which this password operation
      applies; see below *password* (can be *None*)

    :param str user: the username for maybe obtaining a password from
      a password service; see below *password*.

    :param str password: a password obtained from the user or a
      configuration setting; can be *None*. If the *password* is

      - *KEYRING* will ask the accounts keyring for the password
         for domain *domain* for username *user*

      - *KEYRING:DOMAIN* will ask the accounts keyring for the password
         for domain *DOMAIN* for username *user*, ignoring the
         *domain* parameter.

      - *FILE:PATH* will read the password from filename *PATH*.

    :returns: the actual password to use

    Password management procedures (FIXME):

    - to set a password in the keyring::

        $ echo KEYRINGPASSWORD | gnome-keyring-daemon --unlock
        $ keyring set "USER"  DOMAIN
        Password for 'DOMAIN' in 'USER': <ENTER PASSWORD HERE>

    - to be able to run the daemon has to be executed under a dbus session::

        $ dbus-session -- sh
        $ echo KEYRINGPASSWORD | gnome-keyring-daemon --unlock
        $ ttbd...etc

    """
    assert domain == None or isinstance(domain, basestring)
    assert isinstance(user, basestring)
    assert password == None or isinstance(password, basestring)
    if password == "KEYRING":
        password = keyring.get_password(domain, user)
        if password == None:
            raise RuntimeError("keyring: no password for user %s @ %s"
                               % (user, domain))
    elif password and password.startswith("KEYRING:"):
        _, domain = password.split(":", 1)
        password = keyring.get_password(domain, user)
        if password == None:
            raise RuntimeError("keyring: no password for user %s @ %s"
                               % (user, domain))
    elif password and password.startswith("FILE:"):
        _, filename = password.split(":", 1)
        with open(filename) as f:
            password = f.read().strip()
    # fallthrough, if none of them, it's just a password
    return password


def split_user_pwd_hostname(s):
    """
    Return a tuple decomponsing ``[USER[:PASSWORD]@HOSTNAME``

    :returns: tuple *( USER, PASSWORD, HOSTNAME )*, *None* in missing fields.

    See :func:`password_get` for details on how the password is handled.
    """
    assert isinstance(s, basestring)
    user = None
    password = None
    hostname = None
    if '@' in s:
        user_password, hostname = s.split('@', 1)
    else:
        user_password = ""
        hostname = s
    if ':' in user_password:
        user, password = user_password.split(':', 1)
    else:
        user = user_password
        password = None
    password = password_get(hostname, user, password)
    return user, password, hostname


def url_remove_user_pwd(url):
    """
    Given a URL, remove the username and password if any::

      print(url_remove_user_pwd("https://user:password@host:port/path"))
      https://host:port/path
    """
    _url = url.scheme + "://" + url.hostname
    if url.port:
        _url += ":%d" % url.port
    if url.path:
        _url += url.path
    return _url


def field_needed(field, projections):
    """
    Check if the name *field* matches any of the *patterns* (ala
    :mod:`fnmatch`).

    :param str field: field name
    :param list(str) projections: list of :mod:`fnmatch` patterns
      against which to check field. Can be *None* and *[ ]* (empty).

    :returns bool: *True* if *field* matches a pattern in *patterns*
      or if *patterns* is empty or *None*. *False* otherwise.
    """
    if projections:
        # there is a list of must haves, check here first
        for projection in projections:
            if fnmatch.fnmatch(field, projection):
                return True	# we need this field
        return False		# we do not need this field
    else:
        return True	# no list, have it

def dict_to_flat(d, projections = None):
    """
    Convert a nested dictionary to a sorted list of tuples *( KEY, VALUE )*

    The KEY is like *KEY[.SUBKEY[.SUBSUBKEY[....]]]*, where *SUBKEY*
    are keys in nested dictionaries.

    :param dict d: dictionary to convert
    :param list(str) projections: (optional) list of :mod:`fnmatch`
      patterns of flay keys to bring in (default: all)
    :returns list: sorted list of tuples *KEY, VAL*

    """

    fl = []

    def __update_recursive(val, field, field_flat, projections = None,
                           depth_limit = 10, prefix = "  "):
        # Merge d into dictionary od with a twist
        #
        # projections is a list of fields to include, if empty, means all
        # of them
        # a field X.Y.Z means od['X']['Y']['Z']

        # GRRRR< has to dig deep first, so that a.a3.* goes all the way
        # deep before evaluating if keepers or not -- I think we need to
        # change it like that and maybe the evaluation can be done before
        # the assignment.

        if field_needed(field_flat, projections):
            bisect.insort(fl, ( field_flat, val ))
        elif isinstance(val, dict) and depth_limit > 0:	# dict to dig in
            for key, value in val.iteritems():
                __update_recursive(value, key, field_flat + "." + str(key),
                                   projections, depth_limit - 1,
                                   prefix = prefix + "    ")

    for key, _val in d.iteritems():
        __update_recursive(d[key], key, key, projections, 10)

    return fl

def _key_rep(r, key, key_flat, val):
    # put val in r[key] if key is already fully expanded (it has no
    # periods); otherwise expand it recursively
    if '.' in key:
        # this key has sublevels, iterate over them
        lhs, rhs = key.split('.', 1)
        if lhs not in r:
            r[lhs] = {}
        elif not isinstance(r[lhs], dict):
            r[lhs] = {}

        _key_rep(r[lhs], rhs, key_flat, val)
    else:
        r[key] = val

def flat_slist_to_dict(fl):
    """
    Given a sorted list of flat keys and values, convert them to a
    nested dictionary

    :param list((str,object)): list of tuples of key and any value
      alphabetically sorted by tuple; same sorting rules as in
      :func:`flat_keys_to_dict`.

    :return dict: nested dictionary as described by the flat space of
      keys and values
    """
    tr = {}
    for key, val in fl:
        _key_rep(tr, key, key, val)
    return tr


def flat_keys_to_dict(d):
    """
    Given a dictionary of flat keys, convert it to a nested dictionary

    Similar to :func:`flat_slist_to_dict`, differing in the
    keys/values being in a dictionary.

    A key/value:

    >>> d["a.b.c"] = 34

    means:

    >>> d['a']['b']['c'] = 34

    Key in the input dictonary are processed in alphabetical order
    (thus, key a.a is processed before a.b.c); later keys override
    earlier keys:

    >>> d['a.a'] = 'aa'
    >>> d['a.a.a'] = 'aaa'
    >>> d['a.a.b'] = 'aab'

    will result in:

    >>> d['a']['a'] = { 'a': 'aaa', 'b': 'aab' }

    The

    >>> d['a.a'] = 'aa'

    gets overriden by the other settings

    :param dict d: dictionary of keys/values
    :returns dict: (nested) dictionary
    """
    tr = {}

    for key in sorted(d.keys()):
        _key_rep(tr, key, key, d[key])

    return tr


class tls_prefix_c(object):

    def __init__(self, tls, prefix):
        assert isinstance(prefix, basestring)
        self.tls = tls
        self.prefix = unicode(prefix)
        self.prefix_old = None

    def __enter__(self):
        self.prefix_old = getattr(self.tls, "prefix_c", u"")
        self.tls.prefix_c = self.prefix_old + self.prefix
        return self

    def __exit__(self, _exct_type, _exce_value, _traceback):
        self.tls.prefix_c = self.prefix_old
        self.prefix_old = None

    def __repr__(self):
        return getattr(self.tls, "prefix_c", None)


def data_dump_recursive(d, prefix = u"", separator = u".", of = sys.stdout,
                        depth_limit = 10):
    """
    Dump a general data tree to stdout in a recursive way

    For example:

    >>> data = [ dict(keya = 1, keyb = 2), [ "one", "two", "three" ], "hello", sys.stdout ]

    produces the stdout::

      [0].keya: 1
      [0].keyb: 2
      [1][0]: one
      [1][1]: two
      [1][2]: three
      [2]: hello
      [3]: <open file '<stdout>', mode 'w' at 0x7f13ba2861e0>

    - in a list/set/tuple, each item is printed prefixing *[INDEX]*
    - in a dictionary, each item is prefixed with it's key
    - strings and cardinals are printed as such
    - others are printed as what their representation as a string produces
    - if an attachment is a generator, it is iterated to gather the data.
    - if an attachment is of :class:generator_factory_c, the method
      for creating the generator is called and then the generator
      iterated to gather the data.

    See also :func:`data_dump_recursive_tls`

    :param d: data to print
    :param str prefix: prefix to start with (defaults to nothing)
    :param str separator: used to separate dictionary keys from the
      prefix (defaults to ".")
    :param FILE of: output stream where to print (defaults to
      *sys.stdout*)
    :param int depth_limit: maximum nesting levels to go deep in the
      data structure (defaults to 10)
    """
    assert isinstance(prefix, basestring)
    assert isinstance(separator, basestring)
    assert depth_limit > 0

    if isinstance(d, dict) and depth_limit > 0:
        if prefix.strip() != "":
            prefix = prefix + separator
        for key, val in sorted(d.items(), key = lambda i: i[0]):
            data_dump_recursive(val, prefix + str(key),
                                separator = separator, of = of,
                                depth_limit = depth_limit - 1)
    elif isinstance(d, (list, set, tuple)) and depth_limit > 0:
        # could use iter(x), but don't wanna catch strings, etc
        count = 0
        for v in d:
            data_dump_recursive(v, prefix + u"[%d]" % count,
                                separator = separator, of = of,
                                depth_limit = depth_limit - 1)
            count += 1
    # HACK: until we move functions to a helper or something, when
    # someone calls the generatory factory as
    # commonl.generator_factory_c, this can't pick it up, so fallback
    # to use the name
    elif isinstance(d, generator_factory_c) \
         or type(d).__name__ == "generator_factory_c":
        of.write(prefix)
        of.writelines(d.make_generator())
    elif isinstance(d, types.GeneratorType):
        of.write(prefix)
        of.writelines(d)
    elif isinstance(d, file):
        # not recommended, prefer generator_factory_c so it reopens the file
        d.seek(0, 0)
        of.write(prefix)
        of.writelines(d)
    else:
        of.write(prefix + u": " + mkutf8(d) + u"\n")


_dict_print_dotted = data_dump_recursive	# COMPAT

def data_dump_recursive_tls(d, tls, separator = u".", of = sys.stdout,
                            depth_limit = 10):
    """
    Dump a general data tree to stdout in a recursive way

    This function works as :func:`data_dump_recursive` (see for more
    information on the usage and arguments). However, it uses TLS for
    storing the prefix as it digs deep into the data structure.

    A variable called *prefix_c* is created in the TLS structure on
    which the current prefix is stored; this is meant to be used in
    conjunction with stream writes such as
    :class:`io_tls_prefix_lines_c`.

    Parameters are as documented in :func:`data_dump_recursive`,
    except for:

    :param thread._local tls: thread local storage to use (as returned
      by *threading.local()*
    """
    assert isinstance(separator, basestring)
    assert depth_limit > 0

    if isinstance(d, dict):
        for key, val in sorted(d.items(), key = lambda i: i[0]):
            with tls_prefix_c(tls, str(key) + ": "):
                data_dump_recursive_tls(val, tls,
                                        separator = separator, of = of,
                                        depth_limit = depth_limit - 1)
    elif isinstance(d, (list, set, tuple)):
        # could use iter(x), but don't wanna catch strings, etc
        count = 0
        for v in d:
            with tls_prefix_c(tls, u"[%d]: " % count):
                data_dump_recursive_tls(v, tls,
                                        separator = separator, of = of,
                                        depth_limit = depth_limit - 1)
            count += 1
    # HACK: until we move functions to a helper or something, when
    # someone calls the generatory factory as
    # commonl.generator_factory_c, this can't pick it up, so fallback
    # to use the name
    elif isinstance(d, generator_factory_c) \
         or type(d).__name__ == "generator_factory_c":
        of.writelines(d.make_generator())
    elif isinstance(d, file):
        # not recommended, prefer generator_factory_c so it reopens the file
        d.seek(0, 0)
        of.writelines(d)
    elif isinstance(d, types.GeneratorType):
        of.writelines(d)
    else:
        of.write(mkutf8(d) + u"\n")


class io_tls_prefix_lines_c(io.TextIOWrapper):
    """
    Write lines to a stream with a prefix obtained from a thread local
    storage variable.

    This is a limited hack to transform a string written as::

      line1
      line2
      line3

    into::

      PREFIXline1
      PREFIXline2
      PREFIXline3

    without any intervention by the caller other than setting the
    prefix in thread local storage and writing to the stream; this
    allows other clients to write to the stream without needing to
    know about the prefixing.

    Note the lines yielded are unicode-escaped or UTF-8 escaped, for
    being able to see in reports any special character.

    Usage:

    .. code-block:: python

       import io
       import commonl
       import threading
   
       tls = threading.local()
   
       f = io.open("/dev/stdout", "w")
       with commonl.tls_prefix_c(tls, "PREFIX"), \
            commonl.io_tls_prefix_lines_c(tls, f.detach()) as of:
   
           of.write(u"line1\nline2\nline3\n")

    Limitations:

    - hack, only works ok if full lines are being printed; eg:
    """
    def __init__(self, tls, *args, **kwargs):
        assert isinstance(tls, thread._local)
        io.TextIOWrapper.__init__(self, *args, **kwargs)
        self.tls = tls
        self.data = u""

    def __write_line(self, s, prefix, offset, pos):
        # Write a whole (\n ended) line to the stream
        #
        # - prefix first
        # - leftover data since last \n
        # - current data from offset to the position where \n was
        #   (unicode-escape encoded)
        # - newline (since the one in s was unicode-escaped)
        substr = s[offset:pos]
        io.TextIOWrapper.write(self, prefix)
        if self.data:
            io.TextIOWrapper.write(
                self, unicode(self.data.encode('unicode-escape')))
            self.data = u""
        io.TextIOWrapper.write(self, unicode(substr.encode('unicode-escape')))
        io.TextIOWrapper.write(self, u"\n")
        # flush after writing one line to avoid corruption from other
        # threads/processes printing to the same FD
        io.TextIOWrapper.flush(self)
        return pos + 1

    def _write(self, s, prefix, acc_offset = 0):
        # write a chunk of data to the stream -- break it by newlines,
        # so when one is found __write_line() can write the prefix
        # first. Accumulate anything left over after the last newline
        # so we can flush it next time we find one.
        offset = 0
        while offset < len(s):
            pos = s.find('\n', offset)
            if pos >= 0:
                offset = self.__write_line(s, prefix, offset, pos)
                continue
            self.data += s[offset:]
            break
        return acc_offset + len(s)

    def flush(self):
        """
        Flush any leftover data in the temporary buffer, write it to the
        stream, prefixing each line with the prefix obtained from
        *self.tls*\'s *prefix_c* attribute.
        """
        prefix = getattr(self.tls, "prefix_c", None)
        if prefix == None:
            io.TextIOWrapper.write(
                self, unicode(self.data.encode('unicode-escape')))
        else:
            # flush whatever is accumulated
            self._write(u"", prefix)
        io.TextIOWrapper.flush(self)

    def write(self, s):
        """
        Write string to the stream, prefixing each line with the
        prefix obtained from *self.tls*\'s *prefix_c* attribute.
        """
        prefix = getattr(self.tls, "prefix_c", None)
        if prefix == None:
            io.TextIOWrapper.write(self, s)
            return
        self._write(s, prefix, 0)

    def writelines(self, itr):
        """
        Write the iterator to the stream, prefixing each line with the
        prefix obtained from *self.tls*\'s *prefix_c* attribute.
        """
        prefix = getattr(self.tls, "prefix_c", None)
        if prefix == None:
            io.TextIOWrapper.writelines(self, itr)
            return
        offset = 0
        for data in itr:
            offset = self._write(data, prefix, offset)

def mkutf8(s):
    #
    # We need a generic 'just write this and heck with encodings', but
    # we don't know how the data is coming to us.
    #
    # If already unicode, pass it through; if str, assume is UTF-8 and
    # try to safely decode it it to UTF-8. If anything else, rep() it into unicode.
    #
    # I am still so confused by Python's string / unicode / encoding /
    # decoding rules
    #
    if isinstance(s, unicode):
        return s
    elif isinstance(s, str):
        return s.decode('utf-8', errors = 'replace')
    else:
        # represent it in unicode, however the object says
        return unicode(s)

class generator_factory_c(object):
    """
    Create generator objects multiple times

    Given a generator function and its arguments, create it when
    :func:`make_generator` is called.

    >>> factory = generator_factory_c(genrator, arg1, arg2..., arg = value...)
    >>> ...
    >>> generator = factory.make_generator()
    >>> for data in generator:
    >>>     do_something(data)
    >>> ...
    >>> another_generator = factory.make_generator()
    >>> for data in another_generator:
    >>>     do_something(data)

    generators once created cannot be reset to the beginning, so this
    can be used to simulate that behavior.

    :param fn: generator function
    :param args: arguments to the generator function
    :param kwargs: keyword arguments to the generator function
    """
    def __init__(self, fn, *args, **kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def make_generator(self):
        """
        Create and return a generator
        """
        return self.fn(*self.args, **self.kwargs)

def file_iterator(filename, chunk_size = 4096):
    """
    Iterate over a file's contents

    Commonly used along with generator_factory_c to with the TCF
    client API to report attachments:

    :param int chunk_size: (optional) read blocks of this size (optional)

    >>> import commonl
    >>>
    >>> class _test(tcfl.tc.tc_c):
    >>>
    >>>   def eval(self):
    >>>     generator_f = commonl.generator_factory_c(commonl.file_iterator, FILENAME)
    >>>     testcase.report_pass("some message", dict(content = generator_f))

    """
    assert chunk_size > 0
    with io.open(filename, "rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            yield data
