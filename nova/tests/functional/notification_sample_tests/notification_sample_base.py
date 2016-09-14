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

import mock
import os
import time

from oslo_config import cfg
from oslo_serialization import jsonutils
from oslo_utils import fixture as utils_fixture

from nova import test
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional.api import client as api_client
from nova.tests.functional import integrated_helpers
from nova.tests.unit.api.openstack.compute import test_services
from nova.tests.unit import fake_notifier
import nova.tests.unit.image.fake

CONF = cfg.CONF


class NotificationSampleTestBase(test.TestCase,
                                 integrated_helpers.InstanceHelperMixin):
    """Base class for notification sample testing.

    To add tests for a versioned notification you have to store a sample file
    under doc/notification_sample directory. In the test method in the subclass
    trigger a change in the system that expected to generate the notification
    then use the _verify_notification function to assert if the stored sample
    matches with the generated one.

    If the notification has different payload content depending on the state
    change you triggered then the replacements parameter of the
    _verify_notification function can be used to override values coming from
    the sample file.

    Check nova.functional.notification_sample_tests.test_service_update as an
    example.
    """

    ANY = object()

    def setUp(self):
        super(NotificationSampleTestBase, self).setUp()
        # Needs to mock this to avoid REQUIRES_LOCKING to be set to True
        patcher = mock.patch('oslo_concurrency.lockutils.lock')
        self.addCleanup(patcher.stop)
        patcher.start()

        api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
                api_version='v2.1'))

        self.api = api_fixture.api
        self.admin_api = api_fixture.admin_api
        fake_notifier.stub_notifier(self)
        self.addCleanup(fake_notifier.reset)

        self.useFixture(utils_fixture.TimeFixture(test_services.fake_utcnow()))

        self.flags(scheduler_driver='nova.scheduler.chance.ChanceScheduler')
        # the image fake backend needed for image discovery
        nova.tests.unit.image.fake.stub_out_image_service(self)
        self.addCleanup(nova.tests.unit.image.fake.FakeImageService_reset)

        self.start_service('conductor', manager=CONF.conductor.manager)
        self.start_service('scheduler')
        self.start_service('network')
        self.compute = self.start_service('compute')

    def _get_notification_sample(self, sample):
        sample_dir = os.path.dirname(os.path.abspath(__file__))
        sample_dir = os.path.normpath(os.path.join(
            sample_dir,
            "../../../../doc/notification_samples"))
        return sample_dir + '/' + sample + '.json'

    def _apply_replacements(self, replacements, sample_obj, notification):
        replacements = replacements or {}
        for key, value in replacements.items():
            obj = sample_obj['payload']
            n_obj = notification['payload']
            for sub_key in key.split('.')[:-1]:
                obj = obj['nova_object.data'][sub_key]
                n_obj = n_obj['nova_object.data'][sub_key]
            if value == NotificationSampleTestBase.ANY:
                del obj['nova_object.data'][key.split('.')[-1]]
                del n_obj['nova_object.data'][key.split('.')[-1]]
            else:
                obj['nova_object.data'][key.split('.')[-1]] = value

    def _verify_notification(self, sample_file_name, replacements=None,
                             actual=None):
        """Assert if the generated notification matches with the stored sample

        :param sample_file_name: The name of the sample file to match relative
                                 to doc/notification_samples
        :param replacements: A dict of key value pairs that is used to update
                             the payload field of the sample data before it is
                             matched against the generated notification.
                             The 'x.y':'new-value' key-value pair selects the
                             ["payload"]["nova_object.data"]["x"]
                             ["nova_object.data"]["y"] value from the sample
                             data and overrides it with 'new-value'. There is
                             a special value ANY that can be used to indicate
                             that the actual field value shall be ignored
                             during matching.
        :param actual: Defines the actual notification to compare with. If
                       None then it defaults to the first versioned
                       notification emitted during the test.
        """
        if not actual:
            self.assertEqual(1, len(fake_notifier.VERSIONED_NOTIFICATIONS))
            notification = fake_notifier.VERSIONED_NOTIFICATIONS[0]
        else:
            notification = actual

        with open(self._get_notification_sample(sample_file_name)) as sample:
            sample_data = sample.read()

        sample_obj = jsonutils.loads(sample_data)
        self._apply_replacements(replacements, sample_obj, notification)

        self.assertJsonEqual(sample_obj, notification)

    def _boot_a_server(self, expected_status='ACTIVE', extra_params=None):

        # We have to depend on a specific image and flavor to fix the content
        # of the notification that will be emitted
        flavor_body = {'flavor': {'name': 'test_flavor',
                                  'ram': 512,
                                  'vcpus': 1,
                                  'disk': 1,
                                  'id': 'a22d5517-147c-4147-a0d1-e698df5cd4e3'
                                  }}

        flavor_id = self.api.post_flavor(flavor_body)['id']

        server = self._build_minimal_create_server_request(
            self.api, 'some-server',
            image_uuid='155d900f-4e14-4e4c-a73d-069cbf4541e6',
            flavor_id=flavor_id)

        extra_params['return_reservation_id'] = True

        if extra_params:
            server.update(extra_params)

        post = {'server': server}
        created_server = self.api.post_server(post)
        reservation_id = created_server['reservation_id']
        created_server = self.api.get_servers(
            detail=False,
            search_opts={'reservation_id': reservation_id})[0]

        self.assertTrue(created_server['id'])

        # Wait for it to finish being created
        found_server = self._wait_for_state_change(self.api, created_server,
                                                   expected_status)
        found_server['reservation_id'] = reservation_id
        return found_server

    def _wait_until_deleted(self, server):
        try:
            for i in range(40):
                server = self.api.get_server(server['id'])
                if server['status'] == 'ERROR':
                    self.fail('Server went to error state instead of'
                              'disappearing.')
                time.sleep(0.5)

            self.fail('Server failed to delete.')
        except api_client.OpenStackApiNotFoundException:
            return

    def _get_notifications(self, event_type):
        return [notification for notification
                    in fake_notifier.VERSIONED_NOTIFICATIONS
                    if notification['event_type'] == event_type]
