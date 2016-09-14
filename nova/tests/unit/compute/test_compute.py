# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Piston Cloud Computing, Inc.
# All Rights Reserved.
# Copyright 2013 Red Hat, Inc.
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
"""Tests for compute service."""

import base64
import datetime
import operator
import sys
import time
import traceback
import uuid

from itertools import chain
import mock
from neutronclient.common import exceptions as neutron_exceptions
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_serialization import jsonutils
from oslo_utils import fixture as utils_fixture
from oslo_utils import importutils
from oslo_utils import timeutils
from oslo_utils import units
from oslo_utils import uuidutils
import six
import testtools
from testtools import matchers as testtools_matchers

import nova
from nova import availability_zones
from nova import block_device
from nova import compute
from nova.compute import api as compute_api
from nova.compute import arch
from nova.compute import flavors
from nova.compute import manager as compute_manager
from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
import nova.conf
from nova.console import type as ctype
from nova import context
from nova import db
from nova import exception
from nova.image import api as image_api
from nova.image import glance
from nova.network import api as network_api
from nova.network import model as network_model
from nova.network.security_group import openstack_driver
from nova import objects
from nova.objects import block_device as block_device_obj
from nova.objects import fields as obj_fields
from nova.objects import instance as instance_obj
from nova.objects import migrate_data as migrate_data_obj
from nova import quota
from nova.scheduler import client as scheduler_client
from nova import test
from nova.tests import fixtures
from nova.tests.unit.compute import eventlet_utils
from nova.tests.unit.compute import fake_resource_tracker
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_network
from nova.tests.unit import fake_network_cache_model
from nova.tests.unit import fake_notifier
from nova.tests.unit import fake_server_actions
from nova.tests.unit.image import fake as fake_image
from nova.tests.unit import matchers
from nova.tests.unit.objects import test_flavor
from nova.tests.unit.objects import test_instance_numa_topology
from nova.tests.unit.objects import test_migration
from nova.tests.unit import utils as test_utils
from nova.tests import uuidsentinel as uuids
from nova import utils
from nova.virt import block_device as driver_block_device
from nova.virt import event
from nova.virt import fake
from nova.virt import hardware
from nova.volume import cinder

QUOTAS = quota.QUOTAS
LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF


FAKE_IMAGE_REF = uuids.image_ref

NODENAME = 'fakenode1'


def fake_not_implemented(*args, **kwargs):
    raise NotImplementedError()


def get_primitive_instance_by_uuid(context, instance_uuid):
    """Helper method to get an instance and then convert it to
    a primitive form using jsonutils.
    """
    instance = db.instance_get_by_uuid(context, instance_uuid)
    return jsonutils.to_primitive(instance)


def unify_instance(instance):
    """Return a dict-like instance for both object-initiated and
    model-initiated sources that can reasonably be compared.
    """
    newdict = dict()
    for k, v in six.iteritems(instance):
        if isinstance(v, datetime.datetime):
            # NOTE(danms): DB models and Instance objects have different
            # timezone expectations
            v = v.replace(tzinfo=None)
        elif k == 'fault':
            # NOTE(danms): DB models don't have 'fault'
            continue
        elif k == 'pci_devices':
            # NOTE(yonlig.he) pci devices need lazy loading
            # fake db does not support it yet.
            continue
        newdict[k] = v
    return newdict


class FakeComputeTaskAPI(object):

    def resize_instance(self, context, instance, extra_instance_updates,
                        scheduler_hint, flavor, reservations):
        pass


class BaseTestCase(test.TestCase):

    def setUp(self):
        super(BaseTestCase, self).setUp()
        self.flags(network_manager='nova.network.manager.FlatManager')
        fake.set_nodes([NODENAME])
        self.flags(use_local=True, group='conductor')

        fake_notifier.stub_notifier(self)
        self.addCleanup(fake_notifier.reset)

        self.compute = importutils.import_object(CONF.compute_manager)
        # execute power syncing synchronously for testing:
        self.compute._sync_power_pool = eventlet_utils.SyncPool()

        # override tracker with a version that doesn't need the database:
        fake_rt = fake_resource_tracker.FakeResourceTracker(self.compute.host,
                    self.compute.driver, NODENAME)
        self.compute._resource_tracker_dict[NODENAME] = fake_rt

        def fake_get_compute_nodes_in_db(self, context, use_slave=False):
            fake_compute_nodes = [{'local_gb': 259,
                                   'uuid': uuids.fake_compute_node,
                                   'vcpus_used': 0,
                                   'deleted': 0,
                                   'hypervisor_type': 'powervm',
                                   'created_at': '2013-04-01T00:27:06.000000',
                                   'local_gb_used': 0,
                                   'updated_at': '2013-04-03T00:35:41.000000',
                                   'hypervisor_hostname': 'fake_phyp1',
                                   'memory_mb_used': 512,
                                   'memory_mb': 131072,
                                   'current_workload': 0,
                                   'vcpus': 16,
                                   'cpu_info': 'ppc64,powervm,3940',
                                   'running_vms': 0,
                                   'free_disk_gb': 259,
                                   'service_id': 7,
                                   'hypervisor_version': 7,
                                   'disk_available_least': 265856,
                                   'deleted_at': None,
                                   'free_ram_mb': 130560,
                                   'metrics': '',
                                   'stats': '',
                                   'numa_topology': '',
                                   'id': 2,
                                   'host': 'fake_phyp1',
                                   'cpu_allocation_ratio': 16.0,
                                   'ram_allocation_ratio': 1.5,
                                   'disk_allocation_ratio': 1.0,
                                   'host_ip': '127.0.0.1'}]
            return [objects.ComputeNode._from_db_object(
                        context, objects.ComputeNode(), cn)
                    for cn in fake_compute_nodes]

        def fake_compute_node_delete(context, compute_node_id):
            self.assertEqual(2, compute_node_id)

        self.stub_out(
            'nova.compute.manager.ComputeManager._get_compute_nodes_in_db',
            fake_get_compute_nodes_in_db)
        self.stub_out('nova.db.compute_node_delete',
                fake_compute_node_delete)

        self.compute.update_available_resource(
                context.get_admin_context())

        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.RequestContext(self.user_id,
                                              self.project_id)
        self.none_quotas = objects.Quotas.from_reservations(
                self.context, None)

        def fake_show(meh, context, id, **kwargs):
            if id:
                return {'id': id,
                        'name': 'fake_name',
                        'status': 'active',
                        'properties': {'kernel_id': uuids.kernel_id,
                                       'ramdisk_id': uuids.ramdisk_id,
                                       'something_else': 'meow'}}
            else:
                raise exception.ImageNotFound(image_id=id)

        fake_image.stub_out_image_service(self)
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      fake_show)

        fake_network.set_stub_network_methods(self)
        fake_server_actions.stub_out_action_events(self)

        def fake_get_nw_info(cls, ctxt, instance, *args, **kwargs):
            return network_model.NetworkInfo()

        self.stub_out('nova.network.api.API.get_instance_nw_info',
                       fake_get_nw_info)

        def fake_allocate_for_instance(cls, ctxt, instance, *args, **kwargs):
            self.assertFalse(ctxt.is_admin)
            return fake_network.fake_get_instance_nw_info(self, 1, 1)

        self.stub_out('nova.network.api.API.allocate_for_instance',
                       fake_allocate_for_instance)
        self.compute_api = compute.API()

        # Just to make long lines short
        self.rt = self.compute._get_resource_tracker(NODENAME)

    def tearDown(self):
        ctxt = context.get_admin_context()
        fake_image.FakeImageService_reset()
        instances = db.instance_get_all(ctxt)
        for instance in instances:
            db.instance_destroy(ctxt, instance['uuid'])
        fake.restore_nodes()
        super(BaseTestCase, self).tearDown()

    def _fake_instance(self, updates):
        return fake_instance.fake_instance_obj(None, **updates)

    def _create_fake_instance_obj(self, params=None, type_name='m1.tiny',
                                  services=False, context=None):
        flavor = flavors.get_flavor_by_name(type_name)
        inst = objects.Instance(context=context or self.context)
        inst.vm_state = vm_states.ACTIVE
        inst.task_state = None
        inst.power_state = power_state.RUNNING
        inst.image_ref = FAKE_IMAGE_REF
        inst.reservation_id = 'r-fakeres'
        inst.user_id = self.user_id
        inst.project_id = self.project_id
        inst.host = self.compute.host
        inst.node = NODENAME
        inst.instance_type_id = flavor.id
        inst.ami_launch_index = 0
        inst.memory_mb = 0
        inst.vcpus = 0
        inst.root_gb = 0
        inst.ephemeral_gb = 0
        inst.architecture = arch.X86_64
        inst.os_type = 'Linux'
        inst.system_metadata = (
            params and params.get('system_metadata', {}) or {})
        inst.locked = False
        inst.created_at = timeutils.utcnow()
        inst.updated_at = timeutils.utcnow()
        inst.launched_at = timeutils.utcnow()
        inst.security_groups = objects.SecurityGroupList(objects=[])
        inst.flavor = flavor
        inst.old_flavor = None
        inst.new_flavor = None
        if params:
            inst.flavor.update(params.pop('flavor', {}))
            inst.update(params)
        if services:
            _create_service_entries(self.context.elevated(),
                                    [['fake_zone', [inst.host]]])
        inst.create()

        return inst

    def _create_instance_type(self, params=None):
        """Create a test instance type."""
        if not params:
            params = {}

        context = self.context.elevated()
        inst = {}
        inst['name'] = 'm1.small'
        inst['memory_mb'] = 1024
        inst['vcpus'] = 1
        inst['root_gb'] = 20
        inst['ephemeral_gb'] = 10
        inst['flavorid'] = '1'
        inst['swap'] = 2048
        inst['rxtx_factor'] = 1
        inst.update(params)
        return db.flavor_create(context, inst)['id']

    def _create_group(self):
        values = {'name': 'testgroup',
                  'description': 'testgroup',
                  'user_id': self.user_id,
                  'project_id': self.project_id}
        return db.security_group_create(self.context, values)

    def _stub_migrate_server(self):
        def _fake_migrate_server(*args, **kwargs):
            pass

        self.stub_out('nova.conductor.manager.ComputeTaskManager'
                      '.migrate_server', _fake_migrate_server)

    def _init_aggregate_with_host(self, aggr, aggr_name, zone, host):
        if not aggr:
            aggr = self.api.create_aggregate(self.context, aggr_name, zone)
        aggr = self.api.add_host_to_aggregate(self.context, aggr.id, host)
        return aggr


class ComputeVolumeTestCase(BaseTestCase):

    def setUp(self):
        super(ComputeVolumeTestCase, self).setUp()
        self.fetched_attempts = 0
        self.instance = {
            'id': 'fake',
            'uuid': uuids.instance,
            'name': 'fake',
            'root_device_name': '/dev/vda',
        }
        self.fake_volume = fake_block_device.FakeDbBlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                 'volume_id': uuids.volume_id, 'device_name': '/dev/vdb',
                 'connection_info': jsonutils.dumps({})})
        self.instance_object = objects.Instance._from_db_object(
                self.context, objects.Instance(),
                fake_instance.fake_db_instance())
        self.stub_out('nova.volume.cinder.API.get', lambda *a, **kw:
                       {'id': uuids.volume_id, 'size': 4,
                        'attach_status': 'detached'})
        self.stub_out('nova.virt.fake.FakeDriver.get_volume_connector',
                       lambda *a, **kw: None)
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                       lambda *a, **kw: {})
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                       lambda *a, **kw: None)
        self.stub_out('nova.volume.cinder.API.attach',
                       lambda *a, **kw: None)
        self.stub_out('nova.volume.cinder.API.detach',
                       lambda *a, **kw: None)
        self.stub_out('eventlet.greenthread.sleep',
                       lambda *a, **kw: None)

        def store_cinfo(context, *args, **kwargs):
            self.cinfo = jsonutils.loads(args[-1].get('connection_info'))
            return self.fake_volume

        self.stub_out('nova.db.block_device_mapping_create', store_cinfo)
        self.stub_out('nova.db.block_device_mapping_update', store_cinfo)

    def test_attach_volume_serial(self):
        fake_bdm = objects.BlockDeviceMapping(context=self.context,
                                              **self.fake_volume)
        with (mock.patch.object(cinder.API, 'get_volume_encryption_metadata',
                                return_value={})):
            instance = self._create_fake_instance_obj()
            self.compute.attach_volume(self.context, instance, bdm=fake_bdm)
            self.assertEqual(self.cinfo.get('serial'), uuids.volume_id)

    def test_attach_volume_raises(self):
        fake_bdm = objects.BlockDeviceMapping(**self.fake_volume)
        instance = self._create_fake_instance_obj()

        def fake_attach(*args, **kwargs):
            raise test.TestingException

        with test.nested(
            mock.patch.object(driver_block_device.DriverVolumeBlockDevice,
                              'attach'),
            mock.patch.object(cinder.API, 'unreserve_volume'),
            mock.patch.object(objects.BlockDeviceMapping,
                              'destroy')
        ) as (mock_attach, mock_unreserve, mock_destroy):
            mock_attach.side_effect = fake_attach
            self.assertRaises(
                    test.TestingException, self.compute.attach_volume,
                    self.context, instance, fake_bdm)
            self.assertTrue(mock_unreserve.called)
            self.assertTrue(mock_destroy.called)

    def test_detach_volume_api_raises(self):
        fake_bdm = objects.BlockDeviceMapping(**self.fake_volume)
        instance = self._create_fake_instance_obj()

        with test.nested(
            mock.patch.object(self.compute, '_driver_detach_volume'),
            mock.patch.object(self.compute.volume_api, 'detach'),
            mock.patch.object(objects.BlockDeviceMapping,
                              'get_by_volume_and_instance'),
            mock.patch.object(fake_bdm, 'destroy')
        ) as (mock_internal_detach, mock_detach, mock_get, mock_destroy):
            mock_detach.side_effect = test.TestingException
            mock_get.return_value = fake_bdm
            self.assertRaises(
                    test.TestingException, self.compute.detach_volume,
                    self.context, 'fake', instance, 'fake_id')
            mock_internal_detach.assert_called_once_with(self.context,
                                                         instance,
                                                         fake_bdm, {})
            self.assertTrue(mock_destroy.called)

    def test_await_block_device_created_too_slow(self):
        self.flags(block_device_allocate_retries=2)
        self.flags(block_device_allocate_retries_interval=0.1)

        def never_get(self, context, vol_id):
            return {
                'status': 'creating',
                'id': 'blah',
            }

        self.stub_out('nova.volume.cinder.API.get', never_get)
        self.assertRaises(exception.VolumeNotCreated,
                          self.compute._await_block_device_map_created,
                          self.context, '1')

    def test_await_block_device_created_failed(self):
        c = self.compute

        fake_result = {'status': 'error', 'id': 'blah'}
        with mock.patch.object(c.volume_api, 'get',
                               return_value=fake_result) as fake_get:
            self.assertRaises(exception.VolumeNotCreated,
                c._await_block_device_map_created,
                self.context, '1')
            fake_get.assert_called_once_with(self.context, '1')

    def test_await_block_device_created_slow(self):
        c = self.compute
        self.flags(block_device_allocate_retries=4)
        self.flags(block_device_allocate_retries_interval=0.1)

        def slow_get(cls, context, vol_id):
            if self.fetched_attempts < 2:
                self.fetched_attempts += 1
                return {
                    'status': 'creating',
                    'id': 'blah',
                }
            return {
                'status': 'available',
                'id': 'blah',
            }

        self.stub_out('nova.volume.cinder.API.get', slow_get)
        attempts = c._await_block_device_map_created(self.context, '1')
        self.assertEqual(attempts, 3)

    def test_await_block_device_created_retries_negative(self):
        c = self.compute
        self.flags(block_device_allocate_retries=-1)
        self.flags(block_device_allocate_retries_interval=0.1)

        def volume_get(self, context, vol_id):
            return {
                'status': 'available',
                'id': 'blah',
            }

        self.stub_out('nova.volume.cinder.API.get', volume_get)
        attempts = c._await_block_device_map_created(self.context, '1')
        self.assertEqual(1, attempts)

    def test_await_block_device_created_retries_zero(self):
        c = self.compute
        self.flags(block_device_allocate_retries=0)
        self.flags(block_device_allocate_retries_interval=0.1)

        def volume_get(self, context, vol_id):
            return {
                'status': 'available',
                'id': 'blah',
            }

        self.stub_out('nova.volume.cinder.API.get', volume_get)
        attempts = c._await_block_device_map_created(self.context, '1')
        self.assertEqual(1, attempts)

    def test_boot_volume_serial(self):
        self.stub_out('nova.volume.cinder.API.check_attach',
                       lambda *a, **kw: None)
        with (
            mock.patch.object(objects.BlockDeviceMapping, 'save')
        ) as mock_save:
            block_device_mapping = [
            block_device.BlockDeviceDict({
                'id': 1,
                'no_device': None,
                'source_type': 'volume',
                'destination_type': 'volume',
                'snapshot_id': None,
                'volume_id': uuids.volume_id,
                'device_name': '/dev/vdb',
                'volume_size': 55,
                'delete_on_termination': False,
            })]
            bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, block_device_mapping)
            prepped_bdm = self.compute._prep_block_device(
                    self.context, self.instance_object, bdms)
            self.assertEqual(2, mock_save.call_count)
            volume_driver_bdm = prepped_bdm['block_device_mapping'][0]
            self.assertEqual(volume_driver_bdm['connection_info']['serial'],
                             uuids.volume_id)

    def test_boot_volume_metadata(self, metadata=True):
        def volume_api_get(*args, **kwargs):
            if metadata:
                return {
                    'size': 1,
                    'volume_image_metadata': {'vol_test_key': 'vol_test_value',
                                              'min_ram': u'128',
                                              'min_disk': u'256',
                                              'size': u'536870912'
                                             },
                }
            else:
                return {}

        self.stub_out('nova.volume.cinder.API.get', volume_api_get)

        expected_no_metadata = {'min_disk': 0, 'min_ram': 0, 'properties': {},
                                'size': 0, 'status': 'active'}

        block_device_mapping = [{
            'id': 1,
            'device_name': 'vda',
            'no_device': None,
            'virtual_name': None,
            'snapshot_id': None,
            'volume_id': uuids.volume_id,
            'delete_on_termination': False,
        }]

        image_meta = self.compute_api._get_bdm_image_metadata(
            self.context, block_device_mapping)
        if metadata:
            self.assertEqual(image_meta['properties']['vol_test_key'],
                             'vol_test_value')
            self.assertEqual(128, image_meta['min_ram'])
            self.assertEqual(256, image_meta['min_disk'])
            self.assertEqual(units.Gi, image_meta['size'])
        else:
            self.assertEqual(expected_no_metadata, image_meta)

        # Test it with new-style BDMs
        block_device_mapping = [{
            'boot_index': 0,
            'source_type': 'volume',
            'destination_type': 'volume',
            'volume_id': uuids.volume_id,
            'delete_on_termination': False,
        }]

        image_meta = self.compute_api._get_bdm_image_metadata(
            self.context, block_device_mapping, legacy_bdm=False)
        if metadata:
            self.assertEqual(image_meta['properties']['vol_test_key'],
                             'vol_test_value')
            self.assertEqual(128, image_meta['min_ram'])
            self.assertEqual(256, image_meta['min_disk'])
            self.assertEqual(units.Gi, image_meta['size'])
        else:
            self.assertEqual(expected_no_metadata, image_meta)

    def test_boot_volume_no_metadata(self):
        self.test_boot_volume_metadata(metadata=False)

    def test_boot_image_metadata(self, metadata=True):
        def image_api_get(*args, **kwargs):
            if metadata:
                return {
                    'properties': {'img_test_key': 'img_test_value'}
                }
            else:
                return {}

        self.stub_out('nova.image.api.API.get', image_api_get)

        block_device_mapping = [{
            'boot_index': 0,
            'source_type': 'image',
            'destination_type': 'local',
            'image_id': "fake-image",
            'delete_on_termination': True,
        }]

        image_meta = self.compute_api._get_bdm_image_metadata(
            self.context, block_device_mapping, legacy_bdm=False)

        if metadata:
            self.assertEqual('img_test_value',
                             image_meta['properties']['img_test_key'])
        else:
            self.assertEqual(image_meta, {})

    def test_boot_image_no_metadata(self):
        self.test_boot_image_metadata(metadata=False)

    @mock.patch.object(time, 'time')
    @mock.patch.object(objects.InstanceList, 'get_by_host')
    @mock.patch.object(utils, 'last_completed_audit_period')
    @mock.patch.object(fake.FakeDriver, 'get_all_bw_counters')
    def test_poll_bandwidth_usage_not_implemented(self, mock_get_counter,
                                    mock_last, mock_get_host, mock_time):
        ctxt = context.get_admin_context()

        # Following methods will be called
        # Note - time called two more times from Log
        mock_last.return_value = (0, 0)
        mock_time.side_effect = (10, 20, 21)

        mock_get_host.return_value = []
        mock_get_counter.side_effect = NotImplementedError

        self.flags(bandwidth_poll_interval=1)
        self.compute._poll_bandwidth_usage(ctxt)
        # A second call won't call the stubs again as the bandwidth
        # poll is now disabled
        self.compute._poll_bandwidth_usage(ctxt)

        mock_get_counter.assert_called_once_with([])
        mock_last.assert_called_once_with()
        mock_get_host.assert_called_once_with(ctxt, 'fake-mini',
                                              use_slave=True)

    @mock.patch.object(objects.InstanceList, 'get_by_host')
    @mock.patch.object(objects.BlockDeviceMappingList,
                       'get_by_instance_uuid')
    def test_get_host_volume_bdms(self, mock_get_by_inst, mock_get_by_host):
        fake_instance = mock.Mock(uuid=uuids.volume_instance)
        mock_get_by_host.return_value = [fake_instance]

        volume_bdm = mock.Mock(id=1, is_volume=True)
        not_volume_bdm = mock.Mock(id=2, is_volume=False)
        mock_get_by_inst.return_value = [volume_bdm, not_volume_bdm]

        expected_host_bdms = [{'instance': fake_instance,
                               'instance_bdms': [volume_bdm]}]

        got_host_bdms = self.compute._get_host_volume_bdms('fake-context')
        mock_get_by_host.assert_called_once_with('fake-context',
                                                 self.compute.host,
                                                 use_slave=False)
        mock_get_by_inst.assert_called_once_with('fake-context',
                                                 uuids.volume_instance,
                                                 use_slave=False)
        self.assertEqual(expected_host_bdms, got_host_bdms)

    @mock.patch.object(utils, 'last_completed_audit_period')
    @mock.patch.object(compute_manager.ComputeManager, '_get_host_volume_bdms')
    def test_poll_volume_usage_disabled(self, mock_get, mock_last):
        # None of the mocks should be called.
        ctxt = 'MockContext'

        self.flags(volume_usage_poll_interval=0)
        self.compute._poll_volume_usage(ctxt)

        self.assertFalse(mock_get.called)
        self.assertFalse(mock_last.called)

    @mock.patch.object(compute_manager.ComputeManager, '_get_host_volume_bdms')
    @mock.patch.object(fake.FakeDriver, 'get_all_volume_usage')
    def test_poll_volume_usage_returns_no_vols(self, mock_get_usage,
                                               mock_get_bdms):
        ctxt = 'MockContext'
        # Following methods are called.
        mock_get_bdms.return_value = []

        self.flags(volume_usage_poll_interval=10)
        self.compute._poll_volume_usage(ctxt)

        mock_get_bdms.assert_called_once_with(ctxt, use_slave=True)

    @mock.patch.object(compute_manager.ComputeManager, '_get_host_volume_bdms')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_update_volume_usage_cache')
    @mock.patch.object(fake.FakeDriver, 'get_all_volume_usage')
    def test_poll_volume_usage_with_data(self, mock_get_usage, mock_update,
                                         mock_get_bdms):
        ctxt = 'MockContext'
        mock_get_usage.side_effect = lambda x, y: [3, 4]
        # All the mocks are called
        mock_get_bdms.return_value = [1, 2]

        self.flags(volume_usage_poll_interval=10)
        self.compute._poll_volume_usage(ctxt)

        mock_get_bdms.assert_called_once_with(ctxt, use_slave=True)
        mock_update.assert_called_once_with(ctxt, [3, 4])

    @mock.patch.object(objects.BlockDeviceMapping,
                       'get_by_volume_and_instance')
    @mock.patch.object(fake.FakeDriver, 'block_stats')
    @mock.patch.object(compute_manager.ComputeManager, '_get_host_volume_bdms')
    @mock.patch.object(fake.FakeDriver, 'get_all_volume_usage')
    @mock.patch.object(fake.FakeDriver, 'instance_exists')
    def test_detach_volume_usage(self, mock_exists, mock_get_all,
                                 mock_get_bdms, mock_stats, mock_get):
        # Test that detach volume update the volume usage cache table correctly
        instance = self._create_fake_instance_obj()
        bdm = objects.BlockDeviceMapping(context=self.context,
                                         id=1, device_name='/dev/vdb',
                                         connection_info='{}',
                                         instance_uuid=instance['uuid'],
                                         source_type='volume',
                                         destination_type='volume',
                                         no_device=False,
                                         disk_bus='foo',
                                         device_type='disk',
                                         volume_size=1,
                                         volume_id=uuids.volume_id)
        host_volume_bdms = {'id': 1, 'device_name': '/dev/vdb',
               'connection_info': '{}', 'instance_uuid': instance['uuid'],
               'volume_id': uuids.volume_id}
        mock_get.return_value = bdm.obj_clone()
        mock_stats.return_value = [1, 30, 1, 20, None]
        mock_get_bdms.return_value = host_volume_bdms
        mock_get_all.return_value = [{'volume': uuids.volume_id,
                                      'rd_req': 1,
                                      'rd_bytes': 10,
                                      'wr_req': 1,
                                      'wr_bytes': 5,
                                      'instance': instance}]
        mock_exists.return_value = True

        def fake_get_volume_encryption_metadata(self, context, volume_id):
            return {}
        self.stub_out('nova.volume.cinder.API.get_volume_encryption_metadata',
                       fake_get_volume_encryption_metadata)

        self.compute.attach_volume(self.context, instance, bdm)

        # Poll volume usage & then detach the volume. This will update the
        # total fields in the volume usage cache.
        self.flags(volume_usage_poll_interval=10)
        self.compute._poll_volume_usage(self.context)
        # Check that a volume.usage and volume.attach notification was sent
        self.assertEqual(2, len(fake_notifier.NOTIFICATIONS))

        self.compute.detach_volume(self.context, uuids.volume_id, instance)

        # Check that volume.attach, 2 volume.usage, and volume.detach
        # notifications were sent
        self.assertEqual(4, len(fake_notifier.NOTIFICATIONS))
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual('compute.instance.volume.attach', msg.event_type)
        msg = fake_notifier.NOTIFICATIONS[2]
        self.assertEqual('volume.usage', msg.event_type)
        payload = msg.payload
        self.assertEqual(instance['uuid'], payload['instance_id'])
        self.assertEqual('fake', payload['user_id'])
        self.assertEqual('fake', payload['tenant_id'])
        self.assertEqual(1, payload['reads'])
        self.assertEqual(30, payload['read_bytes'])
        self.assertEqual(1, payload['writes'])
        self.assertEqual(20, payload['write_bytes'])
        self.assertIsNone(payload['availability_zone'])
        msg = fake_notifier.NOTIFICATIONS[3]
        self.assertEqual('compute.instance.volume.detach', msg.event_type)

        # Check the database for the
        volume_usages = db.vol_get_usage_by_time(self.context, 0)
        self.assertEqual(1, len(volume_usages))
        volume_usage = volume_usages[0]
        self.assertEqual(0, volume_usage['curr_reads'])
        self.assertEqual(0, volume_usage['curr_read_bytes'])
        self.assertEqual(0, volume_usage['curr_writes'])
        self.assertEqual(0, volume_usage['curr_write_bytes'])
        self.assertEqual(1, volume_usage['tot_reads'])
        self.assertEqual(30, volume_usage['tot_read_bytes'])
        self.assertEqual(1, volume_usage['tot_writes'])
        self.assertEqual(20, volume_usage['tot_write_bytes'])

        mock_get.assert_called_once_with(self.context, uuids.volume_id,
                                         instance.uuid)
        mock_stats.assert_called_once_with(instance, 'vdb')
        mock_get_bdms.assert_called_once_with(self.context, use_slave=True)
        mock_get_all(self.context, host_volume_bdms)
        mock_exists.assert_called_once_with(mock.ANY)

    def test_prepare_image_mapping(self):
        swap_size = 1
        ephemeral_size = 1
        instance_type = {'swap': swap_size,
                         'ephemeral_gb': ephemeral_size}
        mappings = [
                {'virtual': 'ami', 'device': 'sda1'},
                {'virtual': 'root', 'device': '/dev/sda1'},

                {'virtual': 'swap', 'device': 'sdb4'},

                {'virtual': 'ephemeral0', 'device': 'sdc1'},
                {'virtual': 'ephemeral1', 'device': 'sdc2'},
        ]

        preped_bdm = self.compute_api._prepare_image_mapping(
            instance_type, mappings)

        expected_result = [
            {
                'device_name': '/dev/sdb4',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': 'swap',
                'boot_index': -1,
                'volume_size': swap_size
            },
            {
                'device_name': '/dev/sdc1',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': CONF.default_ephemeral_format,
                'boot_index': -1,
                'volume_size': ephemeral_size
            },
            {
                'device_name': '/dev/sdc2',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': CONF.default_ephemeral_format,
                'boot_index': -1,
                'volume_size': ephemeral_size
            }
        ]

        for expected, got in zip(expected_result, preped_bdm):
            self.assertThat(expected, matchers.IsSubDictOf(got))

    def test_validate_bdm(self):
        def fake_get(self, context, res_id):
            return {'id': res_id, 'size': 4}

        def fake_check_attach(*args, **kwargs):
            pass

        self.stub_out('nova.volume.cinder.API.get', fake_get)
        self.stub_out('nova.volume.cinder.API.get_snapshot', fake_get)
        self.stub_out('nova.volume.cinder.API.check_attach',
                       fake_check_attach)

        volume_id = '55555555-aaaa-bbbb-cccc-555555555555'
        snapshot_id = '66666666-aaaa-bbbb-cccc-555555555555'
        image_id = '77777777-aaaa-bbbb-cccc-555555555555'

        instance = self._create_fake_instance_obj()
        instance_type = {'swap': 1, 'ephemeral_gb': 2}
        mappings = [
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sdb4',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': 'swap',
                'boot_index': -1,
                'volume_size': 1
            }, anon=True),
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sda1',
                'source_type': 'volume',
                'destination_type': 'volume',
                'device_type': 'disk',
                'volume_id': volume_id,
                'guest_format': None,
                'boot_index': 1,
            }, anon=True),
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sda2',
                'source_type': 'snapshot',
                'destination_type': 'volume',
                'snapshot_id': snapshot_id,
                'device_type': 'disk',
                'guest_format': None,
                'volume_size': 6,
                'boot_index': 0,
            }, anon=True),
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sda3',
                'source_type': 'image',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': None,
                'boot_index': 2,
                'volume_size': 1
            }, anon=True)
        ]
        mappings = block_device_obj.block_device_make_list_from_dicts(
                self.context, mappings)

        # Make sure it passes at first
        self.compute_api._validate_bdm(self.context, instance,
                                       instance_type, mappings)
        self.assertEqual(4, mappings[1].volume_size)
        self.assertEqual(6, mappings[2].volume_size)

        # Boot sequence
        mappings[2].boot_index = 2
        self.assertRaises(exception.InvalidBDMBootSequence,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings)
        mappings[2].boot_index = 0

        # number of local block_devices
        self.flags(max_local_block_devices=1)
        self.assertRaises(exception.InvalidBDMLocalsLimit,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings)
        ephemerals = [
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/vdb',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': None,
                'boot_index': -1,
                'volume_size': 1
            }, anon=True),
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/vdc',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': None,
                'boot_index': -1,
                'volume_size': 1
            }, anon=True)
        ]
        ephemerals = block_device_obj.block_device_make_list_from_dicts(
                self.context, ephemerals)

        self.flags(max_local_block_devices=4)
        # More ephemerals are OK as long as they are not over the size limit
        mappings_ = mappings[:]
        mappings_.objects.extend(ephemerals)
        self.compute_api._validate_bdm(self.context, instance,
                                       instance_type, mappings_)

        # Ephemerals over the size limit
        ephemerals[0].volume_size = 3
        mappings_ = mappings[:]
        mappings_.objects.extend(ephemerals)
        self.assertRaises(exception.InvalidBDMEphemeralSize,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings_)

        # Swap over the size limit
        mappings[0].volume_size = 3
        self.assertRaises(exception.InvalidBDMSwapSize,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings)
        mappings[0].volume_size = 1

        additional_swap = [
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/vdb',
                'source_type': 'blank',
                'destination_type': 'local',
                'device_type': 'disk',
                'guest_format': 'swap',
                'boot_index': -1,
                'volume_size': 1
            }, anon=True)
        ]
        additional_swap = block_device_obj.block_device_make_list_from_dicts(
                self.context, additional_swap)

        # More than one swap
        mappings_ = mappings[:]
        mappings_.objects.extend(additional_swap)
        self.assertRaises(exception.InvalidBDMFormat,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings_)

        image_no_size = [
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sda4',
                'source_type': 'image',
                'image_id': image_id,
                'destination_type': 'volume',
                'boot_index': -1,
                'volume_size': None,
            }, anon=True)
        ]
        image_no_size = block_device_obj.block_device_make_list_from_dicts(
                self.context, image_no_size)
        mappings_ = mappings[:]
        mappings_.objects.extend(image_no_size)
        self.assertRaises(exception.InvalidBDM,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings_)

        # blank device without a specified size fails
        blank_no_size = [
            fake_block_device.FakeDbBlockDeviceDict({
                'device_name': '/dev/sda4',
                'source_type': 'blank',
                'destination_type': 'volume',
                'boot_index': -1,
                'volume_size': None,
            }, anon=True)
        ]
        blank_no_size = block_device_obj.block_device_make_list_from_dicts(
                self.context, blank_no_size)
        mappings_ = mappings[:]
        mappings_.objects.extend(blank_no_size)
        self.assertRaises(exception.InvalidBDM,
                          self.compute_api._validate_bdm,
                          self.context, instance, instance_type,
                          mappings_)

    def test_validate_bdm_with_more_than_one_default(self):
        instance_type = {'swap': 1, 'ephemeral_gb': 1}
        all_mappings = [fake_block_device.FakeDbBlockDeviceDict({
                         'id': 1,
                         'no_device': None,
                         'source_type': 'volume',
                         'destination_type': 'volume',
                         'snapshot_id': None,
                         'volume_size': 1,
                         'device_name': 'vda',
                         'boot_index': 0,
                         'delete_on_termination': False}, anon=True),
                        fake_block_device.FakeDbBlockDeviceDict({
                         'device_name': '/dev/vdb',
                         'source_type': 'blank',
                         'destination_type': 'local',
                         'device_type': 'disk',
                         'volume_size': None,
                         'boot_index': -1}, anon=True),
                        fake_block_device.FakeDbBlockDeviceDict({
                         'device_name': '/dev/vdc',
                         'source_type': 'blank',
                         'destination_type': 'local',
                         'device_type': 'disk',
                         'volume_size': None,
                         'boot_index': -1}, anon=True)]
        all_mappings = block_device_obj.block_device_make_list_from_dicts(
                self.context, all_mappings)

        self.assertRaises(exception.InvalidBDMEphemeralSize,
                          self.compute_api._validate_bdm,
                          self.context, self.instance,
                          instance_type, all_mappings)

    @mock.patch.object(cinder.API, 'get')
    @mock.patch.object(cinder.API, 'check_availability_zone')
    @mock.patch.object(cinder.API, 'reserve_volume',
                       side_effect=exception.InvalidVolume(reason='error'))
    def test_validate_bdm_media_service_invalid_volume(self, mock_reserve_vol,
                                                       mock_check_av_zone,
                                                       mock_get):
        volume_id = uuids.volume_id
        instance_type = {'swap': 1, 'ephemeral_gb': 1}
        bdms = [fake_block_device.FakeDbBlockDeviceDict({
                        'id': 1,
                        'no_device': None,
                        'source_type': 'volume',
                        'destination_type': 'volume',
                        'snapshot_id': None,
                        'volume_id': volume_id,
                        'device_name': 'vda',
                        'boot_index': 0,
                        'delete_on_termination': False}, anon=True)]
        bdms = block_device_obj.block_device_make_list_from_dicts(self.context,
                                                                  bdms)

        # We test a list of invalid status values that should result
        # in an InvalidVolume exception being raised.
        status_values = (
            # First two check that the status is 'available'.
            ('creating', 'detached'),
            ('error', 'detached'),
            # Checks that the attach_status is 'detached'.
            ('available', 'attached')
        )

        for status, attach_status in status_values:
            if attach_status == 'attached':
                mock_get.return_value = {'id': volume_id,
                                         'status': status,
                                         'attach_status': attach_status,
                                         'multiattach': False,
                                         'attachments': {}}

            else:
                mock_get.return_value = {'id': volume_id,
                                         'status': status,
                                         'attach_status': attach_status,
                                         'multiattach': False}

            self.assertRaises(exception.InvalidVolume,
                              self.compute_api._validate_bdm,
                              self.context, self.instance,
                              instance_type, bdms)
            mock_get.assert_called_with(self.context, volume_id)

    @mock.patch.object(cinder.API, 'get')
    def test_validate_bdm_media_service_volume_not_found(self, mock_get):
        volume_id = uuids.volume_id
        instance_type = {'swap': 1, 'ephemeral_gb': 1}
        bdms = [fake_block_device.FakeDbBlockDeviceDict({
                         'id': 1,
                         'no_device': None,
                         'source_type': 'volume',
                         'destination_type': 'volume',
                         'snapshot_id': None,
                         'volume_id': volume_id,
                         'device_name': 'vda',
                         'boot_index': 0,
                         'delete_on_termination': False}, anon=True)]
        bdms = block_device_obj.block_device_make_list_from_dicts(self.context,
                                                                  bdms)

        mock_get.side_effect = exception.VolumeNotFound(volume_id)
        self.assertRaises(exception.InvalidBDMVolume,
                          self.compute_api._validate_bdm,
                          self.context, self.instance,
                          instance_type, bdms)

    @mock.patch.object(cinder.API, 'get')
    @mock.patch.object(cinder.API, 'check_availability_zone')
    def test_validate_bdm_media_service_valid(self, mock_check_av_zone,
                                              mock_get):
        volume_id = uuids.volume_id
        instance_type = {'swap': 1, 'ephemeral_gb': 1}
        bdms = [fake_block_device.FakeDbBlockDeviceDict({
                         'id': 1,
                         'no_device': None,
                         'source_type': 'volume',
                         'destination_type': 'volume',
                         'snapshot_id': None,
                         'volume_id': volume_id,
                         'device_name': 'vda',
                         'boot_index': 0,
                         'delete_on_termination': False}, anon=True)]
        bdms = block_device_obj.block_device_make_list_from_dicts(self.context,
                                                                  bdms)
        volume = {'id': volume_id,
                  'status': 'available',
                  'attach_status': 'detached',
                  'multiattach': False}

        mock_get.return_value = volume
        self.compute_api._validate_bdm(self.context, self.instance,
                                       instance_type, bdms)
        mock_get.assert_called_once_with(self.context, volume_id)
        mock_check_av_zone.assert_called_once_with(self.context, volume,
                                                   self.instance)

    def test_volume_snapshot_create(self):
        self.assertRaises(messaging.ExpectedException,
                self.compute.volume_snapshot_create, self.context,
                self.instance_object, 'fake_id', {})

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(NotImplementedError,
                self.compute.volume_snapshot_create, self.context,
                self.instance_object, 'fake_id', {})

    def test_volume_snapshot_delete(self):
        self.assertRaises(messaging.ExpectedException,
                self.compute.volume_snapshot_delete, self.context,
                self.instance_object, 'fake_id', 'fake_id2', {})

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(NotImplementedError,
                self.compute.volume_snapshot_delete, self.context,
                self.instance_object, 'fake_id', 'fake_id2', {})

    @mock.patch.object(cinder.API, 'create',
                       side_effect=exception.OverQuota(overs='volumes'))
    def test_prep_block_device_over_quota_failure(self, mock_create):
        instance = self._create_fake_instance_obj()
        bdms = [
            block_device.BlockDeviceDict({
                'boot_index': 0,
                'guest_format': None,
                'connection_info': None,
                'device_type': u'disk',
                'source_type': 'image',
                'destination_type': 'volume',
                'volume_size': 1,
                'image_id': 1,
                'device_name': '/dev/vdb',
            })]
        bdms = block_device_obj.block_device_make_list_from_dicts(
            self.context, bdms)
        self.assertRaises(exception.VolumeLimitExceeded,
                          compute_manager.ComputeManager()._prep_block_device,
                          self.context, instance, bdms)
        self.assertTrue(mock_create.called)

    @mock.patch.object(nova.virt.block_device, 'get_swap')
    @mock.patch.object(nova.virt.block_device, 'convert_blanks')
    @mock.patch.object(nova.virt.block_device, 'convert_images')
    @mock.patch.object(nova.virt.block_device, 'convert_snapshots')
    @mock.patch.object(nova.virt.block_device, 'convert_volumes')
    @mock.patch.object(nova.virt.block_device, 'convert_ephemerals')
    @mock.patch.object(nova.virt.block_device, 'convert_swap')
    @mock.patch.object(nova.virt.block_device, 'attach_block_devices')
    def test_prep_block_device_with_blanks(self, attach_block_devices,
                                           convert_swap, convert_ephemerals,
                                           convert_volumes, convert_snapshots,
                                           convert_images, convert_blanks,
                                           get_swap):
        instance = self._create_fake_instance_obj()
        instance['root_device_name'] = '/dev/vda'
        root_volume = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'image',
                'destination_type': 'volume',
                'image_id': 'fake-image-id-1',
                'volume_size': 1,
                'boot_index': 0}))
        blank_volume1 = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'volume',
                'volume_size': 1,
                'boot_index': 1}))
        blank_volume2 = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'volume',
                'volume_size': 1,
                'boot_index': 2}))
        bdms = [blank_volume1, blank_volume2, root_volume]

        def fake_attach_block_devices(bdm, *args, **kwargs):
            return bdm

        convert_swap.return_value = []
        convert_ephemerals.return_value = []
        convert_volumes.return_value = [blank_volume1, blank_volume2]
        convert_snapshots.return_value = []
        convert_images.return_value = [root_volume]
        convert_blanks.return_value = []
        attach_block_devices.side_effect = fake_attach_block_devices
        get_swap.return_value = []

        expected_block_device_info = {
            'root_device_name': '/dev/vda',
            'swap': [],
            'ephemerals': [],
            'block_device_mapping': bdms
        }

        manager = compute_manager.ComputeManager()
        manager.use_legacy_block_device_info = False
        mock_bdm_saves = [mock.patch.object(bdm, 'save') for bdm in bdms]
        with test.nested(*mock_bdm_saves):
            block_device_info = manager._prep_block_device(self.context,
                                                           instance, bdms)

            for bdm in bdms:
                bdm.save.assert_called_once_with()
                self.assertIsNotNone(bdm.device_name)

        convert_swap.assert_called_once_with(bdms)
        convert_ephemerals.assert_called_once_with(bdms)
        bdm_args = tuple(bdms)
        convert_volumes.assert_called_once_with(bdm_args)
        convert_snapshots.assert_called_once_with(bdm_args)
        convert_images.assert_called_once_with(bdm_args)
        convert_blanks.assert_called_once_with(bdm_args)

        self.assertEqual(expected_block_device_info, block_device_info)
        self.assertEqual(1, attach_block_devices.call_count)
        get_swap.assert_called_once_with([])


class ComputeTestCase(BaseTestCase):
    def setUp(self):
        super(ComputeTestCase, self).setUp()
        self.useFixture(fixtures.SpawnIsSynchronousFixture())

    def test_wrap_instance_fault(self):
        inst = {"uuid": uuids.instance}

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stub_out('nova.compute.utils.add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance):
            raise NotImplementedError()

        self.assertRaises(NotImplementedError, failer,
                          self.compute, self.context, instance=inst)

        self.assertTrue(called['fault_added'])

    def test_wrap_instance_fault_instance_in_args(self):
        inst = {"uuid": uuids.instance}

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stub_out('nova.compute.utils.add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance):
            raise NotImplementedError()

        self.assertRaises(NotImplementedError, failer,
                          self.compute, self.context, inst)

        self.assertTrue(called['fault_added'])

    def test_wrap_instance_fault_no_instance(self):
        inst = {"uuid": uuids.instance}

        called = {'fault_added': False}

        def did_it_add_fault(*args):
            called['fault_added'] = True

        self.stub_out('nova.utils.add_instance_fault_from_exc',
                       did_it_add_fault)

        @compute_manager.wrap_instance_fault
        def failer(self2, context, instance):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.assertRaises(exception.InstanceNotFound, failer,
                          self.compute, self.context, inst)

        self.assertFalse(called['fault_added'])

    def test_object_compat(self):
        db_inst = fake_instance.fake_db_instance()

        @compute_manager.object_compat
        def test_fn(_self, context, instance):
            self.assertIsInstance(instance, objects.Instance)
            self.assertEqual(instance.uuid, db_inst['uuid'])
            self.assertEqual(instance.metadata, db_inst['metadata'])
            self.assertEqual(instance.system_metadata,
                             db_inst['system_metadata'])
        test_fn(None, self.context, instance=db_inst)

    def test_object_compat_no_metas(self):
        # Tests that we don't try to set metadata/system_metadata on the
        # instance object using fields that aren't in the db object.
        db_inst = fake_instance.fake_db_instance()
        db_inst.pop('metadata', None)
        db_inst.pop('system_metadata', None)

        @compute_manager.object_compat
        def test_fn(_self, context, instance):
            self.assertIsInstance(instance, objects.Instance)
            self.assertEqual(instance.uuid, db_inst['uuid'])
            self.assertNotIn('metadata', instance)
            self.assertNotIn('system_metadata', instance)
        test_fn(None, self.context, instance=db_inst)

    def test_object_compat_more_positional_args(self):
        db_inst = fake_instance.fake_db_instance()

        @compute_manager.object_compat
        def test_fn(_self, context, instance, pos_arg_1, pos_arg_2):
            self.assertIsInstance(instance, objects.Instance)
            self.assertEqual(instance.uuid, db_inst['uuid'])
            self.assertEqual(instance.metadata, db_inst['metadata'])
            self.assertEqual(instance.system_metadata,
                             db_inst['system_metadata'])
            self.assertEqual(pos_arg_1, 'fake_pos_arg1')
            self.assertEqual(pos_arg_2, 'fake_pos_arg2')

        test_fn(None, self.context, db_inst, 'fake_pos_arg1', 'fake_pos_arg2')

    def test_create_instance_with_img_ref_associates_config_drive(self):
        # Make sure create associates a config drive.

        instance = self._create_fake_instance_obj(
                        params={'config_drive': '1234', })

        try:
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                                                {}, block_device_mapping=[])
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertTrue(instance['config_drive'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_create_instance_associates_config_drive(self):
        # Make sure create associates a config drive.

        instance = self._create_fake_instance_obj(
                        params={'config_drive': '1234', })

        try:
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                                                {}, block_device_mapping=[])
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertTrue(instance['config_drive'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_create_instance_unlimited_memory(self):
        # Default of memory limit=None is unlimited.
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        params = {"flavor": {"memory_mb": 999999999999}}
        filter_properties = {'limits': {'memory_mb': None}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                                            filter_properties,
                                            block_device_mapping=[])
        self.assertEqual(999999999999, self.rt.compute_node.memory_mb_used)

    def test_create_instance_unlimited_disk(self):
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        params = {"root_gb": 999999999999,
                  "ephemeral_gb": 99999999999}
        filter_properties = {'limits': {'disk_gb': None}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                filter_properties, block_device_mapping=[])

    def test_create_multiple_instances_then_starve(self):
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        limits = {'memory_mb': 4096, 'disk_gb': 1000}
        params = {"flavor": {"memory_mb": 1024, "root_gb": 128,
                             "ephemeral_gb": 128}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                {}, block_device_mapping=[], limits=limits)
        self.assertEqual(1024, self.rt.compute_node.memory_mb_used)
        self.assertEqual(256, self.rt.compute_node.local_gb_used)

        params = {"flavor": {"memory_mb": 2048, "root_gb": 256,
                             "ephemeral_gb": 256}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                {}, block_device_mapping=[], limits=limits)
        self.assertEqual(3072, self.rt.compute_node.memory_mb_used)
        self.assertEqual(768, self.rt.compute_node.local_gb_used)

        params = {"flavor": {"memory_mb": 8192, "root_gb": 8192,
                             "ephemeral_gb": 8192}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance,
                {}, {}, {}, block_device_mapping=[], limits=limits)
        self.assertEqual(3072, self.rt.compute_node.memory_mb_used)
        self.assertEqual(768, self.rt.compute_node.local_gb_used)

    def test_create_multiple_instance_with_neutron_port(self):
        instance_type = flavors.get_default_flavor()

        def fake_is_neutron():
            return True
        self.stub_out('nova.utils.is_neutron', fake_is_neutron)
        requested_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(port_id=uuids.port_instance)])
        self.assertRaises(exception.MultiplePortsNotApplicable,
                          self.compute_api.create,
                          self.context,
                          instance_type=instance_type,
                          image_href=None,
                          max_count=2,
                          requested_networks=requested_networks)

    def test_create_instance_with_oversubscribed_ram(self):
        # Test passing of oversubscribed ram policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_mem_mb = resources['memory_mb']

        oversub_limit_mb = total_mem_mb * 1.5
        instance_mb = int(total_mem_mb * 1.45)

        # build an instance, specifying an amount of memory that exceeds
        # total_mem_mb, but is less than the oversubscribed limit:
        params = {"flavor": {"memory_mb": instance_mb, "root_gb": 128,
                             "ephemeral_gb": 128}}
        instance = self._create_fake_instance_obj(params)

        limits = {'memory_mb': oversub_limit_mb}
        filter_properties = {'limits': limits}
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                filter_properties, block_device_mapping=[])

        self.assertEqual(instance_mb, self.rt.compute_node.memory_mb_used)

    def test_create_instance_with_oversubscribed_ram_fail(self):
        """Test passing of oversubscribed ram policy from the scheduler, but
        with insufficient memory.
        """
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_mem_mb = resources['memory_mb']

        oversub_limit_mb = total_mem_mb * 1.5
        instance_mb = int(total_mem_mb * 1.55)

        # build an instance, specifying an amount of memory that exceeds
        # both total_mem_mb and the oversubscribed limit:
        params = {"flavor": {"memory_mb": instance_mb, "root_gb": 128,
                             "ephemeral_gb": 128}}
        instance = self._create_fake_instance_obj(params)

        filter_properties = {'limits': {'memory_mb': oversub_limit_mb}}

        self.compute.build_and_run_instance(self.context, instance,
                          {}, {}, filter_properties, block_device_mapping=[])

    def test_create_instance_with_oversubscribed_cpu(self):
        # Test passing of oversubscribed cpu policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())
        limits = {'vcpu': 3}
        filter_properties = {'limits': limits}

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        self.assertEqual(1, resources['vcpus'])

        # build an instance, specifying an amount of memory that exceeds
        # total_mem_mb, but is less than the oversubscribed limit:
        params = {"flavor": {"memory_mb": 10, "root_gb": 1,
                             "ephemeral_gb": 1, "vcpus": 2}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                filter_properties, block_device_mapping=[])

        self.assertEqual(2, self.rt.compute_node.vcpus_used)

        # create one more instance:
        params = {"flavor": {"memory_mb": 10, "root_gb": 1,
                             "ephemeral_gb": 1, "vcpus": 1}}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                filter_properties, block_device_mapping=[])

        self.assertEqual(3, self.rt.compute_node.vcpus_used)

        # delete the instance:
        instance['vm_state'] = vm_states.DELETED
        self.rt.update_usage(self.context,
                instance=instance)

        self.assertEqual(2, self.rt.compute_node.vcpus_used)

        # now oversubscribe vcpus and fail:
        params = {"flavor": {"memory_mb": 10, "root_gb": 1,
                             "ephemeral_gb": 1, "vcpus": 2}}
        instance = self._create_fake_instance_obj(params)

        limits = {'vcpu': 3}
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                {}, block_device_mapping=[], limits=limits)
        self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_create_instance_with_oversubscribed_disk(self):
        # Test passing of oversubscribed disk policy from the scheduler.

        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_disk_gb = resources['local_gb']

        oversub_limit_gb = total_disk_gb * 1.5
        instance_gb = int(total_disk_gb * 1.45)

        # build an instance, specifying an amount of disk that exceeds
        # total_disk_gb, but is less than the oversubscribed limit:
        params = {"flavor": {"root_gb": instance_gb, "memory_mb": 10}}
        instance = self._create_fake_instance_obj(params)

        limits = {'disk_gb': oversub_limit_gb}
        filter_properties = {'limits': limits}
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                filter_properties, block_device_mapping=[])

        self.assertEqual(instance_gb, self.rt.compute_node.local_gb_used)

    def test_create_instance_with_oversubscribed_disk_fail(self):
        """Test passing of oversubscribed disk policy from the scheduler, but
        with insufficient disk.
        """
        self.flags(reserved_host_disk_mb=0, reserved_host_memory_mb=0)
        self.rt.update_available_resource(self.context.elevated())

        # get total memory as reported by virt driver:
        resources = self.compute.driver.get_available_resource(NODENAME)
        total_disk_gb = resources['local_gb']

        oversub_limit_gb = total_disk_gb * 1.5
        instance_gb = int(total_disk_gb * 1.55)

        # build an instance, specifying an amount of disk that exceeds
        # total_disk_gb, but is less than the oversubscribed limit:
        params = {"flavor": {"root_gb": instance_gb, "memory_mb": 10}}
        instance = self._create_fake_instance_obj(params)

        limits = {'disk_gb': oversub_limit_gb}
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                {}, block_device_mapping=[], limits=limits)
        self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_create_instance_without_node_param(self):
        instance = self._create_fake_instance_obj({'node': None})

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instances = db.instance_get_all(self.context)
        instance = instances[0]

        self.assertEqual(NODENAME, instance['node'])

    def test_create_instance_no_image(self):
        # Create instance with no image provided.
        params = {'image_ref': ''}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        self._assert_state({'vm_state': vm_states.ACTIVE,
                            'task_state': None})

    @testtools.skipIf(test_utils.is_osx(),
                      'IPv6 pretty-printing broken on OSX, see bug 1409135')
    def test_default_access_ip(self):
        self.flags(default_access_ip_network_name='test1')
        fake_network.unset_stub_network_methods(self)
        instance = self._create_fake_instance_obj()

        orig_update = self.compute._instance_update

        # Make sure the access_ip_* updates happen in the same DB
        # update as the set to ACTIVE.
        def _instance_update(self, ctxt, instance_uuid, **kwargs):
            if kwargs.get('vm_state', None) == vm_states.ACTIVE:
                self.assertEqual(kwargs['access_ip_v4'], '192.168.1.100')
                self.assertEqual(kwargs['access_ip_v6'], '2001:db8:0:1::1')
            return orig_update(ctxt, instance_uuid, **kwargs)

        self.stub_out('nova.compute.manager.ComputeManager._instance_update',
                      _instance_update)

        try:
            self.compute.build_and_run_instance(self.context, instance, {},
                    {}, {}, block_device_mapping=[])
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertEqual(instance['access_ip_v4'], '192.168.1.100')
            self.assertEqual(instance['access_ip_v6'],
                             '2001:db8:0:1:dcad:beff:feef:1')
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_no_default_access_ip(self):
        instance = self._create_fake_instance_obj()

        try:
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                    {}, block_device_mapping=[])
            instances = db.instance_get_all(self.context)
            instance = instances[0]

            self.assertFalse(instance['access_ip_v4'])
            self.assertFalse(instance['access_ip_v6'])
        finally:
            db.instance_destroy(self.context, instance['uuid'])

    def test_fail_to_schedule_persists(self):
        # check the persistence of the ERROR(scheduling) state.
        params = {'vm_state': vm_states.ERROR,
                  'task_state': task_states.SCHEDULING}
        self._create_fake_instance_obj(params=params)
        # check state is failed even after the periodic poll
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': task_states.SCHEDULING})

    def test_run_instance_setup_block_device_mapping_fail(self):
        """block device mapping failure test.

        Make sure that when there is a block device mapping problem,
        the instance goes to ERROR state, cleaning the task state
        """
        def fake(*args, **kwargs):
            raise exception.InvalidBDM()
        self.stub_out('nova.compute.manager.ComputeManager'
                      '._prep_block_device', fake)
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(
                          self.context, instance=instance, image={},
                          request_spec={}, block_device_mapping=[],
                          filter_properties={}, requested_networks=[],
                          injected_files=None, admin_password=None,
                          node=None)
        # check state is failed even after the periodic poll
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})

    @mock.patch('nova.compute.manager.ComputeManager._prep_block_device',
                side_effect=exception.OverQuota(overs='volumes'))
    def test_setup_block_device_over_quota_fail(self, mock_prep_block_dev):
        """block device mapping over quota failure test.

        Make sure when we're over volume quota according to Cinder client, the
        appropriate exception is raised and the instances to ERROR state,
        cleaning the task state.
        """
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(
                          self.context, instance=instance, request_spec={},
                          filter_properties={}, requested_networks=[],
                          injected_files=None, admin_password=None,
                          node=None, block_device_mapping=[], image={})
        # check state is failed even after the periodic poll
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.assertTrue(mock_prep_block_dev.called)

    def test_run_instance_spawn_fail(self):
        """spawn failure test.

        Make sure that when there is a spawning problem,
        the instance goes to ERROR state, cleaning the task state.
        """
        def fake(*args, **kwargs):
            raise test.TestingException()
        self.stub_out('nova.virt.fake.FakeDriver.spawn', fake)
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(
                          self.context, instance=instance, request_spec={},
                          filter_properties={}, requested_networks=[],
                          injected_files=None, admin_password=None,
                          block_device_mapping=[], image={}, node=None)
        # check state is failed even after the periodic poll
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})
        self.compute.periodic_tasks(context.get_admin_context())
        self._assert_state({'vm_state': vm_states.ERROR,
                            'task_state': None})

    def test_run_instance_dealloc_network_instance_not_found(self):
        """spawn network deallocate test.

        Make sure that when an instance is not found during spawn
        that the network is deallocated
        """
        instance = self._create_fake_instance_obj()

        def fake(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id="fake")

        with test.nested(
            mock.patch.object(self.compute, '_deallocate_network'),
            mock.patch.object(self.compute.driver, 'spawn')
        ) as (mock_deallocate, mock_spawn):
            mock_spawn.side_effect = fake
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                                                {}, block_device_mapping=[])

            mock_deallocate.assert_called_with(mock.ANY, mock.ANY, None)
            self.assertTrue(mock_spawn.called)

    def test_run_instance_bails_on_missing_instance(self):
        # Make sure that run_instance() will quickly ignore a deleted instance
        instance = self._create_fake_instance_obj()

        with mock.patch.object(instance, 'save') as mock_save:
            mock_save.side_effect = exception.InstanceNotFound(instance_id=1)
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                                                {}, block_device_mapping=[])
            self.assertTrue(mock_save.called)

    def test_run_instance_bails_on_deleting_instance(self):
        # Make sure that run_instance() will quickly ignore a deleting instance
        instance = self._create_fake_instance_obj()

        with mock.patch.object(instance, 'save') as mock_save:
            mock_save.side_effect = exception.UnexpectedDeletingTaskStateError(
                instance_uuid=instance['uuid'],
                expected={'task_state': 'bar'},
                actual={'task_state': 'foo'})
            self.compute.build_and_run_instance(self.context, instance, {}, {},
                                                {}, block_device_mapping=[])
            self.assertTrue(mock_save.called)

    def test_can_terminate_on_error_state(self):
        # Make sure that the instance can be terminated in ERROR state.
        # check failed to schedule --> terminate
        params = {'vm_state': vm_states.ERROR}
        instance = self._create_fake_instance_obj(params=params)
        self.compute.terminate_instance(self.context, instance, [], [])
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          self.context, instance['uuid'])
        # Double check it's not there for admins, either.
        self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                          self.context.elevated(), instance['uuid'])

    def test_run_terminate(self):
        # Make sure it is possible to  run and terminate instance.
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instances = db.instance_get_all(self.context)
        LOG.info("Running instances: %s", instances)
        self.assertEqual(len(instances), 1)

        self.compute.terminate_instance(self.context, instance, [], [])

        instances = db.instance_get_all(self.context)
        LOG.info("After terminating instances: %s", instances)
        self.assertEqual(len(instances), 0)

        admin_deleted_context = context.get_admin_context(
                read_deleted="only")
        instance = db.instance_get_by_uuid(admin_deleted_context,
                                           instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.DELETED)
        self.assertIsNone(instance['task_state'])

    def test_run_terminate_with_vol_attached(self):
        """Make sure it is possible to  run and terminate instance with volume
        attached
        """
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instances = db.instance_get_all(self.context)
        LOG.info("Running instances: %s", instances)
        self.assertEqual(len(instances), 1)

        def fake_check_attach(*args, **kwargs):
            pass

        def fake_reserve_volume(*args, **kwargs):
            pass

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id,
                    'attach_status': 'attached',
                    'attachments': {instance.uuid: {
                                       'attachment_id': 'abc123'
                                        }
                                    }
                    }

        def fake_terminate_connection(self, context, volume_id, connector):
            pass

        def fake_detach(self, context, volume_id, instance_uuid):
            pass

        bdms = []

        def fake_rpc_reserve_block_device_name(self, context, instance, device,
                                               volume_id, **kwargs):
            bdm = objects.BlockDeviceMapping(
                        **{'context': context,
                           'source_type': 'volume',
                           'destination_type': 'volume',
                           'volume_id': uuids.volume_id,
                           'instance_uuid': instance['uuid'],
                           'device_name': '/dev/vdc'})
            bdm.create()
            bdms.append(bdm)
            return bdm

        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)
        self.stub_out('nova.volume.cinder.API.check_attach', fake_check_attach)
        self.stub_out('nova.volume.cinder.API.reserve_volume',
                       fake_reserve_volume)
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                       fake_terminate_connection)
        self.stub_out('nova.volume.cinder.API.detach', fake_detach)
        self.stub_out('nova.compute.rpcapi.ComputeAPI.'
                       'reserve_block_device_name',
                       fake_rpc_reserve_block_device_name)

        self.compute_api.attach_volume(self.context, instance, 1,
                                       '/dev/vdc')

        self.compute.terminate_instance(self.context,
                instance, bdms, [])

        instances = db.instance_get_all(self.context)
        LOG.info("After terminating instances: %s", instances)
        self.assertEqual(len(instances), 0)
        bdms = db.block_device_mapping_get_all_by_instance(self.context,
                                                           instance['uuid'])
        self.assertEqual(len(bdms), 0)

    def test_run_terminate_no_image(self):
        """Make sure instance started without image (from volume)
        can be termintad without issues
        """
        params = {'image_ref': ''}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        self._assert_state({'vm_state': vm_states.ACTIVE,
                            'task_state': None})

        self.compute.terminate_instance(self.context, instance, [], [])
        instances = db.instance_get_all(self.context)
        self.assertEqual(len(instances), 0)

    def test_terminate_no_network(self):
        # This is as reported in LP bug 1008875
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                            block_device_mapping=[])

        instances = db.instance_get_all(self.context)
        LOG.info("Running instances: %s", instances)
        self.assertEqual(len(instances), 1)

        self.compute.terminate_instance(self.context, instance, [], [])

        instances = db.instance_get_all(self.context)
        LOG.info("After terminating instances: %s", instances)
        self.assertEqual(len(instances), 0)

    def test_run_terminate_timestamps(self):
        # Make sure timestamps are set for launched and destroyed.
        instance = self._create_fake_instance_obj()
        instance['launched_at'] = None
        self.assertIsNone(instance['launched_at'])
        self.assertIsNone(instance['deleted_at'])
        launch = timeutils.utcnow()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.refresh()
        self.assertGreater(instance['launched_at'].replace(tzinfo=None),
                           launch)
        self.assertIsNone(instance['deleted_at'])
        terminate = timeutils.utcnow()
        self.compute.terminate_instance(self.context, instance, [], [])

        with utils.temporary_mutation(self.context, read_deleted='only'):
            instance = db.instance_get_by_uuid(self.context,
                    instance['uuid'])
        self.assertTrue(instance['launched_at'].replace(
            tzinfo=None) < terminate)
        self.assertGreater(instance['deleted_at'].replace(
            tzinfo=None), terminate)

    def test_run_terminate_deallocate_net_failure_sets_error_state(self):
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instances = db.instance_get_all(self.context)
        LOG.info("Running instances: %s", instances)
        self.assertEqual(len(instances), 1)

        def _fake_deallocate_network(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.compute.manager.ComputeManager.'
                      '_deallocate_network', _fake_deallocate_network)

        self.assertRaises(test.TestingException,
                          self.compute.terminate_instance,
                          self.context, instance, [], [])

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)

    def test_stop(self):
        # Ensure instance can be stopped.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {},
                                            {}, block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        inst_uuid = instance['uuid']
        extra = ['system_metadata', 'metadata']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                inst_uuid,
                                                expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj,
                                   clean_shutdown=True)
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch('nova.compute.utils.notify_about_instance_action')
    def test_start(self, mock_notify):
        # Ensure instance can be started.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        extra = ['system_metadata', 'metadata']
        inst_uuid = instance['uuid']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                inst_uuid,
                                                expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj,
                                   clean_shutdown=True)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save()
        self.compute.start_instance(self.context, instance=inst_obj)
        mock_notify.assert_has_calls([
            mock.call(self.context, inst_obj, 'fake-mini', action='power_on',
                      phase='start'),
            mock.call(self.context, inst_obj, 'fake-mini', action='power_on',
                      phase='end')])
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_start_shelved_instance(self):
        # Ensure shelved instance can be started.
        self.deleted_image_id = None

        def fake_delete(self_, ctxt, image_id):
            self.deleted_image_id = image_id

        fake_image.stub_out_image_service(self)
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.delete',
                      fake_delete)

        instance = self._create_fake_instance_obj()
        image = {'id': 'fake_id'}
        # Adding shelved information to instance system metadata.
        shelved_time = timeutils.utcnow().isoformat()
        instance.system_metadata['shelved_at'] = shelved_time
        instance.system_metadata['shelved_image_id'] = image['id']
        instance.system_metadata['shelved_host'] = 'fake-mini'
        instance.save()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF,
                            "vm_state": vm_states.SHELVED})
        extra = ['system_metadata', 'metadata']
        inst_uuid = instance['uuid']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                inst_uuid,
                                                expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj,
                                   clean_shutdown=True)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save()
        self.compute.start_instance(self.context, instance=inst_obj)
        self.assertEqual(image['id'], self.deleted_image_id)
        self.assertNotIn('shelved_at', inst_obj.system_metadata)
        self.assertNotIn('shelved_image_id', inst_obj.system_metadata)
        self.assertNotIn('shelved_host', inst_obj.system_metadata)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_stop_start_no_image(self):
        params = {'image_ref': ''}
        instance = self._create_fake_instance_obj(params)
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.POWERING_OFF})
        extra = ['system_metadata', 'metadata']
        inst_uuid = instance['uuid']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                inst_uuid,
                                                expected_attrs=extra)
        self.compute.stop_instance(self.context, instance=inst_obj,
                                   clean_shutdown=True)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save()
        self.compute.start_instance(self.context, instance=inst_obj)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rescue(self):
        # Ensure instance can be rescued and unrescued.

        called = {'rescued': False,
                  'unrescued': False}

        def fake_rescue(self, context, instance_ref, network_info, image_meta,
                        rescue_password):
            called['rescued'] = True

        self.stub_out('nova.virt.fake.FakeDriver.rescue', fake_rescue)

        def fake_unrescue(self, instance_ref, network_info):
            called['unrescued'] = True

        self.stub_out('nova.virt.fake.FakeDriver.unrescue',
                       fake_unrescue)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.task_state = task_states.RESCUING
        instance.save()
        self.compute.rescue_instance(self.context, instance, None, None, True)
        self.assertTrue(called['rescued'])
        instance.task_state = task_states.UNRESCUING
        instance.save()
        self.compute.unrescue_instance(self.context, instance)
        self.assertTrue(called['unrescued'])

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rescue_notifications(self):
        # Ensure notifications on instance rescue.
        def fake_rescue(self, context, instance_ref, network_info, image_meta,
                        rescue_password):
            pass
        self.stub_out('nova.virt.fake.FakeDriver.rescue', fake_rescue)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        fake_notifier.NOTIFICATIONS = []
        instance.task_state = task_states.RESCUING
        instance.save()
        self.compute.rescue_instance(self.context, instance, None,
                                     rescue_image_ref=uuids.fake_image_ref_1,
                                     clean_shutdown=True)

        expected_notifications = ['compute.instance.rescue.start',
                                  'compute.instance.exists',
                                  'compute.instance.rescue.end']
        self.assertEqual([m.event_type for m in fake_notifier.NOTIFICATIONS],
                         expected_notifications)
        for n, msg in enumerate(fake_notifier.NOTIFICATIONS):
            self.assertEqual(msg.event_type, expected_notifications[n])
            self.assertEqual(msg.priority, 'INFO')
            payload = msg.payload
            self.assertEqual(payload['tenant_id'], self.project_id)
            self.assertEqual(payload['user_id'], self.user_id)
            self.assertEqual(payload['instance_id'], instance.uuid)
            self.assertEqual(payload['instance_type'], 'm1.tiny')
            type_id = flavors.get_flavor_by_name('m1.tiny')['id']
            self.assertEqual(str(payload['instance_type_id']), str(type_id))
            self.assertIn('display_name', payload)
            self.assertIn('created_at', payload)
            self.assertIn('launched_at', payload)
            image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
            self.assertEqual(payload['image_ref_url'], image_ref_url)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertIn('rescue_image_name', msg.payload)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_unrescue_notifications(self):
        # Ensure notifications on instance rescue.
        def fake_unrescue(self, instance_ref, network_info):
            pass
        self.stub_out('nova.virt.fake.FakeDriver.unrescue',
                       fake_unrescue)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        fake_notifier.NOTIFICATIONS = []
        instance.task_state = task_states.UNRESCUING
        instance.save()
        self.compute.unrescue_instance(self.context, instance)

        expected_notifications = ['compute.instance.unrescue.start',
                                  'compute.instance.unrescue.end']
        self.assertEqual([m.event_type for m in fake_notifier.NOTIFICATIONS],
                         expected_notifications)
        for n, msg in enumerate(fake_notifier.NOTIFICATIONS):
            self.assertEqual(msg.event_type, expected_notifications[n])
            self.assertEqual(msg.priority, 'INFO')
            payload = msg.payload
            self.assertEqual(payload['tenant_id'], self.project_id)
            self.assertEqual(payload['user_id'], self.user_id)
            self.assertEqual(payload['instance_id'], instance.uuid)
            self.assertEqual(payload['instance_type'], 'm1.tiny')
            type_id = flavors.get_flavor_by_name('m1.tiny')['id']
            self.assertEqual(str(payload['instance_type_id']), str(type_id))
            self.assertIn('display_name', payload)
            self.assertIn('created_at', payload)
            self.assertIn('launched_at', payload)
            image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
            self.assertEqual(payload['image_ref_url'], image_ref_url)

        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch.object(fake.FakeDriver, 'rescue')
    @mock.patch.object(compute_manager.ComputeManager, '_get_rescue_image')
    def test_rescue_handle_err(self, mock_get, mock_rescue):
        # If the driver fails to rescue, instance state should got to ERROR
        # and the exception should be converted to InstanceNotRescuable
        inst_obj = self._create_fake_instance_obj()
        mock_get.return_value = objects.ImageMeta.from_dict({})
        mock_rescue.side_effect = RuntimeError("Try again later")

        expected_message = ('Instance %s cannot be rescued: '
                            'Driver Error: Try again later' % inst_obj.uuid)

        with testtools.ExpectedException(
                exception.InstanceNotRescuable, expected_message):
                self.compute.rescue_instance(
                    self.context, instance=inst_obj,
                    rescue_password='password', rescue_image_ref=None,
                    clean_shutdown=True)

        self.assertEqual(vm_states.ERROR, inst_obj.vm_state)
        mock_get.assert_called_once_with(mock.ANY, inst_obj, mock.ANY)
        mock_rescue.assert_called_once_with(mock.ANY, inst_obj, [],
                                            mock.ANY, 'password')

    @mock.patch.object(image_api.API, "get")
    @mock.patch.object(nova.virt.fake.FakeDriver, "rescue")
    def test_rescue_with_image_specified(self, mock_rescue,
                                         mock_image_get):
        image_ref = uuids.image_instance
        rescue_image_meta = {}
        params = {"task_state": task_states.RESCUING}
        instance = self._create_fake_instance_obj(params=params)

        ctxt = context.get_admin_context()
        mock_context = mock.Mock()
        mock_context.elevated.return_value = ctxt

        mock_image_get.return_value = rescue_image_meta

        self.compute.rescue_instance(mock_context, instance=instance,
                    rescue_password="password", rescue_image_ref=image_ref,
                    clean_shutdown=True)

        mock_image_get.assert_called_with(ctxt, image_ref)
        mock_rescue.assert_called_with(ctxt, instance, [],
                                       test.MatchType(objects.ImageMeta),
                                       'password')
        self.compute.terminate_instance(ctxt, instance, [], [])

    @mock.patch.object(image_api.API, "get")
    @mock.patch.object(nova.virt.fake.FakeDriver, "rescue")
    def test_rescue_with_base_image_when_image_not_specified(self,
            mock_rescue, mock_image_get):
        image_ref = FAKE_IMAGE_REF
        system_meta = {"image_base_image_ref": image_ref}
        rescue_image_meta = {}
        params = {"task_state": task_states.RESCUING,
                  "system_metadata": system_meta}
        instance = self._create_fake_instance_obj(params=params)

        ctxt = context.get_admin_context()
        mock_context = mock.Mock()
        mock_context.elevated.return_value = ctxt

        mock_image_get.return_value = rescue_image_meta

        self.compute.rescue_instance(mock_context, instance=instance,
                                     rescue_password="password",
                                     rescue_image_ref=None,
                                     clean_shutdown=True)

        mock_image_get.assert_called_with(ctxt, image_ref)

        mock_rescue.assert_called_with(ctxt, instance, [],
                                       test.MatchType(objects.ImageMeta),
                                       'password')
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_power_on(self):
        # Ensure instance can be powered on.

        called = {'power_on': False}

        def fake_driver_power_on(self, context, instance, network_info,
                                 block_device_info):
            called['power_on'] = True

        self.stub_out('nova.virt.fake.FakeDriver.power_on',
                       fake_driver_power_on)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        extra = ['system_metadata', 'metadata']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                instance['uuid'],
                                                expected_attrs=extra)
        inst_obj.task_state = task_states.POWERING_ON
        inst_obj.save()
        self.compute.start_instance(self.context, instance=inst_obj)
        self.assertTrue(called['power_on'])
        self.compute.terminate_instance(self.context, inst_obj, [], [])

    def test_power_off(self):
        # Ensure instance can be powered off.

        called = {'power_off': False}

        def fake_driver_power_off(self, instance,
                                  shutdown_timeout, shutdown_attempts):
            called['power_off'] = True

        self.stub_out('nova.virt.fake.FakeDriver.power_off',
                       fake_driver_power_off)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        extra = ['system_metadata', 'metadata']
        inst_obj = objects.Instance.get_by_uuid(self.context,
                                                instance['uuid'],
                                                expected_attrs=extra)
        inst_obj.task_state = task_states.POWERING_OFF
        inst_obj.save()
        self.compute.stop_instance(self.context, instance=inst_obj,
                                   clean_shutdown=True)
        self.assertTrue(called['power_off'])
        self.compute.terminate_instance(self.context, inst_obj, [], [])

    @mock.patch('nova.compute.utils.notify_about_instance_action')
    @mock.patch.object(nova.context.RequestContext, 'elevated')
    def test_pause(self, mock_context, mock_notify):
        # Ensure instance can be paused and unpaused.
        instance = self._create_fake_instance_obj()
        ctxt = context.get_admin_context()
        mock_context.return_value = ctxt
        self.compute.build_and_run_instance(self.context,
                instance, {}, {}, {}, block_device_mapping=[])
        instance.task_state = task_states.PAUSING
        instance.save()
        fake_notifier.NOTIFICATIONS = []
        self.compute.pause_instance(self.context, instance=instance)
        mock_notify.assert_has_calls([
            mock.call(ctxt, instance, 'fake-mini',
                      action='pause', phase='start'),
            mock.call(ctxt, instance, 'fake-mini',
                      action='pause', phase='end')])
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'compute.instance.pause.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'compute.instance.pause.end')
        instance.task_state = task_states.UNPAUSING
        instance.save()
        fake_notifier.NOTIFICATIONS = []
        self.compute.unpause_instance(self.context, instance=instance)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'compute.instance.unpause.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'compute.instance.unpause.end')
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch('nova.compute.utils.notify_about_instance_action')
    @mock.patch('nova.context.RequestContext.elevated')
    def test_suspend(self, mock_context, mock_notify):
        # ensure instance can be suspended and resumed.
        context = self.context
        mock_context.return_value = context
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.task_state = task_states.SUSPENDING
        instance.save()
        self.compute.suspend_instance(context, instance)
        instance.task_state = task_states.RESUMING
        instance.save()
        self.compute.resume_instance(context, instance)

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 6)

        msg = fake_notifier.NOTIFICATIONS[2]
        self.assertEqual(msg.event_type,
                         'compute.instance.suspend.start')
        msg = fake_notifier.NOTIFICATIONS[3]
        self.assertEqual(msg.event_type,
                         'compute.instance.suspend.end')
        mock_notify.assert_has_calls([
        mock.call(context, instance, 'fake-mini',
                  action='suspend', phase='start'),
        mock.call(context, instance, 'fake-mini',
                  action='suspend', phase='end')])
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_suspend_error(self):
        # Ensure vm_state is ERROR when suspend error occurs.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        with mock.patch.object(self.compute.driver, 'suspend',
                               side_effect=test.TestingException):
            self.assertRaises(test.TestingException,
                              self.compute.suspend_instance,
                              self.context,
                              instance=instance)

            instance = db.instance_get_by_uuid(self.context, instance.uuid)
            self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_suspend_not_implemented(self):
        # Ensure expected exception is raised and the vm_state of instance
        # restore to original value if suspend is not implemented by driver
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        with mock.patch.object(self.compute.driver, 'suspend',
                           side_effect=NotImplementedError('suspend test')):
            self.assertRaises(NotImplementedError,
                              self.compute.suspend_instance,
                              self.context,
                              instance=instance)

            instance = db.instance_get_by_uuid(self.context, instance.uuid)
            self.assertEqual(vm_states.ACTIVE, instance.vm_state)

    def test_suspend_rescued(self):
        # ensure rescued instance can be suspended and resumed.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.vm_state = vm_states.RESCUED
        instance.task_state = task_states.SUSPENDING
        instance.save()

        self.compute.suspend_instance(self.context, instance)
        self.assertEqual(instance.vm_state, vm_states.SUSPENDED)

        instance.task_state = task_states.RESUMING
        instance.save()
        self.compute.resume_instance(self.context, instance)
        self.assertEqual(instance.vm_state, vm_states.RESCUED)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_resume_notifications(self):
        # ensure instance can be suspended and resumed.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.task_state = task_states.SUSPENDING
        instance.save()
        self.compute.suspend_instance(self.context, instance)
        instance.task_state = task_states.RESUMING
        instance.save()
        self.compute.resume_instance(self.context, instance)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 6)
        msg = fake_notifier.NOTIFICATIONS[4]
        self.assertEqual(msg.event_type,
                         'compute.instance.resume.start')
        msg = fake_notifier.NOTIFICATIONS[5]
        self.assertEqual(msg.event_type,
                         'compute.instance.resume.end')
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_resume_no_old_state(self):
        # ensure a suspended instance with no old_vm_state is resumed to the
        # ACTIVE state
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.vm_state = vm_states.SUSPENDED
        instance.task_state = task_states.RESUMING
        instance.save()

        self.compute.resume_instance(self.context, instance)
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_resume_error(self):
        # Ensure vm_state is ERROR when resume error occurs.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.task_state = task_states.SUSPENDING
        instance.save()
        self.compute.suspend_instance(self.context, instance)
        instance.task_state = task_states.RESUMING
        instance.save()
        with mock.patch.object(self.compute.driver, 'resume',
                               side_effect=test.TestingException):
            self.assertRaises(test.TestingException,
                              self.compute.resume_instance,
                              self.context,
                              instance)

        instance = db.instance_get_by_uuid(self.context, instance.uuid)
        self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_rebuild(self):
        # Ensure instance can be rebuilt.
        instance = self._create_fake_instance_obj()
        image_ref = instance['image_ref']
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata,
                                      bdms=[], recreate=False,
                                      on_shared_storage=False)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rebuild_driver(self):
        # Make sure virt drivers can override default rebuild
        called = {'rebuild': False}

        def fake(*args, **kwargs):
            instance = kwargs['instance']
            instance.task_state = task_states.REBUILD_BLOCK_DEVICE_MAPPING
            instance.save(expected_task_state=[task_states.REBUILDING])
            instance.task_state = task_states.REBUILD_SPAWNING
            instance.save(
                expected_task_state=[task_states.REBUILD_BLOCK_DEVICE_MAPPING])
            called['rebuild'] = True

        self.stub_out('nova.virt.fake.FakeDriver.rebuild', fake)
        instance = self._create_fake_instance_obj()
        image_ref = instance['image_ref']
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata,
                                      bdms=[], recreate=False,
                                      on_shared_storage=False)
        self.assertTrue(called['rebuild'])
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch('nova.compute.manager.ComputeManager._detach_volume')
    def test_rebuild_driver_with_volumes(self, mock_detach):
        bdms = block_device_obj.block_device_make_list(self.context,
                [fake_block_device.FakeDbBlockDeviceDict({
                'id': 3,
                    'volume_id': uuids.volume_id,
                    'instance_uuid': uuids.block_device_instance,
                    'device_name': '/dev/vda',
                    'connection_info': '{"driver_volume_type": "rbd"}',
                    'source_type': 'image',
                    'destination_type': 'volume',
                    'image_id': 'fake-image-id-1',
                    'boot_index': 0
        })])

        # Make sure virt drivers can override default rebuild
        called = {'rebuild': False}

        def fake(*args, **kwargs):
            instance = kwargs['instance']
            instance.task_state = task_states.REBUILD_BLOCK_DEVICE_MAPPING
            instance.save(expected_task_state=[task_states.REBUILDING])
            instance.task_state = task_states.REBUILD_SPAWNING
            instance.save(
                expected_task_state=[task_states.REBUILD_BLOCK_DEVICE_MAPPING])
            called['rebuild'] = True
            func = kwargs['detach_block_devices']
            # Have the fake driver call the function to detach block devices
            func(self.context, bdms)
            # Verify volumes to be detached without destroying
            mock_detach.assert_called_once_with(self.context,
                                                bdms[0].volume_id,
                                                instance, destroy_bdm=False)

        self.stub_out('nova.virt.fake.FakeDriver.rebuild', fake)
        instance = self._create_fake_instance_obj()
        image_ref = instance['image_ref']
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata,
                                      bdms=bdms, recreate=False,
                                      on_shared_storage=False)
        self.assertTrue(called['rebuild'])
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rebuild_no_image(self):
        # Ensure instance can be rebuilt when started with no image.
        params = {'image_ref': ''}
        instance = self._create_fake_instance_obj(params)
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      '', '', injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata, bdms=[],
                                      recreate=False, on_shared_storage=False)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rebuild_launched_at_time(self):
        # Ensure instance can be rebuilt.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        time_fixture = self.useFixture(utils_fixture.TimeFixture(old_time))
        instance = self._create_fake_instance_obj()
        image_ref = instance['image_ref']

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        time_fixture.advance_time_delta(cur_time - old_time)
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=[],
                                      new_pass="new_password",
                                      orig_sys_metadata={},
                                      bdms=[], recreate=False,
                                      on_shared_storage=False)
        instance.refresh()
        self.assertEqual(cur_time,
                         instance['launched_at'].replace(tzinfo=None))
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rebuild_with_injected_files(self):
        # Ensure instance can be rebuilt with injected files.
        injected_files = [
            (b'/a/b/c', base64.b64encode(b'foobarbaz')),
        ]

        self.decoded_files = [
            (b'/a/b/c', b'foobarbaz'),
        ]

        def _spawn(cls, context, instance, image_meta, injected_files,
                   admin_password, network_info, block_device_info):
            self.assertEqual(self.decoded_files, injected_files)

        self.stub_out('nova.virt.fake.FakeDriver.spawn', _spawn)
        instance = self._create_fake_instance_obj()
        image_ref = instance['image_ref']
        sys_metadata = db.instance_system_metadata_get(self.context,
                        instance['uuid'])
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": task_states.REBUILDING})
        self.compute.rebuild_instance(self.context, instance,
                                      image_ref, image_ref,
                                      injected_files=injected_files,
                                      new_pass="new_password",
                                      orig_sys_metadata=sys_metadata,
                                      bdms=[], recreate=False,
                                      on_shared_storage=False)
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch.object(compute_manager.ComputeManager,
                           '_get_instance_block_device_info')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_notify_about_instance_usage')
    @mock.patch.object(compute_manager.ComputeManager, '_instance_update')
    @mock.patch.object(db, 'instance_update_and_get_original')
    @mock.patch.object(compute_manager.ComputeManager, '_get_power_state')
    def _test_reboot(self, soft, mock_get_power, mock_get_orig,
                 mock_update, mock_notify, mock_get_nw, mock_get_blk,
                 test_delete=False, test_unrescue=False,
                 fail_reboot=False, fail_running=False):
        reboot_type = soft and 'SOFT' or 'HARD'
        task_pending = (soft and task_states.REBOOT_PENDING
                        or task_states.REBOOT_PENDING_HARD)
        task_started = (soft and task_states.REBOOT_STARTED
                        or task_states.REBOOT_STARTED_HARD)
        expected_task = (soft and task_states.REBOOTING
                         or task_states.REBOOTING_HARD)
        expected_tasks = (soft and (task_states.REBOOTING,
                                    task_states.REBOOT_PENDING,
                                    task_states.REBOOT_STARTED)
                          or (task_states.REBOOTING_HARD,
                              task_states.REBOOT_PENDING_HARD,
                              task_states.REBOOT_STARTED_HARD))

        # This is a true unit test, so we don't need the network stubs.
        fake_network.unset_stub_network_methods(self)

        # FIXME(comstud): I don't feel like the context needs to
        # be elevated at all.  Hopefully remove elevated from
        # reboot_instance and remove the mock here in a future patch.
        # econtext would just become self.context below then.
        econtext = self.context.elevated()

        db_instance = fake_instance.fake_db_instance(
            **dict(uuid=uuids.db_instance,
                   power_state=power_state.NOSTATE,
                   vm_state=vm_states.ACTIVE,
                   task_state=expected_task,
                   launched_at=timeutils.utcnow()))
        instance = objects.Instance._from_db_object(econtext,
                                objects.Instance(), db_instance)

        updated_dbinstance1 = fake_instance.fake_db_instance(
            **dict(uuid=uuids.db_instance_1,
                   power_state=10003,
                   vm_state=vm_states.ACTIVE,
                   task_state=expected_task,
                   instance_type=flavors.get_default_flavor(),
                   launched_at=timeutils.utcnow()))
        updated_dbinstance2 = fake_instance.fake_db_instance(
            **dict(uuid=uuids.db_instance_2,
                   power_state=10003,
                   vm_state=vm_states.ACTIVE,
                   instance_type=flavors.get_default_flavor(),
                   task_state=expected_task,
                   launched_at=timeutils.utcnow()))

        if test_unrescue:
            instance.vm_state = vm_states.RESCUED
        instance.obj_reset_changes()

        fake_nw_model = network_model.NetworkInfo()

        fake_block_dev_info = 'fake_block_dev_info'
        fake_power_state1 = 10001
        fake_power_state2 = power_state.RUNNING
        fake_power_state3 = 10002

        def _fake_elevated(self):
            return econtext

        # Beginning of calls we expect.
        self.stub_out('nova.context.RequestContext.elevated', _fake_elevated)
        mock_get_blk.return_value = fake_block_dev_info
        mock_get_nw.return_value = fake_nw_model
        mock_get_power.side_effect = [fake_power_state1]
        mock_get_orig.side_effect = [(None, updated_dbinstance1),
                                     (None, updated_dbinstance1)]
        notify_call_list = [mock.call(econtext, instance, 'reboot.start')]
        ps_call_list = [mock.call(econtext, instance)]
        db_call_list = [mock.call(econtext, instance['uuid'],
                                  {'task_state': task_pending,
                                   'expected_task_state': expected_tasks,
                                   'power_state': fake_power_state1},
                                  columns_to_join=['system_metadata',
                                                   'extra',
                                                   'extra.flavor']),
                        mock.call(econtext, updated_dbinstance1['uuid'],
                                  {'task_state': task_started,
                                   'expected_task_state': task_pending},
                                  columns_to_join=['system_metadata'])]
        expected_nw_info = fake_nw_model

        # Annoying.  driver.reboot is wrapped in a try/except, and
        # doesn't re-raise.  It eats exception generated by mock if
        # this is called with the wrong args, so we have to hack
        # around it.
        reboot_call_info = {}
        expected_call_info = {
            'args': (econtext, instance, expected_nw_info,
                     reboot_type),
            'kwargs': {'block_device_info': fake_block_dev_info}}
        fault = exception.InstanceNotFound(instance_id='instance-0000')

        def fake_reboot(self, *args, **kwargs):
            reboot_call_info['args'] = args
            reboot_call_info['kwargs'] = kwargs

            # NOTE(sirp): Since `bad_volumes_callback` is a function defined
            # within `reboot_instance`, we don't have access to its value and
            # can't stub it out, thus we skip that comparison.
            kwargs.pop('bad_volumes_callback')
            if fail_reboot:
                raise fault

        self.stub_out('nova.virt.fake.FakeDriver.reboot', fake_reboot)

        # Power state should be updated again
        if not fail_reboot or fail_running:
            new_power_state = fake_power_state2
            ps_call_list.append(mock.call(econtext, instance))
            mock_get_power.side_effect = chain(mock_get_power.side_effect,
                                               [fake_power_state2])
        else:
            new_power_state = fake_power_state3
            ps_call_list.append(mock.call(econtext, instance))
            mock_get_power.side_effect = chain(mock_get_power.side_effect,
                                               [fake_power_state3])

        if test_delete:
            fault = exception.InstanceNotFound(
                instance_id=instance['uuid'])
            mock_get_orig.side_effect = chain(mock_get_orig.side_effect,
                                              [fault])
            db_call_list.append(
                mock.call(econtext, updated_dbinstance1['uuid'],
                          {'power_state': new_power_state,
                           'task_state': None,
                           'vm_state': vm_states.ACTIVE},
                          columns_to_join=['system_metadata']))
            notify_call_list.append(mock.call(econtext, instance,
                                              'reboot.end'))
        elif fail_reboot and not fail_running:
            mock_get_orig.side_effect = chain(mock_get_orig.side_effect,
                                              [fault])
            db_call_list.append(
                mock.call(econtext, updated_dbinstance1['uuid'],
                          {'vm_state': vm_states.ERROR},
                          columns_to_join=['system_metadata'], ))
        else:
            mock_get_orig.side_effect = chain(mock_get_orig.side_effect,
                                              [(None, updated_dbinstance2)])
            db_call_list.append(
                mock.call(econtext, updated_dbinstance1['uuid'],
                          {'power_state': new_power_state,
                           'task_state': None,
                           'vm_state': vm_states.ACTIVE},
                          columns_to_join=['system_metadata'], ))
            if fail_running:
                notify_call_list.append(mock.call(econtext, instance,
                                                  'reboot.error', fault=fault))
            notify_call_list.append(mock.call(econtext, instance,
                                              'reboot.end'))

        if not fail_reboot or fail_running:
            self.compute.reboot_instance(self.context, instance=instance,
                                             block_device_info=None,
                                             reboot_type=reboot_type)
        else:
            self.assertRaises(exception.InstanceNotFound,
                                  self.compute.reboot_instance,
                                  self.context, instance=instance,
                                  block_device_info=None,
                                  reboot_type=reboot_type)

        self.assertEqual(expected_call_info, reboot_call_info)
        mock_get_blk.assert_called_once_with(econtext, instance)
        mock_get_nw.assert_called_once_with(econtext, instance)
        mock_notify.assert_has_calls(notify_call_list)
        mock_get_power.assert_has_calls(ps_call_list)
        mock_get_orig.assert_has_calls(db_call_list)

    def test_reboot_soft(self):
        self._test_reboot(True)

    def test_reboot_soft_and_delete(self):
        self._test_reboot(True, test_delete=True)

    def test_reboot_soft_and_rescued(self):
        self._test_reboot(True, test_delete=False, test_unrescue=True)

    def test_reboot_soft_and_delete_and_rescued(self):
        self._test_reboot(True, test_delete=True, test_unrescue=True)

    def test_reboot_hard(self):
        self._test_reboot(False)

    def test_reboot_hard_and_delete(self):
        self._test_reboot(False, test_delete=True)

    def test_reboot_hard_and_rescued(self):
        self._test_reboot(False, test_delete=False, test_unrescue=True)

    def test_reboot_hard_and_delete_and_rescued(self):
        self._test_reboot(False, test_delete=True, test_unrescue=True)

    @mock.patch.object(jsonutils, 'to_primitive')
    def test_reboot_fail(self, mock_to_primitive):
        self._test_reboot(False, fail_reboot=True)

    def test_reboot_fail_running(self):
        self._test_reboot(False, fail_reboot=True, fail_running=True)

    def test_get_instance_block_device_info_source_image(self):
        bdms = block_device_obj.block_device_make_list(self.context,
                [fake_block_device.FakeDbBlockDeviceDict({
                'id': 3,
                    'volume_id': uuids.volume_id,
                    'instance_uuid': uuids.block_device_instance,
                    'device_name': '/dev/vda',
                    'connection_info': '{"driver_volume_type": "rbd"}',
                    'source_type': 'image',
                    'destination_type': 'volume',
                    'image_id': 'fake-image-id-1',
                    'boot_index': 0
        })])

        with (mock.patch.object(
                objects.BlockDeviceMappingList,
                'get_by_instance_uuid',
                return_value=bdms)
        ) as mock_get_by_instance:
            block_device_info = (
                self.compute._get_instance_block_device_info(
                    self.context, self._create_fake_instance_obj())
            )
            expected = {
                'swap': None,
                'ephemerals': [],
                'root_device_name': None,
                'block_device_mapping': [{
                    'connection_info': {
                        'driver_volume_type': 'rbd'
                    },
                    'mount_device': '/dev/vda',
                    'delete_on_termination': False
                }]
            }
            self.assertTrue(mock_get_by_instance.called)
            self.assertEqual(block_device_info, expected)

    def test_get_instance_block_device_info_passed_bdms(self):
        bdms = block_device_obj.block_device_make_list(self.context,
                [fake_block_device.FakeDbBlockDeviceDict({
                    'id': 3,
                    'volume_id': uuids.volume_id,
                    'device_name': '/dev/vdd',
                    'connection_info': '{"driver_volume_type": "rbd"}',
                    'source_type': 'volume',
                    'destination_type': 'volume'})
               ])
        with (mock.patch.object(
                objects.BlockDeviceMappingList,
                'get_by_instance_uuid')) as mock_get_by_instance:
            block_device_info = (
                self.compute._get_instance_block_device_info(
                    self.context, self._create_fake_instance_obj(), bdms=bdms)
            )
            expected = {
                'swap': None,
                'ephemerals': [],
                'root_device_name': None,
                'block_device_mapping': [{
                    'connection_info': {
                        'driver_volume_type': 'rbd'
                    },
                    'mount_device': '/dev/vdd',
                    'delete_on_termination': False
                }]
            }
            self.assertFalse(mock_get_by_instance.called)
            self.assertEqual(block_device_info, expected)

    def test_get_instance_block_device_info_swap_and_ephemerals(self):
        instance = self._create_fake_instance_obj()

        ephemeral0 = fake_block_device.FakeDbBlockDeviceDict({
            'id': 1,
            'instance_uuid': uuids.block_device_instance,
            'device_name': '/dev/vdb',
            'source_type': 'blank',
            'destination_type': 'local',
            'device_type': 'disk',
            'disk_bus': 'virtio',
            'delete_on_termination': True,
            'guest_format': None,
            'volume_size': 1,
            'boot_index': -1
        })
        ephemeral1 = fake_block_device.FakeDbBlockDeviceDict({
            'id': 2,
            'instance_uuid': uuids.block_device_instance,
            'device_name': '/dev/vdc',
            'source_type': 'blank',
            'destination_type': 'local',
            'device_type': 'disk',
            'disk_bus': 'virtio',
            'delete_on_termination': True,
            'guest_format': None,
            'volume_size': 2,
            'boot_index': -1
        })
        swap = fake_block_device.FakeDbBlockDeviceDict({
            'id': 3,
            'instance_uuid': uuids.block_device_instance,
            'device_name': '/dev/vdd',
            'source_type': 'blank',
            'destination_type': 'local',
            'device_type': 'disk',
            'disk_bus': 'virtio',
            'delete_on_termination': True,
            'guest_format': 'swap',
            'volume_size': 1,
            'boot_index': -1
        })

        bdms = block_device_obj.block_device_make_list(self.context,
            [swap, ephemeral0, ephemeral1])

        with (
              mock.patch.object(objects.BlockDeviceMappingList,
                                'get_by_instance_uuid', return_value=bdms)
        ) as mock_get_by_instance_uuid:
            expected_block_device_info = {
                'swap': {'device_name': '/dev/vdd', 'swap_size': 1},
                'ephemerals': [{'device_name': '/dev/vdb', 'num': 0, 'size': 1,
                                'virtual_name': 'ephemeral0'},
                               {'device_name': '/dev/vdc', 'num': 1, 'size': 2,
                                'virtual_name': 'ephemeral1'}],
                'block_device_mapping': [],
                'root_device_name': None
            }

            block_device_info = (
                self.compute._get_instance_block_device_info(
                    self.context, instance)
            )

            mock_get_by_instance_uuid.assert_called_once_with(self.context,
                                                              instance['uuid'])
            self.assertEqual(expected_block_device_info, block_device_info)

    def test_inject_network_info(self):
        # Ensure we can inject network info.
        called = {'inject': False}

        def fake_driver_inject_network(self, instance, network_info):
            called['inject'] = True

        self.stub_out('nova.virt.fake.FakeDriver.inject_network_info',
                       fake_driver_inject_network)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        self.compute.inject_network_info(self.context, instance=instance)
        self.assertTrue(called['inject'])
        self.compute.terminate_instance(self.context,
                                        instance, [], [])

    def test_reset_network(self):
        # Ensure we can reset networking on an instance.
        called = {'count': 0}

        def fake_driver_reset_network(self, instance):
            called['count'] += 1

        self.stub_out('nova.virt.fake.FakeDriver.reset_network',
                       fake_driver_reset_network)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        self.compute.reset_network(self.context, instance)

        self.assertEqual(called['count'], 1)

        self.compute.terminate_instance(self.context, instance, [], [])

    def _get_snapshotting_instance(self):
        # Ensure instance can be snapshotted.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.task_state = task_states.IMAGE_SNAPSHOT_PENDING
        instance.save()
        return instance

    def test_snapshot(self):
        inst_obj = self._get_snapshotting_instance()
        self.compute.snapshot_instance(self.context, image_id='fakesnap',
                                       instance=inst_obj)

    def test_snapshot_no_image(self):
        inst_obj = self._get_snapshotting_instance()
        inst_obj.image_ref = ''
        inst_obj.save()
        self.compute.snapshot_instance(self.context, image_id='fakesnap',
                                       instance=inst_obj)

    def _test_snapshot_fails(self, raise_during_cleanup, method,
                             expected_state=True):
        def fake_snapshot(*args, **kwargs):
            raise test.TestingException()

        self.fake_image_delete_called = False

        def fake_delete(self_, context, image_id):
            self.fake_image_delete_called = True
            if raise_during_cleanup:
                raise Exception()

        self.stub_out('nova.virt.fake.FakeDriver.snapshot', fake_snapshot)
        fake_image.stub_out_image_service(self)
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.delete',
                      fake_delete)

        inst_obj = self._get_snapshotting_instance()
        if method == 'snapshot':
            self.assertRaises(test.TestingException,
                              self.compute.snapshot_instance,
                              self.context, image_id='fakesnap',
                              instance=inst_obj)
        else:
            self.assertRaises(test.TestingException,
                              self.compute.backup_instance,
                              self.context, image_id='fakesnap',
                              instance=inst_obj, backup_type='fake',
                              rotation=1)

        self.assertEqual(expected_state, self.fake_image_delete_called)
        self._assert_state({'task_state': None})

    @mock.patch.object(nova.compute.manager.ComputeManager, '_rotate_backups')
    def test_backup_fails(self, mock_rotate):
        self._test_snapshot_fails(False, 'backup')

    @mock.patch.object(nova.compute.manager.ComputeManager, '_rotate_backups')
    def test_backup_fails_cleanup_ignores_exception(self, mock_rotate):
        self._test_snapshot_fails(True, 'backup')

    @mock.patch.object(nova.compute.manager.ComputeManager, '_rotate_backups')
    @mock.patch.object(nova.compute.manager.ComputeManager,
                       '_do_snapshot_instance')
    def test_backup_fails_rotate_backup(self, mock_snap, mock_rotate):
        mock_rotate.side_effect = test.TestingException()
        self._test_snapshot_fails(True, 'backup', False)

    def test_snapshot_fails(self):
        self._test_snapshot_fails(False, 'snapshot')

    def test_snapshot_fails_cleanup_ignores_exception(self):
        self._test_snapshot_fails(True, 'snapshot')

    def _test_snapshot_deletes_image_on_failure(self, status, exc):
        self.fake_image_delete_called = False

        def fake_show(self_, context, image_id, **kwargs):
            self.assertEqual('fakesnap', image_id)
            image = {'id': image_id,
                     'status': status}
            return image

        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      fake_show)

        def fake_delete(self_, context, image_id):
            self.fake_image_delete_called = True
            self.assertEqual('fakesnap', image_id)

        self.stub_out('nova.tests.unit.image.fake._FakeImageService.delete',
                      fake_delete)

        def fake_snapshot(*args, **kwargs):
            raise exc

        self.stub_out('nova.virt.fake.FakeDriver.snapshot', fake_snapshot)

        fake_image.stub_out_image_service(self)

        inst_obj = self._get_snapshotting_instance()

        self.compute.snapshot_instance(self.context, image_id='fakesnap',
                                       instance=inst_obj)

    def test_snapshot_fails_with_glance_error(self):
        image_not_found = exception.ImageNotFound(image_id='fakesnap')
        self._test_snapshot_deletes_image_on_failure('error', image_not_found)
        self.assertFalse(self.fake_image_delete_called)
        self._assert_state({'task_state': None})

    def test_snapshot_fails_with_task_state_error(self):
        deleting_state_error = exception.UnexpectedDeletingTaskStateError(
            instance_uuid=uuids.instance,
            expected={'task_state': task_states.IMAGE_SNAPSHOT},
            actual={'task_state': task_states.DELETING})
        self._test_snapshot_deletes_image_on_failure(
            'error', deleting_state_error)
        self.assertTrue(self.fake_image_delete_called)
        self._test_snapshot_deletes_image_on_failure(
            'active', deleting_state_error)
        self.assertFalse(self.fake_image_delete_called)

    def test_snapshot_fails_with_instance_not_found(self):
        instance_not_found = exception.InstanceNotFound(instance_id='uuid')
        self._test_snapshot_deletes_image_on_failure(
            'error', instance_not_found)
        self.assertTrue(self.fake_image_delete_called)
        self._test_snapshot_deletes_image_on_failure(
            'active', instance_not_found)
        self.assertFalse(self.fake_image_delete_called)

    def test_snapshot_handles_cases_when_instance_is_deleted(self):
        inst_obj = self._get_snapshotting_instance()
        inst_obj.task_state = task_states.DELETING
        inst_obj.save()
        self.compute.snapshot_instance(self.context, image_id='fakesnap',
                                       instance=inst_obj)

    def test_snapshot_handles_cases_when_instance_is_not_found(self):
        inst_obj = self._get_snapshotting_instance()
        inst_obj2 = objects.Instance.get_by_uuid(self.context, inst_obj.uuid)
        inst_obj2.destroy()
        self.compute.snapshot_instance(self.context, image_id='fakesnap',
                                       instance=inst_obj)

    def _assert_state(self, state_dict):
        """Assert state of VM is equal to state passed as parameter."""
        instances = db.instance_get_all(self.context)
        self.assertEqual(len(instances), 1)

        if 'vm_state' in state_dict:
            self.assertEqual(state_dict['vm_state'], instances[0]['vm_state'])
        if 'task_state' in state_dict:
            self.assertEqual(state_dict['task_state'],
                             instances[0]['task_state'])
        if 'power_state' in state_dict:
            self.assertEqual(state_dict['power_state'],
                             instances[0]['power_state'])

    def test_console_output(self):
        # Make sure we can get console output from instance.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        output = self.compute.get_console_output(self.context,
                instance=instance, tail_length=None)
        self.assertEqual('FAKE CONSOLE OUTPUT\nANOTHER\nLAST LINE', output)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_console_output_bytes(self):
        # Make sure we can get console output from instance.
        instance = self._create_fake_instance_obj()

        with mock.patch.object(self.compute,
                               'get_console_output') as mock_console_output:
            mock_console_output.return_value = b'Hello.'

            output = self.compute.get_console_output(self.context,
                    instance=instance, tail_length=None)
            self.assertEqual(output, b'Hello.')
            self.compute.terminate_instance(self.context, instance, [], [])

    def test_console_output_tail(self):
        # Make sure we can get console output from instance.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        output = self.compute.get_console_output(self.context,
                instance=instance, tail_length=2)
        self.assertEqual('ANOTHER\nLAST LINE', output)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_console_output_not_implemented(self):
        def fake_not_implemented(*args, **kwargs):
            raise NotImplementedError()

        self.stub_out('nova.virt.fake.FakeDriver.get_console_output',
                       fake_not_implemented)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_console_output, self.context,
                          instance, 0)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(NotImplementedError,
                          self.compute.get_console_output, self.context,
                          instance, 0)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_console_output_instance_not_found(self):
        def fake_not_found(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id='fake-instance')

        self.stub_out('nova.virt.fake.FakeDriver.get_console_output',
                       fake_not_found)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_console_output, self.context,
                          instance, 0)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.InstanceNotFound,
                          self.compute.get_console_output, self.context,
                          instance, 0)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_novnc_vnc_console(self):
        # Make sure we can a vnc console for an instance.
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=False, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        # Try with the full instance
        console = self.compute.get_vnc_console(self.context, 'novnc',
                                               instance=instance)
        self.assertTrue(console)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_validate_console_port_vnc(self):
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=True, group='spice')
        instance = self._create_fake_instance_obj()

        def fake_driver_get_console(*args, **kwargs):
            return ctype.ConsoleVNC(host="fake_host", port=5900)

        self.stub_out("nova.virt.fake.FakeDriver.get_vnc_console",
                       fake_driver_get_console)

        self.assertTrue(self.compute.validate_console_port(
            context=self.context, instance=instance, port=5900,
            console_type="novnc"))

    def test_validate_console_port_spice(self):
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=True, group='spice')
        instance = self._create_fake_instance_obj()

        def fake_driver_get_console(*args, **kwargs):
            return ctype.ConsoleSpice(host="fake_host", port=5900, tlsPort=88)

        self.stub_out("nova.virt.fake.FakeDriver.get_spice_console",
                       fake_driver_get_console)

        self.assertTrue(self.compute.validate_console_port(
            context=self.context, instance=instance, port=5900,
            console_type="spice-html5"))

    def test_validate_console_port_rdp(self):
        self.flags(enabled=True, group='rdp')
        instance = self._create_fake_instance_obj()

        def fake_driver_get_console(*args, **kwargs):
            return ctype.ConsoleRDP(host="fake_host", port=5900)

        self.stub_out("nova.virt.fake.FakeDriver.get_rdp_console",
                       fake_driver_get_console)

        self.assertTrue(self.compute.validate_console_port(
            context=self.context, instance=instance, port=5900,
            console_type="rdp-html5"))

    def test_validate_console_port_mks(self):
        self.flags(enabled=True, group='mks')
        instance = self._create_fake_instance_obj()
        with mock.patch.object(
                self.compute.driver, 'get_mks_console') as mock_getmks:
            mock_getmks.return_value = ctype.ConsoleMKS(host="fake_host",
                                                        port=5900)
            result = self.compute.validate_console_port(context=self.context,
                        instance=instance, port=5900, console_type="webmks")
            self.assertTrue(result)

    def test_validate_console_port_wrong_port(self):
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=True, group='spice')
        instance = self._create_fake_instance_obj()

        def fake_driver_get_console(*args, **kwargs):
            return ctype.ConsoleSpice(host="fake_host", port=5900, tlsPort=88)

        self.stub_out("nova.virt.fake.FakeDriver.get_vnc_console",
                       fake_driver_get_console)

        self.assertFalse(self.compute.validate_console_port(
            context=self.context, instance=instance, port="wrongport",
            console_type="spice-html5"))

    def test_xvpvnc_vnc_console(self):
        # Make sure we can a vnc console for an instance.
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=False, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        console = self.compute.get_vnc_console(self.context, 'xvpvnc',
                                               instance=instance)
        self.assertTrue(console)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_invalid_vnc_console_type(self):
        # Raise useful error if console type is an unrecognised string.
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=False, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_vnc_console,
                          self.context, 'invalid', instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_vnc_console,
                          self.context, 'invalid', instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_missing_vnc_console_type(self):
        # Raise useful error is console type is None.
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=False, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_vnc_console,
                          self.context, None, instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_vnc_console,
                          self.context, None, instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_get_vnc_console_not_implemented(self):
        self.stub_out('nova.virt.fake.FakeDriver.get_vnc_console',
                       fake_not_implemented)

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_vnc_console,
                          self.context, 'novnc', instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(NotImplementedError,
                          self.compute.get_vnc_console,
                          self.context, 'novnc', instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_spicehtml5_spice_console(self):
        # Make sure we can a spice console for an instance.
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        # Try with the full instance
        console = self.compute.get_spice_console(self.context, 'spice-html5',
                                               instance=instance)
        self.assertTrue(console)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_invalid_spice_console_type(self):
        # Raise useful error if console type is an unrecognised string
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_spice_console,
                          self.context, 'invalid', instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_spice_console,
                          self.context, 'invalid', instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_get_spice_console_not_implemented(self):
        self.stub_out('nova.virt.fake.FakeDriver.get_spice_console',
                       fake_not_implemented)
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_spice_console,
                          self.context, 'spice-html5', instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(NotImplementedError,
                          self.compute.get_spice_console,
                          self.context, 'spice-html5', instance=instance)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_missing_spice_console_type(self):
        # Raise useful error is console type is None
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='spice')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_spice_console,
                          self.context, None, instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_spice_console,
                          self.context, None, instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_rdphtml5_rdp_console(self):
        # Make sure we can a rdp console for an instance.
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='rdp')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        # Try with the full instance
        console = self.compute.get_rdp_console(self.context, 'rdp-html5',
                                               instance=instance)
        self.assertTrue(console)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_invalid_rdp_console_type(self):
        # Raise useful error if console type is an unrecognised string
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='rdp')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_rdp_console,
                          self.context, 'invalid', instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_rdp_console,
                          self.context, 'invalid', instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_missing_rdp_console_type(self):
        # Raise useful error is console type is None
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='rdp')

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
            instance, {}, {}, {}, block_device_mapping=[])

        self.assertRaises(messaging.ExpectedException,
                          self.compute.get_rdp_console,
                          self.context, None, instance=instance)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeInvalid,
                          self.compute.get_rdp_console,
                          self.context, None, instance=instance)

        self.compute.terminate_instance(self.context, instance, [], [])

    def test_vnc_console_instance_not_ready(self):
        self.flags(enabled=True, group='vnc')
        self.flags(enabled=False, group='spice')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        def fake_driver_get_console(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.stub_out("nova.virt.fake.FakeDriver.get_vnc_console",
                       fake_driver_get_console)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.InstanceNotReady,
                self.compute.get_vnc_console, self.context, 'novnc',
                instance=instance)

    def test_spice_console_instance_not_ready(self):
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='spice')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        def fake_driver_get_console(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.stub_out("nova.virt.fake.FakeDriver.get_spice_console",
                       fake_driver_get_console)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.InstanceNotReady,
                self.compute.get_spice_console, self.context, 'spice-html5',
                instance=instance)

    def test_rdp_console_instance_not_ready(self):
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=True, group='rdp')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        def fake_driver_get_console(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id=instance['uuid'])

        self.stub_out("nova.virt.fake.FakeDriver.get_rdp_console",
                       fake_driver_get_console)

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.InstanceNotReady,
                self.compute.get_rdp_console, self.context, 'rdp-html5',
                instance=instance)

    def test_vnc_console_disabled(self):
        self.flags(enabled=False, group='vnc')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeUnavailable,
                self.compute.get_vnc_console, self.context, 'novnc',
                instance=instance)

    def test_spice_console_disabled(self):
        self.flags(enabled=False, group='spice')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeUnavailable,
                self.compute.get_spice_console, self.context, 'spice-html5',
                instance=instance)

    def test_rdp_console_disabled(self):
        self.flags(enabled=False, group='rdp')
        instance = self._create_fake_instance_obj(
                params={'vm_state': vm_states.BUILDING})

        self.compute = utils.ExceptionHelper(self.compute)

        self.assertRaises(exception.ConsoleTypeUnavailable,
                self.compute.get_rdp_console, self.context, 'rdp-html5',
                instance=instance)

    def test_diagnostics(self):
        # Make sure we can get diagnostics for an instance.
        expected_diagnostic = {'cpu0_time': 17300000000,
                             'memory': 524288,
                             'vda_errors': -1,
                             'vda_read': 262144,
                             'vda_read_req': 112,
                             'vda_write': 5778432,
                             'vda_write_req': 488,
                             'vnet1_rx': 2070139,
                             'vnet1_rx_drop': 0,
                             'vnet1_rx_errors': 0,
                             'vnet1_rx_packets': 26701,
                             'vnet1_tx': 140208,
                             'vnet1_tx_drop': 0,
                             'vnet1_tx_errors': 0,
                             'vnet1_tx_packets': 662,
                            }

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
                instance, {}, {}, {}, block_device_mapping=[])

        diagnostics = self.compute.get_diagnostics(self.context,
                instance=instance)
        self.assertEqual(diagnostics, expected_diagnostic)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_instance_diagnostics(self):
        # Make sure we can get diagnostics for an instance.
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        diagnostics = self.compute.get_instance_diagnostics(self.context,
                instance=instance)
        expected = {'config_drive': True,
                    'cpu_details': [{'time': 17300000000}],
                    'disk_details': [{'errors_count': 0,
                                      'id': 'fake-disk-id',
                                      'read_bytes': 262144,
                                      'read_requests': 112,
                                      'write_bytes': 5778432,
                                      'write_requests': 488}],
                    'driver': 'fake',
                    'hypervisor_os': 'fake-os',
                    'memory_details': {'maximum': 524288, 'used': 0},
                    'nic_details': [{'mac_address': '01:23:45:67:89:ab',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 2070139,
                                     'rx_packets': 26701,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 140208,
                                     'tx_packets': 662}],
                    'state': 'running',
                    'uptime': 46664,
                    'version': '1.0'}
        self.assertEqual(expected, diagnostics)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_add_fixed_ip_usage_notification(self):
        def dummy(*args, **kwargs):
            pass

        self.stub_out('nova.network.api.API.add_fixed_ip_to_instance',
                       dummy)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       'inject_network_info', dummy)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       'reset_network', dummy)

        instance = self._create_fake_instance_obj()

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 0)
        self.compute.add_fixed_ip_to_instance(self.context, network_id=1,
                                              instance=instance)

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_remove_fixed_ip_usage_notification(self):
        def dummy(*args, **kwargs):
            pass

        self.stub_out('nova.network.api.API.remove_fixed_ip_from_instance',
                       dummy)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       'inject_network_info', dummy)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       'reset_network', dummy)

        instance = self._create_fake_instance_obj()

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 0)
        self.compute.remove_fixed_ip_from_instance(self.context, 1,
                                                   instance=instance)

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_run_instance_usage_notification(self, request_spec=None):
        # Ensure run instance generates appropriate usage notification.
        request_spec = request_spec or {}
        instance = self._create_fake_instance_obj()
        expected_image_name = request_spec.get('image', {}).get('name', '')
        self.compute.build_and_run_instance(self.context, instance,
                                            request_spec=request_spec,
                                            filter_properties={},
                                            image={'name':
                                                   expected_image_name},
                                            block_device_mapping=[])
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        instance.refresh()
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type, 'compute.instance.create.start')
        # The last event is the one with the sugar in it.
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.priority, 'INFO')
        self.assertEqual(msg.event_type, 'compute.instance.create.end')
        payload = msg.payload
        self.assertEqual(payload['tenant_id'], self.project_id)
        self.assertEqual(payload['user_id'], self.user_id)
        self.assertEqual(payload['instance_id'], instance['uuid'])
        self.assertEqual(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEqual(str(payload['instance_type_id']), str(type_id))
        flavor_id = flavors.get_flavor_by_name('m1.tiny')['flavorid']
        self.assertEqual(str(payload['instance_flavor_id']), str(flavor_id))
        self.assertEqual(payload['state'], 'active')
        self.assertIn('display_name', payload)
        self.assertIn('created_at', payload)
        self.assertIn('launched_at', payload)
        self.assertIn('fixed_ips', payload)
        self.assertTrue(payload['launched_at'])
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEqual(payload['image_ref_url'], image_ref_url)
        self.assertEqual('Success', payload['message'])
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_run_instance_image_usage_notification(self):
        request_spec = {'image': {'name': 'fake_name', 'key': 'value'}}
        self.test_run_instance_usage_notification(request_spec=request_spec)

    def test_run_instance_usage_notification_volume_meta(self):
        # Volume's image metadata won't contain the image name
        request_spec = {'image': {'key': 'value'}}
        self.test_run_instance_usage_notification(request_spec=request_spec)

    def test_run_instance_end_notification_on_abort(self):
        # Test that an error notif is sent if the build is aborted
        instance = self._create_fake_instance_obj()
        instance_uuid = instance['uuid']

        def build_inst_abort(*args, **kwargs):
            raise exception.BuildAbortException(reason="already deleted",
                    instance_uuid=instance_uuid)

        self.stub_out('nova.virt.fake.FakeDriver.spawn',
                       build_inst_abort)

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        self.assertGreaterEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type, 'compute.instance.create.start')
        msg = fake_notifier.NOTIFICATIONS[-1]

        self.assertEqual(msg.event_type, 'compute.instance.create.error')
        self.assertEqual('ERROR', msg.priority)
        payload = msg.payload
        message = payload['message']
        self.assertNotEqual(-1, message.find("already deleted"))

    def test_run_instance_error_notification_on_reschedule(self):
        # Test that error notif is sent if the build got rescheduled
        instance = self._create_fake_instance_obj()
        instance_uuid = instance['uuid']

        def build_inst_fail(*args, **kwargs):
            raise exception.RescheduledException(instance_uuid=instance_uuid,
                    reason="something bad happened")

        self.stub_out('nova.virt.fake.FakeDriver.spawn',
                       build_inst_fail)

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        self.assertGreaterEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type, 'compute.instance.create.start')
        msg = fake_notifier.NOTIFICATIONS[-1]

        self.assertEqual(msg.event_type, 'compute.instance.create.error')
        self.assertEqual('ERROR', msg.priority)
        payload = msg.payload
        message = payload['message']
        self.assertNotEqual(-1, message.find("something bad happened"))

    def test_run_instance_error_notification_on_failure(self):
        # Test that error notif is sent if build fails hard
        instance = self._create_fake_instance_obj()

        def build_inst_fail(*args, **kwargs):
            raise test.TestingException("i'm dying")

        self.stub_out('nova.virt.fake.FakeDriver.spawn',
                       build_inst_fail)

        self.compute.build_and_run_instance(
                self.context, instance, {}, {}, {}, block_device_mapping=[])

        self.assertGreaterEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type, 'compute.instance.create.start')
        msg = fake_notifier.NOTIFICATIONS[-1]

        self.assertEqual(msg.event_type, 'compute.instance.create.error')
        self.assertEqual('ERROR', msg.priority)
        payload = msg.payload
        message = payload['message']
        self.assertNotEqual(-1, message.find("i'm dying"))

    def test_terminate_usage_notification(self):
        # Ensure terminate_instance generates correct usage notification.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)

        time_fixture = self.useFixture(utils_fixture.TimeFixture(old_time))

        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        fake_notifier.NOTIFICATIONS = []
        time_fixture.advance_time_delta(cur_time - old_time)
        self.compute.terminate_instance(self.context, instance, [], [])

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 4)

        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.priority, 'INFO')
        self.assertEqual(msg.event_type, 'compute.instance.delete.start')
        msg1 = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg1.event_type, 'compute.instance.shutdown.start')
        msg1 = fake_notifier.NOTIFICATIONS[2]
        self.assertEqual(msg1.event_type, 'compute.instance.shutdown.end')
        msg1 = fake_notifier.NOTIFICATIONS[3]
        self.assertEqual(msg1.event_type, 'compute.instance.delete.end')
        payload = msg1.payload
        self.assertEqual(payload['tenant_id'], self.project_id)
        self.assertEqual(payload['user_id'], self.user_id)
        self.assertEqual(payload['instance_id'], instance['uuid'])
        self.assertEqual(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEqual(str(payload['instance_type_id']), str(type_id))
        flavor_id = flavors.get_flavor_by_name('m1.tiny')['flavorid']
        self.assertEqual(str(payload['instance_flavor_id']), str(flavor_id))
        self.assertIn('display_name', payload)
        self.assertIn('created_at', payload)
        self.assertIn('launched_at', payload)
        self.assertIn('terminated_at', payload)
        self.assertIn('deleted_at', payload)
        self.assertEqual(payload['terminated_at'], utils.strtime(cur_time))
        self.assertEqual(payload['deleted_at'], utils.strtime(cur_time))
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEqual(payload['image_ref_url'], image_ref_url)

    @mock.patch.object(network_api.API, "allocate_for_instance")
    @mock.patch.object(fake.FakeDriver, "macs_for_instance")
    def test_run_instance_queries_macs(self, mock_mac, mock_allocate):
        # run_instance should ask the driver for node mac addresses and pass
        # that to the network_api in use.
        fake_network.unset_stub_network_methods(self)
        instance = self._create_fake_instance_obj()

        macs = set(['01:23:45:67:89:ab'])
        mock_allocate.return_value = fake_network.fake_get_instance_nw_info(
                                                                    self, 1, 1)
        mock_mac.return_value = macs

        self.compute._build_networks_for_instance(self.context, instance,
                requested_networks=None, security_groups=None)

        mock_allocate.assert_called_once_with(self.context, instance,
                vpn=False, requested_networks=None, macs=macs,
                security_groups=[], dhcp_options=None,
                bind_host_id=self.compute.host)
        mock_mac.assert_called_once_with(test.MatchType(instance_obj.Instance))

    def _create_server_group(self, policies, instance_host):
        group_instance = self._create_fake_instance_obj(
                params=dict(host=instance_host))

        instance_group = objects.InstanceGroup(self.context)
        instance_group.user_id = self.user_id
        instance_group.project_id = self.project_id
        instance_group.name = 'messi'
        instance_group.uuid = str(uuid.uuid4())
        instance_group.members = [group_instance.uuid]
        instance_group.policies = policies
        fake_notifier.NOTIFICATIONS = []
        instance_group.create()
        self.assertEqual(1, len(fake_notifier.NOTIFICATIONS))
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(instance_group.name, msg.payload['name'])
        self.assertEqual(instance_group.members, msg.payload['members'])
        self.assertEqual(instance_group.policies, msg.payload['policies'])
        self.assertEqual(instance_group.project_id, msg.payload['project_id'])
        self.assertEqual(instance_group.uuid, msg.payload['uuid'])
        self.assertEqual('servergroup.create', msg.event_type)
        return instance_group

    def test_instance_set_to_error_on_uncaught_exception(self):
        # Test that instance is set to error state when exception is raised.
        instance = self._create_fake_instance_obj()
        fake_network.unset_stub_network_methods(self)

        @mock.patch.object(self.compute.network_api, 'allocate_for_instance',
                side_effect=messaging.RemoteError())
        @mock.patch.object(self.compute.network_api, 'deallocate_for_instance')
        def _do_test(mock_deallocate, mock_allocate):
            self.compute.build_and_run_instance(self.context, instance, {},
                    {}, {}, block_device_mapping=[])

            instance.refresh()
            self.assertEqual(vm_states.ERROR, instance.vm_state)

            self.compute.terminate_instance(self.context, instance, [], [])

        _do_test()

    @mock.patch.object(fake.FakeDriver, 'destroy')
    def test_delete_instance_keeps_net_on_power_off_fail(self, mock_destroy):
        exp = exception.InstancePowerOffFailure(reason='')
        mock_destroy.side_effect = exp
        instance = self._create_fake_instance_obj()

        self.assertRaises(exception.InstancePowerOffFailure,
                          self.compute._delete_instance,
                          self.context,
                          instance,
                          [],
                          self.none_quotas)

        mock_destroy.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY,
                                             mock.ANY)

    @mock.patch.object(compute_manager.ComputeManager, '_deallocate_network')
    @mock.patch.object(fake.FakeDriver, 'destroy')
    def test_delete_instance_loses_net_on_other_fail(self, mock_destroy,
                                                     mock_deallocate):
        exp = test.TestingException()
        mock_destroy.side_effect = exp
        instance = self._create_fake_instance_obj()

        self.assertRaises(test.TestingException,
                          self.compute._delete_instance,
                          self.context,
                          instance,
                          [],
                          self.none_quotas)

        mock_destroy.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY,
                                             mock.ANY)
        mock_deallocate.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY)

    def test_delete_instance_deletes_console_auth_tokens(self):
        instance = self._create_fake_instance_obj()
        self.flags(enabled=True, group='vnc')

        self.tokens_deleted = False

        def fake_delete_tokens(*args, **kwargs):
            self.tokens_deleted = True

        self.stub_out('nova.consoleauth.rpcapi.ConsoleAuthAPI.'
                       'delete_tokens_for_instance',
                       fake_delete_tokens)

        self.compute._delete_instance(self.context, instance, [],
                                      self.none_quotas)

        self.assertTrue(self.tokens_deleted)

    def test_delete_instance_deletes_console_auth_tokens_cells(self):
        instance = self._create_fake_instance_obj()
        self.flags(enabled=True, group='vnc')
        self.flags(enable=True, group='cells')

        self.tokens_deleted = False

        def fake_delete_tokens(*args, **kwargs):
            self.tokens_deleted = True

        self.stub_out('nova.cells.rpcapi.CellsAPI.consoleauth_delete_tokens',
                       fake_delete_tokens)

        self.compute._delete_instance(self.context, instance,
                                      [], self.none_quotas)

        self.assertTrue(self.tokens_deleted)

    def test_delete_instance_changes_power_state(self):
        """Test that the power state is NOSTATE after deleting an instance."""
        instance = self._create_fake_instance_obj()
        self.compute._delete_instance(self.context, instance, [],
                                      self.none_quotas)
        self.assertEqual(power_state.NOSTATE, instance.power_state)

    def test_instance_termination_exception_sets_error(self):
        """Test that we handle InstanceTerminationFailure
        which is propagated up from the underlying driver.
        """
        instance = self._create_fake_instance_obj()

        def fake_delete_instance(self, context, instance, bdms,
                                 reservations=None):
            raise exception.InstanceTerminationFailure(reason='')

        self.stub_out('nova.compute.manager.ComputeManager._delete_instance',
                       fake_delete_instance)

        self.assertRaises(exception.InstanceTerminationFailure,
                          self.compute.terminate_instance,
                          self.context,
                          instance, [], [])
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertEqual(instance['vm_state'], vm_states.ERROR)

    @mock.patch.object(compute_manager.ComputeManager, '_prep_block_device')
    def test_network_is_deallocated_on_spawn_failure(self, mock_prep):
        # When a spawn fails the network must be deallocated.
        instance = self._create_fake_instance_obj()
        mock_prep.side_effect = messaging.RemoteError('', '', '')

        self.compute.build_and_run_instance(
            self.context, instance, {}, {}, {}, block_device_mapping=[])

        self.compute.terminate_instance(self.context, instance, [], [])
        mock_prep.assert_called_once_with(mock.ANY, mock.ANY, mock.ANY)

    def _test_state_revert(self, instance, operation, pre_task_state,
                           kwargs=None, vm_state=None):
        if kwargs is None:
            kwargs = {}

        # The API would have set task_state, so do that here to test
        # that the state gets reverted on failure
        db.instance_update(self.context, instance['uuid'],
                           {"task_state": pre_task_state})

        orig_elevated = self.context.elevated
        orig_notify = self.compute._notify_about_instance_usage

        def _get_an_exception(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.context.RequestContext.elevated',
                      _get_an_exception)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       '_notify_about_instance_usage', _get_an_exception)

        func = getattr(self.compute, operation)

        self.assertRaises(test.TestingException,
                func, self.context, instance=instance, **kwargs)
        # self.context.elevated() is called in tearDown()
        self.stub_out('nova.context.RequestContext.elevated', orig_elevated)
        self.stub_out('nova.compute.manager.ComputeManager.'
                       '_notify_about_instance_usage', orig_notify)

        # Fetch the instance's task_state and make sure it reverted to None.
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        if vm_state:
            self.assertEqual(instance.vm_state, vm_state)
        self.assertIsNone(instance["task_state"])

    def test_state_revert(self):
        # ensure that task_state is reverted after a failed operation.
        migration = objects.Migration(context=self.context.elevated())
        migration.instance_uuid = 'b48316c5-71e8-45e4-9884-6c78055b9b13'
        migration.new_instance_type_id = '1'
        instance_type = objects.Flavor()

        actions = [
            ("reboot_instance", task_states.REBOOTING,
                                {'block_device_info': [],
                                 'reboot_type': 'SOFT'}),
            ("stop_instance", task_states.POWERING_OFF,
                              {'clean_shutdown': True}),
            ("start_instance", task_states.POWERING_ON),
            ("terminate_instance", task_states.DELETING,
                                     {'bdms': [],
                                     'reservations': []},
                                     vm_states.ERROR),
            ("soft_delete_instance", task_states.SOFT_DELETING,
                                     {'reservations': []}),
            ("restore_instance", task_states.RESTORING),
            ("rebuild_instance", task_states.REBUILDING,
                                 {'orig_image_ref': None,
                                  'image_ref': None,
                                  'injected_files': [],
                                  'new_pass': '',
                                  'orig_sys_metadata': {},
                                  'bdms': [],
                                  'recreate': False,
                                  'on_shared_storage': False}),
            ("set_admin_password", task_states.UPDATING_PASSWORD,
                                   {'new_pass': None}),
            ("rescue_instance", task_states.RESCUING,
                                {'rescue_password': None,
                                 'rescue_image_ref': None,
                                 'clean_shutdown': True}),
            ("unrescue_instance", task_states.UNRESCUING),
            ("revert_resize", task_states.RESIZE_REVERTING,
                              {'migration': migration,
                               'reservations': []}),
            ("prep_resize", task_states.RESIZE_PREP,
                            {'image': {},
                             'instance_type': instance_type,
                             'reservations': [],
                             'request_spec': {},
                             'filter_properties': {},
                             'node': None,
                             'clean_shutdown': True}),
            ("resize_instance", task_states.RESIZE_PREP,
                                {'migration': migration,
                                 'image': {},
                                 'reservations': [],
                                 'instance_type': {},
                                 'clean_shutdown': True}),
            ("pause_instance", task_states.PAUSING),
            ("unpause_instance", task_states.UNPAUSING),
            ("suspend_instance", task_states.SUSPENDING),
            ("resume_instance", task_states.RESUMING),
            ]

        self._stub_out_resize_network_methods()
        instance = self._create_fake_instance_obj()
        for operation in actions:
            if 'revert_resize' in operation:
                migration.source_compute = 'fake-mini'

            def fake_migration_save(*args, **kwargs):
                raise test.TestingException()

            self.stub_out('nova.objects.migration.Migration.save',
                          fake_migration_save)
            self._test_state_revert(instance, *operation)

    def _ensure_quota_reservations(self, instance,
                                   reservations, mock_quota):
        """Mock up commit/rollback of quota reservations."""
        mock_quota.assert_called_once_with(mock.ANY, reservations,
                                 project_id=instance['project_id'],
                                 user_id=instance['user_id'])

    @mock.patch.object(nova.quota.QUOTAS, 'commit')
    def test_quotas_successful_delete(self, mock_commit):
        instance = self._create_fake_instance_obj()
        resvs = list('fake_res')
        self.compute.terminate_instance(self.context, instance,
                                        bdms=[], reservations=resvs)
        self._ensure_quota_reservations(instance, resvs, mock_commit)

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_quotas_failed_delete(self, mock_rollback):
        instance = self._create_fake_instance_obj()

        def fake_shutdown_instance(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.compute.manager.ComputeManager._shutdown_instance',
                       fake_shutdown_instance)

        resvs = list('fake_res')
        self.assertRaises(test.TestingException,
                          self.compute.terminate_instance,
                          self.context, instance,
                          bdms=[], reservations=resvs)
        self._ensure_quota_reservations(instance,
                                        resvs, mock_rollback)

    @mock.patch.object(nova.quota.QUOTAS, 'commit')
    def test_quotas_successful_soft_delete(self, mock_commit):
        instance = self._create_fake_instance_obj(
                params=dict(task_state=task_states.SOFT_DELETING))
        resvs = list('fake_res')
        self.compute.soft_delete_instance(self.context, instance,
                                          reservations=resvs)
        self._ensure_quota_reservations(instance, resvs, mock_commit)

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_quotas_failed_soft_delete(self, mock_rollback):
        instance = self._create_fake_instance_obj(
            params=dict(task_state=task_states.SOFT_DELETING))

        def fake_soft_delete(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.virt.fake.FakeDriver.soft_delete',
                       fake_soft_delete)

        resvs = list('fake_res')

        self.assertRaises(test.TestingException,
                          self.compute.soft_delete_instance,
                          self.context, instance,
                          reservations=resvs)

        self._ensure_quota_reservations(instance,
                                        resvs, mock_rollback)

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_quotas_destroy_of_soft_deleted_instance(self, mock_rollback):
        instance = self._create_fake_instance_obj(
            params=dict(vm_state=vm_states.SOFT_DELETED))
        # Termination should be successful, but quota reservations
        # rolled back because the instance was in SOFT_DELETED state.
        resvs = list('fake_res')

        self.compute.terminate_instance(self.context, instance,
                                        bdms=[], reservations=resvs)

        self._ensure_quota_reservations(instance,
                                        resvs, mock_rollback)

    def _stub_out_resize_network_methods(self):
        def fake(cls, ctxt, instance, *args, **kwargs):
            pass

        self.stub_out('nova.network.api.API.setup_networks_on_host', fake)
        self.stub_out('nova.network.api.API.migrate_instance_start', fake)
        self.stub_out('nova.network.api.API.migrate_instance_finish', fake)

    def _test_finish_resize(self, power_on, resize_instance=True):
        # Contrived test to ensure finish_resize doesn't raise anything and
        # also tests resize from ACTIVE or STOPPED state which determines
        # if the resized instance is powered on or not.
        vm_state = None
        if power_on:
            vm_state = vm_states.ACTIVE
        else:
            vm_state = vm_states.STOPPED
        params = {'vm_state': vm_state}
        instance = self._create_fake_instance_obj(params)
        image = {}
        disk_info = 'fake-disk-info'
        instance_type = flavors.get_default_flavor()

        if not resize_instance:
            old_instance_type = flavors.get_flavor_by_name('m1.tiny')
            instance_type['root_gb'] = old_instance_type['root_gb']
            instance_type['swap'] = old_instance_type['swap']
            instance_type['ephemeral_gb'] = old_instance_type['ephemeral_gb']

        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={}, reservations=[], request_spec={},
                                 filter_properties={}, node=None,
                                 clean_shutdown=True)
        instance.task_state = task_states.RESIZE_MIGRATED
        instance.save()

        # NOTE(mriedem): make sure prep_resize set old_vm_state correctly
        sys_meta = instance.system_metadata
        self.assertIn('old_vm_state', sys_meta)
        if power_on:
            self.assertEqual(vm_states.ACTIVE, sys_meta['old_vm_state'])
        else:
            self.assertEqual(vm_states.STOPPED, sys_meta['old_vm_state'])
        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')

        orig_mig_save = migration.save
        orig_inst_save = instance.save
        network_api = self.compute.network_api

        with test.nested(
            mock.patch.object(network_api, 'setup_networks_on_host'),
            mock.patch.object(network_api, 'migrate_instance_finish'),
            mock.patch.object(self.compute.network_api,
                              'get_instance_nw_info'),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute.driver, 'finish_migration'),
            mock.patch.object(self.compute, '_get_instance_block_device_info'),
            mock.patch.object(migration, 'save'),
            mock.patch.object(instance, 'save'),
            mock.patch.object(nova.quota.QUOTAS, 'commit')
        ) as (mock_setup, mock_net_mig, mock_get_nw, mock_notify,
              mock_virt_mig, mock_get_blk, mock_mig_save, mock_inst_save,
              mock_commit):
            def _mig_save():
                self.assertEqual(migration.status, 'finished')
                self.assertEqual(vm_state, instance.vm_state)
                self.assertEqual(task_states.RESIZE_FINISH,
                                 instance.task_state)
                self.assertTrue(migration._context.is_admin)
                orig_mig_save()

            def _instance_save0(expected_task_state=None):
                self.assertEqual(task_states.RESIZE_MIGRATED,
                                 expected_task_state)
                self.assertEqual(instance_type['id'],
                                 instance.instance_type_id)
                self.assertEqual(task_states.RESIZE_FINISH,
                                 instance.task_state)
                orig_inst_save(expected_task_state=expected_task_state)

            def _instance_save1(expected_task_state=None):
                self.assertEqual(task_states.RESIZE_FINISH,
                                 expected_task_state)
                self.assertEqual(vm_states.RESIZED, instance.vm_state)
                self.assertIsNone(instance.task_state)
                self.assertIn('launched_at', instance.obj_what_changed())
                orig_inst_save(expected_task_state=expected_task_state)

            mock_get_nw.return_value = 'fake-nwinfo1'
            mock_get_blk.return_value = 'fake-bdminfo'
            inst_call_list = []

            # First save to update old/current flavor and task state
            exp_kwargs = dict(expected_task_state=task_states.RESIZE_MIGRATED)
            inst_call_list.append(mock.call(**exp_kwargs))
            mock_inst_save.side_effect = [_instance_save0]

            # Ensure instance status updates is after the migration finish
            mock_mig_save.side_effect = _mig_save
            exp_kwargs = dict(expected_task_state=task_states.RESIZE_FINISH)
            inst_call_list.append(mock.call(**exp_kwargs))
            mock_inst_save.side_effect = chain(mock_inst_save.side_effect,
                                               [_instance_save1])
            reservations = list('fake_res')

            self.compute.finish_resize(self.context,
                                       migration=migration,
                                       disk_info=disk_info, image=image,
                                       instance=instance,
                                       reservations=reservations)

            mock_setup.assert_called_once_with(self.context, instance,
                                               'fake-mini')
            mock_net_mig.assert_called_once_with(self.context,
                test.MatchType(objects.Instance), test.MatchType(dict))
            mock_get_nw.assert_called_once_with(self.context, instance)
            mock_notify.assert_has_calls([
                mock.call(self.context, instance, 'finish_resize.start',
                          network_info='fake-nwinfo1'),
                mock.call(self.context, instance, 'finish_resize.end',
                          network_info='fake-nwinfo1')])
            # nova.conf sets the default flavor to m1.small and the test
            # sets the default flavor to m1.tiny so they should be different
            # which makes this a resize
            mock_virt_mig.assert_called_once_with(self.context, migration,
                instance, disk_info, 'fake-nwinfo1',
                test.MatchType(objects.ImageMeta), resize_instance,
                'fake-bdminfo', power_on)
            mock_get_blk.assert_called_once_with(self.context, instance,
                                                 refresh_conn_info=True)
            mock_inst_save.assert_has_calls(inst_call_list)
            mock_mig_save.assert_called_once_with()
            self._ensure_quota_reservations(instance, reservations,
                                            mock_commit)

    def test_finish_resize_from_active(self):
        self._test_finish_resize(power_on=True)

    def test_finish_resize_from_stopped(self):
        self._test_finish_resize(power_on=False)

    def test_finish_resize_without_resize_instance(self):
        self._test_finish_resize(power_on=True, resize_instance=False)

    @mock.patch.object(nova.quota.QUOTAS, 'commit')
    def test_finish_resize_with_volumes(self, mock_commit):
        """Contrived test to ensure finish_resize doesn't raise anything."""

        # create instance
        instance = self._create_fake_instance_obj()

        # create volume
        volume = {'instance_uuid': None,
                  'device_name': None,
                  'id': 'fake',
                  'size': 200,
                  'attach_status': 'detached'}
        bdm = objects.BlockDeviceMapping(
                        **{'context': self.context,
                           'source_type': 'volume',
                           'destination_type': 'volume',
                           'volume_id': uuids.volume_id,
                           'instance_uuid': instance['uuid'],
                           'device_name': '/dev/vdc'})
        bdm.create()

        # stub out volume attach
        def fake_volume_get(self, context, volume_id):
            return volume
        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)

        def fake_volume_check_attach(self, context, volume_id, instance):
            pass
        self.stub_out('nova.volume.cinder.API.check_attach',
                      fake_volume_check_attach)

        def fake_get_volume_encryption_metadata(self, context, volume_id):
            return {}
        self.stub_out('nova.volume.cinder.API.get_volume_encryption_metadata',
                       fake_get_volume_encryption_metadata)

        orig_connection_data = {
            'target_discovered': True,
            'target_iqn': 'iqn.2010-10.org.openstack:%s.1' % uuids.volume_id,
            'target_portal': '127.0.0.0.1:3260',
            'volume_id': uuids.volume_id,
        }
        connection_info = {
            'driver_volume_type': 'iscsi',
            'data': orig_connection_data,
        }

        def fake_init_conn(self, context, volume_id, session):
            return connection_info
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                      fake_init_conn)

        def fake_attach(self, context, volume_id, instance_uuid, device_name,
                        mode='rw'):
            volume['instance_uuid'] = instance_uuid
            volume['device_name'] = device_name
        self.stub_out('nova.volume.cinder.API.attach', fake_attach)

        # stub out virt driver attach
        def fake_get_volume_connector(*args, **kwargs):
            return {}
        self.stub_out('nova.virt.fake.FakeDriver.get_volume_connector',
                       fake_get_volume_connector)

        def fake_attach_volume(*args, **kwargs):
            pass
        self.stub_out('nova.virt.fake.FakeDriver.attach_volume',
                       fake_attach_volume)

        # attach volume to instance
        self.compute.attach_volume(self.context, instance, bdm)

        # assert volume attached correctly
        self.assertEqual(volume['device_name'], '/dev/vdc')
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance.uuid)
        self.assertEqual(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEqual(bdm['device_name'], volume['device_name'])
            self.assertEqual(bdm['connection_info'],
                             jsonutils.dumps(connection_info))

        # begin resize
        instance_type = flavors.get_default_flavor()
        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={}, reservations=[], request_spec={},
                                 filter_properties={}, node=None,
                                 clean_shutdown=True)

        # fake out detach for prep_resize (and later terminate)
        def fake_terminate_connection(self, context, volume, connector):
            connection_info['data'] = None
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                       fake_terminate_connection)

        self._stub_out_resize_network_methods()

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')
        self.compute.resize_instance(self.context, instance=instance,
                migration=migration, image={}, reservations=[],
                instance_type=jsonutils.to_primitive(instance_type),
                clean_shutdown=True)

        # assert bdm is unchanged
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance.uuid)
        self.assertEqual(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEqual(bdm['device_name'], volume['device_name'])
            cached_connection_info = jsonutils.loads(bdm['connection_info'])
            self.assertEqual(cached_connection_info['data'],
                              orig_connection_data)
        # but connection was terminated
        self.assertIsNone(connection_info['data'])

        # stub out virt driver finish_migration
        def fake(*args, **kwargs):
            pass
        self.stub_out('nova.virt.fake.FakeDriver.finish_migration', fake)

        instance.task_state = task_states.RESIZE_MIGRATED
        instance.save()

        reservations = list('fake_res')

        # new initialize connection
        new_connection_data = dict(orig_connection_data)
        new_iqn = 'iqn.2010-10.org.openstack:%s.2' % uuids.volume_id,
        new_connection_data['target_iqn'] = new_iqn

        def fake_init_conn_with_data(self, context, volume, session):
            connection_info['data'] = new_connection_data
            return connection_info
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                       fake_init_conn_with_data)

        self.compute.finish_resize(self.context,
                migration=migration,
                disk_info={}, image={}, instance=instance,
                reservations=reservations)

        # assert volume attached correctly
        disk_info = db.block_device_mapping_get_all_by_instance(
            self.context, instance['uuid'])
        self.assertEqual(len(disk_info), 1)
        for bdm in disk_info:
            self.assertEqual(bdm['connection_info'],
                              jsonutils.dumps(connection_info))

        # stub out detach
        def fake_detach(self, context, volume_uuid):
            volume['device_path'] = None
            volume['instance_uuid'] = None
        self.stub_out('nova.volume.cinder.API.detach', fake_detach)
        self._ensure_quota_reservations(instance,
                                        reservations, mock_commit)

        # clean up
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_finish_resize_handles_error(self, mock_rollback):
        # Make sure we don't leave the instance in RESIZE on error.

        def throw_up(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.virt.fake.FakeDriver.finish_migration', throw_up)

        self._stub_out_resize_network_methods()

        old_flavor_name = 'm1.tiny'
        instance = self._create_fake_instance_obj(type_name=old_flavor_name)
        reservations = list('fake_res')

        instance_type = flavors.get_flavor_by_name('m1.small')

        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={}, reservations=reservations,
                                 request_spec={}, filter_properties={},
                                 node=None, clean_shutdown=True)

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')

        instance.refresh()
        instance.task_state = task_states.RESIZE_MIGRATED
        instance.save()
        self.assertRaises(test.TestingException, self.compute.finish_resize,
                          self.context,
                          migration=migration,
                          disk_info={}, image={}, instance=instance,
                          reservations=reservations)
        instance.refresh()
        self.assertEqual(vm_states.ERROR, instance.vm_state)

        old_flavor = flavors.get_flavor_by_name(old_flavor_name)
        self.assertEqual(old_flavor['memory_mb'], instance.memory_mb)
        self.assertEqual(old_flavor['vcpus'], instance.vcpus)
        self.assertEqual(old_flavor['root_gb'], instance.root_gb)
        self.assertEqual(old_flavor['ephemeral_gb'], instance.ephemeral_gb)
        self.assertEqual(old_flavor['id'], instance.instance_type_id)
        self.assertNotEqual(instance_type['id'], instance.instance_type_id)
        self._ensure_quota_reservations(instance, reservations,
                                        mock_rollback)

    def test_set_instance_info(self):
        old_flavor_name = 'm1.tiny'
        new_flavor_name = 'm1.small'
        instance = self._create_fake_instance_obj(type_name=old_flavor_name)
        new_flavor = flavors.get_flavor_by_name(new_flavor_name)

        self.compute._set_instance_info(instance, new_flavor.obj_clone())

        self.assertEqual(new_flavor['memory_mb'], instance.memory_mb)
        self.assertEqual(new_flavor['vcpus'], instance.vcpus)
        self.assertEqual(new_flavor['root_gb'], instance.root_gb)
        self.assertEqual(new_flavor['ephemeral_gb'], instance.ephemeral_gb)
        self.assertEqual(new_flavor['id'], instance.instance_type_id)

    def test_rebuild_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        time_fixture = self.useFixture(utils_fixture.TimeFixture(old_time))
        inst_ref = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context, inst_ref, {}, {}, {},
                                            block_device_mapping=[])
        time_fixture.advance_time_delta(cur_time - old_time)

        fake_notifier.NOTIFICATIONS = []
        instance = db.instance_get_by_uuid(self.context, inst_ref['uuid'])
        orig_sys_metadata = db.instance_system_metadata_get(self.context,
                inst_ref['uuid'])
        image_ref = instance["image_ref"]
        new_image_ref = uuids.new_image_ref
        db.instance_update(self.context, inst_ref['uuid'],
                           {'image_ref': new_image_ref})

        password = "new_password"

        inst_ref.task_state = task_states.REBUILDING
        inst_ref.save()
        self.compute.rebuild_instance(self.context,
                                      inst_ref,
                                      image_ref, new_image_ref,
                                      injected_files=[],
                                      new_pass=password,
                                      orig_sys_metadata=orig_sys_metadata,
                                      bdms=[], recreate=False,
                                      on_shared_storage=False)

        inst_ref.refresh()

        image_ref_url = glance.generate_image_url(image_ref)
        new_image_ref_url = glance.generate_image_url(new_image_ref)

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 3)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                          'compute.instance.exists')
        self.assertEqual(msg.payload['image_ref_url'], image_ref_url)
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                          'compute.instance.rebuild.start')
        self.assertEqual(msg.payload['image_ref_url'], new_image_ref_url)
        self.assertEqual(msg.payload['image_name'], 'fake_name')
        msg = fake_notifier.NOTIFICATIONS[2]
        self.assertEqual(msg.event_type,
                          'compute.instance.rebuild.end')
        self.assertEqual(msg.priority, 'INFO')
        payload = msg.payload
        self.assertEqual(payload['image_name'], 'fake_name')
        self.assertEqual(payload['tenant_id'], self.project_id)
        self.assertEqual(payload['user_id'], self.user_id)
        self.assertEqual(payload['instance_id'], inst_ref['uuid'])
        self.assertEqual(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEqual(str(payload['instance_type_id']), str(type_id))
        flavor_id = flavors.get_flavor_by_name('m1.tiny')['flavorid']
        self.assertEqual(str(payload['instance_flavor_id']), str(flavor_id))
        self.assertIn('display_name', payload)
        self.assertIn('created_at', payload)
        self.assertIn('launched_at', payload)
        self.assertEqual(payload['launched_at'], utils.strtime(cur_time))
        self.assertEqual(payload['image_ref_url'], new_image_ref_url)
        self.compute.terminate_instance(self.context, inst_ref, [], [])

    def test_finish_resize_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        time_fixture = self.useFixture(utils_fixture.TimeFixture(old_time))
        instance = self._create_fake_instance_obj()
        new_type = flavors.get_flavor_by_name('m1.small')
        new_type_id = new_type['id']
        flavor_id = new_type['flavorid']
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.host = 'foo'
        instance.task_state = task_states.RESIZE_PREP
        instance.save()

        self.compute.prep_resize(self.context, instance=instance,
                instance_type=new_type, image={}, reservations=[],
                request_spec={}, filter_properties={}, node=None,
                clean_shutdown=True)

        self._stub_out_resize_network_methods()

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')
        self.compute.resize_instance(self.context, instance=instance,
                migration=migration, image={}, instance_type=new_type,
                reservations=[], clean_shutdown=True)
        time_fixture.advance_time_delta(cur_time - old_time)
        fake_notifier.NOTIFICATIONS = []

        self.compute.finish_resize(self.context,
                migration=migration, reservations=[],
                disk_info={}, image={}, instance=instance)

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'compute.instance.finish_resize.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'compute.instance.finish_resize.end')
        self.assertEqual(msg.priority, 'INFO')
        payload = msg.payload
        self.assertEqual(payload['tenant_id'], self.project_id)
        self.assertEqual(payload['user_id'], self.user_id)
        self.assertEqual(payload['instance_id'], instance.uuid)
        self.assertEqual(payload['instance_type'], 'm1.small')
        self.assertEqual(str(payload['instance_type_id']), str(new_type_id))
        self.assertEqual(str(payload['instance_flavor_id']), str(flavor_id))
        self.assertIn('display_name', payload)
        self.assertIn('created_at', payload)
        self.assertIn('launched_at', payload)
        self.assertEqual(payload['launched_at'], utils.strtime(cur_time))
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEqual(payload['image_ref_url'], image_ref_url)
        self.compute.terminate_instance(self.context, instance, [], [])

    def test_resize_instance_notification(self):
        # Ensure notifications on instance migrate/resize.
        old_time = datetime.datetime(2012, 4, 1)
        cur_time = datetime.datetime(2012, 12, 21, 12, 21)
        time_fixture = self.useFixture(utils_fixture.TimeFixture(old_time))
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        time_fixture.advance_time_delta(cur_time - old_time)
        fake_notifier.NOTIFICATIONS = []

        instance.host = 'foo'
        instance.task_state = task_states.RESIZE_PREP
        instance.save()

        instance_type = flavors.get_default_flavor()
        self.compute.prep_resize(self.context, instance=instance,
                instance_type=instance_type, image={}, reservations=[],
                request_spec={}, filter_properties={}, node=None,
                clean_shutdown=True)
        db.migration_get_by_instance_and_status(self.context.elevated(),
                                                instance.uuid,
                                                'pre-migrating')

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 3)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'compute.instance.exists')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'compute.instance.resize.prep.start')
        msg = fake_notifier.NOTIFICATIONS[2]
        self.assertEqual(msg.event_type,
                         'compute.instance.resize.prep.end')
        self.assertEqual(msg.priority, 'INFO')
        payload = msg.payload
        self.assertEqual(payload['tenant_id'], self.project_id)
        self.assertEqual(payload['user_id'], self.user_id)
        self.assertEqual(payload['instance_id'], instance.uuid)
        self.assertEqual(payload['instance_type'], 'm1.tiny')
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        self.assertEqual(str(payload['instance_type_id']), str(type_id))
        flavor_id = flavors.get_flavor_by_name('m1.tiny')['flavorid']
        self.assertEqual(str(payload['instance_flavor_id']), str(flavor_id))
        self.assertIn('display_name', payload)
        self.assertIn('created_at', payload)
        self.assertIn('launched_at', payload)
        image_ref_url = glance.generate_image_url(FAKE_IMAGE_REF)
        self.assertEqual(payload['image_ref_url'], image_ref_url)
        self.compute.terminate_instance(self.context, instance, [], [])

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_prep_resize_instance_migration_error_on_none_host(self,
                                                               mock_rollback):
        """Ensure prep_resize raises a migration error if destination host is
        not defined
        """
        instance = self._create_fake_instance_obj()

        reservations = list('fake_res')

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.host = None
        instance.save()
        instance_type = flavors.get_default_flavor()

        self.assertRaises(exception.MigrationError, self.compute.prep_resize,
                          self.context, instance=instance,
                          instance_type=instance_type, image={},
                          reservations=reservations, request_spec={},
                          filter_properties={}, node=None,
                          clean_shutdown=True)
        self.compute.terminate_instance(self.context, instance, [], [])
        self._ensure_quota_reservations(instance, reservations,
                                        mock_rollback)

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_resize_instance_driver_error(self, mock_rollback):
        # Ensure instance status set to Error on resize error.

        def throw_up(*args, **kwargs):
            raise test.TestingException()

        self.stub_out('nova.virt.fake.FakeDriver.migrate_disk_and_power_off',
                       throw_up)

        instance = self._create_fake_instance_obj()
        instance_type = flavors.get_default_flavor()

        reservations = list('fake_res')

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.host = 'foo'
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type, image={},
                                 reservations=reservations, request_spec={},
                                 filter_properties={}, node=None,
                                 clean_shutdown=True)
        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')

        # verify
        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=instance,
                          migration=migration, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type),
                          clean_shutdown=True)
        # NOTE(comstud): error path doesn't use objects, so our object
        # is not updated.  Refresh and compare against the DB.
        instance.refresh()
        self.assertEqual(instance.vm_state, vm_states.ERROR)
        self.compute.terminate_instance(self.context, instance, [], [])
        self._ensure_quota_reservations(instance, reservations,
                                        mock_rollback)

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_resize_instance_driver_rollback(self, mock_rollback):
        # Ensure instance status set to Running after rollback.

        def throw_up(*args, **kwargs):
            raise exception.InstanceFaultRollback(test.TestingException())

        self.stub_out('nova.virt.fake.FakeDriver.migrate_disk_and_power_off',
                       throw_up)

        instance = self._create_fake_instance_obj()
        instance_type = flavors.get_default_flavor()
        reservations = list('fake_res')
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.host = 'foo'
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type, image={},
                                 reservations=reservations, request_spec={},
                                 filter_properties={}, node=None,
                                 clean_shutdown=True)
        instance.task_state = task_states.RESIZE_PREP
        instance.save()

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')

        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=instance,
                          migration=migration, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type),
                          clean_shutdown=True)
        # NOTE(comstud): error path doesn't use objects, so our object
        # is not updated.  Refresh and compare against the DB.
        instance.refresh()
        self.assertEqual(instance.vm_state, vm_states.ACTIVE)
        self.assertIsNone(instance.task_state)
        self.compute.terminate_instance(self.context, instance, [], [])
        self._ensure_quota_reservations(instance, reservations,
                                        mock_rollback)

    def _test_resize_instance(self, clean_shutdown=True):
        # Ensure instance can be migrated/resized.
        instance = self._create_fake_instance_obj()
        instance_type = flavors.get_default_flavor()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.host = 'foo'
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                instance_type=instance_type, image={}, reservations=[],
                request_spec={}, filter_properties={}, node=None,
                clean_shutdown=True)

        # verify 'old_vm_state' was set on system_metadata
        instance.refresh()
        sys_meta = instance.system_metadata
        self.assertEqual(vm_states.ACTIVE, sys_meta['old_vm_state'])

        self._stub_out_resize_network_methods()

        instance.task_state = task_states.RESIZE_PREP
        instance.save()

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')

        with test.nested(
            mock.patch.object(nova.compute.utils,
                'notify_about_instance_action'),
            mock.patch.object(objects.BlockDeviceMappingList,
                'get_by_instance_uuid', return_value='fake_bdms'),
            mock.patch.object(
                self.compute, '_get_instance_block_device_info',
                return_value='fake_bdinfo'),
            mock.patch.object(self.compute, '_terminate_volume_connections'),
            mock.patch.object(self.compute, '_get_power_off_values',
                return_value=(1, 2))
        ) as (mock_notify_action, mock_get_by_inst_uuid,
                mock_get_instance_vol_bdinfo,
                mock_terminate_vol_conn, mock_get_power_off_values):
            self.compute.resize_instance(self.context, instance=instance,
                    migration=migration, image={}, reservations=[],
                    instance_type=jsonutils.to_primitive(instance_type),
                    clean_shutdown=clean_shutdown)
            mock_notify_action.assert_has_calls([
                mock.call(self.context, instance, 'fake-mini',
                      action='resize', phase='start'),
                mock.call(self.context, instance, 'fake-mini',
                      action='resize', phase='end')])
            mock_get_instance_vol_bdinfo.assert_called_once_with(
                    self.context, instance, bdms='fake_bdms')
            mock_terminate_vol_conn.assert_called_once_with(self.context,
                    instance, 'fake_bdms')
            mock_get_power_off_values.assert_called_once_with(self.context,
                    instance, clean_shutdown)
            self.assertEqual(migration.dest_compute, instance.host)
            self.compute.terminate_instance(self.context, instance, [], [])

    def test_resize_instance(self):
        self._test_resize_instance()

    def test_resize_instance_forced_shutdown(self):
        self._test_resize_instance(clean_shutdown=False)

    @mock.patch.object(nova.quota.QUOTAS, 'commit')
    def _test_confirm_resize(self, mock_commit, power_on, numa_topology=None):
        # Common test case method for confirm_resize
        def fake(*args, **kwargs):
            pass

        def fake_confirm_migration_driver(*args, **kwargs):
            # Confirm the instance uses the new type in finish_resize
            self.assertEqual('3', instance.flavor.flavorid)

        old_vm_state = None
        p_state = None
        if power_on:
            old_vm_state = vm_states.ACTIVE
            p_state = power_state.RUNNING
        else:
            old_vm_state = vm_states.STOPPED
            p_state = power_state.SHUTDOWN
        params = {'vm_state': old_vm_state, 'power_state': p_state}
        instance = self._create_fake_instance_obj(params)

        self.flags(allow_resize_to_same_host=True)
        self.stub_out('nova.virt.fake.FakeDriver.finish_migration', fake)
        self.stub_out('nova.virt.fake.FakeDriver.confirm_migration',
                       fake_confirm_migration_driver)

        self._stub_out_resize_network_methods()

        reservations = list('fake_res')

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        # Confirm the instance size before the resize starts
        instance.refresh()
        flavor = objects.Flavor.get_by_id(self.context,
                                          instance.instance_type_id)
        self.assertEqual(flavor.flavorid, '1')

        instance.vm_state = old_vm_state
        instance.power_state = p_state
        instance.numa_topology = numa_topology
        instance.save()

        new_instance_type_ref = flavors.get_flavor_by_flavor_id(3)
        self.compute.prep_resize(self.context,
                instance=instance,
                instance_type=new_instance_type_ref,
                image={}, reservations=reservations, request_spec={},
                filter_properties={}, node=None, clean_shutdown=True)

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')
        migration_context = objects.MigrationContext.get_by_instance_uuid(
            self.context.elevated(), instance.uuid)
        self.assertIsInstance(migration_context.old_numa_topology,
                              numa_topology.__class__)
        self.assertIsNone(migration_context.new_numa_topology)

        # NOTE(mriedem): ensure prep_resize set old_vm_state in system_metadata
        sys_meta = instance.system_metadata
        self.assertEqual(old_vm_state, sys_meta['old_vm_state'])
        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        self.compute.resize_instance(self.context, instance=instance,
                                     migration=migration,
                                     image={},
                                     reservations=[],
                                     instance_type=new_instance_type_ref,
                                     clean_shutdown=True)
        self.compute.finish_resize(self.context,
                    migration=migration, reservations=[],
                    disk_info={}, image={}, instance=instance)

        # Prove that the instance size is now the new size
        flavor = objects.Flavor.get_by_id(self.context,
                                          instance.instance_type_id)
        self.assertEqual(flavor.flavorid, '3')
        # Prove that the NUMA topology has also been updated to that of the new
        # flavor - meaning None
        self.assertIsNone(instance.numa_topology)

        # Finally, confirm the resize and verify the new flavor is applied
        instance.task_state = None
        instance.save()
        self.compute.confirm_resize(self.context, instance=instance,
                                    reservations=reservations,
                                    migration=migration)

        instance.refresh()

        flavor = objects.Flavor.get_by_id(self.context,
                                          instance.instance_type_id)
        self.assertEqual(flavor.flavorid, '3')
        self.assertEqual('fake-mini', migration.source_compute)
        self.assertEqual(old_vm_state, instance.vm_state)
        self.assertIsNone(instance.task_state)
        self.assertIsNone(instance.migration_context)
        self.assertEqual(p_state, instance.power_state)
        self.compute.terminate_instance(self.context, instance, [], [])
        self._ensure_quota_reservations(instance, reservations,
                                        mock_commit)

    def test_confirm_resize_from_active(self):
        self._test_confirm_resize(power_on=True)

    def test_confirm_resize_from_stopped(self):
        self._test_confirm_resize(power_on=False)

    def test_confirm_resize_with_migration_context(self):
        numa_topology = (
            test_instance_numa_topology.get_fake_obj_numa_topology(
                self.context))
        self._test_confirm_resize(power_on=True, numa_topology=numa_topology)

    def test_confirm_resize_with_numa_topology_and_cpu_pinning(self):
        instance = self._create_fake_instance_obj()
        instance.old_flavor = instance.flavor
        instance.new_flavor = instance.flavor

        # we have two hosts with the same NUMA topologies.
        # now instance use two cpus from node_0 (cpu1 and cpu2) on current host
        old_inst_topology = objects.InstanceNUMATopology(
            instance_uuid=instance.uuid, cells=[
                objects.InstanceNUMACell(
                    id=0, cpuset=set([1, 2]), memory=512, pagesize=2048,
                    cpu_policy=obj_fields.CPUAllocationPolicy.DEDICATED,
                    cpu_pinning={'0': 1, '1': 2})
        ])
        # instance will use two cpus from node_1 (cpu3 and cpu4)
        # on *some other host*
        new_inst_topology = objects.InstanceNUMATopology(
            instance_uuid=instance.uuid, cells=[
                objects.InstanceNUMACell(
                    id=1, cpuset=set([3, 4]), memory=512, pagesize=2048,
                    cpu_policy=obj_fields.CPUAllocationPolicy.DEDICATED,
                    cpu_pinning={'0': 3, '1': 4})
        ])

        instance.numa_topology = old_inst_topology

        # instance placed in node_0 on current host. cpu1 and cpu2 from node_0
        # are used
        cell1 = objects.NUMACell(
            id=0, cpuset=set([1, 2]), pinned_cpus=set([1, 2]), memory=512,
            pagesize=2048, cpu_usage=2, memory_usage=0, siblings=[],
            mempages=[objects.NUMAPagesTopology(
                size_kb=2048, total=256, used=256)])
        # as instance placed in node_0 all cpus from node_1 (cpu3 and cpu4)
        # are free (on current host)
        cell2 = objects.NUMACell(
            id=1, cpuset=set([3, 4]), pinned_cpus=set(), memory=512,
            pagesize=2048, memory_usage=0, cpu_usage=0, siblings=[],
            mempages=[objects.NUMAPagesTopology(
                size_kb=2048, total=256, used=0)])
        host_numa_topology = objects.NUMATopology(cells=[cell1, cell2])

        migration = objects.Migration(context=self.context.elevated())
        migration.instance_uuid = instance.uuid
        migration.status = 'finished'
        migration.migration_type = 'migration'
        migration.source_node = NODENAME
        migration.create()

        migration_context = objects.MigrationContext()
        migration_context.migration_id = migration.id
        migration_context.old_numa_topology = old_inst_topology
        migration_context.new_numa_topology = new_inst_topology

        instance.migration_context = migration_context
        instance.vm_state = vm_states.RESIZED
        instance.system_metadata = {}
        instance.save()

        self.rt.tracked_migrations[instance.uuid] = (migration,
                                                     instance.flavor)
        self.rt.compute_node.numa_topology = jsonutils.dumps(
            host_numa_topology.obj_to_primitive())

        with mock.patch.object(self.compute.network_api,
                               'setup_networks_on_host'):
            self.compute.confirm_resize(self.context, instance=instance,
                                        migration=migration, reservations=[])
        instance.refresh()
        self.assertEqual(vm_states.ACTIVE, instance['vm_state'])

        updated_topology = objects.NUMATopology.obj_from_primitive(
            jsonutils.loads(self.rt.compute_node.numa_topology))

        # after confirming resize all cpus on currect host must be free
        self.assertEqual(2, len(updated_topology.cells))
        for cell in updated_topology.cells:
            self.assertEqual(0, cell.cpu_usage)
            self.assertEqual(set(), cell.pinned_cpus)

    def _test_resize_with_pci(self, method, expected_pci_addr):
        instance = self._create_fake_instance_obj()
        instance.old_flavor = instance.flavor
        instance.new_flavor = instance.flavor

        old_pci_devices = objects.PciDeviceList(
            objects=[objects.PciDevice(vendor_id='1377',
                                       product_id='0047',
                                       address='0000:0a:00.1')])

        new_pci_devices = objects.PciDeviceList(
            objects=[objects.PciDevice(vendor_id='1377',
                                       product_id='0047',
                                       address='0000:0b:00.1')])

        if expected_pci_addr == old_pci_devices[0].address:
            expected_pci_device = old_pci_devices[0]
        else:
            expected_pci_device = new_pci_devices[0]

        migration = objects.Migration(context=self.context.elevated())
        migration.instance_uuid = instance.uuid
        migration.status = 'finished'
        migration.migration_type = 'migration'
        migration.source_node = NODENAME
        migration.create()

        migration_context = objects.MigrationContext()
        migration_context.migration_id = migration.id
        migration_context.old_pci_devices = old_pci_devices
        migration_context.new_pci_devices = new_pci_devices

        instance.pci_devices = old_pci_devices
        instance.migration_context = migration_context
        instance.vm_state = vm_states.RESIZED
        instance.system_metadata = {}
        instance.save()

        self.rt.pci_tracker = mock.Mock()
        self.rt.tracked_migrations[instance.uuid] = (migration,
                                                     instance.flavor)

        with test.nested(
            mock.patch.object(self.compute.network_api,
                              'setup_networks_on_host'),
            mock.patch.object(self.compute.network_api,
                              'migrate_instance_start'),
            mock.patch.object(self.rt.pci_tracker,
                              'free_device')
            ) as (mock_setup, mock_migrate, mock_pci_free_device):
            method(self.context, instance=instance,
                                 migration=migration, reservations=[])
            mock_pci_free_device.assert_called_once_with(
                expected_pci_device, mock.ANY)

    def test_confirm_resize_with_pci(self):
        self._test_resize_with_pci(
            self.compute.confirm_resize, '0000:0a:00.1')

    def test_revert_resize_with_pci(self):
        self._test_resize_with_pci(
            self.compute.revert_resize, '0000:0b:00.1')

    @mock.patch.object(nova.quota.QUOTAS, 'commit')
    def _test_finish_revert_resize(self, mock_commit, power_on,
                                   remove_old_vm_state=False,
                                   numa_topology=None):
        """Convenience method that does most of the work for the
        test_finish_revert_resize tests.
        :param power_on -- True if testing resize from ACTIVE state, False if
        testing resize from STOPPED state.
        :param remove_old_vm_state -- True if testing a case where the
        'old_vm_state' system_metadata is not present when the
        finish_revert_resize method is called.
        """
        def fake(*args, **kwargs):
            pass

        def fake_finish_revert_migration_driver(*args, **kwargs):
            # Confirm the instance uses the old type in finish_revert_resize
            inst = args[2]
            self.assertEqual('1', inst.flavor.flavorid)

        old_vm_state = None
        if power_on:
            old_vm_state = vm_states.ACTIVE
        else:
            old_vm_state = vm_states.STOPPED
        params = {'vm_state': old_vm_state}
        instance = self._create_fake_instance_obj(params)

        self.stub_out('nova.virt.fake.FakeDriver.finish_migration', fake)
        self.stub_out('nova.virt.fake.FakeDriver.finish_revert_migration',
                      fake_finish_revert_migration_driver)

        self._stub_out_resize_network_methods()

        reservations = list('fake_res')

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.refresh()
        flavor = objects.Flavor.get_by_id(self.context,
                                          instance.instance_type_id)
        self.assertEqual(flavor.flavorid, '1')

        old_vm_state = instance['vm_state']

        instance.host = 'foo'
        instance.vm_state = old_vm_state
        instance.numa_topology = numa_topology
        instance.save()

        new_instance_type_ref = flavors.get_flavor_by_flavor_id(3)
        self.compute.prep_resize(self.context,
                instance=instance,
                instance_type=new_instance_type_ref,
                image={}, reservations=reservations, request_spec={},
                filter_properties={}, node=None,
                clean_shutdown=True)

        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')
        migration_context = objects.MigrationContext.get_by_instance_uuid(
            self.context.elevated(), instance.uuid)
        self.assertIsInstance(migration_context.old_numa_topology,
                              numa_topology.__class__)

        # NOTE(mriedem): ensure prep_resize set old_vm_state in system_metadata
        sys_meta = instance.system_metadata
        self.assertEqual(old_vm_state, sys_meta['old_vm_state'])
        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        self.compute.resize_instance(self.context, instance=instance,
                                     migration=migration,
                                     image={},
                                     reservations=[],
                                     instance_type=new_instance_type_ref,
                                     clean_shutdown=True)
        self.compute.finish_resize(self.context,
                    migration=migration, reservations=[],
                    disk_info={}, image={}, instance=instance)

        # Prove that the instance size is now the new size
        instance_type_ref = flavors.get_flavor_by_flavor_id(3)
        self.assertEqual(instance_type_ref['flavorid'], '3')
        # Prove that the NUMA topology has also been updated to that of the new
        # flavor - meaning None
        self.assertIsNone(instance.numa_topology)

        instance.task_state = task_states.RESIZE_REVERTING
        instance.save()

        self.compute.revert_resize(self.context,
                migration=migration, instance=instance,
                reservations=reservations)

        instance.refresh()
        if remove_old_vm_state:
            # need to wipe out the old_vm_state from system_metadata
            # before calling finish_revert_resize
            sys_meta = instance.system_metadata
            sys_meta.pop('old_vm_state')
            # Have to reset for save() to work
            instance.system_metadata = sys_meta
            instance.save()

        self.compute.finish_revert_resize(self.context,
                migration=migration,
                instance=instance, reservations=reservations)

        self.assertIsNone(instance.task_state)

        flavor = objects.Flavor.get_by_id(self.context,
                                          instance['instance_type_id'])
        self.assertEqual(flavor.flavorid, '1')
        self.assertEqual(instance.host, migration.source_compute)
        self.assertEqual(migration.dest_compute, migration.source_compute)
        self.assertIsInstance(instance.numa_topology, numa_topology.__class__)

        if remove_old_vm_state:
            self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        else:
            self.assertEqual(old_vm_state, instance.vm_state)

        self._ensure_quota_reservations(instance, reservations,
                                        mock_commit)

    def test_finish_revert_resize_from_active(self):
        self._test_finish_revert_resize(power_on=True)

    def test_finish_revert_resize_from_stopped(self):
        self._test_finish_revert_resize(power_on=False)

    def test_finish_revert_resize_from_stopped_remove_old_vm_state(self):
        # in  this case we resize from STOPPED but end up with ACTIVE
        # because the old_vm_state value is not present in
        # finish_revert_resize
        self._test_finish_revert_resize(power_on=False,
                                        remove_old_vm_state=True)

    def test_finish_revert_resize_migration_context(self):
        numa_topology = (
            test_instance_numa_topology.get_fake_obj_numa_topology(
                self.context))
        self._test_finish_revert_resize(power_on=True,
                                        numa_topology=numa_topology)

    def test_get_by_flavor_id(self):
        flavor_type = flavors.get_flavor_by_flavor_id(1)
        self.assertEqual(flavor_type['name'], 'm1.tiny')

    @mock.patch.object(nova.quota.QUOTAS, 'rollback')
    def test_resize_instance_handles_migration_error(self, mock_rollback):
        # Ensure vm_state is ERROR when error occurs.
        def raise_migration_failure(*args):
            raise test.TestingException()
        self.stub_out('nova.virt.fake.FakeDriver.migrate_disk_and_power_off',
                raise_migration_failure)

        instance = self._create_fake_instance_obj()
        reservations = list('fake_res')

        instance_type = flavors.get_default_flavor()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance.host = 'foo'
        instance.save()
        self.compute.prep_resize(self.context, instance=instance,
                                 instance_type=instance_type,
                                 image={}, reservations=reservations,
                                 request_spec={}, filter_properties={},
                                 node=None, clean_shutdown=True)
        migration = objects.Migration.get_by_instance_and_status(
                self.context.elevated(),
                instance.uuid, 'pre-migrating')
        instance.task_state = task_states.RESIZE_PREP
        instance.save()
        self.assertRaises(test.TestingException, self.compute.resize_instance,
                          self.context, instance=instance,
                          migration=migration, image={},
                          reservations=reservations,
                          instance_type=jsonutils.to_primitive(instance_type),
                          clean_shutdown=True)
        # NOTE(comstud): error path doesn't use objects, so our object
        # is not updated.  Refresh and compare against the DB.
        instance.refresh()
        self.assertEqual(instance.vm_state, vm_states.ERROR)
        self.compute.terminate_instance(self.context, instance, [], [])
        self._ensure_quota_reservations(instance, reservations,
                                        mock_rollback)

    def test_pre_live_migration_instance_has_no_fixed_ip(self):
        # Confirm that no exception is raised if there is no fixed ip on
        # pre_live_migration
        self.compute.driver.pre_live_migration(
            test.MatchType(nova.context.RequestContext),
            test.MatchType(objects.Instance),
            {'block_device_mapping': []},
            mock.ANY, mock.ANY, mock.ANY)

    @mock.patch.object(network_api.API, 'setup_networks_on_host')
    @mock.patch.object(fake.FakeDriver, 'ensure_filtering_rules_for_instance')
    @mock.patch.object(fake.FakeDriver, 'pre_live_migration')
    def test_pre_live_migration_works_correctly(self, mock_pre, mock_ensure,
                                                mock_setup):
        # Confirm setup_compute_volume is called when volume is mounted.
        def stupid(*args, **kwargs):
            return fake_network.fake_get_instance_nw_info(self)
        self.stub_out('nova.network.api.API.get_instance_nw_info', stupid)

        # creating instance testdata
        instance = self._create_fake_instance_obj({'host': 'dummy'})
        c = context.get_admin_context()
        nw_info = fake_network.fake_get_instance_nw_info(self)
        fake_notifier.NOTIFICATIONS = []
        migrate_data = {'is_shared_instance_path': False}
        mock_pre.return_value = None

        ret = self.compute.pre_live_migration(c, instance=instance,
                                              block_migration=False, disk=None,
                                              migrate_data=migrate_data)
        self.assertIsNone(ret)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'compute.instance.live_migration.pre.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'compute.instance.live_migration.pre.end')

        mock_pre.assert_called_once_with(
            test.MatchType(nova.context.RequestContext),
            test.MatchType(objects.Instance),
            {'swap': None, 'ephemerals': [],
             'root_device_name': None,
             'block_device_mapping': []},
            mock.ANY, mock.ANY, mock.ANY)
        mock_ensure.assert_called_once_with(test.MatchType(objects.Instance),
                                            nw_info)
        mock_setup.assert_called_once_with(c, instance, self.compute.host)

        # cleanup
        db.instance_destroy(c, instance['uuid'])

    @mock.patch.object(fake.FakeDriver, 'get_instance_disk_info')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'pre_live_migration')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(network_api.API, 'setup_networks_on_host')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'remove_volume_connection')
    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'rollback_live_migration_at_destination')
    @mock.patch('nova.objects.Migration.save')
    def test_live_migration_exception_rolls_back(self, mock_save,
                                mock_rollback, mock_remove, mock_setup,
                                mock_get_uuid, mock_pre, mock_get_disk):
        # Confirm exception when pre_live_migration fails.
        c = context.get_admin_context()

        instance = self._create_fake_instance_obj(
            {'host': 'src_host',
             'task_state': task_states.MIGRATING})
        updated_instance = self._create_fake_instance_obj(
                                               {'host': 'fake-dest-host'})
        dest_host = updated_instance['host']
        fake_bdms = [
                objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                        {'volume_id': uuids.volume_id_1,
                         'source_type': 'volume',
                         'destination_type': 'volume'})),
                objects.BlockDeviceMapping(
                    **fake_block_device.FakeDbBlockDeviceDict(
                        {'volume_id': uuids.volume_id_2,
                         'source_type': 'volume',
                         'destination_type': 'volume'}))
        ]
        migrate_data = migrate_data_obj.XenapiLiveMigrateData(
            block_migration=True)

        block_device_info = {
                'swap': None, 'ephemerals': [], 'block_device_mapping': [],
                'root_device_name': None}
        mock_get_disk.return_value = 'fake_disk'
        mock_pre.side_effect = test.TestingException
        mock_get_uuid.return_value = fake_bdms

        # start test
        migration = objects.Migration()
        self.assertRaises(test.TestingException,
                          self.compute.live_migration,
                          c, dest=dest_host, block_migration=True,
                          instance=instance, migration=migration,
                          migrate_data=migrate_data)
        instance.refresh()

        self.assertEqual('src_host', instance.host)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertIsNone(instance.task_state)
        self.assertEqual('error', migration.status)
        mock_get_disk.assert_called_once_with(instance,
                block_device_info=block_device_info)
        mock_pre.assert_called_once_with(c,
                instance, True, 'fake_disk', dest_host, migrate_data)
        mock_setup.assert_called_once_with(c, instance, self.compute.host)
        mock_get_uuid.assert_called_with(c, instance.uuid)
        mock_remove.assert_has_calls([
            mock.call(c, instance, uuids.volume_id_1, dest_host),
            mock.call(c, instance, uuids.volume_id_2, dest_host)])
        mock_rollback.assert_called_once_with(c, instance, dest_host,
            destroy_disks=True,
            migrate_data=test.MatchType(
                            migrate_data_obj.XenapiLiveMigrateData))

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'pre_live_migration')
    @mock.patch.object(network_api.API, 'migrate_instance_start')
    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'post_live_migration_at_destination')
    @mock.patch.object(network_api.API, 'setup_networks_on_host')
    @mock.patch.object(compute_manager.InstanceEvents,
                       'clear_events_for_instance')
    @mock.patch.object(compute_utils, 'EventReporter')
    @mock.patch('nova.objects.Migration.save')
    def test_live_migration_works_correctly(self, mock_save, mock_event,
            mock_clear, mock_setup, mock_post, mock_migrate, mock_pre):
        # Confirm live_migration() works as expected correctly.
        # creating instance testdata
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj(context=c)
        instance.host = self.compute.host
        dest = 'desthost'

        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_instance_path=False,
            is_shared_block_storage=False)
        mock_pre.return_value = migrate_data

        # start test
        migration = objects.Migration()
        ret = self.compute.live_migration(c, dest=dest,
                                          instance=instance,
                                          block_migration=False,
                                          migration=migration,
                                          migrate_data=migrate_data)

        self.assertIsNone(ret)
        mock_event.assert_called_with(
                c, 'compute_live_migration', instance.uuid)
        # cleanup
        instance.destroy()

        self.assertEqual('completed', migration.status)
        mock_pre.assert_called_once_with(c, instance, False, None,
                                         dest, migrate_data)
        mock_migrate.assert_called_once_with(c, instance,
                                             {'source_compute': instance[
                                              'host'], 'dest_compute': dest})
        mock_post.assert_called_once_with(c, instance, False, dest)
        mock_clear.assert_called_once_with(mock.ANY)

    @mock.patch.object(fake.FakeDriver, 'unfilter_instance')
    @mock.patch.object(network_api.API, 'migrate_instance_start')
    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'post_live_migration_at_destination')
    @mock.patch.object(network_api.API, 'setup_networks_on_host')
    @mock.patch.object(compute_manager.InstanceEvents,
                       'clear_events_for_instance')
    def test_post_live_migration_no_shared_storage_working_correctly(self,
            mock_clear, mock_setup, mock_post, mock_migrate, mock_unfilter):
        """Confirm post_live_migration() works correctly as expected
           for non shared storage migration.
        """
        # Create stubs
        result = {}
        # No share storage live migration don't need to destroy at source
        # server because instance has been migrated to destination, but a
        # cleanup for block device and network are needed.

        def fakecleanup(*args, **kwargs):
            result['cleanup'] = True

        self.stub_out('nova.virt.fake.FakeDriver.cleanup', fakecleanup)
        dest = 'desthost'
        srchost = self.compute.host

        # creating testdata
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj({
                                'host': srchost,
                                'state_description': 'migrating',
                                'state': power_state.PAUSED,
                                'task_state': task_states.MIGRATING,
                                'power_state': power_state.PAUSED})

        migration = {'source_compute': srchost, 'dest_compute': dest, }
        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_instance_path=False,
            is_shared_block_storage=False,
            block_migration=False)

        self.compute._post_live_migration(c, instance, dest,
                                          migrate_data=migrate_data)

        self.assertIn('cleanup', result)
        self.assertTrue(result['cleanup'])
        mock_unfilter.assert_called_once_with(instance, [])
        mock_migrate.assert_called_once_with(c, instance, migration)
        mock_post.assert_called_once_with(c, instance, False, dest)
        mock_clear.assert_called_once_with(mock.ANY)

    def test_post_live_migration_working_correctly(self):
        # Confirm post_live_migration() works as expected correctly.
        dest = 'desthost'
        srchost = self.compute.host

        # creating testdata
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj({
                                        'host': srchost,
                                        'state_description': 'migrating',
                                        'state': power_state.PAUSED},
                                                  context=c)

        instance.update({'task_state': task_states.MIGRATING,
                        'power_state': power_state.PAUSED})
        instance.save()

        migration_obj = objects.Migration()
        migrate_data = migrate_data_obj.LiveMigrateData(
            migration=migration_obj)

        # creating mocks
        with test.nested(
            mock.patch.object(self.compute.driver, 'post_live_migration'),
            mock.patch.object(self.compute.driver, 'unfilter_instance'),
            mock.patch.object(self.compute.network_api,
                              'migrate_instance_start'),
            mock.patch.object(self.compute.compute_rpcapi,
                              'post_live_migration_at_destination'),
            mock.patch.object(self.compute.driver,
                              'post_live_migration_at_source'),
            mock.patch.object(self.compute.network_api,
                              'setup_networks_on_host'),
            mock.patch.object(self.compute.instance_events,
                              'clear_events_for_instance'),
            mock.patch.object(self.compute, 'update_available_resource'),
            mock.patch.object(migration_obj, 'save'),
        ) as (
            post_live_migration, unfilter_instance,
            migrate_instance_start, post_live_migration_at_destination,
            post_live_migration_at_source, setup_networks_on_host,
            clear_events, update_available_resource, mig_save
        ):
            self.compute._post_live_migration(c, instance, dest,
                                              migrate_data=migrate_data)

            post_live_migration.assert_has_calls([
                mock.call(c, instance, {'swap': None, 'ephemerals': [],
                                        'root_device_name': None,
                                        'block_device_mapping': []},
                                        migrate_data)])
            unfilter_instance.assert_has_calls([mock.call(instance, [])])
            migration = {'source_compute': srchost,
                         'dest_compute': dest, }
            migrate_instance_start.assert_has_calls([
                mock.call(c, instance, migration)])
            post_live_migration_at_destination.assert_has_calls([
                mock.call(c, instance, False, dest)])
            post_live_migration_at_source.assert_has_calls(
                [mock.call(c, instance, [])])
            clear_events.assert_called_once_with(instance)
            update_available_resource.assert_has_calls([mock.call(c)])
            self.assertEqual('completed', migration_obj.status)
            mig_save.assert_called_once_with()

    def test_post_live_migration_terminate_volume_connections(self):
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj({
                                        'host': self.compute.host,
                                        'state_description': 'migrating',
                                        'state': power_state.PAUSED},
                                                  context=c)
        instance.update({'task_state': task_states.MIGRATING,
                         'power_state': power_state.PAUSED})
        instance.save()

        bdms = block_device_obj.block_device_make_list(c,
                [fake_block_device.FakeDbBlockDeviceDict({
                    'source_type': 'blank', 'guest_format': None,
                    'destination_type': 'local'}),
                 fake_block_device.FakeDbBlockDeviceDict({
                    'source_type': 'volume', 'destination_type': 'volume',
                    'volume_id': uuids.volume_id}),
                 ])

        with test.nested(
            mock.patch.object(self.compute.network_api,
                              'migrate_instance_start'),
            mock.patch.object(self.compute.compute_rpcapi,
                              'post_live_migration_at_destination'),
            mock.patch.object(self.compute.network_api,
                              'setup_networks_on_host'),
            mock.patch.object(self.compute.instance_events,
                              'clear_events_for_instance'),
            mock.patch.object(self.compute,
                              '_get_instance_block_device_info'),
            mock.patch.object(objects.BlockDeviceMappingList,
                              'get_by_instance_uuid'),
            mock.patch.object(self.compute.driver, 'get_volume_connector'),
            mock.patch.object(cinder.API, 'terminate_connection')
        ) as (
            migrate_instance_start, post_live_migration_at_destination,
            setup_networks_on_host, clear_events_for_instance,
            get_instance_volume_block_device_info, get_by_instance_uuid,
            get_volume_connector, terminate_connection
        ):
            get_by_instance_uuid.return_value = bdms
            get_volume_connector.return_value = 'fake-connector'

            self.compute._post_live_migration(c, instance, 'dest_host')

            terminate_connection.assert_called_once_with(
                    c, uuids.volume_id, 'fake-connector')

    @mock.patch('nova.objects.BlockDeviceMappingList.get_by_instance_uuid')
    def test_rollback_live_migration(self, mock_bdms):
        c = context.get_admin_context()
        instance = mock.MagicMock()
        migration = mock.MagicMock()
        migrate_data = {'migration': migration}

        mock_bdms.return_value = []

        @mock.patch.object(self.compute, '_live_migration_cleanup_flags')
        @mock.patch.object(self.compute, 'network_api')
        def _test(mock_nw_api, mock_lmcf):
            mock_lmcf.return_value = False, False
            self.compute._rollback_live_migration(c, instance, 'foo',
                                                  False,
                                                  migrate_data=migrate_data)
            mock_nw_api.setup_networks_on_host.assert_called_once_with(
                c, instance, self.compute.host)
        _test()

        self.assertEqual('error', migration.status)
        self.assertEqual(0, instance.progress)
        migration.save.assert_called_once_with()

    @mock.patch('nova.objects.BlockDeviceMappingList.get_by_instance_uuid')
    def test_rollback_live_migration_set_migration_status(self, mock_bdms):
        c = context.get_admin_context()
        instance = mock.MagicMock()
        migration = mock.MagicMock()
        migrate_data = {'migration': migration}

        mock_bdms.return_value = []

        @mock.patch.object(self.compute, '_live_migration_cleanup_flags')
        @mock.patch.object(self.compute, 'network_api')
        def _test(mock_nw_api, mock_lmcf):
            mock_lmcf.return_value = False, False
            self.compute._rollback_live_migration(c, instance, 'foo',
                                                  False,
                                                  migrate_data=migrate_data,
                                                  migration_status='fake')
            mock_nw_api.setup_networks_on_host.assert_called_once_with(
                c, instance, self.compute.host)
        _test()

        self.assertEqual('fake', migration.status)
        migration.save.assert_called_once_with()

    @mock.patch.object(network_api.API, 'setup_networks_on_host')
    @mock.patch.object(fake.FakeDriver,
                       'rollback_live_migration_at_destination')
    def test_rollback_live_migration_at_destination_correctly(self,
                                                    mock_rollback, mock_setup):
        # creating instance testdata
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj({'host': 'dummy'})
        fake_notifier.NOTIFICATIONS = []

        # start test
        ret = self.compute.rollback_live_migration_at_destination(c,
                                                    instance=instance,
                                                    destroy_disks=True,
                                                    migrate_data=None)
        self.assertIsNone(ret)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                       'compute.instance.live_migration.rollback.dest.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                        'compute.instance.live_migration.rollback.dest.end')
        mock_setup.assert_called_once_with(c, instance, self.compute.host,
                                           teardown=True)
        mock_rollback.assert_called_once_with(c, instance, [],
                        {'swap': None, 'ephemerals': [],
                         'root_device_name': None,
                         'block_device_mapping': []},
                        destroy_disks=True, migrate_data=None)

    @mock.patch('nova.network.api.API.setup_networks_on_host',
                side_effect=test.TestingException)
    @mock.patch('nova.virt.driver.ComputeDriver.'
                'rollback_live_migration_at_destination')
    @mock.patch('nova.objects.migrate_data.LiveMigrateData.'
                'detect_implementation')
    def test_rollback_live_migration_at_destination_network_fails(
            self, mock_detect, mock_rollback, net_mock):
        c = context.get_admin_context()
        instance = self._create_fake_instance_obj()
        self.assertRaises(test.TestingException,
                          self.compute.rollback_live_migration_at_destination,
                          c, instance, destroy_disks=True, migrate_data={})
        mock_rollback.assert_called_once_with(
            c, instance, mock.ANY, mock.ANY,
            destroy_disks=True,
            migrate_data=mock_detect.return_value)

    def test_run_kill_vm(self):
        # Detect when a vm is terminated behind the scenes.
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instances = db.instance_get_all(self.context)
        LOG.info("Running instances: %s", instances)
        self.assertEqual(len(instances), 1)

        instance_uuid = instances[0]['uuid']
        self.compute.driver._test_remove_vm(instance_uuid)

        # Force the compute manager to do its periodic poll
        ctxt = context.get_admin_context()
        self.compute._sync_power_states(ctxt)

        instances = db.instance_get_all(self.context)
        LOG.info("After force-killing instances: %s", instances)
        self.assertEqual(len(instances), 1)
        self.assertIsNone(instances[0]['task_state'])

    def _fill_fault(self, values):
        extra = {x: None for x in ['created_at',
                                   'deleted_at',
                                   'updated_at',
                                   'deleted']}
        extra['id'] = 1
        extra['details'] = ''
        extra.update(values)
        return extra

    def test_add_instance_fault(self):
        instance = self._create_fake_instance_obj()
        exc_info = None

        def fake_db_fault_create(ctxt, values):
            self.assertIn('raise NotImplementedError', values['details'])
            del values['details']

            expected = {
                'code': 500,
                'message': 'test',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        try:
            raise NotImplementedError('test')
        except NotImplementedError:
            exc_info = sys.exc_info()

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  instance,
                                                  NotImplementedError('test'),
                                                  exc_info)

    def test_add_instance_fault_with_remote_error(self):
        instance = self._create_fake_instance_obj()
        exc_info = None
        raised_exc = None

        def fake_db_fault_create(ctxt, values):
            global exc_info
            global raised_exc

            self.assertIn('raise messaging.RemoteError', values['details'])
            del values['details']

            expected = {
                'code': 500,
                'instance_uuid': instance['uuid'],
                'message': 'Remote error: test My Test Message\nNone.',
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        try:
            raise messaging.RemoteError('test', 'My Test Message')
        except messaging.RemoteError as exc:
            raised_exc = exc
            exc_info = sys.exc_info()

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
            instance, raised_exc, exc_info)

    def test_add_instance_fault_user_error(self):
        instance = self._create_fake_instance_obj()
        exc_info = None

        def fake_db_fault_create(ctxt, values):

            expected = {
                'code': 400,
                'message': 'fake details',
                'details': '',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        user_exc = exception.Invalid('fake details', code=400)

        try:
            raise user_exc
        except exception.Invalid:
            exc_info = sys.exc_info()

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
            instance, user_exc, exc_info)

    def test_add_instance_fault_no_exc_info(self):
        instance = self._create_fake_instance_obj()

        def fake_db_fault_create(ctxt, values):
            expected = {
                'code': 500,
                'message': 'test',
                'details': '',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  instance,
                                                  NotImplementedError('test'))

    def test_add_instance_fault_long_message(self):
        instance = self._create_fake_instance_obj()

        message = 300 * 'a'

        def fake_db_fault_create(ctxt, values):
            expected = {
                'code': 500,
                'message': message[:255],
                'details': '',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  instance,
                                                  NotImplementedError(message))

    def test_add_instance_fault_with_message(self):
        instance = self._create_fake_instance_obj()
        exc_info = None

        def fake_db_fault_create(ctxt, values):
            self.assertIn('raise NotImplementedError', values['details'])
            del values['details']

            expected = {
                'code': 500,
                'message': 'hoge',
                'instance_uuid': instance['uuid'],
                'host': self.compute.host
            }
            self.assertEqual(expected, values)
            return self._fill_fault(expected)

        try:
            raise NotImplementedError('test')
        except NotImplementedError:
            exc_info = sys.exc_info()

        self.stub_out('nova.db.instance_fault_create', fake_db_fault_create)

        ctxt = context.get_admin_context()
        compute_utils.add_instance_fault_from_exc(ctxt,
                                                  instance,
                                                  NotImplementedError('test'),
                                                  exc_info,
                                                  fault_message='hoge')

    def _test_cleanup_running(self, action):
        admin_context = context.get_admin_context()
        deleted_at = (timeutils.utcnow() -
                      datetime.timedelta(hours=1, minutes=5))
        instance1 = self._create_fake_instance_obj({"deleted_at": deleted_at,
                                                    "deleted": True})
        instance2 = self._create_fake_instance_obj({"deleted_at": deleted_at,
                                                    "deleted": True})
        self.flags(running_deleted_instance_timeout=3600,
                   running_deleted_instance_action=action)

        return admin_context, instance1, instance2

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(compute_manager.ComputeManager, "_cleanup_volumes")
    @mock.patch.object(compute_manager.ComputeManager, "_shutdown_instance")
    @mock.patch.object(objects.BlockDeviceMappingList, "get_by_instance_uuid")
    def test_cleanup_running_deleted_instances_reap(self, mock_get_uuid,
                                mock_shutdown, mock_cleanup, mock_get_inst):
        ctxt, inst1, inst2 = self._test_cleanup_running('reap')
        bdms = block_device_obj.block_device_make_list(ctxt, [])

        # Simulate an error and make sure cleanup proceeds with next instance.
        mock_shutdown.side_effect = [test.TestingException, None]
        mock_get_uuid.side_effect = [bdms, bdms]
        mock_cleanup.return_value = None
        mock_get_inst.return_value = [inst1, inst2]

        self.compute._cleanup_running_deleted_instances(ctxt)

        mock_shutdown.assert_has_calls([
            mock.call(ctxt, inst1, bdms, notify=False),
            mock.call(ctxt, inst2, bdms, notify=False)])
        mock_cleanup.assert_called_once_with(ctxt, inst2['uuid'], bdms)
        mock_get_uuid.assert_has_calls([
            mock.call(ctxt, inst1.uuid, use_slave=True),
            mock.call(ctxt, inst2.uuid, use_slave=True)])
        mock_get_inst.assert_called_once_with(ctxt,
                                              {'deleted': True,
                                               'soft_deleted': False,
                                               'host': self.compute.host})

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(fake.FakeDriver, "set_bootable")
    @mock.patch.object(fake.FakeDriver, "power_off")
    def test_cleanup_running_deleted_instances_shutdown(self, mock_power,
                                                        mock_set, mock_get):
        ctxt, inst1, inst2 = self._test_cleanup_running('shutdown')
        mock_get.return_value = [inst1, inst2]

        self.compute._cleanup_running_deleted_instances(ctxt)

        mock_get.assert_called_once_with(ctxt,
                                              {'deleted': True,
                                               'soft_deleted': False,
                                               'host': self.compute.host})
        mock_power.assert_has_calls([mock.call(inst1), mock.call(inst2)])
        mock_set.assert_has_calls([mock.call(inst1, False),
                                   mock.call(inst2, False)])

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(fake.FakeDriver, "set_bootable")
    @mock.patch.object(fake.FakeDriver, "power_off")
    def test_cleanup_running_deleted_instances_shutdown_notimpl(self,
                                            mock_power, mock_set, mock_get):
        ctxt, inst1, inst2 = self._test_cleanup_running('shutdown')
        mock_get.return_value = [inst1, inst2]
        mock_set.side_effect = [NotImplementedError, NotImplementedError]

        self.compute._cleanup_running_deleted_instances(ctxt)

        mock_get.assert_called_once_with(ctxt,
                                         {'deleted': True,
                                          'soft_deleted': False,
                                          'host': self.compute.host})
        mock_set.assert_has_calls([mock.call(inst1, False),
                                   mock.call(inst2, False)])
        mock_power.assert_has_calls([mock.call(inst1), mock.call(inst2)])

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(fake.FakeDriver, "set_bootable")
    @mock.patch.object(fake.FakeDriver, "power_off")
    def test_cleanup_running_deleted_instances_shutdown_error(self, mock_power,
                                        mock_set, mock_get):
        ctxt, inst1, inst2 = self._test_cleanup_running('shutdown')
        e = test.TestingException('bad')
        mock_get.return_value = [inst1, inst2]
        mock_power.side_effect = [e, e]

        self.compute._cleanup_running_deleted_instances(ctxt)

        mock_get.assert_called_once_with(ctxt,
                                         {'deleted': True,
                                          'soft_deleted': False,
                                          'host': self.compute.host})
        mock_power.assert_has_calls([mock.call(inst1), mock.call(inst2)])
        mock_set.assert_has_calls([mock.call(inst1, False),
                                   mock.call(inst2, False)])

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(timeutils, 'is_older_than')
    def test_running_deleted_instances(self, mock_is_older, mock_get):
        admin_context = context.get_admin_context()
        self.compute.host = 'host'
        instance = self._create_fake_instance_obj()
        instance.deleted = True
        now = timeutils.utcnow()
        instance.deleted_at = now
        mock_get.return_value = [instance]
        mock_is_older.return_value = True

        val = self.compute._running_deleted_instances(admin_context)

        self.assertEqual(val, [instance])
        mock_get.assert_called_once_with(
            admin_context, {'deleted': True,
                            'soft_deleted': False,
                            'host': self.compute.host})
        mock_is_older.assert_called_once_with(now,
                    CONF.running_deleted_instance_timeout)

    def _heal_instance_info_cache(self,
                                  _get_instance_nw_info_raise=False,
                                  _get_instance_nw_info_raise_cache=False):
        # Update on every call for the test
        self.flags(heal_instance_info_cache_interval=-1)
        ctxt = context.get_admin_context()

        instance_map = {}
        instances = []
        for x in range(8):
            inst_uuid = getattr(uuids, 'db_instance_%i' % x)
            instance_map[inst_uuid] = fake_instance.fake_db_instance(
                uuid=inst_uuid, host=CONF.host, created_at=None)
            # These won't be in our instance since they're not requested
            instances.append(instance_map[inst_uuid])

        call_info = {'get_all_by_host': 0, 'get_by_uuid': 0,
                'get_nw_info': 0, 'expected_instance': None}

        def fake_instance_get_all_by_host(context, host,
                                          columns_to_join, use_slave=False):
            call_info['get_all_by_host'] += 1
            self.assertEqual([], columns_to_join)
            return instances[:]

        def fake_instance_get_by_uuid(context, instance_uuid,
                                      columns_to_join, use_slave=False):
            if instance_uuid not in instance_map:
                raise exception.InstanceNotFound(instance_id=instance_uuid)
            call_info['get_by_uuid'] += 1
            self.assertEqual(['system_metadata', 'info_cache', 'extra',
                              'extra.flavor'],
                             columns_to_join)
            return instance_map[instance_uuid]

        # NOTE(comstud): Override the stub in setUp()
        def fake_get_instance_nw_info(cls, context, instance,
                                      use_slave=False):
            # Note that this exception gets caught in compute/manager
            # and is ignored.  However, the below increment of
            # 'get_nw_info' won't happen, and you'll get an assert
            # failure checking it below.
            self.assertEqual(call_info['expected_instance']['uuid'],
                             instance['uuid'])
            call_info['get_nw_info'] += 1
            if _get_instance_nw_info_raise:
                raise exception.InstanceNotFound(instance_id=instance['uuid'])
            if _get_instance_nw_info_raise_cache:
                raise exception.InstanceInfoCacheNotFound(
                                                instance_uuid=instance['uuid'])

        self.stub_out('nova.db.instance_get_all_by_host',
                fake_instance_get_all_by_host)
        self.stub_out('nova.db.instance_get_by_uuid',
                fake_instance_get_by_uuid)
        self.stub_out('nova.network.api.API.get_instance_nw_info',
                fake_get_instance_nw_info)

        # Make an instance appear to be still Building
        instances[0]['vm_state'] = vm_states.BUILDING
        # Make an instance appear to be Deleting
        instances[1]['task_state'] = task_states.DELETING
        # '0', '1' should be skipped..
        call_info['expected_instance'] = instances[2]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(1, call_info['get_all_by_host'])
        self.assertEqual(0, call_info['get_by_uuid'])
        self.assertEqual(1, call_info['get_nw_info'])

        call_info['expected_instance'] = instances[3]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(1, call_info['get_all_by_host'])
        self.assertEqual(1, call_info['get_by_uuid'])
        self.assertEqual(2, call_info['get_nw_info'])

        # Make an instance switch hosts
        instances[4]['host'] = 'not-me'
        # Make an instance disappear
        instance_map.pop(instances[5]['uuid'])
        # Make an instance switch to be Deleting
        instances[6]['task_state'] = task_states.DELETING
        # '4', '5', and '6' should be skipped..
        call_info['expected_instance'] = instances[7]
        self.compute._heal_instance_info_cache(ctxt)
        self.assertEqual(1, call_info['get_all_by_host'])
        self.assertEqual(4, call_info['get_by_uuid'])
        self.assertEqual(3, call_info['get_nw_info'])
        # Should be no more left.
        self.assertEqual(0, len(self.compute._instance_uuids_to_heal))

        # This should cause a DB query now, so get a list of instances
        # where none can be processed to make sure we handle that case
        # cleanly.   Use just '0' (Building) and '1' (Deleting)
        instances = instances[0:2]

        self.compute._heal_instance_info_cache(ctxt)
        # Should have called the list once more
        self.assertEqual(2, call_info['get_all_by_host'])
        # Stays the same because we remove invalid entries from the list
        self.assertEqual(4, call_info['get_by_uuid'])
        # Stays the same because we didn't find anything to process
        self.assertEqual(3, call_info['get_nw_info'])

    def test_heal_instance_info_cache(self):
        self._heal_instance_info_cache()

    def test_heal_instance_info_cache_with_instance_exception(self):
        self._heal_instance_info_cache(_get_instance_nw_info_raise=True)

    def test_heal_instance_info_cache_with_info_cache_exception(self):
        self._heal_instance_info_cache(_get_instance_nw_info_raise_cache=True)

    @mock.patch('nova.objects.InstanceList.get_by_filters')
    @mock.patch('nova.compute.api.API.unrescue')
    def test_poll_rescued_instances(self, unrescue, get):
        timed_out_time = timeutils.utcnow() - datetime.timedelta(minutes=5)
        not_timed_out_time = timeutils.utcnow()

        instances = [objects.Instance(
                         uuid=uuids.pool_instance_1,
                         vm_state=vm_states.RESCUED,
                         launched_at=timed_out_time),
                     objects.Instance(
                         uuid=uuids.pool_instance_2,
                         vm_state=vm_states.RESCUED,
                         launched_at=timed_out_time),
                     objects.Instance(
                         uuid=uuids.pool_instance_3,
                         vm_state=vm_states.RESCUED,
                         launched_at=not_timed_out_time)]
        unrescued_instances = {uuids.pool_instance_1: False,
                               uuids.pool_instance_2: False}

        def fake_instance_get_all_by_filters(context, filters,
                                             expected_attrs=None,
                                             use_slave=False):
            self.assertEqual(["system_metadata"], expected_attrs)
            return instances

        get.side_effect = fake_instance_get_all_by_filters

        def fake_unrescue(context, instance):
            unrescued_instances[instance['uuid']] = True

        unrescue.side_effect = fake_unrescue

        self.flags(rescue_timeout=60)
        ctxt = context.get_admin_context()

        self.compute._poll_rescued_instances(ctxt)

        for instance in unrescued_instances.values():
            self.assertTrue(instance)

    @mock.patch('nova.objects.InstanceList.get_by_filters')
    def test_poll_rebooting_instances(self, get):
        reboot_timeout = 60
        updated_at = timeutils.utcnow() - datetime.timedelta(minutes=5)
        to_poll = [objects.Instance(
                       uuid=uuids.pool_instance_1,
                       task_state=task_states.REBOOTING,
                       updated_at=updated_at),
                   objects.Instance(
                       uuid=uuids.pool_instance_2,
                       task_state=task_states.REBOOT_STARTED,
                       updated_at=updated_at),
                   objects.Instance(
                       uuid=uuids.pool_instance_3,
                       task_state=task_states.REBOOT_PENDING,
                       updated_at=updated_at)]
        self.flags(reboot_timeout=reboot_timeout)
        get.return_value = to_poll
        ctxt = context.get_admin_context()

        with (mock.patch.object(
            self.compute.driver, 'poll_rebooting_instances'
        )) as mock_poll:
            self.compute._poll_rebooting_instances(ctxt)
            mock_poll.assert_called_with(reboot_timeout, to_poll)

        filters = {'host': 'fake-mini',
                   'task_state': [
                       task_states.REBOOTING, task_states.REBOOT_STARTED,
                       task_states.REBOOT_PENDING]}
        get.assert_called_once_with(ctxt, filters,
                                    expected_attrs=[], use_slave=True)

    def test_poll_unconfirmed_resizes(self):
        instances = [
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_1,
                                           vm_state=vm_states.RESIZED,
                                           task_state=None),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_none),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_2,
                                           vm_state=vm_states.ERROR,
                                           task_state=None),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_3,
                                           vm_state=vm_states.ACTIVE,
                                           task_state=
                                           task_states.REBOOTING),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_4,
                                           vm_state=vm_states.RESIZED,
                                           task_state=None),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_5,
                                           vm_state=vm_states.ACTIVE,
                                           task_state=None),
            # The expceted migration result will be None instead of error
            # since _poll_unconfirmed_resizes will not change it
            # when the instance vm state is RESIZED and task state
            # is deleting, see bug 1301696 for more detail
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_6,
                                           vm_state=vm_states.RESIZED,
                                           task_state='deleting'),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_7,
                                           vm_state=vm_states.RESIZED,
                                           task_state='soft-deleting'),
            fake_instance.fake_db_instance(uuid=uuids.migration_instance_8,
                                           vm_state=vm_states.ACTIVE,
                                           task_state='resize_finish')]
        expected_migration_status = {uuids.migration_instance_1: 'confirmed',
                                     uuids.migration_instance_none: 'error',
                                     uuids.migration_instance_2: 'error',
                                     uuids.migration_instance_3: 'error',
                                     uuids.migration_instance_4: None,
                                     uuids.migration_instance_5: 'error',
                                     uuids.migration_instance_6: None,
                                     uuids.migration_instance_7: None,
                                     uuids.migration_instance_8: None}
        migrations = []
        for i, instance in enumerate(instances, start=1):
            fake_mig = test_migration.fake_db_migration()
            fake_mig.update({'id': i,
                             'instance_uuid': instance['uuid'],
                             'status': None})
            migrations.append(fake_mig)

        def fake_instance_get_by_uuid(context, instance_uuid,
                columns_to_join=None, use_slave=False):
            self.assertIn('metadata', columns_to_join)
            self.assertIn('system_metadata', columns_to_join)
            # raise InstanceNotFound exception for non-existing instance
            # represented by UUID: uuids.migration_instance_none
            if instance_uuid == uuids.db_instance_nonexist:
                raise exception.InstanceNotFound(instance_id=instance_uuid)
            for instance in instances:
                if instance['uuid'] == instance_uuid:
                    return instance

        def fake_migration_get_unconfirmed_by_dest_compute(context,
                resize_confirm_window, dest_compute, use_slave=False):
            self.assertEqual(dest_compute, CONF.host)
            return migrations

        def fake_migration_update(context, mid, updates):
            for migration in migrations:
                if migration['id'] == mid:
                    migration.update(updates)
                    return migration

        def fake_confirm_resize(cls, context, instance, migration=None):
            # raise exception for uuids.migration_instance_4 to check
            # migration status does not get set to 'error' on confirm_resize
            # failure.
            if instance['uuid'] == uuids.migration_instance_4:
                raise test.TestingException('bomb')
            self.assertIsNotNone(migration)
            for migration2 in migrations:
                if (migration2['instance_uuid'] ==
                        migration['instance_uuid']):
                    migration2['status'] = 'confirmed'

        self.stub_out('nova.db.instance_get_by_uuid',
                fake_instance_get_by_uuid)
        self.stub_out('nova.db.migration_get_unconfirmed_by_dest_compute',
                fake_migration_get_unconfirmed_by_dest_compute)
        self.stub_out('nova.db.migration_update', fake_migration_update)
        self.stub_out('nova.compute.api.API.confirm_resize',
                      fake_confirm_resize)

        def fetch_instance_migration_status(instance_uuid):
            for migration in migrations:
                if migration['instance_uuid'] == instance_uuid:
                    return migration['status']

        self.flags(resize_confirm_window=60)
        ctxt = context.get_admin_context()

        self.compute._poll_unconfirmed_resizes(ctxt)

        for instance_uuid, status in six.iteritems(expected_migration_status):
            self.assertEqual(status,
                             fetch_instance_migration_status(instance_uuid))

    def test_instance_build_timeout_mixed_instances(self):
        # Tests that instances which failed to build within the configured
        # instance_build_timeout value are set to error state.
        self.flags(instance_build_timeout=30)
        ctxt = context.get_admin_context()
        created_at = timeutils.utcnow() + datetime.timedelta(seconds=-60)

        filters = {'vm_state': vm_states.BUILDING, 'host': CONF.host}
        # these are the ones that are expired
        old_instances = []
        for x in range(4):
            instance = {'uuid': str(uuid.uuid4()), 'created_at': created_at}
            instance.update(filters)
            old_instances.append(fake_instance.fake_db_instance(**instance))

        # not expired
        instances = list(old_instances)  # copy the contents of old_instances
        new_instance = {
            'uuid': str(uuid.uuid4()),
            'created_at': timeutils.utcnow(),
        }
        sort_key = 'created_at'
        sort_dir = 'desc'
        new_instance.update(filters)
        instances.append(fake_instance.fake_db_instance(**new_instance))

        # creating mocks
        with test.nested(
            mock.patch.object(self.compute.db.sqlalchemy.api,
                              'instance_get_all_by_filters',
                              return_value=instances),
            mock.patch.object(objects.Instance, 'save'),
        ) as (
            instance_get_all_by_filters,
            conductor_instance_update
        ):
            # run the code
            self.compute._check_instance_build_time(ctxt)
            # check our assertions
            instance_get_all_by_filters.assert_called_once_with(
                                            ctxt, filters,
                                            sort_key,
                                            sort_dir,
                                            marker=None,
                                            columns_to_join=[],
                                            limit=None)
            self.assertThat(conductor_instance_update.mock_calls,
                            testtools_matchers.HasLength(len(old_instances)))
            for inst in old_instances:
                conductor_instance_update.assert_has_calls([
                    mock.call()])

    def test_get_resource_tracker_fail(self):
        self.assertRaises(exception.NovaException,
                          self.compute._get_resource_tracker,
                          'invalidnodename')

    @mock.patch.object(objects.Instance, 'save')
    def test_instance_update_host_check(self, mock_save):
        # make sure rt usage doesn't happen if the host or node is different
        def fail_get(self, nodename):
            raise test.TestingException("wrong host/node")
        self.stub_out('nova.compute.manager.ComputeManager.'
                      '_get_resource_tracker', fail_get)

        instance = self._create_fake_instance_obj({'host': 'someotherhost'})
        self.compute._instance_update(self.context, instance, vcpus=4)

        instance = self._create_fake_instance_obj({'node': 'someothernode'})
        self.compute._instance_update(self.context, instance, vcpus=4)

        params = {'host': 'someotherhost', 'node': 'someothernode'}
        instance = self._create_fake_instance_obj(params)
        self.compute._instance_update(self.context, instance, vcpus=4)

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instance_block_device_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_is_instance_storage_shared')
    @mock.patch.object(fake.FakeDriver, 'destroy')
    @mock.patch('nova.objects.MigrationList.get_by_filters')
    @mock.patch('nova.objects.Migration.save')
    def test_destroy_evacuated_instance_on_shared_storage(self, mock_save,
            mock_get_filter, mock_destroy, mock_is_inst, mock_get_blk,
            mock_get_nw, mock_get_inst):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host})
            ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance_obj(
            {'host': 'otherhost'})

        migration = objects.Migration(instance_uuid=evacuated_instance.uuid)
        mock_get_filter.return_value = [migration]
        instances.append(evacuated_instance)
        mock_get_inst.return_value = instances
        mock_get_nw.return_value = 'fake_network_info'
        mock_get_blk.return_value = 'fake_bdi'
        mock_is_inst.return_value = True

        self.compute._destroy_evacuated_instances(fake_context)

        mock_get_filter.assert_called_once_with(fake_context,
                                         {'source_compute': self.compute.host,
                                          'status': ['accepted', 'done'],
                                          'migration_type': 'evacuation'})
        mock_get_inst.assert_called_once_with(fake_context, {'deleted': False})
        mock_get_nw.assert_called_once_with(fake_context, evacuated_instance)
        mock_get_blk.assert_called_once_with(fake_context, evacuated_instance)
        mock_is_inst.assert_called_once_with(fake_context, evacuated_instance)
        mock_destroy.assert_called_once_with(fake_context, evacuated_instance,
                                             'fake_network_info',
                                             'fake_bdi', False)

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instance_block_device_info')
    @mock.patch.object(fake.FakeDriver,
                       'check_instance_shared_storage_local')
    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'check_instance_shared_storage')
    @mock.patch.object(fake.FakeDriver,
                       'check_instance_shared_storage_cleanup')
    @mock.patch.object(fake.FakeDriver, 'destroy')
    @mock.patch('nova.objects.MigrationList.get_by_filters')
    @mock.patch('nova.objects.Migration.save')
    def test_destroy_evacuated_instance_with_disks(self, mock_save,
            mock_get_filter, mock_destroy, mock_check_clean, mock_check,
            mock_check_local, mock_get_blk, mock_get_nw, mock_get_drv):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host})
        ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance_obj(
            {'host': 'otherhost'})

        migration = objects.Migration(instance_uuid=evacuated_instance.uuid)
        mock_get_filter.return_value = [migration]
        instances.append(evacuated_instance)
        mock_get_drv.return_value = instances
        mock_get_nw.return_value = 'fake_network_info'
        mock_get_blk.return_value = 'fake-bdi'
        mock_check_local.return_value = {'filename': 'tmpfilename'}
        mock_check.return_value = False

        self.compute._destroy_evacuated_instances(fake_context)

        mock_get_drv.assert_called_once_with(fake_context, {'deleted': False})
        mock_get_nw.assert_called_once_with(fake_context, evacuated_instance)
        mock_get_blk.assert_called_once_with(fake_context, evacuated_instance)
        mock_check_local.assert_called_once_with(fake_context,
                                                 evacuated_instance)
        mock_check.assert_called_once_with(fake_context, evacuated_instance,
                                           {'filename': 'tmpfilename'},
                                           host=None)
        mock_check_clean.assert_called_once_with(fake_context,
                                                 {'filename': 'tmpfilename'})
        mock_destroy.assert_called_once_with(fake_context, evacuated_instance,
                                             'fake_network_info', 'fake-bdi',
                                             True)

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instances_on_driver')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_instance_block_device_info')
    @mock.patch.object(fake.FakeDriver, 'check_instance_shared_storage_local')
    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'check_instance_shared_storage')
    @mock.patch.object(fake.FakeDriver,
                       'check_instance_shared_storage_cleanup')
    @mock.patch.object(fake.FakeDriver, 'destroy')
    @mock.patch('nova.objects.MigrationList.get_by_filters')
    @mock.patch('nova.objects.Migration.save')
    def test_destroy_evacuated_instance_not_implemented(self, mock_save,
            mock_get_filter, mock_destroy, mock_check_clean, mock_check,
            mock_check_local, mock_get_blk, mock_get_nw, mock_get_inst):
        fake_context = context.get_admin_context()

        # instances in central db
        instances = [
            # those are still related to this host
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host}),
            self._create_fake_instance_obj(
                {'host': self.compute.host})
        ]

        # those are already been evacuated to other host
        evacuated_instance = self._create_fake_instance_obj(
            {'host': 'otherhost'})

        migration = objects.Migration(instance_uuid=evacuated_instance.uuid)
        mock_get_filter.return_value = [migration]
        instances.append(evacuated_instance)
        mock_get_inst.return_value = instances
        mock_get_nw.return_value = 'fake_network_info'
        mock_get_blk.return_value = 'fake_bdi'
        mock_check_local.side_effect = NotImplementedError

        self.compute._destroy_evacuated_instances(fake_context)

        mock_get_inst.assert_called_once_with(fake_context, {'deleted': False})
        mock_get_nw.assert_called_once_with(fake_context, evacuated_instance)
        mock_get_blk.assert_called_once_with(fake_context, evacuated_instance)
        mock_check_local.assert_called_once_with(fake_context,
                                                 evacuated_instance)
        mock_destroy.assert_called_once_with(fake_context, evacuated_instance,
                                             'fake_network_info',
                                             'fake_bdi', True)

    def test_complete_partial_deletion(self):
        admin_context = context.get_admin_context()
        instance = objects.Instance()
        instance.id = 1
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.DELETED
        instance.task_state = None
        instance.system_metadata = {'fake_key': 'fake_value'}
        instance.flavor = objects.Flavor(vcpus=1, memory_mb=1)
        instance.project_id = 'fake-prj'
        instance.user_id = 'fake-user'
        instance.deleted = False

        def fake_destroy(self):
            instance.deleted = True

        self.stub_out('nova.objects.instance.Instance.destroy', fake_destroy)

        self.stub_out('nova.db.block_device_mapping_get_all_by_instance',
                      lambda *a, **k: None)

        self.stub_out('nova.compute.manager.ComputeManager.'
                       '_complete_deletion',
                       lambda *a, **k: None)

        self.stub_out('nova.objects.quotas.Quotas.reserve',
                      lambda *a, **k: None)

        self.compute._complete_partial_deletion(admin_context, instance)

        self.assertNotEqual(0, instance.deleted)

    def test_terminate_instance_updates_tracker(self):
        rt = self.compute._get_resource_tracker(NODENAME)
        admin_context = context.get_admin_context()

        self.assertEqual(0, rt.compute_node.vcpus_used)
        instance = self._create_fake_instance_obj()
        instance.vcpus = 1

        rt.instance_claim(admin_context, instance)
        self.assertEqual(1, rt.compute_node.vcpus_used)

        self.compute.terminate_instance(admin_context, instance, [], [])
        self.assertEqual(0, rt.compute_node.vcpus_used)

    @mock.patch('nova.compute.manager.ComputeManager'
                '._notify_about_instance_usage')
    @mock.patch('nova.objects.Quotas.reserve')
    # NOTE(cdent): At least in this test destroy() on the instance sets it
    # state back to active, meaning the resource tracker won't
    # update properly.
    @mock.patch('nova.objects.Instance.destroy')
    def test_init_deleted_instance_updates_tracker(self, noop1, noop2, noop3):
        rt = self.compute._get_resource_tracker(NODENAME)
        admin_context = context.get_admin_context()

        self.assertEqual(0, rt.compute_node.vcpus_used)
        instance = self._create_fake_instance_obj()
        instance.vcpus = 1

        self.assertEqual(0, rt.compute_node.vcpus_used)

        rt.instance_claim(admin_context, instance)
        self.compute._init_instance(admin_context, instance)
        self.assertEqual(1, rt.compute_node.vcpus_used)

        instance.vm_state = vm_states.DELETED
        self.compute._init_instance(admin_context, instance)

        self.assertEqual(0, rt.compute_node.vcpus_used)

    def test_init_instance_for_partial_deletion(self):
        admin_context = context.get_admin_context()
        instance = objects.Instance(admin_context)
        instance.id = 1
        instance.vm_state = vm_states.DELETED
        instance.deleted = False
        instance.host = self.compute.host

        def fake_partial_deletion(self, context, instance):
            instance['deleted'] = instance['id']

        self.stub_out('nova.compute.manager.ComputeManager.'
                       '_complete_partial_deletion',
                       fake_partial_deletion)
        self.compute._init_instance(admin_context, instance)

        self.assertNotEqual(0, instance['deleted'])

    @mock.patch.object(compute_manager.ComputeManager,
                       '_complete_partial_deletion')
    def test_partial_deletion_raise_exception(self, mock_complete):
        admin_context = context.get_admin_context()
        instance = objects.Instance(admin_context)
        instance.uuid = str(uuid.uuid4())
        instance.vm_state = vm_states.DELETED
        instance.deleted = False
        instance.host = self.compute.host
        mock_complete.side_effect = ValueError

        self.compute._init_instance(admin_context, instance)

        mock_complete.assert_called_once_with(admin_context, instance)

    def test_add_remove_fixed_ip_updates_instance_updated_at(self):
        def _noop(*args, **kwargs):
            pass

        self.stub_out('nova.network.api.API.'
                       'add_fixed_ip_to_instance', _noop)
        self.stub_out('nova.network.api.API.'
                       'remove_fixed_ip_from_instance', _noop)

        instance = self._create_fake_instance_obj()
        updated_at_1 = instance['updated_at']

        self.compute.add_fixed_ip_to_instance(self.context, 'fake', instance)
        updated_at_2 = db.instance_get_by_uuid(self.context,
                                               instance['uuid'])['updated_at']

        self.compute.remove_fixed_ip_from_instance(self.context, 'fake',
                                                   instance)
        updated_at_3 = db.instance_get_by_uuid(self.context,
                                               instance['uuid'])['updated_at']

        updated_ats = (updated_at_1, updated_at_2, updated_at_3)
        self.assertEqual(len(updated_ats), len(set(updated_ats)))

    def test_no_pending_deletes_for_soft_deleted_instances(self):
        self.flags(reclaim_instance_interval=0)
        ctxt = context.get_admin_context()

        instance = self._create_fake_instance_obj(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'deleted_at': timeutils.utcnow()})

        self.compute._run_pending_deletes(ctxt)
        instance = db.instance_get_by_uuid(self.context, instance['uuid'])
        self.assertFalse(instance['cleaned'])

    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(compute_manager.ComputeManager, '_delete_instance')
    def test_reclaim_queued_deletes(self, mock_delete, mock_bdms):
        self.flags(reclaim_instance_interval=3600)
        ctxt = context.get_admin_context()
        mock_bdms.return_value = []

        # Active
        self._create_fake_instance_obj(params={'host': CONF.host})

        # Deleted not old enough
        self._create_fake_instance_obj(params={'host': CONF.host,
                                           'vm_state': vm_states.SOFT_DELETED,
                                           'deleted_at': timeutils.utcnow()})

        # Deleted old enough (only this one should be reclaimed)
        deleted_at = (timeutils.utcnow() -
                      datetime.timedelta(hours=1, minutes=5))
        self._create_fake_instance_obj(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'deleted_at': deleted_at})

        # Restoring
        # NOTE(hanlind): This specifically tests for a race condition
        # where restoring a previously soft deleted instance sets
        # deleted_at back to None, causing reclaim to think it can be
        # deleted, see LP #1186243.
        self._create_fake_instance_obj(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'task_state': task_states.RESTORING})

        self.compute._reclaim_queued_deletes(ctxt)

        mock_delete.assert_called_once_with(
                ctxt, test.MatchType(objects.Instance), [],
                test.MatchType(objects.Quotas))
        mock_bdms.assert_called_once_with(ctxt, mock.ANY)

    @mock.patch.object(objects.Quotas, 'from_reservations')
    @mock.patch.object(objects.InstanceList, 'get_by_filters')
    @mock.patch.object(compute_manager.ComputeManager, '_deleted_old_enough')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(compute_manager.ComputeManager, '_delete_instance')
    def test_reclaim_queued_deletes_continue_on_error(self, mock_delete_inst,
                mock_get_uuid, mock_delete_old, mock_get_filter, mock_quota):
        # Verify that reclaim continues on error.
        self.flags(reclaim_instance_interval=3600)
        ctxt = context.get_admin_context()

        deleted_at = (timeutils.utcnow() -
                      datetime.timedelta(hours=1, minutes=5))
        instance1 = self._create_fake_instance_obj(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'deleted_at': deleted_at})
        instance2 = self._create_fake_instance_obj(
                params={'host': CONF.host,
                        'vm_state': vm_states.SOFT_DELETED,
                        'deleted_at': deleted_at})

        mock_get_filter.return_value = [instance1, instance2]
        mock_delete_old.side_effect = (True, True)
        mock_get_uuid.side_effect = ([], [])
        mock_delete_inst.side_effect = (test.TestingException, None)
        mock_quota.return_value = self.none_quotas

        self.compute._reclaim_queued_deletes(ctxt)

        mock_get_filter.assert_called_once_with(ctxt, mock.ANY,
                expected_attrs=instance_obj.INSTANCE_DEFAULT_FIELDS,
                use_slave=True)
        mock_delete_old.assert_has_calls([mock.call(instance1, 3600),
                                          mock.call(instance2, 3600)])
        mock_get_uuid.assert_has_calls([mock.call(ctxt, instance1.uuid),
                                        mock.call(ctxt, instance2.uuid)])
        mock_delete_inst.assert_has_calls([
            mock.call(ctxt, instance1, [], self.none_quotas),
            mock.call(ctxt, instance2, [], self.none_quotas)])
        mock_quota.assert_called_once_with(ctxt, None)

    @mock.patch.object(fake.FakeDriver, 'get_info')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_sync_instance_power_state')
    def test_sync_power_states(self, mock_sync, mock_get):
        ctxt = self.context.elevated()
        self._create_fake_instance_obj({'host': self.compute.host})
        self._create_fake_instance_obj({'host': self.compute.host})
        self._create_fake_instance_obj({'host': self.compute.host})

        mock_get.side_effect = [
            exception.InstanceNotFound(instance_id=uuids.instance),
            hardware.InstanceInfo(state=power_state.RUNNING),
            hardware.InstanceInfo(state=power_state.SHUTDOWN)]
        mock_sync.side_effect = \
            exception.InstanceNotFound(instance_id=uuids.instance)

        self.compute._sync_power_states(ctxt)

        mock_get.assert_has_calls([mock.call(mock.ANY), mock.call(mock.ANY),
                                   mock.call(mock.ANY)])
        mock_sync.assert_has_calls([
            mock.call(ctxt, mock.ANY, power_state.NOSTATE, use_slave=True),
            mock.call(ctxt, mock.ANY, power_state.RUNNING, use_slave=True),
            mock.call(ctxt, mock.ANY, power_state.SHUTDOWN, use_slave=True)])

    @mock.patch.object(compute_manager.ComputeManager, '_get_power_state')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_sync_instance_power_state')
    def _test_lifecycle_event(self, lifecycle_event, vm_power_state, mock_sync,
                              mock_get, is_actual_state=True):
        instance = self._create_fake_instance_obj()
        uuid = instance['uuid']

        actual_state = (vm_power_state
                        if vm_power_state is not None and is_actual_state
                        else power_state.NOSTATE)
        mock_get.return_value = actual_state

        self.compute.handle_events(event.LifecycleEvent(uuid, lifecycle_event))

        mock_get.assert_called_once_with(mock.ANY,
            test.ContainKeyValue('uuid', uuid))
        if actual_state == vm_power_state:
            mock_sync.assert_called_once_with(mock.ANY,
                test.ContainKeyValue('uuid', uuid),
                vm_power_state)

    def test_lifecycle_events(self):
        self._test_lifecycle_event(event.EVENT_LIFECYCLE_STOPPED,
                                   power_state.SHUTDOWN)
        self._test_lifecycle_event(event.EVENT_LIFECYCLE_STOPPED,
                                   power_state.SHUTDOWN,
                                   is_actual_state=False)
        self._test_lifecycle_event(event.EVENT_LIFECYCLE_STARTED,
                                   power_state.RUNNING)
        self._test_lifecycle_event(event.EVENT_LIFECYCLE_PAUSED,
                                   power_state.PAUSED)
        self._test_lifecycle_event(event.EVENT_LIFECYCLE_RESUMED,
                                   power_state.RUNNING)
        self._test_lifecycle_event(-1, None)

    def test_lifecycle_event_non_existent_instance(self):
        # No error raised for non-existent instance because of inherent race
        # between database updates and hypervisor events. See bug #1180501.
        event_instance = event.LifecycleEvent('does-not-exist',
                event.EVENT_LIFECYCLE_STOPPED)
        self.compute.handle_events(event_instance)

    @mock.patch.object(objects.Migration, 'get_by_id')
    @mock.patch.object(objects.Quotas, 'rollback')
    def test_confirm_resize_roll_back_quota_migration_not_found(self,
            mock_rollback, mock_get_by_id):
        instance = self._create_fake_instance_obj()

        migration = objects.Migration()
        migration.instance_uuid = instance.uuid
        migration.status = 'finished'
        migration.id = 0

        mock_get_by_id.side_effect = exception.MigrationNotFound(
                migration_id=0)
        self.compute.confirm_resize(self.context, instance=instance,
                                    migration=migration, reservations=[])
        self.assertTrue(mock_rollback.called)

    @mock.patch.object(instance_obj.Instance, 'get_by_uuid')
    @mock.patch.object(objects.Quotas, 'rollback')
    def test_confirm_resize_roll_back_quota_instance_not_found(self,
            mock_rollback, mock_get_by_id):
        instance = self._create_fake_instance_obj()

        migration = objects.Migration()
        migration.instance_uuid = instance.uuid
        migration.status = 'finished'
        migration.id = 0

        mock_get_by_id.side_effect = exception.InstanceNotFound(
                instance_id=instance.uuid)
        self.compute.confirm_resize(self.context, instance=instance,
                                    migration=migration, reservations=[])
        self.assertTrue(mock_rollback.called)

    @mock.patch.object(objects.Migration, 'get_by_id')
    @mock.patch.object(objects.Quotas, 'rollback')
    def test_confirm_resize_roll_back_quota_status_confirmed(self,
            mock_rollback, mock_get_by_id):
        instance = self._create_fake_instance_obj()

        migration = objects.Migration()
        migration.instance_uuid = instance.uuid
        migration.status = 'confirmed'
        migration.id = 0

        mock_get_by_id.return_value = migration
        self.compute.confirm_resize(self.context, instance=instance,
                                    migration=migration, reservations=[])
        self.assertTrue(mock_rollback.called)

    @mock.patch.object(objects.Migration, 'get_by_id')
    @mock.patch.object(objects.Quotas, 'rollback')
    def test_confirm_resize_roll_back_quota_status_dummy(self,
            mock_rollback, mock_get_by_id):
        instance = self._create_fake_instance_obj()

        migration = objects.Migration()
        migration.instance_uuid = instance.uuid
        migration.status = 'dummy'
        migration.id = 0

        mock_get_by_id.return_value = migration
        self.compute.confirm_resize(self.context, instance=instance,
                                    migration=migration, reservations=[])
        self.assertTrue(mock_rollback.called)

    def test_allow_confirm_resize_on_instance_in_deleting_task_state(self):
        instance = self._create_fake_instance_obj()
        old_type = instance.flavor
        new_type = flavors.get_flavor_by_flavor_id('4')

        instance.flavor = new_type
        instance.old_flavor = old_type
        instance.new_flavor = new_type

        fake_rt = mock.MagicMock()

        def fake_drop_move_claim(*args, **kwargs):
            pass

        def fake_get_resource_tracker(self):
            return fake_rt

        def fake_setup_networks_on_host(self, *args, **kwargs):
            pass

        with test.nested(
            mock.patch.object(fake_rt, 'drop_move_claim',
                              side_effect=fake_drop_move_claim),
            mock.patch.object(self.compute, '_get_resource_tracker',
                              side_effect=fake_get_resource_tracker),
            mock.patch.object(self.compute.network_api,
                              'setup_networks_on_host',
                              side_effect=fake_setup_networks_on_host)
        ) as (mock_drop, mock_get, mock_setup):
            migration = objects.Migration(context=self.context.elevated())
            migration.instance_uuid = instance.uuid
            migration.status = 'finished'
            migration.migration_type = 'resize'
            migration.create()

            instance.task_state = task_states.DELETING
            instance.vm_state = vm_states.RESIZED
            instance.system_metadata = {}
            instance.save()

            self.compute.confirm_resize(self.context, instance=instance,
                                        migration=migration, reservations=[])
            instance.refresh()
            self.assertEqual(vm_states.ACTIVE, instance['vm_state'])

    def _get_instance_and_bdm_for_dev_defaults_tests(self):
        instance = self._create_fake_instance_obj(
            params={'root_device_name': '/dev/vda'})
        block_device_mapping = block_device_obj.block_device_make_list(
                self.context, [fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 3,
                     'instance_uuid': uuids.block_device_instance,
                     'device_name': '/dev/vda',
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'image_id': 'fake-image-id-1',
                     'boot_index': 0})])

        return instance, block_device_mapping

    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_default_device_names_for_instance')
    def test_default_block_device_names_empty_instance_root_dev(self, mock_def,
                                                                mock_save):
        instance, bdms = self._get_instance_and_bdm_for_dev_defaults_tests()
        instance.root_device_name = None

        self.compute._default_block_device_names(instance, {}, bdms)

        self.assertEqual('/dev/vda', instance.root_device_name)
        mock_def.assert_called_once_with(instance, '/dev/vda', [], [],
                                         [bdm for bdm in bdms])

    @mock.patch.object(objects.BlockDeviceMapping, 'save')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_default_device_names_for_instance')
    def test_default_block_device_names_empty_root_device(self, mock_def,
                                                          mock_save):
        instance, bdms = self._get_instance_and_bdm_for_dev_defaults_tests()
        bdms[0]['device_name'] = None
        mock_save.return_value = None

        self.compute._default_block_device_names(instance, {}, bdms)

        mock_def.assert_called_once_with(instance, '/dev/vda', [], [],
                                         [bdm for bdm in bdms])

    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.BlockDeviceMapping, 'save')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_default_root_device_name')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_default_device_names_for_instance')
    def test_default_block_device_names_no_root_device(self, mock_default_name,
                        mock_default_dev, mock_blk_save, mock_inst_save):
        instance, bdms = self._get_instance_and_bdm_for_dev_defaults_tests()
        instance.root_device_name = None
        bdms[0]['device_name'] = None
        mock_default_dev.return_value = '/dev/vda'
        mock_blk_save.return_value = None

        self.compute._default_block_device_names(instance, {}, bdms)

        self.assertEqual('/dev/vda', instance.root_device_name)
        mock_default_dev.assert_called_once_with(instance, mock.ANY, bdms[0])
        mock_default_name.assert_called_once_with(instance, '/dev/vda', [], [],
                                                  [bdm for bdm in bdms])

    def test_default_block_device_names_with_blank_volumes(self):
        instance = self._create_fake_instance_obj()
        image_meta = {}
        root_volume = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'id': 1,
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'volume',
                'destination_type': 'volume',
                'image_id': 'fake-image-id-1',
                'boot_index': 0}))
        blank_volume1 = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'id': 2,
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'volume',
                'boot_index': -1}))
        blank_volume2 = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'id': 3,
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'volume',
                'boot_index': -1}))
        ephemeral = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'id': 4,
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'local'}))
        swap = objects.BlockDeviceMapping(
             **fake_block_device.FakeDbBlockDeviceDict({
                'id': 5,
                'instance_uuid': uuids.block_device_instance,
                'source_type': 'blank',
                'destination_type': 'local',
                'guest_format': 'swap'
                }))
        bdms = block_device_obj.block_device_make_list(
            self.context, [root_volume, blank_volume1, blank_volume2,
                           ephemeral, swap])

        with test.nested(
            mock.patch.object(self.compute, '_default_root_device_name',
                              return_value='/dev/vda'),
            mock.patch.object(objects.BlockDeviceMapping, 'save'),
            mock.patch.object(self.compute,
                              '_default_device_names_for_instance')
        ) as (default_root_device, object_save,
              default_device_names):
            self.compute._default_block_device_names(instance,
                                                     image_meta, bdms)
            default_root_device.assert_called_once_with(instance, image_meta,
                                                        bdms[0])
            self.assertEqual('/dev/vda', instance.root_device_name)
            self.assertTrue(object_save.called)
            default_device_names.assert_called_once_with(instance,
                '/dev/vda', [bdms[-2]], [bdms[-1]],
                [bdm for bdm in bdms[:-2]])

    def test_reserve_block_device_name(self):
        instance = self._create_fake_instance_obj(
                params={'root_device_name': '/dev/vda'})
        bdm = objects.BlockDeviceMapping(
                **{'context': self.context, 'source_type': 'image',
                   'destination_type': 'local',
                   'image_id': uuids.image_instance,
                   'device_name': '/dev/vda',
                   'instance_uuid': instance.uuid})
        bdm.create()

        self.compute.reserve_block_device_name(self.context, instance,
                                               '/dev/vdb',
                                                uuids.block_device_instance,
                                               'virtio', 'disk')

        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                self.context, instance.uuid)
        bdms = list(bdms)
        self.assertEqual(len(bdms), 2)
        bdms.sort(key=operator.attrgetter('device_name'))
        vol_bdm = bdms[1]
        self.assertEqual(vol_bdm.source_type, 'volume')
        self.assertIsNone(vol_bdm.boot_index)
        self.assertIsNone(vol_bdm.guest_format)
        self.assertEqual(vol_bdm.destination_type, 'volume')
        self.assertEqual(vol_bdm.device_name, '/dev/vdb')
        self.assertEqual(vol_bdm.volume_id, uuids.block_device_instance)
        self.assertEqual(vol_bdm.disk_bus, 'virtio')
        self.assertEqual(vol_bdm.device_type, 'disk')

    def test_reserve_block_device_name_with_iso_instance(self):
        instance = self._create_fake_instance_obj(
                params={'root_device_name': '/dev/hda'})
        bdm = objects.BlockDeviceMapping(
                context=self.context,
                **{'source_type': 'image', 'destination_type': 'local',
                   'image_id': 'fake-image-id', 'device_name': '/dev/hda',
                   'instance_uuid': instance.uuid})
        bdm.create()

        self.compute.reserve_block_device_name(self.context, instance,
                                               '/dev/vdb',
                                                uuids.block_device_instance,
                                               'ide', 'disk')

        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                self.context, instance.uuid)
        bdms = list(bdms)
        self.assertEqual(2, len(bdms))
        bdms.sort(key=operator.attrgetter('device_name'))
        vol_bdm = bdms[1]
        self.assertEqual('volume', vol_bdm.source_type)
        self.assertEqual('volume', vol_bdm.destination_type)
        self.assertEqual('/dev/hdb', vol_bdm.device_name)
        self.assertEqual(uuids.block_device_instance, vol_bdm.volume_id)
        self.assertEqual('ide', vol_bdm.disk_bus)
        self.assertEqual('disk', vol_bdm.device_type)

    @mock.patch.object(cinder.API, 'get_snapshot')
    def test_quiesce(self, mock_snapshot_get):
        # ensure instance can be quiesced and unquiesced
        instance = self._create_fake_instance_obj()
        mapping = [{'source_type': 'snapshot', 'snapshot_id': 'fake-id1'},
                   {'source_type': 'snapshot', 'snapshot_id': 'fake-id2'}]
        # unquiesce should wait until volume snapshots are completed
        mock_snapshot_get.side_effect = [{'status': 'creating'},
                                         {'status': 'available'}] * 2
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        self.compute.quiesce_instance(self.context, instance)
        self.compute.unquiesce_instance(self.context, instance, mapping)
        self.compute.terminate_instance(self.context, instance, [], [])
        mock_snapshot_get.assert_any_call(mock.ANY, 'fake-id1')
        mock_snapshot_get.assert_any_call(mock.ANY, 'fake-id2')
        self.assertEqual(4, mock_snapshot_get.call_count)

    def test_instance_fault_message_no_rescheduled_details_without_retry(self):
        """This test simulates a spawn failure with no retry data.

        If driver spawn raises an exception and there is no retry data
        available, the instance fault message should not contain any details
        about rescheduling. The fault message field is limited in size and a
        long message about rescheduling displaces the original error message.
        """
        class TestException(Exception):
            pass

        instance = self._create_fake_instance_obj()

        with mock.patch.object(self.compute.driver, 'spawn') as mock_spawn:
            mock_spawn.side_effect = TestException('Preserve this')
            self.compute.build_and_run_instance(
                    self.context, instance, {}, {}, {},
                    block_device_mapping=[])
        self.assertEqual('Preserve this', instance.fault.message)


class ComputeAPITestCase(BaseTestCase):
    def setUp(self):
        def fake_get_nw_info(cls, ctxt, instance):
            self.assertTrue(ctxt.is_admin)
            return fake_network.fake_get_instance_nw_info(self, 1, 1)

        super(ComputeAPITestCase, self).setUp()
        self.useFixture(fixtures.SpawnIsSynchronousFixture())
        self.stub_out('nova.network.api.API.get_instance_nw_info',
                       fake_get_nw_info)
        self.security_group_api = (
            openstack_driver.get_openstack_security_group_driver())

        self.compute_api = compute.API(
                                   security_group_api=self.security_group_api)
        self.fake_image = {
            'id': 'f9000000-0000-0000-0000-000000000000',
            'name': 'fake_name',
            'status': 'active',
            'properties': {'kernel_id': uuids.kernel_id,
                           'ramdisk_id': uuids.ramdisk_id},
        }

        def fake_show(obj, context, image_id, **kwargs):
            if image_id:
                return self.fake_image
            else:
                raise exception.ImageNotFound(image_id=image_id)

        self.fake_show = fake_show

        def fake_lookup(self, context, instance):
            return instance

        self.stub_out('nova.compute.api.API._lookup_instance', fake_lookup)

        # Mock out build_instances and rebuild_instance since nothing in these
        # tests should need those to actually run. We do this to avoid
        # possible races with other tests that actually test those methods
        # and mock things out within them, like conductor tests.
        self.build_instances_mock = mock.Mock(autospec=True)
        self.compute_api.compute_task_api.build_instances = \
            self.build_instances_mock

        self.rebuild_instance_mock = mock.Mock(autospec=True)
        self.compute_api.compute_task_api.rebuild_instance = \
            self.rebuild_instance_mock

    def _run_instance(self, params=None):
        instance = self._create_fake_instance_obj(params, services=True)
        instance_uuid = instance['uuid']
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance.refresh()
        self.assertIsNone(instance['task_state'])
        return instance, instance_uuid

    def test_create_with_too_little_ram(self):
        # Test an instance type with too little memory.

        inst_type = flavors.get_default_flavor()
        inst_type['memory_mb'] = 1

        self.fake_image['min_ram'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorMemoryTooSmall,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Now increase the inst_type memory and make sure all is fine.
        inst_type['memory_mb'] = 2
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])

    def test_create_with_too_little_disk(self):
        # Test an instance type with too little disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1

        self.fake_image['min_disk'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorDiskSmallerThanMinDisk,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Now increase the inst_type disk space and make sure all is fine.
        inst_type['root_gb'] = 2
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])

    def test_create_with_too_large_image(self):
        # Test an instance type with too little disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1

        self.fake_image['size'] = '1073741825'

        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorDiskSmallerThanImage,
            self.compute_api.create, self.context,
            inst_type, self.fake_image['id'])

        # Reduce image to 1 GB limit and ensure it works
        self.fake_image['size'] = '1073741824'
        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])

    def test_create_just_enough_ram_and_disk(self):
        # Test an instance type with just enough ram and disk space.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 2
        inst_type['memory_mb'] = 2

        self.fake_image['min_ram'] = 2
        self.fake_image['min_disk'] = 2
        self.fake_image['name'] = 'fake_name'
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])

    def test_create_with_no_ram_and_disk_reqs(self):
        # Test an instance type with no min_ram or min_disk.

        inst_type = flavors.get_default_flavor()
        inst_type['root_gb'] = 1
        inst_type['memory_mb'] = 1

        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        (refs, resv_id) = self.compute_api.create(self.context,
                inst_type, self.fake_image['id'])

    def test_create_bdm_from_flavor(self):
        instance_type_params = {
            'flavorid': 'test', 'name': 'test',
            'swap': 1024, 'ephemeral_gb': 1, 'root_gb': 1,
        }
        self._create_instance_type(params=instance_type_params)
        inst_type = flavors.get_flavor_by_name('test')
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)
        (refs, resv_id) = self.compute_api.create(self.context, inst_type,
                                                  self.fake_image['id'])

        instance_uuid = refs[0]['uuid']
        bdms = block_device_obj.BlockDeviceMappingList.get_by_instance_uuid(
            self.context, instance_uuid)

        ephemeral = list(filter(block_device.new_format_is_ephemeral, bdms))
        self.assertEqual(1, len(ephemeral))
        swap = list(filter(block_device.new_format_is_swap, bdms))
        self.assertEqual(1, len(swap))

        self.assertEqual(1024, swap[0].volume_size)
        self.assertEqual(1, ephemeral[0].volume_size)

    def test_create_with_deleted_image(self):
        # If we're given a deleted image by glance, we should not be able to
        # build from it
        inst_type = flavors.get_default_flavor()

        self.fake_image['name'] = 'fake_name'
        self.fake_image['status'] = 'DELETED'
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        expected_message = (
            exception.ImageNotActive.msg_fmt % {'image_id':
            self.fake_image['id']})
        with testtools.ExpectedException(exception.ImageNotActive,
                                         expected_message):
            self.compute_api.create(self.context, inst_type,
                                    self.fake_image['id'])

    @mock.patch('nova.virt.hardware.numa_get_constraints')
    def test_create_with_numa_topology(self, numa_constraints_mock):
        inst_type = flavors.get_default_flavor()

        numa_topology = objects.InstanceNUMATopology(
            cells=[objects.InstanceNUMACell(
                id=0, cpuset=set([1, 2]), memory=512),
                   objects.InstanceNUMACell(
                id=1, cpuset=set([3, 4]), memory=512)])
        numa_constraints_mock.return_value = numa_topology

        instances, resv_id = self.compute_api.create(self.context, inst_type,
                                                     self.fake_image['id'])

        numa_constraints_mock.assert_called_once_with(
            inst_type, test.MatchType(objects.ImageMeta))
        self.assertEqual(
            numa_topology.cells[0].obj_to_primitive(),
            instances[0].numa_topology.cells[0].obj_to_primitive())
        self.assertEqual(
            numa_topology.cells[1].obj_to_primitive(),
            instances[0].numa_topology.cells[1].obj_to_primitive())

    def test_create_instance_defaults_display_name(self):
        # Verify that an instance cannot be created without a display_name.
        cases = [dict(), dict(display_name=None)]
        for instance in cases:
            (ref, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                'f5000000-0000-0000-0000-000000000000', **instance)
            self.assertIsNotNone(ref[0]['display_name'])

    def test_create_instance_sets_system_metadata(self):
        # Make sure image properties are copied into system metadata.
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href='f5000000-0000-0000-0000-000000000000')

        sys_metadata = db.instance_system_metadata_get(self.context,
                ref[0]['uuid'])

        image_props = {'image_kernel_id': uuids.kernel_id,
                 'image_ramdisk_id': uuids.ramdisk_id,
                 'image_something_else': 'meow', }
        for key, value in six.iteritems(image_props):
            self.assertIn(key, sys_metadata)
            self.assertEqual(value, sys_metadata[key])

    def test_create_saves_flavor(self):
        instance_type = flavors.get_default_flavor()
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=instance_type,
                image_href=uuids.image_href_id)

        instance = objects.Instance.get_by_uuid(self.context, ref[0]['uuid'])
        self.assertEqual(instance_type.flavorid, instance.flavor.flavorid)
        self.assertNotIn('instance_type_id', instance.system_metadata)

    def test_create_instance_associates_security_groups(self):
        # Make sure create associates security groups.
        group = self._create_group()
        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                security_group=['testgroup'])

        groups_for_instance = db.security_group_get_by_instance(
                         self.context, ref[0]['uuid'])
        self.assertEqual(1, len(groups_for_instance))
        self.assertEqual(group.id, groups_for_instance[0].id)
        group_with_instances = db.security_group_get(self.context,
                                      group.id,
                                      columns_to_join=['instances'])
        self.assertEqual(1, len(group_with_instances.instances))

    def test_create_instance_with_invalid_security_group_raises(self):
        instance_type = flavors.get_default_flavor()

        pre_build_len = len(db.instance_get_all(self.context))
        self.assertRaises(exception.SecurityGroupNotFoundForProject,
                          self.compute_api.create,
                          self.context,
                          instance_type=instance_type,
                          image_href=None,
                          security_group=['this_is_a_fake_sec_group'])
        self.assertEqual(pre_build_len,
                         len(db.instance_get_all(self.context)))

    def test_create_with_large_user_data(self):
        # Test an instance type with too much user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.InstanceUserDataTooLarge,
            self.compute_api.create, self.context, inst_type,
            self.fake_image['id'], user_data=(b'1' * 65536))

    def test_create_with_malformed_user_data(self):
        # Test an instance type with malformed user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.InstanceUserDataMalformed,
            self.compute_api.create, self.context, inst_type,
            self.fake_image['id'], user_data=b'banana')

    def test_create_with_base64_user_data(self):
        # Test an instance type with ok much user data.

        inst_type = flavors.get_default_flavor()

        self.fake_image['min_ram'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        # NOTE(mikal): a string of length 48510 encodes to 65532 characters of
        # base64
        (refs, resv_id) = self.compute_api.create(
            self.context, inst_type, self.fake_image['id'],
            user_data=base64.encodestring(b'1' * 48510))

    def test_populate_instance_for_create(self, num_instances=1):
        base_options = {'image_ref': self.fake_image['id'],
                        'system_metadata': {'fake': 'value'},
                        'display_name': 'foo',
                        'uuid': uuids.instance}
        instance = objects.Instance()
        instance.update(base_options)
        inst_type = flavors.get_flavor_by_name("m1.tiny")
        instance = self.compute_api._populate_instance_for_create(
                                self.context,
                                instance,
                                self.fake_image,
                                1,
                                security_groups=objects.SecurityGroupList(),
                                instance_type=inst_type,
                                num_instances=num_instances,
                                shutdown_terminate=False)
        self.assertEqual(str(base_options['image_ref']),
                         instance['system_metadata']['image_base_image_ref'])
        self.assertEqual(vm_states.BUILDING, instance['vm_state'])
        self.assertEqual(task_states.SCHEDULING, instance['task_state'])
        self.assertEqual(1, instance['launch_index'])
        self.assertEqual(base_options['display_name'],
                         instance['display_name'])
        self.assertIsNotNone(instance.get('uuid'))
        self.assertEqual([], instance.security_groups.objects)

    def test_default_hostname_generator(self):
        fake_uuids = [str(uuid.uuid4()) for x in range(4)]

        orig_populate = self.compute_api._populate_instance_for_create

        def _fake_populate(self, context, base_options, *args, **kwargs):
            base_options['uuid'] = fake_uuids.pop(0)
            return orig_populate(context, base_options, *args, **kwargs)

        self.stub_out('nova.compute.api.API.'
                      '_populate_instance_for_create', _fake_populate)

        cases = [(None, 'server-%s' % fake_uuids[0]),
                 ('Hello, Server!', 'hello-server'),
                 ('<}\x1fh\x10e\x08l\x02l\x05o\x12!{>', 'hello'),
                 ('hello_server', 'hello-server')]
        for display_name, hostname in cases:
            (ref, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                display_name=display_name)

            self.assertEqual(ref[0]['hostname'], hostname)

    def test_instance_create_adds_to_instance_group(self):
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        group = objects.InstanceGroup(self.context)
        group.uuid = str(uuid.uuid4())
        group.project_id = self.context.project_id
        group.user_id = self.context.user_id
        group.create()

        inst_type = flavors.get_default_flavor()
        (refs, resv_id) = self.compute_api.create(
            self.context, inst_type, self.fake_image['id'],
            scheduler_hints={'group': group.uuid})

        group = objects.InstanceGroup.get_by_uuid(self.context, group.uuid)
        self.assertIn(refs[0]['uuid'], group.members)

    def test_instance_create_with_group_uuid_fails_group_not_exist(self):
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        inst_type = flavors.get_default_flavor()
        self.assertRaises(
                exception.InstanceGroupNotFound,
                self.compute_api.create,
                self.context,
                inst_type,
                self.fake_image['id'],
                scheduler_hints={'group':
                                     '5b674f73-c8cf-40ef-9965-3b6fe4b304b1'})

    def test_destroy_instance_disassociates_security_groups(self):
        # Make sure destroying disassociates security groups.
        group = self._create_group()

        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                security_group=['testgroup'])

        db.instance_destroy(self.context, ref[0]['uuid'])
        group = db.security_group_get(self.context, group['id'],
                                      columns_to_join=['instances'])
        self.assertEqual(0, len(group['instances']))

    def test_destroy_security_group_disassociates_instances(self):
        # Make sure destroying security groups disassociates instances.
        group = self._create_group()

        (ref, resv_id) = self.compute_api.create(
                self.context,
                instance_type=flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                security_group=['testgroup'])

        db.security_group_destroy(self.context, group['id'])
        admin_deleted_context = context.get_admin_context(
                read_deleted="only")
        group = db.security_group_get(admin_deleted_context, group['id'],
                                      columns_to_join=['instances'])
        self.assertEqual(0, len(group['instances']))

    def _test_rebuild(self, vm_state):
        instance = self._create_fake_instance_obj()
        instance_uuid = instance['uuid']
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        instance = objects.Instance.get_by_uuid(self.context,
                                                instance_uuid)
        self.assertIsNone(instance.task_state)
        # Set some image metadata that should get wiped out and reset
        # as well as some other metadata that should be preserved.
        instance.system_metadata.update({
                'image_kernel_id': 'old-data',
                'image_ramdisk_id': 'old_data',
                'image_something_else': 'old-data',
                'image_should_remove': 'bye-bye',
                'preserved': 'preserve this!'})

        instance.save()

        # Make sure Compute API updates the image_ref before casting to
        # compute manager.
        info = {'image_ref': None, 'clean': False}

        def fake_rpc_rebuild(context, **kwargs):
            info['image_ref'] = kwargs['instance'].image_ref
            info['clean'] = ('progress' not in
                             kwargs['instance'].obj_what_changed())

        with mock.patch.object(self.compute_api.compute_task_api,
                               'rebuild_instance', fake_rpc_rebuild):
            image_ref = instance["image_ref"] + '-new_image_ref'
            password = "new_password"

            instance.vm_state = vm_state
            instance.save()

            self.compute_api.rebuild(self.context, instance,
                                     image_ref, password)
            self.assertEqual(info['image_ref'], image_ref)
            self.assertTrue(info['clean'])

            instance.refresh()
            self.assertEqual(instance.task_state, task_states.REBUILDING)
            sys_meta = {k: v for k, v in instance.system_metadata.items()
                        if not k.startswith('instance_type')}
            self.assertEqual(sys_meta,
                    {'image_kernel_id': uuids.kernel_id,
                    'image_min_disk': '1',
                    'image_ramdisk_id': uuids.ramdisk_id,
                    'image_something_else': 'meow',
                    'preserved': 'preserve this!'})

    def test_rebuild(self):
        self._test_rebuild(vm_state=vm_states.ACTIVE)

    def test_rebuild_in_error_state(self):
        self._test_rebuild(vm_state=vm_states.ERROR)

    def test_rebuild_in_error_not_launched(self):
        instance = self._create_fake_instance_obj(params={'image_ref': ''})
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        db.instance_update(self.context, instance['uuid'],
                           {"vm_state": vm_states.ERROR,
                            "launched_at": None})

        instance = db.instance_get_by_uuid(self.context, instance['uuid'])

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.rebuild,
                          self.context,
                          instance,
                          instance['image_ref'],
                          "new password")

    def test_rebuild_no_image(self):
        instance = self._create_fake_instance_obj(params={'image_ref': ''})
        instance_uuid = instance.uuid
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)
        self.compute_api.rebuild(self.context, instance, '', 'new_password')

        instance = db.instance_get_by_uuid(self.context, instance_uuid)
        self.assertEqual(instance['task_state'], task_states.REBUILDING)

    def test_rebuild_with_deleted_image(self):
        # If we're given a deleted image by glance, we should not be able to
        # rebuild from it
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})
        self.fake_image['name'] = 'fake_name'
        self.fake_image['status'] = 'DELETED'
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        expected_message = (
            exception.ImageNotActive.msg_fmt % {'image_id':
            self.fake_image['id']})
        with testtools.ExpectedException(exception.ImageNotActive,
                                         expected_message):
            self.compute_api.rebuild(self.context, instance,
                                     self.fake_image['id'], 'new_password')

    def test_rebuild_with_too_little_ram(self):
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})
        instance.flavor.memory_mb = 64
        instance.flavor.root_gb = 1

        self.fake_image['min_ram'] = 128
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorMemoryTooSmall,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image memory requirements and make sure it works
        self.fake_image['min_ram'] = 64

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')

    def test_rebuild_with_too_little_disk(self):
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})

        def fake_extract_flavor(_inst, prefix=''):
            if prefix == '':
                f = objects.Flavor(**test_flavor.fake_flavor)
                f.memory_mb = 64
                f.root_gb = 1
                return f
            else:
                raise KeyError()

        self.stub_out('nova.compute.flavors.extract_flavor',
                       fake_extract_flavor)

        self.fake_image['min_disk'] = 2
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorDiskSmallerThanMinDisk,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image disk requirements and make sure it works
        self.fake_image['min_disk'] = 1

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')

    def test_rebuild_with_just_enough_ram_and_disk(self):
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})

        def fake_extract_flavor(_inst, prefix=''):
            if prefix == '':
                f = objects.Flavor(**test_flavor.fake_flavor)
                f.memory_mb = 64
                f.root_gb = 1
                return f
            else:
                raise KeyError()

        self.stub_out('nova.compute.flavors.extract_flavor',
                       fake_extract_flavor)

        self.fake_image['min_ram'] = 64
        self.fake_image['min_disk'] = 1
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')

    def test_rebuild_with_no_ram_and_disk_reqs(self):
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})

        def fake_extract_flavor(_inst, prefix=''):
            if prefix == '':
                f = objects.Flavor(**test_flavor.fake_flavor)
                f.memory_mb = 64
                f.root_gb = 1
                return f
            else:
                raise KeyError()

        self.stub_out('nova.compute.flavors.extract_flavor',
                       fake_extract_flavor)
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')

    def test_rebuild_with_too_large_image(self):
        instance = self._create_fake_instance_obj(
            params={'image_ref': FAKE_IMAGE_REF})

        def fake_extract_flavor(_inst, prefix=''):
            if prefix == '':
                f = objects.Flavor(**test_flavor.fake_flavor)
                f.memory_mb = 64
                f.root_gb = 1
                return f
            else:
                raise KeyError()

        self.stub_out('nova.compute.flavors.extract_flavor',
                       fake_extract_flavor)

        self.fake_image['size'] = '1073741825'
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      self.fake_show)

        self.assertRaises(exception.FlavorDiskSmallerThanImage,
            self.compute_api.rebuild, self.context,
            instance, self.fake_image['id'], 'new_password')

        # Reduce image to 1 GB limit and ensure it works
        self.fake_image['size'] = '1073741824'
        self.compute_api.rebuild(self.context,
                instance, self.fake_image['id'], 'new_password')

    def test_hostname_create(self):
        # Ensure instance hostname is set during creation.
        inst_type = flavors.get_flavor_by_name('m1.tiny')
        (instances, _) = self.compute_api.create(self.context,
                           inst_type,
                           image_href=uuids.image_href_id,
                           display_name='test host')

        self.assertEqual('test-host', instances[0]['hostname'])

    def _fake_rescue_block_devices(self, instance, status="in-use"):
        fake_bdms = block_device_obj.block_device_make_list(self.context,
                    [fake_block_device.FakeDbBlockDeviceDict(
                     {'device_name': '/dev/vda',
                     'source_type': 'volume',
                     'boot_index': 0,
                     'destination_type': 'volume',
                     'volume_id': 'bf0b6b00-a20c-11e2-9e96-0800200c9a66'})])

        volume = {'id': 'bf0b6b00-a20c-11e2-9e96-0800200c9a66',
                  'state': 'active', 'instance_uuid': instance['uuid']}

        return fake_bdms, volume

    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(cinder.API, 'get')
    def test_rescue_volume_backed_no_image(self, mock_get_vol, mock_get_bdms):
        # Instance started without an image
        params = {'image_ref': ''}
        volume_backed_inst_1 = self._create_fake_instance_obj(params=params)
        bdms, volume = self._fake_rescue_block_devices(volume_backed_inst_1)

        mock_get_vol.return_value = {'id': volume['id'], 'status': "in-use"}
        mock_get_bdms.return_value = bdms

        with mock.patch.object(self.compute, '_prep_block_device'):
            self.compute.build_and_run_instance(self.context,
                                        volume_backed_inst_1, {}, {}, {},
                                        block_device_mapping=[])

        self.assertRaises(exception.InstanceNotRescuable,
                          self.compute_api.rescue, self.context,
                          volume_backed_inst_1)

    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(cinder.API, 'get')
    def test_rescue_volume_backed_placeholder_image(self,
                                                    mock_get_vol,
                                                    mock_get_bdms):
        # Instance started with a placeholder image (for metadata)
        volume_backed_inst_2 = self._create_fake_instance_obj(
                {'image_ref': FAKE_IMAGE_REF,
                 'root_device_name': '/dev/vda'})
        bdms, volume = self._fake_rescue_block_devices(volume_backed_inst_2)

        mock_get_vol.return_value = {'id': volume['id'], 'status': "in-use"}
        mock_get_bdms.return_value = bdms

        with mock.patch.object(self.compute, '_prep_block_device'):
            self.compute.build_and_run_instance(self.context,
                                        volume_backed_inst_2, {}, {}, {},
                                        block_device_mapping=[])

        self.assertRaises(exception.InstanceNotRescuable,
                          self.compute_api.rescue, self.context,
                          volume_backed_inst_2)

    def test_get(self):
        # Test get instance.
        exp_instance = self._create_fake_instance_obj()
        instance = self.compute_api.get(self.context, exp_instance.uuid)
        self.assertEqual(exp_instance.id, instance.id)

    def test_get_with_admin_context(self):
        # Test get instance.
        c = context.get_admin_context()
        exp_instance = self._create_fake_instance_obj()
        instance = self.compute_api.get(c, exp_instance['uuid'])
        self.assertEqual(exp_instance.id, instance.id)

    def test_get_all_by_name_regexp(self):
        # Test searching instances by name (display_name).
        c = context.get_admin_context()
        instance1 = self._create_fake_instance_obj({'display_name': 'woot'})
        instance2 = self._create_fake_instance_obj({
                'display_name': 'woo'})
        instance3 = self._create_fake_instance_obj({
                'display_name': 'not-woot'})

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^woo.*'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance1['uuid'], instance_uuids)
        self.assertIn(instance2['uuid'], instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^woot.*'})
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertEqual(len(instances), 1)
        self.assertIn(instance1['uuid'], instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '.*oot.*'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance1['uuid'], instance_uuids)
        self.assertIn(instance3['uuid'], instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': '^n.*'})
        self.assertEqual(len(instances), 1)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance3['uuid'], instance_uuids)

        instances = self.compute_api.get_all(c,
                search_opts={'name': 'noth.*'})
        self.assertEqual(len(instances), 0)

    def test_get_all_by_multiple_options_at_once(self):
        # Test searching by multiple options at once.
        c = context.get_admin_context()

        def fake_network_info(ip):
            info = [{
                'address': 'aa:bb:cc:dd:ee:ff',
                'id': 1,
                'network': {
                    'bridge': 'br0',
                    'id': 1,
                    'label': 'private',
                    'subnets': [{
                        'cidr': '192.168.0.0/24',
                        'ips': [{
                            'address': ip,
                            'type': 'fixed',
                        }]
                    }]
                }
            }]
            return jsonutils.dumps(info)

        instance1 = self._create_fake_instance_obj({
                'display_name': 'woot',
                'uuid': '00000000-0000-0000-0000-000000000010',
                'info_cache': objects.InstanceInfoCache(
                    network_info=fake_network_info('192.168.0.1'))})
        self._create_fake_instance_obj({  # instance2
                'display_name': 'woo',
                'uuid': '00000000-0000-0000-0000-000000000020',
                'info_cache': objects.InstanceInfoCache(
                    network_info=fake_network_info('192.168.0.2'))})
        instance3 = self._create_fake_instance_obj({
                'display_name': 'not-woot',
                'uuid': '00000000-0000-0000-0000-000000000030',
                'info_cache': objects.InstanceInfoCache(
                    network_info=fake_network_info('192.168.0.3'))})

        # ip ends up matching 2nd octet here.. so all 3 match ip
        # but 'name' only matches one
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1', 'name': 'not.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance3['uuid'])

        # ip ends up matching any ip with a '1' in the last octet..
        # so instance 1 and 3.. but name should only match #1
        # but 'name' only matches one
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1$', 'name': '^woo.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        # same as above but no match on name (name matches instance1
        # but the ip query doesn't
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.2$', 'name': '^woot.*'})
        self.assertEqual(len(instances), 0)

        # ip matches all 3... ipv6 matches #2+#3...name matches #3
        instances = self.compute_api.get_all(c,
                search_opts={'ip': '.*\.1',
                             'name': 'not.*',
                             'ip6': '^.*12.*34.*'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance3['uuid'])

    def test_get_all_by_image(self):
        # Test searching instances by image.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance_obj(
            {'image_ref': uuids.fake_image_ref_1})
        instance2 = self._create_fake_instance_obj(
            {'image_ref': uuids.fake_image_ref_2})
        instance3 = self._create_fake_instance_obj(
            {'image_ref': uuids.fake_image_ref_2})

        instances = self.compute_api.get_all(c, search_opts={'image': '123'})
        self.assertEqual(len(instances), 0)

        instances = self.compute_api.get_all(
            c, search_opts={'image': uuids.fake_image_ref_1})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        instances = self.compute_api.get_all(
            c, search_opts={'image': uuids.fake_image_ref_2})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance2['uuid'], instance_uuids)
        self.assertIn(instance3['uuid'], instance_uuids)

        # Test passing a list as search arg
        instances = self.compute_api.get_all(
            c, search_opts={'image': [uuids.fake_image_ref_1,
                                     uuids.fake_image_ref_2]})
        self.assertEqual(len(instances), 3)

    def test_get_all_by_flavor(self):
        # Test searching instances by image.
        c = context.get_admin_context()
        flavor_dict = {f.flavorid: f for f in objects.FlavorList.get_all(c)}
        instance1 = self._create_fake_instance_obj(
            {'instance_type_id': flavor_dict['1'].id})
        instance2 = self._create_fake_instance_obj(
            {'instance_type_id': flavor_dict['2'].id})
        instance3 = self._create_fake_instance_obj(
            {'instance_type_id': flavor_dict['2'].id})

        instances = self.compute_api.get_all(c,
                search_opts={'flavor': 5})
        self.assertEqual(len(instances), 0)

        # ensure unknown filter maps to an exception
        self.assertRaises(exception.FlavorNotFound,
                          self.compute_api.get_all, c,
                          search_opts={'flavor': 99})

        instances = self.compute_api.get_all(c, search_opts={'flavor': 1})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['id'], instance1['id'])

        instances = self.compute_api.get_all(c, search_opts={'flavor': 2})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance2['uuid'], instance_uuids)
        self.assertIn(instance3['uuid'], instance_uuids)

    def test_get_all_by_state(self):
        # Test searching instances by state.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance_obj({
            'power_state': power_state.SHUTDOWN,
        })
        instance2 = self._create_fake_instance_obj({
            'power_state': power_state.RUNNING,
        })
        instance3 = self._create_fake_instance_obj({
            'power_state': power_state.RUNNING,
        })

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.SUSPENDED})
        self.assertEqual(len(instances), 0)

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.SHUTDOWN})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

        instances = self.compute_api.get_all(c,
                search_opts={'power_state': power_state.RUNNING})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance2['uuid'], instance_uuids)
        self.assertIn(instance3['uuid'], instance_uuids)

        # Test passing a list as search arg
        instances = self.compute_api.get_all(c,
                search_opts={'power_state': [power_state.SHUTDOWN,
                        power_state.RUNNING]})
        self.assertEqual(len(instances), 3)

    def test_get_all_by_metadata(self):
        # Test searching instances by metadata.

        c = context.get_admin_context()
        self._create_fake_instance_obj()  # instance0
        self._create_fake_instance_obj({  # instance1
                'metadata': {'key1': 'value1'}})
        instance2 = self._create_fake_instance_obj({
                'metadata': {'key2': 'value2'}})
        instance3 = self._create_fake_instance_obj({
                'metadata': {'key3': 'value3'}})
        instance4 = self._create_fake_instance_obj({
                'metadata': {'key3': 'value3',
                             'key4': 'value4'}})

        # get all instances
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': u"{}"})
        self.assertEqual(len(instances), 5)

        # wrong key/value combination
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': u'{"key1": "value3"}'})
        self.assertEqual(len(instances), 0)

        # non-existing keys
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': u'{"key5": "value1"}'})
        self.assertEqual(len(instances), 0)

        # find existing instance
        instances = self.compute_api.get_all(c,
                search_opts={'metadata': u'{"key2": "value2"}'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance2['uuid'])

        instances = self.compute_api.get_all(c,
                search_opts={'metadata': u'{"key3": "value3"}'})
        self.assertEqual(len(instances), 2)
        instance_uuids = [instance['uuid'] for instance in instances]
        self.assertIn(instance3['uuid'], instance_uuids)
        self.assertIn(instance4['uuid'], instance_uuids)

        # multiple criteria as a dict
        instances = self.compute_api.get_all(c,
            search_opts={'metadata': u'{"key3": "value3","key4": "value4"}'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance4['uuid'])

        # multiple criteria as a list
        instances = self.compute_api.get_all(c,
            search_opts=
                {'metadata': u'[{"key4": "value4"},{"key3": "value3"}]'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance4['uuid'])

    def test_get_all_by_system_metadata(self):
        # Test searching instances by system metadata.

        c = context.get_admin_context()
        instance1 = self._create_fake_instance_obj({
                'system_metadata': {'key1': 'value1'}})

        # find existing instance
        instances = self.compute_api.get_all(c,
                search_opts={'system_metadata': u'{"key1": "value1"}'})
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]['uuid'], instance1['uuid'])

    def test_all_instance_metadata(self):
        self._create_fake_instance_obj({'metadata': {'key1': 'value1'},
                                                'user_id': 'user1',
                                                'project_id': 'project1'})

        self._create_fake_instance_obj({'metadata': {'key2': 'value2'},
                                                'user_id': 'user2',
                                                'project_id': 'project2'})

        _context = self.context
        _context.user_id = 'user1'
        _context.project_id = 'project1'
        metadata = self.compute_api.get_all_instance_metadata(_context,
                                                              search_filts=[])
        self.assertEqual(1, len(metadata))
        self.assertEqual(metadata[0]['key'], 'key1')

        _context.user_id = 'user2'
        _context.project_id = 'project2'
        metadata = self.compute_api.get_all_instance_metadata(_context,
                                                              search_filts=[])
        self.assertEqual(1, len(metadata))
        self.assertEqual(metadata[0]['key'], 'key2')

        _context = context.get_admin_context()
        metadata = self.compute_api.get_all_instance_metadata(_context,
                                                              search_filts=[])
        self.assertEqual(2, len(metadata))

    def test_instance_metadata(self):
        meta_changes = [None]
        self.flags(notify_on_state_change='vm_state')

        def fake_change_instance_metadata(inst, ctxt, diff, instance=None,
                                          instance_uuid=None):
            meta_changes[0] = diff
        self.stub_out('nova.compute.rpcapi.ComputeAPI.'
                      'change_instance_metadata',
                      fake_change_instance_metadata)

        _context = context.get_admin_context()
        instance = self._create_fake_instance_obj({'metadata':
                                                       {'key1': 'value1'}})

        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key1': 'value1'})

        self.compute_api.update_instance_metadata(_context, instance,
                                                  {'key2': 'value2'})
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key1': 'value1', 'key2': 'value2'})
        self.assertEqual(meta_changes, [{'key2': ['+', 'value2']}])

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 1)
        msg = fake_notifier.NOTIFICATIONS[0]
        payload = msg.payload
        self.assertIn('metadata', payload)
        self.assertEqual(payload['metadata'], metadata)

        new_metadata = {'key2': 'bah', 'key3': 'value3'}
        self.compute_api.update_instance_metadata(_context, instance,
                                                  new_metadata, delete=True)
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, new_metadata)
        self.assertEqual(meta_changes, [{
                    'key1': ['-'],
                    'key2': ['+', 'bah'],
                    'key3': ['+', 'value3'],
                    }])

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[1]
        payload = msg.payload
        self.assertIn('metadata', payload)
        self.assertEqual(payload['metadata'], metadata)

        self.compute_api.delete_instance_metadata(_context, instance, 'key2')
        metadata = self.compute_api.get_instance_metadata(_context, instance)
        self.assertEqual(metadata, {'key3': 'value3'})
        self.assertEqual(meta_changes, [{'key2': ['-']}])

        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 3)
        msg = fake_notifier.NOTIFICATIONS[2]
        payload = msg.payload
        self.assertIn('metadata', payload)
        self.assertEqual(payload['metadata'], {'key3': 'value3'})

    def test_disallow_metadata_changes_during_building(self):
        def fake_change_instance_metadata(inst, ctxt, diff, instance=None,
                                          instance_uuid=None):
            pass
        self.stub_out('nova.compute.rpcapi.ComputeAPI.'
                      'change_instance_metadata',
                       fake_change_instance_metadata)

        instance = self._create_fake_instance_obj(
            {'vm_state': vm_states.BUILDING})

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.delete_instance_metadata, self.context,
                instance, "key")

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.update_instance_metadata, self.context,
                instance, "key")

    @staticmethod
    def _parse_db_block_device_mapping(bdm_ref):
        attr_list = ('delete_on_termination', 'device_name', 'no_device',
                     'virtual_name', 'volume_id', 'volume_size', 'snapshot_id')
        bdm = {}
        for attr in attr_list:
            val = bdm_ref.get(attr, None)
            if val:
                bdm[attr] = val

        return bdm

    def _test_check_and_transform_bdm(self, bdms, expected_bdms,
                                      image_bdms=None, base_options=None,
                                      legacy_bdms=False,
                                      legacy_image_bdms=False):
        image_bdms = image_bdms or []
        image_meta = {}
        if image_bdms:
            image_meta = {'properties': {'block_device_mapping': image_bdms}}
            if not legacy_image_bdms:
                image_meta['properties']['bdm_v2'] = True
        base_options = base_options or {'root_device_name': 'vda',
                                        'image_ref': FAKE_IMAGE_REF}
        transformed_bdm = self.compute_api._check_and_transform_bdm(
                self.context, base_options, {},
                image_meta, 1, 1, bdms, legacy_bdms)
        for expected, got in zip(expected_bdms, transformed_bdm):
            self.assertEqual(dict(expected.items()), dict(got.items()))

    def test_check_and_transform_legacy_bdm_no_image_bdms(self):
        legacy_bdms = [
            {'device_name': '/dev/vda',
             'volume_id': '33333333-aaaa-bbbb-cccc-333333333333',
             'delete_on_termination': False}]
        expected_bdms = [block_device.BlockDeviceDict.from_legacy(
            legacy_bdms[0])]
        expected_bdms[0]['boot_index'] = 0
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, expected_bdms)
        self._test_check_and_transform_bdm(legacy_bdms, expected_bdms,
                                           legacy_bdms=True)

    def test_check_and_transform_legacy_bdm_legacy_image_bdms(self):
        image_bdms = [
            {'device_name': '/dev/vda',
             'volume_id': '33333333-aaaa-bbbb-cccc-333333333333',
             'delete_on_termination': False}]
        legacy_bdms = [
            {'device_name': '/dev/vdb',
             'volume_id': '33333333-aaaa-bbbb-cccc-444444444444',
             'delete_on_termination': False}]
        expected_bdms = [
                block_device.BlockDeviceDict.from_legacy(legacy_bdms[0]),
                block_device.BlockDeviceDict.from_legacy(image_bdms[0])]
        expected_bdms[0]['boot_index'] = -1
        expected_bdms[1]['boot_index'] = 0
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, expected_bdms)
        self._test_check_and_transform_bdm(legacy_bdms, expected_bdms,
                                           image_bdms=image_bdms,
                                           legacy_bdms=True,
                                           legacy_image_bdms=True)

    def test_check_and_transform_legacy_bdm_image_bdms(self):
        legacy_bdms = [
            {'device_name': '/dev/vdb',
             'volume_id': '33333333-aaaa-bbbb-cccc-444444444444',
             'delete_on_termination': False}]
        image_bdms = [block_device.BlockDeviceDict(
            {'source_type': 'volume', 'destination_type': 'volume',
             'volume_id': '33333333-aaaa-bbbb-cccc-444444444444',
             'boot_index': 0})]
        expected_bdms = [
                block_device.BlockDeviceDict.from_legacy(legacy_bdms[0]),
                image_bdms[0]]
        expected_bdms[0]['boot_index'] = -1
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, expected_bdms)
        self._test_check_and_transform_bdm(legacy_bdms, expected_bdms,
                                           image_bdms=image_bdms,
                                           legacy_bdms=True)

    def test_check_and_transform_bdm_no_image_bdms(self):
        bdms = [block_device.BlockDeviceDict({'source_type': 'image',
                                              'destination_type': 'local',
                                              'image_id': FAKE_IMAGE_REF,
                                              'boot_index': 0})]
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, bdms)
        self._test_check_and_transform_bdm(bdms, expected_bdms)

    def test_check_and_transform_bdm_image_bdms(self):
        bdms = [block_device.BlockDeviceDict({'source_type': 'image',
                                              'destination_type': 'local',
                                              'image_id': FAKE_IMAGE_REF,
                                              'boot_index': 0})]
        image_bdms = [block_device.BlockDeviceDict(
            {'source_type': 'volume', 'destination_type': 'volume',
             'volume_id': '33333333-aaaa-bbbb-cccc-444444444444'})]
        expected_bdms = bdms + image_bdms
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, expected_bdms)
        self._test_check_and_transform_bdm(bdms, expected_bdms,
                                           image_bdms=image_bdms)

    def test_check_and_transform_bdm_image_bdms_w_overrides(self):
        bdms = [block_device.BlockDeviceDict({'source_type': 'image',
                                              'destination_type': 'local',
                                              'image_id': FAKE_IMAGE_REF,
                                              'boot_index': 0}),
                block_device.BlockDeviceDict({'device_name': 'vdb',
                                              'no_device': True})]
        image_bdms = [block_device.BlockDeviceDict(
            {'source_type': 'volume', 'destination_type': 'volume',
             'volume_id': '33333333-aaaa-bbbb-cccc-444444444444',
             'device_name': '/dev/vdb'})]
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, bdms)
        self._test_check_and_transform_bdm(bdms, expected_bdms,
                                           image_bdms=image_bdms)

    def test_check_and_transform_bdm_image_bdms_w_overrides_complex(self):
        bdms = [block_device.BlockDeviceDict({'source_type': 'image',
                                              'destination_type': 'local',
                                              'image_id': FAKE_IMAGE_REF,
                                              'boot_index': 0}),
                block_device.BlockDeviceDict({'device_name': 'vdb',
                                              'no_device': True}),
                block_device.BlockDeviceDict(
                    {'source_type': 'volume', 'destination_type': 'volume',
                    'volume_id': '11111111-aaaa-bbbb-cccc-222222222222',
                    'device_name': 'vdc'})]
        image_bdms = [
            block_device.BlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                'volume_id': '33333333-aaaa-bbbb-cccc-444444444444',
                'device_name': '/dev/vdb'}),
            block_device.BlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                'volume_id': '55555555-aaaa-bbbb-cccc-666666666666',
                'device_name': '/dev/vdc'}),
            block_device.BlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                'volume_id': '77777777-aaaa-bbbb-cccc-8888888888888',
                'device_name': '/dev/vdd'})]
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, bdms + [image_bdms[2]])
        self._test_check_and_transform_bdm(bdms, expected_bdms,
                                           image_bdms=image_bdms)

    def test_check_and_transform_bdm_legacy_image_bdms(self):
        bdms = [block_device.BlockDeviceDict({'source_type': 'image',
                                              'destination_type': 'local',
                                              'image_id': FAKE_IMAGE_REF,
                                              'boot_index': 0})]
        image_bdms = [{'device_name': '/dev/vda',
                       'volume_id': '33333333-aaaa-bbbb-cccc-333333333333',
                       'delete_on_termination': False}]
        expected_bdms = [block_device.BlockDeviceDict.from_legacy(
            image_bdms[0])]
        expected_bdms[0]['boot_index'] = 0
        expected_bdms = block_device_obj.block_device_make_list_from_dicts(
                self.context, expected_bdms)
        self._test_check_and_transform_bdm(bdms, expected_bdms,
                                           image_bdms=image_bdms,
                                           legacy_image_bdms=True)

    def test_check_and_transform_image(self):
        base_options = {'root_device_name': 'vdb',
                        'image_ref': FAKE_IMAGE_REF}
        fake_legacy_bdms = [
            {'device_name': '/dev/vda',
             'volume_id': '33333333-aaaa-bbbb-cccc-333333333333',
             'delete_on_termination': False}]

        image_meta = {'properties': {'block_device_mapping': [
            {'device_name': '/dev/vda',
             'snapshot_id': '33333333-aaaa-bbbb-cccc-333333333333',
             'boot_index': 0}]}}

        # We get an image BDM
        transformed_bdm = self.compute_api._check_and_transform_bdm(
            self.context, base_options, {}, {}, 1, 1, fake_legacy_bdms, True)
        self.assertEqual(len(transformed_bdm), 2)

        # No image BDM created if image already defines a root BDM
        base_options['root_device_name'] = 'vda'
        base_options['image_ref'] = None
        transformed_bdm = self.compute_api._check_and_transform_bdm(
            self.context, base_options, {}, image_meta, 1, 1, [], True)
        self.assertEqual(len(transformed_bdm), 1)

        # No image BDM created
        transformed_bdm = self.compute_api._check_and_transform_bdm(
            self.context, base_options, {}, {}, 1, 1, fake_legacy_bdms, True)
        self.assertEqual(len(transformed_bdm), 1)

        # Volumes with multiple instances fails
        self.assertRaises(exception.InvalidRequest,
            self.compute_api._check_and_transform_bdm, self.context,
            base_options, {}, {}, 1, 2, fake_legacy_bdms, True)

        # Volume backed so no image_ref in base_options
        # v2 bdms contains a root image to volume mapping
        # image_meta contains a snapshot as the image
        # is created by nova image-create from a volume backed server
        # see bug 1381598
        fake_v2_bdms = [{'boot_index': 0,
                         'connection_info': None,
                         'delete_on_termination': None,
                         'destination_type': u'volume',
                         'image_id': FAKE_IMAGE_REF,
                         'source_type': u'image',
                         'volume_id': None,
                         'volume_size': 1}]
        base_options['image_ref'] = None
        transformed_bdm = self.compute_api._check_and_transform_bdm(
            self.context, base_options, {}, image_meta, 1, 1,
            fake_v2_bdms, False)
        self.assertEqual(len(transformed_bdm), 1)

        # Image BDM overrides mappings
        base_options['image_ref'] = FAKE_IMAGE_REF
        image_meta = {
            'properties': {
                'mappings': [
                    {'virtual': 'ephemeral0', 'device': 'vdb'}],
                'bdm_v2': True,
                'block_device_mapping': [
                    {'device_name': '/dev/vdb', 'source_type': 'blank',
                     'destination_type': 'volume', 'volume_size': 1}]}}
        transformed_bdm = self.compute_api._check_and_transform_bdm(
            self.context, base_options, {}, image_meta, 1, 1, [], False)
        self.assertEqual(1, len(transformed_bdm))
        self.assertEqual('volume', transformed_bdm[0]['destination_type'])
        self.assertEqual('/dev/vdb', transformed_bdm[0]['device_name'])

    def test_volume_size(self):
        ephemeral_size = 2
        swap_size = 3
        volume_size = 5

        swap_bdm = {'source_type': 'blank', 'guest_format': 'swap',
                    'destination_type': 'local'}
        ephemeral_bdm = {'source_type': 'blank', 'guest_format': None,
                         'destination_type': 'local'}
        volume_bdm = {'source_type': 'volume', 'volume_size': volume_size,
                      'destination_type': 'volume'}
        blank_bdm = {'source_type': 'blank', 'destination_type': 'volume'}

        inst_type = {'ephemeral_gb': ephemeral_size, 'swap': swap_size}
        self.assertEqual(
            self.compute_api._volume_size(inst_type, ephemeral_bdm),
            ephemeral_size)
        ephemeral_bdm['volume_size'] = 42
        self.assertEqual(
            self.compute_api._volume_size(inst_type, ephemeral_bdm), 42)
        self.assertEqual(
            self.compute_api._volume_size(inst_type, swap_bdm),
            swap_size)
        swap_bdm['volume_size'] = 42
        self.assertEqual(
                self.compute_api._volume_size(inst_type, swap_bdm), 42)
        self.assertEqual(
            self.compute_api._volume_size(inst_type, volume_bdm),
            volume_size)
        self.assertIsNone(
            self.compute_api._volume_size(inst_type, blank_bdm))

    def test_reservation_id_one_instance(self):
        """Verify building an instance has a reservation_id that
        matches return value from create.
        """
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['reservation_id'], resv_id)

    def test_reservation_ids_two_instances(self):
        """Verify building 2 instances at once results in a
        reservation_id being returned equal to reservation id set
        in both instances.
        """
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                min_count=2, max_count=2)
        self.assertEqual(len(refs), 2)
        self.assertIsNotNone(resv_id)
        for instance in refs:
            self.assertEqual(instance['reservation_id'], resv_id)

    def test_multi_instance_display_name_template(self, cells_enabled=False):
        num_instances = 2
        self.flags(multi_instance_display_name_template='%(name)s')
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                min_count=num_instances, max_count=num_instances,
                display_name='x')
        for i in range(num_instances):
            hostname = None if cells_enabled else 'x'
            self.assertEqual(refs[i]['display_name'], 'x')
            self.assertEqual(refs[i]['hostname'], hostname)

        self.flags(multi_instance_display_name_template='%(name)s-%(count)d')
        self._multi_instance_display_name_default(cells_enabled=cells_enabled)

        self.flags(multi_instance_display_name_template='%(name)s-%(uuid)s')
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                min_count=num_instances, max_count=num_instances,
                display_name='x')
        for i in range(num_instances):
            name = 'x' if cells_enabled else 'x-%s' % refs[i]['uuid']
            hostname = None if cells_enabled else name
            self.assertEqual(refs[i]['display_name'], name)
            self.assertEqual(refs[i]['hostname'], hostname)

    def test_multi_instance_display_name_default(self):
        self._multi_instance_display_name_default()

    def _multi_instance_display_name_default(self, cells_enabled=False):
        num_instances = 2
        (refs, resv_id) = self.compute_api.create(self.context,
                flavors.get_default_flavor(),
                image_href=uuids.image_href_id,
                min_count=num_instances, max_count=num_instances,
                display_name='x')
        for i in range(num_instances):
            name = 'x' if cells_enabled else 'x-%s' % (i + 1,)
            hostname = None if cells_enabled else name
            self.assertEqual(refs[i]['display_name'], name)
            self.assertEqual(refs[i]['hostname'], hostname)

    def test_instance_architecture(self):
        # Test the instance architecture.
        i_ref = self._create_fake_instance_obj()
        self.assertEqual(i_ref['architecture'], arch.X86_64)

    def test_instance_unknown_architecture(self):
        # Test if the architecture is unknown.
        instance = self._create_fake_instance_obj(
                        params={'architecture': ''})

        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])
        instance = db.instance_get_by_uuid(self.context,
                instance['uuid'])
        self.assertNotEqual(instance['architecture'], 'Unknown')

    def test_instance_name_template(self):
        # Test the instance_name template.
        self.flags(instance_name_template='instance-%d')
        i_ref = self._create_fake_instance_obj()
        self.assertEqual(i_ref['name'], 'instance-%d' % i_ref['id'])

        self.flags(instance_name_template='instance-%(uuid)s')
        i_ref = self._create_fake_instance_obj()
        self.assertEqual(i_ref['name'], 'instance-%s' % i_ref['uuid'])

        self.flags(instance_name_template='%(id)d-%(uuid)s')
        i_ref = self._create_fake_instance_obj()
        self.assertEqual(i_ref['name'], '%d-%s' %
                (i_ref['id'], i_ref['uuid']))

        # not allowed.. default is uuid
        self.flags(instance_name_template='%(name)s')
        i_ref = self._create_fake_instance_obj()
        self.assertEqual(i_ref['name'], i_ref['uuid'])

    def test_add_remove_fixed_ip(self):
        instance = self._create_fake_instance_obj(params={'host': CONF.host})
        self.stub_out('nova.network.api.API.deallocate_for_instance',
                       lambda *a, **kw: None)
        self.compute_api.add_fixed_ip(self.context, instance, '1')
        self.compute_api.remove_fixed_ip(self.context,
                                         instance, '192.168.1.1')
        with mock.patch.object(self.compute_api, '_lookup_instance',
                               return_value=instance):
            self.compute_api.delete(self.context, instance)

    def test_attach_volume_invalid(self):
        instance = fake_instance.fake_instance_obj(None, **{
            'locked': False, 'vm_state': vm_states.ACTIVE,
            'task_state': None,
            'launched_at': timeutils.utcnow()})
        self.assertRaises(exception.InvalidDevicePath,
                self.compute_api.attach_volume,
                self.context,
                instance,
                None,
                '/invalid')

    def test_add_missing_dev_names_assign_dev_name(self):
        instance = self._create_fake_instance_obj()
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'instance_uuid': instance.uuid,
                 'volume_id': 'vol-id',
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': None,
                 'boot_index': None,
                 'disk_bus': None,
                 'device_type': None
                 }))]
        with mock.patch.object(objects.BlockDeviceMapping,
                               'save') as mock_save:
            self.compute._add_missing_dev_names(bdms, instance)
            mock_save.assert_called_once_with()
        self.assertIsNotNone(bdms[0].device_name)

    @mock.patch.object(compute_manager.ComputeManager,
                       '_get_device_name_for_instance')
    def test_add_missing_dev_names_skip_bdms_with_dev_name(self,
                                                       mock_get_dev_name):
        instance = self._create_fake_instance_obj()
        bdms = [objects.BlockDeviceMapping(
                **fake_block_device.FakeDbBlockDeviceDict(
                {
                 'instance_uuid': instance.uuid,
                 'volume_id': 'vol-id',
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': '/dev/vda',
                 'boot_index': None,
                 'disk_bus': None,
                 'device_type': None
                 }))]
        self.compute._add_missing_dev_names(bdms, instance)
        self.assertFalse(mock_get_dev_name.called)

    def test_no_attach_volume_in_rescue_state(self):
        def fake(*args, **kwargs):
            pass

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}

        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)
        self.stub_out('nova.volume.cinder.API.check_attach', fake)
        self.stub_out('nova.volume.cinder.API.reserve_volume', fake)

        instance = fake_instance.fake_instance_obj(None, **{
            'uuid': 'f3000000-0000-0000-0000-000000000000', 'locked': False,
            'vm_state': vm_states.RESCUED})
        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.attach_volume,
                self.context,
                instance,
                None,
                '/dev/vdb')

    def test_no_attach_volume_in_suspended_state(self):
        instance = fake_instance.fake_instance_obj(None, **{
            'uuid': 'f3000000-0000-0000-0000-000000000000', 'locked': False,
            'vm_state': vm_states.SUSPENDED})
        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.attach_volume,
                self.context,
                instance,
                {'id': 'fake-volume-id'},
                '/dev/vdb')

    def test_no_detach_volume_in_rescue_state(self):
        # Ensure volume can be detached from instance

        params = {'vm_state': vm_states.RESCUED}
        instance = self._create_fake_instance_obj(params=params)

        volume = {'id': 1, 'attach_status': 'attached',
                   'instance_uuid': instance['uuid']}

        self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.detach_volume,
                self.context, instance, volume)

    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(cinder.API, 'get')
    def test_no_rescue_in_volume_state_attaching(self,
                                                 mock_get_vol,
                                                 mock_get_bdms):
        # Make sure a VM cannot be rescued while volume is being attached
        instance = self._create_fake_instance_obj()
        bdms, volume = self._fake_rescue_block_devices(instance)

        mock_get_vol.return_value = {'id': volume['id'],
                                     'status': "attaching"}
        mock_get_bdms.return_value = bdms

        self.assertRaises(exception.InvalidVolume,
                self.compute_api.rescue, self.context, instance)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_vnc_console')
    @mock.patch.object(compute_api.consoleauth_rpcapi.ConsoleAuthAPI,
                       'authorize_console')
    def test_vnc_console(self, mock_auth, mock_get):
        # Make sure we can a vnc console for an instance.

        fake_instance = self._fake_instance(
            {'uuid': 'f3000000-0000-0000-0000-000000000000',
             'host': 'fake_compute_host'})
        fake_console_type = "novnc"
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_console_host',
                             'port': 'fake_console_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance.uuid,
                             'access_url': 'fake_console_url'}
        mock_get.return_value = fake_connect_info

        console = self.compute_api.get_vnc_console(self.context,
                fake_instance, fake_console_type)

        self.assertEqual(console, {'url': 'fake_console_url'})
        mock_get.assert_called_once_with(
                self.context, instance=fake_instance,
                console_type=fake_console_type)
        mock_auth.assert_called_once_with(
            self.context, 'fake_token', fake_console_type, 'fake_console_host',
            'fake_console_port', 'fake_access_path',
            'f3000000-0000-0000-0000-000000000000',
            access_url='fake_console_url')

    def test_get_vnc_console_no_host(self):
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_vnc_console,
                          self.context, instance, 'novnc')

    @mock.patch.object(compute_api.consoleauth_rpcapi.ConsoleAuthAPI,
                       'authorize_console')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_spice_console')
    def test_spice_console(self, mock_spice, mock_auth):
        # Make sure we can a spice console for an instance.

        fake_instance = self._fake_instance(
            {'uuid': 'f3000000-0000-0000-0000-000000000000',
             'host': 'fake_compute_host'})
        fake_console_type = "spice-html5"
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_console_host',
                             'port': 'fake_console_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance.uuid,
                             'access_url': 'fake_console_url'}
        mock_spice.return_value = fake_connect_info

        console = self.compute_api.get_spice_console(self.context,
                fake_instance, fake_console_type)

        self.assertEqual(console, {'url': 'fake_console_url'})
        mock_spice.assert_called_once_with(self.context,
                                           instance=fake_instance,
                                           console_type=fake_console_type)
        mock_auth.assert_called_once_with(
            self.context, 'fake_token', fake_console_type, 'fake_console_host',
            'fake_console_port', 'fake_access_path',
            'f3000000-0000-0000-0000-000000000000',
            access_url='fake_console_url')

    def test_get_spice_console_no_host(self):
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_spice_console,
                          self.context, instance, 'spice')

    @mock.patch.object(compute_api.consoleauth_rpcapi.ConsoleAuthAPI,
                       'authorize_console')
    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_rdp_console')
    def test_rdp_console(self, mock_rdp, mock_auth):
        # Make sure we can a rdp console for an instance.
        fake_instance = self._fake_instance({
                         'uuid': 'f3000000-0000-0000-0000-000000000000',
                         'host': 'fake_compute_host'})
        fake_console_type = "rdp-html5"
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_console_host',
                             'port': 'fake_console_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance.uuid,
                             'access_url': 'fake_console_url'}
        mock_rdp.return_value = fake_connect_info

        console = self.compute_api.get_rdp_console(self.context,
                fake_instance, fake_console_type)

        self.assertEqual(console, {'url': 'fake_console_url'})
        mock_rdp.assert_called_once_with(self.context, instance=fake_instance,
                                         console_type=fake_console_type)
        mock_auth.assert_called_once_with(
            self.context, 'fake_token', fake_console_type, 'fake_console_host',
            'fake_console_port', 'fake_access_path',
            'f3000000-0000-0000-0000-000000000000',
            access_url='fake_console_url')

    def test_get_rdp_console_no_host(self):
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_rdp_console,
                          self.context, instance, 'rdp')

    def test_serial_console(self):
        # Make sure we can  get a serial proxy url for an instance.

        fake_instance = self._fake_instance({
                         'uuid': 'f3000000-0000-0000-0000-000000000000',
                         'host': 'fake_compute_host'})
        fake_console_type = 'serial'
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_serial_host',
                             'port': 'fake_tcp_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance.uuid,
                             'access_url': 'fake_access_url'}

        rpcapi = compute_rpcapi.ComputeAPI

        with test.nested(
            mock.patch.object(rpcapi, 'get_serial_console',
                              return_value=fake_connect_info),
            mock.patch.object(self.compute_api.consoleauth_rpcapi,
                              'authorize_console')
        ) as (mock_get_serial_console, mock_authorize_console):
            self.compute_api.consoleauth_rpcapi.authorize_console(
                    self.context, 'fake_token', fake_console_type,
                    'fake_serial_host', 'fake_tcp_port',
                    'fake_access_path',
                    'f3000000-0000-0000-0000-000000000000',
                    access_url='fake_access_url')

            console = self.compute_api.get_serial_console(self.context,
                                                          fake_instance,
                                                          fake_console_type)
            self.assertEqual(console, {'url': 'fake_access_url'})

    def test_get_serial_console_no_host(self):
        # Make sure an exception is raised when instance is not Active.
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_serial_console,
                          self.context, instance, 'serial')

    def test_mks_console(self):
        fake_instance = self._fake_instance({
                         'uuid': 'f3000000-0000-0000-0000-000000000000',
                         'host': 'fake_compute_host'})
        fake_console_type = 'webmks'
        fake_connect_info = {'token': 'fake_token',
                             'console_type': fake_console_type,
                             'host': 'fake_mks_host',
                             'port': 'fake_tcp_port',
                             'internal_access_path': 'fake_access_path',
                             'instance_uuid': fake_instance.uuid,
                             'access_url': 'fake_access_url'}

        with test.nested(
            mock.patch.object(self.compute_api.compute_rpcapi,
                              'get_mks_console',
                              return_value=fake_connect_info),
            mock.patch.object(self.compute_api.consoleauth_rpcapi,
                              'authorize_console')
        ) as (mock_get_mks_console, mock_authorize_console):
            console = self.compute_api.get_mks_console(self.context,
                                                       fake_instance,
                                                       fake_console_type)
            self.assertEqual(console, {'url': 'fake_access_url'})

    def test_get_mks_console_no_host(self):
        # Make sure an exception is raised when instance is not Active.
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_mks_console,
                          self.context, instance, 'mks')

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_console_output')
    def test_console_output(self, mock_console):
        fake_instance = self._fake_instance({
                         'uuid': 'f3000000-0000-0000-0000-000000000000',
                         'host': 'fake_compute_host'})
        fake_tail_length = 699
        fake_console_output = 'fake console output'
        mock_console.return_value = fake_console_output

        output = self.compute_api.get_console_output(self.context,
                fake_instance, tail_length=fake_tail_length)

        self.assertEqual(output, fake_console_output)
        mock_console.assert_called_once_with(self.context,
                                             instance=fake_instance,
                                             tail_length=fake_tail_length)

    def test_console_output_no_host(self):
        instance = self._create_fake_instance_obj(params={'host': ''})

        self.assertRaises(exception.InstanceNotReady,
                          self.compute_api.get_console_output,
                          self.context, instance)

    @mock.patch.object(network_api.API, 'allocate_port_for_instance')
    def test_attach_interface(self, mock_allocate):
        new_type = flavors.get_flavor_by_flavor_id('4')
        instance = objects.Instance(image_ref=uuids.image_instance,
                                    system_metadata={},
                                    flavor=new_type,
                                    host='fake-host')
        nwinfo = [fake_network_cache_model.new_vif()]
        network_id = nwinfo[0]['network']['id']
        port_id = nwinfo[0]['id']
        req_ip = '1.2.3.4'
        mock_allocate.return_value = nwinfo

        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_attach_interface=True):
            vif = self.compute.attach_interface(self.context,
                                                instance,
                                                network_id,
                                                port_id,
                                                req_ip)
        self.assertEqual(vif['id'], network_id)
        mock_allocate.assert_called_once_with(
            self.context, instance, port_id, network_id, req_ip,
            bind_host_id='fake-host')
        return nwinfo, port_id

    def test_attach_interface_failed(self):
        new_type = flavors.get_flavor_by_flavor_id('4')
        instance = objects.Instance(
                       id=42,
                       uuid=uuids.interface_failed_instance,
                       image_ref='foo',
                       system_metadata={},
                       flavor=new_type,
                       host='fake-host')
        nwinfo = [fake_network_cache_model.new_vif()]
        network_id = nwinfo[0]['network']['id']
        port_id = nwinfo[0]['id']
        req_ip = '1.2.3.4'

        with test.nested(
            mock.patch.object(self.compute.driver, 'attach_interface'),
            mock.patch.object(self.compute.network_api,
                              'allocate_port_for_instance'),
            mock.patch.object(self.compute.network_api,
                              'deallocate_port_for_instance'),
            mock.patch.dict(self.compute.driver.capabilities,
                            supports_attach_interface=True)) as (
                mock_attach, mock_allocate, mock_deallocate, mock_dict):

            mock_allocate.return_value = nwinfo
            mock_attach.side_effect = exception.NovaException("attach_failed")
            self.assertRaises(exception.InterfaceAttachFailed,
                              self.compute.attach_interface, self.context,
                              instance, network_id, port_id, req_ip)
            mock_allocate.assert_called_once_with(self.context, instance,
                                                  network_id, port_id, req_ip,
                                                  bind_host_id='fake-host')
            mock_deallocate.assert_called_once_with(self.context, instance,
                                                    port_id)

    def test_detach_interface(self):
        nwinfo, port_id = self.test_attach_interface()
        self.stub_out('nova.network.api.API.'
                       'deallocate_port_for_instance',
                       lambda a, b, c, d: [])
        instance = objects.Instance()
        instance.info_cache = objects.InstanceInfoCache.new(
            self.context, uuids.info_cache_instance)
        instance.info_cache.network_info = network_model.NetworkInfo.hydrate(
            nwinfo)
        self.compute.detach_interface(self.context, instance, port_id)
        self.assertEqual(self.compute.driver._interfaces, {})

    def test_detach_interface_failed(self):
        nwinfo, port_id = self.test_attach_interface()
        instance = objects.Instance(id=42)
        instance['uuid'] = uuids.info_cache_instance
        instance.info_cache = objects.InstanceInfoCache.new(
            self.context, uuids.info_cache_instance)
        instance.info_cache.network_info = network_model.NetworkInfo.hydrate(
            nwinfo)

        with test.nested(
            mock.patch.object(self.compute.driver, 'detach_interface',
                side_effect=exception.NovaException('detach_failed')),
            mock.patch.object(self.compute.network_api,
                              'deallocate_port_for_instance')) as (
            mock_detach, mock_deallocate):
            self.assertRaises(exception.InterfaceDetachFailed,
                              self.compute.detach_interface, self.context,
                              instance, port_id)
            self.assertFalse(mock_deallocate.called)

    @mock.patch.object(compute_manager.LOG, 'warning')
    def test_detach_interface_deallocate_port_for_instance_failed(self,
                                                                  warn_mock):
        # Tests that when deallocate_port_for_instance fails we log the failure
        # before exiting compute.detach_interface.
        nwinfo, port_id = self.test_attach_interface()
        instance = objects.Instance(id=42, uuid=uuidutils.generate_uuid())
        instance.info_cache = objects.InstanceInfoCache.new(
            self.context, uuids.info_cache_instance)
        instance.info_cache.network_info = network_model.NetworkInfo.hydrate(
            nwinfo)

        # Sometimes neutron errors slip through the neutronv2 API so we want
        # to make sure we catch those in the compute manager and not just
        # NovaExceptions.
        error = neutron_exceptions.PortNotFoundClient()
        with test.nested(
            mock.patch.object(self.compute.driver, 'detach_interface'),
            mock.patch.object(self.compute.network_api,
                              'deallocate_port_for_instance',
                              side_effect=error),
            mock.patch.object(self.compute, '_instance_update')) as (
            mock_detach, mock_deallocate, mock_instance_update):
            ex = self.assertRaises(neutron_exceptions.PortNotFoundClient,
                                   self.compute.detach_interface, self.context,
                                   instance, port_id)
            self.assertEqual(error, ex)
        mock_deallocate.assert_called_once_with(
            self.context, instance, port_id)
        self.assertEqual(1, warn_mock.call_count)

    def test_attach_volume(self):
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                 'volume_id': uuids.volume_id, 'device_name': '/dev/vdb'})
        bdm = block_device_obj.BlockDeviceMapping()._from_db_object(
                self.context,
                block_device_obj.BlockDeviceMapping(),
                fake_bdm)
        instance = self._create_fake_instance_obj()
        instance.id = 42
        fake_volume = {'id': 'fake-volume-id'}

        with test.nested(
            mock.patch.object(cinder.API, 'get', return_value=fake_volume),
            mock.patch.object(cinder.API, 'check_availability_zone'),
            mock.patch.object(cinder.API, 'reserve_volume'),
            mock.patch.object(compute_rpcapi.ComputeAPI,
                'reserve_block_device_name', return_value=bdm),
            mock.patch.object(compute_rpcapi.ComputeAPI, 'attach_volume')
        ) as (mock_get, mock_check_availability_zone, mock_reserve_vol,
                mock_reserve_bdm, mock_attach):

            self.compute_api.attach_volume(
                    self.context, instance, 'fake-volume-id',
                    '/dev/vdb', 'ide', 'cdrom')

            mock_reserve_bdm.assert_called_once_with(
                    self.context, instance, '/dev/vdb', 'fake-volume-id',
                    disk_bus='ide', device_type='cdrom')
            self.assertEqual(mock_get.call_args,
                             mock.call(self.context, 'fake-volume-id'))
            self.assertEqual(mock_check_availability_zone.call_args,
                             mock.call(
                                 self.context, fake_volume, instance=instance))
            mock_reserve_vol.assert_called_once_with(
                    self.context, 'fake-volume-id')
            a, kw = mock_attach.call_args
            self.assertEqual(a[2].device_name, '/dev/vdb')
            self.assertEqual(a[2].volume_id, uuids.volume_id)

    def test_attach_volume_shelved_offloaded(self):
        instance = self._create_fake_instance_obj()
        with test.nested(
             mock.patch.object(compute_api.API,
                               '_check_attach_and_reserve_volume'),
             mock.patch.object(cinder.API, 'attach')
        ) as (mock_attach_and_reserve, mock_attach):
            self.compute_api._attach_volume_shelved_offloaded(
                    self.context, instance, 'fake-volume-id',
                    '/dev/vdb', 'ide', 'cdrom')
            mock_attach_and_reserve.assert_called_once_with(self.context,
                                                            'fake-volume-id',
                                                            instance)
            mock_attach.assert_called_once_with(self.context,
                                                'fake-volume-id',
                                                instance.uuid,
                                                '/dev/vdb')
            self.assertTrue(mock_attach.called)

    def test_attach_volume_no_device(self):

        called = {}

        def fake_check_availability_zone(*args, **kwargs):
            called['fake_check_availability_zone'] = True

        def fake_reserve_volume(*args, **kwargs):
            called['fake_reserve_volume'] = True

        def fake_volume_get(self, context, volume_id):
            called['fake_volume_get'] = True
            return {'id': volume_id}

        def fake_rpc_attach_volume(self, context, instance, bdm):
            called['fake_rpc_attach_volume'] = True

        def fake_rpc_reserve_block_device_name(self, context, instance, device,
                                               volume_id, **kwargs):
            called['fake_rpc_reserve_block_device_name'] = True
            bdm = block_device_obj.BlockDeviceMapping(context=context)
            bdm['device_name'] = '/dev/vdb'
            return bdm

        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)
        self.stub_out('nova.volume.cinder.API.check_availability_zone',
                      fake_check_availability_zone)
        self.stub_out('nova.volume.cinder.API.reserve_volume',
                       fake_reserve_volume)
        self.stub_out('nova.compute.rpcapi.ComputeAPI.'
                       'reserve_block_device_name',
                       fake_rpc_reserve_block_device_name)
        self.stub_out('nova.compute.rpcapi.ComputeAPI.attach_volume',
                       fake_rpc_attach_volume)

        instance = self._create_fake_instance_obj()
        self.compute_api.attach_volume(self.context, instance, 1, device=None)
        self.assertTrue(called.get('fake_check_availability_zone'))
        self.assertTrue(called.get('fake_reserve_volume'))
        self.assertTrue(called.get('fake_volume_get'))
        self.assertTrue(called.get('fake_rpc_reserve_block_device_name'))
        self.assertTrue(called.get('fake_rpc_attach_volume'))

    def test_detach_volume(self):
        # Ensure volume can be detached from instance
        called = {}
        instance = self._create_fake_instance_obj()
        # Set attach_status to 'fake' as nothing is reading the value.
        volume = {'id': 1, 'attach_status': 'fake'}

        def fake_check_detach(*args, **kwargs):
            called['fake_check_detach'] = True

        def fake_begin_detaching(*args, **kwargs):
            called['fake_begin_detaching'] = True

        def fake_rpc_detach_volume(self, context, **kwargs):
            called['fake_rpc_detach_volume'] = True

        self.stub_out('nova.volume.cinder.API.check_detach', fake_check_detach)
        self.stub_out('nova.volume.cinder.API.begin_detaching',
                      fake_begin_detaching)
        self.stub_out('nova.compute.rpcapi.ComputeAPI.detach_volume',
                       fake_rpc_detach_volume)

        self.compute_api.detach_volume(self.context,
                instance, volume)
        self.assertTrue(called.get('fake_check_detach'))
        self.assertTrue(called.get('fake_begin_detaching'))
        self.assertTrue(called.get('fake_rpc_detach_volume'))

    @mock.patch.object(compute_api.API, '_check_and_begin_detach')
    @mock.patch.object(compute_api.API, '_local_cleanup_bdm_volumes')
    @mock.patch.object(objects.BlockDeviceMapping, 'get_by_volume_id')
    def test_detach_volume_shelved_offloaded(self,
                                             mock_block_dev,
                                             mock_local_cleanup,
                                             mock_check_begin_detach):

        mock_block_dev.return_value = [block_device_obj.BlockDeviceMapping(
                                      context=context)]
        instance = self._create_fake_instance_obj()
        volume = {'id': 1, 'attach_status': 'fake'}
        self.compute_api._detach_volume_shelved_offloaded(self.context,
                                                          instance,
                                                          volume)
        mock_check_begin_detach.assert_called_once_with(self.context,
                                                        volume,
                                                        instance)
        self.assertTrue(mock_local_cleanup.called)

    def test_detach_invalid_volume(self):
        # Ensure exception is raised while detaching an un-attached volume
        fake_instance = self._fake_instance({
                    'uuid': 'f7000000-0000-0000-0000-000000000001',
                    'locked': False,
                    'launched_at': timeutils.utcnow(),
                    'vm_state': vm_states.ACTIVE,
                    'task_state': None})
        volume = {'id': 1, 'attach_status': 'detached', 'status': 'available'}

        self.assertRaises(exception.InvalidVolume,
                          self.compute_api.detach_volume, self.context,
                          fake_instance, volume)

    def test_detach_unattached_volume(self):
        # Ensure exception is raised when volume's idea of attached
        # instance doesn't match.
        fake_instance = self._fake_instance({
                    'uuid': 'f7000000-0000-0000-0000-000000000001',
                    'locked': False,
                    'launched_at': timeutils.utcnow(),
                    'vm_state': vm_states.ACTIVE,
                    'task_state': None})
        volume = {'id': 1, 'attach_status': 'attached', 'status': 'in-use',
                  'attachments': {'fake_uuid': {'attachment_id': 'fakeid'}}}

        self.assertRaises(exception.VolumeUnattached,
                          self.compute_api.detach_volume, self.context,
                          fake_instance, volume)

    def test_detach_suspended_instance_fails(self):
        fake_instance = self._fake_instance({
                    'uuid': 'f7000000-0000-0000-0000-000000000001',
                    'locked': False,
                    'launched_at': timeutils.utcnow(),
                    'vm_state': vm_states.SUSPENDED,
                    'task_state': None})
        # Unused
        volume = {}

        self.assertRaises(exception.InstanceInvalidState,
                          self.compute_api.detach_volume, self.context,
                          fake_instance, volume)

    @mock.patch.object(objects.BlockDeviceMapping,
                       'get_by_volume_and_instance')
    def test_detach_volume_libvirt_is_down(self, mock_get):
        # Ensure rollback during detach if libvirt goes down
        called = {}
        instance = self._create_fake_instance_obj()

        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
                {'device_name': '/dev/vdb', 'volume_id': uuids.volume_id,
                 'source_type': 'snapshot', 'destination_type': 'volume',
                 'connection_info': '{"test": "test"}'})

        def fake_libvirt_driver_instance_exists(self, _instance):
            called['fake_libvirt_driver_instance_exists'] = True
            return False

        def fake_libvirt_driver_detach_volume_fails(*args, **kwargs):
            called['fake_libvirt_driver_detach_volume_fails'] = True
            raise AttributeError()

        def fake_roll_detaching(*args, **kwargs):
            called['fake_roll_detaching'] = True

        self.stub_out('nova.volume.cinder.API.roll_detaching',
                      fake_roll_detaching)
        self.stub_out('nova.virt.fake.FakeDriver.instance_exists',
                       fake_libvirt_driver_instance_exists)
        self.stub_out('nova.virt.fake.FakeDriver.detach_volume',
                       fake_libvirt_driver_detach_volume_fails)
        mock_get.return_value = objects.BlockDeviceMapping(
                                    context=self.context, **fake_bdm)

        self.assertRaises(AttributeError, self.compute.detach_volume,
                          self.context, 1, instance)
        self.assertTrue(called.get('fake_libvirt_driver_instance_exists'))
        self.assertTrue(called.get('fake_roll_detaching'))
        mock_get.assert_called_once_with(self.context, 1, instance.uuid)

    def test_detach_volume_not_found(self):
        # Ensure that a volume can be detached even when it is removed
        # from an instance but remaining in bdm. See bug #1367964.

        instance = self._create_fake_instance_obj()
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
                {'source_type': 'volume', 'destination_type': 'volume',
                 'volume_id': 'fake-id', 'device_name': '/dev/vdb',
                 'connection_info': '{"test": "test"}'})
        bdm = objects.BlockDeviceMapping(context=self.context, **fake_bdm)

        # Stub out fake_volume_get so cinder api does not raise exception
        # and manager gets to call bdm.destroy()
        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}
        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)

        with test.nested(
            mock.patch.object(self.compute.driver, 'detach_volume',
                              side_effect=exception.DiskNotFound('sdb')),
            mock.patch.object(objects.BlockDeviceMapping,
                              'get_by_volume_and_instance', return_value=bdm),
            mock.patch.object(cinder.API, 'terminate_connection'),
            mock.patch.object(bdm, 'destroy'),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute.volume_api, 'detach'),
            mock.patch.object(self.compute.driver, 'get_volume_connector',
                              return_value='fake-connector')
        ) as (mock_detach_volume, mock_volume, mock_terminate_connection,
              mock_destroy, mock_notify, mock_detach, mock_volume_connector):
            self.compute.detach_volume(self.context, 'fake-id', instance)
            self.assertTrue(mock_detach_volume.called)
            mock_terminate_connection.assert_called_once_with(self.context,
                                                              'fake-id',
                                                              'fake-connector')
            mock_destroy.assert_called_once_with()
            mock_detach.assert_called_once_with(mock.ANY, 'fake-id',
                                                instance.uuid, None)

    def test_terminate_with_volumes(self):
        # Make sure that volumes get detached during instance termination.
        admin = context.get_admin_context()
        instance = self._create_fake_instance_obj()

        volume_id = 'fake'
        values = {'instance_uuid': instance['uuid'],
                  'device_name': '/dev/vdc',
                  'delete_on_termination': False,
                  'volume_id': volume_id,
                  'destination_type': 'volume'
                  }
        db.block_device_mapping_create(admin, values)

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}
        self.stub_out("nova.volume.cinder.API.get", fake_volume_get)

        # Stub out and record whether it gets detached
        result = {"detached": False}

        def fake_detach(self, context, volume_id_param, instance_uuid):
            result["detached"] = volume_id_param == volume_id
        self.stub_out("nova.volume.cinder.API.detach", fake_detach)

        def fake_terminate_connection(self, context, volume_id, connector):
            return {}
        self.stub_out("nova.volume.cinder.API.terminate_connection",
                       fake_terminate_connection)

        # Kill the instance and check that it was detached
        bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
            admin, instance['uuid'])
        self.compute.terminate_instance(admin, instance, bdms, [])

        self.assertTrue(result["detached"])

    def test_terminate_deletes_all_bdms(self):
        admin = context.get_admin_context()
        instance = self._create_fake_instance_obj()

        img_bdm = {'context': admin,
                     'instance_uuid': instance['uuid'],
                     'device_name': '/dev/vda',
                     'source_type': 'image',
                     'destination_type': 'local',
                     'delete_on_termination': False,
                     'boot_index': 0,
                     'image_id': 'fake_image'}
        vol_bdm = {'context': admin,
                     'instance_uuid': instance['uuid'],
                     'device_name': '/dev/vdc',
                     'source_type': 'volume',
                     'destination_type': 'volume',
                     'delete_on_termination': False,
                     'volume_id': 'fake_vol'}
        bdms = []
        for bdm in img_bdm, vol_bdm:
            bdm_obj = objects.BlockDeviceMapping(**bdm)
            bdm_obj.create()
            bdms.append(bdm_obj)
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                      mock.MagicMock())
        self.stub_out('nova.volume.cinder.API.detach', mock.MagicMock())

        def fake_volume_get(self, context, volume_id):
            return {'id': volume_id}
        self.stub_out('nova.volume.cinder.API.get', fake_volume_get)

        self.stub_out('nova.compute.manager.ComputeManager_prep_block_device',
                      mock.MagicMock())
        self.compute.build_and_run_instance(self.context, instance, {}, {}, {},
                                            block_device_mapping=[])

        self.compute.terminate_instance(self.context, instance, bdms, [])

        bdms = db.block_device_mapping_get_all_by_instance(admin,
                                                           instance['uuid'])
        self.assertEqual(len(bdms), 0)

    def test_inject_network_info(self):
        instance = self._create_fake_instance_obj(params={'host': CONF.host})
        self.compute.build_and_run_instance(self.context,
                instance, {}, {}, {}, block_device_mapping=[])
        instance = self.compute_api.get(self.context, instance['uuid'])
        self.compute_api.inject_network_info(self.context, instance)

    def test_reset_network(self):
        instance = self._create_fake_instance_obj()
        self.compute.build_and_run_instance(self.context,
                instance, {}, {}, {}, block_device_mapping=[])
        instance = self.compute_api.get(self.context, instance['uuid'])
        self.compute_api.reset_network(self.context, instance)

    def test_lock(self):
        instance = self._create_fake_instance_obj()
        self.stub_out('nova.network.api.API.deallocate_for_instance',
                       lambda *a, **kw: None)
        self.compute_api.lock(self.context, instance)

    def test_unlock(self):
        instance = self._create_fake_instance_obj()
        self.stub_out('nova.network.api.API.deallocate_for_instance',
                       lambda *a, **kw: None)
        self.compute_api.unlock(self.context, instance)

    def test_add_remove_security_group(self):
        instance = self._create_fake_instance_obj()

        self.compute.build_and_run_instance(self.context,
                instance, {}, {}, {}, block_device_mapping=[])
        instance = self.compute_api.get(self.context, instance.uuid)
        security_group_name = self._create_group()['name']

        self.security_group_api.add_to_instance(self.context,
                                                instance,
                                                security_group_name)
        self.security_group_api.remove_from_instance(self.context,
                                                     instance,
                                                     security_group_name)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_diagnostics')
    def test_get_diagnostics(self, mock_get):
        instance = self._create_fake_instance_obj()

        self.compute_api.get_diagnostics(self.context, instance)

        mock_get.assert_called_once_with(self.context, instance=instance)

    @mock.patch.object(compute_rpcapi.ComputeAPI, 'get_instance_diagnostics')
    def test_get_instance_diagnostics(self, mock_get):
        instance = self._create_fake_instance_obj()

        self.compute_api.get_instance_diagnostics(self.context, instance)

        mock_get.assert_called_once_with(self.context, instance=instance)

    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'refresh_instance_security_rules')
    def test_refresh_instance_security_rules(self, mock_refresh):
        inst1 = self._create_fake_instance_obj()
        inst2 = self._create_fake_instance_obj({'host': None})

        self.security_group_api._refresh_instance_security_rules(
            self.context, [inst1, inst2])
        mock_refresh.assert_called_once_with(self.context, inst1, inst1.host)

    @mock.patch.object(compute_rpcapi.ComputeAPI,
                       'refresh_instance_security_rules')
    def test_refresh_instance_security_rules_empty(self, mock_refresh):
        self.security_group_api._refresh_instance_security_rules(self.context,
                                                                 [])
        self.assertFalse(mock_refresh.called)

    @mock.patch.object(compute_api.SecurityGroupAPI,
                       '_refresh_instance_security_rules')
    @mock.patch.object(objects.InstanceList,
                       'get_by_grantee_security_group_ids')
    def test_secgroup_refresh(self, mock_get, mock_refresh):
        mock_get.return_value = mock.sentinel.instances

        self.security_group_api.trigger_members_refresh(mock.sentinel.ctxt,
                                                        mock.sentinel.ids)

        mock_get.assert_called_once_with(mock.sentinel.ctxt, mock.sentinel.ids)
        mock_refresh.assert_called_once_with(mock.sentinel.ctxt,
                                             mock.sentinel.instances)

    @mock.patch.object(compute_api.SecurityGroupAPI,
                       '_refresh_instance_security_rules')
    @mock.patch.object(objects.InstanceList,
                       'get_by_security_group_id')
    def test_secrule_refresh(self, mock_get, mock_refresh):
        mock_get.return_value = mock.sentinel.instances

        self.security_group_api.trigger_rules_refresh(mock.sentinel.ctxt,
                                                      mock.sentinel.id)

        mock_get.assert_called_once_with(mock.sentinel.ctxt, mock.sentinel.id)
        mock_refresh.assert_called_once_with(mock.sentinel.ctxt,
                                             mock.sentinel.instances)

    def _test_live_migrate(self, force=None):
        instance, instance_uuid = self._run_instance()

        rpcapi = self.compute_api.compute_task_api
        fake_spec = objects.RequestSpec()

        @mock.patch.object(rpcapi, 'live_migrate_instance')
        @mock.patch.object(objects.ComputeNodeList, 'get_all_by_host')
        @mock.patch.object(objects.RequestSpec, 'get_by_instance_uuid')
        @mock.patch.object(self.compute_api, '_record_action_start')
        def do_test(record_action_start, get_by_instance_uuid,
                    get_all_by_host, live_migrate_instance):
            get_by_instance_uuid.return_value = fake_spec
            get_all_by_host.return_value = objects.ComputeNodeList(
                objects=[objects.ComputeNode(
                    host='fake_dest_host',
                    hypervisor_hostname='fake_dest_node')])

            self.compute_api.live_migrate(self.context, instance,
                                          block_migration=True,
                                          disk_over_commit=True,
                                          host_name='fake_dest_host',
                                          force=force, async=False)

            record_action_start.assert_called_once_with(self.context, instance,
                                                        'live-migration')
            if force is False:
                host = None
            else:
                host = 'fake_dest_host'
            live_migrate_instance.assert_called_once_with(
                self.context, instance, host,
                block_migration=True,
                disk_over_commit=True,
                request_spec=fake_spec, async=False)

        do_test()
        instance.refresh()
        self.assertEqual(instance['task_state'], task_states.MIGRATING)
        if force is False:
            req_dest = fake_spec.requested_destination
            self.assertIsNotNone(req_dest)
            self.assertIsInstance(req_dest, objects.Destination)
            self.assertEqual('fake_dest_host', req_dest.host)
            self.assertEqual('fake_dest_node', req_dest.node)

    def test_live_migrate(self):
        self._test_live_migrate()

    def test_live_migrate_with_not_forced_host(self):
        self._test_live_migrate(force=False)

    def test_live_migrate_with_forced_host(self):
        self._test_live_migrate(force=True)

    def test_fail_live_migrate_with_non_existing_destination(self):
        instance = self._create_fake_instance_obj(services=True)
        self.assertIsNone(instance.task_state)

        self.assertRaises(
            exception.ComputeHostNotFound,
            self.compute_api.live_migrate, self.context.elevated(),
            instance, block_migration=True,
            disk_over_commit=True,
            host_name='fake_dest_host',
            force=False)

    def _test_evacuate(self, force=None):
        instance = self._create_fake_instance_obj(services=True)
        self.assertIsNone(instance.task_state)

        ctxt = self.context.elevated()

        fake_spec = objects.RequestSpec()

        def fake_rebuild_instance(*args, **kwargs):
            # NOTE(sbauza): Host can be set to None, we need to fake a correct
            # destination if this is the case.
            instance.host = kwargs['host'] or 'fake_dest_host'
            instance.save()

        @mock.patch.object(self.compute_api.compute_task_api,
                           'rebuild_instance')
        @mock.patch.object(objects.ComputeNodeList, 'get_all_by_host')
        @mock.patch.object(objects.RequestSpec,
                           'get_by_instance_uuid')
        @mock.patch.object(self.compute_api.servicegroup_api, 'service_is_up')
        def do_test(service_is_up, get_by_instance_uuid, get_all_by_host,
                    rebuild_instance):
            service_is_up.return_value = False
            get_by_instance_uuid.return_value = fake_spec
            rebuild_instance.side_effect = fake_rebuild_instance
            get_all_by_host.return_value = objects.ComputeNodeList(
                objects=[objects.ComputeNode(
                    host='fake_dest_host',
                    hypervisor_hostname='fake_dest_node')])

            self.compute_api.evacuate(ctxt,
                                      instance,
                                      host='fake_dest_host',
                                      on_shared_storage=True,
                                      admin_password=None,
                                      force=force)
            if force is False:
                host = None
            else:
                host = 'fake_dest_host'
            rebuild_instance.assert_called_once_with(
                ctxt,
                instance=instance,
                new_pass=None,
                injected_files=None,
                image_ref=None,
                orig_image_ref=None,
                orig_sys_metadata=None,
                bdms=None,
                recreate=True,
                on_shared_storage=True,
                request_spec=fake_spec,
                host=host)
        do_test()

        instance.refresh()
        self.assertEqual(instance.task_state, task_states.REBUILDING)
        self.assertEqual(instance.host, 'fake_dest_host')
        migs = objects.MigrationList.get_by_filters(
            self.context, {'source_host': 'fake_host'})
        self.assertEqual(1, len(migs))
        self.assertEqual(self.compute.host, migs[0].source_compute)
        self.assertEqual('accepted', migs[0].status)
        self.assertEqual('compute.instance.evacuate',
                         fake_notifier.NOTIFICATIONS[0].event_type)
        if force is False:
            req_dest = fake_spec.requested_destination
            self.assertIsNotNone(req_dest)
            self.assertIsInstance(req_dest, objects.Destination)
            self.assertEqual('fake_dest_host', req_dest.host)
            self.assertEqual('fake_dest_node', req_dest.node)

    def test_evacuate(self):
        self._test_evacuate()

    def test_evacuate_with_not_forced_host(self):
        self._test_evacuate(force=False)

    def test_evacuate_with_forced_host(self):
        self._test_evacuate(force=True)

    @mock.patch('nova.servicegroup.api.API.service_is_up',
                return_value=False)
    def test_fail_evacuate_with_non_existing_destination(self, _service_is_up):
        instance = self._create_fake_instance_obj(services=True)
        self.assertIsNone(instance.task_state)

        self.assertRaises(exception.ComputeHostNotFound,
                self.compute_api.evacuate, self.context.elevated(), instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None, force=False)

    def test_fail_evacuate_from_non_existing_host(self):
        inst = {}
        inst['vm_state'] = vm_states.ACTIVE
        inst['launched_at'] = timeutils.utcnow()
        inst['image_ref'] = FAKE_IMAGE_REF
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = self.user_id
        inst['project_id'] = self.project_id
        inst['host'] = 'fake_host'
        inst['node'] = NODENAME
        type_id = flavors.get_flavor_by_name('m1.tiny')['id']
        inst['instance_type_id'] = type_id
        inst['ami_launch_index'] = 0
        inst['memory_mb'] = 0
        inst['vcpus'] = 0
        inst['root_gb'] = 0
        inst['ephemeral_gb'] = 0
        inst['architecture'] = arch.X86_64
        inst['os_type'] = 'Linux'
        instance = self._create_fake_instance_obj(inst)

        self.assertIsNone(instance.task_state)
        self.assertRaises(exception.ComputeHostNotFound,
                self.compute_api.evacuate, self.context.elevated(), instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)

    def test_fail_evacuate_from_running_host(self):
        instance = self._create_fake_instance_obj(services=True)
        self.assertIsNone(instance.task_state)

        def fake_service_is_up(*args, **kwargs):
            return True

        self.stub_out('nova.servicegroup.api.API.service_is_up',
                      fake_service_is_up)

        self.assertRaises(exception.ComputeServiceInUse,
                self.compute_api.evacuate, self.context.elevated(), instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)

    def test_fail_evacuate_instance_in_wrong_state(self):
        states = [vm_states.BUILDING, vm_states.PAUSED, vm_states.SUSPENDED,
                  vm_states.RESCUED, vm_states.RESIZED, vm_states.SOFT_DELETED,
                  vm_states.DELETED]
        instances = [self._create_fake_instance_obj({'vm_state': state})
                     for state in states]

        for instance in instances:
            self.assertRaises(exception.InstanceInvalidState,
                self.compute_api.evacuate, self.context, instance,
                host='fake_dest_host', on_shared_storage=True,
                admin_password=None)

    @mock.patch.object(db, "migration_get_all_by_filters")
    def test_get_migrations(self, mock_migration):
        migration = test_migration.fake_db_migration()
        filters = {'host': 'host1'}
        mock_migration.return_value = [migration]

        migrations = self.compute_api.get_migrations(self.context,
                                                             filters)
        self.assertEqual(1, len(migrations))
        self.assertEqual(migrations[0].id, migration['id'])
        mock_migration.assert_called_once_with(self.context, filters)

    @mock.patch("nova.db.migration_get_in_progress_by_instance")
    def test_get_migrations_in_progress_by_instance(self, mock_get):
        migration = test_migration.fake_db_migration(
            instance_uuid=uuids.instance)
        mock_get.return_value = [migration]
        db.migration_get_in_progress_by_instance(self.context,
                                                 uuids.instance)
        migrations = self.compute_api.get_migrations_in_progress_by_instance(
                self.context, uuids.instance)
        self.assertEqual(1, len(migrations))
        self.assertEqual(migrations[0].id, migration['id'])

    @mock.patch("nova.db.migration_get_by_id_and_instance")
    def test_get_migration_by_id_and_instance(self, mock_get):
        migration = test_migration.fake_db_migration(
            instance_uuid=uuids.instance)
        mock_get.return_value = migration
        db.migration_get_by_id_and_instance(
                self.context, migration['id'], uuid)
        res = self.compute_api.get_migration_by_id_and_instance(
                self.context, migration['id'], uuids.instance)
        self.assertEqual(res.id, migration['id'])


class ComputeAPIIpFilterTestCase(test.NoDBTestCase):
    '''Verifies the IP filtering in the compute API.'''

    def setUp(self):
        super(ComputeAPIIpFilterTestCase, self).setUp()
        self.compute_api = compute.API()

    def _get_ip_filtering_instances(self):
        '''Utility function to get instances for the IP filtering tests.'''
        info = [{
            'address': 'aa:bb:cc:dd:ee:ff',
            'id': 1,
            'network': {
                'bridge': 'br0',
                'id': 1,
                'label': 'private',
                'subnets': [{
                    'cidr': '192.168.0.0/24',
                    'ips': [{
                        'address': '192.168.0.10',
                        'type': 'fixed'
                    }, {
                        'address': '192.168.0.11',
                        'type': 'fixed'
                    }]
                }]
            }
        }, {
            'address': 'aa:bb:cc:dd:ee:ff',
            'id': 2,
            'network': {
                'bridge': 'br1',
                'id': 2,
                'label': 'private',
                'subnets': [{
                    'cidr': '192.164.0.0/24',
                    'ips': [{
                        'address': '192.164.0.10',
                        'type': 'fixed'
                    }]
                }]
            }
        }]

        info1 = objects.InstanceInfoCache(network_info=jsonutils.dumps(info))
        inst1 = objects.Instance(id=1, info_cache=info1)
        info[0]['network']['subnets'][0]['ips'][0]['address'] = '192.168.0.20'
        info[0]['network']['subnets'][0]['ips'][1]['address'] = '192.168.0.21'
        info[1]['network']['subnets'][0]['ips'][0]['address'] = '192.164.0.20'
        info2 = objects.InstanceInfoCache(network_info=jsonutils.dumps(info))
        inst2 = objects.Instance(id=2, info_cache=info2)
        return objects.InstanceList(objects=[inst1, inst2])

    def test_ip_filtering_no_matches(self):
        instances = self._get_ip_filtering_instances()
        insts = self.compute_api._ip_filter(instances, {'ip': '.*30'}, None)
        self.assertEqual(0, len(insts))

    def test_ip_filtering_one_match(self):
        instances = self._get_ip_filtering_instances()
        for val in ('192.168.0.10', '192.168.0.1', '192.164.0.10', '.*10'):
            insts = self.compute_api._ip_filter(instances, {'ip': val}, None)
            self.assertEqual([1], [i.id for i in insts])

    def test_ip_filtering_one_match_limit(self):
        instances = self._get_ip_filtering_instances()
        for limit in (None, 1, 2):
            insts = self.compute_api._ip_filter(instances,
                                                {'ip': '.*10'},
                                                limit)
            self.assertEqual([1], [i.id for i in insts])

    def test_ip_filtering_two_matches(self):
        instances = self._get_ip_filtering_instances()
        for val in ('192.16', '192.168', '192.164'):
            insts = self.compute_api._ip_filter(instances, {'ip': val}, None)
            self.assertEqual([1, 2], [i.id for i in insts])

    def test_ip_filtering_two_matches_limit(self):
        instances = self._get_ip_filtering_instances()
        # Up to 2 match, based on the passed limit
        for limit in (None, 1, 2, 3):
            insts = self.compute_api._ip_filter(instances,
                                                {'ip': '192.168.0.*'},
                                                limit)
            expected_ids = [1, 2]
            if limit:
                expected_len = min(limit, len(expected_ids))
                expected_ids = expected_ids[:expected_len]
            self.assertEqual(expected_ids, [inst.id for inst in insts])

    @mock.patch.object(objects.CellMapping, 'get_by_uuid',
                       side_effect=exception.CellMappingNotFound(uuid='fake'))
    def test_ip_filtering_no_limit_to_db(self, _mock_cell_map_get):
        c = context.get_admin_context()
        # Limit is not supplied to the DB when using an IP filter
        with mock.patch('nova.objects.InstanceList.get_by_filters') as m_get:
            m_get.return_value = objects.InstanceList(objects=[])
            self.compute_api.get_all(c, search_opts={'ip': '.10'}, limit=1)
            self.assertEqual(1, m_get.call_count)
            kwargs = m_get.call_args[1]
            self.assertIsNone(kwargs['limit'])

    @mock.patch.object(objects.CellMapping, 'get_by_uuid',
                       side_effect=exception.CellMappingNotFound(uuid='fake'))
    def test_ip_filtering_pass_limit_to_db(self, _mock_cell_map_get):
        c = context.get_admin_context()
        # No IP filter, verify that the limit is passed
        with mock.patch('nova.objects.InstanceList.get_by_filters') as m_get:
            m_get.return_value = objects.InstanceList(objects=[])
            self.compute_api.get_all(c, search_opts={}, limit=1)
            self.assertEqual(1, m_get.call_count)
            kwargs = m_get.call_args[1]
            self.assertEqual(1, kwargs['limit'])


def fake_rpc_method(self, context, method, **kwargs):
    pass


def _create_service_entries(context, values=[['avail_zone1', ['fake_host1',
                                                             'fake_host2']],
                                             ['avail_zone2', ['fake_host3']]]):
    for (avail_zone, hosts) in values:
        for host in hosts:
            db.service_create(context,
                              {'host': host,
                               'binary': 'nova-compute',
                               'topic': 'compute',
                               'report_count': 0})
    return values


class ComputeAPIAggrTestCase(BaseTestCase):
    """This is for unit coverage of aggregate-related methods
    defined in nova.compute.api.
    """

    def setUp(self):
        super(ComputeAPIAggrTestCase, self).setUp()
        self.api = compute_api.AggregateAPI()
        self.context = context.get_admin_context()
        self.stub_out('oslo_messaging.rpc.client.call', fake_rpc_method)
        self.stub_out('oslo_messaging.rpc.client.cast', fake_rpc_method)

    def test_aggregate_no_zone(self):
        # Ensure we can create an aggregate without an availability  zone
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         None)
        self.api.delete_aggregate(self.context, aggr.id)
        self.assertRaises(exception.AggregateNotFound,
                          self.api.delete_aggregate, self.context, aggr.id)

    def test_check_az_for_aggregate(self):
        # Ensure all conflict hosts can be returned
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host1 = values[0][1][0]
        fake_host2 = values[0][1][1]
        aggr1 = self._init_aggregate_with_host(None, 'fake_aggregate1',
                                               fake_zone, fake_host1)
        aggr1 = self._init_aggregate_with_host(aggr1, None, None, fake_host2)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host2)
        aggr2 = self._init_aggregate_with_host(aggr2, None, None, fake_host1)
        metadata = {'availability_zone': 'another_zone'}
        self.assertRaises(exception.InvalidAggregateActionUpdate,
                          self.api.update_aggregate,
                          self.context, aggr2.id, metadata)

    def test_update_aggregate(self):
        # Ensure metadata can be updated.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        fake_notifier.NOTIFICATIONS = []
        self.api.update_aggregate(self.context, aggr.id,
                                         {'name': 'new_fake_aggregate'})
        self.assertIsNone(availability_zones._get_cache().get('cache'))
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updateprop.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updateprop.end')

    def test_update_aggregate_no_az(self):
        # Ensure metadata without availability zone can be
        # updated,even the aggregate contains hosts belong
        # to another availability zone
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        self._init_aggregate_with_host(None, 'fake_aggregate1',
                                       fake_zone, fake_host)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host)
        metadata = {'name': 'new_fake_aggregate'}
        fake_notifier.NOTIFICATIONS = []
        self.api.update_aggregate(self.context, aggr2.id, metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updateprop.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updateprop.end')

    def test_update_aggregate_az_change(self):
        # Ensure availability zone can be updated,
        # when the aggregate is the only one with
        # availability zone
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        aggr1 = self._init_aggregate_with_host(None, 'fake_aggregate1',
                                               fake_zone, fake_host)
        self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                       fake_host)
        metadata = {'availability_zone': 'new_fake_zone'}
        fake_notifier.NOTIFICATIONS = []
        self.api.update_aggregate(self.context, aggr1.id, metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')

    def test_update_aggregate_az_fails(self):
        # Ensure aggregate's availability zone can't be updated,
        # when aggregate has hosts in other availability zone
        fake_notifier.NOTIFICATIONS = []
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        self._init_aggregate_with_host(None, 'fake_aggregate1',
                                       fake_zone, fake_host)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host)
        metadata = {'availability_zone': 'another_zone'}
        self.assertRaises(exception.InvalidAggregateActionUpdate,
                          self.api.update_aggregate,
                          self.context, aggr2.id, metadata)
        fake_host2 = values[0][1][1]
        aggr3 = self._init_aggregate_with_host(None, 'fake_aggregate3',
                                               None, fake_host2)
        metadata = {'availability_zone': fake_zone}
        self.api.update_aggregate(self.context, aggr3.id, metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 15)
        msg = fake_notifier.NOTIFICATIONS[13]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[14]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')
        aggr4 = self.api.create_aggregate(self.context, 'fake_aggregate', None)
        metadata = {'availability_zone': ""}
        self.assertRaises(exception.InvalidAggregateActionUpdate,
                          self.api.update_aggregate, self.context,
                          aggr4.id, metadata)

    def test_update_aggregate_az_fails_with_nova_az(self):
        # Ensure aggregate's availability zone can't be updated,
        # when aggregate has hosts in other availability zone
        fake_notifier.NOTIFICATIONS = []
        values = _create_service_entries(self.context)
        fake_host = values[0][1][0]
        self._init_aggregate_with_host(None, 'fake_aggregate1',
                                       CONF.default_availability_zone,
                                       fake_host)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host)
        metadata = {'availability_zone': 'another_zone'}
        self.assertRaises(exception.InvalidAggregateActionUpdate,
                          self.api.update_aggregate,
                          self.context, aggr2.id, metadata)

    def test_update_aggregate_metadata(self):
        # Ensure metadata can be updated.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        metadata = {'foo_key1': 'foo_value1',
                    'foo_key2': 'foo_value2',
                    'availability_zone': 'fake_zone'}
        fake_notifier.NOTIFICATIONS = []
        availability_zones._get_cache().add('fake_key', 'fake_value')
        aggr = self.api.update_aggregate_metadata(self.context, aggr.id,
                                                  metadata)
        self.assertIsNone(availability_zones._get_cache().get('fake_key'))
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')
        fake_notifier.NOTIFICATIONS = []
        metadata['foo_key1'] = None
        expected_payload_meta_data = {'foo_key1': None,
                                      'foo_key2': 'foo_value2',
                                      'availability_zone': 'fake_zone'}
        expected = self.api.update_aggregate_metadata(self.context,
                                             aggr.id, metadata)
        self.assertEqual(2, len(fake_notifier.NOTIFICATIONS))
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual('aggregate.updatemetadata.start', msg.event_type)
        self.assertEqual(expected_payload_meta_data, msg.payload['meta_data'])
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual('aggregate.updatemetadata.end', msg.event_type)
        self.assertEqual(expected_payload_meta_data, msg.payload['meta_data'])
        self.assertThat(expected.metadata,
                        matchers.DictMatches({'availability_zone': 'fake_zone',
                        'foo_key2': 'foo_value2'}))

    def test_update_aggregate_metadata_no_az(self):
        # Ensure metadata without availability zone can be
        # updated,even the aggregate contains hosts belong
        # to another availability zone
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        self._init_aggregate_with_host(None, 'fake_aggregate1',
                                       fake_zone, fake_host)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host)
        metadata = {'foo_key2': 'foo_value3'}
        fake_notifier.NOTIFICATIONS = []
        aggr2 = self.api.update_aggregate_metadata(self.context, aggr2.id,
                                                  metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')
        self.assertThat(aggr2.metadata,
                        matchers.DictMatches({'foo_key2': 'foo_value3'}))

    def test_update_aggregate_metadata_az_change(self):
        # Ensure availability zone can be updated,
        # when the aggregate is the only one with
        # availability zone
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        aggr1 = self._init_aggregate_with_host(None, 'fake_aggregate1',
                                               fake_zone, fake_host)
        self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                       fake_host)
        metadata = {'availability_zone': 'new_fake_zone'}
        fake_notifier.NOTIFICATIONS = []
        self.api.update_aggregate_metadata(self.context, aggr1.id, metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')

    def test_update_aggregate_az_do_not_replace_existing_metadata(self):
        # Ensure that the update of the aggregate availability zone
        # does not replace the aggregate existing metadata
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        metadata = {'foo_key1': 'foo_value1'}
        aggr = self.api.update_aggregate_metadata(self.context,
                                                  aggr.id,
                                                  metadata)
        metadata = {'availability_zone': 'new_fake_zone'}
        aggr = self.api.update_aggregate(self.context,
                                         aggr.id,
                                         metadata)
        self.assertThat(aggr.metadata, matchers.DictMatches(
            {'availability_zone': 'new_fake_zone', 'foo_key1': 'foo_value1'}))

    def test_update_aggregate_metadata_az_fails(self):
        # Ensure aggregate's availability zone can't be updated,
        # when aggregate has hosts in other availability zone
        fake_notifier.NOTIFICATIONS = []
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        self._init_aggregate_with_host(None, 'fake_aggregate1',
                                       fake_zone, fake_host)
        aggr2 = self._init_aggregate_with_host(None, 'fake_aggregate2', None,
                                               fake_host)
        metadata = {'availability_zone': 'another_zone'}
        self.assertRaises(exception.InvalidAggregateActionUpdateMeta,
                          self.api.update_aggregate_metadata,
                          self.context, aggr2.id, metadata)
        aggr3 = self._init_aggregate_with_host(None, 'fake_aggregate3',
                                               None, fake_host)
        metadata = {'availability_zone': fake_zone}
        self.api.update_aggregate_metadata(self.context, aggr3.id, metadata)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 15)
        msg = fake_notifier.NOTIFICATIONS[13]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.start')
        msg = fake_notifier.NOTIFICATIONS[14]
        self.assertEqual(msg.event_type,
                         'aggregate.updatemetadata.end')
        aggr4 = self.api.create_aggregate(self.context, 'fake_aggregate', None)
        metadata = {'availability_zone': ""}
        self.assertRaises(exception.InvalidAggregateActionUpdateMeta,
                          self.api.update_aggregate_metadata, self.context,
                          aggr4.id, metadata)

    def test_delete_aggregate(self):
        # Ensure we can delete an aggregate.
        fake_notifier.NOTIFICATIONS = []
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.create.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.create.end')
        fake_notifier.NOTIFICATIONS = []
        self.api.delete_aggregate(self.context, aggr.id)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.delete.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.delete.end')
        self.assertRaises(exception.AggregateNotFound,
                          self.api.delete_aggregate, self.context, aggr.id)

    def test_delete_non_empty_aggregate(self):
        # Ensure InvalidAggregateAction is raised when non empty aggregate.
        _create_service_entries(self.context,
                                [['fake_availability_zone', ['fake_host']]])
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_availability_zone')
        self.api.add_host_to_aggregate(self.context, aggr.id, 'fake_host')
        self.assertRaises(exception.InvalidAggregateActionDelete,
                          self.api.delete_aggregate, self.context, aggr.id)

    @mock.patch.object(availability_zones,
                       'update_host_availability_zone_cache')
    def test_add_host_to_aggregate(self, mock_az):
        # Ensure we can add a host to an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)

        def fake_add_aggregate_host(*args, **kwargs):
            hosts = kwargs["aggregate"].hosts
            self.assertIn(fake_host, hosts)

        self.stub_out('nova.compute.rpcapi.ComputeAPI.add_aggregate_host',
                       fake_add_aggregate_host)

        fake_notifier.NOTIFICATIONS = []
        aggr = self.api.add_host_to_aggregate(self.context,
                                              aggr.id, fake_host)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.addhost.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.addhost.end')
        self.assertEqual(len(aggr.hosts), 1)
        mock_az.assert_called_once_with(self.context, fake_host)

    def test_add_host_to_aggr_with_no_az(self):
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        aggr = self.api.add_host_to_aggregate(self.context, aggr.id,
                                              fake_host)
        aggr_no_az = self.api.create_aggregate(self.context, 'fake_aggregate2',
                                               None)
        aggr_no_az = self.api.add_host_to_aggregate(self.context,
                                                    aggr_no_az.id,
                                                    fake_host)
        self.assertIn(fake_host, aggr.hosts)
        self.assertIn(fake_host, aggr_no_az.hosts)

    def test_add_host_to_multi_az(self):
        # Ensure we can't add a host to different availability zone
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        fake_host = values[0][1][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        aggr = self.api.add_host_to_aggregate(self.context,
                                              aggr.id, fake_host)
        self.assertEqual(len(aggr.hosts), 1)
        fake_zone2 = "another_zone"
        aggr2 = self.api.create_aggregate(self.context,
                                         'fake_aggregate2', fake_zone2)
        self.assertRaises(exception.InvalidAggregateActionAdd,
                          self.api.add_host_to_aggregate,
                          self.context, aggr2.id, fake_host)

    def test_add_host_to_multi_az_with_nova_agg(self):
        # Ensure we can't add a host if already existing in an agg with AZ set
        #  to default
        values = _create_service_entries(self.context)
        fake_host = values[0][1][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate',
                                         CONF.default_availability_zone)
        aggr = self.api.add_host_to_aggregate(self.context,
                                              aggr.id, fake_host)
        self.assertEqual(len(aggr.hosts), 1)
        fake_zone2 = "another_zone"
        aggr2 = self.api.create_aggregate(self.context,
                                         'fake_aggregate2', fake_zone2)
        self.assertRaises(exception.InvalidAggregateActionAdd,
                          self.api.add_host_to_aggregate,
                          self.context, aggr2.id, fake_host)

    def test_add_host_to_aggregate_multiple(self):
        # Ensure we can add multiple hosts to an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        for host in values[0][1]:
            aggr = self.api.add_host_to_aggregate(self.context,
                                                  aggr.id, host)
        self.assertEqual(len(aggr.hosts), len(values[0][1]))

    def test_add_host_to_aggregate_raise_not_found(self):
        # Ensure ComputeHostNotFound is raised when adding invalid host.
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        fake_notifier.NOTIFICATIONS = []
        self.assertRaises(exception.ComputeHostNotFound,
                          self.api.add_host_to_aggregate,
                          self.context, aggr.id, 'invalid_host')
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        self.assertEqual(fake_notifier.NOTIFICATIONS[1].publisher_id,
                         'compute.fake-mini')

    @mock.patch.object(availability_zones,
                       'update_host_availability_zone_cache')
    def test_remove_host_from_aggregate_active(self, mock_az):
        # Ensure we can remove a host from an aggregate.
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        aggr = self.api.create_aggregate(self.context,
                                         'fake_aggregate', fake_zone)
        for host in values[0][1]:
            aggr = self.api.add_host_to_aggregate(self.context,
                                                  aggr.id, host)
        host_to_remove = values[0][1][0]

        def fake_remove_aggregate_host(*args, **kwargs):
            hosts = kwargs["aggregate"].hosts
            self.assertNotIn(host_to_remove, hosts)

        self.stub_out('nova.compute.rpcapi.ComputeAPI.remove_aggregate_host',
                       fake_remove_aggregate_host)

        fake_notifier.NOTIFICATIONS = []
        expected = self.api.remove_host_from_aggregate(self.context,
                                                       aggr.id,
                                                       host_to_remove)
        self.assertEqual(len(fake_notifier.NOTIFICATIONS), 2)
        msg = fake_notifier.NOTIFICATIONS[0]
        self.assertEqual(msg.event_type,
                         'aggregate.removehost.start')
        msg = fake_notifier.NOTIFICATIONS[1]
        self.assertEqual(msg.event_type,
                         'aggregate.removehost.end')
        self.assertEqual(len(aggr.hosts) - 1, len(expected.hosts))
        mock_az.assert_called_with(self.context, host_to_remove)

    def test_remove_host_from_aggregate_raise_not_found(self):
        # Ensure ComputeHostNotFound is raised when removing invalid host.
        _create_service_entries(self.context, [['fake_zone', ['fake_host']]])
        aggr = self.api.create_aggregate(self.context, 'fake_aggregate',
                                         'fake_zone')
        self.assertRaises(exception.ComputeHostNotFound,
                          self.api.remove_host_from_aggregate,
                          self.context, aggr.id, 'invalid_host')

    def test_aggregate_list(self):
        aggregate = self.api.create_aggregate(self.context,
                                              'fake_aggregate',
                                              'fake_zone')
        metadata = {'foo_key1': 'foo_value1',
                    'foo_key2': 'foo_value2'}
        meta_aggregate = self.api.create_aggregate(self.context,
                                                   'fake_aggregate2',
                                                   'fake_zone2')
        self.api.update_aggregate_metadata(self.context, meta_aggregate.id,
                                           metadata)
        aggregate_list = self.api.get_aggregate_list(self.context)
        self.assertIn(aggregate.id,
                      map(lambda x: x.id, aggregate_list))
        self.assertIn(meta_aggregate.id,
                      map(lambda x: x.id, aggregate_list))
        self.assertIn('fake_aggregate',
                      map(lambda x: x.name, aggregate_list))
        self.assertIn('fake_aggregate2',
                      map(lambda x: x.name, aggregate_list))
        self.assertIn('fake_zone',
                      map(lambda x: x.availability_zone, aggregate_list))
        self.assertIn('fake_zone2',
                      map(lambda x: x.availability_zone, aggregate_list))
        test_agg_meta = aggregate_list[1].metadata
        self.assertIn('foo_key1', test_agg_meta)
        self.assertIn('foo_key2', test_agg_meta)
        self.assertEqual('foo_value1', test_agg_meta['foo_key1'])
        self.assertEqual('foo_value2', test_agg_meta['foo_key2'])

    def test_aggregate_list_with_hosts(self):
        values = _create_service_entries(self.context)
        fake_zone = values[0][0]
        host_aggregate = self.api.create_aggregate(self.context,
                                                   'fake_aggregate',
                                                   fake_zone)
        self.api.add_host_to_aggregate(self.context, host_aggregate.id,
                                       values[0][1][0])
        aggregate_list = self.api.get_aggregate_list(self.context)
        aggregate = aggregate_list[0]
        hosts = aggregate.hosts if 'hosts' in aggregate else None
        self.assertIn(values[0][1][0], hosts)


class ComputeAPIAggrCallsSchedulerTestCase(test.NoDBTestCase):
    """This is for making sure that all Aggregate API methods which are
    updating the aggregates DB table also notifies the Scheduler by using
    its client.
    """

    def setUp(self):
        super(ComputeAPIAggrCallsSchedulerTestCase, self).setUp()
        self.api = compute_api.AggregateAPI()
        self.context = context.RequestContext('fake', 'fake')

    @mock.patch.object(scheduler_client.SchedulerClient, 'update_aggregates')
    def test_create_aggregate(self, update_aggregates):
        with mock.patch.object(objects.Aggregate, 'create'):
            agg = self.api.create_aggregate(self.context, 'fake', None)
        update_aggregates.assert_called_once_with(self.context, [agg])

    @mock.patch.object(scheduler_client.SchedulerClient, 'update_aggregates')
    def test_update_aggregate(self, update_aggregates):
        self.api.is_safe_to_update_az = mock.Mock()
        agg = objects.Aggregate()
        with mock.patch.object(objects.Aggregate, 'get_by_id',
                               return_value=agg):
            self.api.update_aggregate(self.context, 1, {})
        update_aggregates.assert_called_once_with(self.context, [agg])

    @mock.patch.object(scheduler_client.SchedulerClient, 'update_aggregates')
    def test_update_aggregate_metadata(self, update_aggregates):
        self.api.is_safe_to_update_az = mock.Mock()
        agg = objects.Aggregate()
        agg.update_metadata = mock.Mock()
        with mock.patch.object(objects.Aggregate, 'get_by_id',
                               return_value=agg):
            self.api.update_aggregate_metadata(self.context, 1, {})
        update_aggregates.assert_called_once_with(self.context, [agg])

    @mock.patch.object(scheduler_client.SchedulerClient, 'delete_aggregate')
    def test_delete_aggregate(self, delete_aggregate):
        self.api.is_safe_to_update_az = mock.Mock()
        agg = objects.Aggregate(hosts=[])
        agg.destroy = mock.Mock()
        with mock.patch.object(objects.Aggregate, 'get_by_id',
                               return_value=agg):
            self.api.delete_aggregate(self.context, 1)
        delete_aggregate.assert_called_once_with(self.context, agg)

    @mock.patch('nova.compute.rpcapi.ComputeAPI.add_aggregate_host')
    @mock.patch.object(scheduler_client.SchedulerClient, 'update_aggregates')
    def test_add_host_to_aggregate(self, update_aggregates, mock_add_agg):
        self.api.is_safe_to_update_az = mock.Mock()
        self.api._update_az_cache_for_host = mock.Mock()
        agg = objects.Aggregate(name='fake', metadata={})
        agg.add_host = mock.Mock()
        with test.nested(
                mock.patch.object(objects.Service, 'get_by_compute_host'),
                mock.patch.object(objects.Aggregate, 'get_by_id',
                                  return_value=agg)):
            self.api.add_host_to_aggregate(self.context, 1, 'fakehost')
        update_aggregates.assert_called_once_with(self.context, [agg])
        mock_add_agg.assert_called_once_with(self.context, aggregate=agg,
                                             host_param='fakehost',
                                             host='fakehost')

    @mock.patch('nova.compute.rpcapi.ComputeAPI.remove_aggregate_host')
    @mock.patch.object(scheduler_client.SchedulerClient, 'update_aggregates')
    def test_remove_host_from_aggregate(self, update_aggregates,
                                        mock_remove_agg):
        self.api._update_az_cache_for_host = mock.Mock()
        agg = objects.Aggregate(name='fake', metadata={})
        agg.delete_host = mock.Mock()
        with test.nested(
                mock.patch.object(objects.Service, 'get_by_compute_host'),
                mock.patch.object(objects.Aggregate, 'get_by_id',
                                  return_value=agg)):
            self.api.remove_host_from_aggregate(self.context, 1, 'fakehost')
        update_aggregates.assert_called_once_with(self.context, [agg])
        mock_remove_agg.assert_called_once_with(self.context, aggregate=agg,
                                                host_param='fakehost',
                                                host='fakehost')


class ComputeAggrTestCase(BaseTestCase):
    """This is for unit coverage of aggregate-related methods
    defined in nova.compute.manager.
    """

    def setUp(self):
        super(ComputeAggrTestCase, self).setUp()
        self.context = context.get_admin_context()
        values = {'name': 'test_aggr'}
        az = {'availability_zone': 'test_zone'}
        self.aggr = db.aggregate_create(self.context, values, metadata=az)

    def test_add_aggregate_host(self):
        def fake_driver_add_to_aggregate(self, context, aggregate, host,
                                         **_ignore):
            fake_driver_add_to_aggregate.called = True
            return {"foo": "bar"}
        self.stub_out("nova.virt.fake.FakeDriver.add_to_aggregate",
                       fake_driver_add_to_aggregate)

        self.compute.add_aggregate_host(self.context, host="host",
                aggregate=jsonutils.to_primitive(self.aggr), slave_info=None)
        self.assertTrue(fake_driver_add_to_aggregate.called)

    def test_remove_aggregate_host(self):
        def fake_driver_remove_from_aggregate(cls, context, aggregate, host,
                                              **_ignore):
            fake_driver_remove_from_aggregate.called = True
            self.assertEqual("host", host, "host")
            return {"foo": "bar"}
        self.stub_out("nova.virt.fake.FakeDriver.remove_from_aggregate",
                       fake_driver_remove_from_aggregate)

        self.compute.remove_aggregate_host(self.context,
                aggregate=jsonutils.to_primitive(self.aggr), host="host",
                slave_info=None)
        self.assertTrue(fake_driver_remove_from_aggregate.called)

    def test_add_aggregate_host_passes_slave_info_to_driver(self):
        def driver_add_to_aggregate(cls, context, aggregate, host, **kwargs):
            self.assertEqual(self.context, context)
            self.assertEqual(aggregate['id'], self.aggr['id'])
            self.assertEqual(host, "the_host")
            self.assertEqual("SLAVE_INFO", kwargs.get("slave_info"))

        self.stub_out("nova.virt.fake.FakeDriver.add_to_aggregate",
                       driver_add_to_aggregate)

        self.compute.add_aggregate_host(self.context, host="the_host",
                slave_info="SLAVE_INFO",
                aggregate=jsonutils.to_primitive(self.aggr))

    def test_remove_from_aggregate_passes_slave_info_to_driver(self):
        def driver_remove_from_aggregate(cls, context, aggregate, host,
                                         **kwargs):
            self.assertEqual(self.context, context)
            self.assertEqual(aggregate['id'], self.aggr['id'])
            self.assertEqual(host, "the_host")
            self.assertEqual("SLAVE_INFO", kwargs.get("slave_info"))

        self.stub_out("nova.virt.fake.FakeDriver.remove_from_aggregate",
                       driver_remove_from_aggregate)

        self.compute.remove_aggregate_host(self.context,
                aggregate=jsonutils.to_primitive(self.aggr), host="the_host",
                slave_info="SLAVE_INFO")


class DisabledInstanceTypesTestCase(BaseTestCase):
    """Some instance-types are marked 'disabled' which means that they will not
    show up in customer-facing listings. We do, however, want those
    instance-types to be available for emergency migrations and for rebuilding
    of existing instances.

    One legitimate use of the 'disabled' field would be when phasing out a
    particular instance-type. We still want customers to be able to use an
    instance that of the old type, and we want Ops to be able perform
    migrations against it, but we *don't* want customers building new
    instances with the phased-out instance-type.
    """
    def setUp(self):
        super(DisabledInstanceTypesTestCase, self).setUp()
        self.compute_api = compute.API()
        self.inst_type = flavors.get_default_flavor()

    def test_can_build_instance_from_visible_instance_type(self):
        self.inst_type['disabled'] = False
        # Assert that exception.FlavorNotFound is not raised
        self.compute_api.create(self.context, self.inst_type,
                                image_href=uuids.image_instance)

    def test_cannot_build_instance_from_disabled_instance_type(self):
        self.inst_type['disabled'] = True
        self.assertRaises(exception.FlavorNotFound,
            self.compute_api.create, self.context, self.inst_type, None)

    def test_can_resize_to_visible_instance_type(self):
        instance = self._create_fake_instance_obj()
        orig_get_flavor_by_flavor_id =\
                flavors.get_flavor_by_flavor_id

        def fake_get_flavor_by_flavor_id(flavor_id, ctxt=None,
                                                read_deleted="yes"):
            instance_type = orig_get_flavor_by_flavor_id(flavor_id,
                                                                ctxt,
                                                                read_deleted)
            instance_type['disabled'] = False
            return instance_type

        self.stub_out('nova.compute.flavors.get_flavor_by_flavor_id',
                       fake_get_flavor_by_flavor_id)

        self._stub_migrate_server()
        self.compute_api.resize(self.context, instance, '4')

    def test_cannot_resize_to_disabled_instance_type(self):
        instance = self._create_fake_instance_obj()
        orig_get_flavor_by_flavor_id = \
                flavors.get_flavor_by_flavor_id

        def fake_get_flavor_by_flavor_id(flavor_id, ctxt=None,
                                                read_deleted="yes"):
            instance_type = orig_get_flavor_by_flavor_id(flavor_id,
                                                                ctxt,
                                                                read_deleted)
            instance_type['disabled'] = True
            return instance_type

        self.stub_out('nova.compute.flavors.get_flavor_by_flavor_id',
                       fake_get_flavor_by_flavor_id)

        self.assertRaises(exception.FlavorNotFound,
            self.compute_api.resize, self.context, instance, '4')


class ComputeReschedulingTestCase(BaseTestCase):
    """Tests re-scheduling logic for new build requests."""

    def setUp(self):
        super(ComputeReschedulingTestCase, self).setUp()

        self.expected_task_state = task_states.SCHEDULING

        def fake_update(*args, **kwargs):
            self.updated_task_state = kwargs.get('task_state')
        self.stub_out('nova.compute.manager.ComputeManager._instance_update',
                      fake_update)

    def _reschedule(self, request_spec=None, filter_properties=None,
                    exc_info=None):
        if not filter_properties:
            filter_properties = {}
        fake_taskapi = FakeComputeTaskAPI()
        with mock.patch.object(self.compute, 'compute_task_api',
                               fake_taskapi):
            instance = self._create_fake_instance_obj()

            scheduler_method = self.compute.compute_task_api.resize_instance
            method_args = (instance, None,
                           dict(filter_properties=filter_properties),
                           {}, None)
            return self.compute._reschedule(self.context, request_spec,
                    filter_properties, instance, scheduler_method,
                    method_args, self.expected_task_state, exc_info=exc_info)

    def test_reschedule_no_filter_properties(self):
        # no filter_properties will disable re-scheduling.
        self.assertFalse(self._reschedule())

    def test_reschedule_no_retry_info(self):
        # no retry info will also disable re-scheduling.
        filter_properties = {}
        self.assertFalse(self._reschedule(filter_properties=filter_properties))

    def test_reschedule_no_request_spec(self):
        # no request spec will also disable re-scheduling.
        retry = dict(num_attempts=1)
        filter_properties = dict(retry=retry)
        self.assertFalse(self._reschedule(filter_properties=filter_properties))

    def test_reschedule_success(self):
        retry = dict(num_attempts=1)
        filter_properties = dict(retry=retry)
        request_spec = {'num_instances': 1}
        try:
            raise test.TestingException("just need an exception")
        except test.TestingException:
            exc_info = sys.exc_info()
            exc_str = traceback.format_exception_only(exc_info[0],
                                                      exc_info[1])

        self.assertTrue(self._reschedule(filter_properties=filter_properties,
            request_spec=request_spec, exc_info=exc_info))
        self.assertEqual(self.updated_task_state, self.expected_task_state)
        self.assertEqual(exc_str, filter_properties['retry']['exc'])


class InnerTestingException(Exception):
    pass


class ComputeRescheduleResizeOrReraiseTestCase(BaseTestCase):
    """Test logic and exception handling around rescheduling prep resize
    requests
    """
    def setUp(self):
        super(ComputeRescheduleResizeOrReraiseTestCase, self).setUp()
        self.instance = self._create_fake_instance_obj()
        self.instance_uuid = self.instance['uuid']
        self.instance_type = flavors.get_flavor_by_name(
                "m1.tiny")

    @mock.patch.object(db, 'migration_create')
    @mock.patch.object(compute_manager.ComputeManager,
                       '_reschedule_resize_or_reraise')
    def test_reschedule_resize_or_reraise_called(self, mock_res, mock_mig):
        """Verify the rescheduling logic gets called when there is an error
        during prep_resize.
        """
        inst_obj = self._create_fake_instance_obj()
        mock_mig.side_effect = test.TestingException("Original")

        self.compute.prep_resize(self.context, image=None,
                                 instance=inst_obj,
                                 instance_type=self.instance_type,
                                 reservations=[], request_spec={},
                                 filter_properties={}, node=None,
                                 clean_shutdown=True)

        mock_mig.assert_called_once_with(mock.ANY, mock.ANY)
        mock_res.assert_called_once_with(mock.ANY, None, inst_obj, mock.ANY,
                                         self.instance_type, mock.ANY, {}, {})

    @mock.patch.object(compute_manager.ComputeManager, "_reschedule")
    def test_reschedule_fails_with_exception(self, mock_res):
        """Original exception should be raised if the _reschedule method
        raises another exception
        """
        instance = self._create_fake_instance_obj()
        scheduler_hint = dict(filter_properties={})
        method_args = (instance, None, scheduler_hint, self.instance_type,
                       None)
        mock_res.side_effect = InnerTestingException("Inner")

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            self.assertRaises(test.TestingException,
                    self.compute._reschedule_resize_or_reraise, self.context,
                    None, instance, exc_info, self.instance_type,
                    self.none_quotas, {}, {})

            mock_res.assert_called_once_with(
                    self.context, {}, {}, instance,
                    self.compute.compute_task_api.resize_instance, method_args,
                    task_states.RESIZE_PREP, exc_info)

    @mock.patch.object(compute_manager.ComputeManager, "_reschedule")
    def test_reschedule_false(self, mock_res):
        """Original exception should be raised if the resize is not
        rescheduled.
        """
        instance = self._create_fake_instance_obj()
        scheduler_hint = dict(filter_properties={})
        method_args = (instance, None, scheduler_hint, self.instance_type,
                       None)
        mock_res.return_value = False

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            self.assertRaises(test.TestingException,
                    self.compute._reschedule_resize_or_reraise, self.context,
                    None, instance, exc_info, self.instance_type,
                    self.none_quotas, {}, {})

            mock_res.assert_called_once_with(
                self.context, {}, {}, instance,
                self.compute.compute_task_api.resize_instance, method_args,
                task_states.RESIZE_PREP, exc_info)

    @mock.patch.object(compute_manager.ComputeManager, "_reschedule")
    @mock.patch.object(compute_manager.ComputeManager, "_log_original_error")
    def test_reschedule_true(self, mock_log, mock_res):
        # If rescheduled, the original resize exception should be logged.
        instance = self._create_fake_instance_obj()
        scheduler_hint = dict(filter_properties={})
        method_args = (instance, None, scheduler_hint, self.instance_type,
                       None)

        try:
            raise test.TestingException("Original")
        except Exception:
            exc_info = sys.exc_info()
            mock_res.return_value = True

            self.compute._reschedule_resize_or_reraise(
                    self.context, None, instance, exc_info,
                    self.instance_type, self.none_quotas, {}, {})

            mock_res.assert_called_once_with(self.context, {}, {},
                    instance, self.compute.compute_task_api.resize_instance,
                    method_args, task_states.RESIZE_PREP, exc_info)
            mock_log.assert_called_once_with(exc_info, instance.uuid)


class ComputeInactiveImageTestCase(BaseTestCase):
    def setUp(self):
        super(ComputeInactiveImageTestCase, self).setUp()

        def fake_show(meh, context, id, **kwargs):
            return {'id': id, 'name': 'fake_name', 'status': 'deleted',
                    'min_ram': 0, 'min_disk': 0,
                    'properties': {'kernel_id': uuids.kernel_id,
                                   'ramdisk_id': uuids.ramdisk_id,
                                   'something_else': 'meow'}}

        fake_image.stub_out_image_service(self)
        self.stub_out('nova.tests.unit.image.fake._FakeImageService.show',
                      fake_show)
        self.compute_api = compute.API()

    def test_create_instance_with_deleted_image(self):
        # Make sure we can't start an instance with a deleted image.
        inst_type = flavors.get_flavor_by_name('m1.tiny')
        self.assertRaises(exception.ImageNotActive,
                          self.compute_api.create,
                          self.context, inst_type, uuids.image_instance)


class EvacuateHostTestCase(BaseTestCase):
    def setUp(self):
        super(EvacuateHostTestCase, self).setUp()
        self.inst = self._create_fake_instance_obj(
            {'host': 'fake_host_2', 'node': 'fakenode2'})
        self.inst.task_state = task_states.REBUILDING
        self.inst.save()

        def fake_get_compute_info(cls, context, host):
            cn = objects.ComputeNode(hypervisor_hostname=self.rt.nodename)
            return cn

        self.stub_out('nova.compute.manager.ComputeManager._get_compute_info',
                       fake_get_compute_info)
        self.useFixture(fixtures.SpawnIsSynchronousFixture())

    def tearDown(self):
        db.instance_destroy(self.context, self.inst.uuid)
        super(EvacuateHostTestCase, self).tearDown()

    def _rebuild(self, on_shared_storage=True, migration=None,
                 send_node=False):
        network_api = self.compute.network_api
        ctxt = context.get_admin_context()

        node = limits = None
        if send_node:
            node = NODENAME
            limits = {}

        @mock.patch.object(network_api, 'setup_networks_on_host')
        @mock.patch.object(network_api, 'setup_instance_network_on_host')
        @mock.patch('nova.context.RequestContext.elevated', return_value=ctxt)
        def _test_rebuild(mock_context, mock_setup_instance_network_on_host,
                          mock_setup_networks_on_host):
            orig_image_ref = None
            image_ref = None
            injected_files = None
            bdms = objects.BlockDeviceMappingList.get_by_instance_uuid(
                self.context, self.inst.uuid)
            self.compute.rebuild_instance(
                ctxt, self.inst, orig_image_ref,
                image_ref, injected_files, 'newpass', {}, bdms, recreate=True,
                on_shared_storage=on_shared_storage, migration=migration,
                scheduled_node=node, limits=limits)
            mock_setup_networks_on_host.assert_called_once_with(
                ctxt, self.inst, self.inst.host)
            mock_setup_instance_network_on_host.assert_called_once_with(
                ctxt, self.inst, self.inst.host)

        _test_rebuild()

    def test_rebuild_on_host_updated_target(self):
        """Confirm evacuate scenario updates host and node."""
        def fake_get_compute_info(context, host):
            self.assertTrue(context.is_admin)
            self.assertEqual('fake-mini', host)
            cn = objects.ComputeNode(hypervisor_hostname=self.rt.nodename)
            return cn

        with test.nested(
                mock.patch.object(self.compute.driver, 'instance_on_disk',
                                  side_effect=lambda x: True),
                mock.patch.object(self.compute, '_get_compute_info',
                                  side_effect=fake_get_compute_info)
        ) as (mock_inst, mock_get):
            self._rebuild()

            # Should be on destination host
            instance = db.instance_get(self.context, self.inst.id)
            self.assertEqual(instance['host'], self.compute.host)
            self.assertEqual(NODENAME, instance['node'])
            self.assertTrue(mock_inst.called)
            self.assertTrue(mock_get.called)

    def test_rebuild_on_host_updated_target_node_not_found(self):
        """Confirm evacuate scenario where compute_node isn't found."""
        def fake_get_compute_info(context, host):
            raise exception.ComputeHostNotFound(host=host)
        with test.nested(
            mock.patch.object(self.compute.driver, 'instance_on_disk',
                              side_effect=lambda x: True),
            mock.patch.object(self.compute, '_get_compute_info',
                              side_effect=fake_get_compute_info)
        ) as (mock_inst, mock_get):
            self._rebuild()

            # Should be on destination host
            instance = db.instance_get(self.context, self.inst.id)
            self.assertEqual(instance['host'], self.compute.host)
            self.assertIsNone(instance['node'])
            self.assertTrue(mock_inst.called)
            self.assertTrue(mock_get.called)

    def test_rebuild_on_host_node_passed(self):
        patch_get_info = mock.patch.object(self.compute, '_get_compute_info')
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        with patch_get_info as get_compute_info, patch_on_disk:
            self._rebuild(send_node=True)
            self.assertEqual(0, get_compute_info.call_count)

        # Should be on destination host and node set to what was passed in
        instance = db.instance_get(self.context, self.inst.id)
        self.assertEqual(instance['host'], self.compute.host)
        self.assertEqual(instance['node'], NODENAME)

    def test_rebuild_with_instance_in_stopped_state(self):
        """Confirm evacuate scenario updates vm_state to stopped
        if instance is in stopped state
        """
        # Initialize the VM to stopped state
        db.instance_update(self.context, self.inst.uuid,
                           {"vm_state": vm_states.STOPPED})
        self.inst.vm_state = vm_states.STOPPED

        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                      lambda *a, **ka: True)

        self._rebuild()

        # Check the vm state is reset to stopped
        instance = db.instance_get(self.context, self.inst.id)
        self.assertEqual(instance['vm_state'], vm_states.STOPPED)

    def test_rebuild_with_wrong_shared_storage(self):
        """Confirm evacuate scenario does not update host."""
        with mock.patch.object(self.compute.driver, 'instance_on_disk',
                               side_effect=lambda x: True) as mock_inst:
            self.assertRaises(exception.InvalidSharedStorage,
                          lambda: self._rebuild(on_shared_storage=False))

            # Should remain on original host
            instance = db.instance_get(self.context, self.inst.id)
            self.assertEqual(instance['host'], 'fake_host_2')
            self.assertTrue(mock_inst.called)

    @mock.patch.object(cinder.API, 'detach')
    @mock.patch.object(compute_manager.ComputeManager, '_prep_block_device')
    @mock.patch.object(compute_manager.ComputeManager, '_driver_detach_volume')
    def test_rebuild_on_remote_host_with_volumes(self, mock_drv_detach,
                                                 mock_prep, mock_detach):
        """Confirm that the evacuate scenario does not attempt a driver detach
           when rebuilding an instance with volumes on a remote host
        """
        values = {'instance_uuid': self.inst.uuid,
                  'source_type': 'volume',
                  'device_name': '/dev/vdc',
                  'delete_on_termination': False,
                  'volume_id': uuids.volume_id,
                  'connection_info': '{}'}

        db.block_device_mapping_create(self.context, values)

        def fake_volume_get(self, context, volume):
            return {'id': 'fake_volume_id'}
        self.stub_out("nova.volume.cinder.API.get", fake_volume_get)

        # Stub out and record whether it gets detached
        result = {"detached": False}

        def fake_detach(context, volume, instance_uuid, attachment_id):
            result["detached"] = volume == 'fake_volume_id'
        mock_detach.side_effect = fake_detach

        def fake_terminate_connection(self, context, volume, connector):
            return {}
        self.stub_out("nova.volume.cinder.API.terminate_connection",
                      fake_terminate_connection)
        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                      lambda *a, **ka: True)
        self._rebuild()

        # cleanup
        bdms = db.block_device_mapping_get_all_by_instance(self.context,
                                                           self.inst.uuid)
        if not bdms:
            self.fail('BDM entry for the attached volume is missing')
        for bdm in bdms:
            db.block_device_mapping_destroy(self.context, bdm['id'])

        self.assertFalse(mock_drv_detach.called)
        # make sure volumes attach, detach are called
        mock_detach.assert_called_once_with(
            test.MatchType(context.RequestContext),
            mock.ANY, mock.ANY, None)
        mock_prep.assert_called_once_with(
            test.MatchType(context.RequestContext),
            test.MatchType(objects.Instance), mock.ANY)

    @mock.patch.object(fake.FakeDriver, 'spawn')
    def test_rebuild_on_host_with_shared_storage(self, mock_spawn):
        """Confirm evacuate scenario on shared storage."""
        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                      lambda *a, **ka: True)

        self._rebuild()

        mock_spawn.assert_called_once_with(
            test.MatchType(context.RequestContext),
            test.MatchType(objects.Instance),
            test.MatchType(objects.ImageMeta),
            mock.ANY, 'newpass',
            network_info=mock.ANY,
            block_device_info=mock.ANY)

    @mock.patch.object(fake.FakeDriver, 'spawn')
    def test_rebuild_on_host_without_shared_storage(self, mock_spawn):
        """Confirm evacuate scenario without shared storage
        (rebuild from image)
        """
        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                      lambda *a, **ka: False)

        self._rebuild(on_shared_storage=False)

        mock_spawn.assert_called_once_with(
            test.MatchType(context.RequestContext),
            test.MatchType(objects.Instance),
            test.MatchType(objects.ImageMeta),
            mock.ANY, 'newpass',
            network_info=mock.ANY,
            block_device_info=mock.ANY)

    def test_rebuild_on_host_instance_exists(self):
        """Rebuild if instance exists raises an exception."""
        db.instance_update(self.context, self.inst.uuid,
                           {"task_state": task_states.SCHEDULING})
        self.compute.build_and_run_instance(self.context,
                self.inst, {}, {}, {}, block_device_mapping=[])

        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                       lambda *a, **kw: True)
        self.assertRaises(exception.InstanceExists,
                          lambda: self._rebuild(on_shared_storage=True))

    def test_driver_does_not_support_recreate(self):
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_recreate=False):
            self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                           lambda *a, **kw: True)
            self.assertRaises(exception.InstanceRecreateNotSupported,
                              lambda: self._rebuild(on_shared_storage=True))

    @mock.patch.object(fake.FakeDriver, 'spawn')
    @mock.patch('nova.objects.ImageMeta.from_image_ref')
    def test_on_shared_storage_not_provided_host_without_shared_storage(self,
            mock_image_meta, mock_spawn):
        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                       lambda *a, **ka: False)

        self._rebuild(on_shared_storage=None)

        # 'spawn' should be called with the image_meta from the image_ref
        mock_spawn.assert_called_once_with(
            test.MatchType(context.RequestContext),
            test.MatchType(objects.Instance),
            mock_image_meta.return_value,
            mock.ANY, 'newpass',
            network_info=mock.ANY,
            block_device_info=mock.ANY)

    @mock.patch.object(fake.FakeDriver, 'spawn')
    @mock.patch('nova.objects.Instance.image_meta',
                new_callable=mock.PropertyMock)
    def test_on_shared_storage_not_provided_host_with_shared_storage(self,
            mock_image_meta, mock_spawn):
        self.stub_out('nova.virt.fake.FakeDriver.instance_on_disk',
                      lambda *a, **ka: True)

        self._rebuild(on_shared_storage=None)

        mock_spawn.assert_called_once_with(
            test.MatchType(context.RequestContext),
            test.MatchType(objects.Instance),
            mock_image_meta.return_value,
            mock.ANY, 'newpass',
            network_info=mock.ANY,
            block_device_info=mock.ANY)

    def test_rebuild_migration_passed_in(self):
        migration = mock.Mock(spec=objects.Migration)

        patch_spawn = mock.patch.object(self.compute.driver, 'spawn')
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        with patch_spawn, patch_on_disk:
            self._rebuild(migration=migration)

        self.assertEqual('done', migration.status)
        migration.save.assert_called_once_with()

    def test_rebuild_migration_node_passed_in(self):
        patch_spawn = mock.patch.object(self.compute.driver, 'spawn')
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        with patch_spawn, patch_on_disk:
            self._rebuild(send_node=True)

        migrations = objects.MigrationList.get_in_progress_by_host_and_node(
            self.context, self.compute.host, NODENAME)
        self.assertEqual(1, len(migrations))
        migration = migrations[0]
        self.assertEqual("evacuation", migration.migration_type)
        self.assertEqual("pre-migrating", migration.status)

    def test_rebuild_migration_claim_fails(self):
        migration = mock.Mock(spec=objects.Migration)

        patch_spawn = mock.patch.object(self.compute.driver, 'spawn')
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        patch_claim = mock.patch.object(
            self.compute._resource_tracker_dict[NODENAME], 'rebuild_claim',
            side_effect=exception.ComputeResourcesUnavailable(reason="boom"))
        with patch_spawn, patch_on_disk, patch_claim:
            self.assertRaises(exception.BuildAbortException,
                              self._rebuild, migration=migration,
                              send_node=True)
        self.assertEqual("failed", migration.status)
        migration.save.assert_called_once_with()

    def test_rebuild_fails_migration_failed(self):
        migration = mock.Mock(spec=objects.Migration)

        patch_spawn = mock.patch.object(self.compute.driver, 'spawn')
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        patch_claim = mock.patch.object(
            self.compute._resource_tracker_dict[NODENAME], 'rebuild_claim')
        patch_rebuild = mock.patch.object(
            self.compute, '_do_rebuild_instance_with_claim',
            side_effect=test.TestingException())
        with patch_spawn, patch_on_disk, patch_claim, patch_rebuild:
            self.assertRaises(test.TestingException,
                              self._rebuild, migration=migration,
                              send_node=True)
        self.assertEqual("failed", migration.status)
        migration.save.assert_called_once_with()

    def test_rebuild_numa_migration_context_honoured(self):
        numa_topology = (
            test_instance_numa_topology.get_fake_obj_numa_topology(
                self.context))

        # NOTE(ndipanov): Make sure that we pass the topology from the context
        def fake_spawn(context, instance, image_meta, injected_files,
                       admin_password, network_info=None,
                       block_device_info=None):
            self.assertIsNone(instance.numa_topology)

        self.inst.numa_topology = numa_topology
        patch_spawn = mock.patch.object(self.compute.driver, 'spawn',
                                        side_effect=fake_spawn)
        patch_on_disk = mock.patch.object(
            self.compute.driver, 'instance_on_disk', return_value=True)
        with patch_spawn, patch_on_disk:
            self._rebuild(send_node=True)
        self.assertIsNone(self.inst.numa_topology)
        self.assertIsNone(self.inst.migration_context)


class ComputeInjectedFilesTestCase(BaseTestCase):
    # Test that running instances with injected_files decodes files correctly

    def setUp(self):
        super(ComputeInjectedFilesTestCase, self).setUp()
        self.instance = self._create_fake_instance_obj()
        self.stub_out('nova.virt.fake.FakeDriver.spawn', self._spawn)
        self.useFixture(fixtures.SpawnIsSynchronousFixture())

    def _spawn(self, context, instance, image_meta, injected_files,
               admin_password, nw_info, block_device_info, db_api=None):
        self.assertEqual(self.expected, injected_files)

    def _test(self, injected_files, decoded_files):
        self.expected = decoded_files
        self.compute.build_and_run_instance(self.context, self.instance, {},
                                            {}, {}, block_device_mapping=[],
                                            injected_files=injected_files)

    def test_injected_none(self):
        # test an input of None for injected_files
        self._test(None, [])

    def test_injected_empty(self):
        # test an input of [] for injected_files
        self._test([], [])

    def test_injected_success(self):
        # test with valid b64 encoded content.
        injected_files = [
            ('/a/b/c', base64.b64encode(b'foobarbaz')),
            ('/d/e/f', base64.b64encode(b'seespotrun')),
        ]

        decoded_files = [
            ('/a/b/c', 'foobarbaz'),
            ('/d/e/f', 'seespotrun'),
        ]
        self._test(injected_files, decoded_files)

    def test_injected_invalid(self):
        # test with invalid b64 encoded content
        injected_files = [
            ('/a/b/c', base64.b64encode(b'foobarbaz')),
            ('/d/e/f', 'seespotrun'),
        ]

        self.assertRaises(exception.Base64Exception,
                self.compute.build_and_run_instance,
                self.context, self.instance, {}, {}, {},
                          block_device_mapping=[],
                          injected_files=injected_files)


class CheckConfigDriveTestCase(test.NoDBTestCase):
    # NOTE(sirp): `TestCase` is far too heavyweight for this test, this should
    # probably derive from a `test.FastTestCase` that omits DB and env
    # handling
    def setUp(self):
        super(CheckConfigDriveTestCase, self).setUp()
        self.compute_api = compute.API()

    def _assertCheck(self, expected, config_drive):
        self.assertEqual(expected,
                         self.compute_api._check_config_drive(config_drive))

    def _assertInvalid(self, config_drive):
        self.assertRaises(exception.ConfigDriveInvalidValue,
                          self.compute_api._check_config_drive,
                          config_drive)

    def test_config_drive_false_values(self):
        self._assertCheck('', None)
        self._assertCheck('', '')
        self._assertCheck('', 'False')
        self._assertCheck('', 'f')
        self._assertCheck('', '0')

    def test_config_drive_true_values(self):
        self._assertCheck(True, 'True')
        self._assertCheck(True, 't')
        self._assertCheck(True, '1')

    def test_config_drive_bogus_values_raise(self):
        self._assertInvalid('asd')
        self._assertInvalid(uuidutils.generate_uuid())


class CheckRequestedImageTestCase(test.TestCase):
    def setUp(self):
        super(CheckRequestedImageTestCase, self).setUp()
        self.compute_api = compute.API()
        self.context = context.RequestContext(
                'fake_user_id', 'fake_project_id')

        self.instance_type = flavors.get_default_flavor()
        self.instance_type['memory_mb'] = 64
        self.instance_type['root_gb'] = 1

    def test_no_image_specified(self):
        self.compute_api._check_requested_image(self.context, None, None,
                self.instance_type, None)

    def test_image_status_must_be_active(self):
        image = dict(id='123', status='foo')

        self.assertRaises(exception.ImageNotActive,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type, None)

        image['status'] = 'active'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_image_min_ram_check(self):
        image = dict(id='123', status='active', min_ram='65')

        self.assertRaises(exception.FlavorMemoryTooSmall,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type, None)

        image['min_ram'] = '64'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_image_min_disk_check(self):
        image = dict(id='123', status='active', min_disk='2')

        self.assertRaises(exception.FlavorDiskSmallerThanMinDisk,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type, None)

        image['min_disk'] = '1'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_image_too_large(self):
        image = dict(id='123', status='active', size='1073741825')

        self.assertRaises(exception.FlavorDiskSmallerThanImage,
                self.compute_api._check_requested_image, self.context,
                image['id'], image, self.instance_type, None)

        image['size'] = '1073741824'
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_root_gb_zero_disables_size_check(self):
        self.instance_type['root_gb'] = 0
        image = dict(id='123', status='active', size='1073741825')

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_root_gb_zero_disables_min_disk(self):
        self.instance_type['root_gb'] = 0
        image = dict(id='123', status='active', min_disk='2')

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)

    def test_config_drive_option(self):
        image = {'id': 1, 'status': 'active'}
        image['properties'] = {'img_config_drive': 'optional'}
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)
        image['properties'] = {'img_config_drive': 'mandatory'}
        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, None)
        image['properties'] = {'img_config_drive': 'bar'}
        self.assertRaises(exception.InvalidImageConfigDrive,
                          self.compute_api._check_requested_image,
                          self.context, image['id'], image, self.instance_type,
                          None)

    def test_volume_blockdevicemapping(self):
        # We should allow a root volume which is larger than the flavor root
        # disk.
        # We should allow a root volume created from an image whose min_disk is
        # larger than the flavor root disk.
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=self.instance_type.root_gb * units.Gi,
                     min_disk=self.instance_type.root_gb + 1)

        volume_uuid = str(uuid.uuid4())
        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='volume', destination_type='volume',
            volume_id=volume_uuid, volume_size=self.instance_type.root_gb + 1)

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, root_bdm)

    def test_volume_blockdevicemapping_min_disk(self):
        # A bdm object volume smaller than the image's min_disk should not be
        # allowed
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=self.instance_type.root_gb * units.Gi,
                     min_disk=self.instance_type.root_gb + 1)

        volume_uuid = str(uuid.uuid4())
        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='image', destination_type='volume',
            image_id=image_uuid, volume_id=volume_uuid,
            volume_size=self.instance_type.root_gb)

        self.assertRaises(exception.VolumeSmallerThanMinDisk,
                          self.compute_api._check_requested_image,
                          self.context, image_uuid, image, self.instance_type,
                          root_bdm)

    def test_volume_blockdevicemapping_min_disk_no_size(self):
        # We should allow a root volume whose size is not given
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=self.instance_type.root_gb * units.Gi,
                     min_disk=self.instance_type.root_gb)

        volume_uuid = str(uuid.uuid4())
        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='volume', destination_type='volume',
            volume_id=volume_uuid, volume_size=None)

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, root_bdm)

    def test_image_blockdevicemapping(self):
        # Test that we can succeed when passing bdms, and the root bdm isn't a
        # volume
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=self.instance_type.root_gb * units.Gi, min_disk=0)

        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='image', destination_type='local', image_id=image_uuid)

        self.compute_api._check_requested_image(self.context, image['id'],
                image, self.instance_type, root_bdm)

    def test_image_blockdevicemapping_too_big(self):
        # We should do a size check against flavor if we were passed bdms but
        # the root bdm isn't a volume
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=(self.instance_type.root_gb + 1) * units.Gi,
                     min_disk=0)

        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='image', destination_type='local', image_id=image_uuid)

        self.assertRaises(exception.FlavorDiskSmallerThanImage,
                          self.compute_api._check_requested_image,
                          self.context, image['id'],
                          image, self.instance_type, root_bdm)

    def test_image_blockdevicemapping_min_disk(self):
        # We should do a min_disk check against flavor if we were passed bdms
        # but the root bdm isn't a volume
        image_uuid = str(uuid.uuid4())
        image = dict(id=image_uuid, status='active',
                     size=0, min_disk=self.instance_type.root_gb + 1)

        root_bdm = block_device_obj.BlockDeviceMapping(
            source_type='image', destination_type='local', image_id=image_uuid)

        self.assertRaises(exception.FlavorDiskSmallerThanMinDisk,
                          self.compute_api._check_requested_image,
                          self.context, image['id'],
                          image, self.instance_type, root_bdm)


class ComputeHooksTestCase(test.BaseHookTestCase):
    def test_delete_instance_has_hook(self):
        delete_func = compute_manager.ComputeManager._delete_instance
        self.assert_has_hook('delete_instance', delete_func)

    def test_create_instance_has_hook(self):
        create_func = compute_api.API.create
        self.assert_has_hook('create_instance', create_func)

    def test_build_instance_has_hook(self):
        build_instance_func = (compute_manager.ComputeManager.
                               _do_build_and_run_instance)
        self.assert_has_hook('build_instance', build_instance_func)
