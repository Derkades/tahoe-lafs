from __future__ import print_function

import os
from random import randrange
from six.moves import StringIO

from twisted.internet import reactor, defer
from twisted.python import failure
from twisted.trial import unittest

from ..util.assertutil import precondition
from ..scripts import runner
from allmydata.util.encodingutil import get_io_encoding
# Imported for backwards compatibility:
from future.utils import bord, bchr, binary_type
from past.builtins import unicode
from .common_py3 import (
    SignalMixin, skip_if_cannot_represent_filename, ReallyEqualMixin
)


def skip_if_cannot_represent_argv(u):
    precondition(isinstance(u, unicode))
    try:
        u.encode(get_io_encoding())
    except UnicodeEncodeError:
        raise unittest.SkipTest("A non-ASCII argv could not be encoded on this platform.")

def run_cli(verb, *args, **kwargs):
    precondition(not [True for arg in args if not isinstance(arg, str)],
                 "arguments to do_cli must be strs -- convert using unicode_to_argv", args=args)
    nodeargs = kwargs.get("nodeargs", [])
    argv = nodeargs + [verb] + list(args)
    stdin = kwargs.get("stdin", "")
    stdout = StringIO()
    stderr = StringIO()
    d = defer.succeed(argv)
    d.addCallback(runner.parse_or_exit_with_explanation, stdout=stdout)
    d.addCallback(runner.dispatch,
                  stdin=StringIO(stdin),
                  stdout=stdout, stderr=stderr)
    def _done(rc):
        return 0, stdout.getvalue(), stderr.getvalue()
    def _err(f):
        f.trap(SystemExit)
        return f.value.code, stdout.getvalue(), stderr.getvalue()
    d.addCallbacks(_done, _err)
    return d

def parse_cli(*argv):
    # This parses the CLI options (synchronously), and returns the Options
    # argument, or throws usage.UsageError if something went wrong.
    return runner.parse_options(argv)

class DevNullDictionary(dict):
    def __setitem__(self, key, value):
        return

def insecurerandstr(n):
    return b''.join(map(bchr, map(randrange, [0]*n, [256]*n)))

def flip_bit(good, which):
    # TODO Probs need to update with bchr/bord as with flip_one_bit, below.
    # flip the low-order bit of good[which]
    if which == -1:
        pieces = good[:which], good[-1:], ""
    else:
        pieces = good[:which], good[which:which+1], good[which+1:]
    return pieces[0] + chr(ord(pieces[1]) ^ 0x01) + pieces[2]

def flip_one_bit(s, offset=0, size=None):
    """ flip one random bit of the string s, in a byte greater than or equal to offset and less
    than offset+size. """
    precondition(isinstance(s, binary_type))
    if size is None:
        size=len(s)-offset
    i = randrange(offset, offset+size)
    result = s[:i] + bchr(bord(s[i])^(0x01<<randrange(0, 8))) + s[i+1:]
    assert result != s, "Internal error -- flip_one_bit() produced the same string as its input: %s == %s" % (result, s)
    return result


class StallMixin(object):
    def stall(self, res=None, delay=1):
        d = defer.Deferred()
        reactor.callLater(delay, d.callback, res)
        return d


class Marker(object):
    pass

class FakeCanary(object):
    """For use in storage tests.

    Can be moved back to test_storage.py once enough Python 3 porting has been
    done.
    """
    def __init__(self, ignore_disconnectors=False):
        self.ignore = ignore_disconnectors
        self.disconnectors = {}
    def notifyOnDisconnect(self, f, *args, **kwargs):
        if self.ignore:
            return
        m = Marker()
        self.disconnectors[m] = (f, args, kwargs)
        return m
    def dontNotifyOnDisconnect(self, marker):
        if self.ignore:
            return
        del self.disconnectors[marker]
    def getRemoteTubID(self):
        return None
    def getPeer(self):
        return "<fake>"


class ShouldFailMixin(object):

    def shouldFail(self, expected_failure, which, substring,
                   callable, *args, **kwargs):
        """Assert that a function call raises some exception. This is a
        Deferred-friendly version of TestCase.assertRaises() .

        Suppose you want to verify the following function:

         def broken(a, b, c):
             if a < 0:
                 raise TypeError('a must not be negative')
             return defer.succeed(b+c)

        You can use:
            d = self.shouldFail(TypeError, 'test name',
                                'a must not be negative',
                                broken, -4, 5, c=12)
        in your test method. The 'test name' string will be included in the
        error message, if any, because Deferred chains frequently make it
        difficult to tell which assertion was tripped.

        The substring= argument, if not None, must appear in the 'repr'
        of the message wrapped by this Failure, or the test will fail.
        """

        assert substring is None or isinstance(substring, (bytes, unicode))
        d = defer.maybeDeferred(callable, *args, **kwargs)
        def done(res):
            if isinstance(res, failure.Failure):
                res.trap(expected_failure)
                if substring:
                    self.failUnless(substring in str(res),
                                    "%s: substring '%s' not in '%s'"
                                    % (which, substring, str(res)))
                # return the Failure for further analysis, but in a form that
                # doesn't make the Deferred chain think that we failed.
                return [res]
            else:
                self.fail("%s was supposed to raise %s, not get '%s'" %
                          (which, expected_failure, res))
        d.addBoth(done)
        return d


class TestMixin(SignalMixin):
    def setUp(self):
        return super(TestMixin, self).setUp()

    def tearDown(self):
        self.clean_pending(required_to_quiesce=True)
        return super(TestMixin, self).tearDown()

    def clean_pending(self, dummy=None, required_to_quiesce=True):
        """
        This handy method cleans all pending tasks from the reactor.

        When writing a unit test, consider the following question:

            Is the code that you are testing required to release control once it
            has done its job, so that it is impossible for it to later come around
            (with a delayed reactor task) and do anything further?

        If so, then trial will usefully test that for you -- if the code under
        test leaves any pending tasks on the reactor then trial will fail it.

        On the other hand, some code is *not* required to release control -- some
        code is allowed to continuously maintain control by rescheduling reactor
        tasks in order to do ongoing work.  Trial will incorrectly require that
        code to clean up all its tasks from the reactor.

        Most people think that such code should be amended to have an optional
        "shutdown" operation that releases all control, but on the contrary it is
        good design for some code to *not* have a shutdown operation, but instead
        to have a "crash-only" design in which it recovers from crash on startup.

        If the code under test is of the "long-running" kind, which is *not*
        required to shutdown cleanly in order to pass tests, then you can simply
        call testutil.clean_pending() at the end of the unit test, and trial will
        be satisfied.
        """
        pending = reactor.getDelayedCalls()
        active = bool(pending)
        for p in pending:
            if p.active():
                p.cancel()
            else:
                print("WEIRDNESS! pending timed call not active!")
        if required_to_quiesce and active:
            self.fail("Reactor was still active when it was required to be quiescent.")


try:
    import win32file
    import win32con
    def make_readonly(path):
        win32file.SetFileAttributes(path, win32con.FILE_ATTRIBUTE_READONLY)
    def make_accessible(path):
        win32file.SetFileAttributes(path, win32con.FILE_ATTRIBUTE_NORMAL)
except ImportError:
    import stat
    def _make_readonly(path):
        os.chmod(path, stat.S_IREAD)
        os.chmod(os.path.dirname(path), stat.S_IREAD)
    def _make_accessible(path):
        os.chmod(os.path.dirname(path), stat.S_IWRITE | stat.S_IEXEC | stat.S_IREAD)
        os.chmod(path, stat.S_IWRITE | stat.S_IEXEC | stat.S_IREAD)
    make_readonly = _make_readonly
    make_accessible = _make_accessible


__all__ = [
    "make_readonly", "make_accessible", "TestMixin", "ShouldFailMixin",
    "StallMixin", "skip_if_cannot_represent_argv", "run_cli", "parse_cli",
    "DevNullDictionary", "insecurerandstr", "flip_bit", "flip_one_bit",
    "SignalMixin", "skip_if_cannot_represent_filename", "ReallyEqualMixin"
]
