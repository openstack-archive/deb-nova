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

import os

from gabbi import fixture
from oslo_utils import uuidutils

from nova.api.openstack.placement import deploy
from nova import conf
from nova import config
from nova import context
from nova import objects
from nova.tests import fixtures


CONF = conf.CONF


def setup_app():
    return deploy.loadapp(CONF)


class APIFixture(fixture.GabbiFixture):
    """Setup the required backend fixtures for a basic placement service."""

    def __init__(self):
        self.conf = None

    def start_fixture(self):
        self.conf = CONF
        self.conf.set_override('auth_strategy', 'noauth2')
        # Be explicit about all three database connections to avoid
        # potential conflicts with config on disk.
        self.conf.set_override('connection', "sqlite://", group='database')
        self.conf.set_override('connection', "sqlite://",
                               group='api_database')
        self.conf.set_override('connection', "sqlite://",
                               group='placement_database')
        config.parse_args([], default_config_files=None, configure_db=False,
                          init_rpc=False)

        # NOTE(cdent): api and main database are not used but we still need
        # to manage them to make the fixtures work correctly and not cause
        # conflicts with other tests in the same process.
        self.api_db_fixture = fixtures.Database('api')
        self.main_db_fixture = fixtures.Database('main')
        self.api_db_fixture.reset()
        self.main_db_fixture.reset()

        os.environ['RP_UUID'] = uuidutils.generate_uuid()
        os.environ['RP_NAME'] = uuidutils.generate_uuid()

    def stop_fixture(self):
        self.api_db_fixture.cleanup()
        self.main_db_fixture.cleanup()
        if self.conf:
            self.conf.reset()


class AllocationFixture(APIFixture):
    """An APIFixture that has some pre-made Allocations."""

    def start_fixture(self):
        super(AllocationFixture, self).start_fixture()
        self.context = context.get_admin_context()
        # Stealing from the super
        rp_name = os.environ['RP_NAME']
        rp_uuid = os.environ['RP_UUID']
        rp = objects.ResourceProvider(
            self.context, name=rp_name, uuid=rp_uuid)
        rp.create()
        inventory = objects.Inventory(
            self.context, resource_provider=rp,
            resource_class='DISK_GB', total=2048)
        inventory.obj_set_defaults()
        rp.add_inventory(inventory)
        allocation = objects.Allocation(
            self.context, resource_provider=rp,
            resource_class='DISK_GB',
            consumer_id=uuidutils.generate_uuid(),
            used=512)
        allocation.create()
        allocation = objects.Allocation(
            self.context, resource_provider=rp,
            resource_class='DISK_GB',
            consumer_id=uuidutils.generate_uuid(),
            used=512)
        allocation.create()
