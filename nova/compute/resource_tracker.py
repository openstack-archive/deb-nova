# Copyright (c) 2012 OpenStack Foundation
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
Track resources like memory and disk for a compute host.  Provides the
scheduler with useful information about availability through the ComputeNode
model.
"""
import copy

from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import importutils

from nova.compute import claims
from nova.compute import monitors
from nova.compute import task_states
from nova.compute import vm_states
import nova.conf
from nova import exception
from nova.i18n import _, _LE, _LI, _LW
from nova import objects
from nova.objects import base as obj_base
from nova.objects import migration as migration_obj
from nova.pci import manager as pci_manager
from nova.pci import request as pci_request
from nova import rpc
from nova.scheduler import client as scheduler_client
from nova import utils
from nova.virt import hardware

CONF = nova.conf.CONF

LOG = logging.getLogger(__name__)
COMPUTE_RESOURCE_SEMAPHORE = "compute_resources"


def _instance_in_resize_state(instance):
    """Returns True if the instance is in one of the resizing states.

    :param instance: `nova.objects.Instance` object
    """
    vm = instance.vm_state
    task = instance.task_state

    if vm == vm_states.RESIZED:
        return True

    if (vm in [vm_states.ACTIVE, vm_states.STOPPED]
            and task in [task_states.RESIZE_PREP,
            task_states.RESIZE_MIGRATING, task_states.RESIZE_MIGRATED,
            task_states.RESIZE_FINISH, task_states.REBUILDING]):
        return True

    return False


def _is_trackable_migration(migration):
    # Only look at resize/migrate migration and evacuation records
    # NOTE(danms): RT should probably examine live migration
    # records as well and do something smart. However, ignore
    # those for now to avoid them being included in below calculations.
    return migration.migration_type in ('resize', 'migration',
                                        'evacuation')


class ResourceTracker(object):
    """Compute helper class for keeping track of resource usage as instances
    are built and destroyed.
    """

    def __init__(self, host, driver, nodename):
        self.host = host
        self.driver = driver
        self.pci_tracker = None
        self.nodename = nodename
        self.compute_node = None
        self.stats = importutils.import_object(CONF.compute_stats_class)
        self.tracked_instances = {}
        self.tracked_migrations = {}
        monitor_handler = monitors.MonitorHandler(self)
        self.monitors = monitor_handler.monitors
        self.old_resources = objects.ComputeNode()
        self.scheduler_client = scheduler_client.SchedulerClient()
        self.ram_allocation_ratio = CONF.ram_allocation_ratio
        self.cpu_allocation_ratio = CONF.cpu_allocation_ratio
        self.disk_allocation_ratio = CONF.disk_allocation_ratio

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def instance_claim(self, context, instance, limits=None):
        """Indicate that some resources are needed for an upcoming compute
        instance build operation.

        This should be called before the compute node is about to perform
        an instance build operation that will consume additional resources.

        :param context: security context
        :param instance: instance to reserve resources for.
        :type instance: nova.objects.instance.Instance object
        :param limits: Dict of oversubscription limits for memory, disk,
                       and CPUs.
        :returns: A Claim ticket representing the reserved resources.  It can
                  be used to revert the resource usage if an error occurs
                  during the instance build.
        """
        if self.disabled:
            # compute_driver doesn't support resource tracking, just
            # set the 'host' and node fields and continue the build:
            self._set_instance_host_and_node(instance)
            return claims.NopClaim()

        # sanity checks:
        if instance.host:
            LOG.warning(_LW("Host field should not be set on the instance "
                            "until resources have been claimed."),
                        instance=instance)

        if instance.node:
            LOG.warning(_LW("Node field should not be set on the instance "
                            "until resources have been claimed."),
                        instance=instance)

        # get the overhead required to build this instance:
        overhead = self.driver.estimate_instance_overhead(instance)
        LOG.debug("Memory overhead for %(flavor)d MB instance; %(overhead)d "
                  "MB", {'flavor': instance.flavor.memory_mb,
                          'overhead': overhead['memory_mb']})
        LOG.debug("Disk overhead for %(flavor)d GB instance; %(overhead)d "
                  "GB", {'flavor': instance.flavor.root_gb,
                         'overhead': overhead.get('disk_gb', 0)})

        pci_requests = objects.InstancePCIRequests.get_by_instance_uuid(
            context, instance.uuid)
        claim = claims.Claim(context, instance, self, self.compute_node,
                             pci_requests, overhead=overhead, limits=limits)

        # self._set_instance_host_and_node() will save instance to the DB
        # so set instance.numa_topology first.  We need to make sure
        # that numa_topology is saved while under COMPUTE_RESOURCE_SEMAPHORE
        # so that the resource audit knows about any cpus we've pinned.
        instance_numa_topology = claim.claimed_numa_topology
        instance.numa_topology = instance_numa_topology
        self._set_instance_host_and_node(instance)

        if self.pci_tracker:
            # NOTE(jaypipes): ComputeNode.pci_device_pools is set below
            # in _update_usage_from_instance().
            self.pci_tracker.claim_instance(context, pci_requests,
                                            instance_numa_topology)

        # Mark resources in-use and update stats
        self._update_usage_from_instance(context, instance)

        elevated = context.elevated()
        # persist changes to the compute node:
        self._update(elevated)

        return claim

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def rebuild_claim(self, context, instance, limits=None, image_meta=None,
                      migration=None):
        """Create a claim for a rebuild operation."""
        instance_type = instance.flavor
        return self._move_claim(context, instance, instance_type,
                                move_type='evacuation', limits=limits,
                                image_meta=image_meta, migration=migration)

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def resize_claim(self, context, instance, instance_type,
                     image_meta=None, limits=None):
        """Create a claim for a resize or cold-migration move."""
        return self._move_claim(context, instance, instance_type,
                                image_meta=image_meta, limits=limits)

    def _move_claim(self, context, instance, new_instance_type, move_type=None,
                    image_meta=None, limits=None, migration=None):
        """Indicate that resources are needed for a move to this host.

        Move can be either a migrate/resize, live-migrate or an
        evacuate/rebuild operation.

        :param context: security context
        :param instance: instance object to reserve resources for
        :param new_instance_type: new instance_type being resized to
        :param image_meta: instance image metadata
        :param move_type: move type - can be one of 'migration', 'resize',
                         'live-migration', 'evacuate'
        :param limits: Dict of oversubscription limits for memory, disk,
        and CPUs
        :param migration: A migration object if one was already created
                          elsewhere for this operation
        :returns: A Claim ticket representing the reserved resources.  This
        should be turned into finalize  a resource claim or free
        resources after the compute operation is finished.
        """
        image_meta = image_meta or {}
        if migration:
            self._claim_existing_migration(migration)
        else:
            migration = self._create_migration(context, instance,
                                               new_instance_type, move_type)

        if self.disabled:
            # compute_driver doesn't support resource tracking, just
            # generate the migration record and continue the resize:
            return claims.NopClaim(migration=migration)

        # get memory overhead required to build this instance:
        overhead = self.driver.estimate_instance_overhead(new_instance_type)
        LOG.debug("Memory overhead for %(flavor)d MB instance; %(overhead)d "
                  "MB", {'flavor': new_instance_type.memory_mb,
                          'overhead': overhead['memory_mb']})
        LOG.debug("Disk overhead for %(flavor)d GB instance; %(overhead)d "
                  "GB", {'flavor': instance.flavor.root_gb,
                         'overhead': overhead.get('disk_gb', 0)})

        # TODO(moshele): we are recreating the pci requests even if
        # there was no change on resize. This will cause allocating
        # the old/new pci device in the resize phase. In the future
        # we would like to optimise this.
        new_pci_requests = pci_request.get_pci_requests_from_flavor(
            new_instance_type)
        new_pci_requests.instance_uuid = instance.uuid
        # PCI requests come from two sources: instance flavor and
        # SR-IOV ports. SR-IOV ports pci_request don't have an alias_name.
        # On resize merge the SR-IOV ports pci_requests with the new
        # instance flavor pci_requests.
        if instance.pci_requests:
            for request in instance.pci_requests.requests:
                if request.alias_name is None:
                    new_pci_requests.requests.append(request)
        claim = claims.MoveClaim(context, instance, new_instance_type,
                                 image_meta, self, self.compute_node,
                                 new_pci_requests, overhead=overhead,
                                 limits=limits)

        claim.migration = migration
        claimed_pci_devices_objs = []
        if self.pci_tracker:
            # NOTE(jaypipes): ComputeNode.pci_device_pools is set below
            # in _update_usage_from_instance().
            claimed_pci_devices_objs = self.pci_tracker.claim_instance(
                    context, new_pci_requests, claim.claimed_numa_topology)
        claimed_pci_devices = objects.PciDeviceList(
                objects=claimed_pci_devices_objs)

        # TODO(jaypipes): Move claimed_numa_topology out of the Claim's
        # constructor flow so the Claim constructor only tests whether
        # resources can be claimed, not consume the resources directly.
        mig_context = objects.MigrationContext(
            context=context, instance_uuid=instance.uuid,
            migration_id=migration.id,
            old_numa_topology=instance.numa_topology,
            new_numa_topology=claim.claimed_numa_topology,
            old_pci_devices=instance.pci_devices,
            new_pci_devices=claimed_pci_devices,
            old_pci_requests=instance.pci_requests,
            new_pci_requests=new_pci_requests)
        instance.migration_context = mig_context
        instance.save()

        # Mark the resources in-use for the resize landing on this
        # compute host:
        self._update_usage_from_migration(context, instance, migration)
        elevated = context.elevated()
        self._update(elevated)

        return claim

    def _create_migration(self, context, instance, new_instance_type,
                          move_type=None):
        """Create a migration record for the upcoming resize.  This should
        be done while the COMPUTE_RESOURCES_SEMAPHORE is held so the resource
        claim will not be lost if the audit process starts.
        """
        migration = objects.Migration(context=context.elevated())
        migration.dest_compute = self.host
        migration.dest_node = self.nodename
        migration.dest_host = self.driver.get_host_ip_addr()
        migration.old_instance_type_id = instance.flavor.id
        migration.new_instance_type_id = new_instance_type.id
        migration.status = 'pre-migrating'
        migration.instance_uuid = instance.uuid
        migration.source_compute = instance.host
        migration.source_node = instance.node
        if move_type:
            migration.migration_type = move_type
        else:
            migration.migration_type = migration_obj.determine_migration_type(
                migration)
        migration.create()
        return migration

    def _claim_existing_migration(self, migration):
        """Make an existing migration record count for resource tracking.

        If a migration record was created already before the request made
        it to this compute host, only set up the migration so it's included in
        resource tracking. This should be done while the
        COMPUTE_RESOURCES_SEMAPHORE is held.
        """
        migration.dest_compute = self.host
        migration.dest_node = self.nodename
        migration.dest_host = self.driver.get_host_ip_addr()
        migration.status = 'pre-migrating'
        migration.save()

    def _set_instance_host_and_node(self, instance):
        """Tag the instance as belonging to this host.  This should be done
        while the COMPUTE_RESOURCES_SEMAPHORE is held so the resource claim
        will not be lost if the audit process starts.
        """
        instance.host = self.host
        instance.launched_on = self.host
        instance.node = self.nodename
        instance.save()

    def _unset_instance_host_and_node(self, instance):
        """Untag the instance so it no longer belongs to the host.

        This should be done while the COMPUTE_RESOURCES_SEMAPHORE is held so
        the resource claim will not be lost if the audit process starts.
        """
        instance.host = None
        instance.node = None
        instance.save()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def abort_instance_claim(self, context, instance):
        """Remove usage from the given instance."""
        self._update_usage_from_instance(context, instance, is_removed=True)

        instance.clear_numa_topology()
        self._unset_instance_host_and_node(instance)

        self._update(context.elevated())

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def drop_move_claim(self, context, instance, instance_type=None,
                        prefix='new_'):
        """Remove usage for an incoming/outgoing migration."""
        if instance['uuid'] in self.tracked_migrations:
            migration, itype = self.tracked_migrations.pop(instance['uuid'])

            if not instance_type:
                ctxt = context.elevated()
                instance_type = self._get_instance_type(ctxt, instance, prefix,
                                                        migration)

            if instance_type is not None and instance_type.id == itype['id']:
                numa_topology = self._get_migration_context_resource(
                    'numa_topology', instance, prefix=prefix)
                usage = self._get_usage_dict(
                        itype, numa_topology=numa_topology)
                if self.pci_tracker:
                    # free old/new allocated pci devices
                    pci_devices = self._get_migration_context_resource(
                        'pci_devices', instance, prefix=prefix)
                    if pci_devices:
                        for pci_device in pci_devices:
                            self.pci_tracker.free_device(pci_device, instance)
                self._update_usage(usage, sign=-1)

                ctxt = context.elevated()
                self._update(ctxt)

            instance.drop_migration_context()

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def update_usage(self, context, instance):
        """Update the resource usage and stats after a change in an
        instance
        """
        if self.disabled:
            return

        uuid = instance['uuid']

        # don't update usage for this instance unless it submitted a resource
        # claim first:
        if uuid in self.tracked_instances:
            self._update_usage_from_instance(context, instance)
            self._update(context.elevated())

    @property
    def disabled(self):
        return self.compute_node is None

    def _init_compute_node(self, context, resources):
        """Initialize the compute node if it does not already exist.

        The resource tracker will be inoperable if compute_node
        is not defined. The compute_node will remain undefined if
        we fail to create it or if there is no associated service
        registered.

        If this method has to create a compute node it needs initial
        values - these come from resources.

        :param context: security context
        :param resources: initial values
        """

        # if there is already a compute node just use resources
        # to initialize
        if self.compute_node:
            self._copy_resources(resources)
            self._setup_pci_tracker(context, resources)
            self.scheduler_client.update_resource_stats(self.compute_node)
            return

        # now try to get the compute node record from the
        # database. If we get one we use resources to initialize
        self.compute_node = self._get_compute_node(context)
        if self.compute_node:
            self._copy_resources(resources)
            self._setup_pci_tracker(context, resources)
            self.scheduler_client.update_resource_stats(self.compute_node)
            return

        # there was no local copy and none in the database
        # so we need to create a new compute node. This needs
        # to be initialized with resource values.
        self.compute_node = objects.ComputeNode(context)
        self.compute_node.host = self.host
        self._copy_resources(resources)
        self.compute_node.create()
        LOG.info(_LI('Compute_service record created for '
                     '%(host)s:%(node)s'),
                 {'host': self.host, 'node': self.nodename})

        self._setup_pci_tracker(context, resources)
        self.scheduler_client.update_resource_stats(self.compute_node)

    def _setup_pci_tracker(self, context, resources):
        if not self.pci_tracker:
            n_id = self.compute_node.id if self.compute_node else None
            self.pci_tracker = pci_manager.PciDevTracker(context, node_id=n_id)
            if 'pci_passthrough_devices' in resources:
                dev_json = resources.pop('pci_passthrough_devices')
                self.pci_tracker.update_devices_from_hypervisor_resources(
                        dev_json)

            dev_pools_obj = self.pci_tracker.stats.to_device_pools_obj()
            self.compute_node.pci_device_pools = dev_pools_obj

    def _copy_resources(self, resources):
        """Copy resource values to initialize compute_node and related
        data structures.
        """
        # purge old stats and init with anything passed in by the driver
        self.stats.clear()
        self.stats.digest_stats(resources.get('stats'))
        self.compute_node.stats = copy.deepcopy(self.stats)

        # update the allocation ratios for the related ComputeNode object
        self.compute_node.ram_allocation_ratio = self.ram_allocation_ratio
        self.compute_node.cpu_allocation_ratio = self.cpu_allocation_ratio
        self.compute_node.disk_allocation_ratio = self.disk_allocation_ratio

        # now copy rest to compute_node
        self.compute_node.update_from_virt_driver(resources)

    def _get_host_metrics(self, context, nodename):
        """Get the metrics from monitors and
        notify information to message bus.
        """
        metrics = objects.MonitorMetricList()
        metrics_info = {}
        for monitor in self.monitors:
            try:
                monitor.populate_metrics(metrics)
            except Exception as exc:
                LOG.warning(_LW("Cannot get the metrics from %(mon)s; "
                                "error: %(exc)s"),
                            {'mon': monitor, 'exc': exc})
        # TODO(jaypipes): Remove this when compute_node.metrics doesn't need
        # to be populated as a JSONified string.
        metrics = metrics.to_list()
        if len(metrics):
            metrics_info['nodename'] = nodename
            metrics_info['metrics'] = metrics
            metrics_info['host'] = self.host
            metrics_info['host_ip'] = CONF.my_ip
            notifier = rpc.get_notifier(service='compute', host=nodename)
            notifier.info(context, 'compute.metrics.update', metrics_info)
        return metrics

    def update_available_resource(self, context):
        """Override in-memory calculations of compute node resource usage based
        on data audited from the hypervisor layer.

        Add in resource claims in progress to account for operations that have
        declared a need for resources, but not necessarily retrieved them from
        the hypervisor layer yet.
        """
        LOG.info(_LI("Auditing locally available compute resources for "
                     "node %(node)s"),
                 {'node': self.nodename})
        resources = self.driver.get_available_resource(self.nodename)
        resources['host_ip'] = CONF.my_ip

        # We want the 'cpu_info' to be None from the POV of the
        # virt driver, but the DB requires it to be non-null so
        # just force it to empty string
        if "cpu_info" not in resources or resources["cpu_info"] is None:
            resources["cpu_info"] = ''

        self._verify_resources(resources)

        self._report_hypervisor_resource_view(resources)

        self._update_available_resource(context, resources)

    def _pair_instances_to_migrations(self, migrations, instances):
        instance_by_uuid = {inst.uuid: inst for inst in instances}
        for migration in migrations:
            try:
                migration.instance = instance_by_uuid[migration.instance_uuid]
            except KeyError:
                # NOTE(danms): If this happens, we don't set it here, and
                # let the code either fail or lazy-load the instance later
                # which is what happened before we added this optimization.
                # This _should_ not be possible, of course.
                LOG.error(_LE('Migration for instance %(uuid)s refers to '
                              'another host\'s instance!'),
                          {'uuid': migration.instance_uuid})

    @utils.synchronized(COMPUTE_RESOURCE_SEMAPHORE)
    def _update_available_resource(self, context, resources):

        # initialize the compute node object, creating it
        # if it does not already exist.
        self._init_compute_node(context, resources)

        # if we could not init the compute node the tracker will be
        # disabled and we should quit now
        if self.disabled:
            return

        # Grab all instances assigned to this node:
        instances = objects.InstanceList.get_by_host_and_node(
            context, self.host, self.nodename,
            expected_attrs=['system_metadata',
                            'numa_topology',
                            'flavor', 'migration_context'])

        # Now calculate usage based on instance utilization:
        self._update_usage_from_instances(context, instances)

        # Grab all in-progress migrations:
        migrations = objects.MigrationList.get_in_progress_by_host_and_node(
                context, self.host, self.nodename)

        self._pair_instances_to_migrations(migrations, instances)
        self._update_usage_from_migrations(context, migrations)

        # Detect and account for orphaned instances that may exist on the
        # hypervisor, but are not in the DB:
        orphans = self._find_orphaned_instances()
        self._update_usage_from_orphans(orphans)

        # NOTE(yjiang5): Because pci device tracker status is not cleared in
        # this periodic task, and also because the resource tracker is not
        # notified when instances are deleted, we need remove all usages
        # from deleted instances.
        self.pci_tracker.clean_usage(instances, migrations, orphans)
        dev_pools_obj = self.pci_tracker.stats.to_device_pools_obj()
        self.compute_node.pci_device_pools = dev_pools_obj

        self._report_final_resource_view()

        metrics = self._get_host_metrics(context, self.nodename)
        # TODO(pmurray): metrics should not be a json string in ComputeNode,
        # but it is. This should be changed in ComputeNode
        self.compute_node.metrics = jsonutils.dumps(metrics)

        # update the compute_node
        self._update(context)
        LOG.info(_LI('Compute_service record updated for %(host)s:%(node)s'),
                     {'host': self.host, 'node': self.nodename})

    def _get_compute_node(self, context):
        """Returns compute node for the host and nodename."""
        try:
            return objects.ComputeNode.get_by_host_and_nodename(
                context, self.host, self.nodename)
        except exception.NotFound:
            LOG.warning(_LW("No compute node record for %(host)s:%(node)s"),
                        {'host': self.host, 'node': self.nodename})

    def _report_hypervisor_resource_view(self, resources):
        """Log the hypervisor's view of free resources.

        This is just a snapshot of resource usage recorded by the
        virt driver.

        The following resources are logged:
            - free memory
            - free disk
            - free CPUs
            - assignable PCI devices
        """
        free_ram_mb = resources['memory_mb'] - resources['memory_mb_used']
        free_disk_gb = resources['local_gb'] - resources['local_gb_used']
        vcpus = resources['vcpus']
        if vcpus:
            free_vcpus = vcpus - resources['vcpus_used']
            LOG.debug("Hypervisor: free VCPUs: %s", free_vcpus)
        else:
            free_vcpus = 'unknown'
            LOG.debug("Hypervisor: VCPU information unavailable")

        pci_devices = resources.get('pci_passthrough_devices')

        LOG.debug("Hypervisor/Node resource view: "
                  "name=%(node)s "
                  "free_ram=%(free_ram)sMB "
                  "free_disk=%(free_disk)sGB "
                  "free_vcpus=%(free_vcpus)s "
                  "pci_devices=%(pci_devices)s",
                  {'node': self.nodename,
                   'free_ram': free_ram_mb,
                   'free_disk': free_disk_gb,
                   'free_vcpus': free_vcpus,
                   'pci_devices': pci_devices})

    def _report_final_resource_view(self):
        """Report final calculate of physical memory, used virtual memory,
        disk, usable vCPUs, used virtual CPUs and PCI devices,
        including instance calculations and in-progress resource claims. These
        values will be exposed via the compute node table to the scheduler.
        """
        vcpus = self.compute_node.vcpus
        if vcpus:
            tcpu = vcpus
            ucpu = self.compute_node.vcpus_used
            LOG.info(_LI("Total usable vcpus: %(tcpu)s, "
                        "total allocated vcpus: %(ucpu)s"),
                        {'tcpu': vcpus,
                         'ucpu': ucpu})
        else:
            tcpu = 0
            ucpu = 0
        pci_stats = (list(self.compute_node.pci_device_pools) if
            self.compute_node.pci_device_pools else [])
        LOG.info(_LI("Final resource view: "
                     "name=%(node)s "
                     "phys_ram=%(phys_ram)sMB "
                     "used_ram=%(used_ram)sMB "
                     "phys_disk=%(phys_disk)sGB "
                     "used_disk=%(used_disk)sGB "
                     "total_vcpus=%(total_vcpus)s "
                     "used_vcpus=%(used_vcpus)s "
                     "pci_stats=%(pci_stats)s"),
                 {'node': self.nodename,
                  'phys_ram': self.compute_node.memory_mb,
                  'used_ram': self.compute_node.memory_mb_used,
                  'phys_disk': self.compute_node.local_gb,
                  'used_disk': self.compute_node.local_gb_used,
                  'total_vcpus': tcpu,
                  'used_vcpus': ucpu,
                  'pci_stats': pci_stats})

    def _resource_change(self):
        """Check to see if any resources have changed."""
        if not obj_base.obj_equal_prims(self.compute_node, self.old_resources):
            self.old_resources = copy.deepcopy(self.compute_node)
            return True
        return False

    def _update(self, context):
        """Update partial stats locally and populate them to Scheduler."""
        if not self._resource_change():
            return
        # Persist the stats to the Scheduler
        self.scheduler_client.update_resource_stats(self.compute_node)
        if self.pci_tracker:
            self.pci_tracker.save(context)

    def _update_usage(self, usage, sign=1):
        mem_usage = usage['memory_mb']
        disk_usage = usage.get('root_gb', 0)

        overhead = self.driver.estimate_instance_overhead(usage)
        mem_usage += overhead['memory_mb']
        disk_usage += overhead.get('disk_gb', 0)

        self.compute_node.memory_mb_used += sign * mem_usage
        self.compute_node.local_gb_used += sign * disk_usage
        self.compute_node.local_gb_used += sign * usage.get('ephemeral_gb', 0)
        self.compute_node.vcpus_used += sign * usage.get('vcpus', 0)

        # free ram and disk may be negative, depending on policy:
        self.compute_node.free_ram_mb = (self.compute_node.memory_mb -
                                         self.compute_node.memory_mb_used)
        self.compute_node.free_disk_gb = (self.compute_node.local_gb -
                                          self.compute_node.local_gb_used)

        self.compute_node.running_vms = self.stats.num_instances

        # Calculate the numa usage
        free = sign == -1
        updated_numa_topology = hardware.get_host_numa_usage_from_instance(
                self.compute_node, usage, free)
        self.compute_node.numa_topology = updated_numa_topology

    def _get_migration_context_resource(self, resource, instance,
                                        prefix='new_'):
        migration_context = instance.migration_context
        resource = prefix + resource
        if migration_context and resource in migration_context:
            return getattr(migration_context, resource)
        return None

    def _update_usage_from_migration(self, context, instance, migration):
        """Update usage for a single migration.  The record may
        represent an incoming or outbound migration.
        """
        if not _is_trackable_migration(migration):
            return

        uuid = migration.instance_uuid
        LOG.info(_LI("Updating from migration %s"), uuid)

        incoming = (migration.dest_compute == self.host and
                    migration.dest_node == self.nodename)
        outbound = (migration.source_compute == self.host and
                    migration.source_node == self.nodename)
        same_node = (incoming and outbound)

        record = self.tracked_instances.get(uuid, None)
        itype = None
        numa_topology = None
        sign = 0
        if same_node:
            # same node resize. record usage for whichever instance type the
            # instance is *not* in:
            if (instance['instance_type_id'] ==
                    migration.old_instance_type_id):
                itype = self._get_instance_type(context, instance, 'new_',
                        migration)
                numa_topology = self._get_migration_context_resource(
                    'numa_topology', instance)
                # Allocate pci device(s) for the instance.
                sign = 1
            else:
                # instance record already has new flavor, hold space for a
                # possible revert to the old instance type:
                itype = self._get_instance_type(context, instance, 'old_',
                        migration)
                numa_topology = self._get_migration_context_resource(
                    'numa_topology', instance, prefix='old_')

        elif incoming and not record:
            # instance has not yet migrated here:
            itype = self._get_instance_type(context, instance, 'new_',
                    migration)
            numa_topology = self._get_migration_context_resource(
                'numa_topology', instance)
            # Allocate pci device(s) for the instance.
            sign = 1

        elif outbound and not record:
            # instance migrated, but record usage for a possible revert:
            itype = self._get_instance_type(context, instance, 'old_',
                    migration)
            numa_topology = self._get_migration_context_resource(
                'numa_topology', instance, prefix='old_')

        if itype:
            usage = self._get_usage_dict(
                        itype, numa_topology=numa_topology)
            if self.pci_tracker and sign:
                self.pci_tracker.update_pci_for_instance(
                    context, instance, sign=sign)
            self._update_usage(usage)
            if self.pci_tracker:
                obj = self.pci_tracker.stats.to_device_pools_obj()
                self.compute_node.pci_device_pools = obj
            else:
                obj = objects.PciDevicePoolList()
                self.compute_node.pci_device_pools = obj
            self.tracked_migrations[uuid] = (migration, itype)

    def _update_usage_from_migrations(self, context, migrations):
        filtered = {}
        instances = {}
        self.tracked_migrations.clear()

        # do some defensive filtering against bad migrations records in the
        # database:
        for migration in migrations:
            uuid = migration.instance_uuid

            try:
                if uuid not in instances:
                    instances[uuid] = migration.instance
            except exception.InstanceNotFound as e:
                # migration referencing deleted instance
                LOG.debug('Migration instance not found: %s', e)
                continue

            # skip migration if instance isn't in a resize state:
            if not _instance_in_resize_state(instances[uuid]):
                LOG.warning(_LW("Instance not resizing, skipping migration."),
                            instance_uuid=uuid)
                continue

            # filter to most recently updated migration for each instance:
            other_migration = filtered.get(uuid, None)
            # NOTE(claudiub): In Python 3, you cannot compare NoneTypes.
            if (not other_migration or (
                    migration.updated_at and other_migration.updated_at and
                    migration.updated_at >= other_migration.updated_at)):
                filtered[uuid] = migration

        for migration in filtered.values():
            instance = instances[migration.instance_uuid]
            try:
                self._update_usage_from_migration(context, instance, migration)
            except exception.FlavorNotFound:
                LOG.warning(_LW("Flavor could not be found, skipping "
                                "migration."), instance_uuid=instance.uuid)
                continue

    def _update_usage_from_instance(self, context, instance, is_removed=False):
        """Update usage for a single instance."""

        uuid = instance['uuid']
        is_new_instance = uuid not in self.tracked_instances
        # NOTE(sfinucan): Both brand new instances as well as instances that
        # are being unshelved will have is_new_instance == True
        is_removed_instance = not is_new_instance and (is_removed or
            instance['vm_state'] in vm_states.ALLOW_RESOURCE_REMOVAL)

        if is_new_instance:
            self.tracked_instances[uuid] = obj_base.obj_to_primitive(instance)
            sign = 1

        if is_removed_instance:
            self.tracked_instances.pop(uuid)
            sign = -1

        self.stats.update_stats_for_instance(instance, is_removed_instance)
        self.compute_node.stats = copy.deepcopy(self.stats)

        # if it's a new or deleted instance:
        if is_new_instance or is_removed_instance:
            if self.pci_tracker:
                self.pci_tracker.update_pci_for_instance(context,
                                                         instance,
                                                         sign=sign)
            self.scheduler_client.reportclient.update_instance_allocation(
                self.compute_node, instance, sign)
            # new instance, update compute node resource usage:
            self._update_usage(self._get_usage_dict(instance), sign=sign)

        self.compute_node.current_workload = self.stats.calculate_workload()
        if self.pci_tracker:
            obj = self.pci_tracker.stats.to_device_pools_obj()
            self.compute_node.pci_device_pools = obj
        else:
            self.compute_node.pci_device_pools = objects.PciDevicePoolList()

    def _update_usage_from_instances(self, context, instances):
        """Calculate resource usage based on instance utilization.  This is
        different than the hypervisor's view as it will account for all
        instances assigned to the local compute host, even if they are not
        currently powered on.
        """
        self.tracked_instances.clear()

        # set some initial values, reserve room for host/hypervisor:
        self.compute_node.local_gb_used = CONF.reserved_host_disk_mb / 1024
        self.compute_node.memory_mb_used = CONF.reserved_host_memory_mb
        self.compute_node.vcpus_used = 0
        self.compute_node.free_ram_mb = (self.compute_node.memory_mb -
                                         self.compute_node.memory_mb_used)
        self.compute_node.free_disk_gb = (self.compute_node.local_gb -
                                          self.compute_node.local_gb_used)
        self.compute_node.current_workload = 0
        self.compute_node.running_vms = 0

        for instance in instances:
            if instance.vm_state not in vm_states.ALLOW_RESOURCE_REMOVAL:
                self._update_usage_from_instance(context, instance)

        self.scheduler_client.reportclient.remove_deleted_instances(
                self.compute_node, self.tracked_instances.values())
        self.compute_node.free_ram_mb = max(0, self.compute_node.free_ram_mb)
        self.compute_node.free_disk_gb = max(0, self.compute_node.free_disk_gb)

    def _find_orphaned_instances(self):
        """Given the set of instances and migrations already account for
        by resource tracker, sanity check the hypervisor to determine
        if there are any "orphaned" instances left hanging around.

        Orphans could be consuming memory and should be accounted for in
        usage calculations to guard against potential out of memory
        errors.
        """
        uuids1 = frozenset(self.tracked_instances.keys())
        uuids2 = frozenset(self.tracked_migrations.keys())
        uuids = uuids1 | uuids2

        usage = self.driver.get_per_instance_usage()
        vuuids = frozenset(usage.keys())

        orphan_uuids = vuuids - uuids
        orphans = [usage[uuid] for uuid in orphan_uuids]

        return orphans

    def _update_usage_from_orphans(self, orphans):
        """Include orphaned instances in usage."""
        for orphan in orphans:
            memory_mb = orphan['memory_mb']

            LOG.warning(_LW("Detected running orphan instance: %(uuid)s "
                            "(consuming %(memory_mb)s MB memory)"),
                        {'uuid': orphan['uuid'], 'memory_mb': memory_mb})

            # just record memory usage for the orphan
            usage = {'memory_mb': memory_mb}
            self._update_usage(usage)

    def _verify_resources(self, resources):
        resource_keys = ["vcpus", "memory_mb", "local_gb", "cpu_info",
                         "vcpus_used", "memory_mb_used", "local_gb_used",
                         "numa_topology"]

        missing_keys = [k for k in resource_keys if k not in resources]
        if missing_keys:
            reason = _("Missing keys: %s") % missing_keys
            raise exception.InvalidInput(reason=reason)

    def _get_instance_type(self, context, instance, prefix, migration):
        """Get the instance type from instance."""
        stashed_flavors = migration.migration_type in ('resize',)
        if stashed_flavors:
            return getattr(instance, '%sflavor' % prefix)
        else:
            # NOTE(ndipanov): Certain migration types (all but resize)
            # do not change flavors so there is no need to stash
            # them. In that case - just get the instance flavor.
            return instance.flavor

    def _get_usage_dict(self, object_or_dict, **updates):
        """Make a usage dict _update methods expect.

        Accepts a dict or an Instance or Flavor object, and a set of updates.
        Converts the object to a dict and applies the updates.

        :param object_or_dict: instance or flavor as an object or just a dict
        :param updates: key-value pairs to update the passed object.
                        Currently only considers 'numa_topology', all other
                        keys are ignored.

        :returns: a dict with all the information from object_or_dict updated
                  with updates
        """
        usage = {}
        if isinstance(object_or_dict, objects.Instance):
            usage = {'memory_mb': object_or_dict.flavor.memory_mb,
                     'vcpus': object_or_dict.flavor.vcpus,
                     'root_gb': object_or_dict.flavor.root_gb,
                     'ephemeral_gb': object_or_dict.flavor.ephemeral_gb,
                     'numa_topology': object_or_dict.numa_topology}
        elif isinstance(object_or_dict, objects.Flavor):
            usage = obj_base.obj_to_primitive(object_or_dict)
        else:
            usage.update(object_or_dict)

        for key in ('numa_topology',):
            if key in updates:
                usage[key] = updates[key]
        return usage
