# -*- coding: utf-8 -*-
#
# diffoscope: in-depth comparison of files, archives, and directories
#
# Copyright © 2015 Reiner Herrmann <reiner@reiner-h.de>
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
# along with diffoscope.  If not, see <http://www.gnu.org/licenses/>.

import re
import os.path
import subprocess
from diffoscope import logger, tool_required
from diffoscope.comparators.binary import File, needs_content
from diffoscope.comparators.utils import Archive, get_compressed_content_name
from diffoscope.difference import Difference


class DexContainer(Archive):
    @property
    def path(self):
        return self._path

    def open_archive(self, path):
        self._path = path
        return self

    def close_archive(self):
        self._path = None

    def get_members(self):
        return {'dex-content': self.get_member(self.get_member_names()[0])}

    def get_member_names(self):
        return [get_compressed_content_name(self.path, '.dex') + '.jar']

    @tool_required('enjarify')
    def extract(self, member_name, dest_dir):
        dest_path = os.path.join(dest_dir, member_name)
        logger.debug('dex extracting to %s', dest_path)
        subprocess.check_call(['enjarify', '-o', dest_path, self.path],
            shell=False, stderr=None, stdout=subprocess.PIPE)
        return dest_path

class DexFile(File):
    RE_FILE_TYPE = re.compile(r'^Dalvik dex file .*\b')

    @staticmethod
    def recognizes(file):
        return DexFile.RE_FILE_TYPE.match(file.magic_file_type)

    @needs_content
    def compare_details(self, other, source=None):
        with DexContainer(self).open() as my_container, \
             DexContainer(other).open() as other_container:
            return my_container.compare(other_container)