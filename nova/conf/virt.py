# Copyright 2015 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_config import cfg
from oslo_config import types

from nova.conf import paths

ALL_OPTS = [
    cfg.StrOpt(
        'vcpu_pin_set',
        help="""
Defines which physical CPUs (pCPUs) can be used by instance
virtual CPUs (vCPUs).

Possible values:

* A comma-separated list of physical CPU numbers that virtual CPUs can be
  allocated to by default. Each element should be either a single CPU number,
  a range of CPU numbers, or a caret followed by a CPU number to be
  excluded from a previous range. For example:

    vcpu_pin_set = "4-12,^8,15"
"""),

    cfg.StrOpt(
        'compute_driver',
        help="""
Defines which driver to use for controlling virtualization.

Possible values:

* ``libvirt.LibvirtDriver``
* ``xenapi.XenAPIDriver``
* ``fake.FakeDriver``
* ``ironic.IronicDriver``
* ``vmwareapi.VMwareVCDriver``
* ``hyperv.HyperVDriver``
"""),

    cfg.StrOpt(
        'default_ephemeral_format',
        help="""
The default format an ephemeral_volume will be formatted with on creation.

Possible values:

* ``ext2``
* ``ext3``
* ``ext4``
* ``xfs``
* ``ntfs`` (only for Windows guests)
"""),

    cfg.StrOpt(
        'preallocate_images',
        default='none',
        choices=('none', 'space'),
        help="""
The image preallocation mode to use. Image preallocation allows
storage for instance images to be allocated up front when the instance is
initially provisioned. This ensures immediate feedback is given if enough
space isn't available. In addition, it should significantly improve
performance on writes to new blocks and may even improve I/O performance to
prewritten blocks due to reduced fragmentation.

Possible values:

* "none"  => no storage provisioning is done up front
* "space" => storage is fully allocated at instance start
"""),

    cfg.BoolOpt(
        'use_cow_images',
        default=True,
        help="""
Enable use of copy-on-write (cow) images.

QEMU/KVM allow the use of qcow2 as backing files. By disabling this,
backing files will not be used.
"""),

    cfg.BoolOpt(
        'vif_plugging_is_fatal',
        default=True,
        help="""
Determine if instance should boot or fail on VIF plugging timeout.

Nova sends a port update to Neutron after an instance has been scheduled,
providing Neutron with the necessary information to finish setup of the port.
Once completed, Neutron notifies Nova that it has finished setting up the
port, at which point Nova resumes the boot of the instance since network
connectivity is now supposed to be present. A timeout will occur if the reply
is not received after a given interval.

This option determines what Nova does when the VIF plugging timeout event
happens. When enabled, the instance will error out. When disabled, the
instance will continue to boot on the assumption that the port is ready.

Possible values:

* True: Instances should fail after VIF plugging timeout
* False: Instances should continue booting after VIF plugging timeout
"""),

    cfg.IntOpt(
        'vif_plugging_timeout',
        default=300,
        min=0,
        help="""
Timeout for Neutron VIF plugging event message arrival.

Number of seconds to wait for Neutron vif plugging events to
arrive before continuing or failing (see 'vif_plugging_is_fatal').

Interdependencies to other options:

* vif_plugging_is_fatal - If ``vif_plugging_timeout`` is set to zero and
  ``vif_plugging_is_fatal`` is False, events should not be expected to
  arrive at all.
"""),

    cfg.StrOpt(
        'firewall_driver',
        help="""
Firewall driver to use with ``nova-network`` service.

This option only applies when using the ``nova-network`` service. When using
another networking services, such as Neutron, this should be to set to the
``nova.virt.firewall.NoopFirewallDriver``.

If unset (the default), this will default to the hypervisor-specified
default driver.

Possible values:

* nova.virt.firewall.IptablesFirewallDriver
* nova.virt.firewall.NoopFirewallDriver
* nova.virt.libvirt.firewall.IptablesFirewallDriver
* [...]

Interdependencies to other options:

* ``use_neutron``: This must be set to ``False`` to enable ``nova-network``
  networking
"""),

    cfg.BoolOpt(
        'allow_same_net_traffic',
        default=True,
        help="""
Determine whether to allow network traffic from same network.

When set to true, hosts on the same subnet are not filtered and are allowed
to pass all types of traffic between them. On a flat network, this allows
all instances from all projects unfiltered communication. With VLAN
networking, this allows access between instances within the same project.

This option only applies when using the ``nova-network`` service. When using
another networking services, such as Neutron, security groups or other
approaches should be used.

Possible values:

* True: Network traffic should be allowed pass between all instances on the
  same network, regardless of their tenant and security policies
* False: Network traffic should not be allowed pass between instances unless
  it is unblocked in a security group

Interdependencies to other options:

* ``use_neutron``: This must be set to ``False`` to enable ``nova-network``
  networking
* ``firewall_driver``: This must be set to
  ``nova.virt.libvirt.firewall.IptablesFirewallDriver`` to ensure the
  libvirt firewall driver is enabled.
"""),

    cfg.BoolOpt(
        'force_raw_images',
        default=True,
        help="""
Force conversion of backing images to raw format.

Possible values:

* True: Backing image files will be converted to raw image format
* False: Backing image files will not be converted

Interdependencies to other options:

* ``compute_driver``: Only the libvirt driver uses this option.
"""),

    cfg.StrOpt(
        'injected_network_template',
        default=paths.basedir_def('nova/virt/interfaces.template'),
        help='Template file for injected network'),

# NOTE(yamahata): ListOpt won't work because the command may include a comma.
# For example:
#
#     mkfs.ext4 -O dir_index,extent -E stride=8,stripe-width=16
#       --label %(fs_label)s %(target)s
#
# list arguments are comma separated and there is no way to escape such
# commas.
    cfg.MultiStrOpt(
        'virt_mkfs',
        default=[],
        help="""
Name of the mkfs commands for ephemeral device. The format is
<os_type>=<mkfs command>
"""),

    cfg.BoolOpt(
        'resize_fs_using_block_device',
        default=False,
        help="""
If enabled, attempt to resize the filesystem by accessing the image over a
block device. This is done by the host and may not be necessary if the image
contains a recent version of cloud-init. Possible mechanisms require the nbd
driver (for qcow and raw), or loop (for raw).
"""),

    cfg.IntOpt(
        'timeout_nbd',
        default=10,
        min=0,
        help='Amount of time, in seconds, to wait for NBD device start up.'),

    cfg.IntOpt(
        'image_cache_manager_interval',
        default=2400,
        min=-1,
        help="""
Number of seconds to wait between runs of the image cache manager.
Set to -1 to disable. Setting this to 0 will run at the default rate.
"""),

    cfg.StrOpt(
        'image_cache_subdirectory_name',
        default='_base',
        help="""
Where cached images are stored under $instances_path. This is NOT the full
path - just a folder name. For per-compute-host cached images, set to
_base_$my_ip
"""),

    cfg.BoolOpt(
        'remove_unused_base_images',
        default=True,
        help='Should unused base images be removed?'),

    cfg.IntOpt(
        'remove_unused_original_minimum_age_seconds',
        default=(24 * 3600),
        help="""
Unused unresized base images younger than this will not be removed
"""),

    cfg.StrOpt(
        'pointer_model',
        default='usbtablet',
        choices=[None, 'ps2mouse', 'usbtablet'],
        help="""
Generic property to specify the pointer type.

Input devices allow interaction with a graphical framebuffer. For
example to provide a graphic tablet for absolute cursor movement.

If set, the 'hw_pointer_model' image property takes precedence over
this configuration option.

Possible values:

* None: Uses default behavior provided by drivers (mouse on PS2 for
        libvirt x86)
* ps2mouse: Uses relative movement. Mouse connected by PS2
* usbtablet: Uses absolute movement. Tablet connect by USB

Interdependencies to other options:

* usbtablet must be configured with VNC enabled or SPICE enabled and SPICE
  agent disabled. When used with libvirt the instance mode should be
  configured as HVM.
 """),

    cfg.MultiOpt(
        "reserved_huge_pages",
        item_type=types.Dict(),
        help="""
Reserves a number of huge/large memory pages per NUMA host cells

Possible values:

* A list of valid key=value which reflect NUMA node ID, page size
  (Default unit is KiB) and number of pages to be reserved.

    reserved_huge_pages = node:0,size:2048,count:64
    reserved_huge_pages = node:1,size:1GB,count:1

  In this example we are reserving on NUMA node 0 64 pages of 2MiB
  and on NUMA node 1 1 page of 1GiB.
"""),
]


def register_opts(conf):
    conf.register_opts(ALL_OPTS)


def list_opts():
    # TODO(sfinucan): This should be moved to a virt or hardware group
    return {'DEFAULT': ALL_OPTS}
