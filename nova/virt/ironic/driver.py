# coding=utf-8
#
# Copyright 2014 Red Hat, Inc.
# Copyright 2013 Hewlett-Packard Development Company, L.P.
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

"""
A driver wrapping the Ironic API, such that Nova may provision
bare metal resources.
"""
import base64
import gzip
import shutil
import tempfile
import time

from oslo_log import log as logging
from oslo_service import loopingcall
from oslo_utils import excutils
from oslo_utils import importutils
import six

from nova.api.metadata import base as instance_metadata
from nova.compute import arch
from nova.compute import hv_type
from nova.compute import power_state
from nova.compute import task_states
from nova.compute import vm_mode
from nova.compute import vm_states
import nova.conf
from nova import context as nova_context
from nova import exception
from nova import hash_ring
from nova.i18n import _
from nova.i18n import _LE
from nova.i18n import _LI
from nova.i18n import _LW
from nova import objects
from nova import servicegroup
from nova.virt import configdrive
from nova.virt import driver as virt_driver
from nova.virt import firewall
from nova.virt import hardware
from nova.virt.ironic import client_wrapper
from nova.virt.ironic import ironic_states
from nova.virt.ironic import patcher


ironic = None

LOG = logging.getLogger(__name__)


CONF = nova.conf.CONF

_POWER_STATE_MAP = {
    ironic_states.POWER_ON: power_state.RUNNING,
    ironic_states.NOSTATE: power_state.NOSTATE,
    ironic_states.POWER_OFF: power_state.SHUTDOWN,
}

_UNPROVISION_STATES = (ironic_states.ACTIVE, ironic_states.DEPLOYFAIL,
                       ironic_states.ERROR, ironic_states.DEPLOYWAIT,
                       ironic_states.DEPLOYING)

_NODE_FIELDS = ('uuid', 'power_state', 'target_power_state', 'provision_state',
                'target_provision_state', 'last_error', 'maintenance',
                'properties', 'instance_uuid')


def map_power_state(state):
    try:
        return _POWER_STATE_MAP[state]
    except KeyError:
        LOG.warning(_LW("Power state %s not found."), state)
        return power_state.NOSTATE


def _get_nodes_supported_instances(cpu_arch=None):
    """Return supported instances for a node."""
    if not cpu_arch:
        return []
    return [(cpu_arch,
             hv_type.BAREMETAL,
             vm_mode.HVM)]


def _log_ironic_polling(what, node, instance):
    power_state = (None if node.power_state is None else
                   '"%s"' % node.power_state)
    tgt_power_state = (None if node.target_power_state is None else
                       '"%s"' % node.target_power_state)
    prov_state = (None if node.provision_state is None else
                  '"%s"' % node.provision_state)
    tgt_prov_state = (None if node.target_provision_state is None else
                      '"%s"' % node.target_provision_state)
    LOG.debug('Still waiting for ironic node %(node)s to %(what)s: '
              'power_state=%(power_state)s, '
              'target_power_state=%(tgt_power_state)s, '
              'provision_state=%(prov_state)s, '
              'target_provision_state=%(tgt_prov_state)s',
              dict(what=what,
                   node=node.uuid,
                   power_state=power_state,
                   tgt_power_state=tgt_power_state,
                   prov_state=prov_state,
                   tgt_prov_state=tgt_prov_state),
              instance=instance)


class IronicDriver(virt_driver.ComputeDriver):
    """Hypervisor driver for Ironic - bare metal provisioning."""

    capabilities = {"has_imagecache": False,
                    "supports_recreate": False,
                    "supports_migrate_to_same_host": False,
                    "supports_attach_interface": False
                    }

    def __init__(self, virtapi, read_only=False):
        super(IronicDriver, self).__init__(virtapi)
        global ironic
        if ironic is None:
            ironic = importutils.import_module('ironicclient')
            # NOTE(deva): work around a lack of symbols in the current version.
            if not hasattr(ironic, 'exc'):
                ironic.exc = importutils.import_module('ironicclient.exc')
            if not hasattr(ironic, 'client'):
                ironic.client = importutils.import_module(
                                                    'ironicclient.client')

        self.firewall_driver = firewall.load_driver(
            default='nova.virt.firewall.NoopFirewallDriver')
        self.node_cache = {}
        self.node_cache_time = 0
        self.servicegroup_api = servicegroup.API()
        self._refresh_hash_ring(nova_context.get_admin_context())

        self.ironicclient = client_wrapper.IronicClientWrapper()

    def _get_node(self, node_uuid):
        """Get a node by its UUID."""
        return self.ironicclient.call('node.get', node_uuid,
                                      fields=_NODE_FIELDS)

    def _validate_instance_and_node(self, instance):
        """Get the node associated with the instance.

        Check with the Ironic service that this instance is associated with a
        node, and return the node.
        """
        try:
            return self.ironicclient.call('node.get_by_instance_uuid',
                                          instance.uuid, fields=_NODE_FIELDS)
        except ironic.exc.NotFound:
            raise exception.InstanceNotFound(instance_id=instance.uuid)

    def _node_resources_unavailable(self, node_obj):
        """Determine whether the node's resources are in an acceptable state.

        Determines whether the node's resources should be presented
        to Nova for use based on the current power, provision and maintenance
        state. This is called after _node_resources_used, so any node that
        is not used and not in AVAILABLE should be considered in a 'bad' state,
        and unavailable for scheduling. Returns True if unacceptable.
        """
        bad_power_states = [
            ironic_states.ERROR, ironic_states.NOSTATE]
        # keep NOSTATE around for compatibility
        good_provision_states = [
            ironic_states.AVAILABLE, ironic_states.NOSTATE]
        return (node_obj.maintenance or
                node_obj.power_state in bad_power_states or
                node_obj.provision_state not in good_provision_states or
                (node_obj.provision_state in good_provision_states and
                 node_obj.instance_uuid is not None))

    def _node_resources_used(self, node_obj):
        """Determine whether the node's resources are currently used.

        Determines whether the node's resources should be considered used
        or not. A node is used when it is either in the process of putting
        a new instance on the node, has an instance on the node, or is in
        the process of cleaning up from a deleted instance. Returns True if
        used.

        If we report resources as consumed for a node that does not have an
        instance on it, the resource tracker will notice there's no instances
        consuming resources and try to correct us. So only nodes with an
        instance attached should report as consumed here.
        """
        return node_obj.instance_uuid is not None

    def _parse_node_properties(self, node):
        """Helper method to parse the node's properties."""
        properties = {}

        for prop in ('cpus', 'memory_mb', 'local_gb'):
            try:
                properties[prop] = int(node.properties.get(prop, 0))
            except (TypeError, ValueError):
                LOG.warning(_LW('Node %(uuid)s has a malformed "%(prop)s". '
                                'It should be an integer.'),
                            {'uuid': node.uuid, 'prop': prop})
                properties[prop] = 0

        raw_cpu_arch = node.properties.get('cpu_arch', None)
        try:
            cpu_arch = arch.canonicalize(raw_cpu_arch)
        except exception.InvalidArchitectureName:
            cpu_arch = None
        if not cpu_arch:
            LOG.warning(_LW("cpu_arch not defined for node '%s'"), node.uuid)

        properties['cpu_arch'] = cpu_arch
        properties['raw_cpu_arch'] = raw_cpu_arch
        properties['capabilities'] = node.properties.get('capabilities')
        return properties

    def _parse_node_instance_info(self, node, props):
        """Helper method to parse the node's instance info.

        If a property cannot be looked up via instance_info, use the original
        value from the properties dict. This is most likely to be correct;
        it should only be incorrect if the properties were changed directly
        in Ironic while an instance was deployed.
        """
        instance_info = {}

        # add this key because it's different in instance_info for some reason
        props['vcpus'] = props['cpus']
        for prop in ('vcpus', 'memory_mb', 'local_gb'):
            original = props[prop]
            try:
                instance_info[prop] = int(node.instance_info.get(prop,
                                                                 original))
            except (TypeError, ValueError):
                LOG.warning(_LW('Node %(uuid)s has a malformed "%(prop)s". '
                                'It should be an integer but its value '
                                'is "%(value)s".'),
                            {'uuid': node.uuid, 'prop': prop,
                             'value': node.instance_info.get(prop)})
                instance_info[prop] = original

        return instance_info

    def _node_resource(self, node):
        """Helper method to create resource dict from node stats."""
        properties = self._parse_node_properties(node)

        vcpus = properties['cpus']
        memory_mb = properties['memory_mb']
        local_gb = properties['local_gb']
        raw_cpu_arch = properties['raw_cpu_arch']
        cpu_arch = properties['cpu_arch']

        nodes_extra_specs = {}

        # NOTE(deva): In Havana and Icehouse, the flavor was required to link
        # to an arch-specific deploy kernel and ramdisk pair, and so the flavor
        # also had to have extra_specs['cpu_arch'], which was matched against
        # the ironic node.properties['cpu_arch'].
        # With Juno, the deploy image(s) may be referenced directly by the
        # node.driver_info, and a flavor no longer needs to contain any of
        # these three extra specs, though the cpu_arch may still be used
        # in a heterogeneous environment, if so desired.
        # NOTE(dprince): we use the raw cpu_arch here because extra_specs
        # filters aren't canonicalized
        nodes_extra_specs['cpu_arch'] = raw_cpu_arch

        # NOTE(gilliard): To assist with more precise scheduling, if the
        # node.properties contains a key 'capabilities', we expect the value
        # to be of the form "k1:v1,k2:v2,etc.." which we add directly as
        # key/value pairs into the node_extra_specs to be used by the
        # ComputeCapabilitiesFilter
        capabilities = properties['capabilities']
        if capabilities:
            for capability in str(capabilities).split(','):
                parts = capability.split(':')
                if len(parts) == 2 and parts[0] and parts[1]:
                    nodes_extra_specs[parts[0].strip()] = parts[1]
                else:
                    LOG.warning(_LW("Ignoring malformed capability '%s'. "
                                    "Format should be 'key:val'."), capability)

        vcpus_used = 0
        memory_mb_used = 0
        local_gb_used = 0

        if self._node_resources_used(node):
            # Node is in the process of deploying, is deployed, or is in
            # the process of cleaning up from a deploy. Report all of its
            # resources as in use.
            instance_info = self._parse_node_instance_info(node, properties)

            # Use instance_info instead of properties here is because the
            # properties of a deployed node can be changed which will count
            # as available resources.
            vcpus_used = vcpus = instance_info['vcpus']
            memory_mb_used = memory_mb = instance_info['memory_mb']
            local_gb_used = local_gb = instance_info['local_gb']

        # Always checking allows us to catch the case where Nova thinks there
        # are available resources on the Node, but Ironic does not (because it
        # is not in a usable state): https://launchpad.net/bugs/1503453
        if self._node_resources_unavailable(node):
            # The node's current state is such that it should not present any
            # of its resources to Nova
            vcpus = 0
            memory_mb = 0
            local_gb = 0

        dic = {
            'hypervisor_hostname': str(node.uuid),
            'hypervisor_type': self._get_hypervisor_type(),
            'hypervisor_version': self._get_hypervisor_version(),
            'resource_class': node.resource_class,
            # The Ironic driver manages multiple hosts, so there are
            # likely many different CPU models in use. As such it is
            # impossible to provide any meaningful info on the CPU
            # model of the "host"
            'cpu_info': None,
            'vcpus': vcpus,
            'vcpus_used': vcpus_used,
            'local_gb': local_gb,
            'local_gb_used': local_gb_used,
            'disk_available_least': local_gb - local_gb_used,
            'memory_mb': memory_mb,
            'memory_mb_used': memory_mb_used,
            'supported_instances': _get_nodes_supported_instances(cpu_arch),
            'stats': nodes_extra_specs,
            'numa_topology': None,
        }
        return dic

    def _start_firewall(self, instance, network_info):
        self.firewall_driver.setup_basic_filtering(instance, network_info)
        self.firewall_driver.prepare_instance_filter(instance, network_info)
        self.firewall_driver.apply_instance_filter(instance, network_info)

    def _stop_firewall(self, instance, network_info):
        self.firewall_driver.unfilter_instance(instance, network_info)

    def _add_driver_fields(self, node, instance, image_meta, flavor,
                           preserve_ephemeral=None):
        patch = patcher.create(node).get_deploy_patch(instance,
                                                      image_meta,
                                                      flavor,
                                                      preserve_ephemeral)

        # Associate the node with an instance
        patch.append({'path': '/instance_uuid', 'op': 'add',
                      'value': instance.uuid})
        try:
            # FIXME(lucasagomes): The "retry_on_conflict" parameter was added
            # to basically causes the deployment to fail faster in case the
            # node picked by the scheduler is already associated with another
            # instance due bug #1341420.
            self.ironicclient.call('node.update', node.uuid, patch,
                                   retry_on_conflict=False)
        except ironic.exc.BadRequest:
            msg = (_("Failed to add deploy parameters on node %(node)s "
                     "when provisioning the instance %(instance)s")
                   % {'node': node.uuid, 'instance': instance.uuid})
            LOG.error(msg)
            raise exception.InstanceDeployFailure(msg)

    def _remove_driver_fields(self, node, instance):
        patch = [{'path': '/instance_info', 'op': 'remove'},
                 {'path': '/instance_uuid', 'op': 'remove'}]
        try:
            self.ironicclient.call('node.update', node.uuid, patch)
        except ironic.exc.BadRequest as e:
            LOG.warning(_LW("Failed to remove deploy parameters from node "
                            "%(node)s when unprovisioning the instance "
                            "%(instance)s: %(reason)s"),
                        {'node': node.uuid, 'instance': instance.uuid,
                         'reason': six.text_type(e)})

    def _cleanup_deploy(self, node, instance, network_info):
        self._unplug_vifs(node, instance, network_info)
        self._stop_firewall(instance, network_info)

    def _wait_for_active(self, instance):
        """Wait for the node to be marked as ACTIVE in Ironic."""
        instance.refresh()
        if (instance.task_state == task_states.DELETING or
            instance.vm_state in (vm_states.ERROR, vm_states.DELETED)):
            raise exception.InstanceDeployFailure(
                _("Instance %s provisioning was aborted") % instance.uuid)

        node = self._validate_instance_and_node(instance)
        if node.provision_state == ironic_states.ACTIVE:
            # job is done
            LOG.debug("Ironic node %(node)s is now ACTIVE",
                      dict(node=node.uuid), instance=instance)
            raise loopingcall.LoopingCallDone()

        if node.target_provision_state in (ironic_states.DELETED,
                                           ironic_states.AVAILABLE):
            # ironic is trying to delete it now
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        if node.provision_state in (ironic_states.NOSTATE,
                                    ironic_states.AVAILABLE):
            # ironic already deleted it
            raise exception.InstanceNotFound(instance_id=instance.uuid)

        if node.provision_state == ironic_states.DEPLOYFAIL:
            # ironic failed to deploy
            msg = (_("Failed to provision instance %(inst)s: %(reason)s")
                   % {'inst': instance.uuid, 'reason': node.last_error})
            raise exception.InstanceDeployFailure(msg)

        _log_ironic_polling('become ACTIVE', node, instance)

    def _wait_for_power_state(self, instance, message):
        """Wait for the node to complete a power state change."""
        node = self._validate_instance_and_node(instance)

        if node.target_power_state == ironic_states.NOSTATE:
            raise loopingcall.LoopingCallDone()

        _log_ironic_polling(message, node, instance)

    def init_host(self, host):
        """Initialize anything that is necessary for the driver to function.

        :param host: the hostname of the compute host.

        """
        return

    def _get_hypervisor_type(self):
        """Get hypervisor type."""
        return 'ironic'

    def _get_hypervisor_version(self):
        """Returns the version of the Ironic API service endpoint."""
        return client_wrapper.IRONIC_API_VERSION[0]

    def instance_exists(self, instance):
        """Checks the existence of an instance.

        Checks the existence of an instance. This is an override of the
        base method for efficiency.

        :param instance: The instance object.
        :returns: True if the instance exists. False if not.

        """
        try:
            self._validate_instance_and_node(instance)
            return True
        except exception.InstanceNotFound:
            return False

    def _get_node_list(self, **kwargs):
        """Helper function to return the list of nodes.

        If unable to connect ironic server, an empty list is returned.

        :returns: a list of raw node from ironic

        """
        try:
            node_list = self.ironicclient.call("node.list", **kwargs)
        except exception.NovaException:
            node_list = []
        return node_list

    def list_instances(self):
        """Return the names of all the instances provisioned.

        :returns: a list of instance names.

        """
        # NOTE(lucasagomes): limit == 0 is an indicator to continue
        # pagination until there're no more values to be returned.
        node_list = self._get_node_list(associated=True, limit=0)
        context = nova_context.get_admin_context()
        return [objects.Instance.get_by_uuid(context,
                                             i.instance_uuid).name
                for i in node_list]

    def list_instance_uuids(self):
        """Return the UUIDs of all the instances provisioned.

        :returns: a list of instance UUIDs.

        """
        # NOTE(lucasagomes): limit == 0 is an indicator to continue
        # pagination until there're no more values to be returned.
        return list(n.instance_uuid
                    for n in self._get_node_list(associated=True, limit=0))

    def node_is_available(self, nodename):
        """Confirms a Nova hypervisor node exists in the Ironic inventory.

        :param nodename: The UUID of the node.
        :returns: True if the node exists, False if not.

        """
        # NOTE(comstud): We can cheat and use caching here. This method
        # just needs to return True for nodes that exist. It doesn't
        # matter if the data is stale. Sure, it's possible that removing
        # node from Ironic will cause this method to return True until
        # the next call to 'get_available_nodes', but there shouldn't
        # be much harm. There's already somewhat of a race.
        if not self.node_cache:
            # Empty cache, try to populate it.
            self._refresh_cache()
        if nodename in self.node_cache:
            return True

        # NOTE(comstud): Fallback and check Ironic. This case should be
        # rare.
        try:
            self._get_node(nodename)
            return True
        except ironic.exc.NotFound:
            return False

    def _refresh_hash_ring(self, ctxt):
        service_list = objects.ServiceList.get_all_computes_by_hv_type(
            ctxt, self._get_hypervisor_type())
        services = set()
        for svc in service_list:
            is_up = self.servicegroup_api.service_is_up(svc)
            if is_up:
                services.add(svc.host)
        # NOTE(jroll): always make sure this service is in the list, because
        # only services that have something registered in the compute_nodes
        # table will be here so far, and we might be brand new.
        services.add(CONF.host)

        self.hash_ring = hash_ring.HashRing(services)

    def _refresh_cache(self):
        # NOTE(lucasagomes): limit == 0 is an indicator to continue
        # pagination until there're no more values to be returned.
        ctxt = nova_context.get_admin_context()
        self._refresh_hash_ring(ctxt)
        instances = objects.InstanceList.get_uuids_by_host(ctxt, CONF.host)
        node_cache = {}

        for node in self._get_node_list(detail=True, limit=0):
            # NOTE(jroll): we always manage the nodes for instances we manage
            if node.instance_uuid in instances:
                node_cache[node.uuid] = node

            # NOTE(jroll): check if the node matches us in the hash ring, and
            # does not have an instance_uuid (which would imply the node has
            # an instance managed by another compute service).
            # Note that this means nodes with an instance that was deleted in
            # nova while the service was down, and not yet reaped, will not be
            # reported until the periodic task cleans it up.
            elif (node.instance_uuid is None and
                  CONF.host in self.hash_ring.get_hosts(node.uuid)):
                node_cache[node.uuid] = node

        self.node_cache = node_cache
        self.node_cache_time = time.time()

    def get_available_nodes(self, refresh=False):
        """Returns the UUIDs of all nodes in the Ironic inventory.

        :param refresh: Boolean value; If True run update first. Ignored by
                        this driver.
        :returns: a list of UUIDs

        """
        # NOTE(jroll) we refresh the cache every time this is called
        #             because it needs to happen in the resource tracker
        #             periodic task. This task doesn't pass refresh=True,
        #             unfortunately.
        self._refresh_cache()

        node_uuids = list(self.node_cache.keys())
        LOG.debug("Returning %(num_nodes)s available node(s)",
                  dict(num_nodes=len(node_uuids)))

        return node_uuids

    def get_available_resource(self, nodename):
        """Retrieve resource information.

        This method is called when nova-compute launches, and
        as part of a periodic task that records the results in the DB.

        :param nodename: the UUID of the node.
        :returns: a dictionary describing resources.

        """
        # NOTE(comstud): We can cheat and use caching here. This method is
        # only called from a periodic task and right after the above
        # get_available_nodes() call is called.
        if not self.node_cache:
            # Well, it's also called from init_host(), so if we have empty
            # cache, let's try to populate it.
            self._refresh_cache()

        cache_age = time.time() - self.node_cache_time
        if nodename in self.node_cache:
            LOG.debug("Using cache for node %(node)s, age: %(age)s",
                      {'node': nodename, 'age': cache_age})
            node = self.node_cache[nodename]
        else:
            LOG.debug("Node %(node)s not found in cache, age: %(age)s",
                      {'node': nodename, 'age': cache_age})
            node = self._get_node(nodename)
        return self._node_resource(node)

    def get_info(self, instance):
        """Get the current state and resource usage for this instance.

        If the instance is not found this method returns (a dictionary
        with) NOSTATE and all resources == 0.

        :param instance: the instance object.
        :returns: a InstanceInfo object
        """
        try:
            node = self._validate_instance_and_node(instance)
        except exception.InstanceNotFound:
            return hardware.InstanceInfo(
                state=map_power_state(ironic_states.NOSTATE))

        properties = self._parse_node_properties(node)
        memory_kib = properties['memory_mb'] * 1024
        if memory_kib == 0:
            LOG.warning(_LW("Warning, memory usage is 0 for "
                            "%(instance)s on baremetal node %(node)s."),
                        {'instance': instance.uuid,
                         'node': instance.node})

        num_cpu = properties['cpus']
        if num_cpu == 0:
            LOG.warning(_LW("Warning, number of cpus is 0 for "
                            "%(instance)s on baremetal node %(node)s."),
                        {'instance': instance.uuid,
                         'node': instance.node})

        return hardware.InstanceInfo(state=map_power_state(node.power_state),
                                     max_mem_kb=memory_kib,
                                     mem_kb=memory_kib,
                                     num_cpu=num_cpu)

    def deallocate_networks_on_reschedule(self, instance):
        """Does the driver want networks deallocated on reschedule?

        :param instance: the instance object.
        :returns: Boolean value. If True deallocate networks on reschedule.
        """
        return True

    def macs_for_instance(self, instance):
        """List the MAC addresses of an instance.

        List of MAC addresses for the node which this instance is
        associated with.

        :param instance: the instance object.
        :return: None, or a set of MAC ids (e.g. set(['12:34:56:78:90:ab'])).
            None means 'no constraints', a set means 'these and only these
            MAC addresses'.
        """
        try:
            node = self._get_node(instance.node)
        except ironic.exc.NotFound:
            return None
        ports = self.ironicclient.call("node.list_ports", node.uuid)
        return set([p.address for p in ports])

    def _generate_configdrive(self, context, instance, node, network_info,
                              extra_md=None, files=None):
        """Generate a config drive.

        :param instance: The instance object.
        :param node: The node object.
        :param network_info: Instance network information.
        :param extra_md: Optional, extra metadata to be added to the
                         configdrive.
        :param files: Optional, a list of paths to files to be added to
                      the configdrive.

        """
        if not extra_md:
            extra_md = {}

        i_meta = instance_metadata.InstanceMetadata(instance,
            content=files, extra_md=extra_md, network_info=network_info,
            request_context=context)

        with tempfile.NamedTemporaryFile() as uncompressed:
            with configdrive.ConfigDriveBuilder(instance_md=i_meta) as cdb:
                cdb.make_drive(uncompressed.name)

            with tempfile.NamedTemporaryFile() as compressed:
                # compress config drive
                with gzip.GzipFile(fileobj=compressed, mode='wb') as gzipped:
                    uncompressed.seek(0)
                    shutil.copyfileobj(uncompressed, gzipped)

                # base64 encode config drive
                compressed.seek(0)
                return base64.b64encode(compressed.read())

    def spawn(self, context, instance, image_meta, injected_files,
              admin_password, network_info=None, block_device_info=None):
        """Deploy an instance.

        :param context: The security context.
        :param instance: The instance object.
        :param image_meta: Image dict returned by nova.image.glance
            that defines the image from which to boot this instance.
        :param injected_files: User files to inject into instance.
        :param admin_password: Administrator password to set in
            instance.
        :param network_info: Instance network information.
        :param block_device_info: Instance block device
            information. Ignored by this driver.
        """
        LOG.debug('Spawn called for instance', instance=instance)

        # The compute manager is meant to know the node uuid, so missing uuid
        # is a significant issue. It may mean we've been passed the wrong data.
        node_uuid = instance.get('node')
        if not node_uuid:
            raise ironic.exc.BadRequest(
                _("Ironic node uuid not supplied to "
                  "driver for instance %s.") % instance.uuid)

        node = self._get_node(node_uuid)
        flavor = instance.flavor

        self._add_driver_fields(node, instance, image_meta, flavor)

        # NOTE(Shrews): The default ephemeral device needs to be set for
        # services (like cloud-init) that depend on it being returned by the
        # metadata server. Addresses bug https://launchpad.net/bugs/1324286.
        if flavor.ephemeral_gb:
            instance.default_ephemeral_device = '/dev/sda1'
            instance.save()

        # validate we are ready to do the deploy
        validate_chk = self.ironicclient.call("node.validate", node_uuid)
        if (not validate_chk.deploy.get('result')
                or not validate_chk.power.get('result')):
            # something is wrong. undo what we have done
            self._cleanup_deploy(node, instance, network_info)
            raise exception.ValidationError(_(
                "Ironic node: %(id)s failed to validate."
                " (deploy: %(deploy)s, power: %(power)s)")
                % {'id': node.uuid,
                   'deploy': validate_chk.deploy,
                   'power': validate_chk.power})

        # prepare for the deploy
        try:
            self._plug_vifs(node, instance, network_info)
            self._start_firewall(instance, network_info)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Error preparing deploy for instance "
                              "%(instance)s on baremetal node %(node)s."),
                          {'instance': instance.uuid,
                           'node': node_uuid})
                self._cleanup_deploy(node, instance, network_info)

        # Config drive
        configdrive_value = None
        if configdrive.required_by(instance):
            extra_md = {}
            if admin_password:
                extra_md['admin_pass'] = admin_password

            try:
                configdrive_value = self._generate_configdrive(
                    context, instance, node, network_info, extra_md=extra_md,
                    files=injected_files)
            except Exception as e:
                with excutils.save_and_reraise_exception():
                    msg = (_LE("Failed to build configdrive: %s") %
                           six.text_type(e))
                    LOG.error(msg, instance=instance)
                    self._cleanup_deploy(node, instance, network_info)

            LOG.info(_LI("Config drive for instance %(instance)s on "
                         "baremetal node %(node)s created."),
                         {'instance': instance['uuid'], 'node': node_uuid})

        # trigger the node deploy
        try:
            self.ironicclient.call("node.set_provision_state", node_uuid,
                                   ironic_states.ACTIVE,
                                   configdrive=configdrive_value)
        except Exception as e:
            with excutils.save_and_reraise_exception():
                msg = (_LE("Failed to request Ironic to provision instance "
                           "%(inst)s: %(reason)s"),
                           {'inst': instance.uuid,
                            'reason': six.text_type(e)})
                LOG.error(msg)
                self._cleanup_deploy(node, instance, network_info)

        timer = loopingcall.FixedIntervalLoopingCall(self._wait_for_active,
                                                     instance)
        try:
            timer.start(interval=CONF.ironic.api_retry_interval).wait()
            LOG.info(_LI('Successfully provisioned Ironic node %s'),
                     node.uuid, instance=instance)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Error deploying instance %(instance)s on "
                              "baremetal node %(node)s."),
                             {'instance': instance.uuid,
                              'node': node_uuid})

    def _unprovision(self, instance, node):
        """This method is called from destroy() to unprovision
        already provisioned node after required checks.
        """
        try:
            self.ironicclient.call("node.set_provision_state", node.uuid,
                                   "deleted")
        except Exception as e:
            # if the node is already in a deprovisioned state, continue
            # This should be fixed in Ironic.
            # TODO(deva): This exception should be added to
            #             python-ironicclient and matched directly,
            #             rather than via __name__.
            if getattr(e, '__name__', None) != 'InstanceDeployFailure':
                raise

        # using a dict because this is modified in the local method
        data = {'tries': 0}

        def _wait_for_provision_state():
            try:
                node = self._validate_instance_and_node(instance)
            except exception.InstanceNotFound:
                LOG.debug("Instance already removed from Ironic",
                          instance=instance)
                raise loopingcall.LoopingCallDone()
            if node.provision_state in (ironic_states.NOSTATE,
                                        ironic_states.CLEANING,
                                        ironic_states.CLEANWAIT,
                                        ironic_states.CLEANFAIL,
                                        ironic_states.AVAILABLE):
                # From a user standpoint, the node is unprovisioned. If a node
                # gets into CLEANFAIL state, it must be fixed in Ironic, but we
                # can consider the instance unprovisioned.
                LOG.debug("Ironic node %(node)s is in state %(state)s, "
                          "instance is now unprovisioned.",
                          dict(node=node.uuid, state=node.provision_state),
                          instance=instance)
                raise loopingcall.LoopingCallDone()

            if data['tries'] >= CONF.ironic.api_max_retries + 1:
                msg = (_("Error destroying the instance on node %(node)s. "
                         "Provision state still '%(state)s'.")
                       % {'state': node.provision_state,
                          'node': node.uuid})
                LOG.error(msg)
                raise exception.NovaException(msg)
            else:
                data['tries'] += 1

            _log_ironic_polling('unprovision', node, instance)

        # wait for the state transition to finish
        timer = loopingcall.FixedIntervalLoopingCall(_wait_for_provision_state)
        timer.start(interval=CONF.ironic.api_retry_interval).wait()

    def destroy(self, context, instance, network_info,
                block_device_info=None, destroy_disks=True, migrate_data=None):
        """Destroy the specified instance, if it can be found.

        :param context: The security context.
        :param instance: The instance object.
        :param network_info: Instance network information.
        :param block_device_info: Instance block device
            information. Ignored by this driver.
        :param destroy_disks: Indicates if disks should be
            destroyed. Ignored by this driver.
        :param migrate_data: implementation specific params.
            Ignored by this driver.
        """
        LOG.debug('Destroy called for instance', instance=instance)
        try:
            node = self._validate_instance_and_node(instance)
        except exception.InstanceNotFound:
            LOG.warning(_LW("Destroy called on non-existing instance %s."),
                        instance.uuid)
            # NOTE(deva): if nova.compute.ComputeManager._delete_instance()
            #             is called on a non-existing instance, the only way
            #             to delete it is to return from this method
            #             without raising any exceptions.
            return

        if node.provision_state in _UNPROVISION_STATES:
            self._unprovision(instance, node)
        else:
            self._remove_driver_fields(node, instance)

        self._cleanup_deploy(node, instance, network_info)
        LOG.info(_LI('Successfully unprovisioned Ironic node %s'),
                 node.uuid, instance=instance)

    def reboot(self, context, instance, network_info, reboot_type,
               block_device_info=None, bad_volumes_callback=None):
        """Reboot the specified instance.

        NOTE: Ironic does not support soft-off, so this method
              always performs a hard-reboot.
        NOTE: Unlike the libvirt driver, this method does not delete
              and recreate the instance; it preserves local state.

        :param context: The security context.
        :param instance: The instance object.
        :param network_info: Instance network information. Ignored by
            this driver.
        :param reboot_type: Either a HARD or SOFT reboot. Ignored by
            this driver.
        :param block_device_info: Info pertaining to attached volumes.
            Ignored by this driver.
        :param bad_volumes_callback: Function to handle any bad volumes
            encountered. Ignored by this driver.

        """
        LOG.debug('Reboot called for instance', instance=instance)
        node = self._validate_instance_and_node(instance)
        self.ironicclient.call("node.set_power_state", node.uuid, 'reboot')

        timer = loopingcall.FixedIntervalLoopingCall(
                    self._wait_for_power_state, instance, 'reboot')
        timer.start(interval=CONF.ironic.api_retry_interval).wait()
        LOG.info(_LI('Successfully rebooted Ironic node %s'),
                 node.uuid, instance=instance)

    def power_off(self, instance, timeout=0, retry_interval=0):
        """Power off the specified instance.

        NOTE: Ironic does not support soft-off, so this method ignores
              timeout and retry_interval parameters.
        NOTE: Unlike the libvirt driver, this method does not delete
              and recreate the instance; it preserves local state.

        :param instance: The instance object.
        :param timeout: time to wait for node to shutdown. Ignored by
            this driver.
        :param retry_interval: How often to signal node while waiting
            for it to shutdown. Ignored by this driver.
        """
        LOG.debug('Power off called for instance', instance=instance)
        node = self._validate_instance_and_node(instance)
        self.ironicclient.call("node.set_power_state", node.uuid, 'off')

        timer = loopingcall.FixedIntervalLoopingCall(
                    self._wait_for_power_state, instance, 'power off')
        timer.start(interval=CONF.ironic.api_retry_interval).wait()
        LOG.info(_LI('Successfully powered off Ironic node %s'),
                 node.uuid, instance=instance)

    def power_on(self, context, instance, network_info,
                 block_device_info=None):
        """Power on the specified instance.

        NOTE: Unlike the libvirt driver, this method does not delete
              and recreate the instance; it preserves local state.

        :param context: The security context.
        :param instance: The instance object.
        :param network_info: Instance network information. Ignored by
            this driver.
        :param block_device_info: Instance block device
            information. Ignored by this driver.

        """
        LOG.debug('Power on called for instance', instance=instance)
        node = self._validate_instance_and_node(instance)
        self.ironicclient.call("node.set_power_state", node.uuid, 'on')

        timer = loopingcall.FixedIntervalLoopingCall(
                    self._wait_for_power_state, instance, 'power on')
        timer.start(interval=CONF.ironic.api_retry_interval).wait()
        LOG.info(_LI('Successfully powered on Ironic node %s'),
                 node.uuid, instance=instance)

    def refresh_security_group_rules(self, security_group_id):
        """Refresh security group rules from data store.

        Invoked when security group rules are updated.

        :param security_group_id: The security group id.

        """
        self.firewall_driver.refresh_security_group_rules(security_group_id)

    def refresh_instance_security_rules(self, instance):
        """Refresh security group rules from data store.

        Gets called when an instance gets added to or removed from
        the security group the instance is a member of or if the
        group gains or loses a rule.

        :param instance: The instance object.

        """
        self.firewall_driver.refresh_instance_security_rules(instance)

    def ensure_filtering_rules_for_instance(self, instance, network_info):
        """Set up filtering rules.

        :param instance: The instance object.
        :param network_info: Instance network information.

        """
        self.firewall_driver.setup_basic_filtering(instance, network_info)
        self.firewall_driver.prepare_instance_filter(instance, network_info)

    def unfilter_instance(self, instance, network_info):
        """Stop filtering instance.

        :param instance: The instance object.
        :param network_info: Instance network information.

        """
        self.firewall_driver.unfilter_instance(instance, network_info)

    def _plug_vifs(self, node, instance, network_info):
        # NOTE(PhilDay): Accessing network_info will block if the thread
        # it wraps hasn't finished, so do this ahead of time so that we
        # don't block while holding the logging lock.
        network_info_str = str(network_info)
        LOG.debug("plug: instance_uuid=%(uuid)s vif=%(network_info)s",
                  {'uuid': instance.uuid,
                   'network_info': network_info_str})
        # start by ensuring the ports are clear
        self._unplug_vifs(node, instance, network_info)

        ports = self.ironicclient.call("node.list_ports", node.uuid)

        if len(network_info) > len(ports):
            raise exception.VirtualInterfacePlugException(_(
                "Ironic node: %(id)s virtual to physical interface count"
                "  mismatch"
                " (Vif count: %(vif_count)d, Pif count: %(pif_count)d)")
                % {'id': node.uuid,
                   'vif_count': len(network_info),
                   'pif_count': len(ports)})

        if len(network_info) > 0:
            # not needed if no vif are defined
            for vif in network_info:
                for pif in ports:
                    if vif['address'] == pif.address:
                        # attach what neutron needs directly to the port
                        port_id = six.text_type(vif['id'])
                        patch = [{'op': 'add',
                                  'path': '/extra/vif_port_id',
                                  'value': port_id}]
                        self.ironicclient.call("port.update", pif.uuid, patch)
                        break

    def _unplug_vifs(self, node, instance, network_info):
        # NOTE(PhilDay): Accessing network_info will block if the thread
        # it wraps hasn't finished, so do this ahead of time so that we
        # don't block while holding the logging lock.
        network_info_str = str(network_info)
        LOG.debug("unplug: instance_uuid=%(uuid)s vif=%(network_info)s",
                  {'uuid': instance.uuid,
                   'network_info': network_info_str})
        if network_info and len(network_info) > 0:
            ports = self.ironicclient.call("node.list_ports", node.uuid,
                                      detail=True)

            # not needed if no vif are defined
            for pif in ports:
                if 'vif_port_id' in pif.extra:
                    # we can not attach a dict directly
                    patch = [{'op': 'remove', 'path': '/extra/vif_port_id'}]
                    try:
                        self.ironicclient.call("port.update", pif.uuid, patch)
                    except ironic.exc.BadRequest:
                        pass

    def plug_vifs(self, instance, network_info):
        """Plug VIFs into networks.

        :param instance: The instance object.
        :param network_info: Instance network information.

        """
        node = self._get_node(instance.node)
        self._plug_vifs(node, instance, network_info)

    def unplug_vifs(self, instance, network_info):
        """Unplug VIFs from networks.

        :param instance: The instance object.
        :param network_info: Instance network information.

        """
        node = self._get_node(instance.node)
        self._unplug_vifs(node, instance, network_info)

    def rebuild(self, context, instance, image_meta, injected_files,
                admin_password, bdms, detach_block_devices,
                attach_block_devices, network_info=None,
                recreate=False, block_device_info=None,
                preserve_ephemeral=False):
        """Rebuild/redeploy an instance.

        This version of rebuild() allows for supporting the option to
        preserve the ephemeral partition. We cannot call spawn() from
        here because it will attempt to set the instance_uuid value
        again, which is not allowed by the Ironic API. It also requires
        the instance to not have an 'active' provision state, but we
        cannot safely change that. Given that, we implement only the
        portions of spawn() we need within rebuild().

        :param context: The security context.
        :param instance: The instance object.
        :param image_meta: Image object returned by nova.image.glance
            that defines the image from which to boot this instance. Ignored
            by this driver.
        :param injected_files: User files to inject into instance. Ignored
            by this driver.
        :param admin_password: Administrator password to set in
            instance. Ignored by this driver.
        :param bdms: block-device-mappings to use for rebuild. Ignored
            by this driver.
        :param detach_block_devices: function to detach block devices. See
            nova.compute.manager.ComputeManager:_rebuild_default_impl for
            usage. Ignored by this driver.
        :param attach_block_devices: function to attach block devices. See
            nova.compute.manager.ComputeManager:_rebuild_default_impl for
            usage. Ignored by this driver.
        :param network_info: Instance network information. Ignored by
            this driver.
        :param recreate: Boolean value; if True the instance is
            recreated on a new hypervisor - all the cleanup of old state is
            skipped. Ignored by this driver.
        :param block_device_info: Instance block device
            information. Ignored by this driver.
        :param preserve_ephemeral: Boolean value; if True the ephemeral
            must be preserved on rebuild.

        """
        LOG.debug('Rebuild called for instance', instance=instance)

        instance.task_state = task_states.REBUILD_SPAWNING
        instance.save(expected_task_state=[task_states.REBUILDING])

        node_uuid = instance.node
        node = self._get_node(node_uuid)

        self._add_driver_fields(node, instance, image_meta, instance.flavor,
                                preserve_ephemeral)

        # Trigger the node rebuild/redeploy.
        try:
            self.ironicclient.call("node.set_provision_state",
                              node_uuid, ironic_states.REBUILD)
        except (exception.NovaException,         # Retry failed
                ironic.exc.InternalServerError,  # Validations
                ironic.exc.BadRequest) as e:     # Maintenance
            msg = (_("Failed to request Ironic to rebuild instance "
                     "%(inst)s: %(reason)s") % {'inst': instance.uuid,
                                                'reason': six.text_type(e)})
            raise exception.InstanceDeployFailure(msg)

        # Although the target provision state is REBUILD, it will actually go
        # to ACTIVE once the redeploy is finished.
        timer = loopingcall.FixedIntervalLoopingCall(self._wait_for_active,
                                                     instance)
        timer.start(interval=CONF.ironic.api_retry_interval).wait()
        LOG.info(_LI('Instance was successfully rebuilt'), instance=instance)

    def network_binding_host_id(self, context, instance):
        """Get host ID to associate with network ports.

        This defines the binding:host_id parameter to the port-create calls for
        Neutron. If using the neutron network interface (separate networks for
        the control plane and tenants), return None here to indicate that the
        port should not yet be bound; Ironic will make a port-update call to
        Neutron later to tell Neutron to bind the port. Otherwise, use the
        default behavior and allow the port to be bound immediately.

        NOTE: the late binding is important for security. If an ML2 mechanism
        manages to connect the tenant network to the baremetal machine before
        deployment is done (e.g. port-create time), then the tenant potentially
        has access to the deploy agent, which may contain firmware blobs or
        secrets. ML2 mechanisms may be able to connect the port without the
        switchport info that comes from ironic, if they store that switchport
        info for some reason. As such, we should *never* pass binding:host_id
        in the port-create call when using the 'neutron' network_interface,
        because a null binding:host_id indicates to Neutron that it should
        not connect the port yet.

        :param context:  request context
        :param instance: nova.objects.instance.Instance that the network
                         ports will be associated with
        :returns: a string representing the host ID, or None
        """

        node = self.ironicclient.call('node.get', instance.node,
                                      fields=['network_interface'])
        if node.network_interface == 'neutron':
            return None
        # flat network, go ahead and allow the port to be bound
        return super(IronicDriver, self).network_binding_host_id(
            context, instance)
