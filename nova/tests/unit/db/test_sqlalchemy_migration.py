# Copyright 2014 IBM Corp.
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

import importlib
import mock
import uuid

from migrate import exceptions as versioning_exceptions
from migrate import UniqueConstraint
from migrate.versioning import api as versioning_api
from oslo_db.sqlalchemy import utils as db_utils
import sqlalchemy

from nova import context
from nova.db.sqlalchemy import api as db_api
from nova.db.sqlalchemy import migration
from nova import exception
from nova import objects
from nova import test
from nova.tests import fixtures as nova_fixtures


class TestNullInstanceUuidScanDB(test.TestCase):

    # NOTE(mriedem): Copied from the 267 database migration.
    def downgrade(self, migrate_engine):
        UniqueConstraint('uuid',
                         table=db_utils.get_table(migrate_engine, 'instances'),
                         name='uniq_instances0uuid').drop()
        for table_name in ('instances', 'shadow_instances'):
            table = db_utils.get_table(migrate_engine, table_name)
            table.columns.uuid.alter(nullable=True)

    def setUp(self):
        super(TestNullInstanceUuidScanDB, self).setUp()

        self.engine = db_api.get_engine()
        # When this test runs, we've already run the schema migration to make
        # instances.uuid non-nullable, so we have to alter the table here
        # so we can test against a real database.
        self.downgrade(self.engine)
        # Now create fake entries in the fixed_ips, consoles and
        # instances table where (instance_)uuid is None for testing.
        for table_name in ('fixed_ips', 'instances', 'consoles'):
            table = db_utils.get_table(self.engine, table_name)
            fake_record = {'id': 1}
            table.insert().execute(fake_record)

    def test_db_null_instance_uuid_scan_readonly(self):
        results = migration.db_null_instance_uuid_scan(delete=False)
        self.assertEqual(1, results.get('instances'))
        self.assertEqual(1, results.get('consoles'))
        # The fixed_ips table should be ignored.
        self.assertNotIn('fixed_ips', results)
        # Now pick a random table with an instance_uuid column and show it's
        # in the results but with 0 hits.
        self.assertEqual(0, results.get('instance_info_caches'))
        # Make sure nothing was deleted.
        for table_name in ('fixed_ips', 'instances', 'consoles'):
            table = db_utils.get_table(self.engine, table_name)
            record = table.select(table.c.id == 1).execute().first()
            self.assertIsNotNone(record)

    def test_db_null_instance_uuid_scan_delete(self):
        results = migration.db_null_instance_uuid_scan(delete=True)
        self.assertEqual(1, results.get('instances'))
        self.assertEqual(1, results.get('consoles'))
        # The fixed_ips table should be ignored.
        self.assertNotIn('fixed_ips', results)
        # Now pick a random table with an instance_uuid column and show it's
        # in the results but with 0 hits.
        self.assertEqual(0, results.get('instance_info_caches'))
        # Make sure fixed_ips wasn't touched, but instances and instance_faults
        # records were deleted.
        fixed_ips = db_utils.get_table(self.engine, 'fixed_ips')
        record = fixed_ips.select(fixed_ips.c.id == 1).execute().first()
        self.assertIsNotNone(record)

        consoles = db_utils.get_table(self.engine, 'consoles')
        record = consoles.select(consoles.c.id == 1).execute().first()
        self.assertIsNone(record)

        instances = db_utils.get_table(self.engine, 'instances')
        record = instances.select(instances.c.id == 1).execute().first()
        self.assertIsNone(record)


@mock.patch.object(migration, 'db_version', return_value=2)
@mock.patch.object(migration, '_find_migrate_repo', return_value='repo')
@mock.patch.object(versioning_api, 'upgrade')
@mock.patch.object(versioning_api, 'downgrade')
@mock.patch.object(migration, 'get_engine', return_value='engine')
class TestDbSync(test.NoDBTestCase):

    def test_version_none(self, mock_get_engine, mock_downgrade, mock_upgrade,
            mock_find_repo, mock_version):
        database = 'fake'
        migration.db_sync(database=database)
        mock_version.assert_called_once_with(database, context=None)
        mock_find_repo.assert_called_once_with(database)
        mock_get_engine.assert_called_once_with(database, context=None)
        mock_upgrade.assert_called_once_with('engine', 'repo', None)
        self.assertFalse(mock_downgrade.called)

    def test_downgrade(self, mock_get_engine, mock_downgrade, mock_upgrade,
            mock_find_repo, mock_version):
        database = 'fake'
        migration.db_sync(1, database=database)
        mock_version.assert_called_once_with(database, context=None)
        mock_find_repo.assert_called_once_with(database)
        mock_get_engine.assert_called_once_with(database, context=None)
        mock_downgrade.assert_called_once_with('engine', 'repo', 1)
        self.assertFalse(mock_upgrade.called)


@mock.patch.object(migration, '_find_migrate_repo', return_value='repo')
@mock.patch.object(versioning_api, 'db_version')
@mock.patch.object(migration, 'get_engine')
class TestDbVersion(test.NoDBTestCase):

    def test_db_version(self, mock_get_engine, mock_db_version,
            mock_find_repo):
        database = 'fake'
        mock_get_engine.return_value = 'engine'
        migration.db_version(database)
        mock_find_repo.assert_called_once_with(database)
        mock_db_version.assert_called_once_with('engine', 'repo')

    def test_not_controlled(self, mock_get_engine, mock_db_version,
            mock_find_repo):
        database = 'api'
        mock_get_engine.side_effect = ['engine', 'engine', 'engine']
        exc = versioning_exceptions.DatabaseNotControlledError()
        mock_db_version.side_effect = [exc, '']
        metadata = mock.MagicMock()
        metadata.tables.return_value = []
        with mock.patch.object(sqlalchemy, 'MetaData',
                metadata), mock.patch.object(migration,
                        'db_version_control') as mock_version_control:
            migration.db_version(database)
            mock_version_control.assert_called_once_with(0,
                                                         database,
                                                         context=None)
            db_version_calls = [mock.call('engine', 'repo')] * 2
            self.assertEqual(db_version_calls, mock_db_version.call_args_list)
        engine_calls = [mock.call(database, context=None)] * 3
        self.assertEqual(engine_calls, mock_get_engine.call_args_list)


@mock.patch.object(migration, '_find_migrate_repo', return_value='repo')
@mock.patch.object(migration, 'get_engine', return_value='engine')
@mock.patch.object(versioning_api, 'version_control')
class TestDbVersionControl(test.NoDBTestCase):

    def test_version_control(self, mock_version_control, mock_get_engine,
            mock_find_repo):
        database = 'fake'
        migration.db_version_control(database=database)
        mock_find_repo.assert_called_once_with(database)
        mock_version_control.assert_called_once_with('engine', 'repo', None)


class TestGetEngine(test.NoDBTestCase):

    def test_get_main_engine(self):
        with mock.patch.object(db_api, 'get_engine',
                return_value='engine') as mock_get_engine:
            engine = migration.get_engine()
            self.assertEqual('engine', engine)
            mock_get_engine.assert_called_once_with(context=None)

    def test_get_api_engine(self):
        with mock.patch.object(db_api, 'get_api_engine',
                return_value='api_engine') as mock_get_engine:
            engine = migration.get_engine('api')
            self.assertEqual('api_engine', engine)
            mock_get_engine.assert_called_once_with()


class TestFlavorCheck(test.TestCase):
    def setUp(self):
        super(TestFlavorCheck, self).setUp()
        self.context = context.get_admin_context()
        self.migration = importlib.import_module(
            'nova.db.sqlalchemy.migrate_repo.versions.'
            '291_enforce_flavors_migrated')
        self.engine = db_api.get_engine()

    def test_upgrade_clean(self):
        inst = objects.Instance(context=self.context,
                                uuid=uuid.uuid4(),
                                user_id=self.context.user_id,
                                project_id=self.context.project_id,
                                system_metadata={'foo': 'bar'})
        inst.create()
        self.migration.upgrade(self.engine)

    def test_upgrade_dirty(self):
        inst = objects.Instance(context=self.context,
                                uuid=uuid.uuid4(),
                                user_id=self.context.user_id,
                                project_id=self.context.project_id,
                                system_metadata={'foo': 'bar',
                                                 'instance_type_id': 'foo'})
        inst.create()
        self.assertRaises(exception.ValidationError,
                          self.migration.upgrade, self.engine)

    def test_upgrade_flavor_deleted_instances(self):
        inst = objects.Instance(context=self.context,
                                uuid=uuid.uuid4(),
                                user_id=self.context.user_id,
                                project_id=self.context.project_id,
                                system_metadata={'foo': 'bar',
                                                 'instance_type_id': 'foo'})
        inst.create()
        inst.destroy()
        self.migration.upgrade(self.engine)


class TestNewtonCheck(test.TestCase):
    def setUp(self):
        super(TestNewtonCheck, self).setUp()
        self.useFixture(nova_fixtures.DatabaseAtVersion(329))
        self.context = context.get_admin_context()
        self.migration = importlib.import_module(
            'nova.db.sqlalchemy.migrate_repo.versions.'
            '330_enforce_mitaka_online_migrations')
        self.engine = db_api.get_engine()

    def test_all_migrated(self):
        cn = objects.ComputeNode(context=self.context,
                                 vcpus=1, memory_mb=512, local_gb=10,
                                 vcpus_used=0, memory_mb_used=256,
                                 local_gb_used=5, hypervisor_type='HyperDanVM',
                                 hypervisor_version='34', cpu_info='foo')
        cn.create()
        objects.Aggregate(context=self.context,
                          name='foo').create()
        objects.PciDevice.create(self.context, {})
        self.migration.upgrade(self.engine)

    def test_cn_not_migrated(self):
        cn = objects.ComputeNode(context=self.context,
                                 vcpus=1, memory_mb=512, local_gb=10,
                                 vcpus_used=0, memory_mb_used=256,
                                 local_gb_used=5, hypervisor_type='HyperDanVM',
                                 hypervisor_version='34', cpu_info='foo')
        cn.create()
        db_api.compute_node_update(self.context, cn.id, {'uuid': None})
        self.assertRaises(exception.ValidationError,
                          self.migration.upgrade, self.engine)

    def test_aggregate_not_migrated(self):
        agg = db_api.aggregate_create(self.context, {"name": "foobar"})
        db_api.aggregate_update(self.context, agg.id, {'uuid': None})
        self.assertRaises(exception.ValidationError,
                          self.migration.upgrade, self.engine)

    def test_pci_device_not_migrated(self):
        db_api.pci_device_update(self.context, 1, 'foo:bar',
                                 {'parent_addr': None,
                                  'compute_node_id': 1,
                                  'address': 'foo:bar',
                                  'vendor_id': '123',
                                  'product_id': '456',
                                  'dev_type': 'foo',
                                  'label': 'foobar',
                                  'status': 'whatisthis?'})
        self.assertRaises(exception.ValidationError,
                          self.migration.upgrade, self.engine)
