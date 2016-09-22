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
from oslo_serialization import jsonutils

from nova import exception
from nova import objects
from nova.objects import build_request
from nova.tests.unit import fake_build_request
from nova.tests.unit import fake_instance
from nova.tests.unit.objects import test_objects
from nova.tests import uuidsentinel as uuids


class _TestBuildRequestObject(object):

    @mock.patch.object(build_request.BuildRequest,
            '_get_by_instance_uuid_from_db')
    def test_get_by_instance_uuid(self, get_by_uuid):
        fake_req = fake_build_request.fake_db_req()
        get_by_uuid.return_value = fake_req

        req_obj = build_request.BuildRequest.get_by_instance_uuid(self.context,
                fake_req['instance_uuid'])

        self.assertEqual(fake_req['instance_uuid'], req_obj.instance_uuid)
        self.assertEqual(fake_req['project_id'], req_obj.project_id)
        self.assertIsInstance(req_obj.instance, objects.Instance)
        get_by_uuid.assert_called_once_with(self.context,
                fake_req['instance_uuid'])

    @mock.patch.object(build_request.BuildRequest,
            '_get_by_instance_uuid_from_db')
    def test_get_by_instance_uuid_instance_none(self, get_by_uuid):
        fake_req = fake_build_request.fake_db_req()
        fake_req['instance'] = None
        get_by_uuid.return_value = fake_req

        self.assertRaises(exception.BuildRequestNotFound,
                build_request.BuildRequest.get_by_instance_uuid, self.context,
                fake_req['instance_uuid'])

    @mock.patch.object(build_request.BuildRequest,
            '_get_by_instance_uuid_from_db')
    def test_get_by_instance_uuid_instance_version_too_new(self, get_by_uuid):
        fake_req = fake_build_request.fake_db_req()
        instance = fake_instance.fake_instance_obj(self.context,
                objects.Instance, uuid=fake_req['instance_uuid'])
        instance.VERSION = '99'
        fake_req['instance'] = jsonutils.dumps(instance.obj_to_primitive)
        get_by_uuid.return_value = fake_req

        self.assertRaises(exception.BuildRequestNotFound,
                build_request.BuildRequest.get_by_instance_uuid, self.context,
                fake_req['instance_uuid'])

    @mock.patch.object(build_request.BuildRequest,
            '_get_by_instance_uuid_from_db')
    def test_get_by_instance_uuid_do_not_override_locked_by(self, get_by_uuid):
        fake_req = fake_build_request.fake_db_req()
        instance = fake_instance.fake_instance_obj(self.context,
                objects.Instance, uuid=fake_req['instance_uuid'])
        instance.locked_by = 'admin'
        fake_req['instance'] = jsonutils.dumps(instance.obj_to_primitive())
        get_by_uuid.return_value = fake_req

        req_obj = build_request.BuildRequest.get_by_instance_uuid(self.context,
                fake_req['instance_uuid'])

        self.assertIsInstance(req_obj.instance, objects.Instance)
        self.assertEqual('admin', req_obj.instance.locked_by)

    def test_create(self):
        fake_req = fake_build_request.fake_db_req()
        req_obj = fake_build_request.fake_req_obj(self.context, fake_req)

        def _test_create_args(self2, context, changes):
            for field in ['instance_uuid', 'project_id']:
                self.assertEqual(fake_req[field], changes[field])
            self.assertEqual(
                    jsonutils.dumps(req_obj.instance.obj_to_primitive()),
                    changes['instance'])
            return fake_req

        with mock.patch.object(build_request.BuildRequest, '_create_in_db',
                _test_create_args):
            req_obj.create()
        self.assertEqual({}, req_obj.obj_get_changes())

    def test_create_id_set(self):
        req_obj = build_request.BuildRequest(self.context)
        req_obj.id = 3

        self.assertRaises(exception.ObjectActionError, req_obj.create)

    def test_create_uuid_set(self):
        req_obj = build_request.BuildRequest(self.context)

        self.assertRaises(exception.ObjectActionError, req_obj.create)

    @mock.patch.object(build_request.BuildRequest, '_destroy_in_db')
    def test_destroy(self, destroy_in_db):
        req_obj = build_request.BuildRequest(self.context)
        req_obj.instance_uuid = uuids.instance
        req_obj.destroy()

        destroy_in_db.assert_called_once_with(self.context,
                                              req_obj.instance_uuid)

    @mock.patch.object(build_request.BuildRequest, '_save_in_db')
    def test_save(self, save_in_db):
        fake_req = fake_build_request.fake_db_req()
        save_in_db.return_value = fake_req
        req_obj = fake_build_request.fake_req_obj(self.context, fake_req)
        # We need to simulate the BuildRequest object being persisted before
        # that call.
        req_obj.id = 1
        req_obj.obj_reset_changes(recursive=True)

        req_obj.project_id = 'foo'
        req_obj.save()

        save_in_db.assert_called_once_with(self.context, req_obj.id,
                                           {'project_id': 'foo'})


class TestBuildRequestObject(test_objects._LocalTest,
                             _TestBuildRequestObject):
    pass


class TestRemoteBuildRequestObject(test_objects._RemoteTest,
                                   _TestBuildRequestObject):
    pass


class _TestBuildRequestListObject(object):
    @mock.patch.object(build_request.BuildRequestList, '_get_all_from_db')
    def test_get_all(self, get_all):
        fake_reqs = [fake_build_request.fake_db_req() for x in range(2)]
        get_all.return_value = fake_reqs

        req_objs = build_request.BuildRequestList.get_all(self.context)

        self.assertEqual(2, len(req_objs))
        for i in range(2):
            self.assertEqual(fake_reqs[i]['instance_uuid'],
                             req_objs[i].instance_uuid)
            self.assertEqual(fake_reqs[i]['project_id'],
                             req_objs[i].project_id)
            self.assertIsInstance(req_objs[i].instance, objects.Instance)


class TestBuildRequestListObject(test_objects._LocalTest,
                                 _TestBuildRequestListObject):
    pass


class TestRemoteBuildRequestListObject(test_objects._RemoteTest,
                                       _TestBuildRequestListObject):
    pass
