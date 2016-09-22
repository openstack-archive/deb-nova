# Copyright 2010-2011 OpenStack Foundation
# Copyright 2011 Piston Cloud Computing, Inc.
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

import base64
import collections
import datetime
import uuid

import fixtures
import iso8601
import mock
from mox3 import mox
from oslo_policy import policy as oslo_policy
from oslo_serialization import jsonutils
from oslo_utils import timeutils
import six
from six.moves import range
import six.moves.urllib.parse as urlparse
import testtools
import webob

from nova.api.openstack import api_version_request
from nova.api.openstack import common
from nova.api.openstack import compute
from nova.api.openstack.compute import extension_info
from nova.api.openstack.compute import ips
from nova.api.openstack.compute import keypairs
from nova.api.openstack.compute import servers
from nova.api.openstack.compute import views
from nova.api.openstack import extensions
from nova.api.openstack import wsgi as os_wsgi
from nova import availability_zones
from nova.compute import api as compute_api
from nova.compute import flavors
from nova.compute import task_states
from nova.compute import vm_states
import nova.conf
from nova import context
from nova import db
from nova.db.sqlalchemy import models
from nova import exception
from nova.image import glance
from nova.network import manager
from nova import objects
from nova.objects import instance as instance_obj
from nova import policy
from nova import test
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_network
from nova.tests.unit.image import fake
from nova.tests.unit import matchers
from nova.tests import uuidsentinel as uuids
from nova import utils as nova_utils

CONF = nova.conf.CONF

FAKE_UUID = fakes.FAKE_UUID

INSTANCE_IDS = {FAKE_UUID: 1}
FIELDS = instance_obj.INSTANCE_DEFAULT_FIELDS


def fake_gen_uuid():
    return FAKE_UUID


def return_servers_empty(context, *args, **kwargs):
    return objects.InstanceList(objects=[])


def instance_update_and_get_original(context, instance_uuid, values,
                                     columns_to_join=None,
                                     ):
    inst = fakes.stub_instance(INSTANCE_IDS.get(instance_uuid),
                               name=values.get('display_name'))
    inst = dict(inst, **values)
    return (inst, inst)


def instance_update(context, instance_uuid, values):
    inst = fakes.stub_instance(INSTANCE_IDS.get(instance_uuid),
                               name=values.get('display_name'))
    inst = dict(inst, **values)
    return inst


def fake_compute_api(cls, req, id):
    return True


def fake_start_stop_not_ready(self, context, instance):
    raise exception.InstanceNotReady(instance_id=instance["uuid"])


def fake_start_stop_invalid_state(self, context, instance):
    raise exception.InstanceInvalidState(
        instance_uuid=instance['uuid'], attr='fake_attr',
        method='fake_method', state='fake_state')


def fake_instance_get_by_uuid_not_found(context, uuid,
                                        columns_to_join, use_slave=False):
    raise exception.InstanceNotFound(instance_id=uuid)


def fake_instance_get_all_with_locked(context, list_locked, **kwargs):
    obj_list = []
    s_id = 0
    for locked in list_locked:
        uuid = fakes.get_fake_uuid(locked)
        s_id = s_id + 1
        kwargs['locked_by'] = None if locked == 'not_locked' else locked
        server = fakes.stub_instance_obj(context, id=s_id, uuid=uuid, **kwargs)
        obj_list.append(server)
    return objects.InstanceList(objects=obj_list)


def fake_instance_get_all_with_description(context, list_desc, **kwargs):
    obj_list = []
    s_id = 0
    for desc in list_desc:
        uuid = fakes.get_fake_uuid(desc)
        s_id = s_id + 1
        kwargs['display_description'] = desc
        server = fakes.stub_instance_obj(context, id=s_id, uuid=uuid, **kwargs)
        obj_list.append(server)
    return objects.InstanceList(objects=obj_list)


class MockSetAdminPassword(object):
    def __init__(self):
        self.instance_id = None
        self.password = None

    def __call__(self, context, instance_id, password):
        self.instance_id = instance_id
        self.password = password


class ControllerTest(test.TestCase):

    def setUp(self):
        super(ControllerTest, self).setUp()
        self.flags(verbose=True, use_ipv6=False)
        fakes.stub_out_key_pair_funcs(self)
        fake.stub_out_image_service(self)
        return_server = fakes.fake_compute_get()
        return_servers = fakes.fake_compute_get_all()
        # Server sort keys extension is enabled in v21 so sort data is passed
        # to the instance API and the sorted DB API is invoked
        self.stubs.Set(compute_api.API, 'get_all',
                       lambda api, *a, **k: return_servers(*a, **k))
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: return_server(*a, **k))
        self.stub_out('nova.db.instance_update_and_get_original',
                      instance_update_and_get_original)
        self.flags(group='glance', api_servers=['http://localhost:9292'])

        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)
        self.ips_controller = ips.IPsController()
        policy.reset()
        policy.init()
        fake_network.stub_out_nw_api_get_instance_nw_info(self)


class ServersControllerTest(ControllerTest):
    wsgi_api_version = os_wsgi.DEFAULT_API_VERSION

    def req(self, url, use_admin_context=False):
        return fakes.HTTPRequest.blank(url,
                                       use_admin_context=use_admin_context,
                                       version=self.wsgi_api_version)

    def test_requested_networks_prefix(self):
        uuid = 'br-00000000-0000-0000-0000-000000000000'
        requested_networks = [{'uuid': uuid}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertIn((uuid, None), res.as_tuples())

    def test_requested_networks_neutronv2_enabled_with_port(self):
        self.flags(use_neutron=True)
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'port': port}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertEqual([(None, None, port, None)], res.as_tuples())

    def test_requested_networks_neutronv2_enabled_with_network(self):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        requested_networks = [{'uuid': network}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertEqual([(network, None, None, None)], res.as_tuples())

    def test_requested_networks_neutronv2_enabled_with_network_and_port(self):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network, 'port': port}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertEqual([(None, None, port, None)], res.as_tuples())

    def test_requested_networks_with_duplicate_networks(self):
        # duplicate networks are allowed only for nova neutron v2.0
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        requested_networks = [{'uuid': network}, {'uuid': network}]
        self.assertRaises(
            webob.exc.HTTPBadRequest,
            self.controller._get_requested_networks,
            requested_networks)

    def test_requested_networks_with_neutronv2_and_duplicate_networks(self):
        # duplicate networks are allowed only for nova neutron v2.0
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        requested_networks = [{'uuid': network}, {'uuid': network}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertEqual([(network, None, None, None),
                          (network, None, None, None)], res.as_tuples())

    def test_requested_networks_neutronv2_enabled_conflict_on_fixed_ip(self):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        addr = '10.0.0.1'
        requested_networks = [{'uuid': network,
                               'fixed_ip': addr,
                               'port': port}]
        self.assertRaises(
            webob.exc.HTTPBadRequest,
            self.controller._get_requested_networks,
            requested_networks)

    def test_requested_networks_neutronv2_disabled_with_port(self):
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'port': port}]
        self.assertRaises(
            webob.exc.HTTPBadRequest,
            self.controller._get_requested_networks,
            requested_networks)

    def test_requested_networks_api_enabled_with_v2_subclass(self):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network, 'port': port}]
        res = self.controller._get_requested_networks(requested_networks)
        self.assertEqual([(None, None, port, None)], res.as_tuples())

    def test_get_server_by_uuid(self):
        req = self.req('/fake/servers/%s' % FAKE_UUID)
        res_dict = self.controller.show(req, FAKE_UUID)
        self.assertEqual(res_dict['server']['id'], FAKE_UUID)

    def test_get_server_joins_pci_devices(self):

        def fake_get(_self, *args, **kwargs):
            expected_attrs = kwargs['expected_attrs']
            self.assertEqual(['flavor', 'info_cache', 'metadata',
                              'numa_topology', 'pci_devices'], expected_attrs)
            ctxt = context.RequestContext('fake', 'fake')
            return fake_instance.fake_instance_obj(
                ctxt, expected_attrs=expected_attrs)

        self.stubs.Set(compute_api.API, 'get', fake_get)

        req = self.req('/fake/servers/%s' % FAKE_UUID)
        self.controller.show(req, FAKE_UUID)

    def test_unique_host_id(self):
        """Create two servers with the same host and different
        project_ids and check that the host_id's are unique.
        """
        def return_instance_with_host(context, *args, **kwargs):
            project_id = str(uuid.uuid4())
            return fakes.stub_instance_obj(context, id=1, uuid=FAKE_UUID,
                                           project_id=project_id,
                                           host='fake_host')

        self.stubs.Set(compute_api.API, 'get',
                       return_instance_with_host)

        req = self.req('/fake/servers/%s' % FAKE_UUID)
        with mock.patch.object(compute_api.API, 'get') as mock_get:
            mock_get.side_effect = return_instance_with_host
            server1 = self.controller.show(req, FAKE_UUID)
            server2 = self.controller.show(req, FAKE_UUID)

        self.assertNotEqual(server1['server']['hostId'],
                            server2['server']['hostId'])

    def _get_server_data_dict(self, uuid, image_bookmark, flavor_bookmark,
                              status="ACTIVE", progress=100):
        return {
            "server": {
                "id": uuid,
                "user_id": "fake_user",
                "tenant_id": "fake_project",
                "updated": "2010-11-11T11:00:00Z",
                "created": "2010-10-10T12:00:00Z",
                "progress": progress,
                "name": "server2",
                "status": status,
                "hostId": '',
                "image": {
                    "id": "10",
                    "links": [
                        {
                            "rel": "bookmark",
                            "href": image_bookmark,
                        },
                    ],
                },
                "flavor": {
                    "id": "2",
                  "links": [
                      {
                          "rel": "bookmark",
                          "href": flavor_bookmark,
                      },
                  ],
                },
                "addresses": {
                    'test1': [
                        {'version': 4, 'addr': '192.168.1.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 6, 'addr': '2001:db8:0:1::1',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'}
                    ]
                },
                "metadata": {
                    "seq": "2",
                },
                "links": [
                    {
                        "rel": "self",
                        "href": "http://localhost/v2/fake/servers/%s" % uuid,
                    },
                    {
                        "rel": "bookmark",
                        "href": "http://localhost/fake/servers/%s" % uuid,
                    },
                ],
                "OS-DCF:diskConfig": "MANUAL",
                "accessIPv4": '',
                "accessIPv6": '',
            }
        }

    def test_get_server_by_id(self):
        self.flags(use_ipv6=True)
        image_bookmark = "http://localhost/fake/images/10"
        flavor_bookmark = "http://localhost/fake/flavors/2"

        uuid = FAKE_UUID
        req = self.req('/v2/fake/servers/%s' % uuid)
        res_dict = self.controller.show(req, uuid)

        expected_server = self._get_server_data_dict(uuid,
                                                     image_bookmark,
                                                     flavor_bookmark,
                                                     progress=0)
        expected_server['server']['name'] = 'server1'
        expected_server['server']['metadata']['seq'] = '1'
        self.assertThat(res_dict, matchers.DictMatches(expected_server))

    def test_get_server_with_active_status_by_id(self):
        image_bookmark = "http://localhost/fake/images/10"
        flavor_bookmark = "http://localhost/fake/flavors/2"

        new_return_server = fakes.fake_compute_get(
            id=2, vm_state=vm_states.ACTIVE, progress=100)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: new_return_server(*a, **k))

        uuid = FAKE_UUID
        req = self.req('/fake/servers/%s' % uuid)
        res_dict = self.controller.show(req, uuid)
        expected_server = self._get_server_data_dict(uuid,
                                                     image_bookmark,
                                                     flavor_bookmark)
        self.assertThat(res_dict, matchers.DictMatches(expected_server))

    def test_get_server_with_id_image_ref_by_id(self):
        image_ref = "10"
        image_bookmark = "http://localhost/fake/images/10"
        flavor_id = "1"
        flavor_bookmark = "http://localhost/fake/flavors/2"

        new_return_server = fakes.fake_compute_get(
            id=2, vm_state=vm_states.ACTIVE, image_ref=image_ref,
            flavor_id=flavor_id, progress=100)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: new_return_server(*a, **k))

        uuid = FAKE_UUID
        req = self.req('/fake/servers/%s' % uuid)
        res_dict = self.controller.show(req, uuid)
        expected_server = self._get_server_data_dict(uuid,
                                                     image_bookmark,
                                                     flavor_bookmark)

        self.assertThat(res_dict, matchers.DictMatches(expected_server))

    def test_get_server_addresses_from_cache(self):
        pub0 = ('172.19.0.1', '172.19.0.2',)
        pub1 = ('1.2.3.4',)
        pub2 = ('b33f::fdee:ddff:fecc:bbaa',)
        priv0 = ('192.168.0.3', '192.168.0.4',)

        def _ip(ip):
            return {'address': ip, 'type': 'fixed'}

        nw_cache = [
            {'address': 'aa:aa:aa:aa:aa:aa',
             'id': 1,
             'network': {'bridge': 'br0',
                         'id': 1,
                         'label': 'public',
                         'subnets': [{'cidr': '172.19.0.0/24',
                                      'ips': [_ip(ip) for ip in pub0]},
                                      {'cidr': '1.2.3.0/16',
                                       'ips': [_ip(ip) for ip in pub1]},
                                      {'cidr': 'b33f::/64',
                                       'ips': [_ip(ip) for ip in pub2]}]}},
            {'address': 'bb:bb:bb:bb:bb:bb',
             'id': 2,
             'network': {'bridge': 'br1',
                         'id': 2,
                         'label': 'private',
                         'subnets': [{'cidr': '192.168.0.0/24',
                                      'ips': [_ip(ip) for ip in priv0]}]}}]

        return_server = fakes.fake_compute_get(nw_cache=nw_cache)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: return_server(*a, **k))

        req = self.req('/fake/servers/%s/ips' % FAKE_UUID)
        res_dict = self.ips_controller.index(req, FAKE_UUID)

        expected = {
            'addresses': {
                'private': [
                    {'version': 4, 'addr': '192.168.0.3'},
                    {'version': 4, 'addr': '192.168.0.4'},
                ],
                'public': [
                    {'version': 4, 'addr': '172.19.0.1'},
                    {'version': 4, 'addr': '172.19.0.2'},
                    {'version': 4, 'addr': '1.2.3.4'},
                    {'version': 6, 'addr': 'b33f::fdee:ddff:fecc:bbaa'},
                ],
            },
        }
        self.assertThat(res_dict, matchers.DictMatches(expected))
        # Make sure we kept the addresses in order
        self.assertIsInstance(res_dict['addresses'], collections.OrderedDict)
        labels = [vif['network']['label'] for vif in nw_cache]
        for index, label in enumerate(res_dict['addresses'].keys()):
            self.assertEqual(label, labels[index])

    def test_get_server_addresses_nonexistent_network(self):
        url = '/v2/fake/servers/%s/ips/network_0' % FAKE_UUID
        req = self.req(url)
        self.assertRaises(webob.exc.HTTPNotFound, self.ips_controller.show,
                          req, FAKE_UUID, 'network_0')

    def test_get_server_addresses_nonexistent_server(self):
        def fake_instance_get(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id='fake')

        self.stubs.Set(compute_api.API, 'get', fake_instance_get)

        server_id = str(uuid.uuid4())
        req = self.req('/fake/servers/%s/ips' % server_id)
        self.assertRaises(webob.exc.HTTPNotFound,
                          self.ips_controller.index, req, server_id)

    def test_get_server_list_empty(self):
        self.stubs.Set(compute_api.API, 'get_all',
                       return_servers_empty)

        req = self.req('/fake/servers')
        res_dict = self.controller.index(req)

        num_servers = len(res_dict['servers'])
        self.assertEqual(0, num_servers)

    def test_get_server_list_with_reservation_id(self):
        req = self.req('/fake/servers?reservation_id=foo')
        res_dict = self.controller.index(req)

        i = 0
        for s in res_dict['servers']:
            self.assertEqual(s.get('name'), 'server%d' % (i + 1))
            i += 1

    def test_get_server_list_with_reservation_id_empty(self):
        req = self.req('/fake/servers/detail?'
                                      'reservation_id=foo')
        res_dict = self.controller.detail(req)

        i = 0
        for s in res_dict['servers']:
            self.assertEqual(s.get('name'), 'server%d' % (i + 1))
            i += 1

    def test_get_server_list_with_reservation_id_details(self):
        req = self.req('/fake/servers/detail?'
                                      'reservation_id=foo')
        res_dict = self.controller.detail(req)

        i = 0
        for s in res_dict['servers']:
            self.assertEqual(s.get('name'), 'server%d' % (i + 1))
            i += 1

    def test_get_server_list(self):
        req = self.req('/fake/servers')
        res_dict = self.controller.index(req)

        self.assertEqual(len(res_dict['servers']), 5)
        for i, s in enumerate(res_dict['servers']):
            self.assertEqual(s['id'], fakes.get_fake_uuid(i))
            self.assertEqual(s['name'], 'server%d' % (i + 1))
            self.assertIsNone(s.get('image', None))

            expected_links = [
                {
                    "rel": "self",
                    "href": "http://localhost/v2/fake/servers/%s" % s['id'],
                },
                {
                    "rel": "bookmark",
                    "href": "http://localhost/fake/servers/%s" % s['id'],
                },
            ]

            self.assertEqual(s['links'], expected_links)

    def test_get_servers_with_limit(self):
        req = self.req('/fake/servers?limit=3')
        res_dict = self.controller.index(req)

        servers = res_dict['servers']
        self.assertEqual([s['id'] for s in servers],
                [fakes.get_fake_uuid(i) for i in range(len(servers))])

        servers_links = res_dict['servers_links']
        self.assertEqual(servers_links[0]['rel'], 'next')
        href_parts = urlparse.urlparse(servers_links[0]['href'])
        self.assertEqual('/v2/fake/servers', href_parts.path)
        params = urlparse.parse_qs(href_parts.query)
        expected_params = {'limit': ['3'],
                           'marker': [fakes.get_fake_uuid(2)]}
        self.assertThat(params, matchers.DictMatches(expected_params))

    def test_get_servers_with_limit_bad_value(self):
        req = self.req('/fake/servers?limit=aaa')
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_get_server_details_empty(self):
        self.stubs.Set(compute_api.API, 'get_all',
                       return_servers_empty)

        req = self.req('/fake/servers/detail')
        res_dict = self.controller.detail(req)

        num_servers = len(res_dict['servers'])
        self.assertEqual(0, num_servers)

    def test_get_server_details_with_bad_name(self):
        req = self.req('/fake/servers/detail?name=%2Binstance')
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_get_server_details_with_limit(self):
        req = self.req('/fake/servers/detail?limit=3')
        res = self.controller.detail(req)

        servers = res['servers']
        self.assertEqual([s['id'] for s in servers],
                [fakes.get_fake_uuid(i) for i in range(len(servers))])

        servers_links = res['servers_links']
        self.assertEqual(servers_links[0]['rel'], 'next')

        href_parts = urlparse.urlparse(servers_links[0]['href'])
        self.assertEqual('/v2/fake/servers/detail', href_parts.path)
        params = urlparse.parse_qs(href_parts.query)
        expected = {'limit': ['3'], 'marker': [fakes.get_fake_uuid(2)]}
        self.assertThat(params, matchers.DictMatches(expected))

    def test_get_server_details_with_limit_bad_value(self):
        req = self.req('/fake/servers/detail?limit=aaa')
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.detail, req)

    def test_get_server_details_with_limit_and_other_params(self):
        req = self.req('/fake/servers/detail'
                                      '?limit=3&blah=2:t'
                                      '&sort_key=id1&sort_dir=asc')
        res = self.controller.detail(req)

        servers = res['servers']
        self.assertEqual([s['id'] for s in servers],
                [fakes.get_fake_uuid(i) for i in range(len(servers))])

        servers_links = res['servers_links']
        self.assertEqual(servers_links[0]['rel'], 'next')

        href_parts = urlparse.urlparse(servers_links[0]['href'])
        self.assertEqual('/v2/fake/servers/detail', href_parts.path)
        params = urlparse.parse_qs(href_parts.query)
        expected = {'limit': ['3'], 'blah': ['2:t'],
                    'sort_key': ['id1'], 'sort_dir': ['asc'],
                    'marker': [fakes.get_fake_uuid(2)]}
        self.assertThat(params, matchers.DictMatches(expected))

    def test_get_servers_with_too_big_limit(self):
        req = self.req('/fake/servers?limit=30')
        res_dict = self.controller.index(req)
        self.assertNotIn('servers_links', res_dict)

    def test_get_servers_with_bad_limit(self):
        req = self.req('/fake/servers?limit=asdf')
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_get_servers_with_marker(self):
        url = '/v2/fake/servers?marker=%s' % fakes.get_fake_uuid(2)
        req = self.req(url)
        servers = self.controller.index(req)['servers']
        self.assertEqual([s['name'] for s in servers], ["server4", "server5"])

    def test_get_servers_with_limit_and_marker(self):
        url = ('/v2/fake/servers?limit=2&marker=%s' %
               fakes.get_fake_uuid(1))
        req = self.req(url)
        servers = self.controller.index(req)['servers']
        self.assertEqual([s['name'] for s in servers], ['server3', 'server4'])

    def test_get_servers_with_bad_marker(self):
        req = self.req('/fake/servers?limit=2&marker=asdf')
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_get_servers_with_bad_option(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            db_list = [fakes.stub_instance(100, uuid=server_uuid)]
            return instance_obj._make_instance_list(
                context, objects.InstanceList(), db_list, FIELDS)

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?unknownoption=whee')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_image(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('image', search_opts)
            self.assertEqual(search_opts['image'], '12345')
            db_list = [fakes.stub_instance(100, uuid=server_uuid)]
            return instance_obj._make_instance_list(
                context, objects.InstanceList(), db_list, FIELDS)

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?image=12345')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_tenant_id_filter_no_admin_context(self):
        def fake_get_all(context, search_opts=None, **kwargs):
            self.assertNotEqual(search_opts, None)
            self.assertEqual(search_opts['project_id'], 'fake')
            return [fakes.stub_instance_obj(100)]

        req = self.req('/fake/servers?tenant_id=newfake')
        with mock.patch.object(compute_api.API, 'get_all') as mock_get:
            mock_get.side_effect = fake_get_all
            servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_param_normal(self):
        def fake_get_all(context, search_opts=None, **kwargs):
            self.assertNotIn('project_id', search_opts)
            return [fakes.stub_instance_obj(100)]

        req = self.req('/fake/servers?all_tenants',
                                      use_admin_context=True)
        with mock.patch.object(compute_api.API, 'get_all') as mock_get:
            mock_get.side_effect = fake_get_all
            servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_param_one(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertNotIn('project_id', search_opts)
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?all_tenants=1',
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_param_zero(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertNotIn('all_tenants', search_opts)
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?all_tenants=0',
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_param_false(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertNotIn('all_tenants', search_opts)
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?all_tenants=false',
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_param_invalid(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertNotIn('all_tenants', search_opts)
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?all_tenants=xxx',
                                      use_admin_context=True)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_admin_restricted_tenant(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertIsNotNone(search_opts)
            self.assertEqual(search_opts['project_id'], 'fake')
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers',
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_pass_policy(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertIsNotNone(search_opts)
            self.assertNotIn('project_id', search_opts)
            self.assertTrue(context.is_admin)
            return [fakes.stub_instance_obj(100)]

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        rules = {
            "os_compute_api:servers:index": "project_id:fake",
            "os_compute_api:servers:index:get_all_tenants": "project_id:fake"
        }
        policy.set_rules(oslo_policy.Rules.from_dict(rules))

        req = self.req('/fake/servers?all_tenants=1')
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 1)

    def test_all_tenants_fail_policy(self):
        def fake_get_all(api, context, search_opts=None, **kwargs):
            self.assertIsNotNone(search_opts)
            return [fakes.stub_instance_obj(100)]

        rules = {
            "os_compute_api:servers:index:get_all_tenants":
                "project_id:non_fake",
            "os_compute_api:servers:get_all": "project_id:fake",
        }

        policy.set_rules(oslo_policy.Rules.from_dict(rules))
        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?all_tenants=1')
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.index, req)

    def test_get_servers_allows_flavor(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('flavor', search_opts)
            # flavor is an integer ID
            self.assertEqual(search_opts['flavor'], '12345')
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?flavor=12345')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_with_bad_flavor(self):
        req = self.req('/fake/servers?flavor=abcde')
        with mock.patch.object(compute_api.API, 'get_all') as mock_get:
            mock_get.return_value = objects.InstanceList(objects=[])
            servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 0)

    def test_get_server_details_with_bad_flavor(self):
        req = self.req('/fake/servers?flavor=abcde')
        with mock.patch.object(compute_api.API, 'get_all') as mock_get:
            mock_get.return_value = objects.InstanceList(objects=[])
            servers = self.controller.detail(req)['servers']

        self.assertThat(servers, testtools.matchers.HasLength(0))

    def test_get_servers_allows_status(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('vm_state', search_opts)
            self.assertEqual(search_opts['vm_state'], [vm_states.ACTIVE])
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?status=active')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_task_status(self):
        server_uuid = str(uuid.uuid4())
        task_state = task_states.REBOOTING

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('task_state', search_opts)
            self.assertEqual([task_states.REBOOT_PENDING,
                              task_states.REBOOT_STARTED,
                              task_states.REBOOTING],
                             search_opts['task_state'])
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid,
                                                 task_state=task_state)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?status=reboot')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_resize_status(self):
        # Test when resize status, it maps list of vm states.
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIn('vm_state', search_opts)
            self.assertEqual(search_opts['vm_state'],
                             [vm_states.ACTIVE, vm_states.STOPPED])

            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?status=resize')

        servers = self.controller.detail(req)['servers']
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_invalid_status(self):
        # Test getting servers by invalid status.
        req = self.req('/fake/servers?status=baloney',
                                      use_admin_context=False)
        servers = self.controller.index(req)['servers']
        self.assertEqual(len(servers), 0)

    def test_get_servers_deleted_status_as_user(self):
        req = self.req('/fake/servers?status=deleted',
                                      use_admin_context=False)
        self.assertRaises(webob.exc.HTTPForbidden,
                          self.controller.detail, req)

    def test_get_servers_deleted_status_as_admin(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIn('vm_state', search_opts)
            self.assertEqual(search_opts['vm_state'], ['deleted'])

            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?status=deleted',
                                      use_admin_context=True)

        servers = self.controller.detail(req)['servers']
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    @mock.patch.object(compute_api.API, 'get_all')
    def test_get_servers_deleted_filter_str_to_bool(self, mock_get_all):
        server_uuid = str(uuid.uuid4())

        db_list = objects.InstanceList(
            objects=[fakes.stub_instance_obj(100, uuid=server_uuid,
                                             vm_state='deleted')])
        mock_get_all.return_value = db_list

        req = self.req('/fake/servers?deleted=true',
                                        use_admin_context=True)

        servers = self.controller.detail(req)['servers']
        self.assertEqual(1, len(servers))
        self.assertEqual(server_uuid, servers[0]['id'])

        # Assert that 'deleted' filter value is converted to boolean
        # while calling get_all() method.
        expected_search_opts = {'deleted': True, 'project_id': 'fake'}
        self.assertEqual(expected_search_opts,
                         mock_get_all.call_args[1]['search_opts'])

    @mock.patch.object(compute_api.API, 'get_all')
    def test_get_servers_deleted_filter_invalid_str(self, mock_get_all):
        server_uuid = str(uuid.uuid4())

        db_list = objects.InstanceList(
            objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])
        mock_get_all.return_value = db_list

        req = fakes.HTTPRequest.blank('/fake/servers?deleted=abc',
                                      use_admin_context=True)

        servers = self.controller.detail(req)['servers']
        self.assertEqual(1, len(servers))
        self.assertEqual(server_uuid, servers[0]['id'])

        # Assert that invalid 'deleted' filter value is converted to boolean
        # False while calling get_all() method.
        expected_search_opts = {'deleted': False, 'project_id': 'fake'}
        self.assertEqual(expected_search_opts,
                         mock_get_all.call_args[1]['search_opts'])

    def test_get_servers_allows_name(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('name', search_opts)
            self.assertEqual(search_opts['name'], 'whee.*')
            self.assertEqual(['pci_devices'], expected_attrs)
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?name=whee.*')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    @mock.patch.object(compute_api.API, 'get_all')
    def test_get_servers_flavor_not_found(self, get_all_mock):
        get_all_mock.side_effect = exception.FlavorNotFound(flavor_id=1)

        req = fakes.HTTPRequest.blank(
                    '/fake/servers?status=active&flavor=abc')
        servers = self.controller.index(req)['servers']
        self.assertEqual(0, len(servers))

    def test_get_servers_allows_changes_since(self):
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('changes-since', search_opts)
            changes_since = datetime.datetime(2011, 1, 24, 17, 8, 1,
                                              tzinfo=iso8601.iso8601.UTC)
            self.assertEqual(search_opts['changes-since'], changes_since)
            self.assertNotIn('deleted', search_opts)
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        params = 'changes-since=2011-01-24T17:08:01Z'
        req = self.req('/fake/servers?%s' % params)
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_changes_since_bad_value(self):
        params = 'changes-since=asdf'
        req = self.req('/fake/servers?%s' % params)
        self.assertRaises(webob.exc.HTTPBadRequest, self.controller.index, req)

    def test_get_servers_admin_filters_as_user(self):
        """Test getting servers by admin-only or unknown options when
        context is not admin. Make sure the admin and unknown options
        are stripped before they get to compute_api.get_all()
        """
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            # Allowed by user
            self.assertIn('name', search_opts)
            self.assertIn('ip', search_opts)
            # OSAPI converts status to vm_state
            self.assertIn('vm_state', search_opts)
            # Allowed only by admins with admin API on
            self.assertNotIn('unknown_option', search_opts)
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        query_str = "name=foo&ip=10.*&status=active&unknown_option=meow"
        req = fakes.HTTPRequest.blank('/fake/servers?%s' % query_str)
        res = self.controller.index(req)

        servers = res['servers']
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_admin_options_as_admin(self):
        """Test getting servers by admin-only or unknown options when
        context is admin. All options should be passed
        """
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            # Allowed by user
            self.assertIn('name', search_opts)
            # OSAPI converts status to vm_state
            self.assertIn('vm_state', search_opts)
            # Allowed only by admins with admin API on
            self.assertIn('ip', search_opts)
            self.assertIn('unknown_option', search_opts)
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        query_str = "name=foo&ip=10.*&status=active&unknown_option=meow"
        req = self.req('/fake/servers?%s' % query_str,
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_ip(self):
        """Test getting servers by ip."""

        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('ip', search_opts)
            self.assertEqual(search_opts['ip'], '10\..*')
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?ip=10\..*')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_admin_allows_ip6(self):
        """Test getting servers by ip6 with admin_api enabled and
        admin context
        """
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('ip6', search_opts)
            self.assertEqual(search_opts['ip6'], 'ffff.*')
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?ip6=ffff.*',
                                      use_admin_context=True)
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_ip6_with_new_version(self):
        """Test getting servers by ip6 with new version requested
        and no admin context
        """
        server_uuid = str(uuid.uuid4())

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIsNotNone(search_opts)
            self.assertIn('ip6', search_opts)
            self.assertEqual(search_opts['ip6'], 'ffff.*')
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(100, uuid=server_uuid)])

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers?ip6=ffff.*')
        req.api_version_request = api_version_request.APIVersionRequest('2.5')
        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_all_server_details(self):
        expected_flavor = {
                "id": "2",
                "links": [
                    {
                        "rel": "bookmark",
                        "href": 'http://localhost/fake/flavors/2',
                        },
                    ],
                }
        expected_image = {
            "id": "10",
            "links": [
                {
                    "rel": "bookmark",
                    "href": 'http://localhost/fake/images/10',
                    },
                ],
            }
        req = self.req('/fake/servers/detail')
        res_dict = self.controller.detail(req)

        for i, s in enumerate(res_dict['servers']):
            self.assertEqual(s['id'], fakes.get_fake_uuid(i))
            self.assertEqual(s['hostId'], '')
            self.assertEqual(s['name'], 'server%d' % (i + 1))
            self.assertEqual(s['image'], expected_image)
            self.assertEqual(s['flavor'], expected_flavor)
            self.assertEqual(s['status'], 'ACTIVE')
            self.assertEqual(s['metadata']['seq'], str(i + 1))

    def test_get_all_server_details_with_host(self):
        """We want to make sure that if two instances are on the same host,
        then they return the same hostId. If two instances are on different
        hosts, they should return different hostIds. In this test,
        there are 5 instances - 2 on one host and 3 on another.
        """

        def return_servers_with_host(*args, **kwargs):
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(None,
                                                 id=i + 1,
                                                 user_id='fake',
                                                 project_id='fake',
                                                 host=i % 2,
                                                 uuid=fakes.get_fake_uuid(i))
                    for i in range(5)])

        self.stubs.Set(compute_api.API, 'get_all', return_servers_with_host)

        req = self.req('/fake/servers/detail')
        res_dict = self.controller.detail(req)

        server_list = res_dict['servers']
        host_ids = [server_list[0]['hostId'], server_list[1]['hostId']]
        self.assertTrue(host_ids[0] and host_ids[1])
        self.assertNotEqual(host_ids[0], host_ids[1])

        for i, s in enumerate(server_list):
            self.assertEqual(s['id'], fakes.get_fake_uuid(i))
            self.assertEqual(s['hostId'], host_ids[i % 2])
            self.assertEqual(s['name'], 'server%d' % (i + 1))

    def test_get_servers_joins_pci_devices(self):

        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertEqual(['pci_devices'], expected_attrs)
            return []

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers', use_admin_context=True)
        self.assertIn('servers', self.controller.index(req))

    def test_get_servers_joins_services(self):
        def fake_get_all(compute_self, context, search_opts=None,
                         limit=None, marker=None,
                         expected_attrs=None, sort_keys=None, sort_dirs=None):
            self.assertIn('services', expected_attrs)
            return objects.InstanceList()

        self.stubs.Set(compute_api.API, 'get_all', fake_get_all)

        req = self.req('/fake/servers/detail', use_admin_context=True)
        self.assertIn('servers', self.controller.detail(req))


class ServersControllerTestV29(ServersControllerTest):
    wsgi_api_version = '2.9'

    def _get_server_data_dict(self, uuid, image_bookmark, flavor_bookmark,
                              status="ACTIVE", progress=100):
        server_dict = super(ServersControllerTestV29,
                            self)._get_server_data_dict(uuid,
                                                        image_bookmark,
                                                        flavor_bookmark,
                                                        status,
                                                        progress)
        server_dict['server']['locked'] = False
        return server_dict

    @mock.patch.object(compute_api.API, 'get')
    def _test_get_server_with_lock(self, locked_by, get_mock):
        image_bookmark = "http://localhost/fake/images/10"
        flavor_bookmark = "http://localhost/fake/flavors/2"
        uuid = FAKE_UUID
        get_mock.side_effect = fakes.fake_compute_get(id=2,
                                                      locked_by=locked_by,
                                                      uuid=uuid)

        req = self.req('/fake/servers/%s' % uuid)
        res_dict = self.controller.show(req, uuid)

        expected_server = self._get_server_data_dict(uuid,
                                                     image_bookmark,
                                                     flavor_bookmark,
                                                     progress=0)
        expected_server['server']['locked'] = True if locked_by else False
        self.assertThat(res_dict, matchers.DictMatches(expected_server))
        return res_dict

    def test_get_server_with_locked_by_admin(self):
        res_dict = self._test_get_server_with_lock('admin')
        self.assertTrue(res_dict['server']['locked'])

    def test_get_server_with_locked_by_owner(self):
        res_dict = self._test_get_server_with_lock('owner')
        self.assertTrue(res_dict['server']['locked'])

    def test_get_server_not_locked(self):
        res_dict = self._test_get_server_with_lock(None)
        self.assertFalse(res_dict['server']['locked'])

    @mock.patch.object(compute_api.API, 'get_all')
    def _test_list_server_detail_with_lock(self,
                                           s1_locked,
                                           s2_locked,
                                           get_all_mock):
        get_all_mock.return_value = fake_instance_get_all_with_locked(
                                        context, [s1_locked, s2_locked])
        req = self.req('/fake/servers/detail')
        servers_list = self.controller.detail(req)
        # Check that each returned server has the same 'locked' value
        # and 'id' as they were created.
        for locked in [s1_locked, s2_locked]:
            server = next(server for server in servers_list['servers']
                          if (server['id'] == fakes.get_fake_uuid(locked)))
            expected = False if locked == 'not_locked' else True
            self.assertEqual(expected, server['locked'])

    def test_list_server_detail_with_locked_s1_admin_s2_owner(self):
        self._test_list_server_detail_with_lock('admin', 'owner')

    def test_list_server_detail_with_locked_s1_owner_s2_admin(self):
        self._test_list_server_detail_with_lock('owner', 'admin')

    def test_list_server_detail_with_locked_s1_admin_s2_admin(self):
        self._test_list_server_detail_with_lock('admin', 'admin')

    def test_list_server_detail_with_locked_s1_admin_s2_not_locked(self):
        self._test_list_server_detail_with_lock('admin', 'not_locked')

    def test_list_server_detail_with_locked_s1_s2_not_locked(self):
        self._test_list_server_detail_with_lock('not_locked',
                                                'not_locked')

    @mock.patch.object(compute_api.API, 'get_all')
    def test_get_servers_remove_non_search_options(self, get_all_mock):
        req = fakes.HTTPRequestV21.blank('/servers'
                                         '?sort_key=id1&sort_dir=asc'
                                         '&sort_key=id2&sort_dir=desc'
                                         '&limit=1&marker=123',
                                         use_admin_context=True)
        self.controller.index(req)
        kwargs = get_all_mock.call_args[1]
        search_opts = kwargs['search_opts']
        for key in ('sort_key', 'sort_dir', 'limit', 'marker'):
            self.assertNotIn(key, search_opts)


class ServersControllerTestV219(ServersControllerTest):
    wsgi_api_version = '2.19'

    def _get_server_data_dict(self, uuid, image_bookmark, flavor_bookmark,
                              status="ACTIVE", progress=100, description=None):
        server_dict = super(ServersControllerTestV219,
                            self)._get_server_data_dict(uuid,
                                                        image_bookmark,
                                                        flavor_bookmark,
                                                        status,
                                                        progress)
        server_dict['server']['locked'] = False
        server_dict['server']['description'] = description
        return server_dict

    @mock.patch.object(compute_api.API, 'get')
    def _test_get_server_with_description(self, description, get_mock):
        image_bookmark = "http://localhost/fake/images/10"
        flavor_bookmark = "http://localhost/fake/flavors/2"
        uuid = FAKE_UUID
        get_mock.side_effect = fakes.fake_compute_get(id=2,
                                              display_description=description,
                                              uuid=uuid)

        req = self.req('/fake/servers/%s' % uuid)
        res_dict = self.controller.show(req, uuid)

        expected_server = self._get_server_data_dict(uuid,
                                                     image_bookmark,
                                                     flavor_bookmark,
                                                     progress=0,
                                                     description=description)
        self.assertThat(res_dict, matchers.DictMatches(expected_server))
        return res_dict

    @mock.patch.object(compute_api.API, 'get_all')
    def _test_list_server_detail_with_descriptions(self,
                                           s1_desc,
                                           s2_desc,
                                           get_all_mock):
        get_all_mock.return_value = fake_instance_get_all_with_description(
                                        context, [s1_desc, s2_desc])
        req = self.req('/fake/servers/detail')
        servers_list = self.controller.detail(req)
        # Check that each returned server has the same 'description' value
        # and 'id' as they were created.
        for desc in [s1_desc, s2_desc]:
            server = next(server for server in servers_list['servers']
                          if (server['id'] == fakes.get_fake_uuid(desc)))
            expected = desc
            self.assertEqual(expected, server['description'])

    def test_get_server_with_description(self):
        self._test_get_server_with_description('test desc')

    def test_list_server_detail_with_descriptions(self):
        self._test_list_server_detail_with_descriptions('desc1', 'desc2')


class ServersControllerTestV226(ControllerTest):
    wsgi_api_version = '2.26'

    @mock.patch.object(compute_api.API, 'get')
    def test_get_server_with_tags_by_id(self, mock_get):
        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID,
                                      version=self.wsgi_api_version)
        ctxt = req.environ['nova.context']
        tags = ['tag1', 'tag2']

        def fake_get(_self, *args, **kwargs):
            self.assertIn('tags', kwargs['expected_attrs'])
            fake_server = fakes.stub_instance_obj(
                ctxt, id=2, vm_state=vm_states.ACTIVE, progress=100)

            tag_list = objects.TagList(objects=[
                objects.Tag(resource_id=FAKE_UUID, tag=tag)
                for tag in tags])

            fake_server.tags = tag_list
            return fake_server

        mock_get.side_effect = fake_get

        res_dict = self.controller.show(req, FAKE_UUID)

        self.assertIn('tags', res_dict['server'])
        self.assertEqual(res_dict['server']['tags'], tags)

    @mock.patch.object(compute_api.API, 'get_all')
    def _test_get_servers_allows_tag_filters(self, filter_name, mock_get_all):
        server_uuid = str(uuid.uuid4())
        req = fakes.HTTPRequest.blank('/fake/servers?%s=t1,t2' % filter_name,
                                      version=self.wsgi_api_version)
        ctxt = req.environ['nova.context']

        def fake_get_all(*a, **kw):
            self.assertIsNotNone(kw['search_opts'])
            self.assertIn(filter_name, kw['search_opts'])
            self.assertEqual(kw['search_opts'][filter_name], ['t1', 't2'])
            return objects.InstanceList(
                objects=[fakes.stub_instance_obj(ctxt, uuid=server_uuid)])

        mock_get_all.side_effect = fake_get_all

        servers = self.controller.index(req)['servers']

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]['id'], server_uuid)

    def test_get_servers_allows_tags_filter(self):
        self._test_get_servers_allows_tag_filters('tags')

    def test_get_servers_allows_tags_any_filter(self):
        self._test_get_servers_allows_tag_filters('tags-any')

    def test_get_servers_allows_not_tags_filter(self):
        self._test_get_servers_allows_tag_filters('not-tags')

    def test_get_servers_allows_not_tags_any_filter(self):
        self._test_get_servers_allows_tag_filters('not-tags-any')


class ServerControllerTestV238(ControllerTest):
    wsgi_api_version = '2.38'

    def _test_invalid_status(self, is_admin):
        req = fakes.HTTPRequest.blank('/fake/servers/detail?status=invalid',
                                      version=self.wsgi_api_version,
                                      use_admin_context=is_admin)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.detail, req)

    def test_list_servers_detail_invalid_status_for_admin(self):
        self._test_invalid_status(True)

    def test_list_servers_detail_invalid_status_for_non_admin(self):
        self._test_invalid_status(False)


class ServersControllerDeleteTest(ControllerTest):

    def setUp(self):
        super(ServersControllerDeleteTest, self).setUp()
        self.server_delete_called = False

        def fake_delete(api, context, instance):
            if instance.uuid == uuids.non_existent_uuid:
                raise exception.InstanceNotFound(instance_id=instance.uuid)
            self.server_delete_called = True

        self.stubs.Set(compute_api.API, 'delete', fake_delete)

    def _create_delete_request(self, uuid):
        fakes.stub_out_instance_quota(self, 0, 10)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s' % uuid)
        req.method = 'DELETE'
        fake_get = fakes.fake_compute_get(
            uuid=uuid,
            vm_state=vm_states.ACTIVE,
            project_id=req.environ['nova.context'].project_id,
            user_id=req.environ['nova.context'].user_id)
        self.stub_out('nova.compute.api.API.get',
                      lambda api, *a, **k: fake_get(*a, **k))
        return req

    def _delete_server_instance(self, uuid=FAKE_UUID):
        req = self._create_delete_request(uuid)
        self.controller.delete(req, uuid)

    def test_delete_server_instance(self):
        self._delete_server_instance()
        self.assertTrue(self.server_delete_called)

    def test_delete_server_instance_not_found(self):
        self.assertRaises(webob.exc.HTTPNotFound,
                          self._delete_server_instance,
                          uuid=uuids.non_existent_uuid)

    def test_delete_server_instance_while_building(self):
        req = self._create_delete_request(FAKE_UUID)
        self.controller.delete(req, FAKE_UUID)

        self.assertTrue(self.server_delete_called)

    def test_delete_locked_server(self):
        req = self._create_delete_request(FAKE_UUID)
        self.stubs.Set(compute_api.API, 'soft_delete',
                       fakes.fake_actions_to_locked_server)
        self.stubs.Set(compute_api.API, 'delete',
                       fakes.fake_actions_to_locked_server)

        self.assertRaises(webob.exc.HTTPConflict, self.controller.delete,
                          req, FAKE_UUID)

    def test_delete_server_instance_while_resize(self):
        req = self._create_delete_request(FAKE_UUID)
        fake_get = fakes.fake_compute_get(
            vm_state=vm_states.ACTIVE,
            task_state=task_states.RESIZE_PREP,
            project_id=req.environ['nova.context'].project_id,
            user_id=req.environ['nova.context'].user_id)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: fake_get(*a, **k))

        self.controller.delete(req, FAKE_UUID)

    def test_delete_server_instance_if_not_launched(self):
        self.flags(reclaim_instance_interval=3600)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'DELETE'

        self.server_delete_called = False

        fake_get = fakes.fake_compute_get(
            launched_at=None,
            project_id=req.environ['nova.context'].project_id,
            user_id=req.environ['nova.context'].user_id)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: fake_get(*a, **k))

        def instance_destroy_mock(*args, **kwargs):
            self.server_delete_called = True
            deleted_at = timeutils.utcnow()
            return fake_instance.fake_db_instance(deleted_at=deleted_at)
        self.stub_out('nova.db.instance_destroy', instance_destroy_mock)

        self.controller.delete(req, FAKE_UUID)
        # delete() should be called for instance which has never been active,
        # even if reclaim_instance_interval has been set.
        self.assertTrue(self.server_delete_called)


class ServersControllerRebuildInstanceTest(ControllerTest):

    image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'

    def setUp(self):
        super(ServersControllerRebuildInstanceTest, self).setUp()
        self.req = fakes.HTTPRequest.blank('/fake/servers/a/action')
        self.req.method = 'POST'
        self.req.headers["content-type"] = "application/json"
        self.req_user_id = self.req.environ['nova.context'].user_id
        self.req_project_id = self.req.environ['nova.context'].project_id

        def fake_get(ctrl, ctxt, uuid):
            if uuid == 'test_inst':
                raise webob.exc.HTTPNotFound(explanation='fakeout')
            return fakes.stub_instance_obj(None,
                                           vm_state=vm_states.ACTIVE,
                                           project_id=self.req_project_id,
                                           user_id=self.req_user_id)

        self.useFixture(
            fixtures.MonkeyPatch('nova.api.openstack.compute.servers.'
                                 'ServersController._get_instance',
                                 fake_get))
        fake_get = fakes.fake_compute_get(vm_state=vm_states.ACTIVE,
                                          project_id=self.req_project_id,
                                          user_id=self.req_user_id)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: fake_get(*a, **k))

        self.body = {
            'rebuild': {
                'name': 'new_name',
                'imageRef': self.image_uuid,
                'metadata': {
                    'open': 'stack',
                },
            },
        }

    def test_rebuild_server_with_image_not_uuid(self):
        self.body['rebuild']['imageRef'] = 'not-uuid'
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID,
                          body=self.body)

    def test_rebuild_server_with_image_as_full_url(self):
        image_href = ('http://localhost/v2/fake/images/'
            '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6')
        self.body['rebuild']['imageRef'] = image_href
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID,
                          body=self.body)

    def test_rebuild_server_with_image_as_empty_string(self):
        self.body['rebuild']['imageRef'] = ''
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID,
                          body=self.body)

    def test_rebuild_instance_name_with_spaces_in_the_middle(self):
        self.body['rebuild']['name'] = 'abc   def'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.controller._action_rebuild(self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_name_with_leading_trailing_spaces(self):
        self.body['rebuild']['name'] = '  abc   def  '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_name_with_leading_trailing_spaces_compat_mode(
            self):
        self.body['rebuild']['name'] = '  abc  def  '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.set_legacy_v2()

        def fake_rebuild(*args, **kwargs):
            self.assertEqual('abc  def', kwargs['display_name'])

        with mock.patch.object(compute_api.API, 'rebuild') as mock_rebuild:
            mock_rebuild.side_effect = fake_rebuild
            self.controller._action_rebuild(self.req, FAKE_UUID,
                                            body=self.body)

    def test_rebuild_instance_with_blank_metadata_key(self):
        self.body['rebuild']['metadata'][''] = 'world'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_with_metadata_key_too_long(self):
        self.body['rebuild']['metadata'][('a' * 260)] = 'world'

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_with_metadata_value_too_long(self):
        self.body['rebuild']['metadata']['key1'] = ('a' * 260)

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild, self.req,
                          FAKE_UUID, body=self.body)

    def test_rebuild_instance_with_metadata_value_not_string(self):
        self.body['rebuild']['metadata']['key1'] = 1

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild, self.req,
                          FAKE_UUID, body=self.body)

    def test_rebuild_instance_fails_when_min_ram_too_small(self):
        # make min_ram larger than our instance ram size
        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True,
                        status='active', properties={'key1': 'value1'},
                        min_ram="4096", min_disk="10")

        self.stubs.Set(fake._FakeImageService, 'show', fake_get_image)

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_fails_when_min_disk_too_small(self):
        # make min_disk larger than our instance disk size
        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True,
                        status='active', properties={'key1': 'value1'},
                        min_ram="128", min_disk="100000")

        self.stubs.Set(fake._FakeImageService, 'show', fake_get_image)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller._action_rebuild, self.req,
                          FAKE_UUID, body=self.body)

    def test_rebuild_instance_image_too_large(self):
        # make image size larger than our instance disk size
        size = str(1000 * (1024 ** 3))

        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True,
                        status='active', size=size)

        self.stubs.Set(fake._FakeImageService, 'show', fake_get_image)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_name_all_blank(self):
        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True, status='active')

        self.stubs.Set(fake._FakeImageService, 'show', fake_get_image)
        self.body['rebuild']['name'] = '     '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_with_deleted_image(self):
        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True,
                        status='DELETED')

        self.stubs.Set(fake._FakeImageService, 'show', fake_get_image)

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_instance_onset_file_limit_over_quota(self):
        def fake_get_image(self, context, image_href, **kwargs):
            return dict(id='76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                        name='public image', is_public=True, status='active')

        with test.nested(
            mock.patch.object(fake._FakeImageService, 'show',
                              side_effect=fake_get_image),
            mock.patch.object(self.controller.compute_api, 'rebuild',
                              side_effect=exception.OnsetFileLimitExceeded)
        ) as (
            show_mock, rebuild_mock
        ):
            self.req.body = jsonutils.dump_as_bytes(self.body)
            self.assertRaises(webob.exc.HTTPForbidden,
                              self.controller._action_rebuild,
                              self.req, FAKE_UUID, body=self.body)

    def test_rebuild_bad_personality(self):
        body = {
            "rebuild": {
                "imageRef": self.image_uuid,
                "personality": [{
                    "path": "/path/to/file",
                    "contents": "INVALID b64",
                }]
            },
        }

        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=body)

    def test_rebuild_personality(self):
        body = {
            "rebuild": {
                "imageRef": self.image_uuid,
                "personality": [{
                    "path": "/path/to/file",
                    "contents": base64.b64encode("Test String"),
                }]
            },
        }

        body = self.controller._action_rebuild(self.req, FAKE_UUID,
                                               body=body).obj

        self.assertNotIn('personality', body['server'])

    def test_start(self):
        self.mox.StubOutWithMock(compute_api.API, 'start')
        compute_api.API.start(mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()

        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(start="")
        self.controller._start_server(req, FAKE_UUID, body)

    def test_start_not_ready(self):
        self.stubs.Set(compute_api.API, 'start', fake_start_stop_not_ready)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(start="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._start_server, req, FAKE_UUID, body)

    def test_start_locked_server(self):
        self.stubs.Set(compute_api.API, 'start',
                       fakes.fake_actions_to_locked_server)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(start="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._start_server, req, FAKE_UUID, body)

    def test_start_invalid(self):
        self.stubs.Set(compute_api.API, 'start', fake_start_stop_invalid_state)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(start="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._start_server, req, FAKE_UUID, body)

    def test_stop(self):
        self.mox.StubOutWithMock(compute_api.API, 'stop')
        compute_api.API.stop(mox.IgnoreArg(), mox.IgnoreArg())
        self.mox.ReplayAll()

        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(stop="")
        self.controller._stop_server(req, FAKE_UUID, body)

    def test_stop_not_ready(self):
        self.stubs.Set(compute_api.API, 'stop', fake_start_stop_not_ready)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(stop="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._stop_server, req, FAKE_UUID, body)

    def test_stop_locked_server(self):
        self.stubs.Set(compute_api.API, 'stop',
                       fakes.fake_actions_to_locked_server)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(stop="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._stop_server, req, FAKE_UUID, body)

    def test_stop_invalid_state(self):
        self.stubs.Set(compute_api.API, 'stop', fake_start_stop_invalid_state)
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s/action' % FAKE_UUID)
        body = dict(start="")
        self.assertRaises(webob.exc.HTTPConflict,
            self.controller._stop_server, req, FAKE_UUID, body)

    def test_start_with_bogus_id(self):
        self.stub_out('nova.db.instance_get_by_uuid',
                      fake_instance_get_by_uuid_not_found)
        req = fakes.HTTPRequestV21.blank('/fake/servers/test_inst/action')
        body = dict(start="")
        self.assertRaises(webob.exc.HTTPNotFound,
            self.controller._start_server, req, 'test_inst', body)

    def test_stop_with_bogus_id(self):
        self.stub_out('nova.db.instance_get_by_uuid',
                      fake_instance_get_by_uuid_not_found)
        req = fakes.HTTPRequestV21.blank('/fake/servers/test_inst/action')
        body = dict(stop="")
        self.assertRaises(webob.exc.HTTPNotFound,
            self.controller._stop_server, req, 'test_inst', body)


class ServersControllerRebuildTestV219(ServersControllerRebuildInstanceTest):

    def setUp(self):
        super(ServersControllerRebuildTestV219, self).setUp()
        self.req.api_version_request = \
            api_version_request.APIVersionRequest('2.19')

    def _rebuild_server(self, set_desc, desc):
        fake_get = fakes.fake_compute_get(vm_state=vm_states.ACTIVE,
                                          display_description=desc,
                                          project_id=self.req_project_id,
                                          user_id=self.req_user_id)
        self.stubs.Set(compute_api.API, 'get',
                       lambda api, *a, **k: fake_get(*a, **k))

        if set_desc:
            self.body['rebuild']['description'] = desc
        self.req.body = jsonutils.dump_as_bytes(self.body)
        server = self.controller._action_rebuild(self.req, FAKE_UUID,
                                                 body=self.body).obj['server']
        self.assertEqual(server['id'], FAKE_UUID)
        self.assertEqual(server['description'], desc)

    def test_rebuild_server_with_description(self):
        self._rebuild_server(True, 'server desc')

    def test_rebuild_server_empty_description(self):
        self._rebuild_server(True, '')

    def test_rebuild_server_without_description(self):
        self._rebuild_server(False, '')

    def test_rebuild_server_remove_description(self):
        self._rebuild_server(True, None)

    def test_rebuild_server_description_too_long(self):
        self.body['rebuild']['description'] = 'x' * 256
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)

    def test_rebuild_server_description_invalid(self):
        # Invalid non-printable control char in the desc.
        self.body['rebuild']['description'] = "123\0d456"
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller._action_rebuild,
                          self.req, FAKE_UUID, body=self.body)


class ServersControllerUpdateTest(ControllerTest):

    def _get_request(self, body=None):
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = 'application/json'
        req.body = jsonutils.dump_as_bytes(body)
        fake_get = fakes.fake_compute_get(
            project_id=req.environ['nova.context'].project_id,
            user_id=req.environ['nova.context'].user_id)
        self.stub_out('nova.compute.api.API.get',
                      lambda api, *a, **k: fake_get(*a, **k))
        return req

    def test_update_server_all_attributes(self):
        body = {'server': {
                  'name': 'server_test',
               }}
        req = self._get_request(body)
        res_dict = self.controller.update(req, FAKE_UUID, body=body)

        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertEqual(res_dict['server']['name'], 'server_test')

    def test_update_server_name(self):
        body = {'server': {'name': 'server_test'}}
        req = self._get_request(body)
        res_dict = self.controller.update(req, FAKE_UUID, body=body)

        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertEqual(res_dict['server']['name'], 'server_test')

    def test_update_server_name_too_long(self):
        body = {'server': {'name': 'x' * 256}}
        req = self._get_request(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_name_all_blank_spaces(self):
        self.stub_out('nova.db.instance_get',
                fakes.fake_instance_get(name='server_test'))
        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = 'application/json'
        body = {'server': {'name': ' ' * 64}}
        req.body = jsonutils.dump_as_bytes(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_name_with_spaces_in_the_middle(self):
        body = {'server': {'name': 'abc   def'}}
        req = self._get_request(body)
        self.controller.update(req, FAKE_UUID, body=body)

    def test_update_server_name_with_leading_trailing_spaces(self):
        self.stub_out('nova.db.instance_get',
                fakes.fake_instance_get(name='server_test'))
        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = 'application/json'
        body = {'server': {'name': '  abc   def  '}}
        req.body = jsonutils.dump_as_bytes(body)
        self.assertRaises(exception.ValidationError,
                          self.controller.update, req, FAKE_UUID, body=body)

    def test_update_server_name_with_leading_trailing_spaces_compat_mode(self):
        body = {'server': {'name': '  abc   def  '}}
        req = self._get_request(body)
        req.set_legacy_v2()
        self.controller.update(req, FAKE_UUID, body=body)

    def test_update_server_admin_password_extra_arg(self):
        inst_dict = dict(name='server_test', admin_password='bacon')
        body = dict(server=inst_dict)

        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = "application/json"
        req.body = jsonutils.dump_as_bytes(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_host_id(self):
        inst_dict = dict(host_id='123')
        body = dict(server=inst_dict)

        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = "application/json"
        req.body = jsonutils.dump_as_bytes(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_not_found(self):
        def fake_get(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id='fake')

        self.stubs.Set(compute_api.API, 'get', fake_get)
        body = {'server': {'name': 'server_test'}}
        req = fakes.HTTPRequest.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'
        req.content_type = "application/json"
        req.body = jsonutils.dump_as_bytes(body)
        self.assertRaises(webob.exc.HTTPNotFound, self.controller.update,
                          req, FAKE_UUID, body=body)

    @mock.patch.object(compute_api.API, 'update_instance')
    def test_update_server_not_found_on_update(self, mock_update_instance):
        def fake_update(*args, **kwargs):
            raise exception.InstanceNotFound(instance_id='fake')

        mock_update_instance.side_effect = fake_update
        body = {'server': {'name': 'server_test'}}
        req = self._get_request(body)
        self.assertRaises(webob.exc.HTTPNotFound, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_policy_fail(self):
        rule = {'compute:update': 'role:admin'}
        policy.set_rules(oslo_policy.Rules.from_dict(rule))
        body = {'server': {'name': 'server_test'}}
        req = self._get_request(body)
        self.assertRaises(exception.PolicyNotAuthorized,
                self.controller.update, req, FAKE_UUID, body=body)


class ServersControllerTriggerCrashDumpTest(ControllerTest):

    def setUp(self):
        super(ServersControllerTriggerCrashDumpTest, self).setUp()

        self.instance = fakes.stub_instance_obj(None,
                                                vm_state=vm_states.ACTIVE,
                                                project_id='fake')

        def fake_get(ctrl, ctxt, uuid):
            if uuid != FAKE_UUID:
                raise webob.exc.HTTPNotFound(explanation='fakeout')
            return self.instance

        self.useFixture(
            fixtures.MonkeyPatch('nova.api.openstack.compute.servers.'
                                 'ServersController._get_instance',
                                 fake_get))

        self.req = fakes.HTTPRequest.blank('/servers/%s/action' % FAKE_UUID)
        self.req.api_version_request =\
            api_version_request.APIVersionRequest('2.17')
        self.body = dict(trigger_crash_dump=None)

    @mock.patch.object(compute_api.API, 'trigger_crash_dump')
    def test_trigger_crash_dump(self, mock_trigger_crash_dump):
        ctxt = self.req.environ['nova.context']
        self.controller._action_trigger_crash_dump(self.req, FAKE_UUID,
                                                   body=self.body)
        mock_trigger_crash_dump.assert_called_with(ctxt, self.instance)

    def test_trigger_crash_dump_policy_failed(self):
        rule_name = "os_compute_api:servers:trigger_crash_dump"
        self.policy.set_rules({rule_name: "project_id:non_fake"})
        exc = self.assertRaises(exception.PolicyNotAuthorized,
                                self.controller._action_trigger_crash_dump,
                                self.req, FAKE_UUID, body=self.body)
        self.assertIn("os_compute_api:servers:trigger_crash_dump",
                      exc.format_message())

    @mock.patch.object(compute_api.API, 'trigger_crash_dump',
                       fake_start_stop_not_ready)
    def test_trigger_crash_dump_not_ready(self):
        self.assertRaises(webob.exc.HTTPConflict,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)

    @mock.patch.object(compute_api.API, 'trigger_crash_dump',
                       fakes.fake_actions_to_locked_server)
    def test_trigger_crash_dump_locked_server(self):
        self.assertRaises(webob.exc.HTTPConflict,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)

    @mock.patch.object(compute_api.API, 'trigger_crash_dump',
                       fake_start_stop_invalid_state)
    def test_trigger_crash_dump_invalid_state(self):
        self.assertRaises(webob.exc.HTTPConflict,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)

    def test_trigger_crash_dump_with_bogus_id(self):
        self.assertRaises(webob.exc.HTTPNotFound,
                          self.controller._action_trigger_crash_dump,
                          self.req, 'test_inst', body=self.body)

    def test_trigger_crash_dump_schema_invalid_type(self):
        self.body['trigger_crash_dump'] = 'not null'
        self.assertRaises(exception.ValidationError,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)

    def test_trigger_crash_dump_schema_extra_property(self):
        self.body['extra_property'] = 'extra'
        self.assertRaises(exception.ValidationError,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)

    @mock.patch.object(compute_api.API, 'trigger_crash_dump',
                       side_effect=exception.TriggerCrashDumpNotSupported)
    def test_trigger_crash_dump_not_supported(self, mock_trigger_crash_dump):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller._action_trigger_crash_dump,
                          self.req, FAKE_UUID, body=self.body)


class ServersControllerUpdateTestV219(ServersControllerUpdateTest):
    def _get_request(self, body=None):
        req = super(ServersControllerUpdateTestV219, self)._get_request(
            body=body)
        req.api_version_request = api_version_request.APIVersionRequest('2.19')
        return req

    def _update_server_desc(self, set_desc, desc=None):
        body = {'server': {}}
        if set_desc:
            body['server']['description'] = desc
        req = self._get_request()
        res_dict = self.controller.update(req, FAKE_UUID, body=body)
        return res_dict

    def test_update_server_description(self):
        res_dict = self._update_server_desc(True, 'server_desc')
        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertEqual(res_dict['server']['description'], 'server_desc')

    def test_update_server_empty_description(self):
        res_dict = self._update_server_desc(True, '')
        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertEqual(res_dict['server']['description'], '')

    def test_update_server_without_description(self):
        res_dict = self._update_server_desc(False)
        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertIsNone(res_dict['server']['description'])

    def test_update_server_remove_description(self):
        res_dict = self._update_server_desc(True)
        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertIsNone(res_dict['server']['description'])

    def test_update_server_all_attributes(self):
        body = {'server': {
                  'name': 'server_test',
                  'description': 'server_desc'
               }}
        req = self._get_request(body)
        res_dict = self.controller.update(req, FAKE_UUID, body=body)

        self.assertEqual(res_dict['server']['id'], FAKE_UUID)
        self.assertEqual(res_dict['server']['name'], 'server_test')
        self.assertEqual(res_dict['server']['description'], 'server_desc')

    def test_update_server_description_too_long(self):
        body = {'server': {'description': 'x' * 256}}
        req = self._get_request(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)

    def test_update_server_description_invalid(self):
        # Invalid non-printable control char in the desc.
        body = {'server': {'description': "123\0d456"}}
        req = self._get_request(body)
        self.assertRaises(exception.ValidationError, self.controller.update,
                          req, FAKE_UUID, body=body)


class ServerStatusTest(test.TestCase):

    def setUp(self):
        super(ServerStatusTest, self).setUp()
        fakes.stub_out_nw_api(self)

        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)

    def _get_with_state(self, vm_state, task_state=None):
        self.stub_out('nova.db.instance_get_by_uuid',
                fakes.fake_instance_get(vm_state=vm_state,
                                        task_state=task_state))

        request = fakes.HTTPRequestV21.blank('/fake/servers/%s' % FAKE_UUID)
        return self.controller.show(request, FAKE_UUID)

    def test_active(self):
        response = self._get_with_state(vm_states.ACTIVE)
        self.assertEqual(response['server']['status'], 'ACTIVE')

    def test_reboot(self):
        response = self._get_with_state(vm_states.ACTIVE,
                                        task_states.REBOOTING)
        self.assertEqual(response['server']['status'], 'REBOOT')

    def test_reboot_hard(self):
        response = self._get_with_state(vm_states.ACTIVE,
                                        task_states.REBOOTING_HARD)
        self.assertEqual(response['server']['status'], 'HARD_REBOOT')

    def test_reboot_resize_policy_fail(self):
        def fake_get_server(context, req, id):
            return fakes.stub_instance(id)

        self.stubs.Set(self.controller, '_get_server', fake_get_server)

        rule = {'compute:reboot': 'role:admin'}
        policy.set_rules(oslo_policy.Rules.from_dict(rule))
        req = fakes.HTTPRequestV21.blank('/fake/servers/1234/action')
        self.assertRaises(exception.PolicyNotAuthorized,
                self.controller._action_reboot, req, '1234',
                body={'reboot': {'type': 'HARD'}})

    def test_rebuild(self):
        response = self._get_with_state(vm_states.ACTIVE,
                                        task_states.REBUILDING)
        self.assertEqual(response['server']['status'], 'REBUILD')

    def test_rebuild_error(self):
        response = self._get_with_state(vm_states.ERROR)
        self.assertEqual(response['server']['status'], 'ERROR')

    def test_resize(self):
        response = self._get_with_state(vm_states.ACTIVE,
                                        task_states.RESIZE_PREP)
        self.assertEqual(response['server']['status'], 'RESIZE')

    def test_confirm_resize_policy_fail(self):
        def fake_get_server(context, req, id):
            return fakes.stub_instance(id)

        self.stubs.Set(self.controller, '_get_server', fake_get_server)

        rule = {'compute:confirm_resize': 'role:admin'}
        policy.set_rules(oslo_policy.Rules.from_dict(rule))
        req = fakes.HTTPRequestV21.blank('/fake/servers/1234/action')
        self.assertRaises(exception.PolicyNotAuthorized,
                self.controller._action_confirm_resize, req, '1234', {})

    def test_verify_resize(self):
        response = self._get_with_state(vm_states.RESIZED, None)
        self.assertEqual(response['server']['status'], 'VERIFY_RESIZE')

    def test_revert_resize(self):
        response = self._get_with_state(vm_states.RESIZED,
                                        task_states.RESIZE_REVERTING)
        self.assertEqual(response['server']['status'], 'REVERT_RESIZE')

    def test_revert_resize_policy_fail(self):
        def fake_get_server(context, req, id):
            return fakes.stub_instance(id)

        self.stubs.Set(self.controller, '_get_server', fake_get_server)

        rule = {'compute:revert_resize': 'role:admin'}
        policy.set_rules(oslo_policy.Rules.from_dict(rule))
        req = fakes.HTTPRequestV21.blank('/fake/servers/1234/action')
        self.assertRaises(exception.PolicyNotAuthorized,
                self.controller._action_revert_resize, req, '1234', {})

    def test_password_update(self):
        response = self._get_with_state(vm_states.ACTIVE,
                                        task_states.UPDATING_PASSWORD)
        self.assertEqual(response['server']['status'], 'PASSWORD')

    def test_stopped(self):
        response = self._get_with_state(vm_states.STOPPED)
        self.assertEqual(response['server']['status'], 'SHUTOFF')


class ServersControllerCreateTest(test.TestCase):
    image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
    flavor_ref = 'http://localhost/123/flavors/3'

    def setUp(self):
        """Shared implementation for tests below that create instance."""
        super(ServersControllerCreateTest, self).setUp()

        self.flags(verbose=True,
                   enable_instance_password=True)
        self.instance_cache_num = 0
        self.instance_cache_by_id = {}
        self.instance_cache_by_uuid = {}

        fakes.stub_out_nw_api(self)

        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)

        def instance_create(context, inst):
            inst_type = flavors.get_flavor_by_flavor_id(3)
            image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
            def_image_ref = 'http://localhost/fake/images/%s' % image_uuid
            self.instance_cache_num += 1
            instance = fake_instance.fake_db_instance(**{
                'id': self.instance_cache_num,
                'display_name': inst['display_name'] or 'test',
                'display_description': inst['display_description'] or '',
                'uuid': FAKE_UUID,
                'instance_type': inst_type,
                'image_ref': inst.get('image_ref', def_image_ref),
                'user_id': 'fake',
                'project_id': 'fake',
                'reservation_id': inst['reservation_id'],
                "created_at": datetime.datetime(2010, 10, 10, 12, 0, 0),
                "updated_at": datetime.datetime(2010, 11, 11, 11, 0, 0),
                "config_drive": None,
                "progress": 0,
                "fixed_ips": [],
                "task_state": "",
                "vm_state": "",
                "root_device_name": inst.get('root_device_name', 'vda'),
            })

            self.instance_cache_by_id[instance['id']] = instance
            self.instance_cache_by_uuid[instance['uuid']] = instance
            return instance

        def instance_get(context, instance_id):
            """Stub for compute/api create() pulling in instance after
            scheduling
            """
            return self.instance_cache_by_id[instance_id]

        def instance_update(context, uuid, values):
            instance = self.instance_cache_by_uuid[uuid]
            instance.update(values)
            return instance

        def server_update_and_get_original(
                context, instance_uuid, params, columns_to_join=None):
            inst = self.instance_cache_by_uuid[instance_uuid]
            inst.update(params)
            return (inst, inst)

        def fake_method(*args, **kwargs):
            pass

        def project_get_networks(context, user_id):
            return dict(id='1', host='localhost')

        fakes.stub_out_key_pair_funcs(self)
        fake.stub_out_image_service(self)
        self.stubs.Set(uuid, 'uuid4', fake_gen_uuid)
        self.stub_out('nova.db.project_get_networks', project_get_networks)
        self.stub_out('nova.db.instance_create', instance_create)
        self.stub_out('nova.db.instance_system_metadata_update', fake_method)
        self.stub_out('nova.db.instance_get', instance_get)
        self.stub_out('nova.db.instance_update', instance_update)
        self.stub_out('nova.db.instance_update_and_get_original',
                server_update_and_get_original)
        self.stubs.Set(manager.VlanManager, 'allocate_fixed_ip',
                       fake_method)
        self.body = {
            'server': {
                'name': 'server_test',
                'imageRef': self.image_uuid,
                'flavorRef': self.flavor_ref,
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                    },
                'personality': [
                    {
                        "path": "/etc/banner.txt",
                        "contents": "MQ==",
                    },
                ],
            },
        }
        self.bdm = [{'delete_on_termination': 1,
                     'device_name': 123,
                     'volume_size': 1,
                     'volume_id': '11111111-1111-1111-1111-111111111111'}]

        self.req = fakes.HTTPRequest.blank('/fake/servers')
        self.req.method = 'POST'
        self.req.headers["content-type"] = "application/json"

    def _check_admin_password_len(self, server_dict):
        """utility function - check server_dict for admin_password length."""
        self.assertEqual(CONF.password_length,
                         len(server_dict["adminPass"]))

    def _check_admin_password_missing(self, server_dict):
        """utility function - check server_dict for admin_password absence."""
        self.assertNotIn("adminPass", server_dict)

    def _test_create_instance(self, flavor=2):
        image_uuid = 'c905cedb-7281-47e4-8a62-f26bc5fc4c77'
        self.body['server']['imageRef'] = image_uuid
        self.body['server']['flavorRef'] = flavor
        self.req.body = jsonutils.dump_as_bytes(self.body)
        server = self.controller.create(self.req, body=self.body).obj['server']
        self._check_admin_password_len(server)
        self.assertEqual(FAKE_UUID, server['id'])

    def test_create_instance_with_none_value_port(self):
        self.body['server'] = {'networks': [{'port': None, 'uuid': FAKE_UUID}]}
        self.body['server']['name'] = 'test'
        self._test_create_instance()

    def test_create_instance_private_flavor(self):
        values = {
            'name': 'fake_name',
            'memory_mb': 512,
            'vcpus': 1,
            'root_gb': 10,
            'ephemeral_gb': 10,
            'flavorid': '1324',
            'swap': 0,
            'rxtx_factor': 0.5,
            'vcpu_weight': 1,
            'disabled': False,
            'is_public': False,
        }
        db.flavor_create(context.get_admin_context(), values)
        self.assertRaises(webob.exc.HTTPBadRequest, self._test_create_instance,
                          flavor=1324)

    def test_create_server_bad_image_uuid(self):
        self.body['server']['min_count'] = 1
        self.body['server']['imageRef'] = 1,
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create,
                          self.req, body=self.body)
    # TODO(cyeoh): bp-v3-api-unittests
    # This needs to be ported to the os-networks extension tests
    # def test_create_server_with_invalid_networks_parameter(self):
    #     self.ext_mgr.extensions = {'os-networks': 'fake'}
    #     image_href = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
    #     flavor_ref = 'http://localhost/123/flavors/3'
    #     body = {
    #         'server': {
    #         'name': 'server_test',
    #         'imageRef': image_href,
    #         'flavorRef': flavor_ref,
    #         'networks': {'uuid': '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'},
    #         }
    #     }
    #     req = fakes.HTTPRequest.blank('/fake/servers')
    #     req.method = 'POST'
    #     req.body = jsonutils.dump_as_bytes(body)
    #     req.headers["content-type"] = "application/json"
    #     self.assertRaises(webob.exc.HTTPBadRequest,
    #                       self.controller.create,
    #                       req,
    #                       body)

    def test_create_server_with_deleted_image(self):
        # Get the fake image service so we can set the status to deleted
        (image_service, image_id) = glance.get_remote_image_service(
                context, '')
        image_service.update(context, self.image_uuid, {'status': 'DELETED'})
        self.addCleanup(image_service.update, context, self.image_uuid,
                        {'status': 'active'})

        self.body['server']['flavorRef'] = 2
        self.req.body = jsonutils.dump_as_bytes(self.body)
        with testtools.ExpectedException(
                webob.exc.HTTPBadRequest,
                'Image 76fa36fc-c930-4bf3-8c8a-ea2a2420deb6 is not active.'):
            self.controller.create(self.req, body=self.body)

    def test_create_server_image_too_large(self):
        # Get the fake image service so we can update the size of the image
        (image_service, image_id) = glance.get_remote_image_service(
                                    context, self.image_uuid)

        image = image_service.show(context, image_id)

        orig_size = image['size']
        new_size = str(1000 * (1024 ** 3))
        image_service.update(context, self.image_uuid, {'size': new_size})

        self.addCleanup(image_service.update, context, self.image_uuid,
                        {'size': orig_size})

        self.body['server']['flavorRef'] = 2
        self.req.body = jsonutils.dump_as_bytes(self.body)

        with testtools.ExpectedException(
                webob.exc.HTTPBadRequest,
                "Flavor's disk is too small for requested image."):
            self.controller.create(self.req, body=self.body)

    def test_create_instance_with_image_non_uuid(self):
        self.body['server']['imageRef'] = 'not-uuid'
        self.assertRaises(exception.ValidationError,
                          self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_with_image_as_full_url(self):
        image_href = ('http://localhost/v2/fake/images/'
            '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6')
        self.body['server']['imageRef'] = image_href
        self.assertRaises(exception.ValidationError,
                          self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_with_image_as_empty_string(self):
        self.body['server']['imageRef'] = ''
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_no_key_pair(self):
        fakes.stub_out_key_pair_funcs(self, have_key_pair=False)
        self._test_create_instance()

    def _test_create_extra(self, params, no_image=False):
        self.body['server']['flavorRef'] = 2
        if no_image:
            self.body['server'].pop('imageRef', None)
        self.body['server'].update(params)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.headers["content-type"] = "application/json"
        self.controller.create(self.req, body=self.body).obj['server']

    # TODO(cyeoh): bp-v3-api-unittests
    # This needs to be ported to the os-keypairs extension tests
    # def test_create_instance_with_keypairs_enabled(self):
    #     self.ext_mgr.extensions = {'os-keypairs': 'fake'}
    #     key_name = 'green'
    #
    #     params = {'key_name': key_name}
    #     old_create = compute_api.API.create
    #
    #     # NOTE(sdague): key pair goes back to the database,
    #     # so we need to stub it out for tests
    #     def key_pair_get(context, user_id, name):
    #         return {'public_key': 'FAKE_KEY',
    #                 'fingerprint': 'FAKE_FINGERPRINT',
    #                 'name': name}
    #
    #     def create(*args, **kwargs):
    #         self.assertEqual(kwargs['key_name'], key_name)
    #         return old_create(*args, **kwargs)
    #
    #     self.stub_out('nova.db.key_pair_get', key_pair_get)
    #     self.stubs.Set(compute_api.API, 'create', create)
    #     self._test_create_extra(params)
    #
    # TODO(cyeoh): bp-v3-api-unittests
    # This needs to be ported to the os-networks extension tests
    # def test_create_instance_with_networks_enabled(self):
    #     self.ext_mgr.extensions = {'os-networks': 'fake'}
    #     net_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
    #     requested_networks = [{'uuid': net_uuid}]
    #     params = {'networks': requested_networks}
    #     old_create = compute_api.API.create

    #     def create(*args, **kwargs):
    #         result = [('76fa36fc-c930-4bf3-8c8a-ea2a2420deb6', None)]
    #         self.assertEqual(kwargs['requested_networks'], result)
    #         return old_create(*args, **kwargs)

    #     self.stubs.Set(compute_api.API, 'create', create)
    #     self._test_create_extra(params)

    def test_create_instance_with_port_with_no_fixed_ips(self):
        port_id = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'port': port_id}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.PortRequiresFixedIP(port_id=port_id)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_raise_user_data_too_large(self, mock_create):
        mock_create.side_effect = exception.InstanceUserDataTooLarge(
            maxsize=1, length=2)

        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_with_network_with_no_subnet(self):
        network = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.NetworkRequiresSubnet(network_uuid=network)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    def test_create_instance_with_non_unique_secgroup_name(self):
        network = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network}]
        params = {'networks': requested_networks,
                  'security_groups': [{'name': 'dup'}, {'name': 'dup'}]}

        def fake_create(*args, **kwargs):
            raise exception.NoUniqueMatch("No Unique match found for ...")

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPConflict,
                          self._test_create_extra, params)

    def test_create_instance_secgroup_leading_trailing_spaces(self):
        network = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network}]
        params = {'networks': requested_networks,
                  'security_groups': [{'name': '  sg  '}]}

        self.assertRaises(exception.ValidationError,
                          self._test_create_extra, params)

    def test_create_instance_secgroup_leading_trailing_spaces_compat_mode(
            self):
        network = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network}]
        params = {'networks': requested_networks,
                  'security_groups': [{'name': '  sg  '}]}

        def fake_create(*args, **kwargs):
            self.assertEqual(['  sg  '], kwargs['security_group'])
            return (objects.InstanceList(objects=[fakes.stub_instance_obj(
                self.req.environ['nova.context'])]), None)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.req.set_legacy_v2()
        self._test_create_extra(params)

    def test_create_instance_with_networks_disabled_neutronv2(self):
        self.flags(use_neutron=True)
        net_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
        requested_networks = [{'uuid': net_uuid}]
        params = {'networks': requested_networks}
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            result = [('76fa36fc-c930-4bf3-8c8a-ea2a2420deb6', None,
                       None, None)]
            self.assertEqual(result, kwargs['requested_networks'].as_tuples())
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)
        self._test_create_extra(params)

    def test_create_instance_with_networks_disabled(self):
        net_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
        requested_networks = [{'uuid': net_uuid}]
        params = {'networks': requested_networks}
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertIsNone(kwargs['requested_networks'])
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)
        self._test_create_extra(params)

    def test_create_instance_with_pass_disabled(self):
        # test with admin passwords disabled See lp bug 921814
        self.flags(enable_instance_password=False)

        self.flags(enable_instance_password=False)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        server = res['server']
        self._check_admin_password_missing(server)
        self.assertEqual(FAKE_UUID, server['id'])

    def test_create_instance_name_too_long(self):
        self.body['server']['name'] = 'X' * 256
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError, self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_name_with_spaces_in_the_middle(self):
        self.body['server']['name'] = 'abc    def'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.controller.create(self.req, body=self.body)

    def test_create_instance_name_with_leading_trailing_spaces(self):
        self.body['server']['name'] = '   abc    def   '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_name_with_leading_trailing_spaces_in_compat_mode(
            self):
        self.body['server']['name'] = '   abc    def   '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.set_legacy_v2()
        self.controller.create(self.req, body=self.body)

    def test_create_instance_name_all_blank_spaces(self):
        image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
        flavor_ref = 'http://localhost/fake/flavors/3'
        body = {
            'server': {
                'name': ' ' * 64,
                'imageRef': image_uuid,
                'flavorRef': flavor_ref,
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                },
            },
        }

        req = fakes.HTTPRequest.blank('/fake/servers')
        req.method = 'POST'
        req.body = jsonutils.dump_as_bytes(body)
        req.headers["content-type"] = "application/json"
        self.assertRaises(exception.ValidationError,
                          self.controller.create, req, body=body)

    def test_create_az_with_leading_trailing_spaces(self):
        self.body['server']['availability_zone'] = '  zone1  '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_az_with_leading_trailing_spaces_in_compat_mode(
            self):
        self.body['server']['name'] = '   abc    def   '
        self.body['server']['availability_zones'] = '  zone1  '
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.set_legacy_v2()
        with mock.patch.object(availability_zones, 'get_availability_zones',
                               return_value=['  zone1  ']):
            self.controller.create(self.req, body=self.body)

    def test_create_instance(self):
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        server = res['server']
        self._check_admin_password_len(server)
        self.assertEqual(FAKE_UUID, server['id'])

    def test_create_instance_extension_create_exception(self):
        def fake_keypair_server_create(self, server_dict,
                                       create_kwargs):
            raise KeyError

        self.stubs.Set(keypairs.Keypairs, 'server_create',
                       fake_keypair_server_create)
        image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
        flavor_ref = 'http://localhost/123/flavors/3'
        body = {
            'server': {
                'name': 'server_test',
                'imageRef': image_uuid,
                'flavorRef': flavor_ref,
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                },
            },
        }

        req = fakes.HTTPRequestV21.blank('/fake/servers')
        req.method = 'POST'
        req.body = jsonutils.dump_as_bytes(body)
        req.headers["content-type"] = "application/json"
        self.assertRaises(webob.exc.HTTPInternalServerError,
                          self.controller.create, req, body=body)

    def test_create_instance_pass_disabled(self):
        self.flags(enable_instance_password=False)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        server = res['server']
        self._check_admin_password_missing(server)
        self.assertEqual(FAKE_UUID, server['id'])

    @mock.patch('nova.virt.hardware.numa_get_constraints')
    def _test_create_instance_numa_topology_wrong(self, exc,
                                                  numa_constraints_mock):
        numa_constraints_mock.side_effect = exc(**{'name': None,
                                                   'cpunum': 0,
                                                   'cpumax': 0,
                                                   'cpuset': None,
                                                   'memsize': 0,
                                                   'memtotal': 0})
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_numa_topology_wrong(self):
        for exc in [exception.ImageNUMATopologyIncomplete,
                    exception.ImageNUMATopologyForbidden,
                    exception.ImageNUMATopologyAsymmetric,
                    exception.ImageNUMATopologyCPUOutOfRange,
                    exception.ImageNUMATopologyCPUDuplicates,
                    exception.ImageNUMATopologyCPUsUnassigned,
                    exception.ImageNUMATopologyMemoryOutOfRange]:
            self._test_create_instance_numa_topology_wrong(exc)

    def test_create_instance_too_much_metadata(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata']['vote'] = 'fiddletown'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPForbidden,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_key_too_long(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = {('a' * 260): '12345'}

        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_value_too_long(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = {'key1': ('a' * 260)}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_key_blank(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = {'': 'abcd'}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_not_dict(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = 'string'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_key_not_string(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = {1: 'test'}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_metadata_value_not_string(self):
        self.flags(quota_metadata_items=1)
        self.body['server']['metadata'] = {'test': ['a', 'list']}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_user_data_malformed_bad_request(self):
        params = {'user_data': 'u1234'}
        self.assertRaises(exception.ValidationError,
                          self._test_create_extra, params)

    def test_create_instance_invalid_key_name(self):
        self.body['server']['key_name'] = 'nonexistentkey'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_valid_key_name(self):
        self.body['server']['key_name'] = 'key'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        self.assertEqual(FAKE_UUID, res["server"]["id"])
        self._check_admin_password_len(res["server"])

    def test_create_instance_invalid_flavor_href(self):
        flavor_ref = 'http://localhost/v2/flavors/asdf'
        self.body['server']['flavorRef'] = flavor_ref
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_invalid_flavor_id_int(self):
        flavor_ref = -1
        self.body['server']['flavorRef'] = flavor_ref
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    @mock.patch.object(nova.compute.flavors, 'get_flavor_by_flavor_id',
                       return_value=objects.Flavor())
    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_with_non_existing_snapshot_id(
            self, mock_create,
            mock_get_flavor_by_flavor_id):
        mock_create.side_effect = exception.SnapshotNotFound(snapshot_id='123')

        self.body['server'] = {'name': 'server_test',
                               'flavorRef': self.flavor_ref,
                               'block_device_mapping_v2':
                                   [{'source_type': 'snapshot',
                                     'uuid': '123'}]}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_invalid_flavor_id_empty(self):
        flavor_ref = ""
        self.body['server']['flavorRef'] = flavor_ref
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_bad_flavor_href(self):
        flavor_ref = 'http://localhost/v2/flavors/17'
        self.body['server']['flavorRef'] = flavor_ref
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_local_href(self):
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        server = res['server']
        self.assertEqual(FAKE_UUID, server['id'])

    def test_create_instance_admin_password(self):
        self.body['server']['flavorRef'] = 3
        self.body['server']['adminPass'] = 'testpass'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        server = res['server']
        self.assertEqual(server['adminPass'],
                         self.body['server']['adminPass'])

    def test_create_instance_admin_password_pass_disabled(self):
        self.flags(enable_instance_password=False)
        self.body['server']['flavorRef'] = 3
        self.body['server']['adminPass'] = 'testpass'
        self.req.body = jsonutils.dump_as_bytes(self.body)
        res = self.controller.create(self.req, body=self.body).obj

        self.assertIn('server', res)
        self.assertIn('adminPass', self.body['server'])

    def test_create_instance_admin_password_empty(self):
        self.body['server']['flavorRef'] = 3
        self.body['server']['adminPass'] = ''
        self.req.body = jsonutils.dump_as_bytes(self.body)

        # The fact that the action doesn't raise is enough validation
        self.controller.create(self.req, body=self.body)

    def test_create_location(self):
        selfhref = 'http://localhost/v2/fake/servers/%s' % FAKE_UUID
        self.req.body = jsonutils.dump_as_bytes(self.body)
        robj = self.controller.create(self.req, body=self.body)

        self.assertEqual(robj['Location'], selfhref)

    def _do_test_create_instance_above_quota(self, resource, allowed, quota,
                                             expected_msg):
        fakes.stub_out_instance_quota(self, allowed, quota, resource)
        self.body['server']['flavorRef'] = 3
        self.req.body = jsonutils.dump_as_bytes(self.body)
        try:
            self.controller.create(self.req, body=self.body).obj['server']
            self.fail('expected quota to be exceeded')
        except webob.exc.HTTPForbidden as e:
            self.assertEqual(e.explanation, expected_msg)

    def test_create_instance_above_quota_instances(self):
        msg = ('Quota exceeded for instances: Requested 1, but'
               ' already used 10 of 10 instances')
        self._do_test_create_instance_above_quota('instances', 0, 10, msg)

    def test_create_instance_above_quota_ram(self):
        msg = ('Quota exceeded for ram: Requested 4096, but'
               ' already used 8192 of 10240 ram')
        self._do_test_create_instance_above_quota('ram', 2048, 10 * 1024, msg)

    def test_create_instance_above_quota_cores(self):
        msg = ('Quota exceeded for cores: Requested 2, but'
               ' already used 9 of 10 cores')
        self._do_test_create_instance_above_quota('cores', 1, 10, msg)

    def test_create_instance_above_quota_server_group_members(self):
        ctxt = self.req.environ['nova.context']
        fake_group = objects.InstanceGroup(ctxt)
        fake_group.project_id = ctxt.project_id
        fake_group.user_id = ctxt.user_id
        fake_group.create()

        def fake_count(context, name, group, user_id):
            self.assertEqual(name, "server_group_members")
            self.assertEqual(group.uuid, fake_group.uuid)
            self.assertEqual(user_id,
                             self.req.environ['nova.context'].user_id)
            return 10

        def fake_limit_check(context, **kwargs):
            if 'server_group_members' in kwargs:
                raise exception.OverQuota(overs={})

        def fake_instance_destroy(context, uuid, constraint):
            return fakes.stub_instance(1)

        self.stubs.Set(fakes.QUOTAS, 'count', fake_count)
        self.stubs.Set(fakes.QUOTAS, 'limit_check', fake_limit_check)
        self.stub_out('nova.db.instance_destroy', fake_instance_destroy)
        self.body['os:scheduler_hints'] = {'group': fake_group.uuid}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        expected_msg = "Quota exceeded, too many servers in group"

        try:
            self.controller.create(self.req, body=self.body).obj
            self.fail('expected quota to be exceeded')
        except webob.exc.HTTPForbidden as e:
            self.assertEqual(e.explanation, expected_msg)

    def test_create_instance_with_group_hint(self):
        ctxt = self.req.environ['nova.context']
        test_group = objects.InstanceGroup(ctxt)
        test_group.project_id = ctxt.project_id
        test_group.user_id = ctxt.user_id
        test_group.create()

        def fake_instance_destroy(context, uuid, constraint):
            return fakes.stub_instance(1)

        self.stub_out('nova.db.instance_destroy', fake_instance_destroy)
        self.body['os:scheduler_hints'] = {'group': test_group.uuid}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        server = self.controller.create(self.req, body=self.body).obj['server']

        test_group = objects.InstanceGroup.get_by_uuid(ctxt, test_group.uuid)
        self.assertIn(server['id'], test_group.members)

    def test_create_instance_with_group_hint_group_not_found(self):
        def fake_instance_destroy(context, uuid, constraint):
            return fakes.stub_instance(1)

        self.stub_out('nova.db.instance_destroy', fake_instance_destroy)
        self.body['os:scheduler_hints'] = {
            'group': '5b674f73-c8cf-40ef-9965-3b6fe4b304b1'}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_with_group_hint_wrong_uuid_format(self):
        self.body['os:scheduler_hints'] = {
            'group': 'non-uuid'}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(exception.ValidationError,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_with_neutronv2_port_in_use(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network, 'port': port}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.PortInUse(port_id=port)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPConflict,
                          self._test_create_extra, params)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_public_network_non_admin(self, mock_create):
        public_network_uuid = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        params = {'networks': [{'uuid': public_network_uuid}]}
        self.req.body = jsonutils.dump_as_bytes(self.body)
        mock_create.side_effect = exception.ExternalNetworkAttachForbidden(
                                             network_uuid=public_network_uuid)
        self.assertRaises(webob.exc.HTTPForbidden,
                          self._test_create_extra, params)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_multiple_instance_with_specified_ip_neutronv2(self,
                                                                  _api_mock):
        _api_mock.side_effect = exception.InvalidFixedIpAndMaxCountRequest(
            reason="")
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        address = '10.0.0.1'
        requested_networks = [{'uuid': network, 'fixed_ip': address,
                               'port': port}]
        params = {'networks': requested_networks}
        self.body['server']['max_count'] = 2
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    def test_create_multiple_instance_with_neutronv2_port(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network, 'port': port}]
        params = {'networks': requested_networks}
        self.body['server']['max_count'] = 2

        def fake_create(*args, **kwargs):
            msg = ("Unable to launch multiple instances with"
                   " a single configured port ID. Please launch your"
                   " instance one by one with different ports.")
            raise exception.MultiplePortsNotApplicable(reason=msg)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    def test_create_instance_with_neutronv2_not_found_network(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        requested_networks = [{'uuid': network}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.NetworkNotFound(network_id=network)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    def test_create_instance_with_neturonv2_network_duplicated(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        requested_networks = [{'uuid': network}, {'uuid': network}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.NetworkDuplicated(network_id=network)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    def test_create_instance_with_neutronv2_port_not_found(self):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        port = 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'
        requested_networks = [{'uuid': network, 'port': port}]
        params = {'networks': requested_networks}

        def fake_create(*args, **kwargs):
            raise exception.PortNotFound(port_id=port)

        self.stubs.Set(compute_api.API, 'create', fake_create)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_with_network_ambiguous(self, mock_create):
        mock_create.side_effect = exception.NetworkAmbiguous()
        self.assertRaises(webob.exc.HTTPConflict,
                          self._test_create_extra, {})

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.UnableToAutoAllocateNetwork(
                           project_id=FAKE_UUID))
    def test_create_instance_with_unable_to_auto_allocate_network(self,
                                                                  mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, {})

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InstanceExists(
                           name='instance-name'))
    def test_create_instance_raise_instance_exists(self, mock_create):
        self.assertRaises(webob.exc.HTTPConflict,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidBDMEphemeralSize)
    def test_create_instance_raise_invalid_bdm_ephsize(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidNUMANodesNumber(
                           details=''))
    def test_create_instance_raise_invalid_numa_nodes(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidBDMFormat(details=''))
    def test_create_instance_raise_invalid_bdm_format(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidBDMSwapSize)
    def test_create_instance_raise_invalid_bdm_swapsize(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidBDM)
    def test_create_instance_raise_invalid_bdm(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.ImageBadRequest(
                        image_id='dummy', response='dummy'))
    def test_create_instance_raise_image_bad_request(self, mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_invalid_availability_zone(self):
        self.body['server']['availability_zone'] = 'invalid::::zone'
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.FixedIpNotFoundForAddress(
                        address='dummy'))
    def test_create_instance_raise_fixed_ip_not_found_bad_request(self,
                                                                  mock_create):
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_invalid_personality(self, mock_create):
        codec = 'utf8'
        content = 'b25zLiINCg0KLVJpY2hhcmQgQ$$%QQmFjaA=='
        start_position = 19
        end_position = 20
        msg = 'invalid start byte'
        mock_create.side_effect = UnicodeDecodeError(codec, content,
                                                     start_position,
                                                     end_position, msg)

        self.body['server']['personality'] = [
            {
                "path": "/etc/banner.txt",
                "contents": "b25zLiINCg0KLVJpY2hhcmQgQ$$%QQmFjaA==",
            },
        ]
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.create, self.req, body=self.body)

    def test_create_instance_without_personality_should_get_empty_list(self):
        old_create = compute_api.API.create
        del self.body['server']['personality']

        def create(*args, **kwargs):
            self.assertEqual([], kwargs['injected_files'])
            return old_create(*args, **kwargs)

        self.stub_out('nova.compute.api.API.create', create)

        self._test_create_instance()

    def test_create_instance_with_extra_personality_arg(self):
        self.body['server']['personality'] = [
            {
                "path": "/etc/banner.txt",
                "contents": "b25zLiINCg0KLVJpY2hhcmQgQ$$%QQmFjaA==",
                "extra_arg": "extra value"
            },
        ]

        self.assertRaises(exception.ValidationError,
                          self.controller.create,
                          self.req, body=self.body)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.PciRequestAliasNotDefined(
                           alias='fake_name'))
    def test_create_instance_pci_alias_not_defined(self, mock_create):
        # Tests that PciRequestAliasNotDefined is translated to a 400 error.
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, {})


class ServersControllerCreateTestV219(ServersControllerCreateTest):
    def _create_instance_req(self, set_desc, desc=None):
        if set_desc:
            self.body['server']['description'] = desc
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.api_version_request = \
            api_version_request.APIVersionRequest('2.19')

    def test_create_instance_with_description(self):
        self._create_instance_req(True, 'server_desc')
        # The fact that the action doesn't raise is enough validation
        self.controller.create(self.req, body=self.body).obj

    def test_create_instance_with_none_description(self):
        self._create_instance_req(True)
        # The fact that the action doesn't raise is enough validation
        self.controller.create(self.req, body=self.body).obj

    def test_create_instance_with_empty_description(self):
        self._create_instance_req(True, '')
        # The fact that the action doesn't raise is enough validation
        self.controller.create(self.req, body=self.body).obj

    def test_create_instance_without_description(self):
        self._create_instance_req(False)
        # The fact that the action doesn't raise is enough validation
        self.controller.create(self.req, body=self.body).obj

    def test_create_instance_description_too_long(self):
        self._create_instance_req(True, 'X' * 256)
        self.assertRaises(exception.ValidationError, self.controller.create,
                          self.req, body=self.body)

    def test_create_instance_description_invalid(self):
        self._create_instance_req(True, "abc\0ddef")
        self.assertRaises(exception.ValidationError, self.controller.create,
                          self.req, body=self.body)


class ServersControllerCreateTestV232(test.NoDBTestCase):
    def setUp(self):
        super(ServersControllerCreateTestV232, self).setUp()
        self.flags(use_neutron=True)

        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)

        self.body = {
            'server': {
                'name': 'device-tagging-server',
                'imageRef': '6b0edabb-8cde-4684-a3f4-978960a51378',
                'flavorRef': '2',
                'networks': [{
                    'uuid': 'ff608d40-75e9-48cb-b745-77bb55b5eaf2'
                }],
                'block_device_mapping_v2': [{
                    'uuid': '70a599e0-31e7-49b7-b260-868f441e862b',
                    'source_type': 'image',
                    'destination_type': 'volume',
                    'boot_index': 0,
                    'volume_size': '1'
                }]
            }
        }

        self.req = fakes.HTTPRequestV21.blank('/fake/servers', version='2.32')
        self.req.method = 'POST'
        self.req.headers['content-type'] = 'application/json'

    def _create_server(self):
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.controller.create(self.req, body=self.body)

    def test_create_server_no_tags_old_compute(self):
        with test.nested(
            mock.patch.object(objects.Service, 'get_minimum_version',
                              return_value=13),
            mock.patch.object(nova.compute.flavors, 'get_flavor_by_flavor_id',
                              return_value=objects.Flavor()),
            mock.patch.object(
                compute_api.API, 'create',
                return_value=(
                    [{'uuid': 'f60012d9-5ba4-4547-ab48-f94ff7e62d4e'}],
                    1)),
        ):
            self._create_server()

    @mock.patch.object(objects.Service, 'get_minimum_version',
                       return_value=13)
    def test_create_server_tagged_nic_old_compute_fails(self, get_min_ver):
        self.body['server']['networks'][0]['tag'] = 'foo'
        self.assertRaises(webob.exc.HTTPBadRequest, self._create_server)

    @mock.patch.object(objects.Service, 'get_minimum_version',
                       return_value=13)
    def test_create_server_tagged_bdm_old_compute_fails(self, get_min_ver):
        self.body['server']['block_device_mapping_v2'][0]['tag'] = 'foo'
        self.assertRaises(webob.exc.HTTPBadRequest, self._create_server)

    def test_create_server_tagged_nic_new_compute(self):
        with test.nested(
            mock.patch.object(objects.Service, 'get_minimum_version',
                              return_value=14),
            mock.patch.object(nova.compute.flavors, 'get_flavor_by_flavor_id',
                              return_value=objects.Flavor()),
            mock.patch.object(
                compute_api.API, 'create',
                return_value=(
                    [{'uuid': 'f60012d9-5ba4-4547-ab48-f94ff7e62d4e'}],
                    1)),
        ):
            self.body['server']['networks'][0]['tag'] = 'foo'
            self._create_server()

    def test_create_server_tagged_bdm_new_compute(self):
        with test.nested(
            mock.patch.object(objects.Service, 'get_minimum_version',
                              return_value=14),
            mock.patch.object(nova.compute.flavors, 'get_flavor_by_flavor_id',
                              return_value=objects.Flavor()),
            mock.patch.object(
                compute_api.API, 'create',
                return_value=(
                    [{'uuid': 'f60012d9-5ba4-4547-ab48-f94ff7e62d4e'}],
                    1)),
        ):
            self.body['server']['block_device_mapping_v2'][0]['tag'] = 'foo'
            self._create_server()


class ServersControllerCreateTestV237(test.NoDBTestCase):
    """Tests server create scenarios with the v2.37 microversion.

    These tests are mostly about testing the validation on the 2.37
    server create request with emphasis on negative scenarios.
    """
    def setUp(self):
        super(ServersControllerCreateTestV237, self).setUp()
        # Set the use_neutron flag to process requested networks.
        self.flags(use_neutron=True)
        # Create the server controller.
        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)
        # Define a basic server create request body which tests can customize.
        self.body = {
            'server': {
                'name': 'auto-allocate-test',
                'imageRef': '6b0edabb-8cde-4684-a3f4-978960a51378',
                'flavorRef': '2',
            },
        }
        # Create a fake request using the 2.37 microversion.
        self.req = fakes.HTTPRequestV21.blank('/fake/servers', version='2.37')
        self.req.method = 'POST'
        self.req.headers['content-type'] = 'application/json'

    def _create_server(self, networks):
        self.body['server']['networks'] = networks
        self.req.body = jsonutils.dump_as_bytes(self.body)
        return self.controller.create(self.req, body=self.body).obj['server']

    def test_create_server_auth_pre_2_37_fails(self):
        """Negative test to make sure you can't pass 'auto' before 2.37"""
        self.req.api_version_request = \
            api_version_request.APIVersionRequest('2.36')
        self.assertRaises(exception.ValidationError, self._create_server,
                          'auto')

    def test_create_server_no_requested_networks_fails(self):
        """Negative test for a server create request with no networks requested
        which should fail with the v2.37 schema validation.
        """
        self.assertRaises(exception.ValidationError, self._create_server, None)

    def test_create_server_network_id_not_uuid_fails(self):
        """Negative test for a server create request where the requested
        network id is not one of the auto/none enums.
        """
        self.assertRaises(exception.ValidationError, self._create_server,
                          'not-auto-or-none')

    def test_create_server_network_id_empty_string_fails(self):
        """Negative test for a server create request where the requested
        network id is the empty string.
        """
        self.assertRaises(exception.ValidationError, self._create_server, '')

    @mock.patch.object(objects.Flavor, 'get_by_flavor_id',
                       side_effect=exception.FlavorNotFound(flavor_id='2'))
    def test_create_server_auto_flavornotfound(self,
                                                                 get_flavor):
        """Tests that requesting auto networking is OK. This test
        short-circuits on a FlavorNotFound error.
        """
        ex = self.assertRaises(
            webob.exc.HTTPBadRequest, self._create_server, 'auto')
        # make sure it was a flavor not found error and not something else
        self.assertIn('Flavor 2 could not be found', six.text_type(ex))

    @mock.patch.object(objects.Flavor, 'get_by_flavor_id',
                       side_effect=exception.FlavorNotFound(flavor_id='2'))
    def test_create_server_none_flavornotfound(self,
                                                                 get_flavor):
        """Tests that requesting none for networking is OK. This test
        short-circuits on a FlavorNotFound error.
        """
        ex = self.assertRaises(
            webob.exc.HTTPBadRequest, self._create_server, 'none')
        # make sure it was a flavor not found error and not something else
        self.assertIn('Flavor 2 could not be found', six.text_type(ex))

    @mock.patch.object(objects.Flavor, 'get_by_flavor_id',
                       side_effect=exception.FlavorNotFound(flavor_id='2'))
    def test_create_server_multiple_specific_nics_flavornotfound(self,
                                                                 get_flavor):
        """Tests that requesting multiple specific network IDs is OK. This test
        short-circuits on a FlavorNotFound error.
        """
        ex = self.assertRaises(
            webob.exc.HTTPBadRequest, self._create_server,
                [{'uuid': 'e3b686a8-b91d-4a61-a3fc-1b74bb619ddb'},
                 {'uuid': 'e0f00941-f85f-46ec-9315-96ded58c2f14'}])
        # make sure it was a flavor not found error and not something else
        self.assertIn('Flavor 2 could not be found', six.text_type(ex))

    def test_create_server_legacy_neutron_network_id_fails(self):
        """Tests that we no longer support the legacy br-<uuid> format for
           a network id.
        """
        uuid = 'br-00000000-0000-0000-0000-000000000000'
        self.assertRaises(exception.ValidationError, self._create_server,
                          [{'uuid': uuid}])

    @mock.patch.object(objects.Service, 'get_minimum_version',
                       return_value=11)
    def test_validate_auto_or_none_network_request_old_computes(self,
                                                                mock_get_ver):
        """Tests that the network request is nulled out when the minimum
           nova-compute is not running new enough code to support 'auto'.
        """
        req_nets = objects.NetworkRequestList(
            objects=[objects.NetworkRequest(network_id='auto')])
        self.assertIsNone(
            self.controller._validate_auto_or_none_network_request(
                req_nets))
        mock_get_ver.assert_called_once_with(mock.ANY, 'nova-compute')


class ServersControllerCreateTestWithMock(test.TestCase):
    image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'
    flavor_ref = 'http://localhost/123/flavors/3'

    def setUp(self):
        """Shared implementation for tests below that create instance."""
        super(ServersControllerCreateTestWithMock, self).setUp()

        self.flags(verbose=True,
                   enable_instance_password=True)
        self.instance_cache_num = 0
        self.instance_cache_by_id = {}
        self.instance_cache_by_uuid = {}

        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)

        self.body = {
            'server': {
                'name': 'server_test',
                'imageRef': self.image_uuid,
                'flavorRef': self.flavor_ref,
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                    },
                },
            }
        self.req = fakes.HTTPRequest.blank('/fake/servers')
        self.req.method = 'POST'
        self.req.headers["content-type"] = "application/json"

    def _test_create_extra(self, params, no_image=False):
        self.body['server']['flavorRef'] = 2
        if no_image:
            self.body['server'].pop('imageRef', None)
        self.body['server'].update(params)
        self.req.body = jsonutils.dump_as_bytes(self.body)
        self.req.headers["content-type"] = "application/json"
        self.controller.create(self.req, body=self.body).obj['server']

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_with_neutronv2_fixed_ip_already_in_use(self,
            create_mock):
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '10.0.2.3'
        requested_networks = [{'uuid': network, 'fixed_ip': address}]
        params = {'networks': requested_networks}
        create_mock.side_effect = exception.FixedIpAlreadyInUse(
            address=address,
            instance_uuid=network)
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, params)
        self.assertEqual(1, len(create_mock.call_args_list))

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_with_neutronv2_invalid_fixed_ip(self,
                                                             create_mock):
        self.flags(use_neutron=True)
        network = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        address = '999.0.2.3'
        requested_networks = [{'uuid': network, 'fixed_ip': address}]
        params = {'networks': requested_networks}
        self.assertRaises(exception.ValidationError,
                          self._test_create_extra, params)
        self.assertFalse(create_mock.called)

    @mock.patch.object(compute_api.API, 'create',
                       side_effect=exception.InvalidVolume(reason='error'))
    def test_create_instance_with_invalid_volume_error(self, create_mock):
        # Tests that InvalidVolume is translated to a 400 error.
        self.assertRaises(webob.exc.HTTPBadRequest,
                          self._test_create_extra, {})


class ServersViewBuilderTest(test.TestCase):

    def setUp(self):
        super(ServersViewBuilderTest, self).setUp()
        self.flags(use_ipv6=True)
        self.flags(group='glance', api_servers=['http://localhost:9292'])
        nw_cache_info = self._generate_nw_cache_info()
        db_inst = fakes.stub_instance(
            id=1,
            image_ref="5",
            uuid="deadbeef-feed-edee-beef-d0ea7beefedd",
            display_name="test_server",
            include_fake_metadata=False,
            nw_cache=nw_cache_info)

        privates = ['172.19.0.1']
        publics = ['192.168.0.3']
        public6s = ['b33f::fdee:ddff:fecc:bbaa']

        def nw_info(*args, **kwargs):
            return [(None, {'label': 'public',
                            'ips': [dict(ip=ip) for ip in publics],
                            'ip6s': [dict(ip=ip) for ip in public6s]}),
                    (None, {'label': 'private',
                            'ips': [dict(ip=ip) for ip in privates]})]

        fakes.stub_out_nw_api_get_instance_nw_info(self, nw_info)

        self.uuid = db_inst['uuid']
        self.view_builder = views.servers.ViewBuilder()
        self.request = fakes.HTTPRequestV21.blank("/fake")
        self.request.context = context.RequestContext('fake', 'fake')
        self.instance = fake_instance.fake_instance_obj(
                    self.request.context,
                    expected_attrs=instance_obj.INSTANCE_DEFAULT_FIELDS,
                    **db_inst)
        self.self_link = "http://localhost/v2/fake/servers/%s" % self.uuid
        self.bookmark_link = "http://localhost/fake/servers/%s" % self.uuid

    def _generate_nw_cache_info(self):
        fixed_ipv4 = ('192.168.1.100', '192.168.2.100', '192.168.3.100')
        fixed_ipv6 = ('2001:db8:0:1::1',)

        def _ip(ip):
            return {'address': ip, 'type': 'fixed'}

        nw_cache = [
            {'address': 'aa:aa:aa:aa:aa:aa',
             'id': 1,
             'network': {'bridge': 'br0',
                         'id': 1,
                         'label': 'test1',
                         'subnets': [{'cidr': '192.168.1.0/24',
                                      'ips': [_ip(fixed_ipv4[0])]},
                                      {'cidr': 'b33f::/64',
                                       'ips': [_ip(fixed_ipv6[0])]}]}},
            {'address': 'bb:bb:bb:bb:bb:bb',
             'id': 2,
             'network': {'bridge': 'br0',
                         'id': 1,
                         'label': 'test1',
                         'subnets': [{'cidr': '192.168.2.0/24',
                                      'ips': [_ip(fixed_ipv4[1])]}]}},
            {'address': 'cc:cc:cc:cc:cc:cc',
             'id': 3,
             'network': {'bridge': 'br0',
                         'id': 2,
                         'label': 'test2',
                         'subnets': [{'cidr': '192.168.3.0/24',
                                      'ips': [_ip(fixed_ipv4[2])]}]}}]
        return nw_cache

    def test_get_flavor_valid_instance_type(self):
        flavor_bookmark = "http://localhost/fake/flavors/1"
        expected = {"id": "1",
                    "links": [{"rel": "bookmark",
                               "href": flavor_bookmark}]}
        result = self.view_builder._get_flavor(self.request, self.instance)
        self.assertEqual(result, expected)

    def test_build_server(self):
        expected_server = {
            "server": {
                "id": self.uuid,
                "name": "test_server",
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
            }
        }

        output = self.view_builder.basic(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))

    def test_build_server_with_project_id(self):
        expected_server = {
            "server": {
                "id": self.uuid,
                "name": "test_server",
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
            }
        }

        output = self.view_builder.basic(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))

    def test_build_server_detail(self):
        image_bookmark = "http://localhost/fake/images/5"
        flavor_bookmark = "http://localhost/fake/flavors/1"
        expected_server = {
            "server": {
                "id": self.uuid,
                "user_id": "fake_user",
                "tenant_id": "fake_project",
                "updated": "2010-11-11T11:00:00Z",
                "created": "2010-10-10T12:00:00Z",
                "progress": 0,
                "name": "test_server",
                "status": "ACTIVE",
                "hostId": '',
                "image": {
                    "id": "5",
                    "links": [
                        {
                            "rel": "bookmark",
                            "href": image_bookmark,
                        },
                    ],
                },
                "flavor": {
                    "id": "1",
                  "links": [
                                            {
                          "rel": "bookmark",
                          "href": flavor_bookmark,
                      },
                  ],
                },
                "addresses": {
                    'test1': [
                        {'version': 4, 'addr': '192.168.1.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 6, 'addr': '2001:db8:0:1::1',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 4, 'addr': '192.168.2.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'bb:bb:bb:bb:bb:bb'}
                    ],
                    'test2': [
                        {'version': 4, 'addr': '192.168.3.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'cc:cc:cc:cc:cc:cc'},
                    ]
                },
                "metadata": {},
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
                "OS-DCF:diskConfig": "MANUAL",
                "accessIPv4": '',
                "accessIPv6": '',
            }
        }

        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))

    def test_build_server_detail_with_fault(self):
        self.instance['vm_state'] = vm_states.ERROR
        self.instance['fault'] = fake_instance.fake_fault_obj(
                                     self.request.context, self.uuid)

        image_bookmark = "http://localhost/fake/images/5"
        flavor_bookmark = "http://localhost/fake/flavors/1"
        expected_server = {
            "server": {
                "id": self.uuid,
                "user_id": "fake_user",
                "tenant_id": "fake_project",
                "updated": "2010-11-11T11:00:00Z",
                "created": "2010-10-10T12:00:00Z",
                "name": "test_server",
                "status": "ERROR",
                "hostId": '',
                "image": {
                    "id": "5",
                    "links": [
                        {
                            "rel": "bookmark",
                            "href": image_bookmark,
                        },
                    ],
                },
                "flavor": {
                    "id": "1",
                  "links": [
                                            {
                          "rel": "bookmark",
                          "href": flavor_bookmark,
                      },
                  ],
                },
                "addresses": {
                    'test1': [
                        {'version': 4, 'addr': '192.168.1.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 6, 'addr': '2001:db8:0:1::1',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 4, 'addr': '192.168.2.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'bb:bb:bb:bb:bb:bb'}
                    ],
                    'test2': [
                        {'version': 4, 'addr': '192.168.3.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'cc:cc:cc:cc:cc:cc'},
                    ]
                },
                "metadata": {},
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
                "fault": {
                    "code": 404,
                    "created": "2010-10-10T12:00:00Z",
                    "message": "HTTPNotFound",
                    "details": "Stock details for test",
                },
                "OS-DCF:diskConfig": "MANUAL",
                "accessIPv4": '',
                "accessIPv6": '',
            }
        }

        self.request.context = context.RequestContext('fake', 'fake')
        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))

    def test_build_server_detail_with_fault_that_has_been_deleted(self):
        self.instance['deleted'] = 1
        self.instance['vm_state'] = vm_states.ERROR
        fault = fake_instance.fake_fault_obj(self.request.context,
                                             self.uuid, code=500,
                                             message="No valid host was found")
        self.instance['fault'] = fault

        expected_fault = {"code": 500,
                          "created": "2010-10-10T12:00:00Z",
                          "message": "No valid host was found"}

        self.request.context = context.RequestContext('fake', 'fake')
        output = self.view_builder.show(self.request, self.instance)
        # Regardless of vm_state deleted servers should be DELETED
        self.assertEqual("DELETED", output['server']['status'])
        self.assertThat(output['server']['fault'],
                        matchers.DictMatches(expected_fault))

    def test_build_server_detail_with_fault_no_details_not_admin(self):
        self.instance['vm_state'] = vm_states.ERROR
        self.instance['fault'] = fake_instance.fake_fault_obj(
                                                   self.request.context,
                                                   self.uuid,
                                                   code=500,
                                                   message='Error')

        expected_fault = {"code": 500,
                          "created": "2010-10-10T12:00:00Z",
                          "message": "Error"}

        self.request.context = context.RequestContext('fake', 'fake')
        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output['server']['fault'],
                        matchers.DictMatches(expected_fault))

    def test_build_server_detail_with_fault_admin(self):
        self.instance['vm_state'] = vm_states.ERROR
        self.instance['fault'] = fake_instance.fake_fault_obj(
                                                   self.request.context,
                                                   self.uuid,
                                                   code=500,
                                                   message='Error')

        expected_fault = {"code": 500,
                          "created": "2010-10-10T12:00:00Z",
                          "message": "Error",
                          'details': 'Stock details for test'}

        self.request.environ['nova.context'].is_admin = True
        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output['server']['fault'],
                        matchers.DictMatches(expected_fault))

    def test_build_server_detail_with_fault_no_details_admin(self):
        self.instance['vm_state'] = vm_states.ERROR
        self.instance['fault'] = fake_instance.fake_fault_obj(
                                                   self.request.context,
                                                   self.uuid,
                                                   code=500,
                                                   message='Error',
                                                   details='')

        expected_fault = {"code": 500,
                          "created": "2010-10-10T12:00:00Z",
                          "message": "Error"}

        self.request.environ['nova.context'].is_admin = True
        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output['server']['fault'],
                        matchers.DictMatches(expected_fault))

    def test_build_server_detail_with_fault_but_active(self):
        self.instance['vm_state'] = vm_states.ACTIVE
        self.instance['progress'] = 100
        self.instance['fault'] = fake_instance.fake_fault_obj(
                                     self.request.context, self.uuid)

        output = self.view_builder.show(self.request, self.instance)
        self.assertNotIn('fault', output['server'])

    def test_build_server_detail_active_status(self):
        # set the power state of the instance to running
        self.instance['vm_state'] = vm_states.ACTIVE
        self.instance['progress'] = 100
        image_bookmark = "http://localhost/fake/images/5"
        flavor_bookmark = "http://localhost/fake/flavors/1"
        expected_server = {
            "server": {
                "id": self.uuid,
                "user_id": "fake_user",
                "tenant_id": "fake_project",
                "updated": "2010-11-11T11:00:00Z",
                "created": "2010-10-10T12:00:00Z",
                "progress": 100,
                "name": "test_server",
                "status": "ACTIVE",
                "hostId": '',
                "image": {
                    "id": "5",
                    "links": [
                        {
                            "rel": "bookmark",
                            "href": image_bookmark,
                        },
                    ],
                },
                "flavor": {
                    "id": "1",
                  "links": [
                                            {
                          "rel": "bookmark",
                          "href": flavor_bookmark,
                      },
                  ],
                },
                "addresses": {
                    'test1': [
                        {'version': 4, 'addr': '192.168.1.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 6, 'addr': '2001:db8:0:1::1',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 4, 'addr': '192.168.2.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'bb:bb:bb:bb:bb:bb'}
                    ],
                    'test2': [
                        {'version': 4, 'addr': '192.168.3.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'cc:cc:cc:cc:cc:cc'},
                    ]
                },
                "metadata": {},
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
                "OS-DCF:diskConfig": "MANUAL",
                "accessIPv4": '',
                "accessIPv6": '',
            }
        }

        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))

    def test_build_server_detail_with_metadata(self):

        metadata = []
        metadata.append(models.InstanceMetadata(key="Open", value="Stack"))
        metadata = nova_utils.metadata_to_dict(metadata)
        self.instance['metadata'] = metadata

        image_bookmark = "http://localhost/fake/images/5"
        flavor_bookmark = "http://localhost/fake/flavors/1"
        expected_server = {
            "server": {
                "id": self.uuid,
                "user_id": "fake_user",
                "tenant_id": "fake_project",
                "updated": "2010-11-11T11:00:00Z",
                "created": "2010-10-10T12:00:00Z",
                "progress": 0,
                "name": "test_server",
                "status": "ACTIVE",
                "hostId": '',
                "image": {
                    "id": "5",
                    "links": [
                        {
                            "rel": "bookmark",
                            "href": image_bookmark,
                        },
                    ],
                },
                "flavor": {
                    "id": "1",
                    "links": [
                                              {
                            "rel": "bookmark",
                            "href": flavor_bookmark,
                        },
                    ],
                },
                "addresses": {
                    'test1': [
                        {'version': 4, 'addr': '192.168.1.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 6, 'addr': '2001:db8:0:1::1',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'aa:aa:aa:aa:aa:aa'},
                        {'version': 4, 'addr': '192.168.2.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'bb:bb:bb:bb:bb:bb'}
                    ],
                    'test2': [
                        {'version': 4, 'addr': '192.168.3.100',
                         'OS-EXT-IPS:type': 'fixed',
                         'OS-EXT-IPS-MAC:mac_addr': 'cc:cc:cc:cc:cc:cc'},
                    ]
                },
                "metadata": {"Open": "Stack"},
                "links": [
                    {
                        "rel": "self",
                        "href": self.self_link,
                    },
                    {
                        "rel": "bookmark",
                        "href": self.bookmark_link,
                    },
                ],
                "OS-DCF:diskConfig": "MANUAL",
                "accessIPv4": '',
                "accessIPv6": '',
            }
        }

        output = self.view_builder.show(self.request, self.instance)
        self.assertThat(output, matchers.DictMatches(expected_server))


class ServersAllExtensionsTestCase(test.TestCase):
    """Servers tests using default API router with all extensions enabled.

    The intent here is to catch cases where extensions end up throwing
    an exception because of a malformed request before the core API
    gets a chance to validate the request and return a 422 response.

    For example, AccessIPsController extends servers.Controller::

        |   @wsgi.extends
        |   def create(self, req, resp_obj, body):
        |       context = req.environ['nova.context']
        |       if authorize(context) and 'server' in resp_obj.obj:
        |           resp_obj.attach(xml=AccessIPTemplate())
        |           server = resp_obj.obj['server']
        |           self._extend_server(req, server)

    we want to ensure that the extension isn't barfing on an invalid
    body.
    """

    def setUp(self):
        super(ServersAllExtensionsTestCase, self).setUp()
        self.app = compute.APIRouterV21()

    def test_create_missing_server(self):
        # Test create with malformed body.

        def fake_create(*args, **kwargs):
            raise test.TestingException("Should not reach the compute API.")

        self.stubs.Set(compute_api.API, 'create', fake_create)

        req = fakes.HTTPRequestV21.blank('/fake/servers')
        req.method = 'POST'
        req.content_type = 'application/json'
        body = {'foo': {'a': 'b'}}

        req.body = jsonutils.dump_as_bytes(body)
        res = req.get_response(self.app)
        self.assertEqual(400, res.status_int)

    def test_update_missing_server(self):
        # Test update with malformed body.

        req = fakes.HTTPRequestV21.blank('/fake/servers/1')
        req.method = 'PUT'
        req.content_type = 'application/json'
        body = {'foo': {'a': 'b'}}
        req.body = jsonutils.dump_as_bytes(body)
        with mock.patch('nova.objects.Instance.save') as mock_save:
            res = req.get_response(self.app)
            self.assertFalse(mock_save.called)
        self.assertEqual(400, res.status_int)


class ServersInvalidRequestTestCase(test.TestCase):
    """Tests of places we throw 400 Bad Request from."""

    def setUp(self):
        super(ServersInvalidRequestTestCase, self).setUp()
        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers.ServersController(extension_info=ext_info)

    def _invalid_server_create(self, body):
        req = fakes.HTTPRequestV21.blank('/fake/servers')
        req.method = 'POST'

        self.assertRaises(exception.ValidationError,
                          self.controller.create, req, body=body)

    def test_create_server_no_body(self):
        self._invalid_server_create(body=None)

    def test_create_server_missing_server(self):
        body = {'foo': {'a': 'b'}}
        self._invalid_server_create(body=body)

    def test_create_server_malformed_entity(self):
        body = {'server': 'string'}
        self._invalid_server_create(body=body)

    def _unprocessable_server_update(self, body):
        req = fakes.HTTPRequestV21.blank('/fake/servers/%s' % FAKE_UUID)
        req.method = 'PUT'

        self.assertRaises(webob.exc.HTTPBadRequest,
                          self.controller.update, req, FAKE_UUID, body=body)

    def test_update_server_no_body(self):
        self._invalid_server_create(body=None)

    def test_update_server_missing_server(self):
        body = {'foo': {'a': 'b'}}
        self._invalid_server_create(body=body)

    def test_create_update_malformed_entity(self):
        body = {'server': 'string'}
        self._invalid_server_create(body=body)


class FakeExt(extensions.V21APIExtensionBase):
    name = "DiskConfig"
    alias = 'os-disk-config'
    version = 1
    fake_schema = {'fake_ext_attr': {'type': 'string'}}

    def fake_extension_point(self, *args, **kwargs):
        pass

    def fake_schema_extension_point(self, version):
        if version in ('2.1', '2.19', '2.32', '2.37'):
            return self.fake_schema
        elif version == '2.0':
            return {}
        # This fake method should return the schema for expected version
        # Return None will make the tests failed, that means there is something
        # in the code.
        return None

    def get_controller_extensions(self):
        return []

    def get_resources(self):
        return []


# TODO(alex_xu): There isn't specified file for ips extension. Most of
# unittest related to ips extension is in this file. So put the ips policy
# enforcement tests at here until there is specified file for ips extension.
class IPsPolicyEnforcementV21(test.NoDBTestCase):

    def setUp(self):
        super(IPsPolicyEnforcementV21, self).setUp()
        self.controller = ips.IPsController()
        self.req = fakes.HTTPRequest.blank("/v2/fake")

    def test_index_policy_failed(self):
        rule_name = "os_compute_api:ips:index"
        self.policy.set_rules({rule_name: "project:non_fake"})
        exc = self.assertRaises(
            exception.PolicyNotAuthorized,
            self.controller.index, self.req, fakes.FAKE_UUID)
        self.assertEqual(
            "Policy doesn't allow %s to be performed." % rule_name,
            exc.format_message())

    def test_show_policy_failed(self):
        rule_name = "os_compute_api:ips:show"
        self.policy.set_rules({rule_name: "project:non_fake"})
        exc = self.assertRaises(
            exception.PolicyNotAuthorized,
            self.controller.show, self.req, fakes.FAKE_UUID, fakes.FAKE_UUID)
        self.assertEqual(
            "Policy doesn't allow %s to be performed." % rule_name,
            exc.format_message())


class ServersPolicyEnforcementV21(test.NoDBTestCase):

    def setUp(self):
        super(ServersPolicyEnforcementV21, self).setUp()
        ext_info = extension_info.LoadedExtensionInfo()
        ext_info.extensions.update({'os-networks': 'fake'})
        self.controller = servers.ServersController(extension_info=ext_info)
        self.req = fakes.HTTPRequest.blank('')
        self.image_uuid = '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6'

    def _common_policy_check(self, rules, rule_name, func, *arg, **kwarg):
        self.policy.set_rules(rules)
        exc = self.assertRaises(
            exception.PolicyNotAuthorized, func, *arg, **kwarg)
        self.assertEqual(
            "Policy doesn't allow %s to be performed." % rule_name,
            exc.format_message())

    @mock.patch.object(servers.ServersController, '_get_instance')
    def test_start_policy_failed(self, _get_instance_mock):
        _get_instance_mock.return_value = None
        rule_name = "os_compute_api:servers:start"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller._start_server,
            self.req, FAKE_UUID, body={})

    @mock.patch.object(servers.ServersController, '_get_instance')
    def test_trigger_crash_dump_policy_failed_with_other_project(
        self, _get_instance_mock):
        _get_instance_mock.return_value = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'])
        rule_name = "os_compute_api:servers:trigger_crash_dump"
        rule = {rule_name: "project_id:%(project_id)s"}
        self.req.api_version_request =\
            api_version_request.APIVersionRequest('2.17')
        # Change the project_id in request context.
        self.req.environ['nova.context'].project_id = 'other-project'
        self._common_policy_check(
            rule, rule_name, self.controller._action_trigger_crash_dump,
            self.req, FAKE_UUID, body={'trigger_crash_dump': None})

    @mock.patch('nova.compute.api.API.trigger_crash_dump')
    @mock.patch.object(servers.ServersController, '_get_instance')
    def test_trigger_crash_dump_overridden_policy_pass_with_same_project(
        self, _get_instance_mock, trigger_crash_dump_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            project_id=self.req.environ['nova.context'].project_id)
        _get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:trigger_crash_dump"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        self.req.api_version_request = (
            api_version_request.APIVersionRequest('2.17'))
        self.controller._action_trigger_crash_dump(
            self.req, fakes.FAKE_UUID, body={'trigger_crash_dump': None})
        trigger_crash_dump_mock.assert_called_once_with(
            self.req.environ['nova.context'], instance)

    @mock.patch.object(servers.ServersController, '_get_instance')
    def test_trigger_crash_dump_overridden_policy_failed_with_other_user(
        self, _get_instance_mock):
        _get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:trigger_crash_dump"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        # Change the user_id in request context.
        self.req.environ['nova.context'].user_id = 'other-user'
        self.req.api_version_request = (
            api_version_request.APIVersionRequest('2.17'))
        exc = self.assertRaises(exception.PolicyNotAuthorized,
                                self.controller._action_trigger_crash_dump,
                                self.req,
                                fakes.FAKE_UUID,
                                body={'trigger_crash_dump': None})
        self.assertEqual(
                      "Policy doesn't allow %s to be performed." % rule_name,
                      exc.format_message())

    @mock.patch('nova.compute.api.API.trigger_crash_dump')
    @mock.patch.object(servers.ServersController, '_get_instance')
    def test_trigger_crash_dump_overridden_policy_pass_with_same_user(
        self, _get_instance_mock, trigger_crash_dump_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            user_id=self.req.environ['nova.context'].user_id)
        _get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:trigger_crash_dump"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        self.req.api_version_request = (
            api_version_request.APIVersionRequest('2.17'))
        self.controller._action_trigger_crash_dump(
            self.req, fakes.FAKE_UUID, body={'trigger_crash_dump': None})
        trigger_crash_dump_mock.assert_called_once_with(
            self.req.environ['nova.context'], instance)

    def test_index_policy_failed(self):
        rule_name = "os_compute_api:servers:index"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller.index, self.req)

    def test_detail_policy_failed(self):
        rule_name = "os_compute_api:servers:detail"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller.detail, self.req)

    def test_detail_get_tenants_policy_failed(self):
        req = fakes.HTTPRequest.blank('')
        req.GET["all_tenants"] = "True"
        rule_name = "os_compute_api:servers:detail:get_all_tenants"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller._get_servers, req, True)

    def test_index_get_tenants_policy_failed(self):
        req = fakes.HTTPRequest.blank('')
        req.GET["all_tenants"] = "True"
        rule_name = "os_compute_api:servers:index:get_all_tenants"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller._get_servers, req, False)

    @mock.patch.object(common, 'get_instance')
    def test_show_policy_failed(self, get_instance_mock):
        get_instance_mock.return_value = None
        rule_name = "os_compute_api:servers:show"
        rule = {rule_name: "project:non_fake"}
        self._common_policy_check(
            rule, rule_name, self.controller.show, self.req, FAKE_UUID)

    @mock.patch.object(common, 'get_instance')
    def test_delete_policy_failed_with_other_project(self, get_instance_mock):
        get_instance_mock.return_value = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'])
        rule_name = "os_compute_api:servers:delete"
        rule = {rule_name: "project_id:%(project_id)s"}
        # Change the project_id in request context.
        self.req.environ['nova.context'].project_id = 'other-project'
        self._common_policy_check(
            rule, rule_name, self.controller.delete, self.req, FAKE_UUID)

    @mock.patch('nova.compute.api.API.soft_delete')
    @mock.patch('nova.api.openstack.common.get_instance')
    def test_delete_overridden_policy_pass_with_same_project(self,
                                                             get_instance_mock,
                                                             soft_delete_mock):
        self.flags(reclaim_instance_interval=3600)
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            project_id=self.req.environ['nova.context'].project_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:delete"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        self.controller.delete(self.req, fakes.FAKE_UUID)
        soft_delete_mock.assert_called_once_with(
            self.req.environ['nova.context'], instance)

    @mock.patch('nova.api.openstack.common.get_instance')
    def test_delete_overridden_policy_failed_with_other_user_in_same_project(
        self, get_instance_mock):
        get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:delete"
        rule = {rule_name: "user_id:%(user_id)s"}
        # Change the user_id in request context.
        self.req.environ['nova.context'].user_id = 'other-user'
        self._common_policy_check(
            rule, rule_name, self.controller.delete, self.req, FAKE_UUID)

    @mock.patch('nova.compute.api.API.soft_delete')
    @mock.patch('nova.api.openstack.common.get_instance')
    def test_delete_overridden_policy_pass_with_same_user(self,
                                                        get_instance_mock,
                                                        soft_delete_mock):
        self.flags(reclaim_instance_interval=3600)
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            user_id=self.req.environ['nova.context'].user_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:delete"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        self.controller.delete(self.req, fakes.FAKE_UUID)
        soft_delete_mock.assert_called_once_with(
            self.req.environ['nova.context'], instance)

    @mock.patch.object(common, 'get_instance')
    def test_update_policy_failed_with_other_project(self, get_instance_mock):
        get_instance_mock.return_value = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'])
        rule_name = "os_compute_api:servers:update"
        rule = {rule_name: "project_id:%(project_id)s"}
        body = {'server': {'name': 'server_test'}}
        # Change the project_id in request context.
        self.req.environ['nova.context'].project_id = 'other-project'
        self._common_policy_check(
            rule, rule_name, self.controller.update, self.req,
            FAKE_UUID, body=body)

    @mock.patch('nova.api.openstack.compute.views.servers.ViewBuilder.show')
    @mock.patch.object(compute_api.API, 'update_instance')
    @mock.patch.object(common, 'get_instance')
    def test_update_overridden_policy_pass_with_same_project(
        self, get_instance_mock, update_instance_mock, view_show_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            project_id=self.req.environ['nova.context'].project_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:update"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        body = {'server': {'name': 'server_test'}}
        self.controller.update(self.req, fakes.FAKE_UUID, body=body)

    @mock.patch.object(common, 'get_instance')
    def test_update_overridden_policy_failed_with_other_user_in_same_project(
        self, get_instance_mock):
        get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:update"
        rule = {rule_name: "user_id:%(user_id)s"}
        # Change the user_id in request context.
        self.req.environ['nova.context'].user_id = 'other-user'
        body = {'server': {'name': 'server_test'}}
        self._common_policy_check(
            rule, rule_name, self.controller.update, self.req,
            FAKE_UUID, body=body)

    @mock.patch('nova.api.openstack.compute.views.servers.ViewBuilder.show')
    @mock.patch.object(compute_api.API, 'update_instance')
    @mock.patch.object(common, 'get_instance')
    def test_update_overridden_policy_pass_with_same_user(self,
                                                          get_instance_mock,
                                                          update_instance_mock,
                                                          view_show_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            user_id=self.req.environ['nova.context'].user_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:update"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        body = {'server': {'name': 'server_test'}}
        self.controller.update(self.req, fakes.FAKE_UUID, body=body)

    def test_confirm_resize_policy_failed(self):
        rule_name = "os_compute_api:servers:confirm_resize"
        rule = {rule_name: "project:non_fake"}
        body = {'server': {'name': 'server_test'}}
        self._common_policy_check(
            rule, rule_name, self.controller._action_confirm_resize,
            self.req, FAKE_UUID, body=body)

    def test_revert_resize_policy_failed(self):
        rule_name = "os_compute_api:servers:revert_resize"
        rule = {rule_name: "project:non_fake"}
        body = {'server': {'name': 'server_test'}}
        self._common_policy_check(
            rule, rule_name, self.controller._action_revert_resize,
            self.req, FAKE_UUID, body=body)

    def test_reboot_policy_failed(self):
        rule_name = "os_compute_api:servers:reboot"
        rule = {rule_name: "project:non_fake"}
        body = {'reboot': {'type': 'HARD'}}
        self._common_policy_check(
            rule, rule_name, self.controller._action_reboot,
            self.req, FAKE_UUID, body=body)

    @mock.patch('nova.api.openstack.common.get_instance')
    def test_resize_policy_failed_with_other_project(self, get_instance_mock):
        get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:resize"
        rule = {rule_name: "project_id:%(project_id)s"}
        body = {'resize': {'flavorRef': '1'}}
        # Change the project_id in request context.
        self.req.environ['nova.context'].project_id = 'other-project'
        self._common_policy_check(
            rule, rule_name, self.controller._action_resize, self.req,
            FAKE_UUID, body=body)

    @mock.patch('nova.compute.api.API.resize')
    @mock.patch('nova.api.openstack.common.get_instance')
    def test_resize_overridden_policy_pass_with_same_project(self,
                                                             get_instance_mock,
                                                             resize_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            project_id=self.req.environ['nova.context'].project_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:resize"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        body = {'resize': {'flavorRef': '1'}}
        self.controller._action_resize(self.req, fakes.FAKE_UUID, body=body)
        resize_mock.assert_called_once_with(self.req.environ['nova.context'],
                                            instance, '1')

    @mock.patch('nova.api.openstack.common.get_instance')
    def test_resize_overridden_policy_failed_with_other_user_in_same_project(
        self, get_instance_mock):
        get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:resize"
        rule = {rule_name: "user_id:%(user_id)s"}
        # Change the user_id in request context.
        self.req.environ['nova.context'].user_id = 'other-user'
        body = {'resize': {'flavorRef': '1'}}
        self._common_policy_check(
            rule, rule_name, self.controller._action_resize, self.req,
            FAKE_UUID, body=body)

    @mock.patch('nova.compute.api.API.resize')
    @mock.patch('nova.api.openstack.common.get_instance')
    def test_resize_overridden_policy_pass_with_same_user(self,
                                                        get_instance_mock,
                                                        resize_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            user_id=self.req.environ['nova.context'].user_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:resize"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        body = {'resize': {'flavorRef': '1'}}
        self.controller._action_resize(self.req, fakes.FAKE_UUID, body=body)
        resize_mock.assert_called_once_with(self.req.environ['nova.context'],
                                            instance, '1')

    @mock.patch('nova.api.openstack.common.get_instance')
    def test_rebuild_policy_failed_with_other_project(self, get_instance_mock):
        get_instance_mock.return_value = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            project_id=self.req.environ['nova.context'].project_id)
        rule_name = "os_compute_api:servers:rebuild"
        rule = {rule_name: "project_id:%(project_id)s"}
        body = {'rebuild': {'imageRef': self.image_uuid}}
        # Change the project_id in request context.
        self.req.environ['nova.context'].project_id = 'other-project'
        self._common_policy_check(
            rule, rule_name, self.controller._action_rebuild,
            self.req, FAKE_UUID, body=body)

    @mock.patch('nova.api.openstack.common.get_instance')
    def test_rebuild_overridden_policy_failed_with_other_user_in_same_project(
        self, get_instance_mock):
        get_instance_mock.return_value = (
            fake_instance.fake_instance_obj(self.req.environ['nova.context']))
        rule_name = "os_compute_api:servers:rebuild"
        rule = {rule_name: "user_id:%(user_id)s"}
        body = {'rebuild': {'imageRef': self.image_uuid}}
        # Change the user_id in request context.
        self.req.environ['nova.context'].user_id = 'other-user'
        self._common_policy_check(
            rule, rule_name, self.controller._action_rebuild,
            self.req, FAKE_UUID, body=body)

    @mock.patch('nova.api.openstack.compute.views.servers.ViewBuilder.show')
    @mock.patch('nova.compute.api.API.rebuild')
    @mock.patch('nova.api.openstack.common.get_instance')
    def test_rebuild_overridden_policy_pass_with_same_user(self,
                                                           get_instance_mock,
                                                           rebuild_mock,
                                                           view_show_mock):
        instance = fake_instance.fake_instance_obj(
            self.req.environ['nova.context'],
            user_id=self.req.environ['nova.context'].user_id)
        get_instance_mock.return_value = instance
        rule_name = "os_compute_api:servers:rebuild"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        body = {'rebuild': {'imageRef': self.image_uuid,
                            'adminPass': 'dumpy_password'}}
        self.controller._action_rebuild(self.req, fakes.FAKE_UUID, body=body)
        rebuild_mock.assert_called_once_with(self.req.environ['nova.context'],
                                             instance,
                                             self.image_uuid,
                                             'dumpy_password')

    def test_create_image_policy_failed(self):
        rule_name = "os_compute_api:servers:create_image"
        rule = {rule_name: "project:non_fake"}
        body = {
            'createImage': {
                'name': 'Snapshot 1',
            },
        }
        self._common_policy_check(
            rule, rule_name, self.controller._action_create_image,
            self.req, FAKE_UUID, body=body)

    @mock.patch('nova.compute.utils.is_volume_backed_instance',
                return_value=True)
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(servers.ServersController, '_get_server')
    def test_create_vol_backed_img_snapshotting_policy_blocks_project(self,
                                                         mock_get_server,
                                                         mock_get_uuidi,
                                                         mock_is_vol_back):
        """Don't permit a snapshot of a volume backed instance if configured
        not to based on project
        """
        rule_name = "os_compute_api:servers:create_image:allow_volume_backed"
        rules = {
                rule_name: "project:non_fake",
                "os_compute_api:servers:create_image": "",
        }
        body = {
            'createImage': {
                'name': 'Snapshot 1',
            },
        }
        self._common_policy_check(
            rules, rule_name, self.controller._action_create_image,
            self.req, FAKE_UUID, body=body)

    @mock.patch('nova.compute.utils.is_volume_backed_instance',
                return_value=True)
    @mock.patch.object(objects.BlockDeviceMappingList, 'get_by_instance_uuid')
    @mock.patch.object(servers.ServersController, '_get_server')
    def test_create_vol_backed_img_snapshotting_policy_blocks_role(self,
                                                         mock_get_server,
                                                         mock_get_uuidi,
                                                         mock_is_vol_back):
        """Don't permit a snapshot of a volume backed instance if configured
        not to based on role
        """
        rule_name = "os_compute_api:servers:create_image:allow_volume_backed"
        rules = {
                rule_name: "role:non_fake",
                "os_compute_api:servers:create_image": "",
        }
        body = {
            'createImage': {
                'name': 'Snapshot 1',
            },
        }
        self._common_policy_check(
            rules, rule_name, self.controller._action_create_image,
            self.req, FAKE_UUID, body=body)

    def _create_policy_check(self, rules, rule_name):
        flavor_ref = 'http://localhost/123/flavors/3'
        body = {
            'server': {
                'name': 'server_test',
                'imageRef': self.image_uuid,
                'flavorRef': flavor_ref,
                'availability_zone': "zone1:host1:node1",
                'block_device_mapping': [{'device_name': "/dev/sda1"}],
                'networks': [{'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'}],
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                },
            },
        }
        self._common_policy_check(
            rules, rule_name, self.controller.create, self.req, body=body)

    def test_create_policy_failed(self):
        rule_name = "os_compute_api:servers:create"
        rules = {rule_name: "project:non_fake"}
        self._create_policy_check(rules, rule_name)

    def test_create_forced_host_policy_failed(self):
        rule_name = "os_compute_api:servers:create:forced_host"
        rule = {"os_compute_api:servers:create": "@",
                rule_name: "project:non_fake"}
        self._create_policy_check(rule, rule_name)

    def test_create_attach_volume_policy_failed(self):
        rule_name = "os_compute_api:servers:create:attach_volume"
        rules = {"os_compute_api:servers:create": "@",
                 "os_compute_api:servers:create:forced_host": "@",
                 rule_name: "project:non_fake"}
        self._create_policy_check(rules, rule_name)

    def test_create_attach_attach_network_policy_failed(self):
        rule_name = "os_compute_api:servers:create:attach_network"
        rules = {"os_compute_api:servers:create": "@",
                 "os_compute_api:servers:create:forced_host": "@",
                 "os_compute_api:servers:create:attach_volume": "@",
                 rule_name: "project:non_fake"}
        self._create_policy_check(rules, rule_name)
