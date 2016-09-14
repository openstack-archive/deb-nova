#    Copyright 2013 IBM Corp.
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

from oslo_log import log as logging
from oslo_utils import versionutils

from nova import availability_zones
from nova import db
from nova import exception
from nova.i18n import _LW
from nova.notifications.objects import base as notification
from nova.notifications.objects import service as service_notification
from nova import objects
from nova.objects import base
from nova.objects import fields


LOG = logging.getLogger(__name__)


# NOTE(danms): This is the global service version counter
SERVICE_VERSION = 15


# NOTE(danms): This is our SERVICE_VERSION history. The idea is that any
# time we bump the version, we will put an entry here to record the change,
# along with any pertinent data. For things that we can programatically
# detect that need a bump, we put something in _collect_things() below to
# assemble a dict of things we can check. For example, we pretty much always
# want to consider the compute RPC API version a thing that requires a service
# bump so that we can drive version pins from it. We could include other
# service RPC versions at some point, minimum object versions, etc.
#
# The TestServiceVersion test will fail if the calculated set of
# things differs from the value in the last item of the list below,
# indicating that a version bump is needed.
#
# Also note that there are other reasons we may want to bump this,
# which will not be caught by the test. An example of this would be
# triggering (or disabling) an online data migration once all services
# in the cluster are at the same level.
#
# If a version bump is required for something mechanical, just document
# that generic thing here (like compute RPC version bumps). No need to
# replicate the details from compute/rpcapi.py here. However, for more
# complex service interactions, extra detail should be provided
SERVICE_VERSION_HISTORY = (
    # Version 0: Pre-history
    {'compute_rpc': '4.0'},

    # Version 1: Introduction of SERVICE_VERSION
    {'compute_rpc': '4.4'},
    # Version 2: Compute RPC version 4.5
    {'compute_rpc': '4.5'},
    # Version 3: Compute RPC version 4.6
    {'compute_rpc': '4.6'},
    # Version 4: Add PciDevice.parent_addr (data migration needed)
    {'compute_rpc': '4.6'},
    # Version 5: Compute RPC version 4.7
    {'compute_rpc': '4.7'},
    # Version 6: Compute RPC version 4.8
    {'compute_rpc': '4.8'},
    # Version 7: Compute RPC version 4.9
    {'compute_rpc': '4.9'},
    # Version 8: Compute RPC version 4.10
    {'compute_rpc': '4.10'},
    # Version 9: Compute RPC version 4.11
    {'compute_rpc': '4.11'},
    # Version 10: Compute node conversion to Inventories
    {'compute_rpc': '4.11'},
    # Version 11: Compute RPC version 4.12
    {'compute_rpc': '4.12'},
    # Version 12: The network APIs and compute manager support a NetworkRequest
    # object where the network_id value is 'auto' or 'none'. BuildRequest
    # objects are populated by nova-api during instance boot.
    {'compute_rpc': '4.12'},
    # Version 13: Compute RPC version 4.13
    {'compute_rpc': '4.13'},
    # Version 14: The compute manager supports setting device tags.
    {'compute_rpc': '4.13'},
    # Version 15: Indicate that nova-conductor will stop a boot if BuildRequest
    # is deleted before RPC to nova-compute.
    {'compute_rpc': '4.13'},
)


# TODO(berrange): Remove NovaObjectDictCompat
@base.NovaObjectRegistry.register
class Service(base.NovaPersistentObject, base.NovaObject,
              base.NovaObjectDictCompat):
    # Version 1.0: Initial version
    # Version 1.1: Added compute_node nested object
    # Version 1.2: String attributes updated to support unicode
    # Version 1.3: ComputeNode version 1.5
    # Version 1.4: Added use_slave to get_by_compute_host
    # Version 1.5: ComputeNode version 1.6
    # Version 1.6: ComputeNode version 1.7
    # Version 1.7: ComputeNode version 1.8
    # Version 1.8: ComputeNode version 1.9
    # Version 1.9: ComputeNode version 1.10
    # Version 1.10: Changes behaviour of loading compute_node
    # Version 1.11: Added get_by_host_and_binary
    # Version 1.12: ComputeNode version 1.11
    # Version 1.13: Added last_seen_up
    # Version 1.14: Added forced_down
    # Version 1.15: ComputeNode version 1.12
    # Version 1.16: Added version
    # Version 1.17: ComputeNode version 1.13
    # Version 1.18: ComputeNode version 1.14
    # Version 1.19: Added get_minimum_version()
    # Version 1.20: Added get_minimum_version_multi()
    VERSION = '1.20'

    fields = {
        'id': fields.IntegerField(read_only=True),
        'host': fields.StringField(nullable=True),
        'binary': fields.StringField(nullable=True),
        'topic': fields.StringField(nullable=True),
        'report_count': fields.IntegerField(),
        'disabled': fields.BooleanField(),
        'disabled_reason': fields.StringField(nullable=True),
        'availability_zone': fields.StringField(nullable=True),
        'compute_node': fields.ObjectField('ComputeNode'),
        'last_seen_up': fields.DateTimeField(nullable=True),
        'forced_down': fields.BooleanField(),
        'version': fields.IntegerField(),
    }

    _MIN_VERSION_CACHE = {}
    _SERVICE_VERSION_CACHING = False

    def __init__(self, *args, **kwargs):
        # NOTE(danms): We're going against the rules here and overriding
        # init. The reason is that we want to *ensure* that we're always
        # setting the current service version on our objects, overriding
        # whatever else might be set in the database, or otherwise (which
        # is the normal reason not to override init).
        #
        # We also need to do this here so that it's set on the client side
        # all the time, such that create() and save() operations will
        # include the current service version.
        if 'version' in kwargs:
            raise exception.ObjectActionError(
                action='init',
                reason='Version field is immutable')

        super(Service, self).__init__(*args, **kwargs)
        self.version = SERVICE_VERSION

    def obj_make_compatible_from_manifest(self, primitive, target_version,
                                          version_manifest):
        super(Service, self).obj_make_compatible_from_manifest(
            primitive, target_version, version_manifest)
        _target_version = versionutils.convert_version_to_tuple(target_version)
        if _target_version < (1, 16) and 'version' in primitive:
            del primitive['version']
        if _target_version < (1, 14) and 'forced_down' in primitive:
            del primitive['forced_down']
        if _target_version < (1, 13) and 'last_seen_up' in primitive:
            del primitive['last_seen_up']
        if _target_version < (1, 10):
            # service.compute_node was not lazy-loaded, we need to provide it
            # when called
            self._do_compute_node(self._context, primitive,
                                  version_manifest)

    def _do_compute_node(self, context, primitive, version_manifest):
        try:
            target_version = version_manifest['ComputeNode']
            # NOTE(sbauza): Some drivers (VMware, Ironic) can have multiple
            # nodes for the same service, but for keeping same behaviour,
            # returning only the first elem of the list
            compute = objects.ComputeNodeList.get_all_by_host(
                context, primitive['host'])[0]
        except Exception:
            return
        primitive['compute_node'] = compute.obj_to_primitive(
            target_version=target_version,
            version_manifest=version_manifest)

    @staticmethod
    def _from_db_object(context, service, db_service):
        allow_missing = ('availability_zone',)
        for key in service.fields:
            if key in allow_missing and key not in db_service:
                continue
            if key == 'compute_node':
                #  NOTE(sbauza); We want to only lazy-load compute_node
                continue
            elif key == 'version':
                # NOTE(danms): Special handling of the version field, since
                # it is read_only and set in our init.
                setattr(service, base.get_attrname(key), db_service[key])
            else:
                service[key] = db_service[key]
        service._context = context
        service.obj_reset_changes()
        return service

    def obj_load_attr(self, attrname):
        if not self._context:
            raise exception.OrphanedObjectError(method='obj_load_attr',
                                                objtype=self.obj_name())

        LOG.debug("Lazy-loading '%(attr)s' on %(name)s id %(id)s",
                  {'attr': attrname,
                   'name': self.obj_name(),
                   'id': self.id,
                   })
        if attrname != 'compute_node':
            raise exception.ObjectActionError(
                action='obj_load_attr',
                reason='attribute %s not lazy-loadable' % attrname)
        if self.binary == 'nova-compute':
            # Only n-cpu services have attached compute_node(s)
            compute_nodes = objects.ComputeNodeList.get_all_by_host(
                self._context, self.host)
        else:
            # NOTE(sbauza); Previous behaviour was raising a ServiceNotFound,
            # we keep it for backwards compatibility
            raise exception.ServiceNotFound(service_id=self.id)
        # NOTE(sbauza): Some drivers (VMware, Ironic) can have multiple nodes
        # for the same service, but for keeping same behaviour, returning only
        # the first elem of the list
        self.compute_node = compute_nodes[0]

    @base.remotable_classmethod
    def get_by_id(cls, context, service_id):
        db_service = db.service_get(context, service_id)
        return cls._from_db_object(context, cls(), db_service)

    @base.remotable_classmethod
    def get_by_host_and_topic(cls, context, host, topic):
        db_service = db.service_get_by_host_and_topic(context, host, topic)
        return cls._from_db_object(context, cls(), db_service)

    @base.remotable_classmethod
    def get_by_host_and_binary(cls, context, host, binary):
        try:
            db_service = db.service_get_by_host_and_binary(context,
                                                           host, binary)
        except exception.HostBinaryNotFound:
            return
        return cls._from_db_object(context, cls(), db_service)

    @staticmethod
    @db.select_db_reader_mode
    def _db_service_get_by_compute_host(context, host, use_slave=False):
        return db.service_get_by_compute_host(context, host)

    @base.remotable_classmethod
    def get_by_compute_host(cls, context, host, use_slave=False):
        db_service = cls._db_service_get_by_compute_host(context, host,
                                                         use_slave=use_slave)
        return cls._from_db_object(context, cls(), db_service)

    # NOTE(ndipanov): This is deprecated and should be removed on the next
    # major version bump
    @base.remotable_classmethod
    def get_by_args(cls, context, host, binary):
        db_service = db.service_get_by_host_and_binary(context, host, binary)
        return cls._from_db_object(context, cls(), db_service)

    def _check_minimum_version(self):
        """Enforce that we are not older that the minimum version.

        This is a loose check to avoid creating or updating our service
        record if we would do so with a version that is older that the current
        minimum of all services. This could happen if we were started with
        older code by accident, either due to a rollback or an old and
        un-updated node suddenly coming back onto the network.

        There is technically a race here between the check and the update,
        but since the minimum version should always roll forward and never
        backwards, we don't need to worry about doing it atomically. Further,
        the consequence for getting this wrong is minor, in that we'll just
        fail to send messages that other services understand.
        """
        if not self.obj_attr_is_set('version'):
            return
        if not self.obj_attr_is_set('binary'):
            return
        minver = self.get_minimum_version(self._context, self.binary)
        if minver > self.version:
            raise exception.ServiceTooOld(thisver=self.version,
                                          minver=minver)

    @base.remotable
    def create(self):
        if self.obj_attr_is_set('id'):
            raise exception.ObjectActionError(action='create',
                                              reason='already created')
        self._check_minimum_version()
        updates = self.obj_get_changes()
        db_service = db.service_create(self._context, updates)
        self._from_db_object(self._context, self, db_service)

    @base.remotable
    def save(self):
        updates = self.obj_get_changes()
        updates.pop('id', None)
        self._check_minimum_version()
        db_service = db.service_update(self._context, self.id, updates)
        self._from_db_object(self._context, self, db_service)

        self._send_status_update_notification(updates)

    def _send_status_update_notification(self, updates):
        # Note(gibi): We do not trigger notification on version as that field
        # is always dirty, which would cause that nova sends notification on
        # every other field change. See the comment in save() too.
        if set(updates.keys()).intersection(
                {'disabled', 'disabled_reason', 'forced_down'}):
            payload = service_notification.ServiceStatusPayload(self)
            service_notification.ServiceStatusNotification(
                publisher=notification.NotificationPublisher.from_service_obj(
                    self),
                event_type=notification.EventType(
                    object='service',
                    action=fields.NotificationAction.UPDATE),
                priority=fields.NotificationPriority.INFO,
                payload=payload).emit(self._context)

    @base.remotable
    def destroy(self):
        db.service_destroy(self._context, self.id)

    @classmethod
    def enable_min_version_cache(cls):
        cls.clear_min_version_cache()
        cls._SERVICE_VERSION_CACHING = True

    @classmethod
    def clear_min_version_cache(cls):
        cls._MIN_VERSION_CACHE = {}

    @staticmethod
    @db.select_db_reader_mode
    def _db_service_get_minimum_version(context, binaries, use_slave=False):
        return db.service_get_minimum_version(context, binaries)

    @base.remotable_classmethod
    def get_minimum_version_multi(cls, context, binaries, use_slave=False):
        if not all(binary.startswith('nova-') for binary in binaries):
            LOG.warning(_LW('get_minimum_version called with likely-incorrect '
                            'binaries `%s\''), ','.join(binaries))
            raise exception.ObjectActionError(action='get_minimum_version',
                                              reason='Invalid binary prefix')

        if (not cls._SERVICE_VERSION_CACHING or
              any(binary not in cls._MIN_VERSION_CACHE
                  for binary in binaries)):
            min_versions = cls._db_service_get_minimum_version(
                context, binaries, use_slave=use_slave)
            if min_versions:
                min_versions = {binary: version or 0
                                for binary, version in
                                min_versions.items()}
                cls._MIN_VERSION_CACHE.update(min_versions)
        else:
            min_versions = {binary: cls._MIN_VERSION_CACHE[binary]
                            for binary in binaries}

        if min_versions:
            version = min(min_versions.values())
        else:
            version = 0
        # NOTE(danms): Since our return value is not controlled by object
        # schema, be explicit here.
        version = int(version)

        return version

    @base.remotable_classmethod
    def get_minimum_version(cls, context, binary, use_slave=False):
        return cls.get_minimum_version_multi(context, [binary],
                                             use_slave=use_slave)


@base.NovaObjectRegistry.register
class ServiceList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial version
    #              Service <= version 1.2
    # Version 1.1  Service version 1.3
    # Version 1.2: Service version 1.4
    # Version 1.3: Service version 1.5
    # Version 1.4: Service version 1.6
    # Version 1.5: Service version 1.7
    # Version 1.6: Service version 1.8
    # Version 1.7: Service version 1.9
    # Version 1.8: Service version 1.10
    # Version 1.9: Added get_by_binary() and Service version 1.11
    # Version 1.10: Service version 1.12
    # Version 1.11: Service version 1.13
    # Version 1.12: Service version 1.14
    # Version 1.13: Service version 1.15
    # Version 1.14: Service version 1.16
    # Version 1.15: Service version 1.17
    # Version 1.16: Service version 1.18
    # Version 1.17: Service version 1.19
    # Version 1.18: Added include_disabled parameter to get_by_binary()
    # Version 1.19: Added get_all_computes_by_hv_type()
    VERSION = '1.19'

    fields = {
        'objects': fields.ListOfObjectsField('Service'),
        }

    @base.remotable_classmethod
    def get_by_topic(cls, context, topic):
        db_services = db.service_get_all_by_topic(context, topic)
        return base.obj_make_list(context, cls(context), objects.Service,
                                  db_services)

    # NOTE(paul-carlton2): In v2.0 of the object the include_disabled flag
    # will be removed so both enabled and disabled hosts are returned
    @base.remotable_classmethod
    def get_by_binary(cls, context, binary, include_disabled=False):
        db_services = db.service_get_all_by_binary(
            context, binary, include_disabled=include_disabled)
        return base.obj_make_list(context, cls(context), objects.Service,
                                  db_services)

    @base.remotable_classmethod
    def get_by_host(cls, context, host):
        db_services = db.service_get_all_by_host(context, host)
        return base.obj_make_list(context, cls(context), objects.Service,
                                  db_services)

    @base.remotable_classmethod
    def get_all(cls, context, disabled=None, set_zones=False):
        db_services = db.service_get_all(context, disabled=disabled)
        if set_zones:
            db_services = availability_zones.set_availability_zones(
                context, db_services)
        return base.obj_make_list(context, cls(context), objects.Service,
                                  db_services)

    @base.remotable_classmethod
    def get_all_computes_by_hv_type(cls, context, hv_type):
        db_services = db.service_get_all_computes_by_hv_type(
            context, hv_type, include_disabled=False)
        return base.obj_make_list(context, cls(context), objects.Service,
                                  db_services)
