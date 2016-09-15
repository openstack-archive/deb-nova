# Copyright 2010-2011 OpenStack Foundation
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

import six


# This is a list of limits which needs to filter out from the API response.
# This is due to the deprecation of network related proxy APIs, the related
# limit should be removed from the API also.
FILTERED_LIMITS = ['floating_ips', 'security_groups', 'security_group_rules']


class ViewBuilder(object):
    """OpenStack API base limits view builder."""

    limit_names = {}

    def __init__(self):
        self.limit_names = {
            "ram": ["maxTotalRAMSize"],
            "instances": ["maxTotalInstances"],
            "cores": ["maxTotalCores"],
            "key_pairs": ["maxTotalKeypairs"],
            "floating_ips": ["maxTotalFloatingIps"],
            "metadata_items": ["maxServerMeta", "maxImageMeta"],
            "injected_files": ["maxPersonality"],
            "injected_file_content_bytes": ["maxPersonalitySize"],
            "security_groups": ["maxSecurityGroups"],
            "security_group_rules": ["maxSecurityGroupRules"],
    }

    def build(self, absolute_limits, filter_result=False):
        absolute_limits = self._build_absolute_limits(
            absolute_limits, filter_result=filter_result)

        output = {
            "limits": {
                "rate": [],
                "absolute": absolute_limits,
            },
        }

        return output

    def _build_absolute_limits(self, absolute_limits, filter_result=False):
        """Builder for absolute limits

        absolute_limits should be given as a dict of limits.
        For example: {"ram": 512, "gigabytes": 1024}.

        """
        filtered_limits = []
        if filter_result:
            filtered_limits = FILTERED_LIMITS
        limits = {}
        for name, value in six.iteritems(absolute_limits):
            if (name in self.limit_names and
                    value is not None and name not in filtered_limits):
                for limit_name in self.limit_names[name]:
                    limits[limit_name] = value
        return limits


class ViewBuilderV21(ViewBuilder):

    def __init__(self):
        super(ViewBuilderV21, self).__init__()
        # NOTE In v2.0 these are added by a specific extension
        self.limit_names["server_groups"] = ["maxServerGroups"]
        self.limit_names["server_group_members"] = ["maxServerGroupMembers"]
