# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
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

from nova.api.openstack.api_version_request \
    import MAX_PROXY_API_SUPPORT_VERSION
from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova import network
from nova.policies import floating_ip_pools as fip_policies


ALIAS = 'os-floating-ip-pools'


def _translate_floating_ip_view(pool_name):
    return {
        'name': pool_name,
    }


def _translate_floating_ip_pools_view(pools):
    return {
        'floating_ip_pools': [_translate_floating_ip_view(pool_name)
                              for pool_name in pools]
    }


class FloatingIPPoolsController(wsgi.Controller):
    """The Floating IP Pool API controller for the OpenStack API."""

    def __init__(self):
        self.network_api = network.API()
        super(FloatingIPPoolsController, self).__init__()

    @wsgi.Controller.api_version("2.1", MAX_PROXY_API_SUPPORT_VERSION)
    @extensions.expected_errors(())
    def index(self, req):
        """Return a list of pools."""
        context = req.environ['nova.context']
        context.can(fip_policies.BASE_POLICY_NAME)
        pools = self.network_api.get_floating_ip_pools(context)
        return _translate_floating_ip_pools_view(pools)


class FloatingIpPools(extensions.V21APIExtensionBase):
    """Floating IPs support."""

    name = "FloatingIpPools"
    alias = ALIAS
    version = 1

    def get_resources(self):
        resource = [extensions.ResourceExtension(ALIAS,
                                                 FloatingIPPoolsController())]
        return resource

    def get_controller_extensions(self):
        """It's an abstract function V21APIExtensionBase and the extension
        will not be loaded without it.
        """
        return []
