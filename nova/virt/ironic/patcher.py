# coding=utf-8
#
# Copyright 2014 Hewlett-Packard Development Company, L.P.
# Copyright 2014 Red Hat, Inc.
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
#
"""
Helper classes for Ironic HTTP PATCH creation.
"""

from oslo_serialization import jsonutils
import six

import nova.conf

CONF = nova.conf.CONF


def create(node):
    """Create an instance of the appropriate DriverFields class.

    :param node: a node object returned from ironicclient
    :returns: A GenericDriverFields instance.
    """
    return GenericDriverFields(node)


class GenericDriverFields(object):

    def __init__(self, node):
        self.node = node

    def get_deploy_patch(self, instance, image_meta, flavor,
                         preserve_ephemeral=None):
        """Build a patch to add the required fields to deploy a node.

        :param instance: the instance object.
        :param image_meta: the nova.objects.ImageMeta object instance
        :param flavor: the flavor object.
        :param preserve_ephemeral: preserve_ephemeral status (bool) to be
                                   specified during rebuild.
        :returns: a json-patch with the fields that needs to be updated.

        """
        patch = []
        patch.append({'path': '/instance_info/image_source', 'op': 'add',
                      'value': image_meta.id})
        patch.append({'path': '/instance_info/root_gb', 'op': 'add',
                      'value': str(instance.flavor.root_gb)})
        patch.append({'path': '/instance_info/swap_mb', 'op': 'add',
                      'value': str(flavor['swap'])})
        patch.append({'path': '/instance_info/display_name',
                      'op': 'add', 'value': instance.display_name})
        patch.append({'path': '/instance_info/vcpus', 'op': 'add',
                      'value': str(instance.flavor.vcpus)})
        patch.append({'path': '/instance_info/memory_mb', 'op': 'add',
                      'value': str(instance.flavor.memory_mb)})
        patch.append({'path': '/instance_info/local_gb', 'op': 'add',
                      'value': str(self.node.properties.get('local_gb', 0))})

        if instance.flavor.ephemeral_gb:
            patch.append({'path': '/instance_info/ephemeral_gb',
                          'op': 'add',
                          'value': str(instance.flavor.ephemeral_gb)})
            if CONF.default_ephemeral_format:
                patch.append({'path': '/instance_info/ephemeral_format',
                              'op': 'add',
                              'value': CONF.default_ephemeral_format})

        if preserve_ephemeral is not None:
            patch.append({'path': '/instance_info/preserve_ephemeral',
                          'op': 'add', 'value': str(preserve_ephemeral)})

        capabilities = {}

        # read the flavor and get the extra_specs value.
        extra_specs = flavor.get('extra_specs')

        # scan through the extra_specs values and ignore the keys
        # not starting with keyword 'capabilities'.

        for key, val in six.iteritems(extra_specs):
            if not key.startswith('capabilities:'):
                continue

            # split the extra_spec key to remove the keyword
            # 'capabilities' and get the actual key.

            capabilities_string, capabilities_key = key.split(':', 1)
            if capabilities_key:
                capabilities[capabilities_key] = val

        if capabilities:
            patch.append({'path': '/instance_info/capabilities',
                          'op': 'add', 'value': jsonutils.dumps(capabilities)})
        return patch
