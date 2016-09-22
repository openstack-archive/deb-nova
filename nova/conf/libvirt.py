# needs:fix_opt_description
# needs:check_deprecation_status
# needs:check_opt_group_and_type
# needs:fix_opt_description_indentation
# needs:fix_opt_registration_consistency


# Copyright 2016 OpenStack Foundation
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

import itertools

from oslo_config import cfg

from nova.conf import paths

# Downtime period in milliseconds
LIVE_MIGRATION_DOWNTIME_MIN = 100
# Step count
LIVE_MIGRATION_DOWNTIME_STEPS_MIN = 3
# Delay in seconds
LIVE_MIGRATION_DOWNTIME_DELAY_MIN = 10

libvirt_group = cfg.OptGroup("libvirt",
                             title="Libvirt Options",
                             help="""
Libvirt options allows cloud administrator to configure related
libvirt hypervisor driver to be used within an OpenStack deployment.
""")

libvirt_general_opts = [
    cfg.StrOpt('rescue_image_id',
               help="""
The ID of the image to boot from to rescue data from a corrupted instance.

If the rescue REST API operation doesn't provide an ID of an image to
use, the image which is referenced by this ID is used. If this
option is not set, the image from the instance is used.

Possible values:

* An ID of an image or nothing. If it points to an *Amazon Machine
  Image* (AMI), consider to set the config options ``rescue_kernel_id``
  and ``rescue_ramdisk_id`` too. If nothing is set, the image of the instance
  is used.

Related options:

* ``rescue_kernel_id``: If the chosen rescue image allows the separate
  definition of its kernel disk, the value of this option is used,
  if specified. This is the case when *Amazon*'s AMI/AKI/ARI image
  format is used for the rescue image.
* ``rescue_ramdisk_id``: If the chosen rescue image allows the separate
  definition of its RAM disk, the value of this option is used if,
  specified. This is the case when *Amazon*'s AMI/AKI/ARI image
  format is used for the rescue image.
"""),
    cfg.StrOpt('rescue_kernel_id',
               help="""
The ID of the kernel (AKI) image to use with the rescue image.

If the chosen rescue image allows the separate definition of its kernel
disk, the value of this option is used, if specified. This is the case
when *Amazon*'s AMI/AKI/ARI image format is used for the rescue image.

Possible values:

* An ID of an kernel image or nothing. If nothing is specified, the kernel
  disk from the instance is used if it was launched with one.

Related options:

* ``rescue_image_id``: If that option points to an image in *Amazon*'s
  AMI/AKI/ARI image format, it's useful to use ``rescue_kernel_id`` too.
"""),
    cfg.StrOpt('rescue_ramdisk_id',
               help="""
The ID of the RAM disk (ARI) image to use with the rescue image.

If the chosen rescue image allows the separate definition of its RAM
disk, the value of this option is used, if specified. This is the case
when *Amazon*'s AMI/AKI/ARI image format is used for the rescue image.

Possible values:

* An ID of a RAM disk image or nothing. If nothing is specified, the RAM
  disk from the instance is used if it was launched with one.

Related options:

* ``rescue_image_id``: If that option points to an image in *Amazon*'s
  AMI/AKI/ARI image format, it's useful to use ``rescue_ramdisk_id`` too.
"""),
    cfg.StrOpt('virt_type',
               default='kvm',
               choices=('kvm', 'lxc', 'qemu', 'uml', 'xen', 'parallels'),
               help="""
Describes the virtualization type (or so called domain type) libvirt should
use.

The choice of this type must match the underlying virtualization strategy
you have chosen for this host.

Possible values:

* See the predefined set of case-sensitive values.

Related options:

* ``connection_uri``: depends on this
* ``disk_prefix``: depends on this
* ``cpu_mode``: depends on this
* ``cpu_model``: depends on this
"""),
    cfg.StrOpt('connection_uri',
               default='',
               help="""
Overrides the default libvirt URI of the chosen virtualization type.

If set, Nova will use this URI to connect to libvirt.

Possible values:

* An URI like ``qemu:///system`` or ``xen+ssh://oirase/`` for example.
  This is only necessary if the URI differs to the commonly known URIs
  for the chosen virtualization type.

Related options:

* ``virt_type``: Influences what is used as default value here.
"""),
    cfg.BoolOpt('inject_password',
                default=False,
                help="""
Allow the injection of an admin password for instance only at ``create`` and
``rebuild`` process.

There is no agent needed within the image to do this. If *libguestfs* is
available on the host, it will be used. Otherwise *nbd* is used. The file
system of the image will be mounted and the admin password, which is provided
in the REST API call will be injected as password for the root user. If no
root user is available, the instance won't be launched and an error is thrown.
Be aware that the injection is *not* possible when the instance gets launched
from a volume.

Possible values:

* True: Allows the injection.
* False (default): Disallows the injection. Any via the REST API provided
admin password will be silently ignored.

Related options:

* ``inject_partition``: That option will decide about the discovery and usage
  of the file system. It also can disable the injection at all.
"""),
    cfg.BoolOpt('inject_key',
                default=False,
                help="""
Allow the injection of an SSH key at boot time.

There is no agent needed within the image to do this. If *libguestfs* is
available on the host, it will be used. Otherwise *nbd* is used. The file
system of the image will be mounted and the SSH key, which is provided
in the REST API call will be injected as SSH key for the root user and
appended to the ``authorized_keys`` of that user. The SELinux context will
be set if necessary. Be aware that the injection is *not* possible when the
instance gets launched from a volume.

This config option will enable directly modifying the instance disk and does
not affect what cloud-init may do using data from config_drive option or the
metadata service.

Related options:

* ``inject_partition``: That option will decide about the discovery and usage
  of the file system. It also can disable the injection at all.
"""),
    cfg.IntOpt('inject_partition',
               default=-2,
               min=-2,
               help="""
Determines the way how the file system is chosen to inject data into it.

*libguestfs* will be used a first solution to inject data. If that's not
available on the host, the image will be locally mounted on the host as a
fallback solution. If libguestfs is not able to determine the root partition
(because there are more or less than one root partition) or cannot mount the
file system it will result in an error and the instance won't be boot.

Possible values:

* -2 => disable the injection of data.
* -1 => find the root partition with the file system to mount with libguestfs
*  0 => The image is not partitioned
* >0 => The number of the partition to use for the injection

Related options:

* ``inject_key``: If this option allows the injection of a SSH key it depends
  on value greater or equal to -1 for ``inject_partition``.
* ``inject_password``: If this option allows the injection of an admin password
  it depends on value greater or equal to -1 for ``inject_partition``.
* ``guestfs`` You can enable the debug log level of libguestfs with this
  config option. A more verbose output will help in debugging issues.
* ``virt_type``: If you use ``lxc`` as virt_type it will be treated as a
  single partition image
"""),
    cfg.BoolOpt('use_usb_tablet',
                default=True,
                deprecated_for_removal=True,
                deprecated_reason="This option is being replaced by the "
                                  "'pointer_model' option.",
                help="""
Enable a mouse cursor within a graphical VNC or SPICE sessions.

This will only be taken into account if the VM is fully virtualized and VNC
and/or SPICE is enabled. If the node doesn't support a graphical framebuffer,
then it is valid to set this to False.

Related options:
* ``[vnc]enabled``: If VNC is enabled, ``use_usb_tablet`` will have an effect.
* ``[spice]enabled`` + ``[spice].agent_enabled``: If SPICE is enabled and the
  spice agent is disabled, the config value of ``use_usb_tablet`` will have
  an effect.
"""),
    cfg.StrOpt('live_migration_inbound_addr',
               help='Live migration target ip or hostname '
                    '(if this option is set to None, which is the default, '
                    'the hostname of the migration target '
                    'compute node will be used)'),
    cfg.StrOpt('live_migration_uri',
               help='Override the default libvirt live migration target URI '
                    '(which is dependent on virt_type) '
                    '(any included "%s" is replaced with '
                    'the migration target hostname)'),
    cfg.BoolOpt('live_migration_tunnelled',
                default=False,
                help='Whether to use tunnelled migration, where migration '
                     'data is transported over the libvirtd connection. If '
                     'True, we use the VIR_MIGRATE_TUNNELLED migration flag, '
                     'avoiding the need to configure the network to allow '
                     'direct hypervisor to hypervisor communication. If '
                     'False, use the native transport. If not set, Nova '
                     'will choose a sensible default based on, for example '
                     'the availability of native encryption support in the '
                     'hypervisor.'),
    cfg.IntOpt('live_migration_bandwidth',
               default=0,
               help='Maximum bandwidth(in MiB/s) to be used during migration. '
                    'If set to 0, will choose a suitable default. Some '
                    'hypervisors do not support this feature and will return '
                    'an error if bandwidth is not 0. Please refer to the '
                    'libvirt documentation for further details'),
    cfg.IntOpt('live_migration_downtime',
               default=500,
               help='Maximum permitted downtime, in milliseconds, for live '
                    'migration switchover. Will be rounded up to a minimum '
                    'of %dms. Use a large value if guest liveness is '
                    'unimportant.' % LIVE_MIGRATION_DOWNTIME_MIN),
    cfg.IntOpt('live_migration_downtime_steps',
               default=10,
               help='Number of incremental steps to reach max downtime value. '
                    'Will be rounded up to a minimum of %d steps' %
                    LIVE_MIGRATION_DOWNTIME_STEPS_MIN),
    cfg.IntOpt('live_migration_downtime_delay',
               default=75,
               help='Time to wait, in seconds, between each step increase '
                    'of the migration downtime. Minimum delay is %d seconds. '
                    'Value is per GiB of guest RAM + disk to be transferred, '
                    'with lower bound of a minimum of 2 GiB per device' %
                    LIVE_MIGRATION_DOWNTIME_DELAY_MIN),
    cfg.IntOpt('live_migration_completion_timeout',
               default=800,
               mutable=True,
               help='Time to wait, in seconds, for migration to successfully '
                    'complete transferring data before aborting the '
                    'operation. Value is per GiB of guest RAM + disk to be '
                    'transferred, with lower bound of a minimum of 2 GiB. '
                    'Should usually be larger than downtime delay * downtime '
                    'steps. Set to 0 to disable timeouts.'),
    cfg.IntOpt('live_migration_progress_timeout',
               default=150,
               mutable=True,
               help='Time to wait, in seconds, for migration to make forward '
                    'progress in transferring data before aborting the '
                    'operation. Set to 0 to disable timeouts.'),
    cfg.BoolOpt('live_migration_permit_post_copy',
                default=False,
                help="""
This option allows nova to switch an on-going live migration to post-copy
mode, i.e., switch the active VM to the one on the destination node before the
migration is complete, therefore ensuring an upper bound on the memory that
needs to be transferred. Post-copy requires libvirt>=1.3.3 and QEMU>=2.5.0.

When permitted, post-copy mode will be automatically activated if a
live-migration memory copy iteration does not make percentage increase of at
least 10% over the last iteration.

The live-migration force complete API also uses post-copy when permitted. If
post-copy mode is not available, force complete falls back to pausing the VM
to ensure the live-migration operation will complete.

When using post-copy mode, if the source and destination hosts loose network
connectivity, the VM being live-migrated will need to be rebooted. For more
details, please see the Administration guide.

Related options:

    * live_migration_permit_auto_converge
"""),
    cfg.BoolOpt('live_migration_permit_auto_converge',
                default=False,
                help="""
This option allows nova to start live migration with auto converge on.
Auto converge throttles down CPU if a progress of on-going live migration
is slow. Auto converge will only be used if this flag is set to True and
post copy is not permitted or post copy is unavailable due to the version
of libvirt and QEMU in use. Auto converge requires libvirt>=1.2.3 and
QEMU>=1.6.0.

Related options:

    * live_migration_permit_post_copy
"""),
    cfg.StrOpt('snapshot_image_format',
               choices=('raw', 'qcow2', 'vmdk', 'vdi'),
               help='Snapshot image format. Defaults to same as source image'),
    cfg.StrOpt('disk_prefix',
               help="""
Override the default disk prefix for the devices attached to an instance.

If set, this is used to identify a free disk device name for a bus.

Possible values:

* Any prefix which will result in a valid disk device name like 'sda' or 'hda'
  for example. This is only necessary if the device names differ to the
  commonly known device name prefixes for a virtualization type such as: sd,
  xvd, uvd, vd.

Related options:

* ``virt_type``: Influences which device type is used, which determines
  the default disk prefix.
"""),
    cfg.IntOpt('wait_soft_reboot_seconds',
               default=120,
               help='Number of seconds to wait for instance to shut down after'
                    ' soft reboot request is made. We fall back to hard reboot'
                    ' if instance does not shutdown within this window.'),
    cfg.StrOpt('cpu_mode',
               choices=('host-model', 'host-passthrough', 'custom', 'none'),
               help="""
Is used to set the CPU mode an instance should have.

If virt_type="kvm|qemu", it will default to "host-model", otherwise it will
default to "none".

Possible values:

* ``host-model``: Clones the host CPU feature flags.
* ``host-passthrough``: Use the host CPU model exactly;
* ``custom``: Use a named CPU model;
* ``none``: Not set any CPU model.

Related options:

* ``cpu_model``: If ``custom`` is used for ``cpu_mode``, set this config
  option too, otherwise this would result in an error and the instance won't
  be launched.
"""),
    cfg.StrOpt('cpu_model',
               help="""
Set the name of the libvirt CPU model the instance should use.

Possible values:

* The names listed in /usr/share/libvirt/cpu_map.xml

Related options:

* ``cpu_mode``: Don't set this when ``cpu_mode`` is NOT set to ``custom``.
  This would result in an error and the instance won't be launched.
* ``virt_type``: Only the virtualization types ``kvm`` and ``qemu`` use this.
"""),
    cfg.StrOpt('snapshots_directory',
               default='$instances_path/snapshots',
               help='Location where libvirt driver will store snapshots '
                    'before uploading them to image service'),
    cfg.StrOpt('xen_hvmloader_path',
               default='/usr/lib/xen/boot/hvmloader',
               help='Location where the Xen hvmloader is kept'),
    cfg.ListOpt('disk_cachemodes',
                default=[],
                help='Specific cachemodes to use for different disk types '
                     'e.g: file=directsync,block=none'),
    cfg.StrOpt('rng_dev_path',
               help='A path to a device that will be used as source of '
                    'entropy on the host. Permitted options are: '
                    '/dev/random or /dev/hwrng'),
    cfg.ListOpt('hw_machine_type',
                help='For qemu or KVM guests, set this option to specify '
                     'a default machine type per host architecture. '
                     'You can find a list of supported machine types '
                     'in your environment by checking the output of '
                     'the "virsh capabilities"command. The format of the '
                     'value for this config option is host-arch=machine-type. '
                     'For example: x86_64=machinetype1,armv7l=machinetype2'),
    cfg.StrOpt('sysinfo_serial',
               default='auto',
               choices=('none', 'os', 'hardware', 'auto'),
               help='The data source used to the populate the host "serial" '
                    'UUID exposed to guest in the virtual BIOS.'),
    cfg.IntOpt('mem_stats_period_seconds',
               default=10,
               help='A number of seconds to memory usage statistics period. '
                    'Zero or negative value mean to disable memory usage '
                    'statistics.'),
    cfg.ListOpt('uid_maps',
                default=[],
                help='List of uid targets and ranges.'
                     'Syntax is guest-uid:host-uid:count'
                     'Maximum of 5 allowed.'),
    cfg.ListOpt('gid_maps',
                default=[],
                help='List of guid targets and ranges.'
                     'Syntax is guest-gid:host-gid:count'
                     'Maximum of 5 allowed.'),
    cfg.IntOpt('realtime_scheduler_priority',
               default=1,
               help='In a realtime host context vCPUs for guest will run in '
                    'that scheduling priority. Priority depends on the host '
                    'kernel (usually 1-99)'),
    cfg.ListOpt('enabled_perf_events',
               default=[],
               help= """
This is a performance event list which could be used as monitor. These events
will be passed to libvirt domain xml while creating a new instances.
Then event statistics data can be collected from libvirt.  The minimum
libvirt version is 2.0.0. For more information about `Performance monitoring
events`, refer https://libvirt.org/formatdomain.html#elementsPerf .

* Possible values:
    A string list.
    For example:
    ``enabled_perf_events = cmt, mbml, mbmt``

    The supported events list can be found in
    https://libvirt.org/html/libvirt-libvirt-domain.html , which
    you may need to search key words ``VIR_PERF_PARAM_*``

* Services that use this:

    ``nova-compute``

* Related options:
    None

"""),
]

libvirt_imagebackend_opts = [
    cfg.StrOpt('images_type',
               default='default',
               choices=('raw', 'flat', 'qcow2', 'lvm', 'rbd', 'ploop',
                        'default'),
               help='VM Images format. If default is specified, then'
                    ' use_cow_images flag is used instead of this one.'),
    cfg.StrOpt('images_volume_group',
               help='LVM Volume Group that is used for VM images, when you'
                    ' specify images_type=lvm.'),
    cfg.BoolOpt('sparse_logical_volumes',
                default=False,
                help='Create sparse logical volumes (with virtualsize)'
                     ' if this flag is set to True.'),
    cfg.StrOpt('images_rbd_pool',
               default='rbd',
               help='The RADOS pool in which rbd volumes are stored'),
    cfg.StrOpt('images_rbd_ceph_conf',
               default='',  # default determined by librados
               help='Path to the ceph configuration file to use'),
    cfg.StrOpt('hw_disk_discard',
               choices=('ignore', 'unmap'),
               help='Discard option for nova managed disks. Need'
                    ' Libvirt(1.0.6) Qemu1.5 (raw format) Qemu1.6(qcow2'
                    ' format)'),
]

libvirt_imagecache_opts = [
    cfg.StrOpt('image_info_filename_pattern',
               default='$instances_path/$image_cache_subdirectory_name/'
                       '%(image)s.info',
               help='Allows image information files to be stored in '
                    'non-standard locations',
               deprecated_for_removal=True,
               deprecated_reason='Image info files are no longer used by the '
                                 'image cache'),
    cfg.IntOpt('remove_unused_resized_minimum_age_seconds',
               default=3600,
               help='Unused resized base images younger than this will not be '
                    'removed'),
    cfg.BoolOpt('checksum_base_images',
                default=False,
                help='Write a checksum for files in _base to disk',
                deprecated_for_removal=True,
                deprecated_reason='The image cache no longer periodically '
                                  'calculates checksums of stored images. '
                                  'Data integrity can be checked at the block '
                                  'or filesystem level.'),
    cfg.IntOpt('checksum_interval_seconds',
               default=3600,
               help='How frequently to checksum base images',
               deprecated_for_removal=True,
               deprecated_reason='The image cache no longer periodically '
                                 'calculates checksums of stored images. '
                                 'Data integrity can be checked at the block '
                                 'or filesystem level.'),
]

libvirt_lvm_opts = [
    cfg.StrOpt('volume_clear',
               default='zero',
               choices=('none', 'zero', 'shred'),
               help='Method used to wipe old volumes.'),
    cfg.IntOpt('volume_clear_size',
               default=0,
               help='Size in MiB to wipe at start of old volumes. 0 => all'),
]

libvirt_utils_opts = [
    cfg.BoolOpt('snapshot_compression',
                default=False,
                help='Compress snapshot images when possible. This '
                     'currently applies exclusively to qcow2 images'),
]

libvirt_vif_opts = [
    cfg.BoolOpt('use_virtio_for_bridges',
                default=True,
                help='Use virtio for bridge interfaces with KVM/QEMU'),
]

libvirt_volume_opts = [
    cfg.ListOpt('qemu_allowed_storage_drivers',
                default=[],
                help='Protocols listed here will be accessed directly '
                     'from QEMU. Currently supported protocols: [gluster]'),
    cfg.BoolOpt('volume_use_multipath',
                default=False,
                help='Use multipath connection of the iSCSI or FC volume',
                deprecated_name='iscsi_use_multipath'),
]

libvirt_volume_aoe_opts = [
    cfg.IntOpt('num_aoe_discover_tries',
               default=3,
               help='Number of times to rediscover AoE target to find volume'),
]

libvirt_volume_glusterfs_opts = [
    cfg.StrOpt('glusterfs_mount_point_base',
               default=paths.state_path_def('mnt'),
               help='Directory where the glusterfs volume is mounted on the '
                    'compute node'),
]

libvirt_volume_iscsi_opts = [
    cfg.IntOpt('num_iscsi_scan_tries',
               default=5,
               help='Number of times to rescan iSCSI target to find volume'),
    cfg.StrOpt('iscsi_iface',
               deprecated_name='iscsi_transport',
               help='The iSCSI transport iface to use to connect to target in '
                    'case offload support is desired. Default format is of '
                    'the form <transport_name>.<hwaddress> where '
                    '<transport_name> is one of (be2iscsi, bnx2i, cxgb3i, '
                    'cxgb4i, qla4xxx, ocs) and <hwaddress> is the MAC address '
                    'of the interface and can be generated via the '
                    'iscsiadm -m iface command. Do not confuse the '
                    'iscsi_iface parameter to be provided here with the '
                    'actual transport name.'),
    # iser is also supported, but use LibvirtISERVolumeDriver
    # instead
]

libvirt_volume_iser_opts = [
    cfg.IntOpt('num_iser_scan_tries',
               default=5,
               help='Number of times to rescan iSER target to find volume'),
    cfg.BoolOpt('iser_use_multipath',
                default=False,
                help='Use multipath connection of the iSER volume'),
]

libvirt_volume_net_opts = [
    cfg.StrOpt('rbd_user',
               help='The RADOS client name for accessing rbd volumes'),
    cfg.StrOpt('rbd_secret_uuid',
               help='The libvirt UUID of the secret for the rbd_user'
                    'volumes'),
]

libvirt_volume_nfs_opts = [
    cfg.StrOpt('nfs_mount_point_base',
               default=paths.state_path_def('mnt'),
               help='Directory where the NFS volume is mounted on the'
                    ' compute node'),
    cfg.StrOpt('nfs_mount_options',
               help='Mount options passed to the NFS client. See section '
                    'of the nfs man page for details'),
]

libvirt_volume_quobyte_opts = [
    cfg.StrOpt('quobyte_mount_point_base',
               default=paths.state_path_def('mnt'),
               help='Directory where the Quobyte volume is mounted on the '
                    'compute node'),
    cfg.StrOpt('quobyte_client_cfg',
               help='Path to a Quobyte Client configuration file.'),
]

libvirt_volume_scality_opts = [
    cfg.StrOpt('scality_sofs_config',
               help='Path or URL to Scality SOFS configuration file'),
    cfg.StrOpt('scality_sofs_mount_point',
               default='$state_path/scality',
               help='Base dir where Scality SOFS shall be mounted'),
]

libvirt_volume_smbfs_opts = [
    cfg.StrOpt('smbfs_mount_point_base',
               default=paths.state_path_def('mnt'),
               help='Directory where the SMBFS shares are mounted on the '
                    'compute node'),
    cfg.StrOpt('smbfs_mount_options',
               default='',
               help='Mount options passed to the SMBFS client. See '
                    'mount.cifs man page for details. Note that the '
                    'libvirt-qemu uid and gid must be specified.'),
]

libvirt_remotefs_opts = [
    cfg.StrOpt('remote_filesystem_transport',
               default='ssh',
               choices=('ssh', 'rsync'),
               help='Use ssh or rsync transport for creating, copying, '
                    'removing files on the remote host.'),
]

libvirt_volume_vzstorage_opts = [
    cfg.StrOpt('vzstorage_mount_point_base',
               default=paths.state_path_def('mnt'),
               help="""
Directory where the Virtuozzo Storage clusters are mounted on the compute node.

This option defines non-standard mountpoint for Vzstorage cluster.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_* group of parameters
"""
              ),
    cfg.StrOpt('vzstorage_mount_user',
               default='stack',
               help="""
Mount owner user name.

This option defines the owner user of Vzstorage cluster mountpoint.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_* group of parameters
"""
              ),
    cfg.StrOpt('vzstorage_mount_group',
               default='qemu',
               help="""
Mount owner group name.

This option defines the owner group of Vzstorage cluster mountpoint.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_* group of parameters
"""
              ),
    cfg.StrOpt('vzstorage_mount_perms',
               default='0770',
               help="""
Mount access mode.

This option defines the access bits of Vzstorage cluster mountpoint,
in the format similar to one of chmod(1) utility, like this: 0770.
It consists of one to four digits ranging from 0 to 7, with missing
lead digits assumed to be 0's.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_* group of parameters
"""
              ),
    cfg.StrOpt('vzstorage_log_path',
               default='/var/log/pstorage/%(cluster_name)s/nova.log.gz',
               help="""
Path to vzstorage client log.

This option defines the log of cluster operations,
it should include "%(cluster_name)s" template to separate
logs from multiple shares.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_opts may include more detailed logging options.
"""
              ),
    cfg.StrOpt('vzstorage_cache_path',
               default=None,
               help="""
Path to the SSD cache file.

You can attach an SSD drive to a client and configure the drive to store
a local cache of frequently accessed data. By having a local cache on a
client's SSD drive, you can increase the overall cluster performance by
up to 10 and more times.
WARNING! There is a lot of SSD models which are not server grade and
may loose arbitrary set of data changes on power loss.
Such SSDs should not be used in Vstorage and are dangerous as may lead
to data corruptions and inconsistencies. Please consult with the manual
on which SSD models are known to be safe or verify it using
vstorage-hwflush-check(1) utility.

This option defines the path which should include "%(cluster_name)s"
template to separate caches from multiple shares.

* Services that use this:

    ``nova-compute``

* Related options:

    vzstorage_mount_opts may include more detailed cache options.
"""
              ),
    cfg.ListOpt('vzstorage_mount_opts',
                default=[],
               help="""
Extra mount options for pstorage-mount

For full description of them, see
https://static.openvz.org/vz-man/man1/pstorage-mount.1.gz.html
Format is a python string representation of arguments list, like:
"[\'-v\', \'-R\', \'500\']"
Shouldn\'t include -c, -l, -C, -u, -g and -m as those have
explicit vzstorage_* options.

* Services that use this:

    ``nova-compute``

* Related options:

    All other vzstorage_* options
"""
              ),
]

ALL_OPTS = list(itertools.chain(
    libvirt_general_opts,
    libvirt_imagebackend_opts,
    libvirt_imagecache_opts,
    libvirt_lvm_opts,
    libvirt_utils_opts,
    libvirt_vif_opts,
    libvirt_volume_opts,
    libvirt_volume_aoe_opts,
    libvirt_volume_glusterfs_opts,
    libvirt_volume_iscsi_opts,
    libvirt_volume_iser_opts,
    libvirt_volume_net_opts,
    libvirt_volume_nfs_opts,
    libvirt_volume_quobyte_opts,
    libvirt_volume_scality_opts,
    libvirt_volume_smbfs_opts,
    libvirt_remotefs_opts,
    libvirt_volume_vzstorage_opts,
))


def register_opts(conf):
    conf.register_group(libvirt_group)
    conf.register_opts(ALL_OPTS, group=libvirt_group)


def list_opts():
    return {libvirt_group: ALL_OPTS}
