#    Copyright 2010 OpenStack Foundation
#    Copyright 2012 University Of Minho
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
from collections import OrderedDict
import contextlib
import copy
import datetime
import errno
import glob
import os
import random
import re
import shutil
import signal
import threading
import time
import uuid

import eventlet
from eventlet import greenthread
import fixtures
from lxml import etree
import mock
from mox3 import mox
from os_brick.initiator import connector
from oslo_concurrency import lockutils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import encodeutils
from oslo_utils import fileutils
from oslo_utils import fixture as utils_fixture
from oslo_utils import importutils
from oslo_utils import units
from oslo_utils import uuidutils
from oslo_utils import versionutils
import six
from six.moves import builtins
from six.moves import range

from nova.api.metadata import base as instance_metadata
from nova.compute import arch
from nova.compute import cpumodel
from nova.compute import manager
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
from nova.compute import vm_states
import nova.conf
from nova import context
from nova import db
from nova import exception
from nova.network import model as network_model
from nova import objects
from nova.objects import block_device as block_device_obj
from nova.objects import fields
from nova.objects import migrate_data as migrate_data_obj
from nova.objects import virtual_interface as obj_vif
from nova.pci import manager as pci_manager
from nova.pci import utils as pci_utils
from nova import test
from nova.tests.unit import fake_block_device
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_network
import nova.tests.unit.image.fake
from nova.tests.unit import matchers
from nova.tests.unit.objects import test_pci_device
from nova.tests.unit.objects import test_vcpu_model
from nova.tests.unit.virt.libvirt import fake_imagebackend
from nova.tests.unit.virt.libvirt import fake_libvirt_utils
from nova.tests.unit.virt.libvirt import fakelibvirt
from nova.tests import uuidsentinel as uuids
from nova import utils
from nova import version
from nova.virt import block_device as driver_block_device
from nova.virt.disk import api as disk_api
from nova.virt import driver
from nova.virt import fake
from nova.virt import firewall as base_firewall
from nova.virt import hardware
from nova.virt.image import model as imgmodel
from nova.virt import images
from nova.virt.libvirt import blockinfo
from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import driver as libvirt_driver
from nova.virt.libvirt import firewall
from nova.virt.libvirt import guest as libvirt_guest
from nova.virt.libvirt import host
from nova.virt.libvirt import imagebackend
from nova.virt.libvirt import imagecache
from nova.virt.libvirt import migration as libvirt_migrate
from nova.virt.libvirt.storage import dmcrypt
from nova.virt.libvirt.storage import lvm
from nova.virt.libvirt.storage import rbd_utils
from nova.virt.libvirt import utils as libvirt_utils
from nova.virt.libvirt.volume import volume as volume_drivers

libvirt_driver.libvirt = fakelibvirt
host.libvirt = fakelibvirt
libvirt_guest.libvirt = fakelibvirt
libvirt_migrate.libvirt = fakelibvirt


CONF = nova.conf.CONF

_fake_network_info = fake_network.fake_get_instance_nw_info

_fake_NodeDevXml = \
    {"pci_0000_04_00_3": """
        <device>
        <name>pci_0000_04_00_3</name>
        <parent>pci_0000_00_01_1</parent>
        <driver>
            <name>igb</name>
        </driver>
        <capability type='pci'>
            <domain>0</domain>
            <bus>4</bus>
            <slot>0</slot>
            <function>3</function>
            <product id='0x1521'>I350 Gigabit Network Connection</product>
            <vendor id='0x8086'>Intel Corporation</vendor>
            <capability type='virt_functions'>
              <address domain='0x0000' bus='0x04' slot='0x10' function='0x3'/>
              <address domain='0x0000' bus='0x04' slot='0x10' function='0x7'/>
              <address domain='0x0000' bus='0x04' slot='0x11' function='0x3'/>
              <address domain='0x0000' bus='0x04' slot='0x11' function='0x7'/>
            </capability>
        </capability>
      </device>""",
    "pci_0000_04_10_7": """
      <device>
         <name>pci_0000_04_10_7</name>
         <parent>pci_0000_00_01_1</parent>
         <driver>
         <name>igbvf</name>
         </driver>
         <capability type='pci'>
          <domain>0</domain>
          <bus>4</bus>
          <slot>16</slot>
          <function>7</function>
          <product id='0x1520'>I350 Ethernet Controller Virtual Function
            </product>
          <vendor id='0x8086'>Intel Corporation</vendor>
          <capability type='phys_function'>
             <address domain='0x0000' bus='0x04' slot='0x00' function='0x3'/>
          </capability>
          <capability type='virt_functions'>
          </capability>
        </capability>
    </device>""",
    "pci_0000_04_11_7": """
      <device>
         <name>pci_0000_04_11_7</name>
         <parent>pci_0000_00_01_1</parent>
         <driver>
         <name>igbvf</name>
         </driver>
         <capability type='pci'>
          <domain>0</domain>
          <bus>4</bus>
          <slot>17</slot>
          <function>7</function>
          <product id='0x1520'>I350 Ethernet Controller Virtual Function
            </product>
          <vendor id='0x8086'>Intel Corporation</vendor>
          <numa node='0'/>
          <capability type='phys_function'>
             <address domain='0x0000' bus='0x04' slot='0x00' function='0x3'/>
          </capability>
          <capability type='virt_functions'>
          </capability>
        </capability>
    </device>""",
    "pci_0000_04_00_1": """
    <device>
      <name>pci_0000_04_00_1</name>
      <path>/sys/devices/pci0000:00/0000:00:02.0/0000:04:00.1</path>
      <parent>pci_0000_00_02_0</parent>
      <driver>
        <name>mlx5_core</name>
      </driver>
      <capability type='pci'>
        <domain>0</domain>
        <bus>4</bus>
        <slot>0</slot>
        <function>1</function>
        <product id='0x1013'>MT27700 Family [ConnectX-4]</product>
        <vendor id='0x15b3'>Mellanox Technologies</vendor>
        <iommuGroup number='15'>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/>
        </iommuGroup>
        <numa node='0'/>
        <pci-express>
          <link validity='cap' port='0' speed='8' width='16'/>
          <link validity='sta' speed='8' width='16'/>
        </pci-express>
      </capability>
    </device>""",
    # libvirt  >= 1.3.0 nodedev-dumpxml
    "pci_0000_03_00_0": """
    <device>
        <name>pci_0000_03_00_0</name>
        <path>/sys/devices/pci0000:00/0000:00:02.0/0000:03:00.0</path>
        <parent>pci_0000_00_02_0</parent>
        <driver>
        <name>mlx5_core</name>
        </driver>
        <capability type='pci'>
        <domain>0</domain>
        <bus>3</bus>
        <slot>0</slot>
        <function>0</function>
        <product id='0x1013'>MT27700 Family [ConnectX-4]</product>
        <vendor id='0x15b3'>Mellanox Technologies</vendor>
        <capability type='virt_functions' maxCount='16'>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x2'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x3'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x4'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x5'/>
        </capability>
        <iommuGroup number='15'>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/>
        </iommuGroup>
        <numa node='0'/>
        <pci-express>
          <link validity='cap' port='0' speed='8' width='16'/>
          <link validity='sta' speed='8' width='16'/>
        </pci-express>
      </capability>
    </device>""",
    "pci_0000_03_00_1": """
    <device>
      <name>pci_0000_03_00_1</name>
      <path>/sys/devices/pci0000:00/0000:00:02.0/0000:03:00.1</path>
      <parent>pci_0000_00_02_0</parent>
      <driver>
        <name>mlx5_core</name>
      </driver>
      <capability type='pci'>
        <domain>0</domain>
        <bus>3</bus>
        <slot>0</slot>
        <function>1</function>
        <product id='0x1013'>MT27700 Family [ConnectX-4]</product>
        <vendor id='0x15b3'>Mellanox Technologies</vendor>
        <capability type='virt_functions' maxCount='16'/>
        <iommuGroup number='15'>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
          <address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/>
        </iommuGroup>
        <numa node='0'/>
        <pci-express>
          <link validity='cap' port='0' speed='8' width='16'/>
          <link validity='sta' speed='8' width='16'/>
        </pci-express>
      </capability>
    </device>""",
    }

_fake_cpu_info = {
    "arch": "test_arch",
    "model": "test_model",
    "vendor": "test_vendor",
    "topology": {
        "sockets": 1,
        "cores": 8,
        "threads": 16
    },
    "features": ["feature1", "feature2"]
}

eph_default_ext = utils.get_hash_str(disk_api._DEFAULT_FILE_SYSTEM)[:7]


def eph_name(size):
    return ('ephemeral_%(size)s_%(ext)s' %
            {'size': size, 'ext': eph_default_ext})


def fake_disk_info_byname(instance, type='qcow2'):
    """Return instance_disk_info corresponding accurately to the properties of
    the given Instance object. The info is returned as an OrderedDict of
    name->disk_info for each disk.

    :param instance: The instance we're generating fake disk_info for.
    :param type: libvirt's disk type.
    :return: disk_info
    :rtype: OrderedDict
    """
    instance_dir = os.path.join(CONF.instances_path, instance.uuid)

    def instance_path(name):
        return os.path.join(instance_dir, name)

    disk_info = OrderedDict()

    # root disk
    if instance.image_ref is not None:
        cache_name = imagecache.get_cache_fname(instance.image_ref)
        disk_info['disk'] = {
            'type': type,
            'path': instance_path('disk'),
            'virt_disk_size': instance.flavor.root_gb * units.Gi,
            'backing_file': cache_name,
            'disk_size': instance.flavor.root_gb * units.Gi,
            'over_committed_disk_size': 0}

    swap_mb = instance.flavor.swap
    if swap_mb > 0:
        disk_info['disk.swap'] = {
            'type': type,
            'path': instance_path('disk.swap'),
            'virt_disk_size': swap_mb * units.Mi,
            'backing_file': 'swap_%s' % swap_mb,
            'disk_size': swap_mb * units.Mi,
            'over_committed_disk_size': 0}

    eph_gb = instance.flavor.ephemeral_gb
    if eph_gb > 0:
        disk_info['disk.local'] = {
            'type': type,
            'path': instance_path('disk.local'),
            'virt_disk_size': eph_gb * units.Gi,
            'backing_file': eph_name(eph_gb),
            'disk_size': eph_gb * units.Gi,
            'over_committed_disk_size': 0}

    if instance.config_drive:
        disk_info['disk.config'] = {
            'type': 'raw',
            'path': instance_path('disk.config'),
            'virt_disk_size': 1024,
            'backing_file': '',
            'disk_size': 1024,
            'over_committed_disk_size': 0}

    return disk_info


def fake_disk_info_json(instance, type='qcow2'):
    """Return fake instance_disk_info corresponding accurately to the
    properties of the given Instance object.

    :param instance: The instance we're generating fake disk_info for.
    :param type: libvirt's disk type.
    :return: JSON representation of instance_disk_info for all disks.
    :rtype: str
    """
    disk_info = fake_disk_info_byname(instance, type)
    return jsonutils.dumps(disk_info.values())


def _concurrency(signal, wait, done, target, is_block_dev=False):
    signal.send()
    wait.wait()
    done.send()


class FakeVirtDomain(object):

    def __init__(self, fake_xml=None, uuidstr=None, id=None, name=None):
        if uuidstr is None:
            uuidstr = str(uuid.uuid4())
        self.uuidstr = uuidstr
        self.id = id
        self.domname = name
        self._info = [power_state.RUNNING, 2048 * units.Mi, 1234 * units.Mi,
                     None, None]
        if fake_xml:
            self._fake_dom_xml = fake_xml
        else:
            self._fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                    </devices>
                </domain>
            """

    def name(self):
        if self.domname is None:
            return "fake-domain %s" % self
        else:
            return self.domname

    def ID(self):
        return self.id

    def info(self):
        return self._info

    def create(self):
        pass

    def managedSave(self, *args):
        pass

    def createWithFlags(self, launch_flags):
        pass

    def XMLDesc(self, flags):
        return self._fake_dom_xml

    def UUIDString(self):
        return self.uuidstr

    def attachDeviceFlags(self, xml, flags):
        pass

    def attachDevice(self, xml):
        pass

    def detachDeviceFlags(self, xml, flags):
        pass

    def snapshotCreateXML(self, xml, flags):
        pass

    def blockCommit(self, disk, base, top, bandwidth=0, flags=0):
        pass

    def blockRebase(self, disk, base, bandwidth=0, flags=0):
        pass

    def blockJobInfo(self, path, flags):
        pass

    def resume(self):
        pass

    def destroy(self):
        pass

    def fsFreeze(self, disks=None, flags=0):
        pass

    def fsThaw(self, disks=None, flags=0):
        pass

    def isActive(self):
        return True


class CacheConcurrencyTestCase(test.NoDBTestCase):
    def setUp(self):
        super(CacheConcurrencyTestCase, self).setUp()

        self.flags(instances_path=self.useFixture(fixtures.TempDir()).path)

        # utils.synchronized() will create the lock_path for us if it
        # doesn't already exist. It will also delete it when it's done,
        # which can cause race conditions with the multiple threads we
        # use for tests. So, create the path here so utils.synchronized()
        # won't delete it out from under one of the threads.
        self.lock_path = os.path.join(CONF.instances_path, 'locks')
        fileutils.ensure_tree(self.lock_path)

        def fake_exists(fname):
            basedir = os.path.join(CONF.instances_path,
                                   CONF.image_cache_subdirectory_name)
            if fname == basedir or fname == self.lock_path:
                return True
            return False

        def fake_execute(*args, **kwargs):
            pass

        def fake_extend(image, size, use_cow=False):
            pass

        self.stub_out('os.path.exists', fake_exists)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(imagebackend.disk, 'extend', fake_extend)
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))

    def _fake_instance(self, uuid):
        return objects.Instance(id=1, uuid=uuid)

    def test_same_fname_concurrency(self):
        # Ensures that the same fname cache runs at a sequentially.
        uuid = uuidutils.generate_uuid()

        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        sig1 = eventlet.event.Event()
        thr1 = eventlet.spawn(backend.image(self._fake_instance(uuid),
                                            'name').cache,
                _concurrency, 'fname', None,
                signal=sig1, wait=wait1, done=done1)
        eventlet.sleep(0)
        # Thread 1 should run before thread 2.
        sig1.wait()

        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        sig2 = eventlet.event.Event()
        thr2 = eventlet.spawn(backend.image(self._fake_instance(uuid),
                                            'name').cache,
                _concurrency, 'fname', None,
                signal=sig2, wait=wait2, done=done2)

        wait2.send()
        eventlet.sleep(0)
        try:
            self.assertFalse(done2.ready())
        finally:
            wait1.send()
        done1.wait()
        eventlet.sleep(0)
        self.assertTrue(done2.ready())
        # Wait on greenthreads to assert they didn't raise exceptions
        # during execution
        thr1.wait()
        thr2.wait()

    def test_different_fname_concurrency(self):
        # Ensures that two different fname caches are concurrent.
        uuid = uuidutils.generate_uuid()

        backend = imagebackend.Backend(False)
        wait1 = eventlet.event.Event()
        done1 = eventlet.event.Event()
        sig1 = eventlet.event.Event()
        thr1 = eventlet.spawn(backend.image(self._fake_instance(uuid),
                                            'name').cache,
                _concurrency, 'fname2', None,
                signal=sig1, wait=wait1, done=done1)
        eventlet.sleep(0)
        # Thread 1 should run before thread 2.
        sig1.wait()

        wait2 = eventlet.event.Event()
        done2 = eventlet.event.Event()
        sig2 = eventlet.event.Event()
        thr2 = eventlet.spawn(backend.image(self._fake_instance(uuid),
                                            'name').cache,
                _concurrency, 'fname1', None,
                signal=sig2, wait=wait2, done=done2)
        eventlet.sleep(0)
        # Wait for thread 2 to start.
        sig2.wait()

        wait2.send()
        tries = 0
        while not done2.ready() and tries < 10:
            eventlet.sleep(0)
            tries += 1
        try:
            self.assertTrue(done2.ready())
        finally:
            wait1.send()
            eventlet.sleep(0)
        # Wait on greenthreads to assert they didn't raise exceptions
        # during execution
        thr1.wait()
        thr2.wait()


class FakeVolumeDriver(object):
    def __init__(self, *args, **kwargs):
        pass

    def attach_volume(self, *args):
        pass

    def detach_volume(self, *args):
        pass

    def get_xml(self, *args):
        return ""

    def get_config(self, *args):
        """Connect the volume to a fake device."""
        conf = vconfig.LibvirtConfigGuestDisk()
        conf.source_type = "network"
        conf.source_protocol = "fake"
        conf.source_name = "fake"
        conf.target_dev = "fake"
        conf.target_bus = "fake"
        return conf

    def connect_volume(self, *args):
        """Connect the volume to a fake device."""
        pass


class FakeConfigGuestDisk(object):
    def __init__(self, *args, **kwargs):
        self.source_type = None
        self.driver_cache = None


class FakeConfigGuest(object):
    def __init__(self, *args, **kwargs):
        self.driver_cache = None


class FakeNodeDevice(object):
    def __init__(self, fakexml):
        self.xml = fakexml

    def XMLDesc(self, flags):
        return self.xml


def _create_test_instance():
    flavor = objects.Flavor(memory_mb=2048,
                            swap=0,
                            vcpu_weight=None,
                            root_gb=10,
                            id=2,
                            name=u'm1.small',
                            ephemeral_gb=20,
                            rxtx_factor=1.0,
                            flavorid=u'1',
                            vcpus=2,
                            extra_specs={})
    return {
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


class LibvirtConnTestCase(test.NoDBTestCase):

    REQUIRES_LOCKING = True

    _EPHEMERAL_20_DEFAULT = eph_name(20)

    def setUp(self):
        super(LibvirtConnTestCase, self).setUp()
        self.user_id = 'fake'
        self.project_id = 'fake'
        self.context = context.get_admin_context()
        temp_dir = self.useFixture(fixtures.TempDir()).path
        self.flags(instances_path=temp_dir)
        self.flags(snapshots_directory=temp_dir, group='libvirt')
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.libvirt_utils',
            fake_libvirt_utils))

        self.flags(sysinfo_serial="hardware", group="libvirt")

        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))

        def fake_extend(image, size, use_cow=False):
            pass

        self.stubs.Set(libvirt_driver.disk_api, 'extend', fake_extend)

        self.stubs.Set(imagebackend.Image, 'resolve_driver_format',
                       imagebackend.Image._get_driver_format)

        self.useFixture(fakelibvirt.FakeLibvirtFixture())
        self.test_instance = _create_test_instance()
        self.test_image_meta = {
            "disk_format": "raw",
        }
        self.image_service = nova.tests.unit.image.fake.stub_out_image_service(
                self)
        self.device_xml_tmpl = """
        <domain type='kvm'>
          <devices>
            <disk type='block' device='disk'>
              <driver name='qemu' type='raw' cache='none'/>
              <source dev='{device_path}'/>
              <target bus='virtio' dev='vdb'/>
              <serial>58a84f6d-3f0c-4e19-a0af-eb657b790657</serial>
              <address type='pci' domain='0x0' bus='0x0' slot='0x04' \
              function='0x0'/>
            </disk>
          </devices>
        </domain>
        """

    def relpath(self, path):
        return os.path.relpath(path, CONF.instances_path)

    def tearDown(self):
        nova.tests.unit.image.fake.FakeImageService_reset()
        super(LibvirtConnTestCase, self).tearDown()

    def test_driver_capabilities(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertTrue(drvr.capabilities['has_imagecache'],
                        'Driver capabilities for \'has_imagecache\' '
                        'is invalid')
        self.assertTrue(drvr.capabilities['supports_recreate'],
                        'Driver capabilities for \'supports_recreate\' '
                        'is invalid')
        self.assertFalse(drvr.capabilities['supports_migrate_to_same_host'],
                         'Driver capabilities for '
                         '\'supports_migrate_to_same_host\' is invalid')
        self.assertTrue(drvr.capabilities['supports_attach_interface'],
                        'Driver capabilities for '
                        '\'supports_attach_interface\' '
                        'is invalid')

    def create_fake_libvirt_mock(self, **kwargs):
        """Defining mocks for LibvirtDriver(libvirt is not used)."""

        # A fake libvirt.virConnect
        class FakeLibvirtDriver(object):
            def defineXML(self, xml):
                return FakeVirtDomain()

        # Creating mocks
        volume_driver = ['iscsi=nova.tests.unit.virt.libvirt.test_driver'
                         '.FakeVolumeDriver']
        fake = FakeLibvirtDriver()
        # Customizing above fake if necessary
        for key, val in kwargs.items():
            fake.__setattr__(key, val)

        self.stubs.Set(libvirt_driver.LibvirtDriver, '_conn', fake)
        self.stubs.Set(libvirt_driver.LibvirtDriver, '_get_volume_drivers',
            lambda x: volume_driver)
        self.stubs.Set(host.Host, 'get_connection', lambda x: fake)

    def fake_lookup(self, instance_name):
        return FakeVirtDomain()

    def fake_execute(self, *args, **kwargs):
        open(args[-1], "a").close()

    def _create_service(self, **kwargs):
        service_ref = {'host': kwargs.get('host', 'dummy'),
                       'disabled': kwargs.get('disabled', False),
                       'binary': 'nova-compute',
                       'topic': 'compute',
                       'report_count': 0}

        return objects.Service(**service_ref)

    def _get_pause_flag(self, drvr, network_info, power_on=True,
                          vifs_already_plugged=False):
        timeout = CONF.vif_plugging_timeout

        events = []
        if (drvr._conn_supports_start_paused and
            utils.is_neutron() and
            not vifs_already_plugged and
            power_on and timeout):
            events = drvr._get_neutron_events(network_info)

        return bool(events)

    def test_public_api_signatures(self):
        baseinst = driver.ComputeDriver(None)
        inst = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertPublicAPISignatures(baseinst, inst)

    def test_legacy_block_device_info(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertFalse(drvr.need_legacy_block_device_info)

    @mock.patch.object(host.Host, "has_min_version")
    def test_min_version_start_ok(self, mock_version):
        mock_version.return_value = True
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")

    @mock.patch.object(host.Host, "has_min_version")
    def test_min_version_start_abort(self, mock_version):
        mock_version.return_value = False
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.init_host,
                          "dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                            libvirt_driver.NEXT_MIN_LIBVIRT_VERSION) - 1)
    @mock.patch.object(libvirt_driver.LOG, 'warning')
    def test_next_min_version_deprecation_warning(self, mock_warning,
                                                  mock_get_libversion):
        # Skip test if there's no currently planned new min version
        if (versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_LIBVIRT_VERSION) ==
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_VERSION)):
            self.skipTest("NEXT_MIN_LIBVIRT_VERSION == MIN_LIBVIRT_VERSION")

        # Test that a warning is logged if the libvirt version is less than
        # the next required minimum version.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")
        # assert that the next min version is in a warning message
        expected_arg = {'version': versionutils.convert_version_to_str(
            versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_LIBVIRT_VERSION))}
        version_arg_found = False
        for call in mock_warning.call_args_list:
            if call[0][1] == expected_arg:
                version_arg_found = True
                break
        self.assertTrue(version_arg_found)

    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                            libvirt_driver.NEXT_MIN_QEMU_VERSION) - 1)
    @mock.patch.object(libvirt_driver.LOG, 'warning')
    def test_next_min_qemu_version_deprecation_warning(self, mock_warning,
                                                       mock_get_libversion):
        # Skip test if there's no currently planned new min version
        if (versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_QEMU_VERSION) ==
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_VERSION)):
            self.skipTest("NEXT_MIN_QEMU_VERSION == MIN_QEMU_VERSION")

        # Test that a warning is logged if the libvirt version is less than
        # the next required minimum version.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")
        # assert that the next min version is in a warning message
        expected_arg = {'version': versionutils.convert_version_to_str(
            versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_QEMU_VERSION))}
        version_arg_found = False
        for call in mock_warning.call_args_list:
            if call[0][1] == expected_arg:
                version_arg_found = True
                break
        self.assertTrue(version_arg_found)

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                            libvirt_driver.NEXT_MIN_LIBVIRT_VERSION))
    @mock.patch.object(libvirt_driver.LOG, 'warning')
    def test_next_min_version_ok(self, mock_warning, mock_get_libversion):
        # Skip test if there's no currently planned new min version

        if (versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_LIBVIRT_VERSION) ==
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_VERSION)):
            self.skipTest("NEXT_MIN_LIBVIRT_VERSION == MIN_LIBVIRT_VERSION")

        # Test that a warning is not logged if the libvirt version is greater
        # than or equal to NEXT_MIN_LIBVIRT_VERSION.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")
        # assert that the next min version is in a warning message
        expected_arg = {'version': versionutils.convert_version_to_str(
            versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_LIBVIRT_VERSION))}
        version_arg_found = False
        for call in mock_warning.call_args_list:
            if call[0][1] == expected_arg:
                version_arg_found = True
                break
        self.assertFalse(version_arg_found)

    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                            libvirt_driver.NEXT_MIN_QEMU_VERSION))
    @mock.patch.object(libvirt_driver.LOG, 'warning')
    def test_next_min_qemu_version_ok(self, mock_warning, mock_get_libversion):
        # Skip test if there's no currently planned new min version

        if (versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_QEMU_VERSION) ==
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_VERSION)):
            self.skipTest("NEXT_MIN_QEMU_VERSION == MIN_QEMU_VERSION")

        # Test that a warning is not logged if the libvirt version is greater
        # than or equal to NEXT_MIN_QEMU_VERSION.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")
        # assert that the next min version is in a warning message
        expected_arg = {'version': versionutils.convert_version_to_str(
            versionutils.convert_version_to_int(
                libvirt_driver.NEXT_MIN_QEMU_VERSION))}
        version_arg_found = False
        for call in mock_warning.call_args_list:
            if call[0][1] == expected_arg:
                version_arg_found = True
                break
        self.assertFalse(version_arg_found)

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.PPC64)) - 1)
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.PPC64)))
    @mock.patch.object(arch, "from_host", return_value=arch.PPC64)
    def test_min_version_ppc_old_libvirt(self, mock_libv, mock_qemu,
                                         mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.init_host,
                          "dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.PPC64)))
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.PPC64)) - 1)
    @mock.patch.object(arch, "from_host", return_value=arch.PPC64)
    def test_min_version_ppc_old_qemu(self, mock_libv, mock_qemu,
                                      mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.init_host,
                          "dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.PPC64)))
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.PPC64)))
    @mock.patch.object(arch, "from_host", return_value=arch.PPC64)
    def test_min_version_ppc_ok(self, mock_libv, mock_qemu, mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.S390X)) - 1)
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.S390X)))
    @mock.patch.object(arch, "from_host", return_value=arch.S390X)
    def test_min_version_s390_old_libvirt(self, mock_libv, mock_qemu,
                                          mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.init_host,
                          "dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.S390X)))
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.S390X)) - 1)
    @mock.patch.object(arch, "from_host", return_value=arch.S390X)
    def test_min_version_s390_old_qemu(self, mock_libv, mock_qemu,
                                       mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.init_host,
                          "dummyhost")

    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_LIBVIRT_OTHER_ARCH.get(
                               arch.S390X)))
    @mock.patch.object(fakelibvirt.Connection, 'getVersion',
                       return_value=versionutils.convert_version_to_int(
                           libvirt_driver.MIN_QEMU_OTHER_ARCH.get(
                               arch.S390X)))
    @mock.patch.object(arch, "from_host", return_value=arch.S390X)
    def test_min_version_s390_ok(self, mock_libv, mock_qemu, mock_arch):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host("dummyhost")

    def _do_test_parse_migration_flags(self, lm_expected=None,
                                       bm_expected=None):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr._parse_migration_flags()

        if lm_expected is not None:
            self.assertEqual(lm_expected, drvr._live_migration_flags)
        if bm_expected is not None:
            self.assertEqual(bm_expected, drvr._block_migration_flags)

    def test_parse_live_migration_flags_default(self):
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE))

    def test_parse_live_migration_flags(self):
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE))

    def test_parse_block_migration_flags_default(self):
        self._do_test_parse_migration_flags(
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    def test_parse_block_migration_flags(self):
        self._do_test_parse_migration_flags(
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    def test_parse_migration_flags_p2p_xen(self):
        self.flags(virt_type='xen', group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    def test_live_migration_tunnelled_none(self):
        self.flags(live_migration_tunnelled=None, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_TUNNELLED),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_TUNNELLED))

    def test_live_migration_tunnelled_true(self):
        self.flags(live_migration_tunnelled=True, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_TUNNELLED),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_TUNNELLED))

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_live_migration_permit_postcopy_true(self, host):
        self.flags(live_migration_permit_post_copy=True, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_POSTCOPY),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_POSTCOPY))

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_live_migration_permit_auto_converge_true(self, host):
        self.flags(live_migration_permit_auto_converge=True, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_AUTO_CONVERGE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_AUTO_CONVERGE))

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_live_migration_permit_auto_converge_and_post_copy_true(self,
                                                                    host):
        self.flags(live_migration_permit_auto_converge=True, group='libvirt')
        self.flags(live_migration_permit_post_copy=True, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_POSTCOPY),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_POSTCOPY))

    @mock.patch.object(host.Host, 'has_min_version')
    def test_live_migration_auto_converge_and_post_copy_true_old_libvirt(
            self, mock_host):
        self.flags(live_migration_permit_auto_converge=True, group='libvirt')
        self.flags(live_migration_permit_post_copy=True, group='libvirt')

        def fake_has_min_version(lv_ver=None, hv_ver=None, hv_type=None):
            if (lv_ver == libvirt_driver.MIN_LIBVIRT_POSTCOPY_VERSION and
                    hv_ver == libvirt_driver.MIN_QEMU_POSTCOPY_VERSION):
                return False
            return True
        mock_host.side_effect = fake_has_min_version

        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_AUTO_CONVERGE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC |
                         libvirt_driver.libvirt.VIR_MIGRATE_AUTO_CONVERGE))

    @mock.patch.object(host.Host, 'has_min_version', return_value=False)
    def test_live_migration_permit_postcopy_true_old_libvirt(self, host):
        self.flags(live_migration_permit_post_copy=True, group='libvirt')

        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    @mock.patch.object(host.Host, 'has_min_version', return_value=False)
    def test_live_migration_permit_auto_converge_true_old_libvirt(self, host):
        self.flags(live_migration_permit_auto_converge=True, group='libvirt')
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    def test_live_migration_permit_postcopy_false(self):
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    def test_live_migration_permit_autoconverge_false(self):
        self._do_test_parse_migration_flags(
            lm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE),
            bm_expected=(libvirt_driver.libvirt.VIR_MIGRATE_UNDEFINE_SOURCE |
                         libvirt_driver.libvirt.VIR_MIGRATE_PEER2PEER |
                         libvirt_driver.libvirt.VIR_MIGRATE_LIVE |
                         libvirt_driver.libvirt.VIR_MIGRATE_NON_SHARED_INC))

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.host.Host.get_guest')
    def test_set_admin_password(self, mock_get_guest, ver, mock_image):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_get_guest.return_value = mock_guest

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.set_admin_password(instance, "123")

        mock_guest.set_user_password.assert_called_once_with("root", "123")

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.host.Host.get_guest')
    def test_set_admin_password_windows(self, mock_get_guest, ver, mock_image):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        instance.os_type = "windows"
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_get_guest.return_value = mock_guest

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.set_admin_password(instance, "123")

        mock_guest.set_user_password.assert_called_once_with(
            "Administrator", "123")

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.host.Host.get_guest')
    def test_set_admin_password_image(self, mock_get_guest, ver, mock_image):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes",
            "os_admin_user": "foo"
        }}
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_get_guest.return_value = mock_guest

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.set_admin_password(instance, "123")

        mock_guest.set_user_password.assert_called_once_with("foo", "123")

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=False)
    def test_set_admin_password_bad_version(self, mock_svc, mock_image):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.SetAdminPasswdNotSupported,
                          drvr.set_admin_password, instance, "123")

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_set_admin_password_bad_hyp(self, mock_svc, mock_image):
        self.flags(virt_type='lxc', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.SetAdminPasswdNotSupported,
                          drvr.set_admin_password, instance, "123")

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_set_admin_password_guest_agent_not_running(self, mock_svc):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.QemuGuestAgentNotEnabled,
                          drvr.set_admin_password, instance, "123")

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.host.Host.get_guest')
    def test_set_admin_password_error(self, mock_get_guest, ver, mock_image):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        mock_image.return_value = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_guest.set_user_password.side_effect = (
            fakelibvirt.libvirtError("error"))
        mock_get_guest.return_value = mock_guest

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.NovaException,
                          drvr.set_admin_password, instance, "123")

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    def test_set_host_enabled_with_disable(self, mock_svc):
        # Tests disabling an enabled host.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        svc = self._create_service(host='fake-mini')
        mock_svc.return_value = svc
        drvr._set_host_enabled(False)
        self.assertTrue(svc.disabled)

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    def test_set_host_enabled_with_enable(self, mock_svc):
        # Tests enabling a disabled host.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        svc = self._create_service(disabled=True, host='fake-mini')
        mock_svc.return_value = svc
        drvr._set_host_enabled(True)
        self.assertTrue(svc.disabled)

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    def test_set_host_enabled_with_enable_state_enabled(self, mock_svc):
        # Tests enabling an enabled host.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        svc = self._create_service(disabled=False, host='fake-mini')
        mock_svc.return_value = svc
        drvr._set_host_enabled(True)
        self.assertFalse(svc.disabled)

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    def test_set_host_enabled_with_disable_state_disabled(self, mock_svc):
        # Tests disabling a disabled host.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        svc = self._create_service(disabled=True, host='fake-mini')
        mock_svc.return_value = svc
        drvr._set_host_enabled(False)
        self.assertTrue(svc.disabled)

    def test_set_host_enabled_swallows_exceptions(self):
        # Tests that set_host_enabled will swallow exceptions coming from the
        # db_api code so they don't break anything calling it, e.g. the
        # _get_new_connection method.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        with mock.patch.object(db, 'service_get_by_compute_host') as db_mock:
            # Make db.service_get_by_compute_host raise NovaException; this
            # is more robust than just raising ComputeHostNotFound.
            db_mock.side_effect = exception.NovaException
            drvr._set_host_enabled(False)

    @mock.patch.object(fakelibvirt.virConnect, "nodeDeviceLookupByName")
    def test_prepare_pci_device(self, mock_lookup):

        pci_devices = [dict(hypervisor_name='xxx')]

        self.flags(virt_type='xen', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        conn = drvr._host.get_connection()

        mock_lookup.side_effect = lambda x: fakelibvirt.NodeDevice(conn)
        drvr._prepare_pci_devices_for_use(pci_devices)

    @mock.patch.object(fakelibvirt.virConnect, "nodeDeviceLookupByName")
    @mock.patch.object(fakelibvirt.virNodeDevice, "dettach")
    def test_prepare_pci_device_exception(self, mock_detach, mock_lookup):

        pci_devices = [dict(hypervisor_name='xxx',
                            id='id1',
                            instance_uuid='uuid')]

        self.flags(virt_type='xen', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        conn = drvr._host.get_connection()

        mock_lookup.side_effect = lambda x: fakelibvirt.NodeDevice(conn)
        mock_detach.side_effect = fakelibvirt.libvirtError("xxxx")

        self.assertRaises(exception.PciDevicePrepareFailed,
                          drvr._prepare_pci_devices_for_use, pci_devices)

    @mock.patch.object(host.Host, "has_min_version", return_value=False)
    def test_device_metadata(self, mock_version):
        xml = """
        <domain>
          <name>dummy</name>
          <uuid>32dfcb37-5af1-552b-357c-be8c3aa38310</uuid>
            <memory>1048576</memory>
              <vcpu>1</vcpu>
            <os>
                <type arch='x86_64' machine='pc-i440fx-2.4'>hvm</type>
            </os>
          <devices>
            <disk type='block' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source dev='/dev/mapper/generic'/>
              <target dev='sda' bus='scsi'/>
             <address type='drive' controller='0' bus='0' target='0' unit='0'/>
            </disk>
            <disk type='block' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source dev='/dev/mapper/generic-1'/>
              <target dev='hda' bus='ide'/>
             <address type='drive' controller='0' bus='1' target='0' unit='0'/>
            </disk>
            <disk type='block' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source dev='/dev/mapper/generic-2'/>
              <target dev='hdb' bus='ide'/>
             <address type='drive' controller='0' bus='1' target='1' unit='1'/>
            </disk>
            <disk type='block' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source dev='/dev/mapper/aa1'/>
              <target dev='sdb' bus='usb'/>
            </disk>
            <disk type='block' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source dev='/var/lib/libvirt/images/centos'/>
              <backingStore/>
              <target dev='vda' bus='virtio'/>
              <boot order='1'/>
              <alias name='virtio-disk0'/>
              <address type='pci' domain='0x0000' bus='0x00' slot='0x09'
              function='0x0'/>
            </disk>
            <disk type='file' device='disk'>
                <driver name='qemu' type='qcow2' cache='none'/>
                <source file='/var/lib/libvirt/images/generic.qcow2'/>
                <target dev='vdb' bus='virtio'/>
                <address type='virtio-mmio'/>
            </disk>
            <disk type='file' device='disk'>
                <driver name='qemu' type='qcow2'/>
                <source file='/var/lib/libvirt/images/test.qcow2'/>
                <backingStore/>
                <target dev='vdc' bus='virtio'/>
                <alias name='virtio-disk1'/>
                <address type='ccw' cssid='0xfe' ssid='0x0' devno='0x0000'/>
            </disk>
            <interface type='network'>
              <mac address='52:54:00:f6:35:8f'/>
              <source network='default'/>
              <model type='virtio'/>
              <address type='pci' domain='0x0000' bus='0x00' slot='0x03'
              function='0x0'/>
            </interface>
            <interface type='network'>
              <mac address='51:5a:2c:a4:5e:1b'/>
              <source network='default'/>
              <model type='virtio'/>
              <address type='pci' domain='0x0000' bus='0x00' slot='0x04'
              function='0x1'/>
            </interface>
            <interface type='network'>
              <mac address='fa:16:3e:d1:28:e4'/>
              <source network='default'/>
              <model type='virtio'/>
              <address type='virtio-mmio'/>
            </interface>
            <interface type='network'>
                <mac address='52:54:00:14:6f:50'/>
                <source network='default' bridge='virbr0'/>
                <target dev='vnet0'/>
                <model type='virtio'/>
                <alias name='net0'/>
                <address type='ccw' cssid='0xfe' ssid='0x0' devno='0x0001'/>
            </interface>
          </devices>
        </domain>"""

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        dom = fakelibvirt.Domain(drvr._get_connection(), xml, False)
        guest = libvirt_guest.Guest(dom)

        instance_ref = objects.Instance(**self.test_instance)
        bdms = block_device_obj.block_device_make_list_from_dicts(
            self.context, [
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 1,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/sda', 'tag': "db"}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 2,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/hda', 'tag': "nfvfunc1"}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 3,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/sdb', 'tag': "nfvfunc2"}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 4,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/hdb'}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 5,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/vda', 'tag': "nfvfunc3"}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 6,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/vdb', 'tag': "nfvfunc4"}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 7,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/vdc', 'tag': "nfvfunc5"}),
            ]
        )
        vif = obj_vif.VirtualInterface(context=self.context)
        vif.address = '52:54:00:f6:35:8f'
        vif.network_id = 123
        vif.instance_uuid = '32dfcb37-5af1-552b-357c-be8c3aa38310'
        vif.uuid = '12ec4b21-ef22-6c21-534b-ba3e3ab3a311'
        vif.tag = 'mytag1'
        vif1 = obj_vif.VirtualInterface(context=self.context)
        vif1.address = '51:5a:2c:a4:5e:1b'
        vif1.network_id = 123
        vif1.instance_uuid = '32dfcb37-5af1-552b-357c-be8c3aa38310'
        vif1.uuid = 'abec4b21-ef22-6c21-534b-ba3e3ab3a312'
        vif1.tag = None
        vif2 = obj_vif.VirtualInterface(context=self.context)
        vif2.address = 'fa:16:3e:d1:28:e4'
        vif2.network_id = 123
        vif2.instance_uuid = '32dfcb37-5af1-552b-357c-be8c3aa38310'
        vif2.uuid = '645686e4-7086-4eab-8c2f-c41f017a1b16'
        vif2.tag = 'mytag2'
        vif3 = obj_vif.VirtualInterface(context=self.context)
        vif3.address = '52:54:00:14:6f:50'
        vif3.network_id = 123
        vif3.instance_uuid = '32dfcb37-5af1-552b-357c-be8c3aa38310'
        vif3.uuid = '99cc3604-782d-4a32-a27c-bc33ac56ce86'
        vif3.tag = 'mytag3'
        vifs = [vif, vif1, vif2, vif3]

        with test.nested(
            mock.patch('nova.objects.VirtualInterfaceList'
                       '.get_by_instance_uuid', return_value=vifs),
            mock.patch('nova.objects.BlockDeviceMappingList'
                       '.get_by_instance_uuid', return_value=bdms),
            mock.patch('nova.virt.libvirt.host.Host.get_guest',
                       return_value=guest),
            mock.patch.object(nova.virt.libvirt.guest.Guest, 'get_xml_desc',
                              return_value=xml)):
            metadata_obj = drvr._build_device_metadata(self.context,
                                                       instance_ref)
            metadata = metadata_obj.devices
            self.assertEqual(9, len(metadata))
            self.assertIsInstance(metadata[0],
                                  objects.DiskMetadata)
            self.assertIsInstance(metadata[0].bus,
                                  objects.SCSIDeviceBus)
            self.assertEqual(['db'], metadata[0].tags)
            self.assertFalse(metadata[0].bus.obj_attr_is_set('address'))
            self.assertEqual(['nfvfunc1'], metadata[1].tags)
            self.assertIsInstance(metadata[1],
                                  objects.DiskMetadata)
            self.assertIsInstance(metadata[1].bus,
                                  objects.IDEDeviceBus)
            self.assertEqual(['nfvfunc1'], metadata[1].tags)
            self.assertFalse(metadata[1].bus.obj_attr_is_set('address'))
            self.assertIsInstance(metadata[2],
                                  objects.DiskMetadata)
            self.assertIsInstance(metadata[2].bus,
                                  objects.USBDeviceBus)
            self.assertEqual(['nfvfunc2'], metadata[2].tags)
            self.assertFalse(metadata[2].bus.obj_attr_is_set('address'))
            self.assertIsInstance(metadata[3],
                                  objects.DiskMetadata)
            self.assertIsInstance(metadata[3].bus,
                                  objects.PCIDeviceBus)
            self.assertEqual(['nfvfunc3'], metadata[3].tags)
            self.assertEqual('0000:00:09.0', metadata[3].bus.address)
            self.assertIsInstance(metadata[4],
                                  objects.DiskMetadata)
            self.assertEqual(['nfvfunc4'], metadata[4].tags)
            self.assertIsInstance(metadata[5],
                                  objects.DiskMetadata)
            self.assertEqual(['nfvfunc5'], metadata[5].tags)
            self.assertIsInstance(metadata[6],
                                  objects.NetworkInterfaceMetadata)
            self.assertIsInstance(metadata[6].bus,
                                  objects.PCIDeviceBus)
            self.assertEqual(['mytag1'], metadata[6].tags)
            self.assertEqual('0000:00:03.0', metadata[6].bus.address)
            self.assertIsInstance(metadata[7],
                                  objects.NetworkInterfaceMetadata)
            self.assertEqual(['mytag2'], metadata[7].tags)
            self.assertIsInstance(metadata[8],
                                  objects.NetworkInterfaceMetadata)
            self.assertEqual(['mytag3'], metadata[8].tags)

    @mock.patch.object(host.Host, 'get_connection')
    @mock.patch.object(nova.virt.libvirt.guest.Guest, 'get_xml_desc')
    def test_detach_pci_devices(self, mocked_get_xml_desc, mock_conn):

        fake_domXML1_with_pci = (
            """<domain> <devices>
            <disk type='file' device='disk'>
            <driver name='qemu' type='qcow2' cache='none'/>
            <source file='xxx'/>
            <target dev='vda' bus='virtio'/>
            <alias name='virtio-disk0'/>
            <address type='pci' domain='0x0000' bus='0x00'
            slot='0x04' function='0x0'/>
            </disk>
            <hostdev mode="subsystem" type="pci" managed="yes">
            <source>
            <address function="0x1" slot="0x10" domain="0x0001"
             bus="0x04"/>
            </source>
            </hostdev></devices></domain>""")

        fake_domXML1_without_pci = (
            """<domain> <devices>
            <disk type='file' device='disk'>
            <driver name='qemu' type='qcow2' cache='none'/>
            <source file='xxx'/>
            <target dev='vda' bus='virtio'/>
            <alias name='virtio-disk0'/>
            <address type='pci' domain='0x0001' bus='0x00'
            slot='0x04' function='0x0'/>
            </disk></devices></domain>""")

        pci_device_info = {'compute_node_id': 1,
                           'instance_uuid': 'uuid',
                           'address': '0001:04:10.1'}
        pci_device = objects.PciDevice(**pci_device_info)
        pci_devices = [pci_device]
        mocked_get_xml_desc.return_value = fake_domXML1_without_pci

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        dom = fakelibvirt.Domain(
            drvr._get_connection(), fake_domXML1_with_pci, False)
        guest = libvirt_guest.Guest(dom)
        drvr._detach_pci_devices(guest, pci_devices)

    @mock.patch.object(host.Host, 'get_connection')
    @mock.patch.object(nova.virt.libvirt.guest.Guest, 'get_xml_desc')
    def test_detach_pci_devices_timeout(self, mocked_get_xml_desc, mock_conn):

        fake_domXML1_with_pci = (
            """<domain> <devices>
            <disk type='file' device='disk'>
            <driver name='qemu' type='qcow2' cache='none'/>
            <source file='xxx'/>
            <target dev='vda' bus='virtio'/>
            <alias name='virtio-disk0'/>
            <address type='pci' domain='0x0000' bus='0x00'
            slot='0x04' function='0x0'/>
            </disk>
            <hostdev mode="subsystem" type="pci" managed="yes">
            <source>
            <address function="0x1" slot="0x10" domain="0x0001"
             bus="0x04"/>
            </source>
            </hostdev></devices></domain>""")

        pci_device_info = {'compute_node_id': 1,
                           'instance_uuid': 'uuid',
                           'address': '0001:04:10.1'}
        pci_device = objects.PciDevice(**pci_device_info)
        pci_devices = [pci_device]
        mocked_get_xml_desc.return_value = fake_domXML1_with_pci

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        dom = fakelibvirt.Domain(
            drvr._get_connection(), fake_domXML1_with_pci, False)
        guest = libvirt_guest.Guest(dom)
        self.assertRaises(exception.PciDeviceDetachFailed,
                          drvr._detach_pci_devices, guest, pci_devices)

    @mock.patch.object(connector, 'get_connector_properties')
    def test_get_connector(self, fake_get_connector):
        initiator = 'fake.initiator.iqn'
        ip = 'fakeip'
        host = 'fakehost'
        wwpns = ['100010604b019419']
        wwnns = ['200010604b019419']
        self.flags(my_ip=ip)
        self.flags(host=host)

        expected = {
            'ip': ip,
            'initiator': initiator,
            'host': host,
            'wwpns': wwpns,
            'wwnns': wwnns
        }
        volume = {
            'id': 'fake'
        }

        # TODO(walter-boring) add the fake in os-brick
        fake_get_connector.return_value = expected
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        result = drvr.get_volume_connector(volume)
        self.assertThat(expected, matchers.DictMatches(result))

    @mock.patch.object(connector, 'get_connector_properties')
    def test_get_connector_storage_ip(self, fake_get_connector):
        ip = '100.100.100.100'
        storage_ip = '101.101.101.101'
        self.flags(my_block_storage_ip=storage_ip, my_ip=ip)
        volume = {
            'id': 'fake'
        }
        expected = {
            'ip': storage_ip
        }
        # TODO(walter-boring) add the fake in os-brick
        fake_get_connector.return_value = expected
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        result = drvr.get_volume_connector(volume)
        self.assertEqual(storage_ip, result['ip'])

    def test_lifecycle_event_registration(self):
        calls = []

        def fake_registerErrorHandler(*args, **kwargs):
            calls.append('fake_registerErrorHandler')

        def fake_get_host_capabilities(**args):
            cpu = vconfig.LibvirtConfigGuestCPU()
            cpu.arch = arch.ARMV7

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            calls.append('fake_get_host_capabilities')
            return caps

        @mock.patch.object(fakelibvirt, 'registerErrorHandler',
                           side_effect=fake_registerErrorHandler)
        @mock.patch.object(host.Host, "get_capabilities",
                            side_effect=fake_get_host_capabilities)
        def test_init_host(get_host_capabilities, register_error_handler):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
            drvr.init_host("test_host")

        test_init_host()
        # NOTE(dkliban): Will fail if get_host_capabilities is called before
        # registerErrorHandler
        self.assertEqual(['fake_registerErrorHandler',
                          'fake_get_host_capabilities'], calls)

    def test_sanitize_log_to_xml(self):
        # setup fake data
        data = {'auth_password': 'scrubme'}
        bdm = [{'connection_info': {'data': data}}]
        bdi = {'block_device_mapping': bdm}

        # Tests that the parameters to the _get_guest_xml method
        # are sanitized for passwords when logged.
        def fake_debug(*args, **kwargs):
            if 'auth_password' in args[0]:
                self.assertNotIn('scrubme', args[0])

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        conf = mock.Mock()
        with test.nested(
            mock.patch.object(libvirt_driver.LOG, 'debug',
                              side_effect=fake_debug),
            mock.patch.object(drvr, '_get_guest_config', return_value=conf)
        ) as (
            debug_mock, conf_mock
        ):
            drvr._get_guest_xml(self.context, self.test_instance,
                                network_info={}, disk_info={},
                                image_meta={}, block_device_info=bdi)
            # we don't care what the log message is, we just want to make sure
            # our stub method is called which asserts the password is scrubbed
            self.assertTrue(debug_mock.called)

    @mock.patch.object(time, "time")
    def test_get_guest_config(self, time_mock):
        time_mock.return_value = 1234567.89

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        test_instance = copy.deepcopy(self.test_instance)
        test_instance["display_name"] = "purple tomatoes"

        ctxt = context.RequestContext(project_id=123,
                                      project_name="aubergine",
                                      user_id=456,
                                      user_name="pie")

        flavor = objects.Flavor(name='m1.small',
                                memory_mb=6,
                                vcpus=28,
                                root_gb=496,
                                ephemeral_gb=8128,
                                swap=33550336,
                                extra_specs={})
        instance_ref = objects.Instance(**test_instance)
        instance_ref.flavor = flavor
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info,
                                     context=ctxt)

        self.assertEqual(cfg.uuid, instance_ref["uuid"])
        self.assertEqual(2, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeatureACPI)
        self.assertIsInstance(cfg.features[1],
                              vconfig.LibvirtConfigGuestFeatureAPIC)
        self.assertEqual(cfg.memory, 6 * units.Ki)
        self.assertEqual(cfg.vcpus, 28)
        self.assertEqual(cfg.os_type, vm_mode.HVM)
        self.assertEqual(cfg.os_boot_dev, ["hd"])
        self.assertIsNone(cfg.os_root)
        self.assertEqual(len(cfg.devices), 10)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[9],
                              vconfig.LibvirtConfigMemoryBalloon)
        self.assertEqual(len(cfg.metadata), 1)
        self.assertIsInstance(cfg.metadata[0],
                              vconfig.LibvirtConfigGuestMetaNovaInstance)
        self.assertEqual(version.version_string_with_package(),
                         cfg.metadata[0].package)
        self.assertEqual("purple tomatoes",
                         cfg.metadata[0].name)
        self.assertEqual(1234567.89,
                         cfg.metadata[0].creationTime)
        self.assertEqual("image",
                         cfg.metadata[0].roottype)
        self.assertEqual(str(instance_ref["image_ref"]),
                         cfg.metadata[0].rootid)

        self.assertIsInstance(cfg.metadata[0].owner,
                              vconfig.LibvirtConfigGuestMetaNovaOwner)
        self.assertEqual(456,
                         cfg.metadata[0].owner.userid)
        self.assertEqual("pie",
                         cfg.metadata[0].owner.username)
        self.assertEqual(123,
                         cfg.metadata[0].owner.projectid)
        self.assertEqual("aubergine",
                         cfg.metadata[0].owner.projectname)

        self.assertIsInstance(cfg.metadata[0].flavor,
                              vconfig.LibvirtConfigGuestMetaNovaFlavor)
        self.assertEqual("m1.small",
                         cfg.metadata[0].flavor.name)
        self.assertEqual(6,
                         cfg.metadata[0].flavor.memory)
        self.assertEqual(28,
                         cfg.metadata[0].flavor.vcpus)
        self.assertEqual(496,
                         cfg.metadata[0].flavor.disk)
        self.assertEqual(8128,
                         cfg.metadata[0].flavor.ephemeral)
        self.assertEqual(33550336,
                         cfg.metadata[0].flavor.swap)

    def test_get_guest_config_lxc(self):
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, {'mapping': {}})
        self.assertEqual(instance_ref["uuid"], cfg.uuid)
        self.assertEqual(instance_ref.flavor.memory_mb * units.Ki, cfg.memory)
        self.assertEqual(instance_ref.flavor.vcpus, cfg.vcpus)
        self.assertEqual(vm_mode.EXE, cfg.os_type)
        self.assertEqual("/sbin/init", cfg.os_init_path)
        self.assertEqual("console=tty0 console=ttyS0", cfg.os_cmdline)
        self.assertIsNone(cfg.os_root)
        self.assertEqual(3, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestFilesys)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestConsole)

    def test_get_guest_config_lxc_with_id_maps(self):
        self.flags(virt_type='lxc', group='libvirt')
        self.flags(uid_maps=['0:1000:100'], group='libvirt')
        self.flags(gid_maps=['0:1000:100'], group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, {'mapping': {}})
        self.assertEqual(instance_ref["uuid"], cfg.uuid)
        self.assertEqual(instance_ref.flavor.memory_mb * units.Ki, cfg.memory)
        self.assertEqual(instance_ref.vcpus, cfg.vcpus)
        self.assertEqual(vm_mode.EXE, cfg.os_type)
        self.assertEqual("/sbin/init", cfg.os_init_path)
        self.assertEqual("console=tty0 console=ttyS0", cfg.os_cmdline)
        self.assertIsNone(cfg.os_root)
        self.assertEqual(3, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestFilesys)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestConsole)
        self.assertEqual(len(cfg.idmaps), 2)
        self.assertIsInstance(cfg.idmaps[0],
                              vconfig.LibvirtConfigGuestUIDMap)
        self.assertIsInstance(cfg.idmaps[1],
                              vconfig.LibvirtConfigGuestGIDMap)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_fits(self, is_able):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=1, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps)):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertIsNone(cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.cpu.numa)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_no_fit(self, is_able):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=4096, vcpus=4, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set', return_value=set([3])),
                mock.patch.object(random, 'choice')
            ) as (get_host_cap_mock,
                  get_vcpu_pin_set_mock, choice_mock):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertFalse(choice_mock.called)
            self.assertEqual(set([3]), cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.cpu.numa)

    def _test_get_guest_memory_backing_config(
            self, host_topology, inst_topology, numatune):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        with mock.patch.object(
                drvr, "_get_host_numa_topology",
                return_value=host_topology):
            return drvr._get_guest_memory_backing_config(
                inst_topology, numatune, {})

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_get_guest_memory_backing_config_large_success(self, mock_version):
        host_topology = objects.NUMATopology(
            cells=[
                objects.NUMACell(
                    id=3, cpuset=set([1]), memory=1024, mempages=[
                        objects.NUMAPagesTopology(size_kb=4, total=2000,
                                                  used=0),
                        objects.NUMAPagesTopology(size_kb=2048, total=512,
                                                  used=0),
                        objects.NUMAPagesTopology(size_kb=1048576, total=0,
                                                  used=0),
                    ])])
        inst_topology = objects.InstanceNUMATopology(cells=[
            objects.InstanceNUMACell(
                id=3, cpuset=set([0, 1]), memory=1024, pagesize=2048)])

        numa_tune = vconfig.LibvirtConfigGuestNUMATune()
        numa_tune.memnodes = [vconfig.LibvirtConfigGuestNUMATuneMemNode()]
        numa_tune.memnodes[0].cellid = 0
        numa_tune.memnodes[0].nodeset = [3]

        result = self._test_get_guest_memory_backing_config(
            host_topology, inst_topology, numa_tune)
        self.assertEqual(1, len(result.hugepages))
        self.assertEqual(2048, result.hugepages[0].size_kb)
        self.assertEqual([0], result.hugepages[0].nodeset)

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_get_guest_memory_backing_config_smallest(self, mock_version):
        host_topology = objects.NUMATopology(
            cells=[
                objects.NUMACell(
                    id=3, cpuset=set([1]), memory=1024, mempages=[
                        objects.NUMAPagesTopology(size_kb=4, total=2000,
                                                  used=0),
                        objects.NUMAPagesTopology(size_kb=2048, total=512,
                                                  used=0),
                        objects.NUMAPagesTopology(size_kb=1048576, total=0,
                                                  used=0),
                    ])])
        inst_topology = objects.InstanceNUMATopology(cells=[
            objects.InstanceNUMACell(
                id=3, cpuset=set([0, 1]), memory=1024, pagesize=4)])

        numa_tune = vconfig.LibvirtConfigGuestNUMATune()
        numa_tune.memnodes = [vconfig.LibvirtConfigGuestNUMATuneMemNode()]
        numa_tune.memnodes[0].cellid = 0
        numa_tune.memnodes[0].nodeset = [3]

        result = self._test_get_guest_memory_backing_config(
            host_topology, inst_topology, numa_tune)
        self.assertIsNone(result)

    def test_get_guest_memory_backing_config_realtime(self):
        flavor = {"extra_specs": {
            "hw:cpu_realtime": "yes",
            "hw:cpu_policy": "dedicated"
        }}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        membacking = drvr._get_guest_memory_backing_config(
            None, None, flavor)
        self.assertTrue(membacking.locked)
        self.assertFalse(membacking.sharedpages)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_pci_no_numa_info(
            self, is_able):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=1, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        pci_device_info = dict(test_pci_device.fake_db_dev)
        pci_device_info.update(compute_node_id=1,
                               label='fake',
                               status=fields.PciDeviceStatus.AVAILABLE,
                               address='0000:00:00.1',
                               instance_uuid=None,
                               request_id=None,
                               extra_info={},
                               numa_node=None)
        pci_device = objects.PciDevice(**pci_device_info)

        with test.nested(
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(
                    host.Host, "get_capabilities", return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set', return_value=set([3])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                mock.patch.object(pci_manager, "get_instance_pci_devs",
                                  return_value=[pci_device])):
            cfg = conn._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertEqual(set([3]), cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.cpu.numa)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_2pci_no_fit(self, is_able):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=4096, vcpus=4, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        pci_device_info = dict(test_pci_device.fake_db_dev)
        pci_device_info.update(compute_node_id=1,
                               label='fake',
                               status=fields.PciDeviceStatus.AVAILABLE,
                               address='0000:00:00.1',
                               instance_uuid=None,
                               request_id=None,
                               extra_info={},
                               numa_node=1)
        pci_device = objects.PciDevice(**pci_device_info)
        pci_device_info.update(numa_node=0, address='0000:00:00.2')
        pci_device2 = objects.PciDevice(**pci_device_info)
        with test.nested(
                mock.patch.object(
                    host.Host, "get_capabilities", return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set', return_value=set([3])),
                mock.patch.object(random, 'choice'),
                mock.patch.object(pci_manager, "get_instance_pci_devs",
                                  return_value=[pci_device, pci_device2])
            ) as (get_host_cap_mock,
                  get_vcpu_pin_set_mock, choice_mock, pci_mock):
            cfg = conn._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertFalse(choice_mock.called)
            self.assertEqual(set([3]), cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.cpu.numa)

    @mock.patch.object(fakelibvirt.Connection, 'getType')
    @mock.patch.object(fakelibvirt.Connection, 'getVersion')
    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion')
    @mock.patch.object(host.Host, 'get_capabilities')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_set_host_enabled')
    def _test_get_guest_config_numa_unsupported(self, fake_lib_version,
                                                fake_version, fake_type,
                                                fake_arch, exception_class,
                                                pagesize, mock_host,
                                                mock_caps, mock_lib_version,
                                                mock_version, mock_type):
        instance_topology = objects.InstanceNUMATopology(
                    cells=[objects.InstanceNUMACell(
                        id=0, cpuset=set([0]),
                        memory=1024, pagesize=pagesize)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=1, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = fake_arch
        caps.host.topology = self._fake_caps_numa_topology()

        mock_type.return_value = fake_type
        mock_version.return_value = fake_version
        mock_lib_version.return_value = fake_lib_version
        mock_caps.return_value = caps

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.assertRaises(exception_class,
                          drvr._get_guest_config,
                          instance_ref, [],
                          image_meta, disk_info)

    def test_get_guest_config_numa_old_version_libvirt(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION) - 1,
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION),
            host.HV_DRIVER_QEMU,
            arch.X86_64,
            exception.NUMATopologyUnsupported,
            None)

    def test_get_guest_config_numa_old_version_libvirt_ppc(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION_PPC) - 1,
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION),
            host.HV_DRIVER_QEMU,
            arch.PPC64LE,
            exception.NUMATopologyUnsupported,
            None)

    def test_get_guest_config_numa_bad_version_libvirt(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.BAD_LIBVIRT_NUMA_VERSIONS[0]),
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION),
            host.HV_DRIVER_QEMU,
            arch.X86_64,
            exception.NUMATopologyUnsupported,
            None)

    @mock.patch.object(libvirt_driver.LOG, 'warning')
    def test_has_numa_support_bad_version_libvirt_log(self, mock_warn):
        # Tests that a warning is logged once and only once when there is a bad
        # BAD_LIBVIRT_NUMA_VERSIONS detected.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertFalse(hasattr(drvr, '_bad_libvirt_numa_version_warn'))
        with mock.patch.object(drvr._host, 'has_version', return_value=True):
            for i in range(2):
                self.assertFalse(drvr._has_numa_support())
        self.assertTrue(drvr._bad_libvirt_numa_version_warn)
        self.assertEqual(1, mock_warn.call_count)
        # assert the version is logged properly
        self.assertEqual('1.2.9.2', mock_warn.call_args[0][1])

    def test_get_guest_config_numa_old_version_qemu(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION),
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION) - 1,
            host.HV_DRIVER_QEMU,
            arch.X86_64,
            exception.NUMATopologyUnsupported,
            None)

    def test_get_guest_config_numa_other_arch_qemu(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION),
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION),
            host.HV_DRIVER_QEMU,
            arch.S390,
            exception.NUMATopologyUnsupported,
            None)

    def test_get_guest_config_numa_xen(self):
        self.flags(virt_type='xen', group='libvirt')
        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION),
            versionutils.convert_version_to_int((4, 5, 0)),
            'XEN',
            arch.X86_64,
            exception.NUMATopologyUnsupported,
            None)

    def test_get_guest_config_numa_old_pages_libvirt(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_HUGEPAGE_VERSION) - 1,
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION),
            host.HV_DRIVER_QEMU,
            arch.X86_64,
            exception.MemoryPagesUnsupported,
            2048)

    def test_get_guest_config_numa_old_pages_qemu(self):
        self.flags(virt_type='kvm', group='libvirt')

        self._test_get_guest_config_numa_unsupported(
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_HUGEPAGE_VERSION),
            versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION) - 1,
            host.HV_DRIVER_QEMU,
            arch.X86_64,
            exception.NUMATopologyUnsupported,
            2048)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_fit_w_cpu_pinset(
            self, is_able):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=1024, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology(kb_mem=4194304)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set', return_value=set([2, 3])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8)))
                ) as (has_min_version_mock, get_host_cap_mock,
                        get_vcpu_pin_set_mock, get_online_cpus_mock):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            # NOTE(ndipanov): we make sure that pin_set was taken into account
            # when choosing viable cells
            self.assertEqual(set([2, 3]), cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.cpu.numa)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_non_numa_host_instance_topo(self, is_able):
        instance_topology = objects.InstanceNUMATopology(
                    cells=[objects.InstanceNUMACell(
                        id=0, cpuset=set([0]), memory=1024),
                           objects.InstanceNUMACell(
                        id=1, cpuset=set([2]), memory=1024)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = None

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps)):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertIsNone(cfg.cpuset)
            self.assertEqual(0, len(cfg.cputune.vcpupin))
            self.assertIsNone(cfg.numatune)
            self.assertIsNotNone(cfg.cpu.numa)
            for instance_cell, numa_cfg_cell in zip(
                    instance_topology.cells, cfg.cpu.numa.cells):
                self.assertEqual(instance_cell.id, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_numa_host_instance_topo(self, is_able):
        instance_topology = objects.InstanceNUMATopology(
                    cells=[objects.InstanceNUMACell(
                        id=1, cpuset=set([0, 1]), memory=1024, pagesize=None),
                           objects.InstanceNUMACell(
                               id=2, cpuset=set([2, 3]), memory=1024,
                               pagesize=None)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=4, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set',
                    return_value=set([2, 3, 4, 5])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                ):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertIsNone(cfg.cpuset)
            # Test that the pinning is correct and limited to allowed only
            self.assertEqual(0, cfg.cputune.vcpupin[0].id)
            self.assertEqual(set([2, 3]), cfg.cputune.vcpupin[0].cpuset)
            self.assertEqual(1, cfg.cputune.vcpupin[1].id)
            self.assertEqual(set([2, 3]), cfg.cputune.vcpupin[1].cpuset)
            self.assertEqual(2, cfg.cputune.vcpupin[2].id)
            self.assertEqual(set([4, 5]), cfg.cputune.vcpupin[2].cpuset)
            self.assertEqual(3, cfg.cputune.vcpupin[3].id)
            self.assertEqual(set([4, 5]), cfg.cputune.vcpupin[3].cpuset)
            self.assertIsNotNone(cfg.cpu.numa)

            self.assertIsInstance(cfg.cputune.emulatorpin,
                                  vconfig.LibvirtConfigGuestCPUTuneEmulatorPin)
            self.assertEqual(set([2, 3, 4, 5]), cfg.cputune.emulatorpin.cpuset)

            for instance_cell, numa_cfg_cell, index in zip(
                    instance_topology.cells,
                    cfg.cpu.numa.cells,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)

            allnodes = [cell.id for cell in instance_topology.cells]
            self.assertEqual(allnodes, cfg.numatune.memory.nodeset)
            self.assertEqual("strict", cfg.numatune.memory.mode)

            for instance_cell, memnode, index in zip(
                    instance_topology.cells,
                    cfg.numatune.memnodes,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, memnode.cellid)
                self.assertEqual([instance_cell.id], memnode.nodeset)
                self.assertEqual("strict", memnode.mode)

    def test_get_guest_config_numa_host_instance_topo_reordered(self):
        instance_topology = objects.InstanceNUMATopology(
                    cells=[objects.InstanceNUMACell(
                        id=3, cpuset=set([0, 1]), memory=1024),
                           objects.InstanceNUMACell(
                        id=0, cpuset=set([2, 3]), memory=1024)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=4, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                ):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertIsNone(cfg.cpuset)
            # Test that the pinning is correct and limited to allowed only
            self.assertEqual(0, cfg.cputune.vcpupin[0].id)
            self.assertEqual(set([6, 7]), cfg.cputune.vcpupin[0].cpuset)
            self.assertEqual(1, cfg.cputune.vcpupin[1].id)
            self.assertEqual(set([6, 7]), cfg.cputune.vcpupin[1].cpuset)
            self.assertEqual(2, cfg.cputune.vcpupin[2].id)
            self.assertEqual(set([0, 1]), cfg.cputune.vcpupin[2].cpuset)
            self.assertEqual(3, cfg.cputune.vcpupin[3].id)
            self.assertEqual(set([0, 1]), cfg.cputune.vcpupin[3].cpuset)
            self.assertIsNotNone(cfg.cpu.numa)

            self.assertIsInstance(cfg.cputune.emulatorpin,
                                  vconfig.LibvirtConfigGuestCPUTuneEmulatorPin)
            self.assertEqual(set([0, 1, 6, 7]), cfg.cputune.emulatorpin.cpuset)

            for index, (instance_cell, numa_cfg_cell) in enumerate(zip(
                    instance_topology.cells,
                    cfg.cpu.numa.cells)):
                self.assertEqual(index, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)
                self.assertIsNone(numa_cfg_cell.memAccess)

            allnodes = set([cell.id for cell in instance_topology.cells])
            self.assertEqual(allnodes, set(cfg.numatune.memory.nodeset))
            self.assertEqual("strict", cfg.numatune.memory.mode)

            for index, (instance_cell, memnode) in enumerate(zip(
                    instance_topology.cells,
                    cfg.numatune.memnodes)):
                self.assertEqual(index, memnode.cellid)
                self.assertEqual([instance_cell.id], memnode.nodeset)
                self.assertEqual("strict", memnode.mode)

    def test_get_guest_config_numa_host_instance_topo_cpu_pinning(self):
        instance_topology = objects.InstanceNUMATopology(
                    cells=[objects.InstanceNUMACell(
                        id=1, cpuset=set([0, 1]), memory=1024,
                        cpu_pinning={0: 24, 1: 25}),
                           objects.InstanceNUMACell(
                        id=0, cpuset=set([2, 3]), memory=1024,
                        cpu_pinning={2: 0, 3: 1})])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology(
            sockets_per_cell=4, cores_per_socket=3, threads_per_core=2)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                ):
            cfg = conn._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            self.assertIsNone(cfg.cpuset)
            # Test that the pinning is correct and limited to allowed only
            self.assertEqual(0, cfg.cputune.vcpupin[0].id)
            self.assertEqual(set([24]), cfg.cputune.vcpupin[0].cpuset)
            self.assertEqual(1, cfg.cputune.vcpupin[1].id)
            self.assertEqual(set([25]), cfg.cputune.vcpupin[1].cpuset)
            self.assertEqual(2, cfg.cputune.vcpupin[2].id)
            self.assertEqual(set([0]), cfg.cputune.vcpupin[2].cpuset)
            self.assertEqual(3, cfg.cputune.vcpupin[3].id)
            self.assertEqual(set([1]), cfg.cputune.vcpupin[3].cpuset)
            self.assertIsNotNone(cfg.cpu.numa)

            # Emulator must be pinned to union of cfg.cputune.vcpupin[*].cpuset
            self.assertIsInstance(cfg.cputune.emulatorpin,
                                  vconfig.LibvirtConfigGuestCPUTuneEmulatorPin)
            self.assertEqual(set([0, 1, 24, 25]),
                             cfg.cputune.emulatorpin.cpuset)

            for i, (instance_cell, numa_cfg_cell) in enumerate(zip(
                    instance_topology.cells, cfg.cpu.numa.cells)):
                self.assertEqual(i, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)
                self.assertIsNone(numa_cfg_cell.memAccess)

            allnodes = set([cell.id for cell in instance_topology.cells])
            self.assertEqual(allnodes, set(cfg.numatune.memory.nodeset))
            self.assertEqual("strict", cfg.numatune.memory.mode)

            for i, (instance_cell, memnode) in enumerate(zip(
                    instance_topology.cells, cfg.numatune.memnodes)):
                self.assertEqual(i, memnode.cellid)
                self.assertEqual([instance_cell.id], memnode.nodeset)
                self.assertEqual("strict", memnode.mode)

    def test_get_guest_config_numa_host_mempages_shared(self):
        instance_topology = objects.InstanceNUMATopology(
            cells=[
                objects.InstanceNUMACell(
                    id=1, cpuset=set([0, 1]),
                    memory=1024, pagesize=2048),
                objects.InstanceNUMACell(
                    id=2, cpuset=set([2, 3]),
                    memory=1024, pagesize=2048)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=4, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={})
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set',
                    return_value=set([2, 3, 4, 5])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                ):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)

            for instance_cell, numa_cfg_cell, index in zip(
                    instance_topology.cells,
                    cfg.cpu.numa.cells,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)
                self.assertEqual("shared", numa_cfg_cell.memAccess)

            allnodes = [cell.id for cell in instance_topology.cells]
            self.assertEqual(allnodes, cfg.numatune.memory.nodeset)
            self.assertEqual("strict", cfg.numatune.memory.mode)

            for instance_cell, memnode, index in zip(
                    instance_topology.cells,
                    cfg.numatune.memnodes,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, memnode.cellid)
                self.assertEqual([instance_cell.id], memnode.nodeset)
                self.assertEqual("strict", memnode.mode)

            self.assertEqual(0, len(cfg.cputune.vcpusched))
            self.assertEqual(set([2, 3, 4, 5]), cfg.cputune.emulatorpin.cpuset)

    def test_get_guest_config_numa_host_instance_cpu_pinning_realtime(self):
        instance_topology = objects.InstanceNUMATopology(
            cells=[
                objects.InstanceNUMACell(
                    id=1, cpuset=set([0, 1]),
                    memory=1024, pagesize=2048),
                objects.InstanceNUMACell(
                    id=2, cpuset=set([2, 3]),
                    memory=1024, pagesize=2048)])
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.numa_topology = instance_topology
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = objects.Flavor(memory_mb=2048, vcpus=2, root_gb=496,
                                ephemeral_gb=8128, swap=33550336, name='fake',
                                extra_specs={
                                    "hw:cpu_realtime": "yes",
                                    "hw:cpu_policy": "dedicated",
                                    "hw:cpu_realtime_mask": "^0-1"
                                })
        instance_ref.flavor = flavor

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with test.nested(
                mock.patch.object(
                    objects.InstanceNUMATopology, "get_by_instance_uuid",
                    return_value=instance_topology),
                mock.patch.object(host.Host, 'has_min_version',
                                  return_value=True),
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set',
                    return_value=set([2, 3, 4, 5])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set(range(8))),
                ):
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)

            for instance_cell, numa_cfg_cell, index in zip(
                    instance_topology.cells,
                    cfg.cpu.numa.cells,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, numa_cfg_cell.id)
                self.assertEqual(instance_cell.cpuset, numa_cfg_cell.cpus)
                self.assertEqual(instance_cell.memory * units.Ki,
                                 numa_cfg_cell.memory)
                self.assertEqual("shared", numa_cfg_cell.memAccess)

            allnodes = [cell.id for cell in instance_topology.cells]
            self.assertEqual(allnodes, cfg.numatune.memory.nodeset)
            self.assertEqual("strict", cfg.numatune.memory.mode)

            for instance_cell, memnode, index in zip(
                    instance_topology.cells,
                    cfg.numatune.memnodes,
                    range(len(instance_topology.cells))):
                self.assertEqual(index, memnode.cellid)
                self.assertEqual([instance_cell.id], memnode.nodeset)
                self.assertEqual("strict", memnode.mode)

            self.assertEqual(1, len(cfg.cputune.vcpusched))
            self.assertEqual("fifo", cfg.cputune.vcpusched[0].scheduler)
            self.assertEqual(set([2, 3]), cfg.cputune.vcpusched[0].vcpus)
            self.assertEqual(set([0, 1]), cfg.cputune.emulatorpin.cpuset)

    def test_get_cpu_numa_config_from_instance(self):
        topology = objects.InstanceNUMATopology(cells=[
            objects.InstanceNUMACell(id=0, cpuset=set([1, 2]), memory=128),
            objects.InstanceNUMACell(id=1, cpuset=set([3, 4]), memory=128),
        ])
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        conf = drvr._get_cpu_numa_config_from_instance(topology, True)

        self.assertIsInstance(conf, vconfig.LibvirtConfigGuestCPUNUMA)
        self.assertEqual(0, conf.cells[0].id)
        self.assertEqual(set([1, 2]), conf.cells[0].cpus)
        self.assertEqual(131072, conf.cells[0].memory)
        self.assertEqual("shared", conf.cells[0].memAccess)
        self.assertEqual(1, conf.cells[1].id)
        self.assertEqual(set([3, 4]), conf.cells[1].cpus)
        self.assertEqual(131072, conf.cells[1].memory)
        self.assertEqual("shared", conf.cells[1].memAccess)

    def test_get_cpu_numa_config_from_instance_none(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        conf = drvr._get_cpu_numa_config_from_instance(None, False)
        self.assertIsNone(conf)

    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_numa_support",
                       return_value=True)
    def test_get_memnode_numa_config_from_instance(self, mock_numa):
        instance_topology = objects.InstanceNUMATopology(cells=[
            objects.InstanceNUMACell(id=0, cpuset=set([1, 2]), memory=128),
            objects.InstanceNUMACell(id=1, cpuset=set([3, 4]), memory=128),
            objects.InstanceNUMACell(id=16, cpuset=set([5, 6]), memory=128)
        ])

        host_topology = objects.NUMATopology(
            cells=[
                objects.NUMACell(
                    id=0, cpuset=set([1, 2]), memory=1024, mempages=[]),
                objects.NUMACell(
                    id=1, cpuset=set([3, 4]), memory=1024, mempages=[]),
                objects.NUMACell(
                    id=16, cpuset=set([5, 6]), memory=1024, mempages=[])])

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        with test.nested(
                mock.patch.object(drvr, "_get_host_numa_topology",
                                  return_value=host_topology)):
            guest_numa_config = drvr._get_guest_numa_config(instance_topology,
                flavor={}, allowed_cpus=[1, 2, 3, 4, 5, 6], image_meta={})
            self.assertEqual(2, guest_numa_config.numatune.memnodes[2].cellid)
            self.assertEqual([16],
                guest_numa_config.numatune.memnodes[2].nodeset)
            self.assertEqual(set([5, 6]),
                guest_numa_config.numaconfig.cells[2].cpus)

    @mock.patch.object(host.Host, 'has_version', return_value=True)
    def test_has_cpu_policy_support(self, mock_has_version):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertRaises(exception.CPUPinningNotSupported,
                          drvr._has_cpu_policy_support)

    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_numa_support",
                       return_value=True)
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_hugepage_support",
                       return_value=True)
    @mock.patch.object(host.Host, "get_capabilities")
    def test_does_not_want_hugepages(self, mock_caps, mock_hp, mock_numa):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_topology = objects.InstanceNUMATopology(
            cells=[
                objects.InstanceNUMACell(
                    id=1, cpuset=set([0, 1]),
                    memory=1024, pagesize=4),
                objects.InstanceNUMACell(
                    id=2, cpuset=set([2, 3]),
                    memory=1024, pagesize=4)])

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        mock_caps.return_value = caps

        host_topology = drvr._get_host_numa_topology()

        self.assertFalse(drvr._wants_hugepages(None, None))
        self.assertFalse(drvr._wants_hugepages(host_topology, None))
        self.assertFalse(drvr._wants_hugepages(None, instance_topology))
        self.assertFalse(drvr._wants_hugepages(host_topology,
                                               instance_topology))

    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_numa_support",
                       return_value=True)
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_hugepage_support",
                       return_value=True)
    @mock.patch.object(host.Host, "get_capabilities")
    def test_does_want_hugepages(self, mock_caps, mock_hp, mock_numa):
        self.flags(reserved_huge_pages=[
            {'node': 0, 'size': 2048, 'count': 128},
            {'node': 1, 'size': 2048, 'count': 1},
            {'node': 3, 'size': 2048, 'count': 64}])
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_topology = objects.InstanceNUMATopology(
            cells=[
                objects.InstanceNUMACell(
                    id=1, cpuset=set([0, 1]),
                    memory=1024, pagesize=2048),
                objects.InstanceNUMACell(
                    id=2, cpuset=set([2, 3]),
                    memory=1024, pagesize=2048)])

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        mock_caps.return_value = caps

        host_topology = drvr._get_host_numa_topology()
        self.assertEqual(128, host_topology.cells[0].mempages[1].reserved)
        self.assertEqual(1, host_topology.cells[1].mempages[1].reserved)
        self.assertEqual(0, host_topology.cells[2].mempages[1].reserved)
        self.assertEqual(64, host_topology.cells[3].mempages[1].reserved)

        self.assertTrue(drvr._wants_hugepages(host_topology,
                                              instance_topology))

    def test_get_guest_config_clock(self):
        self.flags(virt_type='kvm', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        hpet_map = {
            arch.X86_64: True,
            arch.I686: True,
            arch.PPC: False,
            arch.PPC64: False,
            arch.ARMV7: False,
            arch.AARCH64: False,
            }

        for guestarch, expect_hpet in hpet_map.items():
            with mock.patch.object(libvirt_driver.libvirt_utils,
                                   'get_arch',
                                   return_value=guestarch):
                cfg = drvr._get_guest_config(instance_ref, [],
                                             image_meta,
                                             disk_info)
                self.assertIsInstance(cfg.clock,
                                      vconfig.LibvirtConfigGuestClock)
                self.assertEqual(cfg.clock.offset, "utc")
                self.assertIsInstance(cfg.clock.timers[0],
                                      vconfig.LibvirtConfigGuestTimer)
                self.assertIsInstance(cfg.clock.timers[1],
                                      vconfig.LibvirtConfigGuestTimer)
                self.assertEqual(cfg.clock.timers[0].name, "pit")
                self.assertEqual(cfg.clock.timers[0].tickpolicy,
                                      "delay")
                self.assertEqual(cfg.clock.timers[1].name, "rtc")
                self.assertEqual(cfg.clock.timers[1].tickpolicy,
                                      "catchup")
                if expect_hpet:
                    self.assertEqual(3, len(cfg.clock.timers))
                    self.assertIsInstance(cfg.clock.timers[2],
                                          vconfig.LibvirtConfigGuestTimer)
                    self.assertEqual('hpet', cfg.clock.timers[2].name)
                    self.assertFalse(cfg.clock.timers[2].present)
                else:
                    self.assertEqual(2, len(cfg.clock.timers))

    @mock.patch.object(libvirt_utils, 'get_arch')
    @mock.patch.object(host.Host, 'has_min_version')
    def test_get_guest_config_windows(self, mock_version, mock_get_arch):
        mock_version.return_value = False
        mock_get_arch.return_value = arch.I686
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref['os_type'] = 'windows'
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)

        self.assertIsInstance(cfg.clock,
                              vconfig.LibvirtConfigGuestClock)
        self.assertEqual(cfg.clock.offset, "localtime")

        self.assertEqual(3, len(cfg.clock.timers), cfg.clock.timers)
        self.assertEqual("pit", cfg.clock.timers[0].name)
        self.assertEqual("rtc", cfg.clock.timers[1].name)
        self.assertEqual("hpet", cfg.clock.timers[2].name)
        self.assertFalse(cfg.clock.timers[2].present)

    @mock.patch.object(libvirt_utils, 'get_arch')
    @mock.patch.object(host.Host, 'has_min_version')
    def test_get_guest_config_windows_timer(self, mock_version, mock_get_arch):
        mock_version.return_value = True
        mock_get_arch.return_value = arch.I686
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref['os_type'] = 'windows'
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)

        self.assertIsInstance(cfg.clock,
                              vconfig.LibvirtConfigGuestClock)
        self.assertEqual(cfg.clock.offset, "localtime")

        self.assertEqual(4, len(cfg.clock.timers), cfg.clock.timers)
        self.assertEqual("pit", cfg.clock.timers[0].name)
        self.assertEqual("rtc", cfg.clock.timers[1].name)
        self.assertEqual("hpet", cfg.clock.timers[2].name)
        self.assertFalse(cfg.clock.timers[2].present)
        self.assertEqual("hypervclock", cfg.clock.timers[3].name)
        self.assertTrue(cfg.clock.timers[3].present)

        self.assertEqual(3, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeatureACPI)
        self.assertIsInstance(cfg.features[1],
                              vconfig.LibvirtConfigGuestFeatureAPIC)
        self.assertIsInstance(cfg.features[2],
                              vconfig.LibvirtConfigGuestFeatureHyperV)

    @mock.patch.object(host.Host, 'has_min_version')
    def test_get_guest_config_windows_hyperv_feature2(self, mock_version):
        mock_version.return_value = True
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref['os_type'] = 'windows'
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)

        self.assertIsInstance(cfg.clock,
                              vconfig.LibvirtConfigGuestClock)
        self.assertEqual(cfg.clock.offset, "localtime")

        self.assertEqual(3, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeatureACPI)
        self.assertIsInstance(cfg.features[1],
                              vconfig.LibvirtConfigGuestFeatureAPIC)
        self.assertIsInstance(cfg.features[2],
                              vconfig.LibvirtConfigGuestFeatureHyperV)

        self.assertTrue(cfg.features[2].relaxed)
        self.assertTrue(cfg.features[2].spinlocks)
        self.assertEqual(8191, cfg.features[2].spinlock_retries)
        self.assertTrue(cfg.features[2].vapic)

    def test_get_guest_config_with_two_nics(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 2),
                                     image_meta, disk_info)
        self.assertEqual(2, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeatureACPI)
        self.assertIsInstance(cfg.features[1],
                              vconfig.LibvirtConfigGuestFeatureAPIC)
        self.assertEqual(cfg.memory, instance_ref.flavor.memory_mb * units.Ki)
        self.assertEqual(cfg.vcpus, instance_ref.flavor.vcpus)
        self.assertEqual(cfg.os_type, vm_mode.HVM)
        self.assertEqual(cfg.os_boot_dev, ["hd"])
        self.assertIsNone(cfg.os_root)
        self.assertEqual(len(cfg.devices), 10)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[9],
                              vconfig.LibvirtConfigMemoryBalloon)

    def test_get_guest_config_bug_1118829(self):
        self.flags(virt_type='uml', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)

        disk_info = {'disk_bus': 'virtio',
                     'cdrom_bus': 'ide',
                     'mapping': {u'vda': {'bus': 'virtio',
                                          'type': 'disk',
                                          'dev': u'vda'},
                                 'root': {'bus': 'virtio',
                                          'type': 'disk',
                                          'dev': 'vda'}}}

        # NOTE(jdg): For this specific test leave this blank
        # This will exercise the failed code path still,
        # and won't require fakes and stubs of the iscsi discovery
        block_device_info = {}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        drvr._get_guest_config(instance_ref, [], image_meta, disk_info,
                               None, block_device_info)
        self.assertEqual(instance_ref['root_device_name'], '/dev/vda')

    def test_get_guest_config_with_root_device_name(self):
        self.flags(virt_type='uml', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        block_device_info = {'root_device_name': '/dev/vdb'}
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            block_device_info)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info,
                                     None, block_device_info)
        self.assertEqual(0, len(cfg.features))
        self.assertEqual(cfg.memory, instance_ref.flavor.memory_mb * units.Ki)
        self.assertEqual(cfg.vcpus, instance_ref.flavor.vcpus)
        self.assertEqual(cfg.os_type, "uml")
        self.assertEqual(cfg.os_boot_dev, [])
        self.assertEqual(cfg.os_root, '/dev/vdb')
        self.assertEqual(len(cfg.devices), 3)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestConsole)

    def test_has_uefi_support_with_invalid_version(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        with mock.patch.object(drvr._host,
                               'has_min_version', return_value=False):
            self.assertFalse(drvr._has_uefi_support())

    def test_has_uefi_support_not_supported_arch(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "alpha"
        self.assertFalse(drvr._has_uefi_support())

    @mock.patch('os.path.exists', return_value=False)
    def test_has_uefi_support_with_no_loader_existed(self, mock_exist):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.assertFalse(drvr._has_uefi_support())

    @mock.patch('os.path.exists', return_value=True)
    def test_has_uefi_support(self, mock_has_version):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"

        with mock.patch.object(drvr._host,
                               'has_min_version', return_value=True):
            self.assertTrue(drvr._has_uefi_support())

    def test_get_guest_config_with_uefi(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_firmware_type": "uefi"}})
        instance_ref = objects.Instance(**self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        with mock.patch.object(drvr, "_has_uefi_support",
                               return_value=True) as mock_support:
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info)
            mock_support.assert_called_once_with()
            self.assertEqual(cfg.os_loader_type, "pflash")

    def test_get_guest_config_with_block_device(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        conn_info = {'driver_volume_type': 'fake'}
        bdms = block_device_obj.block_device_make_list_from_dicts(
            self.context, [
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 1,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/vdc'}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 2,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/vdd'}),
            ]
        )
        info = {'block_device_mapping': driver_block_device.convert_volumes(
            bdms
        )}
        info['block_device_mapping'][0]['connection_info'] = conn_info
        info['block_device_mapping'][1]['connection_info'] = conn_info

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            info)
        with mock.patch.object(
                driver_block_device.DriverVolumeBlockDevice, 'save'
        ) as mock_save:
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info,
                                         None, info)
            self.assertIsInstance(cfg.devices[2],
                                  vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[2].target_dev, 'vdc')
            self.assertIsInstance(cfg.devices[3],
                                  vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[3].target_dev, 'vdd')
            mock_save.assert_called_with()

    def test_get_guest_config_lxc_with_attached_volume(self):
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        conn_info = {'driver_volume_type': 'fake'}
        bdms = block_device_obj.block_device_make_list_from_dicts(
            self.context, [
              fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 1,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'boot_index': 0}),
              fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 2,
                     'source_type': 'volume', 'destination_type': 'volume',
                    }),
              fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 3,
                     'source_type': 'volume', 'destination_type': 'volume',
                    }),
           ]
        )
        info = {'block_device_mapping': driver_block_device.convert_volumes(
            bdms
        )}

        info['block_device_mapping'][0]['connection_info'] = conn_info
        info['block_device_mapping'][1]['connection_info'] = conn_info
        info['block_device_mapping'][2]['connection_info'] = conn_info
        info['block_device_mapping'][0]['mount_device'] = '/dev/vda'
        info['block_device_mapping'][1]['mount_device'] = '/dev/vdc'
        info['block_device_mapping'][2]['mount_device'] = '/dev/vdd'
        with mock.patch.object(
                driver_block_device.DriverVolumeBlockDevice, 'save'
        ) as mock_save:
            disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                                instance_ref,
                                                image_meta,
                                                info)
            cfg = drvr._get_guest_config(instance_ref, [],
                                         image_meta, disk_info,
                                         None, info)
            self.assertIsInstance(cfg.devices[1],
                                  vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[1].target_dev, 'vdc')
            self.assertIsInstance(cfg.devices[2],
                                  vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[2].target_dev, 'vdd')
            mock_save.assert_called_with()

    def test_get_guest_config_with_configdrive(self):
        # It's necessary to check if the architecture is power, because
        # power doesn't have support to ide, and so libvirt translate
        # all ide calls to scsi

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        # make configdrive.required_by() return True
        instance_ref['config_drive'] = True

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        # The last device is selected for this. on x86 is the last ide
        # device (hdd). Since power only support scsi, the last device
        # is sdz

        expect = {"ppc": "sdz", "ppc64": "sdz",
                    "ppc64le": "sdz", "aarch64": "sdz"}
        disk = expect.get(blockinfo.libvirt_utils.get_arch({}), "hdd")
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertEqual(cfg.devices[2].target_dev, disk)

    def test_get_guest_config_with_virtio_scsi_bus(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_scsi_model": "virtio-scsi"}})
        instance_ref = objects.Instance(**self.test_instance)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            [])
        cfg = drvr._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertIsInstance(cfg.devices[0],
                         vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                         vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                         vconfig.LibvirtConfigGuestController)
        self.assertEqual(cfg.devices[2].model, 'virtio-scsi')

    def test_get_guest_config_with_virtio_scsi_bus_bdm(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_scsi_model": "virtio-scsi"}})
        instance_ref = objects.Instance(**self.test_instance)
        conn_info = {'driver_volume_type': 'fake'}
        bdms = block_device_obj.block_device_make_list_from_dicts(
            self.context, [
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 1,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/sdc', 'disk_bus': 'scsi'}),
                fake_block_device.FakeDbBlockDeviceDict(
                    {'id': 2,
                     'source_type': 'volume', 'destination_type': 'volume',
                     'device_name': '/dev/sdd', 'disk_bus': 'scsi'}),
                ]
        )
        bd_info = {
            'block_device_mapping': driver_block_device.convert_volumes(bdms)}
        bd_info['block_device_mapping'][0]['connection_info'] = conn_info
        bd_info['block_device_mapping'][1]['connection_info'] = conn_info

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            bd_info)
        with mock.patch.object(
                driver_block_device.DriverVolumeBlockDevice, 'save'
        ) as mock_save:
            cfg = drvr._get_guest_config(instance_ref, [], image_meta,
                    disk_info, [], bd_info)
            self.assertIsInstance(cfg.devices[2],
                             vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[2].target_dev, 'sdc')
            self.assertEqual(cfg.devices[2].target_bus, 'scsi')
            self.assertIsInstance(cfg.devices[3],
                             vconfig.LibvirtConfigGuestDisk)
            self.assertEqual(cfg.devices[3].target_dev, 'sdd')
            self.assertEqual(cfg.devices[3].target_bus, 'scsi')
            self.assertIsInstance(cfg.devices[4],
                             vconfig.LibvirtConfigGuestController)
            self.assertEqual(cfg.devices[4].model, 'virtio-scsi')
            mock_save.assert_called_with()

    def test_get_guest_config_with_vnc(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   group='libvirt')
        self.flags(enabled=False, group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 7)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "vnc")

    def test_get_guest_config_with_vnc_and_tablet(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=True,
                   group='libvirt')
        self.flags(enabled=False, group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "tablet")
        self.assertEqual(cfg.devices[5].type, "vnc")

    def test_get_guest_config_with_spice_and_tablet(self):
        self.flags(enabled=False, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=True,
                   group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=False,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "tablet")
        self.assertEqual(cfg.devices[5].type, "spice")

    def test_get_guest_config_with_spice_and_agent(self):
        self.flags(enabled=False, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=True,
                   group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestChannel)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].target_name, "com.redhat.spice.0")
        self.assertEqual(cfg.devices[5].type, "spice")
        self.assertEqual(cfg.devices[6].type, "qxl")

    @mock.patch('nova.console.serial.acquire_port')
    @mock.patch('nova.virt.hardware.get_number_of_serial_ports',
                return_value=1)
    @mock.patch.object(libvirt_driver.libvirt_utils, 'get_arch',)
    def test_create_serial_console_devices_based_on_arch(self, mock_get_arch,
                                           mock_get_port_number,
                                           mock_acquire_port):
        self.flags(enabled=True, group='serial_console')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        expected = {arch.X86_64: vconfig.LibvirtConfigGuestSerial,
                    arch.S390: vconfig.LibvirtConfigGuestConsole,
                    arch.S390X: vconfig.LibvirtConfigGuestConsole}

        for guest_arch, device_type in expected.items():
            mock_get_arch.return_value = guest_arch
            guest = vconfig.LibvirtConfigGuest()
            drvr._create_serial_console_devices(guest, instance=None,
                                                flavor={}, image_meta={})
            self.assertEqual(1, len(guest.devices))
            console_device = guest.devices[0]
            self.assertIsInstance(console_device, device_type)
            self.assertEqual("tcp", console_device.type)

    @mock.patch('nova.virt.hardware.get_number_of_serial_ports',
                return_value=4)
    @mock.patch.object(libvirt_driver.libvirt_utils, 'get_arch',
                       side_effect=[arch.X86_64, arch.S390, arch.S390X])
    def test_create_serial_console_devices_with_limit_exceeded_based_on_arch(
            self, mock_get_arch, mock_get_port_number):
        self.flags(enabled=True, group='serial_console')
        self.flags(virt_type="qemu", group='libvirt')
        flavor = 'fake_flavor'
        image_meta = objects.ImageMeta()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        guest = vconfig.LibvirtConfigGuest()
        self.assertRaises(exception.SerialPortNumberLimitExceeded,
                          drvr._create_serial_console_devices,
                          guest, None, flavor, image_meta)
        mock_get_arch.assert_called_with(image_meta)
        mock_get_port_number.assert_called_with(flavor,
                                                image_meta)

        drvr._create_serial_console_devices(guest, None, flavor, image_meta)
        mock_get_arch.assert_called_with(image_meta)
        mock_get_port_number.assert_called_with(flavor,
                                                image_meta)

        drvr._create_serial_console_devices(guest, None, flavor, image_meta)
        mock_get_arch.assert_called_with(image_meta)
        mock_get_port_number.assert_called_with(flavor,
                                                image_meta)

    @mock.patch('nova.console.serial.acquire_port')
    def test_get_guest_config_serial_console(self, acquire_port):
        self.flags(enabled=True, group='serial_console')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        acquire_port.return_value = 11111

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(8, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("tcp", cfg.devices[2].type)
        self.assertEqual(11111, cfg.devices[2].listen_port)

    def test_get_guest_config_serial_console_through_flavor(self):
        self.flags(enabled=True, group='serial_console')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw:serial_port_count': 3}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(10, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[9],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("tcp", cfg.devices[2].type)
        self.assertEqual("tcp", cfg.devices[3].type)
        self.assertEqual("tcp", cfg.devices[4].type)

    def test_get_guest_config_serial_console_invalid_flavor(self):
        self.flags(enabled=True, group='serial_console')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw:serial_port_count': "a"}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.assertRaises(
            exception.ImageSerialPortNumberInvalid,
            drvr._get_guest_config, instance_ref, [],
            image_meta, disk_info)

    def test_get_guest_config_serial_console_image_and_flavor(self):
        self.flags(enabled=True, group='serial_console')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_serial_port_count": "3"}})
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw:serial_port_count': 4}

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [], image_meta,
                                     disk_info)
        self.assertEqual(10, len(cfg.devices), cfg.devices)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[9],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("tcp", cfg.devices[2].type)
        self.assertEqual("tcp", cfg.devices[3].type)
        self.assertEqual("tcp", cfg.devices[4].type)

    @mock.patch('nova.console.serial.acquire_port')
    def test_get_guest_config_serial_console_through_port_rng_exhausted(
            self, acquire_port):
        self.flags(enabled=True, group='serial_console')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        acquire_port.side_effect = exception.SocketPortRangeExhaustedException(
            '127.0.0.1')
        self.assertRaises(
            exception.SocketPortRangeExhaustedException,
            drvr._get_guest_config, instance_ref, [],
            image_meta, disk_info)

    @mock.patch('nova.console.serial.release_port')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'get_info')
    @mock.patch.object(host.Host, 'get_guest')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       '_get_serial_ports_from_guest')
    def test_serial_console_release_port(
            self, mock_get_serial_ports_from_guest, mock_get_guest,
            mock_get_info, mock_release_port):
        self.flags(enabled="True", group='serial_console')

        guest = libvirt_guest.Guest(FakeVirtDomain())
        guest.power_off = mock.Mock()
        mock_get_info.return_value = hardware.InstanceInfo(
            state=power_state.SHUTDOWN)
        mock_get_guest.return_value = guest
        mock_get_serial_ports_from_guest.return_value = iter([
            ('127.0.0.1', 10000), ('127.0.0.1', 10001)])

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._destroy(objects.Instance(**self.test_instance))
        mock_release_port.assert_has_calls(
            [mock.call(host='127.0.0.1', port=10000),
             mock.call(host='127.0.0.1', port=10001)])

    @mock.patch('os.path.getsize', return_value=0)  # size doesn't matter
    @mock.patch('nova.virt.libvirt.storage.lvm.get_volume_size',
                return_value='fake-size')
    def test_detach_encrypted_volumes(self, mock_getsize,
                                      mock_get_volume_size):
        """Test that unencrypted volumes are not disconnected with dmcrypt."""
        instance = objects.Instance(**self.test_instance)
        xml = """
              <domain type='kvm'>
                  <devices>
                      <disk type='file'>
                          <driver name='fake-driver' type='fake-type' />
                          <source file='filename'/>
                          <target dev='vdc' bus='virtio'/>
                      </disk>
                      <disk type='block' device='disk'>
                          <driver name='fake-driver' type='fake-type' />
                          <source dev='/dev/mapper/disk'/>
                          <target dev='vda'/>
                      </disk>
                      <disk type='block' device='disk'>
                          <driver name='fake-driver' type='fake-type' />
                          <source dev='/dev/mapper/swap'/>
                          <target dev='vdb'/>
                      </disk>
                  </devices>
              </domain>
              """
        dom = FakeVirtDomain(fake_xml=xml)
        instance.ephemeral_key_uuid = uuids.ephemeral_key_uuid  # encrypted

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        @mock.patch.object(dmcrypt, 'delete_volume')
        @mock.patch.object(conn._host, 'get_domain', return_value=dom)
        def detach_encrypted_volumes(block_device_info, mock_get_domain,
                                     mock_delete_volume):
            conn._detach_encrypted_volumes(instance, block_device_info)

            mock_get_domain.assert_called_once_with(instance)
            self.assertFalse(mock_delete_volume.called)

        block_device_info = {'root_device_name': '/dev/vda',
                             'ephemerals': [],
                             'block_device_mapping': []}

        detach_encrypted_volumes(block_device_info)

    @mock.patch.object(libvirt_guest.Guest, "get_xml_desc")
    def test_get_serial_ports_from_guest(self, mock_get_xml_desc):
        i = self._test_get_serial_ports_from_guest(None,
                                                   mock_get_xml_desc)
        self.assertEqual([
            ('127.0.0.1', 100),
            ('127.0.0.1', 101),
            ('127.0.0.2', 100),
            ('127.0.0.2', 101)], list(i))

    @mock.patch.object(libvirt_guest.Guest, "get_xml_desc")
    def test_get_serial_ports_from_guest_bind_only(self, mock_get_xml_desc):
        i = self._test_get_serial_ports_from_guest('bind',
                                                   mock_get_xml_desc)
        self.assertEqual([
            ('127.0.0.1', 101),
            ('127.0.0.2', 100)], list(i))

    @mock.patch.object(libvirt_guest.Guest, "get_xml_desc")
    def test_get_serial_ports_from_guest_connect_only(self,
                                                      mock_get_xml_desc):
        i = self._test_get_serial_ports_from_guest('connect',
                                                   mock_get_xml_desc)
        self.assertEqual([
            ('127.0.0.1', 100),
            ('127.0.0.2', 101)], list(i))

    @mock.patch.object(libvirt_guest.Guest, "get_xml_desc")
    def test_get_serial_ports_from_guest_on_s390(self, mock_get_xml_desc):
        i = self._test_get_serial_ports_from_guest(None,
                                                   mock_get_xml_desc,
                                                   'console')
        self.assertEqual([
            ('127.0.0.1', 100),
            ('127.0.0.1', 101),
            ('127.0.0.2', 100),
            ('127.0.0.2', 101)], list(i))

    def _test_get_serial_ports_from_guest(self, mode, mock_get_xml_desc,
                                          dev_name='serial'):
        xml = """
        <domain type='kvm'>
          <devices>
            <%(dev_name)s type="tcp">
              <source host="127.0.0.1" service="100" mode="connect"/>
            </%(dev_name)s>
            <%(dev_name)s type="tcp">
              <source host="127.0.0.1" service="101" mode="bind"/>
            </%(dev_name)s>
            <%(dev_name)s type="tcp">
              <source host="127.0.0.2" service="100" mode="bind"/>
            </%(dev_name)s>
            <%(dev_name)s type="tcp">
              <source host="127.0.0.2" service="101" mode="connect"/>
            </%(dev_name)s>
          </devices>
        </domain>""" % {'dev_name': dev_name}

        mock_get_xml_desc.return_value = xml

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        guest = libvirt_guest.Guest(FakeVirtDomain())
        return drvr._get_serial_ports_from_guest(guest, mode=mode)

    def test_get_guest_config_with_type_xen(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='xen',
                   use_usb_tablet=False,
                   group='libvirt')
        self.flags(enabled=False,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 6)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestConsole)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[3].type, "vnc")
        self.assertEqual(cfg.devices[4].type, "xen")

    @mock.patch.object(libvirt_driver.libvirt_utils, 'get_arch',
                       return_value=arch.S390X)
    def test_get_guest_config_with_type_kvm_on_s390(self, mock_get_arch):
        self.flags(enabled=False, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   group='libvirt')

        self._stub_host_capabilities_cpu_arch(arch.S390X)

        instance_ref = objects.Instance(**self.test_instance)

        cfg = self._get_guest_config_via_fake_api(instance_ref)

        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        log_file_device = cfg.devices[2]
        self.assertIsInstance(log_file_device,
                              vconfig.LibvirtConfigGuestConsole)
        self.assertEqual("sclplm", log_file_device.target_type)
        self.assertEqual("file", log_file_device.type)
        terminal_device = cfg.devices[3]
        self.assertIsInstance(terminal_device,
                              vconfig.LibvirtConfigGuestConsole)
        self.assertEqual("sclp", terminal_device.target_type)
        self.assertEqual("pty", terminal_device.type)
        self.assertEqual("s390-ccw-virtio", cfg.os_mach_type)

    def _stub_host_capabilities_cpu_arch(self, cpu_arch):
        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigGuestCPU()
            cpu.arch = cpu_arch

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            return caps

        self.stubs.Set(host.Host, "get_capabilities",
                       get_host_capabilities_stub)

    def _get_guest_config_via_fake_api(self, instance):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)
        return drvr._get_guest_config(instance, [],
                                      image_meta, disk_info)

    def test_get_guest_config_with_type_xen_pae_hvm(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='xen',
                   use_usb_tablet=False,
                   group='libvirt')
        self.flags(enabled=False,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref['vm_mode'] = vm_mode.HVM
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(cfg.os_type, vm_mode.HVM)
        self.assertEqual(cfg.os_loader, CONF.libvirt.xen_hvmloader_path)
        self.assertEqual(3, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeaturePAE)
        self.assertIsInstance(cfg.features[1],
                              vconfig.LibvirtConfigGuestFeatureACPI)
        self.assertIsInstance(cfg.features[2],
                              vconfig.LibvirtConfigGuestFeatureAPIC)

    def test_get_guest_config_with_type_xen_pae_pvm(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='xen',
                   use_usb_tablet=False,
                   group='libvirt')
        self.flags(enabled=False,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(cfg.os_type, vm_mode.XEN)
        self.assertEqual(1, len(cfg.features))
        self.assertIsInstance(cfg.features[0],
                              vconfig.LibvirtConfigGuestFeaturePAE)

    def test_get_guest_config_with_vnc_and_spice(self):
        self.flags(enabled=True, group='vnc')
        self.flags(virt_type='kvm',
                   use_usb_tablet=True,
                   group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 10)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestChannel)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[9],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "tablet")
        self.assertEqual(cfg.devices[5].target_name, "com.redhat.spice.0")
        self.assertEqual(cfg.devices[6].type, "vnc")
        self.assertEqual(cfg.devices[7].type, "spice")

    def test_get_guest_config_with_watchdog_action_image_meta(self):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_watchdog_action": "none"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 9)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestWatchdog)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("none", cfg.devices[7].action)

    def _test_get_guest_usb_tablet(self, vnc_enabled, spice_enabled, os_type,
                                   agent_enabled=False, image_meta=None):
        self.flags(enabled=vnc_enabled, group='vnc')
        self.flags(enabled=spice_enabled,
                   agent_enabled=agent_enabled, group='spice')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        image_meta = objects.ImageMeta.from_dict(image_meta)
        return drvr._get_guest_pointer_model(os_type, image_meta)

    def test_get_guest_usb_tablet_wipe(self):
        self.flags(use_usb_tablet=True, group='libvirt')

        tablet = self._test_get_guest_usb_tablet(True, True, vm_mode.HVM)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(True, False, vm_mode.HVM)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(False, True, vm_mode.HVM)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(False, False, vm_mode.HVM)
        self.assertIsNone(tablet)

        tablet = self._test_get_guest_usb_tablet(True, True, "foo")
        self.assertIsNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            False, True, vm_mode.HVM, True)
        self.assertIsNone(tablet)

    def test_get_guest_usb_tablet_image_meta(self):
        self.flags(use_usb_tablet=True, group='libvirt')
        image_meta = {"properties": {"hw_pointer_model": "usbtablet"}}

        tablet = self._test_get_guest_usb_tablet(
            True, True, vm_mode.HVM, image_meta=image_meta)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            True, False, vm_mode.HVM, image_meta=image_meta)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            False, True, vm_mode.HVM, image_meta=image_meta)
        self.assertIsNotNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            False, False, vm_mode.HVM, image_meta=image_meta)
        self.assertIsNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            True, True, "foo", image_meta=image_meta)
        self.assertIsNone(tablet)

        tablet = self._test_get_guest_usb_tablet(
            False, True, vm_mode.HVM, True, image_meta=image_meta)
        self.assertIsNone(tablet)

    def test_get_guest_usb_tablet_image_meta_no_vnc(self):
        self.flags(use_usb_tablet=False, group='libvirt')
        self.flags(pointer_model=None)

        image_meta = {"properties": {"hw_pointer_model": "usbtablet"}}
        self.assertRaises(
            exception.UnsupportedPointerModelRequested,
            self._test_get_guest_usb_tablet,
            False, False, vm_mode.HVM, True, image_meta=image_meta)

    def test_get_guest_no_pointer_model_usb_tablet_set(self):
        self.flags(use_usb_tablet=True, group='libvirt')

        tablet = self._test_get_guest_usb_tablet(True, True, vm_mode.HVM)
        self.assertIsNotNone(tablet)

    def test_get_guest_no_pointer_model_usb_tablet_not_set(self):
        self.flags(use_usb_tablet=False, group='libvirt')

        tablet = self._test_get_guest_usb_tablet(True, True, vm_mode.HVM)
        self.assertIsNone(tablet)

    def test_get_guest_pointer_model_usb_tablet(self):
        self.flags(use_usb_tablet=False, group='libvirt')
        self.flags(pointer_model='usbtablet')
        tablet = self._test_get_guest_usb_tablet(True, True, vm_mode.HVM)
        self.assertIsNotNone(tablet)

    def test_get_guest_pointer_model_usb_tablet_image(self):
        image_meta = {"properties": {"hw_pointer_model": "usbtablet"}}
        tablet = self._test_get_guest_usb_tablet(
            True, True, vm_mode.HVM, image_meta=image_meta)
        self.assertIsNotNone(tablet)

    def test_get_guest_pointer_model_usb_tablet_image_no_HVM(self):
        self.flags(pointer_model=None)
        self.flags(use_usb_tablet=False, group='libvirt')
        image_meta = {"properties": {"hw_pointer_model": "usbtablet"}}
        self.assertRaises(
            exception.UnsupportedPointerModelRequested,
            self._test_get_guest_usb_tablet,
            True, True, vm_mode.XEN, image_meta=image_meta)

    def _test_get_guest_config_with_watchdog_action_flavor(self,
            hw_watchdog_action="hw:watchdog_action"):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {hw_watchdog_action: 'none'}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(9, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                            vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestWatchdog)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("none", cfg.devices[7].action)

    def test_get_guest_config_with_watchdog_action_through_flavor(self):
        self._test_get_guest_config_with_watchdog_action_flavor()

    # TODO(pkholkin): the test accepting old property name 'hw_watchdog_action'
    #                should be removed in the next release
    def test_get_guest_config_with_watchdog_action_through_flavor_no_scope(
            self):
        self._test_get_guest_config_with_watchdog_action_flavor(
            hw_watchdog_action="hw_watchdog_action")

    def test_get_guest_config_with_watchdog_overrides_flavor(self):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_watchdog_action': 'none'}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_watchdog_action": "pause"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(9, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestWatchdog)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual("pause", cfg.devices[7].action)

    def test_get_guest_config_with_video_driver_image_meta(self):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_video_model": "vmvga"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[5].type, "vnc")
        self.assertEqual(cfg.devices[6].type, "vmvga")

    def test_get_guest_config_with_qga_through_image_meta(self):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_qemu_guest_agent": "yes"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 9)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigGuestChannel)
        self.assertIsInstance(cfg.devices[8],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "tablet")
        self.assertEqual(cfg.devices[5].type, "vnc")
        self.assertEqual(cfg.devices[7].type, "unix")
        self.assertEqual(cfg.devices[7].target_name, "org.qemu.guest_agent.0")

    def test_get_guest_config_with_video_driver_vram(self):
        self.flags(enabled=False, group='vnc')
        self.flags(virt_type='kvm', group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_video:ram_max_mb': "100"}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_video_model": "qxl",
                           "hw_video_ram": "64"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestChannel)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[5].type, "spice")
        self.assertEqual(cfg.devices[6].type, "qxl")
        self.assertEqual(cfg.devices[6].vram, 64 * units.Mi / units.Ki)

    @mock.patch('nova.virt.disk.api.teardown_container')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.get_info')
    @mock.patch('nova.virt.disk.api.setup_container')
    @mock.patch('oslo_utils.fileutils.ensure_tree')
    @mock.patch.object(fake_libvirt_utils, 'get_instance_path')
    def test_unmount_fs_if_error_during_lxc_create_domain(self,
            mock_get_inst_path, mock_ensure_tree, mock_setup_container,
            mock_get_info, mock_teardown):
        """If we hit an error during a `_create_domain` call to `libvirt+lxc`
        we need to ensure the guest FS is unmounted from the host so that any
        future `lvremove` calls will work.
        """
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        mock_instance = mock.MagicMock()
        mock_get_inst_path.return_value = '/tmp/'
        mock_image_backend = mock.MagicMock()
        drvr.image_backend = mock_image_backend
        mock_image = mock.MagicMock()
        mock_image.path = '/tmp/test.img'
        drvr.image_backend.image.return_value = mock_image
        mock_setup_container.return_value = '/dev/nbd0'
        mock_get_info.side_effect = exception.InstanceNotFound(
                                                   instance_id='foo')
        drvr._conn.defineXML = mock.Mock()
        drvr._conn.defineXML.side_effect = ValueError('somethingbad')
        with test.nested(
              mock.patch.object(drvr, '_is_booted_from_volume',
                                return_value=False),
              mock.patch.object(drvr, 'plug_vifs'),
              mock.patch.object(drvr, 'firewall_driver'),
              mock.patch.object(drvr, 'cleanup')):
            self.assertRaises(ValueError,
                              drvr._create_domain_and_network,
                              self.context,
                              'xml',
                              mock_instance, None, None)

            mock_teardown.assert_called_with(container_dir='/tmp/rootfs')

    def test_video_driver_flavor_limit_not_set(self):
        self.flags(virt_type='kvm', group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_video_model": "qxl",
                           "hw_video_ram": "64"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        with mock.patch.object(objects.Instance, 'save'):
            self.assertRaises(exception.RequestedVRamTooHigh,
                              drvr._get_guest_config,
                              instance_ref,
                              [],
                              image_meta,
                              disk_info)

    def test_video_driver_ram_above_flavor_limit(self):
        self.flags(virt_type='kvm', group='libvirt')
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')

        instance_ref = objects.Instance(**self.test_instance)
        instance_type = instance_ref.get_flavor()
        instance_type.extra_specs = {'hw_video:ram_max_mb': "50"}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_video_model": "qxl",
                           "hw_video_ram": "64"}})
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        with mock.patch.object(objects.Instance, 'save'):
            self.assertRaises(exception.RequestedVRamTooHigh,
                              drvr._get_guest_config,
                              instance_ref,
                              [],
                              image_meta,
                              disk_info)

    def test_get_guest_config_without_qga_through_image_meta(self):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_qemu_guest_agent": "no"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[4].type, "tablet")
        self.assertEqual(cfg.devices[5].type, "vnc")

    def test_get_guest_config_with_rng_device(self):
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_rng:allowed': 'True'}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_rng_model": "virtio"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                            vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestRng)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[6].model, 'random')
        self.assertIsNone(cfg.devices[6].backend)
        self.assertIsNone(cfg.devices[6].rate_bytes)
        self.assertIsNone(cfg.devices[6].rate_period)

    def test_get_guest_config_with_rng_not_allowed(self):
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_rng_model": "virtio"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 7)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigMemoryBalloon)

    def test_get_guest_config_with_rng_limits(self):
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_rng:allowed': 'True',
                                           'hw_rng:rate_bytes': '1024',
                                           'hw_rng:rate_period': '2'}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_rng_model": "virtio"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestRng)
        self.assertIsInstance(cfg.devices[7],
                            vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[6].model, 'random')
        self.assertIsNone(cfg.devices[6].backend)
        self.assertEqual(cfg.devices[6].rate_bytes, 1024)
        self.assertEqual(cfg.devices[6].rate_period, 2)

    @mock.patch('nova.virt.libvirt.driver.os.path.exists')
    def test_get_guest_config_with_rng_backend(self, mock_path):
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   rng_dev_path='/dev/hw_rng',
                   group='libvirt')
        mock_path.return_value = True

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_rng:allowed': 'True'}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_rng_model": "virtio"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        self.assertEqual(len(cfg.devices), 8)
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[1],
                            vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestSerial)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)
        self.assertIsInstance(cfg.devices[6],
                              vconfig.LibvirtConfigGuestRng)
        self.assertIsInstance(cfg.devices[7],
                              vconfig.LibvirtConfigMemoryBalloon)

        self.assertEqual(cfg.devices[6].model, 'random')
        self.assertEqual(cfg.devices[6].backend, '/dev/hw_rng')
        self.assertIsNone(cfg.devices[6].rate_bytes)
        self.assertIsNone(cfg.devices[6].rate_period)

    @mock.patch('nova.virt.libvirt.driver.os.path.exists')
    def test_get_guest_config_with_rng_dev_not_present(self, mock_path):
        self.flags(virt_type='kvm',
                   use_usb_tablet=False,
                   rng_dev_path='/dev/hw_rng',
                   group='libvirt')
        mock_path.return_value = False

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'hw_rng:allowed': 'True'}
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_rng_model": "virtio"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.assertRaises(exception.RngDeviceNotExist,
                          drvr._get_guest_config,
                          instance_ref,
                          [],
                          image_meta, disk_info)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_guest_cpu_shares_with_multi_vcpu(self, is_able):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.vcpus = 4
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(4096, cfg.cputune.shares)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_with_cpu_quota(self, is_able):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'quota:cpu_shares': '10000',
                                           'quota:cpu_period': '20000'}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)

        self.assertEqual(10000, cfg.cputune.shares)
        self.assertEqual(20000, cfg.cputune.period)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=True)
    def test_get_guest_config_with_bogus_cpu_quota(self, is_able):
        self.flags(virt_type='kvm', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'quota:cpu_shares': 'fishfood',
                                           'quota:cpu_period': '20000'}
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.assertRaises(ValueError,
                          drvr._get_guest_config,
                          instance_ref, [], image_meta, disk_info)

    @mock.patch.object(
        host.Host, "is_cpu_control_policy_capable", return_value=False)
    def test_get_update_guest_cputune(self, is_able):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = {'quota:cpu_shares': '10000',
                                           'quota:cpu_period': '20000'}
        self.assertRaises(
            exception.UnsupportedHostCPUControlPolicy,
            drvr._update_guest_cputune, {}, instance_ref.flavor, "kvm")

    def _test_get_guest_config_sysinfo_serial(self, expected_serial):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        instance_ref = objects.Instance(**self.test_instance)

        cfg = drvr._get_guest_config_sysinfo(instance_ref)

        self.assertIsInstance(cfg, vconfig.LibvirtConfigGuestSysinfo)
        self.assertEqual(version.vendor_string(),
                         cfg.system_manufacturer)
        self.assertEqual(version.product_string(),
                         cfg.system_product)
        self.assertEqual(version.version_string_with_package(),
                         cfg.system_version)
        self.assertEqual(expected_serial,
                         cfg.system_serial)
        self.assertEqual(instance_ref['uuid'],
                         cfg.system_uuid)
        self.assertEqual("Virtual Machine",
                         cfg.system_family)

    def test_get_guest_config_sysinfo_serial_none(self):
        self.flags(sysinfo_serial="none", group="libvirt")
        self._test_get_guest_config_sysinfo_serial(None)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_get_host_sysinfo_serial_hardware")
    def test_get_guest_config_sysinfo_serial_hardware(self, mock_uuid):
        self.flags(sysinfo_serial="hardware", group="libvirt")

        theuuid = "56b40135-a973-4eb3-87bb-a2382a3e6dbc"
        mock_uuid.return_value = theuuid

        self._test_get_guest_config_sysinfo_serial(theuuid)

    @contextlib.contextmanager
    def patch_exists(self, result):
        real_exists = os.path.exists

        def fake_exists(filename):
            if filename == "/etc/machine-id":
                return result
            return real_exists(filename)

        with mock.patch.object(os.path, "exists") as mock_exists:
            mock_exists.side_effect = fake_exists
            yield mock_exists

    def test_get_guest_config_sysinfo_serial_os(self):
        self.flags(sysinfo_serial="os", group="libvirt")
        theuuid = "56b40135-a973-4eb3-87bb-a2382a3e6dbc"
        with test.nested(
                mock.patch.object(six.moves.builtins, "open",
                    mock.mock_open(read_data=theuuid)),
                self.patch_exists(True)):
            self._test_get_guest_config_sysinfo_serial(theuuid)

    def test_get_guest_config_sysinfo_serial_os_empty_machine_id(self):
        self.flags(sysinfo_serial="os", group="libvirt")
        with test.nested(
                mock.patch.object(six.moves.builtins, "open",
                                  mock.mock_open(read_data="")),
                self.patch_exists(True)):
            self.assertRaises(exception.NovaException,
                    self._test_get_guest_config_sysinfo_serial,
                    None)

    def test_get_guest_config_sysinfo_serial_os_no_machine_id_file(self):
        self.flags(sysinfo_serial="os", group="libvirt")
        with self.patch_exists(False):
            self.assertRaises(exception.NovaException,
                    self._test_get_guest_config_sysinfo_serial,
                    None)

    def test_get_guest_config_sysinfo_serial_auto_hardware(self):
        self.flags(sysinfo_serial="auto", group="libvirt")

        real_exists = os.path.exists
        with test.nested(
                mock.patch.object(os.path, "exists"),
                mock.patch.object(libvirt_driver.LibvirtDriver,
                                  "_get_host_sysinfo_serial_hardware")
        ) as (mock_exists, mock_uuid):
            def fake_exists(filename):
                if filename == "/etc/machine-id":
                    return False
                return real_exists(filename)

            mock_exists.side_effect = fake_exists

            theuuid = "56b40135-a973-4eb3-87bb-a2382a3e6dbc"
            mock_uuid.return_value = theuuid

            self._test_get_guest_config_sysinfo_serial(theuuid)

    def test_get_guest_config_sysinfo_serial_auto_os(self):
        self.flags(sysinfo_serial="auto", group="libvirt")

        real_exists = os.path.exists
        real_open = builtins.open
        with test.nested(
                mock.patch.object(os.path, "exists"),
                mock.patch.object(builtins, "open"),
        ) as (mock_exists, mock_open):
            def fake_exists(filename):
                if filename == "/etc/machine-id":
                    return True
                return real_exists(filename)

            mock_exists.side_effect = fake_exists

            theuuid = "56b40135-a973-4eb3-87bb-a2382a3e6dbc"

            def fake_open(filename, *args, **kwargs):
                if filename == "/etc/machine-id":
                    h = mock.MagicMock()
                    h.read.return_value = theuuid
                    h.__enter__.return_value = h
                    return h
                return real_open(filename, *args, **kwargs)

            mock_open.side_effect = fake_open

            self._test_get_guest_config_sysinfo_serial(theuuid)

    def _create_fake_service_compute(self):
        service_info = {
            'id': 1729,
            'host': 'fake',
            'report_count': 0
        }
        service_ref = objects.Service(**service_info)

        compute_info = {
            'id': 1729,
            'vcpus': 2,
            'memory_mb': 1024,
            'local_gb': 2048,
            'vcpus_used': 0,
            'memory_mb_used': 0,
            'local_gb_used': 0,
            'free_ram_mb': 1024,
            'free_disk_gb': 2048,
            'hypervisor_type': 'xen',
            'hypervisor_version': 1,
            'running_vms': 0,
            'cpu_info': '',
            'current_workload': 0,
            'service_id': service_ref['id'],
            'host': service_ref['host']
        }
        compute_ref = objects.ComputeNode(**compute_info)
        return (service_ref, compute_ref)

    def test_get_guest_config_with_pci_passthrough_kvm(self):
        self.flags(virt_type='kvm', group='libvirt')
        service_ref, compute_ref = self._create_fake_service_compute()

        instance = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        pci_device_info = dict(test_pci_device.fake_db_dev)
        pci_device_info.update(compute_node_id=1,
                               label='fake',
                               status=fields.PciDeviceStatus.ALLOCATED,
                               address='0000:00:00.1',
                               compute_id=compute_ref.id,
                               instance_uuid=instance.uuid,
                               request_id=None,
                               extra_info={})
        pci_device = objects.PciDevice(**pci_device_info)
        pci_list = objects.PciDeviceList()
        pci_list.objects.append(pci_device)
        instance.pci_devices = pci_list

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)
        cfg = drvr._get_guest_config(instance, [],
                                     image_meta, disk_info)

        had_pci = 0
        # care only about the PCI devices
        for dev in cfg.devices:
            if type(dev) == vconfig.LibvirtConfigGuestHostdevPCI:
                had_pci += 1
                self.assertEqual(dev.type, 'pci')
                self.assertEqual(dev.managed, 'yes')
                self.assertEqual(dev.mode, 'subsystem')

                self.assertEqual(dev.domain, "0000")
                self.assertEqual(dev.bus, "00")
                self.assertEqual(dev.slot, "00")
                self.assertEqual(dev.function, "1")
        self.assertEqual(had_pci, 1)

    def test_get_guest_config_with_pci_passthrough_xen(self):
        self.flags(virt_type='xen', group='libvirt')
        service_ref, compute_ref = self._create_fake_service_compute()

        instance = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        pci_device_info = dict(test_pci_device.fake_db_dev)
        pci_device_info.update(compute_node_id=1,
                               label='fake',
                               status=fields.PciDeviceStatus.ALLOCATED,
                               address='0000:00:00.2',
                               compute_id=compute_ref.id,
                               instance_uuid=instance.uuid,
                               request_id=None,
                               extra_info={})
        pci_device = objects.PciDevice(**pci_device_info)
        pci_list = objects.PciDeviceList()
        pci_list.objects.append(pci_device)
        instance.pci_devices = pci_list

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)
        cfg = drvr._get_guest_config(instance, [],
                                     image_meta, disk_info)
        had_pci = 0
        # care only about the PCI devices
        for dev in cfg.devices:
            if type(dev) == vconfig.LibvirtConfigGuestHostdevPCI:
                had_pci += 1
                self.assertEqual(dev.type, 'pci')
                self.assertEqual(dev.managed, 'no')
                self.assertEqual(dev.mode, 'subsystem')

                self.assertEqual(dev.domain, "0000")
                self.assertEqual(dev.bus, "00")
                self.assertEqual(dev.slot, "00")
                self.assertEqual(dev.function, "2")
        self.assertEqual(had_pci, 1)

    def test_get_guest_config_os_command_line_through_image_meta(self):
        self.flags(virt_type="kvm",
                   cpu_mode='none',
                   group='libvirt')

        self.test_instance['kernel_id'] = "fake_kernel_id"

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"os_command_line":
                           "fake_os_command_line"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertEqual(cfg.os_cmdline, "fake_os_command_line")

    def test_get_guest_config_os_command_line_without_kernel_id(self):
        self.flags(virt_type="kvm",
                cpu_mode='none',
                group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"os_command_line":
                           "fake_os_command_line"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertIsNone(cfg.os_cmdline)

    def test_get_guest_config_os_command_empty(self):
        self.flags(virt_type="kvm",
                   cpu_mode='none',
                   group='libvirt')

        self.test_instance['kernel_id'] = "fake_kernel_id"

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"os_command_line": ""}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        # the instance has 'root=/dev/vda console=tty0 console=ttyS0' set by
        # default, so testing an empty string and None value in the
        # os_command_line image property must pass
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertNotEqual(cfg.os_cmdline, "")

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_get_guest_storage_config")
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_numa_support")
    def test_get_guest_config_armv7(self, mock_numa, mock_storage):
        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigGuestCPU()
            cpu.arch = arch.ARMV7

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            return caps

        self.flags(virt_type="kvm",
                   group="libvirt")

        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.stubs.Set(host.Host, "get_capabilities",
                       get_host_capabilities_stub)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertEqual(cfg.os_mach_type, "vexpress-a15")

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_get_guest_storage_config")
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_has_numa_support")
    def test_get_guest_config_aarch64(self, mock_numa, mock_storage):
        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigGuestCPU()
            cpu.arch = arch.AARCH64

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu
            return caps

        self.flags(virt_type="kvm",
                   group="libvirt")

        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        self.stubs.Set(host.Host, "get_capabilities",
                       get_host_capabilities_stub)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertEqual(cfg.os_mach_type, "virt")

    def test_get_guest_config_machine_type_s390(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigGuestCPU()

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        host_cpu_archs = (arch.S390, arch.S390X)
        for host_cpu_arch in host_cpu_archs:
            caps.host.cpu.arch = host_cpu_arch
            os_mach_type = drvr._get_machine_type(image_meta, caps)
            self.assertEqual('s390-ccw-virtio', os_mach_type)

    def test_get_guest_config_machine_type_through_image_meta(self):
        self.flags(virt_type="kvm",
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "raw",
            "properties": {"hw_machine_type":
                           "fake_machine_type"}})

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertEqual(cfg.os_mach_type, "fake_machine_type")

    def test_get_guest_config_machine_type_from_config(self):
        self.flags(virt_type='kvm', group='libvirt')
        self.flags(hw_machine_type=['x86_64=fake_machine_type'],
                group='libvirt')

        def fake_getCapabilities():
            return """
            <capabilities>
                <host>
                    <uuid>cef19ce0-0ca2-11df-855d-b19fbce37686</uuid>
                    <cpu>
                      <arch>x86_64</arch>
                      <model>Penryn</model>
                      <vendor>Intel</vendor>
                      <topology sockets='1' cores='2' threads='1'/>
                      <feature name='xtpr'/>
                    </cpu>
                </host>
            </capabilities>
            """

        def fake_baselineCPU(cpu, flag):
            return """<cpu mode='custom' match='exact'>
                        <model fallback='allow'>Penryn</model>
                        <vendor>Intel</vendor>
                        <feature policy='require' name='xtpr'/>
                      </cpu>
                   """

        # Make sure the host arch is mocked as x86_64
        self.create_fake_libvirt_mock(getCapabilities=fake_getCapabilities,
                                      baselineCPU=fake_baselineCPU,
                                      getVersion=lambda: 1005001)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                     _fake_network_info(self, 1),
                                     image_meta, disk_info)
        self.assertEqual(cfg.os_mach_type, "fake_machine_type")

    def _test_get_guest_config_ppc64(self, device_index):
        """Test for nova.virt.libvirt.driver.LibvirtDriver._get_guest_config.
        """
        self.flags(virt_type='kvm', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        expected = (arch.PPC64, arch.PPC)
        for guestarch in expected:
            with mock.patch.object(libvirt_driver.libvirt_utils,
                                   'get_arch',
                                   return_value=guestarch):
                cfg = drvr._get_guest_config(instance_ref, [],
                                            image_meta,
                                            disk_info)
                self.assertIsInstance(cfg.devices[device_index],
                                      vconfig.LibvirtConfigGuestVideo)
                self.assertEqual(cfg.devices[device_index].type, 'vga')

    def test_get_guest_config_ppc64_through_image_meta_vnc_enabled(self):
        self.flags(enabled=True, group='vnc')
        self._test_get_guest_config_ppc64(6)

    def test_get_guest_config_ppc64_through_image_meta_spice_enabled(self):
        self.flags(enabled=True,
                   agent_enabled=True,
                   group='spice')
        self._test_get_guest_config_ppc64(8)

    def _test_get_guest_config_bootmenu(self, image_meta, extra_specs):
        self.flags(virt_type='kvm', group='libvirt')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.extra_specs = extra_specs
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref, image_meta)
        conf = conn._get_guest_config(instance_ref, [], image_meta, disk_info)
        self.assertTrue(conf.os_bootmenu)

    def test_get_guest_config_bootmenu_via_image_meta(self):
        image_meta = objects.ImageMeta.from_dict(
            {"disk_format": "raw",
             "properties": {"hw_boot_menu": "True"}})
        self._test_get_guest_config_bootmenu(image_meta, {})

    def test_get_guest_config_bootmenu_via_extra_specs(self):
        image_meta = objects.ImageMeta.from_dict(
            self.test_image_meta)
        self._test_get_guest_config_bootmenu(image_meta,
                                             {'hw:boot_menu': 'True'})

    def test_get_guest_cpu_config_none(self):
        self.flags(cpu_mode="none", group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertIsNone(conf.cpu.mode)
        self.assertIsNone(conf.cpu.model)
        self.assertEqual(conf.cpu.sockets, instance_ref.flavor.vcpus)
        self.assertEqual(conf.cpu.cores, 1)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_cpu_config_default_kvm(self):
        self.flags(virt_type="kvm",
                   cpu_mode='none',
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertIsNone(conf.cpu.mode)
        self.assertIsNone(conf.cpu.model)
        self.assertEqual(conf.cpu.sockets, instance_ref.flavor.vcpus)
        self.assertEqual(conf.cpu.cores, 1)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_cpu_config_default_uml(self):
        self.flags(virt_type="uml",
                   cpu_mode='none',
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsNone(conf.cpu)

    def test_get_guest_cpu_config_default_lxc(self):
        self.flags(virt_type="lxc",
                   cpu_mode='none',
                   group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsNone(conf.cpu)

    def test_get_guest_cpu_config_host_passthrough(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.flags(cpu_mode="host-passthrough", group='libvirt')
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertEqual(conf.cpu.mode, "host-passthrough")
        self.assertIsNone(conf.cpu.model)
        self.assertEqual(conf.cpu.sockets, instance_ref.flavor.vcpus)
        self.assertEqual(conf.cpu.cores, 1)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_cpu_config_host_model(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.flags(cpu_mode="host-model", group='libvirt')
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertEqual(conf.cpu.mode, "host-model")
        self.assertIsNone(conf.cpu.model)
        self.assertEqual(conf.cpu.sockets, instance_ref.flavor.vcpus)
        self.assertEqual(conf.cpu.cores, 1)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_cpu_config_custom(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.flags(cpu_mode="custom",
                   cpu_model="Penryn",
                   group='libvirt')
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertEqual(conf.cpu.mode, "custom")
        self.assertEqual(conf.cpu.model, "Penryn")
        self.assertEqual(conf.cpu.sockets, instance_ref.flavor.vcpus)
        self.assertEqual(conf.cpu.cores, 1)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_cpu_topology(self):
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.flavor.vcpus = 8
        instance_ref.flavor.extra_specs = {'hw:cpu_max_sockets': '4'}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        conf = drvr._get_guest_config(instance_ref,
                                      _fake_network_info(self, 1),
                                      image_meta, disk_info)
        self.assertIsInstance(conf.cpu,
                              vconfig.LibvirtConfigGuestCPU)
        self.assertEqual(conf.cpu.mode, "host-model")
        self.assertEqual(conf.cpu.sockets, 4)
        self.assertEqual(conf.cpu.cores, 2)
        self.assertEqual(conf.cpu.threads, 1)

    def test_get_guest_memory_balloon_config_by_default(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                self.assertIsInstance(device,
                                      vconfig.LibvirtConfigMemoryBalloon)
                self.assertEqual('virtio', device.model)
                self.assertEqual(10, device.period)

    def test_get_guest_memory_balloon_config_disable(self):
        self.flags(mem_stats_period_seconds=0, group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        no_exist = True
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                no_exist = False
                break
        self.assertTrue(no_exist)

    def test_get_guest_memory_balloon_config_period_value(self):
        self.flags(mem_stats_period_seconds=21, group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                self.assertIsInstance(device,
                                      vconfig.LibvirtConfigMemoryBalloon)
                self.assertEqual('virtio', device.model)
                self.assertEqual(21, device.period)

    def test_get_guest_memory_balloon_config_qemu(self):
        self.flags(virt_type='qemu', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                self.assertIsInstance(device,
                                      vconfig.LibvirtConfigMemoryBalloon)
                self.assertEqual('virtio', device.model)
                self.assertEqual(10, device.period)

    def test_get_guest_memory_balloon_config_xen(self):
        self.flags(virt_type='xen', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                self.assertIsInstance(device,
                                      vconfig.LibvirtConfigMemoryBalloon)
                self.assertEqual('xen', device.model)
                self.assertEqual(10, device.period)

    def test_get_guest_memory_balloon_config_lxc(self):
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                     image_meta, disk_info)
        no_exist = True
        for device in cfg.devices:
            if device.root_name == 'memballoon':
                no_exist = False
                break
        self.assertTrue(no_exist)

    @mock.patch('nova.virt.libvirt.driver.LOG.warning')
    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(host.Host, "get_capabilities")
    def test_get_supported_perf_events_foo(self, mock_get_caps,
                                           mock_min_version,
                                           mock_warn):
        self.flags(enabled_perf_events=['foo'], group='libvirt')

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        mock_get_caps.return_value = caps
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        events = drvr._get_supported_perf_events()

        self.assertTrue(mock_warn.called)
        self.assertEqual([], events)

    @mock.patch.object(host.Host, "get_capabilities")
    def _test_get_guest_with_perf(self, caps, events, mock_get_caps):
        self.flags(enabled_perf_events=['cmt'], group='libvirt')
        mock_get_caps.return_value = caps

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.init_host('test_perf')
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                        instance_ref,
                                        image_meta)
        cfg = drvr._get_guest_config(instance_ref, [],
                                 image_meta, disk_info)

        self.assertEqual(events, cfg.perf_events)

    @mock.patch.object(fakelibvirt, 'VIR_PERF_PARAM_CMT', True,
                       create=True)
    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_get_guest_with_perf_supported(self,
                                 mock_min_version):
        self.flags(enabled_perf_events=['cmt'], group='libvirt')
        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()
        feature = vconfig.LibvirtConfigGuestCPUFeature()
        feature.name = 'cqm'
        feature.policy = cpumodel.POLICY_REQUIRE
        caps.host.cpu.features = set([feature])

        self._test_get_guest_with_perf(caps, ['cmt'])

    @mock.patch.object(host.Host, 'has_min_version')
    def test_get_guest_with_perf_libvirt_unsupported(self, mock_min_version):

        def fake_has_min_version(lv_ver=None, hv_ver=None, hv_type=None):
            if lv_ver == libvirt_driver.MIN_LIBVIRT_PERF_VERSION:
                return False
            return True

        mock_min_version.side_effect = fake_has_min_version
        self.flags(enabled_perf_events=['cmt'], group='libvirt')

        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"

        self._test_get_guest_with_perf(caps, [])

    @mock.patch.object(fakelibvirt, 'VIR_PERF_PARAM_CMT', True,
                       create=True)
    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_get_guest_with_perf_host_unsupported(self,
                                             mock_min_version):
        self.flags(enabled_perf_events=['cmt'], group='libvirt')
        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = "x86_64"
        caps.host.topology = self._fake_caps_numa_topology()

        self._test_get_guest_with_perf(caps, [])

    def test_xml_and_uri_no_ramdisk_no_kernel(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri_no_ramdisk_no_kernel_xen_hvm(self):
        instance_data = dict(self.test_instance)
        instance_data.update({'vm_mode': vm_mode.HVM})
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, expect_xen_hvm=True)

    def test_xml_and_uri_no_ramdisk_no_kernel_xen_pv(self):
        instance_data = dict(self.test_instance)
        instance_data.update({'vm_mode': vm_mode.XEN})
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, expect_xen_hvm=False,
                                xen_only=True)

    def test_xml_and_uri_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=False)

    def test_xml_and_uri_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=False, expect_ramdisk=False)

    def test_xml_and_uri(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data,
                                expect_kernel=True, expect_ramdisk=True)

    def test_xml_and_uri_rescue(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'ari-deadbeef'
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_and_uri_rescue_no_kernel(self):
        instance_data = dict(self.test_instance)
        instance_data['ramdisk_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=False,
                                expect_ramdisk=True, rescue=instance_data)

    def test_xml_and_uri_rescue_no_ramdisk(self):
        instance_data = dict(self.test_instance)
        instance_data['kernel_id'] = 'aki-deadbeef'
        self._check_xml_and_uri(instance_data, expect_kernel=True,
                                expect_ramdisk=False, rescue=instance_data)

    def test_xml_uuid(self):
        self._check_xml_and_uuid(self.test_image_meta)

    def test_lxc_container_and_uri(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_container(instance_data)

    def test_xml_disk_prefix(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_prefix(instance_data, None)

    def test_xml_user_specified_disk_prefix(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_prefix(instance_data, 'sd')

    def test_xml_disk_driver(self):
        instance_data = dict(self.test_instance)
        self._check_xml_and_disk_driver(instance_data)

    def test_xml_disk_bus_virtio(self):
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        self._check_xml_and_disk_bus(image_meta,
                                     None,
                                     (("disk", "virtio", "vda"),))

    def test_xml_disk_bus_ide(self):
        # It's necessary to check if the architecture is power, because
        # power doesn't have support to ide, and so libvirt translate
        # all ide calls to scsi

        expected = {arch.PPC: ("cdrom", "scsi", "sda"),
                    arch.PPC64: ("cdrom", "scsi", "sda"),
                    arch.PPC64LE: ("cdrom", "scsi", "sda"),
                    arch.AARCH64: ("cdrom", "scsi", "sda")}

        expec_val = expected.get(blockinfo.libvirt_utils.get_arch({}),
                                  ("cdrom", "ide", "hda"))
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "iso"})
        self._check_xml_and_disk_bus(image_meta,
                                     None,
                                     (expec_val,))

    def test_xml_disk_bus_ide_and_virtio(self):
        # It's necessary to check if the architecture is power, because
        # power doesn't have support to ide, and so libvirt translate
        # all ide calls to scsi

        expected = {arch.PPC: ("cdrom", "scsi", "sda"),
                    arch.PPC64: ("cdrom", "scsi", "sda"),
                    arch.PPC64LE: ("cdrom", "scsi", "sda"),
                    arch.AARCH64: ("cdrom", "scsi", "sda")}

        swap = {'device_name': '/dev/vdc',
                'swap_size': 1}
        ephemerals = [{'device_type': 'disk',
                       'disk_bus': 'virtio',
                       'device_name': '/dev/vdb',
                       'size': 1}]
        block_device_info = {
                'swap': swap,
                'ephemerals': ephemerals}
        expec_val = expected.get(blockinfo.libvirt_utils.get_arch({}),
                                  ("cdrom", "ide", "hda"))
        image_meta = objects.ImageMeta.from_dict({
            "disk_format": "iso"})
        self._check_xml_and_disk_bus(image_meta,
                                     block_device_info,
                                     (expec_val,
                                      ("disk", "virtio", "vdb"),
                                      ("disk", "virtio", "vdc")))

    @mock.patch.object(host.Host, "list_instance_domains")
    def test_list_instances(self, mock_list):
        vm1 = FakeVirtDomain(id=3, name="instance00000001")
        vm2 = FakeVirtDomain(id=17, name="instance00000002")
        vm3 = FakeVirtDomain(name="instance00000003")
        vm4 = FakeVirtDomain(name="instance00000004")

        mock_list.return_value = [vm1, vm2, vm3, vm4]

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        names = drvr.list_instances()
        self.assertEqual(names[0], vm1.name())
        self.assertEqual(names[1], vm2.name())
        self.assertEqual(names[2], vm3.name())
        self.assertEqual(names[3], vm4.name())
        mock_list.assert_called_with(only_guests=True, only_running=False)

    @mock.patch.object(host.Host, "list_instance_domains")
    def test_list_instance_uuids(self, mock_list):
        vm1 = FakeVirtDomain(id=3, name="instance00000001")
        vm2 = FakeVirtDomain(id=17, name="instance00000002")
        vm3 = FakeVirtDomain(name="instance00000003")
        vm4 = FakeVirtDomain(name="instance00000004")

        mock_list.return_value = [vm1, vm2, vm3, vm4]
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        uuids = drvr.list_instance_uuids()
        self.assertEqual(len(uuids), 4)
        self.assertEqual(uuids[0], vm1.UUIDString())
        self.assertEqual(uuids[1], vm2.UUIDString())
        self.assertEqual(uuids[2], vm3.UUIDString())
        self.assertEqual(uuids[3], vm4.UUIDString())
        mock_list.assert_called_with(only_guests=True, only_running=False)

    @mock.patch('nova.virt.libvirt.host.Host.get_online_cpus')
    def test_get_host_vcpus(self, get_online_cpus):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.flags(vcpu_pin_set="4-5")
        get_online_cpus.return_value = set([4, 5, 6])
        expected_vcpus = 2
        vcpus = drvr._get_vcpu_total()
        self.assertEqual(expected_vcpus, vcpus)

    @mock.patch('nova.virt.libvirt.host.Host.get_online_cpus')
    def test_get_host_vcpus_out_of_range(self, get_online_cpus):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.flags(vcpu_pin_set="4-6")
        get_online_cpus.return_value = set([4, 5])
        self.assertRaises(exception.Invalid, drvr._get_vcpu_total)

    @mock.patch('nova.virt.libvirt.host.Host.get_online_cpus')
    def test_get_host_vcpus_libvirt_error(self, get_online_cpus):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        not_supported_exc = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            'this function is not supported by the connection driver:'
            ' virNodeNumOfDevices',
            error_code=fakelibvirt.VIR_ERR_NO_SUPPORT)
        self.flags(vcpu_pin_set="4-6")
        get_online_cpus.side_effect = not_supported_exc
        self.assertRaises(exception.Invalid, drvr._get_vcpu_total)

    @mock.patch('nova.virt.libvirt.host.Host.get_online_cpus')
    def test_get_host_vcpus_libvirt_error_success(self, get_online_cpus):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        not_supported_exc = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            'this function is not supported by the connection driver:'
            ' virNodeNumOfDevices',
            error_code=fakelibvirt.VIR_ERR_NO_SUPPORT)
        self.flags(vcpu_pin_set="1")
        get_online_cpus.side_effect = not_supported_exc
        expected_vcpus = 1
        vcpus = drvr._get_vcpu_total()
        self.assertEqual(expected_vcpus, vcpus)

    @mock.patch('nova.virt.libvirt.host.Host.get_cpu_count')
    def test_get_host_vcpus_after_hotplug(self, get_cpu_count):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        get_cpu_count.return_value = 2
        expected_vcpus = 2
        vcpus = drvr._get_vcpu_total()
        self.assertEqual(expected_vcpus, vcpus)
        get_cpu_count.return_value = 3
        expected_vcpus = 3
        vcpus = drvr._get_vcpu_total()
        self.assertEqual(expected_vcpus, vcpus)

    @mock.patch.object(host.Host, "has_min_version", return_value=True)
    def test_quiesce(self, mock_has_min_version):
        self.create_fake_libvirt_mock(lookupByName=self.fake_lookup)
        with mock.patch.object(FakeVirtDomain, "fsFreeze") as mock_fsfreeze:
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
            instance = objects.Instance(**self.test_instance)
            image_meta = objects.ImageMeta.from_dict(
                {"properties": {"hw_qemu_guest_agent": "yes"}})
            self.assertIsNone(drvr.quiesce(self.context, instance, image_meta))
            mock_fsfreeze.assert_called_once_with()

    def test_quiesce_not_supported(self):
        self.create_fake_libvirt_mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        instance = objects.Instance(**self.test_instance)
        self.assertRaises(exception.InstanceQuiesceNotSupported,
                      drvr.quiesce, self.context, instance, None)

    @mock.patch.object(host.Host, "has_min_version", return_value=True)
    def test_unquiesce(self, mock_has_min_version):
        self.create_fake_libvirt_mock(getLibVersion=lambda: 1002005,
                                      lookupByName=self.fake_lookup)
        with mock.patch.object(FakeVirtDomain, "fsThaw") as mock_fsthaw:
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
            instance = objects.Instance(**self.test_instance)
            image_meta = objects.ImageMeta.from_dict(
                {"properties": {"hw_qemu_guest_agent": "yes"}})
            self.assertIsNone(drvr.unquiesce(self.context, instance,
                                             image_meta))
            mock_fsthaw.assert_called_once_with()

    def test_create_snapshot_metadata(self):
        base = objects.ImageMeta.from_dict(
            {'disk_format': 'raw'})
        instance_data = {'kernel_id': 'kernel',
                    'project_id': 'prj_id',
                    'ramdisk_id': 'ram_id',
                    'os_type': None}
        instance = objects.Instance(**instance_data)
        img_fmt = 'raw'
        snp_name = 'snapshot_name'
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = drvr._create_snapshot_metadata(base, instance, img_fmt, snp_name)
        expected = {'is_public': False,
                    'status': 'active',
                    'name': snp_name,
                    'properties': {
                                   'kernel_id': instance['kernel_id'],
                                   'image_location': 'snapshot',
                                   'image_state': 'available',
                                   'owner_id': instance['project_id'],
                                   'ramdisk_id': instance['ramdisk_id'],
                                   },
                    'disk_format': img_fmt,
                    'container_format': 'bare',
                    }
        self.assertEqual(ret, expected)

        # simulate an instance with os_type field defined
        # disk format equals to ami
        # container format not equals to bare
        instance['os_type'] = 'linux'
        base = objects.ImageMeta.from_dict(
            {'disk_format': 'ami',
             'container_format': 'test_container'})
        expected['properties']['os_type'] = instance['os_type']
        expected['disk_format'] = base.disk_format
        expected['container_format'] = base.container_format
        ret = drvr._create_snapshot_metadata(base, instance, img_fmt, snp_name)
        self.assertEqual(ret, expected)

    def test_get_volume_driver(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        connection_info = {'driver_volume_type': 'fake',
                           'data': {'device_path': '/fake',
                                    'access_mode': 'rw'}}
        driver = conn._get_volume_driver(connection_info)
        result = isinstance(driver, volume_drivers.LibvirtFakeVolumeDriver)
        self.assertTrue(result)

    def test_get_volume_driver_unknown(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        connection_info = {'driver_volume_type': 'unknown',
                           'data': {'device_path': '/fake',
                                    'access_mode': 'rw'}}
        self.assertRaises(
            exception.VolumeDriverNotFound,
            conn._get_volume_driver,
            connection_info
        )

    @mock.patch.object(volume_drivers.LibvirtFakeVolumeDriver,
                       'connect_volume')
    @mock.patch.object(volume_drivers.LibvirtFakeVolumeDriver, 'get_config')
    def test_get_volume_config(self, get_config, connect_volume):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        connection_info = {'driver_volume_type': 'fake',
                           'data': {'device_path': '/fake',
                                    'access_mode': 'rw'}}
        bdm = {'device_name': 'vdb',
               'disk_bus': 'fake-bus',
               'device_type': 'fake-type'}
        disk_info = {'bus': bdm['disk_bus'], 'type': bdm['device_type'],
                     'dev': 'vdb'}
        mock_config = mock.MagicMock()

        get_config.return_value = mock_config
        config = drvr._get_volume_config(connection_info, disk_info)
        get_config.assert_called_once_with(connection_info, disk_info)
        self.assertEqual(mock_config, config)

    def test_attach_invalid_volume_type(self):
        self.create_fake_libvirt_mock()
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        instance = objects.Instance(**self.test_instance)
        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.VolumeDriverNotFound,
                          drvr.attach_volume, None,
                          {"driver_volume_type": "badtype"},
                          instance,
                          "/dev/sda")

    def test_attach_blockio_invalid_hypervisor(self):
        self.flags(virt_type='lxc', group='libvirt')
        self.create_fake_libvirt_mock()
        libvirt_driver.LibvirtDriver._conn.lookupByName = self.fake_lookup
        instance = objects.Instance(**self.test_instance)
        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.InvalidHypervisorType,
                          drvr.attach_volume, None,
                          {"driver_volume_type": "fake",
                           "data": {"logical_block_size": "4096",
                                    "physical_block_size": "4096"}
                          },
                          instance,
                          "/dev/sda")

    def _test_check_discard(self, mock_log, driver_discard=None,
                            bus=None, should_log=False):
        mock_config = mock.Mock()
        mock_config.driver_discard = driver_discard
        mock_config.target_bus = bus
        mock_instance = mock.Mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._check_discard_for_attach_volume(mock_config, mock_instance)
        self.assertEqual(should_log, mock_log.called)

    @mock.patch('nova.virt.libvirt.driver.LOG.debug')
    def test_check_discard_for_attach_volume_no_unmap(self, mock_log):
        self._test_check_discard(mock_log, driver_discard=None,
                                 bus='scsi', should_log=False)

    @mock.patch('nova.virt.libvirt.driver.LOG.debug')
    def test_check_discard_for_attach_volume_blk_controller(self, mock_log):
        self._test_check_discard(mock_log, driver_discard='unmap',
                                 bus='virtio', should_log=True)

    @mock.patch('nova.virt.libvirt.driver.LOG.debug')
    def test_check_discard_for_attach_volume_valid_controller(self, mock_log):
        self._test_check_discard(mock_log, driver_discard='unmap',
                                 bus='scsi', should_log=False)

    @mock.patch('nova.virt.libvirt.driver.LOG.debug')
    def test_check_discard_for_attach_volume_blk_controller_no_unmap(self,
                                                                     mock_log):
        self._test_check_discard(mock_log, driver_discard=None,
                                 bus='virtio', should_log=False)

    @mock.patch('nova.utils.get_image_from_system_metadata')
    @mock.patch('nova.virt.libvirt.blockinfo.get_info_from_bdm')
    @mock.patch('nova.virt.libvirt.host.Host.get_domain')
    def test_attach_volume_with_vir_domain_affect_live_flag(self,
            mock_get_domain, mock_get_info, get_image):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        image_meta = {}
        get_image.return_value = image_meta
        mock_dom = mock.MagicMock()
        mock_get_domain.return_value = mock_dom

        connection_info = {"driver_volume_type": "fake",
                           "data": {"device_path": "/fake",
                                    "access_mode": "rw"}}
        bdm = {'device_name': 'vdb',
               'disk_bus': 'fake-bus',
               'device_type': 'fake-type'}
        disk_info = {'bus': bdm['disk_bus'], 'type': bdm['device_type'],
                     'dev': 'vdb'}
        mock_get_info.return_value = disk_info
        mock_conf = mock.MagicMock()
        flags = (fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                 fakelibvirt.VIR_DOMAIN_AFFECT_LIVE)

        with test.nested(
            mock.patch.object(drvr, '_connect_volume'),
            mock.patch.object(drvr, '_get_volume_config',
                              return_value=mock_conf),
            mock.patch.object(drvr, '_set_cache_mode'),
            mock.patch.object(drvr, '_check_discard_for_attach_volume')
        ) as (mock_connect_volume, mock_get_volume_config,
              mock_set_cache_mode, mock_check_discard):
            for state in (power_state.RUNNING, power_state.PAUSED):
                mock_dom.info.return_value = [state, 512, 512, 2, 1234, 5678]

                drvr.attach_volume(self.context, connection_info, instance,
                                   "/dev/vdb", disk_bus=bdm['disk_bus'],
                                   device_type=bdm['device_type'])

                mock_get_domain.assert_called_with(instance)
                mock_get_info.assert_called_with(
                    instance,
                    CONF.libvirt.virt_type,
                    test.MatchType(objects.ImageMeta),
                    bdm)
                mock_connect_volume.assert_called_with(
                    connection_info, disk_info)
                mock_get_volume_config.assert_called_with(
                    connection_info, disk_info)
                mock_set_cache_mode.assert_called_with(mock_conf)
                mock_dom.attachDeviceFlags.assert_called_with(
                    mock_conf.to_xml(), flags=flags)
                mock_check_discard.assert_called_with(mock_conf, instance)

    @mock.patch('nova.virt.libvirt.host.Host.get_domain')
    def test_detach_volume_with_vir_domain_affect_live_flag(self,
            mock_get_domain):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        mock_xml_with_disk = """<domain>
  <devices>
    <disk type='file'>
      <source file='/path/to/fake-volume'/>
      <target dev='vdc' bus='virtio'/>
    </disk>
  </devices>
</domain>"""
        mock_xml_without_disk = """<domain>
  <devices>
  </devices>
</domain>"""
        mock_dom = mock.MagicMock()

        # Second time don't return anything about disk vdc so it looks removed
        return_list = [mock_xml_with_disk, mock_xml_without_disk]
        # Doubling the size of return list because we test with two guest power
        # states
        mock_dom.XMLDesc.side_effect = return_list + return_list

        connection_info = {"driver_volume_type": "fake",
                           "data": {"device_path": "/fake",
                                    "access_mode": "rw"}}
        flags = (fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                 fakelibvirt.VIR_DOMAIN_AFFECT_LIVE)

        with mock.patch.object(drvr, '_disconnect_volume') as \
                mock_disconnect_volume:
            for state in (power_state.RUNNING, power_state.PAUSED):
                mock_dom.info.return_value = [state, 512, 512, 2, 1234, 5678]
                mock_get_domain.return_value = mock_dom
                drvr.detach_volume(connection_info, instance, '/dev/vdc')

                mock_get_domain.assert_called_with(instance)
                mock_dom.detachDeviceFlags.assert_called_with("""<disk type="file" device="disk">
  <source file="/path/to/fake-volume"/>
  <target bus="virtio" dev="vdc"/>
</disk>
""", flags=flags)
                mock_disconnect_volume.assert_called_with(
                    connection_info, 'vdc')

    @mock.patch('nova.virt.libvirt.host.Host.get_domain')
    def test_detach_volume_disk_not_found(self, mock_get_domain):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        mock_xml_without_disk = """<domain>
  <devices>
  </devices>
</domain>"""
        mock_dom = mock.MagicMock(return_value=mock_xml_without_disk)

        connection_info = {"driver_volume_type": "fake",
                           "data": {"device_path": "/fake",
                                    "access_mode": "rw"}}

        mock_dom.info.return_value = [power_state.RUNNING, 512, 512, 2, 1234,
                                      5678]
        mock_get_domain.return_value = mock_dom
        self.assertRaises(exception.DiskNotFound, drvr.detach_volume,
                          connection_info, instance, '/dev/vdc')

        mock_get_domain.assert_called_once_with(instance)

    def test_multi_nic(self):
        network_info = _fake_network_info(self, 2)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        xml = drvr._get_guest_xml(self.context, instance_ref,
                                  network_info, disk_info,
                                  image_meta)
        tree = etree.fromstring(xml)
        interfaces = tree.findall("./devices/interface")
        self.assertEqual(len(interfaces), 2)
        self.assertEqual(interfaces[0].get('type'), 'bridge')

    def _behave_supports_direct_io(self, raise_open=False, raise_write=False,
                                   exc=ValueError()):
        open_behavior = os.open(os.path.join('.', '.directio.test'),
                                os.O_CREAT | os.O_WRONLY | os.O_DIRECT)
        if raise_open:
            open_behavior.AndRaise(exc)
        else:
            open_behavior.AndReturn(3)
            write_bahavior = os.write(3, mox.IgnoreArg())
            if raise_write:
                write_bahavior.AndRaise(exc)

            # ensure unlink(filepath) will actually remove the file by deleting
            # the remaining link to it in close(fd)
            os.close(3)

        os.unlink(3)

    def test_supports_direct_io(self):
        # O_DIRECT is not supported on all Python runtimes, so on platforms
        # where it's not supported (e.g. Mac), we can still test the code-path
        # by stubbing out the value.
        if not hasattr(os, 'O_DIRECT'):
            # `mock` seems to have trouble stubbing an attr that doesn't
            # originally exist, so falling back to stubbing out the attribute
            # directly.
            os.O_DIRECT = 16384
            self.addCleanup(delattr, os, 'O_DIRECT')

        einval = OSError()
        einval.errno = errno.EINVAL
        self.mox.StubOutWithMock(os, 'open')
        self.mox.StubOutWithMock(os, 'write')
        self.mox.StubOutWithMock(os, 'close')
        self.mox.StubOutWithMock(os, 'unlink')
        _supports_direct_io = libvirt_driver.LibvirtDriver._supports_direct_io

        self._behave_supports_direct_io()
        self._behave_supports_direct_io(raise_write=True)
        self._behave_supports_direct_io(raise_open=True)
        self._behave_supports_direct_io(raise_write=True, exc=einval)
        self._behave_supports_direct_io(raise_open=True, exc=einval)

        self.mox.ReplayAll()
        self.assertTrue(_supports_direct_io('.'))
        self.assertRaises(ValueError, _supports_direct_io, '.')
        self.assertRaises(ValueError, _supports_direct_io, '.')
        self.assertFalse(_supports_direct_io('.'))
        self.assertFalse(_supports_direct_io('.'))
        self.mox.VerifyAll()

    def _check_xml_and_container(self, instance):
        instance_ref = objects.Instance(**instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        self.assertEqual(drvr._uri(), 'lxc:///')

        network_info = _fake_network_info(self, 1)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        xml = drvr._get_guest_xml(self.context, instance_ref,
                                  network_info, disk_info,
                                  image_meta)
        tree = etree.fromstring(xml)

        check = [
            (lambda t: t.find('.').get('type'), 'lxc'),
            (lambda t: t.find('./os/type').text, 'exe'),
            (lambda t: t.find('./devices/filesystem/target').get('dir'), '/')]

        for i, (check, expected_result) in enumerate(check):
            self.assertEqual(check(tree),
                             expected_result,
                             '%s failed common check %d' % (xml, i))

        target = tree.find('./devices/filesystem/source').get('dir')
        self.assertGreater(len(target), 0)

    def _check_xml_and_disk_prefix(self, instance, prefix):
        instance_ref = objects.Instance(**instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        def _get_prefix(p, default):
            if p:
                return p + 'a'
            return default

        type_disk_map = {
            'qemu': [
                (lambda t: t.find('.').get('type'), 'qemu'),
                (lambda t: t.find('./devices/disk/target').get('dev'),
                 _get_prefix(prefix, 'vda'))],
            'xen': [
                (lambda t: t.find('.').get('type'), 'xen'),
                (lambda t: t.find('./devices/disk/target').get('dev'),
                 _get_prefix(prefix, 'xvda'))],
            'kvm': [
                (lambda t: t.find('.').get('type'), 'kvm'),
                (lambda t: t.find('./devices/disk/target').get('dev'),
                 _get_prefix(prefix, 'vda'))],
            'uml': [
                (lambda t: t.find('.').get('type'), 'uml'),
                (lambda t: t.find('./devices/disk/target').get('dev'),
                 _get_prefix(prefix, 'ubda'))]
            }

        for (virt_type, checks) in six.iteritems(type_disk_map):
            self.flags(virt_type=virt_type, group='libvirt')
            if prefix:
                self.flags(disk_prefix=prefix, group='libvirt')
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

            network_info = _fake_network_info(self, 1)
            disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                                instance_ref,
                                                image_meta)

            xml = drvr._get_guest_xml(self.context, instance_ref,
                                      network_info, disk_info,
                                      image_meta)
            tree = etree.fromstring(xml)

            for i, (check, expected_result) in enumerate(checks):
                self.assertEqual(check(tree),
                                 expected_result,
                                 '%s != %s failed check %d' %
                                 (check(tree), expected_result, i))

    def _check_xml_and_disk_driver(self, image_meta):
        os_open = os.open
        directio_supported = True

        def os_open_stub(path, flags, *args, **kwargs):
            if flags & os.O_DIRECT:
                if not directio_supported:
                    raise OSError(errno.EINVAL,
                                  '%s: %s' % (os.strerror(errno.EINVAL), path))
                flags &= ~os.O_DIRECT
            return os_open(path, flags, *args, **kwargs)

        self.stub_out('os.open', os_open_stub)

        @staticmethod
        def connection_supports_direct_io_stub(dirpath):
            return directio_supported

        self.stubs.Set(libvirt_driver.LibvirtDriver,
            '_supports_direct_io', connection_supports_direct_io_stub)

        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        network_info = _fake_network_info(self, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        xml = drv._get_guest_xml(self.context, instance_ref,
                                 network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for guest_disk in disks:
            self.assertEqual(guest_disk.get("cache"), "none")

        directio_supported = False

        # The O_DIRECT availability is cached on first use in
        # LibvirtDriver, hence we re-create it here
        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        xml = drv._get_guest_xml(self.context, instance_ref,
                                 network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        disks = tree.findall('./devices/disk/driver')
        for guest_disk in disks:
            self.assertEqual(guest_disk.get("cache"), "writethrough")

    def _check_xml_and_disk_bus(self, image_meta,
                                block_device_info, wantConfig):
        instance_ref = objects.Instance(**self.test_instance)
        network_info = _fake_network_info(self, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            block_device_info)

        xml = drv._get_guest_xml(self.context, instance_ref,
                                 network_info, disk_info, image_meta,
                                 block_device_info=block_device_info)
        tree = etree.fromstring(xml)

        got_disks = tree.findall('./devices/disk')
        got_disk_targets = tree.findall('./devices/disk/target')
        for i in range(len(wantConfig)):
            want_device_type = wantConfig[i][0]
            want_device_bus = wantConfig[i][1]
            want_device_dev = wantConfig[i][2]

            got_device_type = got_disks[i].get('device')
            got_device_bus = got_disk_targets[i].get('bus')
            got_device_dev = got_disk_targets[i].get('dev')

            self.assertEqual(got_device_type, want_device_type)
            self.assertEqual(got_device_bus, want_device_bus)
            self.assertEqual(got_device_dev, want_device_dev)

    def _check_xml_and_uuid(self, image_meta):
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        network_info = _fake_network_info(self, 1)

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)
        xml = drv._get_guest_xml(self.context, instance_ref,
                                 network_info, disk_info, image_meta)
        tree = etree.fromstring(xml)
        self.assertEqual(tree.find('./uuid').text,
                         instance_ref['uuid'])

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_get_host_sysinfo_serial_hardware",)
    def _check_xml_and_uri(self, instance, mock_serial,
                           expect_ramdisk=False, expect_kernel=False,
                           rescue=None, expect_xen_hvm=False, xen_only=False):
        mock_serial.return_value = "cef19ce0-0ca2-11df-855d-b19fbce37686"
        instance_ref = objects.Instance(**instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        xen_vm_mode = vm_mode.XEN
        if expect_xen_hvm:
            xen_vm_mode = vm_mode.HVM

        type_uri_map = {'qemu': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'qemu'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.HVM),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'kvm': ('qemu:///system',
                             [(lambda t: t.find('.').get('type'), 'kvm'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.HVM),
                              (lambda t: t.find('./devices/emulator'), None)]),
                        'uml': ('uml:///system',
                             [(lambda t: t.find('.').get('type'), 'uml'),
                              (lambda t: t.find('./os/type').text,
                               vm_mode.UML)]),
                        'xen': ('xen:///',
                             [(lambda t: t.find('.').get('type'), 'xen'),
                              (lambda t: t.find('./os/type').text,
                               xen_vm_mode)])}

        if expect_xen_hvm or xen_only:
            hypervisors_to_check = ['xen']
        else:
            hypervisors_to_check = ['qemu', 'kvm', 'xen']

        for hypervisor_type in hypervisors_to_check:
            check_list = type_uri_map[hypervisor_type][1]

            if rescue:
                suffix = '.rescue'
            else:
                suffix = ''
            if expect_kernel:
                check = (lambda t: self.relpath(t.find('./os/kernel').text).
                         split('/')[1], 'kernel' + suffix)
            else:
                check = (lambda t: t.find('./os/kernel'), None)
            check_list.append(check)

            if expect_kernel:
                check = (lambda t: "no_timer_check" in t.find('./os/cmdline').
                         text, hypervisor_type == "qemu")
                check_list.append(check)
            # Hypervisors that only support vm_mode.HVM and Xen
            # should not produce configuration that results in kernel
            # arguments
            if not expect_kernel and (hypervisor_type in
                                      ['qemu', 'kvm', 'xen']):
                check = (lambda t: t.find('./os/root'), None)
                check_list.append(check)
                check = (lambda t: t.find('./os/cmdline'), None)
                check_list.append(check)

            if expect_ramdisk:
                check = (lambda t: self.relpath(t.find('./os/initrd').text).
                         split('/')[1], 'ramdisk' + suffix)
            else:
                check = (lambda t: t.find('./os/initrd'), None)
            check_list.append(check)

            if hypervisor_type in ['qemu', 'kvm']:
                xpath = "./sysinfo/system/entry"
                check = (lambda t: t.findall(xpath)[0].get("name"),
                         "manufacturer")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[0].text,
                         version.vendor_string())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[1].get("name"),
                         "product")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[1].text,
                         version.product_string())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[2].get("name"),
                         "version")
                check_list.append(check)
                # NOTE(sirp): empty strings don't roundtrip in lxml (they are
                # converted to None), so we need an `or ''` to correct for that
                check = (lambda t: t.findall(xpath)[2].text or '',
                         version.version_string_with_package())
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[3].get("name"),
                         "serial")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[3].text,
                         "cef19ce0-0ca2-11df-855d-b19fbce37686")
                check_list.append(check)

                check = (lambda t: t.findall(xpath)[4].get("name"),
                         "uuid")
                check_list.append(check)
                check = (lambda t: t.findall(xpath)[4].text,
                         instance['uuid'])
                check_list.append(check)

            if hypervisor_type in ['qemu', 'kvm']:
                check = (lambda t: t.findall('./devices/serial')[0].get(
                        'type'), 'file')
                check_list.append(check)
                check = (lambda t: t.findall('./devices/serial')[1].get(
                        'type'), 'pty')
                check_list.append(check)
                check = (lambda t: self.relpath(t.findall(
                         './devices/serial/source')[0].get('path')).
                         split('/')[1], 'console.log')
                check_list.append(check)
            else:
                check = (lambda t: t.find('./devices/console').get(
                        'type'), 'pty')
                check_list.append(check)

        common_checks = [
            (lambda t: t.find('.').tag, 'domain'),
            (lambda t: t.find('./memory').text, '2097152')]
        if rescue:
            common_checks += [
                (lambda t: self.relpath(t.findall('./devices/disk/source')[0].
                    get('file')).split('/')[1], 'disk.rescue'),
                (lambda t: self.relpath(t.findall('./devices/disk/source')[1].
                    get('file')).split('/')[1], 'disk')]
        else:
            common_checks += [(lambda t: self.relpath(t.findall(
                './devices/disk/source')[0].get('file')).split('/')[1],
                               'disk')]
            common_checks += [(lambda t: self.relpath(t.findall(
                './devices/disk/source')[1].get('file')).split('/')[1],
                               'disk.local')]

        for virt_type in hypervisors_to_check:
            expected_uri = type_uri_map[virt_type][0]
            checks = type_uri_map[virt_type][1]
            self.flags(virt_type=virt_type, group='libvirt')

            with mock.patch('nova.virt.libvirt.driver.libvirt') as old_virt:
                del old_virt.VIR_CONNECT_BASELINE_CPU_EXPAND_FEATURES

                drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

                self.assertEqual(drvr._uri(), expected_uri)

                network_info = _fake_network_info(self, 1)
                disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                                    instance_ref,
                                                    image_meta,
                                                    rescue=rescue)

                xml = drvr._get_guest_xml(self.context, instance_ref,
                                          network_info, disk_info,
                                          image_meta,
                                          rescue=rescue)
                tree = etree.fromstring(xml)
                for i, (check, expected_result) in enumerate(checks):
                    self.assertEqual(check(tree),
                                     expected_result,
                                     '%s != %s failed check %d' %
                                     (check(tree), expected_result, i))

                for i, (check, expected_result) in enumerate(common_checks):
                    self.assertEqual(check(tree),
                                     expected_result,
                                     '%s != %s failed common check %d' %
                                     (check(tree), expected_result, i))

                filterref = './devices/interface/filterref'
                vif = network_info[0]
                nic_id = vif['address'].lower().replace(':', '')
                fw = firewall.NWFilterFirewall(drvr)
                instance_filter_name = fw._instance_filter_name(instance_ref,
                                                                nic_id)
                self.assertEqual(tree.find(filterref).get('filter'),
                                 instance_filter_name)

        # This test is supposed to make sure we don't
        # override a specifically set uri
        #
        # Deliberately not just assigning this string to CONF.connection_uri
        # and checking against that later on. This way we make sure the
        # implementation doesn't fiddle around with the CONF.
        testuri = 'something completely different'
        self.flags(connection_uri=testuri, group='libvirt')
        for (virt_type, (expected_uri, checks)) in six.iteritems(type_uri_map):
            self.flags(virt_type=virt_type, group='libvirt')
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
            self.assertEqual(drvr._uri(), testuri)

    def test_ensure_filtering_rules_for_instance_timeout(self):
        # ensure_filtering_fules_for_instance() finishes with timeout.
        # Preparing mocks
        def fake_none(self, *args):
            return

        class FakeTime(object):
            def __init__(self):
                self.counter = 0

            def sleep(self, t):
                self.counter += t

        fake_timer = FakeTime()

        def fake_sleep(t):
            fake_timer.sleep(t)

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self, 1)
        self.create_fake_libvirt_mock()
        instance_ref = objects.Instance(**self.test_instance)

        # Start test
        self.mox.ReplayAll()
        try:
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.stubs.Set(drvr.firewall_driver,
                           'setup_basic_filtering',
                           fake_none)
            self.stubs.Set(drvr.firewall_driver,
                           'prepare_instance_filter',
                           fake_none)
            self.stubs.Set(drvr.firewall_driver,
                           'instance_filter_exists',
                           fake_none)
            self.stubs.Set(greenthread,
                           'sleep',
                           fake_sleep)
            drvr.ensure_filtering_rules_for_instance(instance_ref,
                                                     network_info)
        except exception.NovaException as e:
            msg = ('The firewall filter for %s does not exist' %
                   instance_ref['name'])
            c1 = (0 <= six.text_type(e).find(msg))
        self.assertTrue(c1)

        self.assertEqual(29, fake_timer.counter, "Didn't wait the expected "
                                                 "amount of time")

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
        '_create_shared_storage_test_file')
    @mock.patch.object(fakelibvirt.Connection, 'compareCPU')
    def test_check_can_live_migrate_dest_all_pass_with_block_migration(
            self, mock_cpu, mock_test_file, mock_svc):
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.vcpu_model = test_vcpu_model.fake_vcpumodel
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'disk_available_least': 400,
                        'cpu_info': 'asdf',
                        }
        filename = "file"

        # _check_cpu_match
        mock_cpu.return_value = 1

        # mounted_on_same_shared_storage
        mock_test_file.return_value = filename

        # No need for the src_compute_info
        return_value = drvr.check_can_live_migrate_destination(self.context,
                instance_ref, None, compute_info, True)
        return_value.is_volume_backed = False
        self.assertThat({"filename": "file",
                         'image_type': 'default',
                         'disk_available_mb': 409600,
                         "disk_over_commit": False,
                         "block_migration": True,
                         "is_volume_backed": False},
                        matchers.DictMatches(return_value.to_legacy_dict()))

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
        '_create_shared_storage_test_file')
    @mock.patch.object(fakelibvirt.Connection, 'compareCPU')
    def test_check_can_live_migrate_dest_all_pass_no_block_migration(
            self, mock_cpu, mock_test_file, mock_svc):
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.vcpu_model = test_vcpu_model.fake_vcpumodel
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'disk_available_least': 400,
                        'cpu_info': 'asdf',
                        }
        filename = "file"

        # _check_cpu_match
        mock_cpu.return_value = 1
        # mounted_on_same_shared_storage
        mock_test_file.return_value = filename
        # No need for the src_compute_info
        return_value = drvr.check_can_live_migrate_destination(self.context,
                instance_ref, None, compute_info, False)
        return_value.is_volume_backed = False
        self.assertThat({"filename": "file",
                         "image_type": 'default',
                         "block_migration": False,
                         "disk_over_commit": False,
                         "disk_available_mb": 409600,
                         "is_volume_backed": False},
                        matchers.DictMatches(return_value.to_legacy_dict()))

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       '_create_shared_storage_test_file',
                       return_value='fake')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_compare_cpu')
    def test_check_can_live_migrate_guest_cpu_none_model(
            self, mock_cpu, mock_test_file):
        # Tests that when instance.vcpu_model.model is None, the host cpu
        # model is used for live migration.
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.vcpu_model = test_vcpu_model.fake_vcpumodel
        instance_ref.vcpu_model.model = None
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'cpu_info': 'asdf', 'disk_available_least': 1}
        result = drvr.check_can_live_migrate_destination(
            self.context, instance_ref, compute_info, compute_info)
        result.is_volume_backed = False
        mock_cpu.assert_called_once_with(None, 'asdf', instance_ref)
        expected_result = {"filename": 'fake',
                           "image_type": CONF.libvirt.images_type,
                           "block_migration": False,
                           "disk_over_commit": False,
                           "disk_available_mb": 1024,
                           "is_volume_backed": False}
        self.assertDictEqual(expected_result, result.to_legacy_dict())

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
        '_create_shared_storage_test_file')
    @mock.patch.object(fakelibvirt.Connection, 'compareCPU')
    def test_check_can_live_migrate_dest_no_instance_cpu_info(
            self, mock_cpu, mock_test_file, mock_svc):
        instance_ref = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'cpu_info': jsonutils.dumps({
            "vendor": "AMD",
            "arch": arch.I686,
            "features": ["sse3"],
            "model": "Opteron_G3",
            "topology": {"cores": 2, "threads": 1, "sockets": 4}
        }), 'disk_available_least': 1}
        filename = "file"

        # _check_cpu_match
        mock_cpu.return_value = 1
        # mounted_on_same_shared_storage
        mock_test_file.return_value = filename

        return_value = drvr.check_can_live_migrate_destination(self.context,
                instance_ref, compute_info, compute_info, False)
        # NOTE(danms): Compute manager would have set this, so set it here
        return_value.is_volume_backed = False
        self.assertThat({"filename": "file",
                         "image_type": 'default',
                         "block_migration": False,
                         "disk_over_commit": False,
                         "disk_available_mb": 1024,
                         "is_volume_backed": False},
                        matchers.DictMatches(return_value.to_legacy_dict()))

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    @mock.patch.object(fakelibvirt.Connection, 'compareCPU')
    def test_check_can_live_migrate_dest_incompatible_cpu_raises(
            self, mock_cpu, mock_svc):
        instance_ref = objects.Instance(**self.test_instance)
        instance_ref.vcpu_model = test_vcpu_model.fake_vcpumodel
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        compute_info = {'cpu_info': 'asdf', 'disk_available_least': 1}

        mock_cpu.side_effect = exception.InvalidCPUInfo(reason='foo')
        self.assertRaises(exception.InvalidCPUInfo,
                          drvr.check_can_live_migrate_destination,
                          self.context, instance_ref,
                          compute_info, compute_info, False)

    @mock.patch.object(host.Host, 'compare_cpu')
    @mock.patch.object(nova.virt.libvirt, 'config')
    def test_compare_cpu_compatible_host_cpu(self, mock_vconfig, mock_compare):
        instance = objects.Instance(**self.test_instance)
        mock_compare.return_value = 5
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = conn._compare_cpu(None, jsonutils.dumps(_fake_cpu_info),
                instance)
        self.assertIsNone(ret)

    @mock.patch.object(host.Host, 'compare_cpu')
    @mock.patch.object(nova.virt.libvirt, 'config')
    def test_compare_cpu_handles_not_supported_error_gracefully(self,
                                                                mock_vconfig,
                                                                mock_compare):
        instance = objects.Instance(**self.test_instance)
        not_supported_exc = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                'this function is not supported by the connection driver:'
                ' virCompareCPU',
                error_code=fakelibvirt.VIR_ERR_NO_SUPPORT)
        mock_compare.side_effect = not_supported_exc
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = conn._compare_cpu(None, jsonutils.dumps(_fake_cpu_info),
                instance)
        self.assertIsNone(ret)

    @mock.patch.object(host.Host, 'compare_cpu')
    @mock.patch.object(nova.virt.libvirt.LibvirtDriver,
                       '_vcpu_model_to_cpu_config')
    def test_compare_cpu_compatible_guest_cpu(self, mock_vcpu_to_cpu,
                                              mock_compare):
        instance = objects.Instance(**self.test_instance)
        mock_compare.return_value = 6
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = conn._compare_cpu(jsonutils.dumps(_fake_cpu_info), None,
                instance)
        self.assertIsNone(ret)

    def test_compare_cpu_virt_type_xen(self):
        instance = objects.Instance(**self.test_instance)
        self.flags(virt_type='xen', group='libvirt')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = conn._compare_cpu(None, None, instance)
        self.assertIsNone(ret)

    def test_compare_cpu_virt_type_qemu(self):
        instance = objects.Instance(**self.test_instance)
        self.flags(virt_type='qemu', group='libvirt')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ret = conn._compare_cpu(None, None, instance)
        self.assertIsNone(ret)

    @mock.patch.object(host.Host, 'compare_cpu')
    @mock.patch.object(nova.virt.libvirt, 'config')
    def test_compare_cpu_invalid_cpuinfo_raises(self, mock_vconfig,
                                                mock_compare):
        instance = objects.Instance(**self.test_instance)
        mock_compare.return_value = 0
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.InvalidCPUInfo,
                          conn._compare_cpu, None,
                          jsonutils.dumps(_fake_cpu_info),
                          instance)

    @mock.patch.object(host.Host, 'compare_cpu')
    @mock.patch.object(nova.virt.libvirt, 'config')
    def test_compare_cpu_incompatible_cpu_raises(self, mock_vconfig,
                                                 mock_compare):
        instance = objects.Instance(**self.test_instance)
        mock_compare.side_effect = fakelibvirt.libvirtError('cpu')
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.MigrationPreCheckError,
                          conn._compare_cpu, None,
                          jsonutils.dumps(_fake_cpu_info),
                          instance)

    def test_check_can_live_migrate_dest_cleanup_works_correctly(self):
        objects.Instance(**self.test_instance)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename="file",
            block_migration=True,
            disk_over_commit=False,
            disk_available_mb=1024)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(drvr, '_cleanup_shared_storage_test_file')
        drvr._cleanup_shared_storage_test_file("file")

        self.mox.ReplayAll()
        drvr.cleanup_live_migration_destination_check(self.context,
                                                      dest_check_data)

    def _mock_can_live_migrate_source(self, block_migration=False,
                                      is_shared_block_storage=False,
                                      is_shared_instance_path=False,
                                      is_booted_from_volume=False,
                                      disk_available_mb=1024,
                                      block_device_info=None,
                                      block_device_text=None):
        instance = objects.Instance(**self.test_instance)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename='file',
            image_type='default',
            block_migration=block_migration,
            disk_over_commit=False,
            disk_available_mb=disk_available_mb)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.mox.StubOutWithMock(drvr, '_is_shared_block_storage')
        drvr._is_shared_block_storage(instance, dest_check_data,
                block_device_info).AndReturn(is_shared_block_storage)
        self.mox.StubOutWithMock(drvr, '_check_shared_storage_test_file')
        drvr._check_shared_storage_test_file('file').AndReturn(
                is_shared_instance_path)
        self.mox.StubOutWithMock(drvr, "get_instance_disk_info")
        drvr.get_instance_disk_info(instance,
                                    block_device_info=block_device_info).\
                                    AndReturn(block_device_text)
        self.mox.StubOutWithMock(drvr, '_is_booted_from_volume')
        drvr._is_booted_from_volume(instance, block_device_text).AndReturn(
            is_booted_from_volume)

        return (instance, dest_check_data, drvr)

    def test_check_can_live_migrate_source_block_migration(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                block_migration=True)

        self.mox.StubOutWithMock(drvr, "_assert_dest_node_has_enough_disk")
        drvr._assert_dest_node_has_enough_disk(
            self.context, instance, dest_check_data.disk_available_mb,
            False, None)

        self.mox.ReplayAll()
        ret = drvr.check_can_live_migrate_source(self.context, instance,
                                                 dest_check_data)
        self.assertIsInstance(ret, objects.LibvirtLiveMigrateData)
        self.assertIn('is_shared_block_storage', ret)
        self.assertIn('is_shared_instance_path', ret)

    @mock.patch.object(fakelibvirt, 'VIR_DOMAIN_XML_MIGRATABLE', None,
                       create=True)
    def test_check_can_live_migrate_source_serial_fails(self):
        self.flags(enabled=True, group="serial_console")
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.MigrationError,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, {})

    @mock.patch.object(fakelibvirt, 'VIR_DOMAIN_XML_MIGRATABLE', None,
                       create=True)
    def test_check_can_live_migrate_source_graphics_fails(self):
        self.flags(enabled=True, vncserver_listen='1.2.3.4', group='vnc')
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.MigrationError,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, {})

    def test_check_can_live_migrate_source_shared_block_storage(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                is_shared_block_storage=True)
        self.mox.ReplayAll()
        drvr.check_can_live_migrate_source(self.context, instance,
                                           dest_check_data)

    def test_check_can_live_migrate_source_shared_instance_path(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                is_shared_instance_path=True)
        self.mox.ReplayAll()
        drvr.check_can_live_migrate_source(self.context, instance,
                                           dest_check_data)

    def test_check_can_live_migrate_source_non_shared_fails(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source()
        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          drvr.check_can_live_migrate_source, self.context,
                          instance, dest_check_data)

    def test_check_can_live_migrate_source_shared_block_migration_fails(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                block_migration=True,
                is_shared_block_storage=True)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidLocalStorage,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data)

    def test_check_can_live_migrate_shared_path_block_migration_fails(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                block_migration=True,
                is_shared_instance_path=True)

        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidLocalStorage,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data, None)

    def test_check_can_live_migrate_non_shared_non_block_migration_fails(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source()
        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data)

    def test_check_can_live_migrate_source_with_dest_not_enough_disk(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                block_migration=True,
                disk_available_mb=0)

        drvr.get_instance_disk_info(instance,
                                    block_device_info=None).AndReturn(
                                        '[{"virt_disk_size":2}]')

        self.mox.ReplayAll()
        self.assertRaises(exception.MigrationError,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data)

    def test_check_can_live_migrate_source_booted_from_volume(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                is_booted_from_volume=True,
                block_device_text='[]')
        self.mox.ReplayAll()
        drvr.check_can_live_migrate_source(self.context, instance,
                                           dest_check_data)

    def test_check_can_live_migrate_source_booted_from_volume_with_swap(self):
        instance, dest_check_data, drvr = self._mock_can_live_migrate_source(
                is_booted_from_volume=True,
                block_device_text='[{"path":"disk.swap"}]')
        self.mox.ReplayAll()
        self.assertRaises(exception.InvalidSharedStorage,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data)

    @mock.patch.object(host.Host, 'has_min_version', return_value=False)
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_assert_dest_node_has_enough_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_has_local_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                'get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_shared_block_storage', return_value=False)
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_check_shared_storage_test_file', return_value=False)
    def test_check_can_live_migrate_source_block_migration_with_bdm_error(
            self, mock_check, mock_shared_block, mock_get_bdi,
            mock_booted_from_volume, mock_has_local, mock_enough,
            mock_min_version):

        bdi = {'block_device_mapping': ['bdm']}
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename='file',
            image_type='default',
            block_migration=True,
            disk_over_commit=False,
            disk_available_mb=100)
        self.assertRaises(exception.MigrationPreCheckError,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data,
                          block_device_info=bdi)

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_assert_dest_node_has_enough_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_has_local_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                'get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_shared_block_storage', return_value=False)
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_check_shared_storage_test_file', return_value=False)
    def test_check_can_live_migrate_source_bm_with_bdm_tunnelled_error(
            self, mock_check, mock_shared_block, mock_get_bdi,
            mock_booted_from_volume, mock_has_local, mock_enough,
            mock_min_version):

        self.flags(live_migration_tunnelled=True,
                   group='libvirt')
        bdi = {'block_device_mapping': ['bdm']}
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename='file',
            image_type='default',
            block_migration=True,
            disk_over_commit=False,
            disk_available_mb=100)
        drvr._parse_migration_flags()
        self.assertRaises(exception.MigrationPreCheckError,
                          drvr.check_can_live_migrate_source,
                          self.context, instance, dest_check_data,
                          block_device_info=bdi)

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_assert_dest_node_has_enough_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_has_local_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                'get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_shared_block_storage')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_check_shared_storage_test_file')
    def _test_check_can_live_migrate_source_block_migration_none(
            self, block_migrate, is_shared_instance_path, is_share_block,
            mock_check, mock_shared_block, mock_get_bdi,
            mock_booted_from_volume, mock_has_local, mock_enough, mock_verson):

        mock_check.return_value = is_shared_instance_path
        mock_shared_block.return_value = is_share_block
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename='file',
            image_type='default',
            disk_over_commit=False,
            disk_available_mb=100)
        dest_check_data_ret = drvr.check_can_live_migrate_source(
                          self.context, instance, dest_check_data)
        self.assertEqual(block_migrate, dest_check_data_ret.block_migration)

    def test_check_can_live_migrate_source_block_migration_none_shared1(self):
        self._test_check_can_live_migrate_source_block_migration_none(
            False,
            True,
            False)

    def test_check_can_live_migrate_source_block_migration_none_shared2(self):
        self._test_check_can_live_migrate_source_block_migration_none(
            False,
            False,
            True)

    def test_check_can_live_migrate_source_block_migration_none_no_share(self):
        self._test_check_can_live_migrate_source_block_migration_none(
            True,
            False,
            False)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_assert_dest_node_has_enough_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_assert_dest_node_has_enough_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_has_local_disk')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_booted_from_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                'get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_is_shared_block_storage')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_check_shared_storage_test_file')
    def test_check_can_live_migration_source_disk_over_commit_none(self,
            mock_check, mock_shared_block, mock_get_bdi,
            mock_booted_from_volume, mock_has_local,
            mock_enough, mock_disk_check):

        mock_check.return_value = False
        mock_shared_block.return_value = False
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dest_check_data = objects.LibvirtLiveMigrateData(
            filename='file',
            image_type='default',
            disk_available_mb=100)

        drvr.check_can_live_migrate_source(
            self.context, instance, dest_check_data)

        self.assertFalse(mock_disk_check.called)

    def _is_shared_block_storage_test_create_mocks(self, disks):
        # Test data
        instance_xml = ("<domain type='kvm'><name>instance-0000000a</name>"
                        "<devices>{}</devices></domain>")
        disks_xml = ''
        for dsk in disks:
            if dsk['type'] is not 'network':
                disks_xml = ''.join([disks_xml,
                                "<disk type='{type}'>"
                                "<driver name='qemu' type='{driver}'/>"
                                "<source {source}='{source_path}'/>"
                                "<target dev='{target_dev}' bus='virtio'/>"
                                "</disk>".format(**dsk)])
            else:
                disks_xml = ''.join([disks_xml,
                                "<disk type='{type}'>"
                                "<driver name='qemu' type='{driver}'/>"
                                "<source protocol='{source_proto}'"
                                "name='{source_image}' >"
                                "<host name='hostname' port='7000'/>"
                                "<config file='/path/to/file'/>"
                                "</source>"
                                "<target dev='{target_dev}'"
                                "bus='ide'/>".format(**dsk)])

        # Preparing mocks
        mock_virDomain = mock.Mock(fakelibvirt.virDomain)
        mock_virDomain.XMLDesc = mock.Mock()
        mock_virDomain.XMLDesc.return_value = (instance_xml.format(disks_xml))

        mock_lookup = mock.Mock()

        def mock_lookup_side_effect(name):
            return mock_virDomain
        mock_lookup.side_effect = mock_lookup_side_effect

        mock_getsize = mock.Mock()
        mock_getsize.return_value = "10737418240"

        return (mock_getsize, mock_lookup)

    def test_is_shared_block_storage_rbd(self):
        self.flags(images_type='rbd', group='libvirt')
        bdi = {'block_device_mapping': []}
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_get_instance_disk_info = mock.Mock()
        data = objects.LibvirtLiveMigrateData(image_type='rbd')
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertTrue(drvr._is_shared_block_storage(instance, data,
                                  block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)
        self.assertTrue(drvr._is_storage_shared_with('foo', 'bar'))

    def test_is_shared_block_storage_lvm(self):
        self.flags(images_type='lvm', group='libvirt')
        bdi = {'block_device_mapping': []}
        instance = objects.Instance(**self.test_instance)
        mock_get_instance_disk_info = mock.Mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        data = objects.LibvirtLiveMigrateData(image_type='lvm',
                                              is_volume_backed=False,
                                              is_shared_instance_path=False)
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertFalse(drvr._is_shared_block_storage(
                                    instance, data,
                                    block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)

    def test_is_shared_block_storage_qcow2(self):
        self.flags(images_type='qcow2', group='libvirt')
        bdi = {'block_device_mapping': []}
        instance = objects.Instance(**self.test_instance)
        mock_get_instance_disk_info = mock.Mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        data = objects.LibvirtLiveMigrateData(image_type='qcow2',
                                              is_volume_backed=False,
                                              is_shared_instance_path=False)
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertFalse(drvr._is_shared_block_storage(
                                    instance, data,
                                    block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)

    def test_is_shared_block_storage_rbd_only_source(self):
        self.flags(images_type='rbd', group='libvirt')
        bdi = {'block_device_mapping': []}
        instance = objects.Instance(**self.test_instance)
        mock_get_instance_disk_info = mock.Mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        data = objects.LibvirtLiveMigrateData(is_shared_instance_path=False,
                                              is_volume_backed=False)
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertFalse(drvr._is_shared_block_storage(
                                  instance, data,
                                  block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)

    def test_is_shared_block_storage_rbd_only_dest(self):
        bdi = {'block_device_mapping': []}
        instance = objects.Instance(**self.test_instance)
        mock_get_instance_disk_info = mock.Mock()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        data = objects.LibvirtLiveMigrateData(image_type='rbd',
                                              is_volume_backed=False,
                                              is_shared_instance_path=False)
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertFalse(drvr._is_shared_block_storage(
                                    instance, data,
                                    block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)

    def test_is_shared_block_storage_volume_backed(self):
        disks = [{'type': 'block',
                 'driver': 'raw',
                 'source': 'dev',
                 'source_path': '/dev/disk',
                 'target_dev': 'vda'}]
        bdi = {'block_device_mapping': [
                  {'connection_info': 'info', 'mount_device': '/dev/vda'}]}
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        (mock_getsize, mock_lookup) =\
            self._is_shared_block_storage_test_create_mocks(disks)
        data = objects.LibvirtLiveMigrateData(is_volume_backed=True,
                                              is_shared_instance_path=False)
        with mock.patch.object(host.Host, 'get_domain', mock_lookup):
            self.assertTrue(drvr._is_shared_block_storage(instance, data,
                                  block_device_info = bdi))
        mock_lookup.assert_called_once_with(instance)

    def test_is_shared_block_storage_volume_backed_with_disk(self):
        disks = [{'type': 'block',
                 'driver': 'raw',
                 'source': 'dev',
                 'source_path': '/dev/disk',
                 'target_dev': 'vda'},
                {'type': 'file',
                 'driver': 'raw',
                 'source': 'file',
                 'source_path': '/instance/disk.local',
                 'target_dev': 'vdb'}]
        bdi = {'block_device_mapping': [
                  {'connection_info': 'info', 'mount_device': '/dev/vda'}]}
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        (mock_getsize, mock_lookup) =\
            self._is_shared_block_storage_test_create_mocks(disks)
        data = objects.LibvirtLiveMigrateData(is_volume_backed=True,
                                              is_shared_instance_path=False)
        with test.nested(
                mock.patch.object(os.path, 'getsize', mock_getsize),
                mock.patch.object(host.Host, 'get_domain', mock_lookup)):
            self.assertFalse(drvr._is_shared_block_storage(
                                    instance, data,
                                    block_device_info = bdi))
        mock_getsize.assert_called_once_with('/instance/disk.local')
        mock_lookup.assert_called_once_with(instance)

    def test_is_shared_block_storage_nfs(self):
        bdi = {'block_device_mapping': []}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_image_backend = mock.MagicMock()
        drvr.image_backend = mock_image_backend
        mock_backend = mock.MagicMock()
        mock_image_backend.backend.return_value = mock_backend
        mock_backend.is_file_in_instance_path.return_value = True
        mock_get_instance_disk_info = mock.Mock()
        data = objects.LibvirtLiveMigrateData(
            is_shared_instance_path=True,
            image_type='foo')
        with mock.patch.object(drvr, 'get_instance_disk_info',
                               mock_get_instance_disk_info):
            self.assertTrue(drvr._is_shared_block_storage(
                'instance', data, block_device_info=bdi))
        self.assertEqual(0, mock_get_instance_disk_info.call_count)

    def test_live_migration_update_graphics_xml(self):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)

        xml_tmpl = ("<domain type='kvm'>"
                    "<devices>"
                    "<graphics type='vnc' listen='{vnc}'>"
                    "<listen address='{vnc}'/>"
                    "</graphics>"
                    "<graphics type='spice' listen='{spice}'>"
                    "<listen address='{spice}'/>"
                    "</graphics>"
                    "</devices>"
                    "</domain>")

        initial_xml = xml_tmpl.format(vnc='1.2.3.4',
                                      spice='5.6.7.8')

        target_xml = xml_tmpl.format(vnc='10.0.0.1',
                                     spice='10.0.0.2')
        target_xml = etree.tostring(etree.fromstring(target_xml))

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        guest = libvirt_guest.Guest(vdmock)
        self.mox.StubOutWithMock(vdmock, "migrateToURI2")
        _bandwidth = CONF.libvirt.live_migration_bandwidth
        vdmock.XMLDesc(flags=fakelibvirt.VIR_DOMAIN_XML_MIGRATABLE).AndReturn(
                initial_xml)
        vdmock.migrateToURI2(drvr._live_migration_uri('dest'),
                             dxml=target_xml,
                             flags=mox.IgnoreArg(),
                             bandwidth=_bandwidth).AndRaise(
                                fakelibvirt.libvirtError("ERR"))

        # start test
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='10.0.0.1',
            graphics_listen_addr_spice='10.0.0.2',
            serial_listen_addr='127.0.0.1',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)
        self.mox.ReplayAll()
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance_ref, 'dest',
                          False, migrate_data, guest, [])

    def test_live_migration_update_volume_xml(self):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)
        target_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'cde.67890.opst-lun-Z')
        # start test
        connection_info = {
            u'driver_volume_type': u'iscsi',
            u'serial': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
            u'data': {
                u'access_mode': u'rw', u'target_discovered': False,
                u'target_iqn': u'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
                u'volume_id': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
                'device_path':
                u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
            },
        }
        bdm = objects.LibvirtLiveMigrateBDMInfo(
            serial='58a84f6d-3f0c-4e19-a0af-eb657b790657',
            bus='virtio', type='disk', dev='vdb',
            connection_info=connection_info)
        migrate_data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='',
            target_connect_addr=None,
            bdms=[bdm],
            block_migration=False)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        test_mock = mock.MagicMock()
        guest = libvirt_guest.Guest(test_mock)

        with mock.patch.object(libvirt_driver.LibvirtDriver, 'get_info') as \
                mget_info,\
                mock.patch.object(drvr._host, 'get_domain') as mget_domain,\
                mock.patch.object(fakelibvirt.virDomain, 'migrateToURI2'),\
                mock.patch.object(
                    libvirt_migrate, 'get_updated_guest_xml') as mupdate:

            mget_info.side_effect = exception.InstanceNotFound(
                                     instance_id='foo')
            mget_domain.return_value = test_mock
            test_mock.XMLDesc.return_value = target_xml
            self.assertFalse(drvr._live_migration_operation(
                             self.context, instance_ref, 'dest', False,
                             migrate_data, guest, []))
            mupdate.assert_called_once_with(
                guest, migrate_data, mock.ANY)

    def test_live_migration_with_valid_target_connect_addr(self):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)
        target_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'cde.67890.opst-lun-Z')
        # start test
        connection_info = {
            u'driver_volume_type': u'iscsi',
            u'serial': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
            u'data': {
                u'access_mode': u'rw', u'target_discovered': False,
                u'target_iqn': u'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
                u'volume_id': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
                'device_path':
                u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
            },
        }
        bdm = objects.LibvirtLiveMigrateBDMInfo(
            serial='58a84f6d-3f0c-4e19-a0af-eb657b790657',
            bus='virtio', type='disk', dev='vdb',
            connection_info=connection_info)
        migrate_data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='',
            target_connect_addr='127.0.0.2',
            bdms=[bdm],
            block_migration=False)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        test_mock = mock.MagicMock()
        guest = libvirt_guest.Guest(test_mock)

        with mock.patch.object(libvirt_migrate,
                               'get_updated_guest_xml') as mupdate:

            test_mock.XMLDesc.return_value = target_xml
            drvr._live_migration_operation(self.context, instance_ref,
                                           'dest', False, migrate_data,
                                           guest, [])
            test_mock.migrateToURI2.assert_called_once_with(
                'qemu+tcp://127.0.0.2/system',
                dxml=mupdate(), flags=0, bandwidth=0)

    def test_update_volume_xml(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        initial_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'abc.12345.opst-lun-X')
        target_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'cde.67890.opst-lun-Z')
        target_xml = etree.tostring(etree.fromstring(target_xml))
        serial = "58a84f6d-3f0c-4e19-a0af-eb657b790657"

        bdmi = objects.LibvirtLiveMigrateBDMInfo(serial=serial,
                                                 bus='virtio',
                                                 type='disk',
                                                 dev='vdb')
        bdmi.connection_info = {u'driver_volume_type': u'iscsi',
           'serial': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
            u'data': {u'access_mode': u'rw', u'target_discovered': False,
            u'target_iqn': u'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
            u'volume_id': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
           'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'}}

        conf = vconfig.LibvirtConfigGuestDisk()
        conf.source_device = bdmi.type
        conf.driver_name = "qemu"
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.target_dev = bdmi.dev
        conf.target_bus = bdmi.bus
        conf.serial = bdmi.connection_info.get('serial')
        conf.source_type = "block"
        conf.source_path = bdmi.connection_info['data'].get('device_path')

        guest = libvirt_guest.Guest(mock.MagicMock())
        with test.nested(
                mock.patch.object(drvr, '_get_volume_config',
                                  return_value=conf),
                mock.patch.object(guest, 'get_xml_desc',
                                  return_value=initial_xml)):
            config = libvirt_migrate.get_updated_guest_xml(guest,
                            objects.LibvirtLiveMigrateData(bdms=[bdmi]),
                            drvr._get_volume_config)
            parser = etree.XMLParser(remove_blank_text=True)
            config = etree.fromstring(config, parser)
            target_xml = etree.fromstring(target_xml, parser)
            self.assertEqual(etree.tostring(target_xml),
                             etree.tostring(config))

    def test_live_migration_uri(self):
        hypervisor_uri_map = (
            ('xen', 'xenmigr://%s/system'),
            ('kvm', 'qemu+tcp://%s/system'),
            ('qemu', 'qemu+tcp://%s/system'),
            # anything else will return None
            ('lxc', None),
            ('parallels', None),
        )
        dest = 'destination'
        for hyperv, uri in hypervisor_uri_map:
            self.flags(virt_type=hyperv, group='libvirt')
            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            if uri is not None:
                uri = uri % dest
                self.assertEqual(uri, drvr._live_migration_uri(dest))
            else:
                self.assertRaises(exception.LiveMigrationURINotAvailable,
                                  drvr._live_migration_uri,
                                  dest)

    def test_live_migration_uri_forced(self):
        dest = 'destination'
        for hyperv in ('kvm', 'xen'):
            self.flags(virt_type=hyperv, group='libvirt')

            forced_uri = 'foo://%s/bar'
            self.flags(live_migration_uri=forced_uri, group='libvirt')

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.assertEqual(forced_uri % dest, drvr._live_migration_uri(dest))

    def test_update_volume_xml_no_serial(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        xml_tmpl = """
        <domain type='kvm'>
          <devices>
            <disk type='block' device='disk'>
              <driver name='qemu' type='raw' cache='none'/>
              <source dev='{device_path}'/>
              <target bus='virtio' dev='vdb'/>
              <serial></serial>
              <address type='pci' domain='0x0' bus='0x0' slot='0x04' \
              function='0x0'/>
            </disk>
          </devices>
        </domain>
        """

        initial_xml = xml_tmpl.format(device_path='/dev/disk/by-path/'
                                      'ip-1.2.3.4:3260-iqn.'
                                      'abc.12345.opst-lun-X')
        target_xml = xml_tmpl.format(device_path='/dev/disk/by-path/'
                                     'ip-1.2.3.4:3260-iqn.'
                                     'abc.12345.opst-lun-X')
        target_xml = etree.tostring(etree.fromstring(target_xml))
        serial = "58a84f6d-3f0c-4e19-a0af-eb657b790657"
        connection_info = {
            u'driver_volume_type': u'iscsi',
            'serial': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
            u'data': {
                u'access_mode': u'rw', u'target_discovered': False,
                u'target_iqn': u'ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
                u'volume_id': u'58a84f6d-3f0c-4e19-a0af-eb657b790657',
                u'device_path':
                u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z',
            },
        }
        bdmi = objects.LibvirtLiveMigrateBDMInfo(serial=serial,
                                                 bus='virtio',
                                                 dev='vdb',
                                                 type='disk')
        bdmi.connection_info = connection_info

        conf = vconfig.LibvirtConfigGuestDisk()
        conf.source_device = bdmi.type
        conf.driver_name = "qemu"
        conf.driver_format = "raw"
        conf.driver_cache = "none"
        conf.target_dev = bdmi.dev
        conf.target_bus = bdmi.bus
        conf.serial = bdmi.connection_info.get('serial')
        conf.source_type = "block"
        conf.source_path = bdmi.connection_info['data'].get('device_path')

        guest = libvirt_guest.Guest(mock.MagicMock())
        with test.nested(
                mock.patch.object(drvr, '_get_volume_config',
                                  return_value=conf),
                mock.patch.object(guest, 'get_xml_desc',
                                  return_value=initial_xml)):
            config = libvirt_migrate.get_updated_guest_xml(guest,
                objects.LibvirtLiveMigrateData(bdms=[bdmi]),
                drvr._get_volume_config)
            self.assertEqual(target_xml, config)

    def test_update_volume_xml_no_connection_info(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        initial_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'abc.12345.opst-lun-X')
        target_xml = self.device_xml_tmpl.format(
            device_path='/dev/disk/by-path/'
            'ip-1.2.3.4:3260-iqn.'
            'abc.12345.opst-lun-X')
        target_xml = etree.tostring(etree.fromstring(target_xml))
        serial = "58a84f6d-3f0c-4e19-a0af-eb657b790657"
        bdmi = objects.LibvirtLiveMigrateBDMInfo(serial=serial,
                                                 dev='vdb',
                                                 type='disk',
                                                 bus='scsi',
                                                 format='qcow')
        bdmi.connection_info = {}
        conf = vconfig.LibvirtConfigGuestDisk()
        guest = libvirt_guest.Guest(mock.MagicMock())
        with test.nested(
                mock.patch.object(drvr, '_get_volume_config',
                                  return_value=conf),
                mock.patch.object(guest, 'get_xml_desc',
                                  return_value=initial_xml)):
            config = libvirt_migrate.get_updated_guest_xml(
                guest,
                objects.LibvirtLiveMigrateData(bdms=[bdmi]),
                drvr._get_volume_config)
            self.assertEqual(target_xml, config)

    @mock.patch.object(fakelibvirt.virDomain, "migrateToURI2")
    @mock.patch.object(fakelibvirt.virDomain, "XMLDesc")
    def test_live_migration_update_serial_console_xml(self, mock_xml,
                                                      mock_migrate):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_ref = self.test_instance

        xml_tmpl = ("<domain type='kvm'>"
                    "<devices>"
                    "<console type='tcp'>"
                    "<source mode='bind' host='{addr}' service='10000'/>"
                    "</console>"
                    "</devices>"
                    "</domain>")

        initial_xml = xml_tmpl.format(addr='9.0.0.1')

        target_xml = xml_tmpl.format(addr='9.0.0.12')
        target_xml = etree.tostring(etree.fromstring(target_xml))

        # Preparing mocks
        mock_xml.return_value = initial_xml
        mock_migrate.side_effect = fakelibvirt.libvirtError("ERR")

        # start test
        bandwidth = CONF.libvirt.live_migration_bandwidth
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='10.0.0.1',
            graphics_listen_addr_spice='10.0.0.2',
            serial_listen_addr='9.0.0.12',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)
        dom = fakelibvirt.virDomain
        guest = libvirt_guest.Guest(dom)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance_ref, 'dest',
                          False, migrate_data, guest, [])
        mock_xml.assert_called_once_with(
                flags=fakelibvirt.VIR_DOMAIN_XML_MIGRATABLE)
        mock_migrate.assert_called_once_with(
                drvr._live_migration_uri('dest'),
                dxml=target_xml, flags=mock.ANY, bandwidth=bandwidth)

    @mock.patch.object(fakelibvirt, 'VIR_DOMAIN_XML_MIGRATABLE', None,
                       create=True)
    def test_live_migration_uses_migrateToURI_without_migratable_flag(self):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        guest = libvirt_guest.Guest(vdmock)
        self.mox.StubOutWithMock(vdmock, "migrateToURI")
        _bandwidth = CONF.libvirt.live_migration_bandwidth
        vdmock.migrateToURI(drvr._live_migration_uri('dest'),
                            flags=mox.IgnoreArg(),
                            bandwidth=_bandwidth).AndRaise(
                                fakelibvirt.libvirtError("ERR"))

        # start test
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='0.0.0.0',
            graphics_listen_addr_spice='0.0.0.0',
            serial_listen_addr='127.0.0.1',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)
        self.mox.ReplayAll()
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance_ref, 'dest',
                          False, migrate_data, guest, [])

    def test_live_migration_uses_migrateToURI_without_dest_listen_addrs(self):
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        guest = libvirt_guest.Guest(vdmock)
        self.mox.StubOutWithMock(vdmock, "migrateToURI")
        _bandwidth = CONF.libvirt.live_migration_bandwidth
        vdmock.migrateToURI(drvr._live_migration_uri('dest'),
                            flags=mox.IgnoreArg(),
                            bandwidth=_bandwidth).AndRaise(
                                fakelibvirt.libvirtError("ERR"))

        # start test
        migrate_data = objects.LibvirtLiveMigrateData(
            serial_listen_addr='',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)
        self.mox.ReplayAll()
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance_ref, 'dest',
                          False, migrate_data, guest, [])

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(fakelibvirt.virDomain, "migrateToURI3")
    @mock.patch('nova.virt.libvirt.migration.get_updated_guest_xml',
                return_value='')
    @mock.patch('nova.virt.libvirt.guest.Guest.get_xml_desc',
                return_value='<xml></xml>')
    def test_live_migration_uses_migrateToURI3(
            self, mock_old_xml, mock_new_xml, mock_migrateToURI3,
            mock_min_version):
        # Preparing mocks
        disk_paths = ['vda', 'vdb']
        params = {
            'migrate_disks': ['vda', 'vdb'],
            'bandwidth': CONF.libvirt.live_migration_bandwidth,
            'destination_xml': '',
        }
        mock_migrateToURI3.side_effect = fakelibvirt.libvirtError("ERR")

        # Start test
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='0.0.0.0',
            graphics_listen_addr_spice='0.0.0.0',
            serial_listen_addr='127.0.0.1',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)

        dom = fakelibvirt.virDomain
        guest = libvirt_guest.Guest(dom)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance, 'dest',
                          False, migrate_data, guest, disk_paths)
        mock_migrateToURI3.assert_called_once_with(
            drvr._live_migration_uri('dest'),
            params=params, flags=0)

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(fakelibvirt.virDomain, "migrateToURI3")
    @mock.patch('nova.virt.libvirt.migration.get_updated_guest_xml',
                return_value='')
    @mock.patch('nova.virt.libvirt.guest.Guest.get_xml_desc', return_value='')
    def test_block_live_migration_tunnelled_migrateToURI3(
            self, mock_old_xml, mock_new_xml,
            mock_migrateToURI3, mock_min_version):
        self.flags(live_migration_tunnelled=True, group='libvirt')
        # Preparing mocks
        disk_paths = []
        params = {
            'bandwidth': CONF.libvirt.live_migration_bandwidth,
            'destination_xml': '',
        }
        # Start test
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='0.0.0.0',
            graphics_listen_addr_spice='0.0.0.0',
            serial_listen_addr='127.0.0.1',
            target_connect_addr=None,
            bdms=[],
            block_migration=True)

        dom = fakelibvirt.virDomain
        guest = libvirt_guest.Guest(dom)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._parse_migration_flags()
        instance = objects.Instance(**self.test_instance)
        drvr._live_migration_operation(self.context, instance, 'dest',
                          True, migrate_data, guest, disk_paths)
        mock_migrateToURI3.assert_called_once_with(
            drvr._live_migration_uri('dest'),
            params=params, flags=151)

    def test_live_migration_raises_exception(self):
        # Confirms recover method is called when exceptions are raised.
        # Preparing data
        self.compute = importutils.import_object(CONF.compute_manager)
        instance_dict = dict(self.test_instance)
        instance_dict.update({'host': 'fake',
                              'power_state': power_state.RUNNING,
                              'vm_state': vm_states.ACTIVE})
        instance_ref = objects.Instance(**instance_dict)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        guest = libvirt_guest.Guest(vdmock)
        self.mox.StubOutWithMock(vdmock, "migrateToURI2")
        _bandwidth = CONF.libvirt.live_migration_bandwidth
        if getattr(fakelibvirt, 'VIR_DOMAIN_XML_MIGRATABLE', None) is None:
            vdmock.migrateToURI(drvr._live_migration_uri('dest'),
                                flags=mox.IgnoreArg(),
                                bandwidth=_bandwidth).AndRaise(
                                        fakelibvirt.libvirtError('ERR'))
        else:
            vdmock.XMLDesc(flags=fakelibvirt.VIR_DOMAIN_XML_MIGRATABLE
            ).AndReturn(FakeVirtDomain().XMLDesc(flags=0))
            vdmock.migrateToURI2(drvr._live_migration_uri('dest'),
                                 dxml=mox.IgnoreArg(),
                                 flags=mox.IgnoreArg(),
                                 bandwidth=_bandwidth).AndRaise(
                                         fakelibvirt.libvirtError('ERR'))

        # start test
        migrate_data = objects.LibvirtLiveMigrateData(
            graphics_listen_addr_vnc='127.0.0.1',
            graphics_listen_addr_spice='127.0.0.1',
            serial_listen_addr='127.0.0.1',
            target_connect_addr=None,
            bdms=[],
            block_migration=False)
        self.mox.ReplayAll()
        self.assertRaises(fakelibvirt.libvirtError,
                          drvr._live_migration_operation,
                          self.context, instance_ref, 'dest',
                          False, migrate_data, guest, [])

        self.assertEqual(vm_states.ACTIVE, instance_ref.vm_state)
        self.assertEqual(power_state.RUNNING, instance_ref.power_state)

    @mock.patch('shutil.rmtree')
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('nova.virt.libvirt.utils.get_instance_path_at_destination')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.destroy')
    def test_rollback_live_migration_at_dest_not_shared(self, mock_destroy,
                                                        mock_get_instance_path,
                                                        mock_exist,
                                                        mock_shutil
                                                        ):
        # destroy method may raise InstanceTerminationFailure or
        # InstancePowerOffFailure, here use their base class Invalid.
        mock_destroy.side_effect = exception.Invalid(reason='just test')
        fake_instance_path = os.path.join(cfg.CONF.instances_path,
                                          '/fake_instance_uuid')
        mock_get_instance_path.return_value = fake_instance_path
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_instance_path=False,
            instance_relative_path=False)
        self.assertRaises(exception.Invalid,
                          drvr.rollback_live_migration_at_destination,
                          "context", "instance", [], None, True, migrate_data)
        mock_exist.assert_called_once_with(fake_instance_path)
        mock_shutil.assert_called_once_with(fake_instance_path)

    @mock.patch('shutil.rmtree')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path_at_destination')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.destroy')
    def test_rollback_live_migration_at_dest_shared(self, mock_destroy,
                                                    mock_get_instance_path,
                                                    mock_exist,
                                                    mock_shutil
                                                    ):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        migrate_data = objects.LibvirtLiveMigrateData(
            is_shared_instance_path=True,
            instance_relative_path=False)
        drvr.rollback_live_migration_at_destination("context", "instance", [],
                                                    None, True, migrate_data)
        mock_destroy.assert_called_once_with("context", "instance", [],
                                             None, True, migrate_data)
        self.assertFalse(mock_get_instance_path.called)
        self.assertFalse(mock_exist.called)
        self.assertFalse(mock_shutil.called)

    @mock.patch.object(host.Host, "get_connection")
    @mock.patch.object(host.Host, "has_min_version", return_value=False)
    @mock.patch.object(fakelibvirt.Domain, "XMLDesc")
    def test_live_migration_copy_disk_paths(self, mock_xml, mock_version,
                                            mock_conn):
        xml = """
        <domain>
          <name>dummy</name>
          <uuid>d4e13113-918e-42fe-9fc9-861693ffd432</uuid>
          <devices>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.root"/>
               <target dev="vda"/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.shared"/>
               <target dev="vdb"/>
               <shareable/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.config"/>
               <target dev="vdc"/>
               <readonly/>
            </disk>
            <disk type="block">
               <source dev="/dev/mapper/somevol"/>
               <target dev="vdd"/>
            </disk>
            <disk type="network">
               <source protocol="https" name="url_path">
                 <host name="hostname" port="443"/>
               </source>
            </disk>
          </devices>
        </domain>"""
        mock_xml.return_value = xml

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dom = fakelibvirt.Domain(drvr._get_connection(), xml, False)
        guest = libvirt_guest.Guest(dom)

        paths = drvr._live_migration_copy_disk_paths(None, None, guest)
        self.assertEqual((["/var/lib/nova/instance/123/disk.root",
                          "/dev/mapper/somevol"], ['vda', 'vdd']), paths)

    @mock.patch.object(fakelibvirt.Domain, "XMLDesc")
    def test_live_migration_copy_disk_paths_tunnelled(self, mock_xml):
        self.flags(live_migration_tunnelled=True, group='libvirt')
        xml = """
        <domain>
          <name>dummy</name>
          <uuid>d4e13113-918e-42fe-9fc9-861693ffd432</uuid>
          <devices>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.root"/>
               <target dev="vda"/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.shared"/>
               <target dev="vdb"/>
               <shareable/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.config"/>
               <target dev="vdc"/>
               <readonly/>
            </disk>
            <disk type="block">
               <source dev="/dev/mapper/somevol"/>
               <target dev="vdd"/>
            </disk>
            <disk type="network">
               <source protocol="https" name="url_path">
                 <host name="hostname" port="443"/>
               </source>
            </disk>
          </devices>
        </domain>"""
        mock_xml.return_value = xml

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._parse_migration_flags()
        dom = fakelibvirt.Domain(drvr._get_connection(), xml, False)
        guest = libvirt_guest.Guest(dom)

        paths = drvr._live_migration_copy_disk_paths(None, None, guest)
        self.assertEqual((["/var/lib/nova/instance/123/disk.root",
                          "/dev/mapper/somevol"], ['vda', 'vdd']), paths)

    @mock.patch.object(host.Host, "get_connection")
    @mock.patch.object(host.Host, "has_min_version", return_value=True)
    @mock.patch('nova.virt.driver.get_block_device_info')
    @mock.patch('nova.objects.BlockDeviceMappingList.get_by_instance_uuid')
    @mock.patch.object(fakelibvirt.Domain, "XMLDesc")
    def test_live_migration_copy_disk_paths_selective_block_migration(
            self, mock_xml, mock_get_instance,
            mock_block_device_info, mock_version, mock_conn):
        xml = """
        <domain>
          <name>dummy</name>
          <uuid>d4e13113-918e-42fe-9fc9-861693ffd432</uuid>
          <devices>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.root"/>
               <target dev="vda"/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.shared"/>
               <target dev="vdb"/>
            </disk>
            <disk type="file">
               <source file="/var/lib/nova/instance/123/disk.config"/>
               <target dev="vdc"/>
            </disk>
            <disk type="block">
               <source dev="/dev/mapper/somevol"/>
               <target dev="vdd"/>
            </disk>
            <disk type="network">
               <source protocol="https" name="url_path">
                 <host name="hostname" port="443"/>
               </source>
            </disk>
          </devices>
        </domain>"""
        mock_xml.return_value = xml
        instance = objects.Instance(**self.test_instance)
        instance.root_device_name = '/dev/vda'
        block_device_info = {
            'swap': {
                'disk_bus': u'virtio',
                'swap_size': 10,
                'device_name': u'/dev/vdc'
            },
            'root_device_name': u'/dev/vda',
            'ephemerals': [{
                'guest_format': u'ext3',
                'device_name': u'/dev/vdb',
                'disk_bus': u'virtio',
                'device_type': u'disk',
                'size': 1
            }],
            'block_device_mapping': [{
                'guest_format': None,
                'boot_index': None,
                'mount_device': u'/dev/vdd',
                'connection_info': {
                    u'driver_volume_type': u'iscsi',
                    'serial': u'147df29f-aec2-4851-b3fe-f68dad151834',
                    u'data': {
                        u'access_mode': u'rw',
                        u'target_discovered': False,
                        u'encrypted': False,
                        u'qos_specs': None,
                        u'target_iqn': u'iqn.2010-10.org.openstack:'
                                       u'volume-147df29f-aec2-4851-b3fe-'
                                       u'f68dad151834',
                        u'target_portal': u'10.102.44.141:3260', u'volume_id':
                            u'147df29f-aec2-4851-b3fe-f68dad151834',
                        u'target_lun': 1,
                        u'auth_password': u'cXELT66FngwzTwpf',
                        u'auth_username': u'QbQQjj445uWgeQkFKcVw',
                        u'auth_method': u'CHAP'
                    }
                },
                'disk_bus': None,
                'device_type': None,
                'delete_on_termination': False
            }]
        }
        mock_block_device_info.return_value = block_device_info
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dom = fakelibvirt.Domain(drvr._get_connection(), xml, False)
        guest = libvirt_guest.Guest(dom)
        return_value = drvr._live_migration_copy_disk_paths(context, instance,
                                                            guest)
        expected = (['/var/lib/nova/instance/123/disk.root',
                     '/var/lib/nova/instance/123/disk.shared',
                     '/var/lib/nova/instance/123/disk.config'],
                    ['vda', 'vdb', 'vdc'])
        self.assertEqual(expected, return_value)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_live_migration_copy_disk_paths")
    def test_live_migration_data_gb_plain(self, mock_paths):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)

        data_gb = drvr._live_migration_data_gb(instance, [])
        self.assertEqual(2, data_gb)
        self.assertEqual(0, mock_paths.call_count)

    def test_live_migration_data_gb_block(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)

        def fake_stat(path):
            class StatResult(object):
                def __init__(self, size):
                    self._size = size

                @property
                def st_size(self):
                    return self._size

            if path == "/var/lib/nova/instance/123/disk.root":
                return StatResult(10 * units.Gi)
            elif path == "/dev/mapper/somevol":
                return StatResult(1.5 * units.Gi)
            else:
                raise Exception("Should not be reached")

        disk_paths = ["/var/lib/nova/instance/123/disk.root",
                      "/dev/mapper/somevol"]
        with mock.patch.object(os, "stat") as mock_stat:
            mock_stat.side_effect = fake_stat
            data_gb = drvr._live_migration_data_gb(instance, disk_paths)
            # Expecting 2 GB for RAM, plus 10 GB for disk.root
            # and 1.5 GB rounded to 2 GB for somevol, so 14 GB
            self.assertEqual(14, data_gb)

    EXPECT_SUCCESS = 1
    EXPECT_FAILURE = 2
    EXPECT_ABORT = 3

    @mock.patch.object(libvirt_guest.Guest, "migrate_start_postcopy")
    @mock.patch.object(time, "time")
    @mock.patch.object(time, "sleep",
                       side_effect=lambda x: eventlet.sleep(0))
    @mock.patch.object(host.Host, "get_connection")
    @mock.patch.object(libvirt_guest.Guest, "get_job_info")
    @mock.patch.object(objects.Instance, "save")
    @mock.patch.object(objects.Migration, "save")
    @mock.patch.object(fakelibvirt.Connection, "_mark_running")
    @mock.patch.object(fakelibvirt.virDomain, "abortJob")
    @mock.patch.object(libvirt_guest.Guest, "pause")
    def _test_live_migration_monitoring(self,
                                        job_info_records,
                                        time_records,
                                        expect_result,
                                        mock_pause,
                                        mock_abort,
                                        mock_running,
                                        mock_save,
                                        mock_mig_save,
                                        mock_job_info,
                                        mock_conn,
                                        mock_sleep,
                                        mock_time,
                                        mock_postcopy_switch,
                                        current_mig_status=None,
                                        expected_mig_status=None,
                                        scheduled_action=None,
                                        scheduled_action_executed=False,
                                        block_migration=False,
                                        expected_switch=False):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        drvr.active_migrations[instance.uuid] = deque()
        dom = fakelibvirt.Domain(drvr._get_connection(), "<domain/>", True)
        guest = libvirt_guest.Guest(dom)
        finish_event = eventlet.event.Event()

        def fake_job_info():
            while True:
                self.assertGreater(len(job_info_records), 0)
                rec = job_info_records.pop(0)

                if type(rec) == str:
                    if rec == "thread-finish":
                        finish_event.send()
                    elif rec == "domain-stop":
                        dom.destroy()
                    elif rec == "force_complete":
                        drvr.active_migrations[instance.uuid].append(
                            "force-complete")
                else:
                    if len(time_records) > 0:
                        time_records.pop(0)
                    return rec
            return rec

        def fake_time():
            if len(time_records) > 0:
                return time_records[0]
            else:
                return int(
                    datetime.datetime(2001, 1, 20, 20, 1, 0)
                    .strftime('%s'))

        mock_job_info.side_effect = fake_job_info
        mock_time.side_effect = fake_time

        dest = mock.sentinel.migrate_dest
        migration = objects.Migration(context=self.context, id=1)
        migrate_data = objects.LibvirtLiveMigrateData(
            migration=migration, block_migration=block_migration)

        if current_mig_status:
            migrate_data.migration.status = current_mig_status
        else:
            migrate_data.migration.status = "unset"
        migrate_data.migration.save()

        fake_post_method = mock.MagicMock()
        fake_recover_method = mock.MagicMock()
        drvr._live_migration_monitor(self.context, instance,
                                     guest, dest,
                                     fake_post_method,
                                     fake_recover_method,
                                     False,
                                     migrate_data,
                                     finish_event,
                                     [])
        if scheduled_action_executed:
            if scheduled_action == 'pause':
                self.assertTrue(mock_pause.called)
            if scheduled_action == 'postcopy_switch':
                self.assertTrue(mock_postcopy_switch.called)
        else:
            if scheduled_action == 'pause':
                self.assertFalse(mock_pause.called)
            if scheduled_action == 'postcopy_switch':
                self.assertFalse(mock_postcopy_switch.called)
        mock_mig_save.assert_called_with()

        if expect_result == self.EXPECT_SUCCESS:
            self.assertFalse(fake_recover_method.called,
                             'Recover method called when success expected')
            self.assertFalse(mock_abort.called,
                             'abortJob not called when success expected')
            if expected_switch:
                self.assertTrue(mock_postcopy_switch.called)
            fake_post_method.assert_called_once_with(
                self.context, instance, dest, False, migrate_data)
        else:
            if expect_result == self.EXPECT_ABORT:
                self.assertTrue(mock_abort.called,
                                'abortJob called when abort expected')
            else:
                self.assertFalse(mock_abort.called,
                                 'abortJob not called when failure expected')
            self.assertFalse(fake_post_method.called,
                             'Post method called when success not expected')
            if expected_mig_status:
                fake_recover_method.assert_called_once_with(
                    self.context, instance, dest, False, migrate_data,
                    migration_status=expected_mig_status)
            else:
                fake_recover_method.assert_called_once_with(
                    self.context, instance, dest, False, migrate_data)
        self.assertNotIn(instance.uuid, drvr.active_migrations)

    def test_live_migration_monitor_success(self):
        # A normal sequence where see all the normal job states
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS)

    def test_live_migration_handle_pause_normal(self):
        # A normal sequence where see all the normal job states, and pause
        # scheduled in between VIR_DOMAIN_JOB_UNBOUNDED
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS,
                                             current_mig_status="running",
                                             scheduled_action="pause",
                                             scheduled_action_executed=True)

    def test_live_migration_handle_pause_on_start(self):
        # A normal sequence where see all the normal job states, and pause
        # scheduled in case of job type VIR_DOMAIN_JOB_NONE and finish_event is
        # not ready yet
        domain_info_records = [
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS,
                                             current_mig_status="preparing",
                                             scheduled_action="pause",
                                             scheduled_action_executed=True)

    def test_live_migration_handle_pause_on_finish(self):
        # A normal sequence where see all the normal job states, and pause
        # scheduled in case of job type VIR_DOMAIN_JOB_NONE and finish_event is
        # ready
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS,
                                             current_mig_status="completed",
                                             scheduled_action="pause",
                                             scheduled_action_executed=False)

    def test_live_migration_handle_pause_on_cancel(self):
        # A normal sequence where see all the normal job states, and pause
        # scheduled in case of job type VIR_DOMAIN_JOB_CANCELLED
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_CANCELLED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_FAILURE,
                                             current_mig_status="cancelled",
                                             expected_mig_status='cancelled',
                                             scheduled_action="pause",
                                             scheduled_action_executed=False)

    def test_live_migration_handle_pause_on_failure(self):
        # A normal sequence where see all the normal job states, and pause
        # scheduled in case of job type VIR_DOMAIN_JOB_FAILED
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_FAILED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_FAILURE,
                                             scheduled_action="pause",
                                             scheduled_action_executed=False)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_normal(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and postcopy
        # switch scheduled in between VIR_DOMAIN_JOB_UNBOUNDED
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_SUCCESS,
                current_mig_status="running",
                scheduled_action="postcopy_switch",
                scheduled_action_executed=True)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_on_start(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and postcopy
        # switch scheduled in case of job type VIR_DOMAIN_JOB_NONE and
        # finish_event is not ready yet
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_SUCCESS,
                current_mig_status="preparing",
                scheduled_action="postcopy_switch",
                scheduled_action_executed=True)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_on_finish(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and postcopy
        # switch scheduled in case of job type VIR_DOMAIN_JOB_NONE and
        # finish_event is ready
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_SUCCESS,
                current_mig_status="completed",
                scheduled_action="postcopy_switch",
                scheduled_action_executed=False)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_on_cancel(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and postcopy
        # scheduled in case of job type VIR_DOMAIN_JOB_CANCELLED
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_CANCELLED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_FAILURE,
                current_mig_status="cancelled",
                expected_mig_status='cancelled',
                scheduled_action="postcopy_switch",
                scheduled_action_executed=False)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_pause_on_postcopy(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and pause
        # scheduled after migration switched to postcopy
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_SUCCESS,
                current_mig_status="running (post-copy)",
                scheduled_action="pause",
                scheduled_action_executed=False)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_on_postcopy(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and pause
        # scheduled after migration switched to postcopy
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_SUCCESS,
                current_mig_status="running (post-copy)",
                scheduled_action="postcopy_switch",
                scheduled_action_executed=False)

    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_handle_postcopy_on_failure(self,
            mock_postcopy_enabled):
        # A normal sequence where see all the normal job states, and postcopy
        # scheduled in case of job type VIR_DOMAIN_JOB_FAILED
        mock_postcopy_enabled.return_value = True
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            "force_complete",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_FAILED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                self.EXPECT_FAILURE,
                scheduled_action="postcopy_switch",
                scheduled_action_executed=False)

    def test_live_migration_monitor_success_race(self):
        # A normalish sequence but we're too slow to see the
        # completed job state
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS)

    def test_live_migration_monitor_failed(self):
        # A failed sequence where we see all the expected events
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_FAILED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_FAILURE)

    def test_live_migration_monitor_failed_race(self):
        # A failed sequence where we are too slow to see the
        # failed event
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_FAILURE)

    def test_live_migration_monitor_cancelled(self):
        # A cancelled sequence where we see all the events
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_CANCELLED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_FAILURE,
                                             expected_mig_status='cancelled')

    @mock.patch.object(fakelibvirt.virDomain, "migrateSetMaxDowntime")
    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_migration_downtime_steps")
    def test_live_migration_monitor_downtime(self, mock_downtime_steps,
                                             mock_set_downtime):
        self.flags(live_migration_completion_timeout=1000000,
                   live_migration_progress_timeout=1000000,
                   group='libvirt')
        # We've setup 4 fake downtime steps - first value is the
        # time delay, second is the downtime value
        downtime_steps = [
            (90, 10),
            (180, 50),
            (270, 200),
            (500, 300),
        ]
        mock_downtime_steps.return_value = downtime_steps

        # Each one of these fake times is used for time.time()
        # when a new domain_info_records entry is consumed.
        # Times are chosen so that only the first 3 downtime
        # steps are needed.
        fake_times = [0, 1, 30, 95, 150, 200, 300]

        # A normal sequence where see all the normal job states
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records,
                                             fake_times, self.EXPECT_SUCCESS)

        mock_set_downtime.assert_has_calls([mock.call(10),
                                            mock.call(50),
                                            mock.call(200)])

    def test_live_migration_monitor_completion(self):
        self.flags(live_migration_completion_timeout=100,
                   live_migration_progress_timeout=1000000,
                   group='libvirt')
        # Each one of these fake times is used for time.time()
        # when a new domain_info_records entry is consumed.
        fake_times = [0, 40, 80, 120, 160, 200, 240, 280, 320]

        # A normal sequence where see all the normal job states
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_CANCELLED),
        ]

        self._test_live_migration_monitoring(domain_info_records,
                                             fake_times, self.EXPECT_ABORT,
                                             expected_mig_status='cancelled')

    def test_live_migration_monitor_progress(self):
        self.flags(live_migration_completion_timeout=1000000,
                   live_migration_progress_timeout=150,
                   group='libvirt')
        # Each one of these fake times is used for time.time()
        # when a new domain_info_records entry is consumed.
        fake_times = [0, 40, 80, 120, 160, 200, 240, 280, 320]

        # A normal sequence where see all the normal job states
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_CANCELLED),
        ]

        self._test_live_migration_monitoring(domain_info_records,
                                             fake_times, self.EXPECT_ABORT,
                                             expected_mig_status='cancelled')

    def test_live_migration_monitor_progress_zero_data_remaining(self):
        self.flags(live_migration_completion_timeout=1000000,
                   live_migration_progress_timeout=150,
                   group='libvirt')
        # Each one of these fake times is used for time.time()
        # when a new domain_info_records entry is consumed.
        fake_times = [0, 40, 80, 120, 160, 200, 240, 280, 320]

        # A normal sequence where see all the normal job states
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=0),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=90),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=70),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=50),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=30),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=10),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED, data_remaining=0),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_FAILED),
        ]

        self._test_live_migration_monitoring(domain_info_records,
                                             fake_times, self.EXPECT_FAILURE)

    def test_live_migration_downtime_steps(self):
        self.flags(live_migration_downtime=400, group='libvirt')
        self.flags(live_migration_downtime_steps=10, group='libvirt')
        self.flags(live_migration_downtime_delay=30, group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        steps = drvr._migration_downtime_steps(3.0)

        self.assertEqual([
            (0, 37),
            (90, 38),
            (180, 39),
            (270, 42),
            (360, 46),
            (450, 55),
            (540, 70),
            (630, 98),
            (720, 148),
            (810, 238),
            (900, 400),
        ], list(steps))

    @mock.patch('nova.virt.libvirt.migration.should_switch_to_postcopy')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_is_post_copy_enabled")
    def test_live_migration_monitor_postcopy_switch(self,
            mock_postcopy_enabled, mock_should_switch):
        # A normal sequence where migration is swtiched to postcopy mode
        mock_postcopy_enabled.return_value = True
        switch_values = [False, False, True]
        mock_should_switch.return_value = switch_values
        domain_info_records = [
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_NONE),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_UNBOUNDED),
            "thread-finish",
            "domain-stop",
            libvirt_guest.JobInfo(
                type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED),
        ]

        self._test_live_migration_monitoring(domain_info_records, [],
                                             self.EXPECT_SUCCESS,
                                             expected_switch=True)

    @mock.patch.object(host.Host, "get_connection")
    @mock.patch.object(utils, "spawn")
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_live_migration_monitor")
    @mock.patch.object(host.Host, "get_guest")
    @mock.patch.object(fakelibvirt.Connection, "_mark_running")
    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       "_live_migration_copy_disk_paths")
    def test_live_migration_main(self, mock_copy_disk_path, mock_running,
                                 mock_guest, mock_monitor, mock_thread,
                                 mock_conn):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        dom = fakelibvirt.Domain(drvr._get_connection(),
                                 "<domain><name>demo</name></domain>", True)
        guest = libvirt_guest.Guest(dom)
        migrate_data = objects.LibvirtLiveMigrateData(block_migration=True)
        disks_to_copy = (['/some/path/one', '/test/path/two'],
                         ['vda', 'vdb'])
        mock_copy_disk_path.return_value = disks_to_copy

        mock_guest.return_value = guest

        def fake_post():
            pass

        def fake_recover():
            pass

        drvr._live_migration(self.context, instance, "fakehost",
                             fake_post, fake_recover, True,
                             migrate_data)
        mock_copy_disk_path.assert_called_once_with(self.context, instance,
                                                    guest)

        class AnyEventletEvent(object):
            def __eq__(self, other):
                return type(other) == eventlet.event.Event

        mock_thread.assert_called_once_with(
            drvr._live_migration_operation,
            self.context, instance, "fakehost", True,
            migrate_data, guest, disks_to_copy[1])
        mock_monitor.assert_called_once_with(
            self.context, instance, guest, "fakehost",
            fake_post, fake_recover, True,
            migrate_data, AnyEventletEvent(), disks_to_copy[0])

    def _do_test_create_images_and_backing(self, disk_type):
        instance = objects.Instance(**self.test_instance)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(drvr, '_fetch_instance_kernel_ramdisk')
        self.mox.StubOutWithMock(libvirt_driver.libvirt_utils, 'create_image')

        disk_info = {'path': 'foo', 'type': disk_type,
                     'disk_size': 1 * 1024 ** 3,
                     'virt_disk_size': 20 * 1024 ** 3,
                     'backing_file': None}

        libvirt_driver.libvirt_utils.create_image(
            disk_info['type'], mox.IgnoreArg(), disk_info['virt_disk_size'])
        drvr._fetch_instance_kernel_ramdisk(self.context, instance,
                                            fallback_from_host=None)
        self.mox.ReplayAll()

        self.stub_out('os.path.exists', lambda *args: False)
        drvr._create_images_and_backing(self.context, instance,
                                        "/fake/instance/dir", [disk_info])

    def test_create_images_and_backing_qcow2(self):
        self._do_test_create_images_and_backing('qcow2')

    def test_create_images_and_backing_raw(self):
        self._do_test_create_images_and_backing('raw')

    def test_create_images_and_backing_images_not_exist_no_fallback(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.test_instance.update({'user_id': 'fake-user',
                                   'os_type': None,
                                   'project_id': 'fake-project'})
        instance = objects.Instance(**self.test_instance)

        backing_file = imagecache.get_cache_fname(instance.image_ref)
        disk_info = [
            {u'backing_file': backing_file,
             u'disk_size': 10747904,
             u'path': u'disk_path',
             u'type': u'qcow2',
             u'virt_disk_size': 25165824}]

        with mock.patch.object(libvirt_driver.libvirt_utils, 'fetch_image',
                               side_effect=exception.ImageNotFound(
                                   image_id="fake_id")):
            self.assertRaises(exception.ImageNotFound,
                              conn._create_images_and_backing,
                              self.context, instance,
                              "/fake/instance/dir", disk_info)

    def test_create_images_and_backing_images_not_exist_fallback(self):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        base_dir = os.path.join(CONF.instances_path,
                                CONF.image_cache_subdirectory_name)
        self.test_instance.update({'user_id': 'fake-user',
                                   'os_type': None,
                                   'kernel_id': uuids.kernel_id,
                                   'ramdisk_id': uuids.ramdisk_id,
                                   'project_id': 'fake-project'})
        instance = objects.Instance(**self.test_instance)

        backing_file = imagecache.get_cache_fname(instance.image_ref)
        disk_info = [
            {u'backing_file': backing_file,
             u'disk_size': 10747904,
             u'path': u'disk_path',
             u'type': u'qcow2',
             u'virt_disk_size': 25165824}]

        with test.nested(
            mock.patch.object(libvirt_driver.libvirt_utils, 'copy_image'),
            mock.patch.object(libvirt_driver.libvirt_utils, 'fetch_image',
                              side_effect=exception.ImageNotFound(
                                  image_id=uuids.fake_id)),
        ) as (copy_image_mock, fetch_image_mock):
            conn._create_images_and_backing(self.context, instance,
                                            "/fake/instance/dir", disk_info,
                                            fallback_from_host="fake_host")
            backfile_path = os.path.join(base_dir, backing_file)
            kernel_path = os.path.join(CONF.instances_path,
                                       self.test_instance['uuid'],
                                       'kernel')
            ramdisk_path = os.path.join(CONF.instances_path,
                                        self.test_instance['uuid'],
                                        'ramdisk')
            copy_image_mock.assert_has_calls([
                mock.call(dest=backfile_path, src=backfile_path,
                          host='fake_host', receive=True),
                mock.call(dest=kernel_path, src=kernel_path,
                          host='fake_host', receive=True),
                mock.call(dest=ramdisk_path, src=ramdisk_path,
                          host='fake_host', receive=True)
            ])
            fetch_image_mock.assert_has_calls([
                mock.call(context=self.context,
                          target=backfile_path,
                          image_id=self.test_instance['image_ref']),
                mock.call(self.context, kernel_path, instance.kernel_id),
                mock.call(self.context, ramdisk_path, instance.ramdisk_id)
            ])

    @mock.patch.object(libvirt_driver.libvirt_utils, 'fetch_image')
    def test_create_images_and_backing_images_exist(self, mock_fetch_image):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.test_instance.update({'user_id': 'fake-user',
                                   'os_type': None,
                                   'kernel_id': 'fake_kernel_id',
                                   'ramdisk_id': 'fake_ramdisk_id',
                                   'project_id': 'fake-project'})
        instance = objects.Instance(**self.test_instance)

        disk_info = [
            {u'backing_file': imagecache.get_cache_fname(instance.image_ref),
             u'disk_size': 10747904,
             u'path': u'disk_path',
             u'type': u'qcow2',
             u'virt_disk_size': 25165824}]

        with test.nested(
                mock.patch.object(imagebackend.Image, 'get_disk_size'),
                mock.patch.object(os.path, 'exists', return_value=True)
        ):
            conn._create_images_and_backing(self.context, instance,
                                            '/fake/instance/dir', disk_info)
        self.assertFalse(mock_fetch_image.called)

    def test_create_images_and_backing_ephemeral_gets_created(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        base_dir = os.path.join(CONF.instances_path,
                                CONF.image_cache_subdirectory_name)
        instance = objects.Instance(**self.test_instance)
        disk_info_byname = fake_disk_info_byname(instance)
        disk_info = disk_info_byname.values()

        # Give the ephemeral disk a non-default name
        disk_info_byname['disk.local']['backing_file'] = 'ephemeral_foo'

        with test.nested(
            mock.patch.object(libvirt_driver.libvirt_utils, 'fetch_image'),
            mock.patch.object(drvr, '_create_ephemeral'),
            mock.patch.object(imagebackend.Image, 'verify_base_size'),
            mock.patch.object(imagebackend.Image, 'get_disk_size')
        ) as (fetch_image_mock, create_ephemeral_mock, verify_base_size_mock,
              disk_size_mock):
            drvr._create_images_and_backing(self.context, instance,
                                            CONF.instances_path, disk_info)
            self.assertEqual(len(create_ephemeral_mock.call_args_list), 1)

            root_backing, ephemeral_backing = [
                os.path.join(base_dir, name)
                for name in (disk_info_byname['disk']['backing_file'],
                             'ephemeral_foo')
            ]

            m_args, m_kwargs = create_ephemeral_mock.call_args_list[0]
            self.assertEqual(ephemeral_backing, m_kwargs['target'])
            self.assertEqual(len(fetch_image_mock.call_args_list), 1)

            m_args, m_kwargs = fetch_image_mock.call_args_list[0]
            self.assertEqual(root_backing, m_kwargs['target'])

            verify_base_size_mock.assert_has_calls([
                mock.call(root_backing, instance.flavor.root_gb * units.Gi),
                mock.call(ephemeral_backing,
                          instance.flavor.ephemeral_gb * units.Gi)
            ])

    def test_create_images_and_backing_disk_info_none(self):
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        fake_backend = self.useFixture(fake_imagebackend.ImageBackendFixture())

        drvr._create_images_and_backing(self.context, instance,
                                        "/fake/instance/dir", None)

        # Assert that we did nothing
        self.assertEqual({}, fake_backend.created_disks)

    def _generate_target_ret(self, target_connect_addr=None):
        target_ret = {
        'graphics_listen_addrs': {'spice': '127.0.0.1', 'vnc': '127.0.0.1'},
        'target_connect_addr': target_connect_addr,
        'serial_listen_addr': '127.0.0.1',
        'volume': {
         '12345': {'connection_info': {u'data': {'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X'},
                   'serial': '12345'},
                   'disk_info': {'bus': 'scsi',
                                 'dev': 'sda',
                                 'type': 'disk'}},
         '67890': {'connection_info': {u'data': {'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'},
                   'serial': '67890'},
                   'disk_info': {'bus': 'scsi',
                                 'dev': 'sdb',
                                 'type': 'disk'}}}}
        return target_ret

    def test_pre_live_migration_works_correctly_mocked(self):
        self._test_pre_live_migration_works_correctly_mocked()

    def test_pre_live_migration_with_transport_ip(self):
        self.flags(live_migration_inbound_addr='127.0.0.2',
                   group='libvirt')
        target_ret = self._generate_target_ret('127.0.0.2')
        self._test_pre_live_migration_works_correctly_mocked(target_ret)

    def _test_pre_live_migration_works_correctly_mocked(self,
                                                        target_ret=None):
        # Creating testdata
        vol = {'block_device_mapping': [
           {'connection_info': {'serial': '12345', u'data':
            {'device_path':
             u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X'}},
             'mount_device': '/dev/sda'},
           {'connection_info': {'serial': '67890', u'data':
            {'device_path':
             u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'}},
             'mount_device': '/dev/sdb'}]}

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        class FakeNetworkInfo(object):
            def fixed_ips(self):
                return ["test_ip_addr"]

        def fake_none(*args, **kwargs):
            return

        self.stubs.Set(drvr, '_create_images_and_backing', fake_none)

        instance = objects.Instance(**self.test_instance)
        c = context.get_admin_context()
        nw_info = FakeNetworkInfo()

        # Creating mocks
        self.mox.StubOutWithMock(driver, "block_device_info_get_mapping")
        driver.block_device_info_get_mapping(vol
            ).AndReturn(vol['block_device_mapping'])
        self.mox.StubOutWithMock(drvr, "_connect_volume")
        for v in vol['block_device_mapping']:
            disk_info = {
                'bus': "scsi",
                'dev': v['mount_device'].rpartition("/")[2],
                'type': "disk"
                }
            drvr._connect_volume(v['connection_info'],
                                 disk_info)
        self.mox.StubOutWithMock(drvr, 'plug_vifs')
        drvr.plug_vifs(mox.IsA(instance), nw_info)

        self.mox.ReplayAll()
        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            block_migration=False,
            instance_relative_path='foo',
            is_shared_block_storage=False,
            is_shared_instance_path=False,
        )
        result = drvr.pre_live_migration(
            c, instance, vol, nw_info, None,
            migrate_data=migrate_data)
        if not target_ret:
            target_ret = self._generate_target_ret()
        self.assertEqual(
            result.to_legacy_dict(
                pre_migration_result=True)['pre_live_migration_result'],
            target_ret)

    @mock.patch.object(os, 'mkdir')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path_at_destination')
    @mock.patch('nova.virt.libvirt.driver.remotefs.'
                'RemoteFilesystem.copy_file')
    @mock.patch('nova.virt.driver.block_device_info_get_mapping')
    @mock.patch('nova.virt.configdrive.required_by', return_value=True)
    def test_pre_live_migration_block_with_config_drive_success(
            self, mock_required_by, block_device_info_get_mapping,
            mock_copy_file, mock_get_instance_path, mock_mkdir):
        self.flags(config_drive_format='iso9660')
        vol = {'block_device_mapping': [
                  {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                  {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}
        fake_instance_path = os.path.join(cfg.CONF.instances_path,
                                          '/fake_instance_uuid')
        mock_get_instance_path.return_value = fake_instance_path

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        instance = objects.Instance(**self.test_instance)
        migrate_data = objects.LibvirtLiveMigrateData()
        migrate_data.is_shared_instance_path = False
        migrate_data.is_shared_block_storage = False
        migrate_data.block_migration = True
        migrate_data.instance_relative_path = 'foo'
        src = "%s:%s/disk.config" % (instance.host, fake_instance_path)

        result = drvr.pre_live_migration(
            self.context, instance, vol, [], None, migrate_data)

        block_device_info_get_mapping.assert_called_once_with(
            {'block_device_mapping': [
                {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}
            ]}
        )
        mock_copy_file.assert_called_once_with(src, fake_instance_path)

        migrate_data.graphics_listen_addrs_vnc = '127.0.0.1'
        migrate_data.graphics_listen_addrs_spice = '127.0.0.1'
        migrate_data.serial_listen_addr = '127.0.0.1'
        self.assertEqual(migrate_data, result)

    @mock.patch('nova.virt.driver.block_device_info_get_mapping',
                return_value=())
    def test_pre_live_migration_block_with_config_drive_mocked_with_vfat(
            self, block_device_info_get_mapping):
        self.flags(config_drive_format='vfat')
        # Creating testdata
        vol = {'block_device_mapping': [
            {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
            {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        instance = objects.Instance(**self.test_instance)
        instance.config_drive = 'True'

        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_instance_path=False,
            is_shared_block_storage=False,
            block_migration=False,
            instance_relative_path='foo',
        )
        res_data = drvr.pre_live_migration(
            self.context, instance, vol, [], None, migrate_data)
        res_data = res_data.to_legacy_dict(pre_migration_result=True)
        block_device_info_get_mapping.assert_called_once_with(
            {'block_device_mapping': [
                {'connection_info': 'dummy', 'mount_device': '/dev/sda'},
                {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}
            ]}
        )
        self.assertEqual({'graphics_listen_addrs': {'spice': '127.0.0.1',
                                                    'vnc': '127.0.0.1'},
                          'target_connect_addr': None,
                          'serial_listen_addr': '127.0.0.1',
                          'volume': {}}, res_data['pre_live_migration_result'])

    def test_pre_live_migration_vol_backed_works_correctly_mocked(self):
        # Creating testdata, using temp dir.
        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)
            vol = {'block_device_mapping': [
             {'connection_info': {'serial': '12345', u'data':
             {'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X'}},
             'mount_device': '/dev/sda'},
             {'connection_info': {'serial': '67890', u'data':
             {'device_path':
             u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'}},
             'mount_device': '/dev/sdb'}]}

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            def fake_none(*args, **kwargs):
                return

            self.stubs.Set(drvr, '_create_images_and_backing', fake_none)

            class FakeNetworkInfo(object):
                def fixed_ips(self):
                    return ["test_ip_addr"]
            inst_ref = objects.Instance(**self.test_instance)
            c = context.get_admin_context()
            nw_info = FakeNetworkInfo()
            # Creating mocks
            self.mox.StubOutWithMock(drvr, "_connect_volume")
            for v in vol['block_device_mapping']:
                disk_info = {
                    'bus': "scsi",
                    'dev': v['mount_device'].rpartition("/")[2],
                    'type': "disk"
                    }
                drvr._connect_volume(v['connection_info'],
                                     disk_info)
            self.mox.StubOutWithMock(drvr, 'plug_vifs')
            drvr.plug_vifs(mox.IsA(inst_ref), nw_info)
            self.mox.ReplayAll()
            migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
                is_shared_instance_path=False,
                is_shared_block_storage=False,
                is_volume_backed=True,
                block_migration=False,
                instance_relative_path=inst_ref['name'],
                disk_over_commit=False,
                disk_available_mb=123,
                image_type='qcow2',
                filename='foo',
            )
            ret = drvr.pre_live_migration(c, inst_ref, vol, nw_info, None,
                                          migrate_data)
            target_ret = {
            'graphics_listen_addrs': {'spice': '127.0.0.1',
                                      'vnc': '127.0.0.1'},
            'target_connect_addr': None,
            'serial_listen_addr': '127.0.0.1',
            'volume': {
            '12345': {'connection_info': {u'data': {'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.abc.12345.opst-lun-X'},
                      'serial': '12345'},
                      'disk_info': {'bus': 'scsi',
                                    'dev': 'sda',
                                    'type': 'disk'}},
            '67890': {'connection_info': {u'data': {'device_path':
              u'/dev/disk/by-path/ip-1.2.3.4:3260-iqn.cde.67890.opst-lun-Z'},
                      'serial': '67890'},
                      'disk_info': {'bus': 'scsi',
                                    'dev': 'sdb',
                                    'type': 'disk'}}}}
            self.assertEqual(
                ret.to_legacy_dict(True)['pre_live_migration_result'],
                target_ret)
            self.assertTrue(os.path.exists('%s/%s/' % (tmpdir,
                                                       inst_ref['name'])))

    def test_pre_live_migration_plug_vifs_retry_fails(self):
        self.flags(live_migration_retry_count=3)
        instance = objects.Instance(**self.test_instance)

        def fake_plug_vifs(instance, network_info):
            raise processutils.ProcessExecutionError()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(eventlet.greenthread, 'sleep',
                       lambda x: eventlet.sleep(0))
        disk_info_json = jsonutils.dumps({})
        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_block_storage=True,
            is_shared_instance_path=True,
            block_migration=False,
        )
        self.assertRaises(processutils.ProcessExecutionError,
                          drvr.pre_live_migration,
                          self.context, instance, block_device_info=None,
                          network_info=[], disk_info=disk_info_json,
                          migrate_data=migrate_data)

    def test_pre_live_migration_plug_vifs_retry_works(self):
        self.flags(live_migration_retry_count=3)
        called = {'count': 0}
        instance = objects.Instance(**self.test_instance)

        def fake_plug_vifs(instance, network_info):
            called['count'] += 1
            if called['count'] < CONF.live_migration_retry_count:
                raise processutils.ProcessExecutionError()
            else:
                return

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(eventlet.greenthread, 'sleep',
                       lambda x: eventlet.sleep(0))
        disk_info_json = jsonutils.dumps({})
        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_block_storage=True,
            is_shared_instance_path=True,
            block_migration=False,
        )
        drvr.pre_live_migration(self.context, instance, block_device_info=None,
                                network_info=[], disk_info=disk_info_json,
                                migrate_data=migrate_data)

    def test_pre_live_migration_image_not_created_with_shared_storage(self):
        migrate_data_set = [{'is_shared_block_storage': False,
                             'is_shared_instance_path': True,
                             'is_volume_backed': False,
                             'filename': 'foo',
                             'instance_relative_path': 'bar',
                             'disk_over_commit': False,
                             'disk_available_mb': 123,
                             'image_type': 'qcow2',
                             'block_migration': False},
                            {'is_shared_block_storage': True,
                             'is_shared_instance_path': True,
                             'is_volume_backed': False,
                             'filename': 'foo',
                             'instance_relative_path': 'bar',
                             'disk_over_commit': False,
                             'disk_available_mb': 123,
                             'image_type': 'qcow2',
                             'block_migration': False},
                            {'is_shared_block_storage': False,
                             'is_shared_instance_path': True,
                             'is_volume_backed': False,
                             'filename': 'foo',
                             'instance_relative_path': 'bar',
                             'disk_over_commit': False,
                             'disk_available_mb': 123,
                             'image_type': 'qcow2',
                             'block_migration': True}]

        def _to_obj(d):
            return migrate_data_obj.LibvirtLiveMigrateData(**d)
        migrate_data_set = map(_to_obj, migrate_data_set)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        # creating mocks
        with test.nested(
            mock.patch.object(drvr,
                              '_create_images_and_backing'),
            mock.patch.object(drvr,
                              'ensure_filtering_rules_for_instance'),
            mock.patch.object(drvr, 'plug_vifs'),
        ) as (
            create_image_mock,
            rules_mock,
            plug_mock,
        ):
            disk_info_json = jsonutils.dumps({})
            for migrate_data in migrate_data_set:
                res = drvr.pre_live_migration(self.context, instance,
                                              block_device_info=None,
                                              network_info=[],
                                              disk_info=disk_info_json,
                                              migrate_data=migrate_data)
                self.assertFalse(create_image_mock.called)
                self.assertIsInstance(res,
                                      objects.LibvirtLiveMigrateData)

    def test_pre_live_migration_with_not_shared_instance_path(self):
        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_block_storage=False,
            is_shared_instance_path=False,
            block_migration=False,
            instance_relative_path='foo',
        )

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)

        def check_instance_dir(context, instance,
                               instance_dir, disk_info,
                               fallback_from_host=False):
            self.assertTrue(instance_dir)
        # creating mocks
        with test.nested(
            mock.patch.object(drvr,
                              '_create_images_and_backing',
                              side_effect=check_instance_dir),
            mock.patch.object(drvr,
                              'ensure_filtering_rules_for_instance'),
            mock.patch.object(drvr, 'plug_vifs'),
        ) as (
            create_image_mock,
            rules_mock,
            plug_mock,
        ):
            disk_info_json = jsonutils.dumps({})
            res = drvr.pre_live_migration(self.context, instance,
                                          block_device_info=None,
                                          network_info=[],
                                          disk_info=disk_info_json,
                                          migrate_data=migrate_data)
            create_image_mock.assert_has_calls(
                [mock.call(self.context, instance, mock.ANY, {},
                           fallback_from_host=instance.host)])
            self.assertIsInstance(res, objects.LibvirtLiveMigrateData)

    def test_pre_live_migration_recreate_disk_info(self):

        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_block_storage=False,
            is_shared_instance_path=False,
            block_migration=True,
            instance_relative_path='/some/path/',
        )
        disk_info = [{'disk_size': 5368709120, 'type': 'raw',
                      'virt_disk_size': 5368709120,
                      'path': '/some/path/disk',
                      'backing_file': '', 'over_committed_disk_size': 0},
                     {'disk_size': 1073741824, 'type': 'raw',
                      'virt_disk_size': 1073741824,
                      'path': '/some/path/disk.eph0',
                      'backing_file': '', 'over_committed_disk_size': 0}]
        image_disk_info = {'/some/path/disk': 'raw',
                           '/some/path/disk.eph0': 'raw'}

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        instance_path = os.path.dirname(disk_info[0]['path'])
        disk_info_path = os.path.join(instance_path, 'disk.info')

        with test.nested(
            mock.patch.object(os, 'mkdir'),
            mock.patch.object(fake_libvirt_utils, 'write_to_file'),
            mock.patch.object(drvr, '_create_images_and_backing')
        ) as (
            mkdir, write_to_file, create_images_and_backing
        ):
            drvr.pre_live_migration(self.context, instance,
                                    block_device_info=None,
                                    network_info=[],
                                    disk_info=jsonutils.dumps(disk_info),
                                    migrate_data=migrate_data)
            write_to_file.assert_called_with(disk_info_path,
                                             jsonutils.dumps(image_disk_info))

    def test_pre_live_migration_with_perf_events(self):

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._supported_perf_events = ['cmt']

        migrate_data = migrate_data_obj.LibvirtLiveMigrateData(
            is_shared_block_storage=False,
            is_shared_instance_path=False,
            block_migration=False,
            instance_relative_path='foo',
        )

        instance = objects.Instance(**self.test_instance)

        res = drvr.pre_live_migration(self.context, instance,
                                      block_device_info=None,
                                      network_info=[],
                                      disk_info=None,
                                      migrate_data=migrate_data)
        self.assertEqual(['cmt'], res.supported_perf_events)

    def test_get_instance_disk_info_works_correctly(self):
        # Test data
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance.name:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        fake_libvirt_utils.disk_sizes['/test/disk'] = 10 * units.Gi
        fake_libvirt_utils.disk_sizes['/test/disk.local'] = 20 * units.Gi
        fake_libvirt_utils.disk_backing_files['/test/disk.local'] = 'file'

        self.mox.StubOutWithMock(os.path, "getsize")
        os.path.getsize('/test/disk').AndReturn((10737418240))
        os.path.getsize('/test/disk.local').AndReturn((3328599655))

        ret = ("image: /test/disk\n"
               "file format: raw\n"
               "virtual size: 20G (21474836480 bytes)\n"
               "disk size: 3.1G\n"
               "cluster_size: 2097152\n"
               "backing file: /test/dummy (actual path: /backing/file)\n")

        self.mox.StubOutWithMock(os.path, "exists")
        os.path.exists('/test/disk.local').AndReturn(True)

        self.mox.StubOutWithMock(utils, "execute")
        utils.execute('env', 'LC_ALL=C', 'LANG=C', 'qemu-img', 'info',
                      '/test/disk.local', prlimit = images.QEMU_IMG_LIMITS,
                      ).AndReturn((ret, ''))

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        info = drvr.get_instance_disk_info(instance)
        info = jsonutils.loads(info)
        self.assertEqual(info[0]['type'], 'raw')
        self.assertEqual(info[0]['path'], '/test/disk')
        self.assertEqual(info[0]['disk_size'], 10737418240)
        self.assertEqual(info[0]['backing_file'], "")
        self.assertEqual(info[0]['over_committed_disk_size'], 0)
        self.assertEqual(info[1]['type'], 'qcow2')
        self.assertEqual(info[1]['path'], '/test/disk.local')
        self.assertEqual(info[1]['virt_disk_size'], 21474836480)
        self.assertEqual(info[1]['backing_file'], "file")
        self.assertEqual(info[1]['over_committed_disk_size'], 18146236825)

    def test_post_live_migration(self):
        vol = {'block_device_mapping': [
                  {'connection_info': {
                       'data': {'multipath_id': 'dummy1'},
                       'serial': 'fake_serial1'},
                    'mount_device': '/dev/sda',
                   },
                  {'connection_info': {
                       'data': {},
                       'serial': 'fake_serial2'},
                    'mount_device': '/dev/sdb', }]}

        def fake_initialize_connection(context, volume_id, connector):
            return {'data': {}}

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        fake_connector = {'host': 'fake'}
        inst_ref = {'id': 'foo'}
        cntx = context.get_admin_context()

        # Set up the mock expectations
        with test.nested(
            mock.patch.object(driver, 'block_device_info_get_mapping',
                              return_value=vol['block_device_mapping']),
            mock.patch.object(drvr, "get_volume_connector",
                              return_value=fake_connector),
            mock.patch.object(drvr._volume_api, "initialize_connection",
                              side_effect=fake_initialize_connection),
            mock.patch.object(drvr, '_disconnect_volume')
        ) as (block_device_info_get_mapping, get_volume_connector,
              initialize_connection, _disconnect_volume):
            drvr.post_live_migration(cntx, inst_ref, vol)

            block_device_info_get_mapping.assert_has_calls([
                mock.call(vol)])
            get_volume_connector.assert_has_calls([
                mock.call(inst_ref)])
            _disconnect_volume.assert_has_calls([
                mock.call({'data': {'multipath_id': 'dummy1'}}, 'sda'),
                mock.call({'data': {}}, 'sdb')])

    def test_get_instance_disk_info_excludes_volumes(self):
        # Test data
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/fake/path/to/volume1'/>"
                    "<target dev='vdc' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/fake/path/to/volume2'/>"
                    "<target dev='vdd' bus='virtio'/></disk>"
                    "</devices></domain>")

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance.name:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        fake_libvirt_utils.disk_sizes['/test/disk'] = 10 * units.Gi
        fake_libvirt_utils.disk_sizes['/test/disk.local'] = 20 * units.Gi
        fake_libvirt_utils.disk_backing_files['/test/disk.local'] = 'file'

        self.mox.StubOutWithMock(os.path, "getsize")
        os.path.getsize('/test/disk').AndReturn((10737418240))
        os.path.getsize('/test/disk.local').AndReturn((3328599655))

        ret = ("image: /test/disk\n"
               "file format: raw\n"
               "virtual size: 20G (21474836480 bytes)\n"
               "disk size: 3.1G\n"
               "cluster_size: 2097152\n"
               "backing file: /test/dummy (actual path: /backing/file)\n")

        self.mox.StubOutWithMock(os.path, "exists")
        os.path.exists('/test/disk.local').AndReturn(True)

        self.mox.StubOutWithMock(utils, "execute")
        utils.execute('env', 'LC_ALL=C', 'LANG=C', 'qemu-img', 'info',
                      '/test/disk.local', prlimit = images.QEMU_IMG_LIMITS,
                      ).AndReturn((ret, ''))

        self.mox.ReplayAll()
        conn_info = {'driver_volume_type': 'fake'}
        info = {'block_device_mapping': [
                  {'connection_info': conn_info, 'mount_device': '/dev/vdc'},
                  {'connection_info': conn_info, 'mount_device': '/dev/vdd'}]}
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        info = drvr.get_instance_disk_info(instance,
                                           block_device_info=info)
        info = jsonutils.loads(info)
        self.assertEqual(info[0]['type'], 'raw')
        self.assertEqual(info[0]['path'], '/test/disk')
        self.assertEqual(info[0]['disk_size'], 10737418240)
        self.assertEqual(info[0]['backing_file'], "")
        self.assertEqual(info[0]['over_committed_disk_size'], 0)
        self.assertEqual(info[1]['type'], 'qcow2')
        self.assertEqual(info[1]['path'], '/test/disk.local')
        self.assertEqual(info[1]['virt_disk_size'], 21474836480)
        self.assertEqual(info[1]['backing_file'], "file")
        self.assertEqual(info[1]['over_committed_disk_size'], 18146236825)

    def test_get_instance_disk_info_no_bdinfo_passed(self):
        # NOTE(ndipanov): _get_disk_overcomitted_size_total calls this method
        # without access to Nova's block device information. We want to make
        # sure that we guess volumes mostly correctly in that case as well
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='block'><driver name='qemu' type='raw'/>"
                    "<source file='/fake/path/to/volume1'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")

        # Preparing mocks
        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance.name:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        fake_libvirt_utils.disk_sizes['/test/disk'] = 10 * units.Gi

        self.mox.StubOutWithMock(os.path, "getsize")
        os.path.getsize('/test/disk').AndReturn((10737418240))

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        info = drvr.get_instance_disk_info(instance)

        info = jsonutils.loads(info)
        self.assertEqual(1, len(info))
        self.assertEqual(info[0]['type'], 'raw')
        self.assertEqual(info[0]['path'], '/test/disk')
        self.assertEqual(info[0]['disk_size'], 10737418240)
        self.assertEqual(info[0]['backing_file'], "")
        self.assertEqual(info[0]['over_committed_disk_size'], 0)

    def test_spawn_with_network_info(self):
        # Preparing mocks
        def fake_none(*args, **kwargs):
            return

        def fake_getLibVersion():
            return fakelibvirt.FAKE_LIBVIRT_VERSION

        def fake_getCapabilities():
            return """
            <capabilities>
                <host>
                    <uuid>cef19ce0-0ca2-11df-855d-b19fbce37686</uuid>
                    <cpu>
                      <arch>x86_64</arch>
                      <model>Penryn</model>
                      <vendor>Intel</vendor>
                      <topology sockets='1' cores='2' threads='1'/>
                      <feature name='xtpr'/>
                    </cpu>
                </host>
            </capabilities>
            """

        def fake_baselineCPU(cpu, flag):
            return """<cpu mode='custom' match='exact'>
                        <model fallback='allow'>Penryn</model>
                        <vendor>Intel</vendor>
                        <feature policy='require' name='xtpr'/>
                      </cpu>
                   """

        # _fake_network_info must be called before create_fake_libvirt_mock(),
        # as _fake_network_info calls importutils.import_class() and
        # create_fake_libvirt_mock() mocks importutils.import_class().
        network_info = _fake_network_info(self, 1)
        self.create_fake_libvirt_mock(getLibVersion=fake_getLibVersion,
                                      getCapabilities=fake_getCapabilities,
                                      getVersion=lambda: 1005001,
                                      baselineCPU=fake_baselineCPU)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 123456  # we send an int to test sha1 call
        instance = objects.Instance(**instance_ref)
        instance.config_drive = ''
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver,
                                 '_build_device_metadata')
        libvirt_driver.LibvirtDriver._build_device_metadata(self.context,
                                                           instance)
        # Mock out the get_info method of the LibvirtDriver so that the polling
        # in the spawn method of the LibvirtDriver returns immediately
        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, 'get_info')
        libvirt_driver.LibvirtDriver.get_info(instance
            ).AndReturn(hardware.InstanceInfo(state=power_state.RUNNING))

        # Start test
        self.mox.ReplayAll()

        with mock.patch('nova.virt.libvirt.driver.libvirt') as old_virt:
            del old_virt.VIR_CONNECT_BASELINE_CPU_EXPAND_FEATURES

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            self.stubs.Set(drvr.firewall_driver,
                        'setup_basic_filtering',
                        fake_none)
            self.stubs.Set(drvr.firewall_driver,
                        'prepare_instance_filter',
                        fake_none)
            self.stubs.Set(imagebackend.Image,
                        'cache',
                        fake_none)

            drvr.spawn(self.context, instance, image_meta, [], 'herp',
                        network_info=network_info)

        path = os.path.join(CONF.instances_path, instance['name'])
        if os.path.isdir(path):
            shutil.rmtree(path)

        path = os.path.join(CONF.instances_path,
                            CONF.image_cache_subdirectory_name)
        if os.path.isdir(path):
            shutil.rmtree(os.path.join(CONF.instances_path,
                                       CONF.image_cache_subdirectory_name))

    # Methods called directly by spawn()
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_get_guest_xml')
    @mock.patch.object(libvirt_driver.LibvirtDriver,
                       '_create_domain_and_network')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'get_info')
    # Methods called by _create_configdrive via post_xml_callback
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_build_device_metadata')
    @mock.patch.object(instance_metadata, 'InstanceMetadata')
    def test_spawn_with_config_drive(self, mock_instance_metadata,
                                     mock_build_device_metadata,
                                     mock_get_info,
                                     mock_create_domain_and_network,
                                     mock_get_guest_xml):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        instance.config_drive = 'True'
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        instance_info = hardware.InstanceInfo(state=power_state.RUNNING)

        mock_build_device_metadata.return_value = None

        def fake_create_domain_and_network(
                context, xml, instance, network_info, disk_info,
                block_device_info=None, power_on=True, reboot=False,
                vifs_already_plugged=False, post_xml_callback=None):
            # The config disk should be created by this callback, so we need
            # to execute it.
            post_xml_callback()

        fake_backend = self.useFixture(
            fake_imagebackend.ImageBackendFixture(exists=lambda _: False))

        mock_get_info.return_value = instance_info
        mock_create_domain_and_network.side_effect = \
            fake_create_domain_and_network

        drvr.spawn(self.context, instance,
                   image_meta, [], None)

        # We should have imported 'disk.config'
        config_disk = fake_backend.disks['disk.config']
        config_disk.import_file.assert_called_once_with(instance, mock.ANY,
                                                        'disk.config')

    def test_spawn_without_image_meta(self):
        def fake_none(*args, **kwargs):
            return

        def fake_get_info(instance):
            return hardware.InstanceInfo(state=power_state.RUNNING)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = objects.Instance(**instance_ref)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, '_get_guest_xml', fake_none)
        self.stubs.Set(drvr, '_create_domain_and_network', fake_none)
        self.stubs.Set(drvr, 'get_info', fake_get_info)

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        fake_backend = self.useFixture(fake_imagebackend.ImageBackendFixture())

        drvr.spawn(self.context, instance, image_meta, [], None)

        # We should have created a root disk and an ephemeral disk
        self.assertEqual(['disk', 'disk.local'],
                         sorted(fake_backend.created_disks.keys()))

    def test_spawn_from_volume_calls_cache(self):
        self.cache_called_for_disk = False

        def fake_none(*args, **kwargs):
            return

        def fake_cache(*args, **kwargs):
            if kwargs.get('image_id') == 'my_fake_image':
                self.cache_called_for_disk = True

        def fake_get_info(instance):
            return hardware.InstanceInfo(state=power_state.RUNNING)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, '_get_guest_xml', fake_none)

        self.stubs.Set(imagebackend.Image, 'cache', fake_cache)
        self.stubs.Set(drvr, '_create_domain_and_network', fake_none)
        self.stubs.Set(drvr, 'get_info', fake_get_info)

        block_device_info = {'root_device_name': '/dev/vda',
                             'block_device_mapping': [
                                {'mount_device': 'vda',
                                 'boot_index': 0}
                                ]
                            }
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        # Volume-backed instance created without image
        instance_ref = self.test_instance
        instance_ref['image_ref'] = ''
        instance_ref['root_device_name'] = '/dev/vda'
        instance_ref['uuid'] = uuidutils.generate_uuid()
        instance = objects.Instance(**instance_ref)

        drvr.spawn(self.context, instance,
                   image_meta, [], None,
                   block_device_info=block_device_info)
        self.assertFalse(self.cache_called_for_disk)

        # Booted from volume but with placeholder image
        instance_ref = self.test_instance
        instance_ref['image_ref'] = 'my_fake_image'
        instance_ref['root_device_name'] = '/dev/vda'
        instance_ref['uuid'] = uuidutils.generate_uuid()
        instance = objects.Instance(**instance_ref)

        drvr.spawn(self.context, instance,
                   image_meta, [], None,
                   block_device_info=block_device_info)
        self.assertFalse(self.cache_called_for_disk)

        # Booted from an image
        instance_ref['image_ref'] = 'my_fake_image'
        instance_ref['uuid'] = uuidutils.generate_uuid()
        instance = objects.Instance(**instance_ref)
        drvr.spawn(self.context, instance,
                   image_meta, [], None)
        self.assertTrue(self.cache_called_for_disk)

    def test_start_lxc_from_volume(self):
        self.flags(virt_type="lxc",
                   group='libvirt')

        def check_setup_container(image, container_dir=None):
            self.assertIsInstance(image, imgmodel.LocalBlockImage)
            self.assertEqual(image.path, '/dev/path/to/dev')
            return '/dev/nbd1'

        bdm = {
                  'guest_format': None,
                  'boot_index': 0,
                  'mount_device': '/dev/sda',
                  'connection_info': {
                      'driver_volume_type': 'iscsi',
                      'serial': 'afc1',
                      'data': {
                          'access_mode': 'rw',
                          'target_discovered': False,
                          'encrypted': False,
                          'qos_specs': None,
                          'target_iqn': 'iqn: volume-afc1',
                          'target_portal': 'ip: 3260',
                          'volume_id': 'afc1',
                          'target_lun': 1,
                          'auth_password': 'uj',
                          'auth_username': '47',
                          'auth_method': 'CHAP'
                      }
                  },
                  'disk_bus': 'scsi',
                  'device_type': 'disk',
                  'delete_on_termination': False
              }

        def _connect_volume_side_effect(connection_info, disk_info):
            bdm['connection_info']['data']['device_path'] = '/dev/path/to/dev'

        def _get(key, opt=None):
            return bdm.get(key, opt)

        def getitem(key):
            return bdm[key]

        def setitem(key, val):
            bdm[key] = val

        bdm_mock = mock.MagicMock()
        bdm_mock.__getitem__.side_effect = getitem
        bdm_mock.__setitem__.side_effect = setitem
        bdm_mock.get = _get

        disk_mock = mock.MagicMock()
        disk_mock.source_path = '/dev/path/to/dev'

        block_device_info = {'block_device_mapping': [bdm_mock],
                             'root_device_name': '/dev/sda'}

        # Volume-backed instance created without image
        instance_ref = self.test_instance
        instance_ref['image_ref'] = ''
        instance_ref['root_device_name'] = '/dev/sda'
        instance_ref['ephemeral_gb'] = 0
        instance_ref['uuid'] = uuidutils.generate_uuid()
        inst_obj = objects.Instance(**instance_ref)
        image_meta = objects.ImageMeta.from_dict({})

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with test.nested(
            mock.patch.object(drvr, 'plug_vifs'),
            mock.patch.object(drvr.firewall_driver, 'setup_basic_filtering'),
            mock.patch.object(drvr.firewall_driver, 'prepare_instance_filter'),
            mock.patch.object(drvr.firewall_driver, 'apply_instance_filter'),
            mock.patch.object(drvr, '_create_domain'),
            mock.patch.object(drvr, '_connect_volume',
                              side_effect=_connect_volume_side_effect),
            mock.patch.object(drvr, '_get_volume_config',
                                     return_value=disk_mock),
            mock.patch.object(drvr, 'get_info',
                              return_value=hardware.InstanceInfo(
                              state=power_state.RUNNING)),
            mock.patch('nova.virt.disk.api.setup_container',
                       side_effect=check_setup_container),
            mock.patch('nova.virt.disk.api.teardown_container'),
            mock.patch.object(objects.Instance, 'save')):

            drvr.spawn(self.context, inst_obj, image_meta, [], None,
                       network_info=[],
                       block_device_info=block_device_info)
            self.assertEqual('/dev/nbd1',
                             inst_obj.system_metadata.get(
                             'rootfs_device_name'))

    def test_spawn_with_pci_devices(self):
        def fake_none(*args, **kwargs):
            return None

        def fake_get_info(instance):
            return hardware.InstanceInfo(state=power_state.RUNNING)

        class FakeLibvirtPciDevice(object):
            def dettach(self):
                return None

            def reset(self):
                return None

        def fake_node_device_lookup_by_name(address):
            pattern = ("pci_%(hex)s{4}_%(hex)s{2}_%(hex)s{2}_%(oct)s{1}"
                       % dict(hex='[\da-f]', oct='[0-8]'))
            pattern = re.compile(pattern)
            if pattern.match(address) is None:
                raise fakelibvirt.libvirtError()
            return FakeLibvirtPciDevice()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, '_get_guest_xml', fake_none)
        self.stubs.Set(drvr, '_create_domain_and_network', fake_none)
        self.stubs.Set(drvr, 'get_info', fake_get_info)

        mock_connection = mock.MagicMock(
            nodeDeviceLookupByName=fake_node_device_lookup_by_name)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 'my_fake_image'
        instance = objects.Instance(**instance_ref)
        instance['pci_devices'] = objects.PciDeviceList(
            objects=[objects.PciDevice(address='0000:00:00.0')])

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        self.useFixture(fake_imagebackend.ImageBackendFixture())

        with mock.patch.object(drvr, '_get_connection',
                               return_value=mock_connection):
            drvr.spawn(self.context, instance, image_meta, [], None)

    def _test_create_image_plain(self, os_type='', filename='', mkfs=False):
        gotFiles = []

        def fake_none(*args, **kwargs):
            return

        def fake_get_info(instance):
            return hardware.InstanceInfo(state=power_state.RUNNING)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = objects.Instance(**instance_ref)
        instance['os_type'] = os_type

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, '_get_guest_xml', fake_none)
        self.stubs.Set(drvr, '_create_domain_and_network', fake_none)
        self.stubs.Set(drvr, 'get_info', fake_get_info)
        if mkfs:
            self.stubs.Set(nova.virt.disk.api, '_MKFS_COMMAND',
                       {os_type: 'mkfs.ext4 --label %(fs_label)s %(target)s'})

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)

        self.useFixture(
            fake_imagebackend.ImageBackendFixture(got_files=gotFiles))

        drvr._create_image(context, instance, disk_info['mapping'])
        drvr._get_guest_xml(self.context, instance, None,
                            disk_info, image_meta)

        wantFiles = [
            {'filename': '356a192b7913b04c54574d18c28d46e6395428ab',
             'size': 10 * units.Gi},
            {'filename': filename,
             'size': 20 * units.Gi},
            ]
        self.assertEqual(gotFiles, wantFiles)

    def test_create_image_plain_os_type_blank(self):
        self._test_create_image_plain(os_type='',
                                      filename=self._EPHEMERAL_20_DEFAULT,
                                      mkfs=False)

    def test_create_image_plain_os_type_none(self):
        self._test_create_image_plain(os_type=None,
                                      filename=self._EPHEMERAL_20_DEFAULT,
                                      mkfs=False)

    def test_create_image_plain_os_type_set_no_fs(self):
        self._test_create_image_plain(os_type='test',
                                      filename=self._EPHEMERAL_20_DEFAULT,
                                      mkfs=False)

    def test_create_image_plain_os_type_set_with_fs(self):
        ephemeral_file_name = ('ephemeral_20_%s' % utils.get_hash_str(
            'mkfs.ext4 --label %(fs_label)s %(target)s')[:7])

        self._test_create_image_plain(os_type='test',
                                      filename=ephemeral_file_name,
                                      mkfs=True)

    def test_create_image_initrd(self):
        kernel_id = uuids.kernel_id
        ramdisk_id = uuids.ramdisk_id

        kernel_fname = imagecache.get_cache_fname(kernel_id)
        ramdisk_fname = imagecache.get_cache_fname(ramdisk_id)

        filename = self._EPHEMERAL_20_DEFAULT

        gotFiles = []

        instance_ref = self.test_instance
        instance_ref['image_ref'] = uuids.instance_id
        instance_ref['kernel_id'] = uuids.kernel_id
        instance_ref['ramdisk_id'] = uuids.ramdisk_id
        instance_ref['os_type'] = 'test'
        instance = objects.Instance(**instance_ref)

        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        fake_backend = self.useFixture(
            fake_imagebackend.ImageBackendFixture(got_files=gotFiles))

        with test.nested(
            mock.patch.object(driver, '_get_guest_xml'),
            mock.patch.object(driver, '_create_domain_and_network'),
            mock.patch.object(driver, 'get_info',
              return_value=[hardware.InstanceInfo(state=power_state.RUNNING)])
        ):
            image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
            disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                                instance,
                                                image_meta)
            driver._create_image(context, instance, disk_info['mapping'])

        # Assert that kernel and ramdisk were fetched with fetch_raw_image
        # and no size
        for name, disk in six.iteritems(fake_backend.disks):
            cache = disk.cache
            if name in ('kernel', 'ramdisk'):
                cache.assert_called_once_with(
                    context=context, filename=mock.ANY, image_id=mock.ANY,
                    fetch_func=fake_libvirt_utils.fetch_raw_image)

        wantFiles = [
            {'filename': kernel_fname,
             'size': None},
            {'filename': ramdisk_fname,
             'size': None},
            {'filename': imagecache.get_cache_fname(uuids.instance_id),
             'size': 10 * units.Gi},
            {'filename': filename,
             'size': 20 * units.Gi},
            ]
        self.assertEqual(wantFiles, gotFiles)

    def _create_image_helper(self, callback, exists=None, suffix='',
                             test_create_configdrive=False):

        def fake_none(*args, **kwargs):
            return

        def fake_get_info(instance):
            return hardware.InstanceInfo(state=power_state.RUNNING)

        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        # NOTE(mikal): use this callback to tweak the instance to match
        # what you're trying to test
        callback(instance_ref)
        instance = objects.Instance(**instance_ref)
        # Turn on some swap to exercise that codepath in _create_image
        instance.flavor.swap = 500

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr, '_get_guest_xml', fake_none)
        self.stubs.Set(drvr, '_create_domain_and_network', fake_none)
        self.stubs.Set(drvr, 'get_info', fake_get_info)
        self.stubs.Set(instance_metadata, 'InstanceMetadata', fake_none)
        self.stubs.Set(nova.virt.configdrive.ConfigDriveBuilder,
                       'make_drive', fake_none)

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)

        gotFiles = []
        imported_files = []
        self.useFixture(fake_imagebackend.ImageBackendFixture(
            got_files=gotFiles, imported_files=imported_files, exists=exists))

        if test_create_configdrive:
            drvr._create_configdrive(context, instance)
        else:
            drvr._create_image(context, instance, disk_info['mapping'],
                               suffix=suffix)
        drvr._get_guest_xml(self.context, instance, None,
                            disk_info, image_meta)

        return gotFiles, imported_files

    def test_create_image_with_swap(self):
        def enable_swap(instance_ref):
            # Turn on some swap to exercise that codepath in _create_image
            instance_ref['system_metadata']['instance_type_swap'] = 500

        gotFiles, _ = self._create_image_helper(enable_swap)
        wantFiles = [
            {'filename': '356a192b7913b04c54574d18c28d46e6395428ab',
             'size': 10 * units.Gi},
            {'filename': self._EPHEMERAL_20_DEFAULT,
             'size': 20 * units.Gi},
            {'filename': 'swap_500',
             'size': 500 * units.Mi},
            ]
        self.assertEqual(gotFiles, wantFiles)

    @mock.patch(
        'nova.virt.libvirt.driver.LibvirtDriver._build_device_metadata',
        return_value=None)
    def test_create_configdrive(self, mock_save):
        def enable_configdrive(instance_ref):
            instance_ref['config_drive'] = 'true'

        # Ensure that we create a config drive and then import it into the
        # image backend store
        _, imported_files = self._create_image_helper(
            enable_configdrive, exists=lambda name: False,
            test_create_configdrive=True)
        self.assertTrue(imported_files[0][0].endswith('/disk.config'))
        self.assertEqual('disk.config', imported_files[0][1])

    @mock.patch.object(nova.virt.libvirt.imagebackend.Image, 'cache',
                       side_effect=exception.ImageNotFound(image_id='fake-id'))
    def test_create_image_not_exist_no_fallback(self, mock_cache):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)
        self.assertRaises(exception.ImageNotFound,
                          drvr._create_image,
                          self.context, instance, disk_info['mapping'])

    @mock.patch.object(nova.virt.libvirt.imagebackend.Image, 'cache')
    def test_create_image_not_exist_fallback(self, mock_cache):

        def side_effect(fetch_func, filename, size=None, *args, **kwargs):
            def second_call(fetch_func, filename, size=None, *args, **kwargs):
                # call copy_from_host ourselves because we mocked image.cache()
                fetch_func('fake-target')
                # further calls have no side effect
                mock_cache.side_effect = None
            mock_cache.side_effect = second_call
            # raise an error only the first call
            raise exception.ImageNotFound(image_id='fake-id')

        mock_cache.side_effect = side_effect
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)

        with mock.patch.object(libvirt_driver.libvirt_utils,
                               'copy_image') as mock_copy:
            drvr._create_image(self.context, instance, disk_info['mapping'],
                               fallback_from_host='fake-source-host')
            mock_copy.assert_called_once_with(src='fake-target',
                                              dest='fake-target',
                                              host='fake-source-host',
                                              receive=True)

    @mock.patch.object(nova.virt.libvirt.imagebackend.Image, 'cache')
    def test_create_image_resize_snap_backend(self, mock_cache):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        instance.task_state = task_states.RESIZE_FINISH
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)

        fake_backend = self.useFixture(fake_imagebackend.ImageBackendFixture())

        drvr._create_image(self.context, instance, disk_info['mapping'])

        # Assert we called create_snap on the root disk
        fake_backend.disks['disk'].create_snap.assert_called_once_with(
            libvirt_utils.RESIZE_SNAPSHOT_NAME)

    @mock.patch.object(utils, 'execute')
    def test_create_ephemeral_specified_fs(self, mock_exec):
        self.flags(default_ephemeral_format='ext3')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr._create_ephemeral('/dev/something', 20, 'myVol', 'linux',
                               is_block_dev=True, specified_fs='ext4')
        mock_exec.assert_called_once_with('mkfs', '-t', 'ext4', '-F', '-L',
                                          'myVol', '/dev/something',
                                          run_as_root=True)

    def test_create_ephemeral_specified_fs_not_valid(self):
        CONF.set_override('default_ephemeral_format', 'ext4')
        ephemerals = [{'device_type': 'disk',
                       'disk_bus': 'virtio',
                       'device_name': '/dev/vdb',
                       'guest_format': 'dummy',
                       'size': 1}]
        block_device_info = {
                'ephemerals': ephemerals}
        instance_ref = self.test_instance
        instance_ref['image_ref'] = 1
        instance = objects.Instance(**instance_ref)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        image_meta = objects.ImageMeta.from_dict({'disk_format': 'raw'})
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta)
        disk_info['mapping'].pop('disk.local')

        with test.nested(
            mock.patch.object(utils, 'execute'),
            mock.patch.object(drvr, 'get_info'),
            mock.patch.object(drvr, '_create_domain_and_network'),
            mock.patch.object(imagebackend.Image, 'verify_base_size'),
            mock.patch.object(imagebackend.Image, 'get_disk_size')):
            self.assertRaises(exception.InvalidBDMFormat, drvr._create_image,
                              context, instance, disk_info['mapping'],
                              block_device_info=block_device_info)

    def test_create_ephemeral_default(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkfs', '-t', 'ext4', '-F', '-L', 'myVol',
                      '/dev/something', run_as_root=True)
        self.mox.ReplayAll()
        drvr._create_ephemeral('/dev/something', 20, 'myVol', 'linux',
                               is_block_dev=True)

    def test_create_ephemeral_with_conf(self):
        CONF.set_override('default_ephemeral_format', 'ext4')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkfs', '-t', 'ext4', '-F', '-L', 'myVol',
                      '/dev/something', run_as_root=True)
        self.mox.ReplayAll()
        drvr._create_ephemeral('/dev/something', 20, 'myVol', 'linux',
                               is_block_dev=True)

    def test_create_ephemeral_with_arbitrary(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(nova.virt.disk.api, '_MKFS_COMMAND',
                       {'linux': 'mkfs.ext4 --label %(fs_label)s %(target)s'})
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkfs.ext4', '--label', 'myVol', '/dev/something',
                      run_as_root=True)
        self.mox.ReplayAll()
        drvr._create_ephemeral('/dev/something', 20, 'myVol', 'linux',
                               is_block_dev=True)

    def test_create_ephemeral_with_ext3(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(nova.virt.disk.api, '_MKFS_COMMAND',
                       {'linux': 'mkfs.ext3 --label %(fs_label)s %(target)s'})
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkfs.ext3', '--label', 'myVol', '/dev/something',
                      run_as_root=True)
        self.mox.ReplayAll()
        drvr._create_ephemeral('/dev/something', 20, 'myVol', 'linux',
                               is_block_dev=True)

    def test_create_swap_default(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.mox.StubOutWithMock(utils, 'execute')
        utils.execute('mkswap', '/dev/something', run_as_root=False)
        self.mox.ReplayAll()

        drvr._create_swap('/dev/something', 1)

    def test_get_console_output_file(self):
        fake_libvirt_utils.files['console.log'] = '01234567890'

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = objects.Instance(**instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            console_log = '%s/console.log' % (console_dir)
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='file'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % console_log

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            try:
                prev_max = libvirt_driver.MAX_CONSOLE_BYTES
                libvirt_driver.MAX_CONSOLE_BYTES = 5
                with mock.patch('os.path.exists', return_value=True):
                    output = drvr.get_console_output(self.context, instance)
            finally:
                libvirt_driver.MAX_CONSOLE_BYTES = prev_max

            self.assertEqual('67890', output)

    def test_get_console_output_file_missing(self):
        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = objects.Instance(**instance_ref)

            console_log = os.path.join(tmpdir, instance['name'],
                                       'non-existent.log')
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='file'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % console_log

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            with mock.patch('os.path.exists', return_value=False):
                output = drvr.get_console_output(self.context, instance)

            self.assertEqual('', output)

    def test_get_console_output_pty(self):
        fake_libvirt_utils.files['pty'] = '01234567890'

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)

            instance_ref = self.test_instance
            instance_ref['image_ref'] = 123456
            instance = objects.Instance(**instance_ref)

            console_dir = (os.path.join(tmpdir, instance['name']))
            pty_file = '%s/fake_pty' % (console_dir)
            fake_dom_xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                        </disk>
                        <console type='pty'>
                            <source path='%s'/>
                            <target port='0'/>
                        </console>
                    </devices>
                </domain>
            """ % pty_file

            def fake_lookup(id):
                return FakeVirtDomain(fake_dom_xml)

            def _fake_flush(self, fake_pty):
                return 'foo'

            def _fake_append_to_file(self, data, fpath):
                return 'pty'

            self.create_fake_libvirt_mock()
            libvirt_driver.LibvirtDriver._conn.lookupByName = fake_lookup
            libvirt_driver.LibvirtDriver._flush_libvirt_console = _fake_flush
            libvirt_driver.LibvirtDriver._append_to_file = _fake_append_to_file

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

            try:
                prev_max = libvirt_driver.MAX_CONSOLE_BYTES
                libvirt_driver.MAX_CONSOLE_BYTES = 5
                output = drvr.get_console_output(self.context, instance)
            finally:
                libvirt_driver.MAX_CONSOLE_BYTES = prev_max

            self.assertEqual('67890', output)

    @mock.patch('nova.virt.libvirt.host.Host.get_domain')
    @mock.patch.object(libvirt_guest.Guest, "get_xml_desc")
    def test_get_console_output_not_available(self, mock_get_xml, get_domain):
        xml = """
        <domain type='kvm'>
            <devices>
                <disk type='file'>
                    <source file='filename'/>
                </disk>
                <console type='foo'>
                    <source path='srcpath'/>
                    <target port='0'/>
                </console>
            </devices>
        </domain>
        """
        mock_get_xml.return_value = xml
        get_domain.return_value = mock.MagicMock()
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.ConsoleNotAvailable,
                          drvr.get_console_output, self.context, instance)

    def test_get_host_ip_addr(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        ip = drvr.get_host_ip_addr()
        self.assertEqual(ip, CONF.my_ip)

    @mock.patch.object(libvirt_driver.LOG, 'warning')
    @mock.patch('nova.compute.utils.get_machine_ips')
    def test_get_host_ip_addr_failure(self, mock_ips, mock_log):
        mock_ips.return_value = ['8.8.8.8', '75.75.75.75']
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.get_host_ip_addr()
        mock_log.assert_called_once_with(u'my_ip address (%(my_ip)s) was '
                                         u'not found on any of the '
                                         u'interfaces: %(ifaces)s',
                                         {'ifaces': '8.8.8.8, 75.75.75.75',
                                          'my_ip': mock.ANY})

    def test_conn_event_handler(self):
        self.mox.UnsetStubs()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        service_mock = mock.MagicMock()
        service_mock.disabled.return_value = False
        with test.nested(
            mock.patch.object(drvr._host, "_connect",
                              side_effect=fakelibvirt.make_libvirtError(
                                  fakelibvirt.libvirtError,
                                  "Failed to connect to host",
                                  error_code=
                                  fakelibvirt.VIR_ERR_INTERNAL_ERROR)),
            mock.patch.object(drvr._host, "_init_events",
                              return_value=None),
            mock.patch.object(objects.Service, "get_by_compute_host",
                              return_value=service_mock)):

            # verify that the driver registers for the close callback
            # and re-connects after receiving the callback
            self.assertRaises(exception.HypervisorUnavailable,
                              drvr.init_host,
                              "wibble")
            self.assertTrue(service_mock.disabled)

    def test_command_with_broken_connection(self):
        self.mox.UnsetStubs()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        service_mock = mock.MagicMock()
        service_mock.disabled.return_value = False
        with test.nested(
            mock.patch.object(drvr._host, "_connect",
                              side_effect=fakelibvirt.make_libvirtError(
                                  fakelibvirt.libvirtError,
                                  "Failed to connect to host",
                                  error_code=
                                  fakelibvirt.VIR_ERR_INTERNAL_ERROR)),
            mock.patch.object(drvr._host, "_init_events",
                              return_value=None),
            mock.patch.object(host.Host, "has_min_version",
                              return_value=True),
            mock.patch.object(drvr, "_do_quality_warnings",
                              return_value=None),
            mock.patch.object(objects.Service, "get_by_compute_host",
                              return_value=service_mock),
            mock.patch.object(host.Host, "get_capabilities")):

            drvr.init_host("wibble")
            self.assertRaises(exception.HypervisorUnavailable,
                              drvr.get_num_instances)
            self.assertTrue(service_mock.disabled)

    def test_service_resume_after_broken_connection(self):
        self.mox.UnsetStubs()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        service_mock = mock.MagicMock()
        service_mock.disabled.return_value = True
        with test.nested(
            mock.patch.object(drvr._host, "_connect",
                              return_value=mock.MagicMock()),
            mock.patch.object(drvr._host, "_init_events",
                              return_value=None),
            mock.patch.object(host.Host, "has_min_version",
                              return_value=True),
            mock.patch.object(drvr, "_do_quality_warnings",
                              return_value=None),
            mock.patch.object(objects.Service, "get_by_compute_host",
                              return_value=service_mock),
            mock.patch.object(host.Host, "get_capabilities")):

            drvr.init_host("wibble")
            drvr.get_num_instances()
            self.assertTrue(not service_mock.disabled and
                            service_mock.disabled_reason is None)

    @mock.patch.object(objects.Instance, 'save')
    def test_immediate_delete(self, mock_save):
        def fake_get_domain(instance):
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        def fake_delete_instance_files(instance):
            pass

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr._host, 'get_domain', fake_get_domain)
        self.stubs.Set(drvr, 'delete_instance_files',
                       fake_delete_instance_files)

        instance = objects.Instance(self.context, **self.test_instance)
        drvr.destroy(self.context, instance, {})
        mock_save.assert_called_once_with()

    @mock.patch.object(objects.Instance, 'get_by_uuid')
    @mock.patch.object(objects.Instance, 'obj_load_attr', autospec=True)
    @mock.patch.object(objects.Instance, 'save', autospec=True)
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_destroy')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'delete_instance_files')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_disconnect_volume')
    @mock.patch.object(driver, 'block_device_info_get_mapping')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_undefine_domain')
    def _test_destroy_removes_disk(self, mock_undefine_domain, mock_mapping,
                                   mock_disconnect_volume,
                                   mock_delete_instance_files, mock_destroy,
                                   mock_inst_save, mock_inst_obj_load_attr,
                                   mock_get_by_uuid, volume_fail=False):
        instance = objects.Instance(self.context, **self.test_instance)
        vol = {'block_device_mapping': [
              {'connection_info': 'dummy', 'mount_device': '/dev/sdb'}]}

        mock_mapping.return_value = vol['block_device_mapping']
        mock_delete_instance_files.return_value = True
        mock_get_by_uuid.return_value = instance
        if volume_fail:
            mock_disconnect_volume.return_value = (
                exception.VolumeNotFound('vol'))

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.destroy(self.context, instance, [], vol)

    def test_destroy_removes_disk(self):
        self._test_destroy_removes_disk(volume_fail=False)

    def test_destroy_removes_disk_volume_fails(self):
        self._test_destroy_removes_disk(volume_fail=True)

    @mock.patch.object(libvirt_driver.LibvirtDriver, 'unplug_vifs')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_destroy')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_undefine_domain')
    def test_destroy_not_removes_disk(self, mock_undefine_domain, mock_destroy,
                                      mock_unplug_vifs):
        instance = fake_instance.fake_instance_obj(
            None, name='instancename', id=1,
            uuid='875a8070-d0b9-4949-8b31-104d125c9a64')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.destroy(self.context, instance, [], None, False)

    @mock.patch.object(libvirt_driver.LibvirtDriver, 'cleanup')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_teardown_container')
    @mock.patch.object(host.Host, 'get_domain')
    def test_destroy_lxc_calls_teardown_container(self, mock_get_domain,
                                                  mock_teardown_container,
                                                  mock_cleanup):
        self.flags(virt_type='lxc', group='libvirt')
        fake_domain = FakeVirtDomain()

        def destroy_side_effect(*args, **kwargs):
            fake_domain._info[0] = power_state.SHUTDOWN

        with mock.patch.object(fake_domain, 'destroy',
               side_effect=destroy_side_effect) as mock_domain_destroy:
            mock_get_domain.return_value = fake_domain
            instance = objects.Instance(**self.test_instance)

            drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
            network_info = []
            drvr.destroy(self.context, instance, network_info, None, False)

            mock_get_domain.assert_has_calls([mock.call(instance),
                                              mock.call(instance)])
            mock_domain_destroy.assert_called_once_with()
            mock_teardown_container.assert_called_once_with(instance)
            mock_cleanup.assert_called_once_with(self.context, instance,
                                                 network_info, None, False,
                                                 None)

    @mock.patch.object(libvirt_driver.LibvirtDriver, 'cleanup')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_teardown_container')
    @mock.patch.object(host.Host, 'get_domain')
    def test_destroy_lxc_calls_teardown_container_when_no_domain(self,
            mock_get_domain, mock_teardown_container, mock_cleanup):
        self.flags(virt_type='lxc', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        inf_exception = exception.InstanceNotFound(instance_id=instance.uuid)
        mock_get_domain.side_effect = inf_exception

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        network_info = []
        drvr.destroy(self.context, instance, network_info, None, False)

        mock_get_domain.assert_has_calls([mock.call(instance),
                                          mock.call(instance)])
        mock_teardown_container.assert_called_once_with(instance)
        mock_cleanup.assert_called_once_with(self.context, instance,
                                             network_info, None, False,
                                             None)

    def test_reboot_different_ids(self):
        class FakeLoopingCall(object):
            def start(self, *a, **k):
                return self

            def wait(self):
                return None

        self.flags(wait_soft_reboot_seconds=1, group='libvirt')
        info_tuple = ('fake', 'fake', 'fake', 'also_fake')
        self.reboot_create_called = False

        # Mock domain
        mock_domain = self.mox.CreateMock(fakelibvirt.virDomain)
        mock_domain.info().AndReturn(
            (libvirt_guest.VIR_DOMAIN_RUNNING,) + info_tuple)
        mock_domain.ID().AndReturn('some_fake_id')
        mock_domain.ID().AndReturn('some_fake_id')
        mock_domain.shutdown()
        mock_domain.info().AndReturn(
            (libvirt_guest.VIR_DOMAIN_CRASHED,) + info_tuple)
        mock_domain.ID().AndReturn('some_other_fake_id')
        mock_domain.ID().AndReturn('some_other_fake_id')

        self.mox.ReplayAll()

        def fake_get_domain(instance):
            return mock_domain

        def fake_create_domain(**kwargs):
            self.reboot_create_called = True

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        self.stubs.Set(drvr._host, 'get_domain', fake_get_domain)
        self.stubs.Set(drvr, '_create_domain', fake_create_domain)
        self.stubs.Set(loopingcall, 'FixedIntervalLoopingCall',
                       lambda *a, **k: FakeLoopingCall())
        self.stubs.Set(pci_manager, 'get_instance_pci_devs', lambda *a: [])
        drvr.reboot(None, instance, [], 'SOFT')
        self.assertTrue(self.reboot_create_called)

    @mock.patch.object(pci_manager, 'get_instance_pci_devs')
    @mock.patch.object(loopingcall, 'FixedIntervalLoopingCall')
    @mock.patch.object(greenthread, 'sleep')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_hard_reboot')
    @mock.patch.object(host.Host, 'get_domain')
    def test_reboot_same_ids(self, mock_get_domain, mock_hard_reboot,
                             mock_sleep, mock_loopingcall,
                             mock_get_instance_pci_devs):
        class FakeLoopingCall(object):
            def start(self, *a, **k):
                return self

            def wait(self):
                return None

        self.flags(wait_soft_reboot_seconds=1, group='libvirt')
        info_tuple = ('fake', 'fake', 'fake', 'also_fake')
        self.reboot_hard_reboot_called = False

        # Mock domain
        mock_domain = mock.Mock(fakelibvirt.virDomain)
        return_values = [(libvirt_guest.VIR_DOMAIN_RUNNING,) + info_tuple,
                         (libvirt_guest.VIR_DOMAIN_CRASHED,) + info_tuple]
        mock_domain.info.side_effect = return_values
        mock_domain.ID.return_value = 'some_fake_id'
        mock_domain.shutdown.side_effect = mock.Mock()

        def fake_hard_reboot(*args, **kwargs):
            self.reboot_hard_reboot_called = True

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        mock_get_domain.return_value = mock_domain
        mock_hard_reboot.side_effect = fake_hard_reboot
        mock_loopingcall.return_value = FakeLoopingCall()
        mock_get_instance_pci_devs.return_value = []
        drvr.reboot(None, instance, [], 'SOFT')
        self.assertTrue(self.reboot_hard_reboot_called)

    @mock.patch.object(libvirt_driver.LibvirtDriver, '_hard_reboot')
    @mock.patch.object(host.Host, 'get_domain')
    def test_soft_reboot_libvirt_exception(self, mock_get_domain,
                                           mock_hard_reboot):
        # Tests that a hard reboot is performed when a soft reboot results
        # in raising a libvirtError.
        info_tuple = ('fake', 'fake', 'fake', 'also_fake')
        # setup mocks
        mock_virDomain = mock.Mock(fakelibvirt.virDomain)
        mock_virDomain.info.return_value = (
            (libvirt_guest.VIR_DOMAIN_RUNNING,) + info_tuple)
        mock_virDomain.ID.return_value = 'some_fake_id'
        mock_virDomain.shutdown.side_effect = fakelibvirt.libvirtError('Err')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        context = None
        instance = objects.Instance(**self.test_instance)
        network_info = []
        mock_get_domain.return_value = mock_virDomain

        drvr.reboot(context, instance, network_info, 'SOFT')

    @mock.patch.object(libvirt_driver.LibvirtDriver, '_hard_reboot')
    @mock.patch.object(host.Host, 'get_domain')
    def _test_resume_state_on_host_boot_with_state(self, state,
                                                   mock_get_domain,
                                                   mock_hard_reboot):
        mock_virDomain = mock.Mock(fakelibvirt.virDomain)
        mock_virDomain.info.return_value = ([state, None, None, None, None])

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_get_domain.return_value = mock_virDomain
        instance = objects.Instance(**self.test_instance)
        network_info = _fake_network_info(self, 1)

        drvr.resume_state_on_host_boot(self.context, instance, network_info,
                                       block_device_info=None)

        ignored_states = (power_state.RUNNING,
                          power_state.SUSPENDED,
                          power_state.NOSTATE,
                          power_state.PAUSED)
        self.assertEqual(mock_hard_reboot.called, state not in ignored_states)

    def test_resume_state_on_host_boot_with_running_state(self):
        self._test_resume_state_on_host_boot_with_state(power_state.RUNNING)

    def test_resume_state_on_host_boot_with_suspended_state(self):
        self._test_resume_state_on_host_boot_with_state(power_state.SUSPENDED)

    def test_resume_state_on_host_boot_with_paused_state(self):
        self._test_resume_state_on_host_boot_with_state(power_state.PAUSED)

    def test_resume_state_on_host_boot_with_nostate(self):
        self._test_resume_state_on_host_boot_with_state(power_state.NOSTATE)

    def test_resume_state_on_host_boot_with_shutdown_state(self):
        self._test_resume_state_on_host_boot_with_state(power_state.RUNNING)

    def test_resume_state_on_host_boot_with_crashed_state(self):
        self._test_resume_state_on_host_boot_with_state(power_state.CRASHED)

    @mock.patch.object(libvirt_driver.LibvirtDriver, '_hard_reboot')
    @mock.patch.object(host.Host, 'get_domain')
    def test_resume_state_on_host_boot_with_instance_not_found_on_driver(
            self, mock_get_domain, mock_hard_reboot):
        instance = objects.Instance(**self.test_instance)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_get_domain.side_effect = exception.InstanceNotFound(
            instance_id='fake')
        drvr.resume_state_on_host_boot(self.context, instance, network_info=[],
                                       block_device_info=None)

        mock_hard_reboot.assert_called_once_with(self.context,
                                                 instance, [], None)

    @mock.patch('nova.virt.libvirt.LibvirtDriver._undefine_domain')
    @mock.patch('nova.virt.libvirt.LibvirtDriver.get_info')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._create_domain_and_network')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._create_images_and_backing')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._get_guest_xml')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.blockinfo.get_disk_info')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._destroy')
    def test_hard_reboot(self, mock_destroy, mock_get_disk_info,
                         mock_get_instance_disk_info, mock_get_guest_xml,
                         mock_create_images_and_backing,
                         mock_create_domain_and_network, mock_get_info,
                         mock_undefine):
        self.context.auth_token = True  # any non-None value will suffice
        instance = objects.Instance(**self.test_instance)
        instance_path = libvirt_utils.get_instance_path(instance)
        network_info = _fake_network_info(self, 1)
        block_device_info = None

        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        return_values = [hardware.InstanceInfo(state=power_state.SHUTDOWN),
                         hardware.InstanceInfo(state=power_state.RUNNING)]
        mock_get_info.side_effect = return_values

        backing_disk_info = [{"virt_disk_size": 2}]

        mock_get_disk_info.return_value = mock.sentinel.disk_info
        mock_get_guest_xml.return_value = dummyxml
        mock_get_instance_disk_info.return_value = backing_disk_info

        drvr._hard_reboot(self.context, instance, network_info,
                          block_device_info)

        mock_destroy.assert_called_once_with(instance)
        mock_undefine.assert_called_once_with(instance)

        # make sure that _create_images_and_backing is passed the disk_info
        # returned from _get_instance_disk_info and not the one that is in
        # scope from blockinfo.get_disk_info
        mock_create_images_and_backing.assert_called_once_with(self.context,
            instance, instance_path, backing_disk_info)

        # make sure that _create_domain_and_network is passed the disk_info
        # returned from blockinfo.get_disk_info and not the one that's
        # returned from _get_instance_disk_info
        mock_create_domain_and_network.assert_called_once_with(self.context,
            dummyxml, instance, network_info, mock.sentinel.disk_info,
            block_device_info=block_device_info,
            reboot=True, vifs_already_plugged=True)

    @mock.patch('oslo_utils.fileutils.ensure_tree')
    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall')
    @mock.patch('nova.pci.manager.get_instance_pci_devs')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._prepare_pci_devices_for_use')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._create_domain_and_network')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._create_images_and_backing')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.utils.write_to_file')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._get_guest_config')
    @mock.patch('nova.virt.libvirt.blockinfo.get_disk_info')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._destroy')
    def test_hard_reboot_does_not_call_glance_show(self,
            mock_destroy, mock_get_disk_info, mock_get_guest_config,
            mock_get_instance_path, mock_write_to_file,
            mock_get_instance_disk_info, mock_create_images_and_backing,
            mock_create_domand_and_network, mock_prepare_pci_devices_for_use,
            mock_get_instance_pci_devs, mock_looping_call, mock_ensure_tree):
        """For a hard reboot, we shouldn't need an additional call to glance
        to get the image metadata.

        This is important for automatically spinning up instances on a
        host-reboot, since we won't have a user request context that'll allow
        the Glance request to go through. We have to rely on the cached image
        metadata, instead.

        https://bugs.launchpad.net/nova/+bug/1339386
        """
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        instance = objects.Instance(**self.test_instance)

        network_info = mock.MagicMock()
        block_device_info = mock.MagicMock()
        mock_get_disk_info.return_value = {}
        mock_get_guest_config.return_value = mock.MagicMock()
        mock_get_instance_path.return_value = '/foo'
        mock_looping_call.return_value = mock.MagicMock()
        drvr._image_api = mock.MagicMock()

        drvr._hard_reboot(self.context, instance, network_info,
                          block_device_info)

        self.assertFalse(drvr._image_api.get.called)
        mock_ensure_tree.assert_called_once_with('/foo')

    def test_suspend(self):
        guest = libvirt_guest.Guest(FakeVirtDomain(id=1))
        dom = guest._domain

        instance = objects.Instance(**self.test_instance)
        instance.ephemeral_key_uuid = None

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        @mock.patch.object(dmcrypt, 'delete_volume')
        @mock.patch.object(conn, '_get_instance_disk_info', return_value=[])
        @mock.patch.object(conn, '_detach_sriov_ports')
        @mock.patch.object(conn, '_detach_pci_devices')
        @mock.patch.object(pci_manager, 'get_instance_pci_devs',
                           return_value='pci devs')
        @mock.patch.object(conn._host, 'get_guest', return_value=guest)
        def suspend(mock_get_guest, mock_get_instance_pci_devs,
                    mock_detach_pci_devices, mock_detach_sriov_ports,
                    mock_get_instance_disk_info, mock_delete_volume):
            mock_managedSave = mock.Mock()
            dom.managedSave = mock_managedSave

            conn.suspend(self.context, instance)

            mock_managedSave.assert_called_once_with(0)
            self.assertFalse(mock_get_instance_disk_info.called)
            mock_delete_volume.assert_has_calls([mock.call(disk['path'])
                for disk in mock_get_instance_disk_info.return_value], False)

        suspend()

    @mock.patch.object(time, 'sleep')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_create_domain')
    @mock.patch.object(host.Host, 'get_domain')
    def _test_clean_shutdown(self, mock_get_domain, mock_create_domain,
                             mock_sleep, seconds_to_shutdown,
                             timeout, retry_interval,
                             shutdown_attempts, succeeds):
        info_tuple = ('fake', 'fake', 'fake', 'also_fake')
        shutdown_count = []

        # Mock domain
        mock_domain = mock.Mock(fakelibvirt.virDomain)
        return_infos = [(libvirt_guest.VIR_DOMAIN_RUNNING,) + info_tuple]
        return_shutdowns = [shutdown_count.append("shutdown")]
        retry_countdown = retry_interval
        for x in range(min(seconds_to_shutdown, timeout)):
            return_infos.append(
                (libvirt_guest.VIR_DOMAIN_RUNNING,) + info_tuple)
            if retry_countdown == 0:
                return_shutdowns.append(shutdown_count.append("shutdown"))
                retry_countdown = retry_interval
            else:
                retry_countdown -= 1

        if seconds_to_shutdown < timeout:
            return_infos.append(
                (libvirt_guest.VIR_DOMAIN_SHUTDOWN,) + info_tuple)

        mock_domain.info.side_effect = return_infos
        mock_domain.shutdown.side_effect = return_shutdowns

        def fake_create_domain(**kwargs):
            self.reboot_create_called = True

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        mock_get_domain.return_value = mock_domain
        mock_create_domain.side_effect = fake_create_domain
        result = drvr._clean_shutdown(instance, timeout, retry_interval)

        self.assertEqual(succeeds, result)
        self.assertEqual(shutdown_attempts, len(shutdown_count))

    def test_clean_shutdown_first_time(self):
        self._test_clean_shutdown(seconds_to_shutdown=2,
                                  timeout=5,
                                  retry_interval=3,
                                  shutdown_attempts=1,
                                  succeeds=True)

    def test_clean_shutdown_with_retry(self):
        self._test_clean_shutdown(seconds_to_shutdown=4,
                                  timeout=5,
                                  retry_interval=3,
                                  shutdown_attempts=2,
                                  succeeds=True)

    def test_clean_shutdown_failure(self):
        self._test_clean_shutdown(seconds_to_shutdown=6,
                                  timeout=5,
                                  retry_interval=3,
                                  shutdown_attempts=2,
                                  succeeds=False)

    def test_clean_shutdown_no_wait(self):
        self._test_clean_shutdown(seconds_to_shutdown=6,
                                  timeout=0,
                                  retry_interval=3,
                                  shutdown_attempts=1,
                                  succeeds=False)

    @mock.patch.object(FakeVirtDomain, 'attachDeviceFlags')
    @mock.patch.object(FakeVirtDomain, 'ID', return_value=1)
    @mock.patch.object(utils, 'get_image_from_system_metadata',
                       return_value=None)
    def test_attach_sriov_ports(self,
                                mock_get_image_metadata,
                                mock_ID,
                                mock_attachDevice):
        instance = objects.Instance(**self.test_instance)

        network_info = _fake_network_info(self, 1)
        network_info[0]['vnic_type'] = network_model.VNIC_TYPE_DIRECT
        guest = libvirt_guest.Guest(FakeVirtDomain())
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        drvr._attach_sriov_ports(self.context, instance, guest, network_info)
        mock_get_image_metadata.assert_called_once_with(
            instance.system_metadata)
        self.assertTrue(mock_attachDevice.called)

    @mock.patch.object(FakeVirtDomain, 'attachDeviceFlags')
    @mock.patch.object(FakeVirtDomain, 'ID', return_value=1)
    @mock.patch.object(utils, 'get_image_from_system_metadata',
                       return_value=None)
    def test_attach_sriov_ports_with_info_cache(self,
                                                mock_get_image_metadata,
                                                mock_ID,
                                                mock_attachDevice):
        instance = objects.Instance(**self.test_instance)

        network_info = _fake_network_info(self, 1)
        network_info[0]['vnic_type'] = network_model.VNIC_TYPE_DIRECT
        instance.info_cache = objects.InstanceInfoCache(
            network_info=network_info)
        guest = libvirt_guest.Guest(FakeVirtDomain())
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        drvr._attach_sriov_ports(self.context, instance, guest, None)
        mock_get_image_metadata.assert_called_once_with(
            instance.system_metadata)
        self.assertTrue(mock_attachDevice.called)

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def _test_detach_sriov_ports(self,
                                 mock_has_min_version, vif_type):
        instance = objects.Instance(**self.test_instance)

        expeted_pci_slot = "0000:00:00.0"
        network_info = _fake_network_info(self, 1)
        network_info[0]['vnic_type'] = network_model.VNIC_TYPE_DIRECT
        # some more adjustments for the fake network_info so that
        # the correct get_config function will be executed (vif's
        # get_config_hw_veb - which is according to the real SRIOV vif)
        # and most importantly the pci_slot which is translated to
        # cfg.source_dev, then to PciDevice.address and sent to
        # _detach_pci_devices
        network_info[0]['profile'] = dict(pci_slot=expeted_pci_slot)
        network_info[0]['type'] = vif_type
        network_info[0]['details'] = dict(vlan="2145")
        instance.info_cache = objects.InstanceInfoCache(
            network_info=network_info)
        # fill the pci_devices of the instance so that
        # pci_manager.get_instance_pci_devs will not return an empty list
        # which will eventually fail the assertion for detachDeviceFlags
        expected_pci_device_obj = (
            objects.PciDevice(address=expeted_pci_slot, request_id=None))
        instance.pci_devices = objects.PciDeviceList()
        instance.pci_devices.objects = [expected_pci_device_obj]

        domain = FakeVirtDomain()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        guest = libvirt_guest.Guest(domain)

        with mock.patch.object(drvr, '_detach_pci_devices') as mock_detach_pci:
            drvr._detach_sriov_ports(self.context, instance, guest)
            mock_detach_pci.assert_called_once_with(
                guest, [expected_pci_device_obj])

    def test_detach_sriov_ports_interface_interface_hostdev(self):
        # Note: test detach_sriov_ports method for vif with config
        # LibvirtConfigGuestInterface
        self._test_detach_sriov_ports(vif_type="hw_veb")

    def test_detach_sriov_ports_interface_pci_hostdev(self):
        # Note: test detach_sriov_ports method for vif with config
        # LibvirtConfigGuestHostdevPCI
        self._test_detach_sriov_ports(vif_type="ib_hostdev")

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(FakeVirtDomain, 'detachDeviceFlags')
    def test_detach_duplicate_mac_sriov_ports(self,
                                              mock_detachDeviceFlags,
                                              mock_has_min_version):
        instance = objects.Instance(**self.test_instance)

        network_info = _fake_network_info(self, 2)

        for network_info_inst in network_info:
            network_info_inst['vnic_type'] = network_model.VNIC_TYPE_DIRECT
            network_info_inst['type'] = "hw_veb"
            network_info_inst['details'] = dict(vlan="2145")
            network_info_inst['address'] = "fa:16:3e:96:2a:48"

        network_info[0]['profile'] = dict(pci_slot="0000:00:00.0")
        network_info[1]['profile'] = dict(pci_slot="0000:00:00.1")

        instance.info_cache = objects.InstanceInfoCache(
            network_info=network_info)
        # fill the pci_devices of the instance so that
        # pci_manager.get_instance_pci_devs will not return an empty list
        # which will eventually fail the assertion for detachDeviceFlags
        instance.pci_devices = objects.PciDeviceList()
        instance.pci_devices.objects = [
            objects.PciDevice(address='0000:00:00.0', request_id=None),
            objects.PciDevice(address='0000:00:00.1', request_id=None)
        ]

        domain = FakeVirtDomain()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        guest = libvirt_guest.Guest(domain)

        drvr._detach_sriov_ports(self.context, instance, guest)

        expected_xml = [
            ('<hostdev mode="subsystem" type="pci" managed="yes">\n'
             '  <source>\n'
             '    <address bus="0x00" domain="0x0000" \
                   function="0x0" slot="0x00"/>\n'
             '  </source>\n'
             '</hostdev>\n'),
            ('<hostdev mode="subsystem" type="pci" managed="yes">\n'
             '  <source>\n'
             '    <address bus="0x00" domain="0x0000" \
                   function="0x1" slot="0x00"/>\n'
             '  </source>\n'
             '</hostdev>\n')
        ]

        mock_detachDeviceFlags.has_calls([
            mock.call(expected_xml[0], flags=1),
            mock.call(expected_xml[1], flags=1)
        ])

    def test_resume(self):
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")
        instance = objects.Instance(**self.test_instance)
        network_info = _fake_network_info(self, 1)
        block_device_info = None
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        guest = libvirt_guest.Guest('fake_dom')
        with test.nested(
            mock.patch.object(drvr, '_get_existing_domain_xml',
                              return_value=dummyxml),
            mock.patch.object(drvr, '_create_domain_and_network',
                              return_value=guest),
            mock.patch.object(drvr, '_attach_pci_devices'),
            mock.patch.object(pci_manager, 'get_instance_pci_devs',
                              return_value='fake_pci_devs'),
            mock.patch.object(utils, 'get_image_from_system_metadata'),
            mock.patch.object(blockinfo, 'get_disk_info'),
        ) as (_get_existing_domain_xml, _create_domain_and_network,
              _attach_pci_devices, get_instance_pci_devs, get_image_metadata,
              get_disk_info):
            get_image_metadata.return_value = {'bar': 234}

            disk_info = {'foo': 123}
            get_disk_info.return_value = disk_info

            drvr.resume(self.context, instance, network_info,
                        block_device_info)
            _get_existing_domain_xml.assert_has_calls([mock.call(instance,
                                            network_info, block_device_info)])
            _create_domain_and_network.assert_has_calls([mock.call(
                                        self.context, dummyxml,
                                        instance, network_info, disk_info,
                                        block_device_info=block_device_info,
                                        vifs_already_plugged=True)])
            _attach_pci_devices.assert_has_calls([mock.call(guest,
                                                 'fake_pci_devs')])

    @mock.patch.object(host.Host, 'get_domain')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'get_info')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'delete_instance_files')
    @mock.patch.object(objects.Instance, 'save')
    def test_destroy_undefines(self, mock_save, mock_delete_instance_files,
                               mock_get_info, mock_get_domain):
        dom_mock = mock.MagicMock()
        dom_mock.undefineFlags.return_value = 1

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_get_domain.return_value = dom_mock
        mock_get_info.return_value = hardware.InstanceInfo(
            state=power_state.SHUTDOWN, id=-1)
        mock_delete_instance_files.return_value = None

        instance = objects.Instance(self.context, **self.test_instance)
        drvr.destroy(self.context, instance, [])
        mock_save.assert_called_once_with()

    @mock.patch.object(rbd_utils, 'RBDDriver')
    def test_cleanup_rbd(self, mock_driver):
        driver = mock_driver.return_value
        driver.cleanup_volumes = mock.Mock()
        fake_instance = {'uuid': '875a8070-d0b9-4949-8b31-104d125c9a64'}

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr._cleanup_rbd(fake_instance)

        driver.cleanup_volumes.assert_called_once_with(fake_instance)

    @mock.patch.object(objects.Instance, 'save')
    def test_destroy_undefines_no_undefine_flags(self, mock_save):
        mock = self.mox.CreateMock(fakelibvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(fakelibvirt.libvirtError('Err'))
        mock.ID().AndReturn(123)
        mock.undefine()

        self.mox.ReplayAll()

        def fake_get_domain(instance):
            return mock

        def fake_get_info(instance_name):
            return hardware.InstanceInfo(state=power_state.SHUTDOWN, id=-1)

        def fake_delete_instance_files(instance):
            return None

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr._host, 'get_domain', fake_get_domain)
        self.stubs.Set(drvr, 'get_info', fake_get_info)
        self.stubs.Set(drvr, 'delete_instance_files',
                       fake_delete_instance_files)
        instance = objects.Instance(self.context, **self.test_instance)
        drvr.destroy(self.context, instance, [])
        mock_save.assert_called_once_with()

    @mock.patch.object(objects.Instance, 'save')
    def test_destroy_undefines_no_attribute_with_managed_save(self, mock_save):
        mock = self.mox.CreateMock(fakelibvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(AttributeError())
        mock.hasManagedSaveImage(0).AndReturn(True)
        mock.managedSaveRemove(0)
        mock.undefine()

        self.mox.ReplayAll()

        def fake_get_domain(instance):
            return mock

        def fake_get_info(instance_name):
            return hardware.InstanceInfo(state=power_state.SHUTDOWN, id=-1)

        def fake_delete_instance_files(instance):
            return None

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr._host, 'get_domain', fake_get_domain)
        self.stubs.Set(drvr, 'get_info', fake_get_info)
        self.stubs.Set(drvr, 'delete_instance_files',
                       fake_delete_instance_files)
        instance = objects.Instance(self.context, **self.test_instance)
        drvr.destroy(self.context, instance, [])
        mock_save.assert_called_once_with()

    @mock.patch.object(objects.Instance, 'save')
    def test_destroy_undefines_no_attribute_no_managed_save(self, mock_save):
        mock = self.mox.CreateMock(fakelibvirt.virDomain)
        mock.ID()
        mock.destroy()
        mock.undefineFlags(1).AndRaise(AttributeError())
        mock.hasManagedSaveImage(0).AndRaise(AttributeError())
        mock.undefine()

        self.mox.ReplayAll()

        def fake_get_domain(self, instance):
            return mock

        def fake_get_info(instance_name):
            return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        def fake_delete_instance_files(instance):
            return None

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(host.Host, 'get_domain', fake_get_domain)
        self.stubs.Set(drvr, 'get_info', fake_get_info)
        self.stubs.Set(drvr, 'delete_instance_files',
                       fake_delete_instance_files)
        instance = objects.Instance(self.context, **self.test_instance)
        drvr.destroy(self.context, instance, [])
        mock_save.assert_called_once_with()

    def test_destroy_timed_out(self):
        mock = self.mox.CreateMock(fakelibvirt.virDomain)
        mock.ID()
        mock.destroy().AndRaise(fakelibvirt.libvirtError("timed out"))
        self.mox.ReplayAll()

        def fake_get_domain(self, instance):
            return mock

        def fake_get_error_code(self):
            return fakelibvirt.VIR_ERR_OPERATION_TIMEOUT

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(host.Host, 'get_domain', fake_get_domain)
        self.stubs.Set(fakelibvirt.libvirtError, 'get_error_code',
                fake_get_error_code)
        instance = objects.Instance(**self.test_instance)
        self.assertRaises(exception.InstancePowerOffFailure,
                drvr.destroy, self.context, instance, [])

    def test_private_destroy_not_found(self):
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                "No such domain",
                error_code=fakelibvirt.VIR_ERR_NO_DOMAIN)
        mock = self.mox.CreateMock(fakelibvirt.virDomain)
        mock.ID()
        mock.destroy().AndRaise(ex)
        mock.info().AndRaise(ex)
        mock.UUIDString()
        self.mox.ReplayAll()

        def fake_get_domain(instance):
            return mock

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr._host, 'get_domain', fake_get_domain)
        instance = objects.Instance(**self.test_instance)
        # NOTE(vish): verifies destroy doesn't raise if the instance disappears
        drvr._destroy(instance)

    def test_private_destroy_lxc_processes_refused_to_die(self):
        self.flags(virt_type='lxc', group='libvirt')
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError, "",
                error_message="internal error: Some processes refused to die",
                error_code=fakelibvirt.VIR_ERR_INTERNAL_ERROR)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with mock.patch.object(conn._host, 'get_domain') as mock_get_domain, \
             mock.patch.object(conn, 'get_info') as mock_get_info:
            mock_domain = mock.MagicMock()
            mock_domain.ID.return_value = 1
            mock_get_domain.return_value = mock_domain
            mock_domain.destroy.side_effect = ex

            mock_info = mock.MagicMock()
            mock_info.id = 1
            mock_info.state = power_state.SHUTDOWN
            mock_get_info.return_value = mock_info

            instance = objects.Instance(**self.test_instance)
            conn._destroy(instance)

    def test_private_destroy_processes_refused_to_die_still_raises(self):
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError, "",
                error_message="internal error: Some processes refused to die",
                error_code=fakelibvirt.VIR_ERR_INTERNAL_ERROR)

        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with mock.patch.object(conn._host, 'get_domain') as mock_get_domain:
            mock_domain = mock.MagicMock()
            mock_domain.ID.return_value = 1
            mock_get_domain.return_value = mock_domain
            mock_domain.destroy.side_effect = ex

            instance = objects.Instance(**self.test_instance)
            self.assertRaises(fakelibvirt.libvirtError, conn._destroy,
                              instance)

    def test_private_destroy_ebusy_timeout(self):
        # Tests that _destroy will retry 3 times to destroy the guest when an
        # EBUSY is raised, but eventually times out and raises the libvirtError
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                ("Failed to terminate process 26425 with SIGKILL: "
                 "Device or resource busy"),
                error_code=fakelibvirt.VIR_ERR_SYSTEM_ERROR,
                int1=errno.EBUSY)

        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        mock_guest.poweroff = mock.Mock(side_effect=ex)

        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with mock.patch.object(drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.assertRaises(fakelibvirt.libvirtError, drvr._destroy,
                              instance)

        self.assertEqual(3, mock_guest.poweroff.call_count)

    def test_private_destroy_ebusy_multiple_attempt_ok(self):
        # Tests that the _destroy attempt loop is broken when EBUSY is no
        # longer raised.
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                ("Failed to terminate process 26425 with SIGKILL: "
                 "Device or resource busy"),
                error_code=fakelibvirt.VIR_ERR_SYSTEM_ERROR,
                int1=errno.EBUSY)

        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        mock_guest.poweroff = mock.Mock(side_effect=[ex, None])

        inst_info = hardware.InstanceInfo(power_state.SHUTDOWN, id=1)
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with mock.patch.object(drvr._host, 'get_guest',
                               return_value=mock_guest):
            with mock.patch.object(drvr, 'get_info', return_value=inst_info):
                drvr._destroy(instance)

        self.assertEqual(2, mock_guest.poweroff.call_count)

    def test_undefine_domain_with_not_found_instance(self):
        def fake_get_domain(self, instance):
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        self.stubs.Set(host.Host, 'get_domain', fake_get_domain)
        self.mox.StubOutWithMock(fakelibvirt.libvirtError, "get_error_code")

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)

        # NOTE(wenjianhn): verifies undefine doesn't raise if the
        # instance disappears
        drvr._undefine_domain(instance)

    @mock.patch.object(host.Host, "list_instance_domains")
    @mock.patch.object(objects.BlockDeviceMappingList, "bdms_by_instance_uuid")
    @mock.patch.object(objects.InstanceList, "get_by_filters")
    def test_disk_over_committed_size_total(self, mock_get, mock_bdms,
                                            mock_list):
        # Ensure destroy calls managedSaveRemove for saved instance.
        class DiagFakeDomain(object):
            def __init__(self, name):
                self._name = name
                self._uuid = str(uuid.uuid4())

            def ID(self):
                return 1

            def name(self):
                return self._name

            def UUIDString(self):
                return self._uuid

            def XMLDesc(self, flags):
                return "<domain/>"

        instance_domains = [
            DiagFakeDomain("instance0000001"),
            DiagFakeDomain("instance0000002")]
        mock_list.return_value = instance_domains

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        fake_disks = {'instance0000001':
                      [{'type': 'qcow2', 'path': '/somepath/disk1',
                        'virt_disk_size': '10737418240',
                        'backing_file': '/somepath/disk1',
                        'disk_size': '83886080',
                        'over_committed_disk_size': '10653532160'}],
                      'instance0000002':
                      [{'type': 'raw', 'path': '/somepath/disk2',
                        'virt_disk_size': '0',
                        'backing_file': '/somepath/disk2',
                        'disk_size': '10737418240',
                        'over_committed_disk_size': '0'}]}

        def get_info(instance_name, xml, **kwargs):
            return fake_disks.get(instance_name)

        instance_uuids = [dom.UUIDString() for dom in instance_domains]
        instances = [objects.Instance(
            uuid=instance_uuids[0],
            root_device_name='/dev/vda'),
            objects.Instance(
            uuid=instance_uuids[1],
            root_device_name='/dev/vdb')
        ]
        mock_get.return_value = instances

        with mock.patch.object(drvr,
                               "_get_instance_disk_info") as mock_info:
            mock_info.side_effect = get_info

            result = drvr._get_disk_over_committed_size_total()
            self.assertEqual(result, 10653532160)
            mock_list.assert_called_once_with()
            self.assertEqual(2, mock_info.call_count)

        filters = {'uuid': instance_uuids}
        mock_get.assert_called_once_with(mock.ANY, filters, use_slave=True)
        mock_bdms.assert_called_with(mock.ANY, instance_uuids)

    @mock.patch.object(host.Host, "list_instance_domains")
    @mock.patch.object(objects.BlockDeviceMappingList, "bdms_by_instance_uuid")
    @mock.patch.object(objects.InstanceList, "get_by_filters")
    def test_disk_over_committed_size_total_eperm(self, mock_get, mock_bdms,
                                                  mock_list):
        # Ensure destroy calls managedSaveRemove for saved instance.
        class DiagFakeDomain(object):
            def __init__(self, name):
                self._name = name
                self._uuid = str(uuid.uuid4())

            def ID(self):
                return 1

            def name(self):
                return self._name

            def UUIDString(self):
                return self._uuid

            def XMLDesc(self, flags):
                return "<domain/>"

        instance_domains = [
            DiagFakeDomain("instance0000001"),
            DiagFakeDomain("instance0000002"),
            DiagFakeDomain("instance0000003"),
            DiagFakeDomain("instance0000004")]
        mock_list.return_value = instance_domains

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        fake_disks = {'instance0000001':
                      [{'type': 'qcow2', 'path': '/somepath/disk1',
                        'virt_disk_size': '10737418240',
                        'backing_file': '/somepath/disk1',
                        'disk_size': '83886080',
                        'over_committed_disk_size': '10653532160'}],
                      'instance0000002':
                      [{'type': 'raw', 'path': '/somepath/disk2',
                        'virt_disk_size': '0',
                        'backing_file': '/somepath/disk2',
                        'disk_size': '10737418240',
                        'over_committed_disk_size': '21474836480'}],
                      'instance0000003':
                      [{'type': 'raw', 'path': '/somepath/disk3',
                        'virt_disk_size': '0',
                        'backing_file': '/somepath/disk3',
                        'disk_size': '21474836480',
                        'over_committed_disk_size': '32212254720'}],
                      'instance0000004':
                      [{'type': 'raw', 'path': '/somepath/disk4',
                        'virt_disk_size': '0',
                        'backing_file': '/somepath/disk4',
                        'disk_size': '32212254720',
                        'over_committed_disk_size': '42949672960'}]}

        def side_effect(name, dom, block_device_info):
            if name == 'instance0000001':
                self.assertEqual('/dev/vda',
                                 block_device_info['root_device_name'])
                raise OSError(errno.ENOENT, 'No such file or directory')
            if name == 'instance0000002':
                self.assertEqual('/dev/vdb',
                                 block_device_info['root_device_name'])
                raise OSError(errno.ESTALE, 'Stale NFS file handle')
            if name == 'instance0000003':
                self.assertEqual('/dev/vdc',
                                 block_device_info['root_device_name'])
                raise OSError(errno.EACCES, 'Permission denied')
            if name == 'instance0000004':
                self.assertEqual('/dev/vdd',
                                 block_device_info['root_device_name'])
                return fake_disks.get(name)
        get_disk_info = mock.Mock()
        get_disk_info.side_effect = side_effect
        drvr._get_instance_disk_info = get_disk_info

        instance_uuids = [dom.UUIDString() for dom in instance_domains]
        instances = [objects.Instance(
            uuid=instance_uuids[0],
            root_device_name='/dev/vda'),
            objects.Instance(
            uuid=instance_uuids[1],
            root_device_name='/dev/vdb'),
            objects.Instance(
            uuid=instance_uuids[2],
            root_device_name='/dev/vdc'),
            objects.Instance(
            uuid=instance_uuids[3],
            root_device_name='/dev/vdd')
        ]
        mock_get.return_value = instances

        result = drvr._get_disk_over_committed_size_total()
        self.assertEqual(42949672960, result)
        mock_list.assert_called_once_with()
        self.assertEqual(4, get_disk_info.call_count)
        filters = {'uuid': instance_uuids}
        mock_get.assert_called_once_with(mock.ANY, filters, use_slave=True)
        mock_bdms.assert_called_with(mock.ANY, instance_uuids)

    @mock.patch.object(host.Host, "list_instance_domains",
                       return_value=[mock.MagicMock(name='foo')])
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_get_instance_disk_info",
                       side_effect=exception.VolumeBDMPathNotFound(path='bar'))
    @mock.patch.object(objects.BlockDeviceMappingList, "bdms_by_instance_uuid")
    @mock.patch.object(objects.InstanceList, "get_by_filters")
    def test_disk_over_committed_size_total_bdm_not_found(self,
                                                          mock_get,
                                                          mock_bdms,
                                                          mock_get_disk_info,
                                                          mock_list_domains):
        # Tests that we handle VolumeBDMPathNotFound gracefully.
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertEqual(0, drvr._get_disk_over_committed_size_total())

    def test_cpu_info(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        def get_host_capabilities_stub(self):
            cpu = vconfig.LibvirtConfigCPU()
            cpu.model = "Opteron_G4"
            cpu.vendor = "AMD"
            cpu.arch = arch.X86_64

            cpu.cells = 1
            cpu.cores = 2
            cpu.threads = 1
            cpu.sockets = 4

            cpu.add_feature(vconfig.LibvirtConfigCPUFeature("extapic"))
            cpu.add_feature(vconfig.LibvirtConfigCPUFeature("3dnow"))

            caps = vconfig.LibvirtConfigCaps()
            caps.host = vconfig.LibvirtConfigCapsHost()
            caps.host.cpu = cpu

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = vm_mode.HVM
            guest.arch = arch.X86_64
            guest.domtype = ["kvm"]
            caps.guests.append(guest)

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = vm_mode.HVM
            guest.arch = arch.I686
            guest.domtype = ["kvm"]
            caps.guests.append(guest)

            return caps

        self.stubs.Set(host.Host, "get_capabilities",
                       get_host_capabilities_stub)

        want = {"vendor": "AMD",
                "features": set(["extapic", "3dnow"]),
                "model": "Opteron_G4",
                "arch": arch.X86_64,
                "topology": {"cells": 1, "cores": 2, "threads": 1,
                             "sockets": 4}}
        got = drvr._get_cpu_info()
        self.assertEqual(want, got)

    def test_get_pcidev_info(self):

        def fake_nodeDeviceLookupByName(self, name):
            return FakeNodeDevice(_fake_NodeDevXml[name])

        self.mox.StubOutWithMock(host.Host, 'device_lookup_by_name')
        host.Host.device_lookup_by_name = fake_nodeDeviceLookupByName

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with mock.patch.object(
            fakelibvirt.Connection, 'getLibVersion') as mock_lib_version:
            mock_lib_version.return_value = (
                versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_PF_WITH_NO_VFS_CAP_VERSION) - 1)

            actualvf = drvr._get_pcidev_info("pci_0000_04_00_3")
            expect_vf = {
                "dev_id": "pci_0000_04_00_3",
                "address": "0000:04:00.3",
                "product_id": '1521',
                "numa_node": None,
                "vendor_id": '8086',
                "label": 'label_8086_1521',
                "dev_type": fields.PciDeviceType.SRIOV_PF,
                }

            self.assertEqual(expect_vf, actualvf)
            actualvf = drvr._get_pcidev_info("pci_0000_04_10_7")
            expect_vf = {
                "dev_id": "pci_0000_04_10_7",
                "address": "0000:04:10.7",
                "product_id": '1520',
                "numa_node": None,
                "vendor_id": '8086',
                "label": 'label_8086_1520',
                "dev_type": fields.PciDeviceType.SRIOV_VF,
                "parent_addr": '0000:04:00.3',
                }
            self.assertEqual(expect_vf, actualvf)

            actualvf = drvr._get_pcidev_info("pci_0000_04_11_7")
            expect_vf = {
                "dev_id": "pci_0000_04_11_7",
                "address": "0000:04:11.7",
                "product_id": '1520',
                "vendor_id": '8086',
                "numa_node": 0,
                "label": 'label_8086_1520',
                "dev_type": fields.PciDeviceType.SRIOV_VF,
                "parent_addr": '0000:04:00.3',
                }

            self.assertEqual(expect_vf, actualvf)

            with mock.patch.object(
                pci_utils, 'is_physical_function', return_value=True):
                actualvf = drvr._get_pcidev_info("pci_0000_04_00_1")
                expect_vf = {
                    "dev_id": "pci_0000_04_00_1",
                    "address": "0000:04:00.1",
                    "product_id": '1013',
                    "numa_node": 0,
                    "vendor_id": '15b3',
                    "label": 'label_15b3_1013',
                    "dev_type": fields.PciDeviceType.SRIOV_PF,
                    }
                self.assertEqual(expect_vf, actualvf)

            with mock.patch.object(
                pci_utils, 'is_physical_function', return_value=False):
                actualvf = drvr._get_pcidev_info("pci_0000_04_00_1")
                expect_vf = {
                    "dev_id": "pci_0000_04_00_1",
                    "address": "0000:04:00.1",
                    "product_id": '1013',
                    "numa_node": 0,
                    "vendor_id": '15b3',
                    "label": 'label_15b3_1013',
                    "dev_type": fields.PciDeviceType.STANDARD,
                    }
                self.assertEqual(expect_vf, actualvf)

        with mock.patch.object(
            fakelibvirt.Connection, 'getLibVersion') as mock_lib_version:
            mock_lib_version.return_value = (
                versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_PF_WITH_NO_VFS_CAP_VERSION))
            actualvf = drvr._get_pcidev_info("pci_0000_03_00_0")
            expect_vf = {
                "dev_id": "pci_0000_03_00_0",
                "address": "0000:03:00.0",
                "product_id": '1013',
                "numa_node": 0,
                "vendor_id": '15b3',
                "label": 'label_15b3_1013',
                "dev_type": fields.PciDeviceType.SRIOV_PF,
                }
            self.assertEqual(expect_vf, actualvf)

            actualvf = drvr._get_pcidev_info("pci_0000_03_00_1")
            expect_vf = {
                "dev_id": "pci_0000_03_00_1",
                "address": "0000:03:00.1",
                "product_id": '1013',
                "numa_node": 0,
                "vendor_id": '15b3',
                "label": 'label_15b3_1013',
                "dev_type": fields.PciDeviceType.SRIOV_PF,
                }

            self.assertEqual(expect_vf, actualvf)

    def test_list_devices_not_supported(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Handle just the NO_SUPPORT error
        not_supported_exc = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                'this function is not supported by the connection driver:'
                ' virNodeNumOfDevices',
                error_code=fakelibvirt.VIR_ERR_NO_SUPPORT)

        with mock.patch.object(drvr._conn, 'listDevices',
                               side_effect=not_supported_exc):
            self.assertEqual('[]', drvr._get_pci_passthrough_devices())

        # We cache not supported status to avoid emitting too many logging
        # messages. Clear this value to test the other exception case.
        del drvr._list_devices_supported

        # Other errors should not be caught
        other_exc = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError,
            'other exc',
            error_code=fakelibvirt.VIR_ERR_NO_DOMAIN)

        with mock.patch.object(drvr._conn, 'listDevices',
                               side_effect=other_exc):
            self.assertRaises(fakelibvirt.libvirtError,
                              drvr._get_pci_passthrough_devices)

    def test_get_pci_passthrough_devices(self):

        def fakelistDevices(caps, fakeargs=0):
            return ['pci_0000_04_00_3', 'pci_0000_04_10_7',
                    'pci_0000_04_11_7']

        self.mox.StubOutWithMock(libvirt_driver.LibvirtDriver, '_conn')

        libvirt_driver.LibvirtDriver._conn.listDevices = fakelistDevices

        def fake_nodeDeviceLookupByName(self, name):
            return FakeNodeDevice(_fake_NodeDevXml[name])

        self.mox.StubOutWithMock(host.Host, 'device_lookup_by_name')
        host.Host.device_lookup_by_name = fake_nodeDeviceLookupByName

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        actjson = drvr._get_pci_passthrough_devices()

        expectvfs = [
            {
                "dev_id": "pci_0000_04_00_3",
                "address": "0000:04:00.3",
                "product_id": '1521',
                "vendor_id": '8086',
                "dev_type": fields.PciDeviceType.SRIOV_PF,
                "phys_function": None,
                "numa_node": None},
            {
                "dev_id": "pci_0000_04_10_7",
                "domain": 0,
                "address": "0000:04:10.7",
                "product_id": '1520',
                "vendor_id": '8086',
                "numa_node": None,
                "dev_type": fields.PciDeviceType.SRIOV_VF,
                "phys_function": [('0x0000', '0x04', '0x00', '0x3')]},
            {
                "dev_id": "pci_0000_04_11_7",
                "domain": 0,
                "address": "0000:04:11.7",
                "product_id": '1520',
                "vendor_id": '8086',
                "numa_node": 0,
                "dev_type": fields.PciDeviceType.SRIOV_VF,
                "phys_function": [('0x0000', '0x04', '0x00', '0x3')],
            }
        ]

        actualvfs = jsonutils.loads(actjson)
        for dev in range(len(actualvfs)):
            for key in actualvfs[dev].keys():
                if key not in ['phys_function', 'virt_functions', 'label']:
                    self.assertEqual(expectvfs[dev][key], actualvfs[dev][key])

    def _fake_caps_numa_topology(self,
                                 cells_per_host=4,
                                 sockets_per_cell=1,
                                 cores_per_socket=1,
                                 threads_per_core=2,
                                 kb_mem=1048576):

        # Generate mempages list per cell
        cell_mempages = list()
        for cellid in range(cells_per_host):
            mempages_0 = vconfig.LibvirtConfigCapsNUMAPages()
            mempages_0.size = 4
            mempages_0.total = 1024 * cellid

            mempages_1 = vconfig.LibvirtConfigCapsNUMAPages()
            mempages_1.size = 2048
            mempages_1.total = 0 + cellid

            cell_mempages.append([mempages_0, mempages_1])

        topology = fakelibvirt.HostInfo._gen_numa_topology(cells_per_host,
                                         sockets_per_cell,
                                         cores_per_socket,
                                         threads_per_core,
                                         kb_mem=kb_mem,
                                         numa_mempages_list=cell_mempages)

        return topology

    def _test_get_host_numa_topology(self, mempages):
        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = arch.X86_64
        caps.host.topology = self._fake_caps_numa_topology()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        expected_topo_dict = {'cells': [
                                {'cpus': '0,1', 'cpu_usage': 0,
                                  'mem': {'total': 256, 'used': 0},
                                  'id': 0},
                                {'cpus': '3', 'cpu_usage': 0,
                                  'mem': {'total': 256, 'used': 0},
                                  'id': 1},
                                {'cpus': '', 'cpu_usage': 0,
                                  'mem': {'total': 256, 'used': 0},
                                  'id': 2},
                                {'cpus': '', 'cpu_usage': 0,
                                  'mem': {'total': 256, 'used': 0},
                                  'id': 3}]}
        with test.nested(
                mock.patch.object(host.Host, "get_capabilities",
                                  return_value=caps),
                mock.patch.object(
                    hardware, 'get_vcpu_pin_set',
                    return_value=set([0, 1, 3, 4, 5])),
                mock.patch.object(host.Host, 'get_online_cpus',
                                  return_value=set([0, 1, 2, 3, 6])),
                ):
            got_topo = drvr._get_host_numa_topology()
            got_topo_dict = got_topo._to_dict()
            self.assertThat(
                    expected_topo_dict, matchers.DictMatches(got_topo_dict))

            if mempages:
                # cells 0
                self.assertEqual(4, got_topo.cells[0].mempages[0].size_kb)
                self.assertEqual(0, got_topo.cells[0].mempages[0].total)
                self.assertEqual(2048, got_topo.cells[0].mempages[1].size_kb)
                self.assertEqual(0, got_topo.cells[0].mempages[1].total)
                # cells 1
                self.assertEqual(4, got_topo.cells[1].mempages[0].size_kb)
                self.assertEqual(1024, got_topo.cells[1].mempages[0].total)
                self.assertEqual(2048, got_topo.cells[1].mempages[1].size_kb)
                self.assertEqual(1, got_topo.cells[1].mempages[1].total)
            else:
                self.assertEqual([], got_topo.cells[0].mempages)
                self.assertEqual([], got_topo.cells[1].mempages)

            self.assertEqual(expected_topo_dict, got_topo_dict)
            self.assertEqual(set([]), got_topo.cells[0].pinned_cpus)
            self.assertEqual(set([]), got_topo.cells[1].pinned_cpus)
            self.assertEqual(set([]), got_topo.cells[2].pinned_cpus)
            self.assertEqual(set([]), got_topo.cells[3].pinned_cpus)
            self.assertEqual([set([0, 1])], got_topo.cells[0].siblings)
            self.assertEqual([], got_topo.cells[1].siblings)

    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    def test_get_host_numa_topology(self, mock_version):
        self._test_get_host_numa_topology(mempages=True)

    @mock.patch.object(fakelibvirt.Connection, 'getType')
    @mock.patch.object(fakelibvirt.Connection, 'getVersion')
    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion')
    def test_get_host_numa_topology_no_mempages(self, mock_lib_version,
                                                mock_version, mock_type):
        self.flags(virt_type='kvm', group='libvirt')
        mock_lib_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_HUGEPAGE_VERSION) - 1
        mock_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION)
        mock_type.return_value = host.HV_DRIVER_QEMU
        self._test_get_host_numa_topology(mempages=False)

    def test_get_host_numa_topology_empty(self):
        caps = vconfig.LibvirtConfigCaps()
        caps.host = vconfig.LibvirtConfigCapsHost()
        caps.host.cpu = vconfig.LibvirtConfigCPU()
        caps.host.cpu.arch = arch.X86_64
        caps.host.topology = None

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with test.nested(
            mock.patch.object(host.Host, 'has_min_version', return_value=True),
            mock.patch.object(host.Host, "get_capabilities",
                              return_value=caps)
        ) as (has_min_version, get_caps):
            self.assertIsNone(drvr._get_host_numa_topology())
        self.assertEqual(2, get_caps.call_count)

    @mock.patch.object(fakelibvirt.Connection, 'getType')
    @mock.patch.object(fakelibvirt.Connection, 'getVersion')
    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion')
    def test_get_host_numa_topology_old_version(self, mock_lib_version,
                                                mock_version, mock_type):
        self.flags(virt_type='kvm', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        mock_lib_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION) - 1
        mock_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION)
        mock_type.return_value = host.HV_DRIVER_QEMU
        self.assertIsNone(drvr._get_host_numa_topology())

    @mock.patch.object(fakelibvirt.Connection, 'getType')
    @mock.patch.object(fakelibvirt.Connection, 'getVersion')
    @mock.patch.object(fakelibvirt.Connection, 'getLibVersion')
    def test_get_host_numa_topology_xen(self, mock_lib_version,
                                        mock_version, mock_type):
        self.flags(virt_type='xen', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        mock_lib_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_LIBVIRT_NUMA_VERSION)
        mock_version.return_value = versionutils.convert_version_to_int(
                libvirt_driver.MIN_QEMU_NUMA_HUGEPAGE_VERSION)
        mock_type.return_value = host.HV_DRIVER_XEN
        self.assertIsNone(drvr._get_host_numa_topology())

    def test_diagnostic_vcpus_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                raise fakelibvirt.libvirtError('vcpus missing')

            def blockStats(self, path):
                return (169, 688640, 0, 0, -1)

            def interfaceStats(self, path):
                return (4408, 82, 0, 0, 0, 0, 0, 0)

            def memoryStats(self):
                return {'actual': 220160, 'rss': 200164}

            def maxMemory(self):
                return 280160

        def fake_get_domain(self, instance):
            return DiagFakeDomain()

        self.stubs.Set(host.Host, "get_domain", fake_get_domain)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'vda_read': 688640,
                  'vda_read_req': 169,
                  'vda_write': 0,
                  'vda_write_req': 0,
                  'vda_errors': -1,
                  'vdb_read': 688640,
                  'vdb_read_req': 169,
                  'vdb_write': 0,
                  'vdb_write_req': 0,
                  'vdb_errors': -1,
                  'memory': 280160,
                  'memory-actual': 220160,
                  'memory-rss': 200164,
                  'vnet0_rx': 4408,
                  'vnet0_rx_drop': 0,
                  'vnet0_rx_errors': 0,
                  'vnet0_rx_packets': 82,
                  'vnet0_tx': 0,
                  'vnet0_tx_drop': 0,
                  'vnet0_tx_errors': 0,
                  'vnet0_tx_packets': 0,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [],
                    'disk_details': [{'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0},
                                     {'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0}],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [{'mac_address': '52:54:00:a4:38:38',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0}],
                    'state': 'running',
                    'uptime': 10,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    def test_diagnostic_blockstats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000, 0),
                         (1, 1, 1640000000, 0),
                         (2, 1, 3040000000, 0),
                         (3, 1, 1420000000, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                raise fakelibvirt.libvirtError('blockStats missing')

            def interfaceStats(self, path):
                return (4408, 82, 0, 0, 0, 0, 0, 0)

            def memoryStats(self):
                return {'actual': 220160, 'rss': 200164}

            def maxMemory(self):
                return 280160

        def fake_get_domain(self, instance):
            return DiagFakeDomain()

        self.stubs.Set(host.Host, "get_domain", fake_get_domain)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'cpu0_time': 15340000000,
                  'cpu1_time': 1640000000,
                  'cpu2_time': 3040000000,
                  'cpu3_time': 1420000000,
                  'memory': 280160,
                  'memory-actual': 220160,
                  'memory-rss': 200164,
                  'vnet0_rx': 4408,
                  'vnet0_rx_drop': 0,
                  'vnet0_rx_errors': 0,
                  'vnet0_rx_packets': 82,
                  'vnet0_tx': 0,
                  'vnet0_tx_drop': 0,
                  'vnet0_tx_errors': 0,
                  'vnet0_tx_packets': 0,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [{'time': 15340000000},
                                    {'time': 1640000000},
                                    {'time': 3040000000},
                                    {'time': 1420000000}],
                    'disk_details': [],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [{'mac_address': '52:54:00:a4:38:38',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0}],
                    'state': 'running',
                    'uptime': 10,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    def test_diagnostic_interfacestats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000, 0),
                         (1, 1, 1640000000, 0),
                         (2, 1, 3040000000, 0),
                         (3, 1, 1420000000, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169, 688640, 0, 0, -1)

            def interfaceStats(self, path):
                raise fakelibvirt.libvirtError('interfaceStat missing')

            def memoryStats(self):
                return {'actual': 220160, 'rss': 200164}

            def maxMemory(self):
                return 280160

        def fake_get_domain(self, instance):
            return DiagFakeDomain()

        self.stubs.Set(host.Host, "get_domain", fake_get_domain)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'cpu0_time': 15340000000,
                  'cpu1_time': 1640000000,
                  'cpu2_time': 3040000000,
                  'cpu3_time': 1420000000,
                  'vda_read': 688640,
                  'vda_read_req': 169,
                  'vda_write': 0,
                  'vda_write_req': 0,
                  'vda_errors': -1,
                  'vdb_read': 688640,
                  'vdb_read_req': 169,
                  'vdb_write': 0,
                  'vdb_write_req': 0,
                  'vdb_errors': -1,
                  'memory': 280160,
                  'memory-actual': 220160,
                  'memory-rss': 200164,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [{'time': 15340000000},
                                    {'time': 1640000000},
                                    {'time': 3040000000},
                                    {'time': 1420000000}],
                    'disk_details': [{'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0},
                                     {'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0}],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [],
                    'state': 'running',
                    'uptime': 10,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    def test_diagnostic_memorystats_exception(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000, 0),
                         (1, 1, 1640000000, 0),
                         (2, 1, 3040000000, 0),
                         (3, 1, 1420000000, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169, 688640, 0, 0, -1)

            def interfaceStats(self, path):
                return (4408, 82, 0, 0, 0, 0, 0, 0)

            def memoryStats(self):
                raise fakelibvirt.libvirtError('memoryStats missing')

            def maxMemory(self):
                return 280160

        def fake_get_domain(self, instance):
            return DiagFakeDomain()

        self.stubs.Set(host.Host, "get_domain", fake_get_domain)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'cpu0_time': 15340000000,
                  'cpu1_time': 1640000000,
                  'cpu2_time': 3040000000,
                  'cpu3_time': 1420000000,
                  'vda_read': 688640,
                  'vda_read_req': 169,
                  'vda_write': 0,
                  'vda_write_req': 0,
                  'vda_errors': -1,
                  'vdb_read': 688640,
                  'vdb_read_req': 169,
                  'vdb_write': 0,
                  'vdb_write_req': 0,
                  'vdb_errors': -1,
                  'memory': 280160,
                  'vnet0_rx': 4408,
                  'vnet0_rx_drop': 0,
                  'vnet0_rx_errors': 0,
                  'vnet0_rx_packets': 82,
                  'vnet0_tx': 0,
                  'vnet0_tx_drop': 0,
                  'vnet0_tx_errors': 0,
                  'vnet0_tx_packets': 0,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [{'time': 15340000000},
                                    {'time': 1640000000},
                                    {'time': 3040000000},
                                    {'time': 1420000000}],
                    'disk_details': [{'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0},
                                     {'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0}],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [{'mac_address': '52:54:00:a4:38:38',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0}],
                    'state': 'running',
                    'uptime': 10,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    def test_diagnostic_full(self):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000, 0),
                         (1, 1, 1640000000, 0),
                         (2, 1, 3040000000, 0),
                         (3, 1, 1420000000, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169, 688640, 0, 0, -1)

            def interfaceStats(self, path):
                return (4408, 82, 0, 0, 0, 0, 0, 0)

            def memoryStats(self):
                return {'actual': 220160, 'rss': 200164}

            def maxMemory(self):
                return 280160

        def fake_get_domain(self, instance):
            return DiagFakeDomain()

        self.stubs.Set(host.Host, "get_domain", fake_get_domain)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'cpu0_time': 15340000000,
                  'cpu1_time': 1640000000,
                  'cpu2_time': 3040000000,
                  'cpu3_time': 1420000000,
                  'vda_read': 688640,
                  'vda_read_req': 169,
                  'vda_write': 0,
                  'vda_write_req': 0,
                  'vda_errors': -1,
                  'vdb_read': 688640,
                  'vdb_read_req': 169,
                  'vdb_write': 0,
                  'vdb_write_req': 0,
                  'vdb_errors': -1,
                  'memory': 280160,
                  'memory-actual': 220160,
                  'memory-rss': 200164,
                  'vnet0_rx': 4408,
                  'vnet0_rx_drop': 0,
                  'vnet0_rx_errors': 0,
                  'vnet0_rx_packets': 82,
                  'vnet0_tx': 0,
                  'vnet0_tx_drop': 0,
                  'vnet0_tx_errors': 0,
                  'vnet0_tx_packets': 0,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [{'time': 15340000000},
                                    {'time': 1640000000},
                                    {'time': 3040000000},
                                    {'time': 1420000000}],
                    'disk_details': [{'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0},
                                     {'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0}],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [{'mac_address': '52:54:00:a4:38:38',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0}],
                    'state': 'running',
                    'uptime': 10,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    @mock.patch.object(host.Host, 'get_domain')
    def test_diagnostic_full_with_multiple_interfaces(self, mock_get_domain):
        xml = """
                <domain type='kvm'>
                    <devices>
                        <disk type='file'>
                            <source file='filename'/>
                            <target dev='vda' bus='virtio'/>
                        </disk>
                        <disk type='block'>
                            <source dev='/path/to/dev/1'/>
                            <target dev='vdb' bus='virtio'/>
                        </disk>
                        <interface type='network'>
                            <mac address='52:54:00:a4:38:38'/>
                            <source network='default'/>
                            <target dev='vnet0'/>
                        </interface>
                        <interface type="bridge">
                            <mac address="53:55:00:a5:39:39"/>
                            <model type="virtio"/>
                            <target dev="br0"/>
                        </interface>
                    </devices>
                </domain>
            """

        class DiagFakeDomain(FakeVirtDomain):

            def __init__(self):
                super(DiagFakeDomain, self).__init__(fake_xml=xml)

            def vcpus(self):
                return ([(0, 1, 15340000000, 0),
                         (1, 1, 1640000000, 0),
                         (2, 1, 3040000000, 0),
                         (3, 1, 1420000000, 0)],
                        [(True, False),
                         (True, False),
                         (True, False),
                         (True, False)])

            def blockStats(self, path):
                return (169, 688640, 0, 0, -1)

            def interfaceStats(self, path):
                return (4408, 82, 0, 0, 0, 0, 0, 0)

            def memoryStats(self):
                return {'actual': 220160, 'rss': 200164}

            def maxMemory(self):
                return 280160

        def fake_get_domain(self):
            return DiagFakeDomain()

        mock_get_domain.side_effect = fake_get_domain

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        actual = drvr.get_diagnostics(instance)
        expect = {'cpu0_time': 15340000000,
                  'cpu1_time': 1640000000,
                  'cpu2_time': 3040000000,
                  'cpu3_time': 1420000000,
                  'vda_read': 688640,
                  'vda_read_req': 169,
                  'vda_write': 0,
                  'vda_write_req': 0,
                  'vda_errors': -1,
                  'vdb_read': 688640,
                  'vdb_read_req': 169,
                  'vdb_write': 0,
                  'vdb_write_req': 0,
                  'vdb_errors': -1,
                  'memory': 280160,
                  'memory-actual': 220160,
                  'memory-rss': 200164,
                  'vnet0_rx': 4408,
                  'vnet0_rx_drop': 0,
                  'vnet0_rx_errors': 0,
                  'vnet0_rx_packets': 82,
                  'vnet0_tx': 0,
                  'vnet0_tx_drop': 0,
                  'vnet0_tx_errors': 0,
                  'vnet0_tx_packets': 0,
                  'br0_rx': 4408,
                  'br0_rx_drop': 0,
                  'br0_rx_errors': 0,
                  'br0_rx_packets': 82,
                  'br0_tx': 0,
                  'br0_tx_drop': 0,
                  'br0_tx_errors': 0,
                  'br0_tx_packets': 0,
                  }
        self.assertEqual(actual, expect)

        lt = datetime.datetime(2012, 11, 22, 12, 00, 00)
        diags_time = datetime.datetime(2012, 11, 22, 12, 00, 10)
        self.useFixture(utils_fixture.TimeFixture(diags_time))

        instance.launched_at = lt
        actual = drvr.get_instance_diagnostics(instance)
        expected = {'config_drive': False,
                    'cpu_details': [{'time': 15340000000},
                                    {'time': 1640000000},
                                    {'time': 3040000000},
                                    {'time': 1420000000}],
                    'disk_details': [{'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0},
                                     {'errors_count': 0,
                                      'id': '',
                                      'read_bytes': 688640,
                                      'read_requests': 169,
                                      'write_bytes': 0,
                                      'write_requests': 0}],
                    'driver': 'libvirt',
                    'hypervisor_os': 'linux',
                    'memory_details': {'maximum': 2048, 'used': 1234},
                    'nic_details': [{'mac_address': '52:54:00:a4:38:38',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0},
                                    {'mac_address': '53:55:00:a5:39:39',
                                     'rx_drop': 0,
                                     'rx_errors': 0,
                                     'rx_octets': 4408,
                                     'rx_packets': 82,
                                     'tx_drop': 0,
                                     'tx_errors': 0,
                                     'tx_octets': 0,
                                     'tx_packets': 0}],
                    'state': 'running',
                    'uptime': 10.,
                    'version': '1.0'}
        self.assertEqual(expected, actual.serialize())

    @mock.patch.object(host.Host, "list_instance_domains")
    def test_failing_vcpu_count(self, mock_list):
        """Domain can fail to return the vcpu description in case it's
        just starting up or shutting down. Make sure None is handled
        gracefully.
        """

        class DiagFakeDomain(object):
            def __init__(self, vcpus):
                self._vcpus = vcpus

            def vcpus(self):
                if self._vcpus is None:
                    raise fakelibvirt.libvirtError("fake-error")
                else:
                    return ([[1, 2, 3, 4]] * self._vcpus, [True] * self._vcpus)

            def ID(self):
                return 1

            def name(self):
                return "instance000001"

            def UUIDString(self):
                return "19479fee-07a5-49bb-9138-d3738280d63c"

        mock_list.return_value = [
            DiagFakeDomain(None), DiagFakeDomain(5)]

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        self.assertEqual(5, drvr._get_vcpu_used())
        mock_list.assert_called_with(only_guests=True, only_running=True)

    @mock.patch.object(host.Host, "list_instance_domains")
    def test_failing_vcpu_count_none(self, mock_list):
        """Domain will return zero if the current number of vcpus used
        is None. This is in case of VM state starting up or shutting
        down. None type returned is counted as zero.
        """

        class DiagFakeDomain(object):
            def __init__(self):
                pass

            def vcpus(self):
                return None

            def ID(self):
                return 1

            def name(self):
                return "instance000001"

        mock_list.return_value = [DiagFakeDomain()]

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertEqual(0, drvr._get_vcpu_used())
        mock_list.assert_called_with(only_guests=True, only_running=True)

    def test_get_instance_capabilities(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        def get_host_capabilities_stub(self):
            caps = vconfig.LibvirtConfigCaps()

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = 'hvm'
            guest.arch = arch.X86_64
            guest.domtype = ['kvm', 'qemu']
            caps.guests.append(guest)

            guest = vconfig.LibvirtConfigGuest()
            guest.ostype = 'hvm'
            guest.arch = arch.I686
            guest.domtype = ['kvm']
            caps.guests.append(guest)

            return caps

        self.stubs.Set(host.Host, "get_capabilities",
                       get_host_capabilities_stub)

        want = [(arch.X86_64, 'kvm', 'hvm'),
                (arch.X86_64, 'qemu', 'hvm'),
                (arch.I686, 'kvm', 'hvm')]
        got = drvr._get_instance_capabilities()
        self.assertEqual(want, got)

    def test_set_cache_mode(self):
        self.flags(disk_cachemodes=['file=directsync'], group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuestDisk()

        fake_conf.source_type = 'file'
        drvr._set_cache_mode(fake_conf)
        self.assertEqual(fake_conf.driver_cache, 'directsync')

    def test_set_cache_mode_invalid_mode(self):
        self.flags(disk_cachemodes=['file=FAKE'], group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuestDisk()

        fake_conf.source_type = 'file'
        drvr._set_cache_mode(fake_conf)
        self.assertIsNone(fake_conf.driver_cache)

    def test_set_cache_mode_invalid_object(self):
        self.flags(disk_cachemodes=['file=directsync'], group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        fake_conf = FakeConfigGuest()

        fake_conf.driver_cache = 'fake'
        drvr._set_cache_mode(fake_conf)
        self.assertEqual(fake_conf.driver_cache, 'fake')

    @mock.patch('os.unlink')
    @mock.patch.object(os.path, 'exists')
    def _test_shared_storage_detection(self, is_same,
                                       mock_exists, mock_unlink):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        drvr.get_host_ip_addr = mock.MagicMock(return_value='bar')
        mock_exists.return_value = is_same
        with test.nested(
            mock.patch.object(drvr._remotefs, 'create_file'),
            mock.patch.object(drvr._remotefs, 'remove_file')
        ) as (mock_rem_fs_create, mock_rem_fs_remove):
            result = drvr._is_storage_shared_with('host', '/path')
        mock_rem_fs_create.assert_any_call('host', mock.ANY)
        create_args, create_kwargs = mock_rem_fs_create.call_args
        self.assertTrue(create_args[1].startswith('/path'))
        if is_same:
            mock_unlink.assert_called_once_with(mock.ANY)
        else:
            mock_rem_fs_remove.assert_called_with('host', mock.ANY)
            remove_args, remove_kwargs = mock_rem_fs_remove.call_args
            self.assertTrue(remove_args[1].startswith('/path'))
        return result

    def test_shared_storage_detection_same_host(self):
        self.assertTrue(self._test_shared_storage_detection(True))

    def test_shared_storage_detection_different_host(self):
        self.assertFalse(self._test_shared_storage_detection(False))

    def test_shared_storage_detection_easy(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.mox.StubOutWithMock(drvr, 'get_host_ip_addr')
        self.mox.StubOutWithMock(utils, 'execute')
        self.mox.StubOutWithMock(os.path, 'exists')
        self.mox.StubOutWithMock(os, 'unlink')
        drvr.get_host_ip_addr().AndReturn('foo')
        self.mox.ReplayAll()
        self.assertTrue(drvr._is_storage_shared_with('foo', '/path'))

    def test_store_pid_remove_pid(self):
        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        popen = mock.Mock(pid=3)
        drvr.job_tracker.add_job(instance, popen.pid)
        self.assertIn(3, drvr.job_tracker.jobs[instance.uuid])
        drvr.job_tracker.remove_job(instance, popen.pid)
        self.assertNotIn(instance.uuid, drvr.job_tracker.jobs)

    @mock.patch('nova.virt.libvirt.host.Host.get_domain')
    def test_get_domain_info_with_more_return(self, mock_get_domain):
        instance = objects.Instance(**self.test_instance)
        dom_mock = mock.MagicMock()
        dom_mock.info.return_value = [
            1, 2048, 737, 8, 12345, 888888
        ]
        dom_mock.ID.return_value = mock.sentinel.instance_id
        mock_get_domain.return_value = dom_mock
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        info = drvr.get_info(instance)
        self.assertEqual(1, info.state)
        self.assertEqual(2048, info.max_mem_kb)
        self.assertEqual(737, info.mem_kb)
        self.assertEqual(8, info.num_cpu)
        self.assertEqual(12345, info.cpu_time_ns)
        self.assertEqual(mock.sentinel.instance_id, info.id)
        dom_mock.info.assert_called_once_with()
        dom_mock.ID.assert_called_once_with()
        mock_get_domain.assert_called_once_with(instance)

    def test_create_domain(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        mock_domain = mock.MagicMock()

        guest = drvr._create_domain(domain=mock_domain)

        self.assertEqual(mock_domain, guest._domain)
        mock_domain.createWithFlags.assert_has_calls([mock.call(0)])

    @mock.patch('nova.virt.disk.api.clean_lxc_namespace')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.get_info')
    @mock.patch('nova.virt.disk.api.setup_container')
    @mock.patch('oslo_utils.fileutils.ensure_tree')
    @mock.patch.object(fake_libvirt_utils, 'get_instance_path')
    def test_create_domain_lxc(self, mock_get_inst_path, mock_ensure_tree,
                           mock_setup_container, mock_get_info, mock_clean):
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        mock_instance = mock.MagicMock()
        inst_sys_meta = dict()
        mock_instance.system_metadata = inst_sys_meta
        mock_get_inst_path.return_value = '/tmp/'
        mock_image_backend = mock.MagicMock()
        drvr.image_backend = mock_image_backend
        mock_image = mock.MagicMock()
        mock_image.path = '/tmp/test.img'
        drvr.image_backend.image.return_value = mock_image
        mock_setup_container.return_value = '/dev/nbd0'
        mock_get_info.return_value = hardware.InstanceInfo(
            state=power_state.RUNNING)

        with test.nested(
            mock.patch.object(drvr, '_is_booted_from_volume',
                              return_value=False),
            mock.patch.object(drvr, '_create_domain'),
            mock.patch.object(drvr, 'plug_vifs'),
            mock.patch.object(drvr.firewall_driver, 'setup_basic_filtering'),
            mock.patch.object(drvr.firewall_driver, 'prepare_instance_filter'),
            mock.patch.object(drvr.firewall_driver, 'apply_instance_filter')):
            drvr._create_domain_and_network(self.context, 'xml',
                                            mock_instance, [], None)

        self.assertEqual('/dev/nbd0', inst_sys_meta['rootfs_device_name'])
        self.assertFalse(mock_instance.called)
        mock_get_inst_path.assert_has_calls([mock.call(mock_instance)])
        mock_ensure_tree.assert_has_calls([mock.call('/tmp/rootfs')])
        drvr.image_backend.image.assert_has_calls([mock.call(mock_instance,
                                                             'disk')])

        setup_container_call = mock.call(
            mock_image.get_model(),
            container_dir='/tmp/rootfs')
        mock_setup_container.assert_has_calls([setup_container_call])
        mock_get_info.assert_has_calls([mock.call(mock_instance)])
        mock_clean.assert_has_calls([mock.call(container_dir='/tmp/rootfs')])

    @mock.patch('nova.virt.disk.api.clean_lxc_namespace')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.get_info')
    @mock.patch.object(fake_libvirt_utils, 'chown_for_id_maps')
    @mock.patch('nova.virt.disk.api.setup_container')
    @mock.patch('oslo_utils.fileutils.ensure_tree')
    @mock.patch.object(fake_libvirt_utils, 'get_instance_path')
    def test_create_domain_lxc_id_maps(self, mock_get_inst_path,
                                       mock_ensure_tree, mock_setup_container,
                                       mock_chown, mock_get_info, mock_clean):
        self.flags(virt_type='lxc', uid_maps=["0:1000:100"],
                   gid_maps=["0:1000:100"], group='libvirt')

        def chown_side_effect(path, id_maps):
            self.assertEqual('/tmp/rootfs', path)
            self.assertIsInstance(id_maps[0], vconfig.LibvirtConfigGuestUIDMap)
            self.assertEqual(0, id_maps[0].start)
            self.assertEqual(1000, id_maps[0].target)
            self.assertEqual(100, id_maps[0].count)
            self.assertIsInstance(id_maps[1], vconfig.LibvirtConfigGuestGIDMap)
            self.assertEqual(0, id_maps[1].start)
            self.assertEqual(1000, id_maps[1].target)
            self.assertEqual(100, id_maps[1].count)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        mock_instance = mock.MagicMock()
        inst_sys_meta = dict()
        mock_instance.system_metadata = inst_sys_meta
        mock_get_inst_path.return_value = '/tmp/'
        mock_image_backend = mock.MagicMock()
        drvr.image_backend = mock_image_backend
        mock_image = mock.MagicMock()
        mock_image.path = '/tmp/test.img'
        drvr.image_backend.image.return_value = mock_image
        mock_setup_container.return_value = '/dev/nbd0'
        mock_chown.side_effect = chown_side_effect
        mock_get_info.return_value = hardware.InstanceInfo(
            state=power_state.RUNNING)

        with test.nested(
            mock.patch.object(drvr, '_is_booted_from_volume',
                              return_value=False),
            mock.patch.object(drvr, '_create_domain'),
            mock.patch.object(drvr, 'plug_vifs'),
            mock.patch.object(drvr.firewall_driver, 'setup_basic_filtering'),
            mock.patch.object(drvr.firewall_driver, 'prepare_instance_filter'),
            mock.patch.object(drvr.firewall_driver, 'apply_instance_filter')
        ) as (
            mock_is_booted_from_volume, mock_create_domain, mock_plug_vifs,
            mock_setup_basic_filtering, mock_prepare_instance_filter,
            mock_apply_instance_filter
        ):
            drvr._create_domain_and_network(self.context, 'xml',
                                            mock_instance, [], None)

        self.assertEqual('/dev/nbd0', inst_sys_meta['rootfs_device_name'])
        self.assertFalse(mock_instance.called)
        mock_get_inst_path.assert_has_calls([mock.call(mock_instance)])
        mock_is_booted_from_volume.assert_called_once_with(mock_instance, {})
        mock_ensure_tree.assert_has_calls([mock.call('/tmp/rootfs')])
        drvr.image_backend.image.assert_has_calls([mock.call(mock_instance,
                                                             'disk')])

        setup_container_call = mock.call(
            mock_image.get_model(),
            container_dir='/tmp/rootfs')
        mock_setup_container.assert_has_calls([setup_container_call])
        mock_get_info.assert_has_calls([mock.call(mock_instance)])
        mock_clean.assert_has_calls([mock.call(container_dir='/tmp/rootfs')])

    @mock.patch('nova.virt.disk.api.teardown_container')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.get_info')
    @mock.patch('nova.virt.disk.api.setup_container')
    @mock.patch('oslo_utils.fileutils.ensure_tree')
    @mock.patch.object(fake_libvirt_utils, 'get_instance_path')
    def test_create_domain_lxc_not_running(self, mock_get_inst_path,
                                           mock_ensure_tree,
                                           mock_setup_container,
                                           mock_get_info, mock_teardown):
        self.flags(virt_type='lxc', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        mock_instance = mock.MagicMock()
        inst_sys_meta = dict()
        mock_instance.system_metadata = inst_sys_meta
        mock_get_inst_path.return_value = '/tmp/'
        mock_image_backend = mock.MagicMock()
        drvr.image_backend = mock_image_backend
        mock_image = mock.MagicMock()
        mock_image.path = '/tmp/test.img'
        drvr.image_backend.image.return_value = mock_image
        mock_setup_container.return_value = '/dev/nbd0'
        mock_get_info.return_value = hardware.InstanceInfo(
            state=power_state.SHUTDOWN)

        with test.nested(
            mock.patch.object(drvr, '_is_booted_from_volume',
                              return_value=False),
            mock.patch.object(drvr, '_create_domain'),
            mock.patch.object(drvr, 'plug_vifs'),
            mock.patch.object(drvr.firewall_driver, 'setup_basic_filtering'),
            mock.patch.object(drvr.firewall_driver, 'prepare_instance_filter'),
            mock.patch.object(drvr.firewall_driver, 'apply_instance_filter')):
            drvr._create_domain_and_network(self.context, 'xml',
                                            mock_instance, [], None)

        self.assertEqual('/dev/nbd0', inst_sys_meta['rootfs_device_name'])
        self.assertFalse(mock_instance.called)
        mock_get_inst_path.assert_has_calls([mock.call(mock_instance)])
        mock_ensure_tree.assert_has_calls([mock.call('/tmp/rootfs')])
        drvr.image_backend.image.assert_has_calls([mock.call(mock_instance,
                                                             'disk')])

        setup_container_call = mock.call(
            mock_image.get_model(),
            container_dir='/tmp/rootfs')
        mock_setup_container.assert_has_calls([setup_container_call])
        mock_get_info.assert_has_calls([mock.call(mock_instance)])
        teardown_call = mock.call(container_dir='/tmp/rootfs')
        mock_teardown.assert_has_calls([teardown_call])

    def test_create_domain_define_xml_fails(self):
        """Tests that the xml is logged when defining the domain fails."""
        fake_xml = "<test>this is a test</test>"

        def fake_defineXML(xml):
            self.assertEqual(fake_xml, xml)
            raise fakelibvirt.libvirtError('virDomainDefineXML() failed')

        def fake_safe_decode(text, *args, **kwargs):
            return text + 'safe decoded'

        self.log_error_called = False

        def fake_error(msg, *args, **kwargs):
            self.log_error_called = True
            self.assertIn(fake_xml, msg % args)
            self.assertIn('safe decoded', msg % args)

        self.stubs.Set(encodeutils, 'safe_decode', fake_safe_decode)
        self.stubs.Set(nova.virt.libvirt.guest.LOG, 'error', fake_error)

        self.create_fake_libvirt_mock(defineXML=fake_defineXML)
        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        self.assertRaises(fakelibvirt.libvirtError, drvr._create_domain,
                          fake_xml)
        self.assertTrue(self.log_error_called)

    def test_create_domain_with_flags_fails(self):
        """Tests that the xml is logged when creating the domain with flags
        fails
        """
        fake_xml = "<test>this is a test</test>"
        fake_domain = FakeVirtDomain(fake_xml)

        def fake_createWithFlags(launch_flags):
            raise fakelibvirt.libvirtError('virDomainCreateWithFlags() failed')

        self.log_error_called = False

        def fake_error(msg, *args, **kwargs):
            self.log_error_called = True
            self.assertIn(fake_xml, msg % args)

        self.stubs.Set(fake_domain, 'createWithFlags', fake_createWithFlags)
        self.stubs.Set(nova.virt.libvirt.guest.LOG, 'error', fake_error)

        self.create_fake_libvirt_mock()
        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        self.assertRaises(fakelibvirt.libvirtError, drvr._create_domain,
                          domain=fake_domain)
        self.assertTrue(self.log_error_called)

    def test_create_domain_enable_hairpin_fails(self):
        """Tests that the xml is logged when enabling hairpin mode for the
        domain fails.
        """
        fake_xml = "<test>this is a test</test>"
        fake_domain = FakeVirtDomain(fake_xml)

        def fake_execute(*args, **kwargs):
            raise processutils.ProcessExecutionError('error')

        def fake_get_interfaces(*args):
            return ["dev"]

        self.log_error_called = False

        def fake_error(msg, *args, **kwargs):
            self.log_error_called = True
            self.assertIn(fake_xml, msg % args)

        self.stubs.Set(nova.virt.libvirt.guest.LOG, 'error', fake_error)

        self.create_fake_libvirt_mock()
        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        self.stubs.Set(nova.utils, 'execute', fake_execute)
        self.stubs.Set(
            nova.virt.libvirt.guest.Guest, 'get_interfaces',
            fake_get_interfaces)

        self.assertRaises(processutils.ProcessExecutionError,
                          drvr._create_domain,
                          domain=fake_domain,
                          power_on=False)
        self.assertTrue(self.log_error_called)

    def test_get_vnc_console(self):
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<graphics type='vnc' port='5900'/>"
                    "</devices></domain>")

        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance['name']:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        vnc_dict = drvr.get_vnc_console(self.context, instance)
        self.assertEqual(vnc_dict.port, '5900')

    def test_get_vnc_console_unavailable(self):
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices></devices></domain>")

        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance['name']:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.ConsoleTypeUnavailable,
                          drvr.get_vnc_console, self.context, instance)

    def test_get_spice_console(self):
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<graphics type='spice' port='5950'/>"
                    "</devices></domain>")

        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance['name']:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        spice_dict = drvr.get_spice_console(self.context, instance)
        self.assertEqual(spice_dict.port, '5950')

    def test_get_spice_console_unavailable(self):
        instance = objects.Instance(**self.test_instance)
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices></devices></domain>")

        vdmock = self.mox.CreateMock(fakelibvirt.virDomain)
        self.mox.StubOutWithMock(vdmock, "XMLDesc")
        vdmock.XMLDesc(flags=0).AndReturn(dummyxml)

        def fake_lookup(instance_name):
            if instance_name == instance['name']:
                return vdmock
        self.create_fake_libvirt_mock(lookupByName=fake_lookup)

        self.mox.ReplayAll()
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertRaises(exception.ConsoleTypeUnavailable,
                          drvr.get_spice_console, self.context, instance)

    def test_detach_volume_with_instance_not_found(self):
        # Test that detach_volume() method does not raise exception,
        # if the instance does not exist.

        instance = objects.Instance(**self.test_instance)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with test.nested(
            mock.patch.object(host.Host, 'get_domain',
                              side_effect=exception.InstanceNotFound(
                                  instance_id=instance.uuid)),
            mock.patch.object(drvr, '_disconnect_volume')
        ) as (_get_domain, _disconnect_volume):
            connection_info = {'driver_volume_type': 'fake'}
            drvr.detach_volume(connection_info, instance, '/dev/sda')
            _get_domain.assert_called_once_with(instance)
            _disconnect_volume.assert_called_once_with(connection_info,
                                                       'sda')

    def _test_attach_detach_interface_get_config(self, method_name):
        """Tests that the get_config() method is properly called in
        attach_interface() and detach_interface().

        method_name: either \"attach_interface\" or \"detach_interface\"
                     depending on the method to test.
        """
        self.stubs.Set(host.Host, "get_domain", lambda a, b: FakeVirtDomain())

        instance = objects.Instance(**self.test_instance)
        network_info = _fake_network_info(self, 1)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        fake_image_meta = objects.ImageMeta.from_dict(
            {'id': instance['image_ref']})

        if method_name == "attach_interface":
            self.mox.StubOutWithMock(drvr.firewall_driver,
                                     'setup_basic_filtering')
            drvr.firewall_driver.setup_basic_filtering(instance, network_info)

        expected = drvr.vif_driver.get_config(instance, network_info[0],
                                              fake_image_meta,
                                              instance.get_flavor(),
                                              CONF.libvirt.virt_type,
                                              drvr._host)
        self.mox.StubOutWithMock(drvr.vif_driver, 'get_config')
        drvr.vif_driver.get_config(instance, network_info[0],
                                   mox.IsA(objects.ImageMeta),
                                   mox.IsA(objects.Flavor),
                                   CONF.libvirt.virt_type,
                                   drvr._host).\
                                   AndReturn(expected)

        self.mox.ReplayAll()

        if method_name == "attach_interface":
            drvr.attach_interface(instance, fake_image_meta,
                                  network_info[0])
        elif method_name == "detach_interface":
            drvr.detach_interface(instance, network_info[0])
        else:
            raise ValueError("Unhandled method %s" % method_name)

    @mock.patch.object(lockutils, "external_lock")
    def test_attach_interface_get_config(self, mock_lock):
        """Tests that the get_config() method is properly called in
        attach_interface().
        """
        mock_lock.return_value = threading.Semaphore()

        self._test_attach_detach_interface_get_config("attach_interface")

    def test_detach_interface_get_config(self):
        """Tests that the get_config() method is properly called in
        detach_interface().
        """
        self._test_attach_detach_interface_get_config("detach_interface")

    def test_default_root_device_name(self):
        instance = {'uuid': 'fake_instance'}
        image_meta = objects.ImageMeta.from_dict({'id': uuids.image_id})
        root_bdm = {'source_type': 'image',
                    'destination_type': 'volume',
                    'image_id': 'fake_id'}
        self.flags(virt_type='qemu', group='libvirt')

        self.mox.StubOutWithMock(blockinfo, 'get_disk_bus_for_device_type')
        self.mox.StubOutWithMock(blockinfo, 'get_root_info')

        blockinfo.get_disk_bus_for_device_type(instance,
                                               'qemu',
                                               image_meta,
                                               'disk').InAnyOrder().\
                                                AndReturn('virtio')
        blockinfo.get_disk_bus_for_device_type(instance,
                                               'qemu',
                                               image_meta,
                                               'cdrom').InAnyOrder().\
                                                AndReturn('ide')
        blockinfo.get_root_info(instance, 'qemu',
                                image_meta, root_bdm,
                                'virtio', 'ide').AndReturn({'dev': 'vda'})
        self.mox.ReplayAll()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.assertEqual(drvr.default_root_device_name(instance, image_meta,
                                                       root_bdm), '/dev/vda')

    @mock.patch.object(objects.BlockDeviceMapping, "save")
    def test_default_device_names_for_instance(self, save_mock):
        instance = objects.Instance(**self.test_instance)
        instance.root_device_name = '/dev/vda'
        ephemerals = [objects.BlockDeviceMapping(
                        **fake_block_device.AnonFakeDbBlockDeviceDict(
                            {'device_name': 'vdb',
                             'source_type': 'blank',
                             'volume_size': 2,
                             'destination_type': 'local'}))]
        swap = [objects.BlockDeviceMapping(
                        **fake_block_device.AnonFakeDbBlockDeviceDict(
                            {'device_name': 'vdg',
                             'source_type': 'blank',
                             'volume_size': 512,
                             'guest_format': 'swap',
                             'destination_type': 'local'}))]
        block_device_mapping = [
            objects.BlockDeviceMapping(
                **fake_block_device.AnonFakeDbBlockDeviceDict(
                    {'source_type': 'volume',
                     'destination_type': 'volume',
                     'volume_id': 'fake-image-id',
                     'device_name': '/dev/vdxx',
                     'disk_bus': 'scsi'}))]

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.default_device_names_for_instance(instance,
                                               instance.root_device_name,
                                               ephemerals, swap,
                                               block_device_mapping)

        # Ephemeral device name was correct so no changes
        self.assertEqual('/dev/vdb', ephemerals[0].device_name)
        # Swap device name was incorrect so it was changed
        self.assertEqual('/dev/vdc', swap[0].device_name)
        # Volume device name was changed too, taking the bus into account
        self.assertEqual('/dev/sda', block_device_mapping[0].device_name)

        self.assertEqual(3, save_mock.call_count)

    def _test_get_device_name_for_instance(self, new_bdm, expected_dev):
        instance = objects.Instance(**self.test_instance)
        instance.root_device_name = '/dev/vda'
        instance.ephemeral_gb = 0
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        got_dev = drvr.get_device_name_for_instance(
            instance, [], new_bdm)
        self.assertEqual(expected_dev, got_dev)

    def test_get_device_name_for_instance_simple(self):
        new_bdm = objects.BlockDeviceMapping(
            context=context,
            source_type='volume', destination_type='volume',
            boot_index=-1, volume_id='fake-id',
            device_name=None, guest_format=None,
            disk_bus=None, device_type=None)
        self._test_get_device_name_for_instance(new_bdm, '/dev/vdb')

    def test_get_device_name_for_instance_suggested(self):
        new_bdm = objects.BlockDeviceMapping(
            context=context,
            source_type='volume', destination_type='volume',
            boot_index=-1, volume_id='fake-id',
            device_name='/dev/vdg', guest_format=None,
            disk_bus=None, device_type=None)
        self._test_get_device_name_for_instance(new_bdm, '/dev/vdb')

    def test_get_device_name_for_instance_bus(self):
        new_bdm = objects.BlockDeviceMapping(
            context=context,
            source_type='volume', destination_type='volume',
            boot_index=-1, volume_id='fake-id',
            device_name=None, guest_format=None,
            disk_bus='scsi', device_type=None)
        self._test_get_device_name_for_instance(new_bdm, '/dev/sda')

    def test_get_device_name_for_instance_device_type(self):
        new_bdm = objects.BlockDeviceMapping(
            context=context,
            source_type='volume', destination_type='volume',
            boot_index=-1, volume_id='fake-id',
            device_name=None, guest_format=None,
            disk_bus=None, device_type='floppy')
        self._test_get_device_name_for_instance(new_bdm, '/dev/fda')

    def test_is_supported_fs_format(self):
        supported_fs = [disk_api.FS_FORMAT_EXT2, disk_api.FS_FORMAT_EXT3,
                        disk_api.FS_FORMAT_EXT4, disk_api.FS_FORMAT_XFS]
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        for fs in supported_fs:
            self.assertTrue(drvr.is_supported_fs_format(fs))

        supported_fs = ['', 'dummy']
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        for fs in supported_fs:
            self.assertFalse(drvr.is_supported_fs_format(fs))

    def test_post_live_migration_at_destination_with_block_device_info(self):
        # Preparing mocks
        mock_domain = self.mox.CreateMock(fakelibvirt.virDomain)
        self.resultXML = None

        def fake_getLibVersion():
            return fakelibvirt.FAKE_LIBVIRT_VERSION

        def fake_getCapabilities():
            return """
            <capabilities>
                <host>
                    <uuid>cef19ce0-0ca2-11df-855d-b19fbce37686</uuid>
                    <cpu>
                      <arch>x86_64</arch>
                      <model>Penryn</model>
                      <vendor>Intel</vendor>
                      <topology sockets='1' cores='2' threads='1'/>
                      <feature name='xtpr'/>
                    </cpu>
                </host>
            </capabilities>
            """

        def fake_to_xml(context, instance, network_info, disk_info,
                        image_meta=None, rescue=None,
                        block_device_info=None, write_to_disk=False):
            if image_meta is None:
                image_meta = objects.ImageMeta.from_dict({})
            conf = drvr._get_guest_config(instance, network_info, image_meta,
                                          disk_info, rescue, block_device_info)
            self.resultXML = conf.to_xml()
            return self.resultXML

        def fake_get_domain(instance):
            return mock_domain

        def fake_baselineCPU(cpu, flag):
            return """<cpu mode='custom' match='exact'>
                        <model fallback='allow'>Westmere</model>
                        <vendor>Intel</vendor>
                        <feature policy='require' name='aes'/>
                      </cpu>
                   """

        network_info = _fake_network_info(self, 1)
        self.create_fake_libvirt_mock(getLibVersion=fake_getLibVersion,
                                      getCapabilities=fake_getCapabilities,
                                      getVersion=lambda: 1005001,
                                      listDefinedDomains=lambda: [],
                                      numOfDomains=lambda: 0,
                                      baselineCPU=fake_baselineCPU)
        instance_ref = self.test_instance
        instance_ref['image_ref'] = 123456  # we send an int to test sha1 call
        instance = objects.Instance(**instance_ref)

        self.mox.ReplayAll()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.stubs.Set(drvr,
                       '_get_guest_xml',
                       fake_to_xml)
        self.stubs.Set(host.Host,
                       'get_domain',
                       fake_get_domain)
        bdm = objects.BlockDeviceMapping(
            self.context,
            **fake_block_device.FakeDbBlockDeviceDict(
                {'id': 1, 'guest_format': None,
                 'boot_index': 0,
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_name': '/dev/vda',
                 'disk_bus': 'virtio',
                 'device_type': 'disk',
                 'delete_on_termination': False}))
        block_device_info = {'block_device_mapping':
                driver_block_device.convert_volumes([bdm])}
        block_device_info['block_device_mapping'][0]['connection_info'] = (
                {'driver_volume_type': 'iscsi'})
        with test.nested(
                mock.patch.object(
                    driver_block_device.DriverVolumeBlockDevice, 'save'),
                mock.patch.object(objects.Instance, 'save')
        ) as (mock_volume_save, mock_instance_save):
            drvr.post_live_migration_at_destination(
                    self.context, instance, network_info, True,
                    block_device_info=block_device_info)
            self.assertIn('fake', self.resultXML)
            mock_volume_save.assert_called_once_with()

    def test_create_propagates_exceptions(self):
        self.flags(virt_type='lxc', group='libvirt')

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(id=1, uuid=uuids.instance,
                                    image_ref='my_fake_image')

        with test.nested(
              mock.patch.object(drvr, '_create_domain_setup_lxc'),
              mock.patch.object(drvr, '_create_domain_cleanup_lxc'),
              mock.patch.object(drvr, '_is_booted_from_volume',
                                return_value=False),
              mock.patch.object(drvr, 'plug_vifs'),
              mock.patch.object(drvr, 'firewall_driver'),
              mock.patch.object(drvr, '_create_domain',
                                side_effect=exception.NovaException),
              mock.patch.object(drvr, 'cleanup')):
            self.assertRaises(exception.NovaException,
                              drvr._create_domain_and_network,
                              self.context,
                              'xml',
                              instance, None, None)

    def test_create_without_pause(self):
        self.flags(virt_type='lxc', group='libvirt')

        @contextlib.contextmanager
        def fake_lxc_disk_handler(*args, **kwargs):
            yield

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)

        with test.nested(
              mock.patch.object(drvr, '_lxc_disk_handler',
                                side_effect=fake_lxc_disk_handler),
              mock.patch.object(drvr, 'plug_vifs'),
              mock.patch.object(drvr, 'firewall_driver'),
              mock.patch.object(drvr, '_create_domain'),
              mock.patch.object(drvr, 'cleanup')) as (
              _handler, cleanup, firewall_driver, create, plug_vifs):
            domain = drvr._create_domain_and_network(self.context, 'xml',
                                                     instance, None, None)
            self.assertEqual(0, create.call_args_list[0][1]['pause'])
            self.assertEqual(0, domain.resume.call_count)

    def _test_create_with_network_events(self, neutron_failure=None,
                                         power_on=True):
        generated_events = []

        def wait_timeout():
            event = mock.MagicMock()
            if neutron_failure == 'timeout':
                raise eventlet.timeout.Timeout()
            elif neutron_failure == 'error':
                event.status = 'failed'
            else:
                event.status = 'completed'
            return event

        def fake_prepare(instance, event_name):
            m = mock.MagicMock()
            m.instance = instance
            m.event_name = event_name
            m.wait.side_effect = wait_timeout
            generated_events.append(m)
            return m

        virtapi = manager.ComputeVirtAPI(mock.MagicMock())
        prepare = virtapi._compute.instance_events.prepare_for_instance_event
        prepare.side_effect = fake_prepare
        drvr = libvirt_driver.LibvirtDriver(virtapi, False)

        instance = objects.Instance(**self.test_instance)
        vifs = [{'id': 'vif1', 'active': False},
                {'id': 'vif2', 'active': False}]

        @mock.patch.object(drvr, 'plug_vifs')
        @mock.patch.object(drvr, 'firewall_driver')
        @mock.patch.object(drvr, '_create_domain')
        @mock.patch.object(drvr, 'cleanup')
        def test_create(cleanup, create, fw_driver, plug_vifs):
            domain = drvr._create_domain_and_network(self.context, 'xml',
                                                     instance, vifs, None,
                                                     power_on=power_on)
            plug_vifs.assert_called_with(instance, vifs)

            pause = self._get_pause_flag(drvr, vifs, power_on=power_on)
            self.assertEqual(pause,
                             create.call_args_list[0][1]['pause'])
            if pause:
                domain.resume.assert_called_once_with()
            if neutron_failure and CONF.vif_plugging_is_fatal:
                cleanup.assert_called_once_with(self.context,
                                                instance, network_info=vifs,
                                                block_device_info=None)

        test_create()

        if utils.is_neutron() and CONF.vif_plugging_timeout and power_on:
            prepare.assert_has_calls([
                mock.call(instance, 'network-vif-plugged-vif1'),
                mock.call(instance, 'network-vif-plugged-vif2')])
            for event in generated_events:
                if neutron_failure and generated_events.index(event) != 0:
                    self.assertEqual(0, event.call_count)
                elif (neutron_failure == 'error' and
                          not CONF.vif_plugging_is_fatal):
                    event.wait.assert_called_once_with()
        else:
            self.assertEqual(0, prepare.call_count)

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron(self, is_neutron):
        self._test_create_with_network_events()

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_power_off(self,
                                                          is_neutron):
        # Tests that we don't wait for events if we don't start the instance.
        self._test_create_with_network_events(power_on=False)

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_nowait(self, is_neutron):
        self.flags(vif_plugging_timeout=0)
        self._test_create_with_network_events()

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_failed_nonfatal_timeout(
            self, is_neutron):
        self.flags(vif_plugging_is_fatal=False)
        self._test_create_with_network_events(neutron_failure='timeout')

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_failed_fatal_timeout(
            self, is_neutron):
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          self._test_create_with_network_events,
                          neutron_failure='timeout')

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_failed_nonfatal_error(
            self, is_neutron):
        self.flags(vif_plugging_is_fatal=False)
        self._test_create_with_network_events(neutron_failure='error')

    @mock.patch('nova.utils.is_neutron', return_value=True)
    def test_create_with_network_events_neutron_failed_fatal_error(
            self, is_neutron):
        self.assertRaises(exception.VirtualInterfaceCreateException,
                          self._test_create_with_network_events,
                          neutron_failure='error')

    @mock.patch('nova.utils.is_neutron', return_value=False)
    def test_create_with_network_events_non_neutron(self, is_neutron):
        self._test_create_with_network_events()

    @mock.patch('nova.volume.encryptors.get_encryption_metadata')
    @mock.patch('nova.virt.libvirt.blockinfo.get_info_from_bdm')
    def test_create_with_bdm(self, get_info_from_bdm, get_encryption_metadata):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        mock_dom = mock.MagicMock()
        mock_encryption_meta = mock.MagicMock()
        get_encryption_metadata.return_value = mock_encryption_meta

        fake_xml = """
            <domain>
                <name>instance-00000001</name>
                <memory>1048576</memory>
                <vcpu>1</vcpu>
                <devices>
                    <disk type='file' device='disk'>
                        <driver name='qemu' type='raw' cache='none'/>
                        <source file='/path/fake-volume1'/>
                        <target dev='vda' bus='virtio'/>
                    </disk>
                </devices>
            </domain>
        """
        fake_volume_id = "fake-volume-id"
        connection_info = {"driver_volume_type": "fake",
                           "data": {"access_mode": "rw",
                                    "volume_id": fake_volume_id}}

        def fake_getitem(*args, **kwargs):
            fake_bdm = {'connection_info': connection_info,
                        'mount_device': '/dev/vda'}
            return fake_bdm.get(args[0])

        mock_volume = mock.MagicMock()
        mock_volume.__getitem__.side_effect = fake_getitem
        block_device_info = {'block_device_mapping': [mock_volume]}
        network_info = [network_model.VIF(id='1'),
                        network_model.VIF(id='2', active=True)]

        with test.nested(
            mock.patch.object(drvr, '_get_volume_encryptor'),
            mock.patch.object(drvr, 'plug_vifs'),
            mock.patch.object(drvr.firewall_driver, 'setup_basic_filtering'),
            mock.patch.object(drvr.firewall_driver,
                              'prepare_instance_filter'),
            mock.patch.object(drvr, '_create_domain'),
            mock.patch.object(drvr.firewall_driver, 'apply_instance_filter'),
        ) as (get_volume_encryptor, plug_vifs, setup_basic_filtering,
              prepare_instance_filter, create_domain, apply_instance_filter):
            create_domain.return_value = libvirt_guest.Guest(mock_dom)

            guest = drvr._create_domain_and_network(
                    self.context, fake_xml, instance, network_info, None,
                    block_device_info=block_device_info)

            get_encryption_metadata.assert_called_once_with(self.context,
                drvr._volume_api, fake_volume_id, connection_info)
            get_volume_encryptor.assert_called_once_with(connection_info,
                                                         mock_encryption_meta)
            plug_vifs.assert_called_once_with(instance, network_info)
            setup_basic_filtering.assert_called_once_with(instance,
                                                          network_info)
            prepare_instance_filter.assert_called_once_with(instance,
                                                          network_info)
            pause = self._get_pause_flag(drvr, network_info)
            create_domain.assert_called_once_with(
                fake_xml, pause=pause, power_on=True, post_xml_callback=None)
            self.assertEqual(mock_dom, guest._domain)

    def test_get_guest_storage_config(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        test_instance = copy.deepcopy(self.test_instance)
        test_instance["default_swap_device"] = None
        instance = objects.Instance(**test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        flavor = instance.get_flavor()
        conn_info = {'driver_volume_type': 'fake', 'data': {}}
        bdm = objects.BlockDeviceMapping(
            self.context,
            **fake_block_device.FakeDbBlockDeviceDict({
                   'id': 1,
                   'source_type': 'volume',
                   'destination_type': 'volume',
                   'device_name': '/dev/vdc'}))
        bdi = {'block_device_mapping':
               driver_block_device.convert_volumes([bdm])}
        bdm = bdi['block_device_mapping'][0]
        bdm['connection_info'] = conn_info
        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance,
                                            image_meta,
                                            bdi)
        mock_conf = mock.MagicMock(source_path='fake')

        with test.nested(
            mock.patch.object(driver_block_device.DriverVolumeBlockDevice,
                              'save'),
            mock.patch.object(drvr, '_connect_volume'),
            mock.patch.object(drvr, '_get_volume_config',
                              return_value=mock_conf),
            mock.patch.object(drvr, '_set_cache_mode')
        ) as (volume_save, connect_volume, get_volume_config, set_cache_mode):
            devices = drvr._get_guest_storage_config(instance, image_meta,
                disk_info, False, bdi, flavor, "hvm")

            self.assertEqual(3, len(devices))
            self.assertEqual('/dev/vdb', instance.default_ephemeral_device)
            self.assertIsNone(instance.default_swap_device)
            connect_volume.assert_called_with(bdm['connection_info'],
                {'bus': 'virtio', 'type': 'disk', 'dev': 'vdc'})
            get_volume_config.assert_called_with(bdm['connection_info'],
                {'bus': 'virtio', 'type': 'disk', 'dev': 'vdc'})
            volume_save.assert_called_once_with()
            self.assertEqual(3, set_cache_mode.call_count)

    def test_get_neutron_events(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        network_info = [network_model.VIF(id='1'),
                        network_model.VIF(id='2', active=True)]
        events = drvr._get_neutron_events(network_info)
        self.assertEqual([('network-vif-plugged', '1')], events)

    def test_unplug_vifs_ignores_errors(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        with mock.patch.object(drvr, 'vif_driver') as vif_driver:
            vif_driver.unplug.side_effect = exception.AgentError(
                method='unplug')
            drvr._unplug_vifs('inst', [1], ignore_errors=True)
            vif_driver.unplug.assert_called_once_with('inst', 1)

    def test_unplug_vifs_reports_errors(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        with mock.patch.object(drvr, 'vif_driver') as vif_driver:
            vif_driver.unplug.side_effect = exception.AgentError(
                method='unplug')
            self.assertRaises(exception.AgentError,
                              drvr.unplug_vifs, 'inst', [1])
            vif_driver.unplug.assert_called_once_with('inst', 1)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._unplug_vifs')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._undefine_domain')
    def test_cleanup_pass_with_no_mount_device(self, undefine, unplug):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        drvr.firewall_driver = mock.Mock()
        drvr._disconnect_volume = mock.Mock()
        fake_inst = {'name': 'foo'}
        fake_bdms = [{'connection_info': 'foo',
                     'mount_device': None}]
        with mock.patch('nova.virt.driver'
                        '.block_device_info_get_mapping',
                        return_value=fake_bdms):
            drvr.cleanup('ctxt', fake_inst, 'netinfo', destroy_disks=False)
        self.assertTrue(drvr._disconnect_volume.called)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._unplug_vifs')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._undefine_domain')
    def test_cleanup_wants_vif_errors_ignored(self, undefine, unplug):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        fake_inst = {'name': 'foo'}
        with mock.patch.object(drvr._conn, 'lookupByName') as lookup:
            lookup.return_value = fake_inst
            # NOTE(danms): Make unplug cause us to bail early, since
            # we only care about how it was called
            unplug.side_effect = test.TestingException
            self.assertRaises(test.TestingException,
                              drvr.cleanup, 'ctxt', fake_inst, 'netinfo')
            unplug.assert_called_once_with(fake_inst, 'netinfo', True)

    @mock.patch.object(libvirt_driver.LibvirtDriver, 'unfilter_instance')
    @mock.patch.object(libvirt_driver.LibvirtDriver, 'delete_instance_files',
                       return_value=True)
    @mock.patch.object(objects.Instance, 'save')
    @mock.patch.object(libvirt_driver.LibvirtDriver, '_undefine_domain')
    def test_cleanup_migrate_data_shared_block_storage(self,
                                                       _undefine_domain,
                                                       save,
                                                       delete_instance_files,
                                                       unfilter_instance):
        # Tests the cleanup method when migrate_data has
        # is_shared_block_storage=True and destroy_disks=False.
        instance = objects.Instance(self.context, **self.test_instance)
        migrate_data = objects.LibvirtLiveMigrateData(
                is_shared_block_storage=True)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        drvr.cleanup(
            self.context, instance, network_info={}, destroy_disks=False,
            migrate_data=migrate_data, destroy_vifs=False)
        delete_instance_files.assert_called_once_with(instance)
        self.assertEqual(1, int(instance.system_metadata['clean_attempts']))
        self.assertTrue(instance.cleaned)
        save.assert_called_once_with()

    def test_swap_volume(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())

        mock_dom = mock.MagicMock()
        guest = libvirt_guest.Guest(mock_dom)

        with mock.patch.object(drvr._conn, 'defineXML',
                               create=True) as mock_define:
            xmldoc = "<domain/>"
            srcfile = "/first/path"
            dstfile = "/second/path"

            mock_dom.XMLDesc.return_value = xmldoc
            mock_dom.isPersistent.return_value = True
            mock_dom.blockJobInfo.return_value = {}

            drvr._swap_volume(guest, srcfile, dstfile, 1)

            mock_dom.XMLDesc.assert_called_once_with(
                flags=(fakelibvirt.VIR_DOMAIN_XML_INACTIVE |
                       fakelibvirt.VIR_DOMAIN_XML_SECURE))
            mock_dom.blockRebase.assert_called_once_with(
                srcfile, dstfile, 0, flags=(
                    fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_COPY |
                    fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_REUSE_EXT))
            mock_dom.blockResize.assert_called_once_with(
                srcfile, 1 * units.Gi / units.Ki)
            mock_define.assert_called_once_with(xmldoc)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._disconnect_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._swap_volume')
    @mock.patch('nova.objects.block_device.BlockDeviceMapping.'
                'get_by_volume_and_instance')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._get_volume_config')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._connect_volume')
    @mock.patch('nova.virt.libvirt.host.Host.get_guest')
    def _test_swap_volume_driver_bdm_save(self, get_guest,
                                         connect_volume, get_volume_config,
                                         get_by_volume_and_instance,
                                         swap_volume,
                                         disconnect_volume,
                                         volume_save,
                                         source_type):
        conn = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        instance = objects.Instance(**self.test_instance)
        old_connection_info = {'driver_volume_type': 'fake',
                               'serial': 'old-volume-id',
                               'data': {'device_path': '/fake-old-volume',
                                        'access_mode': 'rw'}}
        new_connection_info = {'driver_volume_type': 'fake',
                               'serial': 'new-volume-id',
                               'data': {'device_path': '/fake-new-volume',
                                        'access_mode': 'rw'}}
        mock_dom = mock.MagicMock()
        guest = libvirt_guest.Guest(mock_dom)
        mock_dom.XMLDesc.return_value = """<domain>
          <devices>
            <disk type='file'>
                <source file='/fake-old-volume'/>
                <target dev='vdb' bus='virtio'/>
            </disk>
          </devices>
        </domain>
        """
        mock_dom.name.return_value = 'inst'
        mock_dom.UUIDString.return_value = 'uuid'
        get_guest.return_value = guest
        disk_info = {'bus': 'virtio', 'type': 'disk', 'dev': 'vdb'}
        get_volume_config.return_value = mock.MagicMock(
            source_path='/fake-new-volume')

        bdm = objects.BlockDeviceMapping(self.context,
            **fake_block_device.FakeDbBlockDeviceDict(
                {'id': 2, 'instance_uuid': uuids.instance,
                 'device_name': '/dev/vdb',
                 'source_type': source_type,
                 'destination_type': 'volume',
                 'volume_id': 'fake-volume-id-2',
                 'boot_index': 0}))
        get_by_volume_and_instance.return_value = bdm

        conn.swap_volume(old_connection_info, new_connection_info, instance,
                         '/dev/vdb', 1)

        get_guest.assert_called_once_with(instance)
        connect_volume.assert_called_once_with(new_connection_info, disk_info)

        swap_volume.assert_called_once_with(guest, 'vdb',
                                            '/fake-new-volume', 1)
        disconnect_volume.assert_called_once_with(old_connection_info, 'vdb')
        volume_save.assert_called_once_with()

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.save')
    def test_swap_volume_driver_bdm_save_source_is_volume(self, volume_save):
        self._test_swap_volume_driver_bdm_save(volume_save=volume_save,
                                          source_type='volume')

    @mock.patch('nova.virt.block_device.DriverImageBlockDevice.save')
    def test_swap_volume_driver_bdm_save_source_is_image(self, volume_save):
        self._test_swap_volume_driver_bdm_save(volume_save=volume_save,
                                          source_type='image')

    @mock.patch('nova.virt.block_device.DriverSnapshotBlockDevice.save')
    def test_swap_volume_driver_bdm_save_source_is_snapshot(self, volume_save):
        self._test_swap_volume_driver_bdm_save(volume_save=volume_save,
                                          source_type='snapshot')

    def _test_live_snapshot(self, can_quiesce=False, require_quiesce=False):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI())
        mock_dom = mock.MagicMock()
        test_image_meta = self.test_image_meta.copy()
        if require_quiesce:
            test_image_meta = {'properties': {'os_require_quiesce': 'yes'}}

        with test.nested(
                mock.patch.object(drvr._conn, 'defineXML', create=True),
                mock.patch.object(fake_libvirt_utils, 'get_disk_size'),
                mock.patch.object(fake_libvirt_utils, 'get_disk_backing_file'),
                mock.patch.object(fake_libvirt_utils, 'create_cow_image'),
                mock.patch.object(fake_libvirt_utils, 'chown'),
                mock.patch.object(fake_libvirt_utils, 'extract_snapshot'),
                mock.patch.object(drvr, '_set_quiesced')
        ) as (mock_define, mock_size, mock_backing, mock_create_cow,
              mock_chown, mock_snapshot, mock_quiesce):

            xmldoc = "<domain/>"
            srcfile = "/first/path"
            dstfile = "/second/path"
            bckfile = "/other/path"
            dltfile = dstfile + ".delta"

            mock_dom.XMLDesc.return_value = xmldoc
            mock_dom.isPersistent.return_value = True
            mock_size.return_value = 1004009
            mock_backing.return_value = bckfile
            guest = libvirt_guest.Guest(mock_dom)

            if not can_quiesce:
                mock_quiesce.side_effect = (
                    exception.InstanceQuiesceNotSupported(
                        instance_id=self.test_instance['id'], reason='test'))

            image_meta = objects.ImageMeta.from_dict(test_image_meta)
            drvr._live_snapshot(self.context, self.test_instance, guest,
                                srcfile, dstfile, "qcow2", "qcow2", image_meta)

            mock_dom.XMLDesc.assert_called_once_with(flags=(
                fakelibvirt.VIR_DOMAIN_XML_INACTIVE |
                fakelibvirt.VIR_DOMAIN_XML_SECURE))
            mock_dom.blockRebase.assert_called_once_with(
                srcfile, dltfile, 0, flags=(
                    fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_COPY |
                    fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_REUSE_EXT |
                    fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_SHALLOW))

            mock_size.assert_called_once_with(srcfile, format="qcow2")
            mock_backing.assert_called_once_with(srcfile, basename=False,
                                                 format="qcow2")
            mock_create_cow.assert_called_once_with(bckfile, dltfile, 1004009)
            mock_chown.assert_called_once_with(dltfile, os.getuid())
            mock_snapshot.assert_called_once_with(dltfile, "qcow2",
                                                  dstfile, "qcow2")
            mock_define.assert_called_once_with(xmldoc)
            mock_quiesce.assert_any_call(mock.ANY, self.test_instance,
                                         mock.ANY, True)
            if can_quiesce:
                mock_quiesce.assert_any_call(mock.ANY, self.test_instance,
                                             mock.ANY, False)

    def test_live_snapshot(self):
        self._test_live_snapshot()

    def test_live_snapshot_with_quiesce(self):
        self._test_live_snapshot(can_quiesce=True)

    def test_live_snapshot_with_require_quiesce(self):
        self._test_live_snapshot(can_quiesce=True, require_quiesce=True)

    def test_live_snapshot_with_require_quiesce_fails(self):
        self.assertRaises(exception.InstanceQuiesceNotSupported,
                          self._test_live_snapshot,
                          can_quiesce=False, require_quiesce=True)

    @mock.patch.object(libvirt_driver.LibvirtDriver, "_live_migration")
    def test_live_migration_hostname_valid(self, mock_lm):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.live_migration(self.context, self.test_instance,
                            "host1.example.com",
                            lambda x: x,
                            lambda x: x)
        self.assertEqual(1, mock_lm.call_count)

    @mock.patch.object(libvirt_driver.LibvirtDriver, "_live_migration")
    @mock.patch.object(fake_libvirt_utils, "is_valid_hostname")
    def test_live_migration_hostname_invalid(self, mock_hostname, mock_lm):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_hostname.return_value = False
        self.assertRaises(exception.InvalidHostname,
                          drvr.live_migration,
                          self.context, self.test_instance,
                          "foo/?com=/bin/sh",
                          lambda x: x,
                          lambda x: x)

    def test_live_migration_force_complete(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = fake_instance.fake_instance_obj(
            None, name='instancename', id=1,
            uuid='c83a75d4-4d53-4be5-9a40-04d9c0389ff8')
        drvr.active_migrations[instance.uuid] = deque()
        drvr.live_migration_force_complete(instance)
        self.assertEqual(
            1, drvr.active_migrations[instance.uuid].count("force-complete"))

    @mock.patch.object(host.Host, "get_connection")
    @mock.patch.object(fakelibvirt.virDomain, "abortJob")
    def test_live_migration_abort(self, mock_abort, mock_conn):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        dom = fakelibvirt.Domain(drvr._get_connection(), "<domain/>", False)
        guest = libvirt_guest.Guest(dom)
        with mock.patch.object(nova.virt.libvirt.host.Host, 'get_guest',
                               return_value=guest):
            drvr.live_migration_abort(self.test_instance)
            self.assertTrue(mock_abort.called)

    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('tempfile.mkstemp')
    @mock.patch('os.close', return_value=None)
    def test_check_instance_shared_storage_local_raw(self,
                                                 mock_close,
                                                 mock_mkstemp,
                                                 mock_exists):
        instance_uuid = str(uuid.uuid4())
        self.flags(images_type='raw', group='libvirt')
        self.flags(instances_path='/tmp')
        mock_mkstemp.return_value = (-1,
                                     '/tmp/{0}/file'.format(instance_uuid))
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        temp_file = driver.check_instance_shared_storage_local(self.context,
                                                               instance)
        self.assertEqual('/tmp/{0}/file'.format(instance_uuid),
                         temp_file['filename'])

    def test_check_instance_shared_storage_local_rbd(self):
        self.flags(images_type='rbd', group='libvirt')
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(**self.test_instance)
        self.assertIsNone(driver.
                          check_instance_shared_storage_local(self.context,
                                                              instance))

    def test_version_to_string(self):
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        string_ver = driver._version_to_string((4, 33, 173))
        self.assertEqual("4.33.173", string_ver)

    def test_parallels_min_version_fail(self):
        self.flags(virt_type='parallels', group='libvirt')
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with mock.patch.object(driver._conn, 'getLibVersion',
                               return_value=1002011):
            self.assertRaises(exception.NovaException,
                              driver.init_host, 'wibble')

    def test_parallels_min_version_ok(self):
        self.flags(virt_type='parallels', group='libvirt')
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with mock.patch.object(driver._conn, 'getLibVersion',
                               return_value=1002012):
            driver.init_host('wibble')

    def test_get_guest_config_parallels_vm(self):
        self.flags(virt_type='parallels', group='libvirt')
        self.flags(images_type='ploop', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance_ref = objects.Instance(**self.test_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta)

        cfg = drvr._get_guest_config(instance_ref,
                                    _fake_network_info(self, 1),
                                    image_meta, disk_info)
        self.assertEqual("parallels", cfg.virt_type)
        self.assertEqual(instance_ref["uuid"], cfg.uuid)
        self.assertEqual(instance_ref.flavor.memory_mb * units.Ki, cfg.memory)
        self.assertEqual(instance_ref.flavor.vcpus, cfg.vcpus)
        self.assertEqual(vm_mode.HVM, cfg.os_type)
        self.assertIsNone(cfg.os_root)
        self.assertEqual(6, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertEqual(cfg.devices[0].driver_format, "ploop")
        self.assertIsInstance(cfg.devices[1],
                              vconfig.LibvirtConfigGuestDisk)
        self.assertIsInstance(cfg.devices[2],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[3],
                              vconfig.LibvirtConfigGuestInput)
        self.assertIsInstance(cfg.devices[4],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[5],
                              vconfig.LibvirtConfigGuestVideo)

    def test_get_guest_config_parallels_ct_rescue(self):
        self._test_get_guest_config_parallels_ct(rescue=True)

    def test_get_guest_config_parallels_ct(self):
        self._test_get_guest_config_parallels_ct(rescue=False)

    def _test_get_guest_config_parallels_ct(self, rescue=False):
        self.flags(virt_type='parallels', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        ct_instance = self.test_instance.copy()
        ct_instance["vm_mode"] = vm_mode.EXE
        instance_ref = objects.Instance(**ct_instance)
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        if rescue:
            rescue_data = ct_instance
        else:
            rescue_data = None

        cfg = drvr._get_guest_config(instance_ref,
                                    _fake_network_info(self, 1),
                                    image_meta, {'mapping': {'disk': {}}},
                                    rescue_data)
        self.assertEqual("parallels", cfg.virt_type)
        self.assertEqual(instance_ref["uuid"], cfg.uuid)
        self.assertEqual(instance_ref.flavor.memory_mb * units.Ki, cfg.memory)
        self.assertEqual(instance_ref.flavor.vcpus, cfg.vcpus)
        self.assertEqual(vm_mode.EXE, cfg.os_type)
        self.assertEqual("/sbin/init", cfg.os_init_path)
        self.assertIsNone(cfg.os_root)
        if rescue:
            self.assertEqual(5, len(cfg.devices))
        else:
            self.assertEqual(4, len(cfg.devices))
        self.assertIsInstance(cfg.devices[0],
                              vconfig.LibvirtConfigGuestFilesys)

        device_index = 0
        fs = cfg.devices[device_index]
        self.assertEqual(fs.source_type, "file")
        self.assertEqual(fs.driver_type, "ploop")
        self.assertEqual(fs.target_dir, "/")

        if rescue:
            device_index = 1
            fs = cfg.devices[device_index]
            self.assertEqual(fs.source_type, "file")
            self.assertEqual(fs.driver_type, "ploop")
            self.assertEqual(fs.target_dir, "/mnt/rescue")

        self.assertIsInstance(cfg.devices[device_index + 1],
                              vconfig.LibvirtConfigGuestInterface)
        self.assertIsInstance(cfg.devices[device_index + 2],
                              vconfig.LibvirtConfigGuestGraphics)
        self.assertIsInstance(cfg.devices[device_index + 3],
                              vconfig.LibvirtConfigGuestVideo)

    def _test_get_guest_config_parallels_volume(self, vmmode, devices):
        self.flags(virt_type='parallels', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        ct_instance = self.test_instance.copy()
        ct_instance["vm_mode"] = vmmode
        instance_ref = objects.Instance(**ct_instance)

        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)
        conn_info = {'driver_volume_type': 'fake'}
        bdm = objects.BlockDeviceMapping(
            self.context,
            **fake_block_device.FakeDbBlockDeviceDict(
                {'id': 0,
                 'source_type': 'volume', 'destination_type': 'volume',
                 'device_name': '/dev/sda'}))
        info = {'block_device_mapping': driver_block_device.convert_volumes(
                [bdm])}
        info['block_device_mapping'][0]['connection_info'] = conn_info

        disk_info = blockinfo.get_disk_info(CONF.libvirt.virt_type,
                                            instance_ref,
                                            image_meta,
                                            info)

        with mock.patch.object(
                        driver_block_device.DriverVolumeBlockDevice, 'save'
                                ) as mock_save:
            cfg = drvr._get_guest_config(instance_ref,
                                    _fake_network_info(self, 1),
                                    image_meta, disk_info, None, info)
            mock_save.assert_called_once_with()

        self.assertEqual("parallels", cfg.virt_type)
        self.assertEqual(instance_ref["uuid"], cfg.uuid)
        self.assertEqual(instance_ref.flavor.memory_mb * units.Ki, cfg.memory)
        self.assertEqual(instance_ref.flavor.vcpus, cfg.vcpus)
        self.assertEqual(vmmode, cfg.os_type)
        self.assertIsNone(cfg.os_root)
        self.assertEqual(devices, len(cfg.devices))

        disk_found = False

        for dev in cfg.devices:
            result = isinstance(dev, vconfig.LibvirtConfigGuestFilesys)
            self.assertFalse(result)
            if (isinstance(dev, vconfig.LibvirtConfigGuestDisk) and
                (dev.source_path is None or
               'disk.local' not in dev.source_path)):
                self.assertEqual("disk", dev.source_device)
                self.assertEqual("sda", dev.target_dev)
                disk_found = True

        self.assertTrue(disk_found)

    def test_get_guest_config_parallels_volume(self):
        self._test_get_guest_config_parallels_volume(vm_mode.EXE, 4)
        self._test_get_guest_config_parallels_volume(vm_mode.HVM, 6)

    def test_get_guest_disk_config_rbd_older_config_drive_fall_back(self):
        # New config drives are stored in rbd but existing instances have
        # config drives in the old location under the instances path.
        # Test that the driver falls back to 'flat' for config drive if it
        # doesn't exist in rbd.
        self.flags(images_type='rbd', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        mock_rbd_image = mock.Mock()
        mock_flat_image = mock.Mock()
        mock_flat_image.libvirt_info.return_value = mock.sentinel.diskconfig
        drvr.image_backend.image.side_effect = [mock_rbd_image,
                                                mock_flat_image]
        mock_rbd_image.exists.return_value = False
        instance = objects.Instance()
        disk_mapping = {'disk.config': {'bus': 'ide',
                                        'dev': 'hdd',
                                        'type': 'file'}}
        flavor = objects.Flavor(extra_specs={})

        diskconfig = drvr._get_guest_disk_config(
            instance, 'disk.config', disk_mapping, flavor,
            drvr._get_disk_config_image_type())

        self.assertEqual(2, drvr.image_backend.image.call_count)
        call1 = mock.call(instance, 'disk.config', 'rbd')
        call2 = mock.call(instance, 'disk.config', 'flat')
        drvr.image_backend.image.assert_has_calls([call1, call2])
        self.assertEqual(mock.sentinel.diskconfig, diskconfig)

    def _test_prepare_domain_for_snapshot(self, live_snapshot, state):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance_ref = objects.Instance(**self.test_instance)
        with mock.patch.object(drvr, "suspend") as mock_suspend:
            drvr._prepare_domain_for_snapshot(
                self.context, live_snapshot, state, instance_ref)
            return mock_suspend.called

    def test_prepare_domain_for_snapshot(self):
        # Ensure that suspend() is only called on RUNNING or PAUSED instances
        for test_power_state in power_state.STATE_MAP.keys():
            if test_power_state in (power_state.RUNNING, power_state.PAUSED):
                self.assertTrue(self._test_prepare_domain_for_snapshot(
                    False, test_power_state))
            else:
                self.assertFalse(self._test_prepare_domain_for_snapshot(
                    False, test_power_state))

    def test_prepare_domain_for_snapshot_lxc(self):
        self.flags(virt_type='lxc', group='libvirt')
        # Ensure that suspend() is never called with LXC
        for test_power_state in power_state.STATE_MAP.keys():
            self.assertFalse(self._test_prepare_domain_for_snapshot(
                False, test_power_state))

    def test_prepare_domain_for_snapshot_live_snapshots(self):
        # Ensure that suspend() is never called for live snapshots
        for test_power_state in power_state.STATE_MAP.keys():
            self.assertFalse(self._test_prepare_domain_for_snapshot(
                True, test_power_state))

    @mock.patch('os.walk')
    @mock.patch('os.path.exists')
    @mock.patch('os.path.getsize')
    @mock.patch('os.path.isdir')
    @mock.patch('nova.utils.execute')
    @mock.patch.object(host.Host, "get_domain")
    def test_get_instance_disk_info_parallels_ct(self, mock_get_domain,
                                                 mock_execute,
                                                 mock_isdir,
                                                 mock_getsize,
                                                 mock_exists,
                                                 mock_walk):

        dummyxml = ("<domain type='parallels'><name>instance-0000000a</name>"
                    "<os><type>exe</type></os>"
                    "<devices>"
                    "<filesystem type='file'>"
                    "<driver format='ploop' type='ploop'/>"
                    "<source file='/test/disk'/>"
                    "<target dir='/'/></filesystem>"
                    "</devices></domain>")

        ret = ("image: /test/disk/root.hds\n"
               "file format: parallels\n"
               "virtual size: 20G (21474836480 bytes)\n"
               "disk size: 789M\n")

        self.flags(virt_type='parallels', group='libvirt')
        instance = objects.Instance(**self.test_instance)
        instance.vm_mode = vm_mode.EXE
        fake_dom = FakeVirtDomain(fake_xml=dummyxml)
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        mock_get_domain.return_value = fake_dom
        mock_walk.return_value = [('/test/disk', [],
                                   ['DiskDescriptor.xml', 'root.hds'])]

        def getsize_sideeffect(*args, **kwargs):
            if args[0] == '/test/disk/DiskDescriptor.xml':
                return 790
            if args[0] == '/test/disk/root.hds':
                return 827326464

        mock_getsize.side_effect = getsize_sideeffect
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_execute.return_value = (ret, '')

        info = drvr.get_instance_disk_info(instance)
        info = jsonutils.loads(info)
        self.assertEqual(info[0]['type'], 'ploop')
        self.assertEqual(info[0]['path'], '/test/disk')
        self.assertEqual(info[0]['disk_size'], 827327254)
        self.assertEqual(info[0]['over_committed_disk_size'], 20647509226)
        self.assertEqual(info[0]['virt_disk_size'], 21474836480)


class HostStateTestCase(test.NoDBTestCase):

    cpu_info = {"vendor": "Intel", "model": "pentium", "arch": "i686",
                 "features": ["ssse3", "monitor", "pni", "sse2", "sse",
                 "fxsr", "clflush", "pse36", "pat", "cmov", "mca", "pge",
                 "mtrr", "sep", "apic"],
                 "topology": {"cores": "1", "threads": "1", "sockets": "1"}}
    instance_caps = [(arch.X86_64, "kvm", "hvm"),
                     (arch.I686, "kvm", "hvm")]
    pci_devices = [{
        "dev_id": "pci_0000_04_00_3",
        "address": "0000:04:10.3",
        "product_id": '1521',
        "vendor_id": '8086',
        "dev_type": fields.PciDeviceType.SRIOV_PF,
        "phys_function": None}]
    numa_topology = objects.NUMATopology(
                        cells=[objects.NUMACell(
                            id=1, cpuset=set([1, 2]), memory=1024,
                            cpu_usage=0, memory_usage=0,
                            mempages=[], siblings=[],
                            pinned_cpus=set([])),
                               objects.NUMACell(
                            id=2, cpuset=set([3, 4]), memory=1024,
                            cpu_usage=0, memory_usage=0,
                            mempages=[], siblings=[],
                            pinned_cpus=set([]))])

    class FakeConnection(libvirt_driver.LibvirtDriver):
        """Fake connection object."""
        def __init__(self):
            super(HostStateTestCase.FakeConnection,
                  self).__init__(fake.FakeVirtAPI(), True)

            self._host = host.Host("qemu:///system")

            def _get_memory_mb_total():
                return 497

            def _get_memory_mb_used():
                return 88

            self._host.get_memory_mb_total = _get_memory_mb_total
            self._host.get_memory_mb_used = _get_memory_mb_used

        def _get_vcpu_total(self):
            return 1

        def _get_vcpu_used(self):
            return 0

        def _get_cpu_info(self):
            return HostStateTestCase.cpu_info

        def _get_disk_over_committed_size_total(self):
            return 0

        def _get_local_gb_info(self):
            return {'total': 100, 'used': 20, 'free': 80}

        def get_host_uptime(self):
            return ('10:01:16 up  1:36,  6 users,  '
                    'load average: 0.21, 0.16, 0.19')

        def _get_disk_available_least(self):
            return 13091

        def _get_instance_capabilities(self):
            return HostStateTestCase.instance_caps

        def _get_pci_passthrough_devices(self):
            return jsonutils.dumps(HostStateTestCase.pci_devices)

        def _get_host_numa_topology(self):
            return HostStateTestCase.numa_topology

    @mock.patch.object(fakelibvirt, "openAuth")
    def test_update_status(self, mock_open):
        mock_open.return_value = fakelibvirt.Connection("qemu:///system")

        drvr = HostStateTestCase.FakeConnection()

        stats = drvr.get_available_resource("compute1")
        self.assertEqual(stats["vcpus"], 1)
        self.assertEqual(stats["memory_mb"], 497)
        self.assertEqual(stats["local_gb"], 100)
        self.assertEqual(stats["vcpus_used"], 0)
        self.assertEqual(stats["memory_mb_used"], 88)
        self.assertEqual(stats["local_gb_used"], 20)
        self.assertEqual(stats["hypervisor_type"], 'QEMU')
        self.assertEqual(stats["hypervisor_version"],
                         fakelibvirt.FAKE_QEMU_VERSION)
        self.assertEqual(stats["hypervisor_hostname"], 'compute1')
        cpu_info = jsonutils.loads(stats["cpu_info"])
        self.assertEqual(cpu_info,
                {"vendor": "Intel", "model": "pentium",
                 "arch": arch.I686,
                 "features": ["ssse3", "monitor", "pni", "sse2", "sse",
                              "fxsr", "clflush", "pse36", "pat", "cmov",
                              "mca", "pge", "mtrr", "sep", "apic"],
                 "topology": {"cores": "1", "threads": "1", "sockets": "1"}
                })
        self.assertEqual(stats["disk_available_least"], 80)
        self.assertEqual(jsonutils.loads(stats["pci_passthrough_devices"]),
                         HostStateTestCase.pci_devices)
        self.assertThat(objects.NUMATopology.obj_from_db_obj(
                            stats['numa_topology'])._to_dict(),
                        matchers.DictMatches(
                                HostStateTestCase.numa_topology._to_dict()))


class LibvirtDriverTestCase(test.NoDBTestCase):
    """Test for nova.virt.libvirt.libvirt_driver.LibvirtDriver."""
    def setUp(self):
        super(LibvirtDriverTestCase, self).setUp()
        self.drvr = libvirt_driver.LibvirtDriver(
            fake.FakeVirtAPI(), read_only=True)
        self.context = context.get_admin_context()
        self.test_image_meta = {
            "disk_format": "raw",
        }

    def _create_instance(self, params=None):
        """Create a test instance."""
        if not params:
            params = {}

        flavor = objects.Flavor(memory_mb=512,
                                swap=0,
                                vcpu_weight=None,
                                root_gb=10,
                                id=2,
                                name=u'm1.tiny',
                                ephemeral_gb=20,
                                rxtx_factor=1.0,
                                flavorid=u'1',
                                vcpus=1)
        flavor.update(params.pop('flavor', {}))

        inst = {}
        inst['id'] = 1
        inst['uuid'] = '52d3b512-1152-431f-a8f7-28f0288a622b'
        inst['os_type'] = 'linux'
        inst['image_ref'] = uuids.fake_image_ref
        inst['reservation_id'] = 'r-fakeres'
        inst['user_id'] = 'fake'
        inst['project_id'] = 'fake'
        inst['instance_type_id'] = 2
        inst['ami_launch_index'] = 0
        inst['host'] = 'host1'
        inst['root_gb'] = flavor.root_gb
        inst['ephemeral_gb'] = flavor.ephemeral_gb
        inst['config_drive'] = True
        inst['kernel_id'] = 2
        inst['ramdisk_id'] = 3
        inst['key_data'] = 'ABCDEFG'
        inst['system_metadata'] = {}
        inst['metadata'] = {}
        inst['task_state'] = None

        inst.update(params)

        return fake_instance.fake_instance_obj(
            self.context, expected_attrs=['metadata', 'system_metadata'],
            flavor=flavor, **inst)

    def test_migrate_disk_and_power_off_exception(self):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off.
        """

        self.counter = 0
        self.checked_shared_storage = False

        def fake_get_instance_disk_info(instance,
                                        block_device_info=None):
            return '[]'

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            self.counter += 1
            if self.counter == 1:
                assert False, "intentional failure"

        def fake_os_path_exists(path):
            return True

        def fake_is_storage_shared(dest, inst_base):
            self.checked_shared_storage = True
            return False

        self.stubs.Set(self.drvr, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.drvr, '_destroy', fake_destroy)
        self.stubs.Set(self.drvr, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(self.drvr, '_is_storage_shared_with',
                       fake_is_storage_shared)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stub_out('os.path.exists', fake_os_path_exists)

        ins_ref = self._create_instance()
        flavor = {'root_gb': 10, 'ephemeral_gb': 20}
        flavor_obj = objects.Flavor(**flavor)

        self.assertRaises(AssertionError,
                          self.drvr.migrate_disk_and_power_off,
                          context.get_admin_context(), ins_ref, '10.0.0.2',
                          flavor_obj, None)

    def _test_migrate_disk_and_power_off(self, flavor_obj,
                                         block_device_info=None,
                                         params_for_instance=None):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off.
        """

        instance = self._create_instance(params=params_for_instance)
        disk_info = fake_disk_info_json(instance)

        def fake_get_instance_disk_info(instance,
                                        block_device_info=None):
            return disk_info

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        def fake_execute(*args, **kwargs):
            pass

        def fake_copy_image(src, dest, host=None, receive=False,
                            on_execute=None, on_completion=None,
                            compression=True):
            self.assertIsNotNone(on_execute)
            self.assertIsNotNone(on_completion)

        self.stubs.Set(self.drvr, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.drvr, '_destroy', fake_destroy)
        self.stubs.Set(self.drvr, 'get_host_ip_addr',
                       fake_get_host_ip_addr)
        self.stubs.Set(utils, 'execute', fake_execute)
        self.stubs.Set(libvirt_utils, 'copy_image', fake_copy_image)

        # dest is different host case
        out = self.drvr.migrate_disk_and_power_off(
               context.get_admin_context(), instance, '10.0.0.2',
               flavor_obj, None, block_device_info=block_device_info)
        self.assertEqual(out, disk_info)

        # dest is same host case
        out = self.drvr.migrate_disk_and_power_off(
               context.get_admin_context(), instance, '10.0.0.1',
               flavor_obj, None, block_device_info=block_device_info)
        self.assertEqual(out, disk_info)

    def test_migrate_disk_and_power_off(self):
        flavor = {'root_gb': 10, 'ephemeral_gb': 20}
        flavor_obj = objects.Flavor(**flavor)

        self._test_migrate_disk_and_power_off(flavor_obj)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._disconnect_volume')
    def test_migrate_disk_and_power_off_boot_from_volume(self,
                                                         disconnect_volume):
        info = {'block_device_mapping': [{'boot_index': None,
                                          'mount_device': '/dev/vdd',
                                          'connection_info': None},
                                         {'boot_index': 0,
                                          'mount_device': '/dev/vda',
                                          'connection_info': None}]}
        flavor = {'root_gb': 1, 'ephemeral_gb': 0}
        flavor_obj = objects.Flavor(**flavor)
        # Note(Mike_D): The size of instance's ephemeral_gb is 0 gb.
        self._test_migrate_disk_and_power_off(
            flavor_obj, block_device_info=info,
            params_for_instance={'image_ref': None,
                                 'flavor': {'root_gb': 1,
                                            'ephemeral_gb': 0}})
        disconnect_volume.assert_called_with(
            info['block_device_mapping'][1]['connection_info'], 'vda')

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.virt.libvirt.utils.copy_image')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._destroy')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.get_host_ip_addr')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    def test_migrate_disk_and_power_off_swap(self, mock_get_disk_info,
                                             get_host_ip_addr,
                                             mock_destroy,
                                             mock_copy_image,
                                             mock_execute):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .migrate_disk_and_power_off.
        """
        self.copy_or_move_swap_called = False

        # Original instance config
        instance = self._create_instance({'flavor': {'root_gb': 10,
                                                     'ephemeral_gb': 0}})

        disk_info = fake_disk_info_json(instance)
        mock_get_disk_info.return_value = disk_info
        get_host_ip_addr.return_value = '10.0.0.1'

        def fake_copy_image(*args, **kwargs):
            # disk.swap should not be touched since it is skipped over
            if '/test/disk.swap' in list(args):
                self.copy_or_move_swap_called = True

        def fake_execute(*args, **kwargs):
            # disk.swap should not be touched since it is skipped over
            if set(['mv', '/test/disk.swap']).issubset(list(args)):
                self.copy_or_move_swap_called = True

        mock_copy_image.side_effect = fake_copy_image
        mock_execute.side_effect = fake_execute

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        # Re-size fake instance to 20G root and 1024M swap disk
        flavor = {'root_gb': 20, 'ephemeral_gb': 0, 'swap': 1024}
        flavor_obj = objects.Flavor(**flavor)

        # Destination is same host
        out = drvr.migrate_disk_and_power_off(context.get_admin_context(),
                                              instance, '10.0.0.1',
                                              flavor_obj, None)

        mock_get_disk_info.assert_called_once_with(instance,
                                                   block_device_info=None)
        self.assertTrue(get_host_ip_addr.called)
        mock_destroy.assert_called_once_with(instance)
        self.assertFalse(self.copy_or_move_swap_called)
        self.assertEqual(disk_info, out)

    def _test_migrate_disk_and_power_off_resize_check(self, expected_exc):
        """Test for nova.virt.libvirt.libvirt_driver.LibvirtConnection
        .migrate_disk_and_power_off.
        """
        instance = self._create_instance()
        disk_info = fake_disk_info_json(instance)

        def fake_get_instance_disk_info(instance, xml=None,
                                        block_device_info=None):
            return disk_info

        def fake_destroy(instance):
            pass

        def fake_get_host_ip_addr():
            return '10.0.0.1'

        self.stubs.Set(self.drvr, 'get_instance_disk_info',
                       fake_get_instance_disk_info)
        self.stubs.Set(self.drvr, '_destroy', fake_destroy)
        self.stubs.Set(self.drvr, 'get_host_ip_addr',
                       fake_get_host_ip_addr)

        flavor = {'root_gb': 10, 'ephemeral_gb': 20}
        flavor_obj = objects.Flavor(**flavor)

        # Migration is not implemented for LVM backed instances
        self.assertRaises(expected_exc,
              self.drvr.migrate_disk_and_power_off,
              None, instance, '10.0.0.1', flavor_obj, None)

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._destroy')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '._is_storage_shared_with')
    def _test_migrate_disk_and_power_off_backing_file(self,
                                                      shared_storage,
                                                      mock_is_shared_storage,
                                                      mock_get_disk_info,
                                                      mock_destroy,
                                                      mock_execute):
        self.convert_file_called = False
        flavor = {'root_gb': 20, 'ephemeral_gb': 30, 'swap': 0}
        flavor_obj = objects.Flavor(**flavor)
        disk_info = [{'type': 'qcow2', 'path': '/test/disk',
                      'virt_disk_size': '10737418240',
                      'backing_file': '/base/disk',
                      'disk_size': '83886080'}]
        disk_info_text = jsonutils.dumps(disk_info)
        mock_get_disk_info.return_value = disk_info_text
        mock_is_shared_storage.return_value = shared_storage

        def fake_execute(*args, **kwargs):
            self.assertNotEqual(args[0:2], ['qemu-img', 'convert'])

        mock_execute.side_effect = fake_execute

        instance = self._create_instance()

        out = self.drvr.migrate_disk_and_power_off(
               context.get_admin_context(), instance, '10.0.0.2',
               flavor_obj, None)

        self.assertTrue(mock_is_shared_storage.called)
        mock_destroy.assert_called_once_with(instance)
        self.assertEqual(out, disk_info_text)

    def test_migrate_disk_and_power_off_shared_storage(self):
        self._test_migrate_disk_and_power_off_backing_file(True)

    def test_migrate_disk_and_power_off_non_shared_storage(self):
        self._test_migrate_disk_and_power_off_backing_file(False)

    def test_migrate_disk_and_power_off_lvm(self):
        self.flags(images_type='lvm', group='libvirt')

        def fake_execute(*args, **kwargs):
            pass

        self.stubs.Set(utils, 'execute', fake_execute)

        expected_exc = exception.InstanceFaultRollback
        self._test_migrate_disk_and_power_off_resize_check(expected_exc)

    def test_migrate_disk_and_power_off_resize_cannot_ssh(self):
        def fake_execute(*args, **kwargs):
            raise processutils.ProcessExecutionError()

        def fake_is_storage_shared(dest, inst_base):
            self.checked_shared_storage = True
            return False

        self.stubs.Set(self.drvr, '_is_storage_shared_with',
                       fake_is_storage_shared)
        self.stubs.Set(utils, 'execute', fake_execute)

        expected_exc = exception.InstanceFaultRollback
        self._test_migrate_disk_and_power_off_resize_check(expected_exc)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    def test_migrate_disk_and_power_off_resize_error(self, mock_get_disk_info):
        instance = self._create_instance()
        flavor = {'root_gb': 5, 'ephemeral_gb': 10}
        flavor_obj = objects.Flavor(**flavor)
        mock_get_disk_info.return_value = fake_disk_info_json(instance)

        self.assertRaises(
            exception.InstanceFaultRollback,
            self.drvr.migrate_disk_and_power_off,
            'ctx', instance, '10.0.0.1', flavor_obj, None)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    def test_migrate_disk_and_power_off_resize_error_default_ephemeral(
            self, mock_get_disk_info):
        # Note(Mike_D): The size of this instance's ephemeral_gb is 20 gb.
        instance = self._create_instance()
        flavor = {'root_gb': 10, 'ephemeral_gb': 0}
        flavor_obj = objects.Flavor(**flavor)
        mock_get_disk_info.return_value = fake_disk_info_json(instance)

        self.assertRaises(exception.InstanceFaultRollback,
                          self.drvr.migrate_disk_and_power_off,
                          'ctx', instance, '10.0.0.1', flavor_obj, None)

    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    @mock.patch('nova.virt.driver.block_device_info_get_ephemerals')
    def test_migrate_disk_and_power_off_resize_error_eph(self, mock_get,
                                                         mock_get_disk_info):
        mappings = [
            {
                 'device_name': '/dev/sdb4',
                 'source_type': 'blank',
                 'destination_type': 'local',
                 'device_type': 'disk',
                 'guest_format': 'swap',
                 'boot_index': -1,
                 'volume_size': 1
             },
             {
                 'device_name': '/dev/sda1',
                 'source_type': 'volume',
                 'destination_type': 'volume',
                 'device_type': 'disk',
                 'volume_id': 1,
                 'guest_format': None,
                 'boot_index': 1,
                 'volume_size': 6
             },
             {
                 'device_name': '/dev/sda2',
                 'source_type': 'snapshot',
                 'destination_type': 'volume',
                 'snapshot_id': 1,
                 'device_type': 'disk',
                 'guest_format': None,
                 'boot_index': 0,
                 'volume_size': 4
             },
             {
                 'device_name': '/dev/sda3',
                 'source_type': 'blank',
                 'destination_type': 'local',
                 'device_type': 'disk',
                 'guest_format': None,
                 'boot_index': -1,
                 'volume_size': 3
             }
        ]
        mock_get.return_value = mappings
        instance = self._create_instance()

        # Old flavor, eph is 20, real disk is 3, target is 2, fail
        flavor = {'root_gb': 10, 'ephemeral_gb': 2}
        flavor_obj = objects.Flavor(**flavor)
        mock_get_disk_info.return_value = fake_disk_info_json(instance)

        self.assertRaises(
            exception.InstanceFaultRollback,
            self.drvr.migrate_disk_and_power_off,
            'ctx', instance, '10.0.0.1', flavor_obj, None)

        # Old flavor, eph is 20, real disk is 3, target is 4
        flavor = {'root_gb': 10, 'ephemeral_gb': 4}
        flavor_obj = objects.Flavor(**flavor)
        self._test_migrate_disk_and_power_off(flavor_obj)

    @mock.patch('nova.utils.execute')
    @mock.patch('nova.virt.libvirt.utils.copy_image')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._destroy')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '._is_storage_shared_with')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver'
                '.get_instance_disk_info')
    def test_migrate_disk_and_power_off_resize_copy_disk_info(self,
                                                              mock_disk_info,
                                                              mock_shared,
                                                              mock_path,
                                                              mock_destroy,
                                                              mock_copy,
                                                              mock_execuate):

        instance = self._create_instance()
        disk_info = fake_disk_info_json(instance)
        disk_info_text = jsonutils.loads(disk_info)
        instance_base = os.path.dirname(disk_info_text[0]['path'])
        flavor = {'root_gb': 10, 'ephemeral_gb': 25}
        flavor_obj = objects.Flavor(**flavor)

        mock_disk_info.return_value = disk_info
        mock_path.return_value = instance_base
        mock_shared.return_value = False

        src_disk_info_path = os.path.join(instance_base + '_resize',
                                          'disk.info')

        with mock.patch.object(os.path, 'exists', autospec=True) \
                as mock_exists:
            # disk.info exists on the source
            mock_exists.side_effect = \
                lambda path: path == src_disk_info_path
            self.drvr.migrate_disk_and_power_off(context.get_admin_context(),
                                                 instance, mock.sentinel,
                                                 flavor_obj, None)
            self.assertTrue(mock_exists.called)

        dst_disk_info_path = os.path.join(instance_base, 'disk.info')
        mock_copy.assert_any_call(src_disk_info_path, dst_disk_info_path,
                                  host=mock.sentinel, on_execute=mock.ANY,
                                  on_completion=mock.ANY)

    def test_wait_for_running(self):
        def fake_get_info(instance):
            if instance['name'] == "not_found":
                raise exception.InstanceNotFound(instance_id=instance['uuid'])
            elif instance['name'] == "running":
                return hardware.InstanceInfo(state=power_state.RUNNING)
            else:
                return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        self.stubs.Set(self.drvr, 'get_info',
                       fake_get_info)

        # instance not found case
        self.assertRaises(exception.InstanceNotFound,
                self.drvr._wait_for_running,
                    {'name': 'not_found',
                     'uuid': 'not_found_uuid'})

        # instance is running case
        self.assertRaises(loopingcall.LoopingCallDone,
                self.drvr._wait_for_running,
                    {'name': 'running',
                     'uuid': 'running_uuid'})

        # else case
        self.drvr._wait_for_running({'name': 'else',
                                                  'uuid': 'other_uuid'})

    def test_disk_size_from_instance_disk_info(self):
        flavor_data = {'root_gb': 10, 'ephemeral_gb': 20, 'swap_gb': 30}
        inst = objects.Instance(flavor=objects.Flavor(**flavor_data))
        self.assertEqual(10 * units.Gi,
                         self.drvr._disk_size_from_instance(inst, 'disk'))

        self.assertEqual(20 * units.Gi,
                         self.drvr._disk_size_from_instance(inst,
                                                            'disk.local'))

        self.assertEqual(0,
                         self.drvr._disk_size_from_instance(inst, 'disk.swap'))

    @mock.patch('nova.utils.execute')
    def test_disk_raw_to_qcow2(self, mock_execute):
        path = '/test/disk'
        _path_qcow = path + '_qcow'

        self.drvr._disk_raw_to_qcow2(path)
        mock_execute.assert_has_calls([
            mock.call('qemu-img', 'convert', '-f', 'raw',
                      '-O', 'qcow2', path, _path_qcow),
            mock.call('mv', _path_qcow, path)])

    @mock.patch('nova.utils.execute')
    def test_disk_qcow2_to_raw(self, mock_execute):
        path = '/test/disk'
        _path_raw = path + '_raw'

        self.drvr._disk_qcow2_to_raw(path)
        mock_execute.assert_has_calls([
            mock.call('qemu-img', 'convert', '-f', 'qcow2',
                      '-O', 'raw', path, _path_raw),
            mock.call('mv', _path_raw, path)])

    @mock.patch('nova.virt.disk.api.extend')
    def test_disk_resize_raw(self, mock_extend):
        image = imgmodel.LocalFileImage("/test/disk",
                                        imgmodel.FORMAT_RAW)

        self.drvr._disk_resize(image, 50)
        mock_extend.assert_called_once_with(image, 50)

    @mock.patch('nova.virt.disk.api.can_resize_image')
    @mock.patch('nova.virt.disk.api.is_image_extendable')
    @mock.patch('nova.virt.disk.api.extend')
    def test_disk_resize_qcow2(
            self, mock_extend, mock_can_resize, mock_is_image_extendable):

        with test.nested(
                mock.patch.object(
                    self.drvr, '_disk_qcow2_to_raw'),
                mock.patch.object(
                    self.drvr, '_disk_raw_to_qcow2'))\
        as (mock_disk_qcow2_to_raw, mock_disk_raw_to_qcow2):

            mock_can_resize.return_value = True
            mock_is_image_extendable.return_value = True

            imageqcow2 = imgmodel.LocalFileImage("/test/disk",
                                                 imgmodel.FORMAT_QCOW2)
            imageraw = imgmodel.LocalFileImage("/test/disk",
                                               imgmodel.FORMAT_RAW)
            self.drvr._disk_resize(imageqcow2, 50)

            mock_disk_qcow2_to_raw.assert_called_once_with(imageqcow2.path)
            mock_extend.assert_called_once_with(imageraw, 50)
            mock_disk_raw_to_qcow2.assert_called_once_with(imageqcow2.path)

    def _test_finish_migration(self, power_on, resize_instance=False):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_migration.
        """

        powered_on = power_on
        self.fake_create_domain_called = False
        self.fake_disk_resize_called = False
        create_image_called = [False]

        def fake_to_xml(context, instance, network_info, disk_info,
                        image_meta=None, rescue=None,
                        block_device_info=None, write_to_disk=False):
            return ""

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_image(context, inst,
                              disk_mapping, suffix='',
                              disk_images=None, network_info=None,
                              block_device_info=None, inject_files=True,
                              fallback_from_host=None):
            self.assertFalse(inject_files)
            create_image_called[0] = True

        def fake_create_domain_and_network(
            context, xml, instance, network_info, disk_info,
            block_device_info=None, power_on=True, reboot=False,
            vifs_already_plugged=False, post_xml_callback=None):
            self.fake_create_domain_called = True
            self.assertEqual(powered_on, power_on)
            self.assertTrue(vifs_already_plugged)

        def fake_enable_hairpin():
            pass

        def fake_execute(*args, **kwargs):
            pass

        def fake_get_info(instance):
            if powered_on:
                return hardware.InstanceInfo(state=power_state.RUNNING)
            else:
                return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        def fake_disk_resize(image, size):
            # Assert that _create_image is called before disk resize,
            # otherwise we might be trying to resize a disk whose backing
            # file hasn't been fetched, yet.
            self.assertTrue(create_image_called[0])
            self.fake_disk_resize_called = True

        self.flags(use_cow_images=True)
        self.stubs.Set(self.drvr, '_disk_resize',
                       fake_disk_resize)
        self.stubs.Set(self.drvr, '_get_guest_xml', fake_to_xml)
        self.stubs.Set(self.drvr, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(self.drvr, '_create_image',
                       fake_create_image)
        self.stubs.Set(self.drvr, '_create_domain_and_network',
                       fake_create_domain_and_network)
        self.stubs.Set(nova.virt.libvirt.guest.Guest, 'enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.drvr, 'firewall_driver', fw)
        self.stubs.Set(self.drvr, 'get_info',
                       fake_get_info)

        instance = self._create_instance({'config_drive': str(True)})

        migration = objects.Migration()
        migration.source_compute = 'fake-source-compute'
        migration.dest_compute = 'fake-dest-compute'
        migration.source_node = 'fake-source-node'
        migration.dest_node = 'fake-dest-node'
        image_meta = objects.ImageMeta.from_dict(self.test_image_meta)

        # Source disks are raw to test conversion
        disk_info = fake_disk_info_json(instance, type='raw')

        with test.nested(
            mock.patch.object(self.drvr, '_disk_raw_to_qcow2',
                              autospec=True),
            mock.patch.object(self.drvr, '_ensure_console_log_for_instance')
        ) as (mock_raw_to_qcow2, mock_ensure_console_log):
            self.drvr.finish_migration(
                          context.get_admin_context(), migration, instance,
                          disk_info, [], image_meta,
                          resize_instance, None, power_on)

            mock_ensure_console_log.assert_called_once_with(instance)

            # Assert that we converted the root and ephemeral disks
            instance_path = libvirt_utils.get_instance_path(instance)
            convert_calls = [mock.call(os.path.join(instance_path, name))
                             for name in ('disk', 'disk.local')]
            mock_raw_to_qcow2.assert_has_calls(convert_calls, any_order=True)

            # Implicitly assert that we did not convert the config disk
            self.assertEqual(len(convert_calls), mock_raw_to_qcow2.call_count)

        self.assertTrue(self.fake_create_domain_called)
        self.assertEqual(
            resize_instance, self.fake_disk_resize_called)

    def test_finish_migration_resize(self):
        self._test_finish_migration(True, resize_instance=True)

    def test_finish_migration_power_on(self):
        self._test_finish_migration(True)

    def test_finish_migration_power_off(self):
        self._test_finish_migration(False)

    def _test_finish_revert_migration(self, power_on):
        """Test for nova.virt.libvirt.libvirt_driver.LivirtConnection
        .finish_revert_migration.
        """
        powered_on = power_on

        self.fake_create_domain_called = False

        def fake_execute(*args, **kwargs):
            pass

        def fake_plug_vifs(instance, network_info):
            pass

        def fake_create_domain(context, xml, instance, network_info,
                               disk_info, block_device_info=None,
                               power_on=None,
                               vifs_already_plugged=None):
            self.fake_create_domain_called = True
            self.assertEqual(powered_on, power_on)
            self.assertTrue(vifs_already_plugged)
            return mock.MagicMock()

        def fake_enable_hairpin():
            pass

        def fake_get_info(instance):
            if powered_on:
                return hardware.InstanceInfo(state=power_state.RUNNING)
            else:
                return hardware.InstanceInfo(state=power_state.SHUTDOWN)

        def fake_to_xml(context, instance, network_info, disk_info,
                        image_meta=None, rescue=None,
                        block_device_info=None):
            return ""

        self.stubs.Set(self.drvr, '_get_guest_xml', fake_to_xml)
        self.stubs.Set(self.drvr, 'plug_vifs', fake_plug_vifs)
        self.stubs.Set(utils, 'execute', fake_execute)
        fw = base_firewall.NoopFirewallDriver()
        self.stubs.Set(self.drvr, 'firewall_driver', fw)
        self.stubs.Set(self.drvr, '_create_domain_and_network',
                       fake_create_domain)
        self.stubs.Set(nova.virt.libvirt.guest.Guest, 'enable_hairpin',
                       fake_enable_hairpin)
        self.stubs.Set(self.drvr, 'get_info',
                       fake_get_info)
        self.stubs.Set(utils, 'get_image_from_system_metadata',
                       lambda *a: self.test_image_meta)

        with utils.tempdir() as tmpdir:
            self.flags(instances_path=tmpdir)
            ins_ref = self._create_instance()
            os.mkdir(os.path.join(tmpdir, ins_ref['name']))
            libvirt_xml_path = os.path.join(tmpdir,
                                            ins_ref['name'],
                                            'libvirt.xml')
            f = open(libvirt_xml_path, 'w')
            f.close()

            self.drvr.finish_revert_migration(
                                       context.get_admin_context(), ins_ref,
                                       [], None, power_on)
            self.assertTrue(self.fake_create_domain_called)

    def test_finish_revert_migration_power_on(self):
        self._test_finish_revert_migration(True)

    def test_finish_revert_migration_power_off(self):
        self._test_finish_revert_migration(False)

    def _test_finish_revert_migration_after_crash(self, backup_made=True,
                                                  del_inst_failed=False):

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend
        context = 'fake_context'
        ins_ref = self._create_instance()

        with test.nested(
                mock.patch.object(os.path, 'exists', return_value=backup_made),
                mock.patch.object(libvirt_utils, 'get_instance_path'),
                mock.patch.object(utils, 'execute'),
                mock.patch.object(drvr, '_create_domain_and_network'),
                mock.patch.object(drvr, '_get_guest_xml'),
                mock.patch.object(shutil, 'rmtree'),
                mock.patch.object(loopingcall, 'FixedIntervalLoopingCall'),
        ) as (mock_stat, mock_path, mock_exec, mock_cdn, mock_ggx,
              mock_rmtree, mock_looping_call):
            mock_path.return_value = '/fake/foo'
            if del_inst_failed:
                mock_rmtree.side_effect = OSError(errno.ENOENT,
                                                  'test exception')
            drvr.finish_revert_migration(context, ins_ref, [])
            if backup_made:
                mock_exec.assert_called_once_with('mv', '/fake/foo_resize',
                                                  '/fake/foo')
            else:
                self.assertFalse(mock_exec.called)

    def test_finish_revert_migration_after_crash(self):
        self._test_finish_revert_migration_after_crash(backup_made=True)

    def test_finish_revert_migration_after_crash_before_new(self):
        self._test_finish_revert_migration_after_crash(backup_made=True)

    def test_finish_revert_migration_after_crash_before_backup(self):
        self._test_finish_revert_migration_after_crash(backup_made=False)

    def test_finish_revert_migration_after_crash_delete_failed(self):
        self._test_finish_revert_migration_after_crash(backup_made=True,
                                                       del_inst_failed=True)

    def test_finish_revert_migration_preserves_disk_bus(self):

        def fake_get_guest_xml(context, instance, network_info, disk_info,
                               image_meta, block_device_info=None):
            self.assertEqual('ide', disk_info['disk_bus'])

        image_meta = {"disk_format": "raw",
                      "properties": {"hw_disk_bus": "ide"}}
        instance = self._create_instance()

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)

        with test.nested(
                mock.patch.object(drvr, '_create_domain_and_network'),
                mock.patch.object(utils, 'get_image_from_system_metadata',
                                  return_value=image_meta),
                mock.patch.object(drvr, '_get_guest_xml',
                                  side_effect=fake_get_guest_xml)):
            drvr.finish_revert_migration('', instance, None, power_on=False)

    def test_finish_revert_migration_snap_backend(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend
        ins_ref = self._create_instance()

        with test.nested(
                mock.patch.object(utils, 'get_image_from_system_metadata'),
                mock.patch.object(drvr, '_create_domain_and_network'),
                mock.patch.object(drvr, '_get_guest_xml')) as (
                mock_image, mock_cdn, mock_ggx):
            mock_image.return_value = {'disk_format': 'raw'}
            drvr.finish_revert_migration('', ins_ref, None, power_on=False)

            drvr.image_backend.rollback_to_snap.assert_called_once_with(
                    libvirt_utils.RESIZE_SNAPSHOT_NAME)
            drvr.image_backend.remove_snap.assert_called_once_with(
                    libvirt_utils.RESIZE_SNAPSHOT_NAME, ignore_errors=True)

    def test_finish_revert_migration_snap_backend_snapshot_not_found(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend
        ins_ref = self._create_instance()

        with test.nested(
                mock.patch.object(rbd_utils, 'RBDDriver'),
                mock.patch.object(utils, 'get_image_from_system_metadata'),
                mock.patch.object(drvr, '_create_domain_and_network'),
                mock.patch.object(drvr, '_get_guest_xml')) as (
                mock_rbd, mock_image, mock_cdn, mock_ggx):
            mock_image.return_value = {'disk_format': 'raw'}
            mock_rbd.rollback_to_snap.side_effect = exception.SnapshotNotFound(
                    snapshot_id='testing')
            drvr.finish_revert_migration('', ins_ref, None, power_on=False)

            drvr.image_backend.remove_snap.assert_called_once_with(
                    libvirt_utils.RESIZE_SNAPSHOT_NAME, ignore_errors=True)

    def test_finish_revert_migration_snap_backend_image_does_not_exist(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend
        drvr.image_backend.exists.return_value = False
        ins_ref = self._create_instance()

        with test.nested(
                mock.patch.object(rbd_utils, 'RBDDriver'),
                mock.patch.object(utils, 'get_image_from_system_metadata'),
                mock.patch.object(drvr, '_create_domain_and_network'),
                mock.patch.object(drvr, '_get_guest_xml')) as (
                mock_rbd, mock_image, mock_cdn, mock_ggx):
            mock_image.return_value = {'disk_format': 'raw'}
            drvr.finish_revert_migration('', ins_ref, None, power_on=False)
            self.assertFalse(drvr.image_backend.rollback_to_snap.called)
            self.assertFalse(drvr.image_backend.remove_snap.called)

    def test_cleanup_failed_migration(self):
        self.mox.StubOutWithMock(shutil, 'rmtree')
        shutil.rmtree('/fake/inst')
        self.mox.ReplayAll()
        self.drvr._cleanup_failed_migration('/fake/inst')

    def test_confirm_migration(self):
        ins_ref = self._create_instance()

        self.mox.StubOutWithMock(self.drvr, "_cleanup_resize")
        self.drvr._cleanup_resize(ins_ref,
                             _fake_network_info(self, 1))

        self.mox.ReplayAll()
        self.drvr.confirm_migration("migration_ref", ins_ref,
                                            _fake_network_info(self, 1))

    def test_cleanup_resize_same_host(self):
        CONF.set_override('policy_dirs', [], group='oslo_policy')
        ins_ref = self._create_instance({'host': CONF.host})

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend

        with test.nested(
                mock.patch.object(os.path, 'exists'),
                mock.patch.object(libvirt_utils, 'get_instance_path'),
                mock.patch.object(utils, 'execute')) as (
                mock_exists, mock_get_path, mock_exec):
            mock_exists.return_value = True
            mock_get_path.return_value = '/fake/inst'

            drvr._cleanup_resize(ins_ref, _fake_network_info(self, 1))
            mock_get_path.assert_called_once_with(ins_ref)
            mock_exec.assert_called_once_with('rm', '-rf', '/fake/inst_resize',
                                              delay_on_retry=True, attempts=5)

    def test_cleanup_resize_not_same_host(self):
        CONF.set_override('policy_dirs', [], group='oslo_policy')
        host = 'not' + CONF.host
        ins_ref = self._create_instance({'host': host})
        fake_net = _fake_network_info(self, 1)

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend

        with test.nested(
                mock.patch.object(os.path, 'exists'),
                mock.patch.object(libvirt_utils, 'get_instance_path'),
                mock.patch.object(utils, 'execute'),
                mock.patch.object(drvr, '_undefine_domain'),
                mock.patch.object(drvr, 'unplug_vifs'),
                mock.patch.object(drvr, 'unfilter_instance')
        ) as (mock_exists, mock_get_path, mock_exec, mock_undef,
              mock_unplug, mock_unfilter):
            mock_exists.return_value = True
            mock_get_path.return_value = '/fake/inst'

            drvr._cleanup_resize(ins_ref, fake_net)
            mock_get_path.assert_called_once_with(ins_ref)
            mock_exec.assert_called_once_with('rm', '-rf', '/fake/inst_resize',
                                              delay_on_retry=True, attempts=5)
            mock_undef.assert_called_once_with(ins_ref)
            mock_unplug.assert_called_once_with(ins_ref, fake_net)
            mock_unfilter.assert_called_once_with(ins_ref, fake_net)

    def test_cleanup_resize_snap_backend(self):
        CONF.set_override('policy_dirs', [], group='oslo_policy')
        ins_ref = self._create_instance({'host': CONF.host})
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend

        with test.nested(
                mock.patch.object(os.path, 'exists'),
                mock.patch.object(libvirt_utils, 'get_instance_path'),
                mock.patch.object(utils, 'execute'),
                mock.patch.object(drvr.image_backend, 'remove_snap')) as (
                mock_exists, mock_get_path, mock_exec, mock_remove):
            mock_exists.return_value = True
            mock_get_path.return_value = '/fake/inst'

            drvr._cleanup_resize(ins_ref, _fake_network_info(self, 1))
            mock_get_path.assert_called_once_with(ins_ref)
            mock_exec.assert_called_once_with('rm', '-rf', '/fake/inst_resize',
                                              delay_on_retry=True, attempts=5)
            mock_remove.assert_called_once_with(
                    libvirt_utils.RESIZE_SNAPSHOT_NAME, ignore_errors=True)

    def test_cleanup_resize_snap_backend_image_does_not_exist(self):
        CONF.set_override('policy_dirs', [], group='oslo_policy')
        ins_ref = self._create_instance({'host': CONF.host})
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        drvr.image_backend = mock.Mock()
        drvr.image_backend.image.return_value = drvr.image_backend
        drvr.image_backend.exists.return_value = False

        with test.nested(
                mock.patch.object(os.path, 'exists'),
                mock.patch.object(libvirt_utils, 'get_instance_path'),
                mock.patch.object(utils, 'execute'),
                mock.patch.object(drvr.image_backend, 'remove_snap')) as (
                mock_exists, mock_get_path, mock_exec, mock_remove):
            mock_exists.return_value = True
            mock_get_path.return_value = '/fake/inst'

            drvr._cleanup_resize(ins_ref, _fake_network_info(self, 1))
            mock_get_path.assert_called_once_with(ins_ref)
            mock_exec.assert_called_once_with('rm', '-rf', '/fake/inst_resize',
                                              delay_on_retry=True, attempts=5)
            self.assertFalse(mock_remove.called)

    def test_get_instance_disk_info_exception(self):
        instance = self._create_instance()

        class FakeExceptionDomain(FakeVirtDomain):
            def __init__(self):
                super(FakeExceptionDomain, self).__init__()

            def XMLDesc(self, flags):
                raise fakelibvirt.libvirtError("Libvirt error")

        def fake_get_domain(self, instance):
            return FakeExceptionDomain()

        self.stubs.Set(host.Host, 'get_domain',
                       fake_get_domain)
        self.assertRaises(exception.InstanceNotFound,
            self.drvr.get_instance_disk_info,
            instance)

    @mock.patch('os.path.exists')
    @mock.patch.object(lvm, 'list_volumes')
    def test_lvm_disks(self, listlvs, exists):
        instance = objects.Instance(uuid=uuids.instance, id=1)
        self.flags(images_volume_group='vols', group='libvirt')
        exists.return_value = True
        listlvs.return_value = ['%s_foo' % uuids.instance,
                                'other-uuid_foo']
        disks = self.drvr._lvm_disks(instance)
        self.assertEqual(['/dev/vols/%s_foo' % uuids.instance], disks)

    def test_is_booted_from_volume(self):
        func = libvirt_driver.LibvirtDriver._is_booted_from_volume
        instance, disk_mapping = {}, {}

        self.assertTrue(func(instance, disk_mapping))
        disk_mapping['disk'] = 'map'
        self.assertTrue(func(instance, disk_mapping))

        instance['image_ref'] = 'uuid'
        self.assertFalse(func(instance, disk_mapping))

    @mock.patch('nova.virt.netutils.get_injected_network_template')
    @mock.patch('nova.virt.disk.api.inject_data')
    @mock.patch.object(libvirt_driver.LibvirtDriver, "_conn")
    def _test_inject_data(self, driver_params, path, disk_params,
                          mock_conn, disk_inject_data, inj_network,
                          called=True):
        class ImageBackend(object):
            path = '/path'

            def get_model(self, connection):
                return imgmodel.LocalFileImage(self.path,
                                               imgmodel.FORMAT_RAW)

        def fake_inj_network(*args, **kwds):
            return args[0] or None
        inj_network.side_effect = fake_inj_network

        image_backend = ImageBackend()
        image_backend.path = path

        with mock.patch.object(
                self.drvr.image_backend,
                'image',
                return_value=image_backend):
            self.flags(inject_partition=0, group='libvirt')

            self.drvr._inject_data(image_backend, **driver_params)

            if called:
                disk_inject_data.assert_called_once_with(
                    mock.ANY,
                    *disk_params,
                    partition=None, mandatory=('files',))

            self.assertEqual(disk_inject_data.called, called)

    def _test_inject_data_default_driver_params(self, **params):
        return {
            'instance': self._create_instance(params=params),
            'network_info': None,
            'admin_pass': None,
            'files': None
        }

    def test_inject_data_adminpass(self):
        self.flags(inject_password=True, group='libvirt')
        driver_params = self._test_inject_data_default_driver_params()
        driver_params['admin_pass'] = 'foobar'
        disk_params = [
            None,  # key
            None,  # net
            {},  # metadata
            'foobar',  # admin_pass
            None,  # files
        ]
        self._test_inject_data(driver_params, "/path", disk_params)

        # Test with the configuration setted to false.
        self.flags(inject_password=False, group='libvirt')
        self._test_inject_data(driver_params, "/path",
                               disk_params, called=False)

    def test_inject_data_key(self):
        driver_params = self._test_inject_data_default_driver_params()
        driver_params['instance']['key_data'] = 'key-content'

        self.flags(inject_key=True, group='libvirt')
        disk_params = [
            'key-content',  # key
            None,  # net
            {},  # metadata
            None,  # admin_pass
            None,  # files
        ]
        self._test_inject_data(driver_params, "/path", disk_params)

        # Test with the configuration setted to false.
        self.flags(inject_key=False, group='libvirt')
        self._test_inject_data(driver_params, "/path",
                               disk_params, called=False)

    def test_inject_data_metadata(self):
        instance_metadata = {'metadata': {'data': 'foo'}}
        driver_params = self._test_inject_data_default_driver_params(
            **instance_metadata
        )
        disk_params = [
            None,  # key
            None,  # net
            {'data': 'foo'},  # metadata
            None,  # admin_pass
            None,  # files
        ]
        self._test_inject_data(driver_params, "/path", disk_params)

    def test_inject_data_files(self):
        driver_params = self._test_inject_data_default_driver_params()
        driver_params['files'] = ['file1', 'file2']
        disk_params = [
            None,  # key
            None,  # net
            {},  # metadata
            None,  # admin_pass
            ['file1', 'file2'],  # files
        ]
        self._test_inject_data(driver_params, "/path", disk_params)

    def test_inject_data_net(self):
        driver_params = self._test_inject_data_default_driver_params()
        driver_params['network_info'] = {'net': 'eno1'}
        disk_params = [
            None,  # key
            {'net': 'eno1'},  # net
            {},  # metadata
            None,  # admin_pass
            None,  # files
        ]
        self._test_inject_data(driver_params, "/path", disk_params)

    def test_inject_not_exist_image(self):
        driver_params = self._test_inject_data_default_driver_params()
        disk_params = [
            'key-content',  # key
            None,  # net
            None,  # metadata
            None,  # admin_pass
            None,  # files
        ]
        self._test_inject_data(driver_params, "/fail/path",
                               disk_params, called=False)

    def _test_attach_detach_interface(self, method, power_state,
                                      expected_flags):
        instance = self._create_instance()
        network_info = _fake_network_info(self, 1)
        domain = FakeVirtDomain()
        self.mox.StubOutWithMock(host.Host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr.firewall_driver,
                                 'setup_basic_filtering')
        self.mox.StubOutWithMock(domain, 'attachDeviceFlags')
        self.mox.StubOutWithMock(domain, 'info')

        host.Host.get_domain(instance).AndReturn(domain)
        if method == 'attach_interface':
            self.drvr.firewall_driver.setup_basic_filtering(
                instance, [network_info[0]])

        fake_image_meta = objects.ImageMeta.from_dict(
            {'id': instance.image_ref})

        expected = self.drvr.vif_driver.get_config(
            instance, network_info[0], fake_image_meta, instance.flavor,
            CONF.libvirt.virt_type, self.drvr._host)

        self.mox.StubOutWithMock(self.drvr.vif_driver,
                                 'get_config')
        self.drvr.vif_driver.get_config(
            instance, network_info[0],
            mox.IsA(objects.ImageMeta),
            mox.IsA(objects.Flavor),
            CONF.libvirt.virt_type,
            self.drvr._host).AndReturn(expected)
        domain.info().AndReturn([power_state, 1, 2, 3, 4])
        if method == 'attach_interface':
            domain.attachDeviceFlags(expected.to_xml(), flags=expected_flags)
        elif method == 'detach_interface':
            domain.detachDeviceFlags(expected.to_xml(), expected_flags)

        self.mox.ReplayAll()
        if method == 'attach_interface':
            self.drvr.attach_interface(
                instance, fake_image_meta, network_info[0])
        elif method == 'detach_interface':
            self.drvr.detach_interface(
                instance, network_info[0])
        self.mox.VerifyAll()

    def test_attach_interface_with_running_instance(self):
        self._test_attach_detach_interface(
            'attach_interface', power_state.RUNNING,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                            fakelibvirt.VIR_DOMAIN_AFFECT_LIVE))

    def test_attach_interface_with_pause_instance(self):
        self._test_attach_detach_interface(
            'attach_interface', power_state.PAUSED,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                            fakelibvirt.VIR_DOMAIN_AFFECT_LIVE))

    def test_attach_interface_with_shutdown_instance(self):
        self._test_attach_detach_interface(
            'attach_interface', power_state.SHUTDOWN,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG))

    def test_detach_interface_with_running_instance(self):
        self._test_attach_detach_interface(
            'detach_interface', power_state.RUNNING,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                            fakelibvirt.VIR_DOMAIN_AFFECT_LIVE))

    def test_detach_interface_with_pause_instance(self):
        self._test_attach_detach_interface(
            'detach_interface', power_state.PAUSED,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG |
                            fakelibvirt.VIR_DOMAIN_AFFECT_LIVE))

    def test_detach_interface_with_shutdown_instance(self):
        self._test_attach_detach_interface(
            'detach_interface', power_state.SHUTDOWN,
            expected_flags=(fakelibvirt.VIR_DOMAIN_AFFECT_CONFIG))

    @mock.patch('nova.virt.libvirt.driver.LOG')
    def test_detach_interface_device_not_found(self, mock_log):
        # Asserts that we don't log an error when the interface device is not
        # found on the guest after a libvirt error during detach.
        instance = self._create_instance()
        vif = _fake_network_info(self, 1)[0]
        guest = mock.Mock(spec='nova.virt.libvirt.guest.Guest')
        guest.get_power_state = mock.Mock()
        self.drvr._host.get_guest = mock.Mock(return_value=guest)
        self.drvr.vif_driver = mock.Mock()
        error = fakelibvirt.libvirtError(
            'no matching network device was found')
        error.err = (fakelibvirt.VIR_ERR_OPERATION_FAILED,)
        guest.detach_device = mock.Mock(side_effect=error)
        # mock out that get_interface_by_mac doesn't find the interface
        guest.get_interface_by_mac = mock.Mock(return_value=None)
        self.drvr.detach_interface(instance, vif)
        guest.get_interface_by_mac.assert_called_once_with(vif['address'])
        # an error shouldn't be logged, but a warning should be logged
        self.assertFalse(mock_log.error.called)
        self.assertEqual(1, mock_log.warning.call_count)
        self.assertIn('the device is no longer found on the guest',
                      six.text_type(mock_log.warning.call_args[0]))

    def test_rescue(self):
        instance = self._create_instance({'config_drive': None})
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")
        network_info = _fake_network_info(self, 1)

        self.mox.StubOutWithMock(self.drvr,
                                     '_get_existing_domain_xml')
        self.mox.StubOutWithMock(libvirt_utils, 'write_to_file')
        self.mox.StubOutWithMock(self.drvr, '_get_guest_xml')
        self.mox.StubOutWithMock(self.drvr, '_destroy')
        self.mox.StubOutWithMock(self.drvr, '_create_domain')

        self.drvr._get_existing_domain_xml(mox.IgnoreArg(),
                        mox.IgnoreArg()).MultipleTimes().AndReturn(dummyxml)
        libvirt_utils.write_to_file(mox.IgnoreArg(), mox.IgnoreArg())

        image_meta = objects.ImageMeta.from_dict(
            {'id': uuids.image_id, 'name': 'fake'})
        self.drvr._get_guest_xml(mox.IgnoreArg(), instance,
                                 network_info, mox.IgnoreArg(),
                                 image_meta,
                                 rescue=mox.IgnoreArg(),
                                 write_to_disk=mox.IgnoreArg()
                             ).AndReturn(dummyxml)

        self.drvr._destroy(instance)
        self.drvr._create_domain(mox.IgnoreArg(),
                                 post_xml_callback=mox.IgnoreArg())

        self.mox.ReplayAll()

        rescue_password = 'fake_password'

        fake_backend = self.useFixture(fake_imagebackend.ImageBackendFixture(
            exists=lambda name: name != 'disk.config.rescue'))

        self.drvr.rescue(self.context, instance, network_info, image_meta,
                         rescue_password)
        self.mox.VerifyAll()

        disks = fake_backend.created_disks
        self.assertEqual(3, len(disks))
        expected_types = {
            'kernel.rescue': 'raw',
            'ramdisk.rescue': 'raw',
            'disk.rescue': 'default'
        }
        # cache() should have been called on the 3 disk backends
        for name in ('kernel', 'ramdisk', 'disk'):
            name = name + '.rescue'
            disk = disks[name]
            self.assertTrue(disk.cache.called)
            self.assertEqual(expected_types[name], disk.image_type)

    @mock.patch.object(libvirt_utils, 'get_instance_path')
    @mock.patch.object(libvirt_utils, 'load_file')
    @mock.patch.object(host.Host, "get_domain")
    def test_unrescue(self, mock_get_domain, mock_load_file,
                                           mock_get_instance_path):
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='block' device='disk'>"
                    "<source dev='/dev/some-vg/some-lv'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "</devices></domain>")

        mock_get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)
        fake_dom = FakeVirtDomain(fake_xml=dummyxml)
        mock_get_domain.return_value = fake_dom
        mock_load_file.return_value = "fake_unrescue_xml"
        unrescue_xml_path = os.path.join('/path', 'unrescue.xml')
        xml_path = os.path.join('/path', 'libvirt.xml')
        rescue_file = os.path.join('/path', 'rescue.file')
        rescue_dir = os.path.join('/path', 'rescue.dir')

        def isdir_sideeffect(*args, **kwargs):
            if args[0] == '/path/rescue.file':
                return False
            if args[0] == '/path/rescue.dir':
                return True

        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        with test.nested(
                mock.patch.object(libvirt_utils, 'write_to_file'),
                mock.patch.object(drvr, '_destroy'),
                mock.patch.object(drvr, '_create_domain'),
                mock.patch.object(libvirt_utils, 'file_delete'),
                mock.patch.object(shutil, 'rmtree'),
                mock.patch.object(os.path, "isdir",
                                  side_effect=isdir_sideeffect),
                mock.patch.object(drvr, '_lvm_disks',
                                  return_value=['lvm.rescue']),
                mock.patch.object(lvm, 'remove_volumes'),
                mock.patch.object(glob, 'iglob',
                                  return_value=[rescue_file, rescue_dir])
                ) as (mock_write, mock_destroy, mock_create, mock_del,
                      mock_rmtree, mock_isdir, mock_lvm_disks,
                      mock_remove_volumes, mock_glob):
            drvr.unrescue(instance, None)
            mock_write.assert_called_once_with(xml_path, "fake_unrescue_xml")
            mock_destroy.assert_called_once_with(instance)
            mock_create.assert_called_once_with("fake_unrescue_xml",
                                                 fake_dom)
            self.assertEqual(2, mock_del.call_count)
            self.assertEqual(unrescue_xml_path,
                             mock_del.call_args_list[0][0][0])
            self.assertEqual(1, mock_rmtree.call_count)
            self.assertEqual(rescue_dir, mock_rmtree.call_args_list[0][0][0])
            self.assertEqual(rescue_file, mock_del.call_args_list[1][0][0])
            mock_remove_volumes.assert_called_once_with(['lvm.rescue'])

    @mock.patch(
        'nova.virt.libvirt.driver.LibvirtDriver._build_device_metadata',
        return_value=None)
    @mock.patch(
        'nova.virt.configdrive.ConfigDriveBuilder.add_instance_metadata')
    @mock.patch('nova.virt.configdrive.ConfigDriveBuilder.make_drive')
    @mock.patch('nova.virt.libvirt.guest.Guest')
    def test_rescue_config_drive(self, mock_guest, mock_make, mock_add,
                                 mock_save):
        instance = self._create_instance({'config_drive': str(True)})
        uuid = instance.uuid
        configdrive_path = uuid + '/disk.config.rescue'
        dummyxml = ("<domain type='kvm'><name>instance-0000000a</name>"
                    "<devices>"
                    "<disk type='file'><driver name='qemu' type='raw'/>"
                    "<source file='/test/disk'/>"
                    "<target dev='vda' bus='virtio'/></disk>"
                    "<disk type='file'><driver name='qemu' type='qcow2'/>"
                    "<source file='/test/disk.local'/>"
                    "<target dev='vdb' bus='virtio'/></disk>"
                    "</devices></domain>")
        network_info = _fake_network_info(self, 1)

        self.mox.StubOutWithMock(self.drvr,
                                    '_get_existing_domain_xml')
        self.mox.StubOutWithMock(libvirt_utils, 'write_to_file')
        self.mox.StubOutWithMock(instance_metadata.InstanceMetadata,
                                                            '__init__')
        self.mox.StubOutWithMock(self.drvr, '_get_guest_xml')
        self.mox.StubOutWithMock(self.drvr, '_destroy')
        self.drvr._get_existing_domain_xml(mox.IgnoreArg(),
                    mox.IgnoreArg()).MultipleTimes().AndReturn(dummyxml)
        libvirt_utils.write_to_file(mox.IgnoreArg(), mox.IgnoreArg())

        instance_metadata.InstanceMetadata.__init__(mox.IgnoreArg(),
                                            content=mox.IgnoreArg(),
                                            extra_md=mox.IgnoreArg(),
                                            network_info=mox.IgnoreArg(),
                                            request_context=mox.IgnoreArg())
        image_meta = objects.ImageMeta.from_dict(
            {'id': uuids.image_id, 'name': 'fake'})
        self.drvr._get_guest_xml(mox.IgnoreArg(), instance,
                                 network_info, mox.IgnoreArg(),
                                 image_meta,
                                 rescue=mox.IgnoreArg(),
                                 write_to_disk=mox.IgnoreArg()
                                ).AndReturn(dummyxml)
        self.drvr._destroy(instance)

        self.mox.ReplayAll()

        rescue_password = 'fake_password'

        fake_backend = self.useFixture(fake_imagebackend.ImageBackendFixture(
            exists=lambda name: name != 'disk.config.rescue'))

        self.drvr.rescue(self.context, instance, network_info, image_meta,
                         rescue_password)

        self.mox.VerifyAll()

        mock_add.assert_any_call(mock.ANY)
        expected_call = [mock.call(os.path.join(CONF.instances_path,
                                                configdrive_path))]
        mock_make.assert_has_calls(expected_call)

        disks = fake_backend.created_disks
        self.assertEqual(4, len(disks))
        expected_types = {
            'kernel.rescue': 'raw',
            'ramdisk.rescue': 'raw',
            'disk.rescue': 'default'
        }
        # cache() should have been called on the 3 non-config disk backends
        for name in ('kernel', 'ramdisk', 'disk'):
            name = name + '.rescue'
            disk = disks[name]
            self.assertTrue(disk.cache.called)
            self.assertEqual(expected_types[name], disk.image_type)

        self.assertEqual('raw', disks['disk.config.rescue'].image_type)

        # import_file() should have been called for the config disk
        self.assertTrue(disks['disk.config.rescue'].import_file.called)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files(self, get_instance_path, exists, exe,
                                   shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        exists.side_effect = [False, False, True, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        exe.assert_called_with('mv', '/path', '/path_del')
        shutil.assert_called_with('/path_del')
        self.assertTrue(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('os.kill')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_kill_running(
            self, get_instance_path, kill, exists, exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)
        self.drvr.job_tracker.jobs[instance.uuid] = [3, 4]

        exists.side_effect = [False, False, True, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        exe.assert_called_with('mv', '/path', '/path_del')
        kill.assert_has_calls([mock.call(3, signal.SIGKILL), mock.call(3, 0),
                               mock.call(4, signal.SIGKILL), mock.call(4, 0)])
        shutil.assert_called_with('/path_del')
        self.assertTrue(result)
        self.assertNotIn(instance.uuid, self.drvr.job_tracker.jobs)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_resize(self, get_instance_path, exists,
                                          exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        nova.utils.execute.side_effect = [Exception(), None]
        exists.side_effect = [False, False, True, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        expected = [mock.call('mv', '/path', '/path_del'),
                    mock.call('mv', '/path_resize', '/path_del')]
        self.assertEqual(expected, exe.mock_calls)
        shutil.assert_called_with('/path_del')
        self.assertTrue(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_failed(self, get_instance_path, exists, exe,
                                          shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        exists.side_effect = [False, False, True, True]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        exe.assert_called_with('mv', '/path', '/path_del')
        shutil.assert_called_with('/path_del')
        self.assertFalse(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_mv_failed(self, get_instance_path, exists,
                                             exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        nova.utils.execute.side_effect = Exception()
        exists.side_effect = [True, True]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        expected = [mock.call('mv', '/path', '/path_del'),
                    mock.call('mv', '/path_resize', '/path_del')] * 2
        self.assertEqual(expected, exe.mock_calls)
        self.assertFalse(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_resume(self, get_instance_path, exists,
                                             exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        nova.utils.execute.side_effect = Exception()
        exists.side_effect = [False, False, True, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        expected = [mock.call('mv', '/path', '/path_del'),
                    mock.call('mv', '/path_resize', '/path_del')] * 2
        self.assertEqual(expected, exe.mock_calls)
        self.assertTrue(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_none(self, get_instance_path, exists,
                                        exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        nova.utils.execute.side_effect = Exception()
        exists.side_effect = [False, False, False, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        expected = [mock.call('mv', '/path', '/path_del'),
                    mock.call('mv', '/path_resize', '/path_del')] * 2
        self.assertEqual(expected, exe.mock_calls)
        self.assertEqual(0, len(shutil.mock_calls))
        self.assertTrue(result)

    @mock.patch('shutil.rmtree')
    @mock.patch('nova.utils.execute')
    @mock.patch('os.path.exists')
    @mock.patch('nova.virt.libvirt.utils.get_instance_path')
    def test_delete_instance_files_concurrent(self, get_instance_path, exists,
                                              exe, shutil):
        get_instance_path.return_value = '/path'
        instance = objects.Instance(uuid=uuids.instance, id=1)

        nova.utils.execute.side_effect = [Exception(), Exception(), None]
        exists.side_effect = [False, False, True, False]

        result = self.drvr.delete_instance_files(instance)
        get_instance_path.assert_called_with(instance)
        expected = [mock.call('mv', '/path', '/path_del'),
                    mock.call('mv', '/path_resize', '/path_del')]
        expected.append(expected[0])
        self.assertEqual(expected, exe.mock_calls)
        shutil.assert_called_with('/path_del')
        self.assertTrue(result)

    def _assert_on_id_map(self, idmap, klass, start, target, count):
        self.assertIsInstance(idmap, klass)
        self.assertEqual(start, idmap.start)
        self.assertEqual(target, idmap.target)
        self.assertEqual(count, idmap.count)

    def test_get_id_maps(self):
        self.flags(virt_type="lxc", group="libvirt")
        CONF.libvirt.virt_type = "lxc"
        CONF.libvirt.uid_maps = ["0:10000:1", "1:20000:10"]
        CONF.libvirt.gid_maps = ["0:10000:1", "1:20000:10"]

        idmaps = self.drvr._get_guest_idmaps()

        self.assertEqual(len(idmaps), 4)
        self._assert_on_id_map(idmaps[0],
                               vconfig.LibvirtConfigGuestUIDMap,
                               0, 10000, 1)
        self._assert_on_id_map(idmaps[1],
                               vconfig.LibvirtConfigGuestUIDMap,
                               1, 20000, 10)
        self._assert_on_id_map(idmaps[2],
                               vconfig.LibvirtConfigGuestGIDMap,
                               0, 10000, 1)
        self._assert_on_id_map(idmaps[3],
                               vconfig.LibvirtConfigGuestGIDMap,
                               1, 20000, 10)

    def test_get_id_maps_not_lxc(self):
        CONF.libvirt.uid_maps = ["0:10000:1", "1:20000:10"]
        CONF.libvirt.gid_maps = ["0:10000:1", "1:20000:10"]

        idmaps = self.drvr._get_guest_idmaps()

        self.assertEqual(0, len(idmaps))

    def test_get_id_maps_only_uid(self):
        self.flags(virt_type="lxc", group="libvirt")
        CONF.libvirt.uid_maps = ["0:10000:1", "1:20000:10"]
        CONF.libvirt.gid_maps = []

        idmaps = self.drvr._get_guest_idmaps()

        self.assertEqual(2, len(idmaps))
        self._assert_on_id_map(idmaps[0],
                               vconfig.LibvirtConfigGuestUIDMap,
                               0, 10000, 1)
        self._assert_on_id_map(idmaps[1],
                               vconfig.LibvirtConfigGuestUIDMap,
                               1, 20000, 10)

    def test_get_id_maps_only_gid(self):
        self.flags(virt_type="lxc", group="libvirt")
        CONF.libvirt.uid_maps = []
        CONF.libvirt.gid_maps = ["0:10000:1", "1:20000:10"]

        idmaps = self.drvr._get_guest_idmaps()

        self.assertEqual(2, len(idmaps))
        self._assert_on_id_map(idmaps[0],
                               vconfig.LibvirtConfigGuestGIDMap,
                               0, 10000, 1)
        self._assert_on_id_map(idmaps[1],
                               vconfig.LibvirtConfigGuestGIDMap,
                               1, 20000, 10)

    def test_instance_on_disk(self):
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(uuid=uuids.instance, id=1)
        self.assertFalse(drvr.instance_on_disk(instance))

    def test_instance_on_disk_rbd(self):
        self.flags(images_type='rbd', group='libvirt')
        drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        instance = objects.Instance(uuid=uuids.instance, id=1)
        self.assertTrue(drvr.instance_on_disk(instance))

    def test_get_disk_xml(self):
        dom_xml = """
              <domain type="kvm">
                <devices>
                  <disk type="file">
                     <source file="disk1_file"/>
                     <target dev="vda" bus="virtio"/>
                     <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
                  </disk>
                  <disk type="block">
                    <source dev="/path/to/dev/1"/>
                    <target dev="vdb" bus="virtio" serial="1234"/>
                  </disk>
                </devices>
              </domain>
              """

        diska_xml = """<disk type="file" device="disk">
  <source file="disk1_file"/>
  <target bus="virtio" dev="vda"/>
  <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
</disk>"""

        diskb_xml = """<disk type="block" device="disk">
  <source dev="/path/to/dev/1"/>
  <target bus="virtio" dev="vdb"/>
</disk>"""

        dom = mock.MagicMock()
        dom.XMLDesc.return_value = dom_xml
        guest = libvirt_guest.Guest(dom)

        # NOTE(gcb): etree.tostring(node) returns an extra line with
        # some white spaces, need to strip it.
        actual_diska_xml = guest.get_disk('vda').to_xml()
        self.assertEqual(diska_xml.strip(), actual_diska_xml.strip())

        actual_diskb_xml = guest.get_disk('vdb').to_xml()
        self.assertEqual(diskb_xml.strip(), actual_diskb_xml.strip())

        self.assertIsNone(guest.get_disk('vdc'))

    def test_vcpu_model_from_config(self):
        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        vcpu_model = drv._cpu_config_to_vcpu_model(None, None)
        self.assertIsNone(vcpu_model)

        cpu = vconfig.LibvirtConfigGuestCPU()
        feature1 = vconfig.LibvirtConfigGuestCPUFeature()
        feature2 = vconfig.LibvirtConfigGuestCPUFeature()
        feature1.name = 'sse'
        feature1.policy = cpumodel.POLICY_REQUIRE
        feature2.name = 'aes'
        feature2.policy = cpumodel.POLICY_REQUIRE

        cpu.features = set([feature1, feature2])
        cpu.mode = cpumodel.MODE_CUSTOM
        cpu.sockets = 1
        cpu.cores = 2
        cpu.threads = 4
        vcpu_model = drv._cpu_config_to_vcpu_model(cpu, None)
        self.assertEqual(cpumodel.MATCH_EXACT, vcpu_model.match)
        self.assertEqual(cpumodel.MODE_CUSTOM, vcpu_model.mode)
        self.assertEqual(4, vcpu_model.topology.threads)
        self.assertEqual(set(['sse', 'aes']),
                         set([f.name for f in vcpu_model.features]))

        cpu.mode = cpumodel.MODE_HOST_MODEL
        vcpu_model_1 = drv._cpu_config_to_vcpu_model(cpu, vcpu_model)
        self.assertEqual(cpumodel.MODE_HOST_MODEL, vcpu_model.mode)
        self.assertEqual(vcpu_model, vcpu_model_1)

    @mock.patch.object(lvm, 'get_volume_size', return_value=10)
    @mock.patch.object(host.Host, "get_guest")
    @mock.patch.object(dmcrypt, 'delete_volume')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.unfilter_instance')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver._undefine_domain')
    @mock.patch.object(objects.Instance, 'save')
    def test_cleanup_lvm_encrypted(self, mock_save, mock_undefine_domain,
                                   mock_unfilter, mock_delete_volume,
                                   mock_get_guest, mock_get_size):
        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance = objects.Instance(
            uuid=uuids.instance, id=1,
            ephemeral_key_uuid=uuids.ephemeral_key_uuid)
        instance.system_metadata = {}
        block_device_info = {'root_device_name': '/dev/vda',
                             'ephemerals': [],
                             'block_device_mapping': []}
        self.flags(images_type="lvm",
                   group='libvirt')
        dom_xml = """
              <domain type="kvm">
                <devices>
                  <disk type="block">
                    <driver name='qemu' type='raw' cache='none'/>
                    <source dev="/dev/mapper/fake-dmcrypt"/>
                    <target dev="vda" bus="virtio" serial="1234"/>
                  </disk>
                </devices>
              </domain>
              """
        dom = mock.MagicMock()
        dom.XMLDesc.return_value = dom_xml
        guest = libvirt_guest.Guest(dom)
        mock_get_guest.return_value = guest
        drv.cleanup(self.context, instance, 'fake_network', destroy_vifs=False,
                        block_device_info=block_device_info)
        mock_delete_volume.assert_called_once_with('/dev/mapper/fake-dmcrypt')

    @mock.patch.object(lvm, 'get_volume_size', return_value=10)
    @mock.patch.object(host.Host, "get_guest")
    @mock.patch.object(dmcrypt, 'delete_volume')
    def _test_cleanup_lvm(self, mock_delete_volume, mock_get_guest, mock_size,
                          encrypted=False):

        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        instance = objects.Instance(
            uuid=uuids.instance, id=1,
            ephemeral_key_uuid=uuids.ephemeral_key_uuid)
        block_device_info = {'root_device_name': '/dev/vda',
                             'ephemerals': [],
                             'block_device_mapping': []}
        dev_name = 'fake-dmcrypt' if encrypted else 'fake'
        dom_xml = """
              <domain type="kvm">
                <devices>
                  <disk type="block">
                    <driver name='qemu' type='raw' cache='none'/>
                    <source dev="/dev/mapper/%s"/>
                    <target dev="vda" bus="virtio" serial="1234"/>
                  </disk>
                </devices>
              </domain>
              """ % dev_name
        dom = mock.MagicMock()
        dom.XMLDesc.return_value = dom_xml
        guest = libvirt_guest.Guest(dom)
        mock_get_guest.return_value = guest
        drv._cleanup_lvm(instance, block_device_info)

        if encrypted:
            mock_delete_volume.assert_called_once_with(
                '/dev/mapper/fake-dmcrypt')
        else:
            self.assertFalse(mock_delete_volume.called)

    def test_cleanup_lvm(self):
        self._test_cleanup_lvm()

    def test_cleanup_encrypted_lvm(self):
        self._test_cleanup_lvm(encrypted=True)

    def test_vcpu_model_to_config(self):
        drv = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)

        feature = objects.VirtCPUFeature(policy=cpumodel.POLICY_REQUIRE,
                                         name='sse')
        feature_1 = objects.VirtCPUFeature(policy=cpumodel.POLICY_FORBID,
                                           name='aes')
        topo = objects.VirtCPUTopology(sockets=1, cores=2, threads=4)
        vcpu_model = objects.VirtCPUModel(mode=cpumodel.MODE_HOST_MODEL,
                                          features=[feature, feature_1],
                                          topology=topo)

        cpu = drv._vcpu_model_to_cpu_config(vcpu_model)
        self.assertEqual(cpumodel.MODE_HOST_MODEL, cpu.mode)
        self.assertEqual(1, cpu.sockets)
        self.assertEqual(4, cpu.threads)
        self.assertEqual(2, len(cpu.features))
        self.assertEqual(set(['sse', 'aes']),
                         set([f.name for f in cpu.features]))
        self.assertEqual(set([cpumodel.POLICY_REQUIRE,
                              cpumodel.POLICY_FORBID]),
                         set([f.policy for f in cpu.features]))

    def test_trigger_crash_dump(self):
        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        instance = objects.Instance(uuid=uuids.instance, id=1)

        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.drvr.trigger_crash_dump(instance)

    def test_trigger_crash_dump_not_running(self):
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                'Requested operation is not valid: domain is not running',
                error_code=fakelibvirt.VIR_ERR_OPERATION_INVALID)

        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        mock_guest.inject_nmi = mock.Mock(side_effect=ex)
        instance = objects.Instance(uuid=uuids.instance, id=1)

        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.assertRaises(exception.InstanceNotRunning,
                              self.drvr.trigger_crash_dump, instance)

    def test_trigger_crash_dump_not_supported(self):
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                '',
                error_code=fakelibvirt.VIR_ERR_NO_SUPPORT)

        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        mock_guest.inject_nmi = mock.Mock(side_effect=ex)
        instance = objects.Instance(uuid=uuids.instance, id=1)

        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.assertRaises(exception.TriggerCrashDumpNotSupported,
                              self.drvr.trigger_crash_dump, instance)

    def test_trigger_crash_dump_unexpected_error(self):
        ex = fakelibvirt.make_libvirtError(
                fakelibvirt.libvirtError,
                'UnexpectedError',
                error_code=fakelibvirt.VIR_ERR_SYSTEM_ERROR)

        mock_guest = mock.Mock(libvirt_guest.Guest, id=1)
        mock_guest.inject_nmi = mock.Mock(side_effect=ex)
        instance = objects.Instance(uuid=uuids.instance, id=1)

        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.assertRaises(fakelibvirt.libvirtError,
                              self.drvr.trigger_crash_dump, instance)


class LibvirtVolumeUsageTestCase(test.NoDBTestCase):
    """Test for LibvirtDriver.get_all_volume_usage."""

    def setUp(self):
        super(LibvirtVolumeUsageTestCase, self).setUp()
        self.drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.c = context.get_admin_context()

        self.ins_ref = objects.Instance(
            id=1729,
            uuid='875a8070-d0b9-4949-8b31-104d125c9a64'
        )

        # verify bootable volume device path also
        self.bdms = [{'volume_id': 1,
                      'device_name': '/dev/vde'},
                     {'volume_id': 2,
                      'device_name': 'vda'}]

    def test_get_all_volume_usage(self):
        def fake_block_stats(instance_name, disk):
            return (169, 688640, 0, 0, -1)

        self.stubs.Set(self.drvr, 'block_stats', fake_block_stats)
        vol_usage = self.drvr.get_all_volume_usage(self.c,
              [dict(instance=self.ins_ref, instance_bdms=self.bdms)])

        expected_usage = [{'volume': 1,
                           'instance': self.ins_ref,
                           'rd_bytes': 688640, 'wr_req': 0,
                           'rd_req': 169, 'wr_bytes': 0},
                           {'volume': 2,
                            'instance': self.ins_ref,
                            'rd_bytes': 688640, 'wr_req': 0,
                            'rd_req': 169, 'wr_bytes': 0}]
        self.assertEqual(vol_usage, expected_usage)

    def test_get_all_volume_usage_device_not_found(self):
        def fake_get_domain(self, instance):
            raise exception.InstanceNotFound(instance_id="fakedom")

        self.stubs.Set(host.Host, 'get_domain', fake_get_domain)
        vol_usage = self.drvr.get_all_volume_usage(self.c,
              [dict(instance=self.ins_ref, instance_bdms=self.bdms)])
        self.assertEqual(vol_usage, [])


class LibvirtNonblockingTestCase(test.NoDBTestCase):
    """Test libvirtd calls are nonblocking."""

    def setUp(self):
        super(LibvirtNonblockingTestCase, self).setUp()
        self.flags(connection_uri="test:///default",
                   group='libvirt')

    def test_connection_to_primitive(self):
        # Test bug 962840.
        import nova.virt.libvirt.driver as libvirt_driver
        drvr = libvirt_driver.LibvirtDriver('')
        drvr.set_host_enabled = mock.Mock()
        jsonutils.to_primitive(drvr._conn, convert_instances=True)

    @mock.patch.object(objects.Service, 'get_by_compute_host')
    def test_tpool_execute_calls_libvirt(self, mock_svc):
        conn = fakelibvirt.virConnect()
        conn.is_expected = True

        self.mox.StubOutWithMock(eventlet.tpool, 'execute')
        eventlet.tpool.execute(
            fakelibvirt.openAuth,
            'test:///default',
            mox.IgnoreArg(),
            mox.IgnoreArg()).AndReturn(conn)
        eventlet.tpool.execute(
            conn.domainEventRegisterAny,
            None,
            fakelibvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE,
            mox.IgnoreArg(),
            mox.IgnoreArg())
        if hasattr(fakelibvirt.virConnect, 'registerCloseCallback'):
            eventlet.tpool.execute(
                conn.registerCloseCallback,
                mox.IgnoreArg(),
                mox.IgnoreArg())
        self.mox.ReplayAll()

        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), True)
        c = driver._get_connection()
        self.assertTrue(c.is_expected)


class LibvirtVolumeSnapshotTestCase(test.NoDBTestCase):
    """Tests for libvirtDriver.volume_snapshot_create/delete."""

    def setUp(self):
        super(LibvirtVolumeSnapshotTestCase, self).setUp()

        self.drvr = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        self.c = context.get_admin_context()

        self.flags(instance_name_template='instance-%s')
        self.flags(qemu_allowed_storage_drivers=[], group='libvirt')

        # creating instance
        self.inst = {}
        self.inst['uuid'] = uuidutils.generate_uuid()
        self.inst['id'] = '1'

        # create domain info
        self.dom_xml = """
              <domain type='kvm'>
                <devices>
                  <disk type='file'>
                     <source file='disk1_file'/>
                     <target dev='vda' bus='virtio'/>
                     <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
                  </disk>
                  <disk type='block'>
                    <source dev='/path/to/dev/1'/>
                    <target dev='vdb' bus='virtio' serial='1234'/>
                  </disk>
                </devices>
              </domain>"""

        # alternate domain info with network-backed snapshot chain
        self.dom_netdisk_xml = """
              <domain type='kvm'>
                <devices>
                  <disk type='file'>
                    <source file='disk1_file'/>
                    <target dev='vda' bus='virtio'/>
                    <serial>0e38683e-f0af-418f-a3f1-6b67eaffffff</serial>
                  </disk>
                  <disk type='network' device='disk'>
                    <driver name='qemu' type='qcow2'/>
                    <source protocol='gluster' name='vol1/root.img'>
                      <host name='server1' port='24007'/>
                    </source>
                    <backingStore type='network' index='1'>
                      <driver name='qemu' type='qcow2'/>
                      <source protocol='gluster' name='vol1/snap.img'>
                        <host name='server1' port='24007'/>
                      </source>
                      <backingStore type='network' index='2'>
                        <driver name='qemu' type='qcow2'/>
                        <source protocol='gluster' name='vol1/snap-b.img'>
                          <host name='server1' port='24007'/>
                        </source>
                        <backingStore/>
                      </backingStore>
                    </backingStore>
                    <target dev='vdb' bus='virtio'/>
                    <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
                  </disk>
                </devices>
              </domain>
        """

        # XML with netdisk attached, and 1 snapshot taken
        self.dom_netdisk_xml_2 = """
              <domain type='kvm'>
                <devices>
                  <disk type='file'>
                    <source file='disk1_file'/>
                    <target dev='vda' bus='virtio'/>
                    <serial>0e38683e-f0af-418f-a3f1-6b67eaffffff</serial>
                  </disk>
                  <disk type='network' device='disk'>
                    <driver name='qemu' type='qcow2'/>
                    <source protocol='gluster' name='vol1/snap.img'>
                      <host name='server1' port='24007'/>
                    </source>
                    <backingStore type='network' index='1'>
                      <driver name='qemu' type='qcow2'/>
                      <source protocol='gluster' name='vol1/root.img'>
                        <host name='server1' port='24007'/>
                      </source>
                      <backingStore/>
                    </backingStore>
                    <target dev='vdb' bus='virtio'/>
                    <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
                  </disk>
                </devices>
              </domain>
        """

        self.create_info = {'type': 'qcow2',
                            'snapshot_id': '1234-5678',
                            'new_file': 'new-file'}

        self.volume_uuid = '0e38683e-f0af-418f-a3f1-6b67ea0f919d'
        self.snapshot_id = '9c3ca9f4-9f4e-4dba-bedd-5c5e4b52b162'

        self.delete_info_1 = {'type': 'qcow2',
                              'file_to_merge': 'snap.img',
                              'merge_target_file': None}

        self.delete_info_2 = {'type': 'qcow2',
                              'file_to_merge': 'snap.img',
                              'merge_target_file': 'other-snap.img'}

        self.delete_info_3 = {'type': 'qcow2',
                              'file_to_merge': None,
                              'merge_target_file': None}

        self.delete_info_netdisk = {'type': 'qcow2',
                                    'file_to_merge': 'snap.img',
                                    'merge_target_file': 'root.img'}

        self.delete_info_invalid_type = {'type': 'made_up_type',
                                         'file_to_merge': 'some_file',
                                         'merge_target_file':
                                             'some_other_file'}

    def tearDown(self):
        super(LibvirtVolumeSnapshotTestCase, self).tearDown()

    @mock.patch('nova.virt.block_device.DriverVolumeBlockDevice.'
                'refresh_connection_info')
    @mock.patch('nova.objects.block_device.BlockDeviceMapping.'
                'get_by_volume_and_instance')
    def test_volume_refresh_connection_info(self,
                                            mock_get_by_volume_and_instance,
                                            mock_refresh_connection_info):
        instance = objects.Instance(**self.inst)
        fake_bdm = fake_block_device.FakeDbBlockDeviceDict({
            'id': 123,
            'instance_uuid': uuids.instance,
            'device_name': '/dev/sdb',
            'source_type': 'volume',
            'destination_type': 'volume',
            'volume_id': 'fake-volume-id-1',
            'connection_info': '{"fake": "connection_info"}'})
        fake_bdm = objects.BlockDeviceMapping(self.c, **fake_bdm)
        mock_get_by_volume_and_instance.return_value = fake_bdm

        self.drvr._volume_refresh_connection_info(self.c, instance,
                                                  self.volume_uuid)

        mock_get_by_volume_and_instance.assert_called_once_with(
            self.c, self.volume_uuid, instance.uuid)
        mock_refresh_connection_info.assert_called_once_with(self.c, instance,
            self.drvr._volume_api, self.drvr)

    def test_volume_snapshot_create(self, quiesce=True):
        """Test snapshot creation with file-based disk."""
        self.flags(instance_name_template='instance-%s')
        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')

        instance = objects.Instance(**self.inst)

        new_file = 'new-file'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        self.mox.StubOutWithMock(domain, 'snapshotCreateXML')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        snap_xml_src = (
           '<domainsnapshot>\n'
           '  <disks>\n'
           '    <disk name="disk1_file" snapshot="external" type="file">\n'
           '      <source file="new-file"/>\n'
           '    </disk>\n'
           '    <disk name="vdb" snapshot="no"/>\n'
           '  </disks>\n'
           '</domainsnapshot>\n')

        # Older versions of libvirt may be missing these.
        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT = 32
        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE = 64

        snap_flags = (fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY |
                      fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA |
                      fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT)

        snap_flags_q = (snap_flags |
                        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE)

        if quiesce:
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags_q)
        else:
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags_q).\
                AndRaise(fakelibvirt.libvirtError(
                            'quiescing failed, no qemu-ga'))
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags)

        self.mox.ReplayAll()

        guest = libvirt_guest.Guest(domain)
        self.drvr._volume_snapshot_create(self.c, instance, guest,
                                          self.volume_uuid, new_file)

        self.mox.VerifyAll()

    def test_volume_snapshot_create_libgfapi(self, quiesce=True):
        """Test snapshot creation with libgfapi network disk."""
        self.flags(instance_name_template = 'instance-%s')
        self.flags(qemu_allowed_storage_drivers = ['gluster'], group='libvirt')
        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')

        self.dom_xml = """
              <domain type='kvm'>
                <devices>
                  <disk type='file'>
                    <source file='disk1_file'/>
                    <target dev='vda' bus='virtio'/>
                    <serial>0e38683e-f0af-418f-a3f1-6b67ea0f919d</serial>
                  </disk>
                  <disk type='block'>
                    <source protocol='gluster' name='gluster1/volume-1234'>
                      <host name='127.3.4.5' port='24007'/>
                    </source>
                    <target dev='vdb' bus='virtio' serial='1234'/>
                  </disk>
                </devices>
              </domain>"""

        instance = objects.Instance(**self.inst)

        new_file = 'new-file'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        self.mox.StubOutWithMock(domain, 'snapshotCreateXML')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        snap_xml_src = (
           '<domainsnapshot>\n'
           '  <disks>\n'
           '    <disk name="disk1_file" snapshot="external" type="file">\n'
           '      <source file="new-file"/>\n'
           '    </disk>\n'
           '    <disk name="vdb" snapshot="no"/>\n'
           '  </disks>\n'
           '</domainsnapshot>\n')

        # Older versions of libvirt may be missing these.
        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT = 32
        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE = 64

        snap_flags = (fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY |
                      fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_NO_METADATA |
                      fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_REUSE_EXT)

        snap_flags_q = (snap_flags |
                        fakelibvirt.VIR_DOMAIN_SNAPSHOT_CREATE_QUIESCE)

        if quiesce:
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags_q)
        else:
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags_q).\
                AndRaise(fakelibvirt.libvirtError(
                    'quiescing failed, no qemu-ga'))
            domain.snapshotCreateXML(snap_xml_src, flags=snap_flags)

        self.mox.ReplayAll()

        guest = libvirt_guest.Guest(domain)
        self.drvr._volume_snapshot_create(self.c, instance, guest,
                                          self.volume_uuid, new_file)

        self.mox.VerifyAll()

    def test_volume_snapshot_create_noquiesce(self):
        self.test_volume_snapshot_create(quiesce=False)

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_can_quiesce(self, ver):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.inst)
        image_meta = objects.ImageMeta.from_dict(
            {"properties": {
                "hw_qemu_guest_agent": "yes"}})
        self.assertIsNone(self.drvr._can_quiesce(instance, image_meta))

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_can_quiesce_bad_hyp(self, ver):
        self.flags(virt_type='lxc', group='libvirt')
        instance = objects.Instance(**self.inst)
        image_meta = objects.ImageMeta.from_dict(
            {"properties": {
                "hw_qemu_guest_agent": "yes"}})
        self.assertRaises(exception.InstanceQuiesceNotSupported,
                          self.drvr._can_quiesce, instance, image_meta)

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=False)
    def test_can_quiesce_bad_ver(self, ver):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.inst)
        image_meta = {"properties": {
            "hw_qemu_guest_agent": "yes"}}
        self.assertRaises(exception.InstanceQuiesceNotSupported,
                          self.drvr._can_quiesce, instance, image_meta)

    @mock.patch.object(host.Host,
                       'has_min_version', return_value=True)
    def test_can_quiesce_agent_not_enable(self, ver):
        self.flags(virt_type='kvm', group='libvirt')
        instance = objects.Instance(**self.inst)
        image_meta = objects.ImageMeta.from_dict({})
        self.assertRaises(exception.QemuGuestAgentNotEnabled,
                          self.drvr._can_quiesce, instance, image_meta)

    @mock.patch('oslo_service.loopingcall.FixedIntervalLoopingCall')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_volume_snapshot_create')
    @mock.patch('nova.virt.libvirt.driver.LibvirtDriver.'
                '_volume_refresh_connection_info')
    def test_volume_snapshot_create_outer_success(self, mock_refresh,
                                                  mock_snap_create, mock_loop):
        class FakeLoopingCall(object):
            def __init__(self, func):
                self.func = func

            def start(self, *a, **k):
                try:
                    self.func()
                except loopingcall.LoopingCallDone:
                    pass
                return self

            def wait(self):
                return None

        mock_loop.side_effect = FakeLoopingCall

        instance = objects.Instance(**self.inst)

        domain = FakeVirtDomain(fake_xml=self.dom_xml, id=1)
        guest = libvirt_guest.Guest(domain)

        @mock.patch.object(self.drvr, '_volume_api')
        @mock.patch.object(self.drvr._host, 'get_guest')
        def _test(mock_get_guest, mock_vol_api):
            mock_get_guest.return_value = guest
            mock_vol_api.get_snapshot.return_value = {'status': 'available'}
            self.drvr.volume_snapshot_create(self.c, instance,
                                             self.volume_uuid,
                                             self.create_info)
            mock_get_guest.assert_called_once_with(instance)
            mock_snap_create.assert_called_once_with(
                    self.c, instance, guest, self.volume_uuid,
                    self.create_info['new_file'])
            mock_vol_api.update_snapshot_status.assert_called_once_with(
                    self.c, self.create_info['snapshot_id'], 'creating')
            mock_vol_api.get_snapshot.assert_called_once_with(
                    self.c, self.create_info['snapshot_id'])
            mock_refresh.assert_called_once_with(
                    self.c, instance, self.volume_uuid)

        _test()

    def test_volume_snapshot_create_outer_failure(self):
        instance = objects.Instance(**self.inst)

        domain = FakeVirtDomain(fake_xml=self.dom_xml, id=1)
        guest = libvirt_guest.Guest(domain)

        self.mox.StubOutWithMock(self.drvr._host, 'get_guest')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')
        self.mox.StubOutWithMock(self.drvr, '_volume_snapshot_create')

        self.drvr._host.get_guest(instance).AndReturn(guest)

        self.drvr._volume_snapshot_create(self.c,
                                          instance,
                                          guest,
                                          self.volume_uuid,
                                          self.create_info['new_file']).\
            AndRaise(exception.NovaException('oops'))

        self.drvr._volume_api.update_snapshot_status(
            self.c, self.create_info['snapshot_id'], 'error')

        self.mox.ReplayAll()

        self.assertRaises(exception.NovaException,
                          self.drvr.volume_snapshot_create,
                          self.c,
                          instance,
                          self.volume_uuid,
                          self.create_info)

    def test_volume_snapshot_delete_1(self):
        """Deleting newest snapshot -- blockRebase."""

        # libvirt lib doesn't have VIR_DOMAIN_BLOCK_REBASE_RELATIVE flag
        fakelibvirt.__dict__.pop('VIR_DOMAIN_BLOCK_REBASE_RELATIVE')
        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        domain.blockRebase('vda', 'snap.img', 0, flags=0)

        domain.blockJobInfo('vda', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vda', flags=0).AndReturn(
            {'cur': 1000, 'end': 1000})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id, self.delete_info_1)

        self.mox.VerifyAll()
        fakelibvirt.__dict__.update({'VIR_DOMAIN_BLOCK_REBASE_RELATIVE': 8})

    def test_volume_snapshot_delete_relative_1(self):
        """Deleting newest snapshot -- blockRebase using relative flag"""

        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        guest = libvirt_guest.Guest(domain)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_guest')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_guest(instance).AndReturn(guest)

        domain.blockRebase('vda', 'snap.img', 0,
                           flags=fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_RELATIVE)

        domain.blockJobInfo('vda', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vda', flags=0).AndReturn(
            {'cur': 1000, 'end': 1000})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id, self.delete_info_1)

        self.mox.VerifyAll()

    def _setup_block_rebase_domain_and_guest_mocks(self, dom_xml):
        mock_domain = mock.Mock(spec=fakelibvirt.virDomain)
        mock_domain.XMLDesc.return_value = dom_xml
        guest = libvirt_guest.Guest(mock_domain)

        exc = fakelibvirt.make_libvirtError(
            fakelibvirt.libvirtError, 'virDomainBlockRebase() failed',
            error_code=fakelibvirt.VIR_ERR_OPERATION_INVALID)
        mock_domain.blockRebase.side_effect = exc

        return mock_domain, guest

    @mock.patch.object(host.Host, "has_min_version",
                       mock.Mock(return_value=True))
    @mock.patch("nova.virt.libvirt.guest.Guest.is_active",
                 mock.Mock(return_value=False))
    @mock.patch('nova.virt.images.qemu_img_info',
                return_value=mock.Mock(file_format="fake_fmt"))
    @mock.patch('nova.utils.execute')
    def test_volume_snapshot_delete_when_dom_not_running(self, mock_execute,
                                                         mock_qemu_img_info):
        """Deleting newest snapshot of a file-based image when the domain is
        not running should trigger a blockRebase using qemu-img not libvirt.
        In this test, we rebase the image with another image as backing file.
        """
        mock_domain, guest = self._setup_block_rebase_domain_and_guest_mocks(
                                self.dom_xml)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'
        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=guest):
            self.drvr._volume_snapshot_delete(self.c, instance,
                                              self.volume_uuid, snapshot_id,
                                              self.delete_info_1)

        mock_qemu_img_info.assert_called_once_with("snap.img")
        mock_execute.assert_called_once_with('qemu-img', 'rebase',
                                             '-b', 'snap.img', '-F',
                                             'fake_fmt', 'disk1_file')

    @mock.patch.object(host.Host, "has_min_version",
                       mock.Mock(return_value=True))
    @mock.patch("nova.virt.libvirt.guest.Guest.is_active",
                 mock.Mock(return_value=False))
    @mock.patch('nova.virt.images.qemu_img_info',
                return_value=mock.Mock(file_format="fake_fmt"))
    @mock.patch('nova.utils.execute')
    def test_volume_snapshot_delete_when_dom_not_running_and_no_rebase_base(
        self, mock_execute, mock_qemu_img_info):
        """Deleting newest snapshot of a file-based image when the domain is
        not running should trigger a blockRebase using qemu-img not libvirt.
        In this test, the image is rebased onto no backing file (i.e.
        it will exist independently of any backing file)
        """
        mock_domain, mock_guest = (
            self._setup_block_rebase_domain_and_guest_mocks(self.dom_xml))

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'
        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            self.drvr._volume_snapshot_delete(self.c, instance,
                                              self.volume_uuid, snapshot_id,
                                              self.delete_info_3)

        self.assertEqual(0, mock_qemu_img_info.call_count)
        mock_execute.assert_called_once_with('qemu-img', 'rebase',
                                             '-b', '', 'disk1_file')

    @mock.patch.object(host.Host, "has_min_version",
                       mock.Mock(return_value=True))
    @mock.patch("nova.virt.libvirt.guest.Guest.is_active",
                 mock.Mock(return_value=False))
    def test_volume_snapshot_delete_when_dom_with_nw_disk_not_running(self):
        """Deleting newest snapshot of a network disk when the domain is not
        running should raise a NovaException.
        """
        mock_domain, mock_guest = (
            self._setup_block_rebase_domain_and_guest_mocks(
                self.dom_netdisk_xml))
        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'
        with mock.patch.object(self.drvr._host, 'get_guest',
                               return_value=mock_guest):
            ex = self.assertRaises(exception.NovaException,
                                   self.drvr._volume_snapshot_delete,
                                   self.c, instance, self.volume_uuid,
                                   snapshot_id, self.delete_info_1)
            self.assertIn('has not been fully tested', six.text_type(ex))

    def test_volume_snapshot_delete_2(self):
        """Deleting older snapshot -- blockCommit."""

        # libvirt lib doesn't have VIR_DOMAIN_BLOCK_COMMIT_RELATIVE
        fakelibvirt.__dict__.pop('VIR_DOMAIN_BLOCK_COMMIT_RELATIVE')
        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        self.mox.ReplayAll()

        self.assertRaises(exception.Invalid,
                          self.drvr._volume_snapshot_delete,
                          self.c,
                          instance,
                          self.volume_uuid,
                          snapshot_id,
                          self.delete_info_2)

        fakelibvirt.__dict__.update({'VIR_DOMAIN_BLOCK_COMMIT_RELATIVE': 4})

    def test_volume_snapshot_delete_relative_2(self):
        """Deleting older snapshot -- blockCommit using relative flag"""

        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        domain.blockCommit('vda', 'other-snap.img', 'snap.img', 0,
                           flags=fakelibvirt.VIR_DOMAIN_BLOCK_COMMIT_RELATIVE)

        domain.blockJobInfo('vda', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vda', flags=0).AndReturn({})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id, self.delete_info_2)

        self.mox.VerifyAll()

    def test_volume_snapshot_delete_nonrelative_null_base(self):
        # Deleting newest and last snapshot of a volume
        # with blockRebase. So base of the new image will be null.

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_xml)
        guest = libvirt_guest.Guest(domain)

        with test.nested(
            mock.patch.object(domain, 'XMLDesc', return_value=self.dom_xml),
            mock.patch.object(self.drvr._host, 'get_guest',
                              return_value=guest),
            mock.patch.object(domain, 'blockRebase'),
            mock.patch.object(domain, 'blockJobInfo',
                              return_value={'cur': 1000, 'end': 1000})
        ) as (mock_xmldesc, mock_get_guest,
              mock_rebase, mock_job_info):

            self.drvr._volume_snapshot_delete(self.c, instance,
                                              self.volume_uuid, snapshot_id,
                                              self.delete_info_3)

            mock_xmldesc.assert_called_once_with(flags=0)
            mock_get_guest.assert_called_once_with(instance)
            mock_rebase.assert_called_once_with('vda', None, 0, flags=0)
            mock_job_info.assert_called_once_with('vda', flags=0)

    def test_volume_snapshot_delete_netdisk_nonrelative_null_base(self):
        # Deleting newest and last snapshot of a network attached volume
        # with blockRebase. So base of the new image will be null.

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeVirtDomain(fake_xml=self.dom_netdisk_xml_2)
        guest = libvirt_guest.Guest(domain)

        with test.nested(
            mock.patch.object(domain, 'XMLDesc',
                              return_value=self.dom_netdisk_xml_2),
            mock.patch.object(self.drvr._host, 'get_guest',
                              return_value=guest),
            mock.patch.object(domain, 'blockRebase'),
            mock.patch.object(domain, 'blockJobInfo',
                              return_value={'cur': 1000, 'end': 1000})
        ) as (mock_xmldesc, mock_get_guest,
              mock_rebase, mock_job_info):

            self.drvr._volume_snapshot_delete(self.c, instance,
                                              self.volume_uuid, snapshot_id,
                                              self.delete_info_3)

            mock_xmldesc.assert_called_once_with(flags=0)
            mock_get_guest.assert_called_once_with(instance)
            mock_rebase.assert_called_once_with('vdb', None, 0, flags=0)
            mock_job_info.assert_called_once_with('vdb', flags=0)

    def test_volume_snapshot_delete_outer_success(self):
        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        FakeVirtDomain(fake_xml=self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')
        self.mox.StubOutWithMock(self.drvr, '_volume_snapshot_delete')

        self.drvr._volume_snapshot_delete(self.c,
                                          instance,
                                          self.volume_uuid,
                                          snapshot_id,
                                          delete_info=self.delete_info_1)

        self.drvr._volume_api.update_snapshot_status(
            self.c, snapshot_id, 'deleting')

        self.mox.StubOutWithMock(self.drvr, '_volume_refresh_connection_info')
        self.drvr._volume_refresh_connection_info(self.c, instance,
                                                  self.volume_uuid)

        self.mox.ReplayAll()

        self.drvr.volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                         snapshot_id,
                                         self.delete_info_1)

        self.mox.VerifyAll()

    def test_volume_snapshot_delete_outer_failure(self):
        instance = objects.Instance(**self.inst)
        snapshot_id = '1234-9876'

        FakeVirtDomain(fake_xml=self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')
        self.mox.StubOutWithMock(self.drvr, '_volume_snapshot_delete')

        self.drvr._volume_snapshot_delete(self.c,
                                          instance,
                                          self.volume_uuid,
                                          snapshot_id,
                                          delete_info=self.delete_info_1).\
            AndRaise(exception.NovaException('oops'))

        self.drvr._volume_api.update_snapshot_status(
            self.c, snapshot_id, 'error_deleting')

        self.mox.ReplayAll()

        self.assertRaises(exception.NovaException,
                          self.drvr.volume_snapshot_delete,
                          self.c,
                          instance,
                          self.volume_uuid,
                          snapshot_id,
                          self.delete_info_1)

        self.mox.VerifyAll()

    def test_volume_snapshot_delete_invalid_type(self):
        instance = objects.Instance(**self.inst)

        FakeVirtDomain(fake_xml=self.dom_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(self.drvr, '_volume_api')

        self.drvr._volume_api.update_snapshot_status(
            self.c, self.snapshot_id, 'error_deleting')

        self.mox.ReplayAll()

        self.assertRaises(exception.NovaException,
                          self.drvr.volume_snapshot_delete,
                          self.c,
                          instance,
                          self.volume_uuid,
                          self.snapshot_id,
                          self.delete_info_invalid_type)

    def test_volume_snapshot_delete_netdisk_1(self):
        """Delete newest snapshot -- blockRebase for libgfapi/network disk."""

        class FakeNetdiskDomain(FakeVirtDomain):
            def __init__(self, *args, **kwargs):
                super(FakeNetdiskDomain, self).__init__(*args, **kwargs)

            def XMLDesc(self, flags):
                return self.dom_netdisk_xml

        # libvirt lib doesn't have VIR_DOMAIN_BLOCK_REBASE_RELATIVE
        fakelibvirt.__dict__.pop('VIR_DOMAIN_BLOCK_REBASE_RELATIVE')
        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeNetdiskDomain(fake_xml=self.dom_netdisk_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_netdisk_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        domain.blockRebase('vdb', 'vdb[1]', 0, flags=0)

        domain.blockJobInfo('vdb', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vdb', flags=0).AndReturn(
            {'cur': 1000, 'end': 1000})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id, self.delete_info_1)
        self.mox.VerifyAll()
        fakelibvirt.__dict__.update({'VIR_DOMAIN_BLOCK_REBASE_RELATIVE': 8})

    def test_volume_snapshot_delete_netdisk_relative_1(self):
        """Delete newest snapshot -- blockRebase for libgfapi/network disk."""

        class FakeNetdiskDomain(FakeVirtDomain):
            def __init__(self, *args, **kwargs):
                super(FakeNetdiskDomain, self).__init__(*args, **kwargs)

            def XMLDesc(self, flags):
                return self.dom_netdisk_xml

        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeNetdiskDomain(fake_xml=self.dom_netdisk_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_netdisk_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        domain.blockRebase('vdb', 'vdb[1]', 0,
                           flags=fakelibvirt.VIR_DOMAIN_BLOCK_REBASE_RELATIVE)

        domain.blockJobInfo('vdb', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vdb', flags=0).AndReturn(
            {'cur': 1000, 'end': 1000})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id, self.delete_info_1)

        self.mox.VerifyAll()

    def test_volume_snapshot_delete_netdisk_2(self):
        """Delete older snapshot -- blockCommit for libgfapi/network disk."""

        class FakeNetdiskDomain(FakeVirtDomain):
            def __init__(self, *args, **kwargs):
                super(FakeNetdiskDomain, self).__init__(*args, **kwargs)

            def XMLDesc(self, flags):
                return self.dom_netdisk_xml

        # libvirt lib doesn't have VIR_DOMAIN_BLOCK_COMMIT_RELATIVE
        fakelibvirt.__dict__.pop('VIR_DOMAIN_BLOCK_COMMIT_RELATIVE')
        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeNetdiskDomain(fake_xml=self.dom_netdisk_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_netdisk_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        self.mox.ReplayAll()

        self.assertRaises(exception.Invalid,
                          self.drvr._volume_snapshot_delete,
                          self.c,
                          instance,
                          self.volume_uuid,
                          snapshot_id,
                          self.delete_info_netdisk)
        fakelibvirt.__dict__.update({'VIR_DOMAIN_BLOCK_COMMIT_RELATIVE': 4})

    def test_volume_snapshot_delete_netdisk_relative_2(self):
        """Delete older snapshot -- blockCommit for libgfapi/network disk."""

        class FakeNetdiskDomain(FakeVirtDomain):
            def __init__(self, *args, **kwargs):
                super(FakeNetdiskDomain, self).__init__(*args, **kwargs)

            def XMLDesc(self, flags):
                return self.dom_netdisk_xml

        self.stubs.Set(libvirt_driver, 'libvirt', fakelibvirt)

        instance = objects.Instance(**self.inst)
        snapshot_id = 'snapshot-1234'

        domain = FakeNetdiskDomain(fake_xml=self.dom_netdisk_xml)
        self.mox.StubOutWithMock(domain, 'XMLDesc')
        domain.XMLDesc(flags=0).AndReturn(self.dom_netdisk_xml)

        self.mox.StubOutWithMock(self.drvr._host, 'get_domain')
        self.mox.StubOutWithMock(domain, 'blockRebase')
        self.mox.StubOutWithMock(domain, 'blockCommit')
        self.mox.StubOutWithMock(domain, 'blockJobInfo')

        self.drvr._host.get_domain(instance).AndReturn(domain)

        domain.blockCommit('vdb', 'vdb[0]', 'vdb[1]', 0,
                           flags=fakelibvirt.VIR_DOMAIN_BLOCK_COMMIT_RELATIVE)

        domain.blockJobInfo('vdb', flags=0).AndReturn({'cur': 1, 'end': 1000})
        domain.blockJobInfo('vdb', flags=0).AndReturn(
            {'cur': 1000, 'end': 1000})

        self.mox.ReplayAll()

        self.drvr._volume_snapshot_delete(self.c, instance, self.volume_uuid,
                                          snapshot_id,
                                          self.delete_info_netdisk)

        self.mox.VerifyAll()


def _fake_convert_image(source, dest, in_format, out_format,
                               run_as_root=True):
    libvirt_driver.libvirt_utils.files[dest] = ''


class _BaseSnapshotTests(test.NoDBTestCase):
    def setUp(self):
        super(_BaseSnapshotTests, self).setUp()
        self.flags(snapshots_directory='./', group='libvirt')
        self.context = context.get_admin_context()

        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.driver.libvirt_utils',
            fake_libvirt_utils))
        self.useFixture(fixtures.MonkeyPatch(
            'nova.virt.libvirt.imagebackend.libvirt_utils',
            fake_libvirt_utils))

        self.image_service = nova.tests.unit.image.fake.stub_out_image_service(
                self)

        self.mock_update_task_state = mock.Mock()

        test_instance = _create_test_instance()
        self.instance_ref = objects.Instance(**test_instance)
        self.instance_ref.info_cache = objects.InstanceInfoCache(
            network_info=None)

    def _assert_snapshot(self, snapshot, disk_format,
                         expected_properties=None):
        self.mock_update_task_state.assert_has_calls([
            mock.call(task_state=task_states.IMAGE_PENDING_UPLOAD),
            mock.call(task_state=task_states.IMAGE_UPLOADING,
                      expected_state=task_states.IMAGE_PENDING_UPLOAD)])

        props = snapshot['properties']
        self.assertEqual(props['image_state'], 'available')
        self.assertEqual(snapshot['status'], 'active')
        self.assertEqual(snapshot['disk_format'], disk_format)
        self.assertEqual(snapshot['name'], 'test-snap')

        if expected_properties:
            for expected_key, expected_value in \
                    six.iteritems(expected_properties):
                self.assertEqual(expected_value, props[expected_key])

    def _create_image(self, extra_properties=None):
        properties = {'instance_id': self.instance_ref['id'],
                      'user_id': str(self.context.user_id)}
        if extra_properties:
            properties.update(extra_properties)

        sent_meta = {'name': 'test-snap',
                     'is_public': False,
                     'status': 'creating',
                     'properties': properties}

        # Create new image. It will be updated in snapshot method
        # To work with it from snapshot, the single image_service is needed
        recv_meta = self.image_service.create(self.context, sent_meta)
        return recv_meta

    @mock.patch.object(host.Host, 'has_min_version')
    @mock.patch.object(imagebackend.Image, 'resolve_driver_format')
    @mock.patch.object(host.Host, 'get_domain')
    def _snapshot(self, image_id, mock_get_domain, mock_resolve, mock_version):
        mock_get_domain.return_value = FakeVirtDomain()
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        driver.snapshot(self.context, self.instance_ref, image_id,
                        self.mock_update_task_state)
        snapshot = self.image_service.show(self.context, image_id)
        return snapshot

    def _test_snapshot(self, disk_format, extra_properties=None):
        recv_meta = self._create_image(extra_properties=extra_properties)
        snapshot = self._snapshot(recv_meta['id'])
        self._assert_snapshot(snapshot, disk_format=disk_format,
                              expected_properties=extra_properties)


class LibvirtSnapshotTests(_BaseSnapshotTests):
    def test_ami(self):
        # Assign different image_ref from nova/images/fakes for testing ami
        self.instance_ref.image_ref = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'
        self.instance_ref.system_metadata = \
          utils.get_system_metadata_from_image(
            {'disk_format': 'ami'})

        self._test_snapshot(disk_format='ami')

    @mock.patch.object(fake_libvirt_utils, 'disk_type', new='raw')
    @mock.patch.object(libvirt_driver.imagebackend.images,
                       'convert_image',
                       side_effect=_fake_convert_image)
    def test_raw(self, mock_convert_image):
        self._test_snapshot(disk_format='raw')

    def test_qcow2(self):
        self._test_snapshot(disk_format='qcow2')

    @mock.patch.object(fake_libvirt_utils, 'disk_type', new='ploop')
    @mock.patch.object(libvirt_driver.imagebackend.images,
                       'convert_image',
                       side_effect=_fake_convert_image)
    def test_ploop(self, mock_convert_image):
        self._test_snapshot(disk_format='ploop')

    def test_no_image_architecture(self):
        self.instance_ref.image_ref = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
        self._test_snapshot(disk_format='qcow2')

    def test_no_original_image(self):
        self.instance_ref.image_ref = '661122aa-1234-dede-fefe-babababababa'
        self._test_snapshot(disk_format='qcow2')

    def test_snapshot_metadata_image(self):
        # Assign an image with an architecture defined (x86_64)
        self.instance_ref.image_ref = 'a440c04b-79fa-479c-bed1-0b816eaec379'

        extra_properties = {'architecture': 'fake_arch',
                            'key_a': 'value_a',
                            'key_b': 'value_b',
                            'os_type': 'linux'}

        self._test_snapshot(disk_format='qcow2',
                            extra_properties=extra_properties)

    @mock.patch.object(rbd_utils, 'RBDDriver')
    @mock.patch.object(rbd_utils, 'rbd')
    def test_raw_with_rbd_clone(self, mock_rbd, mock_driver):
        self.flags(images_type='rbd', group='libvirt')
        rbd = mock_driver.return_value
        rbd.parent_info = mock.Mock(return_value=['test-pool', '', ''])
        rbd.parse_url = mock.Mock(return_value=['a', 'b', 'c', 'd'])
        with mock.patch.object(fake_libvirt_utils, 'find_disk',
                               return_value=('rbd://some/fake/rbd/image',
                                             'raw')):
            with mock.patch.object(fake_libvirt_utils, 'disk_type', new='rbd'):
                self._test_snapshot(disk_format='raw')
        rbd.clone.assert_called_with(mock.ANY, mock.ANY, dest_pool='test-pool')
        rbd.flatten.assert_called_with(mock.ANY, pool='test-pool')

    @mock.patch.object(rbd_utils, 'RBDDriver')
    @mock.patch.object(rbd_utils, 'rbd')
    def test_raw_with_rbd_clone_graceful_fallback(self, mock_rbd, mock_driver):
        self.flags(images_type='rbd', group='libvirt')
        rbd = mock_driver.return_value
        rbd.parent_info = mock.Mock(side_effect=exception.ImageUnacceptable(
            image_id='fake_id', reason='rbd testing'))
        with test.nested(
                mock.patch.object(libvirt_driver.imagebackend.images,
                                  'convert_image',
                                  side_effect=_fake_convert_image),
                mock.patch.object(fake_libvirt_utils, 'find_disk',
                                  return_value=('rbd://some/fake/rbd/image',
                                                'raw')),
                mock.patch.object(fake_libvirt_utils, 'disk_type', new='rbd')):
            self._test_snapshot(disk_format='raw')
            self.assertFalse(rbd.clone.called)

    @mock.patch.object(rbd_utils, 'RBDDriver')
    @mock.patch.object(rbd_utils, 'rbd')
    def test_raw_with_rbd_clone_eperm(self, mock_rbd, mock_driver):
        self.flags(images_type='rbd', group='libvirt')
        rbd = mock_driver.return_value
        rbd.parent_info = mock.Mock(return_value=['test-pool', '', ''])
        rbd.parse_url = mock.Mock(return_value=['a', 'b', 'c', 'd'])
        rbd.clone = mock.Mock(side_effect=exception.Forbidden(
                image_id='fake_id', reason='rbd testing'))
        with test.nested(
                mock.patch.object(libvirt_driver.imagebackend.images,
                                  'convert_image',
                                  side_effect=_fake_convert_image),
                mock.patch.object(fake_libvirt_utils, 'find_disk',
                                  return_value=('rbd://some/fake/rbd/image',
                                                'raw')),
                mock.patch.object(fake_libvirt_utils, 'disk_type', new='rbd')):
            self._test_snapshot(disk_format='raw')
            # Ensure that the direct_snapshot attempt was cleaned up
            rbd.remove_snap.assert_called_with('c', 'd', ignore_errors=False,
                                               pool='b', force=True)

    @mock.patch.object(rbd_utils, 'RBDDriver')
    @mock.patch.object(rbd_utils, 'rbd')
    def test_raw_with_rbd_clone_post_process_fails(self, mock_rbd,
                                                   mock_driver):
        self.flags(images_type='rbd', group='libvirt')
        rbd = mock_driver.return_value
        rbd.parent_info = mock.Mock(return_value=['test-pool', '', ''])
        rbd.parse_url = mock.Mock(return_value=['a', 'b', 'c', 'd'])
        with test.nested(
                mock.patch.object(fake_libvirt_utils, 'find_disk',
                                  return_value=('rbd://some/fake/rbd/image',
                                                'raw')),
                mock.patch.object(fake_libvirt_utils, 'disk_type', new='rbd'),
                mock.patch.object(self.image_service, 'update',
                                  side_effect=test.TestingException)):
            self.assertRaises(test.TestingException, self._test_snapshot,
                              disk_format='raw')
        rbd.clone.assert_called_with(mock.ANY, mock.ANY, dest_pool='test-pool')
        rbd.flatten.assert_called_with(mock.ANY, pool='test-pool')
        # Ensure that the direct_snapshot attempt was cleaned up
        rbd.remove_snap.assert_called_with('c', 'd', ignore_errors=True,
                                           pool='b', force=True)

    @mock.patch.object(imagebackend.Image, 'direct_snapshot')
    @mock.patch.object(imagebackend.Image, 'resolve_driver_format')
    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(host.Host, 'get_guest')
    def test_raw_with_rbd_clone_is_live_snapshot(self,
                                                 mock_get_guest,
                                                 mock_version,
                                                 mock_resolve,
                                                 mock_snapshot):
        self.flags(disable_libvirt_livesnapshot=False, group='workarounds')
        self.flags(images_type='rbd', group='libvirt')
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_guest._domain = mock.Mock()
        mock_get_guest.return_value = mock_guest
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        recv_meta = self._create_image()
        with mock.patch.object(driver, "suspend") as mock_suspend:
            driver.snapshot(self.context, self.instance_ref, recv_meta['id'],
                            self.mock_update_task_state)
            self.assertFalse(mock_suspend.called)

    @mock.patch.object(libvirt_driver.imagebackend.images, 'convert_image',
                       side_effect=_fake_convert_image)
    @mock.patch.object(fake_libvirt_utils, 'find_disk')
    @mock.patch.object(imagebackend.Image, 'resolve_driver_format')
    @mock.patch.object(host.Host, 'has_min_version', return_value=True)
    @mock.patch.object(host.Host, 'get_guest')
    @mock.patch.object(rbd_utils, 'RBDDriver')
    @mock.patch.object(rbd_utils, 'rbd')
    def test_raw_with_rbd_clone_failure_does_cold_snapshot(self,
                                                           mock_rbd,
                                                           mock_driver,
                                                           mock_get_guest,
                                                           mock_version,
                                                           mock_resolve,
                                                           mock_find_disk,
                                                           mock_convert):
        self.flags(disable_libvirt_livesnapshot=False, group='workarounds')
        self.flags(images_type='rbd', group='libvirt')
        rbd = mock_driver.return_value
        rbd.parent_info = mock.Mock(side_effect=exception.ImageUnacceptable(
            image_id='fake_id', reason='rbd testing'))
        mock_find_disk.return_value = ('rbd://some/fake/rbd/image', 'raw')
        mock_guest = mock.Mock(spec=libvirt_guest.Guest)
        mock_guest.get_power_state.return_value = power_state.RUNNING
        mock_guest._domain = mock.Mock()
        mock_get_guest.return_value = mock_guest
        driver = libvirt_driver.LibvirtDriver(fake.FakeVirtAPI(), False)
        recv_meta = self._create_image()

        with mock.patch.object(fake_libvirt_utils, 'disk_type', new='rbd'):
            with mock.patch.object(driver, "suspend") as mock_suspend:
                driver.snapshot(self.context, self.instance_ref,
                                recv_meta['id'], self.mock_update_task_state)
                self.assertTrue(mock_suspend.called)


class LXCSnapshotTests(LibvirtSnapshotTests):
    """Repeat all of the Libvirt snapshot tests, but with LXC enabled"""
    def setUp(self):
        super(LXCSnapshotTests, self).setUp()
        self.flags(virt_type='lxc', group='libvirt')

    def test_raw_with_rbd_clone_failure_does_cold_snapshot(self):
        self.skipTest("managedSave is not supported with LXC")


class LVMSnapshotTests(_BaseSnapshotTests):
    @mock.patch.object(fake_libvirt_utils, 'disk_type', new='lvm')
    @mock.patch.object(libvirt_driver.imagebackend.images,
                       'convert_image',
                       side_effect=_fake_convert_image)
    @mock.patch.object(libvirt_driver.imagebackend.lvm, 'volume_info')
    def _test_lvm_snapshot(self, disk_format, mock_volume_info,
                           mock_convert_image):
        self.flags(images_type='lvm',
                   images_volume_group='nova-vg', group='libvirt')

        self._test_snapshot(disk_format=disk_format)

        mock_volume_info.assert_has_calls([mock.call('/dev/nova-vg/lv')])
        mock_convert_image.assert_called_once_with(
            '/dev/nova-vg/lv', mock.ANY, 'raw', disk_format,
            run_as_root=True)

    def test_raw(self):
        self._test_lvm_snapshot('raw')

    def test_qcow2(self):
        self.flags(snapshot_image_format='qcow2', group='libvirt')
        self._test_lvm_snapshot('qcow2')
