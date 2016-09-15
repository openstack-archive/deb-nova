# Copyright 2016 Red Hat, Inc.
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

'''
This module contains code for converting from the original
nova.network.model data structure, to the new os-vif based
versioned object model os_vif.objects.*
'''

import sys

import os_vif
from os_vif import objects
from oslo_config import cfg
from oslo_log import log as logging

from nova import exception
from nova.i18n import _
from nova.network import model


LOG = logging.getLogger(__name__)
CONF = cfg.CONF

# Ensure os-vif objects are registered and plugins loaded
os_vif.initialize()


def _get_vif_name(vif):
    """Get a VIF device name

    :param vif: the nova.nework.model.VIF instance

    Get a string suitable for use as a host OS network
    device name

    :returns: a device name
    """

    if vif.get('devname', None) is not None:
        return vif['devname']
    return ('nic' + vif['id'])[:model.NIC_NAME_LEN]


def _get_hybrid_bridge_name(vif):
    """Get a bridge device name

    :param vif: the nova.nework.model.VIF instance

    Get a string suitable for use as a host OS bridge
    device name

    :returns: a bridge name
    """

    return ('qbr' + vif['id'])[:model.NIC_NAME_LEN]


def _is_firewall_required(vif):
    """Check if local firewall is required

    :param vif: the nova.nework.model.VIF instance

    :returns: True if local firewall is required
    """

    if vif.is_neutron_filtering_enabled():
        return False
    if CONF.firewall_driver != "nova.virt.firewall.NoopFirewallDriver":
        return True
    return False


def nova_to_osvif_instance(instance):
    """Convert a Nova instance object to an os-vif instance object

    :param vif: a nova.objects.Instance instance

    :returns: a os_vif.objects.instance_info.InstanceInfo
    """

    info = objects.instance_info.InstanceInfo(
        uuid=instance.uuid,
        name=instance.name)

    if (instance.obj_attr_is_set("project_id") and
            instance.project_id is not None):
        info.project_id = instance.project_id

    return info


def _nova_to_osvif_ip(ip):
    """Convert Nova IP object into os_vif object

    :param route: nova.network.model.IP instance

    :returns: os_vif.objects.fixed_ip.FixedIP instance
    """
    floating_ips = [fip['address'] for fip in ip.get('floating_ips', [])]
    return objects.fixed_ip.FixedIP(
        address=ip['address'],
        floating_ips=floating_ips)


def _nova_to_osvif_ips(ips):
    """Convert Nova IP list into os_vif object

    :param routes: list of nova.network.model.IP instances

    :returns: os_vif.objects.fixed_ip.FixedIPList instance
    """

    return objects.fixed_ip.FixedIPList(
        objects=[_nova_to_osvif_ip(ip) for ip in ips])


def _nova_to_osvif_route(route):
    """Convert Nova route object into os_vif object

    :param route: nova.network.model.Route instance

    :returns: os_vif.objects.route.Route instance
    """

    obj = objects.route.Route(
        cidr=route['cidr'])

    if route['interface'] is not None:
        obj.interface = route['interface']

    if (route['gateway'] is not None and
        route['gateway']['address'] is not None):
        obj.gateway = route['gateway']['address']

    return obj


def _nova_to_osvif_routes(routes):
    """Convert Nova route list into os_vif object

    :param routes: list of nova.network.model.Route instances

    :returns: os_vif.objects.route.RouteList instance
    """

    return objects.route.RouteList(
        objects=[_nova_to_osvif_route(route) for route in routes])


def _nova_to_osvif_subnet(subnet):
    """Convert Nova subnet object into os_vif object

    :param subnet: nova.network.model.Subnet instance

    :returns: os_vif.objects.subnet.Subnet instance
    """

    dnsaddrs = [ip['address'] for ip in subnet['dns']]

    obj = objects.subnet.Subnet(
        dns=dnsaddrs,
        ips=_nova_to_osvif_ips(subnet['ips']),
        routes=_nova_to_osvif_routes(subnet['routes']))
    if subnet['cidr'] is not None:
        obj.cidr = subnet['cidr']
    if (subnet['gateway'] is not None and
        subnet['gateway']['address'] is not None):
        obj.gateway = subnet['gateway']['address']
    return obj


def _nova_to_osvif_subnets(subnets):
    """Convert Nova subnet list into os_vif object

    :param subnets: list of nova.network.model.Subnet instances

    :returns: os_vif.objects.subnet.SubnetList instance
    """

    return objects.subnet.SubnetList(
        objects=[_nova_to_osvif_subnet(subnet) for subnet in subnets])


def _nova_to_osvif_network(network):
    """Convert Nova network object into os_vif object

    :param network: nova.network.model.Network instance

    :returns: os_vif.objects.network.Network instance
    """

    netobj = objects.network.Network(
        id=network['id'],
        bridge_interface=network.get_meta("bridge_interface"),
        subnets=_nova_to_osvif_subnets(network['subnets']))

    if network["bridge"] is not None:
        netobj.bridge = network['bridge']
    if network['label'] is not None:
        netobj.label = network['label']

    if network.get_meta("multi_host") is not None:
        netobj.multi_host = network.get_meta("multi_host")
    if network.get_meta("should_create_bridge") is not None:
        netobj.should_provide_bridge = \
            network.get_meta("should_create_bridge")
    if network.get_meta("should_create_vlan") is not None:
        netobj.should_provide_vlan = network.get_meta("should_create_vlan")
        if network.get_meta("vlan") is None:
            raise exception.NovaException(_("Missing vlan number in %s") %
                                          network)
        netobj.vlan = network.get_meta("vlan")

    return netobj


def _get_vif_instance(vif, cls, **kwargs):
    """Instantiate an os-vif VIF instance

    :param vif: the nova.nework.model.VIF instance
    :param cls: class for a os_vif.objects.vif.VIFBase subclass

    :returns: a os_vif.objects.vif.VIFBase instance
    """

    return cls(
        id=vif['id'],
        address=vif['address'],
        network=_nova_to_osvif_network(vif['network']),
        has_traffic_filtering=vif.is_neutron_filtering_enabled(),
        preserve_on_delete=vif['preserve_on_delete'],
        active=vif['active'],
        **kwargs)


# VIF_TYPE_BRIDGE = 'bridge'
def _nova_to_osvif_vif_bridge(vif):
    obj = _get_vif_instance(
        vif,
        objects.vif.VIFBridge,
        plugin="linux_bridge",
        vif_name=_get_vif_name(vif))
    if vif["network"]["bridge"] is not None:
        obj.bridge_name = vif["network"]["bridge"]
    return obj


# VIF_TYPE_OVS = 'ovs'
def _nova_to_osvif_vif_ovs(vif):
    profile = objects.vif.VIFPortProfileOpenVSwitch(
        interface_id=vif.get('ovs_interfaceid') or vif['id'])
    if _is_firewall_required(vif) or vif.is_hybrid_plug_enabled():
        obj = _get_vif_instance(
            vif,
            objects.vif.VIFBridge,
            port_profile=profile,
            plugin="ovs",
            vif_name=_get_vif_name(vif),
            bridge_name=_get_hybrid_bridge_name(vif))
    else:
        obj = _get_vif_instance(
            vif,
            objects.vif.VIFOpenVSwitch,
            port_profile=profile,
            plugin="ovs",
            vif_name=_get_vif_name(vif))
        if vif["network"]["bridge"] is not None:
            obj.bridge_name = vif["network"]["bridge"]
    return obj


# VIF_TYPE_IVS = 'ivs'
def _nova_to_osvif_vif_ivs(vif):
    raise NotImplementedError()


# VIF_TYPE_DVS = 'dvs'
def _nova_to_osvif_vif_dvs(vif):
    raise NotImplementedError()


# VIF_TYPE_IOVISOR = 'iovisor'
def _nova_to_osvif_vif_iovisor(vif):
    raise NotImplementedError()


# VIF_TYPE_802_QBG = '802.1qbg'
def _nova_to_osvif_vif_802_1qbg(vif):
    raise NotImplementedError()


# VIF_TYPE_802_QBH = '802.1qbh'
def _nova_to_osvif_vif_802_1qbh(vif):
    raise NotImplementedError()


# VIF_TYPE_HW_VEB = 'hw_veb'
def _nova_to_osvif_vif_hw_veb(vif):
    raise NotImplementedError()


# VIF_TYPE_IB_HOSTDEV = 'ib_hostdev'
def _nova_to_osvif_vif_ib_hostdev(vif):
    raise NotImplementedError()


# VIF_TYPE_MIDONET = 'midonet'
def _nova_to_osvif_vif_midonet(vif):
    raise NotImplementedError()


# VIF_TYPE_VHOSTUSER = 'vhostuser'
def _nova_to_osvif_vif_vhostuser(vif):
    raise NotImplementedError()


# VIF_TYPE_VROUTER = 'vrouter'
def _nova_to_osvif_vif_vrouter(vif):
    raise NotImplementedError()


# VIF_TYPE_TAP = 'tap'
def _nova_to_osvif_vif_tap(vif):
    raise NotImplementedError()


# VIF_TYPE_MACVTAP = 'macvtap'
def _nova_to_osvif_vif_macvtap(vif):
    raise NotImplementedError()


# VIF_TYPE_HOSTDEV = 'hostdev_physical'
def _nova_to_osvif_vif_hostdev_physical(vif):
    raise NotImplementedError()


def nova_to_osvif_vif(vif):
    """Convert a Nova VIF model to an os-vif object

    :param vif: a nova.network.model.VIF instance

    Attempt to convert a nova VIF instance into an os-vif
    VIF object, pointing to a suitable plugin. This will
    return None if there is no os-vif plugin available yet.

    :returns: a os_vif.objects.vif.VIFBase subclass, or None
      if not supported with os-vif yet
    """

    LOG.debug("Converting VIF %s", vif)

    funcname = "_nova_to_osvif_vif_" + vif['type'].replace(".", "_")
    func = getattr(sys.modules[__name__], funcname, None)

    if not func:
        raise exception.NovaException(
            "Unsupported VIF type %(type)s convert '%(func)s'" %
            {'type': vif['type'], 'func': funcname})

    try:
        vifobj = func(vif)
        LOG.debug("Converted object %s", vifobj)
        return vifobj
    except NotImplementedError:
        LOG.debug("No conversion for VIF type %s yet",
                  vif['type'])
        return None
