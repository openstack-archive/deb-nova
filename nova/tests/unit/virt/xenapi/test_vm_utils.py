# Copyright 2013 OpenStack Foundation
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

import contextlib
import uuid

from eventlet import greenthread
import mock
from mox3 import mox
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import fixture as config_fixture
from oslo_utils import fixture as utils_fixture
from oslo_utils import timeutils
from oslo_utils import units
from oslo_utils import uuidutils
import six

from nova.compute import flavors
from nova.compute import power_state
from nova.compute import vm_mode
import nova.conf
from nova import context
from nova import exception
from nova import objects
from nova import test
from nova.tests.unit import fake_flavor
from nova.tests.unit import fake_instance
from nova.tests.unit.objects import test_flavor
from nova.tests.unit.virt.xenapi import stubs
from nova.tests import uuidsentinel as uuids
from nova import utils
from nova.virt import hardware
from nova.virt.xenapi import driver as xenapi_conn
from nova.virt.xenapi import fake
from nova.virt.xenapi import vm_utils

CONF = nova.conf.CONF
XENSM_TYPE = 'xensm'
ISCSI_TYPE = 'iscsi'


def get_fake_connection_data(sr_type):
    fakes = {XENSM_TYPE: {'sr_uuid': 'falseSR',
                          'name_label': 'fake_storage',
                          'name_description': 'test purposes',
                          'server': 'myserver',
                          'serverpath': '/local/scratch/myname',
                          'sr_type': 'nfs',
                          'introduce_sr_keys': ['server',
                                                'serverpath',
                                                'sr_type'],
                          'vdi_uuid': 'falseVDI'},
             ISCSI_TYPE: {'volume_id': 'fake_volume_id',
                          'target_lun': 1,
                          'target_iqn': 'fake_iqn:volume-fake_volume_id',
                          'target_portal': u'localhost:3260',
                          'target_discovered': False}, }
    return fakes[sr_type]


@contextlib.contextmanager
def contextified(result):
    yield result


def _fake_noop(*args, **kwargs):
    return


class VMUtilsTestBase(stubs.XenAPITestBaseNoDB):
    pass


class LookupTestCase(VMUtilsTestBase):
    def setUp(self):
        super(LookupTestCase, self).setUp()
        self.session = self.mox.CreateMockAnything('Fake Session')
        self.name_label = 'my_vm'

    def _do_mock(self, result):
        self.session.call_xenapi(
            "VM.get_by_name_label", self.name_label).AndReturn(result)
        self.mox.ReplayAll()

    def test_normal(self):
        self._do_mock(['x'])
        result = vm_utils.lookup(self.session, self.name_label)
        self.assertEqual('x', result)

    def test_no_result(self):
        self._do_mock([])
        result = vm_utils.lookup(self.session, self.name_label)
        self.assertIsNone(result)

    def test_too_many(self):
        self._do_mock(['a', 'b'])
        self.assertRaises(exception.InstanceExists,
                          vm_utils.lookup,
                          self.session, self.name_label)

    def test_rescue_none(self):
        self.session.call_xenapi(
            "VM.get_by_name_label", self.name_label + '-rescue').AndReturn([])
        self._do_mock(['x'])
        result = vm_utils.lookup(self.session, self.name_label,
                                 check_rescue=True)
        self.assertEqual('x', result)

    def test_rescue_found(self):
        self.session.call_xenapi(
            "VM.get_by_name_label",
            self.name_label + '-rescue').AndReturn(['y'])
        self.mox.ReplayAll()
        result = vm_utils.lookup(self.session, self.name_label,
                                 check_rescue=True)
        self.assertEqual('y', result)

    def test_rescue_too_many(self):
        self.session.call_xenapi(
            "VM.get_by_name_label",
            self.name_label + '-rescue').AndReturn(['a', 'b', 'c'])
        self.mox.ReplayAll()
        self.assertRaises(exception.InstanceExists,
                          vm_utils.lookup,
                          self.session, self.name_label,
                          check_rescue=True)


class GenerateConfigDriveTestCase(VMUtilsTestBase):
    @mock.patch.object(vm_utils, 'safe_find_sr')
    @mock.patch.object(vm_utils, "create_vdi", return_value='vdi_ref')
    @mock.patch.object(vm_utils.instance_metadata, "InstanceMetadata")
    @mock.patch.object(vm_utils.configdrive, 'ConfigDriveBuilder')
    @mock.patch.object(vm_utils.utils, 'execute')
    @mock.patch.object(vm_utils.volume_utils, 'stream_to_vdi')
    @mock.patch.object(vm_utils.os.path, 'getsize', return_value=100)
    @mock.patch.object(vm_utils, 'create_vbd', return_value='vbd_ref')
    @mock.patch.object(vm_utils.utils, 'tempdir')
    def test_no_admin_pass(self, mock_tmpdir, mock_create_vbd, mock_size,
                           mock_stream, mock_execute, mock_builder,
                           mock_instance_metadata, mock_create_vdi,
                           mock_find_sr):

        mock_tmpdir.return_value.__enter__.return_value = '/mock'

        with mock.patch.object(six.moves.builtins, 'open') as mock_open:
            mock_open.return_value.__enter__.return_value = 'open_fd'
            vm_utils.generate_configdrive('session', 'context', 'instance',
                                          'vm_ref', 'userdevice',
                                          'network_info')

            mock_size.assert_called_with('/mock/configdrive.vhd')
            mock_open.assert_called_with('/mock/configdrive.vhd')
            mock_execute.assert_called_with('qemu-img', 'convert', '-Ovpc',
                                            '/mock/configdrive',
                                            '/mock/configdrive.vhd')
            mock_instance_metadata.assert_called_with(
                'instance', content=None, extra_md={},
                network_info='network_info', request_context='context')
            mock_stream.assert_called_with('session', 'instance', 'vhd',
                                           'open_fd', 100, 'vdi_ref')

    @mock.patch.object(vm_utils, "destroy_vdi")
    @mock.patch.object(vm_utils, 'safe_find_sr')
    @mock.patch.object(vm_utils, "create_vdi", return_value='vdi_ref')
    @mock.patch.object(vm_utils.instance_metadata, "InstanceMetadata",
                       side_effect=test.TestingException)
    def test_vdi_cleaned_up(self, mock_instance_metadata, mock_create,
                            mock_find_sr, mock_destroy):
        self.assertRaises(test.TestingException, vm_utils.generate_configdrive,
                          'session', None, None, None, None, None)
        mock_destroy.assert_called_once_with('session', 'vdi_ref')


class XenAPIGetUUID(VMUtilsTestBase):
    def test_get_this_vm_uuid_new_kernel(self):
        self.mox.StubOutWithMock(vm_utils, '_get_sys_hypervisor_uuid')

        vm_utils._get_sys_hypervisor_uuid().AndReturn(
            '2f46f0f5-f14c-ef1b-1fac-9eeca0888a3f')

        self.mox.ReplayAll()
        self.assertEqual('2f46f0f5-f14c-ef1b-1fac-9eeca0888a3f',
                         vm_utils.get_this_vm_uuid(None))
        self.mox.VerifyAll()

    def test_get_this_vm_uuid_old_kernel_reboot(self):
        self.mox.StubOutWithMock(vm_utils, '_get_sys_hypervisor_uuid')
        self.mox.StubOutWithMock(utils, 'execute')

        vm_utils._get_sys_hypervisor_uuid().AndRaise(
            IOError(13, 'Permission denied'))
        utils.execute('xenstore-read', 'domid', run_as_root=True).AndReturn(
            ('27', ''))
        utils.execute('xenstore-read', '/local/domain/27/vm',
                      run_as_root=True).AndReturn(
            ('/vm/2f46f0f5-f14c-ef1b-1fac-9eeca0888a3f', ''))

        self.mox.ReplayAll()
        self.assertEqual('2f46f0f5-f14c-ef1b-1fac-9eeca0888a3f',
                         vm_utils.get_this_vm_uuid(None))
        self.mox.VerifyAll()


class FakeSession(object):
    def call_xenapi(self, *args):
        pass

    def call_plugin(self, *args):
        pass

    def call_plugin_serialized(self, plugin, fn, *args, **kwargs):
        pass

    def call_plugin_serialized_with_retry(self, plugin, fn, num_retries,
                                          callback, *args, **kwargs):
        pass


class FetchVhdImageTestCase(VMUtilsTestBase):
    def setUp(self):
        super(FetchVhdImageTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.context.auth_token = 'auth_token'
        self.session = FakeSession()
        self.instance = {"uuid": "uuid"}
        self.flags(group='glance', api_servers=['http://localhost:9292'])

        self.mox.StubOutWithMock(vm_utils, '_make_uuid_stack')
        vm_utils._make_uuid_stack().AndReturn(["uuid_stack"])

        self.mox.StubOutWithMock(vm_utils, 'get_sr_path')
        vm_utils.get_sr_path(self.session).AndReturn('sr_path')

    def _stub_glance_download_vhd(self, raise_exc=None):
        self.mox.StubOutWithMock(
                self.session, 'call_plugin_serialized_with_retry')
        func = self.session.call_plugin_serialized_with_retry(
                'glance', 'download_vhd2', 0, mox.IgnoreArg(), mox.IgnoreArg(),
                extra_headers={'X-Auth-Token': 'auth_token',
                               'X-Roles': '',
                               'X-Tenant-Id': None,
                               'X-User-Id': None,
                               'X-Identity-Status': 'Confirmed'},
                image_id='image_id',
                uuid_stack=["uuid_stack"],
                sr_path='sr_path')

        if raise_exc:
            func.AndRaise(raise_exc)
        else:
            func.AndReturn({'root': {'uuid': 'vdi'}})

    def _stub_bittorrent_download_vhd(self, raise_exc=None):
        self.mox.StubOutWithMock(
                self.session, 'call_plugin_serialized')
        func = self.session.call_plugin_serialized(
            'bittorrent', 'download_vhd',
            image_id='image_id',
            uuid_stack=["uuid_stack"],
            sr_path='sr_path',
            torrent_download_stall_cutoff=600,
            torrent_listen_port_start=6881,
            torrent_listen_port_end=6891,
            torrent_max_last_accessed=86400,
            torrent_max_seeder_processes_per_host=1,
            torrent_seed_chance=1.0,
            torrent_seed_duration=3600,
            torrent_url='http://foo/image_id.torrent'
        )
        if raise_exc:
            func.AndRaise(raise_exc)
        else:
            func.AndReturn({'root': {'uuid': 'vdi'}})

    def test_fetch_vhd_image_works_with_glance(self):
        self.mox.StubOutWithMock(vm_utils, '_image_uses_bittorrent')
        vm_utils._image_uses_bittorrent(
            self.context, self.instance).AndReturn(False)

        self._stub_glance_download_vhd()

        self.mox.StubOutWithMock(vm_utils, 'safe_find_sr')
        vm_utils.safe_find_sr(self.session).AndReturn("sr")

        self.mox.StubOutWithMock(vm_utils, '_scan_sr')
        vm_utils._scan_sr(self.session, "sr")

        self.mox.StubOutWithMock(vm_utils, '_check_vdi_size')
        vm_utils._check_vdi_size(
                self.context, self.session, self.instance, "vdi")

        self.mox.ReplayAll()

        self.assertEqual("vdi", vm_utils._fetch_vhd_image(self.context,
            self.session, self.instance, 'image_id')['root']['uuid'])

        self.mox.VerifyAll()

    def test_fetch_vhd_image_works_with_bittorrent(self):
        self.flags(torrent_base_url='http://foo', group='xenserver')

        self.mox.StubOutWithMock(vm_utils, '_image_uses_bittorrent')
        vm_utils._image_uses_bittorrent(
            self.context, self.instance).AndReturn(True)

        self._stub_bittorrent_download_vhd()

        self.mox.StubOutWithMock(vm_utils, 'safe_find_sr')
        vm_utils.safe_find_sr(self.session).AndReturn("sr")

        self.mox.StubOutWithMock(vm_utils, '_scan_sr')
        vm_utils._scan_sr(self.session, "sr")

        self.mox.StubOutWithMock(vm_utils, '_check_vdi_size')
        vm_utils._check_vdi_size(self.context, self.session, self.instance,
                                 "vdi")

        self.mox.ReplayAll()

        self.assertEqual("vdi", vm_utils._fetch_vhd_image(self.context,
            self.session, self.instance, 'image_id')['root']['uuid'])

        self.mox.VerifyAll()

    def test_fetch_vhd_image_cleans_up_vdi_on_fail(self):
        self.mox.StubOutWithMock(vm_utils, '_image_uses_bittorrent')
        vm_utils._image_uses_bittorrent(
            self.context, self.instance).AndReturn(False)

        self._stub_glance_download_vhd()

        self.mox.StubOutWithMock(vm_utils, 'safe_find_sr')
        vm_utils.safe_find_sr(self.session).AndReturn("sr")

        self.mox.StubOutWithMock(vm_utils, '_scan_sr')
        vm_utils._scan_sr(self.session, "sr")

        self.mox.StubOutWithMock(vm_utils, '_check_vdi_size')
        vm_utils._check_vdi_size(self.context, self.session, self.instance,
                "vdi").AndRaise(exception.FlavorDiskSmallerThanImage(
                flavor_size=0, image_size=1))

        self.mox.StubOutWithMock(self.session, 'call_xenapi')
        self.session.call_xenapi("VDI.get_by_uuid", "vdi").AndReturn("ref")

        self.mox.StubOutWithMock(vm_utils, 'destroy_vdi')
        vm_utils.destroy_vdi(self.session,
                "ref").AndRaise(exception.StorageError(reason=""))

        self.mox.ReplayAll()

        self.assertRaises(exception.FlavorDiskSmallerThanImage,
                vm_utils._fetch_vhd_image, self.context, self.session,
                self.instance, 'image_id')

        self.mox.VerifyAll()

    def test_fallback_to_default_handler(self):
        self.flags(torrent_base_url='http://foo', group='xenserver')

        self.mox.StubOutWithMock(vm_utils, '_image_uses_bittorrent')
        vm_utils._image_uses_bittorrent(
            self.context, self.instance).AndReturn(True)

        self._stub_bittorrent_download_vhd(raise_exc=RuntimeError)

        vm_utils._make_uuid_stack().AndReturn(["uuid_stack"])
        vm_utils.get_sr_path(self.session).AndReturn('sr_path')

        self._stub_glance_download_vhd()

        self.mox.StubOutWithMock(vm_utils, 'safe_find_sr')
        vm_utils.safe_find_sr(self.session).AndReturn("sr")

        self.mox.StubOutWithMock(vm_utils, '_scan_sr')
        vm_utils._scan_sr(self.session, "sr")

        self.mox.StubOutWithMock(vm_utils, '_check_vdi_size')
        vm_utils._check_vdi_size(self.context, self.session, self.instance,
                                 "vdi")

        self.mox.ReplayAll()

        self.assertEqual("vdi", vm_utils._fetch_vhd_image(self.context,
            self.session, self.instance, 'image_id')['root']['uuid'])

        self.mox.VerifyAll()

    def test_default_handler_does_not_fallback_to_itself(self):
        self.flags(torrent_base_url='http://foo', group='xenserver')

        self.mox.StubOutWithMock(vm_utils, '_image_uses_bittorrent')
        vm_utils._image_uses_bittorrent(
            self.context, self.instance).AndReturn(False)

        self._stub_glance_download_vhd(raise_exc=RuntimeError)

        self.mox.ReplayAll()

        self.assertRaises(RuntimeError, vm_utils._fetch_vhd_image,
                self.context, self.session, self.instance, 'image_id')

        self.mox.VerifyAll()


class TestImageCompression(VMUtilsTestBase):
    def test_image_compression(self):
        # Testing for nova.conf, too low, negative, and a correct value.
        self.assertIsNone(vm_utils.get_compression_level())
        self.flags(image_compression_level=6, group='xenserver')
        self.assertEqual(vm_utils.get_compression_level(), 6)


class ResizeHelpersTestCase(VMUtilsTestBase):
    def setUp(self):
        super(ResizeHelpersTestCase, self).setUp()
        self.context = context.RequestContext('user', 'project')

    def test_repair_filesystem(self):
        self.mox.StubOutWithMock(utils, 'execute')

        utils.execute('e2fsck', '-f', "-y", "fakepath",
            run_as_root=True, check_exit_code=[0, 1, 2]).AndReturn(
                ("size is: 42", ""))

        self.mox.ReplayAll()

        vm_utils._repair_filesystem("fakepath")

    def _call_tune2fs_remove_journal(self, path):
        utils.execute("tune2fs", "-O ^has_journal", path, run_as_root=True)

    def _call_tune2fs_add_journal(self, path):
        utils.execute("tune2fs", "-j", path, run_as_root=True)

    def _call_parted_mkpart(self, path, start, end):
        utils.execute('parted', '--script', path, 'rm', '1',
            run_as_root=True)
        utils.execute('parted', '--script', path, 'mkpart',
            'primary', '%ds' % start, '%ds' % end, run_as_root=True)

    def _call_parted_boot_flag(self, path):
        utils.execute('parted', '--script', path, 'set', '1',
            'boot', 'on', run_as_root=True)

    def test_resize_part_and_fs_down_succeeds(self):
        self.mox.StubOutWithMock(vm_utils, "_repair_filesystem")
        self.mox.StubOutWithMock(utils, 'execute')

        dev_path = "/dev/fake"
        partition_path = "%s1" % dev_path
        vm_utils._repair_filesystem(partition_path)
        self._call_tune2fs_remove_journal(partition_path)
        utils.execute("resize2fs", partition_path, "10s", run_as_root=True)
        self._call_parted_mkpart(dev_path, 0, 9)
        self._call_parted_boot_flag(dev_path)
        self._call_tune2fs_add_journal(partition_path)

        self.mox.ReplayAll()

        vm_utils._resize_part_and_fs("fake", 0, 20, 10, "boot")

    def test_log_progress_if_required(self):
        self.mox.StubOutWithMock(vm_utils.LOG, "debug")
        vm_utils.LOG.debug("Sparse copy in progress, "
                           "%(complete_pct).2f%% complete. "
                           "%(left)s bytes left to copy",
                           {"complete_pct": 50.0, "left": 1})
        current = timeutils.utcnow()
        time_fixture = self.useFixture(utils_fixture.TimeFixture(current))
        time_fixture.advance_time_seconds(
            vm_utils.PROGRESS_INTERVAL_SECONDS + 1)
        self.mox.ReplayAll()
        vm_utils._log_progress_if_required(1, current, 2)

    def test_log_progress_if_not_required(self):
        self.mox.StubOutWithMock(vm_utils.LOG, "debug")
        current = timeutils.utcnow()
        time_fixture = self.useFixture(utils_fixture.TimeFixture(current))
        time_fixture.advance_time_seconds(
            vm_utils.PROGRESS_INTERVAL_SECONDS - 1)
        self.mox.ReplayAll()
        vm_utils._log_progress_if_required(1, current, 2)

    def test_resize_part_and_fs_down_fails_disk_too_big(self):
        self.mox.StubOutWithMock(vm_utils, "_repair_filesystem")
        self.mox.StubOutWithMock(utils, 'execute')

        dev_path = "/dev/fake"
        partition_path = "%s1" % dev_path
        new_sectors = 10
        vm_utils._repair_filesystem(partition_path)
        self._call_tune2fs_remove_journal(partition_path)
        mobj = utils.execute("resize2fs",
                             partition_path,
                             "%ss" % new_sectors,
                             run_as_root=True)
        mobj.AndRaise(processutils.ProcessExecutionError)
        self.mox.ReplayAll()
        self.assertRaises(exception.ResizeError,
                          vm_utils._resize_part_and_fs,
                          "fake", 0, 20, 10, "boot")

    def test_resize_part_and_fs_up_succeeds(self):
        self.mox.StubOutWithMock(vm_utils, "_repair_filesystem")
        self.mox.StubOutWithMock(utils, 'execute')

        dev_path = "/dev/fake"
        partition_path = "%s1" % dev_path
        vm_utils._repair_filesystem(partition_path)
        self._call_tune2fs_remove_journal(partition_path)
        self._call_parted_mkpart(dev_path, 0, 29)
        utils.execute("resize2fs", partition_path, run_as_root=True)
        self._call_tune2fs_add_journal(partition_path)

        self.mox.ReplayAll()

        vm_utils._resize_part_and_fs("fake", 0, 20, 30, "")

    def test_resize_disk_throws_on_zero_size(self):
        flavor = fake_flavor.fake_flavor_obj(self.context, root_gb=0)
        self.assertRaises(exception.ResizeError, vm_utils.resize_disk,
                          "session", "instance", "vdi_ref", flavor)

    def test_auto_config_disk_returns_early_on_zero_size(self):
        vm_utils.try_auto_configure_disk("bad_session", "bad_vdi_ref", 0)

    @mock.patch.object(utils, "execute")
    def test_get_partitions(self, mock_execute):
        parted_return = "BYT;\n...\n"
        parted_return += "1:2s:11s:10s:ext3::boot;\n"
        parted_return += "2:20s:11s:10s::bob:;\n"
        mock_execute.return_value = (parted_return, None)

        partitions = vm_utils._get_partitions("abc")

        self.assertEqual(2, len(partitions))
        self.assertEqual((1, 2, 10, "ext3", "", "boot"), partitions[0])
        self.assertEqual((2, 20, 10, "", "bob", ""), partitions[1])


class CheckVDISizeTestCase(VMUtilsTestBase):
    def setUp(self):
        super(CheckVDISizeTestCase, self).setUp()
        self.context = 'fakecontext'
        self.session = 'fakesession'
        self.instance = objects.Instance(uuid=str(uuid.uuid4()))
        self.flavor = objects.Flavor()
        self.vdi_uuid = 'fakeuuid'

    def test_not_too_large(self):
        self.mox.StubOutWithMock(vm_utils, '_get_vdi_chain_size')
        vm_utils._get_vdi_chain_size(self.session,
                self.vdi_uuid).AndReturn(1073741824)

        self.mox.ReplayAll()

        with mock.patch.object(self.instance, 'get_flavor') as get:
            self.flavor.root_gb = 1
            get.return_value = self.flavor
            vm_utils._check_vdi_size(self.context, self.session, self.instance,
                                     self.vdi_uuid)

    def test_too_large(self):
        self.mox.StubOutWithMock(vm_utils, '_get_vdi_chain_size')
        vm_utils._get_vdi_chain_size(self.session,
                self.vdi_uuid).AndReturn(11811160065)  # 10GB overhead allowed

        self.mox.ReplayAll()

        with mock.patch.object(self.instance, 'get_flavor') as get:
            self.flavor.root_gb = 1
            get.return_value = self.flavor
            self.assertRaises(exception.FlavorDiskSmallerThanImage,
                              vm_utils._check_vdi_size, self.context,
                              self.session, self.instance, self.vdi_uuid)

    def test_zero_root_gb_disables_check(self):
        with mock.patch.object(self.instance, 'get_flavor') as get:
            self.flavor.root_gb = 0
            get.return_value = self.flavor
            vm_utils._check_vdi_size(self.context, self.session, self.instance,
                                     self.vdi_uuid)


class GetInstanceForVdisForSrTestCase(VMUtilsTestBase):
    def setUp(self):
        super(GetInstanceForVdisForSrTestCase, self).setUp()
        self.fixture = self.useFixture(config_fixture.Config(lockutils.CONF))
        self.fixture.config(disable_process_locking=True,
                            group='oslo_concurrency')
        self.flags(instance_name_template='%d',
                   firewall_driver='nova.virt.xenapi.firewall.'
                                   'Dom0IptablesFirewallDriver')
        self.flags(connection_url='test_url',
                   connection_password='test_pass',
                   group='xenserver')

    def test_get_instance_vdis_for_sr(self):
        vm_ref = fake.create_vm("foo", "Running")
        sr_ref = fake.create_sr()

        vdi_1 = fake.create_vdi('vdiname1', sr_ref)
        vdi_2 = fake.create_vdi('vdiname2', sr_ref)

        for vdi_ref in [vdi_1, vdi_2]:
            fake.create_vbd(vm_ref, vdi_ref)

        stubs.stubout_session(self.stubs, fake.SessionBase)
        driver = xenapi_conn.XenAPIDriver(False)

        result = list(vm_utils.get_instance_vdis_for_sr(
            driver._session, vm_ref, sr_ref))

        self.assertEqual([vdi_1, vdi_2], result)

    def test_get_instance_vdis_for_sr_no_vbd(self):
        vm_ref = fake.create_vm("foo", "Running")
        sr_ref = fake.create_sr()

        stubs.stubout_session(self.stubs, fake.SessionBase)
        driver = xenapi_conn.XenAPIDriver(False)

        result = list(vm_utils.get_instance_vdis_for_sr(
            driver._session, vm_ref, sr_ref))

        self.assertEqual([], result)


class VMRefOrRaiseVMFoundTestCase(VMUtilsTestBase):

    @mock.patch.object(vm_utils, 'lookup', return_value='ignored')
    def test_lookup_call(self, mock_lookup):
        vm_utils.vm_ref_or_raise('session', 'somename')
        mock_lookup.assert_called_once_with('session', 'somename')

    @mock.patch.object(vm_utils, 'lookup', return_value='vmref')
    def test_return_value(self, mock_lookup):
        self.assertEqual(
            'vmref', vm_utils.vm_ref_or_raise('session', 'somename'))
        mock_lookup.assert_called_once_with('session', 'somename')


class VMRefOrRaiseVMNotFoundTestCase(VMUtilsTestBase):

    @mock.patch.object(vm_utils, 'lookup', return_value=None)
    def test_exception_raised(self, mock_lookup):
        self.assertRaises(
            exception.InstanceNotFound,
            lambda: vm_utils.vm_ref_or_raise('session', 'somename')
        )
        mock_lookup.assert_called_once_with('session', 'somename')

    @mock.patch.object(vm_utils, 'lookup', return_value=None)
    def test_exception_msg_contains_vm_name(self, mock_lookup):
        try:
            vm_utils.vm_ref_or_raise('session', 'somename')
        except exception.InstanceNotFound as e:
            self.assertIn('somename', six.text_type(e))
        mock_lookup.assert_called_once_with('session', 'somename')


@mock.patch.object(vm_utils, 'safe_find_sr', return_value='safe_find_sr')
class CreateCachedImageTestCase(VMUtilsTestBase):
    def setUp(self):
        super(CreateCachedImageTestCase, self).setUp()
        self.session = stubs.get_fake_session()

    @mock.patch.object(vm_utils, '_clone_vdi', return_value='new_vdi_ref')
    def test_cached(self, mock_clone_vdi, mock_safe_find_sr):
        self.session.call_xenapi.side_effect = ['ext', {'vdi_ref': 2},
                                                None, None, None, 'vdi_uuid']
        self.assertEqual((False, {'root': {'uuid': 'vdi_uuid', 'file': None}}),
                         vm_utils._create_cached_image('context', self.session,
                                    'instance', 'name', 'uuid',
                                    vm_utils.ImageType.DISK_VHD))

    @mock.patch.object(vm_utils, '_safe_copy_vdi', return_value='new_vdi_ref')
    def test_no_cow(self, mock_safe_copy_vdi, mock_safe_find_sr):
        self.flags(use_cow_images=False)
        self.session.call_xenapi.side_effect = ['ext', {'vdi_ref': 2},
                                                None, None, None, 'vdi_uuid']
        self.assertEqual((False, {'root': {'uuid': 'vdi_uuid', 'file': None}}),
                         vm_utils._create_cached_image('context', self.session,
                                    'instance', 'name', 'uuid',
                                    vm_utils.ImageType.DISK_VHD))

    def test_no_cow_no_ext(self, mock_safe_find_sr):
        self.flags(use_cow_images=False)
        self.session.call_xenapi.side_effect = ['non-ext', {'vdi_ref': 2},
                                                'vdi_ref', None, None, None,
                                                'vdi_uuid']
        self.assertEqual((False, {'root': {'uuid': 'vdi_uuid', 'file': None}}),
                         vm_utils._create_cached_image('context', self.session,
                                    'instance', 'name', 'uuid',
                                    vm_utils.ImageType.DISK_VHD))

    @mock.patch.object(vm_utils, '_clone_vdi', return_value='new_vdi_ref')
    @mock.patch.object(vm_utils, '_fetch_image',
                       return_value={'root': {'uuid': 'vdi_uuid',
                                              'file': None}})
    def test_noncached(self, mock_fetch_image, mock_clone_vdi,
                       mock_safe_find_sr):
        self.session.call_xenapi.side_effect = ['ext', {}, 'cache_vdi_ref',
                                                None, None, None, None, None,
                                                None, 'vdi_uuid']
        self.assertEqual((True, {'root': {'uuid': 'vdi_uuid', 'file': None}}),
                         vm_utils._create_cached_image('context', self.session,
                                    'instance', 'name', 'uuid',
                                    vm_utils.ImageType.DISK_VHD))


class BittorrentTestCase(VMUtilsTestBase):
    def setUp(self):
        super(BittorrentTestCase, self).setUp()
        self.context = context.get_admin_context()

    def test_image_uses_bittorrent(self):
        instance = {'system_metadata': {'image_bittorrent': True}}
        self.flags(torrent_images='some', group='xenserver')
        self.assertTrue(vm_utils._image_uses_bittorrent(self.context,
                                                        instance))

    def _test_create_image(self, cache_type):
        instance = {'system_metadata': {'image_cache_in_nova': True}}
        self.flags(cache_images=cache_type, group='xenserver')

        was = {'called': None}

        def fake_create_cached_image(*args):
            was['called'] = 'some'
            return (False, {})
        self.stubs.Set(vm_utils, '_create_cached_image',
                       fake_create_cached_image)

        def fake_fetch_image(*args):
            was['called'] = 'none'
            return {}
        self.stubs.Set(vm_utils, '_fetch_image',
                       fake_fetch_image)

        vm_utils.create_image(self.context, None, instance,
                              'foo', 'bar', 'baz')

        self.assertEqual(was['called'], cache_type)

    def test_create_image_cached(self):
        self._test_create_image('some')

    def test_create_image_uncached(self):
        self._test_create_image('none')


class ShutdownTestCase(VMUtilsTestBase):

    def test_hardshutdown_should_return_true_when_vm_is_shutdown(self):
        self.mock = mox.Mox()
        session = FakeSession()
        instance = "instance"
        vm_ref = "vm-ref"
        self.mock.StubOutWithMock(vm_utils, 'is_vm_shutdown')
        vm_utils.is_vm_shutdown(session, vm_ref).AndReturn(True)
        self.mock.StubOutWithMock(vm_utils, 'LOG')
        self.assertTrue(vm_utils.hard_shutdown_vm(
            session, instance, vm_ref))

    def test_cleanshutdown_should_return_true_when_vm_is_shutdown(self):
        self.mock = mox.Mox()
        session = FakeSession()
        instance = "instance"
        vm_ref = "vm-ref"
        self.mock.StubOutWithMock(vm_utils, 'is_vm_shutdown')
        vm_utils.is_vm_shutdown(session, vm_ref).AndReturn(True)
        self.mock.StubOutWithMock(vm_utils, 'LOG')
        self.assertTrue(vm_utils.clean_shutdown_vm(
            session, instance, vm_ref))


class CreateVBDTestCase(VMUtilsTestBase):
    def setUp(self):
        super(CreateVBDTestCase, self).setUp()
        self.session = FakeSession()
        self.mock = mox.Mox()
        self.mock.StubOutWithMock(self.session, 'call_xenapi')
        self.vbd_rec = self._generate_vbd_rec()

    def _generate_vbd_rec(self):
        vbd_rec = {}
        vbd_rec['VM'] = 'vm_ref'
        vbd_rec['VDI'] = 'vdi_ref'
        vbd_rec['userdevice'] = '0'
        vbd_rec['bootable'] = False
        vbd_rec['mode'] = 'RW'
        vbd_rec['type'] = 'disk'
        vbd_rec['unpluggable'] = True
        vbd_rec['empty'] = False
        vbd_rec['other_config'] = {}
        vbd_rec['qos_algorithm_type'] = ''
        vbd_rec['qos_algorithm_params'] = {}
        vbd_rec['qos_supported_algorithms'] = []
        return vbd_rec

    def test_create_vbd_default_args(self):
        self.session.call_xenapi('VBD.create',
                self.vbd_rec).AndReturn("vbd_ref")
        self.mock.ReplayAll()

        result = vm_utils.create_vbd(self.session, "vm_ref", "vdi_ref", 0)
        self.assertEqual(result, "vbd_ref")
        self.mock.VerifyAll()

    def test_create_vbd_osvol(self):
        self.session.call_xenapi('VBD.create',
                self.vbd_rec).AndReturn("vbd_ref")
        self.session.call_xenapi('VBD.add_to_other_config', "vbd_ref",
                                 "osvol", "True")
        self.mock.ReplayAll()
        result = vm_utils.create_vbd(self.session, "vm_ref", "vdi_ref", 0,
                                     osvol=True)
        self.assertEqual(result, "vbd_ref")
        self.mock.VerifyAll()

    def test_create_vbd_extra_args(self):
        self.vbd_rec['VDI'] = 'OpaqueRef:NULL'
        self.vbd_rec['type'] = 'a'
        self.vbd_rec['mode'] = 'RO'
        self.vbd_rec['bootable'] = True
        self.vbd_rec['empty'] = True
        self.vbd_rec['unpluggable'] = False
        self.session.call_xenapi('VBD.create',
                self.vbd_rec).AndReturn("vbd_ref")
        self.mock.ReplayAll()

        result = vm_utils.create_vbd(self.session, "vm_ref", None, 0,
                vbd_type="a", read_only=True, bootable=True,
                empty=True, unpluggable=False)
        self.assertEqual(result, "vbd_ref")
        self.mock.VerifyAll()

    def test_attach_cd(self):
        self.mock.StubOutWithMock(vm_utils, 'create_vbd')

        vm_utils.create_vbd(self.session, "vm_ref", None, 1,
                vbd_type='cd', read_only=True, bootable=True,
                empty=True, unpluggable=False).AndReturn("vbd_ref")
        self.session.call_xenapi('VBD.insert', "vbd_ref", "vdi_ref")
        self.mock.ReplayAll()

        result = vm_utils.attach_cd(self.session, "vm_ref", "vdi_ref", 1)
        self.assertEqual(result, "vbd_ref")
        self.mock.VerifyAll()


class UnplugVbdTestCase(VMUtilsTestBase):
    @mock.patch.object(greenthread, 'sleep')
    def test_unplug_vbd_works(self, mock_sleep):
        session = stubs.get_fake_session()
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'

        vm_utils.unplug_vbd(session, vbd_ref, vm_ref)

        session.call_xenapi.assert_called_once_with('VBD.unplug', vbd_ref)
        self.assertEqual(0, mock_sleep.call_count)

    def test_unplug_vbd_raises_unexpected_error(self):
        session = stubs.get_fake_session()
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'
        session.call_xenapi.side_effect = test.TestingException()

        self.assertRaises(test.TestingException, vm_utils.unplug_vbd,
                          session, vm_ref, vbd_ref)
        self.assertEqual(1, session.call_xenapi.call_count)

    def test_unplug_vbd_already_detached_works(self):
        error = "DEVICE_ALREADY_DETACHED"
        session = stubs.get_fake_session(error)
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'

        vm_utils.unplug_vbd(session, vbd_ref, vm_ref)
        self.assertEqual(1, session.call_xenapi.call_count)

    def test_unplug_vbd_already_raises_unexpected_xenapi_error(self):
        session = stubs.get_fake_session("")
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'

        self.assertRaises(exception.StorageError, vm_utils.unplug_vbd,
                          session, vbd_ref, vm_ref)
        self.assertEqual(1, session.call_xenapi.call_count)

    def _test_uplug_vbd_retries(self, mock_sleep, error):
        session = stubs.get_fake_session(error)
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'

        self.assertRaises(exception.StorageError, vm_utils.unplug_vbd,
                          session, vm_ref, vbd_ref)

        self.assertEqual(11, session.call_xenapi.call_count)
        self.assertEqual(10, mock_sleep.call_count)

    def _test_uplug_vbd_retries_with_neg_val(self):
        session = stubs.get_fake_session()
        self.flags(num_vbd_unplug_retries=-1, group='xenserver')
        vbd_ref = "vbd_ref"
        vm_ref = 'vm_ref'

        vm_utils.unplug_vbd(session, vbd_ref, vm_ref)
        self.assertEqual(1, session.call_xenapi.call_count)

    @mock.patch.object(greenthread, 'sleep')
    def test_uplug_vbd_retries_on_rejected(self, mock_sleep):
        self._test_uplug_vbd_retries(mock_sleep,
                                     "DEVICE_DETACH_REJECTED")

    @mock.patch.object(greenthread, 'sleep')
    def test_uplug_vbd_retries_on_internal_error(self, mock_sleep):
        self._test_uplug_vbd_retries(mock_sleep,
                                     "INTERNAL_ERROR")


class VDIOtherConfigTestCase(VMUtilsTestBase):
    """Tests to ensure that the code is populating VDI's `other_config`
    attribute with the correct metadta.
    """

    def setUp(self):
        super(VDIOtherConfigTestCase, self).setUp()

        class _FakeSession(object):
            def call_xenapi(self, operation, *args, **kwargs):
                # VDI.add_to_other_config -> VDI_add_to_other_config
                method = getattr(self, operation.replace('.', '_'), None)
                if method:
                    return method(*args, **kwargs)

                self.operation = operation
                self.args = args
                self.kwargs = kwargs

        self.session = _FakeSession()
        self.context = context.get_admin_context()
        self.fake_instance = {'uuid': 'aaaa-bbbb-cccc-dddd',
                              'name': 'myinstance'}

    def test_create_vdi(self):
        # Some images are registered with XenServer explicitly by calling
        # `create_vdi`
        vm_utils.create_vdi(self.session, 'sr_ref', self.fake_instance,
                            'myvdi', 'root', 1024, read_only=True)

        expected = {'nova_disk_type': 'root',
                    'nova_instance_uuid': 'aaaa-bbbb-cccc-dddd'}

        self.assertEqual(expected, self.session.args[0]['other_config'])

    def test_create_image(self):
        # Other images are registered implicitly when they are dropped into
        # the SR by a dom0 plugin or some other process
        self.flags(cache_images='none', group='xenserver')

        def fake_fetch_image(*args):
            return {'root': {'uuid': 'fake-uuid'}}

        self.stubs.Set(vm_utils, '_fetch_image', fake_fetch_image)

        other_config = {}

        def VDI_add_to_other_config(ref, key, value):
            other_config[key] = value

        # Stubbing on the session object and not class so we don't pollute
        # other tests
        self.session.VDI_add_to_other_config = VDI_add_to_other_config
        self.session.VDI_get_other_config = lambda vdi: {}

        vm_utils.create_image(self.context, self.session, self.fake_instance,
                'myvdi', 'image1', vm_utils.ImageType.DISK_VHD)

        expected = {'nova_disk_type': 'root',
                    'nova_instance_uuid': 'aaaa-bbbb-cccc-dddd'}

        self.assertEqual(expected, other_config)

    def test_import_migrated_vhds(self):
        # Migrated images should preserve the `other_config`
        other_config = {}

        def VDI_add_to_other_config(ref, key, value):
            other_config[key] = value

        def call_plugin_serialized(*args, **kwargs):
            return {'root': {'uuid': 'aaaa-bbbb-cccc-dddd'}}

        # Stubbing on the session object and not class so we don't pollute
        # other tests
        self.session.VDI_add_to_other_config = VDI_add_to_other_config
        self.session.VDI_get_other_config = lambda vdi: {}
        self.session.call_plugin_serialized = call_plugin_serialized

        self.stubs.Set(vm_utils, 'get_sr_path', lambda *a, **k: None)
        self.stubs.Set(vm_utils, 'scan_default_sr', lambda *a, **k: None)

        vm_utils._import_migrated_vhds(self.session, self.fake_instance,
                                       "disk_label", "root", "vdi_label")

        expected = {'nova_disk_type': 'root',
                    'nova_instance_uuid': 'aaaa-bbbb-cccc-dddd'}

        self.assertEqual(expected, other_config)


class GenerateDiskTestCase(VMUtilsTestBase):

    @mock.patch.object(vm_utils, 'vdi_attached')
    @mock.patch.object(vm_utils.utils, 'mkfs',
                       side_effect = test.TestingException())
    @mock.patch.object(vm_utils, '_get_dom0_ref', return_value='dom0_ref')
    @mock.patch.object(vm_utils, 'safe_find_sr', return_value='sr_ref')
    @mock.patch.object(vm_utils, 'create_vdi', return_value='vdi_ref')
    @mock.patch.object(vm_utils, 'create_vbd')
    def test_generate_disk_with_no_fs_given(self, mock_create_vbd,
                                            mock_create_vdi, mock_findsr,
                                            mock_dom0ref, mock_mkfs,
                                            mock_attached_here):
        session = stubs.get_fake_session()
        vdi_ref = mock.MagicMock()
        mock_attached_here.return_value = vdi_ref

        instance = {'uuid': 'fake_uuid'}
        vm_utils._generate_disk(session, instance, 'vm_ref', '2',
                                'name', 'user', 10, None, None)

        mock_attached_here.assert_called_once_with(session, 'vdi_ref',
                                                   read_only=False,
                                                   dom0=True)

        mock_create_vbd.assert_called_with(session, 'vm_ref', 'vdi_ref', '2',
                                           bootable=False)

    @mock.patch.object(vm_utils, 'vdi_attached')
    @mock.patch.object(vm_utils.utils, 'mkfs')
    @mock.patch.object(vm_utils, '_get_dom0_ref', return_value='dom0_ref')
    @mock.patch.object(vm_utils, 'safe_find_sr', return_value='sr_ref')
    @mock.patch.object(vm_utils, 'create_vdi', return_value='vdi_ref')
    @mock.patch.object(vm_utils.utils, 'make_dev_path',
                       return_value='/dev/fake_devp1')
    @mock.patch.object(vm_utils, 'create_vbd')
    def test_generate_disk_swap(self, mock_create_vbd, mock_make_path,
                                mock_create_vdi,
                                mock_findsr, mock_dom0ref, mock_mkfs,
                                mock_attached_here):
        session = stubs.get_fake_session()
        vdi_dev = mock.MagicMock()
        mock_attached_here.return_value = vdi_dev
        vdi_dev.__enter__.return_value = 'fakedev'
        instance = {'uuid': 'fake_uuid'}

        vm_utils._generate_disk(session, instance, 'vm_ref', '2',
                                'name', 'user', 10, 'swap',
                                'swap-1')

        mock_attached_here.assert_any_call(session, 'vdi_ref',
                                           read_only=False,
                                           dom0=True)

        # As swap is supported in dom0, mkfs will run there
        session.call_plugin_serialized.assert_any_call(
            'partition_utils.py', 'mkfs', 'fakedev', '1', 'swap', 'swap-1')

        mock_create_vbd.assert_called_with(session, 'vm_ref', 'vdi_ref', '2',
                                           bootable=False)

    @mock.patch.object(vm_utils, 'vdi_attached')
    @mock.patch.object(vm_utils.utils, 'mkfs')
    @mock.patch.object(vm_utils, '_get_dom0_ref', return_value='dom0_ref')
    @mock.patch.object(vm_utils, 'safe_find_sr', return_value='sr_ref')
    @mock.patch.object(vm_utils, 'create_vdi', return_value='vdi_ref')
    @mock.patch.object(vm_utils.utils, 'make_dev_path',
                       return_value='/dev/fake_devp1')
    @mock.patch.object(vm_utils, 'create_vbd')
    def test_generate_disk_ephemeral(self, mock_create_vbd, mock_make_path,
                                     mock_create_vdi, mock_findsr,
                                     mock_dom0ref, mock_mkfs,
                                     mock_attached_here):
        session = stubs.get_fake_session()
        vdi_ref = mock.MagicMock()
        mock_attached_here.return_value = vdi_ref
        instance = {'uuid': 'fake_uuid'}

        vm_utils._generate_disk(session, instance, 'vm_ref', '2',
                                'name', 'ephemeral', 10, 'ext4',
                                'ephemeral-1')

        mock_attached_here.assert_any_call(session, 'vdi_ref',
                                           read_only=False,
                                           dom0=True)

        # As ext4 is not supported in dom0, mkfs will run in domU
        mock_attached_here.assert_any_call(session, 'vdi_ref',
                                           read_only=False)
        mock_mkfs.assert_called_with('ext4', '/dev/fake_devp1',
                                     'ephemeral-1', run_as_root=True)

        mock_create_vbd.assert_called_with(session, 'vm_ref', 'vdi_ref', '2',
                                           bootable=False)

    @mock.patch.object(vm_utils, 'safe_find_sr', return_value='sr_ref')
    @mock.patch.object(vm_utils, 'create_vdi', return_value='vdi_ref')
    @mock.patch.object(vm_utils, '_get_dom0_ref',
                       side_effect = test.TestingException())
    @mock.patch.object(vm_utils, 'safe_destroy_vdis')
    def test_generate_disk_ensure_cleanup_called(self, mock_destroy_vdis,
                                                 mock_dom0ref,
                                                 mock_create_vdi,
                                                 mock_findsr):
        session = stubs.get_fake_session()
        instance = {'uuid': 'fake_uuid'}

        self.assertRaises(test.TestingException, vm_utils._generate_disk,
                          session, instance, None, '2', 'name', 'user', 10,
                          None, None)

        mock_destroy_vdis.assert_called_once_with(session, ['vdi_ref'])

    @mock.patch.object(vm_utils, 'safe_find_sr', return_value='sr_ref')
    @mock.patch.object(vm_utils, 'create_vdi', return_value='vdi_ref')
    @mock.patch.object(vm_utils, 'vdi_attached')
    @mock.patch.object(vm_utils, '_get_dom0_ref', return_value='dom0_ref')
    @mock.patch.object(vm_utils, 'create_vbd')
    def test_generate_disk_ephemeral_no_vmref(self, mock_create_vbd,
                                              mock_dom0_ref,
                                              mock_attached_here,
                                              mock_create_vdi,
                                              mock_findsr):
        session = stubs.get_fake_session()
        vdi_ref = mock.MagicMock()
        mock_attached_here.return_value = vdi_ref
        instance = {'uuid': 'fake_uuid'}

        vdi_ref = vm_utils._generate_disk(
            session, instance,
            None, None, 'name', 'user', 10, None, None)

        mock_attached_here.assert_called_once_with(session, 'vdi_ref',
                                                   read_only=False, dom0=True)
        self.assertFalse(mock_create_vbd.called)


class GenerateEphemeralTestCase(VMUtilsTestBase):
    def setUp(self):
        super(GenerateEphemeralTestCase, self).setUp()
        self.session = "session"
        self.instance = "instance"
        self.vm_ref = "vm_ref"
        self.name_label = "name"
        self.ephemeral_name_label = "name ephemeral"
        self.userdevice = 4
        self.fs_label = "ephemeral"
        self.mox.StubOutWithMock(vm_utils, "_generate_disk")
        self.mox.StubOutWithMock(vm_utils, "safe_destroy_vdis")

    def test_get_ephemeral_disk_sizes_simple(self):
        result = vm_utils.get_ephemeral_disk_sizes(20)
        expected = [20]
        self.assertEqual(expected, list(result))

    def test_get_ephemeral_disk_sizes_three_disks_2000(self):
        result = vm_utils.get_ephemeral_disk_sizes(4030)
        expected = [2000, 2000, 30]
        self.assertEqual(expected, list(result))

    def test_get_ephemeral_disk_sizes_two_disks_1024(self):
        result = vm_utils.get_ephemeral_disk_sizes(2048)
        expected = [1024, 1024]
        self.assertEqual(expected, list(result))

    def _expect_generate_disk(self, size, device, name_label, fs_label):
        vm_utils._generate_disk(
            self.session, self.instance, self.vm_ref,
            str(device), name_label, 'ephemeral',
            size * 1024, None, fs_label).AndReturn(device)

    def test_generate_ephemeral_adds_one_disk(self):
        self._expect_generate_disk(
            20, self.userdevice, self.ephemeral_name_label, self.fs_label)
        self.mox.ReplayAll()

        vm_utils.generate_ephemeral(
            self.session, self.instance, self.vm_ref,
            str(self.userdevice), self.name_label, 20)

    def test_generate_ephemeral_adds_multiple_disks(self):
        self._expect_generate_disk(
            2000, self.userdevice, self.ephemeral_name_label, self.fs_label)
        self._expect_generate_disk(
            2000, self.userdevice + 1, self.ephemeral_name_label + " (1)",
            self.fs_label + "1")
        self._expect_generate_disk(
            30, self.userdevice + 2, self.ephemeral_name_label + " (2)",
            self.fs_label + "2")
        self.mox.ReplayAll()

        vm_utils.generate_ephemeral(
            self.session, self.instance, self.vm_ref,
            str(self.userdevice), self.name_label, 4030)

    def test_generate_ephemeral_cleans_up_on_error(self):
        self._expect_generate_disk(
            1024, self.userdevice, self.ephemeral_name_label, self.fs_label)
        self._expect_generate_disk(
            1024, self.userdevice + 1, self.ephemeral_name_label + " (1)",
            self.fs_label + "1")

        vm_utils._generate_disk(
            self.session, self.instance, self.vm_ref,
            str(self.userdevice + 2), "name ephemeral (2)", 'ephemeral',
            units.Mi, None, 'ephemeral2').AndRaise(exception.NovaException)

        vm_utils.safe_destroy_vdis(self.session, [4, 5])

        self.mox.ReplayAll()

        self.assertRaises(
            exception.NovaException, vm_utils.generate_ephemeral,
            self.session, self.instance, self.vm_ref,
            str(self.userdevice), self.name_label, 4096)


class FakeFile(object):
    def __init__(self):
        self._file_operations = []

    def seek(self, offset):
        self._file_operations.append((self.seek, offset))


class StreamDiskTestCase(VMUtilsTestBase):
    def setUp(self):
        super(StreamDiskTestCase, self).setUp()
        self.mox.StubOutWithMock(vm_utils.utils, 'make_dev_path')
        self.mox.StubOutWithMock(vm_utils.utils, 'temporary_chown')
        self.mox.StubOutWithMock(vm_utils, '_write_partition')

        # NOTE(matelakat): This might hide the fail reason, as test runners
        # are unhappy with a mocked out open.
        self.mox.StubOutWithMock(six.moves.builtins, 'open')
        self.image_service_func = self.mox.CreateMockAnything()

    def test_non_ami(self):
        fake_file = FakeFile()

        vm_utils.utils.make_dev_path('dev').AndReturn('some_path')
        vm_utils.utils.temporary_chown(
            'some_path').AndReturn(contextified(None))
        open('some_path', 'wb').AndReturn(contextified(fake_file))
        self.image_service_func(fake_file)

        self.mox.ReplayAll()

        vm_utils._stream_disk("session", self.image_service_func,
                              vm_utils.ImageType.KERNEL, None, 'dev')

        self.assertEqual([(fake_file.seek, 0)], fake_file._file_operations)

    def test_ami_disk(self):
        fake_file = FakeFile()

        vm_utils._write_partition("session", 100, 'dev')
        vm_utils.utils.make_dev_path('dev').AndReturn('some_path')
        vm_utils.utils.temporary_chown(
            'some_path').AndReturn(contextified(None))
        open('some_path', 'wb').AndReturn(contextified(fake_file))
        self.image_service_func(fake_file)

        self.mox.ReplayAll()

        vm_utils._stream_disk("session", self.image_service_func,
                              vm_utils.ImageType.DISK, 100, 'dev')

        self.assertEqual(
            [(fake_file.seek, vm_utils.MBR_SIZE_BYTES)],
            fake_file._file_operations)


class VMUtilsSRPath(VMUtilsTestBase):
    def setUp(self):
        super(VMUtilsSRPath, self).setUp()
        self.fixture = self.useFixture(config_fixture.Config(lockutils.CONF))
        self.fixture.config(disable_process_locking=True,
                            group='oslo_concurrency')
        self.flags(instance_name_template='%d',
                   firewall_driver='nova.virt.xenapi.firewall.'
                                   'Dom0IptablesFirewallDriver')
        self.flags(connection_url='test_url',
                   connection_password='test_pass',
                   group='xenserver')
        stubs.stubout_session(self.stubs, fake.SessionBase)
        driver = xenapi_conn.XenAPIDriver(False)
        self.session = driver._session
        self.session.is_local_connection = False

    def test_defined(self):
        self.mox.StubOutWithMock(vm_utils, "safe_find_sr")
        self.mox.StubOutWithMock(self.session, "call_xenapi")

        vm_utils.safe_find_sr(self.session).AndReturn("sr_ref")
        self.session.host_ref = "host_ref"
        self.session.call_xenapi('PBD.get_all_records_where',
            'field "host"="host_ref" and field "SR"="sr_ref"').AndReturn(
            {'pbd_ref': {'device_config': {'path': 'sr_path'}}})

        self.mox.ReplayAll()
        self.assertEqual(vm_utils.get_sr_path(self.session), "sr_path")

    def test_default(self):
        self.mox.StubOutWithMock(vm_utils, "safe_find_sr")
        self.mox.StubOutWithMock(self.session, "call_xenapi")

        vm_utils.safe_find_sr(self.session).AndReturn("sr_ref")
        self.session.host_ref = "host_ref"
        self.session.call_xenapi('PBD.get_all_records_where',
            'field "host"="host_ref" and field "SR"="sr_ref"').AndReturn(
            {'pbd_ref': {'device_config': {}}})
        self.session.call_xenapi("SR.get_record", "sr_ref").AndReturn(
            {'uuid': 'sr_uuid', 'type': 'ext'})
        self.mox.ReplayAll()
        self.assertEqual(vm_utils.get_sr_path(self.session),
                         "/var/run/sr-mount/sr_uuid")


class CreateKernelRamdiskTestCase(VMUtilsTestBase):
    def setUp(self):
        super(CreateKernelRamdiskTestCase, self).setUp()
        self.context = "context"
        self.session = FakeSession()
        self.instance = {"kernel_id": None, "ramdisk_id": None}
        self.name_label = "name"
        self.mox.StubOutWithMock(self.session, "call_plugin")
        self.mox.StubOutWithMock(uuid, "uuid4")
        self.mox.StubOutWithMock(vm_utils, "_fetch_disk_image")

    def test_create_kernel_and_ramdisk_no_create(self):
        self.mox.ReplayAll()
        result = vm_utils.create_kernel_and_ramdisk(self.context,
                    self.session, self.instance, self.name_label)
        self.assertEqual((None, None), result)

    def test_create_kernel_and_ramdisk_create_both_cached(self):
        kernel_id = "kernel"
        ramdisk_id = "ramdisk"
        self.instance["kernel_id"] = kernel_id
        self.instance["ramdisk_id"] = ramdisk_id

        args_kernel = {}
        args_kernel['cached-image'] = kernel_id
        args_kernel['new-image-uuid'] = "fake_uuid1"
        uuid.uuid4().AndReturn("fake_uuid1")
        self.session.call_plugin('kernel', 'create_kernel_ramdisk',
                                  args_kernel).AndReturn("k")

        args_ramdisk = {}
        args_ramdisk['cached-image'] = ramdisk_id
        args_ramdisk['new-image-uuid'] = "fake_uuid2"
        uuid.uuid4().AndReturn("fake_uuid2")
        self.session.call_plugin('kernel', 'create_kernel_ramdisk',
                                  args_ramdisk).AndReturn("r")

        self.mox.ReplayAll()
        result = vm_utils.create_kernel_and_ramdisk(self.context,
                    self.session, self.instance, self.name_label)
        self.assertEqual(("k", "r"), result)

    def test_create_kernel_and_ramdisk_create_kernel_not_cached(self):
        kernel_id = "kernel"
        self.instance["kernel_id"] = kernel_id

        args_kernel = {}
        args_kernel['cached-image'] = kernel_id
        args_kernel['new-image-uuid'] = "fake_uuid1"
        uuid.uuid4().AndReturn("fake_uuid1")
        self.session.call_plugin('kernel', 'create_kernel_ramdisk',
                                  args_kernel).AndReturn("")

        kernel = {"kernel": {"file": "k"}}
        vm_utils._fetch_disk_image(self.context, self.session, self.instance,
                    self.name_label, kernel_id, 0).AndReturn(kernel)

        self.mox.ReplayAll()
        result = vm_utils.create_kernel_and_ramdisk(self.context,
                    self.session, self.instance, self.name_label)
        self.assertEqual(("k", None), result)

    def _test_create_kernel_image(self, cache_images):
        kernel_id = "kernel"
        self.instance["kernel_id"] = kernel_id

        args_kernel = {}
        args_kernel['cached-image'] = kernel_id
        args_kernel['new-image-uuid'] = "fake_uuid1"
        self.flags(cache_images=cache_images, group='xenserver')

        if cache_images == 'all':
            uuid.uuid4().AndReturn("fake_uuid1")
            self.session.call_plugin('kernel', 'create_kernel_ramdisk',
                                     args_kernel).AndReturn("cached_image")
        else:
            kernel = {"kernel": {"file": "new_image", "uuid": None}}
            vm_utils._fetch_disk_image(self.context, self.session,
                                       self.instance, self.name_label,
                                       kernel_id, 0).AndReturn(kernel)

        self.mox.ReplayAll()

        result = vm_utils._create_kernel_image(self.context,
                                               self.session,
                                               self.instance,
                                               self.name_label,
                                               kernel_id, 0)

        if cache_images == 'all':
            self.assertEqual(result, {"kernel":
                                      {"file": "cached_image", "uuid": None}})
        else:
            self.assertEqual(result, {"kernel":
                                      {"file": "new_image", "uuid": None}})

    def test_create_kernel_image_cached_config(self):
        self._test_create_kernel_image('all')

    def test_create_kernel_image_uncached_config(self):
        self._test_create_kernel_image('none')


class ScanSrTestCase(VMUtilsTestBase):
    @mock.patch.object(vm_utils, "_scan_sr")
    @mock.patch.object(vm_utils, "safe_find_sr")
    def test_scan_default_sr(self, mock_safe_find_sr, mock_scan_sr):
        mock_safe_find_sr.return_value = "sr_ref"

        self.assertEqual("sr_ref", vm_utils.scan_default_sr("fake_session"))

        mock_scan_sr.assert_called_once_with("fake_session", "sr_ref")

    def test_scan_sr_works(self):
        session = mock.Mock()
        vm_utils._scan_sr(session, "sr_ref")
        session.call_xenapi.assert_called_once_with('SR.scan', "sr_ref")

    def test_scan_sr_unknown_error_fails_once(self):
        session = mock.Mock()
        session.call_xenapi.side_effect = test.TestingException
        self.assertRaises(test.TestingException,
                          vm_utils._scan_sr, session, "sr_ref")
        session.call_xenapi.assert_called_once_with('SR.scan', "sr_ref")

    @mock.patch.object(greenthread, 'sleep')
    def test_scan_sr_known_error_retries_then_throws(self, mock_sleep):
        session = mock.Mock()

        class FakeException(Exception):
            details = ['SR_BACKEND_FAILURE_40', "", "", ""]

        session.XenAPI.Failure = FakeException
        session.call_xenapi.side_effect = FakeException

        self.assertRaises(FakeException,
                          vm_utils._scan_sr, session, "sr_ref")

        session.call_xenapi.assert_called_with('SR.scan', "sr_ref")
        self.assertEqual(4, session.call_xenapi.call_count)
        mock_sleep.assert_has_calls([mock.call(2), mock.call(4), mock.call(8)])

    @mock.patch.object(greenthread, 'sleep')
    def test_scan_sr_known_error_retries_then_succeeds(self, mock_sleep):
        session = mock.Mock()

        class FakeException(Exception):
            details = ['SR_BACKEND_FAILURE_40', "", "", ""]

        session.XenAPI.Failure = FakeException

        def fake_call_xenapi(*args):
            fake_call_xenapi.count += 1
            if fake_call_xenapi.count != 2:
                raise FakeException()

        fake_call_xenapi.count = 0
        session.call_xenapi.side_effect = fake_call_xenapi

        vm_utils._scan_sr(session, "sr_ref")

        session.call_xenapi.assert_called_with('SR.scan', "sr_ref")
        self.assertEqual(2, session.call_xenapi.call_count)
        mock_sleep.assert_called_once_with(2)


@mock.patch.object(flavors, 'extract_flavor',
                   return_value={
                            'memory_mb': 1024,
                            'vcpus': 1,
                            'vcpu_weight': 1.0,
                        })
class CreateVmTestCase(VMUtilsTestBase):
    def test_vss_provider(self, mock_extract):
        self.flags(vcpu_pin_set="2,3")
        session = stubs.get_fake_session()
        instance = objects.Instance(uuid=uuids.nova_uuid,
                                    os_type="windows",
                                    system_metadata={})

        with mock.patch.object(instance, 'get_flavor') as get:
            get.return_value = objects.Flavor._from_db_object(
                None, objects.Flavor(), test_flavor.fake_flavor)
            vm_utils.create_vm(session, instance, "label",
                               "kernel", "ramdisk")

        vm_rec = {
            'VCPUs_params': {'cap': '0', 'mask': '2,3', 'weight': '1'},
            'PV_args': '',
            'memory_static_min': '0',
            'ha_restart_priority': '',
            'HVM_boot_policy': 'BIOS order',
            'PV_bootloader': '', 'tags': [],
            'VCPUs_max': '4',
            'memory_static_max': '1073741824',
            'actions_after_shutdown': 'destroy',
            'memory_dynamic_max': '1073741824',
            'user_version': '0',
            'xenstore_data': {'vm-data/allowvssprovider': 'false'},
            'blocked_operations': {},
            'is_a_template': False,
            'name_description': '',
            'memory_dynamic_min': '1073741824',
            'actions_after_crash': 'destroy',
            'memory_target': '1073741824',
            'PV_ramdisk': '',
            'PV_bootloader_args': '',
            'PCI_bus': '',
            'other_config': {'nova_uuid': uuids.nova_uuid},
            'name_label': 'label',
            'actions_after_reboot': 'restart',
            'VCPUs_at_startup': '4',
            'HVM_boot_params': {'order': 'dc'},
            'platform': {'nx': 'true', 'pae': 'true', 'apic': 'true',
                         'timeoffset': '0', 'viridian': 'true',
                         'acpi': 'true'},
            'PV_legacy_args': '',
            'PV_kernel': '',
            'affinity': '',
            'recommendations': '',
            'ha_always_run': False
        }
        session.call_xenapi.assert_called_once_with("VM.create", vm_rec)

    def test_invalid_cpu_mask_raises(self, mock_extract):
        self.flags(vcpu_pin_set="asdf")
        session = mock.Mock()
        instance = objects.Instance(uuid=str(uuid.uuid4()),
                                    system_metadata={})
        with mock.patch.object(instance, 'get_flavor') as get:
            get.return_value = objects.Flavor._from_db_object(
                None, objects.Flavor(), test_flavor.fake_flavor)
            self.assertRaises(exception.Invalid,
                              vm_utils.create_vm,
                              session, instance, "label",
                              "kernel", "ramdisk")

    def test_destroy_vm(self, mock_extract):
        session = mock.Mock()
        instance = objects.Instance(uuid=str(uuid.uuid4()))

        vm_utils.destroy_vm(session, instance, "vm_ref")

        session.VM.destroy.assert_called_once_with("vm_ref")

    def test_destroy_vm_silently_fails(self, mock_extract):
        session = mock.Mock()
        exc = test.TestingException()
        session.XenAPI.Failure = test.TestingException
        session.VM.destroy.side_effect = exc
        instance = objects.Instance(uuid=str(uuid.uuid4()))

        vm_utils.destroy_vm(session, instance, "vm_ref")

        session.VM.destroy.assert_called_once_with("vm_ref")


class DetermineVmModeTestCase(VMUtilsTestBase):
    def _fake_object(self, updates):
        return fake_instance.fake_instance_obj(None, **updates)

    def test_determine_vm_mode_returns_xen_mode(self):
        instance = self._fake_object({"vm_mode": "xen"})
        self.assertEqual(vm_mode.XEN,
            vm_utils.determine_vm_mode(instance, None))

    def test_determine_vm_mode_returns_hvm_mode(self):
        instance = self._fake_object({"vm_mode": "hvm"})
        self.assertEqual(vm_mode.HVM,
            vm_utils.determine_vm_mode(instance, None))

    def test_determine_vm_mode_returns_xen_for_linux(self):
        instance = self._fake_object({"vm_mode": None, "os_type": "linux"})
        self.assertEqual(vm_mode.XEN,
            vm_utils.determine_vm_mode(instance, None))

    def test_determine_vm_mode_returns_hvm_for_windows(self):
        instance = self._fake_object({"vm_mode": None, "os_type": "windows"})
        self.assertEqual(vm_mode.HVM,
            vm_utils.determine_vm_mode(instance, None))

    def test_determine_vm_mode_returns_hvm_by_default(self):
        instance = self._fake_object({"vm_mode": None, "os_type": None})
        self.assertEqual(vm_mode.HVM,
            vm_utils.determine_vm_mode(instance, None))

    def test_determine_vm_mode_returns_xen_for_VHD(self):
        instance = self._fake_object({"vm_mode": None, "os_type": None})
        self.assertEqual(vm_mode.XEN,
            vm_utils.determine_vm_mode(instance, vm_utils.ImageType.DISK_VHD))

    def test_determine_vm_mode_returns_xen_for_DISK(self):
        instance = self._fake_object({"vm_mode": None, "os_type": None})
        self.assertEqual(vm_mode.XEN,
            vm_utils.determine_vm_mode(instance, vm_utils.ImageType.DISK))


class CallXenAPIHelpersTestCase(VMUtilsTestBase):
    def test_vm_get_vbd_refs(self):
        session = mock.Mock()
        session.call_xenapi.return_value = "foo"
        self.assertEqual("foo", vm_utils._vm_get_vbd_refs(session, "vm_ref"))
        session.call_xenapi.assert_called_once_with("VM.get_VBDs", "vm_ref")

    def test_vbd_get_rec(self):
        session = mock.Mock()
        session.call_xenapi.return_value = "foo"
        self.assertEqual("foo", vm_utils._vbd_get_rec(session, "vbd_ref"))
        session.call_xenapi.assert_called_once_with("VBD.get_record",
                                                    "vbd_ref")

    def test_vdi_get_rec(self):
        session = mock.Mock()
        session.call_xenapi.return_value = "foo"
        self.assertEqual("foo", vm_utils._vdi_get_rec(session, "vdi_ref"))
        session.call_xenapi.assert_called_once_with("VDI.get_record",
                                                    "vdi_ref")

    def test_vdi_snapshot(self):
        session = mock.Mock()
        session.call_xenapi.return_value = "foo"
        self.assertEqual("foo", vm_utils._vdi_snapshot(session, "vdi_ref"))
        session.call_xenapi.assert_called_once_with("VDI.snapshot",
                                                    "vdi_ref", {})

    def test_vdi_get_virtual_size(self):
        session = mock.Mock()
        session.call_xenapi.return_value = "123"
        self.assertEqual(123, vm_utils._vdi_get_virtual_size(session, "ref"))
        session.call_xenapi.assert_called_once_with("VDI.get_virtual_size",
                                                    "ref")

    @mock.patch.object(vm_utils, '_get_resize_func_name')
    def test_vdi_resize(self, mock_get_resize_func_name):
        session = mock.Mock()
        mock_get_resize_func_name.return_value = "VDI.fake"
        vm_utils._vdi_resize(session, "ref", 123)
        session.call_xenapi.assert_called_once_with("VDI.fake", "ref", "123")

    @mock.patch.object(vm_utils, '_vdi_resize')
    @mock.patch.object(vm_utils, '_vdi_get_virtual_size')
    def test_update_vdi_virtual_size_works(self, mock_get_size, mock_resize):
        mock_get_size.return_value = (1024 ** 3) - 1
        instance = {"uuid": "a"}

        vm_utils.update_vdi_virtual_size("s", instance, "ref", 1)

        mock_get_size.assert_called_once_with("s", "ref")
        mock_resize.assert_called_once_with("s", "ref", 1024 ** 3)

    @mock.patch.object(vm_utils, '_vdi_resize')
    @mock.patch.object(vm_utils, '_vdi_get_virtual_size')
    def test_update_vdi_virtual_size_skips_resize_down(self, mock_get_size,
                                                       mock_resize):
        mock_get_size.return_value = 1024 ** 3
        instance = {"uuid": "a"}

        vm_utils.update_vdi_virtual_size("s", instance, "ref", 1)

        mock_get_size.assert_called_once_with("s", "ref")
        self.assertFalse(mock_resize.called)

    @mock.patch.object(vm_utils, '_vdi_resize')
    @mock.patch.object(vm_utils, '_vdi_get_virtual_size')
    def test_update_vdi_virtual_size_raise_if_disk_big(self, mock_get_size,
                                                       mock_resize):
        mock_get_size.return_value = 1024 ** 3 + 1
        instance = {"uuid": "a"}

        self.assertRaises(exception.ResizeError,
                          vm_utils.update_vdi_virtual_size,
                          "s", instance, "ref", 1)

        mock_get_size.assert_called_once_with("s", "ref")
        self.assertFalse(mock_resize.called)


@mock.patch.object(vm_utils, '_vdi_get_rec')
@mock.patch.object(vm_utils, '_vbd_get_rec')
@mock.patch.object(vm_utils, '_vm_get_vbd_refs')
class GetVdiForVMTestCase(VMUtilsTestBase):
    def test_get_vdi_for_vm_safely(self, vm_get_vbd_refs,
                                   vbd_get_rec, vdi_get_rec):
        session = "session"

        vm_get_vbd_refs.return_value = ["a", "b"]
        vbd_get_rec.return_value = {'userdevice': '0', 'VDI': 'vdi_ref'}
        vdi_get_rec.return_value = {}

        result = vm_utils.get_vdi_for_vm_safely(session, "vm_ref")
        self.assertEqual(('vdi_ref', {}), result)

        vm_get_vbd_refs.assert_called_once_with(session, "vm_ref")
        vbd_get_rec.assert_called_once_with(session, "a")
        vdi_get_rec.assert_called_once_with(session, "vdi_ref")

    def test_get_vdi_for_vm_safely_fails(self, vm_get_vbd_refs,
                                         vbd_get_rec, vdi_get_rec):
        session = "session"

        vm_get_vbd_refs.return_value = ["a", "b"]
        vbd_get_rec.return_value = {'userdevice': '0', 'VDI': 'vdi_ref'}

        self.assertRaises(exception.NovaException,
                          vm_utils.get_vdi_for_vm_safely,
                          session, "vm_ref", userdevice='1')

        self.assertEqual([], vdi_get_rec.call_args_list)
        self.assertEqual(2, len(vbd_get_rec.call_args_list))


@mock.patch.object(vm_utils, '_vdi_get_uuid')
@mock.patch.object(vm_utils, '_vbd_get_rec')
@mock.patch.object(vm_utils, '_vm_get_vbd_refs')
class GetAllVdiForVMTestCase(VMUtilsTestBase):
    def _setup_get_all_vdi_uuids_for_vm(self, vm_get_vbd_refs,
                                       vbd_get_rec, vdi_get_uuid):
        def fake_vbd_get_rec(session, vbd_ref):
            return {'userdevice': vbd_ref, 'VDI': "vdi_ref_%s" % vbd_ref}

        def fake_vdi_get_uuid(session, vdi_ref):
            return vdi_ref

        vm_get_vbd_refs.return_value = ["0", "2"]
        vbd_get_rec.side_effect = fake_vbd_get_rec
        vdi_get_uuid.side_effect = fake_vdi_get_uuid

    def test_get_all_vdi_uuids_for_vm_works(self, vm_get_vbd_refs,
                                            vbd_get_rec, vdi_get_uuid):
        self._setup_get_all_vdi_uuids_for_vm(vm_get_vbd_refs,
                vbd_get_rec, vdi_get_uuid)

        result = vm_utils.get_all_vdi_uuids_for_vm('session', "vm_ref")
        expected = ['vdi_ref_0', 'vdi_ref_2']
        self.assertEqual(expected, list(result))

    def test_get_all_vdi_uuids_for_vm_finds_none(self, vm_get_vbd_refs,
                                                 vbd_get_rec, vdi_get_uuid):
        self._setup_get_all_vdi_uuids_for_vm(vm_get_vbd_refs,
                vbd_get_rec, vdi_get_uuid)

        result = vm_utils.get_all_vdi_uuids_for_vm('session', "vm_ref",
                                                   min_userdevice=1)
        expected = ["vdi_ref_2"]
        self.assertEqual(expected, list(result))


class GetAllVdisTestCase(VMUtilsTestBase):
    def test_get_all_vdis_in_sr(self):

        def fake_get_rec(record_type, ref):
            if ref == "2":
                return "vdi_rec_2"

        session = mock.Mock()
        session.call_xenapi.return_value = ["1", "2"]
        session.get_rec.side_effect = fake_get_rec

        sr_ref = "sr_ref"
        actual = list(vm_utils._get_all_vdis_in_sr(session, sr_ref))
        self.assertEqual(actual, [('2', 'vdi_rec_2')])

        session.call_xenapi.assert_called_once_with("SR.get_VDIs", sr_ref)


class SnapshotAttachedHereTestCase(VMUtilsTestBase):
    @mock.patch.object(vm_utils, '_snapshot_attached_here_impl')
    def test_snapshot_attached_here(self, mock_impl):
        def fake_impl(session, instance, vm_ref, label, userdevice,
                      post_snapshot_callback):
            self.assertEqual("session", session)
            self.assertEqual("instance", instance)
            self.assertEqual("vm_ref", vm_ref)
            self.assertEqual("label", label)
            self.assertEqual('0', userdevice)
            self.assertIsNone(post_snapshot_callback)
            yield "fake"

        mock_impl.side_effect = fake_impl

        with vm_utils.snapshot_attached_here("session", "instance", "vm_ref",
                                             "label") as result:
            self.assertEqual("fake", result)

        mock_impl.assert_called_once_with("session", "instance", "vm_ref",
                                          "label", '0', None)

    @mock.patch.object(vm_utils, '_delete_snapshots_in_vdi_chain')
    @mock.patch.object(vm_utils, 'safe_destroy_vdis')
    @mock.patch.object(vm_utils, '_walk_vdi_chain')
    @mock.patch.object(vm_utils, '_wait_for_vhd_coalesce')
    @mock.patch.object(vm_utils, '_vdi_get_uuid')
    @mock.patch.object(vm_utils, '_vdi_snapshot')
    @mock.patch.object(vm_utils, 'get_vdi_for_vm_safely')
    def test_snapshot_attached_here_impl(self, mock_get_vdi_for_vm_safely,
            mock_vdi_snapshot, mock_vdi_get_uuid,
            mock_wait_for_vhd_coalesce, mock_walk_vdi_chain,
            mock_safe_destroy_vdis, mock_delete_snapshots_in_vdi_chain):
        session = "session"
        instance = {"uuid": "uuid"}
        mock_callback = mock.Mock()

        mock_get_vdi_for_vm_safely.return_value = ("vdi_ref",
                                                   {"SR": "sr_ref",
                                                    "uuid": "vdi_uuid"})
        mock_vdi_snapshot.return_value = "snap_ref"
        mock_vdi_get_uuid.return_value = "snap_uuid"
        mock_walk_vdi_chain.return_value = [{"uuid": "a"}, {"uuid": "b"}]

        try:
            with vm_utils.snapshot_attached_here(session, instance, "vm_ref",
                    "label", '2', mock_callback) as result:
                self.assertEqual(["a", "b"], result)
                raise test.TestingException()
            self.assertTrue(False)
        except test.TestingException:
            pass

        mock_get_vdi_for_vm_safely.assert_called_once_with(session, "vm_ref",
                                                           '2')
        mock_vdi_snapshot.assert_called_once_with(session, "vdi_ref")
        mock_wait_for_vhd_coalesce.assert_called_once_with(session, instance,
                "sr_ref", "vdi_ref", ['a', 'b'])
        mock_vdi_get_uuid.assert_called_once_with(session, "snap_ref")
        mock_walk_vdi_chain.assert_has_calls([mock.call(session, "vdi_uuid"),
                                              mock.call(session, "snap_uuid")])
        mock_callback.assert_called_once_with(
                task_state="image_pending_upload")
        mock_safe_destroy_vdis.assert_called_once_with(session, ["snap_ref"])
        mock_delete_snapshots_in_vdi_chain.assert_called_once_with(session,
                instance, ['a', 'b'], "sr_ref")

    @mock.patch.object(greenthread, 'sleep')
    def test_wait_for_vhd_coalesce_leaf_node(self, mock_sleep):
        instance = {"uuid": "fake"}
        vm_utils._wait_for_vhd_coalesce("session", instance,
                "sr_ref", "vdi_ref", ["uuid"])
        self.assertFalse(mock_sleep.called)

    @mock.patch.object(vm_utils, '_count_children')
    @mock.patch.object(greenthread, 'sleep')
    def test_wait_for_vhd_coalesce_parent_snapshot(self, mock_sleep,
                                                   mock_count):
        mock_count.return_value = 2
        instance = {"uuid": "fake"}

        vm_utils._wait_for_vhd_coalesce("session", instance,
                "sr_ref", "vdi_ref", ["uuid1", "uuid2"])

        self.assertFalse(mock_sleep.called)
        self.assertTrue(mock_count.called)

    @mock.patch.object(greenthread, 'sleep')
    @mock.patch.object(vm_utils, '_get_vhd_parent_uuid')
    @mock.patch.object(vm_utils, '_count_children')
    @mock.patch.object(vm_utils, '_scan_sr')
    def test_wait_for_vhd_coalesce_raises(self, mock_scan_sr,
            mock_count, mock_get_vhd_parent_uuid, mock_sleep):
        mock_count.return_value = 1
        instance = {"uuid": "fake"}

        self.assertRaises(exception.NovaException,
                vm_utils._wait_for_vhd_coalesce, "session", instance,
                "sr_ref", "vdi_ref", ["uuid1", "uuid2"])

        self.assertTrue(mock_count.called)
        self.assertEqual(20, mock_sleep.call_count)
        self.assertEqual(20, mock_scan_sr.call_count)

    @mock.patch.object(greenthread, 'sleep')
    @mock.patch.object(vm_utils, '_get_vhd_parent_uuid')
    @mock.patch.object(vm_utils, '_count_children')
    @mock.patch.object(vm_utils, '_scan_sr')
    def test_wait_for_vhd_coalesce_success(self, mock_scan_sr,
            mock_count, mock_get_vhd_parent_uuid, mock_sleep):
        mock_count.return_value = 1
        instance = {"uuid": "fake"}
        mock_get_vhd_parent_uuid.side_effect = ["bad", "uuid2"]

        vm_utils._wait_for_vhd_coalesce("session", instance,
                "sr_ref", "vdi_ref", ["uuid1", "uuid2"])

        self.assertEqual(1, mock_sleep.call_count)
        self.assertEqual(2, mock_scan_sr.call_count)

    @mock.patch.object(vm_utils, '_get_all_vdis_in_sr')
    def test_count_children(self, mock_get_all_vdis_in_sr):
        vdis = [('child1', {'sm_config': {'vhd-parent': 'parent1'}}),
                ('child2', {'sm_config': {'vhd-parent': 'parent2'}}),
                ('child3', {'sm_config': {'vhd-parent': 'parent1'}})]
        mock_get_all_vdis_in_sr.return_value = vdis
        self.assertEqual(2, vm_utils._count_children('session',
                                                     'parent1', 'sr'))


class ImportMigratedDisksTestCase(VMUtilsTestBase):
    @mock.patch.object(vm_utils, '_import_migrate_ephemeral_disks')
    @mock.patch.object(vm_utils, '_import_migrated_root_disk')
    def test_import_all_migrated_disks(self, mock_root, mock_ephemeral):
        session = "session"
        instance = "instance"
        mock_root.return_value = "root_vdi"
        mock_ephemeral.return_value = ["a", "b"]

        result = vm_utils.import_all_migrated_disks(session, instance)

        expected = {'root': 'root_vdi', 'ephemerals': ["a", "b"]}
        self.assertEqual(expected, result)
        mock_root.assert_called_once_with(session, instance)
        mock_ephemeral.assert_called_once_with(session, instance)

    @mock.patch.object(vm_utils, '_import_migrate_ephemeral_disks')
    @mock.patch.object(vm_utils, '_import_migrated_root_disk')
    def test_import_all_migrated_disks_import_root_false(self, mock_root,
            mock_ephemeral):
        session = "session"
        instance = "instance"
        mock_root.return_value = "root_vdi"
        mock_ephemeral.return_value = ["a", "b"]

        result = vm_utils.import_all_migrated_disks(session, instance,
                import_root=False)

        expected = {'root': None, 'ephemerals': ["a", "b"]}
        self.assertEqual(expected, result)
        self.assertEqual(0, mock_root.call_count)
        mock_ephemeral.assert_called_once_with(session, instance)

    @mock.patch.object(vm_utils, '_import_migrated_vhds')
    def test_import_migrated_root_disk(self, mock_migrate):
        mock_migrate.return_value = "foo"
        instance = {"uuid": "uuid", "name": "name"}

        result = vm_utils._import_migrated_root_disk("s", instance)

        self.assertEqual("foo", result)
        mock_migrate.assert_called_once_with("s", instance, "uuid", "root",
                                             "name")

    @mock.patch.object(vm_utils, '_import_migrated_vhds')
    def test_import_migrate_ephemeral_disks(self, mock_migrate):
        mock_migrate.return_value = "foo"
        instance = objects.Instance(id=1, uuid=uuidutils.generate_uuid())
        instance.old_flavor = objects.Flavor(ephemeral_gb=4000)

        result = vm_utils._import_migrate_ephemeral_disks("s", instance)

        self.assertEqual({'4': 'foo', '5': 'foo'}, result)
        inst_uuid = instance.uuid
        inst_name = instance.name
        expected_calls = [mock.call("s", instance,
                                    "%s_ephemeral_1" % inst_uuid,
                                    "ephemeral",
                                    "%s ephemeral (1)" % inst_name),
                          mock.call("s", instance,
                                    "%s_ephemeral_2" % inst_uuid,
                                    "ephemeral",
                                    "%s ephemeral (2)" % inst_name)]
        self.assertEqual(expected_calls, mock_migrate.call_args_list)

    @mock.patch.object(vm_utils, 'get_ephemeral_disk_sizes')
    def test_import_migrate_ephemeral_disks_use_old_flavor(self,
            mock_get_sizes):
        mock_get_sizes.return_value = []
        instance = objects.Instance(id=1, uuid=uuidutils.generate_uuid(),
                ephemeral_gb=2000)
        instance.old_flavor = objects.Flavor(ephemeral_gb=4000)

        vm_utils._import_migrate_ephemeral_disks("s", instance)
        mock_get_sizes.assert_called_once_with(4000)

    @mock.patch.object(vm_utils, '_set_vdi_info')
    @mock.patch.object(vm_utils, 'scan_default_sr')
    @mock.patch.object(vm_utils, 'get_sr_path')
    def test_import_migrated_vhds(self, mock_get_sr_path, mock_scan_sr,
                                  mock_set_info):
        session = mock.Mock()
        instance = {"uuid": "uuid"}
        session.call_plugin_serialized.return_value = {"root": {"uuid": "a"}}
        session.call_xenapi.return_value = "vdi_ref"
        mock_get_sr_path.return_value = "sr_path"

        result = vm_utils._import_migrated_vhds(session, instance,
                'chain_label', 'disk_type', 'vdi_label')

        expected = {'uuid': "a", 'ref': "vdi_ref"}
        self.assertEqual(expected, result)
        mock_get_sr_path.assert_called_once_with(session)
        session.call_plugin_serialized.assert_called_once_with('migration',
                'move_vhds_into_sr', instance_uuid='chain_label',
                sr_path='sr_path', uuid_stack=mock.ANY)
        mock_scan_sr.assert_called_once_with(session)
        session.call_xenapi.assert_called_once_with('VDI.get_by_uuid', 'a')
        mock_set_info.assert_called_once_with(session, 'vdi_ref', 'disk_type',
                'vdi_label', 'disk_type', instance)

    def test_get_vhd_parent_uuid_rec_provided(self):
        session = mock.Mock()
        vdi_ref = 'vdi_ref'
        vdi_rec = {'sm_config': {}}
        self.assertIsNone(vm_utils._get_vhd_parent_uuid(session,
                                                             vdi_ref,
                                                             vdi_rec))
        self.assertFalse(session.call_xenapi.called)


class MigrateVHDTestCase(VMUtilsTestBase):
    def _assert_transfer_called(self, session, label):
        session.call_plugin_serialized.assert_called_once_with(
                'migration', 'transfer_vhd', instance_uuid=label, host="dest",
                vdi_uuid="vdi_uuid", sr_path="sr_path", seq_num=2)

    def test_migrate_vhd_root(self):
        session = mock.Mock()
        instance = {"uuid": "a"}

        vm_utils.migrate_vhd(session, instance, "vdi_uuid", "dest",
                             "sr_path", 2)

        self._assert_transfer_called(session, "a")

    def test_migrate_vhd_ephemeral(self):
        session = mock.Mock()
        instance = {"uuid": "a"}

        vm_utils.migrate_vhd(session, instance, "vdi_uuid", "dest",
                             "sr_path", 2, 2)

        self._assert_transfer_called(session, "a_ephemeral_2")

    def test_migrate_vhd_converts_exceptions(self):
        session = mock.Mock()
        session.XenAPI.Failure = test.TestingException
        session.call_plugin_serialized.side_effect = test.TestingException()
        instance = {"uuid": "a"}

        self.assertRaises(exception.MigrationError, vm_utils.migrate_vhd,
                          session, instance, "vdi_uuid", "dest", "sr_path", 2)
        self._assert_transfer_called(session, "a")


class StripBaseMirrorTestCase(VMUtilsTestBase):
    def test_strip_base_mirror_from_vdi_works(self):
        session = mock.Mock()
        vm_utils._try_strip_base_mirror_from_vdi(session, "vdi_ref")
        session.call_xenapi.assert_called_once_with(
                "VDI.remove_from_sm_config", "vdi_ref", "base_mirror")

    def test_strip_base_mirror_from_vdi_hides_error(self):
        session = mock.Mock()
        session.XenAPI.Failure = test.TestingException
        session.call_xenapi.side_effect = test.TestingException()

        vm_utils._try_strip_base_mirror_from_vdi(session, "vdi_ref")

        session.call_xenapi.assert_called_once_with(
                "VDI.remove_from_sm_config", "vdi_ref", "base_mirror")

    @mock.patch.object(vm_utils, '_try_strip_base_mirror_from_vdi')
    def test_strip_base_mirror_from_vdis(self, mock_strip):
        def call_xenapi(method, arg):
            if method == "VM.get_VBDs":
                return ['VBD_ref_1', 'VBD_ref_2']
            if method == "VBD.get_VDI":
                return 'VDI' + arg[3:]
            return "Unexpected call_xenapi: %s.%s" % (method, arg)

        session = mock.Mock()
        session.call_xenapi.side_effect = call_xenapi

        vm_utils.strip_base_mirror_from_vdis(session, "vm_ref")

        expected = [mock.call('VM.get_VBDs', "vm_ref"),
                    mock.call('VBD.get_VDI', "VBD_ref_1"),
                    mock.call('VBD.get_VDI', "VBD_ref_2")]
        self.assertEqual(expected, session.call_xenapi.call_args_list)

        expected = [mock.call(session, "VDI_ref_1"),
                    mock.call(session, "VDI_ref_2")]
        self.assertEqual(expected, mock_strip.call_args_list)


class DeviceIdTestCase(VMUtilsTestBase):
    def test_device_id_is_none_if_not_specified_in_meta_data(self):
        image_meta = objects.ImageMeta.from_dict({})
        session = mock.Mock()
        session.product_version = (6, 1, 0)
        self.assertIsNone(vm_utils.get_vm_device_id(session, image_meta))

    def test_get_device_id_if_hypervisor_version_is_greater_than_6_1(self):
        image_meta = objects.ImageMeta.from_dict(
            {'properties': {'xenapi_device_id': '0002'}})
        session = mock.Mock()
        session.product_version = (6, 2, 0)
        self.assertEqual(2,
                         vm_utils.get_vm_device_id(session, image_meta))
        session.product_version = (6, 3, 1)
        self.assertEqual(2,
                         vm_utils.get_vm_device_id(session, image_meta))

    def test_raise_exception_if_device_id_not_supported_by_hyp_version(self):
        image_meta = objects.ImageMeta.from_dict(
            {'properties': {'xenapi_device_id': '0002'}})
        session = mock.Mock()
        session.product_version = (6, 0)
        exc = self.assertRaises(exception.NovaException,
                                vm_utils.get_vm_device_id, session, image_meta)
        self.assertEqual("Device id 2 specified is not supported by "
                         "hypervisor version (6, 0)", exc.message)
        session.product_version = ('6a')
        exc = self.assertRaises(exception.NovaException,
                                vm_utils.get_vm_device_id, session, image_meta)
        self.assertEqual("Device id 2 specified is not supported by "
                         "hypervisor version 6a", exc.message)


class CreateVmRecordTestCase(VMUtilsTestBase):
    @mock.patch.object(flavors, 'extract_flavor')
    def test_create_vm_record_linux(self, mock_extract_flavor):
        instance = objects.Instance(uuid=uuids.nova_uuid,
                                    os_type="linux")
        self._test_create_vm_record(mock_extract_flavor, instance, False)

    @mock.patch.object(flavors, 'extract_flavor')
    def test_create_vm_record_windows(self, mock_extract_flavor):
        instance = objects.Instance(uuid=uuids.nova_uuid,
                                    os_type="windows")
        with mock.patch.object(instance, 'get_flavor') as get:
            get.return_value = objects.Flavor._from_db_object(
                None, objects.Flavor(), test_flavor.fake_flavor)
            self._test_create_vm_record(mock_extract_flavor, instance, True)

    def _test_create_vm_record(self, mock_extract_flavor, instance,
                               is_viridian):
        session = stubs.get_fake_session()
        flavor = {"memory_mb": 1024, "vcpus": 1, "vcpu_weight": 2}
        mock_extract_flavor.return_value = flavor

        with mock.patch.object(instance, 'get_flavor') as get:
            get.return_value = objects.Flavor(memory_mb=1024,
                                              vcpus=1,
                                              vcpu_weight=2)
            vm_utils.create_vm(session, instance, "name", "kernel", "ramdisk",
                               device_id=2)

        is_viridian_str = str(is_viridian).lower()

        expected_vm_rec = {
            'VCPUs_params': {'cap': '0', 'weight': '2'},
            'PV_args': '',
            'memory_static_min': '0',
            'ha_restart_priority': '',
            'HVM_boot_policy': 'BIOS order',
            'PV_bootloader': '',
            'tags': [],
            'VCPUs_max': '1',
            'memory_static_max': '1073741824',
            'actions_after_shutdown': 'destroy',
            'memory_dynamic_max': '1073741824',
            'user_version': '0',
            'xenstore_data': {'vm-data/allowvssprovider': 'false'},
            'blocked_operations': {},
            'is_a_template': False,
            'name_description': '',
            'memory_dynamic_min': '1073741824',
            'actions_after_crash': 'destroy',
            'memory_target': '1073741824',
            'PV_ramdisk': '',
            'PV_bootloader_args': '',
            'PCI_bus': '',
            'other_config': {'nova_uuid': uuids.nova_uuid},
            'name_label': 'name',
            'actions_after_reboot': 'restart',
            'VCPUs_at_startup': '1',
            'HVM_boot_params': {'order': 'dc'},
            'platform': {'nx': 'true', 'pae': 'true', 'apic': 'true',
                         'timeoffset': '0', 'viridian': is_viridian_str,
                         'acpi': 'true', 'device_id': '0002'},
            'PV_legacy_args': '',
            'PV_kernel': '',
            'affinity': '',
            'recommendations': '',
            'ha_always_run': False}

        session.call_xenapi.assert_called_with('VM.create', expected_vm_rec)

    def test_list_vms(self):
        self.fixture = self.useFixture(config_fixture.Config(lockutils.CONF))
        self.fixture.config(disable_process_locking=True,
                            group='oslo_concurrency')
        self.flags(instance_name_template='%d',
                   firewall_driver='nova.virt.xenapi.firewall.'
                                   'Dom0IptablesFirewallDriver')
        self.flags(connection_url='test_url',
                   connection_password='test_pass',
                   group='xenserver')

        fake.create_vm("foo1", "Halted")
        vm_ref = fake.create_vm("foo2", "Running")

        stubs.stubout_session(self.stubs, fake.SessionBase)
        driver = xenapi_conn.XenAPIDriver(False)

        result = list(vm_utils.list_vms(driver._session))

        # Will have 3 VMs - but one is Dom0 and one is not running on the host
        self.assertEqual(len(driver._session.call_xenapi('VM.get_all')), 3)
        self.assertEqual(len(result), 1)

        result_keys = [key for (key, value) in result]

        self.assertIn(vm_ref, result_keys)


class ChildVHDsTestCase(test.NoDBTestCase):
    all_vdis = [
        ("my-vdi-ref",
         {"uuid": "my-uuid", "sm_config": {},
          "is_a_snapshot": False, "other_config": {}}),
        ("non-parent",
         {"uuid": "uuid-1", "sm_config": {},
          "is_a_snapshot": False, "other_config": {}}),
        ("diff-parent",
         {"uuid": "uuid-1", "sm_config": {"vhd-parent": "other-uuid"},
          "is_a_snapshot": False, "other_config": {}}),
        ("child",
          {"uuid": "uuid-child", "sm_config": {"vhd-parent": "my-uuid"},
           "is_a_snapshot": False, "other_config": {}}),
        ("child-snap",
         {"uuid": "uuid-child-snap", "sm_config": {"vhd-parent": "my-uuid"},
          "is_a_snapshot": True, "other_config": {}}),
    ]

    @mock.patch.object(vm_utils, '_get_all_vdis_in_sr')
    def test_child_vhds_defaults(self, mock_get_all):
        mock_get_all.return_value = self.all_vdis

        result = vm_utils._child_vhds("session", "sr_ref", ["my-uuid"])

        self.assertJsonEqual(['uuid-child', 'uuid-child-snap'], result)

    @mock.patch.object(vm_utils, '_get_all_vdis_in_sr')
    def test_child_vhds_only_snapshots(self, mock_get_all):
        mock_get_all.return_value = self.all_vdis

        result = vm_utils._child_vhds("session", "sr_ref", ["my-uuid"],
                                      old_snapshots_only=True)

        self.assertEqual(['uuid-child-snap'], result)

    @mock.patch.object(vm_utils, '_get_all_vdis_in_sr')
    def test_child_vhds_chain(self, mock_get_all):
        mock_get_all.return_value = self.all_vdis

        result = vm_utils._child_vhds("session", "sr_ref",
                ["my-uuid", "other-uuid"], old_snapshots_only=True)

        self.assertEqual(['uuid-child-snap'], result)

    def test_is_vdi_a_snapshot_works(self):
        vdi_rec = {"is_a_snapshot": True,
                    "other_config": {}}

        self.assertTrue(vm_utils._is_vdi_a_snapshot(vdi_rec))

    def test_is_vdi_a_snapshot_base_images_false(self):
        vdi_rec = {"is_a_snapshot": True,
                    "other_config": {"image-id": "fake"}}

        self.assertFalse(vm_utils._is_vdi_a_snapshot(vdi_rec))

    def test_is_vdi_a_snapshot_false_for_non_snapshot(self):
        vdi_rec = {"is_a_snapshot": False,
                    "other_config": {}}

        self.assertFalse(vm_utils._is_vdi_a_snapshot(vdi_rec))


class RemoveOldSnapshotsTestCase(test.NoDBTestCase):

    @mock.patch.object(vm_utils, 'get_vdi_for_vm_safely')
    @mock.patch.object(vm_utils, '_walk_vdi_chain')
    @mock.patch.object(vm_utils, '_delete_snapshots_in_vdi_chain')
    def test_remove_old_snapshots(self, mock_delete, mock_walk, mock_get):
        instance = {"uuid": "fake"}
        mock_get.return_value = ("ref", {"uuid": "vdi", "SR": "sr_ref"})
        mock_walk.return_value = [{"uuid": "uuid1"}, {"uuid": "uuid2"}]

        vm_utils.remove_old_snapshots("session", instance, "vm_ref")

        mock_delete.assert_called_once_with("session", instance,
                ["uuid1", "uuid2"], "sr_ref")
        mock_get.assert_called_once_with("session", "vm_ref")
        mock_walk.assert_called_once_with("session", "vdi")

    @mock.patch.object(vm_utils, '_child_vhds')
    def test_delete_snapshots_in_vdi_chain_no_chain(self, mock_child):
        instance = {"uuid": "fake"}

        vm_utils._delete_snapshots_in_vdi_chain("session", instance,
                ["uuid"], "sr")

        self.assertFalse(mock_child.called)

    @mock.patch.object(vm_utils, '_child_vhds')
    def test_delete_snapshots_in_vdi_chain_no_snapshots(self, mock_child):
        instance = {"uuid": "fake"}
        mock_child.return_value = []

        vm_utils._delete_snapshots_in_vdi_chain("session", instance,
                ["uuid1", "uuid2"], "sr")

        mock_child.assert_called_once_with("session", "sr", ["uuid2"],
                old_snapshots_only=True)

    @mock.patch.object(vm_utils, '_scan_sr')
    @mock.patch.object(vm_utils, 'safe_destroy_vdis')
    @mock.patch.object(vm_utils, '_child_vhds')
    def test_delete_snapshots_in_vdi_chain_calls_destroy(self, mock_child,
                mock_destroy, mock_scan):
        instance = {"uuid": "fake"}
        mock_child.return_value = ["suuid1", "suuid2"]
        session = mock.Mock()
        session.VDI.get_by_uuid.side_effect = ["ref1", "ref2"]

        vm_utils._delete_snapshots_in_vdi_chain(session, instance,
                ["uuid1", "uuid2"], "sr")

        mock_child.assert_called_once_with(session, "sr", ["uuid2"],
                old_snapshots_only=True)
        session.VDI.get_by_uuid.assert_has_calls([
                mock.call("suuid1"), mock.call("suuid2")])
        mock_destroy.assert_called_once_with(session, ["ref1", "ref2"])
        mock_scan.assert_called_once_with(session, "sr")


class ResizeFunctionTestCase(test.NoDBTestCase):
    def _call_get_resize_func_name(self, brand, version):
        session = mock.Mock()
        session.product_brand = brand
        session.product_version = version

        return vm_utils._get_resize_func_name(session)

    def _test_is_resize(self, brand, version):
        result = self._call_get_resize_func_name(brand, version)
        self.assertEqual("VDI.resize", result)

    def _test_is_resize_online(self, brand, version):
        result = self._call_get_resize_func_name(brand, version)
        self.assertEqual("VDI.resize_online", result)

    def test_xenserver_5_5(self):
        self._test_is_resize_online("XenServer", (5, 5, 0))

    def test_xenserver_6_0(self):
        self._test_is_resize("XenServer", (6, 0, 0))

    def test_xcp_1_1(self):
        self._test_is_resize_online("XCP", (1, 1, 0))

    def test_xcp_1_2(self):
        self._test_is_resize("XCP", (1, 2, 0))

    def test_xcp_2_0(self):
        self._test_is_resize("XCP", (2, 0, 0))

    def test_random_brand(self):
        self._test_is_resize("asfd", (1, 1, 0))

    def test_default(self):
        self._test_is_resize(None, None)

    def test_empty(self):
        self._test_is_resize("", "")

    def test_bad_version(self):
        self._test_is_resize("XenServer", "asdf")


class VMInfoTests(VMUtilsTestBase):
    def setUp(self):
        super(VMInfoTests, self).setUp()
        self.session = mock.Mock()

    def test_get_power_state_valid(self):
        # Save on test setup calls by having these simple tests in one method
        self.session.call_xenapi.return_value = "Running"
        self.assertEqual(vm_utils.get_power_state(self.session, "ref"),
                         power_state.RUNNING)

        self.session.call_xenapi.return_value = "Halted"
        self.assertEqual(vm_utils.get_power_state(self.session, "ref"),
                         power_state.SHUTDOWN)

        self.session.call_xenapi.return_value = "Paused"
        self.assertEqual(vm_utils.get_power_state(self.session, "ref"),
                         power_state.PAUSED)

        self.session.call_xenapi.return_value = "Suspended"
        self.assertEqual(vm_utils.get_power_state(self.session, "ref"),
                         power_state.SUSPENDED)

        self.session.call_xenapi.return_value = "Crashed"
        self.assertEqual(vm_utils.get_power_state(self.session, "ref"),
                         power_state.CRASHED)

    def test_get_power_state_invalid(self):
        self.session.call_xenapi.return_value = "Invalid"
        self.assertRaises(KeyError,
                          vm_utils.get_power_state, self.session, "ref")

    _XAPI_record = {'power_state': 'Running',
                    'memory_static_max': str(10 << 10),
                    'memory_dynamic_max': str(9 << 10),
                    'VCPUs_max': '5'}

    def test_compile_info(self):

        def call_xenapi(method, *args):
            if method.startswith('VM.get_') and args[0] == 'dummy':
                return self._XAPI_record[method[7:]]

        self.session.call_xenapi.side_effect = call_xenapi

        info = vm_utils.compile_info(self.session, "dummy")
        self.assertEqual(hardware.InstanceInfo(state=power_state.RUNNING,
                                               max_mem_kb=10, mem_kb=9,
                                               num_cpu='5', cpu_time_ns=0),
                         info)
