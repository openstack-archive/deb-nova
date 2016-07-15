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
import os

from nova import exception
from nova.tests.unit.virt.libvirt.volume import test_volume
from nova import utils
from nova.virt.libvirt.volume import vzstorage
from os_brick.initiator import connector


class LibvirtVZStorageTestCase(test_volume.LibvirtVolumeBaseTestCase):
    """Tests the libvirt vzstorage volume driver."""

    def setUp(self):
        super(LibvirtVZStorageTestCase, self).setUp()
        self.mnt_base = '/mnt'
        self.flags(vzstorage_mount_point_base=self.mnt_base, group='libvirt')
        self.flags(vzstorage_cache_path="/tmp/ssd-cache/%(cluster_name)s",
                   group='libvirt')

    def test_libvirt_vzstorage_driver(self):
        libvirt_driver = vzstorage.LibvirtVZStorageVolumeDriver(self.fake_conn)
        self.assertIsInstance(libvirt_driver.connector,
                              connector.RemoteFsConnector)

    def test_libvirt_vzstorage_driver_opts_negative(self):
        """Test that custom options cannot duplicate the configured"""
        bad_opts = [
                     ["-c", "clus111", "-v"],
                     ["-l", "/var/log/pstorage.log", "-L", "5x5"],
                     ["-u", "user1", "-p", "pass1"],
                     ["-v", "-R", "100", "-C", "/ssd"],
                   ]
        for opts in bad_opts:
            self.flags(vzstorage_mount_opts=opts, group='libvirt')
            self.assertRaises(exception.NovaException,
                              vzstorage.LibvirtVZStorageVolumeDriver,
                              self.fake_conn)

    def test_libvirt_vzstorage_driver_share_fmt_neg(self):

        drv = vzstorage.LibvirtVZStorageVolumeDriver(self.fake_conn)

        wrong_export_string = 'mds1, mds2:/testcluster:passwd12111'
        connection_info = {'data': {'export': wrong_export_string,
                                    'name': self.name}}

        err_pattern = ("^Valid share format is "
                    "\[mds\[,mds1\[\.\.\.\]\]:/\]clustername\[:password\]$")
        self.assertRaisesRegex(exception.InvalidVolume,
                                err_pattern,
                                drv.connect_volume,
                                connection_info,
                                self.disk_info)

    def test_libvirt_vzstorage_driver_connect(self):
        def brick_conn_vol(data):
            return {'path': 'vstorage://testcluster'}

        drv = vzstorage.LibvirtVZStorageVolumeDriver(self.fake_conn)
        drv.connector.connect_volume = brick_conn_vol

        export_string = 'testcluster'
        connection_info = {'data': {'export': export_string,
                                    'name': self.name}}

        drv.connect_volume(connection_info, self.disk_info)
        self.assertEqual('vstorage://testcluster',
                         connection_info['data']['device_path'])
        self.assertEqual('-u stack -g qemu -m 0770 '
                         '-l /var/log/pstorage/testcluster/nova.log.gz '
                         '-C /tmp/ssd-cache/testcluster',
                          connection_info['data']['options'])

    def test_libvirt_vzstorage_driver_disconnect(self):
        drv = vzstorage.LibvirtVZStorageVolumeDriver(self.fake_conn)
        drv.connector.disconnect_volume = mock.MagicMock()
        conn = {'data': self.disk_info}
        drv.disconnect_volume(conn, self.disk_info)
        drv.connector.disconnect_volume.assert_called_once_with(
            self.disk_info, None)

    def test_libvirt_vzstorage_driver_get_config(self):
        libvirt_driver = vzstorage.LibvirtVZStorageVolumeDriver(self.fake_conn)
        export_string = 'vzstorage'
        export_mnt_base = os.path.join(self.mnt_base,
                                       utils.get_hash_str(export_string))
        file_path = os.path.join(export_mnt_base, self.name)

        connection_info = {'data': {'export': export_string,
                                    'name': self.name,
                                    'device_path': file_path}}
        conf = libvirt_driver.get_config(connection_info, self.disk_info)
        self.assertEqual('file', conf.source_type)
        self.assertEqual(file_path, conf.source_path)
        self.assertEqual('raw', conf.driver_format)
        self.assertEqual('writethrough', conf.driver_cache)
