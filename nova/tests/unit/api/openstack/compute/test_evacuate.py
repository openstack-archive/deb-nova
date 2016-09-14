#   Copyright 2013 OpenStack Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

import uuid

import mock
import testtools
import webob

from nova.api.openstack.compute import evacuate as evacuate_v21
from nova.compute import api as compute_api
from nova.compute import vm_states
import nova.conf
from nova import exception
from nova import test
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_instance


CONF = nova.conf.CONF


def fake_compute_api(*args, **kwargs):
    return True


def fake_compute_api_get(self, context, instance_id, **kwargs):
    # BAD_UUID is something that does not exist
    if instance_id == 'BAD_UUID':
        raise exception.InstanceNotFound(instance_id=instance_id)
    else:
        return fake_instance.fake_instance_obj(context, id=1, uuid=instance_id,
                                               task_state=None, host='host1',
                                               vm_state=vm_states.ACTIVE)


def fake_service_get_by_compute_host(self, context, host):
    if host == 'bad-host':
        raise exception.ComputeHostNotFound(host=host)
    else:
        return {
            'host_name': host,
            'service': 'compute',
            'zone': 'nova'
            }


class EvacuateTestV21(test.NoDBTestCase):
    validation_error = exception.ValidationError
    _methods = ('resize', 'evacuate')

    def setUp(self):
        super(EvacuateTestV21, self).setUp()
        self.stubs.Set(compute_api.API, 'get', fake_compute_api_get)
        self.stubs.Set(compute_api.HostAPI, 'service_get_by_compute_host',
                       fake_service_get_by_compute_host)
        self.UUID = uuid.uuid4()
        for _method in self._methods:
            self.stubs.Set(compute_api.API, _method, fake_compute_api)
        self._set_up_controller()
        self.admin_req = fakes.HTTPRequest.blank('', use_admin_context=True)
        self.req = fakes.HTTPRequest.blank('')

    def _set_up_controller(self):
        self.controller = evacuate_v21.EvacuateController()
        self.controller_no_ext = self.controller

    def _get_evacuate_response(self, json_load, uuid=None):
        base_json_load = {'evacuate': json_load}
        response = self.controller._evacuate(self.admin_req, uuid or self.UUID,
                                             body=base_json_load)

        return response

    def _check_evacuate_failure(self, exception, body, uuid=None,
                                controller=None):
        controller = controller or self.controller
        body = {'evacuate': body}
        self.assertRaises(exception,
                          controller._evacuate,
                          self.admin_req, uuid or self.UUID, body=body)

    def test_evacuate_with_valid_instance(self):
        admin_pass = 'MyNewPass'
        res = self._get_evacuate_response({'host': 'my-host',
                                           'onSharedStorage': 'False',
                                           'adminPass': admin_pass})

        self.assertEqual(admin_pass, res['adminPass'])

    def test_evacuate_with_invalid_instance(self):
        self._check_evacuate_failure(webob.exc.HTTPNotFound,
                                     {'host': 'my-host',
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'},
                                     uuid='BAD_UUID')

    def test_evacuate_with_active_service(self):
        def fake_evacuate(*args, **kwargs):
            raise exception.ComputeServiceInUse("Service still in use")

        self.stubs.Set(compute_api.API, 'evacuate', fake_evacuate)
        self._check_evacuate_failure(webob.exc.HTTPBadRequest,
                                     {'host': 'my-host',
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_no_target(self):
        admin_pass = 'MyNewPass'
        res = self._get_evacuate_response({'onSharedStorage': 'False',
                                           'adminPass': admin_pass})

        self.assertEqual(admin_pass, res['adminPass'])

    def test_evacuate_instance_without_on_shared_storage(self):
        self._check_evacuate_failure(self.validation_error,
                                     {'host': 'my-host',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_invalid_characters_host(self):
        host = 'abc!#'
        self._check_evacuate_failure(self.validation_error,
                                     {'host': host,
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_too_long_host(self):
        host = 'a' * 256
        self._check_evacuate_failure(self.validation_error,
                                     {'host': host,
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_invalid_on_shared_storage(self):
        self._check_evacuate_failure(self.validation_error,
                                     {'host': 'my-host',
                                      'onSharedStorage': 'foo',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_bad_target(self):
        self._check_evacuate_failure(webob.exc.HTTPNotFound,
                                     {'host': 'bad-host',
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_target(self):
        admin_pass = 'MyNewPass'
        res = self._get_evacuate_response({'host': 'my-host',
                                           'onSharedStorage': 'False',
                                           'adminPass': admin_pass})

        self.assertEqual(admin_pass, res['adminPass'])

    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_shared_and_pass(self, mock_save):
        self._check_evacuate_failure(webob.exc.HTTPBadRequest,
                                     {'host': 'bad-host',
                                      'onSharedStorage': 'True',
                                      'adminPass': 'MyNewPass'})

    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_not_shared_pass_generated(self, mock_save):
        res = self._get_evacuate_response({'host': 'my-host',
                                           'onSharedStorage': 'False'})
        self.assertEqual(CONF.password_length, len(res['adminPass']))

    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_shared(self, mock_save):
        self._get_evacuate_response({'host': 'my-host',
                                     'onSharedStorage': 'True'})

    def test_not_admin(self):
        body = {'evacuate': {'host': 'my-host',
                             'onSharedStorage': 'False'}}
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller._evacuate,
                          self.req, self.UUID, body=body)

    def test_evacuate_to_same_host(self):
        self._check_evacuate_failure(webob.exc.HTTPBadRequest,
                                     {'host': 'host1',
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_empty_host(self):
        self._check_evacuate_failure(self.validation_error,
                                     {'host': '',
                                      'onSharedStorage': 'False',
                                      'adminPass': 'MyNewPass'},
                                     controller=self.controller_no_ext)

    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_instance_with_underscore_in_hostname(self, mock_save):
        admin_pass = 'MyNewPass'
        # NOTE: The hostname grammar in RFC952 does not allow for
        # underscores in hostnames. However, we should test that it
        # is supported because it sometimes occurs in real systems.
        res = self._get_evacuate_response({'host': 'underscore_hostname',
                                           'onSharedStorage': 'False',
                                           'adminPass': admin_pass})

        self.assertEqual(admin_pass, res['adminPass'])

    def test_evacuate_disable_password_return(self):
        self._test_evacuate_enable_instance_password_conf(enable_pass=False)

    def test_evacuate_enable_password_return(self):
        self._test_evacuate_enable_instance_password_conf(enable_pass=True)

    @mock.patch('nova.objects.Instance.save')
    def _test_evacuate_enable_instance_password_conf(self, mock_save,
                                                     enable_pass):
        self.flags(enable_instance_password=enable_pass)

        res = self._get_evacuate_response({'host': 'underscore_hostname',
                                           'onSharedStorage': 'False'})
        if enable_pass:
            self.assertIn('adminPass', res)
        else:
            self.assertIsNone(res)


class EvacuatePolicyEnforcementv21(test.NoDBTestCase):

    def setUp(self):
        super(EvacuatePolicyEnforcementv21, self).setUp()
        self.controller = evacuate_v21.EvacuateController()
        self.req = fakes.HTTPRequest.blank('')
        req_context = self.req.environ['nova.context']
        self.stub_out('nova.compute.api.HostAPI.service_get_by_compute_host',
                       fake_service_get_by_compute_host)

        def fake_get_instance(self, context, id):
            return fake_instance.fake_instance_obj(
                req_context,
                project_id=req_context.project_id,
                user_id=req_context.user_id)

        self.stub_out(
            'nova.api.openstack.common.get_instance', fake_get_instance)

    def test_evacuate_policy_failed_with_other_project(self):
        rule_name = "os_compute_api:os-evacuate"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        req = fakes.HTTPRequest.blank('')
        # Change the project_id in request context.
        req.environ['nova.context'].project_id = 'other-project'
        body = {'evacuate': {'host': 'my-host',
                             'onSharedStorage': 'False',
                             'adminPass': 'MyNewPass'
                             }}
        exc = self.assertRaises(
            exception.PolicyNotAuthorized,
            self.controller._evacuate, req, fakes.FAKE_UUID,
            body=body)
        self.assertEqual(
            "Policy doesn't allow %s to be performed." % rule_name,
            exc.format_message())

    @mock.patch('nova.compute.api.API.evacuate')
    def test_evacuate_overridden_policy_pass_with_same_project(self,
                                                               evacuate_mock):
        rule_name = "os_compute_api:os-evacuate"
        self.policy.set_rules({rule_name: "project_id:%(project_id)s"})
        body = {'evacuate': {'host': 'my-host',
                             'onSharedStorage': 'False',
                             'adminPass': 'MyNewPass'
                             }}
        self.controller._evacuate(self.req, fakes.FAKE_UUID, body=body)
        evacuate_mock.assert_called_once_with(self.req.environ['nova.context'],
                                              mock.ANY, 'my-host', False,
                                              'MyNewPass', None)

    def test_evacuate_overridden_policy_failed_with_other_user(self):
        rule_name = "os_compute_api:os-evacuate"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        req = fakes.HTTPRequest.blank('')
        # Change the user_id in request context.
        req.environ['nova.context'].user_id = 'other-user'
        body = {'evacuate': {'host': 'my-host',
                             'onSharedStorage': 'False',
                             'adminPass': 'MyNewPass'
                             }}
        exc = self.assertRaises(exception.PolicyNotAuthorized,
                                self.controller._evacuate, req,
                                fakes.FAKE_UUID, body=body)
        self.assertEqual(
                      "Policy doesn't allow %s to be performed." % rule_name,
                      exc.format_message())

    @mock.patch('nova.compute.api.API.evacuate')
    def test_evacuate_overridden_policy_pass_with_same_user(self,
                                                        evacuate_mock):
        rule_name = "os_compute_api:os-evacuate"
        self.policy.set_rules({rule_name: "user_id:%(user_id)s"})
        body = {'evacuate': {'host': 'my-host',
                             'onSharedStorage': 'False',
                             'adminPass': 'MyNewPass'
                             }}
        self.controller._evacuate(self.req, fakes.FAKE_UUID, body=body)
        evacuate_mock.assert_called_once_with(self.req.environ['nova.context'],
                                              mock.ANY, 'my-host', False,
                                              'MyNewPass', None)


class EvacuateTestV214(EvacuateTestV21):
    def setUp(self):
        super(EvacuateTestV214, self).setUp()
        self.admin_req = fakes.HTTPRequest.blank('', use_admin_context=True,
                                                 version='2.14')
        self.req = fakes.HTTPRequest.blank('', version='2.14')

    def _get_evacuate_response(self, json_load, uuid=None):
        json_load.pop('onSharedStorage', None)
        base_json_load = {'evacuate': json_load}
        response = self.controller._evacuate(self.admin_req, uuid or self.UUID,
                                             body=base_json_load)

        return response

    def _check_evacuate_failure(self, exception, body, uuid=None,
                                controller=None):
        controller = controller or self.controller
        body.pop('onSharedStorage', None)
        body = {'evacuate': body}
        self.assertRaises(exception,
                          controller._evacuate,
                          self.admin_req, uuid or self.UUID, body=body)

    @mock.patch.object(compute_api.API, 'evacuate')
    def test_evacuate_instance(self, mock_evacuate):
        self._get_evacuate_response({})
        admin_pass = mock_evacuate.call_args_list[0][0][4]
        on_shared_storage = mock_evacuate.call_args_list[0][0][3]
        self.assertEqual(CONF.password_length, len(admin_pass))
        self.assertIsNone(on_shared_storage)

    def test_evacuate_with_valid_instance(self):
        admin_pass = 'MyNewPass'
        res = self._get_evacuate_response({'host': 'my-host',
                                           'adminPass': admin_pass})

        self.assertIsNone(res)

    @testtools.skip('Password is not returned from Microversion 2.14')
    def test_evacuate_disable_password_return(self):
        pass

    @testtools.skip('Password is not returned from Microversion 2.14')
    def test_evacuate_enable_password_return(self):
        pass

    @testtools.skip('onSharedStorage was removed from Microversion 2.14')
    def test_evacuate_instance_with_invalid_on_shared_storage(self):
        pass

    @testtools.skip('onSharedStorage was removed from Microversion 2.14')
    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_not_shared_pass_generated(self, mock_save):
        pass

    @mock.patch.object(compute_api.API, 'evacuate')
    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_pass_generated(self, mock_save, mock_evacuate):
        self._get_evacuate_response({'host': 'my-host'})
        self.assertEqual(CONF.password_length,
                         len(mock_evacuate.call_args_list[0][0][4]))

    def test_evacuate_instance_without_on_shared_storage(self):
        self._get_evacuate_response({'host': 'my-host',
                                     'adminPass': 'MyNewPass'})

    def test_evacuate_instance_with_no_target(self):
        admin_pass = 'MyNewPass'
        with mock.patch.object(compute_api.API, 'evacuate') as mock_evacuate:
            self._get_evacuate_response({'adminPass': admin_pass})
            self.assertEqual(admin_pass,
                             mock_evacuate.call_args_list[0][0][4])

    def test_not_admin(self):
        body = {'evacuate': {'host': 'my-host'}}
        self.assertRaises(exception.PolicyNotAuthorized,
                          self.controller._evacuate,
                          self.req, self.UUID, body=body)

    @testtools.skip('onSharedStorage was removed from Microversion 2.14')
    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_shared_and_pass(self, mock_save):
        pass

    @testtools.skip('from Microversion 2.14 it is covered with '
                    'test_evacuate_pass_generated')
    def test_evacuate_instance_with_target(self):
        pass

    @mock.patch('nova.objects.Instance.save')
    def test_evacuate_instance_with_underscore_in_hostname(self, mock_save):
        # NOTE: The hostname grammar in RFC952 does not allow for
        # underscores in hostnames. However, we should test that it
        # is supported because it sometimes occurs in real systems.
        self._get_evacuate_response({'host': 'underscore_hostname'})


class EvacuateTestV229(EvacuateTestV214):
    def setUp(self):
        super(EvacuateTestV229, self).setUp()
        self.admin_req = fakes.HTTPRequest.blank('', use_admin_context=True,
                                                 version='2.29')
        self.req = fakes.HTTPRequest.blank('', version='2.29')

    @mock.patch.object(compute_api.API, 'evacuate')
    def test_evacuate_instance(self, mock_evacuate):
        self._get_evacuate_response({})
        admin_pass = mock_evacuate.call_args_list[0][0][4]
        on_shared_storage = mock_evacuate.call_args_list[0][0][3]
        force = mock_evacuate.call_args_list[0][0][5]
        self.assertEqual(CONF.password_length, len(admin_pass))
        self.assertIsNone(on_shared_storage)
        self.assertEqual(False, force)

    def test_evacuate_with_valid_instance(self):
        admin_pass = 'MyNewPass'
        res = self._get_evacuate_response({'host': 'my-host',
                                           'adminPass': admin_pass,
                                           'force': 'false'})
        self.assertIsNone(res)

    @mock.patch.object(compute_api.API, 'evacuate')
    def test_evacuate_instance_with_forced_host(self, mock_evacuate):
        self._get_evacuate_response({'host': 'my-host',
                                     'force': 'true'})
        force = mock_evacuate.call_args_list[0][0][5]
        self.assertEqual(True, force)

    def test_forced_evacuate_with_no_host_provided(self):
        self._check_evacuate_failure(webob.exc.HTTPBadRequest,
                                     {'force': 'true'})
