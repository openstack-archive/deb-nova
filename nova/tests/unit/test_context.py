#    Copyright 2011 OpenStack Foundation
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
from oslo_context import context as o_context
from oslo_context import fixture as o_fixture

from nova import context
from nova import exception
from nova import objects
from nova import test


class ContextTestCase(test.NoDBTestCase):

    def setUp(self):
        super(ContextTestCase, self).setUp()
        self.useFixture(o_fixture.ClearRequestContext())

    def test_request_context_elevated(self):
        user_ctxt = context.RequestContext('111',
                                           '222',
                                           is_admin=False)
        self.assertFalse(user_ctxt.is_admin)
        admin_ctxt = user_ctxt.elevated()
        self.assertTrue(admin_ctxt.is_admin)
        self.assertIn('admin', admin_ctxt.roles)
        self.assertFalse(user_ctxt.is_admin)
        self.assertNotIn('admin', user_ctxt.roles)

    def test_request_context_sets_is_admin(self):
        ctxt = context.RequestContext('111',
                                      '222',
                                      roles=['admin', 'weasel'])
        self.assertTrue(ctxt.is_admin)

    def test_request_context_sets_is_admin_by_role(self):
        ctxt = context.RequestContext('111',
                                      '222',
                                      roles=['administrator'])
        self.assertTrue(ctxt.is_admin)

    def test_request_context_sets_is_admin_upcase(self):
        ctxt = context.RequestContext('111',
                                      '222',
                                      roles=['Admin', 'weasel'])
        self.assertTrue(ctxt.is_admin)

    def test_request_context_read_deleted(self):
        ctxt = context.RequestContext('111',
                                      '222',
                                      read_deleted='yes')
        self.assertEqual('yes', ctxt.read_deleted)

        ctxt.read_deleted = 'no'
        self.assertEqual('no', ctxt.read_deleted)

    def test_request_context_read_deleted_invalid(self):
        self.assertRaises(ValueError,
                          context.RequestContext,
                          '111',
                          '222',
                          read_deleted=True)

        ctxt = context.RequestContext('111', '222')
        self.assertRaises(ValueError,
                          setattr,
                          ctxt,
                          'read_deleted',
                          True)

    def test_service_catalog_default(self):
        ctxt = context.RequestContext('111', '222')
        self.assertEqual([], ctxt.service_catalog)

        ctxt = context.RequestContext('111', '222',
                service_catalog=[])
        self.assertEqual([], ctxt.service_catalog)

        ctxt = context.RequestContext('111', '222',
                service_catalog=None)
        self.assertEqual([], ctxt.service_catalog)

    def test_service_catalog_cinder_only(self):
        service_catalog = [
                {u'type': u'compute', u'name': u'nova'},
                {u'type': u's3', u'name': u's3'},
                {u'type': u'image', u'name': u'glance'},
                {u'type': u'volume', u'name': u'cinder'},
                {u'type': u'ec2', u'name': u'ec2'},
                {u'type': u'object-store', u'name': u'swift'},
                {u'type': u'identity', u'name': u'keystone'},
                {u'type': None, u'name': u'S_withouttype'},
                {u'type': u'vo', u'name': u'S_partofvolume'}]

        volume_catalog = [{u'type': u'volume', u'name': u'cinder'}]
        ctxt = context.RequestContext('111', '222',
                service_catalog=service_catalog)
        self.assertEqual(volume_catalog, ctxt.service_catalog)

    def test_to_dict_from_dict_no_log(self):
        warns = []

        def stub_warn(msg, *a, **kw):
            if (a and len(a) == 1 and isinstance(a[0], dict) and a[0]):
                a = a[0]
            warns.append(str(msg) % a)

        self.stub_out('nova.context.LOG.warning', stub_warn)

        ctxt = context.RequestContext('111',
                                      '222',
                                      roles=['admin', 'weasel'])

        context.RequestContext.from_dict(ctxt.to_dict())

        self.assertEqual(0, len(warns), warns)

    def test_store_when_no_overwrite(self):
        # If no context exists we store one even if overwrite is false
        # (since we are not overwriting anything).
        ctx = context.RequestContext('111',
                                      '222',
                                      overwrite=False)
        self.assertIs(o_context.get_current(), ctx)

    def test_no_overwrite(self):
        # If there is already a context in the cache a new one will
        # not overwrite it if overwrite=False.
        ctx1 = context.RequestContext('111',
                                      '222',
                                      overwrite=True)
        context.RequestContext('333',
                               '444',
                               overwrite=False)
        self.assertIs(o_context.get_current(), ctx1)

    def test_admin_no_overwrite(self):
        # If there is already a context in the cache creating an admin
        # context will not overwrite it.
        ctx1 = context.RequestContext('111',
                                      '222',
                                      overwrite=True)
        context.get_admin_context()
        self.assertIs(o_context.get_current(), ctx1)

    def test_convert_from_rc_to_dict(self):
        ctx = context.RequestContext(
            111, 222, request_id='req-679033b7-1755-4929-bf85-eb3bfaef7e0b',
            timestamp='2015-03-02T22:31:56.641629')
        values2 = ctx.to_dict()
        expected_values = {'auth_token': None,
                           'domain': None,
                           'instance_lock_checked': False,
                           'is_admin': False,
                           'is_admin_project': True,
                           'project_id': 222,
                           'project_domain': None,
                           'project_name': None,
                           'quota_class': None,
                           'read_deleted': 'no',
                           'read_only': False,
                           'remote_address': None,
                           'request_id':
                               'req-679033b7-1755-4929-bf85-eb3bfaef7e0b',
                           'resource_uuid': None,
                           'roles': [],
                           'service_catalog': [],
                           'show_deleted': False,
                           'tenant': 222,
                           'timestamp': '2015-03-02T22:31:56.641629',
                           'user': 111,
                           'user_domain': None,
                           'user_id': 111,
                           'user_identity': '111 222 - - -',
                           'user_name': None}
        self.assertEqual(expected_values, values2)

    def test_convert_from_dict_to_dict_version_2_4_x(self):
        # fake dict() created with oslo.context 2.4.x, Missing is_admin_project
        # key
        values = {'user': '111',
                  'user_id': '111',
                  'tenant': '222',
                  'project_id': '222',
                  'domain': None, 'project_domain': None,
                  'auth_token': None,
                  'resource_uuid': None, 'read_only': False,
                  'user_identity': '111 222 - - -',
                  'instance_lock_checked': False,
                  'user_name': None, 'project_name': None,
                  'timestamp': '2015-03-02T20:03:59.416299',
                  'remote_address': None, 'quota_class': None,
                  'is_admin': True,
                  'service_catalog': [],
                  'read_deleted': 'no', 'show_deleted': False,
                  'roles': [],
                  'request_id': 'req-956637ad-354a-4bc5-b969-66fd1cc00f50',
                  'user_domain': None}
        ctx = context.RequestContext.from_dict(values)
        self.assertEqual('111', ctx.user)
        self.assertEqual('222', ctx.tenant)
        self.assertEqual('111', ctx.user_id)
        self.assertEqual('222', ctx.project_id)
        # to_dict() will add is_admin_project
        values.update({'is_admin_project': True})
        values2 = ctx.to_dict()
        self.assertEqual(values, values2)

    def test_convert_from_dict_then_to_dict(self):
        values = {'user': '111',
                  'user_id': '111',
                  'tenant': '222',
                  'project_id': '222',
                  'domain': None, 'project_domain': None,
                  'auth_token': None,
                  'resource_uuid': None, 'read_only': False,
                  'user_identity': '111 222 - - -',
                  'instance_lock_checked': False,
                  'user_name': None, 'project_name': None,
                  'timestamp': '2015-03-02T20:03:59.416299',
                  'remote_address': None, 'quota_class': None,
                  'is_admin': True,
                  'is_admin_project': True,
                  'service_catalog': [],
                  'read_deleted': 'no', 'show_deleted': False,
                  'roles': [],
                  'request_id': 'req-956637ad-354a-4bc5-b969-66fd1cc00f50',
                  'user_domain': None}
        ctx = context.RequestContext.from_dict(values)
        self.assertEqual('111', ctx.user)
        self.assertEqual('222', ctx.tenant)
        self.assertEqual('111', ctx.user_id)
        self.assertEqual('222', ctx.project_id)
        values2 = ctx.to_dict()
        self.assertEqual(values, values2)

    @mock.patch.object(context.policy, 'authorize')
    def test_can(self, mock_authorize):
        mock_authorize.return_value = True
        ctxt = context.RequestContext('111', '222')

        result = ctxt.can(mock.sentinel.rule)

        self.assertTrue(result)
        mock_authorize.assert_called_once_with(
          ctxt, mock.sentinel.rule,
          {'project_id': ctxt.project_id, 'user_id': ctxt.user_id})

    @mock.patch.object(context.policy, 'authorize')
    def test_can_fatal(self, mock_authorize):
        mock_authorize.side_effect = exception.Forbidden
        ctxt = context.RequestContext('111', '222')

        self.assertRaises(exception.Forbidden,
                          ctxt.can, mock.sentinel.rule)

    @mock.patch.object(context.policy, 'authorize')
    def test_can_non_fatal(self, mock_authorize):
        mock_authorize.side_effect = exception.Forbidden
        ctxt = context.RequestContext('111', '222')

        result = ctxt.can(mock.sentinel.rule, mock.sentinel.target,
                          fatal=False)

        self.assertFalse(result)
        mock_authorize.assert_called_once_with(ctxt, mock.sentinel.rule,
                                               mock.sentinel.target)

    @mock.patch('nova.db.create_context_manager')
    def test_target_cell(self, mock_create_ctxt_mgr):
        mock_create_ctxt_mgr.return_value = mock.sentinel.cm
        ctxt = context.RequestContext('111',
                                      '222',
                                      roles=['admin', 'weasel'])
        # Verify the existing db_connection, if any, is restored
        ctxt.db_connection = mock.sentinel.db_conn
        mapping = objects.CellMapping(database_connection='fake://')
        with context.target_cell(ctxt, mapping):
            self.assertEqual(ctxt.db_connection, mock.sentinel.cm)
        self.assertEqual(mock.sentinel.db_conn, ctxt.db_connection)
