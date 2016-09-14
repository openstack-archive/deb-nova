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
"""Placement API handlers for usage information."""

from oslo_serialization import jsonutils
import webob

from nova.api.openstack.placement import util
from nova import objects


def _serialize_usages(resource_provider, usage):
    usage_dict = {resource.resource_class: resource.usage
                  for resource in usage}
    return {'resource_provider_generation': resource_provider.generation,
            'usages': usage_dict}


@webob.dec.wsgify
@util.check_accept('application/json')
def list_usages(req):
    """GET a dictionary of resource provider usage by resource class.

    If the resource provider does not exist return a 404.

    On success return a 200 with an application/json representation of
    the usage dictionary.
    """
    context = req.environ['placement.context']
    uuid = util.wsgi_path_item(req.environ, 'uuid')

    # Resource provider object needed for two things: If it is
    # NotFound we'll get a 404 here, which needs to happen because
    # get_all_by_resource_provider_uuid can return an empty list.
    # It is also needed for the generation, used in the outgoing
    # representation.
    resource_provider = objects.ResourceProvider.get_by_uuid(
        context, uuid)
    usage = objects.UsageList.get_all_by_resource_provider_uuid(
        context, uuid)

    response = req.response
    response.body = jsonutils.dumps(
        _serialize_usages(resource_provider, usage))
    req.response.content_type = 'application/json'
    return req.response
