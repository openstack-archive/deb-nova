# Copyright (c) 2012 Rackspace Hosting
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
"""
Tests For CellsManager
"""
import copy
import datetime

import mock
from oslo_utils import timeutils
from six.moves import range

from nova.cells import messaging
from nova.cells import utils as cells_utils
import nova.conf
from nova import context
from nova import objects
from nova import test
from nova.tests.unit.cells import fakes
from nova.tests.unit import fake_instance
from nova.tests.unit import fake_server_actions
from nova.tests.unit.objects import test_flavor

CONF = nova.conf.CONF

FAKE_COMPUTE_NODES = [dict(id=1, host='host1'), dict(id=2, host='host2')]
FAKE_SERVICES = [dict(id=1, host='host1'),
                 dict(id=2, host='host2'),
                 dict(id=3, host='host3')]
FAKE_TASK_LOGS = [dict(id=1, host='host1'),
                  dict(id=2, host='host2')]


class CellsManagerClassTestCase(test.NoDBTestCase):
    """Test case for CellsManager class."""

    def setUp(self):
        super(CellsManagerClassTestCase, self).setUp()
        fakes.init(self)
        # pick a child cell to use for tests.
        self.our_cell = 'grandchild-cell1'
        self.cells_manager = fakes.get_cells_manager(self.our_cell)
        self.msg_runner = self.cells_manager.msg_runner
        self.state_manager = fakes.get_state_manager(self.our_cell)
        self.driver = self.cells_manager.driver
        self.ctxt = 'fake_context'

    def _get_fake_response(self, raw_response=None, exc=False):
        if exc:
            return messaging.Response(self.ctxt, 'fake',
                                      test.TestingException(),
                                      True)
        if raw_response is None:
            raw_response = 'fake-response'
        return messaging.Response(self.ctxt, 'fake', raw_response, False)

    def test_get_cell_info_for_neighbors(self):
        self.mox.StubOutWithMock(self.cells_manager.state_manager,
                'get_cell_info_for_neighbors')
        self.cells_manager.state_manager.get_cell_info_for_neighbors()
        self.mox.ReplayAll()
        self.cells_manager.get_cell_info_for_neighbors(self.ctxt)

    def test_post_start_hook_child_cell(self):
        self.mox.StubOutWithMock(self.driver, 'start_servers')
        self.mox.StubOutWithMock(context, 'get_admin_context')
        self.mox.StubOutWithMock(self.cells_manager, '_update_our_parents')

        self.driver.start_servers(self.msg_runner)
        context.get_admin_context().AndReturn(self.ctxt)
        self.cells_manager._update_our_parents(self.ctxt)
        self.mox.ReplayAll()
        self.cells_manager.post_start_hook()

    def test_post_start_hook_middle_cell(self):
        cells_manager = fakes.get_cells_manager('child-cell2')
        msg_runner = cells_manager.msg_runner
        driver = cells_manager.driver

        self.mox.StubOutWithMock(driver, 'start_servers')
        self.mox.StubOutWithMock(context, 'get_admin_context')
        self.mox.StubOutWithMock(msg_runner,
                                 'ask_children_for_capabilities')
        self.mox.StubOutWithMock(msg_runner,
                                 'ask_children_for_capacities')

        driver.start_servers(msg_runner)
        context.get_admin_context().AndReturn(self.ctxt)
        msg_runner.ask_children_for_capabilities(self.ctxt)
        msg_runner.ask_children_for_capacities(self.ctxt)
        self.mox.ReplayAll()
        cells_manager.post_start_hook()

    def test_update_our_parents(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'tell_parents_our_capabilities')
        self.mox.StubOutWithMock(self.msg_runner,
                                 'tell_parents_our_capacities')

        self.msg_runner.tell_parents_our_capabilities(self.ctxt)
        self.msg_runner.tell_parents_our_capacities(self.ctxt)
        self.mox.ReplayAll()
        self.cells_manager._update_our_parents(self.ctxt)

    def test_build_instances(self):
        build_inst_kwargs = {'instances': [objects.Instance(),
                                           objects.Instance()]}
        self.mox.StubOutWithMock(self.msg_runner, 'build_instances')
        our_cell = self.msg_runner.state_manager.get_my_state()
        self.msg_runner.build_instances(self.ctxt, our_cell, build_inst_kwargs)
        self.mox.ReplayAll()
        self.cells_manager.build_instances(self.ctxt,
                build_inst_kwargs=build_inst_kwargs)

    def test_build_instances_old_flavor(self):
        flavor_dict = test_flavor.fake_flavor
        args = {'filter_properties': {'instance_type': flavor_dict},
                'instances': [objects.Instance()]}
        with mock.patch.object(self.msg_runner, 'build_instances') as mock_bi:
            self.cells_manager.build_instances(self.ctxt,
                                               build_inst_kwargs=args)
            filter_properties = mock_bi.call_args[0][2]['filter_properties']
            self.assertIsInstance(filter_properties['instance_type'],
                                  objects.Flavor)

    def test_build_instances_old_instances(self):
        args = {'instances': [fake_instance.fake_db_instance()]}
        with mock.patch.object(self.msg_runner, 'build_instances') as mock_bi:
            self.cells_manager.build_instances(self.ctxt,
                                               build_inst_kwargs=args)
            self.assertIsInstance(mock_bi.call_args[0][2]['instances'][0],
                                  objects.Instance)

    def test_run_compute_api_method(self):
        # Args should just be silently passed through
        cell_name = 'fake-cell-name'
        method_info = 'fake-method-info'

        self.mox.StubOutWithMock(self.msg_runner,
                                 'run_compute_api_method')
        fake_response = self._get_fake_response()
        self.msg_runner.run_compute_api_method(self.ctxt,
                                               cell_name,
                                               method_info,
                                               True).AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.run_compute_api_method(
                self.ctxt, cell_name=cell_name, method_info=method_info,
                call=True)
        self.assertEqual('fake-response', response)

    def test_instance_update_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner, 'instance_update_at_top')
        self.msg_runner.instance_update_at_top(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.instance_update_at_top(self.ctxt,
                                                  instance='fake-instance')

    def test_instance_destroy_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner, 'instance_destroy_at_top')
        self.msg_runner.instance_destroy_at_top(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.instance_destroy_at_top(self.ctxt,
                                                  instance='fake-instance')

    def test_instance_delete_everywhere(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'instance_delete_everywhere')
        self.msg_runner.instance_delete_everywhere(self.ctxt,
                                                   'fake-instance',
                                                   'fake-type')
        self.mox.ReplayAll()
        self.cells_manager.instance_delete_everywhere(
                self.ctxt, instance='fake-instance',
                delete_type='fake-type')

    def test_instance_fault_create_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'instance_fault_create_at_top')
        self.msg_runner.instance_fault_create_at_top(self.ctxt,
                                                     'fake-fault')
        self.mox.ReplayAll()
        self.cells_manager.instance_fault_create_at_top(
                self.ctxt, instance_fault='fake-fault')

    def test_bw_usage_update_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'bw_usage_update_at_top')
        self.msg_runner.bw_usage_update_at_top(self.ctxt,
                                               'fake-bw-info')
        self.mox.ReplayAll()
        self.cells_manager.bw_usage_update_at_top(
                self.ctxt, bw_update_info='fake-bw-info')

    def test_heal_instances(self):
        self.flags(instance_updated_at_threshold=1000,
                   instance_update_num_instances=2,
                   group='cells')

        fake_context = context.RequestContext('fake', 'fake')
        stalled_time = timeutils.utcnow()
        updated_since = stalled_time - datetime.timedelta(seconds=1000)

        def utcnow():
            return stalled_time

        call_info = {'get_instances': 0, 'sync_instances': []}

        instances = ['instance1', 'instance2', 'instance3']

        def get_instances_to_sync(context, **kwargs):
            self.assertEqual(fake_context, context)
            call_info['shuffle'] = kwargs.get('shuffle')
            call_info['project_id'] = kwargs.get('project_id')
            call_info['updated_since'] = kwargs.get('updated_since')
            call_info['get_instances'] += 1
            return iter(instances)

        @staticmethod
        def instance_get_by_uuid(context, uuid):
            return instances[int(uuid[-1]) - 1]

        def sync_instance(context, instance):
            self.assertEqual(fake_context, context)
            call_info['sync_instances'].append(instance)

        self.stubs.Set(cells_utils, 'get_instances_to_sync',
                get_instances_to_sync)
        self.stubs.Set(objects.Instance, 'get_by_uuid',
                instance_get_by_uuid)
        self.stubs.Set(self.cells_manager, '_sync_instance',
                sync_instance)
        self.stubs.Set(timeutils, 'utcnow', utcnow)

        self.cells_manager._heal_instances(fake_context)
        self.assertTrue(call_info['shuffle'])
        self.assertIsNone(call_info['project_id'])
        self.assertEqual(updated_since, call_info['updated_since'])
        self.assertEqual(1, call_info['get_instances'])
        # Only first 2
        self.assertEqual(instances[:2], call_info['sync_instances'])

        call_info['sync_instances'] = []
        self.cells_manager._heal_instances(fake_context)
        self.assertTrue(call_info['shuffle'])
        self.assertIsNone(call_info['project_id'])
        self.assertEqual(updated_since, call_info['updated_since'])
        self.assertEqual(2, call_info['get_instances'])
        # Now the last 1 and the first 1
        self.assertEqual([instances[-1], instances[0]],
                         call_info['sync_instances'])

    def test_sync_instances(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'sync_instances')
        self.msg_runner.sync_instances(self.ctxt, 'fake-project',
                                       'fake-time', 'fake-deleted')
        self.mox.ReplayAll()
        self.cells_manager.sync_instances(self.ctxt,
                                          project_id='fake-project',
                                          updated_since='fake-time',
                                          deleted='fake-deleted')

    def test_service_get_all(self):
        responses = []
        expected_response = []
        # 3 cells... so 3 responses.  Each response is a list of services.
        # Manager should turn these into a single list of responses.
        for i in range(3):
            cell_name = 'path!to!cell%i' % i
            services = []
            for service in FAKE_SERVICES:
                fake_service = objects.Service(**service)
                services.append(fake_service)
                expected_service = cells_utils.ServiceProxy(fake_service,
                                                            cell_name)
                expected_response.append(
                    (cell_name, expected_service, fake_service))
            response = messaging.Response(self.ctxt, cell_name, services,
                                          False)
            responses.append(response)

        self.mox.StubOutWithMock(self.msg_runner,
                                 'service_get_all')
        self.mox.StubOutWithMock(cells_utils, 'add_cell_to_service')
        self.msg_runner.service_get_all(self.ctxt,
                                        'fake-filters').AndReturn(responses)
        # Calls are done by cells, so we need to sort the list by the cell name
        expected_response.sort(key=lambda k: k[0])
        for cell_name, service_proxy, service in expected_response:
            cells_utils.add_cell_to_service(
                service, cell_name).AndReturn(service_proxy)
        self.mox.ReplayAll()
        response = self.cells_manager.service_get_all(self.ctxt,
                                                      filters='fake-filters')
        self.assertEqual([proxy for cell, proxy, service in expected_response],
                         response)

    def test_service_get_by_compute_host(self):
        fake_cell = 'fake-cell'
        fake_service = objects.Service(**FAKE_SERVICES[0])
        fake_response = messaging.Response(self.ctxt, fake_cell,
                                           fake_service,
                                           False)
        expected_response = cells_utils.ServiceProxy(fake_service, fake_cell)
        cell_and_host = cells_utils.cell_with_item('fake-cell', 'fake-host')

        self.mox.StubOutWithMock(self.msg_runner,
                                 'service_get_by_compute_host')
        self.mox.StubOutWithMock(cells_utils, 'add_cell_to_service')
        self.msg_runner.service_get_by_compute_host(self.ctxt,
                fake_cell, 'fake-host').AndReturn(fake_response)
        cells_utils.add_cell_to_service(fake_service, fake_cell).AndReturn(
            expected_response)

        self.mox.ReplayAll()
        response = self.cells_manager.service_get_by_compute_host(self.ctxt,
                host_name=cell_and_host)
        self.assertEqual(expected_response, response)

    def test_get_host_uptime(self):
        fake_cell = 'parent!fake-cell'
        fake_host = 'fake-host'
        fake_cell_and_host = cells_utils.cell_with_item(fake_cell, fake_host)
        host_uptime = (" 08:32:11 up 93 days, 18:25, 12 users,  load average:"
                       " 0.20, 0.12, 0.14")
        fake_response = messaging.Response(self.ctxt, fake_cell, host_uptime,
                                           False)

        self.mox.StubOutWithMock(self.msg_runner,
                                 'get_host_uptime')
        self.msg_runner.get_host_uptime(self.ctxt, fake_cell, fake_host).\
            AndReturn(fake_response)
        self.mox.ReplayAll()

        response = self.cells_manager.get_host_uptime(self.ctxt,
                                                      fake_cell_and_host)
        self.assertEqual(host_uptime, response)

    def test_service_update(self):
        fake_cell = 'fake-cell'
        fake_service = objects.Service(**FAKE_SERVICES[0])
        fake_response = messaging.Response(
            self.ctxt, fake_cell, fake_service, False)
        expected_response = cells_utils.ServiceProxy(fake_service, fake_cell)
        cell_and_host = cells_utils.cell_with_item('fake-cell', 'fake-host')
        params_to_update = {'disabled': True}

        self.mox.StubOutWithMock(self.msg_runner, 'service_update')
        self.mox.StubOutWithMock(cells_utils, 'add_cell_to_service')
        self.msg_runner.service_update(self.ctxt,
                fake_cell, 'fake-host', 'nova-api',
                params_to_update).AndReturn(fake_response)
        cells_utils.add_cell_to_service(fake_service, fake_cell).AndReturn(
            expected_response)
        self.mox.ReplayAll()

        response = self.cells_manager.service_update(
            self.ctxt, host_name=cell_and_host, binary='nova-api',
            params_to_update=params_to_update)
        self.assertEqual(expected_response, response)

    def test_service_delete(self):
        fake_cell = 'fake-cell'
        service_id = '1'
        cell_service_id = cells_utils.cell_with_item(fake_cell, service_id)

        with mock.patch.object(self.msg_runner,
                               'service_delete') as service_delete:
            self.cells_manager.service_delete(self.ctxt, cell_service_id)
            service_delete.assert_called_once_with(
                self.ctxt, fake_cell, service_id)

    def test_proxy_rpc_to_manager(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'proxy_rpc_to_manager')
        fake_response = self._get_fake_response()
        cell_and_host = cells_utils.cell_with_item('fake-cell', 'fake-host')
        topic = "%s.%s" % (CONF.compute_topic, cell_and_host)
        self.msg_runner.proxy_rpc_to_manager(self.ctxt, 'fake-cell',
                'fake-host', topic, 'fake-rpc-msg',
                True, -1).AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.proxy_rpc_to_manager(self.ctxt,
                topic=topic, rpc_message='fake-rpc-msg', call=True,
                timeout=-1)
        self.assertEqual('fake-response', response)

    def _build_task_log_responses(self, num):
        responses = []
        expected_response = []
        # 3 cells... so 3 responses.  Each response is a list of task log
        # entries. Manager should turn these into a single list of
        # task log entries.
        for i in range(num):
            cell_name = 'path!to!cell%i' % i
            task_logs = []
            for task_log in FAKE_TASK_LOGS:
                task_logs.append(copy.deepcopy(task_log))
                expected_task_log = copy.deepcopy(task_log)
                cells_utils.add_cell_to_task_log(expected_task_log,
                                                 cell_name)
                expected_response.append(expected_task_log)
            response = messaging.Response(self.ctxt, cell_name, task_logs,
                                          False)
            responses.append(response)
        return expected_response, responses

    def test_task_log_get_all(self):
        expected_response, responses = self._build_task_log_responses(3)
        self.mox.StubOutWithMock(self.msg_runner,
                                 'task_log_get_all')
        self.msg_runner.task_log_get_all(self.ctxt, None,
                'fake-name', 'fake-begin',
                'fake-end', host=None, state=None).AndReturn(responses)
        self.mox.ReplayAll()
        response = self.cells_manager.task_log_get_all(self.ctxt,
                task_name='fake-name',
                period_beginning='fake-begin', period_ending='fake-end')
        self.assertEqual(expected_response, response)

    def test_task_log_get_all_with_filters(self):
        expected_response, responses = self._build_task_log_responses(1)
        cell_and_host = cells_utils.cell_with_item('fake-cell', 'fake-host')
        self.mox.StubOutWithMock(self.msg_runner,
                                 'task_log_get_all')
        self.msg_runner.task_log_get_all(self.ctxt, 'fake-cell',
                'fake-name', 'fake-begin', 'fake-end', host='fake-host',
                state='fake-state').AndReturn(responses)
        self.mox.ReplayAll()
        response = self.cells_manager.task_log_get_all(self.ctxt,
                task_name='fake-name',
                period_beginning='fake-begin', period_ending='fake-end',
                host=cell_and_host, state='fake-state')
        self.assertEqual(expected_response, response)

    def test_task_log_get_all_with_cell_but_no_host_filters(self):
        expected_response, responses = self._build_task_log_responses(1)
        # Host filter only has cell name.
        cell_and_host = 'fake-cell'
        self.mox.StubOutWithMock(self.msg_runner,
                                 'task_log_get_all')
        self.msg_runner.task_log_get_all(self.ctxt, 'fake-cell',
                'fake-name', 'fake-begin', 'fake-end', host=None,
                state='fake-state').AndReturn(responses)
        self.mox.ReplayAll()
        response = self.cells_manager.task_log_get_all(self.ctxt,
                task_name='fake-name',
                period_beginning='fake-begin', period_ending='fake-end',
                host=cell_and_host, state='fake-state')
        self.assertEqual(expected_response, response)

    def test_compute_node_get_all(self):
        responses = []
        expected_response = []
        # 3 cells... so 3 responses.  Each response is a list of computes.
        # Manager should turn these into a single list of responses.
        for i in range(3):
            cell_name = 'path!to!cell%i' % i
            compute_nodes = []
            for compute_node in FAKE_COMPUTE_NODES:
                fake_compute = objects.ComputeNode(**compute_node)
                fake_compute._cached_service = None
                compute_nodes.append(fake_compute)
                expected_compute_node = cells_utils.ComputeNodeProxy(
                    fake_compute, cell_name)
                expected_response.append(
                    (cell_name, expected_compute_node, fake_compute))
            response = messaging.Response(self.ctxt, cell_name, compute_nodes,
                                          False)
            responses.append(response)
        self.mox.StubOutWithMock(self.msg_runner,
                                 'compute_node_get_all')
        self.mox.StubOutWithMock(cells_utils, 'add_cell_to_compute_node')
        self.msg_runner.compute_node_get_all(self.ctxt,
                hypervisor_match='fake-match').AndReturn(responses)
        # Calls are done by cells, so we need to sort the list by the cell name
        expected_response.sort(key=lambda k: k[0])
        for cell_name, compute_proxy, compute_node in expected_response:
            cells_utils.add_cell_to_compute_node(
                compute_node, cell_name).AndReturn(compute_proxy)
        self.mox.ReplayAll()
        response = self.cells_manager.compute_node_get_all(self.ctxt,
                hypervisor_match='fake-match')
        self.assertEqual([proxy for cell, proxy, compute in expected_response],
                         response)

    def test_compute_node_stats(self):
        raw_resp1 = {'key1': 1, 'key2': 2}
        raw_resp2 = {'key2': 1, 'key3': 2}
        raw_resp3 = {'key3': 1, 'key4': 2}
        responses = [messaging.Response(self.ctxt, 'cell1', raw_resp1, False),
                     messaging.Response(self.ctxt, 'cell2', raw_resp2, False),
                     messaging.Response(self.ctxt, 'cell2', raw_resp3, False)]
        expected_resp = {'key1': 1, 'key2': 3, 'key3': 3, 'key4': 2}

        self.mox.StubOutWithMock(self.msg_runner,
                                 'compute_node_stats')
        self.msg_runner.compute_node_stats(self.ctxt).AndReturn(responses)
        self.mox.ReplayAll()
        response = self.cells_manager.compute_node_stats(self.ctxt)
        self.assertEqual(expected_resp, response)

    def test_compute_node_get(self):
        fake_cell = 'fake-cell'
        fake_compute = objects.ComputeNode(**FAKE_COMPUTE_NODES[0])
        fake_compute._cached_service = None
        fake_response = messaging.Response(self.ctxt, fake_cell,
                                           fake_compute,
                                           False)

        expected_response = cells_utils.ComputeNodeProxy(fake_compute,
                                                         fake_cell)
        cell_and_id = cells_utils.cell_with_item(fake_cell, 'fake-id')
        self.mox.StubOutWithMock(self.msg_runner,
                                 'compute_node_get')
        self.mox.StubOutWithMock(cells_utils, 'add_cell_to_compute_node')
        self.msg_runner.compute_node_get(self.ctxt,
                'fake-cell', 'fake-id').AndReturn(fake_response)
        cells_utils.add_cell_to_compute_node(
            fake_compute, fake_cell).AndReturn(expected_response)
        self.mox.ReplayAll()
        response = self.cells_manager.compute_node_get(self.ctxt,
                compute_id=cell_and_id)
        self.assertEqual(expected_response, response)

    def test_actions_get(self):
        fake_uuid = fake_server_actions.FAKE_UUID
        fake_req_id = fake_server_actions.FAKE_REQUEST_ID1
        fake_act = fake_server_actions.FAKE_ACTIONS[fake_uuid][fake_req_id]
        fake_response = messaging.Response(self.ctxt, 'fake-cell', [fake_act],
                                           False)
        expected_response = [fake_act]
        self.mox.StubOutWithMock(self.msg_runner, 'actions_get')
        self.msg_runner.actions_get(self.ctxt, 'fake-cell',
                                    'fake-uuid').AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.actions_get(self.ctxt, 'fake-cell',
                                                  'fake-uuid')
        self.assertEqual(expected_response, response)

    def test_action_get_by_request_id(self):
        fake_uuid = fake_server_actions.FAKE_UUID
        fake_req_id = fake_server_actions.FAKE_REQUEST_ID1
        fake_act = fake_server_actions.FAKE_ACTIONS[fake_uuid][fake_req_id]
        fake_response = messaging.Response(self.ctxt, 'fake-cell', fake_act,
                                           False)
        expected_response = fake_act
        self.mox.StubOutWithMock(self.msg_runner, 'action_get_by_request_id')
        self.msg_runner.action_get_by_request_id(self.ctxt, 'fake-cell',
                            'fake-uuid', 'req-fake').AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.action_get_by_request_id(self.ctxt,
                                                               'fake-cell',
                                                               'fake-uuid',
                                                               'req-fake')
        self.assertEqual(expected_response, response)

    def test_action_events_get(self):
        fake_action_id = fake_server_actions.FAKE_ACTION_ID1
        fake_events = fake_server_actions.FAKE_EVENTS[fake_action_id]
        fake_response = messaging.Response(self.ctxt, 'fake-cell', fake_events,
                                           False)
        expected_response = fake_events
        self.mox.StubOutWithMock(self.msg_runner, 'action_events_get')
        self.msg_runner.action_events_get(self.ctxt, 'fake-cell',
                                    'fake-action').AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.action_events_get(self.ctxt, 'fake-cell',
                                                        'fake-action')
        self.assertEqual(expected_response, response)

    def test_consoleauth_delete_tokens(self):
        instance_uuid = 'fake-instance-uuid'

        self.mox.StubOutWithMock(self.msg_runner,
                                 'consoleauth_delete_tokens')
        self.msg_runner.consoleauth_delete_tokens(self.ctxt, instance_uuid)
        self.mox.ReplayAll()
        self.cells_manager.consoleauth_delete_tokens(self.ctxt,
                instance_uuid=instance_uuid)

    def test_get_capacities(self):
        cell_name = 'cell_name'
        response = {"ram_free":
                   {"units_by_mb": {"64": 20, "128": 10}, "total_mb": 1491}}
        self.mox.StubOutWithMock(self.state_manager,
                                 'get_capacities')
        self.state_manager.get_capacities(cell_name).AndReturn(response)
        self.mox.ReplayAll()
        self.assertEqual(response,
                self.cells_manager.get_capacities(self.ctxt, cell_name))

    def test_validate_console_port(self):
        instance_uuid = 'fake-instance-uuid'
        cell_name = 'fake-cell-name'
        instance = objects.Instance(cell_name=cell_name)
        console_port = 'fake-console-port'
        console_type = 'fake-console-type'

        self.mox.StubOutWithMock(self.msg_runner,
                                 'validate_console_port')
        self.mox.StubOutWithMock(objects.Instance, 'get_by_uuid')
        fake_response = self._get_fake_response()

        objects.Instance.get_by_uuid(self.ctxt,
                instance_uuid).AndReturn(instance)
        self.msg_runner.validate_console_port(self.ctxt, cell_name,
                instance_uuid, console_port,
                console_type).AndReturn(fake_response)
        self.mox.ReplayAll()
        response = self.cells_manager.validate_console_port(self.ctxt,
                instance_uuid=instance_uuid, console_port=console_port,
                console_type=console_type)
        self.assertEqual('fake-response', response)

    def test_bdm_update_or_create_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'bdm_update_or_create_at_top')
        self.msg_runner.bdm_update_or_create_at_top(self.ctxt,
                                                    'fake-bdm',
                                                    create='foo')
        self.mox.ReplayAll()
        self.cells_manager.bdm_update_or_create_at_top(self.ctxt,
                                                       'fake-bdm',
                                                       create='foo')

    def test_bdm_destroy_at_top(self):
        self.mox.StubOutWithMock(self.msg_runner, 'bdm_destroy_at_top')
        self.msg_runner.bdm_destroy_at_top(self.ctxt,
                                           'fake_instance_uuid',
                                           device_name='fake_device_name',
                                           volume_id='fake_volume_id')

        self.mox.ReplayAll()
        self.cells_manager.bdm_destroy_at_top(self.ctxt,
                                              'fake_instance_uuid',
                                              device_name='fake_device_name',
                                              volume_id='fake_volume_id')

    def test_get_migrations(self):
        filters = {'status': 'confirmed'}
        cell1_migrations = objects.MigrationList(
            objects=[objects.Migration(id=123)])
        cell2_migrations = objects.MigrationList(
            objects=[objects.Migration(id=456)])
        fake_responses = [self._get_fake_response(cell1_migrations),
                          self._get_fake_response(cell2_migrations)]
        self.mox.StubOutWithMock(self.msg_runner,
                                 'get_migrations')
        self.msg_runner.get_migrations(self.ctxt, None, False, filters).\
            AndReturn(fake_responses)
        self.mox.ReplayAll()

        response = self.cells_manager.get_migrations(self.ctxt, filters)

        self.assertEqual(cell1_migrations.objects + cell2_migrations.objects,
                         response.objects)

    def test_get_migrations_for_a_given_cell(self):
        filters = {'status': 'confirmed', 'cell_name': 'ChildCell1'}
        target_cell = '%s%s%s' % (CONF.cells.name, '!', filters['cell_name'])
        migrations = objects.MigrationList(objects=[objects.Migration(id=123)])
        fake_responses = [self._get_fake_response(migrations)]
        self.mox.StubOutWithMock(self.msg_runner,
                                 'get_migrations')
        self.msg_runner.get_migrations(self.ctxt, target_cell, False,
                                           filters).AndReturn(fake_responses)
        self.mox.ReplayAll()

        response = self.cells_manager.get_migrations(self.ctxt, filters)
        self.assertEqual(migrations.objects, response.objects)

    def test_instance_update_from_api(self):
        self.mox.StubOutWithMock(self.msg_runner,
                                 'instance_update_from_api')
        self.msg_runner.instance_update_from_api(self.ctxt,
                                                 'fake-instance',
                                                 'exp_vm', 'exp_task',
                                                 'admin_reset')
        self.mox.ReplayAll()
        self.cells_manager.instance_update_from_api(
                self.ctxt, instance='fake-instance',
                expected_vm_state='exp_vm',
                expected_task_state='exp_task',
                admin_state_reset='admin_reset')

    def test_start_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'start_instance')
        self.msg_runner.start_instance(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.start_instance(self.ctxt, instance='fake-instance')

    def test_stop_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'stop_instance')
        self.msg_runner.stop_instance(self.ctxt, 'fake-instance',
                                      do_cast='meow',
                                      clean_shutdown='purr')
        self.mox.ReplayAll()
        self.cells_manager.stop_instance(self.ctxt,
                                         instance='fake-instance',
                                         do_cast='meow',
                                         clean_shutdown='purr')

    def test_cell_create(self):
        values = 'values'
        response = 'created_cell'
        self.mox.StubOutWithMock(self.state_manager,
                                 'cell_create')
        self.state_manager.cell_create(self.ctxt, values).\
            AndReturn(response)
        self.mox.ReplayAll()
        self.assertEqual(response,
                         self.cells_manager.cell_create(self.ctxt, values))

    def test_cell_update(self):
        cell_name = 'cell_name'
        values = 'values'
        response = 'updated_cell'
        self.mox.StubOutWithMock(self.state_manager,
                                 'cell_update')
        self.state_manager.cell_update(self.ctxt, cell_name, values).\
            AndReturn(response)
        self.mox.ReplayAll()
        self.assertEqual(response,
                         self.cells_manager.cell_update(self.ctxt, cell_name,
                                                        values))

    def test_cell_delete(self):
        cell_name = 'cell_name'
        response = 1
        self.mox.StubOutWithMock(self.state_manager,
                                 'cell_delete')
        self.state_manager.cell_delete(self.ctxt, cell_name).\
            AndReturn(response)
        self.mox.ReplayAll()
        self.assertEqual(response,
                         self.cells_manager.cell_delete(self.ctxt, cell_name))

    def test_cell_get(self):
        cell_name = 'cell_name'
        response = 'cell_info'
        self.mox.StubOutWithMock(self.state_manager,
                                 'cell_get')
        self.state_manager.cell_get(self.ctxt, cell_name).\
            AndReturn(response)
        self.mox.ReplayAll()
        self.assertEqual(response,
                         self.cells_manager.cell_get(self.ctxt, cell_name))

    def test_reboot_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'reboot_instance')
        self.msg_runner.reboot_instance(self.ctxt, 'fake-instance',
                                        'HARD')
        self.mox.ReplayAll()
        self.cells_manager.reboot_instance(self.ctxt,
                                           instance='fake-instance',
                                           reboot_type='HARD')

    def test_suspend_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'suspend_instance')
        self.msg_runner.suspend_instance(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.suspend_instance(self.ctxt,
                                            instance='fake-instance')

    def test_resume_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'resume_instance')
        self.msg_runner.resume_instance(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.resume_instance(self.ctxt,
                                           instance='fake-instance')

    def test_terminate_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'terminate_instance')
        self.msg_runner.terminate_instance(self.ctxt, 'fake-instance',
                                           delete_type='delete')
        self.mox.ReplayAll()
        self.cells_manager.terminate_instance(self.ctxt,
                                              instance='fake-instance',
                                              delete_type='delete')

    def test_soft_delete_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'soft_delete_instance')
        self.msg_runner.soft_delete_instance(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.soft_delete_instance(self.ctxt,
                                                instance='fake-instance')

    def _test_resize_instance(self, clean_shutdown=True):
        self.mox.StubOutWithMock(self.msg_runner, 'resize_instance')
        self.msg_runner.resize_instance(self.ctxt, 'fake-instance',
                                       'fake-flavor', 'fake-updates',
                                       clean_shutdown=clean_shutdown)
        self.mox.ReplayAll()
        self.cells_manager.resize_instance(
                self.ctxt, instance='fake-instance', flavor='fake-flavor',
                extra_instance_updates='fake-updates',
                clean_shutdown=clean_shutdown)

    def test_resize_instance(self):
        self._test_resize_instance()

    def test_resize_instance_forced_shutdown(self):
        self._test_resize_instance(clean_shutdown=False)

    def test_live_migrate_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'live_migrate_instance')
        self.msg_runner.live_migrate_instance(self.ctxt, 'fake-instance',
                                              'fake-block', 'fake-commit',
                                              'fake-host')
        self.mox.ReplayAll()
        self.cells_manager.live_migrate_instance(
                self.ctxt, instance='fake-instance',
                block_migration='fake-block', disk_over_commit='fake-commit',
                host_name='fake-host')

    def test_revert_resize(self):
        self.mox.StubOutWithMock(self.msg_runner, 'revert_resize')
        self.msg_runner.revert_resize(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.revert_resize(self.ctxt, instance='fake-instance')

    def test_confirm_resize(self):
        self.mox.StubOutWithMock(self.msg_runner, 'confirm_resize')
        self.msg_runner.confirm_resize(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.confirm_resize(self.ctxt, instance='fake-instance')

    def test_reset_network(self):
        self.mox.StubOutWithMock(self.msg_runner, 'reset_network')
        self.msg_runner.reset_network(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.reset_network(self.ctxt, instance='fake-instance')

    def test_inject_network_info(self):
        self.mox.StubOutWithMock(self.msg_runner, 'inject_network_info')
        self.msg_runner.inject_network_info(self.ctxt, 'fake-instance')
        self.mox.ReplayAll()
        self.cells_manager.inject_network_info(self.ctxt,
                                               instance='fake-instance')

    def test_snapshot_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'snapshot_instance')
        self.msg_runner.snapshot_instance(self.ctxt, 'fake-instance',
                                          'fake-id')
        self.mox.ReplayAll()
        self.cells_manager.snapshot_instance(self.ctxt,
                                             instance='fake-instance',
                                             image_id='fake-id')

    def test_backup_instance(self):
        self.mox.StubOutWithMock(self.msg_runner, 'backup_instance')
        self.msg_runner.backup_instance(self.ctxt, 'fake-instance',
                                        'fake-id', 'backup-type',
                                        'rotation')
        self.mox.ReplayAll()
        self.cells_manager.backup_instance(self.ctxt,
                                           instance='fake-instance',
                                           image_id='fake-id',
                                           backup_type='backup-type',
                                           rotation='rotation')

    def test_set_admin_password(self):
        with mock.patch.object(self.msg_runner,
                               'set_admin_password') as set_admin_password:
            self.cells_manager.set_admin_password(self.ctxt,
                    instance='fake-instance', new_pass='fake-password')
            set_admin_password.assert_called_once_with(self.ctxt,
                    'fake-instance', 'fake-password')

    def test_get_keypair_at_top(self):
        keypairs = [self._get_fake_response('fake_keypair'),
                    self._get_fake_response('fake_keypair2')]
        with mock.patch.object(self.msg_runner,
                               'get_keypair_at_top',
                               return_value=keypairs) as fake_get_keypair:
            response = self.cells_manager.get_keypair_at_top(self.ctxt,
                                                             'fake_user_id',
                                                             'fake_name')
            fake_get_keypair.assert_called_once_with(self.ctxt, 'fake_user_id',
                                                     'fake_name')
            self.assertEqual('fake_keypair', response)

    def test_get_keypair_at_top_with_empty_responses(self):
        with mock.patch.object(self.msg_runner,
                               'get_keypair_at_top',
                               return_value=[]) as fake_get_keypair:
            self.assertIsNone(
                self.cells_manager.get_keypair_at_top(self.ctxt,
                                                      'fake_user_id',
                                                      'fake_name'))
            fake_get_keypair.assert_called_once_with(self.ctxt, 'fake_user_id',
                                                     'fake_name')
