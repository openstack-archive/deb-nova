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

"""Starter script for Nova Network."""

import sys

from oslo_log import log as logging
from oslo_reports import guru_meditation_report as gmr

from nova.cmd import common as cmd_common
from nova.conductor import rpcapi as conductor_rpcapi
import nova.conf
from nova import config
from nova.i18n import _LW
from nova import objects
from nova.objects import base as objects_base
from nova import service
from nova import utils
from nova import version

CONF = nova.conf.CONF
LOG = logging.getLogger('nova.network')


def main():
    config.parse_args(sys.argv)
    logging.setup(CONF, "nova")
    utils.monkey_patch()
    objects.register_all()

    gmr.TextGuruMeditation.setup_autorun(version)

    if not CONF.conductor.use_local:
        cmd_common.block_db_access('nova-network')
        objects_base.NovaObject.indirection_api = \
            conductor_rpcapi.ConductorAPI()
    else:
        LOG.warning(_LW('Conductor local mode is deprecated and will '
                        'be removed in a subsequent release'))

    LOG.warning(_LW('Nova network is deprecated and will be removed '
                    'in the future'))
    server = service.Service.create(binary='nova-network',
                                    topic=CONF.network_topic,
                                    db_allowed=CONF.conductor.use_local)
    service.serve(server)
    service.wait()
