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

from nova.tests.functional.api_sample_tests import api_sample_base


class AggregatesSampleJsonTest(api_sample_base.ApiSampleTestBaseV21):
    ADMIN_API = True
    sample_dir = "os-aggregates"

    def _test_aggregate_create(self):
        subs = {
            "aggregate_id": '(?P<id>\d+)'
        }
        response = self._do_post('os-aggregates', 'aggregate-post-req', subs)
        return self._verify_response('aggregate-post-resp',
                                     subs, response, 200)

    def test_aggregate_create(self):
        self._test_aggregate_create()

    def _test_add_host(self, aggregate_id, host):
        subs = {
            "host_name": host
        }
        response = self._do_post('os-aggregates/%s/action' % aggregate_id,
                                 'aggregate-add-host-post-req', subs)
        self._verify_response('aggregates-add-host-post-resp', subs,
                              response, 200)

    def test_list_aggregates(self):
        aggregate_id = self._test_aggregate_create()
        self._test_add_host(aggregate_id, self.compute.host)
        response = self._do_get('os-aggregates')
        self._verify_response('aggregates-list-get-resp', {}, response, 200)

    def test_aggregate_get(self):
        agg_id = self._test_aggregate_create()
        response = self._do_get('os-aggregates/%s' % agg_id)
        self._verify_response('aggregates-get-resp', {}, response, 200)

    def test_add_metadata(self):
        agg_id = self._test_aggregate_create()
        response = self._do_post('os-aggregates/%s/action' % agg_id,
                                 'aggregate-metadata-post-req',
                                 {'action': 'set_metadata'})
        self._verify_response('aggregates-metadata-post-resp', {},
                              response, 200)

    def test_add_host(self):
        aggregate_id = self._test_aggregate_create()
        self._test_add_host(aggregate_id, self.compute.host)

    def test_remove_host(self):
        self.test_add_host()
        subs = {
            "host_name": self.compute.host,
        }
        response = self._do_post('os-aggregates/1/action',
                                 'aggregate-remove-host-post-req', subs)
        self._verify_response('aggregates-remove-host-post-resp',
                              subs, response, 200)

    def test_update_aggregate(self):
        aggregate_id = self._test_aggregate_create()
        response = self._do_put('os-aggregates/%s' % aggregate_id,
                                  'aggregate-update-post-req', {})
        self._verify_response('aggregate-update-post-resp',
                              {}, response, 200)
