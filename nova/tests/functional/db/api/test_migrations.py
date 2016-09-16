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
Tests for database migrations.
There are "opportunistic" tests which allows testing against all 3 databases
(sqlite in memory, mysql, pg) in a properly configured unit test environment.

For the opportunistic testing you need to set up db's named 'openstack_citest'
with user 'openstack_citest' and password 'openstack_citest' on localhost. The
test will then use that db and u/p combo to run the tests.

For postgres on Ubuntu this can be done with the following commands::

| sudo -u postgres psql
| postgres=# create user openstack_citest with createdb login password
|       'openstack_citest';
| postgres=# create database openstack_citest with owner openstack_citest;

"""

import logging
import os

from migrate.versioning import repository
import mock
from oslo_db.sqlalchemy import test_base
from oslo_db.sqlalchemy import test_migrations
from oslo_db.sqlalchemy import utils as db_utils
import sqlalchemy
from sqlalchemy.engine import reflection

from nova.db import migration
from nova.db.sqlalchemy.api_migrations import migrate_repo
from nova.db.sqlalchemy import api_models
from nova.db.sqlalchemy import migration as sa_migration
from nova import test


class NovaAPIModelsSync(test_migrations.ModelsMigrationsSync):
    """Test that the models match the database after migrations are run."""

    def db_sync(self, engine):
        with mock.patch.object(sa_migration, 'get_engine',
                               return_value=engine):
            sa_migration.db_sync(database='api')

    @property
    def migrate_engine(self):
        return self.engine

    def get_engine(self, context=None):
        return self.migrate_engine

    def get_metadata(self):
        return api_models.API_BASE.metadata

    def include_object(self, object_, name, type_, reflected, compare_to):
        if type_ == 'table':
            # migrate_version is a sqlalchemy-migrate control table and
            # isn't included in the model.
            if name == 'migrate_version':
                return False

        return True

    def filter_metadata_diff(self, diff):
        # Filter out diffs that shouldn't cause a sync failure.

        new_diff = []

        # Define a whitelist of ForeignKeys that exist on the model but not in
        # the database. They will be removed from the model at a later time.
        fkey_whitelist = {'build_requests': ['request_spec_id']}

        # Define a whitelist of columns that will be removed from the
        # DB at a later release and aren't on a model anymore.

        column_whitelist = {
                'build_requests': ['vm_state', 'instance_metadata',
                    'display_name', 'access_ip_v6', 'access_ip_v4', 'key_name',
                    'locked_by', 'image_ref', 'progress', 'request_spec_id',
                    'info_cache', 'user_id', 'task_state', 'security_groups',
                    'config_drive']
        }

        for element in diff:
            if isinstance(element, list):
                # modify_nullable is a list
                new_diff.append(element)
            else:
                # tuple with action as first element. Different actions have
                # different tuple structures.
                if element[0] == 'add_fk':
                    fkey = element[1]
                    tablename = fkey.table.name
                    column_keys = fkey.column_keys
                    if (tablename in fkey_whitelist and
                            column_keys == fkey_whitelist[tablename]):
                        continue
                elif element[0] == 'remove_column':
                    tablename = element[2]
                    column = element[3]
                    if (tablename in column_whitelist and
                            column.name in column_whitelist[tablename]):
                        continue

                new_diff.append(element)
        return new_diff


class TestNovaAPIMigrationsSQLite(NovaAPIModelsSync,
                                  test_base.DbTestCase,
                                  test.NoDBTestCase):
    pass


class TestNovaAPIMigrationsMySQL(NovaAPIModelsSync,
                                 test_base.MySQLOpportunisticTestCase,
                                 test.NoDBTestCase):
    pass


class TestNovaAPIMigrationsPostgreSQL(NovaAPIModelsSync,
        test_base.PostgreSQLOpportunisticTestCase, test.NoDBTestCase):
    pass


class NovaAPIMigrationsWalk(test_migrations.WalkVersionsMixin):
    def setUp(self):
        super(NovaAPIMigrationsWalk, self).setUp()
        # NOTE(viktors): We should reduce log output because it causes issues,
        #                when we run tests with testr
        migrate_log = logging.getLogger('migrate')
        old_level = migrate_log.level
        migrate_log.setLevel(logging.WARN)
        self.addCleanup(migrate_log.setLevel, old_level)

    @property
    def INIT_VERSION(self):
        return migration.db_initial_version('api')

    @property
    def REPOSITORY(self):
        return repository.Repository(
            os.path.abspath(os.path.dirname(migrate_repo.__file__)))

    @property
    def migration_api(self):
        return sa_migration.versioning_api

    @property
    def migrate_engine(self):
        return self.engine

    def _skippable_migrations(self):
        mitaka_placeholders = range(8, 13)
        return mitaka_placeholders

    def migrate_up(self, version, with_data=False):
        if with_data:
            check = getattr(self, '_check_%03d' % version, None)
            if version not in self._skippable_migrations():
                self.assertIsNotNone(check,
                                     ('API DB Migration %i does not have a '
                                      'test. Please add one!') % version)
        super(NovaAPIMigrationsWalk, self).migrate_up(version, with_data)

    def test_walk_versions(self):
        self.walk_versions(snake_walk=False, downgrade=False)

    def assertColumnExists(self, engine, table_name, column):
        self.assertTrue(db_utils.column_exists(engine, table_name, column),
                'Column %s.%s does not exist' % (table_name, column))

    def assertIndexExists(self, engine, table_name, index):
        self.assertTrue(db_utils.index_exists(engine, table_name, index),
                        'Index %s on table %s does not exist' %
                        (index, table_name))

    def assertUniqueConstraintExists(self, engine, table_name, columns):
        inspector = reflection.Inspector.from_engine(engine)
        constrs = inspector.get_unique_constraints(table_name)
        constr_columns = [constr['column_names'] for constr in constrs]
        self.assertIn(columns, constr_columns)

    def assertTableNotExists(self, engine, table_name):
        self.assertRaises(sqlalchemy.exc.NoSuchTableError,
                db_utils.get_table, engine, table_name)

    def _check_001(self, engine, data):
        for column in ['created_at', 'updated_at', 'id', 'uuid', 'name',
                'transport_url', 'database_connection']:
            self.assertColumnExists(engine, 'cell_mappings', column)

        self.assertIndexExists(engine, 'cell_mappings', 'uuid_idx')
        self.assertUniqueConstraintExists(engine, 'cell_mappings',
                ['uuid'])

    def _check_002(self, engine, data):
        for column in ['created_at', 'updated_at', 'id', 'instance_uuid',
                'cell_id', 'project_id']:
            self.assertColumnExists(engine, 'instance_mappings', column)

        for index in ['instance_uuid_idx', 'project_id_idx']:
            self.assertIndexExists(engine, 'instance_mappings', index)

        self.assertUniqueConstraintExists(engine, 'instance_mappings',
                ['instance_uuid'])

        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('instance_mappings')[0]
        self.assertEqual('cell_mappings', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['cell_id'], fk['constrained_columns'])

    def _check_003(self, engine, data):
        for column in ['created_at', 'updated_at', 'id',
                'cell_id', 'host']:
            self.assertColumnExists(engine, 'host_mappings', column)

        self.assertIndexExists(engine, 'host_mappings', 'host_idx')
        self.assertUniqueConstraintExists(engine, 'host_mappings',
                ['host'])

        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('host_mappings')[0]
        self.assertEqual('cell_mappings', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['cell_id'], fk['constrained_columns'])

    def _check_004(self, engine, data):
        columns = ['created_at', 'updated_at', 'id', 'instance_uuid', 'spec']
        for column in columns:
            self.assertColumnExists(engine, 'request_specs', column)

        self.assertUniqueConstraintExists(engine, 'request_specs',
                ['instance_uuid'])
        self.assertIndexExists(engine, 'request_specs',
                               'request_spec_instance_uuid_idx')

    def _check_005(self, engine, data):
        # flavors
        for column in ['created_at', 'updated_at', 'name', 'id', 'memory_mb',
            'vcpus', 'swap', 'vcpu_weight', 'flavorid', 'rxtx_factor',
            'root_gb', 'ephemeral_gb', 'disabled', 'is_public']:
            self.assertColumnExists(engine, 'flavors', column)
        self.assertUniqueConstraintExists(engine, 'flavors',
                ['flavorid'])
        self.assertUniqueConstraintExists(engine, 'flavors',
                ['name'])

        # flavor_extra_specs
        for column in ['created_at', 'updated_at', 'id', 'flavor_id', 'key',
            'value']:
            self.assertColumnExists(engine, 'flavor_extra_specs', column)

        self.assertIndexExists(engine, 'flavor_extra_specs',
                               'flavor_extra_specs_flavor_id_key_idx')
        self.assertUniqueConstraintExists(engine, 'flavor_extra_specs',
            ['flavor_id', 'key'])

        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('flavor_extra_specs')[0]
        self.assertEqual('flavors', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['flavor_id'], fk['constrained_columns'])

        # flavor_projects
        for column in ['created_at', 'updated_at', 'id', 'flavor_id',
            'project_id']:
            self.assertColumnExists(engine, 'flavor_projects', column)

        self.assertUniqueConstraintExists(engine, 'flavor_projects',
            ['flavor_id', 'project_id'])

        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('flavor_projects')[0]
        self.assertEqual('flavors', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['flavor_id'], fk['constrained_columns'])

    def _check_006(self, engine, data):
        for column in ['id', 'request_spec_id', 'project_id', 'user_id',
                'display_name', 'instance_metadata', 'progress', 'vm_state',
                'image_ref', 'access_ip_v4', 'access_ip_v6', 'info_cache',
                'security_groups', 'config_drive', 'key_name', 'locked_by']:
            self.assertColumnExists(engine, 'build_requests', column)

        self.assertIndexExists(engine, 'build_requests',
            'build_requests_project_id_idx')
        self.assertUniqueConstraintExists(engine, 'build_requests',
                ['request_spec_id'])

        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('build_requests')[0]
        self.assertEqual('request_specs', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['request_spec_id'], fk['constrained_columns'])

    def _check_007(self, engine, data):
        map_table = db_utils.get_table(engine, 'instance_mappings')
        self.assertTrue(map_table.columns['cell_id'].nullable)

        # Ensure the foreign key still exists
        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('instance_mappings')[0]
        self.assertEqual('cell_mappings', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])
        self.assertEqual(['cell_id'], fk['constrained_columns'])

    def _check_013(self, engine, data):
        for column in ['instance_uuid', 'instance']:
            self.assertColumnExists(engine, 'build_requests', column)
        self.assertIndexExists(engine, 'build_requests',
            'build_requests_instance_uuid_idx')
        self.assertUniqueConstraintExists(engine, 'build_requests',
                ['instance_uuid'])

    def _check_014(self, engine, data):
        for column in ['name', 'public_key']:
            self.assertColumnExists(engine, 'key_pairs', column)
        self.assertUniqueConstraintExists(engine, 'key_pairs',
                                          ['user_id', 'name'])

    def _check_015(self, engine, data):
        build_requests_table = db_utils.get_table(engine, 'build_requests')
        for column in ['request_spec_id', 'user_id', 'security_groups',
                'config_drive']:
            self.assertTrue(build_requests_table.columns[column].nullable)
        inspector = reflection.Inspector.from_engine(engine)
        constrs = inspector.get_unique_constraints('build_requests')
        constr_columns = [constr['column_names'] for constr in constrs]
        self.assertNotIn(['request_spec_id'], constr_columns)

    def _check_016(self, engine, data):
        self.assertColumnExists(engine, 'resource_providers', 'id')
        self.assertIndexExists(engine, 'resource_providers',
                               'resource_providers_name_idx')
        self.assertIndexExists(engine, 'resource_providers',
                               'resource_providers_uuid_idx')

        self.assertColumnExists(engine, 'inventories', 'id')
        self.assertIndexExists(engine, 'inventories',
                               'inventories_resource_class_id_idx')

        self.assertColumnExists(engine, 'allocations', 'id')
        self.assertColumnExists(engine, 'resource_provider_aggregates',
                                'aggregate_id')

    def _check_017(self, engine, data):
        # aggregate_metadata
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'aggregate_id',
                       'key',
                       'value']:
            self.assertColumnExists(engine, 'aggregate_metadata', column)

        self.assertUniqueConstraintExists(engine, 'aggregate_metadata',
                ['aggregate_id', 'key'])
        self.assertIndexExists(engine, 'aggregate_metadata',
            'aggregate_metadata_key_idx')

        # aggregate_hosts
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'host',
                       'aggregate_id']:
            self.assertColumnExists(engine, 'aggregate_hosts', column)

        self.assertUniqueConstraintExists(engine, 'aggregate_hosts',
                ['host', 'aggregate_id'])

        # aggregates
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'name']:
            self.assertColumnExists(engine, 'aggregates', column)

        self.assertIndexExists(engine, 'aggregates',
            'aggregate_uuid_idx')
        self.assertUniqueConstraintExists(engine, 'aggregates', ['name'])

    def _check_018(self, engine, data):
        # instance_groups
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'user_id',
                       'project_id',
                       'uuid',
                       'name']:
            self.assertColumnExists(engine, 'instance_groups', column)

        self.assertUniqueConstraintExists(engine, 'instance_groups', ['uuid'])

        # instance_group_policy
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'policy',
                       'group_id']:
            self.assertColumnExists(engine, 'instance_group_policy', column)

        self.assertIndexExists(engine, 'instance_group_policy',
                               'instance_group_policy_policy_idx')
        # Ensure the foreign key still exists
        inspector = reflection.Inspector.from_engine(engine)
        # There should only be one foreign key here
        fk = inspector.get_foreign_keys('instance_group_policy')[0]
        self.assertEqual('instance_groups', fk['referred_table'])
        self.assertEqual(['id'], fk['referred_columns'])

        # instance_group_member
        for column in ['created_at',
                       'updated_at',
                       'id',
                       'instance_uuid',
                       'group_id']:
            self.assertColumnExists(engine, 'instance_group_member', column)

        self.assertIndexExists(engine, 'instance_group_member',
                               'instance_group_member_instance_idx')

    def _check_019(self, engine, data):
        self.assertColumnExists(engine, 'build_requests',
                                'block_device_mappings')


class TestNovaAPIMigrationsWalkSQLite(NovaAPIMigrationsWalk,
                                      test_base.DbTestCase,
                                      test.NoDBTestCase):
    pass


class TestNovaAPIMigrationsWalkMySQL(NovaAPIMigrationsWalk,
                                     test_base.MySQLOpportunisticTestCase,
                                     test.NoDBTestCase):
    pass


class TestNovaAPIMigrationsWalkPostgreSQL(NovaAPIMigrationsWalk,
        test_base.PostgreSQLOpportunisticTestCase, test.NoDBTestCase):
    pass
