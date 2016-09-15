# Copyright 2011 OpenStack Foundation
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

"""RequestContext: context for requests that persist through all of nova."""

from contextlib import contextmanager
import copy

from keystoneauth1.access import service_catalog as ksa_service_catalog
from keystoneauth1 import plugin
from oslo_context import context
from oslo_db.sqlalchemy import enginefacade
from oslo_log import log as logging
from oslo_utils import timeutils
import six

from nova import exception
from nova.i18n import _
from nova import policy
from nova import utils

LOG = logging.getLogger(__name__)


class _ContextAuthPlugin(plugin.BaseAuthPlugin):
    """A keystoneauth auth plugin that uses the values from the Context.

    Ideally we would use the plugin provided by auth_token middleware however
    this plugin isn't serialized yet so we construct one from the serialized
    auth data.
    """

    def __init__(self, auth_token, sc):
        super(_ContextAuthPlugin, self).__init__()

        self.auth_token = auth_token
        self.service_catalog = ksa_service_catalog.ServiceCatalogV2(sc)

    def get_token(self, *args, **kwargs):
        return self.auth_token

    def get_endpoint(self, session, service_type=None, interface=None,
                     region_name=None, service_name=None, **kwargs):
        return self.service_catalog.url_for(service_type=service_type,
                                            service_name=service_name,
                                            interface=interface,
                                            region_name=region_name)


@enginefacade.transaction_context_provider
class RequestContext(context.RequestContext):
    """Security context and request information.

    Represents the user taking a given action within the system.

    """

    def __init__(self, user_id=None, project_id=None, is_admin=None,
                 read_deleted="no", remote_address=None, timestamp=None,
                 quota_class=None, user_name=None, project_name=None,
                 service_catalog=None, instance_lock_checked=False,
                 user_auth_plugin=None, **kwargs):
        """:param read_deleted: 'no' indicates deleted records are hidden,
                'yes' indicates deleted records are visible,
                'only' indicates that *only* deleted records are visible.

           :param overwrite: Set to False to ensure that the greenthread local
                copy of the index is not overwritten.

           :param user_auth_plugin: The auth plugin for the current request's
                authentication data.
        """
        if user_id:
            kwargs['user'] = user_id
        if project_id:
            kwargs['tenant'] = project_id

        super(RequestContext, self).__init__(is_admin=is_admin, **kwargs)

        self.read_deleted = read_deleted
        self.remote_address = remote_address
        if not timestamp:
            timestamp = timeutils.utcnow()
        if isinstance(timestamp, six.string_types):
            timestamp = timeutils.parse_strtime(timestamp)
        self.timestamp = timestamp

        if service_catalog:
            # Only include required parts of service_catalog
            self.service_catalog = [s for s in service_catalog
                if s.get('type') in ('volume', 'volumev2', 'key-manager',
                                     'placement')]
        else:
            # if list is empty or none
            self.service_catalog = []

        self.instance_lock_checked = instance_lock_checked

        # NOTE(markmc): this attribute is currently only used by the
        # rs_limits turnstile pre-processor.
        # See https://lists.launchpad.net/openstack/msg12200.html
        self.quota_class = quota_class
        self.user_name = user_name
        self.project_name = project_name

        # NOTE(dheeraj): The following attributes are used by cellsv2 to store
        # connection information for connecting to the target cell.
        # It is only manipulated using the target_cell contextmanager
        # provided by this module
        self.db_connection = None
        self.mq_connection = None

        self.user_auth_plugin = user_auth_plugin
        if self.is_admin is None:
            self.is_admin = policy.check_is_admin(self)

    def get_auth_plugin(self):
        if self.user_auth_plugin:
            return self.user_auth_plugin
        else:
            return _ContextAuthPlugin(self.auth_token, self.service_catalog)

    def _get_read_deleted(self):
        return self._read_deleted

    def _set_read_deleted(self, read_deleted):
        if read_deleted not in ('no', 'yes', 'only'):
            raise ValueError(_("read_deleted can only be one of 'no', "
                               "'yes' or 'only', not %r") % read_deleted)
        self._read_deleted = read_deleted

    def _del_read_deleted(self):
        del self._read_deleted

    read_deleted = property(_get_read_deleted, _set_read_deleted,
                            _del_read_deleted)

    # FIXME(dims): user_id and project_id duplicate information that is
    # already present in the oslo_context's RequestContext. We need to
    # get rid of them.
    @property
    def project_id(self):
        return self.tenant

    @project_id.setter
    def project_id(self, value):
        self.tenant = value

    @property
    def user_id(self):
        return self.user

    @user_id.setter
    def user_id(self, value):
        self.user = value

    def to_dict(self):
        values = super(RequestContext, self).to_dict()
        # FIXME(dims): defensive hasattr() checks need to be
        # removed once we figure out why we are seeing stack
        # traces
        values.update({
            'user_id': getattr(self, 'user_id', None),
            'project_id': getattr(self, 'project_id', None),
            'is_admin': getattr(self, 'is_admin', None),
            'read_deleted': getattr(self, 'read_deleted', 'no'),
            'remote_address': getattr(self, 'remote_address', None),
            'timestamp': utils.strtime(self.timestamp) if hasattr(
                self, 'timestamp') else None,
            'request_id': getattr(self, 'request_id', None),
            'quota_class': getattr(self, 'quota_class', None),
            'user_name': getattr(self, 'user_name', None),
            'service_catalog': getattr(self, 'service_catalog', None),
            'project_name': getattr(self, 'project_name', None),
            'instance_lock_checked': getattr(self, 'instance_lock_checked',
                                             False)
        })
        # NOTE(tonyb): This can be removed once we're certain to have a
        # RequestContext contains 'is_admin_project', We can only get away with
        # this because we "know" the default value of 'is_admin_project' which
        # is very fragile.
        values.update({
            'is_admin_project': getattr(self, 'is_admin_project', True),
        })
        return values

    @classmethod
    def from_dict(cls, values):
        return cls(
            user_id=values.get('user_id'),
            user=values.get('user'),
            project_id=values.get('project_id'),
            tenant=values.get('tenant'),
            is_admin=values.get('is_admin'),
            read_deleted=values.get('read_deleted', 'no'),
            roles=values.get('roles'),
            remote_address=values.get('remote_address'),
            timestamp=values.get('timestamp'),
            request_id=values.get('request_id'),
            auth_token=values.get('auth_token'),
            quota_class=values.get('quota_class'),
            user_name=values.get('user_name'),
            project_name=values.get('project_name'),
            service_catalog=values.get('service_catalog'),
            instance_lock_checked=values.get('instance_lock_checked', False),
        )

    @classmethod
    def from_environ(cls, environ, **kwargs):
        ctx = super(RequestContext, cls).from_environ(environ, **kwargs)

        # the base oslo.context sets its user param and tenant param but not
        # our user_id and project_id param so fix those up.
        if ctx.user and not ctx.user_id:
            ctx.user_id = ctx.user
        if ctx.tenant and not ctx.project_id:
            ctx.project_id = ctx.tenant

        return ctx

    def elevated(self, read_deleted=None):
        """Return a version of this context with admin flag set."""
        context = copy.copy(self)
        # context.roles must be deepcopied to leave original roles
        # without changes
        context.roles = copy.deepcopy(self.roles)
        context.is_admin = True

        if 'admin' not in context.roles:
            context.roles.append('admin')

        if read_deleted is not None:
            context.read_deleted = read_deleted

        return context

    def can(self, rule, target=None, fatal=True):
        """Verifies that the given rule is valid on the target in this context.

        :param action: string representing the action to be checked.
        :param target: dictionary representing the object of the action
            for object creation this should be a dictionary representing the
            location of the object e.g. ``{'project_id': context.project_id}``.
            If None, then this default target will be considered:
            {'project_id': self.project_id, 'user_id': self.user_id}
        :param fatal: if False, will return False when an exception.Forbidden
           occurs.

        :raises nova.exception.Forbidden: if verification fails and fatal is
            True.

        :return: returns a non-False value (not necessarily "True") if
            authorized and False if not authorized and fatal is False.
        """
        if target is None:
            target = {'project_id': self.project_id,
                      'user_id': self.user_id}

        try:
            return policy.authorize(self, rule, target)
        except exception.Forbidden:
            if fatal:
                raise
            return False

    def __str__(self):
        return "<Context %s>" % self.to_dict()


def get_admin_context(read_deleted="no"):
    return RequestContext(user_id=None,
                          project_id=None,
                          is_admin=True,
                          read_deleted=read_deleted,
                          overwrite=False)


def is_user_context(context):
    """Indicates if the request context is a normal user."""
    if not context:
        return False
    if context.is_admin:
        return False
    if not context.user_id or not context.project_id:
        return False
    return True


def require_admin_context(ctxt):
    """Raise exception.AdminRequired() if context is not an admin context."""
    if not ctxt.is_admin:
        raise exception.AdminRequired()


def require_context(ctxt):
    """Raise exception.Forbidden() if context is not a user or an
    admin context.
    """
    if not ctxt.is_admin and not is_user_context(ctxt):
        raise exception.Forbidden()


def authorize_project_context(context, project_id):
    """Ensures a request has permission to access the given project."""
    if is_user_context(context):
        if not context.project_id:
            raise exception.Forbidden()
        elif context.project_id != project_id:
            raise exception.Forbidden()


def authorize_user_context(context, user_id):
    """Ensures a request has permission to access the given user."""
    if is_user_context(context):
        if not context.user_id:
            raise exception.Forbidden()
        elif context.user_id != user_id:
            raise exception.Forbidden()


def authorize_quota_class_context(context, class_name):
    """Ensures a request has permission to access the given quota class."""
    if is_user_context(context):
        if not context.quota_class:
            raise exception.Forbidden()
        elif context.quota_class != class_name:
            raise exception.Forbidden()


@contextmanager
def target_cell(context, cell_mapping):
    """Adds database connection information to the context
    for communicating with the given target cell.

    :param context: The RequestContext to add connection information
    :param cell_mapping: A objects.CellMapping object
    """
    original_db_connection = context.db_connection
    # avoid circular import
    from nova import db
    db_connection_string = cell_mapping.database_connection
    context.db_connection = db.create_context_manager(db_connection_string)
    try:
        yield context
    finally:
        context.db_connection = original_db_connection
