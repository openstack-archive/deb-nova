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

"""Unit tests for compute API."""

import datetime

import iso8601
import mock
from mox3 import mox
from oslo_messaging import exceptions as oslo_exceptions
from oslo_serialization import jsonutils
from oslo_utils import fixture as utils_fixture
from oslo_utils import timeutils
from oslo_utils import uuidutils

from nova.compute import api as compute_api
from nova.compute import arch
from nova.compute import cells_api as compute_cells_api
from nova.compute import flavors
from nova.compute import instance_actions
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_mode
from nova.compute import vm_states
from nova import conductor
from nova import context
from nova import db
from nova import exception
from nova import objects
from nova.objects import base as obj_base
from nova.objects import block_device as block_device_obj
from nova.objects import fields as fields_obj
from nova.objects import quotas as quotas_obj
from nova import quota
from nova import test
from nova.tests import fixtures
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_build_request
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_volume
from nova.tests.unit.image import fake as fake_image
from nova.tests.unit import matchers
from nova.tests.unit.objects import test_flavor
from nova.tests.unit.objects import test_migration
from nova.tests import uuidsentinel as uuids
from nova import utils
from nova.volume import cinder


FAKE_IMAGE_REF = 'fake-image-ref'
NODENAME = 'fakenode1'
SHELVED_IMAGE = 'fake-shelved-image'
SHELVED_IMAGE_NOT_FOUND = 'fake-shelved-image-notfound'
SHELVED_IMAGE_NOT_AUTHORIZED = 'fake-shelved-image-not-authorized'
SHELVED_IMAGE_EXCEPTION = 'fake-shelved-image-exception'


class _ComputeAPIUnitTestMixIn(object):
    def setUp(self):
        super(_ComputeAPIUnitTestMixIn, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.compute_api = compute_api.API()
        self.context = context.RequestContext(self.user_id,
                                              self.project_id)

    def _get_vm_states(self, exclude_states=None):
        vm_state = set([vm_states.ACTIVE, vm_states.BUILDING, vm_states.PAUSED,
                    vm_states.SUSPENDED, vm_states.RESCUED, vm_states.STOPPED,
                    vm_states.RESIZED, vm_states.SOFT_DELETED,
                    vm_states.DELETED, vm_states.ERROR, vm_states.SHELVED,
                    vm_states.SHELVED_OFFLOADED])
        if not exclude_states:
            exclude_states = set()
        return vm_state - exclude_states

    def _create_flavor(self, **updates):
        flavor = {'id': 1,
                  'flavorid': 1,
                  'name': 'm1.tiny',
                  'memory_mb': 512,
                  'vcpus': 1,
                  'vcpu_weight': None,
                  'root_gb': 1,
                  'ephemeral_gb': 0,
                  'rxtx_factor': 1,
                  'swap': 0,
                  'deleted': 0,
                  'disabled': False,
                  'is_public': True,
                  'deleted_at': None,
                  'created_at': datetime.datetime(2012, 1, 19, 18,
                                                  49, 30, 877329),
                  'updated_at': None,
                 }
        if updates:
            flavor.update(updates)
        return objects.Flavor._from_db_object(self.context, objects.Flavor(),
                                              flavor)

    def _create_instance_obj(self, params=None, flavor=None):
        """Create a test instance."""
        if not params:
            params = {}

        if flavor is None:
            flavor = self._create_flavor()

        now = timeutils.utcnow()

        instance = objects.Instance()
        instance.metadata = {}
        instance.metadata.update(params.pop('metadata', {}))
        instance.system_metadata = params.pop('system_metadata', {})
        instance._context = self.context
        instance.id = 1
        instance.uuid = uuidutils.generate_uuid()
        instance.cell_name = 'api!child'
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = None
        instance.image_ref = FAKE_IMAGE_REF
        instance.reservation_id = 'r-fakeres'
        instance.user_id = self.user_id
        instance.project_id = self.project_id
        instance.host = 'fake_host'
        instance.node = NODENAME
        instance.instance_type_id = flavor.id
        instance.ami_launch_index = 0
        instance.memory_mb = 0
        instance.vcpus = 0
        instance.root_gb = 0
        instance.ephemeral_gb = 0
        instance.architecture = arch.X86_64
        instance.os_type = 'Linux'
        instance.locked = False
        instance.created_at = now
        instance.updated_at = now
        instance.launched_at = now
        instance.disable_terminate = False
        instance.info_cache = objects.InstanceInfoCache()
        instance.flavor = flavor
        instance.old_flavor = instance.new_flavor = None

        if params:
            instance.update(params)
        instance.obj_reset_changes()
        return instance

    def _obj_to_list_obj(self, list_obj, obj):
        list_obj.objects = []
        list_obj.objects.append(obj)
        list_obj._context = self.context
        list_obj.obj_reset_changes()
        return list_obj

    @mock.patch('nova.conductor.conductor_api.ComputeTaskAPI.build_instances')
    @mock.patch('nova.compute.api.API._record_action_start')
    @mock.patch('nova.compute.api.API._check_requested_networks')
    @mock.patch('nova.compute.api.API._get_image')
    @mock.patch('nova.compute.api.API._provision_instances')
    def test_create_with_networks_max_count_none(self, provision_instances,
                                                 get_image,
                                                 check_requested_networks,
                                                 record_action_start,
                                                 build_instances):
        # Make sure max_count is checked for None, as Python3 doesn't allow
        # comparison between NoneType and Integer, something that's allowed in
        # Python 2.
        get_image.return_value = (None, {})
        check_requested_networks.return_value = 1

        instance_type = self._create_flavor()

        port = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '10.0.0.1'
        requested_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(address=address,
                                            port_id=port)])

        self.compute_api.create(self.context, instance_type, 'image_id',
                                requested_networks=requested_networks,
                                max_count=None)

    def test_create_quota_exceeded_messages(self):
        image_href = "image_href"
        image_id = 0
        instance_type = self._create_flavor()

        self.mox.StubOutWithMock(self.compute_api, "_get_image")
        self.mox.StubOutWithMock(quota.QUOTAS, "limit_check")
        self.mox.StubOutWithMock(quota.QUOTAS, "reserve")

        quotas = {'instances': 1, 'cores': 1, 'ram': 1}
        usages = {r: {'in_use': 1, 'reserved': 1} for r in
                  ['instances', 'cores', 'ram']}
        quota_exception = exception.OverQuota(quotas=quotas,
            usages=usages, overs=['instances'])

        for _unused in range(2):
            self.compute_api._get_image(self.context, image_href).AndReturn(
                (image_id, {}))
            quota.QUOTAS.limit_check(self.context, metadata_items=mox.IsA(int),
                                     project_id=mox.IgnoreArg(),
                                     user_id=mox.IgnoreArg())
            quota.QUOTAS.reserve(self.context, instances=40,
                                 cores=mox.IsA(int),
                                 expire=mox.IgnoreArg(),
                                 project_id=mox.IgnoreArg(),
                                 user_id=mox.IgnoreArg(),
                                 ram=mox.IsA(int)).AndRaise(quota_exception)

        self.mox.ReplayAll()

        for min_count, message in [(20, '20-40'), (40, '40')]:
            try:
                self.compute_api.create(self.context, instance_type,
                                        "image_href", min_count=min_count,
                                        max_count=40)
            except exception.TooManyInstances as e:
                self.assertEqual(message, e.kwargs['req'])
            else:
                self.fail("Exception not raised")

    def _test_create_max_net_count(self, max_net_count, min_count, max_count):
        with test.nested(
            mock.patch.object(self.compute_api, '_get_image',
                              return_value=(None, {})),
            mock.patch.object(self.compute_api, '_check_auto_disk_config'),
            mock.patch.object(self.compute_api,
                              '_validate_and_build_base_options',
                              return_value=({}, max_net_count, None))
        ) as (
            get_image,
            check_auto_disk_config,
            validate_and_build_base_options
        ):
            self.assertRaises(exception.PortLimitExceeded,
                self.compute_api.create, self.context, 'fake_flavor',
                'image_id', min_count=min_count, max_count=max_count)

    def test_max_net_count_zero(self):
        # Test when max_net_count is zero.
        max_net_count = 0
        min_count = 2
        max_count = 3
        self._test_create_max_net_count(max_net_count, min_count, max_count)

    def test_max_net_count_less_than_min_count(self):
        # Test when max_net_count is nonzero but less than min_count.
        max_net_count = 1
        min_count = 2
        max_count = 3
        self._test_create_max_net_count(max_net_count, min_count, max_count)

    def test_specified_port_and_multiple_instances_neutronv2(self):
        # Tests that if port is specified there is only one instance booting
        # (i.e max_count == 1) as we can't share the same port across multiple
        # instances.
        self.flags(use_neutron=True)
        port = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '10.0.0.1'
        min_count = 1
        max_count = 2
        requested_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(address=address,
                                            port_id=port)])

        self.assertRaises(exception.MultiplePortsNotApplicable,
            self.compute_api.create, self.context, 'fake_flavor', 'image_id',
            min_count=min_count, max_count=max_count,
            requested_networks=requested_networks)

    def _test_specified_ip_and_multiple_instances_helper(self,
                                                         requested_networks):
        # Tests that if ip is specified there is only one instance booting
        # (i.e max_count == 1)
        min_count = 1
        max_count = 2
        self.assertRaises(exception.InvalidFixedIpAndMaxCountRequest,
            self.compute_api.create, self.context, "fake_flavor", 'image_id',
            min_count=min_count, max_count=max_count,
            requested_networks=requested_networks)

    def test_specified_ip_and_multiple_instances(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '10.0.0.1'
        requested_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id=network,
                                            address=address)])
        self._test_specified_ip_and_multiple_instances_helper(
            requested_networks)

    def test_specified_ip_and_multiple_instances_neutronv2(self):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '10.0.0.1'
        requested_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id=network,
                                            address=address)])
        self._test_specified_ip_and_multiple_instances_helper(
            requested_networks)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'reserve_block_device_name')
    def test_create_volume_bdm_call_reserve_dev_name(self, mock_reserve):
        bdm = objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'id': 1,
                 'volume_id': 1,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                 'boot_index': 1,
                 }))
        mock_reserve.return_value = bdm
        instance = self._create_instance_obj()
        result = self.compute_api._create_volume_bdm(self.context,
                                                     instance,
                                                     'vda',
                                                     '1',
                                                     None,
                                                     None)
        self.assertTrue(mock_reserve.called)
        self.assertEqual(result, bdm)

    @mock.patch.object(objects.BlockDeviceMapping, 'create')
    def test_create_volume_bdm_local_creation(self, bdm_create):
        instance = self._create_instance_obj()
        volume_id = 'fake-vol-id'
        bdm = objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'instance_uuid': instance.uuid,
                 'volume_id': volume_id,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                 'boot_index': None,
                 'disk_bus': None,
                 'device_type': None
                 }))
        result = self.compute_api._create_volume_bdm(self.context,
                                                     instance,
                                                     '/dev/vda',
                                                     volume_id,
                                                     None,
                                                     None,
                                                     is_local_creation=True)
        self.assertEqual(result.instance_uuid, bdm.instance_uuid)
        self.assertIsNone(result.device_name)
        self.assertEqual(result.volume_id, bdm.volume_id)
        self.assertTrue(bdm_create.called)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'reserve_block_device_name')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'attach_volume')
    def test_attach_volume(self, mock_attach, mock_reserve):
        instance = self._create_instance_obj()
        volume = fake_volume.fake_volume(1, 'test-vol', 'test-vol',
                                         None, None, None, None, None)

        fake_bdm = mock.MagicMock(spec=objects.BlockDeviceMapping)
        mock_reserve.return_value = fake_bdm

        mock_volume_api = mock.patch.object(self.compute_api, 'volume_api',
                                            mock.MagicMock(spec=cinder.API))

        with mock_volume_api as mock_v_api:
            mock_v_api.get.return_value = volume
            self.compute_api.attach_volume(
                self.context, instance, volume['id'])
            mock_v_api.check_availability_zone.assert_called_once_with(
                self.context, volume, instance=instance)
            mock_v_api.reserve_volume.assert_called_once_with(self.context,
                                                              volume['id'])
            mock_attach.assert_called_once_with(self.context,
                                                instance, fake_bdm)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'reserve_block_device_name')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'attach_volume')
    def test_attach_volume_reserve_fails(self, mock_attach, mock_reserve):
        instance = self._create_instance_obj()
        volume = fake_volume.fake_volume(1, 'test-vol', 'test-vol',
                                         None, None, None, None, None)

        fake_bdm = mock.MagicMock(spec=objects.BlockDeviceMapping)
        mock_reserve.return_value = fake_bdm

        mock_volume_api = mock.patch.object(self.compute_api, 'volume_api',
                                            mock.MagicMock(spec=cinder.API))

        with mock_volume_api as mock_v_api:
            mock_v_api.get.return_value = volume
            mock_v_api.reserve_volume.side_effect = test.TestingException()
            self.assertRaises(test.TestingException,
                              self.compute_api.attach_volume,
                              self.context, instance, volume['id'])
            mock_v_api.check_availability_zone.assert_called_once_with(
                self.context, volume, instance=instance)
            mock_v_api.reserve_volume.assert_called_once_with(self.context,
                                                              volume['id'])
            self.assertEqual(0, mock_attach.call_count)
            fake_bdm.destroy.assert_called_once_with()

    def test_suspend(self):
        # Ensure instance can be suspended.
        instance = self._create_instance_obj()
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)
        self.assertIsNone(instance.task_state)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'suspend_instance')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.SUSPEND)
        rpcapi.suspend_instance(self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.suspend(self.context, instance)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertEqual(task_states.SUSPENDING,
                         instance.task_state)

    def _test_suspend_fails(self, vm_state):
        params = dict(vm_state=vm_state)
        instance = self._create_instance_obj(params=params)
        self.assertIsNone(instance.task_state)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.suspend,
                          self.context, instance)

    def test_suspend_fails_invalid_states(self):
        invalid_vm_states = self._get_vm_states(set([vm_states.ACTIVE]))
        for state in invalid_vm_states:
            self._test_suspend_fails(state)

    def test_resume(self):
        # Ensure instance can be resumed (if suspended).
        instance = self._create_instance_obj(
                params=dict(vm_state=vm_states.SUSPENDED))
        self.assertEqual(instance.vm_state, vm_states.SUSPENDED)
        self.assertIsNone(instance.task_state)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'resume_instance')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.RESUME)
        rpcapi.resume_instance(self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.resume(self.context, instance)
        self.assertEqual(vm_states.SUSPENDED, instance.vm_state)
        self.assertEqual(task_states.RESUMING,
                         instance.task_state)

    def test_start(self):
        params = dict(vm_state=vm_states.STOPPED)
        instance = self._create_instance_obj(params=params)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.START)

        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi

        self.mox.StubOutWithMock(rpcapi, 'start_instance')
        rpcapi.start_instance(self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.start(self.context, instance)
        self.assertEqual(task_states.POWERING_ON,
                         instance.task_state)

    def test_start_invalid_state(self):
        instance = self._create_instance_obj()
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.start,
                          self.context, instance)

    def test_start_no_host(self):
        params = dict(vm_state=vm_states.STOPPED, host='')
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.start,
                          self.context, instance)

    def _test_stop(self, vm_state, force=False, clean_shutdown=True):
        # Make sure 'progress' gets reset
        params = dict(task_state=None, progress=99, vm_state=vm_state)
        instance = self._create_instance_obj(params=params)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.STOP)

        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi

        self.mox.StubOutWithMock(rpcapi, 'stop_instance')
        rpcapi.stop_instance(self.context, instance, do_cast=True,
                             clean_shutdown=clean_shutdown)

        self.mox.ReplayAll()

        if force:
            self.compute_api.force_stop(self.context, instance,
                                        clean_shutdown=clean_shutdown)
        else:
            self.compute_api.stop(self.context, instance,
                                  clean_shutdown=clean_shutdown)
        self.assertEqual(task_states.POWERING_OFF,
                         instance.task_state)
        self.assertEqual(0, instance.progress)

    def test_stop(self):
        self._test_stop(vm_states.ACTIVE)

    def test_stop_stopped_instance_with_bypass(self):
        self._test_stop(vm_states.STOPPED, force=True)

    def test_stop_forced_shutdown(self):
        self._test_stop(vm_states.ACTIVE, force=True)

    def test_stop_without_clean_shutdown(self):
        self._test_stop(vm_states.ACTIVE,
                       clean_shutdown=False)

    def test_stop_forced_without_clean_shutdown(self):
        self._test_stop(vm_states.ACTIVE, force=True,
                        clean_shutdown=False)

    def _test_stop_invalid_state(self, vm_state):
        params = dict(vm_state=vm_state)
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.stop,
                          self.context, instance)

    def test_stop_fails_invalid_states(self):
        invalid_vm_states = self._get_vm_states(set([vm_states.ACTIVE,
                                                     vm_states.ERROR]))
        for state in invalid_vm_states:
            self._test_stop_invalid_state(state)

    def test_stop_a_stopped_inst(self):
        params = {'vm_state': vm_states.STOPPED}
        instance = self._create_instance_obj(params=params)

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.stop,
                          self.context, instance)

    def test_stop_no_host(self):
        params = {'host': ''}
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.stop,
                          self.context, instance)

    @mock.patch('nova.compute.api.API._record_action_start')
    @mock.patch('nova.compute.rpcapi.ComputeAPI.trigger_crash_dump')
    def test_trigger_crash_dump(self,
                                trigger_crash_dump,
                                _record_action_start):
        instance = self._create_instance_obj()

        self.compute_api.trigger_crash_dump(self.context, instance)

        _record_action_start.assert_called_once_with(self.context, instance,
            instance_actions.TRIGGER_CRASH_DUMP)

        if self.cell_type == 'api':
            # cell api has not been implemented.
            pass
        else:
            trigger_crash_dump.assert_called_once_with(self.context, instance)

        self.assertIsNone(instance.task_state)

    def test_trigger_crash_dump_invalid_state(self):
        params = dict(vm_state=vm_states.STOPPED)
        instance = self._create_instance_obj(params)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.trigger_crash_dump,
                          self.context, instance)

    def test_trigger_crash_dump_no_host(self):
        params = dict(host='')
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.trigger_crash_dump,
                          self.context, instance)

    def test_trigger_crash_dump_locked(self):
        params = dict(locked=True)
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceIsLocked,
                          self.compute_api.trigger_crash_dump,
                          self.context, instance)

    def _test_shelve(self, vm_state=vm_states.ACTIVE,
                     boot_from_volume=False, clean_shutdown=True):
        params = dict(task_state=None, vm_state=vm_state,
                      display_name='fake-name')
        instance = self._create_instance_obj(params=params)
        with test.nested(
            mock.patch.object(compute_utils, 'is_volume_backed_instance',
                              return_value=boot_from_volume),
            mock.patch.object(self.compute_api, '_create_image',
                              return_value=dict(id='fake-image-id')),
            mock.patch.object(instance, 'save'),
            mock.patch.object(self.compute_api, '_record_action_start'),
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'shelve_instance'),
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'shelve_offload_instance')
        ) as (
            volume_backed_inst, create_image, instance_save,
            record_action_start, rpcapi_shelve_instance,
            rpcapi_shelve_offload_instance
        ):
            self.compute_api.shelve(self.context, instance,
                                    clean_shutdown=clean_shutdown)
            # assert field values set on the instance object
            self.assertEqual(task_states.SHELVING, instance.task_state)
            # assert our mock calls
            volume_backed_inst.assert_called_once_with(
                self.context, instance)
            instance_save.assert_called_once_with(expected_task_state=[None])
            record_action_start.assert_called_once_with(
                self.context, instance, instance_actions.SHELVE)
            if boot_from_volume:
                rpcapi_shelve_offload_instance.assert_called_once_with(
                    self.context, instance=instance,
                    clean_shutdown=clean_shutdown)
            else:
                rpcapi_shelve_instance.assert_called_once_with(
                    self.context, instance=instance, image_id='fake-image-id',
                    clean_shutdown=clean_shutdown)

    def test_shelve(self):
        self._test_shelve()

    def test_shelve_stopped(self):
        self._test_shelve(vm_state=vm_states.STOPPED)

    def test_shelve_paused(self):
        self._test_shelve(vm_state=vm_states.PAUSED)

    def test_shelve_suspended(self):
        self._test_shelve(vm_state=vm_states.SUSPENDED)

    def test_shelve_boot_from_volume(self):
        self._test_shelve(boot_from_volume=True)

    def test_shelve_forced_shutdown(self):
        self._test_shelve(clean_shutdown=False)

    def test_shelve_boot_from_volume_forced_shutdown(self):
        self._test_shelve(boot_from_volume=True,
                          clean_shutdown=False)

    def _test_shelve_invalid_state(self, vm_state):
        params = dict(vm_state=vm_state)
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.shelve,
                          self.context, instance)

    def test_shelve_fails_invalid_states(self):
        invalid_vm_states = self._get_vm_states(set([vm_states.ACTIVE,
                                                     vm_states.STOPPED,
                                                     vm_states.PAUSED,
                                                     vm_states.SUSPENDED]))
        for state in invalid_vm_states:
            self._test_shelve_invalid_state(state)

    def _test_shelve_offload(self, clean_shutdown=True):
        params = dict(task_state=None, vm_state=vm_states.SHELVED)
        instance = self._create_instance_obj(params=params)
        with test.nested(
            mock.patch.object(instance, 'save'),
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'shelve_offload_instance')
        ) as (
            instance_save, rpcapi_shelve_offload_instance
        ):
            self.compute_api.shelve_offload(self.context, instance,
                                            clean_shutdown=clean_shutdown)
            # assert field values set on the instance object
            self.assertEqual(task_states.SHELVING_OFFLOADING,
                             instance.task_state)
            instance_save.assert_called_once_with(expected_task_state=[None])
            rpcapi_shelve_offload_instance.assert_called_once_with(
                    self.context, instance=instance,
                    clean_shutdown=clean_shutdown)

    def test_shelve_offload(self):
        self._test_shelve_offload()

    def test_shelve_offload_forced_shutdown(self):
        self._test_shelve_offload(clean_shutdown=False)

    def _test_shelve_offload_invalid_state(self, vm_state):
        params = dict(vm_state=vm_state)
        instance = self._create_instance_obj(params=params)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.shelve_offload,
                          self.context, instance)

    def test_shelve_offload_fails_invalid_states(self):
        invalid_vm_states = self._get_vm_states(set([vm_states.SHELVED]))
        for state in invalid_vm_states:
            self._test_shelve_offload_invalid_state(state)

    def _test_reboot_type(self, vm_state, reboot_type, task_state=None):
        # Ensure instance can be soft rebooted.
        inst = self._create_instance_obj()
        inst.vm_state = vm_state
        inst.task_state = task_state

        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(inst, 'save')
        expected_task_state = [None]
        if reboot_type == 'HARD':
            expected_task_state.extend([task_states.REBOOTING,
                                        task_states.REBOOT_PENDING,
                                        task_states.REBOOT_STARTED,
                                        task_states.REBOOTING_HARD,
                                        task_states.RESUMING,
                                        task_states.UNPAUSING,
                                        task_states.SUSPENDING])
        inst.save(expected_task_state=expected_task_state)
        self.compute_api._record_action_start(self.context, inst,
                                              instance_actions.REBOOT)

        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi

        self.mox.StubOutWithMock(rpcapi, 'reboot_instance')
        rpcapi.reboot_instance(self.context, instance=inst,
                               block_device_info=None,
                               reboot_type=reboot_type)
        self.mox.ReplayAll()

        self.compute_api.reboot(self.context, inst, reboot_type)

    def _test_reboot_type_fails(self, reboot_type, **updates):
        inst = self._create_instance_obj()
        inst.update(updates)

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.reboot,
                          self.context, inst, reboot_type)

    def test_reboot_hard_active(self):
        self._test_reboot_type(vm_states.ACTIVE, 'HARD')

    def test_reboot_hard_error(self):
        self._test_reboot_type(vm_states.ERROR, 'HARD')

    def test_reboot_hard_rebooting(self):
        self._test_reboot_type(vm_states.ACTIVE, 'HARD',
                               task_state=task_states.REBOOTING)

    def test_reboot_hard_reboot_started(self):
        self._test_reboot_type(vm_states.ACTIVE, 'HARD',
                               task_state=task_states.REBOOT_STARTED)

    def test_reboot_hard_reboot_pending(self):
        self._test_reboot_type(vm_states.ACTIVE, 'HARD',
                               task_state=task_states.REBOOT_PENDING)

    def test_reboot_hard_rescued(self):
        self._test_reboot_type_fails('HARD', vm_state=vm_states.RESCUED)

    def test_reboot_hard_resuming(self):
        self._test_reboot_type(vm_states.ACTIVE,
                               'HARD', task_state=task_states.RESUMING)

    def test_reboot_hard_pausing(self):
        self._test_reboot_type(vm_states.ACTIVE,
                               'HARD', task_state=task_states.PAUSING)

    def test_reboot_hard_unpausing(self):
        self._test_reboot_type(vm_states.ACTIVE,
                               'HARD', task_state=task_states.UNPAUSING)

    def test_reboot_hard_suspending(self):
        self._test_reboot_type(vm_states.ACTIVE,
                               'HARD', task_state=task_states.SUSPENDING)

    def test_reboot_hard_error_not_launched(self):
        self._test_reboot_type_fails('HARD', vm_state=vm_states.ERROR,
                                     launched_at=None)

    def test_reboot_soft(self):
        self._test_reboot_type(vm_states.ACTIVE, 'SOFT')

    def test_reboot_soft_error(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.ERROR)

    def test_reboot_soft_paused(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.PAUSED)

    def test_reboot_soft_stopped(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.STOPPED)

    def test_reboot_soft_suspended(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.SUSPENDED)

    def test_reboot_soft_rebooting(self):
        self._test_reboot_type_fails('SOFT', task_state=task_states.REBOOTING)

    def test_reboot_soft_rebooting_hard(self):
        self._test_reboot_type_fails('SOFT',
                                     task_state=task_states.REBOOTING_HARD)

    def test_reboot_soft_reboot_started(self):
        self._test_reboot_type_fails('SOFT',
                                     task_state=task_states.REBOOT_STARTED)

    def test_reboot_soft_reboot_pending(self):
        self._test_reboot_type_fails('SOFT',
                                     task_state=task_states.REBOOT_PENDING)

    def test_reboot_soft_rescued(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.RESCUED)

    def test_reboot_soft_error_not_launched(self):
        self._test_reboot_type_fails('SOFT', vm_state=vm_states.ERROR,
                                     launched_at=None)

    def test_reboot_soft_resuming(self):
        self._test_reboot_type_fails('SOFT', task_state=task_states.RESUMING)

    def test_reboot_soft_pausing(self):
        self._test_reboot_type_fails('SOFT', task_state=task_states.PAUSING)

    def test_reboot_soft_unpausing(self):
        self._test_reboot_type_fails('SOFT', task_state=task_states.UNPAUSING)

    def test_reboot_soft_suspending(self):
        self._test_reboot_type_fails('SOFT', task_state=task_states.SUSPENDING)

    def _test_delete_resizing_part(self, inst, deltas):
        old_flavor = inst.old_flavor
        deltas['cores'] = -old_flavor.vcpus
        deltas['ram'] = -old_flavor.memory_mb

    def _test_delete_resized_part(self, inst):
        migration = objects.Migration._from_db_object(
                self.context, objects.Migration(),
                test_migration.fake_db_migration())

        self.mox.StubOutWithMock(objects.Migration,
                                 'get_by_instance_and_status')

        self.context.elevated().AndReturn(self.context)
        objects.Migration.get_by_instance_and_status(
            self.context, inst.uuid, 'finished').AndReturn(migration)
        compute_utils.downsize_quota_delta(self.context,
                                           inst).AndReturn('deltas')
        fake_quotas = objects.Quotas.from_reservations(self.context,
                                                          ['rsvs'])
        compute_utils.reserve_quota_delta(self.context, 'deltas',
                                          inst).AndReturn(fake_quotas)
        self.compute_api._record_action_start(
            self.context, inst, instance_actions.CONFIRM_RESIZE)
        self.compute_api.compute_rpcapi.confirm_resize(
            self.context, inst, migration,
            migration['source_compute'], fake_quotas.reservations, cast=False)

    def _test_delete_shelved_part(self, inst):
        image_api = self.compute_api.image_api
        self.mox.StubOutWithMock(image_api, 'delete')

        snapshot_id = inst.system_metadata.get('shelved_image_id')
        if snapshot_id == SHELVED_IMAGE:
            image_api.delete(self.context, snapshot_id).AndReturn(True)
        elif snapshot_id == SHELVED_IMAGE_NOT_FOUND:
            image_api.delete(self.context, snapshot_id).AndRaise(
                exception.ImageNotFound(image_id=snapshot_id))
        elif snapshot_id == SHELVED_IMAGE_NOT_AUTHORIZED:
            image_api.delete(self.context, snapshot_id).AndRaise(
                exception.ImageNotAuthorized(image_id=snapshot_id))
        elif snapshot_id == SHELVED_IMAGE_EXCEPTION:
            image_api.delete(self.context, snapshot_id).AndRaise(
                test.TestingException("Unexpected error"))

    def _test_downed_host_part(self, inst, updates, delete_time, delete_type):
        compute_utils.notify_about_instance_usage(
            self.compute_api.notifier, self.context, inst,
            '%s.start' % delete_type)
        self.context.elevated().AndReturn(self.context)
        self.compute_api.network_api.deallocate_for_instance(
            self.context, inst)
        state = ('soft' in delete_type and vm_states.SOFT_DELETED or
                 vm_states.DELETED)
        updates.update({'vm_state': state,
                        'task_state': None,
                        'terminated_at': delete_time})
        inst.save()

        updates.update({'deleted_at': delete_time,
                        'deleted': True})
        fake_inst = fake_instance.fake_db_instance(**updates)
        self.compute_api._local_cleanup_bdm_volumes([], inst, self.context)
        db.instance_destroy(self.context, inst.uuid,
                            constraint=None).AndReturn(fake_inst)
        compute_utils.notify_about_instance_usage(
            self.compute_api.notifier,
            self.context, inst, '%s.end' % delete_type,
            system_metadata=inst.system_metadata)

    def _test_delete(self, delete_type, **attrs):
        reservations = ['fake-resv']
        inst = self._create_instance_obj()
        inst.update(attrs)
        inst._context = self.context
        deltas = {'instances': -1,
                  'cores': -inst.flavor.vcpus,
                  'ram': -inst.flavor.memory_mb}
        delete_time = datetime.datetime(1955, 11, 5, 9, 30,
                                        tzinfo=iso8601.iso8601.Utc())
        self.useFixture(utils_fixture.TimeFixture(delete_time))
        task_state = (delete_type == 'soft_delete' and
                      task_states.SOFT_DELETING or task_states.DELETING)
        updates = {'progress': 0, 'task_state': task_state}
        if delete_type == 'soft_delete':
            updates['deleted_at'] = delete_time
        self.mox.StubOutWithMock(inst, 'save')
        self.mox.StubOutWithMock(objects.BlockDeviceMappingList,
                                 'get_by_instance_uuid')
        self.mox.StubOutWithMock(quota.QUOTAS, 'reserve')
        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')
        self.mox.StubOutWithMock(self.compute_api.servicegroup_api,
                                 'service_is_up')
        self.mox.StubOutWithMock(compute_utils, 'downsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(db, 'instance_update_and_get_original')
        self.mox.StubOutWithMock(self.compute_api.network_api,
                                 'deallocate_for_instance')
        self.mox.StubOutWithMock(db, 'instance_system_metadata_get')
        self.mox.StubOutWithMock(db, 'instance_destroy')
        self.mox.StubOutWithMock(compute_utils,
                                 'notify_about_instance_usage')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(quota.QUOTAS, 'rollback')
        rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'confirm_resize')

        if (inst.vm_state in
            (vm_states.SHELVED, vm_states.SHELVED_OFFLOADED)):
            self._test_delete_shelved_part(inst)

        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'terminate_instance')
        self.mox.StubOutWithMock(rpcapi, 'soft_delete_instance')

        objects.BlockDeviceMappingList.get_by_instance_uuid(
            self.context, inst.uuid).AndReturn([])
        inst.save()
        if inst.task_state == task_states.RESIZE_FINISH:
            self._test_delete_resizing_part(inst, deltas)
        quota.QUOTAS.reserve(self.context, project_id=inst.project_id,
                             user_id=inst.user_id,
                             expire=mox.IgnoreArg(),
                             **deltas).AndReturn(reservations)

        # NOTE(comstud): This is getting messy.  But what we are wanting
        # to test is:
        # If cells is enabled and we're the API cell:
        #   * Cast to cells_rpcapi.<method> with reservations=None
        #   * Commit reservations
        # Otherwise:
        #   * Check for downed host
        #   * If downed host:
        #     * Clean up instance, destroying it, sending notifications.
        #       (Tested in _test_downed_host_part())
        #     * Commit reservations
        #   * If not downed host:
        #     * Record the action start.
        #     * Cast to compute_rpcapi.<method> with the reservations

        cast = True
        commit_quotas = True
        soft_delete = False
        if self.cell_type != 'api':
            if inst.vm_state == vm_states.RESIZED:
                self._test_delete_resized_part(inst)
            if inst.vm_state == vm_states.SOFT_DELETED:
                soft_delete = True
            if inst.vm_state != vm_states.SHELVED_OFFLOADED:
                self.context.elevated().AndReturn(self.context)
                objects.Service.get_by_compute_host(self.context,
                        inst.host).AndReturn(objects.Service())
                self.compute_api.servicegroup_api.service_is_up(
                        mox.IsA(objects.Service)).AndReturn(
                                inst.host != 'down-host')

            if (inst.host == 'down-host' or
                    inst.vm_state == vm_states.SHELVED_OFFLOADED):

                self._test_downed_host_part(inst, updates, delete_time,
                                            delete_type)
                cast = False
            else:
                # Happens on the manager side
                commit_quotas = False

        if cast:
            if self.cell_type != 'api':
                self.compute_api._record_action_start(self.context, inst,
                                                      instance_actions.DELETE)
            if commit_quotas or soft_delete:
                cast_reservations = None
            else:
                cast_reservations = reservations
            if delete_type == 'soft_delete':
                rpcapi.soft_delete_instance(self.context, inst,
                                            reservations=cast_reservations)
            elif delete_type in ['delete', 'force_delete']:
                rpcapi.terminate_instance(self.context, inst, [],
                                          reservations=cast_reservations,
                                          delete_type=delete_type)

        if soft_delete:
            quota.QUOTAS.rollback(self.context, reservations,
                                  project_id=inst.project_id,
                                  user_id=inst.user_id)

        if commit_quotas:
            # Local delete or when we're testing API cell.
            quota.QUOTAS.commit(self.context, reservations,
                                project_id=inst.project_id,
                                user_id=inst.user_id)

        self.mox.ReplayAll()

        getattr(self.compute_api, delete_type)(self.context, inst)
        for k, v in updates.items():
            self.assertEqual(inst[k], v)

        self.mox.UnsetStubs()

    def test_delete(self):
        self._test_delete('delete')

    def test_delete_if_not_launched(self):
        self._test_delete('delete', launched_at=None)

    def test_delete_in_resizing(self):
        old_flavor = objects.Flavor(vcpus=1, memory_mb=512, extra_specs={})
        self._test_delete('delete',
                          task_state=task_states.RESIZE_FINISH,
                          old_flavor=old_flavor)

    def test_delete_in_resized(self):
        self._test_delete('delete', vm_state=vm_states.RESIZED)

    def test_delete_shelved(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE}
        self._test_delete('delete',
                          vm_state=vm_states.SHELVED,
                          system_metadata=fake_sys_meta)

    def test_delete_shelved_offloaded(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE}
        self._test_delete('delete',
                          vm_state=vm_states.SHELVED_OFFLOADED,
                          system_metadata=fake_sys_meta)

    def test_delete_shelved_image_not_found(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE_NOT_FOUND}
        self._test_delete('delete',
                          vm_state=vm_states.SHELVED_OFFLOADED,
                          system_metadata=fake_sys_meta)

    def test_delete_shelved_image_not_authorized(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE_NOT_AUTHORIZED}
        self._test_delete('delete',
                          vm_state=vm_states.SHELVED_OFFLOADED,
                          system_metadata=fake_sys_meta)

    def test_delete_shelved_exception(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE_EXCEPTION}
        self._test_delete('delete',
                          vm_state=vm_states.SHELVED,
                          system_metadata=fake_sys_meta)

    def test_delete_with_down_host(self):
        self._test_delete('delete', host='down-host')

    def test_delete_soft_with_down_host(self):
        self._test_delete('soft_delete', host='down-host')

    def test_delete_soft(self):
        self._test_delete('soft_delete')

    def test_delete_forced(self):
        fake_sys_meta = {'shelved_image_id': SHELVED_IMAGE}
        for vm_state in self._get_vm_states():
            if vm_state in (vm_states.SHELVED, vm_states.SHELVED_OFFLOADED):
                self._test_delete('force_delete',
                                  vm_state=vm_state,
                                  system_metadata=fake_sys_meta)
            self._test_delete('force_delete', vm_state=vm_state)

    def test_delete_fast_if_host_not_set(self):
        inst = self._create_instance_obj()
        inst.host = ''
        quotas = quotas_obj.Quotas(self.context)
        updates = {'progress': 0, 'task_state': task_states.DELETING}

        self.mox.StubOutWithMock(objects.BuildRequest,
                                 'get_by_instance_uuid')
        self.mox.StubOutWithMock(inst, 'save')
        self.mox.StubOutWithMock(objects.BlockDeviceMappingList,
                                 'get_by_instance_uuid')

        self.mox.StubOutWithMock(db, 'constraint')
        self.mox.StubOutWithMock(db, 'instance_destroy')
        self.mox.StubOutWithMock(self.compute_api, '_create_reservations')
        self.mox.StubOutWithMock(self.compute_api, '_lookup_instance')
        self.mox.StubOutWithMock(compute_utils,
                                 'notify_about_instance_usage')
        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'terminate_instance')

        self.compute_api._lookup_instance(self.context,
                                          inst.uuid).AndReturn(inst)
        objects.BlockDeviceMappingList.get_by_instance_uuid(
            self.context, inst.uuid).AndReturn(
                objects.BlockDeviceMappingList())
        objects.BuildRequest.get_by_instance_uuid(
            self.context, inst.uuid).AndRaise(
                exception.BuildRequestNotFound(uuid=inst.uuid))
        inst.save()
        self.compute_api._create_reservations(self.context,
                                              inst, inst.task_state,
                                              inst.project_id, inst.user_id
                                              ).AndReturn(quotas)

        if self.cell_type == 'api':
            rpcapi.terminate_instance(
                    self.context, inst,
                    mox.IsA(objects.BlockDeviceMappingList),
                    reservations=None, delete_type='delete')
        else:
            compute_utils.notify_about_instance_usage(
                    self.compute_api.notifier, self.context,
                    inst, 'delete.start')
            db.constraint(host=mox.IgnoreArg()).AndReturn('constraint')
            delete_time = datetime.datetime(1955, 11, 5, 9, 30,
                                            tzinfo=iso8601.iso8601.Utc())
            updates['deleted_at'] = delete_time
            updates['deleted'] = True
            fake_inst = fake_instance.fake_db_instance(**updates)
            db.instance_destroy(self.context, inst.uuid,
                                constraint='constraint').AndReturn(fake_inst)
            compute_utils.notify_about_instance_usage(
                    self.compute_api.notifier, self.context,
                    inst, 'delete.end',
                    system_metadata=inst.system_metadata)

        self.mox.ReplayAll()

        self.compute_api.delete(self.context, inst)
        for k, v in updates.items():
            self.assertEqual(inst[k], v)

    def _fake_do_delete(context, instance, bdms,
                        rservations=None, local=False):
        pass

    def test_local_delete_with_deleted_volume(self):
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {'id': 42, 'volume_id': 'volume_id',
                 'source_type': 'volume', 'destination_type': 'volume',
                 'delete_on_termination': False}))]

        inst = self._create_instance_obj()
        inst._context = self.context

        self.mox.StubOutWithMock(inst, 'destroy')
        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(self.compute_api.network_api,
                                 'deallocate_for_instance')
        self.mox.StubOutWithMock(db, 'instance_system_metadata_get')
        self.mox.StubOutWithMock(compute_utils,
                                 'notify_about_instance_usage')
        self.mox.StubOutWithMock(self.compute_api.volume_api,
                                 'detach')
        self.mox.StubOutWithMock(objects.BlockDeviceMapping, 'destroy')

        compute_utils.notify_about_instance_usage(
                    self.compute_api.notifier, self.context,
                    inst, 'delete.start')
        self.context.elevated().MultipleTimes().AndReturn(self.context)
        if self.cell_type != 'api':
            self.compute_api.network_api.deallocate_for_instance(
                        self.context, inst)

        self.compute_api.volume_api.detach(
            mox.IgnoreArg(), 'volume_id', inst.uuid).\
               AndRaise(exception.VolumeNotFound('volume_id'))
        bdms[0].destroy()

        inst.destroy()
        compute_utils.notify_about_instance_usage(
                    self.compute_api.notifier, self.context,
                    inst, 'delete.end',
                    system_metadata=inst.system_metadata)

        self.mox.ReplayAll()
        self.compute_api._local_delete(self.context, inst, bdms,
                                       'delete',
                                       self._fake_do_delete)

    @mock.patch.object(objects.BlockDeviceMapping, 'destroy')
    def test_local_cleanup_bdm_volumes_stashed_connector(self, mock_destroy):
        """Tests that we call volume_api.terminate_connection when we found
        a stashed connector in the bdm.connection_info dict.
        """
        inst = self._create_instance_obj()
        # create two fake bdms, one is a volume and one isn't, both will be
        # destroyed but we only cleanup the volume bdm in cinder
        conn_info = {'connector': {'host': inst.host}}
        vol_bdm = objects.BlockDeviceMapping(self.context, id=1,
                                             instance_uuid=inst.uuid,
                                             volume_id=uuids.volume_id,
                                             source_type='volume',
                                             destination_type='volume',
                                             delete_on_termination=True,
                                             connection_info=jsonutils.dumps(
                                                conn_info
                                             ))
        loc_bdm = objects.BlockDeviceMapping(self.context, id=2,
                                             instance_uuid=inst.uuid,
                                             volume_id=uuids.volume_id2,
                                             source_type='blank',
                                             destination_type='local')
        bdms = objects.BlockDeviceMappingList(objects=[vol_bdm, loc_bdm])

        @mock.patch.object(self.compute_api.volume_api, 'terminate_connection')
        @mock.patch.object(self.compute_api.volume_api, 'detach')
        @mock.patch.object(self.compute_api.volume_api, 'delete')
        @mock.patch.object(self.context, 'elevated', return_value=self.context)
        def do_test(self, mock_elevated, mock_delete,
                    mock_detach, mock_terminate):
            self.compute_api._local_cleanup_bdm_volumes(
                bdms, inst, self.context)
            mock_terminate.assert_called_once_with(
                self.context, uuids.volume_id, conn_info['connector'])
            mock_detach.assert_called_once_with(
                self.context, uuids.volume_id, inst.uuid)
            mock_delete.assert_called_once_with(self.context, uuids.volume_id)
            self.assertEqual(2, mock_destroy.call_count)

        do_test(self)

    def test_get_stashed_volume_connector_none(self):
        inst = self._create_instance_obj()
        # connection_info isn't set
        bdm = objects.BlockDeviceMapping(self.context)
        self.assertIsNone(
            self.compute_api._get_stashed_volume_connector(bdm, inst))
        # connection_info is None
        bdm.connection_info = None
        self.assertIsNone(
            self.compute_api._get_stashed_volume_connector(bdm, inst))
        # connector is not set in connection_info
        bdm.connection_info = jsonutils.dumps({})
        self.assertIsNone(
            self.compute_api._get_stashed_volume_connector(bdm, inst))
        # connector is set but different host
        conn_info = {'connector': {'host': 'other_host'}}
        bdm.connection_info = jsonutils.dumps(conn_info)
        self.assertIsNone(
            self.compute_api._get_stashed_volume_connector(bdm, inst))

    def test_local_delete_without_info_cache(self):
        inst = self._create_instance_obj()

        with test.nested(
            mock.patch.object(inst, 'destroy'),
            mock.patch.object(self.context, 'elevated'),
            mock.patch.object(self.compute_api.network_api,
                              'deallocate_for_instance'),
            mock.patch.object(db, 'instance_system_metadata_get'),
            mock.patch.object(compute_utils,
                              'notify_about_instance_usage')
        ) as (
            inst_destroy, context_elevated, net_api_deallocate_for_instance,
            db_instance_system_metadata_get, notify_about_instance_usage
        ):

            compute_utils.notify_about_instance_usage(
                        self.compute_api.notifier, self.context,
                        inst, 'delete.start')
            self.context.elevated().MultipleTimes().AndReturn(self.context)
            if self.cell_type != 'api':
                self.compute_api.network_api.deallocate_for_instance(
                            self.context, inst)

            inst.destroy()
            compute_utils.notify_about_instance_usage(
                        self.compute_api.notifier, self.context,
                        inst, 'delete.end',
                        system_metadata=inst.system_metadata)
            inst.info_cache = None
            self.compute_api._local_delete(self.context, inst, [],
                                           'delete',
                                           self._fake_do_delete)

    def test_delete_disabled(self):
        inst = self._create_instance_obj()
        inst.disable_terminate = True
        self.mox.StubOutWithMock(db, 'instance_update_and_get_original')
        self.mox.ReplayAll()
        self.compute_api.delete(self.context, inst)

    def test_delete_soft_rollback(self):
        inst = self._create_instance_obj()
        self.mox.StubOutWithMock(objects.BlockDeviceMappingList,
                                 'get_by_instance_uuid')
        self.mox.StubOutWithMock(inst, 'save')

        delete_time = datetime.datetime(1955, 11, 5)
        self.useFixture(utils_fixture.TimeFixture(delete_time))

        objects.BlockDeviceMappingList.get_by_instance_uuid(
            self.context, inst.uuid).AndReturn(
                objects.BlockDeviceMappingList())
        inst.save().AndRaise(test.TestingException)

        self.mox.ReplayAll()

        self.assertRaises(test.TestingException,
                          self.compute_api.soft_delete, self.context, inst)

    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_attempt_delete_of_buildrequest_success(self, mock_get_by_inst):
        build_req_mock = mock.MagicMock()
        mock_get_by_inst.return_value = build_req_mock

        inst = self._create_instance_obj()
        self.assertTrue(
            self.compute_api._attempt_delete_of_buildrequest(self.context,
                                                             inst))
        self.assertTrue(build_req_mock.destroy.called)

    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_attempt_delete_of_buildrequest_not_found(self, mock_get_by_inst):
        mock_get_by_inst.side_effect = exception.BuildRequestNotFound(
                                                                uuid='fake')

        inst = self._create_instance_obj()
        self.assertFalse(
            self.compute_api._attempt_delete_of_buildrequest(self.context,
                                                             inst))

    def test_attempt_delete_of_buildrequest_already_deleted(self):
        inst = self._create_instance_obj()
        build_req_mock = mock.MagicMock()
        build_req_mock.destroy.side_effect = exception.BuildRequestNotFound(
                                                                uuid='fake')
        with mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid',
                               return_value=build_req_mock):
            self.assertFalse(
                self.compute_api._attempt_delete_of_buildrequest(self.context,
                                                                 inst))
            self.assertTrue(build_req_mock.destroy.called)

    @mock.patch.object(objects.Service, 'get_minimum_version', return_value=0)
    def test_delete_while_booting_low_service_version(self,
            mock_get_service_version):
        inst = self._create_instance_obj()
        with mock.patch.object(self.compute_api,
                   '_attempt_delete_of_buildrequest') as mock_attempt_delete:
            self.assertFalse(
                self.compute_api._delete_while_booting(self.context, inst))
            self.assertTrue(mock_attempt_delete.called)
        mock_get_service_version.assert_called_once_with(self.context,
                                                         'nova-osapi_compute')

    def test_delete_while_booting_buildreq_not_deleted(self):
        self.useFixture(fixtures.AllServicesCurrent())
        inst = self._create_instance_obj()
        with mock.patch.object(self.compute_api,
                               '_attempt_delete_of_buildrequest',
                               return_value=False):
            self.assertFalse(
                self.compute_api._delete_while_booting(self.context, inst))

    def test_delete_while_booting_buildreq_deleted_instance_none(self):
        self.useFixture(fixtures.AllServicesCurrent())
        inst = self._create_instance_obj()
        quota_mock = mock.MagicMock()

        @mock.patch.object(self.compute_api, '_attempt_delete_of_buildrequest',
                           return_value=True)
        @mock.patch.object(self.compute_api, '_lookup_instance',
                           return_value=None)
        @mock.patch.object(self.compute_api, '_create_reservations',
                           return_value=quota_mock)
        def test(mock_create_res, mock_lookup, mock_attempt):
            self.assertTrue(
                self.compute_api._delete_while_booting(self.context,
                                                       inst))
            self.assertTrue(quota_mock.commit.called)

        test()

    def test_delete_while_booting_buildreq_deleted_instance_not_found(self):
        self.useFixture(fixtures.AllServicesCurrent())
        inst = self._create_instance_obj()
        quota_mock = mock.MagicMock()

        @mock.patch.object(self.compute_api, '_attempt_delete_of_buildrequest',
                           return_value=True)
        @mock.patch.object(self.compute_api, '_lookup_instance',
                           side_effect=exception.InstanceNotFound(
                               instance_id='fake'))
        @mock.patch.object(self.compute_api, '_create_reservations',
                           return_value=quota_mock)
        def test(mock_create_res, mock_lookup, mock_attempt):
            self.assertTrue(
                self.compute_api._delete_while_booting(self.context,
                                                       inst))
            self.assertTrue(quota_mock.commit.called)
            self.assertTrue(quota_mock.rollback.called)

        test()

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid',
                       side_effect=exception.InstanceMappingNotFound(
                           uuid='fake'))
    def test_lookup_instance_mapping_none(self, mock_map_get,
                                          mock_target_cell):
        instance = self._create_instance_obj()
        with mock.patch.object(objects.Instance, 'get_by_uuid',
                               return_value=instance) as mock_inst_get:

            ret_instance = self.compute_api._lookup_instance(self.context,
                                                             instance.uuid)
            self.assertEqual(instance, ret_instance)
            mock_inst_get.assert_called_once_with(self.context, instance.uuid)
            self.assertFalse(mock_target_cell.called)

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid',
                       return_value=objects.InstanceMapping(cell_mapping=None))
    def test_lookup_instance_cell_mapping_none(self, mock_map_get,
                                          mock_target_cell):
        instance = self._create_instance_obj()
        with mock.patch.object(objects.Instance, 'get_by_uuid',
                               return_value=instance) as mock_inst_get:

            ret_instance = self.compute_api._lookup_instance(self.context,
                                                             instance.uuid)
            self.assertEqual(instance, ret_instance)
            mock_inst_get.assert_called_once_with(self.context, instance.uuid)
            self.assertFalse(mock_target_cell.called)

    @mock.patch.object(context, 'target_cell')
    def test_lookup_instance_cell_mapping(self, mock_target_cell):
        instance = self._create_instance_obj()

        inst_map = objects.InstanceMapping(
            cell_mapping=objects.CellMapping(database_connection='',
                                             transport_url='none'))

        @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid',
                           return_value=inst_map)
        @mock.patch.object(objects.Instance, 'get_by_uuid',
                           return_value=instance)
        def test(mock_inst_get, mock_map_get):
            ret_instance = self.compute_api._lookup_instance(self.context,
                                                             instance.uuid)
            self.assertEqual(instance, ret_instance)
            mock_inst_get.assert_called_once_with(self.context, instance.uuid)
            mock_target_cell.assert_called_once_with(self.context,
                                                     inst_map.cell_mapping)

        test()

    def _test_confirm_resize(self, mig_ref_passed=False):
        params = dict(vm_state=vm_states.RESIZED)
        fake_inst = self._create_instance_obj(params=params)
        fake_mig = objects.Migration._from_db_object(
                self.context, objects.Migration(),
                test_migration.fake_db_migration())

        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(objects.Migration,
                                 'get_by_instance_and_status')
        self.mox.StubOutWithMock(compute_utils, 'downsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(fake_mig, 'save')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                                 'confirm_resize')

        self.context.elevated().AndReturn(self.context)
        if not mig_ref_passed:
            objects.Migration.get_by_instance_and_status(
                    self.context, fake_inst['uuid'], 'finished').AndReturn(
                            fake_mig)
        compute_utils.downsize_quota_delta(self.context,
                                           fake_inst).AndReturn('deltas')

        resvs = ['resvs']
        fake_quotas = objects.Quotas.from_reservations(self.context, resvs)

        compute_utils.reserve_quota_delta(self.context, 'deltas',
                                          fake_inst).AndReturn(fake_quotas)

        def _check_mig(expected_task_state=None):
            self.assertEqual('confirming', fake_mig.status)

        fake_mig.save().WithSideEffects(_check_mig)

        if self.cell_type:
            quota.QUOTAS.commit(self.context, resvs, project_id=None,
                                user_id=None)

        self.compute_api._record_action_start(self.context, fake_inst,
                                              'confirmResize')

        self.compute_api.compute_rpcapi.confirm_resize(
                self.context, fake_inst, fake_mig, 'compute-source',
                [] if self.cell_type else fake_quotas.reservations)

        self.mox.ReplayAll()

        if mig_ref_passed:
            self.compute_api.confirm_resize(self.context, fake_inst,
                                            migration=fake_mig)
        else:
            self.compute_api.confirm_resize(self.context, fake_inst)

    def test_confirm_resize(self):
        self._test_confirm_resize()

    def test_confirm_resize_with_migration_ref(self):
        self._test_confirm_resize(mig_ref_passed=True)

    def _test_revert_resize(self):
        params = dict(vm_state=vm_states.RESIZED)
        fake_inst = self._create_instance_obj(params=params)
        fake_mig = objects.Migration._from_db_object(
                self.context, objects.Migration(),
                test_migration.fake_db_migration())

        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(objects.Migration,
                                 'get_by_instance_and_status')
        self.mox.StubOutWithMock(compute_utils,
                                 'reverse_upsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(fake_inst, 'save')
        self.mox.StubOutWithMock(fake_mig, 'save')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                                 'revert_resize')

        self.context.elevated().AndReturn(self.context)
        objects.Migration.get_by_instance_and_status(
                self.context, fake_inst['uuid'], 'finished').AndReturn(
                        fake_mig)
        compute_utils.reverse_upsize_quota_delta(
            self.context, fake_inst).AndReturn('deltas')

        resvs = ['resvs']
        fake_quotas = objects.Quotas.from_reservations(self.context, resvs)

        compute_utils.reserve_quota_delta(self.context, 'deltas',
                                          fake_inst).AndReturn(fake_quotas)

        def _check_state(expected_task_state=None):
            self.assertEqual(task_states.RESIZE_REVERTING,
                             fake_inst.task_state)

        fake_inst.save(expected_task_state=[None]).WithSideEffects(
                _check_state)

        def _check_mig(expected_task_state=None):
            self.assertEqual('reverting', fake_mig.status)

        fake_mig.save().WithSideEffects(_check_mig)

        if self.cell_type:
            quota.QUOTAS.commit(self.context, resvs, project_id=None,
                                user_id=None)

        self.compute_api._record_action_start(self.context, fake_inst,
                                              'revertResize')

        self.compute_api.compute_rpcapi.revert_resize(
                self.context, fake_inst, fake_mig, 'compute-dest',
                [] if self.cell_type else fake_quotas.reservations)

        self.mox.ReplayAll()

        self.compute_api.revert_resize(self.context, fake_inst)

    def test_revert_resize(self):
        self._test_revert_resize()

    def test_revert_resize_concurrent_fail(self):
        params = dict(vm_state=vm_states.RESIZED)
        fake_inst = self._create_instance_obj(params=params)
        fake_mig = objects.Migration._from_db_object(
                self.context, objects.Migration(),
                test_migration.fake_db_migration())

        self.mox.StubOutWithMock(self.context, 'elevated')
        self.mox.StubOutWithMock(objects.Migration,
                                 'get_by_instance_and_status')
        self.mox.StubOutWithMock(compute_utils,
                                 'reverse_upsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(fake_inst, 'save')
        self.mox.StubOutWithMock(quota.QUOTAS, 'rollback')

        self.context.elevated().AndReturn(self.context)
        objects.Migration.get_by_instance_and_status(
            self.context, fake_inst['uuid'], 'finished').AndReturn(fake_mig)

        delta = ['delta']
        compute_utils.reverse_upsize_quota_delta(
            self.context, fake_inst).AndReturn(delta)
        resvs = ['resvs']
        fake_quotas = objects.Quotas.from_reservations(self.context, resvs)
        compute_utils.reserve_quota_delta(
            self.context, delta, fake_inst).AndReturn(fake_quotas)

        exc = exception.UnexpectedTaskStateError(
            instance_uuid=fake_inst['uuid'],
            actual={'task_state': task_states.RESIZE_REVERTING},
            expected={'task_state': [None]})
        fake_inst.save(expected_task_state=[None]).AndRaise(exc)

        quota.QUOTAS.rollback(self.context, resvs, project_id=None,
                              user_id=None)

        self.mox.ReplayAll()
        self.assertRaises(exception.UnexpectedTaskStateError,
                          self.compute_api.revert_resize,
                          self.context,
                          fake_inst)

    def _test_resize(self, flavor_id_passed=True,
                     same_host=False, allow_same_host=False,
                     project_id=None,
                     extra_kwargs=None,
                     same_flavor=False,
                     clean_shutdown=True):
        if extra_kwargs is None:
            extra_kwargs = {}

        self.flags(allow_resize_to_same_host=allow_same_host)

        params = {}
        if project_id is not None:
            # To test instance w/ different project id than context (admin)
            params['project_id'] = project_id
        fake_inst = self._create_instance_obj(params=params)

        self.mox.StubOutWithMock(flavors, 'get_flavor_by_flavor_id')
        self.mox.StubOutWithMock(compute_utils, 'upsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(fake_inst, 'save')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(objects.RequestSpec, 'get_by_instance_uuid')
        self.mox.StubOutWithMock(self.compute_api.compute_task_api,
                                 'resize_instance')

        current_flavor = fake_inst.get_flavor()
        if flavor_id_passed:
            new_flavor = self._create_flavor(id=200, flavorid='new-flavor-id',
                                name='new_flavor', disabled=False)
            if same_flavor:
                new_flavor.id = current_flavor.id
            flavors.get_flavor_by_flavor_id(
                    'new-flavor-id',
                    read_deleted='no').AndReturn(new_flavor)
        else:
            new_flavor = current_flavor

        if (self.cell_type == 'compute' or
                not (flavor_id_passed and same_flavor)):
            resvs = ['resvs']
            project_id, user_id = quotas_obj.ids_from_instance(self.context,
                                                               fake_inst)
            fake_quotas = objects.Quotas.from_reservations(self.context,
                                                           resvs)
            if flavor_id_passed:
                compute_utils.upsize_quota_delta(
                    self.context, mox.IsA(objects.Flavor),
                    mox.IsA(objects.Flavor)).AndReturn('deltas')
                compute_utils.reserve_quota_delta(
                    self.context, 'deltas', fake_inst).AndReturn(fake_quotas)

            def _check_state(expected_task_state=None):
                self.assertEqual(task_states.RESIZE_PREP,
                                 fake_inst.task_state)
                self.assertEqual(fake_inst.progress, 0)
                for key, value in extra_kwargs.items():
                    self.assertEqual(value, getattr(fake_inst, key))

            fake_inst.save(expected_task_state=[None]).WithSideEffects(
                    _check_state)

            if allow_same_host:
                filter_properties = {'ignore_hosts': []}
            else:
                filter_properties = {'ignore_hosts': [fake_inst['host']]}

            if flavor_id_passed:
                expected_reservations = fake_quotas.reservations
            else:
                expected_reservations = []
            if self.cell_type == 'api':
                if flavor_id_passed:
                    quota.QUOTAS.commit(self.context, resvs, project_id=None,
                                        user_id=None)
                expected_reservations = []
                mig = objects.Migration()

                def _get_migration(context=None):
                    return mig

                def _check_mig():
                    self.assertEqual(fake_inst.uuid, mig.instance_uuid)
                    self.assertEqual(current_flavor.id,
                                     mig.old_instance_type_id)
                    self.assertEqual(new_flavor.id,
                                     mig.new_instance_type_id)
                    self.assertEqual('finished', mig.status)
                    if new_flavor.id != current_flavor.id:
                        self.assertEqual('resize', mig.migration_type)
                    else:
                        self.assertEqual('migration', mig.migration_type)

                self.stubs.Set(objects, 'Migration', _get_migration)
                self.mox.StubOutWithMock(self.context, 'elevated')
                self.mox.StubOutWithMock(mig, 'create')

                self.context.elevated().AndReturn(self.context)
                mig.create().WithSideEffects(_check_mig)

            if flavor_id_passed:
                self.compute_api._record_action_start(self.context, fake_inst,
                                                      'resize')
            else:
                self.compute_api._record_action_start(self.context, fake_inst,
                                                      'migrate')

            fake_spec = objects.RequestSpec()
            objects.RequestSpec.get_by_instance_uuid(
                self.context, fake_inst.uuid).AndReturn(fake_spec)

            scheduler_hint = {'filter_properties': filter_properties}

            self.compute_api.compute_task_api.resize_instance(
                    self.context, fake_inst, extra_kwargs,
                    scheduler_hint=scheduler_hint,
                    flavor=mox.IsA(objects.Flavor),
                    reservations=expected_reservations,
                    clean_shutdown=clean_shutdown,
                    request_spec=fake_spec)

        self.mox.ReplayAll()

        if flavor_id_passed:
            self.compute_api.resize(self.context, fake_inst,
                                    flavor_id='new-flavor-id',
                                    clean_shutdown=clean_shutdown,
                                    **extra_kwargs)
        else:
            self.compute_api.resize(self.context, fake_inst,
                                    clean_shutdown=clean_shutdown,
                                    **extra_kwargs)

        if allow_same_host:
            self.assertEqual([], fake_spec.ignore_hosts)
        else:
            self.assertEqual([fake_inst['host']], fake_spec.ignore_hosts)

    def _test_migrate(self, *args, **kwargs):
        self._test_resize(*args, flavor_id_passed=False, **kwargs)

    def test_resize(self):
        self._test_resize()

    def test_resize_with_kwargs(self):
        self._test_resize(extra_kwargs=dict(cow='moo'))

    def test_resize_same_host_and_allowed(self):
        self._test_resize(same_host=True, allow_same_host=True)

    def test_resize_same_host_and_not_allowed(self):
        self._test_resize(same_host=True, allow_same_host=False)

    def test_resize_different_project_id(self):
        self._test_resize(project_id='different')

    def test_resize_forced_shutdown(self):
        self._test_resize(clean_shutdown=False)

    def test_migrate(self):
        self._test_migrate()

    def test_migrate_with_kwargs(self):
        self._test_migrate(extra_kwargs=dict(cow='moo'))

    def test_migrate_same_host_and_allowed(self):
        self._test_migrate(same_host=True, allow_same_host=True)

    def test_migrate_same_host_and_not_allowed(self):
        self._test_migrate(same_host=True, allow_same_host=False)

    def test_migrate_different_project_id(self):
        self._test_migrate(project_id='different')

    def test_resize_invalid_flavor_fails(self):
        self.mox.StubOutWithMock(flavors, 'get_flavor_by_flavor_id')
        # Should never reach these.
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(self.compute_api.compute_task_api,
                                 'resize_instance')

        fake_inst = self._create_instance_obj()
        exc = exception.FlavorNotFound(flavor_id='flavor-id')

        flavors.get_flavor_by_flavor_id('flavor-id',
                                        read_deleted='no').AndRaise(exc)

        self.mox.ReplayAll()

        with mock.patch.object(fake_inst, 'save') as mock_save:
            self.assertRaises(exception.FlavorNotFound,
                              self.compute_api.resize, self.context,
                              fake_inst, flavor_id='flavor-id')
            self.assertFalse(mock_save.called)

    def test_resize_disabled_flavor_fails(self):
        self.mox.StubOutWithMock(flavors, 'get_flavor_by_flavor_id')
        # Should never reach these.
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(self.compute_api.compute_task_api,
                                 'resize_instance')

        fake_inst = self._create_instance_obj()
        fake_flavor = self._create_flavor(id=200, flavorid='flavor-id',
                            name='foo', disabled=True)

        flavors.get_flavor_by_flavor_id(
                'flavor-id', read_deleted='no').AndReturn(fake_flavor)

        self.mox.ReplayAll()

        with mock.patch.object(fake_inst, 'save') as mock_save:
            self.assertRaises(exception.FlavorNotFound,
                              self.compute_api.resize, self.context,
                              fake_inst, flavor_id='flavor-id')
            self.assertFalse(mock_save.called)

    @mock.patch.object(flavors, 'get_flavor_by_flavor_id')
    def test_resize_to_zero_disk_flavor_fails(self, get_flavor_by_flavor_id):
        fake_inst = self._create_instance_obj()
        fake_flavor = self._create_flavor(id=200, flavorid='flavor-id',
                            name='foo', root_gb=0)

        get_flavor_by_flavor_id.return_value = fake_flavor

        with mock.patch.object(compute_utils, 'is_volume_backed_instance',
                               return_value=False):
            self.assertRaises(exception.CannotResizeDisk,
                              self.compute_api.resize, self.context,
                              fake_inst, flavor_id='flavor-id')

    @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch('nova.compute.api.API._record_action_start')
    @mock.patch('nova.compute.api.API._resize_cells_support')
    @mock.patch('nova.conductor.conductor_api.ComputeTaskAPI.resize_instance')
    @mock.patch.object(flavors, 'get_flavor_by_flavor_id')
    def test_resize_to_zero_disk_flavor_volume_backed(self,
                                                      get_flavor_by_flavor_id,
                                                      resize_instance_mock,
                                                      cells_support_mock,
                                                      record_mock,
                                                      get_by_inst):
        params = dict(image_ref='')
        fake_inst = self._create_instance_obj(params=params)

        fake_flavor = self._create_flavor(id=200, flavorid='flavor-id',
                                          name='foo', root_gb=0)

        get_flavor_by_flavor_id.return_value = fake_flavor

        @mock.patch.object(compute_utils, 'is_volume_backed_instance',
                           return_value=True)
        @mock.patch.object(fake_inst, 'save')
        def do_test(mock_save, mock_volume):
            self.compute_api.resize(self.context, fake_inst,
                                    flavor_id='flavor-id')
            mock_volume.assert_called_once_with(self.context, fake_inst)

        do_test()

    def test_resize_quota_exceeds_fails(self):
        self.mox.StubOutWithMock(flavors, 'get_flavor_by_flavor_id')
        self.mox.StubOutWithMock(compute_utils, 'upsize_quota_delta')
        self.mox.StubOutWithMock(compute_utils, 'reserve_quota_delta')
        # Should never reach these.
        self.mox.StubOutWithMock(quota.QUOTAS, 'commit')
        self.mox.StubOutWithMock(self.compute_api, '_record_action_start')
        self.mox.StubOutWithMock(self.compute_api.compute_task_api,
                                 'resize_instance')

        fake_inst = self._create_instance_obj()
        fake_flavor = self._create_flavor(id=200, flavorid='flavor-id',
                            name='foo', disabled=False)
        flavors.get_flavor_by_flavor_id(
                'flavor-id', read_deleted='no').AndReturn(fake_flavor)
        deltas = dict(resource=0)
        compute_utils.upsize_quota_delta(
            self.context, mox.IsA(objects.Flavor),
            mox.IsA(objects.Flavor)).AndReturn(deltas)
        usage = dict(in_use=0, reserved=0)
        quotas = {'resource': 0}
        usages = {'resource': usage}
        overs = ['resource']
        over_quota_args = dict(quotas=quotas,
                               usages=usages,
                               overs=overs)

        compute_utils.reserve_quota_delta(self.context, deltas,
                                          fake_inst).AndRaise(
            exception.OverQuota(**over_quota_args))

        self.mox.ReplayAll()

        with mock.patch.object(fake_inst, 'save') as mock_save:
            self.assertRaises(exception.TooManyInstances,
                              self.compute_api.resize, self.context,
                              fake_inst, flavor_id='flavor-id')
            self.assertFalse(mock_save.called)

    @mock.patch.object(flavors, 'get_flavor_by_flavor_id')
    @mock.patch.object(compute_utils, 'upsize_quota_delta')
    @mock.patch.object(compute_utils, 'reserve_quota_delta')
    def test_resize_quota_exceeds_fails_instance(self, mock_reserve,
                                                 mock_upsize, mock_flavor):
        fake_inst = self._create_instance_obj()
        fake_flavor = self._create_flavor(id=200, flavorid='flavor-id',
                            name='foo', disabled=False)
        mock_flavor.return_value = fake_flavor
        deltas = dict(cores=1, ram=1)
        mock_upsize.return_value = deltas
        usage = dict(in_use=0, reserved=0)
        quotas = {'instances': 1, 'cores': -1, 'ram': -1}
        usages = {'instances': usage, 'cores': usage, 'ram': usage}
        overs = ['ram']
        over_quota_args = dict(quotas=quotas,
                               usages=usages,
                               overs=overs)
        mock_reserve.side_effect = exception.OverQuota(**over_quota_args)

        with mock.patch.object(fake_inst, 'save') as mock_save:
            self.assertRaises(exception.TooManyInstances,
                              self.compute_api.resize, self.context,
                              fake_inst, flavor_id='flavor-id')
            self.assertFalse(mock_save.called)

    def test_check_instance_quota_exceeds_with_multiple_resources(self):
        quotas = {'cores': 1, 'instances': 1, 'ram': 512}
        usages = {'cores': dict(in_use=1, reserved=0),
                  'instances': dict(in_use=1, reserved=0),
                  'ram': dict(in_use=512, reserved=0)}
        overs = ['cores', 'instances', 'ram']
        over_quota_args = dict(quotas=quotas,
                               usages=usages,
                               overs=overs)
        e = exception.OverQuota(**over_quota_args)
        fake_flavor = self._create_flavor()
        instance_num = 1
        with mock.patch.object(objects.Quotas, 'reserve', side_effect=e):
            try:
                self.compute_api._check_num_instances_quota(self.context,
                                                            fake_flavor,
                                                            instance_num,
                                                            instance_num)
            except exception.TooManyInstances as e:
                self.assertEqual('cores, instances, ram', e.kwargs['overs'])
                self.assertEqual('1, 1, 512', e.kwargs['req'])
                self.assertEqual('1, 1, 512', e.kwargs['used'])
                self.assertEqual('1, 1, 512', e.kwargs['allowed'])
            else:
                self.fail("Exception not raised")

    @mock.patch.object(flavors, 'get_flavor_by_flavor_id')
    @mock.patch.object(objects.Quotas, 'reserve')
    def test_resize_instance_quota_exceeds_with_multiple_resources(
            self, mock_reserve, mock_get_flavor):
        quotas = {'cores': 1, 'ram': 512}
        usages = {'cores': dict(in_use=1, reserved=0),
                  'ram': dict(in_use=512, reserved=0)}
        overs = ['cores', 'ram']
        over_quota_args = dict(quotas=quotas,
                               usages=usages,
                               overs=overs)

        mock_reserve.side_effect = exception.OverQuota(**over_quota_args)
        mock_get_flavor.return_value = self._create_flavor(id=333,
                                                           vcpus=3,
                                                           memory_mb=1536)
        try:
            self.compute_api.resize(self.context, self._create_instance_obj(),
                                    'fake_flavor_id')
        except exception.TooManyInstances as e:
            self.assertEqual('cores, ram', e.kwargs['overs'])
            self.assertEqual('2, 1024', e.kwargs['req'])
            self.assertEqual('1, 512', e.kwargs['used'])
            self.assertEqual('1, 512', e.kwargs['allowed'])
            mock_get_flavor.assert_called_once_with('fake_flavor_id',
                                                    read_deleted="no")
        else:
            self.fail("Exception not raised")

    def test_pause(self):
        # Ensure instance can be paused.
        instance = self._create_instance_obj()
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)
        self.assertIsNone(instance.task_state)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'pause_instance')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.PAUSE)
        rpcapi.pause_instance(self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.pause(self.context, instance)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertEqual(task_states.PAUSING,
                         instance.task_state)

    def _test_pause_fails(self, vm_state):
        params = dict(vm_state=vm_state)
        instance = self._create_instance_obj(params=params)
        self.assertIsNone(instance.task_state)
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.pause,
                          self.context, instance)

    def test_pause_fails_invalid_states(self):
        invalid_vm_states = self._get_vm_states(set([vm_states.ACTIVE]))
        for state in invalid_vm_states:
            self._test_pause_fails(state)

    def test_unpause(self):
        # Ensure instance can be unpaused.
        params = dict(vm_state=vm_states.PAUSED)
        instance = self._create_instance_obj(params=params)
        self.assertEqual(instance.vm_state, vm_states.PAUSED)
        self.assertIsNone(instance.task_state)

        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api,
                '_record_action_start')
        if self.cell_type == 'api':
            rpcapi = self.compute_api.cells_rpcapi
        else:
            rpcapi = self.compute_api.compute_rpcapi
        self.mox.StubOutWithMock(rpcapi, 'unpause_instance')

        instance.save(expected_task_state=[None])
        self.compute_api._record_action_start(self.context,
                instance, instance_actions.UNPAUSE)
        rpcapi.unpause_instance(self.context, instance)

        self.mox.ReplayAll()

        self.compute_api.unpause(self.context, instance)
        self.assertEqual(vm_states.PAUSED, instance.vm_state)
        self.assertEqual(task_states.UNPAUSING, instance.task_state)

    def test_get_diagnostics_none_host(self):
        instance = self._create_instance_obj()
        instance.host = None
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_diagnostics,
                          self.context, instance)

    def test_get_instance_diagnostics_none_host(self):
        instance = self._create_instance_obj()
        instance.host = None
        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_instance_diagnostics,
                          self.context, instance)

    def test_live_migrate_active_vm_state(self):
        instance = self._create_instance_obj()
        self._live_migrate_instance(instance)

    def test_live_migrate_paused_vm_state(self):
        paused_state = dict(vm_state=vm_states.PAUSED)
        instance = self._create_instance_obj(params=paused_state)
        self._live_migrate_instance(instance)

    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(objects.InstanceAction, 'action_start')
    @mock.patch.object(objects.Instance, 'save')
    def test_live_migrate_messaging_timeout(self, _save, _action, get_spec,
                                            add_instance_fault_from_exc):
        instance = self._create_instance_obj()
        if self.cell_type == 'api':
            api = self.compute_api.cells_rpcapi
        else:
            api = conductor.api.ComputeTaskAPI

        with mock.patch.object(api, 'live_migrate_instance',
                               side_effect=oslo_exceptions.MessagingTimeout):
            self.assertRaises(oslo_exceptions.MessagingTimeout,
                              self.compute_api.live_migrate,
                              self.context, instance,
                              host_name='fake_dest_host',
                              block_migration=True, disk_over_commit=True)
            add_instance_fault_from_exc.assert_called_once_with(
                self.context,
                instance,
                mock.ANY)

    @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.InstanceAction, 'action_start')
    def _live_migrate_instance(self, instance, _save, _action, get_spec):
        # TODO(gilliard): This logic is upside-down (different
        # behaviour depending on which class this method is mixed-into. Once
        # we have cellsv2 we can remove this kind of logic from this test
        if self.cell_type == 'api':
            api = self.compute_api.cells_rpcapi
        else:
            api = conductor.api.ComputeTaskAPI
        fake_spec = objects.RequestSpec()
        get_spec.return_value = fake_spec
        with mock.patch.object(api, 'live_migrate_instance') as task:
            self.compute_api.live_migrate(self.context, instance,
                                          block_migration=True,
                                          disk_over_commit=True,
                                          host_name='fake_dest_host')
            self.assertEqual(task_states.MIGRATING, instance.task_state)
            task.assert_called_once_with(self.context, instance,
                                         'fake_dest_host',
                                         block_migration=True,
                                         disk_over_commit=True,
                                         request_spec=fake_spec,
                                         async=False)

    def test_swap_volume_volume_api_usage(self):
        # This test ensures that volume_id arguments are passed to volume_api
        # and that volumes return to previous states in case of error.
        def fake_vol_api_begin_detaching(context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            volumes[volume_id]['status'] = 'detaching'

        def fake_vol_api_roll_detaching(context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            if volumes[volume_id]['status'] == 'detaching':
                volumes[volume_id]['status'] = 'in-use'

        def fake_vol_api_reserve(context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            self.assertEqual(volumes[volume_id]['status'], 'available')
            volumes[volume_id]['status'] = 'attaching'

        def fake_vol_api_unreserve(context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            if volumes[volume_id]['status'] == 'attaching':
                volumes[volume_id]['status'] = 'available'

        def fake_swap_volume_exc(context, instance, old_volume_id,
                                 new_volume_id):
            raise AttributeError  # Random exception

        # Should fail if VM state is not valid
        instance = fake_instance.fake_instance_obj(None, **{
                    'vm_state': vm_states.BUILDING,
                    'launched_at': timeutils.utcnow(),
                    'locked': False,
                    'availability_zone': 'fake_az',
                    'uuid': uuids.vol_instance})
        volumes = {}
        old_volume_id = uuidutils.generate_uuid()
        volumes[old_volume_id] = {'id': old_volume_id,
                                  'display_name': 'old_volume',
                                  'attach_status': 'attached',
                                  'size': 5,
                                  'status': 'in-use',
                                  'multiattach': False,
                                  'attachments': {uuids.vol_instance: {
                                                    'attachment_id': 'fakeid'
                                                     }
                                                  }
                                  }
        new_volume_id = uuidutils.generate_uuid()
        volumes[new_volume_id] = {'id': new_volume_id,
                                  'display_name': 'new_volume',
                                  'attach_status': 'detached',
                                  'size': 5,
                                  'status': 'available',
                                  'multiattach': False}
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        instance['vm_state'] = vm_states.ACTIVE
        instance['task_state'] = None

        # Should fail if old volume is not attached
        volumes[old_volume_id]['attach_status'] = 'detached'
        self.assertRaises(exception.VolumeUnattached,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')
        volumes[old_volume_id]['attach_status'] = 'attached'

        # Should fail if old volume's instance_uuid is not that of the instance
        volumes[old_volume_id]['attachments'] = {uuids.vol_instance_2:
                                                 {'attachment_id': 'fakeid'}}
        self.assertRaises(exception.InvalidVolume,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')
        volumes[old_volume_id]['attachments'] = {uuids.vol_instance:
                                                 {'attachment_id': 'fakeid'}}

        # Should fail if new volume is attached
        volumes[new_volume_id]['attach_status'] = 'attached'
        self.assertRaises(exception.InvalidVolume,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')
        volumes[new_volume_id]['attach_status'] = 'detached'

        # Should fail if new volume is smaller than the old volume
        volumes[new_volume_id]['size'] = 4
        self.assertRaises(exception.InvalidVolume,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')
        volumes[new_volume_id]['size'] = 5

        # Fail call to swap_volume
        self.stubs.Set(self.compute_api.volume_api, 'begin_detaching',
                       fake_vol_api_begin_detaching)
        self.stubs.Set(self.compute_api.volume_api, 'roll_detaching',
                       fake_vol_api_roll_detaching)
        self.stubs.Set(self.compute_api.volume_api, 'reserve_volume',
                       fake_vol_api_reserve)
        self.stubs.Set(self.compute_api.volume_api, 'unreserve_volume',
                       fake_vol_api_unreserve)
        self.stubs.Set(self.compute_api.compute_rpcapi, 'swap_volume',
                       fake_swap_volume_exc)
        self.assertRaises(AttributeError,
                          self.compute_api.swap_volume, self.context, instance,
                          volumes[old_volume_id], volumes[new_volume_id])
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')

        # Should succeed
        self.stubs.Set(self.compute_api.compute_rpcapi, 'swap_volume',
                       lambda c, instance, old_volume_id, new_volume_id: True)
        self.compute_api.swap_volume(self.context, instance,
                                     volumes[old_volume_id],
                                     volumes[new_volume_id])

    def _test_snapshot_and_backup(self, is_snapshot=True,
                                  with_base_ref=False, min_ram=None,
                                  min_disk=None,
                                  create_fails=False,
                                  instance_vm_state=vm_states.ACTIVE):
        params = dict(locked=True)
        instance = self._create_instance_obj(params=params)
        instance.vm_state = instance_vm_state

        # 'cache_in_nova' is for testing non-inheritable properties
        # 'user_id' should also not be carried from sys_meta into
        # image property...since it should be set explicitly by
        # _create_image() in compute api.
        fake_image_meta = {
            'is_public': True,
            'name': 'base-name',
            'disk_format': 'fake',
            'container_format': 'fake',
            'properties': {
                'user_id': 'meow',
                'foo': 'bar',
                'blah': 'bug?',
                'cache_in_nova': 'dropped',
            },
        }
        image_type = is_snapshot and 'snapshot' or 'backup'
        sent_meta = {
            'is_public': False,
            'name': 'fake-name',
            'disk_format': 'fake',
            'container_format': 'fake',
            'properties': {
                'user_id': self.context.user_id,
                'instance_uuid': instance.uuid,
                'image_type': image_type,
                'foo': 'bar',
                'blah': 'bug?',
                'cow': 'moo',
                'cat': 'meow',
            },

        }
        if is_snapshot:
            if min_ram is not None:
                fake_image_meta['min_ram'] = min_ram
                sent_meta['min_ram'] = min_ram
            if min_disk is not None:
                fake_image_meta['min_disk'] = min_disk
                sent_meta['min_disk'] = min_disk
            sent_meta.pop('disk_format', None)
            sent_meta.pop('container_format', None)
        else:
            sent_meta['properties']['backup_type'] = 'fake-backup-type'

        extra_props = dict(cow='moo', cat='meow')

        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(self.compute_api.image_api,
                                 'create')
        self.mox.StubOutWithMock(instance, 'save')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                                 'snapshot_instance')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                                 'backup_instance')

        if not is_snapshot:
            self.mox.StubOutWithMock(compute_utils,
                'is_volume_backed_instance')

            compute_utils.is_volume_backed_instance(self.context,
                instance).AndReturn(False)

        utils.get_image_from_system_metadata(
            instance.system_metadata).AndReturn(fake_image_meta)

        fake_image = dict(id='fake-image-id')
        mock_method = self.compute_api.image_api.create(
                self.context, sent_meta)
        if create_fails:
            mock_method.AndRaise(test.TestingException())
        else:
            mock_method.AndReturn(fake_image)

        def check_state(expected_task_state=None):
            expected_state = (is_snapshot and
                              task_states.IMAGE_SNAPSHOT_PENDING or
                              task_states.IMAGE_BACKUP)
            self.assertEqual(expected_state, instance.task_state)

        if not create_fails:
            instance.save(expected_task_state=[None]).WithSideEffects(
                    check_state)
            if is_snapshot:
                self.compute_api.compute_rpcapi.snapshot_instance(
                        self.context, instance, fake_image['id'])
            else:
                self.compute_api.compute_rpcapi.backup_instance(
                        self.context, instance, fake_image['id'],
                        'fake-backup-type', 'fake-rotation')

        self.mox.ReplayAll()

        got_exc = False
        try:
            if is_snapshot:
                res = self.compute_api.snapshot(self.context, instance,
                                          'fake-name',
                                          extra_properties=extra_props)
            else:
                res = self.compute_api.backup(self.context, instance,
                                        'fake-name',
                                        'fake-backup-type',
                                        'fake-rotation',
                                        extra_properties=extra_props)
            self.assertEqual(fake_image, res)
        except test.TestingException:
            got_exc = True
        self.assertEqual(create_fails, got_exc)
        self.mox.UnsetStubs()

    def test_snapshot(self):
        self._test_snapshot_and_backup()

    def test_snapshot_fails(self):
        self._test_snapshot_and_backup(create_fails=True)

    def test_snapshot_invalid_state(self):
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_SNAPSHOT
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.snapshot,
                          self.context, instance, 'fake-name')
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_BACKUP
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.snapshot,
                          self.context, instance, 'fake-name')
        instance.vm_state = vm_states.BUILDING
        instance.task_state = None
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.snapshot,
                          self.context, instance, 'fake-name')

    def test_snapshot_with_base_image_ref(self):
        self._test_snapshot_and_backup(with_base_ref=True)

    def test_snapshot_min_ram(self):
        self._test_snapshot_and_backup(min_ram=42)

    def test_snapshot_min_disk(self):
        self._test_snapshot_and_backup(min_disk=42)

    def test_backup(self):
        for state in [vm_states.ACTIVE, vm_states.STOPPED,
                      vm_states.PAUSED, vm_states.SUSPENDED]:
            self._test_snapshot_and_backup(is_snapshot=False,
                                           instance_vm_state=state)

    def test_backup_fails(self):
        self._test_snapshot_and_backup(is_snapshot=False, create_fails=True)

    def test_backup_invalid_state(self):
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_SNAPSHOT
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.backup,
                          self.context, instance, 'fake-name',
                          'fake', 'fake')
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_BACKUP
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.backup,
                          self.context, instance, 'fake-name',
                          'fake', 'fake')
        instance.vm_state = vm_states.BUILDING
        instance.task_state = None
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.backup,
                          self.context, instance, 'fake-name',
                          'fake', 'fake')

    def test_backup_with_base_image_ref(self):
        self._test_snapshot_and_backup(is_snapshot=False,
                                       with_base_ref=True)

    def test_backup_volume_backed_instance(self):
        instance = self._create_instance_obj()

        with mock.patch.object(compute_utils, 'is_volume_backed_instance',
                               return_value=True) as mock_is_volume_backed:
            self.assertRaises(exception.InvalidRequest,
                              self.compute_api.backup, self.context,
                              instance, 'fake-name', 'weekly',
                              3, extra_properties={})
            mock_is_volume_backed.assert_called_once_with(self.context,
                                                          instance)

    def _test_snapshot_volume_backed(self, quiesce_required, quiesce_fails,
                                     vm_state=vm_states.ACTIVE):
        fake_sys_meta = {'image_min_ram': '11',
                         'image_min_disk': '22',
                         'image_container_format': 'ami',
                         'image_disk_format': 'ami',
                         'image_ram_disk': 'fake_ram_disk_id',
                         'image_bdm_v2': 'True',
                         'image_block_device_mapping': '[]',
                         'image_mappings': '[]',
                         'image_cache_in_nova': 'True'}
        if quiesce_required:
            fake_sys_meta['image_os_require_quiesce'] = 'yes'
        params = dict(locked=True, vm_state=vm_state,
                      system_metadata=fake_sys_meta)
        instance = self._create_instance_obj(params=params)
        instance['root_device_name'] = 'vda'

        instance_bdms = []

        expect_meta = {
            'name': 'test-snapshot',
            'properties': {'root_device_name': 'vda',
                           'ram_disk': 'fake_ram_disk_id'},
            'size': 0,
            'min_disk': '22',
            'is_public': False,
            'min_ram': '11',
        }
        if quiesce_required:
            expect_meta['properties']['os_require_quiesce'] = 'yes'

        quiesced = [False, False]
        quiesce_expected = not quiesce_fails and vm_state == vm_states.ACTIVE

        @classmethod
        def fake_bdm_list_get_by_instance_uuid(cls, context, instance_uuid):
            return obj_base.obj_make_list(context, cls(),
                    objects.BlockDeviceMapping, instance_bdms)

        def fake_image_create(context, image_meta, data=None):
            self.assertThat(image_meta, matchers.DictMatches(expect_meta))

        def fake_volume_get(context, volume_id):
            return {'id': volume_id, 'display_description': ''}

        def fake_volume_create_snapshot(context, volume_id, name, description):
            return {'id': '%s-snapshot' % volume_id}

        def fake_quiesce_instance(context, instance):
            if quiesce_fails:
                raise exception.InstanceQuiesceNotSupported(
                    instance_id=instance['uuid'], reason='test')
            quiesced[0] = True

        def fake_unquiesce_instance(context, instance, mapping=None):
            quiesced[1] = True

        self.stub_out('nova.objects.BlockDeviceMappingList'
                      '.get_by_instance_uuid',
                      fake_bdm_list_get_by_instance_uuid)
        self.stubs.Set(self.compute_api.image_api, 'create',
                       fake_image_create)
        self.stubs.Set(self.compute_api.volume_api, 'get',
                       fake_volume_get)
        self.stubs.Set(self.compute_api.volume_api, 'create_snapshot_force',
                       fake_volume_create_snapshot)
        self.stubs.Set(self.compute_api.compute_rpcapi, 'quiesce_instance',
                       fake_quiesce_instance)
        self.stubs.Set(self.compute_api.compute_rpcapi, 'unquiesce_instance',
                       fake_unquiesce_instance)
        fake_image.stub_out_image_service(self)

        # No block devices defined
        self.compute_api.snapshot_volume_backed(
            self.context, instance, 'test-snapshot')

        bdm = fake_block_device.FakeDbBlockDeviceDict(
                {'no_device': False, 'volume_id': '1', 'boot_index': 0,
                 'connection_info': 'inf', 'device_name': '/dev/vda',
                 'source_type': 'volume', 'destination_type': 'volume',
                 'tag': None})
        instance_bdms.append(bdm)

        expect_meta['properties']['bdm_v2'] = True
        expect_meta['properties']['block_device_mapping'] = []
        expect_meta['properties']['block_device_mapping'].append(
            {'guest_format': None, 'boot_index': 0, 'no_device': None,
             'image_id': None, 'volume_id': None, 'disk_bus': None,
             'volume_size': None, 'source_type': 'snapshot',
             'device_type': None, 'snapshot_id': '1-snapshot',
             'device_name': '/dev/vda',
             'destination_type': 'volume', 'delete_on_termination': False,
             'tag': None})

        # All the db_only fields and the volume ones are removed
        self.compute_api.snapshot_volume_backed(
            self.context, instance, 'test-snapshot')

        self.assertEqual(quiesce_expected, quiesced[0])
        self.assertEqual(quiesce_expected, quiesced[1])

        instance.system_metadata['image_mappings'] = jsonutils.dumps(
            [{'virtual': 'ami', 'device': 'vda'},
             {'device': 'vda', 'virtual': 'ephemeral0'},
             {'device': 'vdb', 'virtual': 'swap'},
             {'device': 'vdc', 'virtual': 'ephemeral1'}])[:255]
        instance.system_metadata['image_block_device_mapping'] = (
            jsonutils.dumps(
                [{'source_type': 'snapshot', 'destination_type': 'volume',
                  'guest_format': None, 'device_type': 'disk', 'boot_index': 1,
                  'disk_bus': 'ide', 'device_name': '/dev/vdf',
                  'delete_on_termination': True, 'snapshot_id': 'snapshot-2',
                  'volume_id': None, 'volume_size': 100, 'image_id': None,
                  'no_device': None}])[:255])

        bdm = fake_block_device.FakeDbBlockDeviceDict(
                {'no_device': False, 'volume_id': None, 'boot_index': -1,
                 'connection_info': 'inf', 'device_name': '/dev/vdh',
                 'source_type': 'blank', 'destination_type': 'local',
                 'guest_format': 'swap', 'delete_on_termination': True,
                 'tag': None})
        instance_bdms.append(bdm)
        expect_meta['properties']['block_device_mapping'].append(
            {'guest_format': 'swap', 'boot_index': -1, 'no_device': False,
             'image_id': None, 'volume_id': None, 'disk_bus': None,
             'volume_size': None, 'source_type': 'blank',
             'device_type': None, 'snapshot_id': None,
             'device_name': '/dev/vdh',
             'destination_type': 'local', 'delete_on_termination': True,
             'tag': None})

        quiesced = [False, False]

        # Check that the mappings from the image properties are not included
        self.compute_api.snapshot_volume_backed(
            self.context, instance, 'test-snapshot')

        self.assertEqual(quiesce_expected, quiesced[0])
        self.assertEqual(quiesce_expected, quiesced[1])

    def test_snapshot_volume_backed(self):
        self._test_snapshot_volume_backed(False, False)

    def test_snapshot_volume_backed_with_quiesce(self):
        self._test_snapshot_volume_backed(True, False)

    def test_snapshot_volume_backed_with_quiesce_skipped(self):
        self._test_snapshot_volume_backed(False, True)

    def test_snapshot_volume_backed_with_quiesce_exception(self):
        self.assertRaises(exception.NovaException,
                          self._test_snapshot_volume_backed, True, True)

    def test_snapshot_volume_backed_with_quiesce_stopped(self):
        self._test_snapshot_volume_backed(True, True,
                                          vm_state=vm_states.STOPPED)

    def test_snapshot_volume_backed_with_quiesce_suspended(self):
        self._test_snapshot_volume_backed(True, True,
                                          vm_state=vm_states.SUSPENDED)

    def test_snapshot_volume_backed_with_suspended(self):
        self._test_snapshot_volume_backed(False, True,
                                          vm_state=vm_states.SUSPENDED)

    def test_volume_snapshot_create(self):
        volume_id = '1'
        create_info = {'id': 'eyedee'}
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict({
                    'id': 123,
                    'device_name': '/dev/sda2',
                    'source_type': 'volume',
                    'destination_type': 'volume',
                    'connection_info': "{'fake': 'connection_info'}",
                    'volume_id': 1,
                    'boot_index': -1})
        fake_bdm['instance'] = fake_instance.fake_db_instance()
        fake_bdm['instance_uuid'] = fake_bdm['instance']['uuid']
        fake_bdm = objects.BlockDeviceMapping._from_db_object(
                self.context, objects.BlockDeviceMapping(),
                fake_bdm, expected_attrs=['instance'])

        self.mox.StubOutWithMock(objects.BlockDeviceMapping,
                                 'get_by_volume')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                'volume_snapshot_create')

        objects.BlockDeviceMapping.get_by_volume(
                self.context, volume_id,
                expected_attrs=['instance']).AndReturn(fake_bdm)
        self.compute_api.compute_rpcapi.volume_snapshot_create(self.context,
                fake_bdm['instance'], volume_id, create_info)

        self.mox.ReplayAll()

        snapshot = self.compute_api.volume_snapshot_create(self.context,
                volume_id, create_info)

        expected_snapshot = {
            'snapshot': {
                'id': create_info['id'],
                'volumeId': volume_id,
            },
        }
        self.assertEqual(snapshot, expected_snapshot)

    def test_volume_snapshot_delete(self):
        volume_id = '1'
        snapshot_id = '2'
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict({
                    'id': 123,
                    'device_name': '/dev/sda2',
                    'source_type': 'volume',
                    'destination_type': 'volume',
                    'connection_info': "{'fake': 'connection_info'}",
                    'volume_id': 1,
                    'boot_index': -1})
        fake_bdm['instance'] = fake_instance.fake_db_instance()
        fake_bdm['instance_uuid'] = fake_bdm['instance']['uuid']
        fake_bdm = objects.BlockDeviceMapping._from_db_object(
                self.context, objects.BlockDeviceMapping(),
                fake_bdm, expected_attrs=['instance'])

        self.mox.StubOutWithMock(objects.BlockDeviceMapping,
                                 'get_by_volume')
        self.mox.StubOutWithMock(self.compute_api.compute_rpcapi,
                'volume_snapshot_delete')

        objects.BlockDeviceMapping.get_by_volume(
                self.context, volume_id,
                expected_attrs=['instance']).AndReturn(fake_bdm)
        self.compute_api.compute_rpcapi.volume_snapshot_delete(self.context,
                fake_bdm['instance'], volume_id, snapshot_id, {})

        self.mox.ReplayAll()

        self.compute_api.volume_snapshot_delete(self.context, volume_id,
                snapshot_id, {})

    def _test_boot_volume_bootable(self, is_bootable=False):
        def get_vol_data(*args, **kwargs):
            return {'bootable': is_bootable}
        block_device_mapping = [{
            'id': 1,
            'device_name': 'vda',
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': '1',
            'delete_on_termination': False,
        }]

        expected_meta = {'min_disk': 0, 'min_ram': 0, 'properties': {},
                         'size': 0, 'status': 'active'}

        with mock.patch.object(self.compute_api.volume_api, 'get',
                               side_effect=get_vol_data):
            if not is_bootable:
                self.assertRaises(exception.InvalidBDMVolumeNotBootable,
                                  self.compute_api._get_bdm_image_metadata,
                                  self.context, block_device_mapping)
            else:
                meta = self.compute_api._get_bdm_image_metadata(self.context,
                                    block_device_mapping)
                self.assertEqual(expected_meta, meta)

    def test_boot_volume_non_bootable(self):
        self._test_boot_volume_bootable(False)

    def test_boot_volume_bootable(self):
        self._test_boot_volume_bootable(True)

    def test_boot_volume_basic_property(self):
        block_device_mapping = [{
            'id': 1,
            'device_name': 'vda',
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': '1',
            'delete_on_termination': False,
        }]
        fake_volume = {"volume_image_metadata":
                       {"min_ram": 256, "min_disk": 128, "foo": "bar"}}
        with mock.patch.object(self.compute_api.volume_api, 'get',
                               return_value=fake_volume):
            meta = self.compute_api._get_bdm_image_metadata(
                self.context, block_device_mapping)
            self.assertEqual(256, meta['min_ram'])
            self.assertEqual(128, meta['min_disk'])
            self.assertEqual('active', meta['status'])
            self.assertEqual('bar', meta['properties']['foo'])

    def test_boot_volume_snapshot_basic_property(self):
        block_device_mapping = [{
            'id': 1,
            'device_name': 'vda',
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': '2',
            'volume_id': None,
            'delete_on_termination': False,
        }]
        fake_volume = {"volume_image_metadata":
                       {"min_ram": 256, "min_disk": 128, "foo": "bar"}}
        fake_snapshot = {"volume_id": "1"}
        with test.nested(
                mock.patch.object(self.compute_api.volume_api, 'get',
                    return_value=fake_volume),
                mock.patch.object(self.compute_api.volume_api, 'get_snapshot',
                    return_value=fake_snapshot)) as (
                            volume_get, volume_get_snapshot):
            meta = self.compute_api._get_bdm_image_metadata(
                self.context, block_device_mapping)
            self.assertEqual(256, meta['min_ram'])
            self.assertEqual(128, meta['min_disk'])
            self.assertEqual('active', meta['status'])
            self.assertEqual('bar', meta['properties']['foo'])
            volume_get_snapshot.assert_called_once_with(self.context,
                    block_device_mapping[0]['snapshot_id'])
            volume_get.assert_called_once_with(self.context,
                    fake_snapshot['volume_id'])

    def _create_instance_with_disabled_disk_config(self, object=False):
        sys_meta = {"image_auto_disk_config": "Disabled"}
        params = {"system_metadata": sys_meta}
        instance = self._create_instance_obj(params=params)
        if object:
            return instance
        return obj_base.obj_to_primitive(instance)

    def _setup_fake_image_with_disabled_disk_config(self):
        self.fake_image = {
            'id': 1,
            'name': 'fake_name',
            'status': 'active',
            'properties': {"auto_disk_config": "Disabled"},
        }

        def fake_show(obj, context, image_id, **kwargs):
            return self.fake_image
        fake_image.stub_out_image_service(self)
        self.stubs.Set(fake_image._FakeImageService, 'show', fake_show)
        return self.fake_image['id']

    def test_resize_with_disabled_auto_disk_config_fails(self):
        fake_inst = self._create_instance_with_disabled_disk_config(
            object=True)

        self.assertRaises(exception.AutoDiskConfigDisabledByImage,
                          self.compute_api.resize,
                          self.context, fake_inst,
                          auto_disk_config=True)

    def test_create_with_disabled_auto_disk_config_fails(self):
        image_id = self._setup_fake_image_with_disabled_disk_config()

        self.assertRaises(exception.AutoDiskConfigDisabledByImage,
            self.compute_api.create, self.context,
            "fake_flavor", image_id, auto_disk_config=True)

    def test_rebuild_with_disabled_auto_disk_config_fails(self):
        fake_inst = self._create_instance_with_disabled_disk_config(
            object=True)
        image_id = self._setup_fake_image_with_disabled_disk_config()
        self.assertRaises(exception.AutoDiskConfigDisabledByImage,
                          self.compute_api.rebuild,
                          self.context,
                          fake_inst,
                          image_id,
                          "new password",
                          auto_disk_config=True)

    @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.Instance, 'get_flavor')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(compute_api.API, '_get_image')
    @mock.patch.object(compute_api.API, '_check_auto_disk_config')
    @mock.patch.object(compute_api.API, '_checks_for_create_and_rebuild')
    @mock.patch.object(compute_api.API, '_record_action_start')
    def test_rebuild(self, _record_action_start,
            _checks_for_create_and_rebuild, _check_auto_disk_config,
            _get_image, bdm_get_by_instance_uuid, get_flavor, instance_save,
            req_spec_get_by_inst_uuid):
        orig_system_metadata = {}
        instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE, cell_name='fake-cell',
                launched_at=timeutils.utcnow(),
                system_metadata=orig_system_metadata,
                image_ref='foo',
                expected_attrs=['system_metadata'])
        get_flavor.return_value = test_flavor.fake_flavor
        flavor = instance.get_flavor()
        image_href = 'foo'
        image = {"min_ram": 10, "min_disk": 1,
                 "properties": {'architecture': arch.X86_64}}
        admin_pass = ''
        files_to_inject = []
        bdms = objects.BlockDeviceMappingList()

        _get_image.return_value = (None, image)
        bdm_get_by_instance_uuid.return_value = bdms

        fake_spec = objects.RequestSpec()
        req_spec_get_by_inst_uuid.return_value = fake_spec

        with mock.patch.object(self.compute_api.compute_task_api,
                'rebuild_instance') as rebuild_instance:
            self.compute_api.rebuild(self.context, instance, image_href,
                    admin_pass, files_to_inject)

            rebuild_instance.assert_called_once_with(self.context,
                    instance=instance, new_pass=admin_pass,
                    injected_files=files_to_inject, image_ref=image_href,
                    orig_image_ref=image_href,
                    orig_sys_metadata=orig_system_metadata, bdms=bdms,
                    preserve_ephemeral=False, host=instance.host,
                    request_spec=fake_spec, kwargs={})

        _check_auto_disk_config.assert_called_once_with(image=image)
        _checks_for_create_and_rebuild.assert_called_once_with(self.context,
                None, image, flavor, {}, [], None)
        self.assertNotEqual(orig_system_metadata, instance.system_metadata)

    @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.Instance, 'get_flavor')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(compute_api.API, '_get_image')
    @mock.patch.object(compute_api.API, '_check_auto_disk_config')
    @mock.patch.object(compute_api.API, '_checks_for_create_and_rebuild')
    @mock.patch.object(compute_api.API, '_record_action_start')
    def test_rebuild_change_image(self, _record_action_start,
            _checks_for_create_and_rebuild, _check_auto_disk_config,
            _get_image, bdm_get_by_instance_uuid, get_flavor, instance_save,
            req_spec_get_by_inst_uuid):
        orig_system_metadata = {}
        get_flavor.return_value = test_flavor.fake_flavor
        orig_image_href = 'orig_image'
        orig_image = {"min_ram": 10, "min_disk": 1,
                      "properties": {'architecture': arch.X86_64,
                                     'vm_mode': 'hvm'}}
        new_image_href = 'new_image'
        new_image = {"min_ram": 10, "min_disk": 1,
                     "properties": {'architecture': arch.X86_64,
                                    'vm_mode': 'xen'}}
        admin_pass = ''
        files_to_inject = []
        bdms = objects.BlockDeviceMappingList()

        instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE, cell_name='fake-cell',
                launched_at=timeutils.utcnow(),
                system_metadata=orig_system_metadata,
                expected_attrs=['system_metadata'],
                image_ref=orig_image_href,
                vm_mode=vm_mode.HVM)
        flavor = instance.get_flavor()

        def get_image(context, image_href):
            if image_href == new_image_href:
                return (None, new_image)
            if image_href == orig_image_href:
                return (None, orig_image)
        _get_image.side_effect = get_image
        bdm_get_by_instance_uuid.return_value = bdms

        fake_spec = objects.RequestSpec()
        req_spec_get_by_inst_uuid.return_value = fake_spec

        with mock.patch.object(self.compute_api.compute_task_api,
                'rebuild_instance') as rebuild_instance:
            self.compute_api.rebuild(self.context, instance, new_image_href,
                    admin_pass, files_to_inject)

            rebuild_instance.assert_called_once_with(self.context,
                    instance=instance, new_pass=admin_pass,
                    injected_files=files_to_inject, image_ref=new_image_href,
                    orig_image_ref=orig_image_href,
                    orig_sys_metadata=orig_system_metadata, bdms=bdms,
                    preserve_ephemeral=False, host=instance.host,
                    request_spec=fake_spec, kwargs={})

        _check_auto_disk_config.assert_called_once_with(image=new_image)
        _checks_for_create_and_rebuild.assert_called_once_with(self.context,
                None, new_image, flavor, {}, [], None)
        self.assertEqual(vm_mode.XEN, instance.vm_mode)

    def _test_check_injected_file_quota_onset_file_limit_exceeded(self,
                                                                  side_effect):
        injected_files = [
            {
                "path": "/etc/banner.txt",
                "contents": "foo"
            }
        ]
        with mock.patch.object(quota.QUOTAS, 'limit_check',
                               side_effect=side_effect):
            self.compute_api._check_injected_file_quota(
                self.context, injected_files)

    def test_check_injected_file_quota_onset_file_limit_exceeded(self):
        # This is the first call to limit_check.
        side_effect = exception.OverQuota(overs='injected_files')
        self.assertRaises(exception.OnsetFileLimitExceeded,
            self._test_check_injected_file_quota_onset_file_limit_exceeded,
            side_effect)

    def test_check_injected_file_quota_onset_file_path_limit(self):
        # This is the second call to limit_check.
        side_effect = (mock.DEFAULT,
                       exception.OverQuota(overs='injected_file_path_bytes'))
        self.assertRaises(exception.OnsetFilePathLimitExceeded,
            self._test_check_injected_file_quota_onset_file_limit_exceeded,
            side_effect)

    def test_check_injected_file_quota_onset_file_content_limit(self):
        # This is the second call to limit_check but with different overs.
        side_effect = (mock.DEFAULT,
            exception.OverQuota(overs='injected_file_content_bytes'))
        self.assertRaises(exception.OnsetFileContentLimitExceeded,
            self._test_check_injected_file_quota_onset_file_limit_exceeded,
            side_effect)

    @mock.patch('nova.objects.Quotas.commit')
    @mock.patch('nova.objects.Quotas.reserve')
    @mock.patch('nova.objects.Instance.save')
    @mock.patch('nova.objects.InstanceAction.action_start')
    def test_restore_by_admin(self, action_start, instance_save,
                              quota_reserve, quota_commit):
        admin_context = context.RequestContext('admin_user',
                                               'admin_project',
                                               True)
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.SOFT_DELETED
        instance.task_state = None
        instance.save()
        with mock.patch.object(self.compute_api, 'compute_rpcapi') as rpc:
            self.compute_api.restore(admin_context, instance)
            rpc.restore_instance.assert_called_once_with(admin_context,
                                                         instance)
        self.assertEqual(instance.task_state, task_states.RESTORING)
        self.assertEqual(1, quota_commit.call_count)
        quota_reserve.assert_called_once_with(instances=1,
            cores=instance.flavor.vcpus, ram=instance.flavor.memory_mb,
            project_id=instance.project_id, user_id=instance.user_id)

    @mock.patch('nova.objects.Quotas.commit')
    @mock.patch('nova.objects.Quotas.reserve')
    @mock.patch('nova.objects.Instance.save')
    @mock.patch('nova.objects.InstanceAction.action_start')
    def test_restore_by_instance_owner(self, action_start, instance_save,
                                       quota_reserve, quota_commit):
        instance = self._create_instance_obj()
        instance.vm_state = vm_states.SOFT_DELETED
        instance.task_state = None
        instance.save()
        with mock.patch.object(self.compute_api, 'compute_rpcapi') as rpc:
            self.compute_api.restore(self.context, instance)
            rpc.restore_instance.assert_called_once_with(self.context,
                                                         instance)
        self.assertEqual(instance.project_id, self.context.project_id)
        self.assertEqual(instance.task_state, task_states.RESTORING)
        self.assertEqual(1, quota_commit.call_count)
        quota_reserve.assert_called_once_with(instances=1,
            cores=instance.flavor.vcpus, ram=instance.flavor.memory_mb,
            project_id=instance.project_id, user_id=instance.user_id)

    def test_external_instance_event(self):
        instances = [
            objects.Instance(uuid=uuids.instance_1, host='host1'),
            objects.Instance(uuid=uuids.instance_2, host='host1'),
            objects.Instance(uuid=uuids.instance_3, host='host2'),
            ]
        events = [
            objects.InstanceExternalEvent(
                instance_uuid=uuids.instance_1),
            objects.InstanceExternalEvent(
                instance_uuid=uuids.instance_2),
            objects.InstanceExternalEvent(
                instance_uuid=uuids.instance_3),
            ]
        self.compute_api.compute_rpcapi = mock.MagicMock()
        self.compute_api.external_instance_event(self.context,
                                                 instances, events)
        method = self.compute_api.compute_rpcapi.external_instance_event
        method.assert_any_call(self.context, instances[0:2], events[0:2])
        method.assert_any_call(self.context, instances[2:], events[2:])
        self.assertEqual(2, method.call_count)

    def test_volume_ops_invalid_task_state(self):
        instance = self._create_instance_obj()
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)
        instance.task_state = 'Any'
        volume_id = uuidutils.generate_uuid()
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.attach_volume,
                          self.context, instance, volume_id)

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.detach_volume,
                          self.context, instance, volume_id)

        new_volume_id = uuidutils.generate_uuid()
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.swap_volume,
                          self.context, instance,
                          volume_id, new_volume_id)

    @mock.patch.object(cinder.API, 'get',
             side_effect=exception.CinderConnectionFailed(reason='error'))
    def test_get_bdm_image_metadata_with_cinder_down(self, mock_get):
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'id': 1,
                 'volume_id': 1,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                 }))]
        self.assertRaises(exception.CinderConnectionFailed,
                          self.compute_api._get_bdm_image_metadata,
                          self.context,
                          bdms, legacy_bdm=True)

    @mock.patch.object(cinder.API, 'get')
    @mock.patch.object(cinder.API, 'check_attach',
                       side_effect=exception.InvalidVolume(reason='error'))
    def test_validate_bdm_with_error_volume(self, mock_check_attach, mock_get):
        # Tests that an InvalidVolume exception raised from
        # volume_api.check_attach due to the volume status not being
        # 'available' results in _validate_bdm re-raising InvalidVolume.
        instance = self._create_instance_obj()
        instance_type = self._create_flavor()
        volume_id = 'e856840e-9f5b-4894-8bde-58c6e29ac1e8'
        volume_info = {'status': 'error',
                       'attach_status': 'detached',
                       'id': volume_id}
        mock_get.return_value = volume_info
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'boot_index': 0,
                 'volume_id': volume_id,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                }))]

        self.assertRaises(exception.InvalidVolume,
                          self.compute_api._validate_bdm,
                          self.context,
                          instance, instance_type, bdms)

        mock_get.assert_called_once_with(self.context, volume_id)
        mock_check_attach.assert_called_once_with(
            self.context, volume_info, instance=instance)

    @mock.patch.object(cinder.API, 'get_snapshot',
             side_effect=exception.CinderConnectionFailed(reason='error'))
    @mock.patch.object(cinder.API, 'get',
             side_effect=exception.CinderConnectionFailed(reason='error'))
    def test_validate_bdm_with_cinder_down(self, mock_get, mock_get_snapshot):
        instance = self._create_instance_obj()
        instance_type = self._create_flavor()
        bdm = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'id': 1,
                 'volume_id': 1,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                 'boot_index': 0,
                 }))]
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'id': 1,
                 'snapshot_id': 1,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': 'vda',
                 'boot_index': 0,
                 }))]
        self.assertRaises(exception.CinderConnectionFailed,
                          self.compute_api._validate_bdm,
                          self.context,
                          instance, instance_type, bdm)
        self.assertRaises(exception.CinderConnectionFailed,
                          self.compute_api._validate_bdm,
                          self.context,
                          instance, instance_type, bdms)

    def _test_provision_instances_with_cinder_error(self,
                                                    expected_exception):
        @mock.patch.object(self.compute_api, '_check_num_instances_quota')
        @mock.patch.object(objects.Instance, 'create')
        @mock.patch.object(self.compute_api.security_group_api,
                'ensure_default')
        @mock.patch.object(self.compute_api, '_create_block_device_mapping')
        @mock.patch.object(objects.RequestSpec, 'from_components')
        def do_test(
                mock_req_spec_from_components, _mock_create_bdm,
                _mock_ensure_default, _mock_create, mock_check_num_inst_quota):
            quota_mock = mock.MagicMock()
            req_spec_mock = mock.MagicMock()

            mock_check_num_inst_quota.return_value = (1, quota_mock)
            mock_req_spec_from_components.return_value = req_spec_mock

            ctxt = context.RequestContext('fake-user', 'fake-project')
            flavor = self._create_flavor()
            min_count = max_count = 1
            boot_meta = {
                'id': 'fake-image-id',
                'properties': {'mappings': []},
                'status': 'fake-status',
                'location': 'far-away'}
            base_options = {'image_ref': 'fake-ref',
                            'display_name': 'fake-name',
                            'project_id': 'fake-project',
                            'availability_zone': None,
                            'metadata': {},
                            'access_ip_v4': None,
                            'access_ip_v6': None,
                            'config_drive': None,
                            'key_name': None,
                            'reservation_id': None,
                            'kernel_id': None,
                            'ramdisk_id': None,
                            'root_device_name': None,
                            'user_data': None,
                            'numa_topology': None,
                            'pci_requests': None}
            security_groups = {}
            block_device_mapping = [objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                    {
                     'id': 1,
                     'volume_id': 1,
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'device_name': 'vda',
                     'boot_index': 0,
                     }))]
            shutdown_terminate = True
            instance_group = None
            check_server_group_quota = False
            filter_properties = {'scheduler_hints': None,
                    'instance_type': flavor}

            self.assertRaises(expected_exception,
                              self.compute_api._provision_instances, ctxt,
                              flavor, min_count, max_count, base_options,
                              boot_meta, security_groups, block_device_mapping,
                              shutdown_terminate, instance_group,
                              check_server_group_quota, filter_properties,
                              None)

        do_test()

    @mock.patch.object(cinder.API, 'get',
             side_effect=exception.CinderConnectionFailed(reason='error'))
    def test_provision_instances_with_cinder_down(self, mock_get):
        self._test_provision_instances_with_cinder_error(
            expected_exception=exception.CinderConnectionFailed)

    @mock.patch.object(cinder.API, 'get',
                       return_value={'id': 1, 'status': 'error',
                                     'attach_status': 'detached'})
    def test_provision_instances_with_error_volume(self, mock_get):
        self._test_provision_instances_with_cinder_error(
            expected_exception=exception.InvalidVolume)

    @mock.patch('nova.objects.RequestSpec.from_components')
    @mock.patch('nova.objects.BuildRequest')
    @mock.patch('nova.objects.Instance')
    @mock.patch('nova.objects.InstanceMapping.create')
    def test_provision_instances_with_keypair(self, mock_im, mock_instance,
                                              mock_br, mock_rs):
        fake_keypair = objects.KeyPair(name='test')

        @mock.patch.object(self.compute_api, '_check_num_instances_quota')
        @mock.patch.object(self.compute_api, 'security_group_api')
        @mock.patch.object(self.compute_api,
                           'create_db_entry_for_new_instance')
        @mock.patch.object(self.compute_api,
                           '_bdm_validate_set_size_and_instance')
        @mock.patch.object(self.compute_api, '_create_block_device_mapping')
        def do_test(mock_cbdm, mock_bdm_v, mock_cdb, mock_sg, mock_cniq):
            mock_cniq.return_value = 1, mock.MagicMock()
            self.compute_api._provision_instances(self.context,
                                                  mock.sentinel.flavor,
                                                  1, 1, mock.MagicMock(),
                                                  {}, None,
                                                  None, None, None, {}, None,
                                                  fake_keypair)
            self.assertEqual(
                'test',
                mock_instance.return_value.keypairs.objects[0].name)
            self.compute_api._provision_instances(self.context,
                                                  mock.sentinel.flavor,
                                                  1, 1, mock.MagicMock(),
                                                  {}, None,
                                                  None, None, None, {}, None,
                                                  None)
            self.assertEqual(
                0,
                len(mock_instance.return_value.keypairs.objects))

        do_test()

    def test_provision_instances_creates_build_request(self):
        @mock.patch.object(self.compute_api, '_check_num_instances_quota')
        @mock.patch.object(objects, 'Instance')
        @mock.patch.object(self.compute_api.security_group_api,
                'ensure_default')
        @mock.patch.object(self.compute_api,
                           '_bdm_validate_set_size_and_instance')
        @mock.patch.object(self.compute_api, '_create_block_device_mapping')
        @mock.patch.object(objects.RequestSpec, 'from_components')
        @mock.patch.object(objects, 'BuildRequest')
        @mock.patch.object(objects.InstanceMapping, 'create')
        def do_test(_mock_inst_mapping_create, mock_build_req,
                mock_req_spec_from_components, _mock_create_bdm,
                mock_bdm_validate, _mock_ensure_default, mock_inst,
                mock_check_num_inst_quota):
            quota_mock = mock.MagicMock()

            min_count = 1
            max_count = 2
            mock_check_num_inst_quota.return_value = (2, quota_mock)
            req_spec_mock = mock.MagicMock()
            mock_req_spec_from_components.return_value = req_spec_mock
            inst_mocks = [mock.MagicMock() for i in range(max_count)]
            for inst_mock in inst_mocks:
                inst_mock.project_id = 'fake-project'
            mock_inst.side_effect = inst_mocks
            bdm_mocks = [mock.MagicMock() for i in range(max_count)]
            mock_bdm_validate.side_effect = bdm_mocks
            build_req_mocks = [mock.MagicMock() for i in range(max_count)]
            mock_build_req.side_effect = build_req_mocks

            ctxt = context.RequestContext('fake-user', 'fake-project')
            flavor = self._create_flavor()
            boot_meta = {
                'id': 'fake-image-id',
                'properties': {'mappings': []},
                'status': 'fake-status',
                'location': 'far-away'}
            base_options = {'image_ref': 'fake-ref',
                            'display_name': 'fake-name',
                            'project_id': 'fake-project',
                            'availability_zone': None,
                            'metadata': {},
                            'access_ip_v4': None,
                            'access_ip_v6': None,
                            'config_drive': None,
                            'key_name': None,
                            'reservation_id': None,
                            'kernel_id': None,
                            'ramdisk_id': None,
                            'root_device_name': None,
                            'user_data': None,
                            'numa_topology': None,
                            'pci_requests': None}
            security_groups = {}
            block_device_mapping = objects.BlockDeviceMappingList(
                objects=[objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                    {
                     'id': 1,
                     'volume_id': 1,
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'device_name': 'vda',
                     'boot_index': 0,
                     }))])
            shutdown_terminate = True
            instance_group = None
            check_server_group_quota = False
            filter_properties = {'scheduler_hints': None,
                    'instance_type': flavor}

            instances = self.compute_api._provision_instances(ctxt, flavor,
                    min_count, max_count, base_options, boot_meta,
                    security_groups, block_device_mapping, shutdown_terminate,
                    instance_group, check_server_group_quota,
                    filter_properties, None)
            for instance in instances:
                self.assertTrue(uuidutils.is_uuid_like(instance.uuid))

            for inst_mock in inst_mocks:
                inst_mock.create.assert_called_once_with()

            build_req_calls = [
                    mock.call(ctxt,
                              instance=instances[0],
                              instance_uuid=instances[0].uuid,
                              project_id=instances[0].project_id,
                              block_device_mappings=bdm_mocks[0]),
                    mock.call(ctxt,
                              instance=instances[1],
                              instance_uuid=instances[1].uuid,
                              project_id=instances[1].project_id,
                              block_device_mappings=bdm_mocks[1]),
                    ]
            mock_build_req.assert_has_calls(build_req_calls)
            for build_req_mock in build_req_mocks:
                build_req_mock.create.assert_called_once_with()

        do_test()

    def test_provision_instances_creates_instance_mapping(self):
        @mock.patch.object(self.compute_api, '_check_num_instances_quota')
        @mock.patch.object(objects.Instance, 'create', new=mock.MagicMock())
        @mock.patch.object(self.compute_api.security_group_api,
                'ensure_default', new=mock.MagicMock())
        @mock.patch.object(self.compute_api, '_validate_bdm',
                new=mock.MagicMock())
        @mock.patch.object(self.compute_api, '_create_block_device_mapping',
                new=mock.MagicMock())
        @mock.patch.object(objects.RequestSpec, 'from_components',
                mock.MagicMock())
        @mock.patch.object(objects, 'BuildRequest', new=mock.MagicMock())
        @mock.patch('nova.objects.InstanceMapping')
        def do_test(mock_inst_mapping, mock_check_num_inst_quota):
            quota_mock = mock.MagicMock()
            inst_mapping_mock = mock.MagicMock()

            mock_check_num_inst_quota.return_value = (1, quota_mock)
            mock_inst_mapping.return_value = inst_mapping_mock

            ctxt = context.RequestContext('fake-user', 'fake-project')
            flavor = self._create_flavor()
            min_count = max_count = 1
            boot_meta = {
                'id': 'fake-image-id',
                'properties': {'mappings': []},
                'status': 'fake-status',
                'location': 'far-away'}
            base_options = {'image_ref': 'fake-ref',
                            'display_name': 'fake-name',
                            'project_id': 'fake-project',
                            'availability_zone': None,
                            'metadata': {},
                            'access_ip_v4': None,
                            'access_ip_v6': None,
                            'config_drive': None,
                            'key_name': None,
                            'reservation_id': None,
                            'kernel_id': None,
                            'ramdisk_id': None,
                            'root_device_name': None,
                            'user_data': None,
                            'numa_topology': None,
                            'pci_requests': None}
            security_groups = {}
            block_device_mapping = objects.BlockDeviceMappingList(
                objects=[objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                    {
                     'id': 1,
                     'volume_id': 1,
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'device_name': 'vda',
                     'boot_index': 0,
                     }))])
            shutdown_terminate = True
            instance_group = None
            check_server_group_quota = False
            filter_properties = {'scheduler_hints': None,
                    'instance_type': flavor}

            instances = self.compute_api._provision_instances(ctxt, flavor,
                    min_count, max_count, base_options, boot_meta,
                    security_groups, block_device_mapping, shutdown_terminate,
                    instance_group, check_server_group_quota,
                    filter_properties, None)
            self.assertTrue(uuidutils.is_uuid_like(instances[0].uuid))

            self.assertEqual(instances[0].uuid,
                    inst_mapping_mock.instance_uuid)
            self.assertIsNone(inst_mapping_mock.cell_mapping)
            self.assertEqual(ctxt.project_id, inst_mapping_mock.project_id)
        do_test()

    @mock.patch.object(cinder.API, 'get')
    @mock.patch.object(cinder.API, 'check_attach',
                   side_effect=(None, exception.InvalidVolume(reason='error')))
    def test_provision_instances_cleans_up_when_volume_invalid(self,
            _mock_cinder_get, _mock_cinder_check_attach):
        @mock.patch.object(self.compute_api, '_check_num_instances_quota')
        @mock.patch.object(objects, 'Instance')
        @mock.patch.object(self.compute_api.security_group_api,
                'ensure_default')
        @mock.patch.object(self.compute_api, '_create_block_device_mapping')
        @mock.patch.object(objects.RequestSpec, 'from_components')
        @mock.patch.object(objects, 'BuildRequest')
        @mock.patch.object(objects, 'InstanceMapping')
        def do_test(mock_inst_mapping, mock_build_req,
                mock_req_spec_from_components, _mock_create_bdm,
                _mock_ensure_default, mock_inst, mock_check_num_inst_quota):
            quota_mock = mock.MagicMock()

            min_count = 1
            max_count = 2
            mock_check_num_inst_quota.return_value = (2, quota_mock)
            req_spec_mock = mock.MagicMock()
            mock_req_spec_from_components.return_value = req_spec_mock
            inst_mocks = [mock.MagicMock() for i in range(max_count)]
            for inst_mock in inst_mocks:
                inst_mock.project_id = 'fake-project'
            mock_inst.side_effect = inst_mocks
            build_req_mocks = [mock.MagicMock() for i in range(max_count)]
            mock_build_req.side_effect = build_req_mocks
            inst_map_mocks = [mock.MagicMock() for i in range(max_count)]
            mock_inst_mapping.side_effect = inst_map_mocks

            ctxt = context.RequestContext('fake-user', 'fake-project')
            flavor = self._create_flavor()
            boot_meta = {
                'id': 'fake-image-id',
                'properties': {'mappings': []},
                'status': 'fake-status',
                'location': 'far-away'}
            base_options = {'image_ref': 'fake-ref',
                            'display_name': 'fake-name',
                            'project_id': 'fake-project',
                            'availability_zone': None,
                            'metadata': {},
                            'access_ip_v4': None,
                            'access_ip_v6': None,
                            'config_drive': None,
                            'key_name': None,
                            'reservation_id': None,
                            'kernel_id': None,
                            'ramdisk_id': None,
                            'root_device_name': None,
                            'user_data': None,
                            'numa_topology': None,
                            'pci_requests': None}
            security_groups = {}
            block_device_mapping = objects.BlockDeviceMappingList(
                objects=[objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                    {
                     'id': 1,
                     'volume_id': 1,
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'device_name': 'vda',
                     'boot_index': 0,
                     }))])
            shutdown_terminate = True
            instance_group = None
            check_server_group_quota = False
            filter_properties = {'scheduler_hints': None,
                    'instance_type': flavor}

            self.assertRaises(exception.InvalidVolume,
                              self.compute_api._provision_instances, ctxt,
                              flavor, min_count, max_count, base_options,
                              boot_meta, security_groups, block_device_mapping,
                              shutdown_terminate, instance_group,
                              check_server_group_quota, filter_properties,
                              None)
            # First instance, build_req, mapping is created and destroyed
            self.assertTrue(inst_mocks[0].create.called)
            self.assertTrue(inst_mocks[0].destroy.called)
            self.assertTrue(build_req_mocks[0].create.called)
            self.assertTrue(build_req_mocks[0].destroy.called)
            self.assertTrue(inst_map_mocks[0].create.called)
            self.assertTrue(inst_map_mocks[0].destroy.called)
            # Second instance, build_req, mapping is not created nor destroyed
            self.assertFalse(inst_mocks[1].create.called)
            self.assertFalse(inst_mocks[1].destroy.called)
            self.assertFalse(build_req_mocks[1].destroy.called)
            self.assertFalse(inst_map_mocks[1].destroy.called)

        do_test()

    def _test_rescue(self, vm_state=vm_states.ACTIVE, rescue_password=None,
                     rescue_image=None, clean_shutdown=True):
        instance = self._create_instance_obj(params={'vm_state': vm_state})
        bdms = []
        with test.nested(
            mock.patch.object(objects.BlockDeviceMappingList,
                              'get_by_instance_uuid', return_value=bdms),
            mock.patch.object(compute_utils, 'is_volume_backed_instance',
                              return_value=False),
            mock.patch.object(instance, 'save'),
            mock.patch.object(self.compute_api, '_record_action_start'),
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'rescue_instance')
        ) as (
            bdm_get_by_instance_uuid, volume_backed_inst, instance_save,
            record_action_start, rpcapi_rescue_instance
        ):
            self.compute_api.rescue(self.context, instance,
                                    rescue_password=rescue_password,
                                    rescue_image_ref=rescue_image,
                                    clean_shutdown=clean_shutdown)
            # assert field values set on the instance object
            self.assertEqual(task_states.RESCUING, instance.task_state)
            # assert our mock calls
            bdm_get_by_instance_uuid.assert_called_once_with(
                self.context, instance.uuid)
            volume_backed_inst.assert_called_once_with(
                self.context, instance, bdms)
            instance_save.assert_called_once_with(expected_task_state=[None])
            record_action_start.assert_called_once_with(
                self.context, instance, instance_actions.RESCUE)
            rpcapi_rescue_instance.assert_called_once_with(
                self.context, instance=instance,
                rescue_password=rescue_password,
                rescue_image_ref=rescue_image,
                clean_shutdown=clean_shutdown)

    def test_rescue_active(self):
        self._test_rescue()

    def test_rescue_stopped(self):
        self._test_rescue(vm_state=vm_states.STOPPED)

    def test_rescue_error(self):
        self._test_rescue(vm_state=vm_states.ERROR)

    def test_rescue_with_password(self):
        self._test_rescue(rescue_password='fake-password')

    def test_rescue_with_image(self):
        self._test_rescue(rescue_image='fake-image')

    def test_rescue_forced_shutdown(self):
        self._test_rescue(clean_shutdown=False)

    def test_unrescue(self):
        instance = self._create_instance_obj(
            params={'vm_state': vm_states.RESCUED})
        with test.nested(
            mock.patch.object(instance, 'save'),
            mock.patch.object(self.compute_api, '_record_action_start'),
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'unrescue_instance')
        ) as (
            instance_save, record_action_start, rpcapi_unrescue_instance
        ):
            self.compute_api.unrescue(self.context, instance)
            # assert field values set on the instance object
            self.assertEqual(task_states.UNRESCUING, instance.task_state)
            # assert our mock calls
            instance_save.assert_called_once_with(expected_task_state=[None])
            record_action_start.assert_called_once_with(
                self.context, instance, instance_actions.UNRESCUE)
            rpcapi_unrescue_instance.assert_called_once_with(
                self.context, instance=instance)

    def test_set_admin_password_invalid_state(self):
        # Tests that InstanceInvalidState is raised when not ACTIVE.
        instance = self._create_instance_obj({'vm_state': vm_states.STOPPED})
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.set_admin_password,
                          self.context, instance)

    def test_set_admin_password(self):
        # Ensure instance can have its admin password set.
        instance = self._create_instance_obj()

        @mock.patch.object(objects.Instance, 'save')
        @mock.patch.object(self.compute_api, '_record_action_start')
        @mock.patch.object(self.compute_api.compute_rpcapi,
                           'set_admin_password')
        def do_test(compute_rpcapi_mock, record_mock, instance_save_mock):
            # call the API
            self.compute_api.set_admin_password(self.context, instance)
            # make our assertions
            instance_save_mock.assert_called_once_with(
                expected_task_state=[None])
            record_mock.assert_called_once_with(
                self.context, instance, instance_actions.CHANGE_PASSWORD)
            compute_rpcapi_mock.assert_called_once_with(
                self.context, instance=instance, new_pass=None)

        do_test()

    def _test_attach_interface_invalid_state(self, state):
        instance = self._create_instance_obj(
            params={'vm_state': state})
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.attach_interface,
                          self.context, instance, '', '', '', [])

    def test_attach_interface_invalid_state(self):
        for state in [vm_states.BUILDING, vm_states.DELETED,
                      vm_states.ERROR, vm_states.RESCUED,
                      vm_states.RESIZED, vm_states.SOFT_DELETED,
                      vm_states.SUSPENDED, vm_states.SHELVED,
                      vm_states.SHELVED_OFFLOADED]:
            self._test_attach_interface_invalid_state(state)

    def _test_detach_interface_invalid_state(self, state):
        instance = self._create_instance_obj(
            params={'vm_state': state})
        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.detach_interface,
                          self.context, instance, '', '', '', [])

    def test_detach_interface_invalid_state(self):
        for state in [vm_states.BUILDING, vm_states.DELETED,
                      vm_states.ERROR, vm_states.RESCUED,
                      vm_states.RESIZED, vm_states.SOFT_DELETED,
                      vm_states.SUSPENDED, vm_states.SHELVED,
                      vm_states.SHELVED_OFFLOADED]:
            self._test_detach_interface_invalid_state(state)

    def _test_check_and_transform_bdm(self, block_device_mapping):
        instance_type = self._create_flavor()
        base_options = {'uuid': uuids.bdm_instance,
                        'image_ref': 'fake_image_ref',
                        'metadata': {}}
        image_meta = {'status': 'active',
                      'name': 'image_name',
                      'deleted': False,
                      'container_format': 'bare',
                      'id': 'image_id'}
        legacy_bdm = False
        block_device_mapping = block_device_mapping
        self.assertRaises(exception.InvalidRequest,
                          self.compute_api._check_and_transform_bdm,
                          self.context, base_options, instance_type,
                          image_meta, 1, 1, block_device_mapping, legacy_bdm)

    def test_check_and_transform_bdm_source_volume(self):
        block_device_mapping = [{'boot_index': 0,
                                 'device_name': None,
                                 'image_id': 'image_id',
                                 'source_type': 'image'},
                                {'device_name': '/dev/vda',
                                 'source_type': 'volume',
                                 'destination_type': 'volume',
                                 'device_type': None,
                                 'volume_id': 'volume_id'}]
        self._test_check_and_transform_bdm(block_device_mapping)

    def test_check_and_transform_bdm_source_snapshot(self):
        block_device_mapping = [{'boot_index': 0,
                                 'device_name': None,
                                 'image_id': 'image_id',
                                 'source_type': 'image'},
                                {'device_name': '/dev/vda',
                                 'source_type': 'snapshot',
                                 'destination_type': 'volume',
                                 'device_type': None,
                                 'volume_id': 'volume_id'}]
        self._test_check_and_transform_bdm(block_device_mapping)

    def test_bdm_validate_set_size_and_instance(self):
        swap_size = 42
        ephemeral_size = 24
        instance = self._create_instance_obj()
        instance_type = self._create_flavor(swap=swap_size,
                                            ephemeral_gb=ephemeral_size)
        block_device_mapping = [
                {'device_name': '/dev/sda1',
                 'source_type': 'snapshot', 'destination_type': 'volume',
                 'snapshot_id': '00000000-aaaa-bbbb-cccc-000000000000',
                 'delete_on_termination': False,
                 'boot_index': 0},
                {'device_name': '/dev/sdb2',
                 'source_type': 'blank', 'destination_type': 'local',
                 'guest_format': 'swap', 'delete_on_termination': False},
                {'device_name': '/dev/sdb3',
                 'source_type': 'blank', 'destination_type': 'local',
                 'guest_format': 'ext3', 'delete_on_termination': False}]

        block_device_mapping = (
                block_device_obj.block_device_make_list_from_dicts(
                    self.context,
                    map(fake_block_device.AnonFakeDbBlockDeviceDict,
                        block_device_mapping)))

        with mock.patch.object(self.compute_api, '_validate_bdm'):
            bdms = self.compute_api._bdm_validate_set_size_and_instance(
                self.context, instance, instance_type, block_device_mapping)

        expected = [{'device_name': '/dev/sda1',
                     'source_type': 'snapshot', 'destination_type': 'volume',
                     'snapshot_id': '00000000-aaaa-bbbb-cccc-000000000000',
                     'delete_on_termination': False,
                     'boot_index': 0},
                    {'device_name': '/dev/sdb2',
                     'source_type': 'blank', 'destination_type': 'local',
                     'guest_format': 'swap', 'delete_on_termination': False},
                    {'device_name': '/dev/sdb3',
                     'source_type': 'blank', 'destination_type': 'local',
                     'delete_on_termination': False}]
        # Check that the bdm matches what was asked for and that instance_uuid
        # and volume_size are set properly.
        for exp, bdm in zip(expected, bdms):
            self.assertEqual(exp['device_name'], bdm.device_name)
            self.assertEqual(exp['destination_type'], bdm.destination_type)
            self.assertEqual(exp['source_type'], bdm.source_type)
            self.assertEqual(exp['delete_on_termination'],
                             bdm.delete_on_termination)
            self.assertEqual(instance.uuid, bdm.instance_uuid)
        self.assertEqual(swap_size, bdms[1].volume_size)
        self.assertEqual(ephemeral_size, bdms[2].volume_size)

    @mock.patch.object(compute_api.API, '_get_instances_by_filters')
    @mock.patch.object(objects.CellMapping, 'get_by_uuid')
    def test_tenant_to_project_conversion(self, mock_cell_map_get, mock_get):
        mock_cell_map_get.side_effect = exception.CellMappingNotFound(
                                                                uuid='fake')
        mock_get.return_value = objects.InstanceList(objects=[])
        api = compute_api.API()
        api.get_all(self.context, search_opts={'tenant_id': 'foo'})
        filters = mock_get.call_args_list[0][0][1]
        self.assertEqual({'project_id': 'foo'}, filters)

    def test_metadata_invalid_return_empty_object(self):
        api = compute_api.API()
        ret = api.get_all(self.context, search_opts={'metadata': 'foo'})
        self.assertIsInstance(ret, objects.InstanceList)
        self.assertEqual(0, len(ret))

    def test_populate_instance_names_host_name(self):
        params = dict(display_name="vm1")
        instance = self._create_instance_obj(params=params)
        self.compute_api._populate_instance_names(instance, 1)
        self.assertEqual('vm1', instance.hostname)

    def test_populate_instance_names_host_name_is_empty(self):
        params = dict(display_name=u'\u865a\u62df\u673a\u662f\u4e2d\u6587')
        instance = self._create_instance_obj(params=params)
        self.compute_api._populate_instance_names(instance, 1)
        self.assertEqual('Server-%s' % instance.uuid, instance.hostname)

    def test_populate_instance_names_host_name_multi(self):
        params = dict(display_name="vm")
        instance = self._create_instance_obj(params=params)
        with mock.patch.object(instance, 'save'):
            self.compute_api._apply_instance_name_template(self.context,
                                                           instance, 1)
            self.assertEqual('vm-2', instance.hostname)

    def test_populate_instance_names_host_name_is_empty_multi(self):
        params = dict(display_name=u'\u865a\u62df\u673a\u662f\u4e2d\u6587')
        instance = self._create_instance_obj(params=params)
        with mock.patch.object(instance, 'save'):
            self.compute_api._apply_instance_name_template(self.context,
                                                           instance, 1)
            self.assertEqual('Server-%s' % instance.uuid, instance.hostname)

    def test_host_statuses(self):
        instances = [
            objects.Instance(uuid=uuids.instance_1, host='host1', services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host1',
                             disabled=True, forced_down=True,
                             binary='nova-compute'))),
            objects.Instance(uuid=uuids.instance_2, host='host2', services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host2',
                             disabled=True, forced_down=False,
                             binary='nova-compute'))),
            objects.Instance(uuid=uuids.instance_3, host='host3', services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host3',
                             disabled=False, last_seen_up=timeutils.utcnow()
                             - datetime.timedelta(minutes=5),
                             forced_down=False, binary='nova-compute'))),
            objects.Instance(uuid=uuids.instance_4, host='host4', services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host4',
                             disabled=False, last_seen_up=timeutils.utcnow(),
                             forced_down=False, binary='nova-compute'))),
            objects.Instance(uuid=uuids.instance_5, host='host5', services=
                             objects.ServiceList()),
            objects.Instance(uuid=uuids.instance_6, host=None, services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host6',
                             disabled=True, forced_down=False,
                             binary='nova-compute'))),
            objects.Instance(uuid=uuids.instance_7, host='host2', services=
                             self._obj_to_list_obj(objects.ServiceList(
                             self.context), objects.Service(id=0, host='host2',
                             disabled=True, forced_down=False,
                             binary='nova-compute')))
            ]

        host_statuses = self.compute_api.get_instances_host_statuses(
                        instances)
        expect_statuses = {uuids.instance_1: fields_obj.HostStatus.DOWN,
                           uuids.instance_2: fields_obj.HostStatus.MAINTENANCE,
                           uuids.instance_3: fields_obj.HostStatus.UNKNOWN,
                           uuids.instance_4: fields_obj.HostStatus.UP,
                           uuids.instance_5: fields_obj.HostStatus.NONE,
                           uuids.instance_6: fields_obj.HostStatus.NONE,
                           uuids.instance_7: fields_obj.HostStatus.MAINTENANCE}
        for instance in instances:
            self.assertEqual(expect_statuses[instance.uuid],
                             host_statuses[instance.uuid])

    @mock.patch.object(objects.Migration, 'get_by_id_and_instance')
    @mock.patch.object(objects.InstanceAction, 'action_start')
    def test_live_migrate_force_complete_succeeded(
            self, action_start, get_by_id_and_instance):

        if self.cell_type == 'api':
            # cell api has not been implemented.
            return
        rpcapi = self.compute_api.compute_rpcapi

        instance = self._create_instance_obj()
        instance.task_state = task_states.MIGRATING

        migration = objects.Migration()
        migration.id = 0
        migration.status = 'running'
        get_by_id_and_instance.return_value = migration

        with mock.patch.object(
                rpcapi, 'live_migration_force_complete') as lm_force_complete:
            self.compute_api.live_migrate_force_complete(
                self.context, instance, migration)

            lm_force_complete.assert_called_once_with(self.context,
                                                      instance,
                                                      migration)
            action_start.assert_called_once_with(
                self.context, instance.uuid, 'live_migration_force_complete',
                want_result=False)

    @mock.patch.object(objects.Migration, 'get_by_id_and_instance')
    def test_live_migrate_force_complete_invalid_migration_state(
            self, get_by_id_and_instance):
        instance = self._create_instance_obj()
        instance.task_state = task_states.MIGRATING

        migration = objects.Migration()
        migration.id = 0
        migration.status = 'error'
        get_by_id_and_instance.return_value = migration

        self.assertRaises(exception.InvalidMigrationState,
                          self.compute_api.live_migrate_force_complete,
                          self.context, instance, migration.id)

    def test_live_migrate_force_complete_invalid_vm_state(self):
        instance = self._create_instance_obj()
        instance.task_state = None

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.live_migrate_force_complete,
                          self.context, instance, '1')

    def _get_migration(self, migration_id, status, migration_type):
        migration = objects.Migration()
        migration.id = migration_id
        migration.status = status
        migration.migration_type = migration_type
        return migration

    @mock.patch('nova.compute.api.API._record_action_start')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'live_migration_abort')
    @mock.patch.object(objects.Migration, 'get_by_id_and_instance')
    def test_live_migrate_abort_succeeded(self,
                                          mock_get_migration,
                                          mock_lm_abort,
                                          mock_rec_action):
        instance = self._create_instance_obj()
        instance.task_state = task_states.MIGRATING
        migration = self._get_migration(21, 'running', 'live-migration')
        mock_get_migration.return_value = migration

        self.compute_api.live_migrate_abort(self.context,
                                            instance,
                                            migration.id)
        mock_rec_action.assert_called_once_with(self.context,
                                    instance,
                                    instance_actions.LIVE_MIGRATION_CANCEL)
        mock_lm_abort.called_once_with(self.context, instance, migration.id)

    @mock.patch.object(objects.Migration, 'get_by_id_and_instance')
    def test_live_migration_abort_wrong_migration_status(self,
                                                         mock_get_migration):
        instance = self._create_instance_obj()
        instance.task_state = task_states.MIGRATING
        migration = self._get_migration(21, 'completed', 'live-migration')
        mock_get_migration.return_value = migration

        self.assertRaises(exception.InvalidMigrationState,
                          self.compute_api.live_migrate_abort,
                          self.context,
                          instance,
                          migration.id)

    def test_check_requested_networks_no_requested_networks(self):
        # When there are no requested_networks we call validate_networks on
        # the network API and return the results.
        with mock.patch.object(self.compute_api.network_api,
                               'validate_networks', return_value=3):
            count = self.compute_api._check_requested_networks(
                self.context, None, 5)
        self.assertEqual(3, count)

    def test_check_requested_networks_no_allocate(self):
        # When requested_networks is the single 'none' case for no allocation,
        # we don't validate networks and return the count passed in.
        requested_networks = (
            objects.NetworkRequestList(
                objects=[objects.NetworkRequest(network_id='none')]))
        with mock.patch.object(self.compute_api.network_api,
                               'validate_networks') as validate:
            count = self.compute_api._check_requested_networks(
                self.context, requested_networks, 5)
        self.assertEqual(5, count)
        self.assertFalse(validate.called)

    def test_check_requested_networks_auto_allocate(self):
        # When requested_networks is the single 'auto' case for allocation,
        # we validate networks and return the results.
        requested_networks = (
            objects.NetworkRequestList(
                objects=[objects.NetworkRequest(network_id='auto')]))
        with mock.patch.object(self.compute_api.network_api,
                               'validate_networks', return_value=4):
            count = self.compute_api._check_requested_networks(
                self.context, requested_networks, 5)
        self.assertEqual(4, count)

    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid',
            side_effect=exception.InstanceMappingNotFound(uuid='fake'))
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def test_get_instance_no_mapping(self, mock_get_inst, mock_get_build_req,
            mock_get_inst_map):

        self.useFixture(fixtures.AllServicesCurrent())
        # Just check that an InstanceMappingNotFound causes the instance to
        # get looked up normally.
        self.compute_api.get(self.context, uuids.inst_uuid)
        mock_get_build_req.assert_not_called()
        mock_get_inst_map.assert_called_once_with(self.context,
                                                  uuids.inst_uuid)
        mock_get_inst.assert_called_once_with(self.context, uuids.inst_uuid,
                                              expected_attrs=[
                                                  'metadata',
                                                  'system_metadata',
                                                  'security_groups',
                                                  'info_cache'])

    @mock.patch.object(objects.Service, 'get_minimum_version', return_value=15)
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_get_instance_not_in_cell(self, mock_get_build_req,
            mock_get_inst_map, mock_get_min_service):
        build_req_obj = fake_build_request.fake_req_obj(self.context)
        mock_get_inst_map.return_value = objects.InstanceMapping(
                cell_mapping=None)
        mock_get_build_req.return_value = build_req_obj

        instance = build_req_obj.instance
        inst_from_build_req = self.compute_api.get(self.context, instance.uuid)
        mock_get_inst_map.assert_called_once_with(self.context, instance.uuid)
        mock_get_build_req.assert_called_once_with(self.context, instance.uuid)
        self.assertEqual(instance, inst_from_build_req)
        mock_get_min_service.assert_called_once_with(self.context,
                                                     'nova-osapi_compute')

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def test_get_instance_not_in_cell_buildreq_deleted_inst_in_cell(
            self, mock_get_inst, mock_get_build_req, mock_get_inst_map,
            mock_target_cell):
        # This test checks the following scenario:
        # The instance is not mapped to a cell, so it should be retrieved from
        # a BuildRequest object. However the BuildRequest does not exist
        # because the instance was put in a cell and mapped while while
        # attempting to get the BuildRequest. So pull the instance from the
        # cell.
        self.useFixture(fixtures.AllServicesCurrent())
        build_req_obj = fake_build_request.fake_req_obj(self.context)
        instance = build_req_obj.instance
        inst_map = objects.InstanceMapping(cell_mapping=objects.CellMapping())

        mock_get_inst_map.side_effect = [
            objects.InstanceMapping(cell_mapping=None), inst_map]
        mock_get_build_req.side_effect = exception.BuildRequestNotFound(
            uuid=instance.uuid)
        mock_get_inst.return_value = instance

        inst_from_get = self.compute_api.get(self.context, instance.uuid)

        inst_map_calls = [mock.call(self.context, instance.uuid),
                          mock.call(self.context, instance.uuid)]
        mock_get_inst_map.assert_has_calls(inst_map_calls)
        self.assertEqual(2, mock_get_inst_map.call_count)
        mock_get_build_req.assert_called_once_with(self.context, instance.uuid)
        mock_target_cell.assert_called_once_with(self.context,
                                                 inst_map.cell_mapping)
        mock_get_inst.assert_called_once_with(self.context, instance.uuid,
                                              expected_attrs=[
                                                  'metadata',
                                                  'system_metadata',
                                                  'security_groups',
                                                  'info_cache'])
        self.assertEqual(instance, inst_from_get)

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def test_get_instance_not_in_cell_buildreq_deleted_inst_still_not_in_cell(
            self, mock_get_inst, mock_get_build_req, mock_get_inst_map,
            mock_target_cell):
        # This test checks the following scenario:
        # The instance is not mapped to a cell, so it should be retrieved from
        # a BuildRequest object. However the BuildRequest does not exist which
        # means it should now be possible to find the instance in a cell db.
        # But the instance is not mapped which means the cellsv2 migration has
        # not occurred in this scenario, so the instance is pulled from the
        # configured Nova db.

        # TODO(alaski): The tested case will eventually be an error condition.
        # But until we force cellsv2 migrations we need this to work.
        self.useFixture(fixtures.AllServicesCurrent())
        build_req_obj = fake_build_request.fake_req_obj(self.context)
        instance = build_req_obj.instance

        mock_get_inst_map.side_effect = [
            objects.InstanceMapping(cell_mapping=None),
            objects.InstanceMapping(cell_mapping=None)]
        mock_get_build_req.side_effect = exception.BuildRequestNotFound(
            uuid=instance.uuid)
        mock_get_inst.return_value = instance

        inst_from_get = self.compute_api.get(self.context, instance.uuid)

        inst_map_calls = [mock.call(self.context, instance.uuid),
                          mock.call(self.context, instance.uuid)]
        mock_get_inst_map.assert_has_calls(inst_map_calls)
        self.assertEqual(2, mock_get_inst_map.call_count)
        mock_get_build_req.assert_called_once_with(self.context, instance.uuid)
        mock_target_cell.assert_not_called()
        mock_get_inst.assert_called_once_with(self.context, instance.uuid,
                                              expected_attrs=[
                                                  'metadata',
                                                  'system_metadata',
                                                  'security_groups',
                                                  'info_cache'])
        self.assertEqual(instance, inst_from_get)

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def test_get_instance_in_cell(self, mock_get_inst, mock_get_build_req,
            mock_get_inst_map, mock_target_cell):
        self.useFixture(fixtures.AllServicesCurrent())
        # This just checks that the instance is looked up normally and not
        # synthesized from a BuildRequest object. Verification of pulling the
        # instance from the proper cell will be added when that capability is.
        instance = self._create_instance_obj()
        build_req_obj = fake_build_request.fake_req_obj(self.context)
        inst_map = objects.InstanceMapping(cell_mapping=objects.CellMapping())
        mock_get_inst_map.return_value = inst_map
        mock_get_build_req.return_value = build_req_obj
        mock_get_inst.return_value = instance

        returned_inst = self.compute_api.get(self.context, instance.uuid)
        mock_get_build_req.assert_not_called()
        mock_get_inst_map.assert_called_once_with(self.context, instance.uuid)
        self.assertEqual(instance, returned_inst)
        mock_target_cell.assert_called_once_with(self.context,
                                                 inst_map.cell_mapping)
        mock_get_inst.assert_called_once_with(self.context, instance.uuid,
                                              expected_attrs=[
                                                  'metadata',
                                                  'system_metadata',
                                                  'security_groups',
                                                  'info_cache'])

    def _list_of_instances(self, length=1):
        instances = []
        for i in range(length):
            instances.append(
                fake_instance.fake_instance_obj(self.context, objects.Instance,
                                                uuid=uuidutils.generate_uuid())
            )
        return instances

    @mock.patch.object(objects.BuildRequestList, 'get_by_filters')
    @mock.patch.object(objects.CellMapping, 'get_by_uuid',
                       side_effect=exception.CellMappingNotFound(uuid='fake'))
    def test_get_all_includes_build_requests(self, mock_cell_mapping_get,
                                             mock_buildreq_get):

        build_req_instances = self._list_of_instances(2)
        build_reqs = [objects.BuildRequest(self.context, instance=instance)
                      for instance in build_req_instances]
        mock_buildreq_get.return_value = objects.BuildRequestList(self.context,
            objects=build_reqs)

        cell_instances = self._list_of_instances(2)

        with mock.patch.object(self.compute_api,
                               '_get_instances_by_filters') as mock_inst_get:
            mock_inst_get.return_value = objects.InstanceList(
                self.context, objects=cell_instances)

            instances = self.compute_api.get_all(
                self.context, search_opts={'foo': 'bar'},
                limit=None, marker='fake-marker', sort_keys=['baz'],
                sort_dirs=['desc'])

            mock_buildreq_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=None, marker='fake-marker',
                sort_keys=['baz'], sort_dirs=['desc'])
            mock_inst_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=None, marker='fake-marker',
                expected_attrs=None, sort_keys=['baz'], sort_dirs=['desc'])
            for i, instance in enumerate(build_req_instances + cell_instances):
                self.assertEqual(instance, instances[i])

    @mock.patch.object(objects.BuildRequestList, 'get_by_filters')
    @mock.patch.object(objects.CellMapping, 'get_by_uuid',
                       side_effect=exception.CellMappingNotFound(uuid='fake'))
    def test_get_all_includes_build_requests_filter_dupes(self,
            mock_cell_mapping_get, mock_buildreq_get):

        build_req_instances = self._list_of_instances(2)
        build_reqs = [objects.BuildRequest(self.context, instance=instance)
                      for instance in build_req_instances]
        mock_buildreq_get.return_value = objects.BuildRequestList(self.context,
            objects=build_reqs)

        cell_instances = self._list_of_instances(2)

        with mock.patch.object(self.compute_api,
                               '_get_instances_by_filters') as mock_inst_get:
            # Insert one of the build_req_instances here so it shows up twice
            mock_inst_get.return_value = objects.InstanceList(
                self.context, objects=build_req_instances[:1] + cell_instances)

            instances = self.compute_api.get_all(
                self.context, search_opts={'foo': 'bar'},
                limit=None, marker='fake-marker', sort_keys=['baz'],
                sort_dirs=['desc'])

            mock_buildreq_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=None, marker='fake-marker',
                sort_keys=['baz'], sort_dirs=['desc'])
            mock_inst_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=None, marker='fake-marker',
                expected_attrs=None, sort_keys=['baz'], sort_dirs=['desc'])
            for i, instance in enumerate(build_req_instances + cell_instances):
                self.assertEqual(instance, instances[i])

    @mock.patch.object(objects.BuildRequestList, 'get_by_filters')
    @mock.patch.object(objects.CellMapping, 'get_by_uuid',
                       side_effect=exception.CellMappingNotFound(uuid='fake'))
    def test_get_all_build_requests_decrement_limit(self,
                                                    mock_cell_mapping_get,
                                                    mock_buildreq_get):

        build_req_instances = self._list_of_instances(2)
        build_reqs = [objects.BuildRequest(self.context, instance=instance)
                      for instance in build_req_instances]
        mock_buildreq_get.return_value = objects.BuildRequestList(self.context,
            objects=build_reqs)

        cell_instances = self._list_of_instances(2)

        with mock.patch.object(self.compute_api,
                               '_get_instances_by_filters') as mock_inst_get:
            mock_inst_get.return_value = objects.InstanceList(
                self.context, objects=cell_instances)

            instances = self.compute_api.get_all(
                self.context, search_opts={'foo': 'bar'},
                limit=10, marker='fake-marker', sort_keys=['baz'],
                sort_dirs=['desc'])

            mock_buildreq_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=10, marker='fake-marker',
                sort_keys=['baz'], sort_dirs=['desc'])
            mock_inst_get.assert_called_once_with(
                self.context, {'foo': 'bar'}, limit=8, marker='fake-marker',
                expected_attrs=None, sort_keys=['baz'], sort_dirs=['desc'])
            for i, instance in enumerate(build_req_instances + cell_instances):
                self.assertEqual(instance, instances[i])

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.BuildRequestList, 'get_by_filters',
                       return_value=objects.BuildRequestList(objects=[]))
    @mock.patch.object(objects.CellMapping, 'get_by_uuid')
    def test_get_all_includes_cell0(self, mock_cell_mapping_get,
                                    mock_buildreq_get, mock_target_cell):

        cell0_instances = self._list_of_instances(2)
        cell_instances = self._list_of_instances(2)

        cell_mapping = objects.CellMapping()
        mock_cell_mapping_get.return_value = cell_mapping

        with mock.patch.object(self.compute_api,
                               '_get_instances_by_filters') as mock_inst_get:
            mock_inst_get.side_effect = [objects.InstanceList(
                                             self.context,
                                             objects=cell0_instances),
                                         objects.InstanceList(
                                             self.context,
                                             objects=cell_instances)]

            instances = self.compute_api.get_all(
                self.context, search_opts={'foo': 'bar'},
                limit=10, marker='fake-marker', sort_keys=['baz'],
                sort_dirs=['desc'])

            mock_target_cell.assert_called_once_with(self.context,
                                                     cell_mapping)
            inst_get_calls = [mock.call(self.context, {'foo': 'bar'},
                                        limit=10, marker='fake-marker',
                                        expected_attrs=None, sort_keys=['baz'],
                                        sort_dirs=['desc']),
                              mock.call(self.context, {'foo': 'bar'},
                                        limit=8, marker='fake-marker',
                                        expected_attrs=None, sort_keys=['baz'],
                                        sort_dirs=['desc'])
                              ]
            self.assertEqual(2, mock_inst_get.call_count)
            mock_inst_get.assert_has_calls(inst_get_calls)
            for i, instance in enumerate(cell0_instances + cell_instances):
                self.assertEqual(instance, instances[i])

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.BuildRequestList, 'get_by_filters')
    @mock.patch.object(objects.CellMapping, 'get_by_uuid')
    def test_get_all_includes_build_request_cell0(self, mock_cell_mapping_get,
                                    mock_buildreq_get, mock_target_cell):

        build_req_instances = self._list_of_instances(2)
        build_reqs = [objects.BuildRequest(self.context, instance=instance)
                      for instance in build_req_instances]
        mock_buildreq_get.return_value = objects.BuildRequestList(self.context,
            objects=build_reqs)

        cell0_instances = self._list_of_instances(2)
        cell_instances = self._list_of_instances(2)

        cell_mapping = objects.CellMapping()
        mock_cell_mapping_get.return_value = cell_mapping

        with mock.patch.object(self.compute_api,
                               '_get_instances_by_filters') as mock_inst_get:
            mock_inst_get.side_effect = [objects.InstanceList(
                                             self.context,
                                             objects=cell0_instances),
                                         objects.InstanceList(
                                             self.context,
                                             objects=cell_instances)]

            instances = self.compute_api.get_all(
                self.context, search_opts={'foo': 'bar'},
                limit=10, marker='fake-marker', sort_keys=['baz'],
                sort_dirs=['desc'])

            mock_target_cell.assert_called_once_with(self.context,
                                                     cell_mapping)
            inst_get_calls = [mock.call(self.context, {'foo': 'bar'},
                                        limit=8, marker='fake-marker',
                                        expected_attrs=None, sort_keys=['baz'],
                                        sort_dirs=['desc']),
                              mock.call(self.context, {'foo': 'bar'},
                                        limit=6, marker='fake-marker',
                                        expected_attrs=None, sort_keys=['baz'],
                                        sort_dirs=['desc'])
                              ]
            self.assertEqual(2, mock_inst_get.call_count)
            mock_inst_get.assert_has_calls(inst_get_calls)
            for i, instance in enumerate(build_req_instances +
                                         cell0_instances +
                                         cell_instances):
                self.assertEqual(instance, instances[i])

    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    def test_update_existing_instance_not_in_cell(self, mock_instmap_get,
                                                  mock_buildreq_get):
        mock_instmap_get.side_effect = exception.InstanceMappingNotFound(
            uuid='fake')
        self.useFixture(fixtures.AllServicesCurrent())

        instance = self._create_instance_obj()
        # Just making sure that the instance has been created
        self.assertIsNotNone(instance.id)
        updates = {'display_name': 'foo_updated'}
        with mock.patch.object(instance, 'save') as mock_inst_save:
            returned_instance = self.compute_api.update_instance(
                self.context, instance, updates)
        mock_buildreq_get.assert_not_called()
        self.assertEqual('foo_updated', returned_instance.display_name)
        mock_inst_save.assert_called_once_with()

    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    def test_update_existing_instance_in_cell(self, mock_instmap_get,
                                              mock_target_cell,
                                              mock_buildreq_get):
        inst_map = objects.InstanceMapping(cell_mapping=objects.CellMapping())
        mock_instmap_get.return_value = inst_map
        self.useFixture(fixtures.AllServicesCurrent())

        instance = self._create_instance_obj()
        # Just making sure that the instance has been created
        self.assertIsNotNone(instance.id)
        updates = {'display_name': 'foo_updated'}
        with mock.patch.object(instance, 'save') as mock_inst_save:
            returned_instance = self.compute_api.update_instance(
                self.context, instance, updates)
        mock_target_cell.assert_called_once_with(self.context,
                                                 inst_map.cell_mapping)
        mock_buildreq_get.assert_not_called()
        self.assertEqual('foo_updated', returned_instance.display_name)
        mock_inst_save.assert_called_once_with()

    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_update_future_instance_with_buildreq(self, mock_buildreq_get):

        # This test checks that a new instance which is not yet peristed in
        # DB can be found by looking up the BuildRequest object so we can
        # update it.

        build_req_obj = fake_build_request.fake_req_obj(self.context)
        mock_buildreq_get.return_value = build_req_obj
        self.useFixture(fixtures.AllServicesCurrent())

        instance = self._create_instance_obj()
        # Fake the fact that the instance is not yet persisted in DB
        del instance.id

        updates = {'display_name': 'foo_updated'}
        with mock.patch.object(build_req_obj, 'save') as mock_buildreq_save:
            returned_instance = self.compute_api.update_instance(
                self.context, instance, updates)

        mock_buildreq_get.assert_called_once_with(self.context, instance.uuid)
        self.assertEqual(build_req_obj.instance, returned_instance)
        mock_buildreq_save.assert_called_once_with()
        self.assertEqual('foo_updated', returned_instance.display_name)

    @mock.patch.object(context, 'target_cell')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_update_instance_in_cell_in_transition_state(self,
                                                         mock_buildreq_get,
                                                         mock_instmap_get,
                                                         mock_inst_get,
                                                         mock_target_cell):

        # This test is for covering the following case:
        #  - when we lookup the instance initially, that one is not yet mapped
        #    to a cell and consequently we retrieve it from the BuildRequest
        #  - when we update the instance, that one could have been mapped
        #    meanwhile and the BuildRequest was deleted
        #  - if the instance is mapped, lookup the cell DB to find the instance

        self.useFixture(fixtures.AllServicesCurrent())

        instance = self._create_instance_obj()
        # Fake the fact that the instance is not yet persisted in DB
        del instance.id

        mock_buildreq_get.side_effect = exception.BuildRequestNotFound(
            uuid=instance.uuid)
        inst_map = objects.InstanceMapping(cell_mapping=objects.CellMapping())
        mock_instmap_get.return_value = inst_map
        mock_inst_get.return_value = instance

        updates = {'display_name': 'foo_updated'}
        with mock.patch.object(instance, 'save') as mock_inst_save:
            returned_instance = self.compute_api.update_instance(
                self.context, instance, updates)

        mock_buildreq_get.assert_called_once_with(self.context, instance.uuid)
        mock_target_cell.assert_called_once_with(self.context,
                                                 inst_map.cell_mapping)
        mock_inst_save.assert_called_once_with()
        self.assertEqual('foo_updated', returned_instance.display_name)

    @mock.patch.object(objects.Instance, 'get_by_uuid')
    @mock.patch.object(objects.InstanceMapping, 'get_by_instance_uuid')
    @mock.patch.object(objects.BuildRequest, 'get_by_instance_uuid')
    def test_update_instance_not_in_cell_in_transition_state(self,
                                                             mock_buildreq_get,
                                                             mock_instmap_get,
                                                             mock_inst_get):

        # This test is for covering the following case:
        #  - when we lookup the instance initially, that one is not yet mapped
        #    to a cell and consequently we retrieve it from the BuildRequest
        #  - when we update the instance, that one could have been mapped
        #    meanwhile and the BuildRequest was deleted
        #  - if the instance is not mapped, lookup the API DB to find whether
        #    the instance was deleted, or if the cellv2 migration is not done

        self.useFixture(fixtures.AllServicesCurrent())

        instance = self._create_instance_obj()
        # Fake the fact that the instance is not yet persisted in DB
        del instance.id

        mock_buildreq_get.side_effect = exception.BuildRequestNotFound(
            uuid=instance.uuid)
        mock_instmap_get.side_effect = exception.InstanceMappingNotFound(
            uuid='fake')
        mock_inst_get.return_value = instance

        updates = {'display_name': 'foo_updated'}
        with mock.patch.object(instance, 'save') as mock_inst_save:
            returned_instance = self.compute_api.update_instance(
                self.context, instance, updates)

        mock_buildreq_get.assert_called_once_with(self.context, instance.uuid)
        mock_inst_save.assert_called_once_with()
        self.assertEqual('foo_updated', returned_instance.display_name)

        # Let's do a quick verification on the same unittest to see what
        # happens if the instance was deleted meanwhile.
        mock_inst_get.side_effect = exception.InstanceNotFound(
            instance_id=instance.uuid)
        self.assertRaises(exception.InstanceNotFound,
                          self.compute_api.update_instance,
                          self.context, instance, updates)


class ComputeAPIUnitTestCase(_ComputeAPIUnitTestMixIn, test.NoDBTestCase):
    def setUp(self):
        super(ComputeAPIUnitTestCase, self).setUp()
        self.compute_api = compute_api.API()
        self.cell_type = None

    def test_resize_same_flavor_fails(self):
        self.assertRaises(exception.CannotResizeToSameFlavor,
                          self._test_resize, same_flavor=True)


class ComputeAPIAPICellUnitTestCase(_ComputeAPIUnitTestMixIn,
                                    test.NoDBTestCase):
    def setUp(self):
        super(ComputeAPIAPICellUnitTestCase, self).setUp()
        self.flags(cell_type='api', enable=True, group='cells')
        self.compute_api = compute_cells_api.ComputeCellsAPI()
        self.cell_type = 'api'

    def test_resize_same_flavor_fails(self):
        self.assertRaises(exception.CannotResizeToSameFlavor,
                          self._test_resize, same_flavor=True)

    @mock.patch.object(compute_cells_api, 'ComputeRPCAPIRedirect')
    def test_create_volume_bdm_call_reserve_dev_name(self, mock_reserve):
        instance = self._create_instance_obj()
        # In the cells rpcapi there isn't the call for the
        # reserve_block_device_name so the volume_bdm returned
        # by the _create_volume_bdm is None
        result = self.compute_api._create_volume_bdm(self.context,
                                                     instance,
                                                     'vda',
                                                     '1',
                                                     None,
                                                     None)
        self.assertIsNone(result, None)

    @mock.patch.object(compute_cells_api.ComputeCellsAPI, '_call_to_cells')
    def test_attach_volume(self, mock_attach):
        instance = self._create_instance_obj()
        volume = fake_volume.fake_volume(1, 'test-vol', 'test-vol',
                                         None, None, None, None, None)

        mock_volume_api = mock.patch.object(self.compute_api, 'volume_api',
                                            mock.MagicMock(spec=cinder.API))
        with mock_volume_api as mock_v_api:
            mock_v_api.get.return_value = volume
            self.compute_api.attach_volume(
                self.context, instance, volume['id'])
            mock_v_api.check_availability_zone.assert_called_once_with(
                self.context, volume, instance=instance)
            mock_attach.assert_called_once_with(self.context, instance,
                                                'attach_volume', volume['id'],
                                                None, None, None)

    def test_create_with_networks_max_count_none(self):
        self.skipTest("This test does not test any rpcapi.")

    def test_attach_volume_reserve_fails(self):
        self.skipTest("Reserve is never done in the API cell.")

    def test_check_requested_networks_no_requested_networks(self):
        # The API cell just returns the number of instances passed in since the
        # actual validation happens in the child (compute) cell.
        self.assertEqual(
            2, self.compute_api._check_requested_networks(
                self.context, None, 2))

    def test_check_requested_networks_auto_allocate(self):
        # The API cell just returns the number of instances passed in since the
        # actual validation happens in the child (compute) cell.
        requested_networks = (
            objects.NetworkRequestList(
                objects=[objects.NetworkRequest(network_id='auto')]))
        count = self.compute_api._check_requested_networks(
            self.context, requested_networks, 5)
        self.assertEqual(5, count)


class ComputeAPIComputeCellUnitTestCase(_ComputeAPIUnitTestMixIn,
                                        test.NoDBTestCase):
    def setUp(self):
        super(ComputeAPIComputeCellUnitTestCase, self).setUp()
        self.flags(cell_type='compute', enable=True, group='cells')
        self.compute_api = compute_api.API()
        self.cell_type = 'compute'

    def test_resize_same_flavor_passes(self):
        self._test_resize(same_flavor=True)


class DiffDictTestCase(test.NoDBTestCase):
    """Unit tests for _diff_dict()."""

    def test_no_change(self):
        old = dict(a=1, b=2, c=3)
        new = dict(a=1, b=2, c=3)
        diff = compute_api._diff_dict(old, new)

        self.assertEqual(diff, {})

    def test_new_key(self):
        old = dict(a=1, b=2, c=3)
        new = dict(a=1, b=2, c=3, d=4)
        diff = compute_api._diff_dict(old, new)

        self.assertEqual(diff, dict(d=['+', 4]))

    def test_changed_key(self):
        old = dict(a=1, b=2, c=3)
        new = dict(a=1, b=4, c=3)
        diff = compute_api._diff_dict(old, new)

        self.assertEqual(diff, dict(b=['+', 4]))

    def test_removed_key(self):
        old = dict(a=1, b=2, c=3)
        new = dict(a=1, c=3)
        diff = compute_api._diff_dict(old, new)

        self.assertEqual(diff, dict(b=['-']))


class SecurityGroupAPITest(test.NoDBTestCase):
    def setUp(self):
        super(SecurityGroupAPITest, self).setUp()
        self.secgroup_api = compute_api.SecurityGroupAPI()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id,
                                              self.project_id)

    def test_get_instance_security_groups(self):
        groups = objects.SecurityGroupList()
        groups.objects = [objects.SecurityGroup(name='foo'),
                          objects.SecurityGroup(name='bar')]
        instance = objects.Instance(security_groups=groups)
        names = self.secgroup_api.get_instance_security_groups(self.context,
                                                               instance)
        self.assertEqual(sorted([{'name': 'bar'}, {'name': 'foo'}], key=str),
                         sorted(names, key=str))

    @mock.patch('nova.objects.security_group.make_secgroup_list')
    def test_populate_security_groups(self, mock_msl):
        r = self.secgroup_api.populate_security_groups([mock.sentinel.group])
        mock_msl.assert_called_once_with([mock.sentinel.group])
        self.assertEqual(r, mock_msl.return_value)
