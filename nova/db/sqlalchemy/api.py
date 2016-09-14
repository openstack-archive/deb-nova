# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""Implementation of SQLAlchemy backend."""

import collections
import copy
import datetime
import functools
import inspect
import sys
import uuid

from oslo_db import api as oslo_db_api
from oslo_db import exception as db_exc
from oslo_db.sqlalchemy import enginefacade
from oslo_db.sqlalchemy import update_match
from oslo_db.sqlalchemy import utils as sqlalchemyutils
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import timeutils
from oslo_utils import uuidutils
import six
from six.moves import range
import sqlalchemy as sa
from sqlalchemy import and_
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy import MetaData
from sqlalchemy import or_
from sqlalchemy.orm import aliased
from sqlalchemy.orm import contains_eager
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import joinedload_all
from sqlalchemy.orm import noload
from sqlalchemy.orm import undefer
from sqlalchemy.schema import Table
from sqlalchemy import sql
from sqlalchemy.sql.expression import asc
from sqlalchemy.sql.expression import desc
from sqlalchemy.sql import false
from sqlalchemy.sql import func
from sqlalchemy.sql import null
from sqlalchemy.sql import true

from nova import block_device
from nova.compute import task_states
from nova.compute import vm_states
import nova.conf
import nova.context
from nova.db.sqlalchemy import models
from nova import exception
from nova.i18n import _, _LI, _LE, _LW
from nova import quota
from nova import safe_utils


CONF = nova.conf.CONF


LOG = logging.getLogger(__name__)

main_context_manager = enginefacade.transaction_context()
api_context_manager = enginefacade.transaction_context()


def _get_db_conf(conf_group, connection=None):
    kw = dict(
        connection=connection or conf_group.connection,
        slave_connection=conf_group.slave_connection,
        sqlite_fk=False,
        __autocommit=True,
        expire_on_commit=False,
        mysql_sql_mode=conf_group.mysql_sql_mode,
        idle_timeout=conf_group.idle_timeout,
        connection_debug=conf_group.connection_debug,
        max_pool_size=conf_group.max_pool_size,
        max_overflow=conf_group.max_overflow,
        pool_timeout=conf_group.pool_timeout,
        sqlite_synchronous=conf_group.sqlite_synchronous,
        connection_trace=conf_group.connection_trace,
        max_retries=conf_group.max_retries,
        retry_interval=conf_group.retry_interval)
    return kw


def _context_manager_from_context(context):
    if context:
        try:
            return context.db_connection
        except AttributeError:
            pass


def configure(conf):
    main_context_manager.configure(**_get_db_conf(conf.database))
    api_context_manager.configure(**_get_db_conf(conf.api_database))


def create_context_manager(connection=None):
    """Create a database context manager object.

    : param connection: The database connection string
    """
    ctxt_mgr = enginefacade.transaction_context()
    ctxt_mgr.configure(**_get_db_conf(CONF.database, connection=connection))
    return ctxt_mgr


def get_context_manager(context):
    """Get a database context manager object.

    :param context: The request context that can contain a context manager
    """
    return _context_manager_from_context(context) or main_context_manager


def get_engine(use_slave=False, context=None):
    """Get a database engine object.

    :param use_slave: Whether to use the slave connection
    :param context: The request context that can contain a context manager
    """
    ctxt_mgr = _context_manager_from_context(context) or main_context_manager
    return ctxt_mgr.get_legacy_facade().get_engine(use_slave=use_slave)


def get_api_engine():
    return api_context_manager.get_legacy_facade().get_engine()


_SHADOW_TABLE_PREFIX = 'shadow_'
_DEFAULT_QUOTA_NAME = 'default'
PER_PROJECT_QUOTAS = ['fixed_ips', 'floating_ips', 'networks']


def get_backend():
    """The backend is this module itself."""
    return sys.modules[__name__]


def require_context(f):
    """Decorator to require *any* user or admin context.

    This does no authorization for user or project access matching, see
    :py:func:`nova.context.authorize_project_context` and
    :py:func:`nova.context.authorize_user_context`.

    The first argument to the wrapped function must be the context.

    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        nova.context.require_context(args[0])
        return f(*args, **kwargs)
    return wrapper


def require_instance_exists_using_uuid(f):
    """Decorator to require the specified instance to exist.

    Requires the wrapped function to use context and instance_uuid as
    their first two arguments.
    """
    @functools.wraps(f)
    def wrapper(context, instance_uuid, *args, **kwargs):
        instance_get_by_uuid(context, instance_uuid)
        return f(context, instance_uuid, *args, **kwargs)

    return wrapper


def require_aggregate_exists(f):
    """Decorator to require the specified aggregate to exist.

    Requires the wrapped function to use context and aggregate_id as
    their first two arguments.
    """

    @functools.wraps(f)
    def wrapper(context, aggregate_id, *args, **kwargs):
        aggregate_get(context, aggregate_id)
        return f(context, aggregate_id, *args, **kwargs)
    return wrapper


def select_db_reader_mode(f):
    """Decorator to select synchronous or asynchronous reader mode.

    The kwarg argument 'use_slave' defines reader mode. Asynchronous reader
    will be used if 'use_slave' is True and synchronous reader otherwise.
    If 'use_slave' is not specified default value 'False' will be used.

    Wrapped function must have a context in the arguments.
    """

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        wrapped_func = safe_utils.get_wrapped_function(f)
        keyed_args = inspect.getcallargs(wrapped_func, *args, **kwargs)

        context = keyed_args['context']
        use_slave = keyed_args.get('use_slave', False)

        if use_slave:
            reader_mode = main_context_manager.async
        else:
            reader_mode = main_context_manager.reader

        with reader_mode.using(context):
            return f(*args, **kwargs)
    return wrapper


def pick_context_manager_writer(f):
    """Decorator to use a writer db context manager.

    The db context manager will be picked from the RequestContext.

    Wrapped function must have a RequestContext in the arguments.
    """
    @functools.wraps(f)
    def wrapped(context, *args, **kwargs):
        ctxt_mgr = get_context_manager(context)
        with ctxt_mgr.writer.using(context):
            return f(context, *args, **kwargs)
    return wrapped


def pick_context_manager_reader(f):
    """Decorator to use a reader db context manager.

    The db context manager will be picked from the RequestContext.

    Wrapped function must have a RequestContext in the arguments.
    """
    @functools.wraps(f)
    def wrapped(context, *args, **kwargs):
        ctxt_mgr = get_context_manager(context)
        with ctxt_mgr.reader.using(context):
            return f(context, *args, **kwargs)
    return wrapped


def pick_context_manager_reader_allow_async(f):
    """Decorator to use a reader.allow_async db context manager.

    The db context manager will be picked from the RequestContext.

    Wrapped function must have a RequestContext in the arguments.
    """
    @functools.wraps(f)
    def wrapped(context, *args, **kwargs):
        ctxt_mgr = get_context_manager(context)
        with ctxt_mgr.reader.allow_async.using(context):
            return f(context, *args, **kwargs)
    return wrapped


def model_query(context, model,
                args=None,
                read_deleted=None,
                project_only=False):
    """Query helper that accounts for context's `read_deleted` field.

    :param context:     NovaContext of the query.
    :param model:       Model to query. Must be a subclass of ModelBase.
    :param args:        Arguments to query. If None - model is used.
    :param read_deleted: If not None, overrides context's read_deleted field.
                        Permitted values are 'no', which does not return
                        deleted values; 'only', which only returns deleted
                        values; and 'yes', which does not filter deleted
                        values.
    :param project_only: If set and context is user-type, then restrict
                        query to match the context's project_id. If set to
                        'allow_none', restriction includes project_id = None.
    """

    if read_deleted is None:
        read_deleted = context.read_deleted

    query_kwargs = {}
    if 'no' == read_deleted:
        query_kwargs['deleted'] = False
    elif 'only' == read_deleted:
        query_kwargs['deleted'] = True
    elif 'yes' == read_deleted:
        pass
    else:
        raise ValueError(_("Unrecognized read_deleted value '%s'")
                           % read_deleted)

    query = sqlalchemyutils.model_query(
        model, context.session, args, **query_kwargs)

    # We can't use oslo.db model_query's project_id here, as it doesn't allow
    # us to return both our projects and unowned projects.
    if nova.context.is_user_context(context) and project_only:
        if project_only == 'allow_none':
            query = query.\
                filter(or_(model.project_id == context.project_id,
                           model.project_id == null()))
        else:
            query = query.filter_by(project_id=context.project_id)

    return query


def convert_objects_related_datetimes(values, *datetime_keys):
    if not datetime_keys:
        datetime_keys = ('created_at', 'deleted_at', 'updated_at')

    for key in datetime_keys:
        if key in values and values[key]:
            if isinstance(values[key], six.string_types):
                try:
                    values[key] = timeutils.parse_strtime(values[key])
                except ValueError:
                    # Try alternate parsing since parse_strtime will fail
                    # with say converting '2015-05-28T19:59:38+00:00'
                    values[key] = timeutils.parse_isotime(values[key])
            # NOTE(danms): Strip UTC timezones from datetimes, since they're
            # stored that way in the database
            values[key] = values[key].replace(tzinfo=None)
    return values


def _sync_instances(context, project_id, user_id):
    return dict(zip(('instances', 'cores', 'ram'),
                    _instance_data_get_for_user(context, project_id, user_id)))


def _sync_floating_ips(context, project_id, user_id):
    return dict(floating_ips=_floating_ip_count_by_project(
                context, project_id))


def _sync_fixed_ips(context, project_id, user_id):
    return dict(fixed_ips=_fixed_ip_count_by_project(context, project_id))


def _sync_security_groups(context, project_id, user_id):
    return dict(security_groups=_security_group_count_by_project_and_user(
                context, project_id, user_id))


def _sync_server_groups(context, project_id, user_id):
    return dict(server_groups=_instance_group_count_by_project_and_user(
                context, project_id, user_id))

QUOTA_SYNC_FUNCTIONS = {
    '_sync_instances': _sync_instances,
    '_sync_floating_ips': _sync_floating_ips,
    '_sync_fixed_ips': _sync_fixed_ips,
    '_sync_security_groups': _sync_security_groups,
    '_sync_server_groups': _sync_server_groups,
}

###################


def constraint(**conditions):
    return Constraint(conditions)


def equal_any(*values):
    return EqualityCondition(values)


def not_equal(*values):
    return InequalityCondition(values)


class Constraint(object):

    def __init__(self, conditions):
        self.conditions = conditions

    def apply(self, model, query):
        for key, condition in self.conditions.items():
            for clause in condition.clauses(getattr(model, key)):
                query = query.filter(clause)
        return query


class EqualityCondition(object):

    def __init__(self, values):
        self.values = values

    def clauses(self, field):
        # method signature requires us to return an iterable even if for OR
        # operator this will actually be a single clause
        return [or_(*[field == value for value in self.values])]


class InequalityCondition(object):

    def __init__(self, values):
        self.values = values

    def clauses(self, field):
        return [field != value for value in self.values]


###################


@pick_context_manager_writer
def service_destroy(context, service_id):
    service = service_get(context, service_id)

    model_query(context, models.Service).\
                filter_by(id=service_id).\
                soft_delete(synchronize_session=False)

    # TODO(sbauza): Remove the service_id filter in a later release
    # once we are sure that all compute nodes report the host field
    model_query(context, models.ComputeNode).\
                filter(or_(models.ComputeNode.service_id == service_id,
                           models.ComputeNode.host == service['host'])).\
                soft_delete(synchronize_session=False)


@pick_context_manager_reader
def service_get(context, service_id):
    query = model_query(context, models.Service).filter_by(id=service_id)

    result = query.first()
    if not result:
        raise exception.ServiceNotFound(service_id=service_id)

    return result


@pick_context_manager_reader_allow_async
def service_get_minimum_version(context, binaries):
    min_versions = context.session.query(
        models.Service.binary,
        func.min(models.Service.version)).\
                         filter(models.Service.binary.in_(binaries)).\
                         filter(models.Service.forced_down == false()).\
                         group_by(models.Service.binary)
    return dict(min_versions)


@pick_context_manager_reader
def service_get_all(context, disabled=None):
    query = model_query(context, models.Service)

    if disabled is not None:
        query = query.filter_by(disabled=disabled)

    return query.all()


@pick_context_manager_reader
def service_get_all_by_topic(context, topic):
    return model_query(context, models.Service, read_deleted="no").\
                filter_by(disabled=False).\
                filter_by(topic=topic).\
                all()


@pick_context_manager_reader
def service_get_by_host_and_topic(context, host, topic):
    return model_query(context, models.Service, read_deleted="no").\
                filter_by(disabled=False).\
                filter_by(host=host).\
                filter_by(topic=topic).\
                first()


@pick_context_manager_reader
def service_get_all_by_binary(context, binary, include_disabled=False):
    query = model_query(context, models.Service, read_deleted="no").\
                    filter_by(binary=binary)
    if not include_disabled:
        query = query.filter_by(disabled=False)
    return query.all()


@pick_context_manager_reader
def service_get_all_computes_by_hv_type(context, hv_type,
                                        include_disabled=False):
    query = model_query(context, models.Service, read_deleted="no").\
                    filter_by(binary='nova-compute')
    if not include_disabled:
        query = query.filter_by(disabled=False)
    query = query.join(models.ComputeNode,
                       models.Service.host == models.ComputeNode.host).\
                  filter(models.ComputeNode.hypervisor_type == hv_type).\
                  distinct('host')
    return query.all()


@pick_context_manager_reader
def service_get_by_host_and_binary(context, host, binary):
    result = model_query(context, models.Service, read_deleted="no").\
                    filter_by(host=host).\
                    filter_by(binary=binary).\
                    first()

    if not result:
        raise exception.HostBinaryNotFound(host=host, binary=binary)

    return result


@pick_context_manager_reader
def service_get_all_by_host(context, host):
    return model_query(context, models.Service, read_deleted="no").\
                filter_by(host=host).\
                all()


@pick_context_manager_reader_allow_async
def service_get_by_compute_host(context, host):
    result = model_query(context, models.Service, read_deleted="no").\
                filter_by(host=host).\
                filter_by(binary='nova-compute').\
                first()

    if not result:
        raise exception.ComputeHostNotFound(host=host)

    return result


@pick_context_manager_writer
def service_create(context, values):
    service_ref = models.Service()
    service_ref.update(values)
    if not CONF.enable_new_services:
        msg = _("New service disabled due to config option.")
        service_ref.disabled = True
        service_ref.disabled_reason = msg
    try:
        service_ref.save(context.session)
    except db_exc.DBDuplicateEntry as e:
        if 'binary' in e.columns:
            raise exception.ServiceBinaryExists(host=values.get('host'),
                        binary=values.get('binary'))
        raise exception.ServiceTopicExists(host=values.get('host'),
                        topic=values.get('topic'))
    return service_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def service_update(context, service_id, values):
    service_ref = service_get(context, service_id)
    # Only servicegroup.drivers.db.DbDriver._report_state() updates
    # 'report_count', so if that value changes then store the timestamp
    # as the last time we got a state report.
    if 'report_count' in values:
        if values['report_count'] > service_ref.report_count:
            service_ref.last_seen_up = timeutils.utcnow()
    service_ref.update(values)

    return service_ref


###################


def _compute_node_select(context, filters=None, limit=None, marker=None):
    if filters is None:
        filters = {}

    cn_tbl = sa.alias(models.ComputeNode.__table__, name='cn')
    select = sa.select([cn_tbl])

    if context.read_deleted == "no":
        select = select.where(cn_tbl.c.deleted == 0)
    if "compute_id" in filters:
        select = select.where(cn_tbl.c.id == filters["compute_id"])
    if "service_id" in filters:
        select = select.where(cn_tbl.c.service_id == filters["service_id"])
    if "host" in filters:
        select = select.where(cn_tbl.c.host == filters["host"])
    if "hypervisor_hostname" in filters:
        hyp_hostname = filters["hypervisor_hostname"]
        select = select.where(cn_tbl.c.hypervisor_hostname == hyp_hostname)
    if marker is not None:
        try:
            compute_node_get(context, marker)
        except exception.ComputeHostNotFound:
            raise exception.MarkerNotFound(marker=marker)
        select = select.where(cn_tbl.c.id > marker)
    if limit is not None:
        select = select.limit(limit)
    # Explictly order by id, so we're not dependent on the native sort
    # order of the underlying DB.
    select = select.order_by(asc("id"))
    return select


def _compute_node_fetchall(context, filters=None, limit=None, marker=None):
    select = _compute_node_select(context, filters, limit=limit, marker=marker)
    engine = get_engine(context)
    conn = engine.connect()

    results = conn.execute(select).fetchall()

    # Callers expect dict-like objects, not SQLAlchemy RowProxy objects...
    results = [dict(r) for r in results]
    conn.close()
    return results


@pick_context_manager_reader
def compute_node_get(context, compute_id):
    results = _compute_node_fetchall(context, {"compute_id": compute_id})
    if not results:
        raise exception.ComputeHostNotFound(host=compute_id)
    return results[0]


@pick_context_manager_reader
def compute_node_get_model(context, compute_id):
    # TODO(edleafe): remove once the compute node resource provider migration
    # is complete, and this distinction is no longer necessary.
    result = model_query(context, models.ComputeNode).\
            filter_by(id=compute_id).\
            first()
    if not result:
        raise exception.ComputeHostNotFound(host=compute_id)
    return result


@pick_context_manager_reader
def compute_nodes_get_by_service_id(context, service_id):
    results = _compute_node_fetchall(context, {"service_id": service_id})
    if not results:
        raise exception.ServiceNotFound(service_id=service_id)
    return results


@pick_context_manager_reader
def compute_node_get_by_host_and_nodename(context, host, nodename):
    results = _compute_node_fetchall(context,
            {"host": host, "hypervisor_hostname": nodename})
    if not results:
        raise exception.ComputeHostNotFound(host=host)
    return results[0]


@pick_context_manager_reader_allow_async
def compute_node_get_all_by_host(context, host):
    results = _compute_node_fetchall(context, {"host": host})
    if not results:
        raise exception.ComputeHostNotFound(host=host)
    return results


@pick_context_manager_reader
def compute_node_get_all(context):
    return _compute_node_fetchall(context)


@pick_context_manager_reader
def compute_node_get_all_by_pagination(context, limit=None, marker=None):
    return _compute_node_fetchall(context, limit=limit, marker=marker)


@pick_context_manager_reader
def compute_node_search_by_hypervisor(context, hypervisor_match):
    field = models.ComputeNode.hypervisor_hostname
    return model_query(context, models.ComputeNode).\
            filter(field.like('%%%s%%' % hypervisor_match)).\
            all()


@pick_context_manager_writer
def compute_node_create(context, values):
    """Creates a new ComputeNode and populates the capacity fields
    with the most recent data.
    """
    convert_objects_related_datetimes(values)

    compute_node_ref = models.ComputeNode()
    compute_node_ref.update(values)
    compute_node_ref.save(context.session)

    return compute_node_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def compute_node_update(context, compute_id, values):
    """Updates the ComputeNode record with the most recent data."""

    compute_ref = compute_node_get_model(context, compute_id)
    # Always update this, even if there's going to be no other
    # changes in data.  This ensures that we invalidate the
    # scheduler cache of compute node data in case of races.
    values['updated_at'] = timeutils.utcnow()
    convert_objects_related_datetimes(values)
    compute_ref.update(values)

    return compute_ref


@pick_context_manager_writer
def compute_node_delete(context, compute_id):
    """Delete a ComputeNode record."""
    result = model_query(context, models.ComputeNode).\
             filter_by(id=compute_id).\
             soft_delete(synchronize_session=False)

    if not result:
        raise exception.ComputeHostNotFound(host=compute_id)


@pick_context_manager_reader
def compute_node_statistics(context):
    """Compute statistics over all compute nodes."""
    engine = get_engine(context)
    services_tbl = models.Service.__table__

    inner_sel = sa.alias(_compute_node_select(context), name='inner_sel')

    # TODO(sbauza): Remove the service_id filter in a later release
    # once we are sure that all compute nodes report the host field
    j = sa.join(
        inner_sel, services_tbl,
        sql.and_(
            sql.or_(
                inner_sel.c.host == services_tbl.c.host,
                inner_sel.c.service_id == services_tbl.c.id
            ),
            services_tbl.c.disabled == false(),
            services_tbl.c.binary == 'nova-compute'
        )
    )

    # NOTE(jaypipes): This COALESCE() stuff is temporary while the data
    # migration to the new resource providers inventories and allocations
    # tables is completed.
    agg_cols = [
        func.count().label('count'),
        sql.func.sum(
            inner_sel.c.vcpus
        ).label('vcpus'),
        sql.func.sum(
            inner_sel.c.memory_mb
        ).label('memory_mb'),
        sql.func.sum(
            inner_sel.c.local_gb
        ).label('local_gb'),
        sql.func.sum(
            inner_sel.c.vcpus_used
        ).label('vcpus_used'),
        sql.func.sum(
            inner_sel.c.memory_mb_used
        ).label('memory_mb_used'),
        sql.func.sum(
            inner_sel.c.local_gb_used
        ).label('local_gb_used'),
        sql.func.sum(
            inner_sel.c.free_ram_mb
        ).label('free_ram_mb'),
        sql.func.sum(
            inner_sel.c.free_disk_gb
        ).label('free_disk_gb'),
        sql.func.sum(
            inner_sel.c.current_workload
        ).label('current_workload'),
        sql.func.sum(
            inner_sel.c.running_vms
        ).label('running_vms'),
        sql.func.sum(
            inner_sel.c.disk_available_least
        ).label('disk_available_least'),
    ]
    select = sql.select(agg_cols).select_from(j)
    conn = engine.connect()

    results = conn.execute(select).fetchone()

    # Build a dict of the info--making no assumptions about result
    fields = ('count', 'vcpus', 'memory_mb', 'local_gb', 'vcpus_used',
              'memory_mb_used', 'local_gb_used', 'free_ram_mb', 'free_disk_gb',
              'current_workload', 'running_vms', 'disk_available_least')
    results = {field: int(results[idx] or 0)
               for idx, field in enumerate(fields)}
    conn.close()
    return results


###################


@main_context_manager.writer
def certificate_create(context, values):
    certificate_ref = models.Certificate()
    for (key, value) in values.items():
        certificate_ref[key] = value
    certificate_ref.save(context.session)
    return certificate_ref


@main_context_manager.reader
def certificate_get_all_by_project(context, project_id):
    return model_query(context, models.Certificate, read_deleted="no").\
                   filter_by(project_id=project_id).\
                   all()


@main_context_manager.reader
def certificate_get_all_by_user(context, user_id):
    return model_query(context, models.Certificate, read_deleted="no").\
                   filter_by(user_id=user_id).\
                   all()


@main_context_manager.reader
def certificate_get_all_by_user_and_project(context, user_id, project_id):
    return model_query(context, models.Certificate, read_deleted="no").\
                   filter_by(user_id=user_id).\
                   filter_by(project_id=project_id).\
                   all()


###################


@require_context
@main_context_manager.reader
def floating_ip_get(context, id):
    try:
        result = model_query(context, models.FloatingIp, project_only=True).\
                     filter_by(id=id).\
                     options(joinedload_all('fixed_ip.instance')).\
                     first()

        if not result:
            raise exception.FloatingIpNotFound(id=id)
    except db_exc.DBError:
        LOG.warning(_LW("Invalid floating IP ID %s in request"), id)
        raise exception.InvalidID(id=id)
    return result


@require_context
@main_context_manager.reader
def floating_ip_get_pools(context):
    pools = []
    for result in model_query(context, models.FloatingIp,
                              (models.FloatingIp.pool,)).distinct():
        pools.append({'name': result[0]})
    return pools


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True,
                           retry_on_request=True)
@main_context_manager.writer
def floating_ip_allocate_address(context, project_id, pool,
                                 auto_assigned=False):
    nova.context.authorize_project_context(context, project_id)
    floating_ip_ref = model_query(context, models.FloatingIp,
                                  read_deleted="no").\
        filter_by(fixed_ip_id=None).\
        filter_by(project_id=None).\
        filter_by(pool=pool).\
        first()

    if not floating_ip_ref:
        raise exception.NoMoreFloatingIps()

    params = {'project_id': project_id, 'auto_assigned': auto_assigned}

    rows_update = model_query(context, models.FloatingIp, read_deleted="no").\
        filter_by(id=floating_ip_ref['id']).\
        filter_by(fixed_ip_id=None).\
        filter_by(project_id=None).\
        filter_by(pool=pool).\
        update(params, synchronize_session='evaluate')

    if not rows_update:
        LOG.debug('The row was updated in a concurrent transaction, '
                  'we will fetch another one')
        raise db_exc.RetryRequest(exception.FloatingIpAllocateFailed())

    return floating_ip_ref['address']


@require_context
@main_context_manager.writer
def floating_ip_bulk_create(context, ips, want_result=True):
    try:
        tab = models.FloatingIp().__table__
        context.session.execute(tab.insert(), ips)
    except db_exc.DBDuplicateEntry as e:
        raise exception.FloatingIpExists(address=e.value)

    if want_result:
        return model_query(context, models.FloatingIp).filter(
            models.FloatingIp.address.in_(
                [ip['address'] for ip in ips])).all()


def _ip_range_splitter(ips, block_size=256):
    """Yields blocks of IPs no more than block_size elements long."""
    out = []
    count = 0
    for ip in ips:
        out.append(ip['address'])
        count += 1

        if count > block_size - 1:
            yield out
            out = []
            count = 0

    if out:
        yield out


@require_context
@main_context_manager.writer
def floating_ip_bulk_destroy(context, ips):
    project_id_to_quota_count = collections.defaultdict(int)
    for ip_block in _ip_range_splitter(ips):
        # Find any floating IPs that were not auto_assigned and
        # thus need quota released.
        query = model_query(context, models.FloatingIp).\
            filter(models.FloatingIp.address.in_(ip_block)).\
            filter_by(auto_assigned=False)
        for row in query.all():
            # The count is negative since we release quota by
            # reserving negative quota.
            project_id_to_quota_count[row['project_id']] -= 1
        # Delete the floating IPs.
        model_query(context, models.FloatingIp).\
            filter(models.FloatingIp.address.in_(ip_block)).\
            soft_delete(synchronize_session='fetch')

    # Delete the quotas, if needed.
    # Quota update happens in a separate transaction, so previous must have
    # been committed first.
    for project_id, count in project_id_to_quota_count.items():
        try:
            reservations = quota.QUOTAS.reserve(context,
                                                project_id=project_id,
                                                floating_ips=count)
            quota.QUOTAS.commit(context, reservations, project_id=project_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE("Failed to update usages bulk "
                                  "deallocating floating IP"))


@require_context
@main_context_manager.writer
def floating_ip_create(context, values):
    floating_ip_ref = models.FloatingIp()
    floating_ip_ref.update(values)
    try:
        floating_ip_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.FloatingIpExists(address=values['address'])
    return floating_ip_ref


def _floating_ip_count_by_project(context, project_id):
    nova.context.authorize_project_context(context, project_id)
    # TODO(tr3buchet): why leave auto_assigned floating IPs out?
    return model_query(context, models.FloatingIp, read_deleted="no").\
                   filter_by(project_id=project_id).\
                   filter_by(auto_assigned=False).\
                   count()


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def floating_ip_fixed_ip_associate(context, floating_address,
                                   fixed_address, host):
    fixed_ip_ref = model_query(context, models.FixedIp).\
                     filter_by(address=fixed_address).\
                     options(joinedload('network')).\
                     first()
    if not fixed_ip_ref:
        raise exception.FixedIpNotFoundForAddress(address=fixed_address)
    rows = model_query(context, models.FloatingIp).\
                filter_by(address=floating_address).\
                filter(models.FloatingIp.project_id ==
                       context.project_id).\
                filter(or_(models.FloatingIp.fixed_ip_id ==
                           fixed_ip_ref['id'],
                           models.FloatingIp.fixed_ip_id.is_(None))).\
                update({'fixed_ip_id': fixed_ip_ref['id'], 'host': host})

    if not rows:
        raise exception.FloatingIpAssociateFailed(address=floating_address)

    return fixed_ip_ref


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def floating_ip_deallocate(context, address):
    return model_query(context, models.FloatingIp).\
        filter_by(address=address).\
        filter(and_(models.FloatingIp.project_id != null()),
                    models.FloatingIp.fixed_ip_id == null()).\
        update({'project_id': None,
                'host': None,
                'auto_assigned': False},
               synchronize_session=False)


@require_context
@main_context_manager.writer
def floating_ip_destroy(context, address):
    model_query(context, models.FloatingIp).\
            filter_by(address=address).\
            delete()


@require_context
@main_context_manager.writer
def floating_ip_disassociate(context, address):
    floating_ip_ref = model_query(context,
                                  models.FloatingIp).\
                        filter_by(address=address).\
                        first()
    if not floating_ip_ref:
        raise exception.FloatingIpNotFoundForAddress(address=address)

    fixed_ip_ref = model_query(context, models.FixedIp).\
        filter_by(id=floating_ip_ref['fixed_ip_id']).\
        options(joinedload('network')).\
        first()
    floating_ip_ref.fixed_ip_id = None
    floating_ip_ref.host = None

    return fixed_ip_ref


def _floating_ip_get_all(context):
    return model_query(context, models.FloatingIp, read_deleted="no")


@main_context_manager.reader
def floating_ip_get_all(context):
    floating_ip_refs = _floating_ip_get_all(context).\
                       options(joinedload('fixed_ip')).\
                       all()
    if not floating_ip_refs:
        raise exception.NoFloatingIpsDefined()
    return floating_ip_refs


@main_context_manager.reader
def floating_ip_get_all_by_host(context, host):
    floating_ip_refs = _floating_ip_get_all(context).\
                       filter_by(host=host).\
                       options(joinedload('fixed_ip')).\
                       all()
    if not floating_ip_refs:
        raise exception.FloatingIpNotFoundForHost(host=host)
    return floating_ip_refs


@require_context
@main_context_manager.reader
def floating_ip_get_all_by_project(context, project_id):
    nova.context.authorize_project_context(context, project_id)
    # TODO(tr3buchet): why do we not want auto_assigned floating IPs here?
    return _floating_ip_get_all(context).\
                         filter_by(project_id=project_id).\
                         filter_by(auto_assigned=False).\
                         options(joinedload_all('fixed_ip.instance')).\
                         all()


@require_context
@main_context_manager.reader
def floating_ip_get_by_address(context, address):
    return _floating_ip_get_by_address(context, address)


def _floating_ip_get_by_address(context, address):

    # if address string is empty explicitly set it to None
    if not address:
        address = None
    try:
        result = model_query(context, models.FloatingIp).\
                    filter_by(address=address).\
                    options(joinedload_all('fixed_ip.instance')).\
                    first()

        if not result:
            raise exception.FloatingIpNotFoundForAddress(address=address)
    except db_exc.DBError:
        msg = _("Invalid floating IP %s in request") % address
        LOG.warning(msg)
        raise exception.InvalidIpAddressError(msg)

    # If the floating IP has a project ID set, check to make sure
    # the non-admin user has access.
    if result.project_id and nova.context.is_user_context(context):
        nova.context.authorize_project_context(context, result.project_id)

    return result


@require_context
@main_context_manager.reader
def floating_ip_get_by_fixed_address(context, fixed_address):
    return model_query(context, models.FloatingIp).\
                       outerjoin(models.FixedIp,
                                 models.FixedIp.id ==
                                 models.FloatingIp.fixed_ip_id).\
                       filter(models.FixedIp.address == fixed_address).\
                       all()


@require_context
@main_context_manager.reader
def floating_ip_get_by_fixed_ip_id(context, fixed_ip_id):
    return model_query(context, models.FloatingIp).\
                filter_by(fixed_ip_id=fixed_ip_id).\
                all()


@require_context
@main_context_manager.writer
def floating_ip_update(context, address, values):
    float_ip_ref = _floating_ip_get_by_address(context, address)
    float_ip_ref.update(values)
    try:
        float_ip_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.FloatingIpExists(address=values['address'])
    return float_ip_ref


###################


@require_context
@main_context_manager.reader
def dnsdomain_get(context, fqdomain):
    return model_query(context, models.DNSDomain, read_deleted="no").\
               filter_by(domain=fqdomain).\
               with_lockmode('update').\
               first()


def _dnsdomain_get_or_create(context, fqdomain):
    domain_ref = dnsdomain_get(context, fqdomain)
    if not domain_ref:
        dns_ref = models.DNSDomain()
        dns_ref.update({'domain': fqdomain,
                        'availability_zone': None,
                        'project_id': None})
        return dns_ref

    return domain_ref


@main_context_manager.writer
def dnsdomain_register_for_zone(context, fqdomain, zone):
    domain_ref = _dnsdomain_get_or_create(context, fqdomain)
    domain_ref.scope = 'private'
    domain_ref.availability_zone = zone
    context.session.add(domain_ref)


@main_context_manager.writer
def dnsdomain_register_for_project(context, fqdomain, project):
    domain_ref = _dnsdomain_get_or_create(context, fqdomain)
    domain_ref.scope = 'public'
    domain_ref.project_id = project
    context.session.add(domain_ref)


@main_context_manager.writer
def dnsdomain_unregister(context, fqdomain):
    model_query(context, models.DNSDomain).\
                 filter_by(domain=fqdomain).\
                 delete()


@main_context_manager.reader
def dnsdomain_get_all(context):
    return model_query(context, models.DNSDomain, read_deleted="no").all()


###################


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True,
                           retry_on_request=True)
@main_context_manager.writer
def fixed_ip_associate(context, address, instance_uuid, network_id=None,
                       reserved=False, virtual_interface_id=None):
    """Keyword arguments:
    reserved -- should be a boolean value(True or False), exact value will be
    used to filter on the fixed IP address
    """
    if not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(uuid=instance_uuid)

    network_or_none = or_(models.FixedIp.network_id == network_id,
                          models.FixedIp.network_id == null())
    fixed_ip_ref = model_query(context, models.FixedIp, read_deleted="no").\
                           filter(network_or_none).\
                           filter_by(reserved=reserved).\
                           filter_by(address=address).\
                           first()

    if fixed_ip_ref is None:
        raise exception.FixedIpNotFoundForNetwork(address=address,
                                        network_uuid=network_id)
    if fixed_ip_ref.instance_uuid:
        raise exception.FixedIpAlreadyInUse(address=address,
                                            instance_uuid=instance_uuid)

    params = {'instance_uuid': instance_uuid,
              'allocated': virtual_interface_id is not None}
    if not fixed_ip_ref.network_id:
        params['network_id'] = network_id
    if virtual_interface_id:
        params['virtual_interface_id'] = virtual_interface_id

    rows_updated = model_query(context, models.FixedIp, read_deleted="no").\
                            filter_by(id=fixed_ip_ref.id).\
                            filter(network_or_none).\
                            filter_by(reserved=reserved).\
                            filter_by(address=address).\
                            update(params, synchronize_session='evaluate')

    if not rows_updated:
        LOG.debug('The row was updated in a concurrent transaction, '
                  'we will fetch another row')
        raise db_exc.RetryRequest(
            exception.FixedIpAssociateFailed(net=network_id))

    return fixed_ip_ref


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True,
                           retry_on_request=True)
@main_context_manager.writer
def fixed_ip_associate_pool(context, network_id, instance_uuid=None,
                            host=None, virtual_interface_id=None):
    """allocate a fixed ip out of a fixed ip network pool.

    This allocates an unallocated fixed ip out of a specified
    network. We sort by updated_at to hand out the oldest address in
    the list.

    """
    if instance_uuid and not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(uuid=instance_uuid)

    network_or_none = or_(models.FixedIp.network_id == network_id,
                          models.FixedIp.network_id == null())
    fixed_ip_ref = model_query(context, models.FixedIp, read_deleted="no").\
                           filter(network_or_none).\
                           filter_by(reserved=False).\
                           filter_by(instance_uuid=None).\
                           filter_by(host=None).\
                           filter_by(leased=False).\
                           order_by(asc(models.FixedIp.updated_at)).\
                           first()

    if not fixed_ip_ref:
        raise exception.NoMoreFixedIps(net=network_id)

    params = {'allocated': virtual_interface_id is not None}
    if fixed_ip_ref['network_id'] is None:
        params['network_id'] = network_id
    if instance_uuid:
        params['instance_uuid'] = instance_uuid
    if host:
        params['host'] = host
    if virtual_interface_id:
        params['virtual_interface_id'] = virtual_interface_id

    rows_updated = model_query(context, models.FixedIp, read_deleted="no").\
        filter_by(id=fixed_ip_ref['id']).\
        filter_by(network_id=fixed_ip_ref['network_id']).\
        filter_by(reserved=False).\
        filter_by(instance_uuid=None).\
        filter_by(host=None).\
        filter_by(leased=False).\
        filter_by(address=fixed_ip_ref['address']).\
        update(params, synchronize_session='evaluate')

    if not rows_updated:
        LOG.debug('The row was updated in a concurrent transaction, '
                  'we will fetch another row')
        raise db_exc.RetryRequest(
            exception.FixedIpAssociateFailed(net=network_id))

    return fixed_ip_ref


@require_context
@main_context_manager.writer
def fixed_ip_create(context, values):
    fixed_ip_ref = models.FixedIp()
    fixed_ip_ref.update(values)
    try:
        fixed_ip_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.FixedIpExists(address=values['address'])
    return fixed_ip_ref


@require_context
@main_context_manager.writer
def fixed_ip_bulk_create(context, ips):
    try:
        tab = models.FixedIp.__table__
        context.session.execute(tab.insert(), ips)
    except db_exc.DBDuplicateEntry as e:
        raise exception.FixedIpExists(address=e.value)


@require_context
@main_context_manager.writer
def fixed_ip_disassociate(context, address):
    _fixed_ip_get_by_address(context, address).update(
        {'instance_uuid': None,
         'virtual_interface_id': None})


@main_context_manager.writer
def fixed_ip_disassociate_all_by_timeout(context, host, time):
    # NOTE(vish): only update fixed ips that "belong" to this
    #             host; i.e. the network host or the instance
    #             host matches. Two queries necessary because
    #             join with update doesn't work.
    host_filter = or_(and_(models.Instance.host == host,
                           models.Network.multi_host == true()),
                      models.Network.host == host)
    result = model_query(context, models.FixedIp, (models.FixedIp.id,),
                         read_deleted="no").\
            filter(models.FixedIp.allocated == false()).\
            filter(models.FixedIp.updated_at < time).\
            join((models.Network,
                  models.Network.id == models.FixedIp.network_id)).\
            join((models.Instance,
                  models.Instance.uuid == models.FixedIp.instance_uuid)).\
            filter(host_filter).\
            all()
    fixed_ip_ids = [fip[0] for fip in result]
    if not fixed_ip_ids:
        return 0
    result = model_query(context, models.FixedIp).\
                         filter(models.FixedIp.id.in_(fixed_ip_ids)).\
                         update({'instance_uuid': None,
                                 'leased': False,
                                 'updated_at': timeutils.utcnow()},
                                synchronize_session='fetch')
    return result


@require_context
@main_context_manager.reader
def fixed_ip_get(context, id, get_network=False):
    query = model_query(context, models.FixedIp).filter_by(id=id)
    if get_network:
        query = query.options(joinedload('network'))
    result = query.first()
    if not result:
        raise exception.FixedIpNotFound(id=id)

    # FIXME(sirp): shouldn't we just use project_only here to restrict the
    # results?
    if (nova.context.is_user_context(context) and
            result['instance_uuid'] is not None):
        instance = instance_get_by_uuid(context.elevated(read_deleted='yes'),
                                        result['instance_uuid'])
        nova.context.authorize_project_context(context, instance.project_id)

    return result


@main_context_manager.reader
def fixed_ip_get_all(context):
    result = model_query(context, models.FixedIp, read_deleted="yes").all()
    if not result:
        raise exception.NoFixedIpsDefined()

    return result


@require_context
@main_context_manager.reader
def fixed_ip_get_by_address(context, address, columns_to_join=None):
    return _fixed_ip_get_by_address(context, address,
                                    columns_to_join=columns_to_join)


def _fixed_ip_get_by_address(context, address, columns_to_join=None):
    if columns_to_join is None:
        columns_to_join = []

    try:
        result = model_query(context, models.FixedIp)
        for column in columns_to_join:
            result = result.options(joinedload_all(column))
        result = result.filter_by(address=address).first()
        if not result:
            raise exception.FixedIpNotFoundForAddress(address=address)
    except db_exc.DBError:
        msg = _("Invalid fixed IP Address %s in request") % address
        LOG.warning(msg)
        raise exception.FixedIpInvalid(msg)

    # NOTE(sirp): shouldn't we just use project_only here to restrict the
    # results?
    if (nova.context.is_user_context(context) and
            result['instance_uuid'] is not None):
        instance = _instance_get_by_uuid(
            context.elevated(read_deleted='yes'),
            result['instance_uuid'])
        nova.context.authorize_project_context(context,
                                               instance.project_id)
    return result


@require_context
@main_context_manager.reader
def fixed_ip_get_by_floating_address(context, floating_address):
    return model_query(context, models.FixedIp).\
                       join(models.FloatingIp,
                            models.FloatingIp.fixed_ip_id ==
                            models.FixedIp.id).\
                       filter(models.FloatingIp.address == floating_address).\
                       first()
    # NOTE(tr3buchet) please don't invent an exception here, None is fine


@require_context
@main_context_manager.reader
def fixed_ip_get_by_instance(context, instance_uuid):
    if not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(uuid=instance_uuid)

    vif_and = and_(models.VirtualInterface.id ==
                   models.FixedIp.virtual_interface_id,
                   models.VirtualInterface.deleted == 0)
    result = model_query(context, models.FixedIp, read_deleted="no").\
                 filter_by(instance_uuid=instance_uuid).\
                 outerjoin(models.VirtualInterface, vif_and).\
                 options(contains_eager("virtual_interface")).\
                 options(joinedload('network')).\
                 options(joinedload('floating_ips')).\
                 order_by(asc(models.VirtualInterface.created_at),
                          asc(models.VirtualInterface.id)).\
                 all()

    if not result:
        raise exception.FixedIpNotFoundForInstance(instance_uuid=instance_uuid)

    return result


@main_context_manager.reader
def fixed_ip_get_by_host(context, host):
    instance_uuids = _instance_get_all_uuids_by_host(context, host)
    if not instance_uuids:
        return []

    return model_query(context, models.FixedIp).\
             filter(models.FixedIp.instance_uuid.in_(instance_uuids)).\
             all()


@require_context
@main_context_manager.reader
def fixed_ip_get_by_network_host(context, network_id, host):
    result = model_query(context, models.FixedIp, read_deleted="no").\
                 filter_by(network_id=network_id).\
                 filter_by(host=host).\
                 first()

    if not result:
        raise exception.FixedIpNotFoundForNetworkHost(network_id=network_id,
                                                      host=host)
    return result


@require_context
@main_context_manager.reader
def fixed_ips_by_virtual_interface(context, vif_id):
    result = model_query(context, models.FixedIp, read_deleted="no").\
                 filter_by(virtual_interface_id=vif_id).\
                 options(joinedload('network')).\
                 options(joinedload('floating_ips')).\
                 all()

    return result


@require_context
@main_context_manager.writer
def fixed_ip_update(context, address, values):
    _fixed_ip_get_by_address(context, address).update(values)


def _fixed_ip_count_by_project(context, project_id):
    nova.context.authorize_project_context(context, project_id)
    return model_query(context, models.FixedIp, (models.FixedIp.id,),
                       read_deleted="no").\
                join((models.Instance,
                      models.Instance.uuid == models.FixedIp.instance_uuid)).\
                filter(models.Instance.project_id == project_id).\
                count()


###################


@require_context
@pick_context_manager_writer
def virtual_interface_create(context, values):
    """Create a new virtual interface record in the database.

    :param values: = dict containing column values
    """
    try:
        vif_ref = models.VirtualInterface()
        vif_ref.update(values)
        vif_ref.save(context.session)
    except db_exc.DBError:
        LOG.exception(_LE("VIF creation failed with a database error."))
        raise exception.VirtualInterfaceCreateException()

    return vif_ref


def _virtual_interface_query(context):
    return model_query(context, models.VirtualInterface, read_deleted="no")


@require_context
@pick_context_manager_writer
def virtual_interface_update(context, address, values):
    vif_ref = virtual_interface_get_by_address(context, address)
    vif_ref.update(values)
    vif_ref.save(context.session)
    return vif_ref


@require_context
@pick_context_manager_reader
def virtual_interface_get(context, vif_id):
    """Gets a virtual interface from the table.

    :param vif_id: = id of the virtual interface
    """
    vif_ref = _virtual_interface_query(context).\
                      filter_by(id=vif_id).\
                      first()
    return vif_ref


@require_context
@pick_context_manager_reader
def virtual_interface_get_by_address(context, address):
    """Gets a virtual interface from the table.

    :param address: = the address of the interface you're looking to get
    """
    try:
        vif_ref = _virtual_interface_query(context).\
                          filter_by(address=address).\
                          first()
    except db_exc.DBError:
        msg = _("Invalid virtual interface address %s in request") % address
        LOG.warning(msg)
        raise exception.InvalidIpAddressError(msg)
    return vif_ref


@require_context
@pick_context_manager_reader
def virtual_interface_get_by_uuid(context, vif_uuid):
    """Gets a virtual interface from the table.

    :param vif_uuid: the uuid of the interface you're looking to get
    """
    vif_ref = _virtual_interface_query(context).\
                      filter_by(uuid=vif_uuid).\
                      first()
    return vif_ref


@require_context
@require_instance_exists_using_uuid
@pick_context_manager_reader_allow_async
def virtual_interface_get_by_instance(context, instance_uuid):
    """Gets all virtual interfaces for instance.

    :param instance_uuid: = uuid of the instance to retrieve vifs for
    """
    vif_refs = _virtual_interface_query(context).\
                       filter_by(instance_uuid=instance_uuid).\
                       order_by(asc("created_at"), asc("id")).\
                       all()
    return vif_refs


@require_context
@pick_context_manager_reader
def virtual_interface_get_by_instance_and_network(context, instance_uuid,
                                                  network_id):
    """Gets virtual interface for instance that's associated with network."""
    vif_ref = _virtual_interface_query(context).\
                      filter_by(instance_uuid=instance_uuid).\
                      filter_by(network_id=network_id).\
                      first()
    return vif_ref


@require_context
@pick_context_manager_writer
def virtual_interface_delete_by_instance(context, instance_uuid):
    """Delete virtual interface records that are associated
    with the instance given by instance_id.

    :param instance_uuid: = uuid of instance
    """
    _virtual_interface_query(context).\
           filter_by(instance_uuid=instance_uuid).\
           soft_delete()


@require_context
@pick_context_manager_writer
def virtual_interface_delete(context, id):
    """Delete virtual interface records.

    :param id: id of the interface
    """
    _virtual_interface_query(context).\
        filter_by(id=id).\
        soft_delete()


@require_context
@pick_context_manager_reader
def virtual_interface_get_all(context):
    """Get all vifs."""
    vif_refs = _virtual_interface_query(context).all()
    return vif_refs


###################


def _metadata_refs(metadata_dict, meta_class):
    metadata_refs = []
    if metadata_dict:
        for k, v in metadata_dict.items():
            metadata_ref = meta_class()
            metadata_ref['key'] = k
            metadata_ref['value'] = v
            metadata_refs.append(metadata_ref)
    return metadata_refs


def _validate_unique_server_name(context, name):
    if not CONF.osapi_compute_unique_server_name_scope:
        return

    lowername = name.lower()
    base_query = model_query(context, models.Instance, read_deleted='no').\
            filter(func.lower(models.Instance.hostname) == lowername)

    if CONF.osapi_compute_unique_server_name_scope == 'project':
        instance_with_same_name = base_query.\
                        filter_by(project_id=context.project_id).\
                        count()

    elif CONF.osapi_compute_unique_server_name_scope == 'global':
        instance_with_same_name = base_query.count()

    else:
        return

    if instance_with_same_name > 0:
        raise exception.InstanceExists(name=lowername)


def _handle_objects_related_type_conversions(values):
    """Make sure that certain things in values (which may have come from
    an objects.instance.Instance object) are in suitable form for the
    database.
    """
    # NOTE(danms): Make sure IP addresses are passed as strings to
    # the database engine
    for key in ('access_ip_v4', 'access_ip_v6'):
        if key in values and values[key] is not None:
            values[key] = str(values[key])

    datetime_keys = ('created_at', 'deleted_at', 'updated_at',
                     'launched_at', 'terminated_at')
    convert_objects_related_datetimes(values, *datetime_keys)


def _check_instance_exists_in_project(context, instance_uuid):
    if not model_query(context, models.Instance, read_deleted="no",
                       project_only=True).filter_by(
                       uuid=instance_uuid).first():
        raise exception.InstanceNotFound(instance_id=instance_uuid)


@require_context
@pick_context_manager_writer
def instance_create(context, values):
    """Create a new Instance record in the database.

    context - request context object
    values - dict containing column values.
    """

    security_group_ensure_default(context)

    values = values.copy()
    values['metadata'] = _metadata_refs(
            values.get('metadata'), models.InstanceMetadata)

    values['system_metadata'] = _metadata_refs(
            values.get('system_metadata'), models.InstanceSystemMetadata)
    _handle_objects_related_type_conversions(values)

    instance_ref = models.Instance()
    if not values.get('uuid'):
        values['uuid'] = str(uuid.uuid4())
    instance_ref['info_cache'] = models.InstanceInfoCache()
    info_cache = values.pop('info_cache', None)
    if info_cache is not None:
        instance_ref['info_cache'].update(info_cache)
    security_groups = values.pop('security_groups', [])
    instance_ref['extra'] = models.InstanceExtra()
    instance_ref['extra'].update(
        {'numa_topology': None,
         'pci_requests': None,
         'vcpu_model': None,
         })
    instance_ref['extra'].update(values.pop('extra', {}))
    instance_ref.update(values)

    def _get_sec_group_models(security_groups):
        models = []
        default_group = _security_group_ensure_default(context)
        if 'default' in security_groups:
            models.append(default_group)
            # Generate a new list, so we don't modify the original
            security_groups = [x for x in security_groups if x != 'default']
        if security_groups:
            models.extend(_security_group_get_by_names(
                context, security_groups))
        return models

    if 'hostname' in values:
        _validate_unique_server_name(context, values['hostname'])
    instance_ref.security_groups = _get_sec_group_models(security_groups)
    context.session.add(instance_ref)

    # create the instance uuid to ec2_id mapping entry for instance
    ec2_instance_create(context, instance_ref['uuid'])

    return instance_ref


def _instance_data_get_for_user(context, project_id, user_id):
    result = model_query(context, models.Instance, (
        func.count(models.Instance.id),
        func.sum(models.Instance.vcpus),
        func.sum(models.Instance.memory_mb))).\
        filter_by(project_id=project_id)
    if user_id:
        result = result.filter_by(user_id=user_id).first()
    else:
        result = result.first()
    # NOTE(vish): convert None to 0
    return (result[0] or 0, result[1] or 0, result[2] or 0)


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def instance_destroy(context, instance_uuid, constraint=None):
    if uuidutils.is_uuid_like(instance_uuid):
        instance_ref = _instance_get_by_uuid(context, instance_uuid)
    else:
        raise exception.InvalidUUID(instance_uuid)

    query = model_query(context, models.Instance).\
                    filter_by(uuid=instance_uuid)
    if constraint is not None:
        query = constraint.apply(models.Instance, query)
    count = query.soft_delete()
    if count == 0:
        raise exception.ConstraintNotMet()
    model_query(context, models.SecurityGroupInstanceAssociation).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceInfoCache).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceMetadata).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceFault).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceExtra).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceSystemMetadata).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    model_query(context, models.InstanceGroupMember).\
            filter_by(instance_id=instance_uuid).\
            soft_delete()
    model_query(context, models.BlockDeviceMapping).\
            filter_by(instance_uuid=instance_uuid).\
            soft_delete()
    # NOTE(snikitin): We can't use model_query here, because there is no
    # column 'deleted' in 'tags' table.
    context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid).delete()
    context.session.query(models.ConsoleAuthToken).filter_by(
        instance_uuid=instance_uuid).delete()

    return instance_ref


@require_context
@pick_context_manager_reader_allow_async
def instance_get_by_uuid(context, uuid, columns_to_join=None):
    return _instance_get_by_uuid(context, uuid,
                                 columns_to_join=columns_to_join)


def _instance_get_by_uuid(context, uuid, columns_to_join=None):
    result = _build_instance_get(context, columns_to_join=columns_to_join).\
                filter_by(uuid=uuid).\
                first()

    if not result:
        raise exception.InstanceNotFound(instance_id=uuid)

    return result


@require_context
@pick_context_manager_reader
def instance_get(context, instance_id, columns_to_join=None):
    try:
        result = _build_instance_get(context, columns_to_join=columns_to_join
                                     ).filter_by(id=instance_id).first()

        if not result:
            raise exception.InstanceNotFound(instance_id=instance_id)

        return result
    except db_exc.DBError:
        # NOTE(sdague): catch all in case the db engine chokes on the
        # id because it's too long of an int to store.
        LOG.warning(_LW("Invalid instance id %s in request"), instance_id)
        raise exception.InvalidID(id=instance_id)


def _build_instance_get(context, columns_to_join=None):
    query = model_query(context, models.Instance, project_only=True).\
            options(joinedload_all('security_groups.rules')).\
            options(joinedload('info_cache'))
    if columns_to_join is None:
        columns_to_join = ['metadata', 'system_metadata']
    for column in columns_to_join:
        if column in ['info_cache', 'security_groups']:
            # Already always joined above
            continue
        if 'extra.' in column:
            query = query.options(undefer(column))
        else:
            query = query.options(joinedload(column))
    # NOTE(alaski) Stop lazy loading of columns not needed.
    for col in ['metadata', 'system_metadata']:
        if col not in columns_to_join:
            query = query.options(noload(col))
    return query


def _instances_fill_metadata(context, instances, manual_joins=None):
    """Selectively fill instances with manually-joined metadata. Note that
    instance will be converted to a dict.

    :param context: security context
    :param instances: list of instances to fill
    :param manual_joins: list of tables to manually join (can be any
                         combination of 'metadata' and 'system_metadata' or
                         None to take the default of both)
    """
    uuids = [inst['uuid'] for inst in instances]

    if manual_joins is None:
        manual_joins = ['metadata', 'system_metadata']

    meta = collections.defaultdict(list)
    if 'metadata' in manual_joins:
        for row in _instance_metadata_get_multi(context, uuids):
            meta[row['instance_uuid']].append(row)

    sys_meta = collections.defaultdict(list)
    if 'system_metadata' in manual_joins:
        for row in _instance_system_metadata_get_multi(context, uuids):
            sys_meta[row['instance_uuid']].append(row)

    pcidevs = collections.defaultdict(list)
    if 'pci_devices' in manual_joins:
        for row in _instance_pcidevs_get_multi(context, uuids):
            pcidevs[row['instance_uuid']].append(row)

    filled_instances = []
    for inst in instances:
        inst = dict(inst)
        inst['system_metadata'] = sys_meta[inst['uuid']]
        inst['metadata'] = meta[inst['uuid']]
        if 'pci_devices' in manual_joins:
            inst['pci_devices'] = pcidevs[inst['uuid']]
        filled_instances.append(inst)

    return filled_instances


def _manual_join_columns(columns_to_join):
    """Separate manually joined columns from columns_to_join

    If columns_to_join contains 'metadata', 'system_metadata', or
    'pci_devices' those columns are removed from columns_to_join and added
    to a manual_joins list to be used with the _instances_fill_metadata method.

    The columns_to_join formal parameter is copied and not modified, the return
    tuple has the modified columns_to_join list to be used with joinedload in
    a model query.

    :param:columns_to_join: List of columns to join in a model query.
    :return: tuple of (manual_joins, columns_to_join)
    """
    manual_joins = []
    columns_to_join_new = copy.copy(columns_to_join)
    for column in ('metadata', 'system_metadata', 'pci_devices'):
        if column in columns_to_join_new:
            columns_to_join_new.remove(column)
            manual_joins.append(column)
    return manual_joins, columns_to_join_new


@require_context
@pick_context_manager_reader
def instance_get_all(context, columns_to_join=None):
    if columns_to_join is None:
        columns_to_join_new = ['info_cache', 'security_groups']
        manual_joins = ['metadata', 'system_metadata']
    else:
        manual_joins, columns_to_join_new = (
            _manual_join_columns(columns_to_join))
    query = model_query(context, models.Instance)
    for column in columns_to_join_new:
        query = query.options(joinedload(column))
    if not context.is_admin:
        # If we're not admin context, add appropriate filter..
        if context.project_id:
            query = query.filter_by(project_id=context.project_id)
        else:
            query = query.filter_by(user_id=context.user_id)
    instances = query.all()
    return _instances_fill_metadata(context, instances, manual_joins)


@require_context
@pick_context_manager_reader_allow_async
def instance_get_all_by_filters(context, filters, sort_key, sort_dir,
                                limit=None, marker=None, columns_to_join=None):
    """Return instances matching all filters sorted by the primary key.

    See instance_get_all_by_filters_sort for more information.
    """
    # Invoke the API with the multiple sort keys and directions using the
    # single sort key/direction
    return instance_get_all_by_filters_sort(context, filters, limit=limit,
                                            marker=marker,
                                            columns_to_join=columns_to_join,
                                            sort_keys=[sort_key],
                                            sort_dirs=[sort_dir])


@require_context
@pick_context_manager_reader_allow_async
def instance_get_all_by_filters_sort(context, filters, limit=None, marker=None,
                                     columns_to_join=None, sort_keys=None,
                                     sort_dirs=None):
    """Return instances that match all filters sorted by the given keys.
    Deleted instances will be returned by default, unless there's a filter that
    says otherwise.

    Depending on the name of a filter, matching for that filter is
    performed using either exact matching or as regular expression
    matching. Exact matching is applied for the following filters::

    |   ['project_id', 'user_id', 'image_ref',
    |    'vm_state', 'instance_type_id', 'uuid',
    |    'metadata', 'host', 'system_metadata']


    A third type of filter (also using exact matching), filters
    based on instance metadata tags when supplied under a special
    key named 'filter'::

    |   filters = {
    |       'filter': [
    |           {'name': 'tag-key', 'value': '<metakey>'},
    |           {'name': 'tag-value', 'value': '<metaval>'},
    |           {'name': 'tag:<metakey>', 'value': '<metaval>'}
    |       ]
    |   }

    Special keys are used to tweek the query further::

    |   'changes-since' - only return instances updated after
    |   'deleted' - only return (or exclude) deleted instances
    |   'soft_deleted' - modify behavior of 'deleted' to either
    |                    include or exclude instances whose
    |                    vm_state is SOFT_DELETED.

    A fourth type of filter (also using exact matching), filters
    based on instance tags (not metadata tags). There are two types
    of these tags:

    `tags` -- One or more strings that will be used to filter results
            in an AND expression: T1 AND T2

    `tags-any` -- One or more strings that will be used to filter results in
            an OR expression: T1 OR T2

    `not-tags` -- One or more strings that will be used to filter results in
            an NOT AND expression: NOT (T1 AND T2)

    `not-tags-any` -- One or more strings that will be used to filter results
            in an NOT OR expression: NOT (T1 OR T2)

    Tags should be represented as list::

    |    filters = {
    |        'tags': [some-tag, some-another-tag],
    |        'tags-any: [some-any-tag, some-another-any-tag],
    |        'not-tags: [some-not-tag, some-another-not-tag],
    |        'not-tags-any: [some-not-any-tag, some-another-not-any-tag]
    |    }

    """
    # NOTE(mriedem): If the limit is 0 there is no point in even going
    # to the database since nothing is going to be returned anyway.
    if limit == 0:
        return []

    sort_keys, sort_dirs = process_sort_params(sort_keys,
                                               sort_dirs,
                                               default_dir='desc')

    if columns_to_join is None:
        columns_to_join_new = ['info_cache', 'security_groups']
        manual_joins = ['metadata', 'system_metadata']
    else:
        manual_joins, columns_to_join_new = (
            _manual_join_columns(columns_to_join))

    query_prefix = context.session.query(models.Instance)
    for column in columns_to_join_new:
        if 'extra.' in column:
            query_prefix = query_prefix.options(undefer(column))
        else:
            query_prefix = query_prefix.options(joinedload(column))

    # Note: order_by is done in the sqlalchemy.utils.py paginate_query(),
    # no need to do it here as well

    # Make a copy of the filters dictionary to use going forward, as we'll
    # be modifying it and we shouldn't affect the caller's use of it.
    filters = filters.copy()

    if 'changes-since' in filters:
        changes_since = timeutils.normalize_time(filters['changes-since'])
        query_prefix = query_prefix.\
                            filter(models.Instance.updated_at >= changes_since)

    if 'deleted' in filters:
        # Instances can be soft or hard deleted and the query needs to
        # include or exclude both
        deleted = filters.pop('deleted')
        if deleted:
            if filters.pop('soft_deleted', True):
                delete = or_(
                    models.Instance.deleted == models.Instance.id,
                    models.Instance.vm_state == vm_states.SOFT_DELETED
                    )
                query_prefix = query_prefix.\
                    filter(delete)
            else:
                query_prefix = query_prefix.\
                    filter(models.Instance.deleted == models.Instance.id)
        else:
            query_prefix = query_prefix.\
                    filter_by(deleted=0)
            if not filters.pop('soft_deleted', False):
                # It would be better to have vm_state not be nullable
                # but until then we test it explicitly as a workaround.
                not_soft_deleted = or_(
                    models.Instance.vm_state != vm_states.SOFT_DELETED,
                    models.Instance.vm_state == null()
                    )
                query_prefix = query_prefix.filter(not_soft_deleted)

    if 'cleaned' in filters:
        cleaned = 1 if filters.pop('cleaned') else 0
        query_prefix = query_prefix.filter(models.Instance.cleaned == cleaned)

    if 'tags' in filters:
        tags = filters.pop('tags')
        # We build a JOIN ladder expression for each tag, JOIN'ing
        # the first tag to the instances table, and each subsequent
        # tag to the last JOIN'd tags table
        first_tag = tags.pop(0)
        query_prefix = query_prefix.join(models.Instance.tags)
        query_prefix = query_prefix.filter(models.Tag.tag == first_tag)

        for tag in tags:
            tag_alias = aliased(models.Tag)
            query_prefix = query_prefix.join(tag_alias,
                                             models.Instance.tags)
            query_prefix = query_prefix.filter(tag_alias.tag == tag)

    if 'tags-any' in filters:
        tags = filters.pop('tags-any')
        tag_alias = aliased(models.Tag)
        query_prefix = query_prefix.join(tag_alias, models.Instance.tags)
        query_prefix = query_prefix.filter(tag_alias.tag.in_(tags))

    if 'not-tags' in filters:
        tags = filters.pop('not-tags')
        first_tag = tags.pop(0)
        subq = query_prefix.session.query(models.Tag.resource_id)
        subq = subq.join(models.Instance.tags)
        subq = subq.filter(models.Tag.tag == first_tag)

        for tag in tags:
            tag_alias = aliased(models.Tag)
            subq = subq.join(tag_alias, models.Instance.tags)
            subq = subq.filter(tag_alias.tag == tag)

        query_prefix = query_prefix.filter(~models.Instance.uuid.in_(subq))

    if 'not-tags-any' in filters:
        tags = filters.pop('not-tags-any')
        query_prefix = query_prefix.filter(~models.Instance.tags.any(
            models.Tag.tag.in_(tags)))

    if not context.is_admin:
        # If we're not admin context, add appropriate filter..
        if context.project_id:
            filters['project_id'] = context.project_id
        else:
            filters['user_id'] = context.user_id

    # Filters for exact matches that we can do along with the SQL query...
    # For other filters that don't match this, we will do regexp matching
    exact_match_filter_names = ['project_id', 'user_id', 'image_ref',
                                'vm_state', 'instance_type_id', 'uuid',
                                'metadata', 'host', 'task_state',
                                'system_metadata']

    # Filter the query
    query_prefix = _exact_instance_filter(query_prefix,
                                filters, exact_match_filter_names)
    if query_prefix is None:
        return []
    query_prefix = _regex_instance_filter(query_prefix, filters)
    query_prefix = _tag_instance_filter(context, query_prefix, filters)

    # paginate query
    if marker is not None:
        try:
            marker = _instance_get_by_uuid(
                    context.elevated(read_deleted='yes'), marker)
        except exception.InstanceNotFound:
            raise exception.MarkerNotFound(marker=marker)
    try:
        query_prefix = sqlalchemyutils.paginate_query(query_prefix,
                               models.Instance, limit,
                               sort_keys,
                               marker=marker,
                               sort_dirs=sort_dirs)
    except db_exc.InvalidSortKey:
        raise exception.InvalidSortKey()

    return _instances_fill_metadata(context, query_prefix.all(), manual_joins)


def _tag_instance_filter(context, query, filters):
    """Applies tag filtering to an Instance query.

    Returns the updated query.  This method alters filters to remove
    keys that are tags.  This filters on resources by tags - this
    method assumes that the caller will take care of access control

    :param context: request context object
    :param query: query to apply filters to
    :param filters: dictionary of filters
    """
    if filters.get('filter') is None:
        return query

    model = models.Instance
    model_metadata = models.InstanceMetadata
    model_uuid = model_metadata.instance_uuid

    or_query = None

    def _to_list(val):
        if isinstance(val, dict):
            val = val.values()
        if not isinstance(val, (tuple, list, set)):
            val = (val,)
        return val

    for filter_block in filters['filter']:
        if not isinstance(filter_block, dict):
            continue

        filter_name = filter_block.get('name')
        if filter_name is None:
            continue

        tag_name = filter_name[4:]
        tag_val = _to_list(filter_block.get('value'))

        if filter_name.startswith('tag-'):
            if tag_name not in ['key', 'value']:
                msg = _("Invalid field name: %s") % tag_name
                raise exception.InvalidParameterValue(err=msg)
            subq = getattr(model_metadata, tag_name).in_(tag_val)
            or_query = subq if or_query is None else or_(or_query, subq)

        elif filter_name.startswith('tag:'):
            subq = model_query(context, model_metadata, (model_uuid,)).\
                filter_by(key=tag_name).\
                filter(model_metadata.value.in_(tag_val))
            query = query.filter(model.uuid.in_(subq))

    if or_query is not None:
        subq = model_query(context, model_metadata, (model_uuid,)).\
                filter(or_query)
        query = query.filter(model.uuid.in_(subq))

    return query


def _get_regexp_op_for_connection(db_connection):
    db_string = db_connection.split(':')[0].split('+')[0]
    regexp_op_map = {
        'postgresql': '~',
        'mysql': 'REGEXP',
        'sqlite': 'REGEXP'
    }
    return regexp_op_map.get(db_string, 'LIKE')


def _regex_instance_filter(query, filters):
    """Applies regular expression filtering to an Instance query.

    Returns the updated query.

    :param query: query to apply filters to
    :param filters: dictionary of filters with regex values
    """

    model = models.Instance
    db_regexp_op = _get_regexp_op_for_connection(CONF.database.connection)
    for filter_name in filters:
        try:
            column_attr = getattr(model, filter_name)
        except AttributeError:
            continue
        if 'property' == type(column_attr).__name__:
            continue
        filter_val = filters[filter_name]
        # Sometimes the REGEX filter value is not a string
        if not isinstance(filter_val, six.string_types):
            filter_val = str(filter_val)
        if db_regexp_op == 'LIKE':
            query = query.filter(column_attr.op(db_regexp_op)(
                                 u'%' + filter_val + u'%'))
        else:
            query = query.filter(column_attr.op(db_regexp_op)(
                                 filter_val))
    return query


def _exact_instance_filter(query, filters, legal_keys):
    """Applies exact match filtering to an Instance query.

    Returns the updated query.  Modifies filters argument to remove
    filters consumed.

    :param query: query to apply filters to
    :param filters: dictionary of filters; values that are lists,
                    tuples, sets, or frozensets cause an 'IN' test to
                    be performed, while exact matching ('==' operator)
                    is used for other values
    :param legal_keys: list of keys to apply exact filtering to
    """

    filter_dict = {}
    model = models.Instance

    # Walk through all the keys
    for key in legal_keys:
        # Skip ones we're not filtering on
        if key not in filters:
            continue

        # OK, filtering on this key; what value do we search for?
        value = filters.pop(key)

        if key in ('metadata', 'system_metadata'):
            column_attr = getattr(model, key)
            if isinstance(value, list):
                for item in value:
                    for k, v in item.items():
                        query = query.filter(column_attr.any(key=k))
                        query = query.filter(column_attr.any(value=v))

            else:
                for k, v in value.items():
                    query = query.filter(column_attr.any(key=k))
                    query = query.filter(column_attr.any(value=v))
        elif isinstance(value, (list, tuple, set, frozenset)):
            if not value:
                return None  # empty IN-predicate; short circuit
            # Looking for values in a list; apply to query directly
            column_attr = getattr(model, key)
            query = query.filter(column_attr.in_(value))
        else:
            # OK, simple exact match; save for later
            filter_dict[key] = value

    # Apply simple exact matches
    if filter_dict:
        query = query.filter(*[getattr(models.Instance, k) == v
                               for k, v in filter_dict.items()])
    return query


def process_sort_params(sort_keys, sort_dirs,
                        default_keys=['created_at', 'id'],
                        default_dir='asc'):
    """Process the sort parameters to include default keys.

    Creates a list of sort keys and a list of sort directions. Adds the default
    keys to the end of the list if they are not already included.

    When adding the default keys to the sort keys list, the associated
    direction is:
    1) The first element in the 'sort_dirs' list (if specified), else
    2) 'default_dir' value (Note that 'asc' is the default value since this is
    the default in sqlalchemy.utils.paginate_query)

    :param sort_keys: List of sort keys to include in the processed list
    :param sort_dirs: List of sort directions to include in the processed list
    :param default_keys: List of sort keys that need to be included in the
                         processed list, they are added at the end of the list
                         if not already specified.
    :param default_dir: Sort direction associated with each of the default
                        keys that are not supplied, used when they are added
                        to the processed list
    :returns: list of sort keys, list of sort directions
    :raise exception.InvalidInput: If more sort directions than sort keys
                                   are specified or if an invalid sort
                                   direction is specified
    """
    # Determine direction to use for when adding default keys
    if sort_dirs and len(sort_dirs) != 0:
        default_dir_value = sort_dirs[0]
    else:
        default_dir_value = default_dir

    # Create list of keys (do not modify the input list)
    if sort_keys:
        result_keys = list(sort_keys)
    else:
        result_keys = []

    # If a list of directions is not provided, use the default sort direction
    # for all provided keys
    if sort_dirs:
        result_dirs = []
        # Verify sort direction
        for sort_dir in sort_dirs:
            if sort_dir not in ('asc', 'desc'):
                msg = _("Unknown sort direction, must be 'desc' or 'asc'")
                raise exception.InvalidInput(reason=msg)
            result_dirs.append(sort_dir)
    else:
        result_dirs = [default_dir_value for _sort_key in result_keys]

    # Ensure that the key and direction length match
    while len(result_dirs) < len(result_keys):
        result_dirs.append(default_dir_value)
    # Unless more direction are specified, which is an error
    if len(result_dirs) > len(result_keys):
        msg = _("Sort direction size exceeds sort key size")
        raise exception.InvalidInput(reason=msg)

    # Ensure defaults are included
    for key in default_keys:
        if key not in result_keys:
            result_keys.append(key)
            result_dirs.append(default_dir_value)

    return result_keys, result_dirs


@require_context
@pick_context_manager_reader_allow_async
def instance_get_active_by_window_joined(context, begin, end=None,
                                         project_id=None, host=None,
                                         columns_to_join=None):
    """Return instances and joins that were active during window."""
    query = context.session.query(models.Instance)

    if columns_to_join is None:
        columns_to_join_new = ['info_cache', 'security_groups']
        manual_joins = ['metadata', 'system_metadata']
    else:
        manual_joins, columns_to_join_new = (
            _manual_join_columns(columns_to_join))

    for column in columns_to_join_new:
        if 'extra.' in column:
            query = query.options(undefer(column))
        else:
            query = query.options(joinedload(column))

    query = query.filter(or_(models.Instance.terminated_at == null(),
                             models.Instance.terminated_at > begin))
    if end:
        query = query.filter(models.Instance.launched_at < end)
    if project_id:
        query = query.filter_by(project_id=project_id)
    if host:
        query = query.filter_by(host=host)

    return _instances_fill_metadata(context, query.all(), manual_joins)


def _instance_get_all_query(context, project_only=False, joins=None):
    if joins is None:
        joins = ['info_cache', 'security_groups']

    query = model_query(context,
                        models.Instance,
                        project_only=project_only)
    for column in joins:
        if 'extra.' in column:
            query = query.options(undefer(column))
        else:
            query = query.options(joinedload(column))
    return query


@pick_context_manager_reader_allow_async
def instance_get_all_by_host(context, host, columns_to_join=None):
    return _instances_fill_metadata(context,
      _instance_get_all_query(context).filter_by(host=host).all(),
                              manual_joins=columns_to_join)


def _instance_get_all_uuids_by_host(context, host):
    """Return a list of the instance uuids on a given host.

    Returns a list of UUIDs, not Instance model objects.
    """
    uuids = []
    for tuple in model_query(context, models.Instance, (models.Instance.uuid,),
                             read_deleted="no").\
                filter_by(host=host).\
                all():
        uuids.append(tuple[0])
    return uuids


@pick_context_manager_reader
def instance_get_all_by_host_and_node(context, host, node,
                                      columns_to_join=None):
    if columns_to_join is None:
        manual_joins = []
    else:
        candidates = ['system_metadata', 'metadata']
        manual_joins = [x for x in columns_to_join if x in candidates]
        columns_to_join = list(set(columns_to_join) - set(candidates))
    return _instances_fill_metadata(context,
            _instance_get_all_query(
                context,
                joins=columns_to_join).filter_by(host=host).
                filter_by(node=node).all(), manual_joins=manual_joins)


@pick_context_manager_reader
def instance_get_all_by_host_and_not_type(context, host, type_id=None):
    return _instances_fill_metadata(context,
        _instance_get_all_query(context).filter_by(host=host).
                   filter(models.Instance.instance_type_id != type_id).all())


@pick_context_manager_reader
def instance_get_all_by_grantee_security_groups(context, group_ids):
    if not group_ids:
        return []
    return _instances_fill_metadata(context,
        _instance_get_all_query(context).
            join(models.Instance.security_groups).
            filter(models.SecurityGroup.rules.any(
                models.SecurityGroupIngressRule.group_id.in_(group_ids))).
            all())


@require_context
@main_context_manager.reader
def instance_floating_address_get_all(context, instance_uuid):
    if not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(uuid=instance_uuid)

    floating_ips = model_query(context,
                               models.FloatingIp,
                               (models.FloatingIp.address,)).\
        join(models.FloatingIp.fixed_ip).\
        filter_by(instance_uuid=instance_uuid)

    return [floating_ip.address for floating_ip in floating_ips]


# NOTE(hanlind): This method can be removed as conductor RPC API moves to v2.0.
@pick_context_manager_reader
def instance_get_all_hung_in_rebooting(context, reboot_window):
    reboot_window = (timeutils.utcnow() -
                     datetime.timedelta(seconds=reboot_window))

    # NOTE(danms): this is only used in the _poll_rebooting_instances()
    # call in compute/manager, so we can avoid the metadata lookups
    # explicitly
    return _instances_fill_metadata(context,
        model_query(context, models.Instance).
            filter(models.Instance.updated_at <= reboot_window).
            filter_by(task_state=task_states.REBOOTING).all(),
        manual_joins=[])


def _retry_instance_update():
    """Wrap with oslo_db_api.wrap_db_retry, and also retry on
    UnknownInstanceUpdateConflict.
    """
    exception_checker = \
        lambda exc: isinstance(exc, (exception.UnknownInstanceUpdateConflict,))
    return oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True,
                                     exception_checker=exception_checker)


@require_context
@_retry_instance_update()
@pick_context_manager_writer
def instance_update(context, instance_uuid, values, expected=None):
    return _instance_update(context, instance_uuid, values, expected)


@require_context
@_retry_instance_update()
@pick_context_manager_writer
def instance_update_and_get_original(context, instance_uuid, values,
                                     columns_to_join=None, expected=None):
    """Set the given properties on an instance and update it. Return
    a shallow copy of the original instance reference, as well as the
    updated one.

    :param context: = request context object
    :param instance_uuid: = instance uuid
    :param values: = dict containing column values

    If "expected_task_state" exists in values, the update can only happen
    when the task state before update matches expected_task_state. Otherwise
    a UnexpectedTaskStateError is thrown.

    :returns: a tuple of the form (old_instance_ref, new_instance_ref)

    Raises NotFound if instance does not exist.
    """
    instance_ref = _instance_get_by_uuid(context, instance_uuid,
                                         columns_to_join=columns_to_join)
    return (copy.copy(instance_ref), _instance_update(
        context, instance_uuid, values, expected, original=instance_ref))


# NOTE(danms): This updates the instance's metadata list in-place and in
# the database to avoid stale data and refresh issues. It assumes the
# delete=True behavior of instance_metadata_update(...)
def _instance_metadata_update_in_place(context, instance, metadata_type, model,
                                       metadata):
    metadata = dict(metadata)
    to_delete = []
    for keyvalue in instance[metadata_type]:
        key = keyvalue['key']
        if key in metadata:
            keyvalue['value'] = metadata.pop(key)
        elif key not in metadata:
            to_delete.append(keyvalue)

    # NOTE: we have to hard_delete here otherwise we will get more than one
    # system_metadata record when we read deleted for an instance;
    # regular metadata doesn't have the same problem because we don't
    # allow reading deleted regular metadata anywhere.
    if metadata_type == 'system_metadata':
        for condemned in to_delete:
            context.session.delete(condemned)
            instance[metadata_type].remove(condemned)
    else:
        for condemned in to_delete:
            condemned.soft_delete(context.session)

    for key, value in metadata.items():
        newitem = model()
        newitem.update({'key': key, 'value': value,
                        'instance_uuid': instance['uuid']})
        context.session.add(newitem)
        instance[metadata_type].append(newitem)


def _instance_update(context, instance_uuid, values, expected, original=None):
    if not uuidutils.is_uuid_like(instance_uuid):
        raise exception.InvalidUUID(instance_uuid)

    if expected is None:
        expected = {}
    else:
        # Coerce all single values to singleton lists
        expected = {k: [None] if v is None else sqlalchemyutils.to_list(v)
                       for (k, v) in six.iteritems(expected)}

    # Extract 'expected_' values from values dict, as these aren't actually
    # updates
    for field in ('task_state', 'vm_state'):
        expected_field = 'expected_%s' % field
        if expected_field in values:
            value = values.pop(expected_field, None)
            # Coerce all single values to singleton lists
            if value is None:
                expected[field] = [None]
            else:
                expected[field] = sqlalchemyutils.to_list(value)

    # Values which need to be updated separately
    metadata = values.pop('metadata', None)
    system_metadata = values.pop('system_metadata', None)

    _handle_objects_related_type_conversions(values)

    # Hostname is potentially unique, but this is enforced in code rather
    # than the DB. The query below races, but the number of users of
    # osapi_compute_unique_server_name_scope is small, and a robust fix
    # will be complex. This is intentionally left as is for the moment.
    if 'hostname' in values:
        _validate_unique_server_name(context, values['hostname'])

    compare = models.Instance(uuid=instance_uuid, **expected)
    try:
        instance_ref = model_query(context, models.Instance,
                                   project_only=True).\
                       update_on_match(compare, 'uuid', values)
    except update_match.NoRowsMatched:
        # Update failed. Try to find why and raise a specific error.

        # We should get here only because our expected values were not current
        # when update_on_match executed. Having failed, we now have a hint that
        # the values are out of date and should check them.

        # This code is made more complex because we are using repeatable reads.
        # If we have previously read the original instance in the current
        # transaction, reading it again will return the same data, even though
        # the above update failed because it has changed: it is not possible to
        # determine what has changed in this transaction. In this case we raise
        # UnknownInstanceUpdateConflict, which will cause the operation to be
        # retried in a new transaction.

        # Because of the above, if we have previously read the instance in the
        # current transaction it will have been passed as 'original', and there
        # is no point refreshing it. If we have not previously read the
        # instance, we can fetch it here and we will get fresh data.
        if original is None:
            original = _instance_get_by_uuid(context, instance_uuid)

        conflicts_expected = {}
        conflicts_actual = {}
        for (field, expected_values) in six.iteritems(expected):
            actual = original[field]
            if actual not in expected_values:
                conflicts_expected[field] = expected_values
                conflicts_actual[field] = actual

        # Exception properties
        exc_props = {
            'instance_uuid': instance_uuid,
            'expected': conflicts_expected,
            'actual': conflicts_actual
        }

        # There was a conflict, but something (probably the MySQL read view,
        # but possibly an exceptionally unlikely second race) is preventing us
        # from seeing what it is. When we go round again we'll get a fresh
        # transaction and a fresh read view.
        if len(conflicts_actual) == 0:
            raise exception.UnknownInstanceUpdateConflict(**exc_props)

        # Task state gets special handling for convenience. We raise the
        # specific error UnexpectedDeletingTaskStateError or
        # UnexpectedTaskStateError as appropriate
        if 'task_state' in conflicts_actual:
            conflict_task_state = conflicts_actual['task_state']
            if conflict_task_state == task_states.DELETING:
                exc = exception.UnexpectedDeletingTaskStateError
            else:
                exc = exception.UnexpectedTaskStateError

        # Everything else is an InstanceUpdateConflict
        else:
            exc = exception.InstanceUpdateConflict

        raise exc(**exc_props)

    if metadata is not None:
        _instance_metadata_update_in_place(context, instance_ref,
                                           'metadata',
                                           models.InstanceMetadata,
                                           metadata)

    if system_metadata is not None:
        _instance_metadata_update_in_place(context, instance_ref,
                                           'system_metadata',
                                           models.InstanceSystemMetadata,
                                           system_metadata)

    return instance_ref


@pick_context_manager_writer
def instance_add_security_group(context, instance_uuid, security_group_id):
    """Associate the given security group with the given instance."""
    sec_group_ref = models.SecurityGroupInstanceAssociation()
    sec_group_ref.update({'instance_uuid': instance_uuid,
                          'security_group_id': security_group_id})
    sec_group_ref.save(context.session)


@require_context
@pick_context_manager_writer
def instance_remove_security_group(context, instance_uuid, security_group_id):
    """Disassociate the given security group from the given instance."""
    model_query(context, models.SecurityGroupInstanceAssociation).\
                filter_by(instance_uuid=instance_uuid).\
                filter_by(security_group_id=security_group_id).\
                soft_delete()


###################


@require_context
@pick_context_manager_reader
def instance_info_cache_get(context, instance_uuid):
    """Gets an instance info cache from the table.

    :param instance_uuid: = uuid of the info cache's instance
    """
    return model_query(context, models.InstanceInfoCache).\
                         filter_by(instance_uuid=instance_uuid).\
                         first()


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def instance_info_cache_update(context, instance_uuid, values):
    """Update an instance info cache record in the table.

    :param instance_uuid: = uuid of info cache's instance
    :param values: = dict containing column values to update
    """
    convert_objects_related_datetimes(values)

    info_cache = model_query(context, models.InstanceInfoCache).\
                     filter_by(instance_uuid=instance_uuid).\
                     first()
    needs_create = False
    if info_cache and info_cache['deleted']:
        raise exception.InstanceInfoCacheNotFound(
                instance_uuid=instance_uuid)
    elif not info_cache:
        # NOTE(tr3buchet): just in case someone blows away an instance's
        #                  cache entry, re-create it.
        values['instance_uuid'] = instance_uuid
        info_cache = models.InstanceInfoCache(**values)
        needs_create = True

    try:
        with main_context_manager.writer.savepoint.using(context):
            if needs_create:
                info_cache.save(context.session)
            else:
                info_cache.update(values)
    except db_exc.DBDuplicateEntry:
        # NOTE(sirp): Possible race if two greenthreads attempt to
        # recreate the instance cache entry at the same time. First one
        # wins.
        pass

    return info_cache


@require_context
@pick_context_manager_writer
def instance_info_cache_delete(context, instance_uuid):
    """Deletes an existing instance_info_cache record

    :param instance_uuid: = uuid of the instance tied to the cache record
    """
    model_query(context, models.InstanceInfoCache).\
                         filter_by(instance_uuid=instance_uuid).\
                         soft_delete()


###################


def _instance_extra_create(context, values):
    inst_extra_ref = models.InstanceExtra()
    inst_extra_ref.update(values)
    inst_extra_ref.save(context.session)
    return inst_extra_ref


@pick_context_manager_writer
def instance_extra_update_by_uuid(context, instance_uuid, values):
    rows_updated = model_query(context, models.InstanceExtra).\
        filter_by(instance_uuid=instance_uuid).\
        update(values)
    if not rows_updated:
        LOG.debug("Created instance_extra for %s", instance_uuid)
        create_values = copy.copy(values)
        create_values["instance_uuid"] = instance_uuid
        _instance_extra_create(context, create_values)
        rows_updated = 1
    return rows_updated


@pick_context_manager_reader
def instance_extra_get_by_instance_uuid(context, instance_uuid,
                                        columns=None):
    query = model_query(context, models.InstanceExtra).\
        filter_by(instance_uuid=instance_uuid)
    if columns is None:
        columns = ['numa_topology', 'pci_requests', 'flavor', 'vcpu_model',
                   'migration_context']
    for column in columns:
        query = query.options(undefer(column))
    instance_extra = query.first()
    return instance_extra


###################


@require_context
@main_context_manager.writer
def key_pair_create(context, values):
    try:
        key_pair_ref = models.KeyPair()
        key_pair_ref.update(values)
        key_pair_ref.save(context.session)
        return key_pair_ref
    except db_exc.DBDuplicateEntry:
        raise exception.KeyPairExists(key_name=values['name'])


@require_context
@main_context_manager.writer
def key_pair_destroy(context, user_id, name):
    result = model_query(context, models.KeyPair).\
                         filter_by(user_id=user_id).\
                         filter_by(name=name).\
                         soft_delete()
    if not result:
        raise exception.KeypairNotFound(user_id=user_id, name=name)


@require_context
@main_context_manager.reader
def key_pair_get(context, user_id, name):
    result = model_query(context, models.KeyPair).\
                     filter_by(user_id=user_id).\
                     filter_by(name=name).\
                     first()

    if not result:
        raise exception.KeypairNotFound(user_id=user_id, name=name)

    return result


@require_context
@main_context_manager.reader
def key_pair_get_all_by_user(context, user_id, limit=None, marker=None):
    marker_row = None
    if marker is not None:
        marker_row = model_query(context, models.KeyPair, read_deleted="no").\
            filter_by(name=marker).filter_by(user_id=user_id).first()
        if not marker_row:
            raise exception.MarkerNotFound(marker=marker)

    query = model_query(context, models.KeyPair, read_deleted="no").\
        filter_by(user_id=user_id)

    query = sqlalchemyutils.paginate_query(
        query, models.KeyPair, limit, ['name'], marker=marker_row)

    return query.all()


@require_context
@main_context_manager.reader
def key_pair_count_by_user(context, user_id):
    return model_query(context, models.KeyPair, read_deleted="no").\
                   filter_by(user_id=user_id).\
                   count()


###################

@main_context_manager.writer
def network_associate(context, project_id, network_id=None, force=False):
    """Associate a project with a network.

    called by project_get_networks under certain conditions
    and network manager add_network_to_project()

    only associate if the project doesn't already have a network
    or if force is True

    force solves race condition where a fresh project has multiple instance
    builds simultaneously picked up by multiple network hosts which attempt
    to associate the project with multiple networks
    force should only be used as a direct consequence of user request
    all automated requests should not use force
    """
    def network_query(project_filter, id=None):
        filter_kwargs = {'project_id': project_filter}
        if id is not None:
            filter_kwargs['id'] = id
        return model_query(context, models.Network, read_deleted="no").\
                       filter_by(**filter_kwargs).\
                       with_lockmode('update').\
                       first()

    if not force:
        # find out if project has a network
        network_ref = network_query(project_id)

    if force or not network_ref:
        # in force mode or project doesn't have a network so associate
        # with a new network

        # get new network
        network_ref = network_query(None, network_id)
        if not network_ref:
            raise exception.NoMoreNetworks()

        # associate with network
        # NOTE(vish): if with_lockmode isn't supported, as in sqlite,
        #             then this has concurrency issues
        network_ref['project_id'] = project_id
        context.session.add(network_ref)
    return network_ref


def _network_ips_query(context, network_id):
    return model_query(context, models.FixedIp, read_deleted="no").\
                   filter_by(network_id=network_id)


@main_context_manager.reader
def network_count_reserved_ips(context, network_id):
    return _network_ips_query(context, network_id).\
                    filter_by(reserved=True).\
                    count()


@main_context_manager.writer
def network_create_safe(context, values):
    network_ref = models.Network()
    network_ref['uuid'] = str(uuid.uuid4())
    network_ref.update(values)

    try:
        network_ref.save(context.session)
        return network_ref
    except db_exc.DBDuplicateEntry:
        raise exception.DuplicateVlan(vlan=values['vlan'])


@main_context_manager.writer
def network_delete_safe(context, network_id):
    result = model_query(context, models.FixedIp, read_deleted="no").\
                     filter_by(network_id=network_id).\
                     filter_by(allocated=True).\
                     count()
    if result != 0:
        raise exception.NetworkInUse(network_id=network_id)
    network_ref = _network_get(context, network_id=network_id)

    model_query(context, models.FixedIp, read_deleted="no").\
            filter_by(network_id=network_id).\
            soft_delete()

    context.session.delete(network_ref)


@main_context_manager.writer
def network_disassociate(context, network_id, disassociate_host,
                         disassociate_project):
    net_update = {}
    if disassociate_project:
        net_update['project_id'] = None
    if disassociate_host:
        net_update['host'] = None
    network_update(context, network_id, net_update)


def _network_get(context, network_id, project_only='allow_none'):
    result = model_query(context, models.Network, project_only=project_only).\
                    filter_by(id=network_id).\
                    first()

    if not result:
        raise exception.NetworkNotFound(network_id=network_id)

    return result


@require_context
@main_context_manager.reader
def network_get(context, network_id, project_only='allow_none'):
    return _network_get(context, network_id, project_only=project_only)


@require_context
@main_context_manager.reader
def network_get_all(context, project_only):
    result = model_query(context, models.Network, read_deleted="no",
                         project_only=project_only).all()

    if not result:
        raise exception.NoNetworksFound()

    return result


@require_context
@main_context_manager.reader
def network_get_all_by_uuids(context, network_uuids, project_only):
    result = model_query(context, models.Network, read_deleted="no",
                         project_only=project_only).\
                filter(models.Network.uuid.in_(network_uuids)).\
                all()

    if not result:
        raise exception.NoNetworksFound()

    # check if the result contains all the networks
    # we are looking for
    for network_uuid in network_uuids:
        for network in result:
            if network['uuid'] == network_uuid:
                break
        else:
            if project_only:
                raise exception.NetworkNotFoundForProject(
                      network_uuid=network_uuid, project_id=context.project_id)
            raise exception.NetworkNotFound(network_id=network_uuid)

    return result


def _get_associated_fixed_ips_query(context, network_id, host=None):
    # NOTE(vish): The ugly joins here are to solve a performance issue and
    #             should be removed once we can add and remove leases
    #             without regenerating the whole list
    vif_and = and_(models.VirtualInterface.id ==
                   models.FixedIp.virtual_interface_id,
                   models.VirtualInterface.deleted == 0)
    inst_and = and_(models.Instance.uuid == models.FixedIp.instance_uuid,
                    models.Instance.deleted == 0)
    # NOTE(vish): This subquery left joins the minimum interface id for each
    #             instance. If the join succeeds (i.e. the 11th column is not
    #             null), then the fixed ip is on the first interface.
    subq = context.session.query(
        func.min(models.VirtualInterface.id).label("id"),
        models.VirtualInterface.instance_uuid).\
        group_by(models.VirtualInterface.instance_uuid).subquery()
    subq_and = and_(subq.c.id == models.FixedIp.virtual_interface_id,
            subq.c.instance_uuid == models.VirtualInterface.instance_uuid)
    query = context.session.query(
        models.FixedIp.address,
        models.FixedIp.instance_uuid,
        models.FixedIp.network_id,
        models.FixedIp.virtual_interface_id,
        models.VirtualInterface.address,
        models.Instance.hostname,
        models.Instance.updated_at,
        models.Instance.created_at,
        models.FixedIp.allocated,
        models.FixedIp.leased,
        subq.c.id).\
        filter(models.FixedIp.deleted == 0).\
        filter(models.FixedIp.network_id == network_id).\
        join((models.VirtualInterface, vif_and)).\
        join((models.Instance, inst_and)).\
        outerjoin((subq, subq_and)).\
        filter(models.FixedIp.instance_uuid != null()).\
        filter(models.FixedIp.virtual_interface_id != null())
    if host:
        query = query.filter(models.Instance.host == host)
    return query


@main_context_manager.reader
def network_get_associated_fixed_ips(context, network_id, host=None):
    # FIXME(sirp): since this returns fixed_ips, this would be better named
    # fixed_ip_get_all_by_network.
    query = _get_associated_fixed_ips_query(context, network_id, host)
    result = query.all()
    data = []
    for datum in result:
        cleaned = {}
        cleaned['address'] = datum[0]
        cleaned['instance_uuid'] = datum[1]
        cleaned['network_id'] = datum[2]
        cleaned['vif_id'] = datum[3]
        cleaned['vif_address'] = datum[4]
        cleaned['instance_hostname'] = datum[5]
        cleaned['instance_updated'] = datum[6]
        cleaned['instance_created'] = datum[7]
        cleaned['allocated'] = datum[8]
        cleaned['leased'] = datum[9]
        # NOTE(vish): default_route is True if this fixed ip is on the first
        #             interface its instance.
        cleaned['default_route'] = datum[10] is not None
        data.append(cleaned)
    return data


@main_context_manager.reader
def network_in_use_on_host(context, network_id, host):
    query = _get_associated_fixed_ips_query(context, network_id, host)
    return query.count() > 0


def _network_get_query(context):
    return model_query(context, models.Network, read_deleted="no")


@main_context_manager.reader
def network_get_by_uuid(context, uuid):
    result = _network_get_query(context).filter_by(uuid=uuid).first()

    if not result:
        raise exception.NetworkNotFoundForUUID(uuid=uuid)

    return result


@main_context_manager.reader
def network_get_by_cidr(context, cidr):
    result = _network_get_query(context).\
                filter(or_(models.Network.cidr == cidr,
                           models.Network.cidr_v6 == cidr)).\
                first()

    if not result:
        raise exception.NetworkNotFoundForCidr(cidr=cidr)

    return result


@main_context_manager.reader
def network_get_all_by_host(context, host):
    fixed_host_filter = or_(models.FixedIp.host == host,
            and_(models.FixedIp.instance_uuid != null(),
                 models.Instance.host == host))
    fixed_ip_query = model_query(context, models.FixedIp,
                                 (models.FixedIp.network_id,)).\
                     outerjoin((models.Instance,
                                models.Instance.uuid ==
                                models.FixedIp.instance_uuid)).\
                     filter(fixed_host_filter)
    # NOTE(vish): return networks that have host set
    #             or that have a fixed ip with host set
    #             or that have an instance with host set
    host_filter = or_(models.Network.host == host,
                      models.Network.id.in_(fixed_ip_query.subquery()))
    return _network_get_query(context).filter(host_filter).all()


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True,
                           retry_on_request=True)
@main_context_manager.writer
def network_set_host(context, network_id, host_id):
    network_ref = _network_get_query(context).\
        filter_by(id=network_id).\
        first()

    if not network_ref:
        raise exception.NetworkNotFound(network_id=network_id)

    if network_ref.host:
        return None

    rows_updated = _network_get_query(context).\
        filter_by(id=network_id).\
        filter_by(host=None).\
        update({'host': host_id})

    if not rows_updated:
        LOG.debug('The row was updated in a concurrent transaction, '
                  'we will fetch another row')
        raise db_exc.RetryRequest(
            exception.NetworkSetHostFailed(network_id=network_id))


@require_context
@main_context_manager.writer
def network_update(context, network_id, values):
    network_ref = _network_get(context, network_id)
    network_ref.update(values)
    try:
        network_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.DuplicateVlan(vlan=values['vlan'])
    return network_ref


###################


@require_context
@main_context_manager.reader
def quota_get(context, project_id, resource, user_id=None):
    model = models.ProjectUserQuota if user_id else models.Quota
    query = model_query(context, model).\
                    filter_by(project_id=project_id).\
                    filter_by(resource=resource)
    if user_id:
        query = query.filter_by(user_id=user_id)

    result = query.first()
    if not result:
        if user_id:
            raise exception.ProjectUserQuotaNotFound(project_id=project_id,
                                                     user_id=user_id)
        else:
            raise exception.ProjectQuotaNotFound(project_id=project_id)

    return result


@require_context
@main_context_manager.reader
def quota_get_all_by_project_and_user(context, project_id, user_id):
    user_quotas = model_query(context, models.ProjectUserQuota,
                              (models.ProjectUserQuota.resource,
                               models.ProjectUserQuota.hard_limit)).\
                   filter_by(project_id=project_id).\
                   filter_by(user_id=user_id).\
                   all()

    result = {'project_id': project_id, 'user_id': user_id}
    for user_quota in user_quotas:
        result[user_quota.resource] = user_quota.hard_limit

    return result


@require_context
@main_context_manager.reader
def quota_get_all_by_project(context, project_id):
    rows = model_query(context, models.Quota, read_deleted="no").\
                   filter_by(project_id=project_id).\
                   all()

    result = {'project_id': project_id}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@require_context
@main_context_manager.reader
def quota_get_all(context, project_id):
    result = model_query(context, models.ProjectUserQuota).\
                   filter_by(project_id=project_id).\
                   all()

    return result


def quota_get_per_project_resources():
    return PER_PROJECT_QUOTAS


@main_context_manager.writer
def quota_create(context, project_id, resource, limit, user_id=None):
    per_user = user_id and resource not in PER_PROJECT_QUOTAS
    quota_ref = models.ProjectUserQuota() if per_user else models.Quota()
    if per_user:
        quota_ref.user_id = user_id
    quota_ref.project_id = project_id
    quota_ref.resource = resource
    quota_ref.hard_limit = limit
    try:
        quota_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.QuotaExists(project_id=project_id, resource=resource)
    return quota_ref


@main_context_manager.writer
def quota_update(context, project_id, resource, limit, user_id=None):
    per_user = user_id and resource not in PER_PROJECT_QUOTAS
    model = models.ProjectUserQuota if per_user else models.Quota
    query = model_query(context, model).\
                filter_by(project_id=project_id).\
                filter_by(resource=resource)
    if per_user:
        query = query.filter_by(user_id=user_id)

    result = query.update({'hard_limit': limit})
    if not result:
        if per_user:
            raise exception.ProjectUserQuotaNotFound(project_id=project_id,
                                                     user_id=user_id)
        else:
            raise exception.ProjectQuotaNotFound(project_id=project_id)


###################


@require_context
@main_context_manager.reader
def quota_class_get(context, class_name, resource):
    result = model_query(context, models.QuotaClass, read_deleted="no").\
                     filter_by(class_name=class_name).\
                     filter_by(resource=resource).\
                     first()

    if not result:
        raise exception.QuotaClassNotFound(class_name=class_name)

    return result


@main_context_manager.reader
def quota_class_get_default(context):
    rows = model_query(context, models.QuotaClass, read_deleted="no").\
                   filter_by(class_name=_DEFAULT_QUOTA_NAME).\
                   all()

    result = {'class_name': _DEFAULT_QUOTA_NAME}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@require_context
@main_context_manager.reader
def quota_class_get_all_by_name(context, class_name):
    rows = model_query(context, models.QuotaClass, read_deleted="no").\
                   filter_by(class_name=class_name).\
                   all()

    result = {'class_name': class_name}
    for row in rows:
        result[row.resource] = row.hard_limit

    return result


@main_context_manager.writer
def quota_class_create(context, class_name, resource, limit):
    quota_class_ref = models.QuotaClass()
    quota_class_ref.class_name = class_name
    quota_class_ref.resource = resource
    quota_class_ref.hard_limit = limit
    quota_class_ref.save(context.session)
    return quota_class_ref


@main_context_manager.writer
def quota_class_update(context, class_name, resource, limit):
    result = model_query(context, models.QuotaClass, read_deleted="no").\
                     filter_by(class_name=class_name).\
                     filter_by(resource=resource).\
                     update({'hard_limit': limit})

    if not result:
        raise exception.QuotaClassNotFound(class_name=class_name)


###################


@require_context
@main_context_manager.reader
def quota_usage_get(context, project_id, resource, user_id=None):
    query = model_query(context, models.QuotaUsage, read_deleted="no").\
                     filter_by(project_id=project_id).\
                     filter_by(resource=resource)
    if user_id:
        if resource not in PER_PROJECT_QUOTAS:
            result = query.filter_by(user_id=user_id).first()
        else:
            result = query.filter_by(user_id=None).first()
    else:
        result = query.first()

    if not result:
        raise exception.QuotaUsageNotFound(project_id=project_id)

    return result


def _quota_usage_get_all(context, project_id, user_id=None):
    query = model_query(context, models.QuotaUsage, read_deleted="no").\
                   filter_by(project_id=project_id)
    result = {'project_id': project_id}
    if user_id:
        query = query.filter(or_(models.QuotaUsage.user_id == user_id,
                                 models.QuotaUsage.user_id == null()))
        result['user_id'] = user_id

    rows = query.all()
    for row in rows:
        if row.resource in result:
            result[row.resource]['in_use'] += row.in_use
            result[row.resource]['reserved'] += row.reserved
        else:
            result[row.resource] = dict(in_use=row.in_use,
                                        reserved=row.reserved)

    return result


@require_context
@main_context_manager.reader
def quota_usage_get_all_by_project_and_user(context, project_id, user_id):
    return _quota_usage_get_all(context, project_id, user_id=user_id)


@require_context
@main_context_manager.reader
def quota_usage_get_all_by_project(context, project_id):
    return _quota_usage_get_all(context, project_id)


def _quota_usage_create(project_id, user_id, resource, in_use,
                        reserved, until_refresh, session):
    quota_usage_ref = models.QuotaUsage()
    quota_usage_ref.project_id = project_id
    quota_usage_ref.user_id = user_id
    quota_usage_ref.resource = resource
    quota_usage_ref.in_use = in_use
    quota_usage_ref.reserved = reserved
    quota_usage_ref.until_refresh = until_refresh
    # updated_at is needed for judgement of max_age
    quota_usage_ref.updated_at = timeutils.utcnow()

    quota_usage_ref.save(session)

    return quota_usage_ref


@main_context_manager.writer
def quota_usage_update(context, project_id, user_id, resource, **kwargs):
    updates = {}

    for key in ['in_use', 'reserved', 'until_refresh']:
        if key in kwargs:
            updates[key] = kwargs[key]

    result = model_query(context, models.QuotaUsage, read_deleted="no").\
                     filter_by(project_id=project_id).\
                     filter_by(resource=resource).\
                     filter(or_(models.QuotaUsage.user_id == user_id,
                                models.QuotaUsage.user_id == null())).\
                     update(updates)

    if not result:
        raise exception.QuotaUsageNotFound(project_id=project_id)


###################


def _reservation_create(uuid, usage, project_id, user_id, resource,
                        delta, expire, session):
    reservation_ref = models.Reservation()
    reservation_ref.uuid = uuid
    reservation_ref.usage_id = usage['id']
    reservation_ref.project_id = project_id
    reservation_ref.user_id = user_id
    reservation_ref.resource = resource
    reservation_ref.delta = delta
    reservation_ref.expire = expire
    reservation_ref.save(session)
    return reservation_ref


###################


# NOTE(johannes): The quota code uses SQL locking to ensure races don't
# cause under or over counting of resources. To avoid deadlocks, this
# code always acquires the lock on quota_usages before acquiring the lock
# on reservations.

def _get_project_user_quota_usages(context, project_id, user_id):
    rows = model_query(context, models.QuotaUsage,
                       read_deleted="no").\
        filter_by(project_id=project_id).\
        order_by(models.QuotaUsage.id.asc()).\
        with_lockmode('update').\
        all()
    proj_result = dict()
    user_result = dict()
    # Get the total count of in_use,reserved
    for row in rows:
        proj_result.setdefault(row.resource,
                               dict(in_use=0, reserved=0, total=0))
        proj_result[row.resource]['in_use'] += row.in_use
        proj_result[row.resource]['reserved'] += row.reserved
        proj_result[row.resource]['total'] += (row.in_use + row.reserved)
        if row.user_id is None or row.user_id == user_id:
            user_result[row.resource] = row
    return proj_result, user_result


def _create_quota_usage_if_missing(user_usages, resource, until_refresh,
                                   project_id, user_id, session):
    """Creates a QuotaUsage record and adds to user_usages if not present.

    :param user_usages:   dict of resource keys to QuotaUsage records. This is
                          updated if resource is not in user_usages yet or
                          until_refresh is not None.
    :param resource:      The resource being checked for quota usage.
    :param until_refresh: Count of reservations until usage is refreshed,
                          int or None
    :param project_id:    The project being checked for quota usage.
    :param user_id:       The user being checked for quota usage.
    :param session:       DB session holding a transaction lock.
    :return:              True if a new QuotaUsage record was created and added
                          to user_usages, False otherwise.
    """
    new_usage = None
    if resource not in user_usages:
        user_id_to_use = user_id
        if resource in PER_PROJECT_QUOTAS:
            user_id_to_use = None
        new_usage = _quota_usage_create(project_id, user_id_to_use, resource,
                                        0, 0, until_refresh or None, session)
        user_usages[resource] = new_usage
    return new_usage is not None


def _is_quota_refresh_needed(quota_usage, max_age):
    """Determines if a quota usage refresh is needed.

    :param quota_usage:   A QuotaUsage object for a given resource.
    :param max_age:       Number of seconds between subsequent usage refreshes.
    :return:              True if a refresh is needed, False otherwise.
    """
    refresh = False
    if quota_usage.in_use < 0:
        # Negative in_use count indicates a desync, so try to
        # heal from that...
        LOG.debug('in_use has dropped below 0; forcing refresh for '
                  'QuotaUsage: %s', dict(quota_usage))
        refresh = True
    elif quota_usage.until_refresh is not None:
        quota_usage.until_refresh -= 1
        if quota_usage.until_refresh <= 0:
            refresh = True
    elif max_age and (timeutils.utcnow() -
            quota_usage.updated_at).seconds >= max_age:
        refresh = True

    return refresh


def _refresh_quota_usages(quota_usage, until_refresh, in_use):
    """Refreshes quota usage for the given resource.

    :param quota_usage:   A QuotaUsage object for a given resource.
    :param until_refresh: Count of reservations until usage is refreshed,
                          int or None
    :param in_use:        Actual quota usage for the resource.
    """
    if quota_usage.in_use != in_use:
        LOG.info(_LI('quota_usages out of sync, updating. '
                     'project_id: %(project_id)s, '
                     'user_id: %(user_id)s, '
                     'resource: %(res)s, '
                     'tracked usage: %(tracked_use)s, '
                     'actual usage: %(in_use)s'),
            {'project_id': quota_usage.project_id,
             'user_id': quota_usage.user_id,
             'res': quota_usage.resource,
             'tracked_use': quota_usage.in_use,
             'in_use': in_use})
    else:
        LOG.debug('QuotaUsage has not changed, refresh is unnecessary for: %s',
                  dict(quota_usage))

    # Update the usage
    quota_usage.in_use = in_use
    quota_usage.until_refresh = until_refresh or None


def _refresh_quota_usages_if_needed(user_usages, context, resources, keys,
                                    project_id, user_id, until_refresh,
                                    max_age, force_refresh=False):
    elevated = context.elevated()

    # Handle usage refresh
    work = set(keys)
    while work:
        resource = work.pop()

        # Do we need to refresh the usage?
        created = _create_quota_usage_if_missing(user_usages, resource,
                                                 until_refresh, project_id,
                                                 user_id, context.session)

        refresh = force_refresh
        if not refresh:
            refresh = created or \
                      _is_quota_refresh_needed(user_usages[resource], max_age)

        # OK, refresh the usage
        if refresh:
            # Grab the sync routine
            sync = QUOTA_SYNC_FUNCTIONS[resources[resource].sync]

            updates = sync(elevated, project_id, user_id)
            for res, in_use in updates.items():
                # Make sure we have a destination for the usage!
                _create_quota_usage_if_missing(user_usages, res,
                                               until_refresh, project_id,
                                               user_id, context.session)
                _refresh_quota_usages(user_usages[res], until_refresh,
                                      in_use)

                # Because more than one resource may be refreshed
                # by the call to the sync routine, and we don't
                # want to double-sync, we make sure all refreshed
                # resources are dropped from the work set.
                work.discard(res)

                # NOTE(Vek): We make the assumption that the sync
                #            routine actually refreshes the
                #            resources that it is the sync routine
                #            for.  We don't check, because this is
                #            a best-effort mechanism.


def _calculate_overquota(project_quotas, user_quotas, deltas,
                         project_usages, user_usages):
    """Checks if any resources will go over quota based on the request.

    :param project_quotas: dict of resource quotas (limits) for the project.
    :param user_quotas:    dict of resource quotas (limits) for the user.
    :param deltas:         dict of resource keys to positive/negative quota
                           changes for the resources in a given operation.
    :param project_usages: dict of resource keys to QuotaUsage records for the
                           project.
    :param user_usages:    dict of resource keys to QuotaUsage records for the
                           user.
    :return:               list of resources that are over-quota for the
                           operation.
    """
    overs = []
    for res, delta in deltas.items():
        # We can't go over-quota if we're not reserving anything.
        if delta >= 0:
            # We can't go over-quota if we have unlimited quotas.
            # over if the project usage + delta is more than project quota
            if 0 <= project_quotas[res] < delta + project_usages[res]['total']:
                LOG.debug('Request is over project quota for resource '
                          '"%(res)s". Project limit: %(limit)s, delta: '
                          '%(delta)s, current total project usage: %(total)s',
                          {'res': res, 'limit': project_quotas[res],
                           'delta': delta,
                           'total': project_usages[res]['total']})
                overs.append(res)
            # We can't go over-quota if we have unlimited quotas.
            # over if the user usage + delta is more than user quota
            elif 0 <= user_quotas[res] < delta + user_usages[res]['total']:
                LOG.debug('Request is over user quota for resource '
                          '"%(res)s". User limit: %(limit)s, delta: '
                          '%(delta)s, current total user usage: %(total)s',
                          {'res': res, 'limit': user_quotas[res],
                           'delta': delta, 'total': user_usages[res]['total']})
                overs.append(res)
    return overs


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def quota_usage_refresh(context, resources, keys, until_refresh, max_age,
                        project_id=None, user_id=None):
    if project_id is None:
        project_id = context.project_id
    if user_id is None:
        user_id = context.user_id

    # Get the current usages
    project_usages, user_usages = _get_project_user_quota_usages(
            context, project_id, user_id)

    # Force refresh of the usages
    _refresh_quota_usages_if_needed(user_usages, context, resources, keys,
                                    project_id, user_id, until_refresh,
                                    max_age, force_refresh=True)


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def quota_reserve(context, resources, project_quotas, user_quotas, deltas,
                  expire, until_refresh, max_age, project_id=None,
                  user_id=None):
    if project_id is None:
        project_id = context.project_id
    if user_id is None:
        user_id = context.user_id

    # Get the current usages
    project_usages, user_usages = _get_project_user_quota_usages(
            context, project_id, user_id)

    _refresh_quota_usages_if_needed(user_usages, context, resources,
                                    deltas.keys(), project_id, user_id,
                                    until_refresh, max_age)

    # Check for deltas that would go negative
    unders = [res for res, delta in deltas.items()
              if delta < 0 and
              delta + user_usages[res].in_use < 0]

    # Now, let's check the quotas
    # NOTE(Vek): We're only concerned about positive increments.
    #            If a project has gone over quota, we want them to
    #            be able to reduce their usage without any
    #            problems.
    for key, value in user_usages.items():
        if key not in project_usages:
            LOG.debug('Copying QuotaUsage for resource "%(key)s" from '
                      'user_usages into project_usages: %(value)s',
                      {'key': key, 'value': dict(value)})
            project_usages[key] = value

    overs = _calculate_overquota(project_quotas, user_quotas, deltas,
                                 project_usages, user_usages)

    # NOTE(Vek): The quota check needs to be in the transaction,
    #            but the transaction doesn't fail just because
    #            we're over quota, so the OverQuota raise is
    #            outside the transaction.  If we did the raise
    #            here, our usage updates would be discarded, but
    #            they're not invalidated by being over-quota.

    # Create the reservations
    if not overs:
        reservations = []
        for res, delta in deltas.items():
            reservation = _reservation_create(
                                             str(uuid.uuid4()),
                                             user_usages[res],
                                             project_id,
                                             user_id,
                                             res, delta, expire,
                                             context.session)
            reservations.append(reservation.uuid)

            # Also update the reserved quantity
            # NOTE(Vek): Again, we are only concerned here about
            #            positive increments.  Here, though, we're
            #            worried about the following scenario:
            #
            #            1) User initiates resize down.
            #            2) User allocates a new instance.
            #            3) Resize down fails or is reverted.
            #            4) User is now over quota.
            #
            #            To prevent this, we only update the
            #            reserved value if the delta is positive.
            if delta > 0:
                user_usages[res].reserved += delta

    # Apply updates to the usages table
    for usage_ref in user_usages.values():
        context.session.add(usage_ref)

    if unders:
        LOG.warning(_LW("Change will make usage less than 0 for the following "
                        "resources: %s"), unders)

    if overs:
        if project_quotas == user_quotas:
            usages = project_usages
        else:
            # NOTE(mriedem): user_usages is a dict of resource keys to
            # QuotaUsage sqlalchemy dict-like objects and doesn't log well
            # so convert the user_usages values to something useful for
            # logging. Remove this if we ever change how
            # _get_project_user_quota_usages returns the user_usages values.
            user_usages = {k: dict(in_use=v['in_use'], reserved=v['reserved'],
                                   total=v['total'])
                      for k, v in user_usages.items()}
            usages = user_usages
        usages = {k: dict(in_use=v['in_use'], reserved=v['reserved'])
                  for k, v in usages.items()}
        LOG.debug('Raise OverQuota exception because: '
                  'project_quotas: %(project_quotas)s, '
                  'user_quotas: %(user_quotas)s, deltas: %(deltas)s, '
                  'overs: %(overs)s, project_usages: %(project_usages)s, '
                  'user_usages: %(user_usages)s',
                  {'project_quotas': project_quotas,
                   'user_quotas': user_quotas,
                   'overs': overs, 'deltas': deltas,
                   'project_usages': project_usages,
                   'user_usages': user_usages})
        raise exception.OverQuota(overs=sorted(overs), quotas=user_quotas,
                                  usages=usages)

    return reservations


def _quota_reservations_query(context, reservations):
    """Return the relevant reservations."""

    # Get the listed reservations
    return model_query(context, models.Reservation, read_deleted="no").\
        filter(models.Reservation.uuid.in_(reservations)).\
        with_lockmode('update')


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_commit(context, reservations, project_id=None, user_id=None):
    _project_usages, user_usages = _get_project_user_quota_usages(
            context, project_id, user_id)
    reservation_query = _quota_reservations_query(context, reservations)
    for reservation in reservation_query.all():
        usage = user_usages[reservation.resource]
        if reservation.delta >= 0:
            usage.reserved -= reservation.delta
        usage.in_use += reservation.delta
    reservation_query.soft_delete(synchronize_session=False)


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_rollback(context, reservations, project_id=None, user_id=None):
    _project_usages, user_usages = _get_project_user_quota_usages(
            context, project_id, user_id)
    reservation_query = _quota_reservations_query(context, reservations)
    for reservation in reservation_query.all():
        usage = user_usages[reservation.resource]
        if reservation.delta >= 0:
            usage.reserved -= reservation.delta
    reservation_query.soft_delete(synchronize_session=False)


@main_context_manager.writer
def quota_destroy_all_by_project_and_user(context, project_id, user_id):
    model_query(context, models.ProjectUserQuota, read_deleted="no").\
        filter_by(project_id=project_id).\
        filter_by(user_id=user_id).\
        soft_delete(synchronize_session=False)

    model_query(context, models.QuotaUsage, read_deleted="no").\
        filter_by(project_id=project_id).\
        filter_by(user_id=user_id).\
        soft_delete(synchronize_session=False)

    model_query(context, models.Reservation, read_deleted="no").\
        filter_by(project_id=project_id).\
        filter_by(user_id=user_id).\
        soft_delete(synchronize_session=False)


@main_context_manager.writer
def quota_destroy_all_by_project(context, project_id):
    model_query(context, models.Quota, read_deleted="no").\
        filter_by(project_id=project_id).\
        soft_delete(synchronize_session=False)

    model_query(context, models.ProjectUserQuota, read_deleted="no").\
        filter_by(project_id=project_id).\
        soft_delete(synchronize_session=False)

    model_query(context, models.QuotaUsage, read_deleted="no").\
        filter_by(project_id=project_id).\
        soft_delete(synchronize_session=False)

    model_query(context, models.Reservation, read_deleted="no").\
        filter_by(project_id=project_id).\
        soft_delete(synchronize_session=False)


@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@main_context_manager.writer
def reservation_expire(context):
    current_time = timeutils.utcnow()
    reservation_query = model_query(
        context, models.Reservation, read_deleted="no").\
        filter(models.Reservation.expire < current_time)

    for reservation in reservation_query.join(models.QuotaUsage).all():
        if reservation.delta >= 0:
            reservation.usage.reserved -= reservation.delta
            context.session.add(reservation.usage)

    reservation_query.soft_delete(synchronize_session=False)


###################


def _ec2_volume_get_query(context):
    return model_query(context, models.VolumeIdMapping, read_deleted='yes')


def _ec2_snapshot_get_query(context):
    return model_query(context, models.SnapshotIdMapping, read_deleted='yes')


@require_context
@main_context_manager.writer
def ec2_volume_create(context, volume_uuid, id=None):
    """Create ec2 compatible volume by provided uuid."""
    ec2_volume_ref = models.VolumeIdMapping()
    ec2_volume_ref.update({'uuid': volume_uuid})
    if id is not None:
        ec2_volume_ref.update({'id': id})

    ec2_volume_ref.save(context.session)

    return ec2_volume_ref


@require_context
@main_context_manager.reader
def ec2_volume_get_by_uuid(context, volume_uuid):
    result = _ec2_volume_get_query(context).\
                    filter_by(uuid=volume_uuid).\
                    first()

    if not result:
        raise exception.VolumeNotFound(volume_id=volume_uuid)

    return result


@require_context
@main_context_manager.reader
def ec2_volume_get_by_id(context, volume_id):
    result = _ec2_volume_get_query(context).\
                    filter_by(id=volume_id).\
                    first()

    if not result:
        raise exception.VolumeNotFound(volume_id=volume_id)

    return result


@require_context
@main_context_manager.writer
def ec2_snapshot_create(context, snapshot_uuid, id=None):
    """Create ec2 compatible snapshot by provided uuid."""
    ec2_snapshot_ref = models.SnapshotIdMapping()
    ec2_snapshot_ref.update({'uuid': snapshot_uuid})
    if id is not None:
        ec2_snapshot_ref.update({'id': id})

    ec2_snapshot_ref.save(context.session)

    return ec2_snapshot_ref


@require_context
@main_context_manager.reader
def ec2_snapshot_get_by_ec2_id(context, ec2_id):
    result = _ec2_snapshot_get_query(context).\
                    filter_by(id=ec2_id).\
                    first()

    if not result:
        raise exception.SnapshotNotFound(snapshot_id=ec2_id)

    return result


@require_context
@main_context_manager.reader
def ec2_snapshot_get_by_uuid(context, snapshot_uuid):
    result = _ec2_snapshot_get_query(context).\
                    filter_by(uuid=snapshot_uuid).\
                    first()

    if not result:
        raise exception.SnapshotNotFound(snapshot_id=snapshot_uuid)

    return result


###################


def _block_device_mapping_get_query(context, columns_to_join=None):
    if columns_to_join is None:
        columns_to_join = []

    query = model_query(context, models.BlockDeviceMapping)

    for column in columns_to_join:
        query = query.options(joinedload(column))

    return query


def _scrub_empty_str_values(dct, keys_to_scrub):
    """Remove any keys found in sequence keys_to_scrub from the dict
    if they have the value ''.
    """
    for key in keys_to_scrub:
        if key in dct and dct[key] == '':
            del dct[key]


def _from_legacy_values(values, legacy, allow_updates=False):
    if legacy:
        if allow_updates and block_device.is_safe_for_update(values):
            return values
        else:
            return block_device.BlockDeviceDict.from_legacy(values)
    else:
        return values


@require_context
@pick_context_manager_writer
def block_device_mapping_create(context, values, legacy=True):
    _scrub_empty_str_values(values, ['volume_size'])
    values = _from_legacy_values(values, legacy)
    convert_objects_related_datetimes(values)

    bdm_ref = models.BlockDeviceMapping()
    bdm_ref.update(values)
    bdm_ref.save(context.session)
    return bdm_ref


@require_context
@pick_context_manager_writer
def block_device_mapping_update(context, bdm_id, values, legacy=True):
    _scrub_empty_str_values(values, ['volume_size'])
    values = _from_legacy_values(values, legacy, allow_updates=True)
    convert_objects_related_datetimes(values)

    query = _block_device_mapping_get_query(context).filter_by(id=bdm_id)
    query.update(values)
    return query.first()


@pick_context_manager_writer
def block_device_mapping_update_or_create(context, values, legacy=True):
    _scrub_empty_str_values(values, ['volume_size'])
    values = _from_legacy_values(values, legacy, allow_updates=True)
    convert_objects_related_datetimes(values)

    result = None
    # NOTE(xqueralt): Only update a BDM when device_name was provided. We
    # allow empty device names so they will be set later by the manager.
    if values['device_name']:
        query = _block_device_mapping_get_query(context)
        result = query.filter_by(instance_uuid=values['instance_uuid'],
                                 device_name=values['device_name']).first()

    if result:
        result.update(values)
    else:
        # Either the device_name doesn't exist in the database yet, or no
        # device_name was provided. Both cases mean creating a new BDM.
        result = models.BlockDeviceMapping(**values)
        result.save(context.session)

    # NOTE(xqueralt): Prevent from having multiple swap devices for the
    # same instance. This will delete all the existing ones.
    if block_device.new_format_is_swap(values):
        query = _block_device_mapping_get_query(context)
        query = query.filter_by(instance_uuid=values['instance_uuid'],
                                source_type='blank', guest_format='swap')
        query = query.filter(models.BlockDeviceMapping.id != result.id)
        query.soft_delete()

    return result


@require_context
@pick_context_manager_reader_allow_async
def block_device_mapping_get_all_by_instance_uuids(context, instance_uuids):
    if not instance_uuids:
        return []
    return _block_device_mapping_get_query(context).filter(
        models.BlockDeviceMapping.instance_uuid.in_(instance_uuids)).all()


@require_context
@pick_context_manager_reader_allow_async
def block_device_mapping_get_all_by_instance(context, instance_uuid):
    return _block_device_mapping_get_query(context).\
                 filter_by(instance_uuid=instance_uuid).\
                 all()


@require_context
@pick_context_manager_reader
def block_device_mapping_get_all_by_volume_id(context, volume_id,
        columns_to_join=None):
    return _block_device_mapping_get_query(context,
            columns_to_join=columns_to_join).\
                 filter_by(volume_id=volume_id).\
                 all()


@require_context
@pick_context_manager_reader
def block_device_mapping_get_by_instance_and_volume_id(context, volume_id,
                                                       instance_uuid,
                                                       columns_to_join=None):
    return _block_device_mapping_get_query(context,
            columns_to_join=columns_to_join).\
                 filter_by(volume_id=volume_id).\
                 filter_by(instance_uuid=instance_uuid).\
                 first()


@require_context
@pick_context_manager_writer
def block_device_mapping_destroy(context, bdm_id):
    _block_device_mapping_get_query(context).\
            filter_by(id=bdm_id).\
            soft_delete()


@require_context
@pick_context_manager_writer
def block_device_mapping_destroy_by_instance_and_volume(context, instance_uuid,
                                                        volume_id):
    _block_device_mapping_get_query(context).\
            filter_by(instance_uuid=instance_uuid).\
            filter_by(volume_id=volume_id).\
            soft_delete()


@require_context
@pick_context_manager_writer
def block_device_mapping_destroy_by_instance_and_device(context, instance_uuid,
                                                        device_name):
    _block_device_mapping_get_query(context).\
            filter_by(instance_uuid=instance_uuid).\
            filter_by(device_name=device_name).\
            soft_delete()


###################


@require_context
@main_context_manager.writer
def security_group_create(context, values):
    security_group_ref = models.SecurityGroup()
    # FIXME(devcamcar): Unless I do this, rules fails with lazy load exception
    # once save() is called.  This will get cleaned up in next orm pass.
    security_group_ref.rules
    security_group_ref.update(values)
    try:
        with main_context_manager.writer.savepoint.using(context):
            security_group_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.SecurityGroupExists(
                project_id=values['project_id'],
                security_group_name=values['name'])
    return security_group_ref


def _security_group_get_query(context, read_deleted=None,
                              project_only=False, join_rules=True):
    query = model_query(context, models.SecurityGroup,
            read_deleted=read_deleted, project_only=project_only)
    if join_rules:
        query = query.options(joinedload_all('rules.grantee_group'))
    return query


def _security_group_get_by_names(context, group_names):
    """Get security group models for a project by a list of names.
    Raise SecurityGroupNotFoundForProject for a name not found.
    """
    query = _security_group_get_query(context, read_deleted="no",
                                      join_rules=False).\
            filter_by(project_id=context.project_id).\
            filter(models.SecurityGroup.name.in_(group_names))
    sg_models = query.all()
    if len(sg_models) == len(group_names):
        return sg_models
    # Find the first one missing and raise
    group_names_from_models = [x.name for x in sg_models]
    for group_name in group_names:
        if group_name not in group_names_from_models:
            raise exception.SecurityGroupNotFoundForProject(
                project_id=context.project_id, security_group_id=group_name)
    # Not Reached


@require_context
@main_context_manager.reader
def security_group_get_all(context):
    return _security_group_get_query(context).all()


@require_context
@main_context_manager.reader
def security_group_get(context, security_group_id, columns_to_join=None):
    join_rules = columns_to_join and 'rules' in columns_to_join
    if join_rules:
        columns_to_join.remove('rules')
    query = _security_group_get_query(context, project_only=True,
                                      join_rules=join_rules).\
                    filter_by(id=security_group_id)

    if columns_to_join is None:
        columns_to_join = []
    for column in columns_to_join:
        if column.startswith('instances'):
            query = query.options(joinedload_all(column))

    result = query.first()
    if not result:
        raise exception.SecurityGroupNotFound(
                security_group_id=security_group_id)

    return result


@require_context
@main_context_manager.reader
def security_group_get_by_name(context, project_id, group_name,
                               columns_to_join=None):
    query = _security_group_get_query(context,
                                      read_deleted="no", join_rules=False).\
            filter_by(project_id=project_id).\
            filter_by(name=group_name)

    if columns_to_join is None:
        columns_to_join = ['instances', 'rules.grantee_group']

    for column in columns_to_join:
        query = query.options(joinedload_all(column))

    result = query.first()
    if not result:
        raise exception.SecurityGroupNotFoundForProject(
                project_id=project_id, security_group_id=group_name)

    return result


@require_context
@main_context_manager.reader
def security_group_get_by_project(context, project_id):
    return _security_group_get_query(context, read_deleted="no").\
                        filter_by(project_id=project_id).\
                        all()


@require_context
@main_context_manager.reader
def security_group_get_by_instance(context, instance_uuid):
    return _security_group_get_query(context, read_deleted="no").\
                   join(models.SecurityGroup.instances).\
                   filter_by(uuid=instance_uuid).\
                   all()


@require_context
@main_context_manager.reader
def security_group_in_use(context, group_id):
    # Are there any instances that haven't been deleted
    # that include this group?
    inst_assoc = model_query(context,
                             models.SecurityGroupInstanceAssociation,
                             read_deleted="no").\
                    filter_by(security_group_id=group_id).\
                    all()
    for ia in inst_assoc:
        num_instances = model_query(context, models.Instance,
                                    read_deleted="no").\
                    filter_by(uuid=ia.instance_uuid).\
                    count()
        if num_instances:
            return True

    return False


@require_context
@main_context_manager.writer
def security_group_update(context, security_group_id, values,
                          columns_to_join=None):
    query = model_query(context, models.SecurityGroup).filter_by(
        id=security_group_id)
    if columns_to_join:
        for column in columns_to_join:
            query = query.options(joinedload_all(column))
    security_group_ref = query.first()

    if not security_group_ref:
        raise exception.SecurityGroupNotFound(
                security_group_id=security_group_id)
    security_group_ref.update(values)
    name = security_group_ref['name']
    project_id = security_group_ref['project_id']
    try:
        security_group_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.SecurityGroupExists(
                project_id=project_id,
                security_group_name=name)
    return security_group_ref


def security_group_ensure_default(context):
    """Ensure default security group exists for a project_id."""

    try:
        # NOTE(rpodolyaka): create the default security group, if it doesn't
        # exist. This must be done in a separate transaction, so that
        # this one is not aborted in case a concurrent one succeeds first
        # and the unique constraint for security group names is violated
        # by a concurrent INSERT
        with main_context_manager.writer.independent.using(context):
            return _security_group_ensure_default(context)
    except exception.SecurityGroupExists:
        # NOTE(rpodolyaka): a concurrent transaction has succeeded first,
        # suppress the error and proceed
        return security_group_get_by_name(context, context.project_id,
                                          'default')


@main_context_manager.writer
def _security_group_ensure_default(context):
    try:
        default_group = _security_group_get_by_names(context, ['default'])[0]
    except exception.NotFound:
        values = {'name': 'default',
                  'description': 'default',
                  'user_id': context.user_id,
                  'project_id': context.project_id}
        default_group = security_group_create(context, values)
        usage = model_query(context, models.QuotaUsage, read_deleted="no").\
            filter_by(project_id=context.project_id).\
            filter_by(user_id=context.user_id).\
            filter_by(resource='security_groups')
        # Create quota usage for auto created default security group
        if not usage.first():
            _quota_usage_create(context.project_id,
                                context.user_id,
                                'security_groups',
                                1, 0,
                                CONF.until_refresh,
                                context.session)
        else:
            usage.update({'in_use': int(usage.first().in_use) + 1})

        default_rules = _security_group_rule_get_default_query(context).all()
        for default_rule in default_rules:
            # This is suboptimal, it should be programmatic to know
            # the values of the default_rule
            rule_values = {'protocol': default_rule.protocol,
                           'from_port': default_rule.from_port,
                           'to_port': default_rule.to_port,
                           'cidr': default_rule.cidr,
                           'parent_group_id': default_group.id,
            }
            _security_group_rule_create(context, rule_values)
    return default_group


@require_context
@main_context_manager.writer
def security_group_destroy(context, security_group_id):
    model_query(context, models.SecurityGroup).\
            filter_by(id=security_group_id).\
            soft_delete()
    model_query(context, models.SecurityGroupInstanceAssociation).\
            filter_by(security_group_id=security_group_id).\
            soft_delete()
    model_query(context, models.SecurityGroupIngressRule).\
            filter_by(group_id=security_group_id).\
            soft_delete()
    model_query(context, models.SecurityGroupIngressRule).\
            filter_by(parent_group_id=security_group_id).\
            soft_delete()


def _security_group_count_by_project_and_user(context, project_id, user_id):
    nova.context.authorize_project_context(context, project_id)
    return model_query(context, models.SecurityGroup, read_deleted="no").\
                   filter_by(project_id=project_id).\
                   filter_by(user_id=user_id).\
                   count()


###################


def _security_group_rule_create(context, values):
    security_group_rule_ref = models.SecurityGroupIngressRule()
    security_group_rule_ref.update(values)
    security_group_rule_ref.save(context.session)
    return security_group_rule_ref


def _security_group_rule_get_query(context):
    return model_query(context, models.SecurityGroupIngressRule)


@require_context
@main_context_manager.reader
def security_group_rule_get(context, security_group_rule_id):
    result = (_security_group_rule_get_query(context).
                         filter_by(id=security_group_rule_id).
                         first())

    if not result:
        raise exception.SecurityGroupNotFoundForRule(
                                               rule_id=security_group_rule_id)

    return result


@require_context
@main_context_manager.reader
def security_group_rule_get_by_security_group(context, security_group_id,
                                              columns_to_join=None):
    if columns_to_join is None:
        columns_to_join = ['grantee_group.instances.system_metadata',
                           'grantee_group.instances.info_cache']
    query = (_security_group_rule_get_query(context).
             filter_by(parent_group_id=security_group_id))
    for column in columns_to_join:
        query = query.options(joinedload_all(column))
    return query.all()


@require_context
@main_context_manager.reader
def security_group_rule_get_by_instance(context, instance_uuid):
    return (_security_group_rule_get_query(context).
            join('parent_group', 'instances').
            filter_by(uuid=instance_uuid).
            options(joinedload('grantee_group')).
            all())


@require_context
@main_context_manager.writer
def security_group_rule_create(context, values):
    return _security_group_rule_create(context, values)


@require_context
@main_context_manager.writer
def security_group_rule_destroy(context, security_group_rule_id):
    count = (_security_group_rule_get_query(context).
                    filter_by(id=security_group_rule_id).
                    soft_delete())
    if count == 0:
        raise exception.SecurityGroupNotFoundForRule(
                                            rule_id=security_group_rule_id)


@require_context
@main_context_manager.reader
def security_group_rule_count_by_group(context, security_group_id):
    return (model_query(context, models.SecurityGroupIngressRule,
                   read_deleted="no").
                   filter_by(parent_group_id=security_group_id).
                   count())


###################


def _security_group_rule_get_default_query(context):
    return model_query(context, models.SecurityGroupIngressDefaultRule)


@require_context
@main_context_manager.reader
def security_group_default_rule_get(context, security_group_rule_default_id):
    result = _security_group_rule_get_default_query(context).\
                        filter_by(id=security_group_rule_default_id).\
                        first()

    if not result:
        raise exception.SecurityGroupDefaultRuleNotFound(
                                        rule_id=security_group_rule_default_id)

    return result


@main_context_manager.writer
def security_group_default_rule_destroy(context,
                                        security_group_rule_default_id):
    count = _security_group_rule_get_default_query(context).\
                        filter_by(id=security_group_rule_default_id).\
                        soft_delete()
    if count == 0:
        raise exception.SecurityGroupDefaultRuleNotFound(
                                    rule_id=security_group_rule_default_id)


@main_context_manager.writer
def security_group_default_rule_create(context, values):
    security_group_default_rule_ref = models.SecurityGroupIngressDefaultRule()
    security_group_default_rule_ref.update(values)
    security_group_default_rule_ref.save(context.session)
    return security_group_default_rule_ref


@require_context
@main_context_manager.reader
def security_group_default_rule_list(context):
    return _security_group_rule_get_default_query(context).all()


###################


@main_context_manager.writer
def provider_fw_rule_create(context, rule):
    fw_rule_ref = models.ProviderFirewallRule()
    fw_rule_ref.update(rule)
    fw_rule_ref.save(context.session)
    return fw_rule_ref


@main_context_manager.reader
def provider_fw_rule_get_all(context):
    return model_query(context, models.ProviderFirewallRule).all()


@main_context_manager.writer
def provider_fw_rule_destroy(context, rule_id):
    context.session.query(models.ProviderFirewallRule).\
        filter_by(id=rule_id).\
        soft_delete()


###################


@require_context
@main_context_manager.writer
def project_get_networks(context, project_id, associate=True):
    # NOTE(tr3buchet): as before this function will associate
    # a project with a network if it doesn't have one and
    # associate is true
    result = model_query(context, models.Network, read_deleted="no").\
                     filter_by(project_id=project_id).\
                     all()

    if not result:
        if not associate:
            return []

        return [network_associate(context, project_id)]

    return result


###################


@pick_context_manager_writer
def migration_create(context, values):
    migration = models.Migration()
    migration.update(values)
    migration.save(context.session)
    return migration


@pick_context_manager_writer
def migration_update(context, id, values):
    migration = migration_get(context, id)
    migration.update(values)

    return migration


@pick_context_manager_reader
def migration_get(context, id):
    result = model_query(context, models.Migration, read_deleted="yes").\
                     filter_by(id=id).\
                     first()

    if not result:
        raise exception.MigrationNotFound(migration_id=id)

    return result


@pick_context_manager_reader
def migration_get_by_id_and_instance(context, id, instance_uuid):
    result = model_query(context, models.Migration).\
                     filter_by(id=id).\
                     filter_by(instance_uuid=instance_uuid).\
                     first()

    if not result:
        raise exception.MigrationNotFoundForInstance(migration_id=id,
                                                     instance_id=instance_uuid)

    return result


@pick_context_manager_reader
def migration_get_by_instance_and_status(context, instance_uuid, status):
    result = model_query(context, models.Migration, read_deleted="yes").\
                     filter_by(instance_uuid=instance_uuid).\
                     filter_by(status=status).\
                     first()

    if not result:
        raise exception.MigrationNotFoundByStatus(instance_id=instance_uuid,
                                                  status=status)

    return result


@pick_context_manager_reader_allow_async
def migration_get_unconfirmed_by_dest_compute(context, confirm_window,
                                              dest_compute):
    confirm_window = (timeutils.utcnow() -
                      datetime.timedelta(seconds=confirm_window))

    return model_query(context, models.Migration, read_deleted="yes").\
             filter(models.Migration.updated_at <= confirm_window).\
             filter_by(status="finished").\
             filter_by(dest_compute=dest_compute).\
             all()


@pick_context_manager_reader
def migration_get_in_progress_by_host_and_node(context, host, node):

    return model_query(context, models.Migration).\
            filter(or_(and_(models.Migration.source_compute == host,
                            models.Migration.source_node == node),
                       and_(models.Migration.dest_compute == host,
                            models.Migration.dest_node == node))).\
            filter(~models.Migration.status.in_(['accepted', 'confirmed',
                                                 'reverted', 'error',
                                                 'failed', 'completed',
                                                 'cancelled'])).\
            options(joinedload_all('instance.system_metadata')).\
            all()


@pick_context_manager_reader
def migration_get_in_progress_by_instance(context, instance_uuid,
                                          migration_type=None):
    # TODO(Shaohe Feng) we should share the in-progress list.
    # TODO(Shaohe Feng) will also summarize all status to a new
    # MigrationStatus class.
    query = model_query(context, models.Migration).\
            filter_by(instance_uuid=instance_uuid).\
            filter(models.Migration.status.in_(['queued', 'preparing',
                                                'running',
                                                'post-migrating']))
    if migration_type:
        query = query.filter(models.Migration.migration_type == migration_type)

    return query.all()


@pick_context_manager_reader
def migration_get_all_by_filters(context, filters):
    query = model_query(context, models.Migration)
    if "status" in filters:
        status = filters["status"]
        status = [status] if isinstance(status, six.string_types) else status
        query = query.filter(models.Migration.status.in_(status))
    if "host" in filters:
        host = filters["host"]
        query = query.filter(or_(models.Migration.source_compute == host,
                                 models.Migration.dest_compute == host))
    elif "source_compute" in filters:
        host = filters['source_compute']
        query = query.filter(models.Migration.source_compute == host)
    if "migration_type" in filters:
        migtype = filters["migration_type"]
        query = query.filter(models.Migration.migration_type == migtype)
    if "hidden" in filters:
        hidden = filters["hidden"]
        query = query.filter(models.Migration.hidden == hidden)
    if "instance_uuid" in filters:
        uuid = filters["instance_uuid"]
        query = query.filter(models.Migration.instance_uuid == uuid)
    return query.all()


##################


@pick_context_manager_writer
def console_pool_create(context, values):
    pool = models.ConsolePool()
    pool.update(values)
    try:
        pool.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.ConsolePoolExists(
            host=values["host"],
            console_type=values["console_type"],
            compute_host=values["compute_host"],
        )
    return pool


@pick_context_manager_reader
def console_pool_get_by_host_type(context, compute_host, host,
                                  console_type):

    result = model_query(context, models.ConsolePool, read_deleted="no").\
                   filter_by(host=host).\
                   filter_by(console_type=console_type).\
                   filter_by(compute_host=compute_host).\
                   options(joinedload('consoles')).\
                   first()

    if not result:
        raise exception.ConsolePoolNotFoundForHostType(
                host=host, console_type=console_type,
                compute_host=compute_host)

    return result


@pick_context_manager_reader
def console_pool_get_all_by_host_type(context, host, console_type):
    return model_query(context, models.ConsolePool, read_deleted="no").\
                   filter_by(host=host).\
                   filter_by(console_type=console_type).\
                   options(joinedload('consoles')).\
                   all()


##################


@pick_context_manager_writer
def console_create(context, values):
    console = models.Console()
    console.update(values)
    console.save(context.session)
    return console


@pick_context_manager_writer
def console_delete(context, console_id):
    # NOTE(mdragon): consoles are meant to be transient.
    context.session.query(models.Console).\
        filter_by(id=console_id).\
        delete()


@pick_context_manager_reader
def console_get_by_pool_instance(context, pool_id, instance_uuid):
    result = model_query(context, models.Console, read_deleted="yes").\
                   filter_by(pool_id=pool_id).\
                   filter_by(instance_uuid=instance_uuid).\
                   options(joinedload('pool')).\
                   first()

    if not result:
        raise exception.ConsoleNotFoundInPoolForInstance(
                pool_id=pool_id, instance_uuid=instance_uuid)

    return result


@pick_context_manager_reader
def console_get_all_by_instance(context, instance_uuid, columns_to_join=None):
    query = model_query(context, models.Console, read_deleted="yes").\
                filter_by(instance_uuid=instance_uuid)
    if columns_to_join:
        for column in columns_to_join:
            query = query.options(joinedload(column))
    return query.all()


@pick_context_manager_reader
def console_get(context, console_id, instance_uuid=None):
    query = model_query(context, models.Console, read_deleted="yes").\
                    filter_by(id=console_id).\
                    options(joinedload('pool'))

    if instance_uuid is not None:
        query = query.filter_by(instance_uuid=instance_uuid)

    result = query.first()

    if not result:
        if instance_uuid:
            raise exception.ConsoleNotFoundForInstance(
                    console_id=console_id, instance_uuid=instance_uuid)
        else:
            raise exception.ConsoleNotFound(console_id=console_id)

    return result


##################


@main_context_manager.writer
def flavor_create(context, values, projects=None):
    """Create a new instance type. In order to pass in extra specs,
    the values dict should contain a 'extra_specs' key/value pair:

    {'extra_specs' : {'k1': 'v1', 'k2': 'v2', ...}}

    """
    specs = values.get('extra_specs')
    specs_refs = []
    if specs:
        for k, v in specs.items():
            specs_ref = models.InstanceTypeExtraSpecs()
            specs_ref['key'] = k
            specs_ref['value'] = v
            specs_refs.append(specs_ref)

    values['extra_specs'] = specs_refs
    instance_type_ref = models.InstanceTypes()
    instance_type_ref.update(values)

    if projects is None:
        projects = []

    try:
        instance_type_ref.save(context.session)
    except db_exc.DBDuplicateEntry as e:
        if 'flavorid' in e.columns:
            raise exception.FlavorIdExists(flavor_id=values['flavorid'])
        raise exception.FlavorExists(name=values['name'])
    except Exception as e:
        raise db_exc.DBError(e)
    for project in set(projects):
        access_ref = models.InstanceTypeProjects()
        access_ref.update({"instance_type_id": instance_type_ref.id,
                           "project_id": project})
        access_ref.save(context.session)

    return _dict_with_extra_specs(instance_type_ref)


def _dict_with_extra_specs(inst_type_query):
    """Takes an instance or instance type query returned
    by sqlalchemy and returns it as a dictionary, converting the
    extra_specs entry from a list of dicts:

    'extra_specs' : [{'key': 'k1', 'value': 'v1', ...}, ...]

    to a single dict:

    'extra_specs' : {'k1': 'v1'}

    """
    inst_type_dict = dict(inst_type_query)
    extra_specs = {x['key']: x['value']
                   for x in inst_type_query['extra_specs']}
    inst_type_dict['extra_specs'] = extra_specs
    return inst_type_dict


def _flavor_get_query(context, read_deleted=None):
    query = model_query(context, models.InstanceTypes,
                       read_deleted=read_deleted).\
                       options(joinedload('extra_specs'))
    if not context.is_admin:
        the_filter = [models.InstanceTypes.is_public == true()]
        the_filter.extend([
            models.InstanceTypes.projects.any(project_id=context.project_id)
        ])
        query = query.filter(or_(*the_filter))
    return query


@require_context
@main_context_manager.reader
def flavor_get_all(context, inactive=False, filters=None,
                   sort_key='flavorid', sort_dir='asc', limit=None,
                   marker=None):
    """Returns all flavors.
    """
    filters = filters or {}

    # FIXME(sirp): now that we have the `disabled` field for flavors, we
    # should probably remove the use of `deleted` to mark inactive. `deleted`
    # should mean truly deleted, e.g. we can safely purge the record out of the
    # database.
    read_deleted = "yes" if inactive else "no"

    query = _flavor_get_query(context, read_deleted=read_deleted)

    if 'min_memory_mb' in filters:
        query = query.filter(
                models.InstanceTypes.memory_mb >= filters['min_memory_mb'])

    if 'min_root_gb' in filters:
        query = query.filter(
                models.InstanceTypes.root_gb >= filters['min_root_gb'])

    if 'disabled' in filters:
        query = query.filter(
                models.InstanceTypes.disabled == filters['disabled'])

    if 'is_public' in filters and filters['is_public'] is not None:
        the_filter = [models.InstanceTypes.is_public == filters['is_public']]
        if filters['is_public'] and context.project_id is not None:
            the_filter.extend([
                models.InstanceTypes.projects.any(
                    project_id=context.project_id, deleted=0)
            ])
        if len(the_filter) > 1:
            query = query.filter(or_(*the_filter))
        else:
            query = query.filter(the_filter[0])

    marker_row = None
    if marker is not None:
        marker_row = _flavor_get_query(context, read_deleted=read_deleted).\
                    filter_by(flavorid=marker).\
                    first()
        if not marker_row:
            raise exception.MarkerNotFound(marker=marker)

    query = sqlalchemyutils.paginate_query(query, models.InstanceTypes, limit,
                                           [sort_key, 'id'],
                                           marker=marker_row,
                                           sort_dir=sort_dir)

    inst_types = query.all()

    return [_dict_with_extra_specs(i) for i in inst_types]


def _flavor_get_id_from_flavor_query(context, flavor_id):
    return model_query(context, models.InstanceTypes,
                       (models.InstanceTypes.id,),
                       read_deleted="no").\
                filter_by(flavorid=flavor_id)


def _flavor_get_id_from_flavor(context, flavor_id):
    result = _flavor_get_id_from_flavor_query(context, flavor_id).first()
    if not result:
        raise exception.FlavorNotFound(flavor_id=flavor_id)
    return result[0]


@require_context
@main_context_manager.reader
def flavor_get(context, id):
    """Returns a dict describing specific flavor."""
    result = _flavor_get_query(context).\
                        filter_by(id=id).\
                        first()
    if not result:
        raise exception.FlavorNotFound(flavor_id=id)
    return _dict_with_extra_specs(result)


@require_context
@main_context_manager.reader
def flavor_get_by_name(context, name):
    """Returns a dict describing specific flavor."""
    result = _flavor_get_query(context).\
                        filter_by(name=name).\
                        first()
    if not result:
        raise exception.FlavorNotFoundByName(flavor_name=name)
    return _dict_with_extra_specs(result)


@require_context
@main_context_manager.reader
def flavor_get_by_flavor_id(context, flavor_id, read_deleted):
    """Returns a dict describing specific flavor_id."""
    result = _flavor_get_query(context, read_deleted=read_deleted).\
                        filter_by(flavorid=flavor_id).\
                        order_by(asc(models.InstanceTypes.deleted),
                                 asc(models.InstanceTypes.id)).\
                        first()
    if not result:
        raise exception.FlavorNotFound(flavor_id=flavor_id)
    return _dict_with_extra_specs(result)


@main_context_manager.writer
def flavor_destroy(context, flavor_id):
    """Marks specific flavor as deleted."""
    ref = model_query(context, models.InstanceTypes, read_deleted="no").\
                filter_by(flavorid=flavor_id).\
                first()
    if not ref:
        raise exception.FlavorNotFound(flavor_id=flavor_id)

    ref.soft_delete(context.session)
    model_query(context, models.InstanceTypeExtraSpecs, read_deleted="no").\
            filter_by(instance_type_id=ref['id']).\
            soft_delete()
    model_query(context, models.InstanceTypeProjects, read_deleted="no").\
            filter_by(instance_type_id=ref['id']).\
            soft_delete()


def _flavor_access_query(context):
    return model_query(context, models.InstanceTypeProjects, read_deleted="no")


@main_context_manager.reader
def flavor_access_get_by_flavor_id(context, flavor_id):
    """Get flavor access list by flavor id."""
    instance_type_id_subq = _flavor_get_id_from_flavor_query(context,
                                                             flavor_id)
    access_refs = _flavor_access_query(context).\
                        filter_by(instance_type_id=instance_type_id_subq).\
                        all()
    return access_refs


@main_context_manager.writer
def flavor_access_add(context, flavor_id, project_id):
    """Add given tenant to the flavor access list."""
    instance_type_id = _flavor_get_id_from_flavor(context, flavor_id)

    access_ref = models.InstanceTypeProjects()
    access_ref.update({"instance_type_id": instance_type_id,
                       "project_id": project_id})
    try:
        access_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.FlavorAccessExists(flavor_id=flavor_id,
                                            project_id=project_id)
    return access_ref


@main_context_manager.writer
def flavor_access_remove(context, flavor_id, project_id):
    """Remove given tenant from the flavor access list."""
    instance_type_id = _flavor_get_id_from_flavor(context, flavor_id)

    count = _flavor_access_query(context).\
                    filter_by(instance_type_id=instance_type_id).\
                    filter_by(project_id=project_id).\
                    soft_delete(synchronize_session=False)
    if count == 0:
        raise exception.FlavorAccessNotFound(flavor_id=flavor_id,
                                             project_id=project_id)


def _flavor_extra_specs_get_query(context, flavor_id):
    instance_type_id_subq = _flavor_get_id_from_flavor_query(context,
                                                             flavor_id)

    return model_query(context, models.InstanceTypeExtraSpecs,
                       read_deleted="no").\
                filter_by(instance_type_id=instance_type_id_subq)


@require_context
@main_context_manager.reader
def flavor_extra_specs_get(context, flavor_id):
    rows = _flavor_extra_specs_get_query(context, flavor_id).all()
    return {row['key']: row['value'] for row in rows}


@require_context
@main_context_manager.writer
def flavor_extra_specs_delete(context, flavor_id, key):
    result = _flavor_extra_specs_get_query(context, flavor_id).\
                     filter(models.InstanceTypeExtraSpecs.key == key).\
                     soft_delete(synchronize_session=False)
    # did not find the extra spec
    if result == 0:
        raise exception.FlavorExtraSpecsNotFound(
                extra_specs_key=key, flavor_id=flavor_id)


@require_context
@main_context_manager.writer
def flavor_extra_specs_update_or_create(context, flavor_id, specs,
                                               max_retries=10):
    for attempt in range(max_retries):
        try:
            instance_type_id = _flavor_get_id_from_flavor(context, flavor_id)

            spec_refs = model_query(context, models.InstanceTypeExtraSpecs,
                                    read_deleted="no").\
              filter_by(instance_type_id=instance_type_id).\
              filter(models.InstanceTypeExtraSpecs.key.in_(specs.keys())).\
              all()

            existing_keys = set()
            for spec_ref in spec_refs:
                key = spec_ref["key"]
                existing_keys.add(key)
                with main_context_manager.writer.savepoint.using(context):
                    spec_ref.update({"value": specs[key]})

            for key, value in specs.items():
                if key in existing_keys:
                    continue
                spec_ref = models.InstanceTypeExtraSpecs()
                with main_context_manager.writer.savepoint.using(context):
                    spec_ref.update({"key": key, "value": value,
                                     "instance_type_id": instance_type_id})
                    context.session.add(spec_ref)

            return specs
        except db_exc.DBDuplicateEntry:
            # a concurrent transaction has been committed,
            # try again unless this was the last attempt
            if attempt == max_retries - 1:
                raise exception.FlavorExtraSpecUpdateCreateFailed(
                                    id=flavor_id, retries=max_retries)


####################


@main_context_manager.writer
def cell_create(context, values):
    cell = models.Cell()
    cell.update(values)
    try:
        cell.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.CellExists(name=values['name'])
    return cell


def _cell_get_by_name_query(context, cell_name):
    return model_query(context, models.Cell).filter_by(name=cell_name)


@main_context_manager.writer
def cell_update(context, cell_name, values):
    cell_query = _cell_get_by_name_query(context, cell_name)
    if not cell_query.update(values):
        raise exception.CellNotFound(cell_name=cell_name)
    cell = cell_query.first()
    return cell


@main_context_manager.writer
def cell_delete(context, cell_name):
    return _cell_get_by_name_query(context, cell_name).soft_delete()


@main_context_manager.reader
def cell_get(context, cell_name):
    result = _cell_get_by_name_query(context, cell_name).first()
    if not result:
        raise exception.CellNotFound(cell_name=cell_name)
    return result


@main_context_manager.reader
def cell_get_all(context):
    return model_query(context, models.Cell, read_deleted="no").all()


########################
# User-provided metadata

def _instance_metadata_get_multi(context, instance_uuids):
    if not instance_uuids:
        return []
    return model_query(context, models.InstanceMetadata).filter(
        models.InstanceMetadata.instance_uuid.in_(instance_uuids))


def _instance_metadata_get_query(context, instance_uuid):
    return model_query(context, models.InstanceMetadata, read_deleted="no").\
                    filter_by(instance_uuid=instance_uuid)


@require_context
@pick_context_manager_reader
def instance_metadata_get(context, instance_uuid):
    rows = _instance_metadata_get_query(context, instance_uuid).all()
    return {row['key']: row['value'] for row in rows}


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def instance_metadata_delete(context, instance_uuid, key):
    _instance_metadata_get_query(context, instance_uuid).\
        filter_by(key=key).\
        soft_delete()


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def instance_metadata_update(context, instance_uuid, metadata, delete):
    all_keys = metadata.keys()
    if delete:
        _instance_metadata_get_query(context, instance_uuid).\
            filter(~models.InstanceMetadata.key.in_(all_keys)).\
            soft_delete(synchronize_session=False)

    already_existing_keys = []
    meta_refs = _instance_metadata_get_query(context, instance_uuid).\
        filter(models.InstanceMetadata.key.in_(all_keys)).\
        all()

    for meta_ref in meta_refs:
        already_existing_keys.append(meta_ref.key)
        meta_ref.update({"value": metadata[meta_ref.key]})

    new_keys = set(all_keys) - set(already_existing_keys)
    for key in new_keys:
        meta_ref = models.InstanceMetadata()
        meta_ref.update({"key": key, "value": metadata[key],
                         "instance_uuid": instance_uuid})
        context.session.add(meta_ref)

    return metadata


#######################
# System-owned metadata


def _instance_system_metadata_get_multi(context, instance_uuids):
    if not instance_uuids:
        return []
    return model_query(context, models.InstanceSystemMetadata,
                       read_deleted='yes').filter(
        models.InstanceSystemMetadata.instance_uuid.in_(instance_uuids))


def _instance_system_metadata_get_query(context, instance_uuid):
    return model_query(context, models.InstanceSystemMetadata).\
                    filter_by(instance_uuid=instance_uuid)


@require_context
@pick_context_manager_reader
def instance_system_metadata_get(context, instance_uuid):
    rows = _instance_system_metadata_get_query(context, instance_uuid).all()
    return {row['key']: row['value'] for row in rows}


@require_context
@pick_context_manager_writer
def instance_system_metadata_update(context, instance_uuid, metadata, delete):
    all_keys = metadata.keys()
    if delete:
        _instance_system_metadata_get_query(context, instance_uuid).\
            filter(~models.InstanceSystemMetadata.key.in_(all_keys)).\
            soft_delete(synchronize_session=False)

    already_existing_keys = []
    meta_refs = _instance_system_metadata_get_query(context, instance_uuid).\
        filter(models.InstanceSystemMetadata.key.in_(all_keys)).\
        all()

    for meta_ref in meta_refs:
        already_existing_keys.append(meta_ref.key)
        meta_ref.update({"value": metadata[meta_ref.key]})

    new_keys = set(all_keys) - set(already_existing_keys)
    for key in new_keys:
        meta_ref = models.InstanceSystemMetadata()
        meta_ref.update({"key": key, "value": metadata[key],
                         "instance_uuid": instance_uuid})
        context.session.add(meta_ref)

    return metadata


####################


@main_context_manager.writer
def agent_build_create(context, values):
    agent_build_ref = models.AgentBuild()
    agent_build_ref.update(values)
    try:
        agent_build_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.AgentBuildExists(hypervisor=values['hypervisor'],
                        os=values['os'], architecture=values['architecture'])
    return agent_build_ref


@main_context_manager.reader
def agent_build_get_by_triple(context, hypervisor, os, architecture):
    return model_query(context, models.AgentBuild, read_deleted="no").\
                   filter_by(hypervisor=hypervisor).\
                   filter_by(os=os).\
                   filter_by(architecture=architecture).\
                   first()


@main_context_manager.reader
def agent_build_get_all(context, hypervisor=None):
    if hypervisor:
        return model_query(context, models.AgentBuild, read_deleted="no").\
                   filter_by(hypervisor=hypervisor).\
                   all()
    else:
        return model_query(context, models.AgentBuild, read_deleted="no").\
                   all()


@main_context_manager.writer
def agent_build_destroy(context, agent_build_id):
    rows_affected = model_query(context, models.AgentBuild).filter_by(
                                        id=agent_build_id).soft_delete()
    if rows_affected == 0:
        raise exception.AgentBuildNotFound(id=agent_build_id)


@main_context_manager.writer
def agent_build_update(context, agent_build_id, values):
    rows_affected = model_query(context, models.AgentBuild).\
                   filter_by(id=agent_build_id).\
                   update(values)
    if rows_affected == 0:
        raise exception.AgentBuildNotFound(id=agent_build_id)


####################

@require_context
@pick_context_manager_reader_allow_async
def bw_usage_get(context, uuid, start_period, mac):
    values = {'start_period': start_period}
    values = convert_objects_related_datetimes(values, 'start_period')
    return model_query(context, models.BandwidthUsage, read_deleted="yes").\
                           filter_by(start_period=values['start_period']).\
                           filter_by(uuid=uuid).\
                           filter_by(mac=mac).\
                           first()


@require_context
@pick_context_manager_reader_allow_async
def bw_usage_get_by_uuids(context, uuids, start_period):
    values = {'start_period': start_period}
    values = convert_objects_related_datetimes(values, 'start_period')
    return (
        model_query(context, models.BandwidthUsage, read_deleted="yes").
        filter(models.BandwidthUsage.uuid.in_(uuids)).
        filter_by(start_period=values['start_period']).
        all()
    )


@require_context
@oslo_db_api.wrap_db_retry(max_retries=5, retry_on_deadlock=True)
@pick_context_manager_writer
def bw_usage_update(context, uuid, mac, start_period, bw_in, bw_out,
                    last_ctr_in, last_ctr_out, last_refreshed=None):

    if last_refreshed is None:
        last_refreshed = timeutils.utcnow()

    # NOTE(comstud): More often than not, we'll be updating records vs
    # creating records.  Optimize accordingly, trying to update existing
    # records.  Fall back to creation when no rows are updated.
    ts_values = {'last_refreshed': last_refreshed,
                 'start_period': start_period}
    ts_keys = ('start_period', 'last_refreshed')
    ts_values = convert_objects_related_datetimes(ts_values, *ts_keys)
    values = {'last_refreshed': ts_values['last_refreshed'],
              'last_ctr_in': last_ctr_in,
              'last_ctr_out': last_ctr_out,
              'bw_in': bw_in,
              'bw_out': bw_out}
    # NOTE(pkholkin): order_by() is needed here to ensure that the
    # same record is updated every time. It can be removed after adding
    # unique constraint to this model.
    bw_usage = model_query(context, models.BandwidthUsage,
            read_deleted='yes').\
                    filter_by(start_period=ts_values['start_period']).\
                    filter_by(uuid=uuid).\
                    filter_by(mac=mac).\
                    order_by(asc(models.BandwidthUsage.id)).first()

    if bw_usage:
        bw_usage.update(values)
        return bw_usage

    bwusage = models.BandwidthUsage()
    bwusage.start_period = ts_values['start_period']
    bwusage.uuid = uuid
    bwusage.mac = mac
    bwusage.last_refreshed = ts_values['last_refreshed']
    bwusage.bw_in = bw_in
    bwusage.bw_out = bw_out
    bwusage.last_ctr_in = last_ctr_in
    bwusage.last_ctr_out = last_ctr_out
    bwusage.save(context.session)

    return bwusage


####################


@require_context
@pick_context_manager_reader
def vol_get_usage_by_time(context, begin):
    """Return volumes usage that have been updated after a specified time."""
    return model_query(context, models.VolumeUsage, read_deleted="yes").\
                   filter(or_(models.VolumeUsage.tot_last_refreshed == null(),
                              models.VolumeUsage.tot_last_refreshed > begin,
                              models.VolumeUsage.curr_last_refreshed == null(),
                              models.VolumeUsage.curr_last_refreshed > begin,
                              )).all()


@require_context
@pick_context_manager_writer
def vol_usage_update(context, id, rd_req, rd_bytes, wr_req, wr_bytes,
                     instance_id, project_id, user_id, availability_zone,
                     update_totals=False):

    refreshed = timeutils.utcnow()

    values = {}
    # NOTE(dricco): We will be mostly updating current usage records vs
    # updating total or creating records. Optimize accordingly.
    if not update_totals:
        values = {'curr_last_refreshed': refreshed,
                  'curr_reads': rd_req,
                  'curr_read_bytes': rd_bytes,
                  'curr_writes': wr_req,
                  'curr_write_bytes': wr_bytes,
                  'instance_uuid': instance_id,
                  'project_id': project_id,
                  'user_id': user_id,
                  'availability_zone': availability_zone}
    else:
        values = {'tot_last_refreshed': refreshed,
                  'tot_reads': models.VolumeUsage.tot_reads + rd_req,
                  'tot_read_bytes': models.VolumeUsage.tot_read_bytes +
                                    rd_bytes,
                  'tot_writes': models.VolumeUsage.tot_writes + wr_req,
                  'tot_write_bytes': models.VolumeUsage.tot_write_bytes +
                                     wr_bytes,
                  'curr_reads': 0,
                  'curr_read_bytes': 0,
                  'curr_writes': 0,
                  'curr_write_bytes': 0,
                  'instance_uuid': instance_id,
                  'project_id': project_id,
                  'user_id': user_id,
                  'availability_zone': availability_zone}

    current_usage = model_query(context, models.VolumeUsage,
                        read_deleted="yes").\
                        filter_by(volume_id=id).\
                        first()
    if current_usage:
        if (rd_req < current_usage['curr_reads'] or
            rd_bytes < current_usage['curr_read_bytes'] or
            wr_req < current_usage['curr_writes'] or
                wr_bytes < current_usage['curr_write_bytes']):
            LOG.info(_LI("Volume(%s) has lower stats then what is in "
                         "the database. Instance must have been rebooted "
                         "or crashed. Updating totals."), id)
            if not update_totals:
                values['tot_reads'] = (models.VolumeUsage.tot_reads +
                                       current_usage['curr_reads'])
                values['tot_read_bytes'] = (
                    models.VolumeUsage.tot_read_bytes +
                    current_usage['curr_read_bytes'])
                values['tot_writes'] = (models.VolumeUsage.tot_writes +
                                        current_usage['curr_writes'])
                values['tot_write_bytes'] = (
                    models.VolumeUsage.tot_write_bytes +
                    current_usage['curr_write_bytes'])
            else:
                values['tot_reads'] = (models.VolumeUsage.tot_reads +
                                       current_usage['curr_reads'] +
                                       rd_req)
                values['tot_read_bytes'] = (
                    models.VolumeUsage.tot_read_bytes +
                    current_usage['curr_read_bytes'] + rd_bytes)
                values['tot_writes'] = (models.VolumeUsage.tot_writes +
                                        current_usage['curr_writes'] +
                                        wr_req)
                values['tot_write_bytes'] = (
                    models.VolumeUsage.tot_write_bytes +
                    current_usage['curr_write_bytes'] + wr_bytes)

        current_usage.update(values)
        current_usage.save(context.session)
        context.session.refresh(current_usage)
        return current_usage

    vol_usage = models.VolumeUsage()
    vol_usage.volume_id = id
    vol_usage.instance_uuid = instance_id
    vol_usage.project_id = project_id
    vol_usage.user_id = user_id
    vol_usage.availability_zone = availability_zone

    if not update_totals:
        vol_usage.curr_last_refreshed = refreshed
        vol_usage.curr_reads = rd_req
        vol_usage.curr_read_bytes = rd_bytes
        vol_usage.curr_writes = wr_req
        vol_usage.curr_write_bytes = wr_bytes
    else:
        vol_usage.tot_last_refreshed = refreshed
        vol_usage.tot_reads = rd_req
        vol_usage.tot_read_bytes = rd_bytes
        vol_usage.tot_writes = wr_req
        vol_usage.tot_write_bytes = wr_bytes

    vol_usage.save(context.session)

    return vol_usage


####################


@main_context_manager.reader
def s3_image_get(context, image_id):
    """Find local s3 image represented by the provided id."""
    result = model_query(context, models.S3Image, read_deleted="yes").\
                 filter_by(id=image_id).\
                 first()

    if not result:
        raise exception.ImageNotFound(image_id=image_id)

    return result


@main_context_manager.reader
def s3_image_get_by_uuid(context, image_uuid):
    """Find local s3 image represented by the provided uuid."""
    result = model_query(context, models.S3Image, read_deleted="yes").\
                 filter_by(uuid=image_uuid).\
                 first()

    if not result:
        raise exception.ImageNotFound(image_id=image_uuid)

    return result


@main_context_manager.writer
def s3_image_create(context, image_uuid):
    """Create local s3 image represented by provided uuid."""
    try:
        s3_image_ref = models.S3Image()
        s3_image_ref.update({'uuid': image_uuid})
        s3_image_ref.save(context.session)
    except Exception as e:
        raise db_exc.DBError(e)

    return s3_image_ref


####################


def _aggregate_get_query(context, model_class, id_field=None, id=None,
                         read_deleted=None):
    columns_to_join = {models.Aggregate: ['_hosts', '_metadata']}

    query = model_query(context, model_class, read_deleted=read_deleted)

    for c in columns_to_join.get(model_class, []):
        query = query.options(joinedload(c))

    if id and id_field:
        query = query.filter(id_field == id)

    return query


@main_context_manager.writer
def aggregate_create(context, values, metadata=None):
    query = _aggregate_get_query(context,
                                 models.Aggregate,
                                 models.Aggregate.name,
                                 values['name'],
                                 read_deleted='no')
    aggregate = query.first()
    if not aggregate:
        aggregate = models.Aggregate()
        aggregate.update(values)
        aggregate.save(context.session)
        # We don't want these to be lazy loaded later.  We know there is
        # nothing here since we just created this aggregate.
        aggregate._hosts = []
        aggregate._metadata = []
    else:
        raise exception.AggregateNameExists(aggregate_name=values['name'])
    if metadata:
        aggregate_metadata_add(context, aggregate.id, metadata)
        # NOTE(pkholkin): '_metadata' attribute was updated during
        # 'aggregate_metadata_add' method, so it should be expired and
        # read from db
        context.session.expire(aggregate, ['_metadata'])
        aggregate._metadata

    return aggregate


@main_context_manager.reader
def aggregate_get(context, aggregate_id):
    query = _aggregate_get_query(context,
                                 models.Aggregate,
                                 models.Aggregate.id,
                                 aggregate_id)
    aggregate = query.first()

    if not aggregate:
        raise exception.AggregateNotFound(aggregate_id=aggregate_id)

    return aggregate


@main_context_manager.reader
def aggregate_get_by_uuid(context, uuid):
    query = _aggregate_get_query(context,
                                 models.Aggregate,
                                 models.Aggregate.uuid,
                                 uuid)
    aggregate = query.first()

    if not aggregate:
        raise exception.AggregateNotFound(aggregate_id=uuid)

    return aggregate


@main_context_manager.reader
def aggregate_get_by_host(context, host, key=None):
    """Return rows that match host (mandatory) and metadata key (optional).

    :param host matches host, and is required.
    :param key Matches metadata key, if not None.
    """
    query = model_query(context, models.Aggregate)
    query = query.options(joinedload('_hosts'))
    query = query.options(joinedload('_metadata'))
    query = query.join('_hosts')
    query = query.filter(models.AggregateHost.host == host)

    if key:
        query = query.join("_metadata").filter(
            models.AggregateMetadata.key == key)
    return query.all()


@main_context_manager.reader
def aggregate_metadata_get_by_host(context, host, key=None):
    query = model_query(context, models.Aggregate)
    query = query.join("_hosts")
    query = query.join("_metadata")
    query = query.filter(models.AggregateHost.host == host)
    query = query.options(contains_eager("_metadata"))

    if key:
        query = query.filter(models.AggregateMetadata.key == key)
    rows = query.all()

    metadata = collections.defaultdict(set)
    for agg in rows:
        for kv in agg._metadata:
            metadata[kv['key']].add(kv['value'])
    return dict(metadata)


@main_context_manager.reader
def aggregate_get_by_metadata_key(context, key):
    """Return rows that match metadata key.

    :param key Matches metadata key.
    """
    query = model_query(context, models.Aggregate)
    query = query.join("_metadata")
    query = query.filter(models.AggregateMetadata.key == key)
    query = query.options(contains_eager("_metadata"))
    query = query.options(joinedload("_hosts"))
    return query.all()


@main_context_manager.writer
def aggregate_update(context, aggregate_id, values):
    if "name" in values:
        aggregate_by_name = (_aggregate_get_query(context,
                                                  models.Aggregate,
                                                  models.Aggregate.name,
                                                  values['name'],
                                                  read_deleted='no').first())
        if aggregate_by_name and aggregate_by_name.id != aggregate_id:
            # there is another aggregate with the new name
            raise exception.AggregateNameExists(aggregate_name=values['name'])

    aggregate = (_aggregate_get_query(context,
                                     models.Aggregate,
                                     models.Aggregate.id,
                                     aggregate_id).first())

    set_delete = True
    if aggregate:
        if "availability_zone" in values:
            az = values.pop('availability_zone')
            if 'metadata' not in values:
                values['metadata'] = {'availability_zone': az}
                set_delete = False
            else:
                values['metadata']['availability_zone'] = az
        metadata = values.get('metadata')
        if metadata is not None:
            aggregate_metadata_add(context,
                                   aggregate_id,
                                   values.pop('metadata'),
                                   set_delete=set_delete)

        aggregate.update(values)
        aggregate.save(context.session)
        return aggregate_get(context, aggregate.id)
    else:
        raise exception.AggregateNotFound(aggregate_id=aggregate_id)


@main_context_manager.writer
def aggregate_delete(context, aggregate_id):
    count = _aggregate_get_query(context,
                                 models.Aggregate,
                                 models.Aggregate.id,
                                 aggregate_id).\
                soft_delete()
    if count == 0:
        raise exception.AggregateNotFound(aggregate_id=aggregate_id)

    # Delete Metadata
    model_query(context, models.AggregateMetadata).\
                filter_by(aggregate_id=aggregate_id).\
                soft_delete()


@main_context_manager.reader
def aggregate_get_all(context):
    return _aggregate_get_query(context, models.Aggregate).all()


def _aggregate_metadata_get_query(context, aggregate_id, read_deleted="yes"):
    return model_query(context,
                       models.AggregateMetadata,
                       read_deleted=read_deleted).\
                filter_by(aggregate_id=aggregate_id)


@require_aggregate_exists
@main_context_manager.reader
def aggregate_metadata_get(context, aggregate_id):
    rows = model_query(context,
                       models.AggregateMetadata).\
                       filter_by(aggregate_id=aggregate_id).all()

    return {r['key']: r['value'] for r in rows}


@require_aggregate_exists
@main_context_manager.writer
def aggregate_metadata_delete(context, aggregate_id, key):
    count = _aggregate_get_query(context,
                                 models.AggregateMetadata,
                                 models.AggregateMetadata.aggregate_id,
                                 aggregate_id).\
                                 filter_by(key=key).\
                                 soft_delete()
    if count == 0:
        raise exception.AggregateMetadataNotFound(aggregate_id=aggregate_id,
                                                  metadata_key=key)


@require_aggregate_exists
@main_context_manager.writer
def aggregate_metadata_add(context, aggregate_id, metadata, set_delete=False,
                           max_retries=10):
    all_keys = metadata.keys()
    for attempt in range(max_retries):
        try:
            query = _aggregate_metadata_get_query(context, aggregate_id,
                                                  read_deleted='no')
            if set_delete:
                query.filter(~models.AggregateMetadata.key.in_(all_keys)).\
                    soft_delete(synchronize_session=False)

            already_existing_keys = set()
            if all_keys:
                query = query.filter(
                    models.AggregateMetadata.key.in_(all_keys))
                for meta_ref in query.all():
                    key = meta_ref.key
                    meta_ref.update({"value": metadata[key]})
                    already_existing_keys.add(key)

            new_entries = []
            for key, value in metadata.items():
                if key in already_existing_keys:
                    continue
                new_entries.append({"key": key,
                                    "value": value,
                                    "aggregate_id": aggregate_id})
            if new_entries:
                context.session.execute(
                    models.AggregateMetadata.__table__.insert(),
                    new_entries)

            return metadata
        except db_exc.DBDuplicateEntry:
            # a concurrent transaction has been committed,
            # try again unless this was the last attempt
            with excutils.save_and_reraise_exception() as ctxt:
                if attempt < max_retries - 1:
                    ctxt.reraise = False
                else:
                    LOG.warning(_LW("Add metadata failed for aggregate %(id)s "
                                    "after %(retries)s retries"),
                                {"id": aggregate_id, "retries": max_retries})


@require_aggregate_exists
@main_context_manager.reader
def aggregate_host_get_all(context, aggregate_id):
    rows = model_query(context,
                       models.AggregateHost).\
                       filter_by(aggregate_id=aggregate_id).all()

    return [r.host for r in rows]


@require_aggregate_exists
@main_context_manager.writer
def aggregate_host_delete(context, aggregate_id, host):
    count = _aggregate_get_query(context,
                                 models.AggregateHost,
                                 models.AggregateHost.aggregate_id,
                                 aggregate_id).\
            filter_by(host=host).\
            soft_delete()
    if count == 0:
        raise exception.AggregateHostNotFound(aggregate_id=aggregate_id,
                                              host=host)


@require_aggregate_exists
@main_context_manager.writer
def aggregate_host_add(context, aggregate_id, host):
    host_ref = models.AggregateHost()
    host_ref.update({"host": host, "aggregate_id": aggregate_id})
    try:
        host_ref.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.AggregateHostExists(host=host,
                                            aggregate_id=aggregate_id)
    return host_ref


################


@pick_context_manager_writer
def instance_fault_create(context, values):
    """Create a new InstanceFault."""
    fault_ref = models.InstanceFault()
    fault_ref.update(values)
    fault_ref.save(context.session)
    return dict(fault_ref)


@pick_context_manager_reader
def instance_fault_get_by_instance_uuids(context, instance_uuids):
    """Get all instance faults for the provided instance_uuids."""
    if not instance_uuids:
        return {}

    rows = model_query(context, models.InstanceFault, read_deleted='no').\
                       filter(models.InstanceFault.instance_uuid.in_(
                           instance_uuids)).\
                       order_by(desc("created_at"), desc("id")).\
                       all()

    output = {}
    for instance_uuid in instance_uuids:
        output[instance_uuid] = []

    for row in rows:
        data = dict(row)
        output[row['instance_uuid']].append(data)

    return output


##################


@pick_context_manager_writer
def action_start(context, values):
    convert_objects_related_datetimes(values, 'start_time')
    action_ref = models.InstanceAction()
    action_ref.update(values)
    action_ref.save(context.session)
    return action_ref


@pick_context_manager_writer
def action_finish(context, values):
    convert_objects_related_datetimes(values, 'start_time', 'finish_time')
    query = model_query(context, models.InstanceAction).\
                        filter_by(instance_uuid=values['instance_uuid']).\
                        filter_by(request_id=values['request_id'])
    if query.update(values) != 1:
        raise exception.InstanceActionNotFound(
                                    request_id=values['request_id'],
                                    instance_uuid=values['instance_uuid'])
    return query.one()


@pick_context_manager_reader
def actions_get(context, instance_uuid):
    """Get all instance actions for the provided uuid."""
    actions = model_query(context, models.InstanceAction).\
                          filter_by(instance_uuid=instance_uuid).\
                          order_by(desc("created_at"), desc("id")).\
                          all()
    return actions


@pick_context_manager_reader
def action_get_by_request_id(context, instance_uuid, request_id):
    """Get the action by request_id and given instance."""
    action = _action_get_by_request_id(context, instance_uuid, request_id)
    return action


def _action_get_by_request_id(context, instance_uuid, request_id):
    result = model_query(context, models.InstanceAction).\
                         filter_by(instance_uuid=instance_uuid).\
                         filter_by(request_id=request_id).\
                         first()
    return result


def _action_get_last_created_by_instance_uuid(context, instance_uuid):
    result = (model_query(context, models.InstanceAction).
                     filter_by(instance_uuid=instance_uuid).
                     order_by(desc("created_at"), desc("id")).
                     first())
    return result


@pick_context_manager_writer
def action_event_start(context, values):
    """Start an event on an instance action."""
    convert_objects_related_datetimes(values, 'start_time')
    action = _action_get_by_request_id(context, values['instance_uuid'],
                                       values['request_id'])
    # When nova-compute restarts, the context is generated again in
    # init_host workflow, the request_id was different with the request_id
    # recorded in InstanceAction, so we can't get the original record
    # according to request_id. Try to get the last created action so that
    # init_instance can continue to finish the recovery action, like:
    # powering_off, unpausing, and so on.
    if not action and not context.project_id:
        action = _action_get_last_created_by_instance_uuid(
            context, values['instance_uuid'])

    if not action:
        raise exception.InstanceActionNotFound(
                                    request_id=values['request_id'],
                                    instance_uuid=values['instance_uuid'])

    values['action_id'] = action['id']

    event_ref = models.InstanceActionEvent()
    event_ref.update(values)
    context.session.add(event_ref)
    return event_ref


@pick_context_manager_writer
def action_event_finish(context, values):
    """Finish an event on an instance action."""
    convert_objects_related_datetimes(values, 'start_time', 'finish_time')
    action = _action_get_by_request_id(context, values['instance_uuid'],
                                       values['request_id'])
    # When nova-compute restarts, the context is generated again in
    # init_host workflow, the request_id was different with the request_id
    # recorded in InstanceAction, so we can't get the original record
    # according to request_id. Try to get the last created action so that
    # init_instance can continue to finish the recovery action, like:
    # powering_off, unpausing, and so on.
    if not action and not context.project_id:
        action = _action_get_last_created_by_instance_uuid(
            context, values['instance_uuid'])

    if not action:
        raise exception.InstanceActionNotFound(
                                    request_id=values['request_id'],
                                    instance_uuid=values['instance_uuid'])

    event_ref = model_query(context, models.InstanceActionEvent).\
                            filter_by(action_id=action['id']).\
                            filter_by(event=values['event']).\
                            first()

    if not event_ref:
        raise exception.InstanceActionEventNotFound(action_id=action['id'],
                                                    event=values['event'])
    event_ref.update(values)

    if values['result'].lower() == 'error':
        action.update({'message': 'Error'})

    return event_ref


@pick_context_manager_reader
def action_events_get(context, action_id):
    events = model_query(context, models.InstanceActionEvent).\
                         filter_by(action_id=action_id).\
                         order_by(desc("created_at"), desc("id")).\
                         all()

    return events


@pick_context_manager_reader
def action_event_get_by_id(context, action_id, event_id):
    event = model_query(context, models.InstanceActionEvent).\
                        filter_by(action_id=action_id).\
                        filter_by(id=event_id).\
                        first()

    return event


##################


@require_context
@pick_context_manager_writer
def ec2_instance_create(context, instance_uuid, id=None):
    """Create ec2 compatible instance by provided uuid."""
    ec2_instance_ref = models.InstanceIdMapping()
    ec2_instance_ref.update({'uuid': instance_uuid})
    if id is not None:
        ec2_instance_ref.update({'id': id})

    ec2_instance_ref.save(context.session)

    return ec2_instance_ref


@require_context
@pick_context_manager_reader
def ec2_instance_get_by_uuid(context, instance_uuid):
    result = _ec2_instance_get_query(context).\
                    filter_by(uuid=instance_uuid).\
                    first()

    if not result:
        raise exception.InstanceNotFound(instance_id=instance_uuid)

    return result


@require_context
@pick_context_manager_reader
def ec2_instance_get_by_id(context, instance_id):
    result = _ec2_instance_get_query(context).\
                    filter_by(id=instance_id).\
                    first()

    if not result:
        raise exception.InstanceNotFound(instance_id=instance_id)

    return result


@require_context
@pick_context_manager_reader
def get_instance_uuid_by_ec2_id(context, ec2_id):
    result = ec2_instance_get_by_id(context, ec2_id)
    return result['uuid']


def _ec2_instance_get_query(context):
    return model_query(context, models.InstanceIdMapping, read_deleted='yes')


##################


def _task_log_get_query(context, task_name, period_beginning,
                        period_ending, host=None, state=None):
    values = {'period_beginning': period_beginning,
              'period_ending': period_ending}
    values = convert_objects_related_datetimes(values, *values.keys())

    query = model_query(context, models.TaskLog).\
                     filter_by(task_name=task_name).\
                     filter_by(period_beginning=values['period_beginning']).\
                     filter_by(period_ending=values['period_ending'])
    if host is not None:
        query = query.filter_by(host=host)
    if state is not None:
        query = query.filter_by(state=state)
    return query


@pick_context_manager_reader
def task_log_get(context, task_name, period_beginning, period_ending, host,
                 state=None):
    return _task_log_get_query(context, task_name, period_beginning,
                               period_ending, host, state).first()


@pick_context_manager_reader
def task_log_get_all(context, task_name, period_beginning, period_ending,
                     host=None, state=None):
    return _task_log_get_query(context, task_name, period_beginning,
                               period_ending, host, state).all()


@pick_context_manager_writer
def task_log_begin_task(context, task_name, period_beginning, period_ending,
                        host, task_items=None, message=None):
    values = {'period_beginning': period_beginning,
              'period_ending': period_ending}
    values = convert_objects_related_datetimes(values, *values.keys())

    task = models.TaskLog()
    task.task_name = task_name
    task.period_beginning = values['period_beginning']
    task.period_ending = values['period_ending']
    task.host = host
    task.state = "RUNNING"
    if message:
        task.message = message
    if task_items:
        task.task_items = task_items
    try:
        task.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.TaskAlreadyRunning(task_name=task_name, host=host)


@pick_context_manager_writer
def task_log_end_task(context, task_name, period_beginning, period_ending,
                      host, errors, message=None):
    values = dict(state="DONE", errors=errors)
    if message:
        values["message"] = message

    rows = _task_log_get_query(context, task_name, period_beginning,
                               period_ending, host).update(values)
    if rows == 0:
        # It's not running!
        raise exception.TaskNotRunning(task_name=task_name, host=host)


##################


def _archive_deleted_rows_for_table(tablename, max_rows):
    """Move up to max_rows rows from one tables to the corresponding
    shadow table.

    :returns: number of rows archived
    """
    # NOTE(guochbo): There is a circular import, nova.db.sqlalchemy.utils
    # imports nova.db.sqlalchemy.api.
    from nova.db.sqlalchemy import utils as db_utils

    engine = get_engine()
    conn = engine.connect()
    metadata = MetaData()
    metadata.bind = engine
    # NOTE(tdurakov): table metadata should be received
    # from models, not db tables. Default value specified by SoftDeleteMixin
    # is known only by models, not DB layer.
    # IMPORTANT: please do not change source of metadata information for table.
    table = models.BASE.metadata.tables[tablename]

    shadow_tablename = _SHADOW_TABLE_PREFIX + tablename
    rows_archived = 0
    try:
        shadow_table = Table(shadow_tablename, metadata, autoload=True)
    except NoSuchTableError:
        # No corresponding shadow table; skip it.
        return rows_archived

    if tablename == "dns_domains":
        # We have one table (dns_domains) where the key is called
        # "domain" rather than "id"
        column = table.c.domain
    else:
        column = table.c.id
    # NOTE(guochbo): Use DeleteFromSelect to avoid
    # database's limit of maximum parameter in one SQL statement.
    deleted_column = table.c.deleted
    columns = [c.name for c in table.c]

    # NOTE(clecomte): Tables instance_actions and instances_actions_events
    # have to be manage differently so we soft-delete them here to let
    # the archive work the same for all tables
    if tablename == "instance_actions":
        instances = models.BASE.metadata.tables["instances"]
        deleted_instances = sql.select([instances.c.uuid]).\
            where(instances.c.deleted != instances.c.deleted.default.arg)
        update_statement = table.update().values(deleted=table.c.id).\
            where(table.c.instance_uuid.in_(deleted_instances))

        conn.execute(update_statement)

    elif tablename == "instance_actions_events":
        # NOTE(clecomte): we have to grab all the relation from
        # instances because instance_actions_events rely on
        # action_id and not uuid
        instances = models.BASE.metadata.tables["instances"]
        instance_actions = models.BASE.metadata.tables["instance_actions"]
        deleted_instances = sql.select([instances.c.uuid]).\
            where(instances.c.deleted != instances.c.deleted.default.arg)
        deleted_actions = sql.select([instance_actions.c.id]).\
            where(instance_actions.c.instance_uuid.in_(deleted_instances))

        update_statement = table.update().values(deleted=table.c.id).\
            where(table.c.action_id.in_(deleted_actions))

        conn.execute(update_statement)

    insert = shadow_table.insert(inline=True).\
        from_select(columns,
                    sql.select([table],
                               deleted_column != deleted_column.default.arg).
                    order_by(column).limit(max_rows))
    query_delete = sql.select([column],
                          deleted_column != deleted_column.default.arg).\
                          order_by(column).limit(max_rows)

    delete_statement = db_utils.DeleteFromSelect(table, query_delete, column)
    try:
        # Group the insert and delete in a transaction.
        with conn.begin():
            conn.execute(insert)
            result_delete = conn.execute(delete_statement)
    except db_exc.DBReferenceError as ex:
        # A foreign key constraint keeps us from deleting some of
        # these rows until we clean up a dependent table.  Just
        # skip this table for now; we'll come back to it later.
        LOG.warning(_LW("IntegrityError detected when archiving table "
                        "%(tablename)s: %(error)s"),
                    {'tablename': tablename, 'error': six.text_type(ex)})
        return rows_archived

    rows_archived = result_delete.rowcount

    return rows_archived


def archive_deleted_rows(max_rows=None):
    """Move up to max_rows rows from production tables to the corresponding
    shadow tables.

    :returns: dict that maps table name to number of rows archived from that
              table, for example:

    ::

        {
            'instances': 5,
            'block_device_mapping': 5,
            'pci_devices': 2,
        }

    """
    table_to_rows_archived = {}
    total_rows_archived = 0
    meta = MetaData(get_engine(use_slave=True))
    meta.reflect()
    # Reverse sort the tables so we get the leaf nodes first for processing.
    for table in reversed(meta.sorted_tables):
        tablename = table.name
        # skip the special sqlalchemy-migrate migrate_version table and any
        # shadow tables
        if (tablename == 'migrate_version' or
                tablename.startswith(_SHADOW_TABLE_PREFIX)):
            continue
        rows_archived = _archive_deleted_rows_for_table(
            tablename, max_rows=max_rows - total_rows_archived)
        total_rows_archived += rows_archived
        # Only report results for tables that had updates.
        if rows_archived:
            table_to_rows_archived[tablename] = rows_archived
        if total_rows_archived >= max_rows:
            break
    return table_to_rows_archived


@main_context_manager.writer
def pcidevice_online_data_migration(context, max_count):
    from nova.objects import pci_device as pci_dev_obj

    count_all = 0
    count_hit = 0

    if not pci_dev_obj.PciDevice.should_migrate_data():
        LOG.error(_LE("Data migrations for PciDevice are not safe, likely "
                      "because not all services that access the DB directly "
                      "are updated to the latest version"))
    else:
        results = model_query(context, models.PciDevice).filter_by(
            parent_addr=None).limit(max_count)

        for db_dict in results:
            count_all += 1
            pci_dev = pci_dev_obj.PciDevice._from_db_object(
                context, pci_dev_obj.PciDevice(), db_dict)
            if pci_dev.obj_what_changed():
                pci_dev.save()
                count_hit += 1
    return count_all, count_hit


@main_context_manager.writer
def aggregate_uuids_online_data_migration(context, max_count):
    from nova.objects import aggregate

    count_all = 0
    count_hit = 0

    results = model_query(context, models.Aggregate).filter_by(
        uuid=None).limit(max_count)
    for db_agg in results:
        count_all += 1
        agg = aggregate.Aggregate._from_db_object(context,
                                                  aggregate.Aggregate(),
                                                  db_agg)
        if 'uuid' in agg:
            count_hit += 1
    return count_all, count_hit


####################


def _instance_group_get_query(context, model_class, id_field=None, id=None,
                              read_deleted=None):
    columns_to_join = {models.InstanceGroup: ['_policies', '_members']}
    query = model_query(context, model_class, read_deleted=read_deleted,
                        project_only=True)
    for c in columns_to_join.get(model_class, []):
        query = query.options(joinedload(c))

    if id and id_field:
        query = query.filter(id_field == id)

    return query


@main_context_manager.writer
def instance_group_create(context, values, policies=None, members=None):
    """Create a new group."""
    uuid = values.get('uuid', None)
    if uuid is None:
        uuid = uuidutils.generate_uuid()
        values['uuid'] = uuid

    try:
        group = models.InstanceGroup()
        group.update(values)
        group.save(context.session)
    except db_exc.DBDuplicateEntry:
        raise exception.InstanceGroupIdExists(group_uuid=uuid)

    # We don't want '_policies' and '_members' attributes to be lazy loaded
    # later. We know there is nothing here since we just created this
    # instance group.
    if policies:
        _instance_group_policies_add(context, group.id, policies)
    else:
        group._policies = []
    if members:
        _instance_group_members_add(context, group.id, members)
    else:
        group._members = []

    return instance_group_get(context, uuid)


@main_context_manager.reader
def instance_group_get(context, group_uuid):
    """Get a specific group by uuid."""
    group = _instance_group_get_query(context,
                                      models.InstanceGroup,
                                      models.InstanceGroup.uuid,
                                      group_uuid).\
                            first()
    if not group:
        raise exception.InstanceGroupNotFound(group_uuid=group_uuid)
    return group


@main_context_manager.reader
def instance_group_get_by_instance(context, instance_uuid):
    group_member = model_query(context, models.InstanceGroupMember).\
                               filter_by(instance_id=instance_uuid).\
                               first()
    if not group_member:
        raise exception.InstanceGroupNotFound(group_uuid='')
    group = _instance_group_get_query(context, models.InstanceGroup,
                                      models.InstanceGroup.id,
                                      group_member.group_id).first()
    if not group:
        raise exception.InstanceGroupNotFound(
                group_uuid=group_member.group_id)
    return group


@main_context_manager.writer
def instance_group_update(context, group_uuid, values):
    """Update the attributes of a group.

    If values contains a metadata key, it updates the aggregate metadata
    too. Similarly for the policies and members.
    """
    group = model_query(context, models.InstanceGroup).\
            filter_by(uuid=group_uuid).\
            first()
    if not group:
        raise exception.InstanceGroupNotFound(group_uuid=group_uuid)

    policies = values.get('policies')
    if policies is not None:
        _instance_group_policies_add(context,
                                     group.id,
                                     values.pop('policies'),
                                     set_delete=True)
    members = values.get('members')
    if members is not None:
        _instance_group_members_add(context,
                                    group.id,
                                    values.pop('members'),
                                    set_delete=True)

    group.update(values)

    if policies:
        values['policies'] = policies
    if members:
        values['members'] = members


@main_context_manager.writer
def instance_group_delete(context, group_uuid):
    """Delete a group."""
    group_id = _instance_group_id(context, group_uuid)

    count = _instance_group_get_query(context,
                                      models.InstanceGroup,
                                      models.InstanceGroup.uuid,
                                      group_uuid).soft_delete()
    if count == 0:
        raise exception.InstanceGroupNotFound(group_uuid=group_uuid)

    # Delete policies, metadata and members
    instance_models = [models.InstanceGroupPolicy,
                       models.InstanceGroupMember]
    for model in instance_models:
        model_query(context, model).filter_by(group_id=group_id).soft_delete()


@main_context_manager.reader
def instance_group_get_all(context):
    """Get all groups."""
    return _instance_group_get_query(context, models.InstanceGroup).all()


@main_context_manager.reader
def instance_group_get_all_by_project_id(context, project_id):
    """Get all groups."""
    return _instance_group_get_query(context, models.InstanceGroup).\
                            filter_by(project_id=project_id).\
                            all()


def _instance_group_count_by_project_and_user(context, project_id, user_id):
    return model_query(context, models.InstanceGroup, read_deleted="no").\
                   filter_by(project_id=project_id).\
                   filter_by(user_id=user_id).\
                   count()


def _instance_group_model_get_query(context, model_class, group_id,
                                    read_deleted='no'):
    return model_query(context,
                       model_class,
                       read_deleted=read_deleted).\
                filter_by(group_id=group_id)


def _instance_group_id(context, group_uuid):
    """Returns the group database ID for the group UUID."""

    result = model_query(context,
                         models.InstanceGroup,
                         (models.InstanceGroup.id,)).\
                filter_by(uuid=group_uuid).\
                first()
    if not result:
        raise exception.InstanceGroupNotFound(group_uuid=group_uuid)
    return result.id


def _instance_group_members_add(context, id, members, set_delete=False):
    all_members = set(members)
    query = _instance_group_model_get_query(context,
                                            models.InstanceGroupMember, id)
    if set_delete:
        query.filter(~models.InstanceGroupMember.instance_id.in_(
                     all_members)).\
              soft_delete(synchronize_session=False)

    query = query.filter(
            models.InstanceGroupMember.instance_id.in_(all_members))
    already_existing = set()
    for member_ref in query.all():
        already_existing.add(member_ref.instance_id)

    for instance_id in members:
        if instance_id in already_existing:
            continue
        member_ref = models.InstanceGroupMember()
        member_ref.update({'instance_id': instance_id,
                           'group_id': id})
        context.session.add(member_ref)

    return members


@main_context_manager.writer
def instance_group_members_add(context, group_uuid, members,
                               set_delete=False):
    id = _instance_group_id(context, group_uuid)
    return _instance_group_members_add(context, id, members,
                                       set_delete=set_delete)


@main_context_manager.writer
def instance_group_member_delete(context, group_uuid, instance_id):
    id = _instance_group_id(context, group_uuid)
    count = _instance_group_model_get_query(context,
                                            models.InstanceGroupMember,
                                            id).\
                            filter_by(instance_id=instance_id).\
                            soft_delete()
    if count == 0:
        raise exception.InstanceGroupMemberNotFound(group_uuid=group_uuid,
                                                    instance_id=instance_id)


@main_context_manager.reader
def instance_group_members_get(context, group_uuid):
    id = _instance_group_id(context, group_uuid)
    instances = model_query(context,
                            models.InstanceGroupMember,
                            (models.InstanceGroupMember.instance_id,)).\
                    filter_by(group_id=id).all()
    return [instance[0] for instance in instances]


def _instance_group_policies_add(context, id, policies, set_delete=False):
    allpols = set(policies)
    query = _instance_group_model_get_query(context,
                                            models.InstanceGroupPolicy, id)
    if set_delete:
        query.filter(~models.InstanceGroupPolicy.policy.in_(allpols)).\
            soft_delete(synchronize_session=False)

    query = query.filter(models.InstanceGroupPolicy.policy.in_(allpols))
    already_existing = set()
    for policy_ref in query.all():
        already_existing.add(policy_ref.policy)

    for policy in policies:
        if policy in already_existing:
            continue
        policy_ref = models.InstanceGroupPolicy()
        policy_ref.update({'policy': policy,
                           'group_id': id})
        context.session.add(policy_ref)

    return policies


####################


@pick_context_manager_reader
def pci_device_get_by_addr(context, node_id, dev_addr):
    pci_dev_ref = model_query(context, models.PciDevice).\
                        filter_by(compute_node_id=node_id).\
                        filter_by(address=dev_addr).\
                        first()
    if not pci_dev_ref:
        raise exception.PciDeviceNotFound(node_id=node_id, address=dev_addr)
    return pci_dev_ref


@pick_context_manager_reader
def pci_device_get_by_id(context, id):
    pci_dev_ref = model_query(context, models.PciDevice).\
                        filter_by(id=id).\
                        first()
    if not pci_dev_ref:
        raise exception.PciDeviceNotFoundById(id=id)
    return pci_dev_ref


@pick_context_manager_reader
def pci_device_get_all_by_node(context, node_id):
    return model_query(context, models.PciDevice).\
                       filter_by(compute_node_id=node_id).\
                       all()


@pick_context_manager_reader
def pci_device_get_all_by_parent_addr(context, node_id, parent_addr):
    return model_query(context, models.PciDevice).\
                       filter_by(compute_node_id=node_id).\
                       filter_by(parent_addr=parent_addr).\
                       all()


@require_context
@pick_context_manager_reader
def pci_device_get_all_by_instance_uuid(context, instance_uuid):
    return model_query(context, models.PciDevice).\
                       filter_by(status='allocated').\
                       filter_by(instance_uuid=instance_uuid).\
                       all()


@pick_context_manager_reader
def _instance_pcidevs_get_multi(context, instance_uuids):
    if not instance_uuids:
        return []
    return model_query(context, models.PciDevice).\
        filter_by(status='allocated').\
        filter(models.PciDevice.instance_uuid.in_(instance_uuids))


@pick_context_manager_writer
def pci_device_destroy(context, node_id, address):
    result = model_query(context, models.PciDevice).\
                         filter_by(compute_node_id=node_id).\
                         filter_by(address=address).\
                         soft_delete()
    if not result:
        raise exception.PciDeviceNotFound(node_id=node_id, address=address)


@pick_context_manager_writer
def pci_device_update(context, node_id, address, values):
    query = model_query(context, models.PciDevice, read_deleted="no").\
                    filter_by(compute_node_id=node_id).\
                    filter_by(address=address)
    if query.update(values) == 0:
        device = models.PciDevice()
        device.update(values)
        context.session.add(device)
    return query.one()


####################


@pick_context_manager_writer
def instance_tag_add(context, instance_uuid, tag):
    tag_ref = models.Tag()
    tag_ref.resource_id = instance_uuid
    tag_ref.tag = tag

    try:
        _check_instance_exists_in_project(context, instance_uuid)
        with get_context_manager(context).writer.savepoint.using(context):
            context.session.add(tag_ref)
    except db_exc.DBDuplicateEntry:
        # NOTE(snikitin): We should ignore tags duplicates
        pass

    return tag_ref


@pick_context_manager_writer
def instance_tag_set(context, instance_uuid, tags):
    _check_instance_exists_in_project(context, instance_uuid)

    existing = context.session.query(models.Tag.tag).filter_by(
        resource_id=instance_uuid).all()

    existing = set(row.tag for row in existing)
    tags = set(tags)
    to_delete = existing - tags
    to_add = tags - existing

    if to_delete:
        context.session.query(models.Tag).filter_by(
            resource_id=instance_uuid).filter(
            models.Tag.tag.in_(to_delete)).delete(
            synchronize_session=False)

    if to_add:
        data = [
            {'resource_id': instance_uuid, 'tag': tag} for tag in to_add]
        context.session.execute(models.Tag.__table__.insert(), data)

    return context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid).all()


@pick_context_manager_reader
def instance_tag_get_by_instance_uuid(context, instance_uuid):
    _check_instance_exists_in_project(context, instance_uuid)
    return context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid).all()


@pick_context_manager_writer
def instance_tag_delete(context, instance_uuid, tag):
    _check_instance_exists_in_project(context, instance_uuid)
    result = context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid, tag=tag).delete()

    if not result:
        raise exception.InstanceTagNotFound(instance_id=instance_uuid,
                                            tag=tag)


@pick_context_manager_writer
def instance_tag_delete_all(context, instance_uuid):
    _check_instance_exists_in_project(context, instance_uuid)
    context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid).delete()


@main_context_manager.reader
def instance_tag_exists(context, instance_uuid, tag):
    _check_instance_exists_in_project(context, instance_uuid)
    q = context.session.query(models.Tag).filter_by(
        resource_id=instance_uuid, tag=tag)
    return context.session.query(q.exists()).scalar()


####################


@pick_context_manager_writer
def console_auth_token_create(context, values):
    instance_uuid = values.get('instance_uuid')
    _check_instance_exists_in_project(context, instance_uuid)
    token_ref = models.ConsoleAuthToken()
    token_ref.update(values)
    context.session.add(token_ref)
    return token_ref


@pick_context_manager_reader
def console_auth_token_get_valid(context, token_hash, instance_uuid):
    _check_instance_exists_in_project(context, instance_uuid)
    return context.session.query(models.ConsoleAuthToken).\
        filter_by(token_hash=token_hash).\
        filter_by(instance_uuid=instance_uuid).\
        filter(models.ConsoleAuthToken.expires > timeutils.utcnow_ts()).\
        first()


@pick_context_manager_writer
def console_auth_token_destroy_all_by_instance(context, instance_uuid):
    context.session.query(models.ConsoleAuthToken).\
        filter_by(instance_uuid=instance_uuid).delete()


@pick_context_manager_writer
def console_auth_token_destroy_expired_by_host(context, host):
    context.session.query(models.ConsoleAuthToken).\
        filter_by(host=host).\
        filter(models.ConsoleAuthToken.expires <= timeutils.utcnow_ts()).\
        delete()
