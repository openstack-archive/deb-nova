# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

"""Starter script for Nova API.

Starts both the EC2 and OpenStack APIs in separate greenthreads.

"""

import sys

from oslo_log import log as logging
from oslo_reports import guru_meditation_report as gmr
import six

import nova.conf
from nova import config
from nova import exception
from nova.i18n import _LE, _LW
from nova import objects
from nova import service
from nova import utils
from nova import version

CONF = nova.conf.CONF


def main():
    config.parse_args(sys.argv)
    logging.setup(CONF, "nova")
    utils.monkey_patch()
    objects.register_all()
    if 'osapi_compute' in CONF.enabled_apis:
        # NOTE(mriedem): This is needed for caching the nova-compute service
        # version which is looked up when a server create request is made with
        # network id of 'auto' or 'none'.
        objects.Service.enable_min_version_cache()
    log = logging.getLogger(__name__)

    gmr.TextGuruMeditation.setup_autorun(version)

    launcher = service.process_launcher()
    started = 0
    for api in CONF.enabled_apis:
        should_use_ssl = api in CONF.enabled_ssl_apis
        try:
            server = service.WSGIService(api, use_ssl=should_use_ssl)
            launcher.launch_service(server, workers=server.workers or 1)
            started += 1
        except exception.PasteAppNotFound as ex:
            log.warning(
                _LW("%s. ``enabled_apis`` includes bad values. "
                    "Fix to remove this warning."), six.text_type(ex))

    if started == 0:
        log.error(_LE('No APIs were started. '
                      'Check the enabled_apis config option.'))
        sys.exit(1)

    launcher.wait()
