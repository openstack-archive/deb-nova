# Copyright 2011 Eldar Nugaev
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock

from nova.api.openstack import compute
from nova.compute import api as compute_api
from nova import exception
from nova.openstack.common import jsonutils
from nova import test
from nova.tests.api.openstack import fakes


UUID = 'abc'


def fake_get_diagnostics(self, _context, instance_uuid):
    return {'data': 'Some diagnostic info'}


def fake_instance_get(self, _context, instance_uuid, want_objects=False):
    if instance_uuid != UUID:
        raise Exception("Invalid UUID")
    return {'uuid': instance_uuid}


class ServerDiagnosticsTest(test.NoDBTestCase):

    def setUp(self):
        super(ServerDiagnosticsTest, self).setUp()
        self.router = compute.APIRouterV3(init_only=('servers',
                                                     'os-server-diagnostics'))

    @mock.patch.object(compute_api.API, 'get_diagnostics',
                       fake_get_diagnostics)
    @mock.patch.object(compute_api.API, 'get', fake_instance_get)
    def test_get_diagnostics(self):
        req = fakes.HTTPRequestV3.blank(
            '/servers/%s/os-server-diagnostics' % UUID)
        res = req.get_response(self.router)
        output = jsonutils.loads(res.body)
        self.assertEqual(output, {'data': 'Some diagnostic info'})

    @mock.patch.object(compute_api.API, 'get_diagnostics',
                fake_get_diagnostics)
    @mock.patch.object(compute_api.API, 'get',
                side_effect=exception.InstanceNotFound(instance_id=UUID))
    def test_get_diagnostics_with_non_existed_instance(self, mock_get):
        req = fakes.HTTPRequestV3.blank(
            '/servers/%s/os-server-diagnostics' % UUID)
        res = req.get_response(self.router)
        self.assertEqual(res.status_int, 404)

    @mock.patch.object(compute_api.API, 'get_diagnostics',
                side_effect=exception.InstanceInvalidState('fake message'))
    @mock.patch.object(compute_api.API, 'get', fake_instance_get)
    def test_get_diagnostics_raise_conflict_on_invalid_state(self,
                                                  mock_get_diagnostics):
        req = fakes.HTTPRequestV3.blank(
            '/servers/%s/os-server-diagnostics' % UUID)
        res = req.get_response(self.router)
        self.assertEqual(409, res.status_int)
