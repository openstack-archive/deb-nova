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

from oslo_serialization import jsonutils
from oslo_utils import uuidutils

from nova import context
from nova import exception
from nova import objects
from nova.objects import build_request
from nova import test
from nova.tests import fixtures
from nova.tests.unit import fake_build_request
from nova.tests.unit import fake_instance


class BuildRequestTestCase(test.NoDBTestCase):
    USES_DB_SELF = True

    def setUp(self):
        super(BuildRequestTestCase, self).setUp()
        # NOTE: This means that we're using a database for this test suite
        # despite inheriting from NoDBTestCase
        self.useFixture(fixtures.Database(database='api'))
        self.context = context.RequestContext('fake-user', 'fake-project')
        self.build_req_obj = build_request.BuildRequest()
        self.instance_uuid = uuidutils.generate_uuid()
        self.project_id = 'fake-project'

    def _create_req(self):
        args = fake_build_request.fake_db_req()
        args.pop('id', None)
        args['instance_uuid'] = self.instance_uuid
        args['project_id'] = self.project_id
        return build_request.BuildRequest._from_db_object(self.context,
                self.build_req_obj,
                self.build_req_obj._create_in_db(self.context, args))

    def test_get_by_instance_uuid_not_found(self):
        self.assertRaises(exception.BuildRequestNotFound,
                self.build_req_obj._get_by_instance_uuid_from_db, self.context,
                self.instance_uuid)

    def test_get_by_uuid(self):
        expected_req = self._create_req()
        req_obj = self.build_req_obj.get_by_instance_uuid(self.context,
                                                          self.instance_uuid)

        for key in self.build_req_obj.fields.keys():
            expected = getattr(expected_req, key)
            db_value = getattr(req_obj, key)
            if key == 'instance':
                objects.base.obj_equal_prims(expected, db_value)
                continue
            elif key == 'block_device_mappings':
                self.assertEqual(1, len(db_value))
                # Can't compare list objects directly, just compare the single
                # item they contain.
                objects.base.obj_equal_prims(expected[0], db_value[0])
                continue
            self.assertEqual(expected, db_value)

    def test_destroy(self):
        self._create_req()
        db_req = self.build_req_obj.get_by_instance_uuid(self.context,
                                                         self.instance_uuid)
        db_req.destroy()
        self.assertRaises(exception.BuildRequestNotFound,
                self.build_req_obj._get_by_instance_uuid_from_db, self.context,
                self.instance_uuid)

    def test_destroy_twice_raises(self):
        self._create_req()
        db_req = self.build_req_obj.get_by_instance_uuid(self.context,
                                                         self.instance_uuid)
        db_req.destroy()
        self.assertRaises(exception.BuildRequestNotFound, db_req.destroy)

    def test_save(self):
        self._create_req()
        db_req = self.build_req_obj.get_by_instance_uuid(self.context,
                                                         self.instance_uuid)
        db_req.project_id = 'foobar'
        db_req.save()
        updated_req = self.build_req_obj.get_by_instance_uuid(
            self.context, self.instance_uuid)
        self.assertEqual('foobar', updated_req.project_id)

    def test_save_not_found(self):
        self._create_req()
        db_req = self.build_req_obj.get_by_instance_uuid(self.context,
                                                         self.instance_uuid)
        db_req.project_id = 'foobar'
        db_req.destroy()
        self.assertRaises(exception.BuildRequestNotFound, db_req.save)


class BuildRequestListTestCase(test.NoDBTestCase):
    USES_DB_SELF = True

    def setUp(self):
        super(BuildRequestListTestCase, self).setUp()
        # NOTE: This means that we're using a database for this test suite
        # despite inheriting from NoDBTestCase
        self.useFixture(fixtures.Database(database='api'))
        self.project_id = 'fake-project'
        self.context = context.RequestContext('fake-user', self.project_id)

    def _create_req(self, project_id=None, instance=None):
        kwargs = {}
        if instance:
            kwargs['instance'] = jsonutils.dumps(instance.obj_to_primitive())
        args = fake_build_request.fake_db_req(**kwargs)
        args.pop('id', None)
        args['instance_uuid'] = uuidutils.generate_uuid()
        args['project_id'] = self.project_id if not project_id else project_id
        return build_request.BuildRequest._from_db_object(self.context,
                build_request.BuildRequest(),
                build_request.BuildRequest._create_in_db(self.context, args))

    def test_get_all_empty(self):
        req_objs = build_request.BuildRequestList.get_all(self.context)
        self.assertEqual([], req_objs.objects)

    def test_get_all(self):
        reqs = [self._create_req(), self._create_req()]

        req_list = build_request.BuildRequestList.get_all(self.context)

        self.assertEqual(2, len(req_list))
        for i in range(len(req_list)):
            self.assertEqual(reqs[i].instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(reqs[i].instance,
                                         req_list[i].instance)

    def test_get_all_filter_by_project_id(self):
        reqs = [self._create_req(), self._create_req(project_id='filter')]

        req_list = build_request.BuildRequestList.get_all(self.context)

        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[0].project_id, req_list[0].project_id)
        self.assertEqual(reqs[0].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[0].instance,
                                     req_list[0].instance)

    def test_get_all_bypass_project_id_filter_as_admin(self):
        reqs = [self._create_req(), self._create_req(project_id='filter')]

        req_list = build_request.BuildRequestList.get_all(
            self.context.elevated())

        self.assertEqual(2, len(req_list))
        for i in range(len(req_list)):
            self.assertEqual(reqs[i].project_id, req_list[i].project_id)
            self.assertEqual(reqs[i].instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(reqs[i].instance,
                                         req_list[i].instance)

    def test_get_by_filters(self):
        reqs = [self._create_req(), self._create_req()]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, sort_keys=['id'], sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        for i in range(len(req_list)):
            self.assertEqual(reqs[i].instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(reqs[i].instance,
                                         req_list[i].instance)

    def test_get_by_filters_limit_0(self):
        self._create_req()

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, limit=0)

        self.assertEqual([], req_list.objects)

    def test_get_by_filters_deleted(self):
        self._create_req()

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'deleted': True})

        self.assertEqual([], req_list.objects)

    def test_get_by_filters_cleaned(self):
        self._create_req()

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'cleaned': True})

        self.assertEqual([], req_list.objects)

    def test_get_by_filters_exact_match(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='findme')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='filterme')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'image_ref': 'findme'})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[1].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[1].instance,
                                     req_list[0].instance)

    def test_get_by_filters_exact_match_list(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='findme')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='filterme')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'image_ref': ['findme', 'fake']})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[1].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[1].instance,
                                     req_list[0].instance)

    def test_get_by_filters_exact_match_metadata(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, metadata={'foo': 'bar'}, expected_attrs='metadata')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, metadata={'bar': 'baz'}, expected_attrs='metadata')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'metadata': {'foo': 'bar'}})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[1].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[1].instance,
                                     req_list[0].instance)

    def test_get_by_filters_exact_match_metadata_list(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, metadata={'foo': 'bar', 'cat': 'meow'},
            expected_attrs='metadata')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, metadata={'bar': 'baz', 'cat': 'meow'},
            expected_attrs='metadata')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'metadata': [{'foo': 'bar'}, {'cat': 'meow'}]})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[1].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[1].instance,
                                     req_list[0].instance)

    def test_get_by_filters_regex_match_one(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, display_name='find this one')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, display_name='filter this one')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'display_name': 'find'})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(1, len(req_list))
        self.assertEqual(reqs[1].instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(reqs[1].instance,
                                     req_list[0].instance)

    def test_get_by_filters_regex_match_both(self):
        instance_find = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, display_name='find this one')
        instance_filter = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, display_name='filter this one')

        reqs = [self._create_req(instance=instance_filter),
                self._create_req(instance=instance_find)]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'display_name': 'this'}, sort_keys=['id'],
            sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        for i in range(len(req_list)):
            self.assertEqual(reqs[i].instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(reqs[i].instance,
                                         req_list[i].instance)

    def test_get_by_filters_sort_asc(self):
        instance_1024 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=1024)
        instance_512 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=512)

        req_second = self._create_req(instance=instance_1024)
        req_first = self._create_req(instance=instance_512)

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, sort_keys=['root_gb'], sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        self.assertEqual(req_first.instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(req_first.instance, req_list[0].instance)

        self.assertEqual(req_second.instance_uuid, req_list[1].instance_uuid)
        objects.base.obj_equal_prims(req_second.instance, req_list[1].instance)

    def test_get_by_filters_sort_desc(self):
        instance_1024 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=1024)
        instance_512 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=512)

        req_second = self._create_req(instance=instance_512)
        req_first = self._create_req(instance=instance_1024)

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, sort_keys=['root_gb'], sort_dirs=['desc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        self.assertEqual(req_first.instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(req_first.instance, req_list[0].instance)

        self.assertEqual(req_second.instance_uuid, req_list[1].instance_uuid)
        objects.base.obj_equal_prims(req_second.instance, req_list[1].instance)

    def test_get_by_filters_sort_build_req_id(self):
        # Create instance objects this way so that there is no 'id' set.
        # The 'id' will not be populated on a BuildRequest.instance so this
        # checks that sorting by 'id' uses the BuildRequest.id.
        instance_1 = objects.Instance(self.context, host=None,
                                      uuid=uuidutils.generate_uuid())
        instance_2 = objects.Instance(self.context, host=None,
                                      uuid=uuidutils.generate_uuid())

        req_first = self._create_req(instance=instance_2)
        req_second = self._create_req(instance=instance_1)

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, sort_keys=['id'], sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        self.assertEqual(req_first.instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(req_first.instance, req_list[0].instance)

        self.assertEqual(req_second.instance_uuid, req_list[1].instance_uuid)
        objects.base.obj_equal_prims(req_second.instance, req_list[1].instance)

    def test_get_by_filters_multiple_sort_keys(self):
        instance_first = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=512, image_ref='ccc')
        instance_second = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=512, image_ref='bbb')
        instance_third = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, root_gb=1024, image_ref='aaa')

        req_first = self._create_req(instance=instance_first)
        req_third = self._create_req(instance=instance_third)
        req_second = self._create_req(instance=instance_second)

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, sort_keys=['root_gb', 'image_ref'],
            sort_dirs=['asc', 'desc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(3, len(req_list))
        self.assertEqual(req_first.instance_uuid, req_list[0].instance_uuid)
        objects.base.obj_equal_prims(req_first.instance, req_list[0].instance)

        self.assertEqual(req_second.instance_uuid, req_list[1].instance_uuid)
        objects.base.obj_equal_prims(req_second.instance, req_list[1].instance)

        self.assertEqual(req_third.instance_uuid, req_list[2].instance_uuid)
        objects.base.obj_equal_prims(req_third.instance, req_list[2].instance)

    def test_get_by_filters_marker(self):
        instance = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None)

        reqs = [self._create_req(),
                self._create_req(instance=instance),
                self._create_req()]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, marker=instance.uuid, sort_keys=['id'],
            sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        for i, req in enumerate(reqs[1:]):
            self.assertEqual(req.instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(req.instance,
                                         req_list[i].instance)

    def test_get_by_filters_limit(self):
        reqs = [self._create_req(),
                self._create_req(),
                self._create_req()]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, limit=2, sort_keys=['id'],
            sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        for i, req in enumerate(reqs[:2]):
            self.assertEqual(req.instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(req.instance,
                                         req_list[i].instance)

    def test_get_by_filters_marker_limit(self):
        instance = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None)

        reqs = [self._create_req(),
                self._create_req(instance=instance),
                self._create_req(),
                self._create_req()]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, marker=instance.uuid, limit=2,
            sort_keys=['id'], sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(2, len(req_list))
        for i, req in enumerate(reqs[1:3]):
            self.assertEqual(req.instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(req.instance,
                                         req_list[i].instance)

    def test_get_by_filters_marker_overlimit(self):
        instance = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None)

        reqs = [self._create_req(),
                self._create_req(instance=instance),
                self._create_req(),
                self._create_req()]

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {}, marker=instance.uuid, limit=4,
            sort_keys=['id'], sort_dirs=['asc'])

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(3, len(req_list))
        for i, req in enumerate(reqs[1:]):
            self.assertEqual(req.instance_uuid, req_list[i].instance_uuid)
            objects.base.obj_equal_prims(req.instance,
                                         req_list[i].instance)

    def test_get_by_filters_bails_on_empty_list_check(self):
        instance1 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='')
        instance2 = fake_instance.fake_instance_obj(
            self.context, objects.Instance, uuid=uuidutils.generate_uuid(),
            host=None, image_ref='')

        self._create_req(instance=instance1)
        self._create_req(instance=instance2)

        req_list = build_request.BuildRequestList.get_by_filters(
            self.context, {'image_ref': []})

        self.assertIsInstance(req_list, objects.BuildRequestList)
        self.assertEqual(0, len(req_list))
