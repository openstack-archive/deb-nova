# Copyright 2012 Grid Dynamics
# Copyright 2013 Inktank Storage, Inc.
# Copyright 2014 Mirantis, Inc.
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

from eventlet import tpool
from six.moves import urllib

try:
    import rados
    import rbd
except ImportError:
    rados = None
    rbd = None

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import units

from nova.compute import task_states
from nova import exception
from nova.i18n import _
from nova.i18n import _LE
from nova.i18n import _LW
from nova import utils
from nova.virt.libvirt import utils as libvirt_utils

LOG = logging.getLogger(__name__)


class RbdProxy(object):
    """A wrapper around rbd.RBD class instance to avoid blocking of process.

    Offloads all calls to rbd.RBD class methods to native OS threads, so that
    we do not block the whole process while executing the librbd code.

    """

    def __init__(self):
        self._rbd = tpool.Proxy(rbd.RBD())

    def __getattr__(self, attr):
        return getattr(self._rbd, attr)


class RBDVolumeProxy(object):
    """Context manager for dealing with an existing rbd volume.

    This handles connecting to rados and opening an ioctx automatically, and
    otherwise acts like a librbd Image object.

    The underlying librados client and ioctx can be accessed as the attributes
    'client' and 'ioctx'.
    """
    def __init__(self, driver, name, pool=None, snapshot=None,
                 read_only=False):
        client, ioctx = driver._connect_to_rados(pool)
        try:
            snap_name = snapshot.encode('utf8') if snapshot else None
            self.volume = tpool.Proxy(rbd.Image(ioctx, name.encode('utf8'),
                                      snapshot=snap_name,
                                      read_only=read_only))
        except rbd.ImageNotFound:
            with excutils.save_and_reraise_exception():
                LOG.debug("rbd image %s does not exist", name)
                driver._disconnect_from_rados(client, ioctx)
        except rbd.Error:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("error opening rbd image %s"), name)
                driver._disconnect_from_rados(client, ioctx)

        self.driver = driver
        self.client = client
        self.ioctx = ioctx

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        try:
            self.volume.close()
        finally:
            self.driver._disconnect_from_rados(self.client, self.ioctx)

    def __getattr__(self, attrib):
        return getattr(self.volume, attrib)


class RADOSClient(object):
    """Context manager to simplify error handling for connecting to ceph."""
    def __init__(self, driver, pool=None):
        self.driver = driver
        self.cluster, self.ioctx = driver._connect_to_rados(pool)

    def __enter__(self):
        return self

    def __exit__(self, type_, value, traceback):
        self.driver._disconnect_from_rados(self.cluster, self.ioctx)

    @property
    def features(self):
        features = self.cluster.conf_get('rbd_default_features')
        if ((features is None) or (int(features) == 0)):
            features = rbd.RBD_FEATURE_LAYERING
        return int(features)


class RBDDriver(object):

    def __init__(self, pool, ceph_conf, rbd_user):
        self.pool = pool.encode('utf8')
        # NOTE(angdraug): rados.Rados fails to connect if ceph_conf is None:
        # https://github.com/ceph/ceph/pull/1787
        self.ceph_conf = ceph_conf.encode('utf8') if ceph_conf else ''
        self.rbd_user = rbd_user.encode('utf8') if rbd_user else None
        if rbd is None:
            raise RuntimeError(_('rbd python libraries not found'))

    def _connect_to_rados(self, pool=None):
        client = rados.Rados(rados_id=self.rbd_user,
                                  conffile=self.ceph_conf)
        try:
            client.connect()
            pool_to_open = pool or self.pool
            ioctx = client.open_ioctx(pool_to_open.encode('utf-8'))
            return client, ioctx
        except rados.Error:
            # shutdown cannot raise an exception
            client.shutdown()
            raise

    def _disconnect_from_rados(self, client, ioctx):
        # closing an ioctx cannot raise an exception
        ioctx.close()
        client.shutdown()

    def ceph_args(self):
        """List of command line parameters to be passed to ceph commands to
           reflect RBDDriver configuration such as RBD user name and location
           of ceph.conf.
        """
        args = []
        if self.rbd_user:
            args.extend(['--id', self.rbd_user])
        if self.ceph_conf:
            args.extend(['--conf', self.ceph_conf])
        return args

    def get_mon_addrs(self):
        args = ['ceph', 'mon', 'dump', '--format=json'] + self.ceph_args()
        out, _ = utils.execute(*args)
        lines = out.split('\n')
        if lines[0].startswith('dumped monmap epoch'):
            lines = lines[1:]
        monmap = jsonutils.loads('\n'.join(lines))
        addrs = [mon['addr'] for mon in monmap['mons']]
        hosts = []
        ports = []
        for addr in addrs:
            host_port = addr[:addr.rindex('/')]
            host, port = host_port.rsplit(':', 1)
            hosts.append(host.strip('[]'))
            ports.append(port)
        return hosts, ports

    def parse_url(self, url):
        prefix = 'rbd://'
        if not url.startswith(prefix):
            reason = _('Not stored in rbd')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        pieces = map(urllib.parse.unquote, url[len(prefix):].split('/'))
        if '' in pieces:
            reason = _('Blank components')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        if len(pieces) != 4:
            reason = _('Not an rbd snapshot')
            raise exception.ImageUnacceptable(image_id=url, reason=reason)
        return pieces

    def get_fsid(self):
        with RADOSClient(self) as client:
            return client.cluster.get_fsid()

    def is_cloneable(self, image_location, image_meta):
        url = image_location['url']
        try:
            fsid, pool, image, snapshot = self.parse_url(url)
        except exception.ImageUnacceptable as e:
            LOG.debug('not cloneable: %s', e)
            return False

        if self.get_fsid() != fsid:
            reason = '%s is in a different ceph cluster' % url
            LOG.debug(reason)
            return False

        if image_meta.get('disk_format') != 'raw':
            reason = ("rbd image clone requires image format to be "
                      "'raw' but image {0} is '{1}'").format(
                          url, image_meta.get('disk_format'))
            LOG.debug(reason)
            return False

        # check that we can read the image
        try:
            return self.exists(image, pool=pool, snapshot=snapshot)
        except rbd.Error as e:
            LOG.debug('Unable to open image %(loc)s: %(err)s' %
                      dict(loc=url, err=e))
            return False

    def clone(self, image_location, dest_name, dest_pool=None):
        _fsid, pool, image, snapshot = self.parse_url(
                image_location['url'])
        LOG.debug('cloning %(pool)s/%(img)s@%(snap)s to '
                  '%(dest_pool)s/%(dest_name)s',
                  dict(pool=pool, img=image, snap=snapshot,
                       dest_pool=dest_pool, dest_name=dest_name))
        with RADOSClient(self, str(pool)) as src_client:
            with RADOSClient(self, dest_pool) as dest_client:
                try:
                    RbdProxy().clone(src_client.ioctx,
                                     image.encode('utf-8'),
                                     snapshot.encode('utf-8'),
                                     dest_client.ioctx,
                                     str(dest_name),
                                     features=src_client.features)
                except rbd.PermissionError:
                    raise exception.Forbidden(_('no write permission on '
                                                'storage pool %s') % dest_pool)

    def size(self, name):
        with RBDVolumeProxy(self, name, read_only=True) as vol:
            return vol.size()

    def resize(self, name, size):
        """Resize RBD volume.

        :name: Name of RBD object
        :size: New size in bytes
        """
        LOG.debug('resizing rbd image %s to %d', name, size)
        with RBDVolumeProxy(self, name) as vol:
            vol.resize(size)

    def parent_info(self, volume, pool=None):
        """Returns the pool, image and snapshot name for the parent of an
        RBD volume.

        :volume: Name of RBD object
        :pool: Name of pool
        """
        try:
            with RBDVolumeProxy(self, str(volume), pool=pool,
                                read_only=True) as vol:
                return vol.parent_info()
        except rbd.ImageNotFound:
            raise exception.ImageUnacceptable(_("no usable parent snapshot "
                                                "for volume %s") % volume)

    def flatten(self, volume, pool=None):
        """"Flattens" a snapshotted image with the parents' data,
        effectively detaching it from the parent.

        :volume: Name of RBD object
        :pool: Name of pool
        """
        LOG.debug('flattening %(pool)s/%(vol)s', dict(pool=pool, vol=volume))
        with RBDVolumeProxy(self, str(volume), pool=pool) as vol:
            vol.flatten()

    def exists(self, name, pool=None, snapshot=None):
        try:
            with RBDVolumeProxy(self, name,
                                pool=pool,
                                snapshot=snapshot,
                                read_only=True):
                return True
        except rbd.ImageNotFound:
            return False

    def remove_image(self, name):
        """Remove RBD volume

        :name: Name of RBD volume
        """
        with RADOSClient(self, self.pool) as client:
            try:
                RbdProxy().remove(client.ioctx, name)
            except rbd.ImageNotFound:
                LOG.warning(_LW('image %(volume)s in pool %(pool)s can not be '
                             'found, failed to remove'),
                            {'volume': name, 'pool': self.pool})
            except rbd.ImageHasSnapshots:
                LOG.error(_LE('image %(volume)s in pool %(pool)s has '
                              'snapshots, failed to remove'),
                            {'volume': name, 'pool': self.pool})

    def import_image(self, base, name):
        """Import RBD volume from image file.

        Uses the command line import instead of librbd since rbd import
        command detects zeroes to preserve sparseness in the image.

        :base: Path to image file
        :name: Name of RBD volume
        """
        args = ['--pool', self.pool, base, name]
        # Image format 2 supports cloning,
        # in stable ceph rbd release default is not 2,
        # we need to use it explicitly.
        args += ['--image-format=2']
        args += self.ceph_args()
        utils.execute('rbd', 'import', *args)

    def _destroy_volume(self, client, volume, pool=None):
        """Destroy an RBD volume, retrying as needed.
        """
        def _cleanup_vol(ioctx, volume, retryctx):
            try:
                RbdProxy().remove(ioctx, volume)
                raise loopingcall.LoopingCallDone(retvalue=False)
            except rbd.ImageHasSnapshots:
                self.remove_snap(volume, libvirt_utils.RESIZE_SNAPSHOT_NAME,
                                 ignore_errors=True)
            except (rbd.ImageBusy, rbd.ImageHasSnapshots):
                LOG.warning(_LW('rbd remove %(volume)s in pool %(pool)s '
                             'failed'),
                         {'volume': volume, 'pool': self.pool})
            retryctx['retries'] -= 1
            if retryctx['retries'] <= 0:
                raise loopingcall.LoopingCallDone()

        # NOTE(danms): We let it go for ten seconds
        retryctx = {'retries': 10}
        timer = loopingcall.FixedIntervalLoopingCall(
            _cleanup_vol, client.ioctx, volume, retryctx)
        timed_out = timer.start(interval=1).wait()
        if timed_out:
            # NOTE(danms): Run this again to propagate the error, but
            # if it succeeds, don't raise the loopingcall exception
            try:
                _cleanup_vol(client.ioctx, volume, retryctx)
            except loopingcall.LoopingCallDone:
                pass

    def cleanup_volumes(self, instance):
        with RADOSClient(self, self.pool) as client:

            def belongs_to_instance(disk):
                # NOTE(nic): On revert_resize, the cleanup steps for the root
                # volume are handled with an "rbd snap rollback" command,
                # and none of this is needed (and is, in fact, harmful) so
                # filter out non-ephemerals from the list
                if instance.task_state == task_states.RESIZE_REVERTING:
                    return (disk.startswith(instance.uuid) and
                            disk.endswith('disk.local'))
                else:
                    return disk.startswith(instance.uuid)

            volumes = RbdProxy().list(client.ioctx)
            for volume in filter(belongs_to_instance, volumes):
                self._destroy_volume(client, volume)

    def get_pool_info(self):
        with RADOSClient(self) as client:
            stats = client.cluster.get_cluster_stats()
            return {'total': stats['kb'] * units.Ki,
                    'free': stats['kb_avail'] * units.Ki,
                    'used': stats['kb_used'] * units.Ki}

    def create_snap(self, volume, name, pool=None, protect=False):
        """Create a snapshot of an RBD volume.

        :volume: Name of RBD object
        :name: Name of snapshot
        :pool: Name of pool
        :protect: Set the snapshot to "protected"
        """
        LOG.debug('creating snapshot(%(snap)s) on rbd image(%(img)s)',
                  {'snap': name, 'img': volume})
        with RBDVolumeProxy(self, str(volume), pool=pool) as vol:
            vol.create_snap(name)
            if protect and not vol.is_protected_snap(name):
                vol.protect_snap(name)

    def remove_snap(self, volume, name, ignore_errors=False, pool=None,
                    force=False):
        """Removes a snapshot from an RBD volume.

        :volume: Name of RBD object
        :name: Name of snapshot
        :ignore_errors: whether or not to log warnings on failures
        :pool: Name of pool
        :force: Remove snapshot even if it is protected
        """
        with RBDVolumeProxy(self, str(volume), pool=pool) as vol:
            if name in [snap.get('name', '') for snap in vol.list_snaps()]:
                if vol.is_protected_snap(name):
                    if force:
                        vol.unprotect_snap(name)
                    elif not ignore_errors:
                        LOG.warning(_LW('snapshot(%(name)s) on rbd '
                                        'image(%(img)s) is protected, '
                                        'skipping'),
                                    {'name': name, 'img': volume})
                        return
                LOG.debug('removing snapshot(%(name)s) on rbd image(%(img)s)',
                          {'name': name, 'img': volume})
                vol.remove_snap(name)
            elif not ignore_errors:
                LOG.warning(_LW('no snapshot(%(name)s) found on rbd '
                                'image(%(img)s)'),
                            {'name': name, 'img': volume})

    def rollback_to_snap(self, volume, name):
        """Revert an RBD volume to its contents at a snapshot.

        :volume: Name of RBD object
        :name: Name of snapshot
        """
        with RBDVolumeProxy(self, volume) as vol:
            if name in [snap.get('name', '') for snap in vol.list_snaps()]:
                LOG.debug('rolling back rbd image(%(img)s) to '
                          'snapshot(%(snap)s)', {'snap': name, 'img': volume})
                vol.rollback_to_snap(name)
            else:
                raise exception.SnapshotNotFound(snapshot_id=name)

    def destroy_volume(self, volume, pool=None):
        """A one-shot version of cleanup_volumes()
        """
        with RADOSClient(self, pool) as client:
            self._destroy_volume(client, volume)
