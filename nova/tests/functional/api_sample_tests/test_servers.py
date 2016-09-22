# Copyright 2012 Nebula, Inc.
# Copyright 2013 IBM Corp.
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

from nova.api.openstack import api_version_request as avr
from nova.tests.functional.api_sample_tests import api_sample_base
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit.image import fake


class ServersSampleBase(api_sample_base.ApiSampleTestBaseV21):
    microversion = None
    sample_dir = 'servers'

    user_data_contents = '#!/bin/bash\n/bin/su\necho "I am in you!"\n'
    user_data = base64.b64encode(user_data_contents)

    common_req_names = [
        (None, '2.36', 'server-create-req'),
        ('2.37', None, 'server-create-req-v237')
    ]

    def _get_request_name(self, use_common):
        if not use_common:
            return 'server-create-req'

        api_version = self.microversion or '2.1'
        for min, max, name in self.common_req_names:
            if avr.APIVersionRequest(api_version).matches(
                    avr.APIVersionRequest(min), avr.APIVersionRequest(max)):
                return name

    def _post_server(self, use_common_server_api_samples=True):
        # param use_common_server_api_samples: Boolean to set whether tests use
        # common sample files for server post request and response.
        # Default is True which means _get_sample_path method will fetch the
        # common server sample files from 'servers' directory.
        # Set False if tests need to use extension specific sample files
        subs = {
            'image_id': fake.get_valid_image_id(),
            'host': self._get_host(),
            'compute_endpoint': self._get_compute_endpoint(),
            'versioned_compute_endpoint': self._get_vers_compute_endpoint(),
            'glance_host': self._get_glance_host(),
            'access_ip_v4': '1.2.3.4',
            'access_ip_v6': '80fe::',
            'user_data': self.user_data,
            'uuid': '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}'
                    '-[0-9a-f]{4}-[0-9a-f]{12}',
        }

        orig_value = self.__class__._use_common_server_api_samples
        orig_sample_dir = self.__class__.sample_dir
        try:
            self.__class__._use_common_server_api_samples = (
                                        use_common_server_api_samples)
            response = self._do_post('servers', self._get_request_name(
                use_common_server_api_samples), subs)
            status = self._verify_response('server-create-resp', subs,
                                           response, 202)
            return status
        finally:
            self.__class__._use_common_server_api_samples = orig_value
            self.__class__.sample_dir = orig_sample_dir

    def setUp(self):
        super(ServersSampleBase, self).setUp()
        self.api.microversion = self.microversion


class ServersSampleJsonTest(ServersSampleBase):
    microversion = None

    def test_servers_post(self):
        return self._post_server()

    def test_servers_get(self):
        self.stub_out('nova.db.block_device_mapping_get_all_by_instance_uuids',
                      fakes.stub_bdm_get_all_by_instance_uuids)
        uuid = self.test_servers_post()
        response = self._do_get('servers/%s' % uuid)
        subs = {}
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['instance_name'] = 'instance-\d{8}'
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        subs['hostname'] = r'[\w\.\-]+'
        subs['mac_addr'] = '(?:[a-f0-9]{2}:){5}[a-f0-9]{2}'
        subs['access_ip_v4'] = '1.2.3.4'
        subs['access_ip_v6'] = '80fe::'
        subs['user_data'] = self.user_data
        # config drive can be a string for True or empty value for False
        subs['cdrive'] = '.*'
        self._verify_response('server-get-resp', subs, response, 200)

    def test_servers_list(self):
        uuid = self._post_server()
        response = self._do_get('servers')
        subs = {'id': uuid}
        self._verify_response('servers-list-resp', subs, response, 200)

    def test_servers_details(self):
        self.stub_out('nova.db.block_device_mapping_get_all_by_instance_uuids',
                      fakes.stub_bdm_get_all_by_instance_uuids)
        uuid = self.test_servers_post()
        response = self._do_get('servers/detail')
        subs = {}
        subs['hostid'] = '[a-f0-9]+'
        subs['id'] = uuid
        subs['instance_name'] = 'instance-\d{8}'
        subs['hypervisor_hostname'] = r'[\w\.\-]+'
        subs['hostname'] = r'[\w\.\-]+'
        subs['mac_addr'] = '(?:[a-f0-9]{2}:){5}[a-f0-9]{2}'
        subs['access_ip_v4'] = '1.2.3.4'
        subs['access_ip_v6'] = '80fe::'
        subs['user_data'] = self.user_data
        # config drive can be a string for True or empty value for False
        subs['cdrive'] = '.*'
        self._verify_response('servers-details-resp', subs, response, 200)


class ServersSampleJson23Test(ServersSampleJsonTest):
    microversion = '2.3'
    scenarios = [('v2_3', {'api_major_version': 'v2.1'})]


class ServersSampleJson29Test(ServersSampleJsonTest):
    microversion = '2.9'
    # NOTE(gmann): microversion tests do not need to run for v2 API
    # so defining scenarios only for v2.9 which will run the original tests
    # by appending '(v2_9)' in test_id.
    scenarios = [('v2_9', {'api_major_version': 'v2.1'})]


class ServersSampleJson216Test(ServersSampleJsonTest):
    microversion = '2.16'
    scenarios = [('v2_16', {'api_major_version': 'v2.1'})]


class ServersSampleJson219Test(ServersSampleJsonTest):
    microversion = '2.19'
    scenarios = [('v2_19', {'api_major_version': 'v2.1'})]

    def test_servers_post(self):
        return self._post_server(False)

    def test_servers_put(self):
        uuid = self.test_servers_post()
        response = self._do_put('servers/%s' % uuid, 'server-put-req', {})
        subs = {
            'image_id': fake.get_valid_image_id(),
            'hostid': '[a-f0-9]+',
            'glance_host': self._get_glance_host(),
            'access_ip_v4': '1.2.3.4',
            'access_ip_v6': '80fe::'
        }
        self._verify_response('server-put-resp', subs, response, 200)


class ServersSampleJson232Test(ServersSampleBase):
    microversion = '2.32'
    sample_dir = 'servers'
    scenarios = [('v2_32', {'api_major_version': 'v2.1'})]

    def test_servers_post(self):
        self._post_server(use_common_server_api_samples=False)


class ServersSampleJson237Test(ServersSampleBase):
    microversion = '2.37'
    sample_dir = 'servers'
    scenarios = [('v2_37', {'api_major_version': 'v2.1'})]

    def test_servers_post(self):
        self._post_server(use_common_server_api_samples=False)


class ServersUpdateSampleJsonTest(ServersSampleBase):

    def test_update_server(self):
        uuid = self._post_server()
        subs = {}
        subs['hostid'] = '[a-f0-9]+'
        subs['access_ip_v4'] = '1.2.3.4'
        subs['access_ip_v6'] = '80fe::'
        response = self._do_put('servers/%s' % uuid,
                                'server-update-req', subs)
        self._verify_response('server-update-resp', subs, response, 200)


class ServerSortKeysJsonTests(ServersSampleBase):
    sample_dir = 'servers-sort'

    def test_servers_list(self):
        self._post_server()
        response = self._do_get('servers?sort_key=display_name&sort_dir=asc')
        self._verify_response('server-sort-keys-list-resp', {}, response,
                              200)


class ServersActionsJsonTest(ServersSampleBase):

    def _test_server_action(self, uuid, action, req_tpl,
                            subs=None, resp_tpl=None, code=202):
        subs = subs or {}
        subs.update({'action': action,
                     'glance_host': self._get_glance_host()})
        response = self._do_post('servers/%s/action' % uuid,
                                 req_tpl,
                                 subs)
        if resp_tpl:
            self._verify_response(resp_tpl, subs, response, code)
        else:
            self.assertEqual(code, response.status_code)
            self.assertEqual("", response.content)

    def test_server_reboot_hard(self):
        uuid = self._post_server()
        self._test_server_action(uuid, "reboot",
                                 'server-action-reboot',
                                 {"type": "HARD"})

    def test_server_reboot_soft(self):
        uuid = self._post_server()
        self._test_server_action(uuid, "reboot",
                                 'server-action-reboot',
                                 {"type": "SOFT"})

    def test_server_rebuild(self):
        uuid = self._post_server()
        image = fake.get_valid_image_id()
        params = {
            'uuid': image,
            'name': 'foobar',
            'pass': 'seekr3t',
            'hostid': '[a-f0-9]+',
            'access_ip_v4': '1.2.3.4',
            'access_ip_v6': '80fe::',
        }

        resp = self._do_post('servers/%s/action' % uuid,
                             'server-action-rebuild', params)
        subs = params.copy()
        del subs['uuid']
        self._verify_response('server-action-rebuild-resp', subs, resp, 202)

    def test_server_resize(self):
        self.flags(allow_resize_to_same_host=True)
        uuid = self._post_server()
        self._test_server_action(uuid, "resize",
                                 'server-action-resize',
                                 {"id": '2',
                                  "host": self._get_host()})
        return uuid

    def test_server_revert_resize(self):
        uuid = self.test_server_resize()
        self._test_server_action(uuid, "revertResize",
                                 'server-action-revert-resize')

    def test_server_confirm_resize(self):
        uuid = self.test_server_resize()
        self._test_server_action(uuid, "confirmResize",
                                 'server-action-confirm-resize',
                                 code=204)

    def test_server_create_image(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'createImage',
                                 'server-action-create-image',
                                 {'name': 'foo-image'})


class ServersActionsJson219Test(ServersSampleBase):
    microversion = '2.19'
    scenarios = [('v2_19', {'api_major_version': 'v2.1'})]

    def test_server_rebuild(self):
        uuid = self._post_server()
        image = fake.get_valid_image_id()
        params = {
            'uuid': image,
            'name': 'foobar',
            'description': 'description of foobar',
            'pass': 'seekr3t',
            'hostid': '[a-f0-9]+',
            'access_ip_v4': '1.2.3.4',
            'access_ip_v6': '80fe::',
        }

        resp = self._do_post('servers/%s/action' % uuid,
                             'server-action-rebuild', params)
        subs = params.copy()
        del subs['uuid']
        self._verify_response('server-action-rebuild-resp', subs, resp, 202)


class ServerStartStopJsonTest(ServersSampleBase):

    def _test_server_action(self, uuid, action, req_tpl):
        response = self._do_post('servers/%s/action' % uuid,
                                 req_tpl,
                                 {'action': action})
        self.assertEqual(202, response.status_code)
        self.assertEqual("", response.content)

    def test_server_start(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'os-stop', 'server-action-stop')
        self._test_server_action(uuid, 'os-start', 'server-action-start')

    def test_server_stop(self):
        uuid = self._post_server()
        self._test_server_action(uuid, 'os-stop', 'server-action-stop')


class ServersSampleMultiStatusJsonTest(ServersSampleBase):

    def test_servers_list(self):
        uuid = self._post_server()
        response = self._do_get('servers?status=active&status=error')
        subs = {'id': uuid}
        self._verify_response('servers-list-resp', subs, response, 200)


class ServerTriggerCrashDumpJsonTest(ServersSampleBase):
    microversion = '2.17'
    scenarios = [('v2_17', {'api_major_version': 'v2.1'})]

    def test_trigger_crash_dump(self):
        uuid = self._post_server()

        response = self._do_post('servers/%s/action' % uuid,
                                 'server-action-trigger-crash-dump',
                                 {})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, "")
