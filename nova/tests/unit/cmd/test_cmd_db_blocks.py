# Copyright 2016 Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib

import mock

from nova.cmd import compute
from nova.cmd import network
from nova import db
from nova import exception
from nova import test


@contextlib.contextmanager
def restore_db():
    orig = db.api.IMPL
    try:
        yield
    finally:
        db.api.IMPL = orig


class ComputeMainTest(test.NoDBTestCase):
    @mock.patch('nova.utils.monkey_patch')
    @mock.patch('nova.conductor.api.API.wait_until_ready')
    @mock.patch('oslo_reports.guru_meditation_report')
    def _call_main(self, mod, gmr, cond, patch):
        @mock.patch.object(mod, 'config')
        @mock.patch.object(mod, 'service')
        def run_main(serv, conf):
            mod.main()

        run_main()

    def test_compute_main_blocks_db(self):
        with restore_db():
            self._call_main(compute)
            self.assertRaises(exception.DBNotAllowed,
                              db.api.instance_get, 1, 2)

    def test_network_main_blocks_db(self):
        with restore_db():
            self._call_main(network)
            self.assertRaises(exception.DBNotAllowed,
                              db.api.instance_get, 1, 2)
