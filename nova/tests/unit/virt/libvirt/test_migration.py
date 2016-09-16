# Copyright (c) 2016 Red Hat, Inc
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

from collections import deque

from lxml import etree
import mock
from oslo_utils import units


from nova.compute import power_state
from nova import objects
from nova import test
from nova.tests.unit import matchers
from nova.tests.unit.virt.libvirt import fakelibvirt
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import guest as libvirt_guest
from nova.virt.libvirt import host
from nova.virt.libvirt import migration

libvirt_guest.libvirt = fakelibvirt
host.libvirt = fakelibvirt
migration.libvirt = fakelibvirt


class UtilityMigrationTestCase(test.NoDBTestCase):

    def test_graphics_listen_addrs(self):
        data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='127.0.0.1',
            graphics_listen_addr_spice='127.0.0.2')
        addrs = migration.graphics_listen_addrs(data)
        self.assertEqual({
            'vnc': '127.0.0.1',
            'spice': '127.0.0.2'}, addrs)

    def test_graphics_listen_addrs_empty(self):
        data = objects.LibvirtLiveMigrateData()
        addrs = migration.graphics_listen_addrs(data)
        self.assertIsNone(None, addrs)

    def test_graphics_listen_addrs_spice(self):
        data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_spice='127.0.0.2')
        addrs = migration.graphics_listen_addrs(data)
        self.assertEqual({
            'vnc': None,
            'spice': '127.0.0.2'}, addrs)

    def test_graphics_listen_addrs_vnc(self):
        data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='127.0.0.1')
        addrs = migration.graphics_listen_addrs(data)
        self.assertEqual({
            'vnc': '127.0.0.1',
            'spice': None}, addrs)

    def test_serial_listen_addr(self):
        data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='127.0.0.1')
        addr = migration.serial_listen_addr(data)
        self.assertEqual('127.0.0.1', addr)

    def test_serial_listen_addr_emtpy(self):
        data = objects.LibvirtLiveMigrateData()
        addr = migration.serial_listen_addr(data)
        self.assertIsNone(addr)

    @mock.patch('lxml.etree.tostring')
    @mock.patch.object(migration, '_update_perf_events_xml')
    @mock.patch.object(migration, '_update_graphics_xml')
    @mock.patch.object(migration, '_update_serial_xml')
    @mock.patch.object(migration, '_update_volume_xml')
    def test_get_updated_guest_xml(
            self, mock_volume, mock_serial, mock_graphics,
            mock_perf_events_xml, mock_tostring):
        data = objects.LibvirtLiveMigrateData()
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        get_volume_config = mock.MagicMock()
        mock_guest.get_xml_desc.return_value = '<domain></domain>'

        migration.get_updated_guest_xml(mock_guest, data, get_volume_config)
        mock_graphics.assert_called_once_with(mock.ANY, data)
        mock_serial.assert_called_once_with(mock.ANY, data)
        mock_volume.assert_called_once_with(mock.ANY, data, get_volume_config)
        mock_perf_events_xml.assert_called_once_with(mock.ANY, data)
        self.assertEqual(1, mock_tostring.called)

    def test_update_serial_xml_serial(self):
        data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='127.0.0.100')
        xml = """<domain>
  <devices>
    <serial type="tcp">
      <source host="127.0.0.1"/>
    </serial>
  </devices>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_serial_xml(doc, data))
        new_xml = xml.replace("127.0.0.1", "127.0.0.100")
        self.assertThat(res, matchers.XMLMatches(new_xml))

    def test_update_serial_xml_console(self):
        data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='127.0.0.100')
        xml = """<domain>
  <devices>
    <console type="tcp">
      <source host="127.0.0.1"/>
    </console>
  </devices>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_serial_xml(doc, data))
        new_xml = xml.replace("127.0.0.1", "127.0.0.100")
        self.assertThat(res, matchers.XMLMatches(new_xml))

    def test_update_graphics(self):
        data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='127.0.0.100',
            graphics_listen_addr_spice='127.0.0.200')
        xml = """<domain>
  <devices>
    <graphics type="vnc">
      <listen type="address" address="127.0.0.1"/>
    </graphics>
    <graphics type="spice">
      <listen type="address" address="127.0.0.2"/>
    </graphics>
  </devices>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_graphics_xml(doc, data))
        new_xml = xml.replace("127.0.0.1", "127.0.0.100")
        new_xml = new_xml.replace("127.0.0.2", "127.0.0.200")
        self.assertThat(res, matchers.XMLMatches(new_xml))

    def test_update_volume_xml(self):
        connection_info = {
            'driver_volume_type': 'iscsi',
            'serial': '58a84f6d-3f0c-4e19-a0af-eb657b790657',
            'data': {
                'access_mode': 'rw',
                'target_discovered': False,
                'target_iqn': 'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
                'volume_id': '58a84f6d-3f0c-4e19-a0af-eb657b790657',
                'device_path':
                '/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'}}
        bdm = objects.LibvirtLiveMigrateBDMInfo(
            serial='58a84f6d-3f0c-4e19-a0af-eb657b790657',
            bus='virtio', type='disk', dev='vdb',
            connection_info=connection_info)
        data = objects.LibvirtLiveMigrateData(
            target_connect_addr='127.0.0.1',
            bdms=[bdm],
            block_migration=False)
        xml = """<domain>
 <devices>
   <disk type='block' device='disk'>
     <driver name='qemu' type='raw' cache='none'/>
     <source dev='/dev/disk/by-path/ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X'/>
     <target bus='virtio' dev='vdb'/>
     <serial>58a84f6d-3f0c-4e19-a0af-eb657b790657</serial>
     <address type='pci' domain='0x0' bus='0x0' slot='0x04' function='0x0'/>
   </disk>
 </devices>
</domain>"""
        conf = vconfig.LibvirtConfigGuestDisk()
        conf.source_device = bdm.type
        conf.driver_name = "qemu"
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.target_dev = bdm.dev
        conf.target_bus = bdm.bus
        conf.serial = bdm.connection_info.get('serial')
        conf.source_type = "block"
        conf.source_path = bdm.connection_info['data'].get('device_path')

        get_volume_config = mock.MagicMock(return_value=conf)
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_volume_xml(
            doc, data, get_volume_config))
        new_xml = xml.replace('ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X',
                              'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z')
        self.assertThat(res, matchers.XMLMatches(new_xml))

    def test_update_perf_events_xml(self):
        data = objects.LibvirtLiveMigrateData(
            supported_perf_events=['cmt'])
        xml = """<domain>
  <perf>
    <event enabled="yes" name="cmt"/>
    <event enabled="yes" name="mbml"/>
  </perf>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_perf_events_xml(doc, data))

        self.assertThat(res, matchers.XMLMatches("""<domain>
  <perf>
    <event enabled="yes" name="cmt"/></perf>
</domain>"""))

    def test_update_perf_events_xml_add_new_events(self):
        data = objects.LibvirtLiveMigrateData(
            supported_perf_events=['cmt'])
        xml = """<domain>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_perf_events_xml(doc, data))

        self.assertThat(res, matchers.XMLMatches("""<domain>
<perf><event enabled="yes" name="cmt"/></perf></domain>"""))

    def test_update_perf_events_xml_add_new_events1(self):
        data = objects.LibvirtLiveMigrateData(
            supported_perf_events=['cmt', 'mbml'])
        xml = """<domain>
  <perf>
    <event enabled="yes" name="cmt"/>
  </perf>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_perf_events_xml(doc, data))

        self.assertThat(res, matchers.XMLMatches("""<domain>
  <perf>
    <event enabled="yes" name="cmt"/><event enabled="yes" name="mbml"/></perf>
</domain>"""))

    def test_update_perf_events_xml_remove_all_events(self):
        data = objects.LibvirtLiveMigrateData(
            supported_perf_events=[])
        xml = """<domain>
  <perf>
    <event enabled="yes" name="cmt"/>
  </perf>
</domain>"""
        doc = etree.fromstring(xml)
        res = etree.tostring(migration._update_perf_events_xml(doc, data))

        self.assertThat(res, matchers.XMLMatches("""<domain>
  <perf>
    </perf>
</domain>"""))


class MigrationMonitorTestCase(test.NoDBTestCase):
    def setUp(self):
        super(MigrationMonitorTestCase, self).setUp()

        flavor = objects.Flavor(memory_mb=2048,
                                swap=0,
                                vcpu_weight=None,
                                root_gb=1,
                                id=2,
                                name=u'm1.small',
                                ephemeral_gb=0,
                                rxtx_factor=1.0,
                                flavorid=u'1',
                                vcpus=1,
                                extra_specs={})

        instance = {
            'id': 1,
            'uuid': '32dfcb37-5af1-552b-357c-be8c3aa38310',
            'memory_kb': '1024000',
            'basepath': '/some/path',
            'bridge_name': 'br100',
            'display_name': "Acme webserver",
            'vcpus': 2,
            'project_id': 'fake',
            'bridge': 'br101',
            'image_ref': '155d900f-4e14-4e4c-a73d-069cbf4541e6',
            'root_gb': 10,
            'ephemeral_gb': 20,
            'instance_type_id': '5',  # m1.small
            'extra_specs': {},
            'system_metadata': {
                'image_disk_format': 'raw',
            },
            'flavor': flavor,
            'new_flavor': None,
            'old_flavor': None,
            'pci_devices': objects.PciDeviceList(),
            'numa_topology': None,
            'config_drive': None,
            'vm_mode': None,
            'kernel_id': None,
            'ramdisk_id': None,
            'os_type': 'linux',
            'user_id': '838a72b0-0d54-4827-8fd6-fb1227633ceb',
            'ephemeral_key_uuid': None,
            'vcpu_model': None,
            'host': 'fake-host',
            'task_state': None,
        }
        self.instance = objects.Instance(**instance)
        self.conn = fakelibvirt.Connection("qemu:///system")
        self.dom = fakelibvirt.Domain(self.conn, "<domain/>", True)
        self.host = host.Host("qemu:///system")
        self.guest = libvirt_guest.Guest(self.dom)

    @mock.patch.object(libvirt_guest.Guest, "is_active", return_value=True)
    def test_live_migration_find_type_active(self, mock_active):
        self.assertEqual(migration.find_job_type(self.guest, self.instance),
                         fakelibvirt.VIR_DOMAIN_JOB_FAILED)

    @mock.patch.object(libvirt_guest.Guest, "is_active", return_value=False)
    def test_live_migration_find_type_inactive(self, mock_active):
        self.assertEqual(migration.find_job_type(self.guest, self.instance),
                         fakelibvirt.VIR_DOMAIN_JOB_COMPLETED)

    @mock.patch.object(libvirt_guest.Guest, "is_active")
    def test_live_migration_find_type_no_domain(self, mock_active):
        mock_active.side_effect = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            "No domain with ID",
            error_code=fakelibvirt.VIR_ERR_NO_DOMAIN)

        self.assertEqual(migration.find_job_type(self.guest, self.instance),
                         fakelibvirt.VIR_DOMAIN_JOB_COMPLETED)

    @mock.patch.object(libvirt_guest.Guest, "is_active")
    def test_live_migration_find_type_bad_err(self, mock_active):
        mock_active.side_effect = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            "Something weird happened",
            error_code=fakelibvirt.VIR_ERR_INTERNAL_ERROR)

        self.assertEqual(migration.find_job_type(self.guest, self.instance),
                         fakelibvirt.VIR_DOMAIN_JOB_FAILED)

    def test_live_migration_abort_stuck(self):
        # Progress time exceeds progress timeout
        self.assertTrue(migration.should_abort(self.instance,
                                               5000,
                                               1000, 2000,
                                               4500, 9000,
                                               "running"))

    def test_live_migration_abort_no_prog_timeout(self):
        # Progress timeout is disabled
        self.assertFalse(migration.should_abort(self.instance,
                                                5000,
                                                1000, 0,
                                                4500, 9000,
                                                "running"))

    def test_live_migration_abort_not_stuck(self):
        # Progress time is less than progress timeout
        self.assertFalse(migration.should_abort(self.instance,
                                                5000,
                                                4500, 2000,
                                                4500, 9000,
                                                "running"))

    def test_live_migration_abort_too_long(self):
        # Elapsed time is over completion timeout
        self.assertTrue(migration.should_abort(self.instance,
                                               5000,
                                               4500, 2000,
                                               4500, 2000,
                                               "running"))

    def test_live_migration_abort_no_comp_timeout(self):
        # Completion timeout is disabled
        self.assertFalse(migration.should_abort(self.instance,
                                                5000,
                                                4500, 2000,
                                                4500, 0,
                                                "running"))

    def test_live_migration_abort_still_working(self):
        # Elapsed time is less than completion timeout
        self.assertFalse(migration.should_abort(self.instance,
                                                5000,
                                                4500, 2000,
                                                4500, 9000,
                                                "running"))

    def test_live_migration_postcopy_switch(self):
        # Migration progress is not fast enough
        self.assertTrue(migration.should_switch_to_postcopy(
                2, 100, 105, "running"))

    def test_live_migration_postcopy_switch_already_switched(self):
        # Migration already running in postcopy mode
        self.assertFalse(migration.should_switch_to_postcopy(
                2, 100, 105, "running (post-copy)"))

    def test_live_migration_postcopy_switch_too_soon(self):
        # First memory iteration not completed yet
        self.assertFalse(migration.should_switch_to_postcopy(
                1, 100, 105, "running"))

    def test_live_migration_postcopy_switch_fast_progress(self):
        # Migration progress is good
        self.assertFalse(migration.should_switch_to_postcopy(
                2, 100, 155, "running"))

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_no_steps(self, mock_dt):
        steps = []
        newdt = migration.update_downtime(self.guest, self.instance,
                                          None, steps, 5000)

        self.assertIsNone(newdt)
        self.assertFalse(mock_dt.called)

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_too_early(self, mock_dt):
        steps = [
            (9000, 50),
            (18000, 200),
        ]
        # We shouldn't change downtime since haven't hit first time
        newdt = migration.update_downtime(self.guest, self.instance,
                                          None, steps, 5000)

        self.assertIsNone(newdt)
        self.assertFalse(mock_dt.called)

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_step1(self, mock_dt):
        steps = [
            (9000, 50),
            (18000, 200),
        ]
        # We should pick the first downtime entry
        newdt = migration.update_downtime(self.guest, self.instance,
                                          None, steps, 11000)

        self.assertEqual(newdt, 50)
        mock_dt.assert_called_once_with(50)

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_nostep1(self, mock_dt):
        steps = [
            (9000, 50),
            (18000, 200),
        ]
        # We shouldn't change downtime, since its already set
        newdt = migration.update_downtime(self.guest, self.instance,
                                          50, steps, 11000)

        self.assertEqual(newdt, 50)
        self.assertFalse(mock_dt.called)

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_step2(self, mock_dt):
        steps = [
            (9000, 50),
            (18000, 200),
        ]
        newdt = migration.update_downtime(self.guest, self.instance,
                                          50, steps, 22000)

        self.assertEqual(newdt, 200)
        mock_dt.assert_called_once_with(200)

    @mock.patch.object(libvirt_guest.Guest,
                       "migrate_configure_max_downtime")
    def test_live_migration_update_downtime_err(self, mock_dt):
        steps = [
            (9000, 50),
            (18000, 200),
        ]
        mock_dt.side_effect = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            "Failed to set downtime",
            error_code=fakelibvirt.VIR_ERR_INTERNAL_ERROR)

        newdt = migration.update_downtime(self.guest, self.instance,
                                          50, steps, 22000)

        self.assertEqual(newdt, 200)
        mock_dt.assert_called_once_with(200)

    @mock.patch.object(objects.Instance, "save")
    @mock.patch.object(objects.Migration, "save")
    def test_live_migration_save_stats(self, mock_isave, mock_msave):
        mig = objects.Migration()

        info = libvirt_guest.JobInfo(
            memory_total=1 * units.Gi,
            memory_processed=5 * units.Gi,
            memory_remaining=500 * units.Mi,
            disk_total=15 * units.Gi,
            disk_processed=10 * units.Gi,
            disk_remaining=14 * units.Gi)

        migration.save_stats(self.instance, mig, info, 75)

        self.assertEqual(mig.memory_total, 1 * units.Gi)
        self.assertEqual(mig.memory_processed, 5 * units.Gi)
        self.assertEqual(mig.memory_remaining, 500 * units.Mi)
        self.assertEqual(mig.disk_total, 15 * units.Gi)
        self.assertEqual(mig.disk_processed, 10 * units.Gi)
        self.assertEqual(mig.disk_remaining, 14 * units.Gi)

        self.assertEqual(self.instance.progress, 25)

        mock_msave.assert_called_once_with()
        mock_isave.assert_called_once_with()

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_empty_tasks(self, mock_pause,
                                                  mock_postcopy):
        tasks = deque()
        active_migrations = {self.instance.uuid: tasks}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, False)

        self.assertFalse(mock_pause.called)
        self.assertFalse(mock_postcopy.called)
        self.assertEqual(len(on_migration_failure), 0)

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_no_tasks(self, mock_pause,
                                               mock_postcopy):
        active_migrations = {}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, False)

        self.assertFalse(mock_pause.called)
        self.assertFalse(mock_postcopy.called)
        self.assertEqual(len(on_migration_failure), 0)

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_no_force_complete(self, mock_pause,
                                                        mock_postcopy):
        tasks = deque()
        # Test to ensure unknown tasks are ignored
        tasks.append("wibble")
        active_migrations = {self.instance.uuid: tasks}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, False)

        self.assertFalse(mock_pause.called)
        self.assertFalse(mock_postcopy.called)
        self.assertEqual(len(on_migration_failure), 0)

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_force_complete(self, mock_pause,
                                                     mock_postcopy):
        tasks = deque()
        tasks.append("force-complete")
        active_migrations = {self.instance.uuid: tasks}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, False)

        mock_pause.assert_called_once_with()
        self.assertFalse(mock_postcopy.called)
        self.assertEqual(len(on_migration_failure), 1)
        self.assertEqual(on_migration_failure.pop(), "unpause")

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_force_complete_postcopy_running(self,
        mock_pause, mock_postcopy):
        tasks = deque()
        tasks.append("force-complete")
        active_migrations = {self.instance.uuid: tasks}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running (post-copy)")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, True)

        self.assertFalse(mock_pause.called)
        self.assertFalse(mock_postcopy.called)
        self.assertEqual(len(on_migration_failure), 0)

    @mock.patch.object(objects.Migration, "save")
    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def test_live_migration_run_tasks_force_complete_postcopy(self,
        mock_pause, mock_postcopy, mock_msave):
        tasks = deque()
        tasks.append("force-complete")
        active_migrations = {self.instance.uuid: tasks}
        on_migration_failure = deque()

        mig = objects.Migration(id=1, status="running")

        migration.run_tasks(self.guest, self.instance,
                            active_migrations, on_migration_failure,
                            mig, True)

        mock_postcopy.assert_called_once_with()
        self.assertFalse(mock_pause.called)
        self.assertEqual(len(on_migration_failure), 0)

    @mock.patch.object(libvirt_guest.Guest, "resume")
    @mock.patch.object(libvirt_guest.Guest, "get_power_state",
                       return_value=power_state.PAUSED)
    def test_live_migration_recover_tasks_resume(self, mock_ps, mock_resume):
        tasks = deque()
        tasks.append("unpause")

        migration.run_recover_tasks(self.host, self.guest,
                                    self.instance, tasks)

        mock_resume.assert_called_once_with()

    @mock.patch.object(libvirt_guest.Guest, "resume")
    @mock.patch.object(libvirt_guest.Guest, "get_power_state",
                       return_value=power_state.RUNNING)
    def test_live_migration_recover_tasks_no_resume(self, mock_ps,
                                                    mock_resume):
        tasks = deque()
        tasks.append("unpause")

        migration.run_recover_tasks(self.host, self.guest,
                                    self.instance, tasks)

        self.assertFalse(mock_resume.called)

    @mock.patch.object(libvirt_guest.Guest, "resume")
    def test_live_migration_recover_tasks_empty_tasks(self, mock_resume):
        tasks = deque()

        migration.run_recover_tasks(self.host, self.guest,
                                    self.instance, tasks)

        self.assertFalse(mock_resume.called)

    @mock.patch.object(libvirt_guest.Guest, "resume")
    def test_live_migration_recover_tasks_no_pause(self, mock_resume):
        tasks = deque()
        # Test to ensure unknown tasks are ignored
        tasks.append("wibble")

        migration.run_recover_tasks(self.host, self.guest,
                                    self.instance, tasks)

        self.assertFalse(mock_resume.called)
