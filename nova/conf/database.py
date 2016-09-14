# Copyright 2015 OpenStack Foundation
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

from nova.conf import paths

from oslo_config import cfg
from oslo_db import options as oslo_db_options

_DEFAULT_SQL_CONNECTION = 'sqlite:///' + paths.state_path_def('nova.sqlite')


# NOTE(sdague): we know of at least 1 instance of out of tree usage
# for this config in RAX. They used this because of performance issues
# with some queries. We think the right path forward is fixing the
# SQLA queries to be more performant for everyone.
db_driver_opt = cfg.StrOpt(
        'db_driver',
        default='nova.db',
        help='DEPRECATED: The driver to use for database access',
        deprecated_for_removal=True)


# NOTE(markus_z): We cannot simply do:
# conf.register_opts(oslo_db_options.database_opts, 'api_database')
# If we reuse a db config option for two different groups ("api_database"
# and "database") and deprecate or rename a config option in one of these
# groups, "oslo.config" cannot correctly determine which one to update.
# That's why we copied & pasted these config options for the "api_database"
# group here. See commit ba407e3 ("Add support for multiple database engines")
# for more details.
api_db_group = cfg.OptGroup('api_database',
                            title='API Database Options',
                            help="""
The *Nova API Database* is a separate database which is used for information
which is used across *cells*. This database is mandatory since the Mitaka
release (13.0.0).
""")

api_db_opts = [
    # TODO(markus_z): This should probably have a required=True attribute
    cfg.StrOpt('connection',
               secret=True,
               help=''),

    cfg.BoolOpt('sqlite_synchronous',
                default=True,
                help=''),

    cfg.StrOpt('slave_connection',
               secret=True,
               help=''),

    cfg.StrOpt('mysql_sql_mode',
               default='TRADITIONAL',
               help=''),

    cfg.IntOpt('idle_timeout',
               default=3600,
               help=''),

    # TODO(markus_z): We should probably default this to 5 to not rely on the
    # SQLAlchemy default. Otherwise we wouldn't provide a stable default.
    cfg.IntOpt('max_pool_size',
               help=''),

    cfg.IntOpt('max_retries',
               default=10,
               help=''),

    # TODO(markus_z): This should have a minimum attribute of 0
    cfg.IntOpt('retry_interval',
               default=10,
               help=''),

    # TODO(markus_z): We should probably default this to 10 to not rely on the
    # SQLAlchemy default. Otherwise we wouldn't provide a stable default.
    cfg.IntOpt('max_overflow',
               help=''),

    # TODO(markus_z): This should probably make use of the "choices" attribute.
    # "oslo.db" uses only the values [<0, 0, 50, 100] see module
    # /oslo_db/sqlalchemy/engines.py method "_setup_logging"
    cfg.IntOpt('connection_debug',
               default=0,
               help=''),

    cfg.BoolOpt('connection_trace',
                default=False,
                help=''),

    # TODO(markus_z): We should probably default this to 30 to not rely on the
    # SQLAlchemy default. Otherwise we wouldn't provide a stable default.
    cfg.IntOpt('pool_timeout',
               help='')
]  # noqa


def enrich_help_text(alt_db_opts):

    def get_db_opts():
        for group_name, db_opts in oslo_db_options.list_opts():
            if group_name == 'database':
                return db_opts
        return []

    for db_opt in get_db_opts():
        for alt_db_opt in alt_db_opts:
            if alt_db_opt.name == db_opt.name:
                # NOTE(markus_z): We can append alternative DB specific help
                # texts here if needed.
                alt_db_opt.help = db_opt.help + alt_db_opt.help

# NOTE(cdent): See the note above on api_db_opts. The same issues
# apply here.

placement_db_group = cfg.OptGroup('placement_database',
                                  title='Placement API database options',
                                  help="""
The *Placement API Database* is a separate database which is used for the new
placement-api service. In Ocata release (14.0.0) this database is optional: if
connection option is not set, api database will be used instead.  However, this
is not recommended, as it implies a potentially lengthy data migration in the
future. Operators are advised to use a separate database for Placement API from
the start.
""")

# TODO(rpodolyaka): see the notes on help messages on api_db_opts above, those
# also apply here
placement_db_opts = [
    cfg.StrOpt('connection',
               default=None,
               help='',
               secret=True),
    cfg.BoolOpt('sqlite_synchronous',
                default=True,
                help=''),
    cfg.StrOpt('slave_connection',
               secret=True,
               help=''),
    cfg.StrOpt('mysql_sql_mode',
               default='TRADITIONAL',
               help=''),
    cfg.IntOpt('idle_timeout',
               default=3600,
               help=''),
    cfg.IntOpt('max_pool_size',
               help=''),
    cfg.IntOpt('max_retries',
               default=10,
               help=''),
    cfg.IntOpt('retry_interval',
               default=10,
               help=''),
    cfg.IntOpt('max_overflow',
               help=''),
    cfg.IntOpt('connection_debug',
               default=0,
               help=''),
    cfg.BoolOpt('connection_trace',
                default=False,
                help=''),
    cfg.IntOpt('pool_timeout',
               help=''),
]  # noqa


def register_opts(conf):
    oslo_db_options.set_defaults(conf, connection=_DEFAULT_SQL_CONNECTION,
                                 sqlite_db='nova.sqlite')
    conf.register_opt(db_driver_opt)
    conf.register_opts(api_db_opts, group=api_db_group)
    conf.register_opts(placement_db_opts, group=placement_db_group)


def list_opts():
    # NOTE(markus_z): 2016-04-04: If we list the oslo_db_options here, they
    # get emitted twice(!) in the "sample.conf" file. First under the
    # namespace "nova.conf" and second under the namespace "oslo.db". This
    # is due to the setting in file "etc/nova/nova-config-generator.conf".
    # As I think it is useful to have the "oslo.db" namespace information
    # in the "sample.conf" file, I omit the listing of the "oslo_db_options"
    # here.
    enrich_help_text(api_db_opts)
    enrich_help_text(placement_db_opts)
    return {'DEFAULT': [db_driver_opt],
            api_db_group: api_db_opts,
            placement_db_group: placement_db_opts,
            }
