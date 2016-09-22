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
import six
import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.orm import contains_eager
from sqlalchemy import sql

from nova.db.sqlalchemy import api as db_api
from nova.db.sqlalchemy import api_models as models
from nova import exception
from nova.i18n import _LW
from nova import objects
from nova.objects import base
from nova.objects import fields

_ALLOC_TBL = models.Allocation.__table__
_INV_TBL = models.Inventory.__table__
_RP_TBL = models.ResourceProvider.__table__

LOG = logging.getLogger(__name__)


def _get_current_inventory_resources(conn, rp):
    """Returns a set() containing the resource class IDs for all resources
    currently having an inventory record for the supplied resource provider.

    :param conn: DB connection to use.
    :param rp: Resource provider to query inventory for.
    """
    cur_res_sel = sa.select([_INV_TBL.c.resource_class_id]).where(
            _INV_TBL.c.resource_provider_id == rp.id)
    existing_resources = conn.execute(cur_res_sel).fetchall()
    return set([r[0] for r in existing_resources])


def _delete_inventory_from_provider(conn, rp, to_delete):
    """Deletes any inventory records from the supplied provider and set() of
    resource class identifiers.

    If there are allocations for any of the inventories to be deleted raise
    InventoryInUse exception.

    :param conn: DB connection to use.
    :param rp: Resource provider from which to delete inventory.
    :param to_delete: set() containing resource class IDs for records to
                      delete.
    """
    allocation_query = sa.select(
        [_ALLOC_TBL.c.resource_class_id.label('resource_class')]).where(
             sa.and_(_ALLOC_TBL.c.resource_provider_id == rp.id,
                     _ALLOC_TBL.c.resource_class_id.in_(to_delete))
         ).group_by(_ALLOC_TBL.c.resource_class_id)
    allocations = conn.execute(allocation_query).fetchall()
    if allocations:
        resource_classes = ', '.join([fields.ResourceClass.from_index(
            allocation.resource_class) for allocation in allocations])
        raise exception.InventoryInUse(resource_classes=resource_classes,
                                       resource_provider=rp.uuid)

    del_stmt = _INV_TBL.delete().where(sa.and_(
            _INV_TBL.c.resource_provider_id == rp.id,
            _INV_TBL.c.resource_class_id.in_(to_delete)))
    res = conn.execute(del_stmt)
    return res.rowcount


def _add_inventory_to_provider(conn, rp, inv_list, to_add):
    """Inserts new inventory records for the supplied resource provider.

    :param conn: DB connection to use.
    :param rp: Resource provider to add inventory to.
    :param inv_list: InventoryList object
    :param to_add: set() containing resource class IDs to search inv_list for
                   adding to resource provider.
    """
    for res_class in to_add:
        inv_record = inv_list.find(res_class)
        if inv_record.capacity <= 0:
            raise exception.InvalidInventoryCapacity(
                resource_class=fields.ResourceClass.from_index(res_class),
                resource_provider=rp.uuid)
        ins_stmt = _INV_TBL.insert().values(
                resource_provider_id=rp.id,
                resource_class_id=res_class,
                total=inv_record.total,
                reserved=inv_record.reserved,
                min_unit=inv_record.min_unit,
                max_unit=inv_record.max_unit,
                step_size=inv_record.step_size,
                allocation_ratio=inv_record.allocation_ratio)
        conn.execute(ins_stmt)


def _update_inventory_for_provider(conn, rp, inv_list, to_update):
    """Updates existing inventory records for the supplied resource provider.

    :param conn: DB connection to use.
    :param rp: Resource provider on which to update inventory.
    :param inv_list: InventoryList object
    :param to_update: set() containing resource class IDs to search inv_list
                      for updating in resource provider.
    :returns: A list of (uuid, class) tuples that have exceeded their
              capacity after this inventory update.
    """
    exceeded = []
    for res_class in to_update:
        inv_record = inv_list.find(res_class)
        if inv_record.capacity <= 0:
            raise exception.InvalidInventoryCapacity(
                resource_class=fields.ResourceClass.from_index(res_class),
                resource_provider=rp.uuid)
        allocation_query = sa.select(
            [func.sum(_ALLOC_TBL.c.used).label('usage')]).\
            where(sa.and_(
                _ALLOC_TBL.c.resource_provider_id == rp.id,
                _ALLOC_TBL.c.resource_class_id == res_class))
        allocations = conn.execute(allocation_query).first()
        if allocations and allocations['usage'] > inv_record.capacity:
            exceeded.append((rp.uuid,
                             fields.ResourceClass.from_index(res_class)))
        upd_stmt = _INV_TBL.update().where(sa.and_(
                _INV_TBL.c.resource_provider_id == rp.id,
                _INV_TBL.c.resource_class_id == res_class)).values(
                        total=inv_record.total,
                        reserved=inv_record.reserved,
                        min_unit=inv_record.min_unit,
                        max_unit=inv_record.max_unit,
                        step_size=inv_record.step_size,
                        allocation_ratio=inv_record.allocation_ratio)
        res = conn.execute(upd_stmt)
        if not res.rowcount:
            raise exception.NotFound(
                'No inventory of class %s found for update'
                % fields.ResourceClass.from_index(res_class))
    return exceeded


def _increment_provider_generation(conn, rp):
    """Increments the supplied provider's generation value, supplying the
    currently-known generation. Returns whether the increment succeeded.

    :param conn: DB connection to use.
    :param rp: `ResourceProvider` whose generation should be updated.
    :returns: The new resource provider generation value if successful.
    :raises nova.exception.ConcurrentUpdateDetected: if another thread updated
            the same resource provider's view of its inventory or allocations
            in between the time when this object was originally read
            and the call to set the inventory.
    """
    rp_gen = rp.generation
    new_generation = rp_gen + 1
    upd_stmt = _RP_TBL.update().where(sa.and_(
            _RP_TBL.c.id == rp.id,
            _RP_TBL.c.generation == rp_gen)).values(
                    generation=(new_generation))

    res = conn.execute(upd_stmt)
    if res.rowcount != 1:
        raise exception.ConcurrentUpdateDetected
    return new_generation


@db_api.api_context_manager.writer
def _add_inventory(context, rp, inventory):
    """Add one Inventory that wasn't already on the provider."""
    resource_class_id = fields.ResourceClass.index(inventory.resource_class)
    inv_list = InventoryList(objects=[inventory])
    conn = context.session.connection()
    with conn.begin():
        _add_inventory_to_provider(
            conn, rp, inv_list, set([resource_class_id]))
        rp.generation = _increment_provider_generation(conn, rp)


@db_api.api_context_manager.writer
def _update_inventory(context, rp, inventory):
    """Update an inventory already on the provider."""
    resource_class_id = fields.ResourceClass.index(inventory.resource_class)
    inv_list = InventoryList(objects=[inventory])
    conn = context.session.connection()
    with conn.begin():
        exceeded = _update_inventory_for_provider(
            conn, rp, inv_list, set([resource_class_id]))
        rp.generation = _increment_provider_generation(conn, rp)
    return exceeded


@db_api.api_context_manager.writer
def _delete_inventory(context, rp, resource_class_id):
    """Delete up to one Inventory of the given resource_class id."""

    conn = context.session.connection()
    with conn.begin():
        if not _delete_inventory_from_provider(conn, rp, [resource_class_id]):
            raise exception.NotFound(
                'No inventory of class %s found for delete'
                % fields.ResourceClass.from_index(resource_class_id))
        rp.generation = _increment_provider_generation(conn, rp)


@db_api.api_context_manager.writer
def _set_inventory(context, rp, inv_list):
    """Given an InventoryList object, replaces the inventory of the
    resource provider in a safe, atomic fashion using the resource
    provider's generation as a consistent view marker.

    :param context: Nova RequestContext.
    :param rp: `ResourceProvider` object upon which to set inventory.
    :param inv_list: `InventoryList` object to save to backend storage.
    :returns: A list of (uuid, class) tuples that have exceeded their
              capacity after this inventory update.
    :raises nova.exception.ConcurrentUpdateDetected: if another thread updated
            the same resource provider's view of its inventory or allocations
            in between the time when this object was originally read
            and the call to set the inventory.
    """

    conn = context.session.connection()

    existing_resources = _get_current_inventory_resources(conn, rp)
    these_resources = set([fields.ResourceClass.index(r.resource_class)
                           for r in inv_list.objects])

    # Determine which resources we should be adding, deleting and/or
    # updating in the resource provider's inventory by comparing sets
    # of resource class identifiers.
    to_add = these_resources - existing_resources
    to_delete = existing_resources - these_resources
    to_update = these_resources & existing_resources
    exceeded = []

    with conn.begin():
        if to_delete:
            _delete_inventory_from_provider(conn, rp, to_delete)
        if to_add:
            _add_inventory_to_provider(conn, rp, inv_list, to_add)
        if to_update:
            exceeded = _update_inventory_for_provider(conn, rp, inv_list,
                                                      to_update)

        # Here is where we update the resource provider's generation value.
        # If this update updates zero rows, that means that another
        # thread has updated the inventory for this resource provider
        # between the time the caller originally read the resource provider
        # record and inventory information and this point. We raise an
        # exception here which will rollback the above transaction and
        # return an error to the caller to indicate that they can attempt
        # to retry the inventory save after reverifying any capacity
        # conditions and re-reading the existing inventory information.
        rp.generation = _increment_provider_generation(conn, rp)

    return exceeded


@base.NovaObjectRegistry.register
class ResourceProvider(base.NovaObject):
    # Version 1.0: Initial version
    # Version 1.1: Add destroy()
    VERSION = '1.1'

    fields = {
        'id': fields.IntegerField(read_only=True),
        'uuid': fields.UUIDField(nullable=False),
        'name': fields.StringField(nullable=False),
        'generation': fields.IntegerField(nullable=False),
    }

    @base.remotable
    def create(self):
        if 'id' in self:
            raise exception.ObjectActionError(action='create',
                                              reason='already created')
        if 'uuid' not in self:
            raise exception.ObjectActionError(action='create',
                                              reason='uuid is required')
        if 'name' not in self:
            raise exception.ObjectActionError(action='create',
                                              reason='name is required')
        updates = self.obj_get_changes()
        db_rp = self._create_in_db(self._context, updates)
        self._from_db_object(self._context, self, db_rp)

    @base.remotable
    def destroy(self):
        self._delete(self._context, self.id)

    @base.remotable
    def save(self):
        updates = self.obj_get_changes()
        if updates and updates.keys() != ['name']:
            raise exception.ObjectActionError(
                action='save',
                reason='Immutable fields changed')
        self._update_in_db(self._context, self.id, updates)

    @base.remotable_classmethod
    def get_by_uuid(cls, context, uuid):
        db_resource_provider = cls._get_by_uuid_from_db(context, uuid)
        return cls._from_db_object(context, cls(), db_resource_provider)

    @base.remotable
    def add_inventory(self, inventory):
        """Add one new Inventory to the resource provider.

        Fails if Inventory of the provided resource class is
        already present.
        """
        _add_inventory(self._context, self, inventory)
        self.obj_reset_changes()

    @base.remotable
    def delete_inventory(self, resource_class):
        """Delete Inventory of provided resource_class."""
        resource_class_id = fields.ResourceClass.index(resource_class)
        _delete_inventory(self._context, self, resource_class_id)
        self.obj_reset_changes()

    @base.remotable
    def set_inventory(self, inv_list):
        """Set all resource provider Inventory to be the provided list."""
        exceeded = _set_inventory(self._context, self, inv_list)
        for uuid, rclass in exceeded:
            LOG.warning(_LW('Resource provider %(uuid)s is now over-'
                            'capacity for %(resource)s'),
                        {'uuid': uuid, 'resource': rclass})
        self.obj_reset_changes()

    @base.remotable
    def update_inventory(self, inventory):
        """Update one existing Inventory of the same resource class.

        Fails if no Inventory of the same class is present.
        """
        exceeded = _update_inventory(self._context, self, inventory)
        for uuid, rclass in exceeded:
            LOG.warning(_LW('Resource provider %(uuid)s is now over-'
                            'capacity for %(resource)s'),
                        {'uuid': uuid, 'resource': rclass})
        self.obj_reset_changes()

    @staticmethod
    @db_api.api_context_manager.writer
    def _create_in_db(context, updates):
        db_rp = models.ResourceProvider()
        db_rp.update(updates)
        context.session.add(db_rp)
        return db_rp

    @staticmethod
    @db_api.api_context_manager.writer
    def _delete(context, _id):
        # Don't delete the resource provider if it has allocations.
        rp_allocations = context.session.query(models.Allocation).\
                         filter(models.Allocation.resource_provider_id == _id).\
                         count()
        if rp_allocations:
            raise exception.ResourceProviderInUse()
        # Delete any inventory associated with the resource provider
        context.session.query(models.Inventory).\
            filter(models.Inventory.resource_provider_id == _id).delete()
        result = context.session.query(models.ResourceProvider).\
                 filter(models.ResourceProvider.id == _id).delete()
        if not result:
            raise exception.NotFound()

    @staticmethod
    @db_api.api_context_manager.writer
    def _update_in_db(context, id, updates):
        db_rp = context.session.query(models.ResourceProvider).filter_by(
            id=id).first()
        db_rp.update(updates)
        db_rp.save(context.session)

    @staticmethod
    def _from_db_object(context, resource_provider, db_resource_provider):
        for field in resource_provider.fields:
            setattr(resource_provider, field, db_resource_provider[field])
        resource_provider._context = context
        resource_provider.obj_reset_changes()
        return resource_provider

    @staticmethod
    @db_api.api_context_manager.reader
    def _get_by_uuid_from_db(context, uuid):
        result = context.session.query(models.ResourceProvider).filter_by(
            uuid=uuid).first()
        if not result:
            raise exception.NotFound()
        return result


@base.NovaObjectRegistry.register
class ResourceProviderList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial Version
    VERSION = '1.0'

    fields = {
        'objects': fields.ListOfObjectsField('ResourceProvider'),
    }

    allowed_filters = (
        'name', 'uuid'
    )

    @staticmethod
    @db_api.api_context_manager.reader
    def _get_all_by_filters_from_db(context, filters):
        if not filters:
            filters = {}
        query = context.session.query(models.ResourceProvider)
        for attr in ResourceProviderList.allowed_filters:
            if attr in filters:
                query = query.filter(
                    getattr(models.ResourceProvider, attr) == filters[attr])
        query = query.filter_by(can_host=filters.get('can_host', 0))
        return query.all()

    @base.remotable_classmethod
    def get_all_by_filters(cls, context, filters=None):
        resource_providers = cls._get_all_by_filters_from_db(context, filters)
        return base.obj_make_list(context, cls(context),
                                  objects.ResourceProvider, resource_providers)


class _HasAResourceProvider(base.NovaObject):
    """Code shared between Inventory and Allocation

    Both contain a ResourceProvider.
    """

    @staticmethod
    def _make_db(updates):
        try:
            resource_provider = updates.pop('resource_provider')
            updates['resource_provider_id'] = resource_provider.id
        except (KeyError, NotImplementedError):
            raise exception.ObjectActionError(
                action='create',
                reason='resource_provider required')
        try:
            resource_class = updates.pop('resource_class')
        except KeyError:
            raise exception.ObjectActionError(
                action='create',
                reason='resource_class required')
        updates['resource_class_id'] = fields.ResourceClass.index(
            resource_class)
        return updates

    @staticmethod
    def _from_db_object(context, target, source):
        for field in target.fields:
            if field not in ('resource_provider', 'resource_class'):
                setattr(target, field, source[field])

        if 'resource_class' not in target:
            target.resource_class = (
                target.fields['resource_class'].from_index(
                    source['resource_class_id']))
        if ('resource_provider' not in target and
            'resource_provider' in source):
            target.resource_provider = ResourceProvider()
            ResourceProvider._from_db_object(
                context,
                target.resource_provider,
                source['resource_provider'])

        target._context = context
        target.obj_reset_changes()
        return target


@db_api.api_context_manager.writer
def _create_inventory_in_db(context, updates):
    db_inventory = models.Inventory()
    db_inventory.update(updates)
    context.session.add(db_inventory)
    return db_inventory


@db_api.api_context_manager.writer
def _update_inventory_in_db(context, id_, updates):
    result = context.session.query(
        models.Inventory).filter_by(id=id_).update(updates)
    if not result:
        raise exception.NotFound()


@base.NovaObjectRegistry.register
class Inventory(_HasAResourceProvider):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'id': fields.IntegerField(read_only=True),
        'resource_provider': fields.ObjectField('ResourceProvider'),
        'resource_class': fields.ResourceClassField(read_only=True),
        'total': fields.NonNegativeIntegerField(),
        'reserved': fields.NonNegativeIntegerField(default=0),
        'min_unit': fields.NonNegativeIntegerField(default=1),
        'max_unit': fields.NonNegativeIntegerField(default=1),
        'step_size': fields.NonNegativeIntegerField(default=1),
        'allocation_ratio': fields.NonNegativeFloatField(default=1.0),
    }

    @property
    def capacity(self):
        """Inventory capacity, adjusted by allocation_ratio."""
        return int((self.total - self.reserved) * self.allocation_ratio)

    @base.remotable
    def create(self):
        if 'id' in self:
            raise exception.ObjectActionError(action='create',
                                              reason='already created')
        updates = self._make_db(self.obj_get_changes())
        db_inventory = self._create_in_db(self._context, updates)
        self._from_db_object(self._context, self, db_inventory)

    @base.remotable
    def save(self):
        if 'id' not in self:
            raise exception.ObjectActionError(action='save',
                                              reason='not created')
        updates = self.obj_get_changes()
        updates.pop('id', None)
        self._update_in_db(self._context, self.id, updates)

    @staticmethod
    def _create_in_db(context, updates):
        return _create_inventory_in_db(context, updates)

    @staticmethod
    def _update_in_db(context, id_, updates):
        return _update_inventory_in_db(context, id_, updates)


@base.NovaObjectRegistry.register
class InventoryList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial Version
    VERSION = '1.0'

    fields = {
        'objects': fields.ListOfObjectsField('Inventory'),
    }

    def find(self, res_class):
        """Return the inventory record from the list of Inventory records that
        matches the supplied resource class, or None.

        :param res_class: An integer or string representing a resource
                          class. If the value is a string, the method first
                          looks up the resource class identifier from the
                          string.
        """
        if isinstance(res_class, six.string_types):
            try:
                res_class = fields.ResourceClass.index(res_class)
            except ValueError:
                raise exception.NotFound("No such resource class '%s'" %
                                         res_class)

        for inv_rec in self.objects:
            if fields.ResourceClass.index(inv_rec.resource_class) == res_class:
                return inv_rec

    @staticmethod
    @db_api.api_context_manager.reader
    def _get_all_by_resource_provider(context, rp_uuid):
        return context.session.query(models.Inventory).\
            join(models.Inventory.resource_provider).\
            options(contains_eager('resource_provider')).\
            filter(models.ResourceProvider.uuid == rp_uuid).all()

    @base.remotable_classmethod
    def get_all_by_resource_provider_uuid(cls, context, rp_uuid):
        db_inventory_list = cls._get_all_by_resource_provider(context,
                                                              rp_uuid)
        return base.obj_make_list(context, cls(context), objects.Inventory,
                                  db_inventory_list)


@base.NovaObjectRegistry.register
class Allocation(_HasAResourceProvider):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'id': fields.IntegerField(),
        'resource_provider': fields.ObjectField('ResourceProvider'),
        'consumer_id': fields.UUIDField(),
        'resource_class': fields.ResourceClassField(),
        'used': fields.IntegerField(),
    }

    @staticmethod
    @db_api.api_context_manager.writer
    def _create_in_db(context, updates):
        db_allocation = models.Allocation()
        db_allocation.update(updates)
        context.session.add(db_allocation)
        # We may be in a nested context manager so must flush so the
        # caller receives an id.
        context.session.flush()
        return db_allocation

    @staticmethod
    @db_api.api_context_manager.writer
    def _destroy(context, id):
        result = context.session.query(models.Allocation).filter_by(
            id=id).delete()
        if not result:
            raise exception.NotFound()

    @base.remotable
    def create(self):
        if 'id' in self:
            raise exception.ObjectActionError(action='create',
                                              reason='already created')
        updates = self._make_db(self.obj_get_changes())
        db_allocation = self._create_in_db(self._context, updates)
        self._from_db_object(self._context, self, db_allocation)

    @base.remotable
    def destroy(self):
        self._destroy(self._context, self.id)


def _delete_current_allocs(conn, allocs):
    """Deletes any existing allocations that correspond to the allocations to
    be written. This is wrapped in a transaction, so if the write subsequently
    fails, the deletion will also be rolled back.
    """
    for alloc in allocs:
        rp_id = alloc.resource_provider.id
        consumer_id = alloc.consumer_id
        del_sql = _ALLOC_TBL.delete().where(
                sa.and_(_ALLOC_TBL.c.resource_provider_id == rp_id,
                        _ALLOC_TBL.c.consumer_id == consumer_id))
        conn.execute(del_sql)


def _check_capacity_exceeded(conn, allocs):
    """Checks to see if the supplied allocation records would result in any of
    the inventories involved having their capacity exceeded.

    Raises an InvalidAllocationCapacityExceeded exception if any inventory
    would be exhausted by the allocation. If no inventories would be exceeded
    by the allocation, the function returns a list of `ResourceProvider`
    objects that contain the generation at the time of the check.

    :param conn: SQLalchemy Connection object to use
    :param allocs: List of `Allocation` objects to check
    """
    # The SQL generated below looks like this:
    # SELECT
    #   rp.id,
    #   rp.uuid,
    #   rp.generation,
    #   inv.resource_class_id,
    #   inv.total,
    #   inv.reserved,
    #   inv.allocation_ratio,
    #   allocs.used
    # FROM resource_providers AS rp
    # JOIN inventories AS i1
    # ON rp.id = i1.resource_provider_id
    # LEFT JOIN (
    #    SELECT resource_provider_id, resource_class_id, SUM(used) AS used
    #    FROM allocations
    #    WHERE resource_class_id IN ($RESOURCE_CLASSES)
    #    GROUP BY resource_provider_id, resource_class_id
    # ) AS allocs
    # ON inv.resource_provider_id = allocs.resource_provider_id
    # AND inv.resource_class_id = allocs.resource_class_id
    # WHERE rp.uuid IN ($RESOURCE_PROVIDERS)
    # AND inv.resource_class_id IN ($RESOURCE_CLASSES)
    #
    # We then take the results of the above and determine if any of the
    # inventory will have its capacity exceeded.
    res_classes = set([fields.ResourceClass.index(a.resource_class)
                       for a in allocs])
    provider_uuids = set([a.resource_provider.uuid for a in allocs])

    usage = sa.select([_ALLOC_TBL.c.resource_provider_id,
                       _ALLOC_TBL.c.consumer_id,
                       _ALLOC_TBL.c.resource_class_id,
                       sql.func.sum(_ALLOC_TBL.c.used).label('used')])
    usage = usage.where(_ALLOC_TBL.c.resource_class_id.in_(res_classes))
    usage = usage.group_by(_ALLOC_TBL.c.resource_provider_id,
                           _ALLOC_TBL.c.resource_class_id)
    usage = sa.alias(usage, name='usage')

    inv_join = sql.join(_RP_TBL, _INV_TBL,
            sql.and_(_RP_TBL.c.id == _INV_TBL.c.resource_provider_id,
                     _INV_TBL.c.resource_class_id.in_(res_classes)))
    primary_join = sql.outerjoin(inv_join, usage,
        sql.and_(
            _INV_TBL.c.resource_provider_id == usage.c.resource_provider_id,
            _INV_TBL.c.resource_class_id == usage.c.resource_class_id)
    )
    cols_in_output = [
        _RP_TBL.c.id.label('resource_provider_id'),
        _RP_TBL.c.uuid,
        _RP_TBL.c.generation,
        _INV_TBL.c.resource_class_id,
        _INV_TBL.c.total,
        _INV_TBL.c.reserved,
        _INV_TBL.c.allocation_ratio,
        usage.c.used,
    ]

    sel = sa.select(cols_in_output).select_from(primary_join)
    sel = sel.where(
            sa.and_(_RP_TBL.c.uuid.in_(provider_uuids),
                    _INV_TBL.c.resource_class_id.in_(res_classes)))
    records = conn.execute(sel)
    # Create a map keyed by (rp_uuid, res_class) for the records in the DB
    usage_map = {}
    provs_with_inv = set()
    for record in records:
        map_key = (record['uuid'], record['resource_class_id'])
        if map_key in usage_map:
            raise KeyError("%s already in usage_map, bad query" % str(map_key))
        usage_map[map_key] = record
        provs_with_inv.add(record["uuid"])
    # Ensure that all providers have existing inventory
    missing_provs = provider_uuids - provs_with_inv
    if missing_provs:
        raise exception.InvalidInventory(resource_class=str(res_classes),
                resource_provider=missing_provs)

    res_providers = {}
    for alloc in allocs:
        res_class = fields.ResourceClass.index(alloc.resource_class)
        rp_uuid = alloc.resource_provider.uuid
        key = (rp_uuid, res_class)
        usage = usage_map[key]
        amount_needed = alloc.used
        allocation_ratio = usage['allocation_ratio']
        # usage["used"] can be returned as None
        used = usage['used'] or 0
        capacity = (usage['total'] - usage['reserved']) * allocation_ratio
        if capacity < (used + amount_needed):
            LOG.warning(
                _LW("Over capacity for %(rc)s on resource provider %(rp)s. "
                    "Needed: %(needed)s, Used: %(used)s, Capacity: %(cap)s"),
                {'rc': fields.ResourceClass.from_index(res_class),
                 'rp': rp_uuid,
                 'needed': amount_needed,
                 'used': used,
                 'cap': capacity})
            raise exception.InvalidAllocationCapacityExceeded(
                resource_class=fields.ResourceClass.from_index(res_class),
                resource_provider=rp_uuid)
        if rp_uuid not in res_providers:
            rp = ResourceProvider(id=usage['resource_provider_id'],
                                  uuid=rp_uuid,
                                  generation=usage['generation'])
            res_providers[rp_uuid] = rp
    return list(res_providers.values())


@base.NovaObjectRegistry.register
class AllocationList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial Version
    # Version 1.1: Add create_all() and delete_all()
    VERSION = '1.1'

    fields = {
        'objects': fields.ListOfObjectsField('Allocation'),
    }

    @staticmethod
    @db_api.api_context_manager.writer
    def _delete_allocations(context, allocations):
        for allocation in allocations:
            allocation._context = context
            allocation.destroy()

    @staticmethod
    @db_api.api_context_manager.reader
    def _get_allocations_from_db(context, resource_provider_uuid=None,
                                 consumer_id=None):
        query = (context.session.query(models.Allocation)
                 .join(models.Allocation.resource_provider)
                 .options(contains_eager('resource_provider')))
        if resource_provider_uuid:
            query = query.filter(
                models.ResourceProvider.uuid == resource_provider_uuid)
        if consumer_id:
            query = query.filter(
                models.Allocation.consumer_id == consumer_id)
        return query.all()

    @staticmethod
    @db_api.api_context_manager.writer
    def _set_allocations(context, allocs):
        """Write a set of allocations.

        We must check that there is capacity for each allocation.
        If there is not we roll back the entire set.
        """
        conn = context.session.connection()
        # Before writing any allocation records, we check that the submitted
        # allocations do not cause any inventory capacity to be exceeded for
        # any resource provider and resource class involved in the allocation
        # transaction. _check_capacity_exceeded() raises an exception if any
        # inventory capacity is exceeded. If capacity is not exceeeded, the
        # function returns a list of ResourceProvider objects containing the
        # generation of the resource provider at the time of the check. These
        # objects are used at the end of the allocation transaction as a guard
        # against concurrent updates.
        with conn.begin():
            # First delete any existing allocations for that rp/consumer combo.
            _delete_current_allocs(conn, allocs)
            before_gens = _check_capacity_exceeded(conn, allocs)
            # Now add the allocations that were passed in.
            for alloc in allocs:
                rp = alloc.resource_provider
                res_class = fields.ResourceClass.index(alloc.resource_class)
                ins_stmt = _ALLOC_TBL.insert().values(
                        resource_provider_id=rp.id,
                        resource_class_id=res_class,
                        consumer_id=alloc.consumer_id,
                        used=alloc.used)
                conn.execute(ins_stmt)

            # Generation checking happens here. If the inventory for
            # this resource provider changed out from under us,
            # this will raise a ConcurrentUpdateDetected which can be caught
            # by the caller to choose to try again. It will also rollback the
            # transaction so that these changes always happen atomically.
            for rp in before_gens:
                _increment_provider_generation(conn, rp)

    @base.remotable_classmethod
    def get_all_by_resource_provider_uuid(cls, context, rp_uuid):
        db_allocation_list = cls._get_allocations_from_db(
            context, resource_provider_uuid=rp_uuid)
        return base.obj_make_list(
            context, cls(context), objects.Allocation, db_allocation_list)

    @base.remotable_classmethod
    def get_all_by_consumer_id(cls, context, consumer_id):
        db_allocation_list = cls._get_allocations_from_db(
            context, consumer_id=consumer_id)
        return base.obj_make_list(
            context, cls(context), objects.Allocation, db_allocation_list)

    @base.remotable
    def create_all(self):
        """Create the supplied allocations."""
        # TODO(jaypipes): Retry the allocation writes on
        # ConcurrentUpdateDetected
        self._set_allocations(self._context, self.objects)

    @base.remotable
    def delete_all(self):
        self._delete_allocations(self._context, self.objects)

    def __repr__(self):
        strings = [repr(x) for x in self.objects]
        return "AllocationList[" + ", ".join(strings) + "]"


@base.NovaObjectRegistry.register
class Usage(base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'resource_class': fields.ResourceClassField(read_only=True),
        'usage': fields.NonNegativeIntegerField(),
    }

    @staticmethod
    def _from_db_object(context, target, source):
        for field in target.fields:
            if field not in ('resource_class'):
                setattr(target, field, source[field])

        if 'resource_class' not in target:
            target.resource_class = (
                target.fields['resource_class'].from_index(
                    source['resource_class_id']))

        target._context = context
        target.obj_reset_changes()
        return target


@base.NovaObjectRegistry.register
class UsageList(base.ObjectListBase, base.NovaObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'objects': fields.ListOfObjectsField('Usage'),
    }

    @staticmethod
    @db_api.api_context_manager.reader
    def _get_all_by_resource_provider_uuid(context, rp_uuid):
        query = (context.session.query(models.Inventory.resource_class_id,
                 func.coalesce(func.sum(models.Allocation.used), 0))
                 .join(models.ResourceProvider,
                       models.Inventory.resource_provider_id ==
                       models.ResourceProvider.id)
                 .outerjoin(models.Allocation,
                            sql.and_(models.Inventory.resource_provider_id ==
                                     models.Allocation.resource_provider_id,
                                     models.Inventory.resource_class_id ==
                                     models.Allocation.resource_class_id))
                 .filter(models.ResourceProvider.uuid == rp_uuid)
                 .group_by(models.Inventory.resource_class_id))
        result = [dict(resource_class_id=item[0], usage=item[1])
                  for item in query.all()]
        return result

    @base.remotable_classmethod
    def get_all_by_resource_provider_uuid(cls, context, rp_uuid):
        usage_list = cls._get_all_by_resource_provider_uuid(context, rp_uuid)
        return base.obj_make_list(context, cls(context), Usage, usage_list)

    def __repr__(self):
        strings = [repr(x) for x in self.objects]
        return "UsageList[" + ", ".join(strings) + "]"
