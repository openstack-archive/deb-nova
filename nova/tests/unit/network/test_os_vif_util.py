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

from os_vif import objects as osv_objects

from nova import exception
from nova.network import model
from nova.network import os_vif_util
from nova import objects
from nova import test


class OSVIFUtilTestCase(test.NoDBTestCase):

    def setUp(self):
        super(OSVIFUtilTestCase, self).setUp()

        osv_objects.register_all()

    # Remove when all os-vif objects include the
    # ComparableVersionedObject mix-in
    def assertObjEqual(self, expect, actual):
        actual.obj_reset_changes(recursive=True)
        expect.obj_reset_changes(recursive=True)
        self.assertEqual(expect.obj_to_primitive(),
                         actual.obj_to_primitive())

    def _test_is_firewall_required(self, port_filter, driver, expect):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type=model.VIF_TYPE_BRIDGE,
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
            details={
                model.VIF_DETAILS_PORT_FILTER: port_filter,
            }
        )
        self.flags(firewall_driver=driver)

        self.assertEqual(expect, os_vif_util._is_firewall_required(vif))

    def test_is_firewall_required_via_vif(self):
        self._test_is_firewall_required(
            True, "nova.virt.libvirt.firewall.IptablesFirewallDriver", False)

    def test_is_firewall_required_via_driver(self):
        self._test_is_firewall_required(
            False, "nova.virt.libvirt.firewall.IptablesFirewallDriver", True)

    def test_is_firewall_required_not(self):
        self._test_is_firewall_required(
            False, "nova.virt.firewall.NoopFirewallDriver", False)

    def test_nova_to_osvif_instance(self):
        inst = objects.Instance(
            id="1242",
            uuid="d5b1090c-9e00-4fa4-9504-4b1494857970",
            project_id="2f37d7f6-e51a-4a1f-8b6e-b0917ffc8390")

        info = os_vif_util.nova_to_osvif_instance(inst)

        expect = osv_objects.instance_info.InstanceInfo(
            uuid="d5b1090c-9e00-4fa4-9504-4b1494857970",
            name="instance-000004da",
            project_id="2f37d7f6-e51a-4a1f-8b6e-b0917ffc8390")

        self.assertObjEqual(info, expect)

    def test_nova_to_osvif_instance_minimal(self):
        inst = objects.Instance(
            id="1242",
            uuid="d5b1090c-9e00-4fa4-9504-4b1494857970")

        actual = os_vif_util.nova_to_osvif_instance(inst)

        expect = osv_objects.instance_info.InstanceInfo(
            uuid=inst.uuid,
            name=inst.name)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_ips(self):
        ips = [
            model.FixedIP(
                address="192.168.122.24",
                floating_ips=[
                    model.IP(address="192.168.122.100",
                             type="floating"),
                    model.IP(address="192.168.122.101",
                             type="floating"),
                    model.IP(address="192.168.122.102",
                             type="floating"),
                ],
                version=4),
            model.FixedIP(
                address="2001::beef",
                version=6),
        ]

        actual = os_vif_util._nova_to_osvif_ips(ips)

        expect = osv_objects.fixed_ip.FixedIPList(
            objects=[
                osv_objects.fixed_ip.FixedIP(
                    address="192.168.122.24",
                    floating_ips=[
                        "192.168.122.100",
                        "192.168.122.101",
                        "192.168.122.102",
                        ]),
                osv_objects.fixed_ip.FixedIP(
                    address="2001::beef",
                    floating_ips=[]),
                ],
            )

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_routes(self):
        routes = [
            model.Route(cidr="192.168.1.0/24",
                        gateway=model.IP(
                            address="192.168.1.254",
                            type='gateway'),
                        interface="eth0"),
            model.Route(cidr="10.0.0.0/8",
                        gateway=model.IP(
                            address="10.0.0.1",
                            type='gateway')),
        ]

        expect = osv_objects.route.RouteList(
            objects=[
                osv_objects.route.Route(
                    cidr="192.168.1.0/24",
                    gateway="192.168.1.254",
                    interface="eth0"),
                osv_objects.route.Route(
                    cidr="10.0.0.0/8",
                    gateway="10.0.0.1"),
            ])

        actual = os_vif_util._nova_to_osvif_routes(routes)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_subnets(self):
        subnets = [
            model.Subnet(cidr="192.168.1.0/24",
                         dns=[
                             model.IP(
                                 address="192.168.1.1",
                                 type="dns"),
                             model.IP(
                                 address="192.168.1.2",
                                 type="dns"),
                         ],
                         gateway=model.IP(
                             address="192.168.1.254",
                             type='gateway'),
                         ips=[
                             model.FixedIP(
                                 address="192.168.1.100",
                             ),
                             model.FixedIP(
                                 address="192.168.1.101",
                             ),
                         ],
                         routes=[
                             model.Route(
                                 cidr="10.0.0.1/24",
                                 gateway=model.IP(
                                     address="192.168.1.254",
                                     type="gateway"),
                                 interface="eth0"),
                         ]),
            model.Subnet(dns=[
                             model.IP(
                                 address="192.168.1.1",
                                 type="dns"),
                             model.IP(
                                 address="192.168.1.2",
                                 type="dns"),
                         ],
                         ips=[
                             model.FixedIP(
                                 address="192.168.1.100",
                             ),
                             model.FixedIP(
                                 address="192.168.1.101",
                             ),
                         ],
                         routes=[
                             model.Route(
                                 cidr="10.0.0.1/24",
                                 gateway=model.IP(
                                     address="192.168.1.254",
                                     type="gateway"),
                                 interface="eth0"),
                         ]),
            model.Subnet(dns=[
                             model.IP(
                                 address="192.168.1.1",
                                 type="dns"),
                             model.IP(
                                 address="192.168.1.2",
                                 type="dns"),
                         ],
                         gateway=model.IP(
                             type='gateway'),
                         ips=[
                             model.FixedIP(
                                 address="192.168.1.100",
                             ),
                             model.FixedIP(
                                 address="192.168.1.101",
                             ),
                         ],
                         routes=[
                             model.Route(
                                 cidr="10.0.0.1/24",
                                 gateway=model.IP(
                                     address="192.168.1.254",
                                     type="gateway"),
                                 interface="eth0"),
                         ]),
        ]

        expect = osv_objects.subnet.SubnetList(
            objects=[
                osv_objects.subnet.Subnet(
                    cidr="192.168.1.0/24",
                    dns=["192.168.1.1",
                         "192.168.1.2"],
                    gateway="192.168.1.254",
                    ips=osv_objects.fixed_ip.FixedIPList(
                        objects=[
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.100",
                                floating_ips=[]),
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.101",
                                floating_ips=[]),
                            ]),
                    routes=osv_objects.route.RouteList(
                        objects=[
                            osv_objects.route.Route(
                                cidr="10.0.0.1/24",
                                gateway="192.168.1.254",
                                interface="eth0")
                            ]),
                    ),
                osv_objects.subnet.Subnet(
                    dns=["192.168.1.1",
                         "192.168.1.2"],
                    ips=osv_objects.fixed_ip.FixedIPList(
                        objects=[
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.100",
                                floating_ips=[]),
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.101",
                                floating_ips=[]),
                            ]),
                    routes=osv_objects.route.RouteList(
                        objects=[
                            osv_objects.route.Route(
                                cidr="10.0.0.1/24",
                                gateway="192.168.1.254",
                                interface="eth0")
                            ]),
                    ),
                osv_objects.subnet.Subnet(
                    dns=["192.168.1.1",
                         "192.168.1.2"],
                    ips=osv_objects.fixed_ip.FixedIPList(
                        objects=[
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.100",
                                floating_ips=[]),
                            osv_objects.fixed_ip.FixedIP(
                                address="192.168.1.101",
                                floating_ips=[]),
                            ]),
                    routes=osv_objects.route.RouteList(
                        objects=[
                            osv_objects.route.Route(
                                cidr="10.0.0.1/24",
                                gateway="192.168.1.254",
                                interface="eth0")
                            ]),
                    ),
            ])

        actual = os_vif_util._nova_to_osvif_subnets(subnets)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_network(self):
        network = model.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            bridge="br0",
            subnets=[
                model.Subnet(cidr="192.168.1.0/24",
                             gateway=model.IP(
                                 address="192.168.1.254",
                                 type='gateway')),
            ])

        expect = osv_objects.network.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            bridge="br0",
            bridge_interface=None,
            subnets=osv_objects.subnet.SubnetList(
                objects=[
                    osv_objects.subnet.Subnet(
                        cidr="192.168.1.0/24",
                        dns=[],
                        gateway="192.168.1.254",
                        ips=osv_objects.fixed_ip.FixedIPList(
                            objects=[]),
                        routes=osv_objects.route.RouteList(
                            objects=[]),
                    )
                ]))

        actual = os_vif_util._nova_to_osvif_network(network)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_network_extra(self):
        network = model.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            bridge="br0",
            multi_host=True,
            should_create_bridge=True,
            should_create_vlan=True,
            bridge_interface="eth0",
            vlan=1729,
            subnets=[
                model.Subnet(cidr="192.168.1.0/24",
                             gateway=model.IP(
                                 address="192.168.1.254",
                                 type='gateway')),
            ])

        expect = osv_objects.network.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            bridge="br0",
            multi_host=True,
            should_provide_bridge=True,
            should_provide_vlan=True,
            bridge_interface="eth0",
            vlan=1729,
            subnets=osv_objects.subnet.SubnetList(
                objects=[
                    osv_objects.subnet.Subnet(
                        cidr="192.168.1.0/24",
                        dns=[],
                        gateway="192.168.1.254",
                        ips=osv_objects.fixed_ip.FixedIPList(
                            objects=[]),
                        routes=osv_objects.route.RouteList(
                            objects=[]),
                    )
                ]))

        actual = os_vif_util._nova_to_osvif_network(network)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_network_labeled_no_bridge(self):
        network = model.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            label="Demo Net",
            subnets=[
                model.Subnet(cidr="192.168.1.0/24",
                             gateway=model.IP(
                                 address="192.168.1.254",
                                 type='gateway')),
            ])

        expect = osv_objects.network.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            bridge_interface=None,
            label="Demo Net",
            subnets=osv_objects.subnet.SubnetList(
                objects=[
                    osv_objects.subnet.Subnet(
                        cidr="192.168.1.0/24",
                        dns=[],
                        gateway="192.168.1.254",
                        ips=osv_objects.fixed_ip.FixedIPList(
                            objects=[]),
                        routes=osv_objects.route.RouteList(
                            objects=[]),
                    )
                ]))

        actual = os_vif_util._nova_to_osvif_network(network)

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_network_labeled_no_vlan(self):
        network = model.Network(
            id="b82c1929-051e-481d-8110-4669916c7915",
            label="Demo Net",
            should_create_vlan=True,
            subnets=[
                model.Subnet(cidr="192.168.1.0/24",
                             gateway=model.IP(
                                 address="192.168.1.254",
                                 type='gateway')),
            ])

        self.assertRaises(exception.NovaException,
                          os_vif_util._nova_to_osvif_network,
                          network)

    def test_nova_to_osvif_vif_linux_bridge(self):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type=model.VIF_TYPE_BRIDGE,
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
            details={
                model.VIF_DETAILS_PORT_FILTER: True,
            }
        )

        actual = os_vif_util.nova_to_osvif_vif(vif)

        expect = osv_objects.vif.VIFBridge(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            active=False,
            address="22:52:25:62:e2:aa",
            has_traffic_filtering=True,
            plugin="linux_bridge",
            preserve_on_delete=False,
            vif_name="nicdc065497-3c",
            network=osv_objects.network.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                bridge_interface=None,
                label="Demo Net",
                subnets=osv_objects.subnet.SubnetList(
                    objects=[])))

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_vif_ovs_plain(self):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type=model.VIF_TYPE_OVS,
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
            details={
                model.VIF_DETAILS_PORT_FILTER: True,
            }
        )

        actual = os_vif_util.nova_to_osvif_vif(vif)

        expect = osv_objects.vif.VIFOpenVSwitch(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            active=False,
            address="22:52:25:62:e2:aa",
            has_traffic_filtering=True,
            plugin="ovs",
            port_profile=osv_objects.vif.VIFPortProfileOpenVSwitch(
                interface_id="dc065497-3c8d-4f44-8fb4-e1d33c16a536"),
            preserve_on_delete=False,
            vif_name="nicdc065497-3c",
            network=osv_objects.network.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                bridge_interface=None,
                label="Demo Net",
                subnets=osv_objects.subnet.SubnetList(
                    objects=[])))

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_vif_ovs_hybrid(self):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type=model.VIF_TYPE_OVS,
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
            details={
                model.VIF_DETAILS_PORT_FILTER: False,
            }
        )

        actual = os_vif_util.nova_to_osvif_vif(vif)

        expect = osv_objects.vif.VIFBridge(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            active=False,
            address="22:52:25:62:e2:aa",
            has_traffic_filtering=False,
            plugin="ovs",
            bridge_name="qbrdc065497-3c",
            port_profile=osv_objects.vif.VIFPortProfileOpenVSwitch(
                interface_id="dc065497-3c8d-4f44-8fb4-e1d33c16a536"),
            preserve_on_delete=False,
            vif_name="nicdc065497-3c",
            network=osv_objects.network.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                bridge_interface=None,
                label="Demo Net",
                subnets=osv_objects.subnet.SubnetList(
                    objects=[])))

        self.assertObjEqual(expect, actual)

    def test_nova_to_osvif_vif_ivs_plain(self):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type=model.VIF_TYPE_IVS,
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
            details={
                model.VIF_DETAILS_PORT_FILTER: True,
            }
        )

        actual = os_vif_util.nova_to_osvif_vif(vif)

        self.assertIsNone(actual)

    def test_nova_to_osvif_vif_unknown(self):
        vif = model.VIF(
            id="dc065497-3c8d-4f44-8fb4-e1d33c16a536",
            type="wibble",
            address="22:52:25:62:e2:aa",
            network=model.Network(
                id="b82c1929-051e-481d-8110-4669916c7915",
                label="Demo Net",
                subnets=[]),
        )

        self.assertRaises(exception.NovaException,
                          os_vif_util.nova_to_osvif_vif,
                          vif)
