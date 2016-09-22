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

"""Unit tests for ComputeManager()."""

import datetime
import time
import uuid

from cinderclient import exceptions as cinder_exception
from eventlet import event as eventlet_event
import mock
import netaddr
import oslo_messaging as messaging
from oslo_serialization import jsonutils
from oslo_utils import importutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
import six

import nova
from nova.compute import build_results
from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import utils as compute_utils
from nova.compute import vm_states
from nova.conductor import api as conductor_api
import nova.conf
from nova import context
from nova import db
from nova import exception
from nova.network import api as network_api
from nova.network import model as network_model
from nova import objects
from nova.objects import block_device as block_device_obj
from nova.objects import instance as instance_obj
from nova.objects import migrate_data as migrate_data_obj
from nova.objects import network_request as net_req_obj
from nova import test
from nova.tests import fixtures
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit.compute import fake_resource_tracker
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_flavor
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_network
from nova.tests.unit import fake_network_cache_model
from nova.tests.unit.objects import test_instance_fault
from nova.tests.unit.objects import test_instance_info_cache
from nova.tests import uuidsentinel as uuids
from nova import utils
from nova.virt import driver as virt_driver
from nova.virt import event as virtevent
from nova.virt import fake as fake_driver
from nova.virt import hardware


CONF = nova.conf.CONF


class ComputeManagerUnitTestCase(test.NoDBTestCase):
    def setUp(self):
        super(ComputeManagerUnitTestCase, self).setUp()
        self.flags(use_local=True, group='conductor')
        self.compute = importutils.import_object(CONF.compute_manager)
        self.context = context.RequestContext(fakes.FAKE_USER_ID,
                                              fakes.FAKE_PROJECT_ID)

        self.useFixture(fixtures.SpawnIsSynchronousFixture())

    @mock.patch.object(manager.ComputeManager, '_get_power_state')
    @mock.patch.object(manager.ComputeManager, '_sync_instance_power_state')
    @mock.patch.object(objects.Instance, 'get_by_uuid')
    def _test_handle_lifecycle_event(self, mock_get, mock_sync,
                                     mock_get_power_state, transition,
                                     event_pwr_state, current_pwr_state):
        event = mock.Mock()
        event.get_instance_uuid.return_value = mock.sentinel.uuid
        event.get_transition.return_value = transition
        mock_get_power_state.return_value = current_pwr_state

        self.compute.handle_lifecycle_event(event)

        mock_get.assert_called_with(mock.ANY, mock.sentinel.uuid,
                                    expected_attrs=[])
        if event_pwr_state == current_pwr_state:
            mock_sync.assert_called_with(mock.ANY, mock_get.return_value,
                                         event_pwr_state)
        else:
            self.assertFalse(mock_sync.called)

    def test_handle_lifecycle_event(self):
        event_map = {virtevent.EVENT_LIFECYCLE_STOPPED: power_state.SHUTDOWN,
                     virtevent.EVENT_LIFECYCLE_STARTED: power_state.RUNNING,
                     virtevent.EVENT_LIFECYCLE_PAUSED: power_state.PAUSED,
                     virtevent.EVENT_LIFECYCLE_RESUMED: power_state.RUNNING,
                     virtevent.EVENT_LIFECYCLE_SUSPENDED:
                         power_state.SUSPENDED,
        }

        for transition, pwr_state in six.iteritems(event_map):
            self._test_handle_lifecycle_event(transition=transition,
                                              event_pwr_state=pwr_state,
                                              current_pwr_state=pwr_state)

    def test_handle_lifecycle_event_state_mismatch(self):
        self._test_handle_lifecycle_event(
            transition=virtevent.EVENT_LIFECYCLE_STOPPED,
            event_pwr_state=power_state.SHUTDOWN,
            current_pwr_state=power_state.RUNNING)

    @mock.patch('nova.compute.utils.notify_about_instance_action')
    def test_delete_instance_info_cache_delete_ordering(self, mock_notify):
        call_tracker = mock.Mock()
        call_tracker.clear_events_for_instance.return_value = None
        mgr_class = self.compute.__class__
        orig_delete = mgr_class._delete_instance
        specd_compute = mock.create_autospec(mgr_class)
        # spec out everything except for the method we really want
        # to test, then use call_tracker to verify call sequence
        specd_compute._delete_instance = orig_delete
        specd_compute.host = 'compute'

        mock_inst = mock.Mock()
        mock_inst.uuid = uuids.instance
        mock_inst.save = mock.Mock()
        mock_inst.destroy = mock.Mock()
        mock_inst.system_metadata = mock.Mock()

        def _mark_notify(*args, **kwargs):
            call_tracker._notify_about_instance_usage(*args, **kwargs)

        def _mark_shutdown(*args, **kwargs):
            call_tracker._shutdown_instance(*args, **kwargs)

        specd_compute.instance_events = call_tracker
        specd_compute._notify_about_instance_usage = _mark_notify
        specd_compute._shutdown_instance = _mark_shutdown
        mock_inst.info_cache = call_tracker

        specd_compute._delete_instance(specd_compute,
                                       self.context,
                                       mock_inst,
                                       mock.Mock(),
                                       mock.Mock())

        methods_called = [n for n, a, k in call_tracker.mock_calls]
        self.assertEqual(['clear_events_for_instance',
                          '_notify_about_instance_usage',
                          '_shutdown_instance', 'delete'],
                         methods_called)
        mock_notify.assert_called_once_with(self.context,
                                            mock_inst,
                                            specd_compute.host,
                                            action='delete',
                                            phase='start')

    def _make_compute_node(self, hyp_hostname, cn_id):
            cn = mock.Mock(spec_set=['hypervisor_hostname', 'id',
                                     'destroy'])
            cn.id = cn_id
            cn.hypervisor_hostname = hyp_hostname
            return cn

    def _make_rt(self, node):
            n = mock.Mock(spec_set=['update_available_resource',
                                    'nodename'])
            n.nodename = node
            return n

    @mock.patch.object(manager.ComputeManager, '_get_resource_tracker')
    @mock.patch.object(fake_driver.FakeDriver, 'get_available_nodes')
    @mock.patch.object(manager.ComputeManager, '_get_compute_nodes_in_db')
    def test_update_available_resource_for_node(
        self, get_db_nodes, get_avail_nodes, get_rt):
        db_nodes = []

        db_nodes = [self._make_compute_node('node%s' % i, i)
                    for i in range(1, 5)]
        avail_nodes = set(['node2', 'node3', 'node4', 'node5'])
        avail_nodes_l = list(avail_nodes)
        rts = [self._make_rt(node) for node in avail_nodes_l]
        # Make the 2nd and 3rd ones raise
        exc = exception.ComputeHostNotFound(host=uuids.fake_host)
        rts[1].update_available_resource.side_effect = exc
        exc = test.TestingException()
        rts[2].update_available_resource.side_effect = exc
        get_db_nodes.return_value = db_nodes
        get_avail_nodes.return_value = avail_nodes
        get_rt.side_effect = rts

        self.compute.update_available_resource_for_node(self.context,
                                                        avail_nodes_l[0])
        self.assertEqual(self.compute._resource_tracker_dict[avail_nodes_l[0]],
                         rts[0])

        # Update ComputeHostNotFound
        self.compute.update_available_resource_for_node(self.context,
                                                        avail_nodes_l[1])
        self.assertNotIn(self.compute._resource_tracker_dict, avail_nodes_l[1])

        # Update TestException
        self.compute.update_available_resource_for_node(self.context,
                                                        avail_nodes_l[2])
        self.assertEqual(self.compute._resource_tracker_dict[
            avail_nodes_l[2]], rts[2])

    @mock.patch.object(manager.ComputeManager, '_get_resource_tracker')
    @mock.patch.object(fake_driver.FakeDriver, 'get_available_nodes')
    @mock.patch.object(manager.ComputeManager, '_get_compute_nodes_in_db')
    def test_update_available_resource(self, get_db_nodes, get_avail_nodes,
                                       get_rt):
        db_nodes = [self._make_compute_node('node%s' % i, i)
                    for i in range(1, 5)]
        avail_nodes = set(['node2', 'node3', 'node4', 'node5'])
        avail_nodes_l = list(avail_nodes)
        rts = [self._make_rt(node) for node in avail_nodes_l]
        # Make the 2nd and 3rd ones raise
        exc = exception.ComputeHostNotFound(host='fake')
        rts[1].update_available_resource.side_effect = exc
        exc = test.TestingException()
        rts[2].update_available_resource.side_effect = exc

        expected_rt_dict = {avail_nodes_l[0]: rts[0],
                            avail_nodes_l[2]: rts[2],
                            avail_nodes_l[3]: rts[3]}
        get_db_nodes.return_value = db_nodes
        get_avail_nodes.return_value = avail_nodes
        get_rt.side_effect = rts
        self.compute.update_available_resource(self.context)
        get_db_nodes.assert_called_once_with(self.context, use_slave=True)
        self.assertEqual(sorted([mock.call(node) for node in avail_nodes]),
                         sorted(get_rt.call_args_list))
        for rt in rts:
            rt.update_available_resource.assert_called_once_with(self.context)
        self.assertEqual(expected_rt_dict,
                         self.compute._resource_tracker_dict)
        # First node in set should have been removed from DB
        for db_node in db_nodes:
            if db_node.hypervisor_hostname == 'node1':
                db_node.destroy.assert_called_once_with()
            else:
                self.assertFalse(db_node.destroy.called)

    @mock.patch('nova.compute.utils.notify_about_instance_action')
    def test_delete_instance_without_info_cache(self, mock_notify):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_states.ERROR,
                host=self.compute.host,
                expected_attrs=['system_metadata'])
        quotas = mock.create_autospec(objects.Quotas, spec_set=True)

        with test.nested(
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute, '_shutdown_instance'),
            mock.patch.object(instance, 'obj_load_attr'),
            mock.patch.object(instance, 'save'),
            mock.patch.object(instance, 'destroy')
        ) as (
            compute_notify_about_instance_usage, compute_shutdown_instance,
            instance_obj_load_attr, instance_save, instance_destroy
        ):
            instance.info_cache = None
            self.compute._delete_instance(self.context, instance, [], quotas)

        mock_notify.assert_has_calls([
            mock.call(self.context, instance, 'fake-mini',
                      action='delete', phase='start'),
            mock.call(self.context, instance, 'fake-mini',
                      action='delete', phase='end')])

    def test_check_device_tagging_no_tagging(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       instance_uuid=uuids.instance)])
        net_req = net_req_obj.NetworkRequest(tag=None)
        net_req_list = net_req_obj.NetworkRequestList(objects=[net_req])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=False):
            self.compute._check_device_tagging(net_req_list, bdms)

    def test_check_device_tagging_no_networks(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       instance_uuid=uuids.instance)])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=False):
            self.compute._check_device_tagging(None, bdms)

    def test_check_device_tagging_tagged_net_req_no_virt_support(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       instance_uuid=uuids.instance)])
        net_req = net_req_obj.NetworkRequest(port_id=uuids.bar, tag='foo')
        net_req_list = net_req_obj.NetworkRequestList(objects=[net_req])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=False):
            self.assertRaises(exception.BuildAbortException,
                              self.compute._check_device_tagging,
                              net_req_list, bdms)

    def test_check_device_tagging_tagged_bdm_no_driver_support(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       tag='foo',
                                       instance_uuid=uuids.instance)])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=False):
            self.assertRaises(exception.BuildAbortException,
                              self.compute._check_device_tagging,
                              None, bdms)

    def test_check_device_tagging_tagged_bdm_no_driver_support_declared(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       tag='foo',
                                       instance_uuid=uuids.instance)])
        with mock.patch.dict(self.compute.driver.capabilities):
            self.compute.driver.capabilities.pop('supports_device_tagging',
                                                 None)
            self.assertRaises(exception.BuildAbortException,
                              self.compute._check_device_tagging,
                              None, bdms)

    def test_check_device_tagging_tagged_bdm_with_driver_support(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       tag='foo',
                                       instance_uuid=uuids.instance)])
        net_req = net_req_obj.NetworkRequest(network_id=uuids.bar)
        net_req_list = net_req_obj.NetworkRequestList(objects=[net_req])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=True):
            self.compute._check_device_tagging(net_req_list, bdms)

    def test_check_device_tagging_tagged_net_req_with_driver_support(self):
        bdms = objects.BlockDeviceMappingList(objects=[
            objects.BlockDeviceMapping(source_type='volume',
                                       destination_type='volume',
                                       instance_uuid=uuids.instance)])
        net_req = net_req_obj.NetworkRequest(network_id=uuids.bar, tag='foo')
        net_req_list = net_req_obj.NetworkRequestList(objects=[net_req])
        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_device_tagging=True):
            self.compute._check_device_tagging(net_req_list, bdms)

    @mock.patch.object(network_api.API, 'allocate_for_instance')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(time, 'sleep')
    def test_allocate_network_succeeds_after_retries(
            self, mock_sleep, mock_save, mock_allocate_for_instance):
        self.flags(network_allocate_retries=8)

        instance = fake_instance.fake_instance_obj(
                       self.context, expected_attrs=['system_metadata'])

        is_vpn = 'fake-is-vpn'
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='fake')])
        macs = 'fake-macs'
        sec_groups = 'fake-sec-groups'
        final_result = 'meow'
        dhcp_options = None

        mock_allocate_for_instance.side_effect = [
            test.TestingException()] * 7 + [final_result]

        expected_sleep_times = [1, 2, 4, 8, 16, 30, 30, 30]

        res = self.compute._allocate_network_async(self.context, instance,
                                                   req_networks,
                                                   macs,
                                                   sec_groups,
                                                   is_vpn,
                                                   dhcp_options)

        mock_sleep.has_calls(expected_sleep_times)
        self.assertEqual(final_result, res)
        # Ensure save is not called in while allocating networks, the instance
        # is saved after the allocation.
        self.assertFalse(mock_save.called)
        self.assertEqual('True', instance.system_metadata['network_allocated'])

    @mock.patch.object(network_api.API, 'allocate_for_instance')
    def test_allocate_network_fails(self, mock_allocate):
        self.flags(network_allocate_retries=0)

        mock_allocate.side_effect = test.TestingException

        instance = {}
        is_vpn = 'fake-is-vpn'
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='fake')])
        macs = 'fake-macs'
        sec_groups = 'fake-sec-groups'
        dhcp_options = None

        self.assertRaises(test.TestingException,
                          self.compute._allocate_network_async,
                          self.context, instance, req_networks, macs,
                          sec_groups, is_vpn, dhcp_options)

        mock_allocate.assert_called_once_with(
            self.context, instance, vpn=is_vpn,
            requested_networks=req_networks, macs=macs,
            security_groups=sec_groups,
            dhcp_options=dhcp_options,
            bind_host_id=instance.get('host'))

    @mock.patch.object(network_api.API, 'allocate_for_instance')
    @mock.patch.object(manager.ComputeManager, '_instance_update')
    @mock.patch.object(time, 'sleep')
    def test_allocate_network_with_conf_value_is_one(
            self, sleep, _instance_update, allocate_for_instance):
        self.flags(network_allocate_retries=1)

        instance = fake_instance.fake_instance_obj(
            self.context, expected_attrs=['system_metadata'])
        is_vpn = 'fake-is-vpn'
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='fake')])
        macs = 'fake-macs'
        sec_groups = 'fake-sec-groups'
        dhcp_options = None
        final_result = 'zhangtralon'

        allocate_for_instance.side_effect = [test.TestingException(),
                                             final_result]
        res = self.compute._allocate_network_async(self.context, instance,
                                                   req_networks,
                                                   macs,
                                                   sec_groups,
                                                   is_vpn,
                                                   dhcp_options)
        self.assertEqual(final_result, res)
        self.assertEqual(1, sleep.call_count)

    def test_allocate_network_skip_for_no_allocate(self):
        # Ensures that we don't do anything if requested_networks has 'none'
        # for the network_id.
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='none')])
        nwinfo = self.compute._allocate_network_async(
            self.context, mock.sentinel.instance, req_networks, macs=None,
            security_groups=['default'], is_vpn=False, dhcp_options=None)
        self.assertEqual(0, len(nwinfo))

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_do_build_and_run_instance')
    def _test_max_concurrent_builds(self, mock_dbari):

        with mock.patch.object(self.compute,
                               '_build_semaphore') as mock_sem:
            instance = objects.Instance(uuid=str(uuid.uuid4()))
            for i in (1, 2, 3):
                self.compute.build_and_run_instance(self.context, instance,
                                                    mock.sentinel.image,
                                                    mock.sentinel.request_spec,
                                                    {})
            self.assertEqual(3, mock_sem.__enter__.call_count)

    def test_max_concurrent_builds_limited(self):
        self.flags(max_concurrent_builds=2)
        self._test_max_concurrent_builds()

    def test_max_concurrent_builds_unlimited(self):
        self.flags(max_concurrent_builds=0)
        self._test_max_concurrent_builds()

    def test_max_concurrent_builds_semaphore_limited(self):
        self.flags(max_concurrent_builds=123)
        self.assertEqual(123,
                         manager.ComputeManager()._build_semaphore.balance)

    def test_max_concurrent_builds_semaphore_unlimited(self):
        self.flags(max_concurrent_builds=0)
        compute = manager.ComputeManager()
        self.assertEqual(0, compute._build_semaphore.balance)
        self.assertIsInstance(compute._build_semaphore,
                              compute_utils.UnlimitedSemaphore)

    def test_nil_out_inst_obj_host_and_node_sets_nil(self):
        instance = fake_instance.fake_instance_obj(self.context,
                                                   uuid=uuids.instance,
                                                   host='foo-host',
                                                   node='foo-node')
        self.assertIsNotNone(instance.host)
        self.assertIsNotNone(instance.node)
        self.compute._nil_out_instance_obj_host_and_node(instance)
        self.assertIsNone(instance.host)
        self.assertIsNone(instance.node)

    def test_init_host(self):
        our_host = self.compute.host
        inst = fake_instance.fake_db_instance(
                vm_state=vm_states.ACTIVE,
                info_cache=dict(test_instance_info_cache.fake_info_cache,
                                network_info=None),
                security_groups=None)
        startup_instances = [inst, inst, inst]

        def _make_instance_list(db_list):
            return instance_obj._make_instance_list(
                    self.context, objects.InstanceList(), db_list, None)

        @mock.patch.object(fake_driver.FakeDriver, 'init_host')
        @mock.patch.object(fake_driver.FakeDriver, 'filter_defer_apply_on')
        @mock.patch.object(fake_driver.FakeDriver, 'filter_defer_apply_off')
        @mock.patch.object(objects.InstanceList, 'get_by_host')
        @mock.patch.object(context, 'get_admin_context')
        @mock.patch.object(manager.ComputeManager,
                           '_destroy_evacuated_instances')
        @mock.patch.object(manager.ComputeManager, '_init_instance')
        def _do_mock_calls(mock_inst_init,
                           mock_destroy, mock_admin_ctxt, mock_host_get,
                           mock_filter_off, mock_filter_on, mock_init_host,
                           defer_iptables_apply):
            mock_admin_ctxt.return_value = self.context
            inst_list = _make_instance_list(startup_instances)
            mock_host_get.return_value = inst_list

            self.compute.init_host()

            if defer_iptables_apply:
                self.assertTrue(mock_filter_on.called)
            mock_destroy.assert_called_once_with(self.context)
            mock_inst_init.assert_has_calls(
                [mock.call(self.context, inst_list[0]),
                 mock.call(self.context, inst_list[1]),
                 mock.call(self.context, inst_list[2])])

            if defer_iptables_apply:
                self.assertTrue(mock_filter_off.called)
            mock_init_host.assert_called_once_with(host=our_host)
            mock_host_get.assert_called_once_with(self.context, our_host,
                                    expected_attrs=['info_cache', 'metadata'])

        # Test with defer_iptables_apply
        self.flags(defer_iptables_apply=True)
        _do_mock_calls(defer_iptables_apply=True)

        # Test without defer_iptables_apply
        self.flags(defer_iptables_apply=False)
        _do_mock_calls(defer_iptables_apply=False)

    @mock.patch('nova.objects.InstanceList')
    @mock.patch('nova.objects.MigrationList.get_by_filters')
    def test_cleanup_host(self, mock_miglist_get, mock_instance_list):
        # just testing whether the cleanup_host method
        # when fired will invoke the underlying driver's
        # equivalent method.

        mock_miglist_get.return_value = []
        mock_instance_list.get_by_host.return_value = []

        with mock.patch.object(self.compute, 'driver') as mock_driver:
            self.compute.init_host()
            mock_driver.init_host.assert_called_once_with(host='fake-mini')

            self.compute.cleanup_host()
            # register_event_listener is called on startup (init_host) and
            # in cleanup_host
            mock_driver.register_event_listener.assert_has_calls([
                mock.call(self.compute.handle_events), mock.call(None)])
            mock_driver.cleanup_host.assert_called_once_with(host='fake-mini')

    def test_init_virt_events_disabled(self):
        self.flags(handle_virt_lifecycle_events=False, group='workarounds')
        with mock.patch.object(self.compute.driver,
                               'register_event_listener') as mock_register:
            self.compute.init_virt_events()
        self.assertFalse(mock_register.called)

    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    @mock.patch.object(manager.ComputeManager, '_get_instances_on_driver')
    @mock.patch.object(manager.ComputeManager, 'init_virt_events')
    @mock.patch.object(context, 'get_admin_context')
    @mock.patch.object(objects.InstanceList, 'get_by_host')
    @mock.patch.object(fake_driver.FakeDriver, 'destroy')
    @mock.patch.object(fake_driver.FakeDriver, 'init_host')
    @mock.patch('nova.objects.MigrationList.get_by_filters')
    @mock.patch('nova.objects.Migration.save')
    def test_init_host_with_evacuated_instance(self, mock_save, mock_mig_get,
            mock_init_host, mock_destroy, mock_host_get, mock_admin_ctxt,
            mock_init_virt, mock_get_inst, mock_get_net):
        our_host = self.compute.host
        not_our_host = 'not-' + our_host

        deleted_instance = fake_instance.fake_instance_obj(
                self.context, host=not_our_host, uuid=uuids.deleted_instance)
        migration = objects.Migration(instance_uuid=deleted_instance.uuid)
        mock_mig_get.return_value = [migration]
        mock_admin_ctxt.return_value = self.context
        mock_host_get.return_value = objects.InstanceList()

        # simulate failed instance
        mock_get_inst.return_value = [deleted_instance]
        mock_get_net.side_effect = exception.InstanceNotFound(
            instance_id=deleted_instance['uuid'])

        self.compute.init_host()

        mock_init_host.assert_called_once_with(host=our_host)
        mock_host_get.assert_called_once_with(self.context, our_host,
                                expected_attrs=['info_cache', 'metadata'])
        mock_init_virt.assert_called_once_with()
        mock_get_inst.assert_called_once_with(self.context, {'deleted': False})
        mock_get_net.assert_called_once_with(self.context, deleted_instance)

        # ensure driver.destroy is called so that driver may
        # clean up any dangling files
        mock_destroy.assert_called_once_with(self.context, deleted_instance,
                                             mock.ANY, mock.ANY, mock.ANY)
        mock_save.assert_called_once_with()

    def test_init_instance_with_binding_failed_vif_type(self):
        # this instance will plug a 'binding_failed' vif
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                info_cache=None,
                power_state=power_state.RUNNING,
                vm_state=vm_states.ACTIVE,
                task_state=None,
                host=self.compute.host,
                expected_attrs=['info_cache'])

        with test.nested(
            mock.patch.object(context, 'get_admin_context',
                return_value=self.context),
            mock.patch.object(compute_utils, 'get_nw_info_for_instance',
                return_value=network_model.NetworkInfo()),
            mock.patch.object(self.compute.driver, 'plug_vifs',
                side_effect=exception.VirtualInterfacePlugException(
                    "Unexpected vif_type=binding_failed")),
            mock.patch.object(self.compute, '_set_instance_obj_error_state')
        ) as (get_admin_context, get_nw_info, plug_vifs, set_error_state):
            self.compute._init_instance(self.context, instance)
            set_error_state.assert_called_once_with(self.context, instance)

    def test__get_power_state_InstanceNotFound(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                power_state=power_state.RUNNING)
        with mock.patch.object(self.compute.driver,
                'get_info',
                side_effect=exception.InstanceNotFound(instance_id=1)):
            self.assertEqual(self.compute._get_power_state(self.context,
                                                           instance),
                    power_state.NOSTATE)

    def test__get_power_state_NotFound(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                power_state=power_state.RUNNING)
        with mock.patch.object(self.compute.driver,
                'get_info',
                side_effect=exception.NotFound()):
            self.assertRaises(exception.NotFound,
                              self.compute._get_power_state,
                              self.context, instance)

    @mock.patch.object(manager.ComputeManager, '_get_power_state')
    @mock.patch.object(fake_driver.FakeDriver, 'plug_vifs')
    @mock.patch.object(fake_driver.FakeDriver, 'resume_state_on_host_boot')
    @mock.patch.object(manager.ComputeManager,
                       '_get_instance_block_device_info')
    @mock.patch.object(manager.ComputeManager, '_set_instance_obj_error_state')
    def test_init_instance_failed_resume_sets_error(self, mock_set_inst,
                mock_get_inst, mock_resume, mock_plug, mock_get_power):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                info_cache=None,
                power_state=power_state.RUNNING,
                vm_state=vm_states.ACTIVE,
                task_state=None,
                host=self.compute.host,
                expected_attrs=['info_cache'])

        self.flags(resume_guests_state_on_host_boot=True)
        mock_get_power.side_effect = (power_state.SHUTDOWN,
                                      power_state.SHUTDOWN)
        mock_get_inst.return_value = 'fake-bdm'
        mock_resume.side_effect = test.TestingException
        self.compute._init_instance('fake-context', instance)
        mock_get_power.assert_has_calls([mock.call(mock.ANY, instance),
                                         mock.call(mock.ANY, instance)])
        mock_plug.assert_called_once_with(instance, mock.ANY)
        mock_get_inst.assert_called_once_with(mock.ANY, instance)
        mock_resume.assert_called_once_with(mock.ANY, instance, mock.ANY,
                                            'fake-bdm')
        mock_set_inst.assert_called_once_with(mock.ANY, instance)

    @mock.patch.object(objects.BlockDeviceMapping, 'destroy')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(objects.Instance, 'destroy')
    @mock.patch.object(objects.Instance, 'obj_load_attr')
    @mock.patch.object(objects.quotas.Quotas, 'commit')
    @mock.patch.object(objects.quotas.Quotas, 'reserve')
    @mock.patch.object(objects.quotas, 'ids_from_instance')
    def test_init_instance_complete_partial_deletion(
            self, mock_ids_from_instance, mock_reserve, mock_commit,
            mock_inst_destroy, mock_obj_load_attr, mock_get_by_instance_uuid,
            mock_bdm_destroy):
        """Test to complete deletion for instances in DELETED status but not
        marked as deleted in the DB
        """
        instance = fake_instance.fake_instance_obj(
                self.context,
                project_id=fakes.FAKE_PROJECT_ID,
                uuid=uuids.instance,
                vcpus=1,
                memory_mb=64,
                power_state=power_state.SHUTDOWN,
                vm_state=vm_states.DELETED,
                host=self.compute.host,
                task_state=None,
                deleted=False,
                deleted_at=None,
                metadata={},
                system_metadata={},
                expected_attrs=['metadata', 'system_metadata'])

        # Make sure instance vm_state is marked as 'DELETED' but instance is
        # not destroyed from db.
        self.assertEqual(vm_states.DELETED, instance.vm_state)
        self.assertFalse(instance.deleted)

        deltas = {'instances': -1,
                  'cores': -instance.flavor.vcpus,
                  'ram': -instance.flavor.memory_mb}

        def fake_inst_destroy():
            instance.deleted = True
            instance.deleted_at = timeutils.utcnow()

        mock_ids_from_instance.return_value = (instance.project_id,
                                               instance.user_id)
        mock_inst_destroy.side_effect = fake_inst_destroy()

        self.compute._init_instance(self.context, instance)

        # Make sure that instance.destroy method was called and
        # instance was deleted from db.
        self.assertTrue(mock_reserve.called)
        self.assertTrue(mock_commit.called)
        self.assertNotEqual(0, instance.deleted)
        mock_reserve.assert_called_once_with(project_id=instance.project_id,
                                             user_id=instance.user_id,
                                             **deltas)

    @mock.patch('nova.compute.manager.LOG')
    def test_init_instance_complete_partial_deletion_raises_exception(
            self, mock_log):
        instance = fake_instance.fake_instance_obj(
                self.context,
                project_id=fakes.FAKE_PROJECT_ID,
                uuid=uuids.instance,
                vcpus=1,
                memory_mb=64,
                power_state=power_state.SHUTDOWN,
                vm_state=vm_states.DELETED,
                host=self.compute.host,
                task_state=None,
                deleted=False,
                deleted_at=None,
                metadata={},
                system_metadata={},
                expected_attrs=['metadata', 'system_metadata'])

        with mock.patch.object(self.compute,
                               '_complete_partial_deletion') as mock_deletion:
            mock_deletion.side_effect = test.TestingException()
            self.compute._init_instance(self, instance)
            msg = u'Failed to complete a deletion'
            mock_log.exception.assert_called_once_with(msg, instance=instance)

    def test_init_instance_stuck_in_deleting(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                project_id=fakes.FAKE_PROJECT_ID,
                uuid=uuids.instance,
                vcpus=1,
                memory_mb=64,
                power_state=power_state.RUNNING,
                vm_state=vm_states.ACTIVE,
                host=self.compute.host,
                task_state=task_states.DELETING)

        bdms = []
        quotas = objects.quotas.Quotas(self.context)

        with test.nested(
                mock.patch.object(objects.BlockDeviceMappingList,
                                  'get_by_instance_uuid',
                                  return_value=bdms),
                mock.patch.object(self.compute, '_delete_instance'),
                mock.patch.object(instance, 'obj_load_attr'),
                mock.patch.object(self.compute, '_create_reservations',
                                  return_value=quotas)
        ) as (mock_get, mock_delete, mock_load, mock_create):
            self.compute._init_instance(self.context, instance)
            mock_get.assert_called_once_with(self.context, instance.uuid)
            mock_create.assert_called_once_with(self.context, instance,
                                                instance.project_id,
                                                instance.user_id)
            mock_delete.assert_called_once_with(self.context, instance,
                                                bdms, mock.ANY)

    @mock.patch.object(objects.Instance, 'get_by_uuid')
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def test_init_instance_stuck_in_deleting_raises_exception(
            self, mock_get_by_instance_uuid, mock_get_by_uuid):

        instance = fake_instance.fake_instance_obj(
            self.context,
            project_id=fakes.FAKE_PROJECT_ID,
            uuid=uuids.instance,
            vcpus=1,
            memory_mb=64,
            metadata={},
            system_metadata={},
            host=self.compute.host,
            vm_state=vm_states.ACTIVE,
            task_state=task_states.DELETING,
            expected_attrs=['metadata', 'system_metadata'])

        bdms = []
        reservations = ['fake-resv']

        def _create_patch(name, attr):
            patcher = mock.patch.object(name, attr)
            mocked_obj = patcher.start()
            self.addCleanup(patcher.stop)
            return mocked_obj

        mock_delete_instance = _create_patch(self.compute, '_delete_instance')
        mock_set_instance_error_state = _create_patch(
            self.compute, '_set_instance_obj_error_state')
        mock_create_reservations = _create_patch(self.compute,
                                                 '_create_reservations')

        mock_create_reservations.return_value = reservations
        mock_get_by_instance_uuid.return_value = bdms
        mock_get_by_uuid.return_value = instance
        mock_delete_instance.side_effect = test.TestingException('test')
        self.compute._init_instance(self.context, instance)
        mock_set_instance_error_state.assert_called_once_with(
            self.context, instance)

    def _test_init_instance_reverts_crashed_migrations(self,
                                                       old_vm_state=None):
        power_on = True if (not old_vm_state or
                            old_vm_state == vm_states.ACTIVE) else False
        sys_meta = {
            'old_vm_state': old_vm_state
            }
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_states.ERROR,
                task_state=task_states.RESIZE_MIGRATING,
                power_state=power_state.SHUTDOWN,
                system_metadata=sys_meta,
                host=self.compute.host,
                expected_attrs=['system_metadata'])

        with test.nested(
            mock.patch.object(compute_utils, 'get_nw_info_for_instance',
                              return_value=network_model.NetworkInfo()),
            mock.patch.object(self.compute.driver, 'plug_vifs'),
            mock.patch.object(self.compute.driver, 'finish_revert_migration'),
            mock.patch.object(self.compute, '_get_instance_block_device_info',
                              return_value=[]),
            mock.patch.object(self.compute.driver, 'get_info'),
            mock.patch.object(instance, 'save'),
            mock.patch.object(self.compute, '_retry_reboot',
                              return_value=(False, None))
        ) as (mock_get_nw, mock_plug, mock_finish, mock_get_inst,
              mock_get_info, mock_save, mock_retry):
            mock_get_info.side_effect = (
                hardware.InstanceInfo(state=power_state.SHUTDOWN),
                hardware.InstanceInfo(state=power_state.SHUTDOWN))

            self.compute._init_instance(self.context, instance)

            mock_retry.assert_called_once_with(self.context, instance,
                power_state.SHUTDOWN)
            mock_get_nw.assert_called_once_with(instance)
            mock_plug.assert_called_once_with(instance, [])
            mock_get_inst.assert_called_once_with(self.context, instance)
            mock_finish.assert_called_once_with(self.context, instance,
                                                [], [], power_on)
            mock_save.assert_called_once_with()
            mock_get_info.assert_has_calls([mock.call(instance),
                                            mock.call(instance)])
        self.assertIsNone(instance.task_state)

    def test_init_instance_reverts_crashed_migration_from_active(self):
        self._test_init_instance_reverts_crashed_migrations(
                                                old_vm_state=vm_states.ACTIVE)

    def test_init_instance_reverts_crashed_migration_from_stopped(self):
        self._test_init_instance_reverts_crashed_migrations(
                                                old_vm_state=vm_states.STOPPED)

    def test_init_instance_reverts_crashed_migration_no_old_state(self):
        self._test_init_instance_reverts_crashed_migrations(old_vm_state=None)

    def test_init_instance_resets_crashed_live_migration(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_states.ACTIVE,
                host=self.compute.host,
                task_state=task_states.MIGRATING)
        with test.nested(
            mock.patch.object(instance, 'save'),
            mock.patch('nova.compute.utils.get_nw_info_for_instance',
                       return_value=network_model.NetworkInfo())
        ) as (save, get_nw_info):
            self.compute._init_instance(self.context, instance)
            save.assert_called_once_with(expected_task_state=['migrating'])
            get_nw_info.assert_called_once_with(instance)
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)

    def _test_init_instance_sets_building_error(self, vm_state,
                                                task_state=None):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_state,
                host=self.compute.host,
                task_state=task_state)
        with mock.patch.object(instance, 'save') as save:
            self.compute._init_instance(self.context, instance)
            save.assert_called_once_with()
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_init_instance_sets_building_error(self):
        self._test_init_instance_sets_building_error(vm_states.BUILDING)

    def test_init_instance_sets_rebuilding_errors(self):
        tasks = [task_states.REBUILDING,
                 task_states.REBUILD_BLOCK_DEVICE_MAPPING,
                 task_states.REBUILD_SPAWNING]
        vms = [vm_states.ACTIVE, vm_states.STOPPED]

        for vm_state in vms:
            for task_state in tasks:
                self._test_init_instance_sets_building_error(
                    vm_state, task_state)

    def _test_init_instance_sets_building_tasks_error(self, instance):
        instance.host = self.compute.host
        with mock.patch.object(instance, 'save') as save:
            self.compute._init_instance(self.context, instance)
            save.assert_called_once_with()
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.ERROR, instance.vm_state)

    def test_init_instance_sets_building_tasks_error_scheduling(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=None,
                task_state=task_states.SCHEDULING)
        self._test_init_instance_sets_building_tasks_error(instance)

    def test_init_instance_sets_building_tasks_error_block_device(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = None
        instance.task_state = task_states.BLOCK_DEVICE_MAPPING
        self._test_init_instance_sets_building_tasks_error(instance)

    def test_init_instance_sets_building_tasks_error_networking(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = None
        instance.task_state = task_states.NETWORKING
        self._test_init_instance_sets_building_tasks_error(instance)

    def test_init_instance_sets_building_tasks_error_spawning(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = None
        instance.task_state = task_states.SPAWNING
        self._test_init_instance_sets_building_tasks_error(instance)

    def _test_init_instance_cleans_image_states(self, instance):
        with mock.patch.object(instance, 'save') as save:
            self.compute._get_power_state = mock.Mock()
            self.compute.driver.post_interrupted_snapshot_cleanup = mock.Mock()
            instance.info_cache = None
            instance.power_state = power_state.RUNNING
            instance.host = self.compute.host
            self.compute._init_instance(self.context, instance)
            save.assert_called_once_with()
            self.compute.driver.post_interrupted_snapshot_cleanup.\
                    assert_called_once_with(self.context, instance)
        self.assertIsNone(instance.task_state)

    @mock.patch('nova.compute.manager.ComputeManager._get_power_state',
                return_value=power_state.RUNNING)
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    def _test_init_instance_cleans_task_states(self, powerstate, state,
            mock_get_uuid, mock_get_power_state):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.info_cache = None
        instance.power_state = power_state.RUNNING
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = state
        instance.host = self.compute.host
        mock_get_power_state.return_value = powerstate

        self.compute._init_instance(self.context, instance)

        return instance

    def test_init_instance_cleans_image_state_pending_upload(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_PENDING_UPLOAD
        self._test_init_instance_cleans_image_states(instance)

    def test_init_instance_cleans_image_state_uploading(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_UPLOADING
        self._test_init_instance_cleans_image_states(instance)

    def test_init_instance_cleans_image_state_snapshot(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_SNAPSHOT
        self._test_init_instance_cleans_image_states(instance)

    def test_init_instance_cleans_image_state_snapshot_pending(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.IMAGE_SNAPSHOT_PENDING
        self._test_init_instance_cleans_image_states(instance)

    @mock.patch.object(objects.Instance, 'save')
    def test_init_instance_cleans_running_pausing(self, mock_save):
        instance = self._test_init_instance_cleans_task_states(
            power_state.RUNNING, task_states.PAUSING)
        mock_save.assert_called_once_with()
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertIsNone(instance.task_state)

    @mock.patch.object(objects.Instance, 'save')
    def test_init_instance_cleans_running_unpausing(self, mock_save):
        instance = self._test_init_instance_cleans_task_states(
            power_state.RUNNING, task_states.UNPAUSING)
        mock_save.assert_called_once_with()
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertIsNone(instance.task_state)

    @mock.patch('nova.compute.manager.ComputeManager.unpause_instance')
    def test_init_instance_cleans_paused_unpausing(self, mock_unpause):

        def fake_unpause(context, instance):
            instance.task_state = None

        mock_unpause.side_effect = fake_unpause
        instance = self._test_init_instance_cleans_task_states(
            power_state.PAUSED, task_states.UNPAUSING)
        mock_unpause.assert_called_once_with(self.context, instance)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)
        self.assertIsNone(instance.task_state)

    def test_init_instance_deletes_error_deleting_instance(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                project_id=fakes.FAKE_PROJECT_ID,
                uuid=uuids.instance,
                vcpus=1,
                memory_mb=64,
                vm_state=vm_states.ERROR,
                host=self.compute.host,
                task_state=task_states.DELETING)
        bdms = []
        quotas = objects.quotas.Quotas(self.context)

        with test.nested(
                mock.patch.object(objects.BlockDeviceMappingList,
                                  'get_by_instance_uuid',
                                  return_value=bdms),
                mock.patch.object(self.compute, '_delete_instance'),
                mock.patch.object(instance, 'obj_load_attr'),
                mock.patch.object(self.compute, '_create_reservations',
                                  return_value=quotas),
                mock.patch.object(objects.quotas, 'ids_from_instance',
                                  return_value=(instance.project_id,
                                                instance.user_id))
        ) as (mock_get, mock_delete, mock_load, mock_create, mock_ids):
            self.compute._init_instance(self.context, instance)
            mock_get.assert_called_once_with(self.context, instance.uuid)
            mock_create.assert_called_once_with(self.context, instance,
                                                instance.project_id,
                                                instance.user_id)
            mock_delete.assert_called_once_with(self.context, instance,
                                                bdms, mock.ANY)
            mock_ids.assert_called_once_with(self.context, instance)

    def test_init_instance_resize_prep(self):
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_states.ACTIVE,
                host=self.compute.host,
                task_state=task_states.RESIZE_PREP,
                power_state=power_state.RUNNING)

        with test.nested(
            mock.patch.object(self.compute, '_get_power_state',
                              return_value=power_state.RUNNING),
            mock.patch.object(compute_utils, 'get_nw_info_for_instance'),
            mock.patch.object(instance, 'save', autospec=True)
        ) as (mock_get_power_state, mock_nw_info, mock_instance_save):
            self.compute._init_instance(self.context, instance)
            mock_instance_save.assert_called_once_with()
            self.assertIsNone(instance.task_state)

    @mock.patch('nova.context.RequestContext.elevated')
    @mock.patch('nova.compute.utils.get_nw_info_for_instance')
    @mock.patch(
        'nova.compute.manager.ComputeManager._get_instance_block_device_info')
    @mock.patch('nova.virt.driver.ComputeDriver.destroy')
    @mock.patch('nova.virt.fake.FakeDriver.get_volume_connector')
    def _test_shutdown_instance_exception(self, exc, mock_connector,
            mock_destroy, mock_blk_device_info, mock_nw_info, mock_elevated):
        mock_connector.side_effect = exc
        mock_elevated.return_value = self.context
        instance = fake_instance.fake_instance_obj(
                self.context,
                uuid=uuids.instance,
                vm_state=vm_states.ERROR,
                task_state=task_states.DELETING)
        bdms = [mock.Mock(id=1, is_volume=True)]

        self.compute._shutdown_instance(self.context, instance, bdms,
                notify=False, try_deallocate_networks=False)

    def test_shutdown_instance_endpoint_not_found(self):
        exc = cinder_exception.EndpointNotFound
        self._test_shutdown_instance_exception(exc)

    def test_shutdown_instance_client_exception(self):
        exc = cinder_exception.ClientException(code=9001)
        self._test_shutdown_instance_exception(exc)

    def test_shutdown_instance_volume_not_found(self):
        exc = exception.VolumeNotFound(volume_id=42)
        self._test_shutdown_instance_exception(exc)

    def test_shutdown_instance_disk_not_found(self):
        exc = exception.DiskNotFound(location="not\\here")
        self._test_shutdown_instance_exception(exc)

    def test_shutdown_instance_other_exception(self):
        exc = Exception('some other exception')
        self._test_shutdown_instance_exception(exc)

    def _test_init_instance_retries_reboot(self, instance, reboot_type,
                                           return_power_state):
        instance.host = self.compute.host
        with test.nested(
            mock.patch.object(self.compute, '_get_power_state',
                               return_value=return_power_state),
            mock.patch.object(self.compute, 'reboot_instance'),
            mock.patch.object(compute_utils, 'get_nw_info_for_instance')
          ) as (
            _get_power_state,
            reboot_instance,
            get_nw_info_for_instance
          ):
            self.compute._init_instance(self.context, instance)
            call = mock.call(self.context, instance, block_device_info=None,
                             reboot_type=reboot_type)
            reboot_instance.assert_has_calls([call])

    def test_init_instance_retries_reboot_pending(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_PENDING
        for state in vm_states.ALLOW_SOFT_REBOOT:
            instance.vm_state = state
            self._test_init_instance_retries_reboot(instance, 'SOFT',
                                                    power_state.RUNNING)

    def test_init_instance_retries_reboot_pending_hard(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_PENDING_HARD
        for state in vm_states.ALLOW_HARD_REBOOT:
            # NOTE(dave-mcnally) while a reboot of a vm in error state is
            # possible we don't attempt to recover an error during init
            if state == vm_states.ERROR:
                continue
            instance.vm_state = state
            self._test_init_instance_retries_reboot(instance, 'HARD',
                                                    power_state.RUNNING)

    def test_init_instance_retries_reboot_pending_soft_became_hard(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_PENDING
        for state in vm_states.ALLOW_HARD_REBOOT:
            # NOTE(dave-mcnally) while a reboot of a vm in error state is
            # possible we don't attempt to recover an error during init
            if state == vm_states.ERROR:
                continue
            instance.vm_state = state
            with mock.patch.object(instance, 'save'):
                self._test_init_instance_retries_reboot(instance, 'HARD',
                                                        power_state.SHUTDOWN)
                self.assertEqual(task_states.REBOOT_PENDING_HARD,
                                instance.task_state)

    def test_init_instance_retries_reboot_started(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.REBOOT_STARTED
        with mock.patch.object(instance, 'save'):
            self._test_init_instance_retries_reboot(instance, 'HARD',
                                                    power_state.NOSTATE)

    def test_init_instance_retries_reboot_started_hard(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.REBOOT_STARTED_HARD
        self._test_init_instance_retries_reboot(instance, 'HARD',
                                                power_state.NOSTATE)

    def _test_init_instance_cleans_reboot_state(self, instance):
        instance.host = self.compute.host
        with test.nested(
            mock.patch.object(self.compute, '_get_power_state',
                               return_value=power_state.RUNNING),
            mock.patch.object(instance, 'save', autospec=True),
            mock.patch.object(compute_utils, 'get_nw_info_for_instance')
          ) as (
            _get_power_state,
            instance_save,
            get_nw_info_for_instance
          ):
            self.compute._init_instance(self.context, instance)
            instance_save.assert_called_once_with()
            self.assertIsNone(instance.task_state)
            self.assertEqual(vm_states.ACTIVE, instance.vm_state)

    def test_init_instance_cleans_image_state_reboot_started(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.REBOOT_STARTED
        instance.power_state = power_state.RUNNING
        self._test_init_instance_cleans_reboot_state(instance)

    def test_init_instance_cleans_image_state_reboot_started_hard(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.REBOOT_STARTED_HARD
        instance.power_state = power_state.RUNNING
        self._test_init_instance_cleans_reboot_state(instance)

    def test_init_instance_retries_power_off(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.id = 1
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.POWERING_OFF
        instance.host = self.compute.host
        with mock.patch.object(self.compute, 'stop_instance'):
            self.compute._init_instance(self.context, instance)
            call = mock.call(self.context, instance, True)
            self.compute.stop_instance.assert_has_calls([call])

    def test_init_instance_retries_power_on(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.id = 1
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.POWERING_ON
        instance.host = self.compute.host
        with mock.patch.object(self.compute, 'start_instance'):
            self.compute._init_instance(self.context, instance)
            call = mock.call(self.context, instance)
            self.compute.start_instance.assert_has_calls([call])

    def test_init_instance_retries_power_on_silent_exception(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.id = 1
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.POWERING_ON
        instance.host = self.compute.host
        with mock.patch.object(self.compute, 'start_instance',
                              return_value=Exception):
            init_return = self.compute._init_instance(self.context, instance)
            call = mock.call(self.context, instance)
            self.compute.start_instance.assert_has_calls([call])
            self.assertIsNone(init_return)

    def test_init_instance_retries_power_off_silent_exception(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.id = 1
        instance.vm_state = vm_states.ACTIVE
        instance.task_state = task_states.POWERING_OFF
        instance.host = self.compute.host
        with mock.patch.object(self.compute, 'stop_instance',
                              return_value=Exception):
            init_return = self.compute._init_instance(self.context, instance)
            call = mock.call(self.context, instance, True)
            self.compute.stop_instance.assert_has_calls([call])
            self.assertIsNone(init_return)

    @mock.patch('nova.objects.InstanceList.get_by_filters')
    def test_get_instances_on_driver(self, mock_instance_list):
        driver_instances = []
        for x in range(10):
            driver_instances.append(fake_instance.fake_db_instance())

        def _make_instance_list(db_list):
            return instance_obj._make_instance_list(
                    self.context, objects.InstanceList(), db_list, None)

        driver_uuids = [inst['uuid'] for inst in driver_instances]
        mock_instance_list.return_value = _make_instance_list(driver_instances)

        with mock.patch.object(self.compute.driver,
                               'list_instance_uuids') as mock_driver_uuids:
            mock_driver_uuids.return_value = driver_uuids
            result = self.compute._get_instances_on_driver(self.context)

        self.assertEqual([x['uuid'] for x in driver_instances],
                         [x['uuid'] for x in result])

    @mock.patch('nova.objects.InstanceList.get_by_filters')
    def test_get_instances_on_driver_empty(self, mock_instance_list):
        with mock.patch.object(self.compute.driver,
                               'list_instance_uuids') as mock_driver_uuids:
            mock_driver_uuids.return_value = []
            result = self.compute._get_instances_on_driver(self.context)

        # Short circuit DB call, get_by_filters should not be called
        self.assertEqual(0, mock_instance_list.call_count)
        self.assertEqual(1, mock_driver_uuids.call_count)
        self.assertEqual([], [x['uuid'] for x in result])

    @mock.patch('nova.objects.InstanceList.get_by_filters')
    def test_get_instances_on_driver_fallback(self, mock_instance_list):
        # Test getting instances when driver doesn't support
        # 'list_instance_uuids'
        self.compute.host = 'host'
        filters = {'host': self.compute.host}

        self.flags(instance_name_template='inst-%i')

        all_instances = []
        driver_instances = []
        for x in range(10):
            instance = fake_instance.fake_db_instance(name='inst-%i' % x,
                                                      id=x)
            if x % 2:
                driver_instances.append(instance)
            all_instances.append(instance)

        def _make_instance_list(db_list):
            return instance_obj._make_instance_list(
                    self.context, objects.InstanceList(), db_list, None)

        driver_instance_names = [inst['name'] for inst in driver_instances]
        mock_instance_list.return_value = _make_instance_list(all_instances)

        with test.nested(
            mock.patch.object(self.compute.driver, 'list_instance_uuids'),
            mock.patch.object(self.compute.driver, 'list_instances')
        ) as (
            mock_driver_uuids,
            mock_driver_instances
        ):
            mock_driver_uuids.side_effect = NotImplementedError()
            mock_driver_instances.return_value = driver_instance_names
            result = self.compute._get_instances_on_driver(self.context,
                                                           filters)

        self.assertEqual([x['uuid'] for x in driver_instances],
                         [x['uuid'] for x in result])

    def test_instance_usage_audit(self):
        instances = [objects.Instance(uuid=uuids.instance)]

        def fake_task_log(*a, **k):
            pass

        def fake_get(*a, **k):
            return instances

        self.flags(instance_usage_audit=True)
        with test.nested(
            mock.patch.object(objects.TaskLog, 'get',
                              side_effect=fake_task_log),
            mock.patch.object(objects.InstanceList,
                              'get_active_by_window_joined',
                              side_effect=fake_get),
            mock.patch.object(objects.TaskLog, 'begin_task',
                              side_effect=fake_task_log),
            mock.patch.object(objects.TaskLog, 'end_task',
                              side_effect=fake_task_log),
            mock.patch.object(compute_utils, 'notify_usage_exists')
        ) as (mock_get, mock_get_active, mock_begin, mock_end, mock_notify):
            self.compute._instance_usage_audit(self.context)
            mock_notify.assert_called_once_with(self.compute.notifier,
                self.context, instances[0], ignore_missing_network_data=False)
            self.assertTrue(mock_get.called)
            self.assertTrue(mock_get_active.called)
            self.assertTrue(mock_begin.called)
            self.assertTrue(mock_end.called)

    @mock.patch.object(objects.InstanceList, 'get_by_host')
    def test_sync_power_states(self, mock_get):
        instance = mock.Mock()
        mock_get.return_value = [instance]
        with mock.patch.object(self.compute._sync_power_pool,
                               'spawn_n') as mock_spawn:
            self.compute._sync_power_states(mock.sentinel.context)
            mock_get.assert_called_with(mock.sentinel.context,
                                        self.compute.host, expected_attrs=[],
                                        use_slave=True)
            mock_spawn.assert_called_once_with(mock.ANY, instance)

    def _get_sync_instance(self, power_state, vm_state, task_state=None,
                           shutdown_terminate=False):
        instance = objects.Instance()
        instance.uuid = uuids.instance
        instance.power_state = power_state
        instance.vm_state = vm_state
        instance.host = self.compute.host
        instance.task_state = task_state
        instance.shutdown_terminate = shutdown_terminate
        return instance

    @mock.patch.object(objects.Instance, 'refresh')
    def test_sync_instance_power_state_match(self, mock_refresh):
        instance = self._get_sync_instance(power_state.RUNNING,
                                           vm_states.ACTIVE)
        self.compute._sync_instance_power_state(self.context, instance,
                                                power_state.RUNNING)
        mock_refresh.assert_called_once_with(use_slave=False)

    @mock.patch.object(objects.Instance, 'refresh')
    @mock.patch.object(objects.Instance, 'save')
    def test_sync_instance_power_state_running_stopped(self, mock_save,
                                                       mock_refresh):
        instance = self._get_sync_instance(power_state.RUNNING,
                                           vm_states.ACTIVE)
        self.compute._sync_instance_power_state(self.context, instance,
                                                power_state.SHUTDOWN)
        self.assertEqual(instance.power_state, power_state.SHUTDOWN)
        mock_refresh.assert_called_once_with(use_slave=False)
        self.assertTrue(mock_save.called)

    def _test_sync_to_stop(self, power_state, vm_state, driver_power_state,
                           stop=True, force=False, shutdown_terminate=False):
        instance = self._get_sync_instance(
            power_state, vm_state, shutdown_terminate=shutdown_terminate)

        with test.nested(
            mock.patch.object(objects.Instance, 'refresh'),
            mock.patch.object(objects.Instance, 'save'),
            mock.patch.object(self.compute.compute_api, 'stop'),
            mock.patch.object(self.compute.compute_api, 'delete'),
            mock.patch.object(self.compute.compute_api, 'force_stop'),
        ) as (mock_refresh, mock_save, mock_stop, mock_delete, mock_force):
            self.compute._sync_instance_power_state(self.context, instance,
                                                    driver_power_state)
            if shutdown_terminate:
                mock_delete.assert_called_once_with(self.context, instance)
            elif stop:
                if force:
                    mock_force.assert_called_once_with(self.context, instance)
                else:
                    mock_stop.assert_called_once_with(self.context, instance)
            mock_refresh.assert_called_once_with(use_slave=False)
            self.assertTrue(mock_save.called)

    def test_sync_instance_power_state_to_stop(self):
        for ps in (power_state.SHUTDOWN, power_state.CRASHED,
                   power_state.SUSPENDED):
            self._test_sync_to_stop(power_state.RUNNING, vm_states.ACTIVE, ps)

        for ps in (power_state.SHUTDOWN, power_state.CRASHED):
            self._test_sync_to_stop(power_state.PAUSED, vm_states.PAUSED, ps,
                                    force=True)

        self._test_sync_to_stop(power_state.SHUTDOWN, vm_states.STOPPED,
                                power_state.RUNNING, force=True)

    def test_sync_instance_power_state_to_terminate(self):
        self._test_sync_to_stop(power_state.RUNNING, vm_states.ACTIVE,
                                power_state.SHUTDOWN,
                                force=False, shutdown_terminate=True)

    def test_sync_instance_power_state_to_no_stop(self):
        for ps in (power_state.PAUSED, power_state.NOSTATE):
            self._test_sync_to_stop(power_state.RUNNING, vm_states.ACTIVE, ps,
                                    stop=False)
        for vs in (vm_states.SOFT_DELETED, vm_states.DELETED):
            for ps in (power_state.NOSTATE, power_state.SHUTDOWN):
                self._test_sync_to_stop(power_state.RUNNING, vs, ps,
                                        stop=False)

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_sync_instance_power_state')
    def test_query_driver_power_state_and_sync_pending_task(
            self, mock_sync_power_state):
        with mock.patch.object(self.compute.driver,
                               'get_info') as mock_get_info:
            db_instance = objects.Instance(uuid=uuids.db_instance,
                                           task_state=task_states.POWERING_OFF)
            self.compute._query_driver_power_state_and_sync(self.context,
                                                            db_instance)
            self.assertFalse(mock_get_info.called)
            self.assertFalse(mock_sync_power_state.called)

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_sync_instance_power_state')
    def test_query_driver_power_state_and_sync_not_found_driver(
            self, mock_sync_power_state):
        error = exception.InstanceNotFound(instance_id=1)
        with mock.patch.object(self.compute.driver,
                               'get_info', side_effect=error) as mock_get_info:
            db_instance = objects.Instance(uuid=uuids.db_instance,
                                           task_state=None)
            self.compute._query_driver_power_state_and_sync(self.context,
                                                            db_instance)
            mock_get_info.assert_called_once_with(db_instance)
            mock_sync_power_state.assert_called_once_with(self.context,
                                                          db_instance,
                                                          power_state.NOSTATE,
                                                          use_slave=True)

    @mock.patch.object(virt_driver.ComputeDriver, 'delete_instance_files')
    @mock.patch.object(objects.InstanceList, 'get_by_filters')
    def test_run_pending_deletes(self, mock_get, mock_delete):
        self.flags(instance_delete_interval=10)

        class FakeInstance(object):
            def __init__(self, uuid, name, smd):
                self.uuid = uuid
                self.name = name
                self.system_metadata = smd
                self.cleaned = False

            def __getitem__(self, name):
                return getattr(self, name)

            def save(self):
                pass

        def _fake_get(ctx, filter, expected_attrs, use_slave):
            mock_get.assert_called_once_with(
                {'read_deleted': 'yes'},
                {'deleted': True, 'soft_deleted': False, 'host': 'fake-mini',
                 'cleaned': False},
                expected_attrs=['info_cache', 'security_groups',
                                'system_metadata'],
                use_slave=True)
            return [a, b, c]

        a = FakeInstance('123', 'apple', {'clean_attempts': '100'})
        b = FakeInstance('456', 'orange', {'clean_attempts': '3'})
        c = FakeInstance('789', 'banana', {})

        mock_get.side_effect = _fake_get
        mock_delete.side_effect = [True, False]

        self.compute._run_pending_deletes({})

        self.assertFalse(a.cleaned)
        self.assertEqual('100', a.system_metadata['clean_attempts'])
        self.assertTrue(b.cleaned)
        self.assertEqual('4', b.system_metadata['clean_attempts'])
        self.assertFalse(c.cleaned)
        self.assertEqual('1', c.system_metadata['clean_attempts'])
        mock_delete.assert_has_calls([mock.call(mock.ANY),
                                      mock.call(mock.ANY)])

    @mock.patch.object(objects.Migration, 'obj_as_admin')
    @mock.patch.object(objects.Migration, 'save')
    @mock.patch.object(objects.MigrationList, 'get_by_filters')
    @mock.patch.object(objects.InstanceList, 'get_by_filters')
    def _test_cleanup_incomplete_migrations(self, inst_host,
                                            mock_inst_get_by_filters,
                                            mock_migration_get_by_filters,
                                            mock_save, mock_obj_as_admin):
        def fake_inst(context, uuid, host):
            inst = objects.Instance(context)
            inst.uuid = uuid
            inst.host = host
            return inst

        def fake_migration(uuid, status, inst_uuid, src_host, dest_host):
            migration = objects.Migration()
            migration.uuid = uuid
            migration.status = status
            migration.instance_uuid = inst_uuid
            migration.source_compute = src_host
            migration.dest_compute = dest_host
            return migration

        fake_instances = [fake_inst(self.context, uuids.instance_1, inst_host),
                          fake_inst(self.context, uuids.instance_2, inst_host)]

        fake_migrations = [fake_migration('123', 'error',
                                          uuids.instance_1,
                                          'fake-host', 'fake-mini'),
                           fake_migration('456', 'error',
                                           uuids.instance_2,
                                          'fake-host', 'fake-mini')]

        mock_migration_get_by_filters.return_value = fake_migrations
        mock_inst_get_by_filters.return_value = fake_instances

        with mock.patch.object(self.compute.driver, 'delete_instance_files'):
            self.compute._cleanup_incomplete_migrations(self.context)

        # Ensure that migration status is set to 'failed' after instance
        # files deletion for those instances whose instance.host is not
        # same as compute host where periodic task is running.
        for inst in fake_instances:
            if inst.host != CONF.host:
                for mig in fake_migrations:
                    if inst.uuid == mig.instance_uuid:
                        self.assertEqual('failed', mig.status)

    def test_cleanup_incomplete_migrations_dest_node(self):
        """Test to ensure instance files are deleted from destination node.

        If instance gets deleted during resizing/revert-resizing operation,
        in that case instance files gets deleted from instance.host (source
        host here), but there is possibility that instance files could be
        present on destination node.
        This test ensures that `_cleanup_incomplete_migration` periodic
        task deletes orphaned instance files from destination compute node.
        """
        self.flags(host='fake-mini')
        self._test_cleanup_incomplete_migrations('fake-host')

    def test_cleanup_incomplete_migrations_source_node(self):
        """Test to ensure instance files are deleted from source node.

        If instance gets deleted during resizing/revert-resizing operation,
        in that case instance files gets deleted from instance.host (dest
        host here), but there is possibility that instance files could be
        present on source node.
        This test ensures that `_cleanup_incomplete_migration` periodic
        task deletes orphaned instance files from source compute node.
        """
        self.flags(host='fake-host')
        self._test_cleanup_incomplete_migrations('fake-mini')

    def test_attach_interface_failure(self):
        # Test that the fault methods are invoked when an attach fails
        db_instance = fake_instance.fake_db_instance()
        f_instance = objects.Instance._from_db_object(self.context,
                                                      objects.Instance(),
                                                      db_instance)
        e = exception.InterfaceAttachFailed(instance_uuid=f_instance.uuid)

        @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
        @mock.patch.object(self.compute.network_api,
                           'allocate_port_for_instance',
                           side_effect=e)
        @mock.patch.object(self.compute, '_instance_update',
                           side_effect=lambda *a, **k: {})
        def do_test(update, meth, add_fault):
            self.assertRaises(exception.InterfaceAttachFailed,
                              self.compute.attach_interface,
                              self.context, f_instance, 'net_id', 'port_id',
                              None)
            add_fault.assert_has_calls([
                    mock.call(self.context, f_instance, e,
                              mock.ANY)])

        with mock.patch.dict(self.compute.driver.capabilities,
                             supports_attach_interface=True):
            do_test()

    def test_detach_interface_failure(self):
        # Test that the fault methods are invoked when a detach fails

        # Build test data that will cause a PortNotFound exception
        f_instance = mock.MagicMock()
        f_instance.info_cache = mock.MagicMock()
        f_instance.info_cache.network_info = []

        @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
        @mock.patch.object(self.compute, '_set_instance_obj_error_state')
        def do_test(meth, add_fault):
            self.assertRaises(exception.PortNotFound,
                              self.compute.detach_interface,
                              self.context, f_instance, 'port_id')
            add_fault.assert_has_calls(
                   [mock.call(self.context, f_instance, mock.ANY, mock.ANY)])

        do_test()

    def test_swap_volume_volume_api_usage(self):
        # This test ensures that volume_id arguments are passed to volume_api
        # and that volume states are OK
        volumes = {}
        old_volume_id = uuidutils.generate_uuid()
        volumes[old_volume_id] = {'id': old_volume_id,
                                  'display_name': 'old_volume',
                                  'status': 'detaching',
                                  'size': 1}
        new_volume_id = uuidutils.generate_uuid()
        volumes[new_volume_id] = {'id': new_volume_id,
                                  'display_name': 'new_volume',
                                  'status': 'available',
                                  'size': 2}

        def fake_vol_api_roll_detaching(cls, context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            if volumes[volume_id]['status'] == 'detaching':
                volumes[volume_id]['status'] = 'in-use'

        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
                   {'device_name': '/dev/vdb', 'source_type': 'volume',
                    'destination_type': 'volume',
                    'instance_uuid': uuids.instance,
                    'connection_info': '{"foo": "bar"}'})

        def fake_vol_api_func(cls, context, volume, *args):
            self.assertTrue(uuidutils.is_uuid_like(volume))
            return {}

        def fake_vol_get(cls, context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            return volumes[volume_id]

        def fake_vol_unreserve(cls, context, volume_id):
            self.assertTrue(uuidutils.is_uuid_like(volume_id))
            if volumes[volume_id]['status'] == 'attaching':
                volumes[volume_id]['status'] = 'available'

        def fake_vol_migrate_volume_completion(cls, context, old_volume_id,
                                               new_volume_id, error=False):
            self.assertTrue(uuidutils.is_uuid_like(old_volume_id))
            self.assertTrue(uuidutils.is_uuid_like(new_volume_id))
            volumes[old_volume_id]['status'] = 'in-use'
            return {'save_volume_id': new_volume_id}

        def fake_func_exc(*args, **kwargs):
            raise AttributeError  # Random exception

        def fake_swap_volume(cls, old_connection_info, new_connection_info,
                             instance, mountpoint, resize_to):
            self.assertEqual(resize_to, 2)

        def fake_block_device_mapping_update(ctxt, id, updates, legacy):
            self.assertEqual(2, updates['volume_size'])
            return fake_bdm

        self.stub_out('nova.volume.cinder.API.roll_detaching',
                       fake_vol_api_roll_detaching)
        self.stub_out('nova.volume.cinder.API.get', fake_vol_get)
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                       fake_vol_api_func)
        self.stub_out('nova.volume.cinder.API.unreserve_volume',
                       fake_vol_unreserve)
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                       fake_vol_api_func)
        self.stub_out('nova.db.'
                      'block_device_mapping_get_by_instance_and_volume_id',
                      lambda x, y, z, v: fake_bdm)
        self.stub_out('nova.virt.driver.ComputeDriver.get_volume_connector',
                       lambda x: {})
        self.stub_out('nova.virt.driver.ComputeDriver.swap_volume',
                       fake_swap_volume)
        self.stub_out('nova.volume.cinder.API.migrate_volume_completion',
                      fake_vol_migrate_volume_completion)
        self.stub_out('nova.db.block_device_mapping_update',
                      fake_block_device_mapping_update)
        self.stub_out('nova.db.instance_fault_create',
                      lambda x, y:
                           test_instance_fault.fake_faults['fake-uuid'][0])
        self.stub_out('nova.compute.manager.ComputeManager.'
                      '_instance_update', lambda c, u, **k: {})

        # Good path
        self.compute.swap_volume(self.context, old_volume_id, new_volume_id,
                fake_instance.fake_instance_obj(
                    self.context, **{'uuid': uuids.instance}))
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')

        # Error paths
        volumes[old_volume_id]['status'] = 'detaching'
        volumes[new_volume_id]['status'] = 'attaching'
        self.stub_out('nova.virt.fake.FakeDriver.swap_volume',
                      fake_func_exc)
        self.assertRaises(AttributeError, self.compute.swap_volume,
                          self.context, old_volume_id, new_volume_id,
                          fake_instance.fake_instance_obj(
                                self.context, **{'uuid': uuids.instance}))
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')

        volumes[old_volume_id]['status'] = 'detaching'
        volumes[new_volume_id]['status'] = 'attaching'
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                       fake_func_exc)
        self.assertRaises(AttributeError, self.compute.swap_volume,
                          self.context, old_volume_id, new_volume_id,
                          fake_instance.fake_instance_obj(
                                self.context, **{'uuid': uuids.instance}))
        self.assertEqual(volumes[old_volume_id]['status'], 'in-use')
        self.assertEqual(volumes[new_volume_id]['status'], 'available')

    @mock.patch('nova.db.block_device_mapping_get_by_instance_and_volume_id')
    @mock.patch.object(fake_driver.FakeDriver, 'get_volume_connector',
                       return_value={})
    @mock.patch.object(fake_driver.FakeDriver, 'swap_volume')
    @mock.patch('nova.volume.cinder.API.get')
    @mock.patch('nova.volume.cinder.API.initialize_connection',
                return_value={})
    @mock.patch('nova.volume.cinder.API.terminate_connection')
    @mock.patch('nova.volume.cinder.API.migrate_volume_completion')
    @mock.patch('nova.objects.BlockDeviceMapping.update')
    @mock.patch('nova.objects.BlockDeviceMapping.save')
    def test_swap_volume_cinder_initiated(self, mock_bdm_save, mock_bdm_update,
                                          mock_migrate_volume_completion,
                                          mock_terminate_connection,
                                          mock_initialize_connection,
                                          mock_get, mock_swap_volume,
                                          mock_get_volume_connector,
                                          mock_bdm_get):
        # Check whether the 'serial' in new connection info is equal to
        # the old volume ID in the case that cinder initiated
        # swapping volumes
        mock_get.return_value = {'id': uuids.old_volume,
                                 'display_name': 'old_volume',
                                 'status': 'detaching',
                                 'size': 2}
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
            {'device_name': '/dev/vdb', 'source_type': 'volume',
             'destination_type': 'volume',
             'instance_uuid': uuids.instance,
             'delete_on_termination': True,
             'connection_info': '{"foo": "bar"}'})
        mock_bdm_get.return_value = fake_bdm
        mock_migrate_volume_completion.return_value = {'save_volume_id':
                                                       uuids.old_volume}
        instance = fake_instance.fake_instance_obj(self.context,
                                                   **{'uuid': uuids.instance})

        self.compute.swap_volume(
            self.context, uuids.old_volume, uuids.new_volume, instance)

        mock_get_volume_connector.assert_called_once_with(instance)
        mock_get.assert_has_calls(
            [mock.call(test.MatchType(context.RequestContext),
                       uuids.old_volume),
             mock.call(test.MatchType(context.RequestContext),
                       uuids.new_volume)])
        mock_initialize_connection.assert_called_once_with(
            test.MatchType(context.RequestContext), uuids.new_volume, {})
        mock_swap_volume.assert_called_once_with(
            {"foo": "bar"}, {'serial': uuids.old_volume}, instance,
            '/dev/vdb', 0)
        mock_terminate_connection.assert_called_once_with(
            test.MatchType(context.RequestContext), uuids.old_volume, {})
        mock_migrate_volume_completion.assert_called_once_with(
            test.MatchType(context.RequestContext), uuids.old_volume,
            uuids.new_volume, error=False)
        # Check 'serial' in new connection info
        mock_bdm_update.assert_called_once_with(
            {'connection_info': jsonutils.dumps({'serial': uuids.old_volume}),
             'source_type': 'volume',
             'destination_type': 'volume',
             'snapshot_id': None,
             'volume_id': uuids.old_volume,
             'no_device': None})
        mock_bdm_save.assert_called_once_with()

    @mock.patch('nova.db.block_device_mapping_get_by_instance_and_volume_id')
    @mock.patch('nova.db.block_device_mapping_update')
    @mock.patch('nova.volume.cinder.API.get')
    @mock.patch('nova.virt.libvirt.LibvirtDriver.get_volume_connector')
    @mock.patch('nova.compute.manager.ComputeManager._swap_volume')
    def test_swap_volume_delete_on_termination_flag(self, swap_volume_mock,
                                                    volume_connector_mock,
                                                    get_volume_mock,
                                                    update_bdm_mock,
                                                    get_bdm_mock):
        # This test ensures that delete_on_termination flag arguments
        # are reserved
        volumes = {}
        old_volume_id = uuidutils.generate_uuid()
        volumes[old_volume_id] = {'id': old_volume_id,
                                  'display_name': 'old_volume',
                                  'status': 'detaching',
                                  'size': 2}
        new_volume_id = uuidutils.generate_uuid()
        volumes[new_volume_id] = {'id': new_volume_id,
                                  'display_name': 'new_volume',
                                  'status': 'available',
                                  'size': 2}
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict(
                   {'device_name': '/dev/vdb', 'source_type': 'volume',
                    'destination_type': 'volume',
                    'instance_uuid': uuids.instance,
                    'delete_on_termination': True,
                    'connection_info': '{"foo": "bar"}'})
        comp_ret = {'save_volume_id': old_volume_id}
        new_info = {"foo": "bar"}
        swap_volume_mock.return_value = (comp_ret, new_info)
        volume_connector_mock.return_value = {}
        update_bdm_mock.return_value = fake_bdm
        get_bdm_mock.return_value = fake_bdm
        get_volume_mock.return_value = volumes[old_volume_id]
        self.compute.swap_volume(self.context, old_volume_id, new_volume_id,
                fake_instance.fake_instance_obj(self.context,
                                                **{'uuid': uuids.instance}))
        update_values = {'no_device': False,
                         'connection_info': u'{"foo": "bar"}',
                         'volume_id': old_volume_id,
                         'source_type': u'volume',
                         'snapshot_id': None,
                         'destination_type': u'volume'}
        update_bdm_mock.assert_called_once_with(mock.ANY, mock.ANY,
                                                update_values, legacy=False)

    @mock.patch.object(fake_driver.FakeDriver,
                       'check_can_live_migrate_source')
    @mock.patch.object(manager.ComputeManager,
                       '_get_instance_block_device_info')
    @mock.patch.object(compute_utils, 'is_volume_backed_instance')
    @mock.patch.object(compute_utils, 'EventReporter')
    def test_check_can_live_migrate_source(self, mock_event, mock_volume,
                                           mock_get_inst, mock_check):
        is_volume_backed = 'volume_backed'
        dest_check_data = migrate_data_obj.LiveMigrateData()
        db_instance = fake_instance.fake_db_instance()
        instance = objects.Instance._from_db_object(
                self.context, objects.Instance(), db_instance)

        mock_volume.return_value = is_volume_backed
        mock_get_inst.return_value = {'block_device_mapping': 'fake'}

        self.compute.check_can_live_migrate_source(
                self.context, instance=instance,
                dest_check_data=dest_check_data)
        mock_event.assert_called_once_with(
            self.context, 'compute_check_can_live_migrate_source',
            instance.uuid)
        mock_check.assert_called_once_with(self.context, instance,
                                           dest_check_data,
                                           {'block_device_mapping': 'fake'})
        mock_volume.assert_called_once_with(self.context, instance)
        mock_get_inst.assert_called_once_with(self.context, instance,
                                              refresh_conn_info=False)

        self.assertTrue(dest_check_data.is_volume_backed)

    def _test_check_can_live_migrate_destination(self, do_raise=False):
        db_instance = fake_instance.fake_db_instance(host='fake-host')
        instance = objects.Instance._from_db_object(
                self.context, objects.Instance(), db_instance)
        instance.host = 'fake-host'
        block_migration = 'block_migration'
        disk_over_commit = 'disk_over_commit'
        src_info = 'src_info'
        dest_info = 'dest_info'
        dest_check_data = dict(foo='bar')
        mig_data = dict(cow='moo')

        with test.nested(
            mock.patch.object(self.compute, '_get_compute_info'),
            mock.patch.object(self.compute.driver,
                              'check_can_live_migrate_destination'),
            mock.patch.object(self.compute.compute_rpcapi,
                              'check_can_live_migrate_source'),
            mock.patch.object(self.compute.driver,
                              'cleanup_live_migration_destination_check'),
            mock.patch.object(db, 'instance_fault_create'),
            mock.patch.object(compute_utils, 'EventReporter')
        ) as (mock_get, mock_check_dest, mock_check_src, mock_check_clean,
              mock_fault_create, mock_event):
            mock_get.side_effect = (src_info, dest_info)
            mock_check_dest.return_value = dest_check_data

            if do_raise:
                mock_check_src.side_effect = test.TestingException
                mock_fault_create.return_value = \
                    test_instance_fault.fake_faults['fake-uuid'][0]
            else:
                mock_check_src.return_value = mig_data

            result = self.compute.check_can_live_migrate_destination(
                self.context, instance=instance,
                block_migration=block_migration,
                disk_over_commit=disk_over_commit)

            if do_raise:
                mock_fault_create.assert_called_once_with(self.context,
                                                          mock.ANY)
            mock_check_src.assert_called_once_with(self.context, instance,
                                                   dest_check_data)
            mock_check_clean.assert_called_once_with(self.context,
                                                     dest_check_data)
            mock_get.assert_has_calls([mock.call(self.context, 'fake-host'),
                                       mock.call(self.context, CONF.host)])
            mock_check_dest.assert_called_once_with(self.context, instance,
                        src_info, dest_info, block_migration, disk_over_commit)

            self.assertEqual(mig_data, result)
            mock_event.assert_called_once_with(
                self.context, 'compute_check_can_live_migrate_destination',
                instance.uuid)

    def test_check_can_live_migrate_destination_success(self):
        self._test_check_can_live_migrate_destination()

    def test_check_can_live_migrate_destination_fail(self):
        self.assertRaises(
                test.TestingException,
                self._test_check_can_live_migrate_destination,
                do_raise=True)

    @mock.patch('nova.compute.manager.InstanceEvents._lock_name')
    def test_prepare_for_instance_event(self, lock_name_mock):
        inst_obj = objects.Instance(uuid=uuids.instance)
        result = self.compute.instance_events.prepare_for_instance_event(
            inst_obj, 'test-event')
        self.assertIn(uuids.instance, self.compute.instance_events._events)
        self.assertIn('test-event',
                      self.compute.instance_events._events[uuids.instance])
        self.assertEqual(
            result,
            self.compute.instance_events._events[uuids.instance]['test-event'])
        self.assertTrue(hasattr(result, 'send'))
        lock_name_mock.assert_called_once_with(inst_obj)

    @mock.patch('nova.compute.manager.InstanceEvents._lock_name')
    def test_pop_instance_event(self, lock_name_mock):
        event = eventlet_event.Event()
        self.compute.instance_events._events = {
            uuids.instance: {
                'network-vif-plugged': event,
                }
            }
        inst_obj = objects.Instance(uuid=uuids.instance)
        event_obj = objects.InstanceExternalEvent(name='network-vif-plugged',
                                                  tag=None)
        result = self.compute.instance_events.pop_instance_event(inst_obj,
                                                                 event_obj)
        self.assertEqual(result, event)
        lock_name_mock.assert_called_once_with(inst_obj)

    @mock.patch('nova.compute.manager.InstanceEvents._lock_name')
    def test_clear_events_for_instance(self, lock_name_mock):
        event = eventlet_event.Event()
        self.compute.instance_events._events = {
            uuids.instance: {
                'test-event': event,
                }
            }
        inst_obj = objects.Instance(uuid=uuids.instance)
        result = self.compute.instance_events.clear_events_for_instance(
            inst_obj)
        self.assertEqual(result, {'test-event': event})
        lock_name_mock.assert_called_once_with(inst_obj)

    def test_instance_events_lock_name(self):
        inst_obj = objects.Instance(uuid=uuids.instance)
        result = self.compute.instance_events._lock_name(inst_obj)
        self.assertEqual(result, "%s-events" % uuids.instance)

    def test_prepare_for_instance_event_again(self):
        inst_obj = objects.Instance(uuid=uuids.instance)
        self.compute.instance_events.prepare_for_instance_event(
            inst_obj, 'test-event')
        # A second attempt will avoid creating a new list; make sure we
        # get the current list
        result = self.compute.instance_events.prepare_for_instance_event(
            inst_obj, 'test-event')
        self.assertIn(uuids.instance, self.compute.instance_events._events)
        self.assertIn('test-event',
                      self.compute.instance_events._events[uuids.instance])
        self.assertEqual(
            result,
            self.compute.instance_events._events[uuids.instance]['test-event'])
        self.assertTrue(hasattr(result, 'send'))

    def test_process_instance_event(self):
        event = eventlet_event.Event()
        self.compute.instance_events._events = {
            uuids.instance: {
                'network-vif-plugged': event,
                }
            }
        inst_obj = objects.Instance(uuid=uuids.instance)
        event_obj = objects.InstanceExternalEvent(name='network-vif-plugged',
                                                  tag=None)
        self.compute._process_instance_event(inst_obj, event_obj)
        self.assertTrue(event.ready())
        self.assertEqual(event_obj, event.wait())
        self.assertEqual({}, self.compute.instance_events._events)

    def test_process_instance_vif_deleted_event(self):
        vif1 = fake_network_cache_model.new_vif()
        vif1['id'] = '1'
        vif2 = fake_network_cache_model.new_vif()
        vif2['id'] = '2'
        nw_info = network_model.NetworkInfo([vif1, vif2])
        info_cache = objects.InstanceInfoCache(network_info=nw_info,
                                               instance_uuid=uuids.instance)
        inst_obj = objects.Instance(id=3, uuid=uuids.instance,
                                    info_cache=info_cache)

        @mock.patch.object(manager.base_net_api,
                           'update_instance_cache_with_nw_info')
        @mock.patch.object(self.compute.driver, 'detach_interface')
        def do_test(detach_interface, update_instance_cache_with_nw_info):
            self.compute._process_instance_vif_deleted_event(self.context,
                                                             inst_obj,
                                                             vif2['id'])
            update_instance_cache_with_nw_info.assert_called_once_with(
                                                   self.compute.network_api,
                                                   self.context,
                                                   inst_obj,
                                                   nw_info=[vif1])
            detach_interface.assert_called_once_with(inst_obj, vif2)
        do_test()

    def test_external_instance_event(self):
        instances = [
            objects.Instance(id=1, uuid=uuids.instance_1),
            objects.Instance(id=2, uuid=uuids.instance_2),
            objects.Instance(id=3, uuid=uuids.instance_3)]
        events = [
            objects.InstanceExternalEvent(name='network-changed',
                                          tag='tag1',
                                          instance_uuid=uuids.instance_1),
            objects.InstanceExternalEvent(name='network-vif-plugged',
                                          instance_uuid=uuids.instance_2,
                                          tag='tag2'),
            objects.InstanceExternalEvent(name='network-vif-deleted',
                                          instance_uuid=uuids.instance_3,
                                          tag='tag3')]

        @mock.patch.object(self.compute, '_process_instance_vif_deleted_event')
        @mock.patch.object(self.compute.network_api, 'get_instance_nw_info')
        @mock.patch.object(self.compute, '_process_instance_event')
        def do_test(_process_instance_event, get_instance_nw_info,
                    _process_instance_vif_deleted_event):
            self.compute.external_instance_event(self.context,
                                                 instances, events)
            get_instance_nw_info.assert_called_once_with(self.context,
                                                         instances[0])
            _process_instance_event.assert_called_once_with(instances[1],
                                                            events[1])
            _process_instance_vif_deleted_event.assert_called_once_with(
                self.context, instances[2], events[2].tag)
        do_test()

    def test_external_instance_event_with_exception(self):
        vif1 = fake_network_cache_model.new_vif()
        vif1['id'] = '1'
        vif2 = fake_network_cache_model.new_vif()
        vif2['id'] = '2'
        nw_info = network_model.NetworkInfo([vif1, vif2])
        info_cache = objects.InstanceInfoCache(network_info=nw_info,
                                               instance_uuid=uuids.instance_2)
        instances = [
            objects.Instance(id=1, uuid=uuids.instance_1),
            objects.Instance(id=2, uuid=uuids.instance_2,
                             info_cache=info_cache),
            objects.Instance(id=3, uuid=uuids.instance_3)]
        events = [
            objects.InstanceExternalEvent(name='network-changed',
                                          tag='tag1',
                                          instance_uuid=uuids.instance_1),
            objects.InstanceExternalEvent(name='network-vif-deleted',
                                          instance_uuid=uuids.instance_2,
                                          tag='2'),
            objects.InstanceExternalEvent(name='network-vif-plugged',
                                          instance_uuid=uuids.instance_3,
                                          tag='tag3')]

        # Make sure all the three events are handled despite the exceptions in
        # processing events 1 and 2
        @mock.patch.object(manager.base_net_api,
                           'update_instance_cache_with_nw_info')
        @mock.patch.object(self.compute.driver, 'detach_interface',
                           side_effect=exception.NovaException)
        @mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                           side_effect=exception.InstanceInfoCacheNotFound(
                                         instance_uuid=uuids.instance_1))
        @mock.patch.object(self.compute, '_process_instance_event')
        def do_test(_process_instance_event, get_instance_nw_info,
                    detach_interface, update_instance_cache_with_nw_info):
            self.compute.external_instance_event(self.context,
                                                 instances, events)
            get_instance_nw_info.assert_called_once_with(self.context,
                                                         instances[0])
            update_instance_cache_with_nw_info.assert_called_once_with(
                                                   self.compute.network_api,
                                                   self.context,
                                                   instances[1],
                                                   nw_info=[vif1])
            detach_interface.assert_called_once_with(instances[1], vif2)
            _process_instance_event.assert_called_once_with(instances[2],
                                                            events[2])
        do_test()

    def test_cancel_all_events(self):
        inst = objects.Instance(uuid=uuids.instance)
        fake_eventlet_event = mock.MagicMock()
        self.compute.instance_events._events = {
            inst.uuid: {
                'network-vif-plugged-bar': fake_eventlet_event,
            }
        }
        self.compute.instance_events.cancel_all_events()
        # call it again to make sure we handle that gracefully
        self.compute.instance_events.cancel_all_events()
        self.assertTrue(fake_eventlet_event.send.called)
        event = fake_eventlet_event.send.call_args_list[0][0][0]
        self.assertEqual('network-vif-plugged', event.name)
        self.assertEqual('bar', event.tag)
        self.assertEqual('failed', event.status)

    def test_cleanup_cancels_all_events(self):
        with mock.patch.object(self.compute, 'instance_events') as mock_ev:
            self.compute.cleanup_host()
            mock_ev.cancel_all_events.assert_called_once_with()

    def test_cleanup_blocks_new_events(self):
        instance = objects.Instance(uuid=uuids.instance)
        self.compute.instance_events.cancel_all_events()
        callback = mock.MagicMock()
        body = mock.MagicMock()
        with self.compute.virtapi.wait_for_instance_event(
                instance, ['network-vif-plugged-bar'],
                error_callback=callback):
            body()
        self.assertTrue(body.called)
        callback.assert_called_once_with('network-vif-plugged-bar', instance)

    def test_pop_events_fails_gracefully(self):
        inst = objects.Instance(uuid=uuids.instance)
        event = mock.MagicMock()
        self.compute.instance_events._events = None
        self.assertIsNone(
            self.compute.instance_events.pop_instance_event(inst, event))

    def test_clear_events_fails_gracefully(self):
        inst = objects.Instance(uuid=uuids.instance)
        self.compute.instance_events._events = None
        self.assertEqual(
            self.compute.instance_events.clear_events_for_instance(inst), {})

    def test_retry_reboot_pending_soft(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_PENDING
        instance.vm_state = vm_states.ACTIVE
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.RUNNING)
        self.assertTrue(allow_reboot)
        self.assertEqual(reboot_type, 'SOFT')

    def test_retry_reboot_pending_hard(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_PENDING_HARD
        instance.vm_state = vm_states.ACTIVE
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.RUNNING)
        self.assertTrue(allow_reboot)
        self.assertEqual(reboot_type, 'HARD')

    def test_retry_reboot_starting_soft_off(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_STARTED
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.NOSTATE)
        self.assertTrue(allow_reboot)
        self.assertEqual(reboot_type, 'HARD')

    def test_retry_reboot_starting_hard_off(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_STARTED_HARD
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.NOSTATE)
        self.assertTrue(allow_reboot)
        self.assertEqual(reboot_type, 'HARD')

    def test_retry_reboot_starting_hard_on(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = task_states.REBOOT_STARTED_HARD
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.RUNNING)
        self.assertFalse(allow_reboot)
        self.assertEqual(reboot_type, 'HARD')

    def test_retry_reboot_no_reboot(self):
        instance = objects.Instance(self.context)
        instance.uuid = uuids.instance
        instance.task_state = 'bar'
        allow_reboot, reboot_type = self.compute._retry_reboot(
            context, instance, power_state.RUNNING)
        self.assertFalse(allow_reboot)
        self.assertEqual(reboot_type, 'HARD')

    @mock.patch('nova.objects.BlockDeviceMapping.get_by_volume_and_instance')
    @mock.patch('nova.compute.manager.ComputeManager._driver_detach_volume')
    @mock.patch('nova.objects.Instance._from_db_object')
    def test_remove_volume_connection(self, inst_from_db, detach, bdm_get):
        bdm = mock.sentinel.bdm
        bdm.connection_info = jsonutils.dumps({})
        inst_obj = mock.Mock()
        inst_obj.uuid = 'uuid'
        bdm_get.return_value = bdm
        inst_from_db.return_value = inst_obj
        with mock.patch.object(self.compute, 'volume_api'):
            self.compute.remove_volume_connection(self.context, 'vol',
                                                  inst_obj)
        detach.assert_called_once_with(self.context, inst_obj, bdm, {})
        bdm_get.assert_called_once_with(self.context, 'vol', 'uuid')

    def test_detach_volume(self):
        self._test_detach_volume()

    def test_detach_volume_not_destroy_bdm(self):
        self._test_detach_volume(destroy_bdm=False)

    @mock.patch('nova.objects.BlockDeviceMapping.get_by_volume_and_instance')
    @mock.patch('nova.compute.manager.ComputeManager._driver_detach_volume')
    @mock.patch('nova.compute.manager.ComputeManager.'
                '_notify_about_instance_usage')
    def _test_detach_volume(self, notify_inst_usage, detach,
                            bdm_get, destroy_bdm=True):
        volume_id = uuids.volume
        inst_obj = mock.Mock()
        inst_obj.uuid = uuids.instance
        inst_obj.host = CONF.host
        attachment_id = uuids.attachment

        bdm = mock.MagicMock(spec=objects.BlockDeviceMapping)
        bdm.device_name = 'vdb'
        bdm.connection_info = jsonutils.dumps({})
        bdm_get.return_value = bdm

        detach.return_value = {}

        with mock.patch.object(self.compute, 'volume_api') as volume_api:
            with mock.patch.object(self.compute, 'driver') as driver:
                connector_sentinel = mock.sentinel.connector
                driver.get_volume_connector.return_value = connector_sentinel

                self.compute._detach_volume(self.context, volume_id,
                                            inst_obj,
                                            destroy_bdm=destroy_bdm,
                                            attachment_id=attachment_id)

                detach.assert_called_once_with(self.context, inst_obj, bdm, {})
                driver.get_volume_connector.assert_called_once_with(inst_obj)
                volume_api.terminate_connection.assert_called_once_with(
                    self.context, volume_id, connector_sentinel)
                volume_api.detach.assert_called_once_with(mock.ANY, volume_id,
                                                          inst_obj.uuid,
                                                          attachment_id)
                notify_inst_usage.assert_called_once_with(
                    self.context, inst_obj, "volume.detach",
                    extra_usage_info={'volume_id': volume_id}
                )

                if destroy_bdm:
                    bdm.destroy.assert_called_once_with()
                else:
                    self.assertFalse(bdm.destroy.called)

    def test_detach_volume_evacuate(self):
        """For evacuate, terminate_connection is called with original host."""
        expected_connector = {'host': 'evacuated-host'}
        conn_info_str = '{"connector": {"host": "evacuated-host"}}'
        self._test_detach_volume_evacuate(conn_info_str,
                                          expected=expected_connector)

    def test_detach_volume_evacuate_legacy(self):
        """Test coverage for evacuate with legacy attachments.

        In this case, legacy means the volume was attached to the instance
        before nova stashed the connector in connection_info. The connector
        sent to terminate_connection will still be for the local host in this
        case because nova does not have the info to get the connector for the
        original (evacuated) host.
        """
        conn_info_str = '{"foo": "bar"}'  # Has no 'connector'.
        self._test_detach_volume_evacuate(conn_info_str)

    def test_detach_volume_evacuate_mismatch(self):
        """Test coverage for evacuate with connector mismatch.

        For evacuate, if the stashed connector also has the wrong host,
        then log it and stay with the local connector.
        """
        conn_info_str = '{"connector": {"host": "other-host"}}'
        self._test_detach_volume_evacuate(conn_info_str)

    @mock.patch('nova.objects.BlockDeviceMapping.get_by_volume_and_instance')
    @mock.patch('nova.compute.manager.ComputeManager.'
                '_notify_about_instance_usage')
    def _test_detach_volume_evacuate(self, conn_info_str, notify_inst_usage,
                                     bdm_get, expected=None):
        """Re-usable code for detach volume evacuate test cases.

        :param conn_info_str: String form of the stashed connector.
        :param expected: Dict of the connector that is expected in the
                         terminate call (optional). Default is to expect the
                         local connector to be used.
        """
        volume_id = 'vol_id'
        instance = fake_instance.fake_instance_obj(self.context,
                                                   host='evacuated-host')
        bdm = mock.Mock()
        bdm.connection_info = conn_info_str
        bdm_get.return_value = bdm

        local_connector = {'host': 'local-connector-host'}
        expected_connector = local_connector if not expected else expected

        with mock.patch.object(self.compute, 'volume_api') as volume_api:
            with mock.patch.object(self.compute, 'driver') as driver:
                driver.get_volume_connector.return_value = local_connector

                self.compute._detach_volume(self.context,
                                            volume_id,
                                            instance,
                                            destroy_bdm=False)

                driver._driver_detach_volume.assert_not_called()
                driver.get_volume_connector.assert_called_once_with(instance)
                volume_api.terminate_connection.assert_called_once_with(
                    self.context, volume_id, expected_connector)
                volume_api.detach.assert_called_once_with(mock.ANY,
                                                          volume_id,
                                                          instance.uuid,
                                                          None)
                notify_inst_usage.assert_called_once_with(
                    self.context, instance, "volume.detach",
                    extra_usage_info={'volume_id': volume_id}
                )

    def _test_rescue(self, clean_shutdown=True):
        instance = fake_instance.fake_instance_obj(
            self.context, vm_state=vm_states.ACTIVE)
        fake_nw_info = network_model.NetworkInfo()
        rescue_image_meta = objects.ImageMeta.from_dict(
            {'id': uuids.image_id, 'name': uuids.image_name})
        with test.nested(
            mock.patch.object(self.context, 'elevated',
                              return_value=self.context),
            mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                              return_value=fake_nw_info),
            mock.patch.object(self.compute, '_get_rescue_image',
                              return_value=rescue_image_meta),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute, '_power_off_instance'),
            mock.patch.object(self.compute.driver, 'rescue'),
            mock.patch.object(compute_utils, 'notify_usage_exists'),
            mock.patch.object(self.compute, '_get_power_state',
                              return_value=power_state.RUNNING),
            mock.patch.object(instance, 'save')
        ) as (
            elevated_context, get_nw_info,
            get_rescue_image, notify_instance_usage, power_off_instance,
            driver_rescue, notify_usage_exists, get_power_state, instance_save
        ):
            self.compute.rescue_instance(
                self.context, instance, rescue_password='verybadpass',
                rescue_image_ref=None, clean_shutdown=clean_shutdown)

            # assert the field values on the instance object
            self.assertEqual(vm_states.RESCUED, instance.vm_state)
            self.assertIsNone(instance.task_state)
            self.assertEqual(power_state.RUNNING, instance.power_state)
            self.assertIsNotNone(instance.launched_at)

            # assert our mock calls
            get_nw_info.assert_called_once_with(self.context, instance)
            get_rescue_image.assert_called_once_with(
                self.context, instance, None)

            extra_usage_info = {'rescue_image_name': uuids.image_name}
            notify_calls = [
                mock.call(self.context, instance, "rescue.start",
                          extra_usage_info=extra_usage_info,
                          network_info=fake_nw_info),
                mock.call(self.context, instance, "rescue.end",
                          extra_usage_info=extra_usage_info,
                          network_info=fake_nw_info)
            ]
            notify_instance_usage.assert_has_calls(notify_calls)

            power_off_instance.assert_called_once_with(self.context, instance,
                                                       clean_shutdown)

            driver_rescue.assert_called_once_with(
                self.context, instance, fake_nw_info, rescue_image_meta,
                'verybadpass')

            notify_usage_exists.assert_called_once_with(self.compute.notifier,
                self.context, instance, current_period=True)

            instance_save.assert_called_once_with(
                expected_task_state=task_states.RESCUING)

    def test_rescue(self):
        self._test_rescue()

    def test_rescue_forced_shutdown(self):
        self._test_rescue(clean_shutdown=False)

    def test_unrescue(self):
        instance = fake_instance.fake_instance_obj(
            self.context, vm_state=vm_states.RESCUED)
        fake_nw_info = network_model.NetworkInfo()
        with test.nested(
            mock.patch.object(self.context, 'elevated',
                              return_value=self.context),
            mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                              return_value=fake_nw_info),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute.driver, 'unrescue'),
            mock.patch.object(self.compute, '_get_power_state',
                              return_value=power_state.RUNNING),
            mock.patch.object(instance, 'save')
        ) as (
            elevated_context, get_nw_info,
            notify_instance_usage, driver_unrescue, get_power_state,
            instance_save
        ):
            self.compute.unrescue_instance(self.context, instance)

            # assert the field values on the instance object
            self.assertEqual(vm_states.ACTIVE, instance.vm_state)
            self.assertIsNone(instance.task_state)
            self.assertEqual(power_state.RUNNING, instance.power_state)

            # assert our mock calls
            get_nw_info.assert_called_once_with(self.context, instance)

            notify_calls = [
                mock.call(self.context, instance, "unrescue.start",
                          network_info=fake_nw_info),
                mock.call(self.context, instance, "unrescue.end",
                          network_info=fake_nw_info)
            ]
            notify_instance_usage.assert_has_calls(notify_calls)

            driver_unrescue.assert_called_once_with(instance, fake_nw_info)

            instance_save.assert_called_once_with(
                expected_task_state=task_states.UNRESCUING)

    @mock.patch('nova.compute.manager.ComputeManager._get_power_state',
                return_value=power_state.RUNNING)
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch('nova.utils.generate_password', return_value='fake-pass')
    def test_set_admin_password(self, gen_password_mock,
                                instance_save_mock, power_state_mock):
        # Ensure instance can have its admin password set.
        instance = fake_instance.fake_instance_obj(
            self.context,
            vm_state=vm_states.ACTIVE,
            task_state=task_states.UPDATING_PASSWORD)

        @mock.patch.object(self.context, 'elevated', return_value=self.context)
        @mock.patch.object(self.compute.driver, 'set_admin_password')
        def do_test(driver_mock, elevated_mock):
            # call the manager method
            self.compute.set_admin_password(self.context, instance, None)
            # make our assertions
            self.assertEqual(vm_states.ACTIVE, instance.vm_state)
            self.assertIsNone(instance.task_state)

            power_state_mock.assert_called_once_with(self.context, instance)
            driver_mock.assert_called_once_with(instance, 'fake-pass')
            instance_save_mock.assert_called_once_with(
                expected_task_state=task_states.UPDATING_PASSWORD)

        do_test()

    @mock.patch('nova.compute.manager.ComputeManager._get_power_state',
                return_value=power_state.NOSTATE)
    @mock.patch('nova.compute.manager.ComputeManager._instance_update')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    def test_set_admin_password_bad_state(self, add_fault_mock,
                                          instance_save_mock,
                                          update_mock,
                                          power_state_mock):
        # Test setting password while instance is rebuilding.
        instance = fake_instance.fake_instance_obj(self.context)
        with mock.patch.object(self.context, 'elevated',
                               return_value=self.context):
            # call the manager method
            self.assertRaises(exception.InstancePasswordSetFailed,
                              self.compute.set_admin_password,
                              self.context, instance, None)

        # make our assertions
        power_state_mock.assert_called_once_with(self.context, instance)
        instance_save_mock.assert_called_once_with(
            expected_task_state=task_states.UPDATING_PASSWORD)
        add_fault_mock.assert_called_once_with(
            self.context, instance, mock.ANY, mock.ANY)

    @mock.patch('nova.utils.generate_password', return_value='fake-pass')
    @mock.patch('nova.compute.manager.ComputeManager._get_power_state',
                return_value=power_state.RUNNING)
    @mock.patch('nova.compute.manager.ComputeManager._instance_update')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    def _do_test_set_admin_password_driver_error(self, exc,
                                                 expected_vm_state,
                                                 expected_task_state,
                                                 expected_exception,
                                                 add_fault_mock,
                                                 instance_save_mock,
                                                 update_mock,
                                                 power_state_mock,
                                                 gen_password_mock):
        # Ensure expected exception is raised if set_admin_password fails.
        instance = fake_instance.fake_instance_obj(
            self.context,
            vm_state=vm_states.ACTIVE,
            task_state=task_states.UPDATING_PASSWORD)

        @mock.patch.object(self.context, 'elevated', return_value=self.context)
        @mock.patch.object(self.compute.driver, 'set_admin_password',
                           side_effect=exc)
        def do_test(driver_mock, elevated_mock):
            # error raised from the driver should not reveal internal
            # information so a new error is raised
            self.assertRaises(expected_exception,
                              self.compute.set_admin_password,
                              self.context,
                              instance=instance,
                              new_pass=None)

            if (expected_exception == exception.SetAdminPasswdNotSupported or
                    expected_exception == exception.InstanceAgentNotEnabled or
                    expected_exception == NotImplementedError):
                instance_save_mock.assert_called_once_with(
                    expected_task_state=task_states.UPDATING_PASSWORD)
            else:
                # setting the instance to error state
                instance_save_mock.assert_called_once_with()

            self.assertEqual(expected_vm_state, instance.vm_state)
            # check revert_task_state decorator
            update_mock.assert_called_once_with(
                self.context, instance, task_state=expected_task_state)
            # check wrap_instance_fault decorator
            add_fault_mock.assert_called_once_with(
                self.context, instance, mock.ANY, mock.ANY)

        do_test()

    def test_set_admin_password_driver_not_authorized(self):
        # Ensure expected exception is raised if set_admin_password not
        # authorized.
        exc = exception.Forbidden('Internal error')
        expected_exception = exception.InstancePasswordSetFailed
        self._do_test_set_admin_password_driver_error(
            exc, vm_states.ERROR, None, expected_exception)

    def test_set_admin_password_driver_not_implemented(self):
        # Ensure expected exception is raised if set_admin_password not
        # implemented by driver.
        exc = NotImplementedError()
        expected_exception = NotImplementedError
        self._do_test_set_admin_password_driver_error(
            exc, vm_states.ACTIVE, None, expected_exception)

    def test_set_admin_password_driver_not_supported(self):
        exc = exception.SetAdminPasswdNotSupported()
        expected_exception = exception.SetAdminPasswdNotSupported
        self._do_test_set_admin_password_driver_error(
            exc, vm_states.ACTIVE, None, expected_exception)

    def test_set_admin_password_guest_agent_no_enabled(self):
        exc = exception.QemuGuestAgentNotEnabled()
        expected_exception = exception.InstanceAgentNotEnabled
        self._do_test_set_admin_password_driver_error(
            exc, vm_states.ACTIVE, None, expected_exception)

    def test_destroy_evacuated_instances(self):
        our_host = self.compute.host
        instance_1 = objects.Instance(self.context)
        instance_1.uuid = uuids.instance_1
        instance_1.task_state = None
        instance_1.vm_state = vm_states.ACTIVE
        instance_1.host = 'not-' + our_host
        instance_2 = objects.Instance(self.context)
        instance_2.uuid = uuids.instance_2
        instance_2.task_state = None
        instance_2.vm_state = vm_states.ACTIVE
        instance_2.host = 'not-' + our_host

        # Only instance 2 has a migration record
        migration = objects.Migration(instance_uuid=instance_2.uuid)
        # Consider the migration successful
        migration.status = 'done'

        with test.nested(
            mock.patch.object(self.compute, '_get_instances_on_driver',
                               return_value=[instance_1,
                                             instance_2]),
            mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                               return_value=None),
            mock.patch.object(self.compute, '_get_instance_block_device_info',
                               return_value={}),
            mock.patch.object(self.compute, '_is_instance_storage_shared',
                               return_value=False),
            mock.patch.object(self.compute.driver, 'destroy'),
            mock.patch('nova.objects.MigrationList.get_by_filters'),
            mock.patch('nova.objects.Migration.save')
        ) as (_get_instances_on_driver, get_instance_nw_info,
              _get_instance_block_device_info, _is_instance_storage_shared,
              destroy, migration_list, migration_save):
            migration_list.return_value = [migration]
            self.compute._destroy_evacuated_instances(self.context)
            # Only instance 2 should be deleted. Instance 1 is still running
            # here, but no migration from our host exists, so ignore it
            destroy.assert_called_once_with(self.context, instance_2, None,
                                            {}, True)

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_destroy_evacuated_instances')
    @mock.patch('nova.compute.manager.LOG')
    def test_init_host_foreign_instance(self, mock_log, mock_destroy):
        inst = mock.MagicMock()
        inst.host = self.compute.host + '-alt'
        self.compute._init_instance(mock.sentinel.context, inst)
        self.assertFalse(inst.save.called)
        self.assertTrue(mock_log.warning.called)
        msg = mock_log.warning.call_args_list[0]
        self.assertIn('appears to not be owned by this host', msg[0][0])

    def test_init_host_pci_passthrough_whitelist_validation_failure(self):
        # Tests that we fail init_host if there is a pci_passthrough_whitelist
        # configured incorrectly.
        self.flags(pci_passthrough_whitelist=[
            # it's invalid to specify both in the same devspec
            jsonutils.dumps({'address': 'foo', 'devname': 'bar'})])
        self.assertRaises(exception.PciDeviceInvalidDeviceName,
                          self.compute.init_host)

    @mock.patch('nova.compute.manager.ComputeManager._instance_update')
    def test_error_out_instance_on_exception_not_implemented_err(self,
                                                        inst_update_mock):
        instance = fake_instance.fake_instance_obj(self.context)

        def do_test():
            with self.compute._error_out_instance_on_exception(
                    self.context, instance, instance_state=vm_states.STOPPED):
                raise NotImplementedError('test')

        self.assertRaises(NotImplementedError, do_test)
        inst_update_mock.assert_called_once_with(
            self.context, instance,
            vm_state=vm_states.STOPPED, task_state=None)

    @mock.patch('nova.compute.manager.ComputeManager._instance_update')
    def test_error_out_instance_on_exception_inst_fault_rollback(self,
                                                        inst_update_mock):
        instance = fake_instance.fake_instance_obj(self.context)

        def do_test():
            with self.compute._error_out_instance_on_exception(self.context,
                                                               instance):
                raise exception.InstanceFaultRollback(
                    inner_exception=test.TestingException('test'))

        self.assertRaises(test.TestingException, do_test)
        inst_update_mock.assert_called_once_with(
            self.context, instance,
            vm_state=vm_states.ACTIVE, task_state=None)

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_set_instance_obj_error_state')
    def test_error_out_instance_on_exception_unknown_with_quotas(self,
                                                                 set_error):
        instance = fake_instance.fake_instance_obj(self.context)
        quotas = mock.create_autospec(objects.Quotas, spec_set=True)

        def do_test():
            with self.compute._error_out_instance_on_exception(
                    self.context, instance, quotas):
                raise test.TestingException('test')

        self.assertRaises(test.TestingException, do_test)
        self.assertEqual(1, len(quotas.method_calls))
        self.assertEqual(mock.call.rollback(), quotas.method_calls[0])
        set_error.assert_called_once_with(self.context, instance)

    def test_cleanup_volumes(self):
        instance = fake_instance.fake_instance_obj(self.context)
        bdm_do_not_delete_dict = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id1', 'source_type': 'image',
                'delete_on_termination': False})
        bdm_delete_dict = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id2', 'source_type': 'image',
                'delete_on_termination': True})
        bdms = block_device_obj.block_device_make_list(self.context,
            [bdm_do_not_delete_dict, bdm_delete_dict])

        with mock.patch.object(self.compute.volume_api,
                'delete') as volume_delete:
            self.compute._cleanup_volumes(self.context, instance.uuid, bdms)
            volume_delete.assert_called_once_with(self.context,
                    bdms[1].volume_id)

    def test_cleanup_volumes_exception_do_not_raise(self):
        instance = fake_instance.fake_instance_obj(self.context)
        bdm_dict1 = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id1', 'source_type': 'image',
                'delete_on_termination': True})
        bdm_dict2 = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id2', 'source_type': 'image',
                'delete_on_termination': True})
        bdms = block_device_obj.block_device_make_list(self.context,
            [bdm_dict1, bdm_dict2])

        with mock.patch.object(self.compute.volume_api,
                'delete',
                side_effect=[test.TestingException(), None]) as volume_delete:
            self.compute._cleanup_volumes(self.context, instance.uuid, bdms,
                    raise_exc=False)
            calls = [mock.call(self.context, bdm.volume_id) for bdm in bdms]
            self.assertEqual(calls, volume_delete.call_args_list)

    def test_cleanup_volumes_exception_raise(self):
        instance = fake_instance.fake_instance_obj(self.context)
        bdm_dict1 = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id1', 'source_type': 'image',
                'delete_on_termination': True})
        bdm_dict2 = fake_block_device.FakeDbBlockDeviceDict(
            {'volume_id': 'fake-id2', 'source_type': 'image',
                'delete_on_termination': True})
        bdms = block_device_obj.block_device_make_list(self.context,
            [bdm_dict1, bdm_dict2])

        with mock.patch.object(self.compute.volume_api,
                'delete',
                side_effect=[test.TestingException(), None]) as volume_delete:
            self.assertRaises(test.TestingException,
                    self.compute._cleanup_volumes, self.context, instance.uuid,
                    bdms)
            calls = [mock.call(self.context, bdm.volume_id) for bdm in bdms]
            self.assertEqual(calls, volume_delete.call_args_list)

    def test_stop_instance_task_state_none_power_state_shutdown(self):
        # Tests that stop_instance doesn't puke when the instance power_state
        # is shutdown and the task_state is None.
        instance = fake_instance.fake_instance_obj(
            self.context, vm_state=vm_states.ACTIVE,
            task_state=None, power_state=power_state.SHUTDOWN)

        @mock.patch.object(self.compute, '_get_power_state',
                           return_value=power_state.SHUTDOWN)
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        @mock.patch.object(self.compute, '_power_off_instance')
        @mock.patch.object(instance, 'save')
        def do_test(save_mock, power_off_mock, notify_mock, get_state_mock):
            # run the code
            self.compute.stop_instance(self.context, instance, True)
            # assert the calls
            self.assertEqual(2, get_state_mock.call_count)
            notify_mock.assert_has_calls([
                mock.call(self.context, instance, 'power_off.start'),
                mock.call(self.context, instance, 'power_off.end')
            ])
            power_off_mock.assert_called_once_with(
                self.context, instance, True)
            save_mock.assert_called_once_with(
                expected_task_state=[task_states.POWERING_OFF, None])
            self.assertEqual(power_state.SHUTDOWN, instance.power_state)
            self.assertIsNone(instance.task_state)
            self.assertEqual(vm_states.STOPPED, instance.vm_state)

        do_test()

    def test_reset_network_driver_not_implemented(self):
        instance = fake_instance.fake_instance_obj(self.context)

        @mock.patch.object(self.compute.driver, 'reset_network',
                           side_effect=NotImplementedError())
        @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
        def do_test(mock_add_fault, mock_reset):
            self.assertRaises(messaging.ExpectedException,
                              self.compute.reset_network,
                              self.context,
                              instance)

            self.compute = utils.ExceptionHelper(self.compute)

            self.assertRaises(NotImplementedError,
                              self.compute.reset_network,
                              self.context,
                              instance)

        do_test()

    def _test_rebuild_ex(self, instance, ex):
        # Test that we do not raise on certain exceptions
        with test.nested(
            mock.patch.object(self.compute, '_get_compute_info'),
            mock.patch.object(self.compute, '_do_rebuild_instance_with_claim',
                              side_effect=ex),
            mock.patch.object(self.compute, '_set_migration_status'),
            mock.patch.object(self.compute, '_notify_about_instance_usage')
        ) as (mock_get, mock_rebuild, mock_set, mock_notify):
            self.compute.rebuild_instance(self.context, instance, None, None,
                                          None, None, None, None, None)
            mock_set.assert_called_once_with(None, 'failed')
            mock_notify.assert_called_once_with(mock.ANY, instance,
                                                'rebuild.error', fault=ex)

    def test_rebuild_deleting(self):
        instance = objects.Instance(uuid=uuids.instance)
        ex = exception.UnexpectedDeletingTaskStateError(
            instance_uuid=instance.uuid, expected='expected', actual='actual')
        self._test_rebuild_ex(instance, ex)

    def test_rebuild_notfound(self):
        instance = objects.Instance(uuid=uuids.instance)
        ex = exception.InstanceNotFound(instance_id=instance.uuid)
        self._test_rebuild_ex(instance, ex)

    def test_rebuild_default_impl(self):
        def _detach(context, bdms):
            # NOTE(rpodolyaka): check that instance has been powered off by
            # the time we detach block devices, exact calls arguments will be
            # checked below
            self.assertTrue(mock_power_off.called)
            self.assertFalse(mock_destroy.called)

        def _attach(context, instance, bdms, do_check_attach=True):
            return {'block_device_mapping': 'shared_block_storage'}

        def _spawn(context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
            self.assertEqual(block_device_info['block_device_mapping'],
                             'shared_block_storage')

        with test.nested(
            mock.patch.object(self.compute.driver, 'destroy',
                              return_value=None),
            mock.patch.object(self.compute.driver, 'spawn',
                              side_effect=_spawn),
            mock.patch.object(objects.Instance, 'save',
                              return_value=None),
            mock.patch.object(self.compute, '_power_off_instance',
                              return_value=None)
        ) as(
             mock_destroy,
             mock_spawn,
             mock_save,
             mock_power_off
        ):
            instance = fake_instance.fake_instance_obj(self.context)
            instance.migration_context = None
            instance.numa_topology = None
            instance.pci_requests = None
            instance.pci_devices = None
            instance.device_metadata = None
            instance.task_state = task_states.REBUILDING
            instance.save(expected_task_state=[task_states.REBUILDING])
            self.compute._rebuild_default_impl(self.context,
                                               instance,
                                               None,
                                               [],
                                               admin_password='new_pass',
                                               bdms=[],
                                               detach_block_devices=_detach,
                                               attach_block_devices=_attach,
                                               network_info=None,
                                               recreate=False,
                                               block_device_info=None,
                                               preserve_ephemeral=False)

            self.assertTrue(mock_save.called)
            self.assertTrue(mock_spawn.called)
            mock_destroy.assert_called_once_with(
                self.context, instance,
                network_info=None, block_device_info=None)
            mock_power_off.assert_called_once_with(
                self.context, instance, clean_shutdown=True)

    @mock.patch.object(utils, 'last_completed_audit_period',
            return_value=(0, 0))
    @mock.patch.object(time, 'time', side_effect=[10, 20, 21])
    @mock.patch.object(objects.InstanceList, 'get_by_host', return_value=[])
    @mock.patch.object(objects.BandwidthUsage, 'get_by_instance_uuid_and_mac')
    @mock.patch.object(db, 'bw_usage_update')
    def test_poll_bandwidth_usage(self, bw_usage_update, get_by_uuid_mac,
            get_by_host, time, last_completed_audit):
        bw_counters = [{'uuid': uuids.instance, 'mac_address': 'fake-mac',
                        'bw_in': 1, 'bw_out': 2}]
        usage = objects.BandwidthUsage()
        usage.bw_in = 3
        usage.bw_out = 4
        usage.last_ctr_in = 0
        usage.last_ctr_out = 0
        self.flags(bandwidth_poll_interval=1)
        get_by_uuid_mac.return_value = usage
        _time = timeutils.utcnow()
        bw_usage_update.return_value = {'uuid': uuids.instance, 'mac': '',
                'start_period': _time, 'last_refreshed': _time, 'bw_in': 0,
                'bw_out': 0, 'last_ctr_in': 0, 'last_ctr_out': 0, 'deleted': 0,
                'created_at': _time, 'updated_at': _time, 'deleted_at': _time}
        with mock.patch.object(self.compute.driver,
                'get_all_bw_counters', return_value=bw_counters):
            self.compute._poll_bandwidth_usage(self.context)
            get_by_uuid_mac.assert_called_once_with(self.context,
                    uuids.instance, 'fake-mac',
                    start_period=0, use_slave=True)
            # NOTE(sdague): bw_usage_update happens at some time in
            # the future, so what last_refreshed is irrelevant.
            bw_usage_update.assert_called_once_with(self.context,
                    uuids.instance,
                    'fake-mac', 0, 4, 6, 1, 2,
                    last_refreshed=mock.ANY,
                    update_cells=False)

    def test_reverts_task_state_instance_not_found(self):
        # Tests that the reverts_task_state decorator in the compute manager
        # will not trace when an InstanceNotFound is raised.
        instance = objects.Instance(uuid=uuids.instance, task_state="FAKE")
        instance_update_mock = mock.Mock(
            side_effect=exception.InstanceNotFound(instance_id=instance.uuid))
        self.compute._instance_update = instance_update_mock

        log_mock = mock.Mock()
        manager.LOG = log_mock

        @manager.reverts_task_state
        def fake_function(self, context, instance):
            raise test.TestingException()

        self.assertRaises(test.TestingException, fake_function,
                          self, self.context, instance)

        self.assertFalse(log_mock.called)

    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'update_instance_info')
    def test_update_scheduler_instance_info(self, mock_update):
        instance = objects.Instance(uuid=uuids.instance)
        self.compute._update_scheduler_instance_info(self.context, instance)
        self.assertEqual(mock_update.call_count, 1)
        args = mock_update.call_args[0]
        self.assertNotEqual(args[0], self.context)
        self.assertIsInstance(args[0], self.context.__class__)
        self.assertEqual(args[1], self.compute.host)
        # Send a single instance; check that the method converts to an
        # InstanceList
        self.assertIsInstance(args[2], objects.InstanceList)
        self.assertEqual(args[2].objects[0], instance)

    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'delete_instance_info')
    def test_delete_scheduler_instance_info(self, mock_delete):
        self.compute._delete_scheduler_instance_info(self.context,
                                                     mock.sentinel.inst_uuid)
        self.assertEqual(mock_delete.call_count, 1)
        args = mock_delete.call_args[0]
        self.assertNotEqual(args[0], self.context)
        self.assertIsInstance(args[0], self.context.__class__)
        self.assertEqual(args[1], self.compute.host)
        self.assertEqual(args[2], mock.sentinel.inst_uuid)

    @mock.patch.object(nova.context.RequestContext, 'elevated')
    @mock.patch.object(nova.objects.InstanceList, 'get_by_host')
    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'sync_instance_info')
    def test_sync_scheduler_instance_info(self, mock_sync, mock_get_by_host,
            mock_elevated):
        inst1 = objects.Instance(uuid=uuids.instance_1)
        inst2 = objects.Instance(uuid=uuids.instance_2)
        inst3 = objects.Instance(uuid=uuids.instance_3)
        exp_uuids = [inst.uuid for inst in [inst1, inst2, inst3]]
        mock_get_by_host.return_value = objects.InstanceList(
                objects=[inst1, inst2, inst3])
        fake_elevated = context.get_admin_context()
        mock_elevated.return_value = fake_elevated
        self.compute._sync_scheduler_instance_info(self.context)
        mock_get_by_host.assert_called_once_with(
                fake_elevated, self.compute.host, expected_attrs=[],
                use_slave=True)
        mock_sync.assert_called_once_with(fake_elevated, self.compute.host,
                                          exp_uuids)

    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'sync_instance_info')
    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'delete_instance_info')
    @mock.patch.object(nova.scheduler.client.SchedulerClient,
                       'update_instance_info')
    def test_scheduler_info_updates_off(self, mock_update, mock_delete,
                                        mock_sync):
        mgr = self.compute
        mgr.send_instance_updates = False
        mgr._update_scheduler_instance_info(self.context,
                                            mock.sentinel.instance)
        mgr._delete_scheduler_instance_info(self.context,
                                            mock.sentinel.instance_uuid)
        mgr._sync_scheduler_instance_info(self.context)
        # None of the calls should have been made
        self.assertFalse(mock_update.called)
        self.assertFalse(mock_delete.called)
        self.assertFalse(mock_sync.called)

    def test_refresh_instance_security_rules_takes_non_object(self):
        inst = fake_instance.fake_db_instance()
        with mock.patch.object(self.compute.driver,
                               'refresh_instance_security_rules') as mock_r:
            self.compute.refresh_instance_security_rules(self.context, inst)
            self.assertIsInstance(mock_r.call_args_list[0][0][0],
                                  objects.Instance)

    def test_set_instance_obj_error_state_with_clean_task_state(self):
        instance = fake_instance.fake_instance_obj(self.context,
            vm_state=vm_states.BUILDING, task_state=task_states.SPAWNING)
        with mock.patch.object(instance, 'save'):
            self.compute._set_instance_obj_error_state(self.context, instance,
                                                       clean_task_state=True)
            self.assertEqual(vm_states.ERROR, instance.vm_state)
            self.assertIsNone(instance.task_state)

    def test_set_instance_obj_error_state_by_default(self):
        instance = fake_instance.fake_instance_obj(self.context,
            vm_state=vm_states.BUILDING, task_state=task_states.SPAWNING)
        with mock.patch.object(instance, 'save'):
            self.compute._set_instance_obj_error_state(self.context, instance)
            self.assertEqual(vm_states.ERROR, instance.vm_state)
            self.assertEqual(task_states.SPAWNING, instance.task_state)

    @mock.patch.object(objects.Instance, 'save')
    def test_instance_update(self, mock_save):
        instance = objects.Instance(task_state=task_states.SCHEDULING,
                                    vm_state=vm_states.BUILDING)
        updates = {'task_state': None, 'vm_state': vm_states.ERROR}

        with mock.patch.object(self.compute,
                               '_update_resource_tracker') as mock_rt:
            self.compute._instance_update(self.context, instance, **updates)

            self.assertIsNone(instance.task_state)
            self.assertEqual(vm_states.ERROR, instance.vm_state)
            mock_save.assert_called_once_with()
            mock_rt.assert_called_once_with(self.context, instance)

    def test_reset_reloads_rpcapi(self):
        orig_rpc = self.compute.compute_rpcapi
        with mock.patch('nova.compute.rpcapi.ComputeAPI') as mock_rpc:
            self.compute.reset()
            mock_rpc.assert_called_once_with()
            self.assertIsNot(orig_rpc, self.compute.compute_rpcapi)

    @mock.patch('nova.objects.BlockDeviceMappingList.get_by_instance_uuid')
    @mock.patch('nova.compute.manager.ComputeManager._delete_instance')
    def test_terminate_instance_no_bdm_volume_id(self, mock_delete_instance,
                                                 mock_bdm_get_by_inst):
        # Tests that we refresh the bdm list if a volume bdm does not have the
        # volume_id set.
        instance = fake_instance.fake_instance_obj(
            self.context, vm_state=vm_states.ERROR,
            task_state=task_states.DELETING)
        bdm = fake_block_device.FakeDbBlockDeviceDict(
            {'source_type': 'snapshot', 'destination_type': 'volume',
             'instance_uuid': instance.uuid, 'device_name': '/dev/vda'})
        bdms = block_device_obj.block_device_make_list(self.context, [bdm])
        # since the bdms passed in don't have a volume_id, we'll go back to the
        # database looking for updated versions
        mock_bdm_get_by_inst.return_value = bdms
        self.compute.terminate_instance(self.context, instance, bdms, [])
        mock_bdm_get_by_inst.assert_called_once_with(
            self.context, instance.uuid)
        mock_delete_instance.assert_called_once_with(
            self.context, instance, bdms, mock.ANY)

    @mock.patch.object(nova.compute.manager.ComputeManager,
                       '_notify_about_instance_usage')
    def test_trigger_crash_dump(self, notify_mock):
        instance = fake_instance.fake_instance_obj(
            self.context, vm_state=vm_states.ACTIVE)

        self.compute.trigger_crash_dump(self.context, instance)

        notify_mock.assert_has_calls([
            mock.call(self.context, instance, 'trigger_crash_dump.start'),
            mock.call(self.context, instance, 'trigger_crash_dump.end')
        ])
        self.assertIsNone(instance.task_state)
        self.assertEqual(vm_states.ACTIVE, instance.vm_state)

    def test_instance_restore_notification(self):
        inst_obj = fake_instance.fake_instance_obj(self.context,
            vm_state=vm_states.SOFT_DELETED)
        with test.nested(
            mock.patch.object(nova.compute.utils,
                              'notify_about_instance_action'),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(objects.Instance, 'save'),
            mock.patch.object(self.compute.driver, 'restore')
        ) as (fake_notify, fake_notify_usage, fake_save, fake_restore):
            self.compute.restore_instance(self.context, inst_obj)
            fake_notify.assert_has_calls([
                mock.call(self.context, inst_obj, 'fake-mini',
                          action='restore', phase='start'),
                mock.call(self.context, inst_obj, 'fake-mini',
                          action='restore', phase='end')])


class ComputeManagerBuildInstanceTestCase(test.NoDBTestCase):
    def setUp(self):
        super(ComputeManagerBuildInstanceTestCase, self).setUp()
        self.compute = importutils.import_object(CONF.compute_manager)
        self.context = context.RequestContext(fakes.FAKE_USER_ID,
                                              fakes.FAKE_PROJECT_ID)
        self.instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE,
                expected_attrs=['metadata', 'system_metadata', 'info_cache'])
        self.admin_pass = 'pass'
        self.injected_files = []
        self.image = {}
        self.node = 'fake-node'
        self.limits = {}
        self.requested_networks = []
        self.security_groups = []
        self.block_device_mapping = []
        self.filter_properties = {'retry': {'num_attempts': 1,
                                            'hosts': [[self.compute.host,
                                                       'fake-node']]}}

        self.useFixture(fixtures.SpawnIsSynchronousFixture())

        def fake_network_info():
            return network_model.NetworkInfo([{'address': '1.2.3.4'}])

        self.network_info = network_model.NetworkInfoAsyncWrapper(
                fake_network_info)
        self.block_device_info = self.compute._prep_block_device(context,
                self.instance, self.block_device_mapping)

        # override tracker with a version that doesn't need the database:
        fake_rt = fake_resource_tracker.FakeResourceTracker(self.compute.host,
                    self.compute.driver, self.node)
        self.compute._resource_tracker_dict[self.node] = fake_rt

    def _do_build_instance_update(self, mock_save, reschedule_update=False):
        mock_save.return_value = self.instance
        if reschedule_update:
            mock_save.side_effect = (self.instance, self.instance)

    @staticmethod
    def _assert_build_instance_update(mock_save,
                                      reschedule_update=False):
        if reschedule_update:
            mock_save.assert_has_calls([
                mock.call(expected_task_state=(task_states.SCHEDULING, None)),
                mock.call()])
        else:
            mock_save.assert_called_once_with(expected_task_state=
                                              (task_states.SCHEDULING, None))

    def _instance_action_events(self, mock_start, mock_finish):
        mock_start.assert_called_once_with(self.context, self.instance.uuid,
                mock.ANY, want_result=False)
        mock_finish.assert_called_once_with(self.context, self.instance.uuid,
                mock.ANY, exc_val=mock.ANY, exc_tb=mock.ANY, want_result=False)

    @staticmethod
    def _assert_build_instance_hook_called(mock_hooks, result):
        # NOTE(coreywright): we want to test the return value of
        # _do_build_and_run_instance, but it doesn't bubble all the way up, so
        # mock the hooking, which allows us to test that too, though a little
        # too intimately
        mock_hooks.setdefault().run_post.assert_called_once_with(
            'build_instance', result, mock.ANY, mock.ANY, f=None)

    def test_build_and_run_instance_called_with_proper_args(self):
        self._test_build_and_run_instance()

    def test_build_and_run_instance_with_unlimited_max_concurrent_builds(self):
        self.flags(max_concurrent_builds=0)
        self.compute = importutils.import_object(CONF.compute_manager)
        self._test_build_and_run_instance()

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def _test_build_and_run_instance(self, mock_hooks, mock_build, mock_save,
                                     mock_start, mock_finish):
        self._do_build_instance_update(mock_save)

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.ACTIVE)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save)
        mock_build.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)

    # This test when sending an icehouse compatible rpc call to juno compute
    # node, NetworkRequest object can load from three items tuple.
    @mock.patch('nova.objects.Instance.save')
    @mock.patch('nova.compute.manager.ComputeManager._build_and_run_instance')
    def test_build_and_run_instance_with_icehouse_requested_network(
            self, mock_build_and_run, mock_save):
        mock_save.return_value = self.instance
        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=[objects.NetworkRequest(
                    network_id='fake_network_id',
                    address='10.0.0.1',
                    port_id=uuids.port_instance)],
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)
        requested_network = mock_build_and_run.call_args[0][5][0]
        self.assertEqual('fake_network_id', requested_network.network_id)
        self.assertEqual('10.0.0.1', str(requested_network.address))
        self.assertEqual(uuids.port_instance, requested_network.port_id)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(manager.ComputeManager, '_cleanup_volumes')
    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.ComputeManager, '_set_instance_obj_error_state')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def test_build_abort_exception(self, mock_hooks, mock_build_run,
                                   mock_build, mock_set, mock_nil, mock_add,
                                   mock_clean_vol, mock_clean_net, mock_save,
                                   mock_start, mock_finish):
        self._do_build_instance_update(mock_save)
        mock_build_run.side_effect = exception.BuildAbortException(reason='',
                                        instance_uuid=self.instance.uuid)

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save)
        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.FAILED)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)
        mock_clean_net.assert_called_once_with(self.context, self.instance,
                self.requested_networks)
        mock_clean_vol.assert_called_once_with(self.context,
                self.instance.uuid, self.block_device_mapping, raise_exc=False)
        mock_add.assert_called_once_with(self.context, self.instance,
                mock.ANY, mock.ANY)
        mock_nil.assert_called_once_with(self.instance)
        mock_set.assert_called_once_with(self.context, self.instance,
                clean_task_state=True)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(network_api.API, 'cleanup_instance_network_on_host')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.ComputeManager, '_set_instance_obj_error_state')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def test_rescheduled_exception(self, mock_hooks, mock_build_run,
                                   mock_build, mock_set, mock_nil, mock_clean,
                                   mock_save, mock_start, mock_finish):
        self._do_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.side_effect = exception.RescheduledException(reason='',
                instance_uuid=self.instance.uuid)

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.RESCHEDULED)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)
        mock_clean.assert_called_once_with(self.context, self.instance,
                self.compute.host)
        mock_nil.assert_called_once_with(self.instance)
        mock_build.assert_called_once_with(self.context,
                [self.instance], self.image, self.filter_properties,
                self.admin_pass, self.injected_files, self.requested_networks,
                self.security_groups, self.block_device_mapping)

    @mock.patch.object(manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(fake_driver.FakeDriver, 'spawn')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    def test_rescheduled_exception_with_non_ascii_exception(self,
            mock_notify, mock_save, mock_spawn, mock_build, mock_shutdown):
        exc = exception.NovaException(u's\xe9quence')

        mock_build.return_value = self.network_info
        mock_spawn.side_effect = exc

        self.assertRaises(exception.RescheduledException,
                          self.compute._build_and_run_instance,
                          self.context, self.instance, self.image,
                          self.injected_files, self.admin_pass,
                          self.requested_networks, self.security_groups,
                          self.block_device_mapping, self.node,
                          self.limits, self.filter_properties)
        mock_save.assert_has_calls([
            mock.call(),
            mock.call(),
            mock.call(expected_task_state='block_device_mapping'),
        ])
        mock_notify.assert_has_calls([
            mock.call(self.context, self.instance, 'create.start',
                extra_usage_info={'image_name': self.image.get('name')}),
            mock.call(self.context, self.instance, 'create.error', fault=exc)])
        mock_build.assert_called_once_with(self.context, self.instance,
            self.requested_networks, self.security_groups)
        mock_shutdown.assert_called_once_with(self.context, self.instance,
            self.block_device_mapping, self.requested_networks,
            try_deallocate_networks=False)
        mock_spawn.assert_called_once_with(self.context, self.instance,
            test.MatchType(objects.ImageMeta), self.injected_files,
            self.admin_pass, network_info=self.network_info,
            block_device_info=self.block_device_info)

    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(network_api.API, 'cleanup_instance_network_on_host')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(virt_driver.ComputeDriver, 'macs_for_instance')
    def test_rescheduled_exception_with_network_allocated(self,
            mock_macs_for_instance, mock_event_finish,
            mock_event_start, mock_ins_save, mock_cleanup_network,
            mock_build_ins, mock_build_and_run):
        instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE,
                system_metadata={'network_allocated': 'True'},
                expected_attrs=['metadata', 'system_metadata', 'info_cache'])
        mock_ins_save.return_value = instance
        mock_macs_for_instance.return_value = []
        mock_build_and_run.side_effect = exception.RescheduledException(
            reason='', instance_uuid=self.instance.uuid)

        self.compute._do_build_and_run_instance(self.context, instance,
            self.image, request_spec={},
            filter_properties=self.filter_properties,
            injected_files=self.injected_files,
            admin_password=self.admin_pass,
            requested_networks=self.requested_networks,
            security_groups=self.security_groups,
            block_device_mapping=self.block_device_mapping, node=self.node,
            limits=self.limits)

        mock_build_and_run.assert_called_once_with(self.context,
            instance,
            self.image, self.injected_files, self.admin_pass,
            self.requested_networks, self.security_groups,
            self.block_device_mapping, self.node, self.limits,
            self.filter_properties)
        mock_cleanup_network.assert_called_once_with(
            self.context, instance, self.compute.host)
        mock_build_ins.assert_called_once_with(self.context,
            [instance], self.image, self.filter_properties,
            self.admin_pass, self.injected_files, self.requested_networks,
            self.security_groups, self.block_device_mapping)

    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(virt_driver.ComputeDriver, 'macs_for_instance')
    def test_rescheduled_exception_with_sriov_network_allocated(self,
            mock_macs_for_instance, mock_event_finish,
            mock_event_start, mock_ins_save, mock_cleanup_network,
            mock_build_ins, mock_build_and_run):
        vif1 = fake_network_cache_model.new_vif()
        vif1['id'] = '1'
        vif1['vnic_type'] = network_model.VNIC_TYPE_NORMAL
        vif2 = fake_network_cache_model.new_vif()
        vif2['id'] = '2'
        vif1['vnic_type'] = network_model.VNIC_TYPE_DIRECT
        nw_info = network_model.NetworkInfo([vif1, vif2])
        instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE,
                system_metadata={'network_allocated': 'True'},
                expected_attrs=['metadata', 'system_metadata', 'info_cache'])
        info_cache = objects.InstanceInfoCache(network_info=nw_info,
                                               instance_uuid=instance.uuid)
        instance.info_cache = info_cache

        mock_ins_save.return_value = instance
        mock_macs_for_instance.return_value = []
        mock_build_and_run.side_effect = exception.RescheduledException(
            reason='', instance_uuid=self.instance.uuid)

        self.compute._do_build_and_run_instance(self.context, instance,
            self.image, request_spec={},
            filter_properties=self.filter_properties,
            injected_files=self.injected_files,
            admin_password=self.admin_pass,
            requested_networks=self.requested_networks,
            security_groups=self.security_groups,
            block_device_mapping=self.block_device_mapping, node=self.node,
            limits=self.limits)

        mock_build_and_run.assert_called_once_with(self.context,
            instance,
            self.image, self.injected_files, self.admin_pass,
            self.requested_networks, self.security_groups,
            self.block_device_mapping, self.node, self.limits,
            self.filter_properties)
        mock_cleanup_network.assert_called_once_with(
            self.context, instance, self.requested_networks)
        mock_build_ins.assert_called_once_with(self.context,
            [instance], self.image, self.filter_properties,
            self.admin_pass, self.injected_files, self.requested_networks,
            self.security_groups, self.block_device_mapping)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.ComputeManager, '_cleanup_volumes')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(manager.ComputeManager, '_set_instance_obj_error_state')
    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def test_rescheduled_exception_without_retry(self, mock_hooks,
            mock_build_run, mock_add, mock_set, mock_clean_net, mock_clean_vol,
            mock_nil, mock_save, mock_start, mock_finish):
        self._do_build_instance_update(mock_save)
        mock_build_run.side_effect = exception.RescheduledException(reason='',
                instance_uuid=self.instance.uuid)

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties={},
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                build_results.FAILED)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits, {})
        mock_clean_net.assert_called_once_with(self.context, self.instance,
                self.requested_networks)
        mock_add.assert_called_once_with(self.context, self.instance,
                mock.ANY, mock.ANY, fault_message=mock.ANY)
        mock_nil.assert_called_once_with(self.instance)
        mock_set.assert_called_once_with(self.context, self.instance,
                clean_task_state=True)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(network_api.API, 'cleanup_instance_network_on_host')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(fake_driver.FakeDriver,
                       'deallocate_networks_on_reschedule')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def test_rescheduled_exception_do_not_deallocate_network(self, mock_hooks,
            mock_build_run, mock_build, mock_deallocate, mock_nil,
            mock_clean_net, mock_clean_inst, mock_save, mock_start,
            mock_finish):
        self._do_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.side_effect = exception.RescheduledException(reason='',
                instance_uuid=self.instance.uuid)
        mock_deallocate.return_value = False

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.RESCHEDULED)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)
        mock_deallocate.assert_called_once_with(self.instance)
        mock_clean_inst.assert_called_once_with(self.context, self.instance,
                self.compute.host)
        mock_nil.assert_called_once_with(self.instance)
        mock_build.assert_called_once_with(self.context,
                [self.instance], self.image, self.filter_properties,
                self.admin_pass, self.injected_files, self.requested_networks,
                self.security_groups, self.block_device_mapping)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(fake_driver.FakeDriver,
                       'deallocate_networks_on_reschedule')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def test_rescheduled_exception_deallocate_network(self, mock_hooks,
            mock_build_run, mock_build, mock_deallocate, mock_nil, mock_clean,
            mock_save, mock_start, mock_finish):
        self._do_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.side_effect = exception.RescheduledException(reason='',
                instance_uuid=self.instance.uuid)
        mock_deallocate.return_value = True

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.RESCHEDULED)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save, reschedule_update=True)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)
        mock_deallocate.assert_called_once_with(self.instance)
        mock_clean.assert_called_once_with(self.context, self.instance,
                self.requested_networks)
        mock_nil.assert_called_once_with(self.instance)
        mock_build.assert_called_once_with(self.context,
                [self.instance], self.image, self.filter_properties,
                self.admin_pass, self.injected_files, self.requested_networks,
                self.security_groups, self.block_device_mapping)

    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_cleanup_allocated_networks')
    @mock.patch.object(manager.ComputeManager, '_cleanup_volumes')
    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(manager.ComputeManager, '_set_instance_obj_error_state')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_build_and_run_instance')
    @mock.patch('nova.hooks._HOOKS')
    def _test_build_and_run_exceptions(self, exc, mock_hooks, mock_build_run,
                mock_build, mock_set, mock_nil, mock_add, mock_clean_vol,
                mock_clean_net, mock_save, mock_start, mock_finish,
                set_error=False, cleanup_volumes=False,
                nil_out_host_and_node=False):
        self._do_build_instance_update(mock_save)
        mock_build_run.side_effect = exc

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._assert_build_instance_hook_called(mock_hooks,
                                                build_results.FAILED)
        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save)
        if cleanup_volumes:
            mock_clean_vol.assert_called_once_with(self.context,
                    self.instance.uuid, self.block_device_mapping,
                    raise_exc=False)
        if nil_out_host_and_node:
            mock_nil.assert_called_once_with(self.instance)
        if set_error:
            mock_add.assert_called_once_with(self.context, self.instance,
                    mock.ANY, mock.ANY)
            mock_set.assert_called_once_with(self.context,
                    self.instance, clean_task_state=True)
        mock_build_run.assert_called_once_with(self.context, self.instance,
                self.image, self.injected_files, self.admin_pass,
                self.requested_networks, self.security_groups,
                self.block_device_mapping, self.node, self.limits,
                self.filter_properties)
        mock_clean_net.assert_called_once_with(self.context, self.instance,
                self.requested_networks)

    def test_build_and_run_notfound_exception(self):
        self._test_build_and_run_exceptions(exception.InstanceNotFound(
            instance_id=''))

    def test_build_and_run_unexpecteddeleting_exception(self):
        self._test_build_and_run_exceptions(
                exception.UnexpectedDeletingTaskStateError(
                    instance_uuid=uuids.instance, expected={}, actual={}))

    def test_build_and_run_buildabort_exception(self):
        self._test_build_and_run_exceptions(
            exception.BuildAbortException(instance_uuid='', reason=''),
            set_error=True, cleanup_volumes=True, nil_out_host_and_node=True)

    def test_build_and_run_unhandled_exception(self):
        self._test_build_and_run_exceptions(test.TestingException(),
                set_error=True, cleanup_volumes=True,
                nil_out_host_and_node=True)

    @mock.patch.object(manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(fake_driver.FakeDriver, 'spawn')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    def test_instance_not_found(self, mock_notify, mock_save, mock_spawn,
                                mock_build, mock_shutdown):
        exc = exception.InstanceNotFound(instance_id=1)
        mock_build.return_value = self.network_info
        mock_spawn.side_effect = exc

        self.assertRaises(exception.InstanceNotFound,
                          self.compute._build_and_run_instance,
                          self.context, self.instance, self.image,
                          self.injected_files, self.admin_pass,
                          self.requested_networks, self.security_groups,
                          self.block_device_mapping, self.node,
                          self.limits, self.filter_properties)

        mock_save.assert_has_calls([
            mock.call(),
            mock.call(),
            mock.call(expected_task_state='block_device_mapping')])
        mock_notify.assert_has_calls([
            mock.call(self.context, self.instance, 'create.start',
                extra_usage_info={'image_name': self.image.get('name')}),
            mock.call(self.context, self.instance, 'create.error',
                fault=exc)])
        mock_build.assert_called_once_with(self.context, self.instance,
            self.requested_networks, self.security_groups)
        mock_shutdown.assert_called_once_with(self.context, self.instance,
            self.block_device_mapping, self.requested_networks,
            try_deallocate_networks=False)
        mock_spawn.assert_called_once_with(self.context, self.instance,
            test.MatchType(objects.ImageMeta), self.injected_files,
            self.admin_pass, network_info=self.network_info,
            block_device_info=self.block_device_info)

    @mock.patch.object(manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(fake_driver.FakeDriver, 'spawn')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    def test_reschedule_on_exception(self, mock_notify, mock_save,
                                     mock_spawn, mock_build, mock_shutdown):
        exc = test.TestingException()
        mock_build.return_value = self.network_info
        mock_spawn.side_effect = exc

        self.assertRaises(exception.RescheduledException,
                          self.compute._build_and_run_instance,
                          self.context, self.instance, self.image,
                          self.injected_files, self.admin_pass,
                          self.requested_networks, self.security_groups,
                          self.block_device_mapping, self.node,
                          self.limits, self.filter_properties)

        mock_save.assert_has_calls([
            mock.call(),
            mock.call(),
            mock.call(expected_task_state='block_device_mapping')])
        mock_notify.assert_has_calls([
            mock.call(self.context, self.instance, 'create.start',
                extra_usage_info={'image_name': self.image.get('name')}),
            mock.call(self.context, self.instance, 'create.error',
                fault=exc)])
        mock_build.assert_called_once_with(self.context, self.instance,
            self.requested_networks, self.security_groups)
        mock_shutdown.assert_called_once_with(self.context, self.instance,
            self.block_device_mapping, self.requested_networks,
            try_deallocate_networks=False)
        mock_spawn.assert_called_once_with(self.context, self.instance,
            test.MatchType(objects.ImageMeta), self.injected_files,
            self.admin_pass, network_info=self.network_info,
            block_device_info=self.block_device_info)

    def test_spawn_network_alloc_failure(self):
        # Because network allocation is asynchronous, failures may not present
        # themselves until the virt spawn method is called.
        self._test_build_and_run_spawn_exceptions(exception.NoMoreNetworks())

    def test_spawn_network_auto_alloc_failure(self):
        # This isn't really a driver.spawn failure, it's a failure from
        # network_api.allocate_for_instance, but testing it here is convenient.
        self._test_build_and_run_spawn_exceptions(
            exception.UnableToAutoAllocateNetwork(
                project_id=self.context.project_id))

    def test_spawn_network_fixed_ip_not_valid_on_host_failure(self):
        self._test_build_and_run_spawn_exceptions(
            exception.FixedIpInvalidOnHost(port_id='fake-port-id'))

    def test_build_and_run_no_more_fixedips_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.NoMoreFixedIps("error messge"))

    def test_build_and_run_flavor_disk_smaller_image_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.FlavorDiskSmallerThanImage(
                flavor_size=0, image_size=1))

    def test_build_and_run_flavor_disk_smaller_min_disk(self):
        self._test_build_and_run_spawn_exceptions(
            exception.FlavorDiskSmallerThanMinDisk(
                flavor_size=0, image_min_disk=1))

    def test_build_and_run_flavor_memory_too_small_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.FlavorMemoryTooSmall())

    def test_build_and_run_image_not_active_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.ImageNotActive(image_id=self.image.get('id')))

    def test_build_and_run_image_unacceptable_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.ImageUnacceptable(image_id=self.image.get('id'),
                                        reason=""))

    def test_build_and_run_invalid_disk_info_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.InvalidDiskInfo(reason=""))

    def test_build_and_run_invalid_disk_format_exception(self):
        self._test_build_and_run_spawn_exceptions(
            exception.InvalidDiskFormat(disk_format=""))

    def test_build_and_run_signature_verification_error(self):
        self._test_build_and_run_spawn_exceptions(
            exception.SignatureVerificationError(reason=""))

    def _test_build_and_run_spawn_exceptions(self, exc):
        with test.nested(
                mock.patch.object(self.compute.driver, 'spawn',
                    side_effect=exc),
                mock.patch.object(self.instance, 'save',
                    side_effect=[self.instance, self.instance, self.instance]),
                mock.patch.object(self.compute,
                    '_build_networks_for_instance',
                    return_value=self.network_info),
                mock.patch.object(self.compute,
                    '_notify_about_instance_usage'),
                mock.patch.object(self.compute,
                    '_shutdown_instance'),
                mock.patch.object(self.compute,
                    '_validate_instance_group_policy')
        ) as (spawn, save,
                _build_networks_for_instance, _notify_about_instance_usage,
                _shutdown_instance, _validate_instance_group_policy):

            self.assertRaises(exception.BuildAbortException,
                    self.compute._build_and_run_instance, self.context,
                    self.instance, self.image, self.injected_files,
                    self.admin_pass, self.requested_networks,
                    self.security_groups, self.block_device_mapping, self.node,
                    self.limits, self.filter_properties)

            _validate_instance_group_policy.assert_called_once_with(
                    self.context, self.instance, self.filter_properties)
            _build_networks_for_instance.assert_has_calls(
                    [mock.call(self.context, self.instance,
                        self.requested_networks, self.security_groups)])

            _notify_about_instance_usage.assert_has_calls([
                mock.call(self.context, self.instance, 'create.start',
                    extra_usage_info={'image_name': self.image.get('name')}),
                mock.call(self.context, self.instance, 'create.error',
                    fault=exc)])

            save.assert_has_calls([
                mock.call(),
                mock.call(),
                mock.call(
                    expected_task_state=task_states.BLOCK_DEVICE_MAPPING)])

            spawn.assert_has_calls([mock.call(self.context, self.instance,
                test.MatchType(objects.ImageMeta),
                self.injected_files, self.admin_pass,
                network_info=self.network_info,
                block_device_info=self.block_device_info)])

            _shutdown_instance.assert_called_once_with(self.context,
                    self.instance, self.block_device_mapping,
                    self.requested_networks, try_deallocate_networks=False)

    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    @mock.patch.object(objects.InstanceActionEvent,
                       'event_finish_with_failure')
    @mock.patch.object(objects.InstanceActionEvent, 'event_start')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager,
                       '_nil_out_instance_obj_host_and_node')
    @mock.patch.object(network_api.API, 'cleanup_instance_network_on_host')
    @mock.patch.object(conductor_api.ComputeTaskAPI, 'build_instances')
    @mock.patch.object(manager.ComputeManager, '_get_resource_tracker')
    def test_reschedule_on_resources_unavailable(self, mock_get_resource,
                mock_build, mock_clean, mock_nil, mock_save, mock_start,
                mock_finish, mock_notify):
        reason = 'resource unavailable'
        exc = exception.ComputeResourcesUnavailable(reason=reason)
        mock_get_resource.side_effect = exc
        self._do_build_instance_update(mock_save, reschedule_update=True)

        self.compute.build_and_run_instance(self.context, self.instance,
                self.image, request_spec={},
                filter_properties=self.filter_properties,
                injected_files=self.injected_files,
                admin_password=self.admin_pass,
                requested_networks=self.requested_networks,
                security_groups=self.security_groups,
                block_device_mapping=self.block_device_mapping, node=self.node,
                limits=self.limits)

        self._instance_action_events(mock_start, mock_finish)
        self._assert_build_instance_update(mock_save, reschedule_update=True)
        mock_get_resource.assert_called_once_with(self.node)
        mock_notify.assert_has_calls([
            mock.call(self.context, self.instance, 'create.start',
                extra_usage_info= {'image_name': self.image.get('name')}),
            mock.call(self.context, self.instance, 'create.error', fault=exc)])
        mock_build.assert_called_once_with(self.context, [self.instance],
                self.image, self.filter_properties, self.admin_pass,
                self.injected_files, self.requested_networks,
                self.security_groups, self.block_device_mapping)
        mock_nil.assert_called_once_with(self.instance)
        mock_clean.assert_called_once_with(self.context, self.instance,
                self.compute.host)

    @mock.patch.object(manager.ComputeManager, '_build_resources')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    def test_build_resources_buildabort_reraise(self, mock_notify, mock_save,
                                                mock_build):
        exc = exception.BuildAbortException(
                instance_uuid=self.instance.uuid, reason='')
        mock_build.side_effect = exc

        self.assertRaises(exception.BuildAbortException,
                          self.compute._build_and_run_instance,
                          self.context,
                          self.instance, self.image, self.injected_files,
                          self.admin_pass, self.requested_networks,
                          self.security_groups, self.block_device_mapping,
                          self.node, self.limits, self.filter_properties)

        mock_save.assert_called_once_with()
        mock_notify.assert_has_calls([
            mock.call(self.context, self.instance, 'create.start',
                extra_usage_info={'image_name': self.image.get('name')}),
            mock.call(self.context, self.instance, 'create.error',
                fault=exc)])
        mock_build.assert_called_once_with(self.context, self.instance,
            self.requested_networks, self.security_groups,
            test.MatchType(objects.ImageMeta), self.block_device_mapping)

    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(manager.ComputeManager, '_prep_block_device')
    def test_build_resources_reraises_on_failed_bdm_prep(self, mock_prep,
                                                        mock_build, mock_save):
        mock_save.return_value = self.instance
        mock_build.return_value = self.network_info
        mock_prep.side_effect = test.TestingException

        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                pass
        except Exception as e:
            self.assertIsInstance(e, exception.BuildAbortException)

        mock_save.assert_called_once_with()
        mock_build.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_prep.assert_called_once_with(self.context, self.instance,
                self.block_device_mapping)

    def test_failed_bdm_prep_from_delete_raises_unexpected(self):
        with test.nested(
                mock.patch.object(self.compute,
                    '_build_networks_for_instance',
                    return_value=self.network_info),
                mock.patch.object(self.instance, 'save',
                    side_effect=exception.UnexpectedDeletingTaskStateError(
                        instance_uuid=uuids.instance,
                        actual={'task_state': task_states.DELETING},
                        expected={'task_state': None})),
        ) as (_build_networks_for_instance, save):

            try:
                with self.compute._build_resources(self.context, self.instance,
                        self.requested_networks, self.security_groups,
                        self.image, self.block_device_mapping):
                    pass
            except Exception as e:
                self.assertIsInstance(e,
                    exception.UnexpectedDeletingTaskStateError)

            _build_networks_for_instance.assert_has_calls(
                    [mock.call(self.context, self.instance,
                        self.requested_networks, self.security_groups)])

            save.assert_has_calls([mock.call()])

    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    def test_build_resources_aborts_on_failed_network_alloc(self, mock_build):
        mock_build.side_effect = test.TestingException

        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups, self.image,
                    self.block_device_mapping):
                pass
        except Exception as e:
            self.assertIsInstance(e, exception.BuildAbortException)

        mock_build.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)

    def test_failed_network_alloc_from_delete_raises_unexpected(self):
        with mock.patch.object(self.compute,
                '_build_networks_for_instance') as _build_networks:

            exc = exception.UnexpectedDeletingTaskStateError
            _build_networks.side_effect = exc(
                instance_uuid=uuids.instance,
                actual={'task_state': task_states.DELETING},
                expected={'task_state': None})

            try:
                with self.compute._build_resources(self.context, self.instance,
                        self.requested_networks, self.security_groups,
                        self.image, self.block_device_mapping):
                    pass
            except Exception as e:
                self.assertIsInstance(e, exc)

            _build_networks.assert_has_calls(
                    [mock.call(self.context, self.instance,
                        self.requested_networks, self.security_groups)])

    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(objects.Instance, 'save')
    def test_build_resources_cleans_up_and_reraises_on_spawn_failure(self,
                                        mock_save, mock_shutdown, mock_build):
        mock_save.return_value = self.instance
        mock_build.return_value = self.network_info
        test_exception = test.TestingException()

        def fake_spawn():
            raise test_exception

        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                fake_spawn()
        except Exception as e:
            self.assertEqual(test_exception, e)

        mock_save.assert_called_once_with()
        mock_build.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_shutdown.assert_called_once_with(self.context, self.instance,
                self.block_device_mapping, self.requested_networks,
                try_deallocate_networks=False)

    @mock.patch('nova.network.model.NetworkInfoAsyncWrapper.wait')
    @mock.patch(
        'nova.compute.manager.ComputeManager._build_networks_for_instance')
    @mock.patch('nova.objects.Instance.save')
    def test_build_resources_instance_not_found_before_yield(
            self, mock_save, mock_build_network, mock_info_wait):
        mock_build_network.return_value = self.network_info
        expected_exc = exception.InstanceNotFound(
            instance_id=self.instance.uuid)
        mock_save.side_effect = expected_exc
        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                raise
        except Exception as e:
            self.assertEqual(expected_exc, e)
        mock_build_network.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_info_wait.assert_called_once_with(do_raise=False)

    @mock.patch('nova.network.model.NetworkInfoAsyncWrapper.wait')
    @mock.patch(
        'nova.compute.manager.ComputeManager._build_networks_for_instance')
    @mock.patch('nova.objects.Instance.save')
    def test_build_resources_unexpected_task_error_before_yield(
            self, mock_save, mock_build_network, mock_info_wait):
        mock_build_network.return_value = self.network_info
        mock_save.side_effect = exception.UnexpectedTaskStateError(
            instance_uuid=uuids.instance, expected={}, actual={})
        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                raise
        except exception.BuildAbortException:
            pass
        mock_build_network.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_info_wait.assert_called_once_with(do_raise=False)

    @mock.patch('nova.network.model.NetworkInfoAsyncWrapper.wait')
    @mock.patch(
        'nova.compute.manager.ComputeManager._build_networks_for_instance')
    @mock.patch('nova.objects.Instance.save')
    def test_build_resources_exception_before_yield(
            self, mock_save, mock_build_network, mock_info_wait):
        mock_build_network.return_value = self.network_info
        mock_save.side_effect = Exception()
        try:
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                raise
        except exception.BuildAbortException:
            pass
        mock_build_network.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_info_wait.assert_called_once_with(do_raise=False)

    @mock.patch.object(manager.ComputeManager, '_build_networks_for_instance')
    @mock.patch.object(manager.ComputeManager, '_shutdown_instance')
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch('nova.compute.manager.LOG')
    def test_build_resources_aborts_on_cleanup_failure(self, mock_log,
                                        mock_save, mock_shutdown, mock_build):
        mock_save.return_value = self.instance
        mock_build.return_value = self.network_info
        mock_shutdown.side_effect = test.TestingException('Failed to shutdown')

        def fake_spawn():
            raise test.TestingException('Failed to spawn')

        with self.assertRaisesRegex(exception.BuildAbortException,
                                    'Failed to spawn'):
            with self.compute._build_resources(self.context, self.instance,
                    self.requested_networks, self.security_groups,
                    self.image, self.block_device_mapping):
                fake_spawn()

        self.assertTrue(mock_log.warning.called)
        msg = mock_log.warning.call_args_list[0]
        self.assertIn('Failed to shutdown', msg[0][1])
        mock_save.assert_called_once_with()
        mock_build.assert_called_once_with(self.context, self.instance,
                self.requested_networks, self.security_groups)
        mock_shutdown.assert_called_once_with(self.context, self.instance,
                self.block_device_mapping, self.requested_networks,
                try_deallocate_networks=False)

    @mock.patch.object(manager.ComputeManager, '_allocate_network')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    def test_build_networks_if_not_allocated(self, mock_get, mock_allocate):
        instance = fake_instance.fake_instance_obj(self.context,
                system_metadata={},
                expected_attrs=['system_metadata'])

        self.compute._build_networks_for_instance(self.context, instance,
                self.requested_networks, self.security_groups)

        mock_allocate.assert_called_once_with(self.context, instance,
                self.requested_networks, None, self.security_groups, None)

    @mock.patch.object(manager.ComputeManager, '_allocate_network')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    def test_build_networks_if_allocated_false(self, mock_get, mock_allocate):
        instance = fake_instance.fake_instance_obj(self.context,
                system_metadata=dict(network_allocated='False'),
                expected_attrs=['system_metadata'])

        self.compute._build_networks_for_instance(self.context, instance,
                self.requested_networks, self.security_groups)

        mock_allocate.assert_called_once_with(self.context, instance,
                self.requested_networks, None, self.security_groups, None)

    @mock.patch.object(network_api.API, 'setup_instance_network_on_host')
    @mock.patch.object(manager.ComputeManager, '_allocate_network')
    @mock.patch.object(network_api.API, 'get_instance_nw_info')
    def test_return_networks_if_found(self, mock_get, mock_allocate,
                                      mock_setup):
        instance = fake_instance.fake_instance_obj(self.context,
                system_metadata=dict(network_allocated='True'),
                expected_attrs=['system_metadata'])

        def fake_network_info():
            return network_model.NetworkInfo([{'address': '123.123.123.123'}])

        mock_get.return_value = network_model.NetworkInfoAsyncWrapper(
                                                            fake_network_info)

        self.compute._build_networks_for_instance(self.context, instance,
                self.requested_networks, self.security_groups)

        mock_get.assert_called_once_with(self.context, instance)
        mock_setup.assert_called_once_with(self.context, instance,
                                           instance.host)

    def test_cleanup_allocated_networks_instance_not_found(self):
        with test.nested(
                mock.patch.object(self.compute, '_deallocate_network'),
                mock.patch.object(self.instance, 'save',
                    side_effect=exception.InstanceNotFound(instance_id=''))
        ) as (_deallocate_network, save):
            # Testing that this doesn't raise an exception
            self.compute._cleanup_allocated_networks(self.context,
                    self.instance, self.requested_networks)
            save.assert_called_once_with()
            self.assertEqual('False',
                    self.instance.system_metadata['network_allocated'])

    def test_deallocate_network_none_requested(self):
        # Tests that we don't deallocate networks if 'none' were
        # specifically requested.
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='none')])
        with mock.patch.object(self.compute.network_api,
                               'deallocate_for_instance') as deallocate:
            self.compute._deallocate_network(
                self.context, mock.sentinel.instance, req_networks)
        self.assertFalse(deallocate.called)

    def test_deallocate_network_auto_requested_or_none_provided(self):
        # Tests that we deallocate networks if we were requested to
        # auto-allocate networks or requested_networks=None.
        req_networks = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='auto')])
        for requested_networks in (req_networks, None):
            with mock.patch.object(self.compute.network_api,
                                   'deallocate_for_instance') as deallocate:
                self.compute._deallocate_network(
                    self.context, mock.sentinel.instance, requested_networks)
            deallocate.assert_called_once_with(
                self.context, mock.sentinel.instance,
                requested_networks=requested_networks)

    @mock.patch.object(manager.ComputeManager, '_instance_update')
    def test_launched_at_in_create_end_notification(self,
            mock_instance_update):

        def fake_notify(*args, **kwargs):
            if args[2] == 'create.end':
                # Check that launched_at is set on the instance
                self.assertIsNotNone(args[1].launched_at)

        with test.nested(
                mock.patch.object(self.compute,
                    '_update_scheduler_instance_info'),
                mock.patch.object(self.compute.driver, 'spawn'),
                mock.patch.object(self.compute,
                    '_build_networks_for_instance', return_value=[]),
                mock.patch.object(self.instance, 'save'),
                mock.patch.object(self.compute, '_notify_about_instance_usage',
                    side_effect=fake_notify)
        ) as (mock_upd, mock_spawn, mock_networks, mock_save, mock_notify):
            self.compute._build_and_run_instance(self.context, self.instance,
                    self.image, self.injected_files, self.admin_pass,
                    self.requested_networks, self.security_groups,
                    self.block_device_mapping, self.node, self.limits,
                    self.filter_properties)
            expected_call = mock.call(self.context, self.instance,
                    'create.end', extra_usage_info={'message': u'Success'},
                    network_info=[])
            create_end_call = mock_notify.call_args_list[
                    mock_notify.call_count - 1]
            self.assertEqual(expected_call, create_end_call)

    def test_access_ip_set_when_instance_set_to_active(self):

        self.flags(default_access_ip_network_name='test1')
        instance = fake_instance.fake_db_instance()

        @mock.patch.object(db, 'instance_update_and_get_original',
                return_value=({}, instance))
        @mock.patch.object(self.compute.driver, 'spawn')
        @mock.patch.object(self.compute, '_build_networks_for_instance',
                return_value=fake_network.fake_get_instance_nw_info(self))
        @mock.patch.object(db, 'instance_extra_update_by_uuid')
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        def _check_access_ip(mock_notify, mock_extra, mock_networks,
                mock_spawn, mock_db_update):
            self.compute._build_and_run_instance(self.context, self.instance,
                    self.image, self.injected_files, self.admin_pass,
                    self.requested_networks, self.security_groups,
                    self.block_device_mapping, self.node, self.limits,
                    self.filter_properties)

            updates = {'vm_state': u'active', 'access_ip_v6':
                    netaddr.IPAddress('2001:db8:0:1:dcad:beff:feef:1'),
                    'access_ip_v4': netaddr.IPAddress('192.168.1.100'),
                    'power_state': 0, 'task_state': None, 'launched_at':
                    mock.ANY, 'expected_task_state': 'spawning'}
            expected_call = mock.call(self.context, self.instance.uuid,
                    updates, columns_to_join=['metadata', 'system_metadata',
                        'info_cache'])
            last_update_call = mock_db_update.call_args_list[
                mock_db_update.call_count - 1]
            self.assertEqual(expected_call, last_update_call)

        _check_access_ip()

    @mock.patch.object(manager.ComputeManager, '_instance_update')
    def test_create_error_on_instance_delete(self, mock_instance_update):

        def fake_notify(*args, **kwargs):
            if args[2] == 'create.error':
                # Check that launched_at is set on the instance
                self.assertIsNotNone(args[1].launched_at)

        exc = exception.InstanceNotFound(instance_id='')

        with test.nested(
                mock.patch.object(self.compute.driver, 'spawn'),
                mock.patch.object(self.compute,
                    '_build_networks_for_instance', return_value=[]),
                mock.patch.object(self.instance, 'save',
                    side_effect=[None, None, None, exc]),
                mock.patch.object(self.compute, '_notify_about_instance_usage',
                    side_effect=fake_notify)
        ) as (mock_spawn, mock_networks, mock_save, mock_notify):
            self.assertRaises(exception.InstanceNotFound,
                    self.compute._build_and_run_instance, self.context,
                    self.instance, self.image, self.injected_files,
                    self.admin_pass, self.requested_networks,
                    self.security_groups, self.block_device_mapping, self.node,
                    self.limits, self.filter_properties)
            expected_call = mock.call(self.context, self.instance,
                    'create.error', fault=exc)
            create_error_call = mock_notify.call_args_list[
                    mock_notify.call_count - 1]
            self.assertEqual(expected_call, create_error_call)


class ComputeManagerMigrationTestCase(test.NoDBTestCase):
    def setUp(self):
        super(ComputeManagerMigrationTestCase, self).setUp()
        self.compute = importutils.import_object(CONF.compute_manager)
        self.context = context.RequestContext(fakes.FAKE_USER_ID,
                                              fakes.FAKE_PROJECT_ID)
        self.image = {}
        self.instance = fake_instance.fake_instance_obj(self.context,
                vm_state=vm_states.ACTIVE,
                expected_attrs=['metadata', 'system_metadata', 'info_cache'])
        self.migration = objects.Migration(context=self.context.elevated(),
                                           new_instance_type_id=7)
        self.migration.status = 'migrating'
        self.useFixture(fixtures.SpawnIsSynchronousFixture())

    @mock.patch.object(objects.Migration, 'save')
    @mock.patch.object(objects.Migration, 'obj_as_admin')
    def test_errors_out_migration_decorator(self, mock_save,
                                            mock_obj_as_admin):
        # Tests that errors_out_migration decorator in compute manager
        # sets migration status to 'error' when an exception is raised
        # from decorated method
        instance = fake_instance.fake_instance_obj(self.context)

        migration = objects.Migration()
        migration.instance_uuid = instance.uuid
        migration.status = 'migrating'
        migration.id = 0

        @manager.errors_out_migration
        def fake_function(self, context, instance, migration):
            raise test.TestingException()

        mock_obj_as_admin.return_value = mock.MagicMock()

        self.assertRaises(test.TestingException, fake_function,
                          self, self.context, instance, migration)
        self.assertEqual('error', migration.status)
        mock_save.assert_called_once_with()
        mock_obj_as_admin.assert_called_once_with()

    def test_finish_resize_failure(self):
        with test.nested(
            mock.patch.object(self.compute, '_finish_resize',
                              side_effect=exception.ResizeError(reason='')),
            mock.patch.object(db, 'instance_fault_create'),
            mock.patch.object(self.compute, '_instance_update'),
            mock.patch.object(self.instance, 'save'),
            mock.patch.object(self.migration, 'save'),
            mock.patch.object(self.migration, 'obj_as_admin',
                              return_value=mock.MagicMock())
        ) as (meth, fault_create, instance_update, instance_save,
              migration_save, migration_obj_as_admin):
            fault_create.return_value = (
                test_instance_fault.fake_faults['fake-uuid'][0])
            self.assertRaises(
                exception.ResizeError, self.compute.finish_resize,
                context=self.context, disk_info=[], image=self.image,
                instance=self.instance, reservations=[],
                migration=self.migration
            )
            self.assertEqual("error", self.migration.status)
            migration_save.assert_called_once_with()
            migration_obj_as_admin.assert_called_once_with()

    def test_resize_instance_failure(self):
        self.migration.dest_host = None
        with test.nested(
            mock.patch.object(self.compute.driver,
                              'migrate_disk_and_power_off',
                              side_effect=exception.ResizeError(reason='')),
            mock.patch.object(db, 'instance_fault_create'),
            mock.patch.object(self.compute, '_instance_update'),
            mock.patch.object(self.migration, 'save'),
            mock.patch.object(self.migration, 'obj_as_admin',
                              return_value=mock.MagicMock()),
            mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                              return_value=None),
            mock.patch.object(self.instance, 'save'),
            mock.patch.object(self.compute, '_notify_about_instance_usage'),
            mock.patch.object(self.compute,
                              '_get_instance_block_device_info',
                              return_value=None),
            mock.patch.object(objects.BlockDeviceMappingList,
                              'get_by_instance_uuid',
                              return_value=None),
            mock.patch.object(objects.Flavor,
                              'get_by_id',
                              return_value=None)
        ) as (meth, fault_create, instance_update,
              migration_save, migration_obj_as_admin, nw_info, save_inst,
              notify, vol_block_info, bdm, flavor):
            fault_create.return_value = (
                test_instance_fault.fake_faults['fake-uuid'][0])
            self.assertRaises(
                exception.ResizeError, self.compute.resize_instance,
                context=self.context, instance=self.instance, image=self.image,
                reservations=[], migration=self.migration,
                instance_type='type', clean_shutdown=True)
            self.assertEqual("error", self.migration.status)
            self.assertEqual([mock.call(), mock.call()],
                             migration_save.mock_calls)
            self.assertEqual([mock.call(), mock.call()],
                             migration_obj_as_admin.mock_calls)

    def _test_revert_resize_instance_destroy_disks(self, is_shared=False):

        # This test asserts that _is_instance_storage_shared() is called from
        # revert_resize() and the return value is passed to driver.destroy().
        # Otherwise we could regress this.

        @mock.patch('nova.compute.rpcapi.ComputeAPI.finish_revert_resize')
        @mock.patch.object(self.instance, 'revert_migration_context')
        @mock.patch.object(self.compute.network_api, 'get_instance_nw_info')
        @mock.patch.object(self.compute, '_is_instance_storage_shared')
        @mock.patch.object(self.compute, 'finish_revert_resize')
        @mock.patch.object(self.compute, '_instance_update')
        @mock.patch.object(self.compute, '_get_resource_tracker')
        @mock.patch.object(self.compute.driver, 'destroy')
        @mock.patch.object(self.compute.network_api, 'setup_networks_on_host')
        @mock.patch.object(self.compute.network_api, 'migrate_instance_start')
        @mock.patch.object(compute_utils, 'notify_usage_exists')
        @mock.patch.object(self.migration, 'save')
        @mock.patch.object(objects.BlockDeviceMappingList,
                           'get_by_instance_uuid')
        def do_test(get_by_instance_uuid,
                    migration_save,
                    notify_usage_exists,
                    migrate_instance_start,
                    setup_networks_on_host,
                    destroy,
                    _get_resource_tracker,
                    _instance_update,
                    finish_revert_resize,
                    _is_instance_storage_shared,
                    get_instance_nw_info,
                    revert_migration_context,
                    mock_finish_revert):

            self.migration.source_compute = self.instance['host']

            # Inform compute that instance uses non-shared or shared storage
            _is_instance_storage_shared.return_value = is_shared

            self.compute.revert_resize(context=self.context,
                                       migration=self.migration,
                                       instance=self.instance,
                                       reservations=None)

            _is_instance_storage_shared.assert_called_once_with(
                self.context, self.instance,
                host=self.migration.source_compute)

            # If instance storage is shared, driver destroy method
            # should not destroy disks otherwise it should destroy disks.
            destroy.assert_called_once_with(self.context, self.instance,
                                            mock.ANY, mock.ANY, not is_shared)
            mock_finish_revert.assert_called_once_with(
                    self.context, self.instance, self.migration,
                    self.migration.source_compute, mock.ANY)

        do_test()

    def test_revert_resize_instance_destroy_disks_shared_storage(self):
        self._test_revert_resize_instance_destroy_disks(is_shared=True)

    def test_revert_resize_instance_destroy_disks_non_shared_storage(self):
        self._test_revert_resize_instance_destroy_disks(is_shared=False)

    def test_finish_revert_resize_network_calls_order(self):
        self.nw_info = None

        def _migrate_instance_finish(context, instance, migration):
            self.nw_info = 'nw_info'

        def _get_instance_nw_info(context, instance):
            return self.nw_info

        @mock.patch.object(self.compute, '_get_resource_tracker')
        @mock.patch.object(self.compute.driver, 'finish_revert_migration')
        @mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                           side_effect=_get_instance_nw_info)
        @mock.patch.object(self.compute.network_api, 'migrate_instance_finish',
                           side_effect=_migrate_instance_finish)
        @mock.patch.object(self.compute.network_api, 'setup_networks_on_host')
        @mock.patch.object(self.migration, 'save')
        @mock.patch.object(self.instance, 'save')
        @mock.patch.object(self.compute, '_set_instance_info')
        @mock.patch.object(compute_utils, 'notify_about_instance_usage')
        def do_test(notify_about_instance_usage,
                    set_instance_info,
                    instance_save,
                    migration_save,
                    setup_networks_on_host,
                    migrate_instance_finish,
                    get_instance_nw_info,
                    finish_revert_migration,
                    get_resource_tracker):

            self.migration.source_compute = self.instance['host']
            self.migration.source_node = self.instance['host']
            self.compute.finish_revert_resize(context=self.context,
                                              migration=self.migration,
                                              instance=self.instance,
                                              reservations=None)
            finish_revert_migration.assert_called_with(self.context,
                self.instance, 'nw_info', mock.ANY, mock.ANY)

        do_test()

    def test_consoles_enabled(self):
        self.flags(enabled=False, group='vnc')
        self.flags(enabled=False, group='spice')
        self.flags(enabled=False, group='rdp')
        self.flags(enabled=False, group='serial_console')
        self.assertFalse(self.compute._consoles_enabled())

        self.flags(enabled=True, group='vnc')
        self.assertTrue(self.compute._consoles_enabled())
        self.flags(enabled=False, group='vnc')

        for console in ['spice', 'rdp', 'serial_console']:
            self.flags(enabled=True, group=console)
            self.assertTrue(self.compute._consoles_enabled())
            self.flags(enabled=False, group=console)

    @mock.patch('nova.compute.manager.ComputeManager.'
                '_do_live_migration')
    def _test_max_concurrent_live(self, mock_lm):

        @mock.patch('nova.objects.Migration.save')
        def _do_it(mock_mig_save):
            instance = objects.Instance(uuid=str(uuid.uuid4()))
            migration = objects.Migration()
            self.compute.live_migration(self.context,
                                        mock.sentinel.dest,
                                        instance,
                                        mock.sentinel.block_migration,
                                        migration,
                                        mock.sentinel.migrate_data)
            self.assertEqual('queued', migration.status)
            migration.save.assert_called_once_with()

        with mock.patch.object(self.compute,
                               '_live_migration_semaphore') as mock_sem:
            for i in (1, 2, 3):
                _do_it()
        self.assertEqual(3, mock_sem.__enter__.call_count)

    def test_max_concurrent_live_limited(self):
        self.flags(max_concurrent_live_migrations=2)
        self._test_max_concurrent_live()

    def test_max_concurrent_live_unlimited(self):
        self.flags(max_concurrent_live_migrations=0)
        self._test_max_concurrent_live()

    def test_max_concurrent_live_semaphore_limited(self):
        self.flags(max_concurrent_live_migrations=123)
        self.assertEqual(
            123,
            manager.ComputeManager()._live_migration_semaphore.balance)

    def test_max_concurrent_live_semaphore_unlimited(self):
        self.flags(max_concurrent_live_migrations=0)
        compute = manager.ComputeManager()
        self.assertEqual(0, compute._live_migration_semaphore.balance)
        self.assertIsInstance(compute._live_migration_semaphore,
                              compute_utils.UnlimitedSemaphore)

    def test_max_concurrent_live_semaphore_negative(self):
        self.flags(max_concurrent_live_migrations=-2)
        compute = manager.ComputeManager()
        self.assertEqual(0, compute._live_migration_semaphore.balance)
        self.assertIsInstance(compute._live_migration_semaphore,
                              compute_utils.UnlimitedSemaphore)

    def test_check_migrate_source_converts_object(self):
        # NOTE(danms): Make sure that we legacy-ify any data objects
        # the drivers give us back, if we were passed a non-object
        data = migrate_data_obj.LiveMigrateData(is_volume_backed=False)
        compute = manager.ComputeManager()

        @mock.patch.object(compute.driver, 'check_can_live_migrate_source')
        @mock.patch.object(compute, '_get_instance_block_device_info')
        @mock.patch.object(compute_utils, 'is_volume_backed_instance')
        def _test(mock_ivbi, mock_gibdi, mock_cclms):
            mock_cclms.return_value = data
            self.assertIsInstance(
                compute.check_can_live_migrate_source(
                    self.context, {'uuid': uuids.instance}, {}),
                dict)
            self.assertIsInstance(mock_cclms.call_args_list[0][0][2],
                                  migrate_data_obj.LiveMigrateData)

        _test()

    def test_pre_live_migration_handles_dict(self):
        compute = manager.ComputeManager()

        @mock.patch.object(compute, '_notify_about_instance_usage')
        @mock.patch.object(compute, 'network_api')
        @mock.patch.object(compute.driver, 'pre_live_migration')
        @mock.patch.object(compute, '_get_instance_block_device_info')
        @mock.patch.object(compute_utils, 'is_volume_backed_instance')
        def _test(mock_ivbi, mock_gibdi, mock_plm, mock_nwapi, mock_notify):
            migrate_data = migrate_data_obj.LiveMigrateData()
            mock_plm.return_value = migrate_data
            r = compute.pre_live_migration(self.context, {'uuid': 'foo'},
                                           False, {}, {})
            self.assertIsInstance(r, dict)
            self.assertIsInstance(mock_plm.call_args_list[0][0][5],
                                  migrate_data_obj.LiveMigrateData)

        _test()

    def test_live_migration_handles_dict(self):
        compute = manager.ComputeManager()

        @mock.patch.object(compute, 'compute_rpcapi')
        @mock.patch.object(compute, 'driver')
        def _test(mock_driver, mock_rpc):
            migrate_data = migrate_data_obj.LiveMigrateData()
            migration = objects.Migration()
            migration.save = mock.MagicMock()
            mock_rpc.pre_live_migration.return_value = migrate_data
            compute._do_live_migration(self.context, 'foo', {'uuid': 'foo'},
                                       False, migration, {})
            self.assertIsInstance(
                mock_rpc.pre_live_migration.call_args_list[0][0][5],
                migrate_data_obj.LiveMigrateData)

        _test()

    def test_rollback_live_migration_handles_dict(self):
        compute = manager.ComputeManager()

        @mock.patch.object(compute.network_api, 'setup_networks_on_host')
        @mock.patch.object(compute, '_notify_about_instance_usage')
        @mock.patch.object(compute, '_live_migration_cleanup_flags')
        @mock.patch('nova.objects.BlockDeviceMappingList.get_by_instance_uuid')
        def _test(mock_bdm, mock_lmcf, mock_notify, mock_nwapi):
            mock_bdm.return_value = []
            mock_lmcf.return_value = False, False
            compute._rollback_live_migration(self.context,
                                             mock.MagicMock(),
                                             'foo', False, {})
            self.assertIsInstance(mock_lmcf.call_args_list[0][0][0],
                                  migrate_data_obj.LiveMigrateData)

        _test()

    def test_live_migration_force_complete_succeeded(self):

        instance = objects.Instance(uuid=str(uuid.uuid4()))
        migration = objects.Migration()
        migration.status = 'running'
        migration.id = 0

        @mock.patch.object(compute_utils.EventReporter, '__enter__')
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        @mock.patch.object(objects.Migration, 'get_by_id',
                           return_value=migration)
        @mock.patch.object(self.compute.driver,
                           'live_migration_force_complete')
        def _do_test(force_complete, get_by_id, _notify_about_instance_usage,
                     enter_event_reporter):
            self.compute.live_migration_force_complete(
                self.context, instance, migration.id)

            force_complete.assert_called_once_with(instance)

            _notify_usage_calls = [
                mock.call(self.context, instance,
                          'live.migration.force.complete.start'),
                mock.call(self.context, instance,
                          'live.migration.force.complete.end')
            ]

            _notify_about_instance_usage.assert_has_calls(_notify_usage_calls)
            enter_event_reporter.assert_called_once_with()

        _do_test()

    def test_post_live_migration_at_destination_success(self):

        @mock.patch.object(self.instance, 'save')
        @mock.patch.object(self.compute.network_api, 'get_instance_nw_info',
                           return_value='test_network')
        @mock.patch.object(self.compute.network_api, 'setup_networks_on_host')
        @mock.patch.object(self.compute.network_api, 'migrate_instance_finish')
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        @mock.patch.object(self.compute, '_get_instance_block_device_info')
        @mock.patch.object(self.compute, '_get_power_state', return_value=1)
        @mock.patch.object(self.compute, '_get_compute_info')
        @mock.patch.object(self.compute.driver,
                           'post_live_migration_at_destination')
        def _do_test(post_live_migration_at_destination, _get_compute_info,
                     _get_power_state, _get_instance_block_device_info,
                     _notify_about_instance_usage, migrate_instance_finish,
                     setup_networks_on_host, get_instance_nw_info, save):

            cn = mock.Mock(spec_set=['hypervisor_hostname'])
            cn.hypervisor_hostname = 'test_host'
            _get_compute_info.return_value = cn
            cn_old = self.instance.host
            instance_old = self.instance

            self.compute.post_live_migration_at_destination(
                self.context, self.instance, False)

            setup_networks_calls = [
                mock.call(self.context, self.instance, self.compute.host),
                mock.call(self.context, self.instance, cn_old, teardown=True),
                mock.call(self.context, self.instance, self.compute.host)
            ]
            setup_networks_on_host.assert_has_calls(setup_networks_calls)

            notify_usage_calls = [
                mock.call(self.context, instance_old,
                          "live_migration.post.dest.start",
                          network_info='test_network'),
                mock.call(self.context, self.instance,
                          "live_migration.post.dest.end",
                          network_info='test_network')
            ]
            _notify_about_instance_usage.assert_has_calls(notify_usage_calls)

            migrate_instance_finish.assert_called_once_with(
                self.context, self.instance,
                {'source_compute': cn_old,
                 'dest_compute': self.compute.host})
            _get_instance_block_device_info.assert_called_once_with(
                self.context, self.instance
            )
            get_instance_nw_info.assert_called_once_with(self.context,
                                                         self.instance)
            _get_power_state.assert_called_once_with(self.context,
                                                     self.instance)
            _get_compute_info.assert_called_once_with(self.context,
                                                      self.compute.host)

            self.assertEqual(self.compute.host, self.instance.host)
            self.assertEqual('test_host', self.instance.node)
            self.assertEqual(1, self.instance.power_state)
            self.assertEqual(0, self.instance.progress)
            self.assertIsNone(self.instance.task_state)
            save.assert_called_once_with(
                expected_task_state=task_states.MIGRATING)

        _do_test()

    def test_post_live_migration_at_destination_compute_not_found(self):

        @mock.patch.object(self.instance, 'save')
        @mock.patch.object(self.compute, 'network_api')
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        @mock.patch.object(self.compute, '_get_instance_block_device_info')
        @mock.patch.object(self.compute, '_get_power_state', return_value=1)
        @mock.patch.object(self.compute, '_get_compute_info',
                           side_effect=exception.ComputeHostNotFound(
                               host=uuids.fake_host))
        @mock.patch.object(self.compute.driver,
                           'post_live_migration_at_destination')
        def _do_test(post_live_migration_at_destination, _get_compute_info,
                     _get_power_state, _get_instance_block_device_info,
                     _notify_about_instance_usage, network_api, save):
            cn = mock.Mock(spec_set=['hypervisor_hostname'])
            cn.hypervisor_hostname = 'test_host'
            _get_compute_info.return_value = cn

            self.compute.post_live_migration_at_destination(
                self.context, self.instance, False)
            self.assertIsNone(self.instance.node)

        _do_test()

    def test_post_live_migration_at_destination_unexpected_exception(self):

        @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
        @mock.patch.object(self.instance, 'save')
        @mock.patch.object(self.compute, 'network_api')
        @mock.patch.object(self.compute, '_notify_about_instance_usage')
        @mock.patch.object(self.compute, '_get_instance_block_device_info')
        @mock.patch.object(self.compute, '_get_power_state', return_value=1)
        @mock.patch.object(self.compute, '_get_compute_info')
        @mock.patch.object(self.compute.driver,
                           'post_live_migration_at_destination',
                           side_effect=exception.NovaException)
        def _do_test(post_live_migration_at_destination, _get_compute_info,
                     _get_power_state, _get_instance_block_device_info,
                     _notify_about_instance_usage, network_api, save,
                     add_instance_fault_from_exc):
            cn = mock.Mock(spec_set=['hypervisor_hostname'])
            cn.hypervisor_hostname = 'test_host'
            _get_compute_info.return_value = cn

            self.assertRaises(exception.NovaException,
                              self.compute.post_live_migration_at_destination,
                              self.context, self.instance, False)
            self.assertEqual(vm_states.ERROR, self.instance.vm_state)

        _do_test()

    def _get_migration(self, migration_id, status, migration_type):
        migration = objects.Migration()
        migration.id = migration_id
        migration.status = status
        migration.migration_type = migration_type
        return migration

    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    @mock.patch.object(objects.Migration, 'get_by_id')
    @mock.patch.object(nova.virt.fake.SmallFakeDriver, 'live_migration_abort')
    def test_live_migration_abort(self,
                                  mock_driver,
                                  mock_get_migration,
                                  mock_notify):
        instance = objects.Instance(id=123, uuid=uuids.instance)
        migration = self._get_migration(10, 'running', 'live-migration')
        mock_get_migration.return_value = migration
        self.compute.live_migration_abort(self.context, instance, migration.id)

        mock_driver.assert_called_with(instance)
        _notify_usage_calls = [mock.call(self.context,
                                         instance,
                                         'live.migration.abort.start'),
                               mock.call(self.context,
                                         instance,
                                        'live.migration.abort.end')]

        mock_notify.assert_has_calls(_notify_usage_calls)

    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(manager.ComputeManager, '_notify_about_instance_usage')
    @mock.patch.object(objects.Migration, 'get_by_id')
    @mock.patch.object(nova.virt.fake.SmallFakeDriver, 'live_migration_abort')
    def test_live_migration_abort_not_supported(self,
                                                mock_driver,
                                                mock_get_migration,
                                                mock_notify,
                                                mock_instance_fault):
        instance = objects.Instance(id=123, uuid=uuids.instance)
        migration = self._get_migration(10, 'running', 'live-migration')
        mock_get_migration.return_value = migration
        mock_driver.side_effect = NotImplementedError()
        self.assertRaises(NotImplementedError,
                          self.compute.live_migration_abort,
                          self.context,
                          instance,
                          migration.id)

    @mock.patch.object(compute_utils, 'add_instance_fault_from_exc')
    @mock.patch.object(objects.Migration, 'get_by_id')
    def test_live_migration_abort_wrong_migration_state(self,
                                                        mock_get_migration,
                                                        mock_instance_fault):
        instance = objects.Instance(id=123, uuid=uuids.instance)
        migration = self._get_migration(10, 'completed', 'live-migration')
        mock_get_migration.return_value = migration
        self.assertRaises(exception.InvalidMigrationState,
                          self.compute.live_migration_abort,
                          self.context,
                          instance,
                          migration.id)

    def test_live_migration_cleanup_flags_block_migrate_libvirt(self):
        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_block_storage=False,
            is_shared_instance_path=False)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertTrue(do_cleanup)
        self.assertTrue(destroy_disks)

    def test_live_migration_cleanup_flags_shared_block_libvirt(self):
        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_block_storage=True,
            is_shared_instance_path=False)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertTrue(do_cleanup)
        self.assertFalse(destroy_disks)

    def test_live_migration_cleanup_flags_shared_path_libvirt(self):
        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_block_storage=False,
            is_shared_instance_path=True)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertFalse(do_cleanup)
        self.assertTrue(destroy_disks)

    def test_live_migration_cleanup_flags_shared_libvirt(self):
        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_block_storage=True,
            is_shared_instance_path=True)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertFalse(do_cleanup)
        self.assertFalse(destroy_disks)

    def test_live_migration_cleanup_flags_block_migrate_xenapi(self):
        migrate_data = objects.XenapiLiveMigrateData(block_migration=True)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertTrue(do_cleanup)
        self.assertTrue(destroy_disks)

    def test_live_migration_cleanup_flags_live_migrate_xenapi(self):
        migrate_data = objects.XenapiLiveMigrateData(block_migration=False)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertFalse(do_cleanup)
        self.assertFalse(destroy_disks)

    def test_live_migration_cleanup_flags_live_migrate(self):
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            {})
        self.assertFalse(do_cleanup)
        self.assertFalse(destroy_disks)

    def test_live_migration_cleanup_flags_block_migrate_hyperv(self):
        migrate_data = objects.HyperVLiveMigrateData(
            is_shared_instance_path=False)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertTrue(do_cleanup)
        self.assertTrue(destroy_disks)

    def test_live_migration_cleanup_flags_shared_hyperv(self):
        migrate_data = objects.HyperVLiveMigrateData(
            is_shared_instance_path=True)
        do_cleanup, destroy_disks = self.compute._live_migration_cleanup_flags(
            migrate_data)
        self.assertFalse(do_cleanup)
        self.assertFalse(destroy_disks)


class ComputeManagerInstanceUsageAuditTestCase(test.TestCase):
    def setUp(self):
        super(ComputeManagerInstanceUsageAuditTestCase, self).setUp()
        self.flags(use_local=True, group='conductor')
        self.flags(group='glance', api_servers=['http://localhost:9292'])
        self.flags(instance_usage_audit=True)

    @mock.patch('nova.objects.TaskLog')
    def test_deleted_instance(self, mock_task_log):
        mock_task_log.get.return_value = None

        compute = importutils.import_object(CONF.compute_manager)
        admin_context = context.get_admin_context()

        fake_db_flavor = fake_flavor.fake_db_flavor()
        flavor = objects.Flavor(admin_context, **fake_db_flavor)

        updates = {'host': compute.host, 'flavor': flavor, 'root_gb': 0,
                   'ephemeral_gb': 0}

        # fudge beginning and ending time by a second (backwards and forwards,
        # respectively) so they differ from the instance's launch and
        # termination times when sub-seconds are truncated and fall within the
        # audit period
        one_second = datetime.timedelta(seconds=1)

        begin = timeutils.utcnow() - one_second
        instance = objects.Instance(admin_context, **updates)
        instance.create()
        instance.launched_at = timeutils.utcnow()
        instance.save()
        instance.destroy()
        end = timeutils.utcnow() + one_second

        def fake_last_completed_audit_period():
            return (begin, end)

        self.stub_out('nova.utils.last_completed_audit_period',
                      fake_last_completed_audit_period)

        compute._instance_usage_audit(admin_context)

        self.assertEqual(1, mock_task_log().task_items,
                         'the deleted test instance was not found in the audit'
                         ' period')
        self.assertEqual(0, mock_task_log().errors,
                         'an error was encountered processing the deleted test'
                         ' instance')
