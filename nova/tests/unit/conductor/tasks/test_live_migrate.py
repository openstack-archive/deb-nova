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
import oslo_messaging as messaging

from nova.compute import power_state
from nova.compute import rpcapi as compute_rpcapi
from nova.compute import vm_states
from nova.conductor.tasks import live_migrate
from nova import exception
from nova import objects
from nova.scheduler import client as scheduler_client
from nova.scheduler import utils as scheduler_utils
from nova import servicegroup
from nova import test
from nova.tests.unit import fake_instance
from nova.tests import uuidsentinel as uuids
from nova import utils


class LiveMigrationTaskTestCase(test.NoDBTestCase):
    def setUp(self):
        super(LiveMigrationTaskTestCase, self).setUp()
        self.context = "context"
        self.instance_host = "host"
        self.instance_uuid = uuids.instance
        self.instance_image = "image_ref"
        db_instance = fake_instance.fake_db_instance(
                host=self.instance_host,
                uuid=self.instance_uuid,
                power_state=power_state.RUNNING,
                vm_state = vm_states.ACTIVE,
                memory_mb=512,
                image_ref=self.instance_image)
        self.instance = objects.Instance._from_db_object(
                self.context, objects.Instance(), db_instance)
        self.instance.system_metadata = {'image_hw_disk_bus': 'scsi'}
        self.destination = "destination"
        self.block_migration = "bm"
        self.disk_over_commit = "doc"
        self.migration = objects.Migration()
        self.fake_spec = objects.RequestSpec()
        self._generate_task()

    def _generate_task(self):
        self.task = live_migrate.LiveMigrationTask(self.context,
            self.instance, self.destination, self.block_migration,
            self.disk_over_commit, self.migration, compute_rpcapi.ComputeAPI(),
            servicegroup.API(), scheduler_client.SchedulerClient(),
            self.fake_spec)

    def test_execute_with_destination(self):
        self.mox.StubOutWithMock(self.task, '_check_host_is_up')
        self.mox.StubOutWithMock(self.task, '_check_requested_destination')
        self.mox.StubOutWithMock(self.task.compute_rpcapi, 'live_migration')

        self.task._check_host_is_up(self.instance_host)
        self.task._check_requested_destination()
        self.task.compute_rpcapi.live_migration(self.context,
                host=self.instance_host,
                instance=self.instance,
                dest=self.destination,
                block_migration=self.block_migration,
                migration=self.migration,
                migrate_data=None).AndReturn("bob")

        self.mox.ReplayAll()
        self.assertEqual("bob", self.task.execute())

    def test_execute_without_destination(self):
        self.destination = None
        self._generate_task()
        self.assertIsNone(self.task.destination)

        self.mox.StubOutWithMock(self.task, '_check_host_is_up')
        self.mox.StubOutWithMock(self.task, '_find_destination')
        self.mox.StubOutWithMock(self.task.compute_rpcapi, 'live_migration')

        self.task._check_host_is_up(self.instance_host)
        self.task._find_destination().AndReturn("found_host")
        self.task.compute_rpcapi.live_migration(self.context,
                host=self.instance_host,
                instance=self.instance,
                dest="found_host",
                block_migration=self.block_migration,
                migration=self.migration,
                migrate_data=None).AndReturn("bob")

        self.mox.ReplayAll()
        with mock.patch.object(self.migration, 'save') as mock_save:
            self.assertEqual("bob", self.task.execute())
            self.assertTrue(mock_save.called)
            self.assertEqual('found_host', self.migration.dest_compute)

    def test_check_instance_is_active_passes_when_paused(self):
        self.task.instance['power_state'] = power_state.PAUSED
        self.task._check_instance_is_active()

    def test_check_instance_is_active_fails_when_shutdown(self):
        self.task.instance['power_state'] = power_state.SHUTDOWN
        self.assertRaises(exception.InstanceInvalidState,
                          self.task._check_instance_is_active)

    def test_check_instance_host_is_up(self):
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')
        self.mox.StubOutWithMock(self.task.servicegroup_api, 'service_is_up')

        objects.Service.get_by_compute_host(self.context,
                                            "host").AndReturn("service")
        self.task.servicegroup_api.service_is_up("service").AndReturn(True)

        self.mox.ReplayAll()
        self.task._check_host_is_up("host")

    def test_check_instance_host_is_up_fails_if_not_up(self):
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')
        self.mox.StubOutWithMock(self.task.servicegroup_api, 'service_is_up')

        objects.Service.get_by_compute_host(self.context,
                                            "host").AndReturn("service")
        self.task.servicegroup_api.service_is_up("service").AndReturn(False)

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeServiceUnavailable,
                self.task._check_host_is_up, "host")

    def test_check_instance_host_is_up_fails_if_not_found(self):
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')

        objects.Service.get_by_compute_host(
            self.context, "host").AndRaise(
            exception.ComputeHostNotFound(host='host'))

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeHostNotFound,
            self.task._check_host_is_up, "host")

    def test_check_requested_destination(self):
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')
        self.mox.StubOutWithMock(self.task, '_get_compute_info')
        self.mox.StubOutWithMock(self.task.servicegroup_api, 'service_is_up')
        self.mox.StubOutWithMock(self.task.compute_rpcapi,
                                 'check_can_live_migrate_destination')

        objects.Service.get_by_compute_host(
            self.context, self.destination).AndReturn("service")
        self.task.servicegroup_api.service_is_up("service").AndReturn(True)
        hypervisor_details = objects.ComputeNode(
            hypervisor_type="a",
            hypervisor_version=6.1,
            free_ram_mb=513,
            memory_mb=512,
            ram_allocation_ratio=1.0,
        )
        self.task._get_compute_info(self.destination)\
                .AndReturn(hypervisor_details)
        self.task._get_compute_info(self.instance_host)\
                .AndReturn(hypervisor_details)
        self.task._get_compute_info(self.destination)\
                .AndReturn(hypervisor_details)

        self.task.compute_rpcapi.check_can_live_migrate_destination(
                self.context, self.instance, self.destination,
                self.block_migration, self.disk_over_commit).AndReturn(
                        "migrate_data")

        self.mox.ReplayAll()
        self.task._check_requested_destination()
        self.assertEqual("migrate_data", self.task.migrate_data)

    def test_check_requested_destination_fails_with_same_dest(self):
        self.task.destination = "same"
        self.task.source = "same"
        self.assertRaises(exception.UnableToMigrateToSelf,
                          self.task._check_requested_destination)

    def test_check_requested_destination_fails_when_destination_is_up(self):
        self.mox.StubOutWithMock(objects.Service, 'get_by_compute_host')

        objects.Service.get_by_compute_host(
            self.context, self.destination).AndRaise(
            exception.ComputeHostNotFound(host='host'))

        self.mox.ReplayAll()
        self.assertRaises(exception.ComputeHostNotFound,
                          self.task._check_requested_destination)

    def test_check_requested_destination_fails_with_not_enough_memory(self):
        self.mox.StubOutWithMock(self.task, '_check_host_is_up')
        self.mox.StubOutWithMock(objects.ComputeNode,
                                 'get_first_node_by_host_for_old_compat')

        self.task._check_host_is_up(self.destination)
        objects.ComputeNode.get_first_node_by_host_for_old_compat(self.context,
            self.destination).AndReturn(
                objects.ComputeNode(free_ram_mb=513,
                                    memory_mb=1024,
                                    ram_allocation_ratio=0.9,
                                    ))

        self.mox.ReplayAll()
        # free_ram is bigger than instance.ram (512) but the allocation ratio
        # reduces the total available RAM to 410MB (1024 * 0.9 - (1024 - 513))
        self.assertRaises(exception.MigrationPreCheckError,
                          self.task._check_requested_destination)

    def test_check_requested_destination_fails_with_hypervisor_diff(self):
        self.mox.StubOutWithMock(self.task, '_check_host_is_up')
        self.mox.StubOutWithMock(self.task,
                '_check_destination_has_enough_memory')
        self.mox.StubOutWithMock(self.task, '_get_compute_info')

        self.task._check_host_is_up(self.destination)
        self.task._check_destination_has_enough_memory()

        self.task._get_compute_info(self.instance_host).AndReturn(
            objects.ComputeNode(hypervisor_type='b')
        )
        self.task._get_compute_info(self.destination).AndReturn(
            objects.ComputeNode(hypervisor_type='a')
        )

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidHypervisorType,
                          self.task._check_requested_destination)

    def test_check_requested_destination_fails_with_hypervisor_too_old(self):
        self.mox.StubOutWithMock(self.task, '_check_host_is_up')
        self.mox.StubOutWithMock(self.task,
                '_check_destination_has_enough_memory')
        self.mox.StubOutWithMock(self.task, '_get_compute_info')

        self.task._check_host_is_up(self.destination)
        self.task._check_destination_has_enough_memory()
        host1 = {'hypervisor_type': 'a', 'hypervisor_version': 7}
        self.task._get_compute_info(self.instance_host).AndReturn(
            objects.ComputeNode(**host1)
        )
        host2 = {'hypervisor_type': 'a', 'hypervisor_version': 6}
        self.task._get_compute_info(self.destination).AndReturn(
            objects.ComputeNode(**host2)
        )

        self.mox.ReplayAll()
        self.assertRaises(exception.DestinationHypervisorTooOld,
                          self.task._check_requested_destination)

    def test_find_destination_works(self):
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(objects.RequestSpec,
                                 'reset_forced_destinations')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')
        self.mox.StubOutWithMock(self.task, '_call_livem_checks_on_host')

        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.fake_spec.reset_forced_destinations()
        self.task.scheduler_client.select_destinations(
            self.context, self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")
        self.task._call_livem_checks_on_host("host1")

        self.mox.ReplayAll()
        self.assertEqual("host1", self.task._find_destination())

    def test_find_destination_works_with_no_request_spec(self):
        task = live_migrate.LiveMigrationTask(
            self.context, self.instance, self.destination,
            self.block_migration, self.disk_over_commit, self.migration,
            compute_rpcapi.ComputeAPI(), servicegroup.API(),
            scheduler_client.SchedulerClient(), request_spec=None)
        another_spec = objects.RequestSpec()
        self.instance.flavor = objects.Flavor()
        self.instance.numa_topology = None
        self.instance.pci_requests = None

        @mock.patch.object(task, '_call_livem_checks_on_host')
        @mock.patch.object(task, '_check_compatible_with_source_hypervisor')
        @mock.patch.object(task.scheduler_client, 'select_destinations')
        @mock.patch.object(objects.RequestSpec, 'from_components')
        @mock.patch.object(scheduler_utils, 'setup_instance_group')
        @mock.patch.object(utils, 'get_image_from_system_metadata')
        def do_test(get_image, setup_ig, from_components, select_dest,
                    check_compat, call_livem_checks):
            get_image.return_value = "image"
            from_components.return_value = another_spec
            select_dest.return_value = [{'host': 'host1'}]

            self.assertEqual("host1", task._find_destination())

            get_image.assert_called_once_with(self.instance.system_metadata)
            fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
            setup_ig.assert_called_once_with(
                self.context, fake_props,
                {'ignore_hosts': [self.instance_host]}
            )
            select_dest.assert_called_once_with(self.context, another_spec)
            check_compat.assert_called_once_with("host1")
            call_livem_checks.assert_called_once_with("host1")
        do_test()

    def test_find_destination_no_image_works(self):
        self.instance['image_ref'] = ''

        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')
        self.mox.StubOutWithMock(self.task, '_call_livem_checks_on_host')

        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")
        self.task._call_livem_checks_on_host("host1")

        self.mox.ReplayAll()
        self.assertEqual("host1", self.task._find_destination())

    def _test_find_destination_retry_hypervisor_raises(self, error):
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')
        self.mox.StubOutWithMock(self.task, '_call_livem_checks_on_host')

        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")\
                .AndRaise(error)

        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host2'}])
        self.task._check_compatible_with_source_hypervisor("host2")
        self.task._call_livem_checks_on_host("host2")

        self.mox.ReplayAll()
        self.assertEqual("host2", self.task._find_destination())

    def test_find_destination_retry_with_old_hypervisor(self):
        self._test_find_destination_retry_hypervisor_raises(
                exception.DestinationHypervisorTooOld)

    def test_find_destination_retry_with_invalid_hypervisor_type(self):
        self._test_find_destination_retry_hypervisor_raises(
                exception.InvalidHypervisorType)

    def test_find_destination_retry_with_invalid_livem_checks(self):
        self.flags(migrate_max_retries=1)
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')
        self.mox.StubOutWithMock(self.task, '_call_livem_checks_on_host')

        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")
        self.task._call_livem_checks_on_host("host1")\
                .AndRaise(exception.Invalid)

        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host2'}])
        self.task._check_compatible_with_source_hypervisor("host2")
        self.task._call_livem_checks_on_host("host2")

        self.mox.ReplayAll()
        self.assertEqual("host2", self.task._find_destination())

    def test_find_destination_retry_with_failed_migration_pre_checks(self):
        self.flags(migrate_max_retries=1)
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')
        self.mox.StubOutWithMock(self.task, '_call_livem_checks_on_host')

        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")
        self.task._call_livem_checks_on_host("host1")\
                .AndRaise(exception.MigrationPreCheckError("reason"))

        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host2'}])
        self.task._check_compatible_with_source_hypervisor("host2")
        self.task._call_livem_checks_on_host("host2")

        self.mox.ReplayAll()
        self.assertEqual("host2", self.task._find_destination())

    def test_find_destination_retry_exceeds_max(self):
        self.flags(migrate_max_retries=0)
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        self.mox.StubOutWithMock(self.task,
                '_check_compatible_with_source_hypervisor')

        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndReturn(
                        [{'host': 'host1'}])
        self.task._check_compatible_with_source_hypervisor("host1")\
                .AndRaise(exception.DestinationHypervisorTooOld)

        self.mox.ReplayAll()
        with mock.patch.object(self.task.migration, 'save') as save_mock:
            self.assertRaises(exception.MaxRetriesExceeded,
                              self.task._find_destination)
            self.assertEqual('failed', self.task.migration.status)
            save_mock.assert_called_once_with()

    def test_find_destination_when_runs_out_of_hosts(self):
        self.mox.StubOutWithMock(utils, 'get_image_from_system_metadata')
        self.mox.StubOutWithMock(scheduler_utils, 'setup_instance_group')
        self.mox.StubOutWithMock(self.task.scheduler_client,
                                 'select_destinations')
        utils.get_image_from_system_metadata(
            self.instance.system_metadata).AndReturn("image")
        fake_props = {'instance_properties': {'uuid': self.instance_uuid}}
        scheduler_utils.setup_instance_group(
            self.context, fake_props, {'ignore_hosts': [self.instance_host]})
        self.task.scheduler_client.select_destinations(self.context,
                self.fake_spec).AndRaise(
                        exception.NoValidHost(reason=""))

        self.mox.ReplayAll()
        self.assertRaises(exception.NoValidHost, self.task._find_destination)

    @mock.patch("nova.utils.get_image_from_system_metadata")
    @mock.patch("nova.scheduler.utils.build_request_spec")
    @mock.patch("nova.scheduler.utils.setup_instance_group")
    @mock.patch("nova.objects.RequestSpec.from_primitives")
    def test_find_destination_with_remoteError(self,
        m_from_primitives, m_setup_instance_group,
        m_build_request_spec, m_get_image_from_system_metadata):
        m_get_image_from_system_metadata.return_value = {'properties': {}}
        m_build_request_spec.return_value = {}
        fake_spec = objects.RequestSpec()
        m_from_primitives.return_value = fake_spec
        with mock.patch.object(self.task.scheduler_client,
            'select_destinations') as m_select_destinations:
            error = messaging.RemoteError()
            m_select_destinations.side_effect = error
            self.assertRaises(exception.MigrationSchedulerRPCError,
                              self.task._find_destination)

    def test_call_livem_checks_on_host(self):
        with mock.patch.object(self.task.compute_rpcapi,
            'check_can_live_migrate_destination',
            side_effect=messaging.MessagingTimeout):
            self.assertRaises(exception.MigrationPreCheckError,
                self.task._call_livem_checks_on_host, {})
