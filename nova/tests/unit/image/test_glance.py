# Copyright 2011 OpenStack Foundation
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


import collections
import datetime

import cryptography
import glanceclient.exc
from glanceclient.v1 import images
import glanceclient.v2.schemas as schemas
import mock
import six
from six.moves import StringIO
import testtools

import nova.conf
from nova import context
from nova import exception
from nova.image import glance
from nova import test
from nova.tests import uuidsentinel as uuids

CONF = nova.conf.CONF
NOW_GLANCE_FORMAT = "2010-10-11T10:30:22.000000"


class tzinfo(datetime.tzinfo):
    @staticmethod
    def utcoffset(*args, **kwargs):
        return datetime.timedelta()

NOW_DATETIME = datetime.datetime(2010, 10, 11, 10, 30, 22, tzinfo=tzinfo())


class FakeSchema(object):
    def __init__(self, raw_schema):
        self.raw_schema = raw_schema
        self.base_props = ('checksum', 'container_format', 'created_at',
                           'direct_url', 'disk_format', 'file', 'id',
                           'locations', 'min_disk', 'min_ram', 'name',
                           'owner', 'protected', 'schema', 'self', 'size',
                           'status', 'tags', 'updated_at', 'virtual_size',
                           'visibility')

    def is_base_property(self, prop_name):
        return prop_name in self.base_props

image_fixtures = {
    'active_image_v1': {
        'checksum': 'eb9139e4942121f22bbc2afc0400b2a4',
        'container_format': 'ami',
        'created_at': '2015-08-31T19:37:41Z',
        'deleted': False,
        'disk_format': 'ami',
        'id': 'da8500d5-8b80-4b9c-8410-cc57fb8fb9d5',
        'is_public': True,
        'min_disk': 0,
        'min_ram': 0,
        'name': 'cirros-0.3.4-x86_64-uec',
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'properties': {
            'kernel_id': 'f6ebd5f0-b110-4406-8c1e-67b28d4e85e7',
            'ramdisk_id': '868efefc-4f2d-4ed8-82b1-7e35576a7a47'},
        'protected': False,
        'size': 25165824,
        'status': 'active',
        'updated_at': '2015-08-31T19:37:45Z'},
    'active_image_v2': {
        'checksum': 'eb9139e4942121f22bbc2afc0400b2a4',
        'container_format': 'ami',
        'created_at': '2015-08-31T19:37:41Z',
        'direct_url': 'swift+config://ref1/glance/'
                      'da8500d5-8b80-4b9c-8410-cc57fb8fb9d5',
        'disk_format': 'ami',
        'file': '/v2/images/'
                'da8500d5-8b80-4b9c-8410-cc57fb8fb9d5/file',
        'id': 'da8500d5-8b80-4b9c-8410-cc57fb8fb9d5',
        'kernel_id': 'f6ebd5f0-b110-4406-8c1e-67b28d4e85e7',
        'locations': [
            {'metadata': {},
             'url': 'swift+config://ref1/glance/'
                    'da8500d5-8b80-4b9c-8410-cc57fb8fb9d5'}],
        'min_disk': 0,
        'min_ram': 0,
        'name': 'cirros-0.3.4-x86_64-uec',
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'protected': False,
        'ramdisk_id': '868efefc-4f2d-4ed8-82b1-7e35576a7a47',
        'schema': '/v2/schemas/image',
        'size': 25165824,
        'status': 'active',
        'tags': [],
        'updated_at': '2015-08-31T19:37:45Z',
        'virtual_size': None,
        'visibility': 'public'},
    'empty_image_v1': {
        'created_at': '2015-09-01T22:37:32.000000',
        'deleted': False,
        'id': '885d1cb0-9f5c-4677-9d03-175be7f9f984',
        'is_public': False,
        'min_disk': 0,
        'min_ram': 0,
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'properties': {},
        'protected': False,
        'size': 0,
        'status': 'queued',
        'updated_at': '2015-09-01T22:37:32.000000'
    },
    'empty_image_v2': {
        'checksum': None,
        'container_format': None,
        'created_at': '2015-09-01T22:37:32Z',
        'disk_format': None,
        'file': '/v2/images/885d1cb0-9f5c-4677-9d03-175be7f9f984/file',
        'id': '885d1cb0-9f5c-4677-9d03-175be7f9f984',
        'locations': [],
        'min_disk': 0,
        'min_ram': 0,
        'name': None,
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'protected': False,
        'schema': '/v2/schemas/image',
        'size': None,
        'status': 'queued',
        'tags': [],
        'updated_at': '2015-09-01T22:37:32Z',
        'virtual_size': None,
        'visibility': 'private'
    },
    'custom_property_image_v1': {
        'checksum': 'e533283e6aac072533d1d091a7d2e413',
        'container_format': 'bare',
        'created_at': '2015-09-02T00:31:16.000000',
        'deleted': False,
        'disk_format': 'qcow2',
        'id': '10ca6b6b-48f4-43ac-8159-aa9e9353f5e4',
        'is_public': False,
        'min_disk': 0,
        'min_ram': 0,
        'name': 'fake_name',
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'properties': {'image_type': 'fake_image_type'},
        'protected': False,
        'size': 616,
        'status': 'active',
        'updated_at': '2015-09-02T00:31:17.000000'
    },
    'custom_property_image_v2': {
        'checksum': 'e533283e6aac072533d1d091a7d2e413',
        'container_format': 'bare',
        'created_at': '2015-09-02T00:31:16Z',
        'disk_format': 'qcow2',
        'file': '/v2/images/10ca6b6b-48f4-43ac-8159-aa9e9353f5e4/file',
        'id': '10ca6b6b-48f4-43ac-8159-aa9e9353f5e4',
        'image_type': 'fake_image_type',
        'min_disk': 0,
        'min_ram': 0,
        'name': 'fake_name',
        'owner': 'ea583a4f34444a12bbe4e08c2418ba1f',
        'protected': False,
        'schema': '/v2/schemas/image',
        'size': 616,
        'status': 'active',
        'tags': [],
        'updated_at': '2015-09-02T00:31:17Z',
        'virtual_size': None,
        'visibility': 'private'
    }
}


class ImageV2(dict):
    # Wrapper class that is used to comply with dual nature of
    # warlock objects, that are inherited from dict and have 'schema'
    # attribute.
    schema = mock.MagicMock()


class TestConversions(test.NoDBTestCase):
    def test_convert_timestamps_to_datetimes(self):
        fixture = {'name': None,
                   'properties': {},
                   'status': None,
                   'is_public': None,
                   'created_at': NOW_GLANCE_FORMAT,
                   'updated_at': NOW_GLANCE_FORMAT,
                   'deleted_at': NOW_GLANCE_FORMAT}
        result = glance._convert_timestamps_to_datetimes(fixture)
        self.assertEqual(result['created_at'], NOW_DATETIME)
        self.assertEqual(result['updated_at'], NOW_DATETIME)
        self.assertEqual(result['deleted_at'], NOW_DATETIME)

    def _test_extracting_missing_attributes(self, include_locations):
        # Verify behavior from glance objects that are missing attributes
        # TODO(jaypipes): Find a better way of testing this crappy
        #                 glanceclient magic object stuff.
        class MyFakeGlanceImage(object):
            def __init__(self, metadata):
                IMAGE_ATTRIBUTES = ['size', 'owner', 'id', 'created_at',
                                    'updated_at', 'status', 'min_disk',
                                    'min_ram', 'is_public']
                raw = dict.fromkeys(IMAGE_ATTRIBUTES)
                raw.update(metadata)
                self.__dict__['raw'] = raw

            def __getattr__(self, key):
                try:
                    return self.__dict__['raw'][key]
                except KeyError:
                    raise AttributeError(key)

            def __setattr__(self, key, value):
                try:
                    self.__dict__['raw'][key] = value
                except KeyError:
                    raise AttributeError(key)

        metadata = {
            'id': 1,
            'created_at': NOW_DATETIME,
            'updated_at': NOW_DATETIME,
        }
        image = MyFakeGlanceImage(metadata)
        observed = glance._extract_attributes(
            image, include_locations=include_locations)
        expected = {
            'id': 1,
            'name': None,
            'is_public': None,
            'size': 0,
            'min_disk': None,
            'min_ram': None,
            'disk_format': None,
            'container_format': None,
            'checksum': None,
            'created_at': NOW_DATETIME,
            'updated_at': NOW_DATETIME,
            'deleted_at': None,
            'deleted': None,
            'status': None,
            'properties': {},
            'owner': None
        }
        if include_locations:
            expected['locations'] = None
            expected['direct_url'] = None
        self.assertEqual(expected, observed)

    def test_extracting_missing_attributes_include_locations(self):
        self._test_extracting_missing_attributes(include_locations=True)

    def test_extracting_missing_attributes_exclude_locations(self):
        self._test_extracting_missing_attributes(include_locations=False)


class TestExceptionTranslations(test.NoDBTestCase):

    def test_client_forbidden_to_imagenotauthed(self):
        in_exc = glanceclient.exc.Forbidden('123')
        out_exc = glance._translate_image_exception('123', in_exc)
        self.assertIsInstance(out_exc, exception.ImageNotAuthorized)

    def test_client_httpforbidden_converts_to_imagenotauthed(self):
        in_exc = glanceclient.exc.HTTPForbidden('123')
        out_exc = glance._translate_image_exception('123', in_exc)
        self.assertIsInstance(out_exc, exception.ImageNotAuthorized)

    def test_client_notfound_converts_to_imagenotfound(self):
        in_exc = glanceclient.exc.NotFound('123')
        out_exc = glance._translate_image_exception('123', in_exc)
        self.assertIsInstance(out_exc, exception.ImageNotFound)

    def test_client_httpnotfound_converts_to_imagenotfound(self):
        in_exc = glanceclient.exc.HTTPNotFound('123')
        out_exc = glance._translate_image_exception('123', in_exc)
        self.assertIsInstance(out_exc, exception.ImageNotFound)


class TestGlanceSerializer(test.NoDBTestCase):
    def test_serialize(self):
        metadata = {'name': 'image1',
                    'is_public': True,
                    'foo': 'bar',
                    'properties': {
                        'prop1': 'propvalue1',
                        'mappings': [
                            {'virtual': 'aaa',
                             'device': 'bbb'},
                            {'virtual': 'xxx',
                             'device': 'yyy'}],
                        'block_device_mapping': [
                            {'virtual_device': 'fake',
                             'device_name': '/dev/fake'},
                            {'virtual_device': 'ephemeral0',
                             'device_name': '/dev/fake0'}]}}
        # NOTE(tdurakov): Assertion of serialized objects won't work
        # during using of random PYTHONHASHSEED. Assertion of
        # serialized/deserialized object and initial one is enough
        converted = glance._convert_to_string(metadata)
        self.assertEqual(glance._convert_from_string(converted), metadata)


class TestGetImageService(test.NoDBTestCase):
    @mock.patch.object(glance.GlanceClientWrapper, '__init__',
                       return_value=None)
    def test_get_remote_service_from_id(self, gcwi_mocked):
        id_or_uri = '123'
        _ignored, image_id = glance.get_remote_image_service(
                mock.sentinel.ctx, id_or_uri)
        self.assertEqual(id_or_uri, image_id)
        gcwi_mocked.assert_called_once_with()

    @mock.patch.object(glance.GlanceClientWrapper, '__init__',
                       return_value=None)
    def test_get_remote_service_from_href(self, gcwi_mocked):
        id_or_uri = 'http://127.0.0.1/v1/images/123'
        _ignored, image_id = glance.get_remote_image_service(
                mock.sentinel.ctx, id_or_uri)
        self.assertEqual('123', image_id)
        gcwi_mocked.assert_called_once_with(context=mock.sentinel.ctx,
                                            endpoint='http://127.0.0.1')


class TestCreateGlanceClient(test.NoDBTestCase):
    @mock.patch('glanceclient.Client')
    def test_headers_passed_glanceclient(self, init_mock):
        self.flags(auth_strategy='keystone')
        auth_token = 'token'
        ctx = context.RequestContext('fake', 'fake', auth_token=auth_token)

        expected_endpoint = 'http://host4:9295'
        expected_params = {
            'identity_headers': {
                'X-Auth-Token': 'token',
                'X-User-Id': 'fake',
                'X-Roles': '',
                'X-Tenant-Id': 'fake',
                'X-Identity-Status': 'Confirmed'
            }
        }
        glance._glanceclient_from_endpoint(ctx, expected_endpoint)
        init_mock.assert_called_once_with('1', expected_endpoint,
                                          **expected_params)

        # Test the version is properly passed to glanceclient.
        init_mock.reset_mock()

        expected_endpoint = 'http://host4:9295'
        expected_params = {
            'identity_headers': {
                'X-Auth-Token': 'token',
                'X-User-Id': 'fake',
                'X-Roles': '',
                'X-Tenant-Id': 'fake',
                'X-Identity-Status': 'Confirmed'
            }
        }
        glance._glanceclient_from_endpoint(ctx, expected_endpoint, version=2)
        init_mock.assert_called_once_with('2', expected_endpoint,
                                          **expected_params)

        # Test that the IPv6 bracketization adapts the endpoint properly.
        init_mock.reset_mock()

        expected_endpoint = 'http://[host4]:9295'
        glance._glanceclient_from_endpoint(ctx, expected_endpoint)
        init_mock.assert_called_once_with('1', expected_endpoint,
                                          **expected_params)


class TestGlanceClientWrapperRetries(test.NoDBTestCase):

    def setUp(self):
        super(TestGlanceClientWrapperRetries, self).setUp()
        self.ctx = context.RequestContext('fake', 'fake')
        api_servers = [
            'host1:9292',
            'https://host2:9293',
            'http://host3:9294'
        ]
        self.flags(api_servers=api_servers, group='glance')

    def assert_retry_attempted(self, sleep_mock, client, expected_url):
        client.call(self.ctx, 1, 'get', 'meow')
        sleep_mock.assert_called_once_with(1)
        self.assertEqual(str(client.api_server), expected_url)

    def assert_retry_not_attempted(self, sleep_mock, client):
        self.assertRaises(exception.GlanceConnectionFailed,
                client.call, self.ctx, 1, 'get', 'meow')
        self.assertFalse(sleep_mock.called)

    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_static_client_without_retries(self, create_client_mock,
                                           sleep_mock):
        side_effect = glanceclient.exc.ServiceUnavailable
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=0, group='glance')
        client = self._get_static_client(create_client_mock)
        self.assert_retry_not_attempted(sleep_mock, client)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_static_client_with_retries_negative(self, create_client_mock,
                                                 sleep_mock, mock_log):
        side_effect = glanceclient.exc.ServiceUnavailable
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=-1, group='glance')
        client = self._get_static_client(create_client_mock)
        self.assert_retry_not_attempted(sleep_mock, client)

        self.assertTrue(mock_log.warning.called)
        msg = mock_log.warning.call_args_list[0]
        self.assertIn('Treating negative config value', msg[0][0])

    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_static_client_with_retries(self, create_client_mock,
                                        sleep_mock):
        side_effect = [
            glanceclient.exc.ServiceUnavailable,
            None
        ]
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=1, group='glance')
        client = self._get_static_client(create_client_mock)
        self.assert_retry_attempted(sleep_mock, client, 'http://host4:9295')

    @mock.patch('random.shuffle')
    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_default_client_with_retries(self, create_client_mock,
                                         sleep_mock, shuffle_mock):
        side_effect = [
            glanceclient.exc.ServiceUnavailable,
            None
        ]
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=1, group='glance')
        client = glance.GlanceClientWrapper()
        self.assert_retry_attempted(sleep_mock, client, 'https://host2:9293')

    @mock.patch('random.shuffle')
    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_retry_works_with_generators(self, create_client_mock,
                                         sleep_mock, shuffle_mock):
        def some_generator(exception):
            if exception:
                raise glanceclient.exc.ServiceUnavailable('Boom!')
            yield 'something'

        side_effect = [
            some_generator(exception=True),
            some_generator(exception=False),
        ]
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=1, group='glance')
        client = glance.GlanceClientWrapper()
        self.assert_retry_attempted(sleep_mock, client, 'https://host2:9293')

    @mock.patch('random.shuffle')
    @mock.patch('time.sleep')
    @mock.patch('nova.image.glance._glanceclient_from_endpoint')
    def test_default_client_without_retries(self, create_client_mock,
                                            sleep_mock, shuffle_mock):
        side_effect = glanceclient.exc.ServiceUnavailable
        self._mock_client_images_response(create_client_mock, side_effect)
        self.flags(num_retries=0, group='glance')
        client = glance.GlanceClientWrapper()

        # Here we are testing the behaviour that calling client.call() twice
        # when there are no retries will cycle through the api_servers and not
        # sleep (which would be an indication of a retry)

        self.assertRaises(exception.GlanceConnectionFailed,
                client.call, self.ctx, 1, 'get', 'meow')
        self.assertEqual(str(client.api_server), 'http://host1:9292')
        self.assertFalse(sleep_mock.called)

        self.assertRaises(exception.GlanceConnectionFailed,
                client.call, self.ctx, 1, 'get', 'meow')
        self.assertEqual(str(client.api_server), 'https://host2:9293')
        self.assertFalse(sleep_mock.called)

    def _get_static_client(self, create_client_mock):
        version = 1 if CONF.glance.use_glance_v1 else 2
        url = 'http://host4:9295'
        client = glance.GlanceClientWrapper(context=self.ctx, endpoint=url)
        create_client_mock.assert_called_once_with(self.ctx, mock.ANY, version)
        return client

    def _mock_client_images_response(self, create_client_mock, side_effect):
        client_mock = mock.MagicMock(spec=glanceclient.Client)
        images_mock = mock.MagicMock(spec=images.ImageManager)
        images_mock.get.side_effect = side_effect
        type(client_mock).images = mock.PropertyMock(return_value=images_mock)
        create_client_mock.return_value = client_mock


class TestGlanceClientWrapper(test.NoDBTestCase):

    @mock.patch('oslo_service.sslutils.is_enabled')
    @mock.patch('glanceclient.Client')
    def test_create_glance_client_with_ssl(self, client_mock,
                                           ssl_enable_mock):
        self.flags(ca_file='foo.cert', cert_file='bar.cert',
                   key_file='wut.key', group='ssl')
        ctxt = mock.sentinel.ctx
        glance._glanceclient_from_endpoint(ctxt, 'https://host4:9295')
        client_mock.assert_called_once_with(
            '1', 'https://host4:9295', insecure=False, ssl_compression=False,
            cert_file='bar.cert', key_file='wut.key', cacert='foo.cert',
            identity_headers=mock.ANY)


class TestDownloadNoDirectUri(test.NoDBTestCase):

    """Tests the download method of the GlanceImageService when the
    default of not allowing direct URI transfers is set.
    """

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_no_data_no_dest_path_v1(self, show_mock, open_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.image_chunks
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        self.assertEqual(mock.sentinel.image_chunks, res)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_data_no_dest_path_v1(self, show_mock, open_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        data = mock.MagicMock()
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id, data=data)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        self.assertIsNone(res)
        data.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        self.assertFalse(data.close.called)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_no_data_dest_path_v1(self, show_mock, open_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertFalse(show_mock.called)
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        open_mock.assert_called_once_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        writer.close.assert_called_once_with()

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_data_dest_path_v1(self, show_mock, open_mock):
        # NOTE(jaypipes): This really shouldn't be allowed, but because of the
        # horrible design of the download() method in GlanceImageService, no
        # error is raised, and the dst_path is ignored...
        # #TODO(jaypipes): Fix the aforementioned horrible design of
        # the download() method.
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        data = mock.MagicMock()
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id, data=data)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        self.assertIsNone(res)
        data.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        self.assertFalse(data.close.called)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_data_dest_path_write_fails_v1(
            self, show_mock, open_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)

        # NOTE(mikal): data is a file like object, which in our case always
        # raises an exception when we attempt to write to the file.
        class FakeDiskException(Exception):
            pass

        class Exceptionator(StringIO):
            def write(self, _):
                raise FakeDiskException('Disk full!')

        self.assertRaises(FakeDiskException, service.download, ctx,
                          mock.sentinel.image_id, data=Exceptionator())

    @mock.patch('nova.image.glance.GlanceImageService._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_direct_file_uri_v1(self, show_mock, get_tran_mock):
        self.flags(allowed_direct_url_schemes=['file'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        tran_mod = mock.MagicMock()
        get_tran_mock.return_value = tran_mod
        client = mock.MagicMock()
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        self.assertFalse(client.call.called)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        tran_mod.download.assert_called_once_with(ctx, mock.ANY,
                                                  mock.sentinel.dst_path,
                                                  mock.sentinel.loc_meta)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_direct_exception_fallback_v1(
            self, show_mock, get_tran_mock, open_mock):
        # Test that we fall back to downloading to the dst_path
        # if the download method of the transfer module raised
        # an exception.
        self.flags(use_glance_v1=True, group='glance')
        self.flags(allowed_direct_url_schemes=['file'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        tran_mod = mock.MagicMock()
        tran_mod.download.side_effect = Exception
        get_tran_mock.return_value = tran_mod
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        tran_mod.download.assert_called_once_with(ctx, mock.ANY,
                                                  mock.sentinel.dst_path,
                                                  mock.sentinel.loc_meta)
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        # NOTE(jaypipes): log messages call open() in part of the
        # download path, so here, we just check that the last open()
        # call was done for the dst_path file descriptor.
        open_mock.assert_called_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageService._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_direct_no_mod_fallback_v1(
            self, show_mock, get_tran_mock, open_mock):
        # Test that we fall back to downloading to the dst_path
        # if no appropriate transfer module is found...
        # an exception.
        self.flags(use_glance_v1=True, group='glance')
        self.flags(allowed_direct_url_schemes=['funky'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        get_tran_mock.return_value = None
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageService(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        client.call.assert_called_once_with(ctx, 1, 'data',
                                            mock.sentinel.image_id)
        # NOTE(jaypipes): log messages call open() in part of the
        # download path, so here, we just check that the last open()
        # call was done for the dst_path file descriptor.
        open_mock.assert_called_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        writer.close.assert_called_once_with()

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_no_data_no_dest_path_v2(self, show_mock, open_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.image_chunks
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        self.assertEqual(mock.sentinel.image_chunks, res)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_data_no_dest_path_v2(self, show_mock, open_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        data = mock.MagicMock()
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id, data=data)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        self.assertIsNone(res)
        data.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        self.assertFalse(data.close.called)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_no_data_dest_path_v2(self, show_mock, open_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertFalse(show_mock.called)
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        open_mock.assert_called_once_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        writer.close.assert_called_once_with()

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_data_dest_path_v2(self, show_mock, open_mock):
        # NOTE(jaypipes): This really shouldn't be allowed, but because of the
        # horrible design of the download() method in GlanceImageService, no
        # error is raised, and the dst_path is ignored...
        # #TODO(jaypipes): Fix the aforementioned horrible design of
        # the download() method.
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        data = mock.MagicMock()
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id, data=data)

        self.assertFalse(show_mock.called)
        self.assertFalse(open_mock.called)
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        self.assertIsNone(res)
        data.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        self.assertFalse(data.close.called)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_data_dest_path_write_fails_v2(
            self, show_mock, open_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)

        # NOTE(mikal): data is a file like object, which in our case always
        # raises an exception when we attempt to write to the file.
        class FakeDiskException(Exception):
            pass

        class Exceptionator(StringIO):
            def write(self, _):
                raise FakeDiskException('Disk full!')

        self.assertRaises(FakeDiskException, service.download, ctx,
                          mock.sentinel.image_id, data=Exceptionator())

    @mock.patch('nova.image.glance.GlanceImageServiceV2._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_direct_file_uri_v2(self, show_mock, get_tran_mock):
        self.flags(use_glance_v1=False, group='glance')
        self.flags(allowed_direct_url_schemes=['file'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        tran_mod = mock.MagicMock()
        get_tran_mock.return_value = tran_mod
        client = mock.MagicMock()
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        self.assertFalse(client.call.called)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        tran_mod.download.assert_called_once_with(ctx, mock.ANY,
                                                  mock.sentinel.dst_path,
                                                  mock.sentinel.loc_meta)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_direct_exception_fallback_v2(
            self, show_mock, get_tran_mock, open_mock):
        # Test that we fall back to downloading to the dst_path
        # if the download method of the transfer module raised
        # an exception.
        self.flags(use_glance_v1=False, group='glance')
        self.flags(allowed_direct_url_schemes=['file'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        tran_mod = mock.MagicMock()
        tran_mod.download.side_effect = Exception
        get_tran_mock.return_value = tran_mod
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        tran_mod.download.assert_called_once_with(ctx, mock.ANY,
                                                  mock.sentinel.dst_path,
                                                  mock.sentinel.loc_meta)
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        # NOTE(jaypipes): log messages call open() in part of the
        # download path, so here, we just check that the last open()
        # call was done for the dst_path file descriptor.
        open_mock.assert_called_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.GlanceImageServiceV2._get_transfer_module')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_direct_no_mod_fallback(
            self, show_mock, get_tran_mock, open_mock):
        # Test that we fall back to downloading to the dst_path
        # if no appropriate transfer module is found...
        # an exception.
        self.flags(use_glance_v1=False, group='glance')
        self.flags(allowed_direct_url_schemes=['funky'], group='glance')
        show_mock.return_value = {
            'locations': [
                {
                    'url': 'file:///files/image',
                    'metadata': mock.sentinel.loc_meta
                }
            ]
        }
        get_tran_mock.return_value = None
        client = mock.MagicMock()
        client.call.return_value = [1, 2, 3]
        ctx = mock.sentinel.ctx
        writer = mock.MagicMock()
        open_mock.return_value = writer
        service = glance.GlanceImageServiceV2(client)
        res = service.download(ctx, mock.sentinel.image_id,
                               dst_path=mock.sentinel.dst_path)

        self.assertIsNone(res)
        show_mock.assert_called_once_with(ctx,
                                          mock.sentinel.image_id,
                                          include_locations=True)
        get_tran_mock.assert_called_once_with('file')
        client.call.assert_called_once_with(ctx, 2, 'data',
                                            mock.sentinel.image_id)
        # NOTE(jaypipes): log messages call open() in part of the
        # download path, so here, we just check that the last open()
        # call was done for the dst_path file descriptor.
        open_mock.assert_called_with(mock.sentinel.dst_path, 'wb')
        self.assertIsNone(res)
        writer.write.assert_has_calls(
                [
                    mock.call(1),
                    mock.call(2),
                    mock.call(3)
                ]
        )
        writer.close.assert_called_once_with()


class TestDownloadSignatureVerification(test.NoDBTestCase):

    class MockVerifier(object):
        def update(self, data):
            return

        def verify(self):
            return True

    class BadVerifier(object):
        def update(self, data):
            return

        def verify(self):
            raise cryptography.exceptions.InvalidSignature(
                'Invalid signature.'
            )

    def setUp(self):
        super(TestDownloadSignatureVerification, self).setUp()
        self.flags(verify_glance_signatures=True, group='glance')
        self.fake_img_props = {
            'properties': {
                'img_signature': 'signature',
                'img_signature_hash_method': 'SHA-224',
                'img_signature_certificate_uuid': uuids.img_sig_cert_uuid,
                'img_signature_key_type': 'RSA-PSS',
            }
        }
        self.fake_img_data = ['A' * 256, 'B' * 256]
        self.client = mock.MagicMock()
        self.client.call.return_value = self.fake_img_data

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_signature_verification_v1(self,
                                                     mock_get_verifier,
                                                     mock_show,
                                                     mock_log):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_get_verifier.return_value = self.MockVerifier()
        mock_show.return_value = self.fake_img_props
        res = service.download(context=None, image_id=None,
                               data=None, dst_path=None)
        self.assertEqual(self.fake_img_data, res)
        mock_get_verifier.assert_called_once_with(None,
                                                  uuids.img_sig_cert_uuid,
                                                  'SHA-224',
                                                  'signature', 'RSA-PSS')
        mock_log.info.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_dst_path_signature_verification_v1(self,
                                                         mock_get_verifier,
                                                         mock_show,
                                                         mock_log,
                                                         mock_open):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_get_verifier.return_value = self.MockVerifier()
        mock_show.return_value = self.fake_img_props
        mock_dest = mock.MagicMock()
        fake_path = 'FAKE_PATH'
        mock_open.return_value = mock_dest
        service.download(context=None, image_id=None,
                         data=None, dst_path=fake_path)
        mock_get_verifier.assert_called_once_with(None,
                                                  uuids.img_sig_cert_uuid,
                                                  'SHA-224',
                                                  'signature', 'RSA-PSS')
        mock_log.info.assert_called_once_with(mock.ANY, mock.ANY)
        self.assertEqual(len(self.fake_img_data), mock_dest.write.call_count)
        self.assertTrue(mock_dest.close.called)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_get_verifier_failure_v1(self,
                                                   mock_get_verifier,
                                                   mock_show,
                                                   mock_log):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_get_verifier.side_effect = exception.SignatureVerificationError(
                                            reason='Signature verification '
                                                   'failed.'
                                        )
        mock_show.return_value = self.fake_img_props
        self.assertRaises(exception.SignatureVerificationError,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=None)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_invalid_signature_v1(self,
                                                mock_get_verifier,
                                                mock_show,
                                                mock_log):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_get_verifier.return_value = self.BadVerifier()
        mock_show.return_value = self.fake_img_props
        self.assertRaises(cryptography.exceptions.InvalidSignature,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=None)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_missing_signature_metadata_v1(self,
                                                    mock_show,
                                                    mock_log):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_show.return_value = {'properties': {}}
        self.assertRaisesRegex(exception.SignatureVerificationError,
                               'Required image properties for signature '
                               'verification do not exist. Cannot verify '
                               'signature. Missing property: .*',
                               service.download,
                               context=None, image_id=None,
                               data=None, dst_path=None)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.signature_utils.get_verifier')
    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageService.show')
    def test_download_dst_path_signature_fail_v1(self, mock_show,
                                                 mock_log, mock_get_verifier,
                                                 mock_open):
        self.flags(use_glance_v1=True, group='glance')
        service = glance.GlanceImageService(self.client)
        mock_get_verifier.return_value = self.BadVerifier()
        mock_dest = mock.MagicMock()
        fake_path = 'FAKE_PATH'
        mock_open.return_value = mock_dest
        mock_show.return_value = self.fake_img_props
        self.assertRaises(cryptography.exceptions.InvalidSignature,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=fake_path)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)
        mock_open.assert_called_once_with(fake_path, 'wb')
        mock_dest.truncate.assert_called_once_with(0)
        self.assertTrue(mock_dest.close.called)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_signature_verification_v2(self,
                                                     mock_get_verifier,
                                                     mock_show,
                                                     mock_log):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_get_verifier.return_value = self.MockVerifier()
        mock_show.return_value = self.fake_img_props
        res = service.download(context=None, image_id=None,
                               data=None, dst_path=None)
        self.assertEqual(self.fake_img_data, res)
        mock_get_verifier.assert_called_once_with(None,
                                                  uuids.img_sig_cert_uuid,
                                                  'SHA-224',
                                                  'signature', 'RSA-PSS')
        mock_log.info.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_dst_path_signature_verification_v2(self,
                                                         mock_get_verifier,
                                                         mock_show,
                                                         mock_log,
                                                         mock_open):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_get_verifier.return_value = self.MockVerifier()
        mock_show.return_value = self.fake_img_props
        mock_dest = mock.MagicMock()
        fake_path = 'FAKE_PATH'
        mock_open.return_value = mock_dest
        service.download(context=None, image_id=None,
                         data=None, dst_path=fake_path)
        mock_get_verifier.assert_called_once_with(None,
                                                  uuids.img_sig_cert_uuid,
                                                  'SHA-224',
                                                  'signature', 'RSA-PSS')
        mock_log.info.assert_called_once_with(mock.ANY, mock.ANY)
        self.assertEqual(len(self.fake_img_data), mock_dest.write.call_count)
        self.assertTrue(mock_dest.close.called)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_get_verifier_failure_v2(self,
                                                   mock_get_verifier,
                                                   mock_show,
                                                   mock_log):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_get_verifier.side_effect = exception.SignatureVerificationError(
                                            reason='Signature verification '
                                                   'failed.'
                                        )
        mock_show.return_value = self.fake_img_props
        self.assertRaises(exception.SignatureVerificationError,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=None)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.signature_utils.get_verifier')
    def test_download_with_invalid_signature_v2(self,
                                                mock_get_verifier,
                                                mock_show,
                                                mock_log):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_get_verifier.return_value = self.BadVerifier()
        mock_show.return_value = self.fake_img_props
        self.assertRaises(cryptography.exceptions.InvalidSignature,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=None)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)

    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_missing_signature_metadata_v2(self,
                                                    mock_show,
                                                    mock_log):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_show.return_value = {'properties': {}}
        self.assertRaisesRegex(exception.SignatureVerificationError,
                               'Required image properties for signature '
                               'verification do not exist. Cannot verify '
                               'signature. Missing property: .*',
                               service.download,
                               context=None, image_id=None,
                               data=None, dst_path=None)

    @mock.patch.object(six.moves.builtins, 'open')
    @mock.patch('nova.signature_utils.get_verifier')
    @mock.patch('nova.image.glance.LOG')
    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    def test_download_dst_path_signature_fail_v2(self, mock_show,
                                                 mock_log, mock_get_verifier,
                                                 mock_open):
        self.flags(use_glance_v1=False, group='glance')
        service = glance.GlanceImageServiceV2(self.client)
        mock_get_verifier.return_value = self.BadVerifier()
        mock_dest = mock.MagicMock()
        fake_path = 'FAKE_PATH'
        mock_open.return_value = mock_dest
        mock_show.return_value = self.fake_img_props
        self.assertRaises(cryptography.exceptions.InvalidSignature,
                          service.download,
                          context=None, image_id=None,
                          data=None, dst_path=fake_path)
        mock_log.error.assert_called_once_with(mock.ANY, mock.ANY)
        mock_open.assert_called_once_with(fake_path, 'wb')
        mock_dest.truncate.assert_called_once_with(0)
        self.assertTrue(mock_dest.close.called)


class TestIsImageAvailable(test.NoDBTestCase):
    """Tests the internal _is_image_available function."""

    class ImageSpecV2(object):
        visibility = None
        properties = None

    class ImageSpecV1(object):
        is_public = None
        properties = None

    def test_auth_token_override(self):
        ctx = mock.MagicMock(auth_token=True)
        img = mock.MagicMock()

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)
        self.assertFalse(img.called)

    def test_admin_override(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=True)
        img = mock.MagicMock()

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)
        self.assertFalse(img.called)

    def test_v2_visibility(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False)
        # We emulate warlock validation that throws an AttributeError
        # if you try to call is_public on an image model returned by
        # a call to V2 image.get(). Here, the ImageSpecV2 does not have
        # an is_public attribute and MagicMock will throw an AttributeError.
        img = mock.MagicMock(visibility='PUBLIC',
                             spec=TestIsImageAvailable.ImageSpecV2)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

    def test_v1_is_public(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False)
        img = mock.MagicMock(is_public=True,
                             spec=TestIsImageAvailable.ImageSpecV1)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

    def test_project_is_owner(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False,
                             project_id='123')
        props = {
            'owner_id': '123'
        }
        img = mock.MagicMock(visibility='private', properties=props,
                             spec=TestIsImageAvailable.ImageSpecV2)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

        ctx.reset_mock()
        img = mock.MagicMock(is_public=False, properties=props,
                             spec=TestIsImageAvailable.ImageSpecV1)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

    def test_project_context_matches_project_prop(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False,
                             project_id='123')
        props = {
            'project_id': '123'
        }
        img = mock.MagicMock(visibility='private', properties=props,
                             spec=TestIsImageAvailable.ImageSpecV2)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

        ctx.reset_mock()
        img = mock.MagicMock(is_public=False, properties=props,
                             spec=TestIsImageAvailable.ImageSpecV1)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

    def test_no_user_in_props(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False,
                             project_id='123')
        props = {
        }
        img = mock.MagicMock(visibility='private', properties=props,
                             spec=TestIsImageAvailable.ImageSpecV2)

        res = glance._is_image_available(ctx, img)
        self.assertFalse(res)

        ctx.reset_mock()
        img = mock.MagicMock(is_public=False, properties=props,
                             spec=TestIsImageAvailable.ImageSpecV1)

        res = glance._is_image_available(ctx, img)
        self.assertFalse(res)

    def test_user_matches_context(self):
        ctx = mock.MagicMock(auth_token=False, is_admin=False,
                             user_id='123')
        props = {
            'user_id': '123'
        }
        img = mock.MagicMock(visibility='private', properties=props,
                             spec=TestIsImageAvailable.ImageSpecV2)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)

        ctx.reset_mock()
        img = mock.MagicMock(is_public=False, properties=props,
                             spec=TestIsImageAvailable.ImageSpecV1)

        res = glance._is_image_available(ctx, img)
        self.assertTrue(res)


class TestShow(test.NoDBTestCase):

    """Tests the show method of the GlanceImageService."""

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_success_v1(self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        is_avail_mock.return_value = True
        trans_from_mock.return_value = {'mock': mock.sentinel.trans_from}
        client = mock.MagicMock()
        client.call.return_value = {}
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        info = service.show(ctx, mock.sentinel.image_id)

        client.call.assert_called_once_with(ctx, 1, 'get',
                                            mock.sentinel.image_id)
        is_avail_mock.assert_called_once_with(ctx, {})
        trans_from_mock.assert_called_once_with({}, include_locations=False)
        self.assertIn('mock', info)
        self.assertEqual(mock.sentinel.trans_from, info['mock'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_not_available_v1(self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        is_avail_mock.return_value = False
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.images_0
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)

        with testtools.ExpectedException(exception.ImageNotFound):
            service.show(ctx, mock.sentinel.image_id)

        client.call.assert_called_once_with(ctx, 1, 'get',
                                            mock.sentinel.image_id)
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        self.assertFalse(trans_from_mock.called)

    @mock.patch('nova.image.glance._reraise_translated_image_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_client_failure_v1(self, is_avail_mock, trans_from_mock,
                                 reraise_mock):
        self.flags(use_glance_v1=True, group='glance')
        raised = exception.ImageNotAuthorized(image_id=123)
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageService(client)

        with testtools.ExpectedException(exception.ImageNotAuthorized):
            service.show(ctx, mock.sentinel.image_id)
            client.call.assert_called_once_with(ctx, 1, 'get',
                                                mock.sentinel.image_id)
            self.assertFalse(is_avail_mock.called)
            self.assertFalse(trans_from_mock.called)
            reraise_mock.assert_called_once_with(mock.sentinel.image_id)

    @mock.patch('nova.image.glance._is_image_available')
    def test_show_queued_image_without_some_attrs_v1(self, is_avail_mock):
        self.flags(use_glance_v1=True, group='glance')
        is_avail_mock.return_value = True
        client = mock.MagicMock()

        # fake image cls without disk_format, container_format, name attributes
        class fake_image_cls(dict):
            id = 'b31aa5dd-f07a-4748-8f15-398346887584'
            deleted = False
            protected = False
            min_disk = 0
            created_at = '2014-05-20T08:16:48'
            size = 0
            status = 'queued'
            is_public = False
            min_ram = 0
            owner = '980ec4870033453ead65c0470a78b8a8'
            updated_at = '2014-05-20T08:16:48'
        glance_image = fake_image_cls()
        client.call.return_value = glance_image
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        image_info = service.show(ctx, glance_image.id)
        client.call.assert_called_once_with(ctx, 1, 'get',
                                            glance_image.id)
        NOVA_IMAGE_ATTRIBUTES = set(['size', 'disk_format', 'owner',
                                     'container_format', 'status', 'id',
                                     'name', 'created_at', 'updated_at',
                                     'deleted', 'deleted_at', 'checksum',
                                     'min_disk', 'min_ram', 'is_public',
                                     'properties'])

        self.assertEqual(NOVA_IMAGE_ATTRIBUTES, set(image_info.keys()))

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_include_locations_success_v1(self, avail_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        locations = [mock.sentinel.loc1]
        avail_mock.return_value = True
        trans_from_mock.return_value = {'locations': locations}

        client = mock.Mock()
        client.call.return_value = mock.sentinel.image
        service = glance.GlanceImageService(client)
        ctx = mock.sentinel.ctx
        image_id = mock.sentinel.image_id
        info = service.show(ctx, image_id, include_locations=True)

        client.call.assert_called_once_with(ctx, 2, 'get', image_id)
        avail_mock.assert_called_once_with(ctx, mock.sentinel.image)
        trans_from_mock.assert_called_once_with(mock.sentinel.image,
                                                include_locations=True)
        self.assertIn('locations', info)
        self.assertEqual(locations, info['locations'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_include_direct_uri_success_v1(self, avail_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        locations = [mock.sentinel.loc1]
        avail_mock.return_value = True
        trans_from_mock.return_value = {'locations': locations,
                                        'direct_uri': mock.sentinel.duri}

        client = mock.Mock()
        client.call.return_value = mock.sentinel.image
        service = glance.GlanceImageService(client)
        ctx = mock.sentinel.ctx
        image_id = mock.sentinel.image_id
        info = service.show(ctx, image_id, include_locations=True)

        client.call.assert_called_once_with(ctx, 2, 'get', image_id)
        expected = locations
        expected.append({'url': mock.sentinel.duri, 'metadata': {}})
        self.assertIn('locations', info)
        self.assertEqual(expected, info['locations'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_do_not_show_deleted_images_v1(
            self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')

        class fake_image_cls(dict):
            id = 'b31aa5dd-f07a-4748-8f15-398346887584'
            deleted = True

        glance_image = fake_image_cls()
        client = mock.MagicMock()
        client.call.return_value = glance_image
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)

        with testtools.ExpectedException(exception.ImageNotFound):
            service.show(ctx, glance_image.id, show_deleted=False)

        client.call.assert_called_once_with(ctx, 1, 'get',
                                            glance_image.id)
        self.assertFalse(is_avail_mock.called)
        self.assertFalse(trans_from_mock.called)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_success_v2(self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        is_avail_mock.return_value = True
        trans_from_mock.return_value = {'mock': mock.sentinel.trans_from}
        client = mock.MagicMock()
        client.call.return_value = {}
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        info = service.show(ctx, mock.sentinel.image_id)

        client.call.assert_called_once_with(ctx, 2, 'get',
                                            mock.sentinel.image_id)
        is_avail_mock.assert_called_once_with(ctx, {})
        trans_from_mock.assert_called_once_with({}, include_locations=False)
        self.assertIn('mock', info)
        self.assertEqual(mock.sentinel.trans_from, info['mock'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_not_available_v2(self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        is_avail_mock.return_value = False
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.images_0
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)

        with testtools.ExpectedException(exception.ImageNotFound):
            service.show(ctx, mock.sentinel.image_id)

        client.call.assert_called_once_with(ctx, 2, 'get',
                                            mock.sentinel.image_id)
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        self.assertFalse(trans_from_mock.called)

    @mock.patch('nova.image.glance._reraise_translated_image_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_client_failure_v2(self, is_avail_mock, trans_from_mock,
                                 reraise_mock):
        self.flags(use_glance_v1=False, group='glance')
        raised = exception.ImageNotAuthorized(image_id=123)
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageServiceV2(client)

        with testtools.ExpectedException(exception.ImageNotAuthorized):
            service.show(ctx, mock.sentinel.image_id)
            client.call.assert_called_once_with(ctx, 2, 'get',
                                                mock.sentinel.image_id)
            self.assertFalse(is_avail_mock.called)
            self.assertFalse(trans_from_mock.called)
            reraise_mock.assert_called_once_with(mock.sentinel.image_id)

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    @mock.patch('nova.image.glance._is_image_available')
    def test_show_queued_image_without_some_attrs_v2(self, is_avail_mock,
                                                     mocked_schema):
        self.flags(use_glance_v1=False, group='glance')
        is_avail_mock.return_value = True
        client = mock.MagicMock()

        # fake image cls without disk_format, container_format, name attributes
        class fake_image_cls(dict):
            pass

        glance_image = fake_image_cls(
            id = 'b31aa5dd-f07a-4748-8f15-398346887584',
            deleted = False,
            protected = False,
            min_disk = 0,
            created_at = '2014-05-20T08:16:48',
            size = 0,
            status = 'queued',
            visibility = 'private',
            min_ram = 0,
            owner = '980ec4870033453ead65c0470a78b8a8',
            updated_at = '2014-05-20T08:16:48',
            schema = '')
        glance_image.id = glance_image['id']
        glance_image.schema = ''
        client.call.return_value = glance_image
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        image_info = service.show(ctx, glance_image.id)
        client.call.assert_called_once_with(ctx, 2, 'get',
                                            glance_image.id)
        NOVA_IMAGE_ATTRIBUTES = set(['size', 'disk_format', 'owner',
                                     'container_format', 'status', 'id',
                                     'name', 'created_at', 'updated_at',
                                     'deleted', 'deleted_at', 'checksum',
                                     'min_disk', 'min_ram', 'is_public',
                                     'properties'])

        self.assertEqual(NOVA_IMAGE_ATTRIBUTES, set(image_info.keys()))

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_include_locations_success_v2(self, avail_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        locations = [mock.sentinel.loc1]
        avail_mock.return_value = True
        trans_from_mock.return_value = {'locations': locations}

        client = mock.Mock()
        client.call.return_value = mock.sentinel.image
        service = glance.GlanceImageServiceV2(client)
        ctx = mock.sentinel.ctx
        image_id = mock.sentinel.image_id
        info = service.show(ctx, image_id, include_locations=True)

        client.call.assert_called_once_with(ctx, 2, 'get', image_id)
        avail_mock.assert_called_once_with(ctx, mock.sentinel.image)
        trans_from_mock.assert_called_once_with(mock.sentinel.image,
                                                include_locations=True)
        self.assertIn('locations', info)
        self.assertEqual(locations, info['locations'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_include_direct_uri_success_v2(self, avail_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        locations = [mock.sentinel.loc1]
        avail_mock.return_value = True
        trans_from_mock.return_value = {'locations': locations,
                                        'direct_uri': mock.sentinel.duri}

        client = mock.Mock()
        client.call.return_value = mock.sentinel.image
        service = glance.GlanceImageServiceV2(client)
        ctx = mock.sentinel.ctx
        image_id = mock.sentinel.image_id
        info = service.show(ctx, image_id, include_locations=True)

        client.call.assert_called_once_with(ctx, 2, 'get', image_id)
        expected = locations
        expected.append({'url': mock.sentinel.duri, 'metadata': {}})
        self.assertIn('locations', info)
        self.assertEqual(expected, info['locations'])

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_do_not_show_deleted_images_v2(
            self, is_avail_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')

        class fake_image_cls(dict):
            id = 'b31aa5dd-f07a-4748-8f15-398346887584'
            deleted = True

        glance_image = fake_image_cls()
        client = mock.MagicMock()
        client.call.return_value = glance_image
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)

        with testtools.ExpectedException(exception.ImageNotFound):
            service.show(ctx, glance_image.id, show_deleted=False)

        client.call.assert_called_once_with(ctx, 2, 'get',
                                            glance_image.id)
        self.assertFalse(is_avail_mock.called)
        self.assertFalse(trans_from_mock.called)


class TestDetail(test.NoDBTestCase):

    """Tests the detail method of the GlanceImageService."""

    @mock.patch('nova.image.glance._extract_query_params')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_success_available_v1(self, is_avail_mock, trans_from_mock,
                                         ext_query_mock):
        self.flags(use_glance_v1=True, group='glance')
        params = {}
        is_avail_mock.return_value = True
        ext_query_mock.return_value = params
        trans_from_mock.return_value = mock.sentinel.trans_from
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        images = service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 1, 'list')
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        trans_from_mock.assert_called_once_with(mock.sentinel.images_0)
        self.assertEqual([mock.sentinel.trans_from], images)

    @mock.patch('nova.image.glance._extract_query_params')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_success_unavailable_v1(
            self, is_avail_mock, trans_from_mock, ext_query_mock):
        self.flags(use_glance_v1=True, group='glance')
        params = {}
        is_avail_mock.return_value = False
        ext_query_mock.return_value = params
        trans_from_mock.return_value = mock.sentinel.trans_from
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        images = service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 1, 'list')
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        self.assertFalse(trans_from_mock.called)
        self.assertEqual([], images)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_params_passed_v1(self, is_avail_mock, _trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        service.detail(ctx, page_size=5, limit=10)

        expected_filters = {
            'is_public': 'none'
        }
        client.call.assert_called_once_with(ctx, 1, 'list',
                                            filters=expected_filters,
                                            page_size=5,
                                            limit=10)

    @mock.patch('nova.image.glance._reraise_translated_exception')
    @mock.patch('nova.image.glance._extract_query_params')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_client_failure_v1(self, is_avail_mock, trans_from_mock,
                                      ext_query_mock, reraise_mock):
        self.flags(use_glance_v1=True, group='glance')
        params = {}
        ext_query_mock.return_value = params
        raised = exception.Forbidden()
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageService(client)

        with testtools.ExpectedException(exception.Forbidden):
            service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 1, 'list')
        self.assertFalse(is_avail_mock.called)
        self.assertFalse(trans_from_mock.called)
        reraise_mock.assert_called_once_with()

    @mock.patch('nova.image.glance._extract_query_params_v2')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_success_available_v2(self, is_avail_mock, trans_from_mock,
                                         ext_query_mock):
        self.flags(use_glance_v1=False, group='glance')
        params = {}
        is_avail_mock.return_value = True
        ext_query_mock.return_value = params
        trans_from_mock.return_value = mock.sentinel.trans_from
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        images = service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 2, 'list')
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        trans_from_mock.assert_called_once_with(mock.sentinel.images_0)
        self.assertEqual([mock.sentinel.trans_from], images)

    @mock.patch('nova.image.glance._extract_query_params_v2')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_success_unavailable_v2(
            self, is_avail_mock, trans_from_mock, ext_query_mock):
        self.flags(use_glance_v1=False, group='glance')
        params = {}
        is_avail_mock.return_value = False
        ext_query_mock.return_value = params
        trans_from_mock.return_value = mock.sentinel.trans_from
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        images = service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 2, 'list')
        is_avail_mock.assert_called_once_with(ctx, mock.sentinel.images_0)
        self.assertFalse(trans_from_mock.called)
        self.assertEqual([], images)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_params_passed_v2(self, is_avail_mock, _trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        service.detail(ctx, page_size=5, limit=10)

        client.call.assert_called_once_with(ctx, 2, 'list',
                                            filters={},
                                            page_size=5,
                                            limit=10)

    @mock.patch('nova.image.glance._reraise_translated_exception')
    @mock.patch('nova.image.glance._extract_query_params_v2')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_client_failure_v2(self, is_avail_mock, trans_from_mock,
                                      ext_query_mock, reraise_mock):
        self.flags(use_glance_v1=False, group='glance')
        params = {}
        ext_query_mock.return_value = params
        raised = exception.Forbidden()
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageServiceV2(client)

        with testtools.ExpectedException(exception.Forbidden):
            service.detail(ctx, **params)

        client.call.assert_called_once_with(ctx, 2, 'list')
        self.assertFalse(is_avail_mock.called)
        self.assertFalse(trans_from_mock.called)
        reraise_mock.assert_called_once_with()


class TestCreate(test.NoDBTestCase):

    """Tests the create method of the GlanceImageService."""

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_create_success_v1(self, trans_to_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        translated = {
            'image_id': mock.sentinel.image_id
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.image_meta
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        image_meta = service.create(ctx, image_mock)

        trans_to_mock.assert_called_once_with(image_mock,)
        client.call.assert_called_once_with(ctx, 1, 'create',
                                            image_id=mock.sentinel.image_id)
        trans_from_mock.assert_called_once_with(mock.sentinel.image_meta)

        self.assertEqual(mock.sentinel.trans_from, image_meta)

        # Now verify that if we supply image data to the call,
        # that the client is also called with the data kwarg
        client.reset_mock()
        service.create(ctx, image_mock, data=mock.sentinel.data)

        client.call.assert_called_once_with(ctx, 1, 'create',
                                            image_id=mock.sentinel.image_id,
                                            data=mock.sentinel.data)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_create_success_v2(
            self, trans_to_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        translated = {
            'name': mock.sentinel.name,
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        client = mock.MagicMock()
        client.call.return_value = {'id': '123'}
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        image_meta = service.create(ctx, image_mock)
        trans_to_mock.assert_called_once_with(image_mock)
        # Verify that the 'id' element has been removed as a kwarg to
        # the call to glanceclient's update (since the image ID is
        # supplied as a positional arg), and that the
        # purge_props default is True.
        client.call.assert_called_once_with(ctx, 2, 'create',
                                            name=mock.sentinel.name)
        trans_from_mock.assert_called_once_with({'id': '123'})
        self.assertEqual(mock.sentinel.trans_from, image_meta)

        # Now verify that if we supply image data to the call,
        # that the client is also called with the data kwarg
        client.reset_mock()
        client.call.return_value = {'id': mock.sentinel.image_id}
        service.create(ctx, {}, data=mock.sentinel.data)

        self.assertEqual(3, client.call.call_count)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_create_success_v2_with_location(
            self, trans_to_mock, trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        translated = {
            'id': mock.sentinel.id,
            'name': mock.sentinel.name,
            'location': mock.sentinel.location
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        client = mock.MagicMock()
        client.call.return_value = translated
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        image_meta = service.create(ctx, image_mock)
        trans_to_mock.assert_called_once_with(image_mock)
        self.assertEqual(2, client.call.call_count)
        trans_from_mock.assert_called_once_with(translated)
        self.assertEqual(mock.sentinel.trans_from, image_meta)

    @mock.patch('nova.image.glance._reraise_translated_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_create_client_failure_v1(self, trans_to_mock, trans_from_mock,
                                   reraise_mock):
        self.flags(use_glance_v1=True, group='glance')
        translated = {}
        trans_to_mock.return_value = translated
        image_mock = mock.MagicMock(spec=dict)
        raised = exception.Invalid()
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.BadRequest
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageService(client)

        self.assertRaises(exception.Invalid, service.create, ctx, image_mock)
        trans_to_mock.assert_called_once_with(image_mock)
        self.assertFalse(trans_from_mock.called)

    @mock.patch('nova.image.glance._reraise_translated_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_create_client_failure_v2(self, trans_to_mock, trans_from_mock,
                                   reraise_mock):
        self.flags(use_glance_v1=False, group='glance')
        translated = {}
        trans_to_mock.return_value = translated
        image_mock = mock.MagicMock(spec=dict)
        raised = exception.Invalid()
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.BadRequest
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageServiceV2(client)

        self.assertRaises(exception.Invalid, service.create, ctx, image_mock)
        trans_to_mock.assert_called_once_with(image_mock)
        self.assertFalse(trans_from_mock.called)


class TestUpdate(test.NoDBTestCase):

    """Tests the update method of the GlanceImageService."""

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_update_success_v1(
            self, trans_to_mock, trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        translated = {
            'id': mock.sentinel.image_id,
            'name': mock.sentinel.name
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.image_meta
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        image_meta = service.update(
                ctx, mock.sentinel.image_id, image_mock, purge_props=True)

        trans_to_mock.assert_called_once_with(image_mock)
        # Verify that the 'id' element has been removed as a kwarg to
        # the call to glanceclient's update (since the image ID is
        # supplied as a positional arg), and that the
        # purge_props default is True.
        client.call.assert_called_once_with(ctx, 1, 'update',
                                            mock.sentinel.image_id,
                                            name=mock.sentinel.name,
                                            purge_props=True)
        trans_from_mock.assert_called_once_with(mock.sentinel.image_meta)
        self.assertEqual(mock.sentinel.trans_from, image_meta)

        # Now verify that if we supply image data to the call,
        # that the client is also called with the data kwarg
        client.reset_mock()
        service.update(ctx, mock.sentinel.image_id,
                       image_mock, data=mock.sentinel.data)

        client.call.assert_called_once_with(ctx, 1, 'update',
                                            mock.sentinel.image_id,
                                            name=mock.sentinel.name,
                                            purge_props=True,
                                            data=mock.sentinel.data)

    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_update_success_v2(
            self, trans_to_mock, trans_from_mock, show_mock):
        self.flags(use_glance_v1=False, group='glance')
        image = {
            'id': mock.sentinel.image_id,
            'name': mock.sentinel.name,
            'properties': {'prop_to_keep': '4'}
        }

        translated = {
            'id': mock.sentinel.image_id,
            'name': mock.sentinel.name,
            'prop_to_keep': '4'
        }

        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        client = mock.MagicMock()
        client.call.return_value = mock.sentinel.image_meta
        ctx = mock.sentinel.ctx
        show_mock.return_value = {
            'image_id': mock.sentinel.image_id,
            'properties': {'prop_to_remove': '1',
                           'prop_to_keep': '3'}
        }
        service = glance.GlanceImageServiceV2(client)
        image_meta = service.update(
                ctx, mock.sentinel.image_id, image, purge_props=True)
        show_mock.assert_called_once_with(
                mock.sentinel.ctx, mock.sentinel.image_id)
        trans_to_mock.assert_called_once_with(image)
        # Verify that the 'id' element has been removed as a kwarg to
        # the call to glanceclient's update (since the image ID is
        # supplied as a positional arg), and that the
        # purge_props default is True.
        client.call.assert_called_once_with(ctx, 2, 'update',
                                            image_id=mock.sentinel.image_id,
                                            name=mock.sentinel.name,
                                            prop_to_keep='4',
                                            remove_props=['prop_to_remove'])
        trans_from_mock.assert_called_once_with(mock.sentinel.image_meta)
        self.assertEqual(mock.sentinel.trans_from, image_meta)

        # Now verify that if we supply image data to the call,
        # that the client is also called with the data kwarg
        client.reset_mock()
        client.call.return_value = {'id': mock.sentinel.image_id}
        service.update(ctx, mock.sentinel.image_id, {},
                       data=mock.sentinel.data)

        self.assertEqual(3, client.call.call_count)

    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_update_success_v2_with_location(
            self, trans_to_mock, trans_from_mock, show_mock):
        self.flags(use_glance_v1=False, group='glance')
        translated = {
            'id': mock.sentinel.id,
            'name': mock.sentinel.name,
            'location': mock.sentinel.location
        }
        show_mock.return_value = {'image_id': mock.sentinel.image_id}
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        client = mock.MagicMock()
        client.call.return_value = translated
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        image_meta = service.update(ctx, mock.sentinel.image_id,
                                    image_mock, purge_props=False)
        trans_to_mock.assert_called_once_with(image_mock)
        self.assertEqual(2, client.call.call_count)
        trans_from_mock.assert_called_once_with(translated)
        self.assertEqual(mock.sentinel.trans_from, image_meta)

    @mock.patch('nova.image.glance._reraise_translated_image_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_update_client_failure_v1(self, trans_to_mock, trans_from_mock,
                                   reraise_mock):
        self.flags(use_glance_v1=True, group='glance')
        translated = {
            'name': mock.sentinel.name
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        image_mock = mock.MagicMock(spec=dict)
        raised = exception.ImageNotAuthorized(image_id=123)
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        service = glance.GlanceImageService(client)

        self.assertRaises(exception.ImageNotAuthorized,
                          service.update, ctx, mock.sentinel.image_id,
                          image_mock)
        client.call.assert_called_once_with(ctx, 1, 'update',
                                            mock.sentinel.image_id,
                                            purge_props=True,
                                            name=mock.sentinel.name)
        self.assertFalse(trans_from_mock.called)
        reraise_mock.assert_called_once_with(mock.sentinel.image_id)

    @mock.patch('nova.image.glance.GlanceImageServiceV2.show')
    @mock.patch('nova.image.glance._reraise_translated_image_exception')
    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._translate_to_glance')
    def test_update_client_failure_v2(self, trans_to_mock, trans_from_mock,
                                   reraise_mock, show_mock):
        self.flags(use_glance_v1=False, group='glance')
        image = {
            'id': mock.sentinel.image_id,
            'name': mock.sentinel.name,
            'properties': {'prop_to_keep': '4'}
        }

        translated = {
            'id': mock.sentinel.image_id,
            'name': mock.sentinel.name,
            'prop_to_keep': '4'
        }
        trans_to_mock.return_value = translated
        trans_from_mock.return_value = mock.sentinel.trans_from
        raised = exception.ImageNotAuthorized(image_id=123)
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.Forbidden
        ctx = mock.sentinel.ctx
        reraise_mock.side_effect = raised
        show_mock.return_value = {
            'image_id': mock.sentinel.image_id,
            'properties': {'prop_to_remove': '1',
                           'prop_to_keep': '3'}
        }
        service = glance.GlanceImageServiceV2(client)

        self.assertRaises(exception.ImageNotAuthorized,
                          service.update, ctx, mock.sentinel.image_id,
                          image)
        client.call.assert_called_once_with(ctx, 2, 'update',
                                            image_id=mock.sentinel.image_id,
                                            name=mock.sentinel.name,
                                            prop_to_keep='4',
                                            remove_props=['prop_to_remove'])
        reraise_mock.assert_called_once_with(mock.sentinel.image_id)


class TestDelete(test.NoDBTestCase):

    """Tests the delete method of the GlanceImageService."""

    def test_delete_success_v1(self):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = True
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        service.delete(ctx, mock.sentinel.image_id)
        client.call.assert_called_once_with(ctx, 1, 'delete',
                                            mock.sentinel.image_id)

    def test_delete_client_failure_v1(self):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.NotFound
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        self.assertRaises(exception.ImageNotFound, service.delete, ctx,
                          mock.sentinel.image_id)

    def test_delete_success_v2(self):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = True
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        service.delete(ctx, mock.sentinel.image_id)
        client.call.assert_called_once_with(ctx, 2, 'delete',
                                            mock.sentinel.image_id)

    def test_delete_client_failure_v2(self):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.side_effect = glanceclient.exc.NotFound
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        self.assertRaises(exception.ImageNotFound, service.delete, ctx,
                          mock.sentinel.image_id)


class TestGlanceApiServers(test.NoDBTestCase):

    def test_get_api_servers(self):
        glance_servers = ['10.0.1.1:9292',
                          'https://10.0.0.1:9293',
                          'http://10.0.2.2:9294']
        expected_servers = ['http://10.0.1.1:9292',
                          'https://10.0.0.1:9293',
                          'http://10.0.2.2:9294']
        self.flags(api_servers=glance_servers, group='glance')
        api_servers = glance.get_api_servers()
        i = 0
        for server in api_servers:
            i += 1
            self.assertIn(server, expected_servers)
            if i > 2:
                break


class TestUpdateGlanceImage(test.NoDBTestCase):
    @mock.patch('nova.image.glance.GlanceImageService')
    def test_start(self, mock_glance_image_service):
        consumer = glance.UpdateGlanceImage(
            'context', 'id', 'metadata', 'stream')

        with mock.patch.object(glance, 'get_remote_image_service') as a_mock:
            a_mock.return_value = (mock_glance_image_service, 'image_id')

            consumer.start()
            mock_glance_image_service.update.assert_called_with(
                'context', 'image_id', 'metadata', 'stream', purge_props=False)


class TestExtractAttributes(test.NoDBTestCase):
    """Test that image output translations from v1 and v2 are the same"""

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    def test_extract_image_attributes_active_images_no_locations(
            self, mocked_schema):
        image_v1_dict = image_fixtures['active_image_v1']
        image_v2 = ImageV2(image_fixtures['active_image_v2'])

        image_v1 = collections.namedtuple('_', image_v1_dict.keys())(
            **image_v1_dict)

        self.flags(use_glance_v1=True, group='glance')
        v1_output = glance._translate_from_glance(
            image_v1, include_locations=False)
        self.flags(use_glance_v1=False, group='glance')
        v2_output = glance._translate_from_glance(
            image_v2, include_locations=False)
        self.assertEqual(v1_output, v2_output)

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    def test_extract_image_attributes_active_images_with_locations(
            self, mocked_schema):
        # Glance API v1 doesn't provide info about locations
        self.flags(use_glance_v1=False, group='glance')
        image_v2 = ImageV2(image_fixtures['active_image_v2'])

        image_v2_meta = glance._translate_from_glance(
            image_v2, include_locations=True)

        self.assertIn('locations', image_v2_meta)
        self.assertIn('direct_url', image_v2_meta)

        image_v2_meta = glance._translate_from_glance(
            image_v2, include_locations=False)

        self.assertNotIn('locations', image_v2_meta)
        self.assertNotIn('direct_url', image_v2_meta)

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    def test_extract_image_attributes_empty_images(self, mocked_schema):
        image_v1_dict = image_fixtures['empty_image_v1']
        image_v2 = ImageV2(image_fixtures['empty_image_v2'])

        image_v1 = collections.namedtuple('_', image_v1_dict.keys())(
            **image_v1_dict)

        self.flags(use_glance_v1=True, group='glance')
        v1_output = glance._translate_from_glance(
            image_v1, include_locations=False)
        self.flags(use_glance_v1=False, group='glance')
        v2_output = glance._translate_from_glance(
            image_v2, include_locations=False)
        self.assertEqual(v1_output, v2_output)

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    def test_extract_image_attributes_empty_images_no_size(self,
                                                           mocked_schema):
        image_v1_dict = dict(image_fixtures['empty_image_v1'])
        # pop the size attribute since it might not be set on a snapshot image
        image_v1_dict.pop('size')
        image_v2 = ImageV2(image_fixtures['empty_image_v2'])

        image_v1 = collections.namedtuple('_', image_v1_dict.keys())(
            **image_v1_dict)

        self.flags(use_glance_v1=True, group='glance')
        v1_output = glance._translate_from_glance(
            image_v1, include_locations=False)
        self.flags(use_glance_v1=False, group='glance')
        v2_output = glance._translate_from_glance(
            image_v2, include_locations=False)
        self.assertEqual(v1_output, v2_output)

    @mock.patch.object(schemas, 'Schema', side_effect=FakeSchema)
    def test_extract_image_attributes_active_images_custom_prop(
            self, mocked_schema):
        image_v1_dict = image_fixtures['custom_property_image_v1']
        image_v2 = ImageV2(image_fixtures['custom_property_image_v2'])

        image_v1 = collections.namedtuple('_', image_v1_dict.keys())(
            **image_v1_dict)

        self.flags(use_glance_v1=True, group='glance')
        v1_output = glance._translate_from_glance(
            image_v1, include_locations=False)
        self.flags(use_glance_v1=False, group='glance')
        v2_output = glance._translate_from_glance(
            image_v2, include_locations=False)
        self.assertEqual(v1_output, v2_output)


class TestExtractQueryParams(test.NoDBTestCase):
    """Test that list in v1 and v2 can work with the same query parameters"""

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_extract_query_params_v1(
            self, is_avail_mock, _trans_from_mock):
        self.flags(use_glance_v1=True, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageService(client)
        input_filters = {
            'property-kernel-id': 'some-id',
            'changes-since': 'some-date',
            'is_public': 'true',
            'name': 'some-name'
        }

        service.detail(ctx, filters=input_filters, page_size=5, limit=10)

        expected_filters_v1 = {
            'property-kernel-id': 'some-id',
            'name': 'some-name',
            'is_public': 'true',
            'changes-since': 'some-date'}

        client.call.assert_called_once_with(ctx, 1, 'list',
                                            filters=expected_filters_v1,
                                            page_size=5,
                                            limit=10)

    @mock.patch('nova.image.glance._translate_from_glance')
    @mock.patch('nova.image.glance._is_image_available')
    def test_detail_extract_query_params_v2(
            self, is_avail_mock, _trans_from_mock):
        self.flags(use_glance_v1=False, group='glance')
        client = mock.MagicMock()
        client.call.return_value = [mock.sentinel.images_0]
        ctx = mock.sentinel.ctx
        service = glance.GlanceImageServiceV2(client)
        input_filters = {
            'property-kernel-id': 'some-id',
            'changes-since': 'some-date',
            'is_public': 'true',
            'name': 'some-name'
        }

        service.detail(ctx, filters=input_filters, page_size=5, limit=10)

        expected_filters_v1 = {'visibility': 'public',
                               'name': 'some-name',
                               'kernel-id': 'some-id',
                               'updated_at': 'gte:some-date'}

        client.call.assert_called_once_with(ctx, 2, 'list',
                                            filters=expected_filters_v1,
                                            page_size=5,
                                            limit=10)


class TestTranslateToGlance(test.NoDBTestCase):
    """Test that image was translated correct to be accepted by Glance"""

    def setUp(self):
        self.fixture = {
            'checksum': 'fb10c6486390bec8414be90a93dfff3b',
            'container_format': 'bare',
            'created_at': "",
            'deleted': False,
            'deleted_at': None,
            'disk_format': 'raw',
            'id': 'f8116538-309f-449c-8d49-df252a97a48d',
            'is_public': True,
            'min_disk': '0',
            'min_ram': '0',
            'name': 'tempest-image-1294122904',
            'owner': 'd76b51cf8a44427ea404046f4c1d82ab',
            'properties':
                {'os_distro': 'value2', 'os_version': 'value1',
                 'base_image_ref': 'ea36315c-e527-4643-a46a-9fd61d027cc1',
                 'image_type': 'test',
                 'instance_uuid': 'ec1ea9c7-8c5e-498d-a753-6ccc2464123c',
                 'kernel_id': 'None',
                 'ramdisk_id': '  ',
                 'user_id': 'ca2ff78fd33042ceb45fbbe19012ef3f',
                 'boolean_prop': True},
            'size': 1024,
            'status': 'active',
            'updated_at': ""}
        super(TestTranslateToGlance, self).setUp()

    def test_convert_to_v1(self):
        self.flags(use_glance_v1=True, group='glance')
        expected_v1_image = {
            'checksum': 'fb10c6486390bec8414be90a93dfff3b',
            'container_format': 'bare',
            'deleted': False,
            'disk_format': 'raw',
            'id': 'f8116538-309f-449c-8d49-df252a97a48d',
            'is_public': True,
            'min_disk': '0',
            'min_ram': '0',
            'name': 'tempest-image-1294122904',
            'owner': 'd76b51cf8a44427ea404046f4c1d82ab',
            'properties': {
                'base_image_ref': 'ea36315c-e527-4643-a46a-9fd61d027cc1',
                'image_type': 'test',
                'instance_uuid': 'ec1ea9c7-8c5e-498d-a753-6ccc2464123c',
                'kernel_id': 'None',
                'os_distro': 'value2',
                'os_version': 'value1',
                'ramdisk_id': '  ',
                'user_id': 'ca2ff78fd33042ceb45fbbe19012ef3f',
                'boolean_prop': True},
            'size': 1024}
        nova_image_dict = self.fixture
        image_v1_dict = glance._translate_to_glance(nova_image_dict)
        self.assertEqual(expected_v1_image, image_v1_dict)

    def test_convert_to_v2(self):
        expected_v2_image = {
            'base_image_ref': 'ea36315c-e527-4643-a46a-9fd61d027cc1',
            'boolean_prop': 'True',
            'checksum': 'fb10c6486390bec8414be90a93dfff3b',
            'container_format': 'bare',
            'disk_format': 'raw',
            'id': 'f8116538-309f-449c-8d49-df252a97a48d',
            'image_type': 'test',
            'instance_uuid': 'ec1ea9c7-8c5e-498d-a753-6ccc2464123c',
            'min_disk': 0,
            'min_ram': 0,
            'name': 'tempest-image-1294122904',
            'os_distro': 'value2',
            'os_version': 'value1',
            'owner': 'd76b51cf8a44427ea404046f4c1d82ab',
            'user_id': 'ca2ff78fd33042ceb45fbbe19012ef3f',
            'visibility': 'public'}
        nova_image_dict = self.fixture
        image_v2_dict = glance._translate_to_glance(nova_image_dict)
        self.assertEqual(expected_v2_image, image_v2_dict)
