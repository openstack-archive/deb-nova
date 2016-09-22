# Copyright 2015 OpenStack Foundation
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

# This package got introduced during the Mitaka cycle in 2015 to
# have a central place where the config options of Nova can be maintained.
# For more background see the blueprint "centralize-config-options"

from oslo_config import cfg

from nova.conf import api
from nova.conf import availability_zone
from nova.conf import base
from nova.conf import cache
from nova.conf import cells
from nova.conf import cert
from nova.conf import cinder
from nova.conf import cloudpipe
from nova.conf import compute
from nova.conf import conductor
from nova.conf import configdrive
from nova.conf import console
from nova.conf import consoleauth
from nova.conf import crypto
from nova.conf import database
from nova.conf import ephemeral_storage
from nova.conf import exceptions
from nova.conf import flavors
from nova.conf import floating_ips
from nova.conf import glance
from nova.conf import guestfs
from nova.conf import hyperv
from nova.conf import image_file_url
from nova.conf import ipv6
from nova.conf import ironic
from nova.conf import key_manager
from nova.conf import libvirt
from nova.conf import mks
from nova.conf import netconf
from nova.conf import network
from nova.conf import neutron
from nova.conf import notifications
from nova.conf import novnc
from nova.conf import osapi_v21
from nova.conf import paths
from nova.conf import pci
from nova.conf import placement
from nova.conf import quota
from nova.conf import rdp
from nova.conf import remote_debug
from nova.conf import rpc
from nova.conf import s3
from nova.conf import scheduler
from nova.conf import serial_console
from nova.conf import service
from nova.conf import servicegroup
from nova.conf import spice
from nova.conf import ssl
from nova.conf import upgrade_levels
from nova.conf import virt
from nova.conf import vmware
from nova.conf import vnc
from nova.conf import workarounds
from nova.conf import wsgi
from nova.conf import xenserver
from nova.conf import xvp

CONF = cfg.CONF

api.register_opts(CONF)
availability_zone.register_opts(CONF)
base.register_opts(CONF)
cache.register_opts(CONF)
cells.register_opts(CONF)
cert.register_opts(CONF)
cinder.register_opts(CONF)
cloudpipe.register_opts(CONF)
compute.register_opts(CONF)
conductor.register_opts(CONF)
configdrive.register_opts(CONF)
console.register_opts(CONF)
consoleauth.register_opts(CONF)
crypto.register_opts(CONF)
database.register_opts(CONF)
ephemeral_storage.register_opts(CONF)
exceptions.register_opts(CONF)
floating_ips.register_opts(CONF)
flavors.register_opts(CONF)
glance.register_opts(CONF)
guestfs.register_opts(CONF)
hyperv.register_opts(CONF)
mks.register_opts(CONF)
image_file_url.register_opts(CONF)
ipv6.register_opts(CONF)
ironic.register_opts(CONF)
key_manager.register_opts(CONF)
libvirt.register_opts(CONF)
netconf.register_opts(CONF)
network.register_opts(CONF)
neutron.register_opts(CONF)
notifications.register_opts(CONF)
novnc.register_opts(CONF)
osapi_v21.register_opts(CONF)
paths.register_opts(CONF)
pci.register_opts(CONF)
placement.register_opts(CONF)
quota.register_opts(CONF)
rdp.register_opts(CONF)
rpc.register_opts(CONF)
s3.register_opts(CONF)
scheduler.register_opts(CONF)
serial_console.register_opts(CONF)
service.register_opts(CONF)
servicegroup.register_opts(CONF)
spice.register_opts(CONF)
ssl.register_opts(CONF)
upgrade_levels.register_opts(CONF)
virt.register_opts(CONF)
vmware.register_opts(CONF)
vnc.register_opts(CONF)
workarounds.register_opts(CONF)
wsgi.register_opts(CONF)
xenserver.register_opts(CONF)
xvp.register_opts(CONF)

remote_debug.register_cli_opts(CONF)
