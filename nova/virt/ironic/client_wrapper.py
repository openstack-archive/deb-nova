# coding=utf-8
#
# Copyright 2014 Hewlett-Packard Development Company, L.P.
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

from keystoneauth1 import identity
from keystoneauth1 import loading as ks_loading
from oslo_log import log as logging
from oslo_utils import importutils

import nova.conf
from nova import exception
from nova.i18n import _
from nova.i18n import _LW


LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF

ironic = None

IRONIC_GROUP = nova.conf.ironic.ironic_group

# The API version required by the Ironic driver
IRONIC_API_VERSION = (1, 21)


class IronicClientWrapper(object):
    """Ironic client wrapper class that encapsulates authentication logic."""

    def __init__(self):
        """Initialise the IronicClientWrapper for use.

        Initialise IronicClientWrapper by loading ironicclient
        dynamically so that ironicclient is not a dependency for
        Nova.
        """
        global ironic
        if ironic is None:
            ironic = importutils.import_module('ironicclient')
            # NOTE(deva): work around a lack of symbols in the current version.
            if not hasattr(ironic, 'exc'):
                ironic.exc = importutils.import_module('ironicclient.exc')
            if not hasattr(ironic, 'client'):
                ironic.client = importutils.import_module(
                                                    'ironicclient.client')
        self._cached_client = None

    def _invalidate_cached_client(self):
        """Tell the wrapper to invalidate the cached ironic-client."""
        self._cached_client = None

    def _get_auth_plugin(self):
        """Load an auth plugin from CONF options."""
        # If an auth plugin name is defined in `auth_type` option of [ironic]
        # group, register its options and load it.
        auth_plugin = ks_loading.load_auth_from_conf_options(CONF,
                                                             IRONIC_GROUP.name)

        # If no plugin name is defined, load a v2Password plugin from the
        # deprecated, legacy auth options in [ironic] group.
        if auth_plugin is None:
            LOG.warning(_LW("Couldn't find adequate authentication options "
                            "under the [ironic] group of nova.conf. Falling "
                            "to legacy auth options: admin_username, "
                            "admin_password, admin_tenant_name and admin_url. "
                            "Please note that these options are deprecated "
                            "and won't be supported anymore in a future "
                            "release."))
            legacy_auth = {
                'username': CONF.ironic.admin_username,
                'password': CONF.ironic.admin_password,
                'tenant_name': CONF.ironic.admin_tenant_name,
                'auth_url': CONF.ironic.admin_url
            }
            auth_plugin = identity.V2Password(**legacy_auth)

        return auth_plugin

    def _get_client(self, retry_on_conflict=True):
        max_retries = CONF.ironic.api_max_retries if retry_on_conflict else 1
        retry_interval = (CONF.ironic.api_retry_interval
                          if retry_on_conflict else 0)

        # If we've already constructed a valid, authed client, just return
        # that.
        if retry_on_conflict and self._cached_client is not None:
            return self._cached_client

        auth_plugin = self._get_auth_plugin()

        sess = ks_loading.load_session_from_conf_options(CONF,
                                                         IRONIC_GROUP.name,
                                                         auth=auth_plugin)

        # Retries for Conflict exception
        kwargs = {}
        kwargs['max_retries'] = max_retries
        kwargs['retry_interval'] = retry_interval
        kwargs['os_ironic_api_version'] = '%d.%d' % IRONIC_API_VERSION

        # NOTE(clenimar): by default, the endpoint is taken from the service
        # catalog. Use `api_endpoint` if you want to override it.
        ironic_url = (CONF.ironic.api_endpoint
                      if CONF.ironic.api_endpoint else None)

        try:
            cli = ironic.client.get_client(IRONIC_API_VERSION[0],
                                           ironic_url=ironic_url,
                                           session=sess, **kwargs)
            # Cache the client so we don't have to reconstruct and
            # reauthenticate it every time we need it.
            if retry_on_conflict:
                self._cached_client = cli

        except ironic.exc.Unauthorized:
            msg = _("Unable to authenticate Ironic client.")
            LOG.error(msg)
            raise exception.NovaException(msg)

        return cli

    def _multi_getattr(self, obj, attr):
        """Support nested attribute path for getattr().

        :param obj: Root object.
        :param attr: Path of final attribute to get. E.g., "a.b.c.d"

        :returns: The value of the final named attribute.
        :raises: AttributeError will be raised if the path is invalid.
        """
        for attribute in attr.split("."):
            obj = getattr(obj, attribute)
        return obj

    def call(self, method, *args, **kwargs):
        """Call an Ironic client method and retry on stale token.

        :param method: Name of the client method to call as a string.
        :param args: Client method arguments.
        :param kwargs: Client method keyword arguments.
        :param retry_on_conflict: Boolean value. Whether the request should be
                                  retried in case of a conflict error
                                  (HTTP 409) or not. If retry_on_conflict is
                                  False the cached instance of the client
                                  won't be used. Defaults to True.
        """
        retry_on_conflict = kwargs.pop('retry_on_conflict', True)

        # NOTE(dtantsur): allow for authentication retry, other retries are
        # handled by ironicclient starting with 0.8.0
        for attempt in range(2):
            client = self._get_client(retry_on_conflict=retry_on_conflict)

            try:
                return self._multi_getattr(client, method)(*args, **kwargs)
            except ironic.exc.Unauthorized:
                # In this case, the authorization token of the cached
                # ironic-client probably expired. So invalidate the cached
                # client and the next try will start with a fresh one.
                if not attempt:
                    self._invalidate_cached_client()
                    LOG.debug("The Ironic client became unauthorized. "
                              "Will attempt to reauthorize and try again.")
                else:
                    # This code should be unreachable actually
                    raise
