# Copyright 2013 OpenStack Foundation
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
from mox3 import mox
from oslo_config import cfg
from oslo_serialization import jsonutils
from six.moves import range
from webob import exc

from nova.api.openstack.compute import block_device_mapping
from nova.api.openstack.compute import extension_info
from nova.api.openstack.compute import servers as servers_v21
from nova import block_device
from nova.compute import api as compute_api
from nova import exception
from nova import objects
from nova import test
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit.image import fake
from nova.tests.unit import matchers

CONF = cfg.CONF


class BlockDeviceMappingTestV21(test.TestCase):
    validation_error = exception.ValidationError

    def _setup_controller(self):
        ext_info = extension_info.LoadedExtensionInfo()
        self.controller = servers_v21.ServersController(
                                        extension_info=ext_info)
        CONF.set_override('extensions_blacklist', 'os-block-device-mapping',
                          'osapi_v21')
        self.no_bdm_v2_controller = servers_v21.ServersController(
                extension_info=ext_info)
        CONF.set_override('extensions_blacklist', '', 'osapi_v21')

    def setUp(self):
        super(BlockDeviceMappingTestV21, self).setUp()
        self._setup_controller()
        fake.stub_out_image_service(self)

        self.bdm = [{
            'no_device': None,
            'source_type': 'volume',
            'destination_type': 'volume',
            'uuid': 'fake',
            'device_name': 'vdb',
            'delete_on_termination': False,
        }]

    def _get_servers_body(self, no_image=False):
        body = {
            'server': {
                'name': 'server_test',
                'imageRef': '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6',
                'flavorRef': 'http://localhost/123/flavors/3',
                'metadata': {
                    'hello': 'world',
                    'open': 'stack',
                },
            },
        }
        if no_image:
            del body['server']['imageRef']
        return body

    def _test_create(self, params, no_image=False, override_controller=None):
        body = self._get_servers_body(no_image)
        body['server'].update(params)

        req = fakes.HTTPRequest.blank('/v2/fake/servers')
        req.method = 'POST'
        req.headers['content-type'] = 'application/json'

        req.body = jsonutils.dump_as_bytes(body)

        if override_controller:
            override_controller.create(req, body=body).obj['server']
        else:
            self.controller.create(req, body=body).obj['server']

    def test_create_instance_with_block_device_mapping_disabled(self):
        bdm = [{'device_name': 'foo'}]

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertNotIn('block_device_mapping', kwargs)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: bdm}
        self._test_create(params,
                          override_controller=self.no_bdm_v2_controller)

    def test_create_instance_with_volumes_enabled_no_image(self):
        """Test that the create will fail if there is no image
        and no bdms supplied in the request
        """
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertNotIn('imageRef', kwargs)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        self.assertRaises(exc.HTTPBadRequest,
                          self._test_create, {}, no_image=True)

    def test_create_instance_with_bdms_and_no_image(self):
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertThat(
                block_device.BlockDeviceDict(self.bdm[0]),
                matchers.DictMatches(kwargs['block_device_mapping'][0])
            )
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        self.mox.StubOutWithMock(compute_api.API, '_validate_bdm')
        self.mox.StubOutWithMock(compute_api.API, '_get_bdm_image_metadata')

        compute_api.API._validate_bdm(
            mox.IgnoreArg(), mox.IgnoreArg(), mox.IgnoreArg(),
            mox.IgnoreArg()).AndReturn(True)
        compute_api.API._get_bdm_image_metadata(
            mox.IgnoreArg(), mox.IgnoreArg(), False).AndReturn({})
        self.mox.ReplayAll()

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self._test_create(params, no_image=True)

    @mock.patch.object(compute_api.API, '_validate_bdm')
    @mock.patch.object(compute_api.API, '_get_bdm_image_metadata')
    def test_create_instance_with_bdms_and_empty_imageRef(
        self, mock_bdm_image_metadata, mock_validate_bdm):
        mock_bdm_image_metadata.return_value = {}
        mock_validate_bdm.return_value = True
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertThat(
                block_device.BlockDeviceDict(self.bdm[0]),
                matchers.DictMatches(kwargs['block_device_mapping'][0])
            )
            return old_create(*args, **kwargs)

        self.stub_out('nova.compute.api.API.create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm,
                  'imageRef': ''}
        self._test_create(params)

    def test_create_instance_with_imageRef_as_full_url(self):
        bdm = [{'device_name': 'foo'}]
        image_href = ('http://localhost/v2/fake/images/'
                     '76fa36fc-c930-4bf3-8c8a-ea2a2420deb6')
        params = {block_device_mapping.ATTRIBUTE_NAME: bdm,
                  'imageRef': image_href}
        self.assertRaises(exception.ValidationError,
                          self._test_create, params)

    def test_create_instance_with_non_uuid_imageRef(self):
        bdm = [{'device_name': 'foo'}]

        params = {block_device_mapping.ATTRIBUTE_NAME: bdm,
                  'imageRef': '123123abcd'}
        self.assertRaises(exception.ValidationError,
                          self._test_create, params)

    def test_create_instance_with_device_name_not_string(self):
        self.bdm[0]['device_name'] = 123
        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertEqual(kwargs['block_device_mapping'], self.bdm)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    @mock.patch.object(compute_api.API, 'create')
    def test_create_instance_with_bdm_param_not_list(self, mock_create):
        self.params = {'block_device_mapping': '/dev/vdb'}
        self.assertRaises(self.validation_error,
                          self._test_create, self.params)

    def test_create_instance_with_device_name_empty(self):
        self.bdm[0]['device_name'] = ''

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertEqual(kwargs['block_device_mapping'], self.bdm)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    def test_create_instance_with_device_name_too_long(self):
        self.bdm[0]['device_name'] = 'a' * 256

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertEqual(kwargs['block_device_mapping'], self.bdm)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    def test_create_instance_with_space_in_device_name(self):
        self.bdm[0]['device_name'] = 'v da'

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertTrue(kwargs['legacy_bdm'])
            self.assertEqual(kwargs['block_device_mapping'], self.bdm)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    def test_create_instance_with_invalid_size(self):
        self.bdm[0]['volume_size'] = 'hello world'

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertEqual(kwargs['block_device_mapping'], self.bdm)
            return old_create(*args, **kwargs)

        self.stubs.Set(compute_api.API, 'create', create)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    def _test_create_instance_with_destination_type_error(self,
                                                          destination_type):
        self.bdm[0]['destination_type'] = destination_type

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(self.validation_error,
                          self._test_create, params, no_image=True)

    def test_create_instance_with_destination_type_empty_string(self):
        self._test_create_instance_with_destination_type_error('')

    def test_create_instance_with_invalid_destination_type(self):
        self._test_create_instance_with_destination_type_error('fake')

    def test_create_instance_bdm(self):
        bdm = [{
            'source_type': 'volume',
            'device_name': 'fake_dev',
            'uuid': 'fake_vol'
        }]
        bdm_expected = [{
            'source_type': 'volume',
            'device_name': 'fake_dev',
            'volume_id': 'fake_vol'
        }]

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertFalse(kwargs['legacy_bdm'])
            for expected, received in zip(bdm_expected,
                                          kwargs['block_device_mapping']):
                self.assertThat(block_device.BlockDeviceDict(expected),
                                matchers.DictMatches(received))
            return old_create(*args, **kwargs)

        def _validate_bdm(*args, **kwargs):
            pass

        self.stubs.Set(compute_api.API, 'create', create)
        self.stubs.Set(compute_api.API, '_validate_bdm', _validate_bdm)

        params = {block_device_mapping.ATTRIBUTE_NAME: bdm}
        self._test_create(params, no_image=True)

    def test_create_instance_bdm_missing_device_name(self):
        del self.bdm[0]['device_name']

        old_create = compute_api.API.create

        def create(*args, **kwargs):
            self.assertFalse(kwargs['legacy_bdm'])
            self.assertNotIn(None,
                             kwargs['block_device_mapping'][0]['device_name'])
            return old_create(*args, **kwargs)

        def _validate_bdm(*args, **kwargs):
            pass

        self.stubs.Set(compute_api.API, 'create', create)
        self.stubs.Set(compute_api.API, '_validate_bdm', _validate_bdm)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self._test_create(params, no_image=True)

    def test_create_instance_bdm_validation_error(self):
        def _validate(*args, **kwargs):
            raise exception.InvalidBDMFormat(details='Wrong BDM')

        self.stubs.Set(block_device.BlockDeviceDict,
                      '_validate', _validate)

        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        self.assertRaises(exc.HTTPBadRequest,
                          self._test_create, params, no_image=True)

    @mock.patch('nova.compute.api.API._get_bdm_image_metadata')
    def test_create_instance_non_bootable_volume_fails(self, fake_bdm_meta):
        params = {block_device_mapping.ATTRIBUTE_NAME: self.bdm}
        fake_bdm_meta.side_effect = exception.InvalidBDMVolumeNotBootable(id=1)
        self.assertRaises(exc.HTTPBadRequest, self._test_create, params,
                          no_image=True)

    def test_create_instance_bdm_api_validation_fails(self):
        self.validation_fail_test_validate_called = False
        self.validation_fail_instance_destroy_called = False

        bdm_exceptions = ((exception.InvalidBDMSnapshot, {'id': 'fake'}),
                          (exception.InvalidBDMVolume, {'id': 'fake'}),
                          (exception.InvalidBDMImage, {'id': 'fake'}),
                          (exception.InvalidBDMBootSequence, {}),
                          (exception.InvalidBDMLocalsLimit, {}))

        ex_iter = iter(bdm_exceptions)

        def _validate_bdm(*args, **kwargs):
            self.validation_fail_test_validate_called = True
            ex, kargs = next(ex_iter)
            raise ex(**kargs)

        def _instance_destroy(*args, **kwargs):
            self.validation_fail_instance_destroy_called = True

        self.stubs.Set(compute_api.API, '_validate_bdm', _validate_bdm)
        self.stubs.Set(objects.Instance, 'destroy', _instance_destroy)

        for _unused in range(len(bdm_exceptions)):
            params = {block_device_mapping.ATTRIBUTE_NAME:
                      [self.bdm[0].copy()]}
            self.assertRaises(exc.HTTPBadRequest,
                              self._test_create, params)
            self.assertTrue(self.validation_fail_test_validate_called)
            self.assertFalse(self.validation_fail_instance_destroy_called)
            self.validation_fail_test_validate_called = False
            self.validation_fail_instance_destroy_called = False
