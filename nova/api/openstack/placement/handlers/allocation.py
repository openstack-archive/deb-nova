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
"""Placement API handlers for setting and deleting allocations."""

import collections

import jsonschema
from oslo_log import log as logging
from oslo_serialization import jsonutils
import webob

from nova.api.openstack.placement import util
from nova import exception
from nova.i18n import _, _LE
from nova import objects


LOG = logging.getLogger(__name__)

ALLOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "allocations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_provider": {
                        "type": "object",
                        "properties": {
                            "uuid": {
                                "type": "string",
                                "format": "uuid"
                            }
                        },
                        "additionalProperties": False,
                        "required": ["uuid"]
                    },
                    "resources": {
                        "type": "object",
                        "patternProperties": {
                            "^[0-9A-Z_]+$": {
                                "type": "integer"
                            }
                        },
                        "additionalProperties": False
                    }
                },
                "required": [
                    "resource_provider",
                    "resources"
                ],
                "additionalProperties": False
            }
        }
    },
    "required": ["allocations"],
    "additionalProperties": False
}


def _allocations_dict(allocations, key_fetcher, resource_provider=None):
    """Turn allocations into a dict of resources keyed by key_fetcher."""
    allocation_data = collections.defaultdict(dict)

    for allocation in allocations:
        key = key_fetcher(allocation)
        if 'resources' not in allocation_data[key]:
            allocation_data[key]['resources'] = {}

        resource_class = allocation.resource_class
        allocation_data[key]['resources'][resource_class] = allocation.used

        if not resource_provider:
            generation = allocation.resource_provider.generation
            allocation_data[key]['generation'] = generation

    result = {'allocations': allocation_data}
    if resource_provider:
        result['resource_provider_generation'] = resource_provider.generation
    return result


def _extract_allocations(body, schema):
    """Extract allocation data from a JSON body."""
    try:
        data = jsonutils.loads(body)
    except ValueError as exc:
        raise webob.exc.HTTPBadRequest(
            _('Malformed JSON: %(error)s') % {'error': exc},
            json_formatter=util.json_error_formatter)
    try:
        jsonschema.validate(data, schema,
                            format_checker=jsonschema.FormatChecker())
    except jsonschema.ValidationError as exc:
        raise webob.exc.HTTPBadRequest(
            _('JSON does not validate: %(error)s') % {'error': exc},
            json_formatter=util.json_error_formatter)
    return data


def _serialize_allocations_for_consumer(allocations):
    """Turn a list of allocations into a dict by resource provider uuid.

    {'allocations':
       RP_UUID_1: {
           'generation': GENERATION,
           'resources': {
              'DISK_GB': 4,
              'VCPU': 2
           }
       },
       RP_UUID_2: {
           'generation': GENERATION,
           'resources': {
              'DISK_GB': 6,
              'VCPU': 3
           }
       }
    }
    """
    return _allocations_dict(allocations,
                             lambda x: x.resource_provider.uuid)


def _serialize_allocations_for_resource_provider(allocations,
                                                 resource_provider):
    """Turn a list of allocations into a dict by consumer id.

    {'resource_provider_generation': GENERATION,
     'allocations':
       CONSUMER_ID_1: {
           'resources': {
              'DISK_GB': 4,
              'VCPU': 2
           }
       },
       CONSUMER_ID_2: {
           'resources': {
              'DISK_GB': 6,
              'VCPU': 3
           }
       }
    }
    """
    return _allocations_dict(allocations, lambda x: x.consumer_id,
                             resource_provider=resource_provider)


@webob.dec.wsgify
@util.check_accept('application/json')
def list_for_consumer(req):
    """List allocations associated with a consumer."""
    context = req.environ['placement.context']
    consumer_id = util.wsgi_path_item(req.environ, 'consumer_uuid')

    # NOTE(cdent): There is no way for a 404 to be returned here,
    # only an empty result. We do not have a way to validate a
    # consumer id.
    allocations = objects.AllocationList.get_all_by_consumer_id(
        context, consumer_id)

    allocations_json = jsonutils.dumps(
        _serialize_allocations_for_consumer(allocations))

    req.response.status = 200
    req.response.body = allocations_json
    req.response.content_type = 'application/json'
    return req.response


@webob.dec.wsgify
@util.check_accept('application/json')
def list_for_resource_provider(req):
    """List allocations associated with a resource provider."""
    # TODO(cdent): On a shared resource provider (for example a
    # giant disk farm) this list could get very long. At the moment
    # we have no facility for limiting the output. Given that we are
    # using a dict of dicts for the output we are potentially limiting
    # ourselves in terms of sorting and filtering.
    context = req.environ['placement.context']
    uuid = util.wsgi_path_item(req.environ, 'uuid')

    # confirm existence of resource provider so we get a reasonable
    # 404 instead of empty list
    try:
        resource_provider = objects.ResourceProvider.get_by_uuid(
            context, uuid)
    except exception.NotFound as exc:
        raise webob.exc.HTTPNotFound(
            _("Resource provider '%(rp_uuid)s' not found: %(error)s") %
            {'rp_uuid': uuid, 'error': exc},
            json_formatter=util.json_error_formatter)

    allocations = objects.AllocationList.get_all_by_resource_provider_uuid(
        context, uuid)

    allocations_json = jsonutils.dumps(
        _serialize_allocations_for_resource_provider(
            allocations, resource_provider))

    req.response.status = 200
    req.response.body = allocations_json
    req.response.content_type = 'application/json'
    return req.response


@webob.dec.wsgify
@util.require_content('application/json')
def set_allocations(req):
    context = req.environ['placement.context']
    consumer_uuid = util.wsgi_path_item(req.environ, 'consumer_uuid')
    data = _extract_allocations(req.body, ALLOCATION_SCHEMA)
    allocation_data = data['allocations']

    # If the body includes an allocation for a resource provider
    # that does not exist, raise a 400.
    allocation_objects = []
    for allocation in allocation_data:
        resource_provider_uuid = allocation['resource_provider']['uuid']

        try:
            resource_provider = objects.ResourceProvider.get_by_uuid(
                context, resource_provider_uuid)
        except exception.NotFound:
            raise webob.exc.HTTPBadRequest(
                _("Allocation for resource provider '%(rp_uuid)s' "
                  "that does not exist.") %
                {'rp_uuid': resource_provider_uuid},
                json_formatter=util.json_error_formatter)

        resources = allocation['resources']
        for resource_class in resources:
            try:
                allocation = objects.Allocation(
                    resource_provider=resource_provider,
                    consumer_id=consumer_uuid,
                    resource_class=resource_class,
                    used=resources[resource_class])
            except ValueError as exc:
                raise webob.exc.HTTPBadRequest(
                    _("Allocation of class '%(class)s' for "
                      "resource provider '%(rp_uuid)s' invalid: %(error)s") %
                    {'class': resource_class, 'rp_uuid':
                     resource_provider_uuid, 'error': exc})
            allocation_objects.append(allocation)

    allocations = objects.AllocationList(context, objects=allocation_objects)

    try:
        allocations.create_all()
        LOG.debug("Successfully wrote allocations %s", allocations)
    # InvalidInventory is a parent for several exceptions that
    # indicate either that Inventory is not present, or that
    # capacity limits have been exceeded.
    except exception.InvalidInventory as exc:
        LOG.exception(_LE("Bad inventory"))
        raise webob.exc.HTTPConflict(
            _('Unable to allocate inventory: %(error)s') % {'error': exc},
            json_formatter=util.json_error_formatter)
    except exception.ConcurrentUpdateDetected as exc:
        LOG.exception(_LE("Concurrent Update"))
        raise webob.exc.HTTPConflict(
            _('Inventory changed while attempting to allocate: %(error)s') %
            {'error': exc},
            json_formatter=util.json_error_formatter)

    req.response.status = 204
    req.response.content_type = None
    return req.response


@webob.dec.wsgify
def delete_allocations(req):
    context = req.environ['placement.context']
    consumer_uuid = util.wsgi_path_item(req.environ, 'consumer_uuid')

    allocations = objects.AllocationList.get_all_by_consumer_id(
        context, consumer_uuid)
    if not allocations:
        raise webob.exc.HTTPNotFound(
            _("No allocations for consumer '%(consumer_uuid)s'") %
            {'consumer_uuid': consumer_uuid},
            json_formatter=util.json_error_formatter)
    allocations.delete_all()
    LOG.debug("Successfully deleted allocations %s", allocations)

    req.response.status = 204
    req.response.content_type = None
    return req.response
