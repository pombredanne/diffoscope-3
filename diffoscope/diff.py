# -*- coding: utf-8 -*-
#
# diffoscope: in-depth comparison of files, archives, and directories
#
# Copyright © 2014-2015 Jérémy Bobbio <lunar@debian.org>
#
# diffoscope is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# diffoscope is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with diffoscope.  If not, see <https://www.gnu.org/licenses/>.

import re
import io
import hashlib
import logging
import threading
import contextlib
import subprocess
import tempfile

from multiprocessing.dummy import Queue

from .tools import tool_required
from .config import Config

DIFF_CHUNK = 4096

logger = logging.getLogger(__name__)
re_diff_change = re.compile(r'^([+-@]).*', re.MULTILINE)


class DiffParser(object):
    RANGE_RE = re.compile(
        r'^@@\s+-(?P<start1>\d+)(,(?P<len1>\d+))?\s+\+(?P<start2>\d+)(,(?P<len2>\d+))?\s+@@$',
    )

    def __init__(self, output, end_nl_q1, end_nl_q2):
        self._output = output
        self._end_nl_q1 = end_nl_q1
        self._end_nl_q2 = end_nl_q2
        self._action = self.read_headers
        self._diff = io.StringIO()
        self._success = False
        self._remaining_hunk_lines = None
        self._block_len = None
        self._direction = None
        self._end_nl = None
        self._max_lines = Config().max_diff_block_lines_saved

    @property
    def diff(self):
        return self._diff.getvalue()

    @property
    def success(self):
        return self._success

    def parse(self):
        for line in self._output:
            self._action = self._action(line.decode('utf-8', errors='replace'))

        self._action('')
        self._success = True
        self._output.close()

    def read_headers(self, line):
        if not line:
            return None

        if line.startswith('---'):
            return self.read_headers

        if line.startswith('+++'):
            return self.read_headers

        found = DiffParser.RANGE_RE.match(line)

        if not found:
            raise ValueError('Unable to parse diff headers: %s' % repr(line))

        self._diff.write(line)
        if found.group('len1'):
            self._remaining_hunk_lines = int(found.group('len1'))
        else:
            self._remaining_hunk_lines = 1
        if found.group('len2'):
            self._remaining_hunk_lines += int(found.group('len2'))
        else:
            self._remaining_hunk_lines += 1

        self._direction = None

        return self.read_hunk

    def read_hunk(self, line):
        if not line:
            return None

        if line[0] == ' ':
            self._remaining_hunk_lines -= 2
        elif line[0] == '+':
            self._remaining_hunk_lines -= 1
        elif line[0] == '-':
            self._remaining_hunk_lines -= 1
        elif line[0] == '\\':
            # When both files don't end with \n, do not show it as a difference
            if self._end_nl is None:
                end_nl1 = self._end_nl_q1.get()
                end_nl2 = self._end_nl_q2.get()
                self._end_nl = end_nl1 and end_nl2
            if not self._end_nl:
                return self.read_hunk
        elif self._remaining_hunk_lines == 0:
            return self.read_headers(line)
        else:
            raise ValueError('Unable to parse diff hunk: %s' % repr(line))

        self._diff.write(line)

        if line[0] in ('-', '+'):
            if line[0] == self._direction:
                self._block_len += 1
            else:
                self._block_len = 1
                self._direction = line[0]

            if self._block_len >= self._max_lines:
                return self.skip_block
        else:
            self._block_len = 1
            self._direction = line[0]

        return self.read_hunk

    def skip_block(self, line):
        if self._remaining_hunk_lines == 0 or line[0] != self._direction:
            removed = self._block_len - Config().max_diff_block_lines_saved
            if removed:
                self._diff.write('%s[ %d lines removed ]\n' % (self._direction, removed))
            return self.read_hunk(line)

        self._block_len += 1
        self._remaining_hunk_lines -= 1

        return self.skip_block

@tool_required('diff')
def run_diff(fifo1, fifo2, end_nl_q1, end_nl_q2):
    cmd = ['diff', '-aU7', fifo1, fifo2]

    logger.debug("Running %s", ' '.join(cmd))

    p = subprocess.Popen(
        cmd,
        shell=False,
        bufsize=1,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    p.stdin.close()

    parser = DiffParser(p.stdout, end_nl_q1, end_nl_q2)
    t_read = threading.Thread(target=parser.parse)
    t_read.daemon = True
    t_read.start()
    t_read.join()
    p.wait()

    logger.debug(
        "%s: returncode %d, parsed %s",
        ' '.join(cmd),
        p.returncode,
        parser.success,
    )

    if not parser.success and p.returncode not in (0, 1):
        raise subprocess.CalledProcessError(p.returncode, cmd, output=diff)

    if p.returncode == 0:
        return None

    return parser.diff

def feed(feeder, f, end_nl_q):
    # work-around unified diff limitation: if there's no newlines in both
    # don't make it a difference
    try:
        end_nl = feeder(f)
        end_nl_q.put(end_nl)
    finally:
        f.close()

class ExThread(threading.Thread):
    """
    Inspired by https://stackoverflow.com/a/6874161
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__status_queue = Queue()

    def run(self, *args, **kwargs):
        try:
            super().run(*args, **kwargs)
        except Exception as ex:
            #except_type, except_class, tb = sys.exc_info()
            self.__status_queue.put(ex)

        self.__status_queue.put(None)

    def wait_for_exc_info(self):
        return self.__status_queue.get()

    def join(self):
        ex = self.wait_for_exc_info()
        if ex is None:
            return
        raise ex

@contextlib.contextmanager
def fd_from_feeder(feeder, end_nl_q, fifo):
    f = open(fifo, 'wb')
    t = ExThread(target=feed, args=(feeder, f, end_nl_q))

    t.daemon = True
    t.start()

    try:
        t.join()
    finally:
        f.close()

def empty_file_feeder():
    def feeder(f):
        return False
    return feeder

def make_feeder_from_raw_reader(in_file, filter=lambda buf: buf):
    def feeder(out_file):
        h = None
        end_nl = False
        max_lines = Config().max_diff_input_lines
        line_count = 0

        if max_lines < float("inf"):
            h = hashlib.sha1()

        for buf in in_file:
            line_count += 1
            out = filter(buf)
            if h:
                h.update(out)
            if line_count < max_lines:
                out_file.write(out)
            end_nl = buf[-1] == '\n'

        if h and line_count >= max_lines:
            out_file.write("[ Too much input for diff (SHA1: {}) ]\n".format(
                h.hexdigest(),
            ).encode('utf-8'))
            end_nl = True

        return end_nl
    return feeder

def diff(feeder1, feeder2):
    end_nl_q1 = Queue()
    end_nl_q2 = Queue()

    with tempfile.TemporaryDirectory() as tmpdir:
        fifo1 = '{}/f1'.format(tmpdir)
        fifo2 = '{}/f2'.format(tmpdir)
        fd_from_feeder(feeder1, end_nl_q1, fifo1)
        fd_from_feeder(feeder2, end_nl_q2, fifo2)
        return run_diff(fifo1, fifo2, end_nl_q1, end_nl_q2)

def reverse_unified_diff(diff):
    res = []
    for line in diff.splitlines(True): # keepends=True
        found = DiffParser.RANGE_RE.match(line)

        if found:
            before = found.group('start2')
            if found.group('len2') is not None:
                before += ',' + found.group('len2')

            after = found.group('start1')
            if found.group('len1') is not None:
                after += ',' + found.group('len1')

            res.append('@@ -%s +%s @@\n' % (before, after))
        elif line.startswith('-'):
            res.append('+')
            res.append(line[1:])
        elif line.startswith('+'):
            res.append('-')
            res.append(line[1:])
        else:
            res.append(line)
    return ''.join(res)

def color_unified_diff(diff):
    RESET = '\033[0m'
    RED, GREEN, CYAN = '\033[31m', '\033[32m', '\033[0;36m'

    def repl(m):
        return '{}{}{}'.format({
            '-': RED,
            '@': CYAN,
            '+': GREEN,
        }[m.group(1)], m.group(0), RESET)

    return re_diff_change.sub(repl, diff)
