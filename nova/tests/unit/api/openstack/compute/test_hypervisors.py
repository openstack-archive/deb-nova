# Copyright (c) 2012 OpenStack Foundation
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

import copy
import mock
import netaddr
from oslo_serialization import jsonutils
from webob import exc

from nova.api.openstack.compute import hypervisors \
        as hypervisors_v21
from nova.cells import utils as cells_utils
from nova import exception
from nova import objects
from nova import test
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance
from nova.tests import uuidsentinel as uuids

CPU_INFO = """
{"arch": "x86_64",
"vendor": "fake",
"topology": {"cores": 1, "threads": 1, "sockets": 1},
"features": [],
"model": ""}"""

TEST_HYPERS = [
    dict(id=1,
         service_id=1,
         host="compute1",
         vcpus=4,
         memory_mb=10 * 1024,
         local_gb=250,
         vcpus_used=2,
         memory_mb_used=5 * 1024,
         local_gb_used=125,
         hypervisor_type="xen",
         hypervisor_version=3,
         hypervisor_hostname="hyper1",
         free_ram_mb=5 * 1024,
         free_disk_gb=125,
         current_workload=2,
         running_vms=2,
         cpu_info=CPU_INFO,
         disk_available_least=100,
         host_ip=netaddr.IPAddress('1.1.1.1')),
    dict(id=2,
         service_id=2,
         host="compute2",
         vcpus=4,
         memory_mb=10 * 1024,
         local_gb=250,
         vcpus_used=2,
         memory_mb_used=5 * 1024,
         local_gb_used=125,
         hypervisor_type="xen",
         hypervisor_version=3,
         hypervisor_hostname="hyper2",
         free_ram_mb=5 * 1024,
         free_disk_gb=125,
         current_workload=2,
         running_vms=2,
         cpu_info=CPU_INFO,
         disk_available_least=100,
         host_ip=netaddr.IPAddress('2.2.2.2'))]


TEST_SERVICES = [
    objects.Service(id=1,
                    host="compute1",
                    binary="nova-compute",
                    topic="compute_topic",
                    report_count=5,
                    disabled=False,
                    disabled_reason=None,
                    availability_zone="nova"),
    objects.Service(id=2,
                    host="compute2",
                    binary="nova-compute",
                    topic="compute_topic",
                    report_count=5,
                    disabled=False,
                    disabled_reason=None,
                    availability_zone="nova"),
]

TEST_HYPERS_OBJ = [objects.ComputeNode(**hyper_dct)
                   for hyper_dct in TEST_HYPERS]

TEST_HYPERS[0].update({'service': TEST_SERVICES[0]})
TEST_HYPERS[1].update({'service': TEST_SERVICES[1]})

TEST_SERVERS = [dict(name="inst1", uuid=uuids.instance_1, host="compute1"),
                dict(name="inst2", uuid=uuids.instance_2, host="compute2"),
                dict(name="inst3", uuid=uuids.instance_3, host="compute1"),
                dict(name="inst4", uuid=uuids.instance_4, host="compute2")]


def fake_compute_node_get_all(context, limit=None, marker=None):
    if marker in ['99999']:
        raise exception.MarkerNotFound(marker)
    marker_found = True if marker is None else False
    output = []
    for hyper in TEST_HYPERS_OBJ:
        if not marker_found and marker == str(hyper.id):
            marker_found = True
        elif marker_found:
            if limit is None or len(output) < int(limit):
                output.append(hyper)
    return output


def fake_compute_node_search_by_hypervisor(context, hypervisor_re):
    return TEST_HYPERS_OBJ


def fake_compute_node_get(context, compute_id):
    for hyper in TEST_HYPERS_OBJ:
        if hyper.id == int(compute_id):
            return hyper
    raise exception.ComputeHostNotFound(host=compute_id)


def fake_service_get_by_compute_host(context, host):
    for service in TEST_SERVICES:
        if service.host == host:
            return service


def fake_compute_node_statistics(context):
    result = dict(
        count=0,
        vcpus=0,
        memory_mb=0,
        local_gb=0,
        vcpus_used=0,
        memory_mb_used=0,
        local_gb_used=0,
        free_ram_mb=0,
        free_disk_gb=0,
        current_workload=0,
        running_vms=0,
        disk_available_least=0,
        )

    for hyper in TEST_HYPERS_OBJ:
        for key in result:
            if key == 'count':
                result[key] += 1
            else:
                result[key] += getattr(hyper, key)

    return result


def fake_instance_get_all_by_host(context, host):
    results = []
    for inst in TEST_SERVERS:
        if inst['host'] == host:
            inst_obj = fake_instance.fake_instance_obj(context, **inst)
            results.append(inst_obj)
    return results


class HypervisorsTestV21(test.NoDBTestCase):
    api_version = '2.1'

    # copying the objects locally so the cells testcases can provide their own
    TEST_HYPERS_OBJ = copy.deepcopy(TEST_HYPERS_OBJ)
    TEST_SERVICES = copy.deepcopy(TEST_SERVICES)
    TEST_SERVERS = copy.deepcopy(TEST_SERVERS)

    DETAIL_HYPERS_DICTS = copy.deepcopy(TEST_HYPERS)
    del DETAIL_HYPERS_DICTS[0]['service_id']
    del DETAIL_HYPERS_DICTS[1]['service_id']
    del DETAIL_HYPERS_DICTS[0]['host']
    del DETAIL_HYPERS_DICTS[1]['host']
    DETAIL_HYPERS_DICTS[0].update({'state': 'up',
                           'status': 'enabled',
                           'service': dict(id=1, host='compute1',
                                        disabled_reason=None)})
    DETAIL_HYPERS_DICTS[1].update({'state': 'up',
                           'status': 'enabled',
                           'service': dict(id=2, host='compute2',
                                        disabled_reason=None)})
    INDEX_HYPER_DICTS = [
        dict(id=1, hypervisor_hostname="hyper1",
             state='up', status='enabled'),
        dict(id=2, hypervisor_hostname="hyper2",
             state='up', status='enabled')]
    DETAIL_NULL_CPUINFO_DICT = {'': '', None: None}

    def _get_request(self, use_admin_context, url=''):
        return fakes.HTTPRequest.blank(url,
                                       use_admin_context=use_admin_context,
                                       version=self.api_version)

    def _set_up_controller(self):
        self.controller = hypervisors_v21.HypervisorsController()
        self.controller.servicegroup_api.service_is_up = mock.MagicMock(
            return_value=True)

    def setUp(self):
        super(HypervisorsTestV21, self).setUp()
        self._set_up_controller()
        self.rule_hyp_show = "os_compute_api:os-hypervisors"
        self.stubs.Set(self.controller.host_api, 'compute_node_get_all',
                       fake_compute_node_get_all)
        self.stubs.Set(self.controller.host_api, 'service_get_by_compute_host',
                       fake_service_get_by_compute_host)
        self.stubs.Set(self.controller.host_api,
                       'compute_node_search_by_hypervisor',
                       fake_compute_node_search_by_hypervisor)
        self.stubs.Set(self.controller.host_api, 'compute_node_get',
                       fake_compute_node_get)
        self.stub_out('nova.db.compute_node_statistics',
                      fake_compute_node_statistics)

    def test_view_hypervisor_nodetail_noservers(self):
        req = self._get_request(True)
        result = self.controller._view_hypervisor(
            self.TEST_HYPERS_OBJ[0], self.TEST_SERVICES[0], False, req)

        self.assertEqual(result, self.INDEX_HYPER_DICTS[0])

    def test_view_hypervisor_detail_noservers(self):
        req = self._get_request(True)
        result = self.controller._view_hypervisor(
            self.TEST_HYPERS_OBJ[0], self.TEST_SERVICES[0], True, req)

        self.assertEqual(result, self.DETAIL_HYPERS_DICTS[0])

    def test_view_hypervisor_servers(self):
        req = self._get_request(True)
        result = self.controller._view_hypervisor(self.TEST_HYPERS_OBJ[0],
                                                  self.TEST_SERVICES[0],
                                                  False, req,
                                                  self.TEST_SERVERS)
        expected_dict = copy.deepcopy(self.INDEX_HYPER_DICTS[0])
        expected_dict.update({'servers': [
                                  dict(name="inst1", uuid=uuids.instance_1),
                                  dict(name="inst2", uuid=uuids.instance_2),
                                  dict(name="inst3", uuid=uuids.instance_3),
                                  dict(name="inst4", uuid=uuids.instance_4)]})

        self.assertEqual(result, expected_dict)

    def _test_view_hypervisor_detail_cpuinfo_null(self, cpu_info):
        req = self._get_request(True)

        test_hypervisor_obj = copy.deepcopy(self.TEST_HYPERS_OBJ[0])
        test_hypervisor_obj.cpu_info = cpu_info
        result = self.controller._view_hypervisor(test_hypervisor_obj,
                                                  self.TEST_SERVICES[0],
                                                  True, req)

        expected_dict = copy.deepcopy(self.DETAIL_HYPERS_DICTS[0])
        expected_dict.update({'cpu_info':
                              self.DETAIL_NULL_CPUINFO_DICT[cpu_info]})
        self.assertEqual(result, expected_dict)

    def test_view_hypervisor_detail_cpuinfo_empty_string(self):
        self._test_view_hypervisor_detail_cpuinfo_null('')

    def test_view_hypervisor_detail_cpuinfo_none(self):
        self._test_view_hypervisor_detail_cpuinfo_null(None)

    def test_index(self):
        req = self._get_request(True)
        result = self.controller.index(req)

        self.assertEqual(result, dict(hypervisors=self.INDEX_HYPER_DICTS))

    def test_index_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.index, req)

    def test_detail(self):
        req = self._get_request(True)
        result = self.controller.detail(req)

        self.assertEqual(result, dict(hypervisors=self.DETAIL_HYPERS_DICTS))

    def test_detail_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.detail, req)

    def test_show_noid(self):
        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound, self.controller.show, req, '3')

    def test_show_non_integer_id(self):
        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound, self.controller.show, req, 'abc')

    def test_show_withid(self):
        req = self._get_request(True)
        result = self.controller.show(req, self.TEST_HYPERS_OBJ[0].id)

        self.assertEqual(result, dict(hypervisor=self.DETAIL_HYPERS_DICTS[0]))

    def test_show_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.show, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_uptime_noid(self):
        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound, self.controller.uptime, req, '3')

    def test_uptime_notimplemented(self):
        def fake_get_host_uptime(context, hyp):
            raise exc.HTTPNotImplemented()

        self.stubs.Set(self.controller.host_api, 'get_host_uptime',
                       fake_get_host_uptime)

        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotImplemented,
                          self.controller.uptime, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_uptime_implemented(self):
        def fake_get_host_uptime(context, hyp):
            return "fake uptime"

        self.stubs.Set(self.controller.host_api, 'get_host_uptime',
                       fake_get_host_uptime)

        req = self._get_request(True)
        result = self.controller.uptime(req, self.TEST_HYPERS_OBJ[0].id)

        expected_dict = copy.deepcopy(self.INDEX_HYPER_DICTS[0])
        expected_dict.update({'uptime': "fake uptime"})
        self.assertEqual(result, dict(hypervisor=expected_dict))

    def test_uptime_non_integer_id(self):
        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound, self.controller.uptime, req, 'abc')

    def test_uptime_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.uptime, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_uptime_hypervisor_down(self):
        def fake_get_host_uptime(context, hyp):
            raise exception.ComputeServiceUnavailable(host='dummy')

        self.stubs.Set(self.controller.host_api, 'get_host_uptime',
                       fake_get_host_uptime)

        req = self._get_request(True)
        self.assertRaises(exc.HTTPBadRequest,
                          self.controller.uptime, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_search(self):
        req = self._get_request(True)
        result = self.controller.search(req, 'hyper')

        self.assertEqual(result, dict(hypervisors=self.INDEX_HYPER_DICTS))

    def test_search_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.search, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_search_non_exist(self):
        def fake_compute_node_search_by_hypervisor_return_empty(context,
                                                                hypervisor_re):
            return []
        self.stubs.Set(self.controller.host_api,
                       'compute_node_search_by_hypervisor',
                       fake_compute_node_search_by_hypervisor_return_empty)
        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound, self.controller.search, req, 'a')

    @mock.patch.object(objects.InstanceList, 'get_by_host',
                       side_effect=fake_instance_get_all_by_host)
    def test_servers(self, mock_get):
        req = self._get_request(True)
        result = self.controller.servers(req, 'hyper')

        expected_dict = copy.deepcopy(self.INDEX_HYPER_DICTS)
        expected_dict[0].update({'servers': [
                                     dict(uuid=uuids.instance_1),
                                     dict(uuid=uuids.instance_3)]})
        expected_dict[1].update({'servers': [
                                     dict(uuid=uuids.instance_2),
                                     dict(uuid=uuids.instance_4)]})

        for output in result['hypervisors']:
            servers = output['servers']
            for server in servers:
                del server['name']
        self.assertEqual(result, dict(hypervisors=expected_dict))

    def test_servers_non_id(self):
        def fake_compute_node_search_by_hypervisor_return_empty(context,
                                                                hypervisor_re):
            return []
        self.stubs.Set(self.controller.host_api,
                       'compute_node_search_by_hypervisor',
                       fake_compute_node_search_by_hypervisor_return_empty)

        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound,
                          self.controller.servers,
                          req, '115')

    def test_servers_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.servers, req,
                          self.TEST_HYPERS_OBJ[0].id)

    def test_servers_with_non_integer_hypervisor_id(self):
        def fake_compute_node_search_by_hypervisor_return_empty(context,
                                                                hypervisor_re):
            return []
        self.stubs.Set(self.controller.host_api,
                       'compute_node_search_by_hypervisor',
                       fake_compute_node_search_by_hypervisor_return_empty)

        req = self._get_request(True)
        self.assertRaises(exc.HTTPNotFound,
                          self.controller.servers, req, 'abc')

    def test_servers_with_no_server(self):
        def fake_instance_get_all_by_host_return_empty(context, hypervisor_re):
            return []
        self.stubs.Set(self.controller.host_api, 'instance_get_all_by_host',
                       fake_instance_get_all_by_host_return_empty)
        req = self._get_request(True)
        result = self.controller.servers(req, self.TEST_HYPERS_OBJ[0].id)
        self.assertEqual(result, dict(hypervisors=self.INDEX_HYPER_DICTS))

    def test_statistics(self):
        req = self._get_request(True)
        result = self.controller.statistics(req)

        self.assertEqual(result, dict(hypervisor_statistics=dict(
                    count=2,
                    vcpus=8,
                    memory_mb=20 * 1024,
                    local_gb=500,
                    vcpus_used=4,
                    memory_mb_used=10 * 1024,
                    local_gb_used=250,
                    free_ram_mb=10 * 1024,
                    free_disk_gb=250,
                    current_workload=4,
                    running_vms=4,
                    disk_available_least=200)))

    def test_statistics_non_admin(self):
        req = self._get_request(False)
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller.statistics, req)


_CELL_PATH = 'cell1'


class CellHypervisorsTestV21(HypervisorsTestV21):
    TEST_HYPERS_OBJ = [cells_utils.ComputeNodeProxy(obj, _CELL_PATH)
                       for obj in TEST_HYPERS_OBJ]
    TEST_SERVICES = [cells_utils.ServiceProxy(obj, _CELL_PATH)
                     for obj in TEST_SERVICES]

    TEST_SERVERS = [dict(server,
                         host=cells_utils.cell_with_item(_CELL_PATH,
                                                         server['host']))
                    for server in TEST_SERVERS]

    DETAIL_HYPERS_DICTS = copy.deepcopy(HypervisorsTestV21.DETAIL_HYPERS_DICTS)
    DETAIL_HYPERS_DICTS = [dict(hyp, id=cells_utils.cell_with_item(_CELL_PATH,
                                                                   hyp['id']),
                                service=dict(hyp['service'],
                                             id=cells_utils.cell_with_item(
                                                 _CELL_PATH,
                                                 hyp['service']['id']),
                                             host=cells_utils.cell_with_item(
                                                 _CELL_PATH,
                                                 hyp['service']['host'])))
                           for hyp in DETAIL_HYPERS_DICTS]

    INDEX_HYPER_DICTS = copy.deepcopy(HypervisorsTestV21.INDEX_HYPER_DICTS)
    INDEX_HYPER_DICTS = [dict(hyp, id=cells_utils.cell_with_item(_CELL_PATH,
                                                                 hyp['id']))
                         for hyp in INDEX_HYPER_DICTS]

    # __deepcopy__ is added for copying an object locally in
    # _test_view_hypervisor_detail_cpuinfo_null
    cells_utils.ComputeNodeProxy.__deepcopy__ = (lambda self, memo:
        cells_utils.ComputeNodeProxy(copy.deepcopy(self._obj, memo),
                                     self._cell_path))

    @classmethod
    def fake_compute_node_get_all(cls, context, limit=None, marker=None):
        return cls.TEST_HYPERS_OBJ

    @classmethod
    def fake_compute_node_search_by_hypervisor(cls, context, hypervisor_re):
        return cls.TEST_HYPERS_OBJ

    @classmethod
    def fake_compute_node_get(cls, context, compute_id):
        for hyper in cls.TEST_HYPERS_OBJ:
            if hyper.id == compute_id:
                return hyper
        raise exception.ComputeHostNotFound(host=compute_id)

    @classmethod
    def fake_service_get_by_compute_host(cls, context, host):
        for service in cls.TEST_SERVICES:
            if service.host == host:
                return service

    @classmethod
    def fake_instance_get_all_by_host(cls, context, host):
        results = []
        for inst in cls.TEST_SERVERS:
            if inst['host'] == host:
                results.append(inst)
        return results

    def setUp(self):

        self.flags(enable=True, cell_type='api', group='cells')

        super(CellHypervisorsTestV21, self).setUp()

        self.stubs.Set(self.controller.host_api, 'compute_node_get_all',
                       self.fake_compute_node_get_all)
        self.stubs.Set(self.controller.host_api, 'service_get_by_compute_host',
                       self.fake_service_get_by_compute_host)
        self.stubs.Set(self.controller.host_api,
                       'compute_node_search_by_hypervisor',
                       self.fake_compute_node_search_by_hypervisor)
        self.stubs.Set(self.controller.host_api, 'compute_node_get',
                       self.fake_compute_node_get)
        self.stubs.Set(self.controller.host_api, 'compute_node_statistics',
                       fake_compute_node_statistics)
        self.stubs.Set(self.controller.host_api, 'instance_get_all_by_host',
                       self.fake_instance_get_all_by_host)


class HypervisorsTestV228(HypervisorsTestV21):
    api_version = '2.28'

    DETAIL_HYPERS_DICTS = copy.deepcopy(HypervisorsTestV21.DETAIL_HYPERS_DICTS)
    DETAIL_HYPERS_DICTS[0]['cpu_info'] = jsonutils.loads(CPU_INFO)
    DETAIL_HYPERS_DICTS[1]['cpu_info'] = jsonutils.loads(CPU_INFO)
    DETAIL_NULL_CPUINFO_DICT = {'': {}, None: {}}


class HypervisorsTestV233(HypervisorsTestV228):
    api_version = '2.33'

    def test_index_pagination(self):
        req = self._get_request(True,
                                '/v2/1234/os-hypervisors?limit=1&marker=1')
        result = self.controller.index(req)
        expected = {
            'hypervisors': [
                {'hypervisor_hostname': 'hyper2',
                 'id': 2,
                 'state': 'up',
                 'status': 'enabled'}
            ],
            'hypervisors_links': [
                {'href': 'http://localhost/v2/hypervisors?limit=1&marker=2',
                 'rel': 'next'}
            ]
        }

        self.assertEqual(expected, result)

    def test_index_pagination_with_invalid_marker(self):
        req = self._get_request(True,
                                '/v2/1234/os-hypervisors?marker=99999')
        self.assertRaises(exc.HTTPBadRequest,
                          self.controller.index, req)

    def test_detail_pagination(self):
        req = self._get_request(
            True, '/v2/1234/os-hypervisors/detail?limit=1&marker=1')
        result = self.controller.detail(req)
        link = 'http://localhost/v2/hypervisors/detail?limit=1&marker=2'
        expected = {
            'hypervisors': [
                {'cpu_info': {'arch': 'x86_64',
                              'features': [],
                              'model': '',
                              'topology': {'cores': 1,
                                           'sockets': 1,
                                           'threads': 1},
                              'vendor': 'fake'},
                'current_workload': 2,
                'disk_available_least': 100,
                'free_disk_gb': 125,
                'free_ram_mb': 5120,
                'host_ip': netaddr.IPAddress('2.2.2.2'),
                'hypervisor_hostname': 'hyper2',
                'hypervisor_type': 'xen',
                'hypervisor_version': 3,
                'id': 2,
                'local_gb': 250,
                'local_gb_used': 125,
                'memory_mb': 10240,
                'memory_mb_used': 5120,
                'running_vms': 2,
                'service': {'disabled_reason': None,
                            'host': 'compute2',
                            'id': 2},
                'state': 'up',
                'status': 'enabled',
                'vcpus': 4,
                'vcpus_used': 2}
            ],
            'hypervisors_links': [{'href': link, 'rel': 'next'}]
        }

        self.assertEqual(expected, result)

    def test_detail_pagination_with_invalid_marker(self):
        req = self._get_request(True,
                                '/v2/1234/os-hypervisors/detail?marker=99999')
        self.assertRaises(exc.HTTPBadRequest,
                          self.controller.index, req)
