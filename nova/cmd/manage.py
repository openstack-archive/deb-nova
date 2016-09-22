# Copyright (c) 2011 X.commerce, a business unit of eBay Inc.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
# Copyright 2013 Red Hat, Inc.
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

# Interactive shell based on Django:
#
# Copyright (c) 2005, the Lawrence Journal-World
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     1. Redistributions of source code must retain the above copyright notice,
#        this list of conditions and the following disclaimer.
#
#     2. Redistributions in binary form must reproduce the above copyright
#        notice, this list of conditions and the following disclaimer in the
#        documentation and/or other materials provided with the distribution.
#
#     3. Neither the name of Django nor the names of its contributors may be
#        used to endorse or promote products derived from this software without
#        specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""
  CLI interface for nova management.
"""

from __future__ import print_function

import functools
import os
import sys

import decorator
import netaddr
from oslo_config import cfg
from oslo_db import exception as db_exc
from oslo_log import log as logging
import oslo_messaging as messaging
from oslo_utils import importutils
from oslo_utils import uuidutils
import six
import six.moves.urllib.parse as urlparse

from nova.api.ec2 import ec2utils
from nova import availability_zones
from nova.cmd import common as cmd_common
import nova.conf
from nova import config
from nova import context
from nova import db
from nova.db import migration
from nova import exception
from nova.i18n import _
from nova import objects
from nova.objects import aggregate as aggregate_obj
from nova.objects import flavor as flavor_obj
from nova.objects import instance as instance_obj
from nova.objects import instance_group as instance_group_obj
from nova.objects import keypair as keypair_obj
from nova.objects import request_spec
from nova import quota
from nova import rpc
from nova import utils
from nova import version

CONF = nova.conf.CONF

QUOTAS = quota.QUOTAS

_EXTRA_DEFAULT_LOG_LEVELS = ['oslo_db=INFO']


# Decorators for actions
args = cmd_common.args


def param2id(object_id):
    """Helper function to convert various volume id types to internal id.
    args: [object_id], e.g. 'vol-0000000a' or 'volume-0000000a' or '10'
    """
    if '-' in object_id:
        return ec2utils.ec2_vol_id_to_uuid(object_id)
    else:
        return object_id


class VpnCommands(object):
    """Class for managing VPNs."""

    description = ('DEPRECATED: VPN commands are deprecated since '
                   'nova-network is deprecated in favor of Neutron. The '
                   'VPN commands will be removed in the Nova 15.0.0 '
                   'Ocata release.')

    @args('--project', dest='project_id', metavar='<Project name>',
            help='Project name')
    @args('--ip', metavar='<IP Address>', help='IP Address')
    @args('--port', metavar='<Port>', help='Port')
    def change(self, project_id, ip, port):
        """Change the IP and port for a VPN.

        This will update all networks associated with a project
        not sure if that's the desired behavior or not, patches accepted.

        """
        # TODO(tr3buchet): perhaps this shouldn't update all networks
        # associated with a project in the future
        admin_context = context.get_admin_context()
        networks = db.project_get_networks(admin_context, project_id)
        for network in networks:
            db.network_update(admin_context,
                              network['id'],
                              {'vpn_public_address': ip,
                               'vpn_public_port': int(port)})


class ShellCommands(object):
    def bpython(self):
        """Runs a bpython shell.

        Falls back to Ipython/python shell if unavailable
        """
        self.run('bpython')

    def ipython(self):
        """Runs an Ipython shell.

        Falls back to Python shell if unavailable
        """
        self.run('ipython')

    def python(self):
        """Runs a python shell.

        Falls back to Python shell if unavailable
        """
        self.run('python')

    @args('--shell', metavar='<bpython|ipython|python >',
            help='Python shell')
    def run(self, shell=None):
        """Runs a Python interactive interpreter."""
        if not shell:
            shell = 'bpython'

        if shell == 'bpython':
            try:
                import bpython
                bpython.embed()
            except ImportError:
                shell = 'ipython'
        if shell == 'ipython':
            try:
                from IPython import embed
                embed()
            except ImportError:
                try:
                    # Ipython < 0.11
                    # Explicitly pass an empty list as arguments, because
                    # otherwise IPython would use sys.argv from this script.
                    import IPython

                    shell = IPython.Shell.IPShell(argv=[])
                    shell.mainloop()
                except ImportError:
                    # no IPython module
                    shell = 'python'

        if shell == 'python':
            import code
            try:
                # Try activating rlcompleter, because it's handy.
                import readline
            except ImportError:
                pass
            else:
                # We don't have to wrap the following import in a 'try',
                # because we already know 'readline' was imported successfully.
                readline.parse_and_bind("tab:complete")
            code.interact()

    @args('--path', metavar='<path>', help='Script path')
    def script(self, path):
        """Runs the script from the specified path with flags set properly.

        arguments: path
        """
        exec(compile(open(path).read(), path, 'exec'), locals(), globals())


def _db_error(caught_exception):
    print(caught_exception)
    print(_("The above error may show that the database has not "
            "been created.\nPlease create a database using "
            "'nova-manage db sync' before running this command."))
    sys.exit(1)


class ProjectCommands(object):
    """Class for managing projects."""

    @args('--project', dest='project_id', metavar='<Project name>',
            help='Project name')
    @args('--user', dest='user_id', metavar='<User name>',
            help='User name')
    @args('--key', metavar='<key>', help='Key')
    @args('--value', metavar='<value>', help='Value')
    def quota(self, project_id, user_id=None, key=None, value=None):
        """Create, update or display quotas for project/user

        If no quota key is provided, the quota will be displayed.
        If a valid quota key is provided and it does not exist,
        it will be created. Otherwise, it will be updated.
        """

        ctxt = context.get_admin_context()
        if user_id:
            quota = QUOTAS.get_user_quotas(ctxt, project_id, user_id)
        else:
            user_id = None
            quota = QUOTAS.get_project_quotas(ctxt, project_id)
        # if key is None, that means we need to show the quotas instead
        # of updating them
        if key:
            settable_quotas = QUOTAS.get_settable_quotas(ctxt,
                                                         project_id,
                                                         user_id=user_id)
            if key in quota:
                minimum = settable_quotas[key]['minimum']
                maximum = settable_quotas[key]['maximum']
                if value.lower() == 'unlimited':
                    value = -1
                if int(value) < -1:
                    print(_('Quota limit must be -1 or greater.'))
                    return(2)
                if ((int(value) < minimum) and
                   (maximum != -1 or (maximum == -1 and int(value) != -1))):
                    print(_('Quota limit must be greater than %s.') % minimum)
                    return(2)
                if maximum != -1 and int(value) > maximum:
                    print(_('Quota limit must be less than %s.') % maximum)
                    return(2)
                try:
                    db.quota_create(ctxt, project_id, key, value,
                                    user_id=user_id)
                except exception.QuotaExists:
                    db.quota_update(ctxt, project_id, key, value,
                                    user_id=user_id)
            else:
                print(_('%(key)s is not a valid quota key. Valid options are: '
                        '%(options)s.') % {'key': key,
                                           'options': ', '.join(quota)})
                return(2)
        print_format = "%-36s %-10s %-10s %-10s"
        print(print_format % (
                    _('Quota'),
                    _('Limit'),
                    _('In Use'),
                    _('Reserved')))
        # Retrieve the quota after update
        if user_id:
            quota = QUOTAS.get_user_quotas(ctxt, project_id, user_id)
        else:
            quota = QUOTAS.get_project_quotas(ctxt, project_id)
        for key, value in six.iteritems(quota):
            if value['limit'] < 0 or value['limit'] is None:
                value['limit'] = 'unlimited'
            print(print_format % (key, value['limit'], value['in_use'],
                                  value['reserved']))

    @args('--project', dest='project_id', metavar='<Project Id>',
            help='Project Id', required=True)
    @args('--user', dest='user_id', metavar='<User Id>',
            help='User Id')
    @args('--key', metavar='<key>', help='Key')
    def quota_usage_refresh(self, project_id, user_id=None, key=None):
        """Refresh the quotas for project/user

        If no quota key is provided, all the quota usages will be refreshed.
        If a valid quota key is provided and it does not exist,
        it will be created. Otherwise, it will be refreshed.
        """
        ctxt = context.get_admin_context()

        keys = None
        if key:
            keys = [key]

        try:
            QUOTAS.usage_refresh(ctxt, project_id, user_id, keys)
        except exception.QuotaUsageRefreshNotAllowed as e:
            print(e.format_message())
            return 2

    @args('--project', dest='project_id', metavar='<Project name>',
            help='Project name')
    def scrub(self, project_id):
        """DEPRECATED: Deletes network data associated with project.

        This command is only for nova-network deployments and nova-network is
        deprecated in favor of Neutron. This command will be removed in the
        Nova 15.0.0 Ocata release.
        """
        admin_context = context.get_admin_context()
        networks = db.project_get_networks(admin_context, project_id)
        for network in networks:
            db.network_disassociate(admin_context, network['id'])
        groups = db.security_group_get_by_project(admin_context, project_id)
        for group in groups:
            db.security_group_destroy(admin_context, group['id'])


AccountCommands = ProjectCommands


class FixedIpCommands(object):
    """Class for managing fixed IP."""

    description = ('DEPRECATED: Fixed IP commands are deprecated since '
                   'nova-network is deprecated in favor of Neutron. The '
                   'fixed IP commands will be removed in the Nova 15.0.0 '
                   'Ocata release.')

    @args('--host', metavar='<host>', help='Host')
    def list(self, host=None):
        """Lists all fixed IPs (optionally by host)."""
        ctxt = context.get_admin_context()

        try:
            if host is None:
                fixed_ips = db.fixed_ip_get_all(ctxt)
            else:
                fixed_ips = db.fixed_ip_get_by_host(ctxt, host)

        except exception.NotFound as ex:
            print(_("error: %s") % ex)
            return(2)

        instances = db.instance_get_all(context.get_admin_context())
        instances_by_uuid = {}
        for instance in instances:
            instances_by_uuid[instance['uuid']] = instance

        print("%-18s\t%-15s\t%-15s\t%s" % (_('network'),
                                              _('IP address'),
                                              _('hostname'),
                                              _('host')))

        all_networks = {}
        try:
            # use network_get_all to retrieve all existing networks
            # this is to ensure that IPs associated with deleted networks
            # will not throw exceptions.
            for network in db.network_get_all(context.get_admin_context()):
                all_networks[network.id] = network
        except exception.NoNetworksFound:
            # do not have any networks, so even if there are IPs, these
            # IPs should have been deleted ones, so return.
            print(_('No fixed IP found.'))
            return

        has_ip = False
        for fixed_ip in fixed_ips:
            hostname = None
            host = None
            network = all_networks.get(fixed_ip['network_id'])
            if network:
                has_ip = True
                if fixed_ip.get('instance_uuid'):
                    instance = instances_by_uuid.get(fixed_ip['instance_uuid'])
                    if instance:
                        hostname = instance['hostname']
                        host = instance['host']
                    else:
                        print(_('WARNING: fixed IP %s allocated to missing'
                                ' instance') % str(fixed_ip['address']))
                print("%-18s\t%-15s\t%-15s\t%s" % (
                        network['cidr'],
                        fixed_ip['address'],
                        hostname, host))

        if not has_ip:
            print(_('No fixed IP found.'))

    @args('--address', metavar='<ip address>', help='IP address')
    def reserve(self, address):
        """Mark fixed IP as reserved

        arguments: address
        """
        return self._set_reserved(address, True)

    @args('--address', metavar='<ip address>', help='IP address')
    def unreserve(self, address):
        """Mark fixed IP as free to use

        arguments: address
        """
        return self._set_reserved(address, False)

    def _set_reserved(self, address, reserved):
        ctxt = context.get_admin_context()

        try:
            fixed_ip = db.fixed_ip_get_by_address(ctxt, address)
            if fixed_ip is None:
                raise exception.NotFound('Could not find address')
            db.fixed_ip_update(ctxt, fixed_ip['address'],
                                {'reserved': reserved})
        except exception.NotFound as ex:
            print(_("error: %s") % ex)
            return(2)


class FloatingIpCommands(object):
    """Class for managing floating IP."""

    description = ('DEPRECATED: Floating IP commands are deprecated since '
                   'nova-network is deprecated in favor of Neutron. The '
                   'floating IP commands will be removed in the Nova 15.0.0 '
                   'Ocata release.')

    @staticmethod
    def address_to_hosts(addresses):
        """Iterate over hosts within an address range.

        If an explicit range specifier is missing, the parameter is
        interpreted as a specific individual address.
        """
        try:
            return [netaddr.IPAddress(addresses)]
        except ValueError:
            net = netaddr.IPNetwork(addresses)
            if net.size < 4:
                reason = _("/%s should be specified as single address(es) "
                           "not in cidr format") % net.prefixlen
                raise exception.InvalidInput(reason=reason)
            elif net.size >= 1000000:
                # NOTE(dripton): If we generate a million IPs and put them in
                # the database, the system will slow to a crawl and/or run
                # out of memory and crash.  This is clearly a misconfiguration.
                reason = _("Too many IP addresses will be generated.  Please "
                           "increase /%s to reduce the number generated."
                          ) % net.prefixlen
                raise exception.InvalidInput(reason=reason)
            else:
                return net.iter_hosts()

    @args('--ip_range', metavar='<range>', help='IP range')
    @args('--pool', metavar='<pool>', help='Optional pool')
    @args('--interface', metavar='<interface>', help='Optional interface')
    def create(self, ip_range, pool=None, interface=None):
        """Creates floating IPs for zone by range."""
        admin_context = context.get_admin_context()
        if not pool:
            pool = CONF.default_floating_pool
        if not interface:
            interface = CONF.public_interface

        ips = [{'address': str(address), 'pool': pool, 'interface': interface}
               for address in self.address_to_hosts(ip_range)]
        try:
            db.floating_ip_bulk_create(admin_context, ips, want_result=False)
        except exception.FloatingIpExists as exc:
            # NOTE(simplylizz): Maybe logging would be better here
            # instead of printing, but logging isn't used here and I
            # don't know why.
            print('error: %s' % exc)
            return(1)

    @args('--ip_range', metavar='<range>', help='IP range')
    def delete(self, ip_range):
        """Deletes floating IPs by range."""
        admin_context = context.get_admin_context()

        ips = ({'address': str(address)}
               for address in self.address_to_hosts(ip_range))
        db.floating_ip_bulk_destroy(admin_context, ips)

    @args('--host', metavar='<host>', help='Host')
    def list(self, host=None):
        """Lists all floating IPs (optionally by host).

        Note: if host is given, only active floating IPs are returned
        """
        ctxt = context.get_admin_context()
        try:
            if host is None:
                floating_ips = db.floating_ip_get_all(ctxt)
            else:
                floating_ips = db.floating_ip_get_all_by_host(ctxt, host)
        except exception.NoFloatingIpsDefined:
            print(_("No floating IP addresses have been defined."))
            return
        for floating_ip in floating_ips:
            instance_uuid = None
            if floating_ip['fixed_ip_id']:
                fixed_ip = db.fixed_ip_get(ctxt, floating_ip['fixed_ip_id'])
                instance_uuid = fixed_ip['instance_uuid']

            print("%s\t%s\t%s\t%s\t%s" % (floating_ip['project_id'],
                                          floating_ip['address'],
                                          instance_uuid,
                                          floating_ip['pool'],
                                          floating_ip['interface']))


@decorator.decorator
def validate_network_plugin(f, *args, **kwargs):
    """Decorator to validate the network plugin."""
    if utils.is_neutron():
        print(_("ERROR: Network commands are not supported when using the "
                "Neutron API.  Use python-neutronclient instead."))
        return(2)
    return f(*args, **kwargs)


class NetworkCommands(object):
    """Class for managing networks."""

    description = ('DEPRECATED: Network commands are deprecated since '
                   'nova-network is deprecated in favor of Neutron. The '
                   'network commands will be removed in the Nova 15.0.0 Ocata '
                   'release.')

    @validate_network_plugin
    @args('--label', metavar='<label>', help='Label for network (ex: public)')
    @args('--fixed_range_v4', dest='cidr', metavar='<x.x.x.x/yy>',
            help='IPv4 subnet (ex: 10.0.0.0/8)')
    @args('--num_networks', metavar='<number>',
            help='Number of networks to create')
    @args('--network_size', metavar='<number>',
            help='Number of IPs per network')
    @args('--vlan', metavar='<vlan id>', help='vlan id')
    @args('--vlan_start', dest='vlan_start', metavar='<vlan start id>',
          help='vlan start id')
    @args('--vpn', dest='vpn_start', help='vpn start')
    @args('--fixed_range_v6', dest='cidr_v6',
          help='IPv6 subnet (ex: fe80::/64')
    @args('--gateway', help='gateway')
    @args('--gateway_v6', help='ipv6 gateway')
    @args('--bridge', metavar='<bridge>',
            help='VIFs on this network are connected to this bridge')
    @args('--bridge_interface', metavar='<bridge interface>',
            help='the bridge is connected to this interface')
    @args('--multi_host', metavar="<'T'|'F'>",
            help='Multi host')
    @args('--dns1', metavar="<DNS Address>", help='First DNS')
    @args('--dns2', metavar="<DNS Address>", help='Second DNS')
    @args('--uuid', metavar="<network uuid>", help='Network UUID')
    @args('--fixed_cidr', metavar='<x.x.x.x/yy>',
            help='IPv4 subnet for fixed IPs (ex: 10.20.0.0/16)')
    @args('--project_id', metavar="<project id>",
          help='Project id')
    @args('--priority', metavar="<number>", help='Network interface priority')
    def create(self, label=None, cidr=None, num_networks=None,
               network_size=None, multi_host=None, vlan=None,
               vlan_start=None, vpn_start=None, cidr_v6=None, gateway=None,
               gateway_v6=None, bridge=None, bridge_interface=None,
               dns1=None, dns2=None, project_id=None, priority=None,
               uuid=None, fixed_cidr=None):
        """Creates fixed IPs for host by range."""

        # NOTE(gmann): These checks are moved here as API layer does all these
        # validation through JSON schema.
        if not label:
            raise exception.NetworkNotCreated(req="label")
        if len(label) > 255:
            raise exception.LabelTooLong()
        if not (cidr or cidr_v6):
            raise exception.NetworkNotCreated(req="cidr or cidr_v6")

        kwargs = {k: v for k, v in six.iteritems(locals())
                  if v and k != "self"}
        if multi_host is not None:
            kwargs['multi_host'] = multi_host == 'T'
        net_manager = importutils.import_object(CONF.network_manager)
        net_manager.create_networks(context.get_admin_context(), **kwargs)

    @validate_network_plugin
    def list(self):
        """List all created networks."""
        _fmt = "%-5s\t%-18s\t%-15s\t%-15s\t%-15s\t%-15s\t%-15s\t%-15s\t%-15s"
        print(_fmt % (_('id'),
                          _('IPv4'),
                          _('IPv6'),
                          _('start address'),
                          _('DNS1'),
                          _('DNS2'),
                          _('VlanID'),
                          _('project'),
                          _("uuid")))
        try:
            # Since network_get_all can throw exception.NoNetworksFound
            # for this command to show a nice result, this exception
            # should be caught and handled as such.
            networks = db.network_get_all(context.get_admin_context())
        except exception.NoNetworksFound:
            print(_('No networks found'))
        else:
            for network in networks:
                print(_fmt % (network.id,
                              network.cidr,
                              network.cidr_v6,
                              network.dhcp_start,
                              network.dns1,
                              network.dns2,
                              network.vlan,
                              network.project_id,
                              network.uuid))

    @validate_network_plugin
    @args('--fixed_range', metavar='<x.x.x.x/yy>', help='Network to delete')
    @args('--uuid', metavar='<uuid>', help='UUID of network to delete')
    def delete(self, fixed_range=None, uuid=None):
        """Deletes a network."""
        if fixed_range is None and uuid is None:
            raise Exception(_("Please specify either fixed_range or uuid"))

        net_manager = importutils.import_object(CONF.network_manager)
        if "NeutronManager" in CONF.network_manager:
            if uuid is None:
                raise Exception(_("UUID is required to delete "
                                  "Neutron Networks"))
            if fixed_range:
                raise Exception(_("Deleting by fixed_range is not supported "
                                "with the NeutronManager"))
        # delete the network
        net_manager.delete_network(context.get_admin_context(),
            fixed_range, uuid)

    @validate_network_plugin
    @args('--fixed_range', metavar='<x.x.x.x/yy>', help='Network to modify')
    @args('--project', metavar='<project name>',
            help='Project name to associate')
    @args('--host', metavar='<host>', help='Host to associate')
    @args('--disassociate-project', action="store_true", dest='dis_project',
          default=False, help='Disassociate Network from Project')
    @args('--disassociate-host', action="store_true", dest='dis_host',
          default=False, help='Disassociate Host from Project')
    def modify(self, fixed_range, project=None, host=None,
               dis_project=None, dis_host=None):
        """Associate/Disassociate Network with Project and/or Host
        arguments: network project host
        leave any field blank to ignore it
        """
        admin_context = context.get_admin_context()
        network = db.network_get_by_cidr(admin_context, fixed_range)
        net = {}
        # User can choose the following actions each for project and host.
        # 1) Associate (set not None value given by project/host parameter)
        # 2) Disassociate (set None by disassociate parameter)
        # 3) Keep unchanged (project/host key is not added to 'net')
        if dis_project:
            net['project_id'] = None
        if dis_host:
            net['host'] = None

        # The --disassociate-X are boolean options, but if they user
        # mistakenly provides a value, it will be used as a positional argument
        # and be erroneously interpreted as some other parameter (e.g.
        # a project instead of host value). The safest thing to do is error-out
        # with a message indicating that there is probably a problem with
        # how the disassociate modifications are being used.
        if dis_project or dis_host:
            if project or host:
                error_msg = "ERROR: Unexpected arguments provided. Please " \
                    "use separate commands."
                print(error_msg)
                return(1)
            db.network_update(admin_context, network['id'], net)
            return

        if project:
            net['project_id'] = project
        if host:
            net['host'] = host

        db.network_update(admin_context, network['id'], net)


class VmCommands(object):
    """Class for managing VM instances."""

    description = ('DEPRECATED: Use the nova list command from '
                   'python-novaclient instead. The vm subcommand will be '
                   'removed in the 15.0.0 Ocata release.')

    @args('--host', metavar='<host>', help='Host')
    def list(self, host=None):
        """DEPRECATED: Show a list of all instances."""

        print(("%-10s %-15s %-10s %-10s %-26s %-9s %-9s %-9s"
               "  %-10s %-10s %-10s %-5s" % (_('instance'),
                                             _('node'),
                                             _('type'),
                                             _('state'),
                                             _('launched'),
                                             _('image'),
                                             _('kernel'),
                                             _('ramdisk'),
                                             _('project'),
                                             _('user'),
                                             _('zone'),
                                             _('index'))))

        if host is None:
            instances = objects.InstanceList.get_by_filters(
                context.get_admin_context(), {}, expected_attrs=['flavor'])
        else:
            instances = objects.InstanceList.get_by_host(
                context.get_admin_context(), host, expected_attrs=['flavor'])

        for instance in instances:
            instance_type = instance.get_flavor()
            print(("%-10s %-15s %-10s %-10s %-26s %-9s %-9s %-9s"
                   " %-10s %-10s %-10s %-5d" % (instance.display_name,
                                                instance.host,
                                                instance_type.name,
                                                instance.vm_state,
                                                instance.launched_at,
                                                instance.image_ref,
                                                instance.kernel_id,
                                                instance.ramdisk_id,
                                                instance.project_id,
                                                instance.user_id,
                                                instance.availability_zone,
                                                instance.launch_index or 0)))


class HostCommands(object):
    """List hosts."""

    def list(self, zone=None):
        """Show a list of all physical hosts. Filter by zone.
        args: [zone]
        """
        print("%-25s\t%-15s" % (_('host'),
                                _('zone')))
        ctxt = context.get_admin_context()
        services = db.service_get_all(ctxt)
        services = availability_zones.set_availability_zones(ctxt, services)
        if zone:
            services = [s for s in services if s['availability_zone'] == zone]
        hosts = []
        for srv in services:
            if not [h for h in hosts if h['host'] == srv['host']]:
                hosts.append(srv)

        for h in hosts:
            print("%-25s\t%-15s" % (h['host'], h['availability_zone']))


class DbCommands(object):
    """Class for managing the main database."""

    online_migrations = (
        db.pcidevice_online_data_migration,
        db.aggregate_uuids_online_data_migration,
        flavor_obj.migrate_flavors,
        flavor_obj.migrate_flavor_reset_autoincrement,
        instance_obj.migrate_instance_keypairs,
        request_spec.migrate_instances_add_request_spec,
        keypair_obj.migrate_keypairs_to_api_db,
        aggregate_obj.migrate_aggregates,
        aggregate_obj.migrate_aggregate_reset_autoincrement,
        instance_group_obj.migrate_instance_groups_to_api_db,
    )

    def __init__(self):
        pass

    @args('--version', metavar='<version>', help='Database version')
    @args('--local_cell', action='store_true',
          help='Only sync db in the local cell: do not attempt to fan-out'
               'to all cells')
    def sync(self, version=None, local_cell=False):
        """Sync the database up to the most recent version."""
        if not local_cell:
            ctxt = context.RequestContext()
            # NOTE(mdoff): Multiple cells not yet implemented. Currently
            # fanout only looks for cell0.
            try:
                cell_mapping = objects.CellMapping.get_by_uuid(ctxt,
                                            objects.CellMapping.CELL0_UUID)
                with context.target_cell(ctxt, cell_mapping):
                    migration.db_sync(version, context=ctxt)
            except exception.CellMappingNotFound:
                print(_('WARNING: cell0 mapping not found - not'
                        ' syncing cell0.'))
            except Exception:
                print(_('ERROR: could not access cell mapping database - has'
                        ' api db been created?'))
        return migration.db_sync(version)

    def version(self):
        """Print the current database version."""
        print(migration.db_version())

    @args('--max_rows', metavar='<number>',
            help='Maximum number of deleted rows to archive')
    @args('--verbose', action='store_true', dest='verbose', default=False,
          help='Print how many rows were archived per table.')
    def archive_deleted_rows(self, max_rows, verbose=False):
        """Move up to max_rows deleted rows from production tables to shadow
        tables.
        """
        if max_rows is not None:
            max_rows = int(max_rows)
            if max_rows < 0:
                print(_("Must supply a positive value for max_rows"))
                return(1)
            if max_rows > db.MAX_INT:
                print(_('max rows must be <= %(max_value)d') %
                      {'max_value': db.MAX_INT})
                return(1)
        table_to_rows_archived = db.archive_deleted_rows(max_rows)
        if verbose:
            if table_to_rows_archived:
                utils.print_dict(table_to_rows_archived, _('Table'),
                                 dict_value=_('Number of Rows Archived'))
            else:
                print(_('Nothing was archived.'))

    @args('--delete', action='store_true', dest='delete',
          help='If specified, automatically delete any records found where '
               'instance_uuid is NULL.')
    def null_instance_uuid_scan(self, delete=False):
        """Lists and optionally deletes database records where
        instance_uuid is NULL.
        """
        hits = migration.db_null_instance_uuid_scan(delete)
        records_found = False
        for table_name, records in six.iteritems(hits):
            # Don't print anything for 0 hits
            if records:
                records_found = True
                if delete:
                    print(_("Deleted %(records)d records "
                            "from table '%(table_name)s'.") %
                          {'records': records, 'table_name': table_name})
                else:
                    print(_("There are %(records)d records in the "
                            "'%(table_name)s' table where the uuid or "
                            "instance_uuid column is NULL. Run this "
                            "command again with the --delete option after you "
                            "have backed up any necessary data.") %
                          {'records': records, 'table_name': table_name})
        # check to see if we didn't find anything
        if not records_found:
            print(_('There were no records found where '
                    'instance_uuid was NULL.'))

    def _run_migration(self, ctxt, max_count):
        ran = 0
        for migration_meth in self.online_migrations:
            count = max_count - ran
            try:
                found, done = migration_meth(ctxt, count)
            except Exception:
                print(_("Error attempting to run %(method)s") % dict(
                      method=migration_meth))
                found = done = 0

            if found:
                print(_('%(total)i rows matched query %(meth)s, %(done)i '
                        'migrated') % {'total': found,
                                       'meth': migration_meth.__name__,
                                       'done': done})
            if max_count is not None:
                ran += done
                if ran >= max_count:
                    break
        return ran

    @args('--max-count', metavar='<number>', dest='max_count',
          help='Maximum number of objects to consider')
    def online_data_migrations(self, max_count=None):
        ctxt = context.get_admin_context()
        if max_count is not None:
            try:
                max_count = int(max_count)
            except ValueError:
                max_count = -1
            unlimited = False
            if max_count < 1:
                print(_('Must supply a positive value for max_number'))
                return 127
        else:
            unlimited = True
            max_count = 50
            print(_('Running batches of %i until complete') % max_count)

        ran = None
        while ran is None or ran != 0:
            ran = self._run_migration(ctxt, max_count)
            if not unlimited:
                break

        return ran and 1 or 0


class ApiDbCommands(object):
    """Class for managing the api database."""

    def __init__(self):
        pass

    @args('--version', metavar='<version>', help='Database version')
    def sync(self, version=None):
        """Sync the database up to the most recent version."""
        return migration.db_sync(version, database='api')

    def version(self):
        """Print the current database version."""
        print(migration.db_version(database='api'))


class AgentBuildCommands(object):
    """Class for managing agent builds."""

    @args('--os', metavar='<os>', help='os')
    @args('--architecture', dest='architecture',
            metavar='<architecture>', help='architecture')
    @args('--version', metavar='<version>', help='version')
    @args('--url', metavar='<url>', help='url')
    @args('--md5hash', metavar='<md5hash>', help='md5hash')
    @args('--hypervisor', metavar='<hypervisor>',
            help='hypervisor(default: xen)')
    def create(self, os, architecture, version, url, md5hash,
                hypervisor='xen'):
        """Creates a new agent build."""
        ctxt = context.get_admin_context()
        db.agent_build_create(ctxt, {'hypervisor': hypervisor,
                                     'os': os,
                                     'architecture': architecture,
                                     'version': version,
                                     'url': url,
                                     'md5hash': md5hash})

    @args('--os', metavar='<os>', help='os')
    @args('--architecture', dest='architecture',
            metavar='<architecture>', help='architecture')
    @args('--hypervisor', metavar='<hypervisor>',
            help='hypervisor(default: xen)')
    def delete(self, os, architecture, hypervisor='xen'):
        """Deletes an existing agent build."""
        ctxt = context.get_admin_context()
        agent_build_ref = db.agent_build_get_by_triple(ctxt,
                                  hypervisor, os, architecture)
        db.agent_build_destroy(ctxt, agent_build_ref['id'])

    @args('--hypervisor', metavar='<hypervisor>',
            help='hypervisor(default: None)')
    def list(self, hypervisor=None):
        """Lists all agent builds.

        arguments: <none>
        """
        fmt = "%-10s  %-8s  %12s  %s"
        ctxt = context.get_admin_context()
        by_hypervisor = {}
        for agent_build in db.agent_build_get_all(ctxt):
            buildlist = by_hypervisor.get(agent_build.hypervisor)
            if not buildlist:
                buildlist = by_hypervisor[agent_build.hypervisor] = []

            buildlist.append(agent_build)

        for key, buildlist in six.iteritems(by_hypervisor):
            if hypervisor and key != hypervisor:
                continue

            print(_('Hypervisor: %s') % key)
            print(fmt % ('-' * 10, '-' * 8, '-' * 12, '-' * 32))
            for agent_build in buildlist:
                print(fmt % (agent_build.os, agent_build.architecture,
                             agent_build.version, agent_build.md5hash))
                print('    %s' % agent_build.url)

            print()

    @args('--os', metavar='<os>', help='os')
    @args('--architecture', dest='architecture',
            metavar='<architecture>', help='architecture')
    @args('--version', metavar='<version>', help='version')
    @args('--url', metavar='<url>', help='url')
    @args('--md5hash', metavar='<md5hash>', help='md5hash')
    @args('--hypervisor', metavar='<hypervisor>',
            help='hypervisor(default: xen)')
    def modify(self, os, architecture, version, url, md5hash,
               hypervisor='xen'):
        """Update an existing agent build."""
        ctxt = context.get_admin_context()
        agent_build_ref = db.agent_build_get_by_triple(ctxt,
                                  hypervisor, os, architecture)
        db.agent_build_update(ctxt, agent_build_ref['id'],
                              {'version': version,
                               'url': url,
                               'md5hash': md5hash})


class GetLogCommands(object):
    """Get logging information."""

    def errors(self):
        """Get all of the errors from the log files."""
        error_found = 0
        if CONF.log_dir:
            logs = [x for x in os.listdir(CONF.log_dir) if x.endswith('.log')]
            for file in logs:
                log_file = os.path.join(CONF.log_dir, file)
                lines = [line.strip() for line in open(log_file, "r")]
                lines.reverse()
                print_name = 0
                for index, line in enumerate(lines):
                    if line.find(" ERROR ") > 0:
                        error_found += 1
                        if print_name == 0:
                            print(log_file + ":-")
                            print_name = 1
                        linenum = len(lines) - index
                        print((_('Line %(linenum)d : %(line)s') %
                               {'linenum': linenum, 'line': line}))
        if error_found == 0:
            print(_('No errors in logfiles!'))

    @args('--num_entries', metavar='<number of entries>',
            help='number of entries(default: 10)')
    def syslog(self, num_entries=10):
        """Get <num_entries> of the nova syslog events."""
        entries = int(num_entries)
        count = 0
        log_file = ''
        if os.path.exists('/var/log/syslog'):
            log_file = '/var/log/syslog'
        elif os.path.exists('/var/log/messages'):
            log_file = '/var/log/messages'
        else:
            print(_('Unable to find system log file!'))
            return(1)
        lines = [line.strip() for line in open(log_file, "r")]
        lines.reverse()
        print(_('Last %s nova syslog entries:-') % (entries))
        for line in lines:
            if line.find("nova") > 0:
                count += 1
                print("%s" % (line))
            if count == entries:
                break

        if count == 0:
            print(_('No nova entries in syslog!'))


class CellCommands(object):
    """Commands for managing cells."""

    def _create_transport_hosts(self, username, password,
                                broker_hosts=None, hostname=None, port=None):
        """Returns a list of oslo.messaging.TransportHost objects."""
        transport_hosts = []
        # Either broker-hosts or hostname should be set
        if broker_hosts:
            hosts = broker_hosts.split(',')
            for host in hosts:
                host = host.strip()
                broker_hostname, broker_port = utils.parse_server_string(host)
                if not broker_port:
                    msg = _('Invalid broker_hosts value: %s. It should be'
                            ' in hostname:port format') % host
                    raise ValueError(msg)
                try:
                    broker_port = int(broker_port)
                except ValueError:
                    msg = _('Invalid port value: %s. It should be '
                             'an integer') % broker_port
                    raise ValueError(msg)
                transport_hosts.append(
                               messaging.TransportHost(
                                   hostname=broker_hostname,
                                   port=broker_port,
                                   username=username,
                                   password=password))
        else:
            try:
                port = int(port)
            except ValueError:
                msg = _("Invalid port value: %s. Should be an integer") % port
                raise ValueError(msg)
            transport_hosts.append(
                           messaging.TransportHost(
                               hostname=hostname,
                               port=port,
                               username=username,
                               password=password))
        return transport_hosts

    @args('--name', metavar='<name>', help='Name for the new cell')
    @args('--cell_type', metavar='<parent|api|child|compute>',
         help='Whether the cell is parent/api or child/compute')
    @args('--username', metavar='<username>',
         help='Username for the message broker in this cell')
    @args('--password', metavar='<password>',
         help='Password for the message broker in this cell')
    @args('--broker_hosts', metavar='<broker_hosts>',
         help='Comma separated list of message brokers in this cell. '
              'Each Broker is specified as hostname:port with both '
              'mandatory. This option overrides the --hostname '
              'and --port options (if provided). ')
    @args('--hostname', metavar='<hostname>',
         help='Address of the message broker in this cell')
    @args('--port', metavar='<number>',
         help='Port number of the message broker in this cell')
    @args('--virtual_host', metavar='<virtual_host>',
         help='The virtual host of the message broker in this cell')
    @args('--woffset', metavar='<float>')
    @args('--wscale', metavar='<float>')
    def create(self, name, cell_type='child', username=None, broker_hosts=None,
               password=None, hostname=None, port=None, virtual_host=None,
               woffset=None, wscale=None):

        if cell_type not in ['parent', 'child', 'api', 'compute']:
            print("Error: cell type must be 'parent'/'api' or "
                "'child'/'compute'")
            return(2)

        # Set up the transport URL
        transport_hosts = self._create_transport_hosts(
                                                 username, password,
                                                 broker_hosts, hostname,
                                                 port)
        transport_url = rpc.get_transport_url()
        transport_url.hosts.extend(transport_hosts)
        transport_url.virtual_host = virtual_host

        is_parent = False
        if cell_type in ['api', 'parent']:
            is_parent = True
        values = {'name': name,
                  'is_parent': is_parent,
                  'transport_url': urlparse.unquote(str(transport_url)),
                  'weight_offset': float(woffset),
                  'weight_scale': float(wscale)}
        ctxt = context.get_admin_context()
        db.cell_create(ctxt, values)

    @args('--cell_name', metavar='<cell_name>',
          help='Name of the cell to delete')
    def delete(self, cell_name):
        ctxt = context.get_admin_context()
        db.cell_delete(ctxt, cell_name)

    def list(self):
        ctxt = context.get_admin_context()
        cells = db.cell_get_all(ctxt)
        fmt = "%3s  %-10s  %-6s  %-10s  %-15s  %-5s  %-10s"
        print(fmt % ('Id', 'Name', 'Type', 'Username', 'Hostname',
                'Port', 'VHost'))
        print(fmt % ('-' * 3, '-' * 10, '-' * 6, '-' * 10, '-' * 15,
                '-' * 5, '-' * 10))
        for cell in cells:
            url = rpc.get_transport_url(cell.transport_url)
            host = url.hosts[0] if url.hosts else messaging.TransportHost()
            print(fmt % (cell.id, cell.name,
                    'parent' if cell.is_parent else 'child',
                    host.username, host.hostname,
                    host.port, url.virtual_host))
        print(fmt % ('-' * 3, '-' * 10, '-' * 6, '-' * 10, '-' * 15,
                '-' * 5, '-' * 10))


class CellV2Commands(object):
    """Commands for managing cells v2."""

    # TODO(melwitt): Remove this when the oslo.messaging function
    # for assembling a transport url from ConfigOpts is available
    @args('--transport-url', metavar='<transport url>', required=True,
          dest='transport_url',
          help='The transport url for the cell message queue')
    def simple_cell_setup(self, transport_url):
        """Simple cellsv2 setup.

        This simplified command is for use by existing non-cells users to
        configure the default environment. If you are using CellsV1, this
        will not work for you. Returns 0 if setup is completed (or has
        already been done), 1 if no hosts are reporting (and this cannot
        be mapped) and 2 if run in a CellsV1 environment.
        """
        if CONF.cells.enable:
            print('CellsV1 users cannot use this simplified setup command')
            return 2
        ctxt = context.RequestContext()
        try:
            cell0_mapping = self.map_cell0()
        except db_exc.DBDuplicateEntry:
            print('Already setup, nothing to do.')
            return 0
        # Run migrations so cell0 is usable
        with context.target_cell(ctxt, cell0_mapping):
            migration.db_sync(None, context=ctxt)
        cell_uuid = self._map_cell_and_hosts(transport_url)
        if cell_uuid is None:
            # There are no compute hosts which means no cell_mapping was
            # created. This should also mean that there are no instances.
            return 1
        self.map_instances(cell_uuid)
        return 0

    @args('--database_connection',
          metavar='<database_connection>',
          help='The database connection url for cell0. '
               'This is optional. If not provided, a standard database '
               'connection will be used based on the API database connection '
               'from the Nova configuration.'
         )
    def map_cell0(self, database_connection=None):
        """Create a cell mapping for cell0.

        cell0 is used for instances that have not been scheduled to any cell.
        This generally applies to instances that have encountered an error
        before they have been scheduled.

        This command creates a cell mapping for this special cell which
        requires a database to store the instance data.
        """
        def cell0_default_connection():
            # If no database connection is provided one is generated
            # based on the API database connection url.
            # The cell0 database will use the same database scheme and
            # netloc as the API database, with a related path.
            scheme, netloc, path, query, fragment = \
                urlparse.urlsplit(CONF.api_database.connection)
            root, ext = os.path.splitext(path)
            path = root + "_cell0" + ext
            return urlparse.urlunsplit((scheme, netloc, path, query,
                                        fragment))

        dbc = database_connection or cell0_default_connection()
        ctxt = context.RequestContext()
        # A transport url of 'none://' is provided for cell0. RPC should not
        # be used to access cell0 objects. Cells transport switching will
        # ignore any 'none' transport type.
        cell_mapping = objects.CellMapping(
                ctxt, uuid=objects.CellMapping.CELL0_UUID, name="cell0",
                transport_url="none:///",
                database_connection=dbc)
        cell_mapping.create()
        return cell_mapping

    def _get_and_map_instances(self, ctxt, cell_mapping, limit, marker):
        filters = {}
        instances = objects.InstanceList.get_by_filters(
                ctxt.elevated(read_deleted='yes'), filters,
                sort_key='created_at', sort_dir='asc', limit=limit,
                marker=marker)

        for instance in instances:
            try:
                mapping = objects.InstanceMapping(ctxt)
                mapping.instance_uuid = instance.uuid
                mapping.cell_mapping = cell_mapping
                mapping.project_id = instance.project_id
                mapping.create()
            except db_exc.DBDuplicateEntry:
                continue

        if len(instances) == 0 or len(instances) < limit:
            # We've hit the end of the instances table
            marker = None
        else:
            marker = instances[-1].uuid
        return marker

    @args('--cell_uuid', metavar='<cell_uuid>', required=True,
            help='Unmigrated instances will be mapped to the cell with the '
                 'uuid provided.')
    @args('--max-count', metavar='<max_count>',
          help='Maximum number of instances to map')
    def map_instances(self, cell_uuid, max_count=None):
        """Map instances into the provided cell.

        This assumes that Nova on this host is still configured to use the nova
        database not just the nova-api database. Instances in the nova database
        will be queried from oldest to newest and mapped to the provided cell.
        A max-count can be set on the number of instance to map in a single
        run. Repeated runs of the command will start from where the last run
        finished so it is not necessary to increase max-count to finish. An
        exit code of 0 indicates that all instances have been mapped.
        """

        if max_count is not None:
            try:
                max_count = int(max_count)
            except ValueError:
                max_count = -1
            map_all = False
            if max_count < 1:
                print(_('Must supply a positive value for max-count'))
                return 127
        else:
            map_all = True
            max_count = 50

        ctxt = context.RequestContext()
        marker_project_id = 'INSTANCE_MIGRATION_MARKER'

        # Validate the cell exists, this will raise if not
        cell_mapping = objects.CellMapping.get_by_uuid(ctxt, cell_uuid)

        # Check for a marker from a previous run
        marker_mapping = objects.InstanceMappingList.get_by_project_id(ctxt,
                marker_project_id)
        if len(marker_mapping) == 0:
            marker = None
        else:
            # There should be only one here
            marker = marker_mapping[0].instance_uuid.replace(' ', '-')
            marker_mapping[0].destroy()

        next_marker = True
        while next_marker is not None:
            next_marker = self._get_and_map_instances(ctxt, cell_mapping,
                    max_count, marker)
            marker = next_marker
            if not map_all:
                break

        if next_marker:
            # Don't judge me. There's already an InstanceMapping with this UUID
            # so the marker needs to be non destructively modified.
            next_marker = next_marker.replace('-', ' ')
            objects.InstanceMapping(ctxt, instance_uuid=next_marker,
                    project_id=marker_project_id).create()
            return 1
        return 0

    def _map_cell_and_hosts(self, transport_url, name=None, verbose=False):
        ctxt = context.RequestContext()
        cell_mapping_uuid = cell_mapping = None
        # First, try to detect if a CellMapping has already been created
        compute_nodes = objects.ComputeNodeList.get_all(ctxt)
        if not compute_nodes:
            print(_('No hosts found to map to cell, exiting.'))
            return None
        missing_nodes = set()
        for compute_node in compute_nodes:
            try:
                host_mapping = objects.HostMapping.get_by_host(
                    ctxt, compute_node.host)
            except exception.HostMappingNotFound:
                missing_nodes.add(compute_node.host)
            else:
                if verbose:
                    print(_(
                        'Host %(host)s is already mapped to cell %(uuid)s'
                        ) % {'host': host_mapping.host,
                             'uuid': host_mapping.cell_mapping.uuid})
                # Re-using the existing UUID in case there is already a mapping
                # NOTE(sbauza): There could be possibly multiple CellMappings
                # if the operator provides another configuration file and moves
                # the hosts to another cell v2, but that's not really something
                # we should support.
                cell_mapping_uuid = host_mapping.cell_mapping.uuid
        if not missing_nodes:
            print(_('All hosts are already mapped to cell(s), exiting.'))
            return cell_mapping_uuid
        # Create the cell mapping in the API database
        if cell_mapping_uuid is not None:
            cell_mapping = objects.CellMapping.get_by_uuid(
                ctxt, cell_mapping_uuid)
        if cell_mapping is None:
            cell_mapping_uuid = uuidutils.generate_uuid()
            cell_mapping = objects.CellMapping(
                ctxt, uuid=cell_mapping_uuid, name=name,
                transport_url=transport_url,
                database_connection=CONF.database.connection)
            cell_mapping.create()
        # Pull the hosts from the cell database and create the host mappings
        for compute_host in missing_nodes:
            host_mapping = objects.HostMapping(
                ctxt, host=compute_host, cell_mapping=cell_mapping)
            host_mapping.create()
        if verbose:
            print(cell_mapping_uuid)
        return cell_mapping_uuid

    @args('--transport-url', metavar='<transport url>', dest='transport_url',
          help='The transport url for the cell message queue')
    @args('--name', metavar='<name>', help='The name of the cell')
    @args('--verbose', action='store_true',
          help='Return and output the uuid of the created cell')
    def map_cell_and_hosts(self, transport_url=None, name=None, verbose=False):
        """EXPERIMENTAL. Create a cell mapping and host mappings for a cell.

        Users not dividing their cloud into multiple cells will be a single
        cell v2 deployment and should specify:

          nova-manage cell_v2 map_cell_and_hosts --config-file <nova.conf>

        Users running multiple cells can add a cell v2 by specifying:

          nova-manage cell_v2 map_cell_and_hosts --config-file <cell nova.conf>
        """
        transport_url = CONF.transport_url or transport_url
        if not transport_url:
            print('Must specify --transport-url if [DEFAULT]/transport_url '
                  'is not set in the configuration file.')
            return 1
        self._map_cell_and_hosts(transport_url, name, verbose)
        # online_data_migrations established a pattern of 0 meaning everything
        # is done, 1 means run again to do more work. This command doesn't do
        # partial work so 0 is appropriate.
        return 0

    @args('--uuid', metavar='<uuid>', dest='uuid', required=True,
          help=_('The instance UUID to verify'))
    @args('--quiet', action='store_true', dest='quiet',
          help=_('Do not print anything'))
    def verify_instance(self, uuid, quiet=False):
        """Verify instance mapping to a cell.

        This command is useful to determine if the cellsv2 environment is
        properly setup, specifically in terms of the cell, host, and instance
        mapping records required.

        This prints one of three strings (and exits with a code) indicating
        whether the instance is successfully mapped to a cell (0), is unmapped
        due to an incomplete upgrade (1), or unmapped due to normally transient
        state (2).
        """
        if not uuid:
            print(_('Must specify --uuid'))
            return 16

        def say(string):
            if not quiet:
                print(string)

        ctxt = context.RequestContext()
        try:
            mapping = objects.InstanceMapping.get_by_instance_uuid(
                ctxt, uuid)
        except exception.InstanceMappingNotFound:
            say('Instance %s is not mapped to a cell '
                '(upgrade is incomplete)' % uuid)
            return 1
        if mapping.cell_mapping is None:
            say('Instance %s is not mapped to a cell' % uuid)
            return 2
        else:
            say('Instance %s is in cell: %s (%s)' % (
                uuid,
                mapping.cell_mapping.name,
                mapping.cell_mapping.uuid))
            return 0

    @args('--cell_uuid', metavar='<cell_uuid>', dest='cell_uuid',
          help='If provided only this cell will be searched for new hosts to '
               'map.')
    def discover_hosts(self, cell_uuid=None):
        """Searches cells, or a single cell, and maps found hosts.

        When a new host is added to a deployment it will add a service entry
        to the db it's configured to use. This command will check the db for
        each cell, or a single one if passed in, and map any hosts which are
        not currently mapped. If a host is already mapped nothing will be done.
        """
        ctxt = context.RequestContext()

        # TODO(alaski): If this is not run on a host configured to use the API
        # database most of the lookups below will fail and may not provide a
        # great error message. Add a check which will raise a useful error
        # message about running this from an API host.
        if cell_uuid:
            cell_mappings = [objects.CellMapping.get_by_uuid(ctxt, cell_uuid)]
        else:
            cell_mappings = objects.CellMappingList.get_all(context)

        for cell_mapping in cell_mappings:
            # TODO(alaski): Factor this into helper method on CellMapping
            if cell_mapping.uuid == cell_mapping.CELL0_UUID:
                continue
            with context.target_cell(ctxt, cell_mapping):
                compute_nodes = objects.ComputeNodeList.get_all(ctxt)
            for compute in compute_nodes:
                try:
                    objects.HostMapping.get_by_host(ctxt, compute.host)
                except exception.HostMappingNotFound:
                    host_mapping = objects.HostMapping(
                        ctxt, host=compute.host,
                        cell_mapping=cell_mapping)
                    host_mapping.create()


CATEGORIES = {
    'account': AccountCommands,
    'agent': AgentBuildCommands,
    'api_db': ApiDbCommands,
    'cell': CellCommands,
    'cell_v2': CellV2Commands,
    'db': DbCommands,
    'fixed': FixedIpCommands,
    'floating': FloatingIpCommands,
    'host': HostCommands,
    'logs': GetLogCommands,
    'network': NetworkCommands,
    'project': ProjectCommands,
    'shell': ShellCommands,
    'vm': VmCommands,
    'vpn': VpnCommands,
}


add_command_parsers = functools.partial(cmd_common.add_command_parsers,
                                        categories=CATEGORIES)


category_opt = cfg.SubCommandOpt('category',
                                 title='Command categories',
                                 help='Available categories',
                                 handler=add_command_parsers)


def main():
    """Parse options and call the appropriate class/method."""
    CONF.register_cli_opt(category_opt)
    try:
        config.parse_args(sys.argv)
        logging.set_defaults(
            default_log_levels=logging.get_default_log_levels() +
            _EXTRA_DEFAULT_LOG_LEVELS)
        logging.setup(CONF, "nova")
    except cfg.ConfigFilesNotFoundError:
        cfgfile = CONF.config_file[-1] if CONF.config_file else None
        if cfgfile and not os.access(cfgfile, os.R_OK):
            st = os.stat(cfgfile)
            print(_("Could not read %s. Re-running with sudo") % cfgfile)
            try:
                os.execvp('sudo', ['sudo', '-u', '#%s' % st.st_uid] + sys.argv)
            except OSError:
                print(_('sudo failed, continuing as if nothing happened'))

        print(_('Please re-run nova-manage as root.'))
        return(2)

    objects.register_all()

    if CONF.category.name == "version":
        print(version.version_string_with_package())
        return(0)

    if CONF.category.name == "bash-completion":
        cmd_common.print_bash_completion(CATEGORIES)
        return(0)

    try:
        fn, fn_args, fn_kwargs = cmd_common.get_action_fn()
        ret = fn(*fn_args, **fn_kwargs)
        rpc.cleanup()
        return(ret)
    except Exception as ex:
        print(_("error: %s") % ex)
        return(1)
