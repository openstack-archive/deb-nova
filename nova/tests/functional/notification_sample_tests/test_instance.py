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
from nova import context
from nova.tests import fixtures
from nova.tests.functional.notification_sample_tests \
    import notification_sample_base
from nova.tests.unit import fake_notifier


class TestInstanceNotificationSample(
        notification_sample_base.NotificationSampleTestBase):

    def setUp(self):
        self.flags(use_neutron=True)
        super(TestInstanceNotificationSample, self).setUp()
        self.neutron = fixtures.NeutronFixture(self)
        self.useFixture(self.neutron)

    def test_instance_action(self):
        # A single test case is used to test most of the instance action
        # notifications to avoid booting up an instance for every action
        # separately.
        # Every instance action test function shall make sure that after the
        # function the instance is in active state and usable by other actions.
        # Therefore some action especially delete cannot be used here as
        # recovering from that action would mean to recreate the instance and
        # that would go against the whole purpose of this optimization

        server = self._boot_a_server(
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]})

        actions = [
            self._test_power_on_server,
            self._test_restore_server,
            self._test_suspend_server,
            self._test_pause_server,
            self._test_shelve_server,
            self._test_resize_server,
        ]

        for action in actions:
            fake_notifier.reset()
            action(server)

    def test_create_delete_server(self):
        server = self._boot_a_server(
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]})
        self.api.delete_server(server['id'])
        self._wait_until_deleted(server)

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-delete-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-delete-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

    def _verify_instance_update_steps(self, steps, notifications,
                                      initial=None):
        replacements = {}
        if initial:
            replacements = initial
        for i, step in enumerate(steps):
            replacements.update(step)
            self._verify_notification(
                'instance-update',
                replacements=replacements,
                actual=notifications[i])
        return replacements

    def test_create_delete_server_with_instance_update(self):
        self.flags(notify_on_state_change='vm_and_task_state')

        server = self._boot_a_server(
            extra_params={'networks': [{'port': self.neutron.port_1['id']}]})

        instance_updates = self._get_notifications('instance.update')

        # The first notification comes from the nova-api the rest is from the
        # nova-compute. To keep the test simpler assert this fact and then
        # modify the publisher_id of the first notification to match the
        # template
        self.assertEqual('nova-api:fake-mini',
                         instance_updates[0]['publisher_id'])
        instance_updates[0]['publisher_id'] = 'nova-compute:fake-mini'

        self.assertEqual(7, len(instance_updates))
        create_steps = [
            # nothing -> scheduling
            {'reservation_id': server['reservation_id'],
             'uuid': server['id'],
             'host': None,
             'node': None,
             'state_update.new_task_state': 'scheduling',
             'state_update.old_task_state': 'scheduling',
             'state_update.state': 'building',
             'state_update.old_state': 'building',
             'state': 'building'},

            # scheduling -> building
            {
             'state_update.new_task_state': None,
             'state_update.old_task_state': 'scheduling',
             'task_state': None},

            # scheduled
            {'host': 'compute',
             'node': 'fake-mini',
             'state_update.old_task_state': None},

            # building -> networking
            {'state_update.new_task_state': 'networking',
             'state_update.old_task_state': 'networking',
             'task_state': 'networking'},

            # networking -> block_device_mapping
            {'state_update.new_task_state': 'block_device_mapping',
             'state_update.old_task_state': 'networking',
             'task_state': 'block_device_mapping',
            },

            # block_device_mapping -> spawning
            {'state_update.new_task_state': 'spawning',
             'state_update.old_task_state': 'block_device_mapping',
             'task_state': 'spawning',
             'ip_addresses': [{
                 "nova_object.name": "IpPayload",
                 "nova_object.namespace": "nova",
                 "nova_object.version": "1.0",
                 "nova_object.data": {
                     "mac": "fa:16:3e:4c:2c:30",
                     "address": "192.168.1.3",
                     "port_uuid": "ce531f90-199f-48c0-816c-13e38010b442",
                     "meta": {},
                     "version": 4,
                     "label": "private-network",
                     "device_name": "tapce531f90-19"
                 }}]
             },

            # spawning -> active
            {'state_update.new_task_state': None,
             'state_update.old_task_state': 'spawning',
             'state_update.state': 'active',
             'launched_at': '2012-10-29T13:42:11Z',
             'state': 'active',
             'task_state': None,
             'power_state': 'running'},
        ]

        replacements = self._verify_instance_update_steps(
                create_steps, instance_updates)

        fake_notifier.reset()

        # Let's generate some bandwidth usage data.
        # Just call the periodic task directly for simplicity
        self.compute.manager._poll_bandwidth_usage(context.get_admin_context())

        self.api.delete_server(server['id'])
        self._wait_until_deleted(server)

        instance_updates = self._get_notifications('instance.update')
        self.assertEqual(2, len(instance_updates))

        delete_steps = [
            # active -> deleting
            {'state_update.new_task_state': 'deleting',
             'state_update.old_task_state': 'deleting',
             'state_update.old_state': 'active',
             'state': 'active',
             'task_state': 'deleting',
             'bandwidth': [
                 {'nova_object.namespace': 'nova',
                  'nova_object.name': 'BandwidthPayload',
                  'nova_object.data':
                      {'network_name': 'private-network',
                       'out_bytes': 0,
                       'in_bytes': 0},
                  'nova_object.version': '1.0'}]
            },

            # deleting -> deleted
            {'state_update.new_task_state': None,
             'state_update.old_task_state': 'deleting',
             'state_update.old_state': 'active',
             'state_update.state': 'deleted',
             'state': 'deleted',
             'task_state': None,
             'terminated_at': '2012-10-29T13:42:11Z',
             'ip_addresses': [],
             'power_state': 'pending',
             'bandwidth': []},
        ]

        self._verify_instance_update_steps(delete_steps, instance_updates,
                                           initial=replacements)

    def _test_power_on_server(self, server):
        self.api.post_server_action(server['id'], {'os-stop': {}})
        self._wait_for_state_change(self.api, server,
                                    expected_status='SHUTOFF')
        self.api.post_server_action(server['id'], {'os-start': {}})
        self._wait_for_state_change(self.api, server,
                                    expected_status='ACTIVE')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-power_on-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-power_on-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

    def _test_shelve_server(self, server):
        self.flags(shelved_offload_time = -1)

        self.api.post_server_action(server['id'], {'shelve': {}})
        self._wait_for_state_change(self.api, server,
                                    expected_status='SHELVED')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-shelve-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-shelve-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

        post = {'unshelve': None}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.admin_api, server, 'ACTIVE')

    def _test_suspend_server(self, server):
        post = {'suspend': {}}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.admin_api, server, 'SUSPENDED')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-suspend-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-suspend-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

        post = {'resume': None}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.admin_api, server, 'ACTIVE')

    def _test_pause_server(self, server):
        self.api.post_server_action(server['id'], {'pause': {}})
        self._wait_for_state_change(self.api, server, 'PAUSED')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-pause-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-pause-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

        post = {'unpause': None}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.admin_api, server, 'ACTIVE')

    def _test_resize_server(self, server):
        self.flags(allow_resize_to_same_host=True)
        post = {'resize': {'flavorRef': '2'}}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.api, server, 'VERIFY_RESIZE')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-resize-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-resize-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

        post = {'revertResize': None}
        self.api.post_server_action(server['id'], post)
        self._wait_for_state_change(self.api, server, 'ACTIVE')

    def _test_restore_server(self, server):
        self.flags(reclaim_instance_interval=30)
        self.api.delete_server(server['id'])
        self._wait_for_state_change(self.api, server, 'SOFT_DELETED')
        self.api.post_server_action(server['id'], {'restore': {}})
        self._wait_for_state_change(self.api, server, 'ACTIVE')

        self.assertEqual(2, len(fake_notifier.VERSIONED_NOTIFICATIONS))
        self._verify_notification(
            'instance-restore-start',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[0])
        self._verify_notification(
            'instance-restore-end',
            replacements={
                'reservation_id': server['reservation_id'],
                'uuid': server['id']},
            actual=fake_notifier.VERSIONED_NOTIFICATIONS[1])

        self.flags(reclaim_instance_interval=0)
