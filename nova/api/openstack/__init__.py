# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
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

"""
WSGI middleware for OpenStack API controllers.
"""

from oslo_log import log as logging
import routes
import six
import stevedore
import webob.dec
import webob.exc

from nova.api.openstack import wsgi
import nova.conf
from nova.i18n import _LE
from nova.i18n import _LI
from nova.i18n import _LW
from nova.i18n import translate
from nova import notifications
from nova import utils
from nova import wsgi as base_wsgi


LOG = logging.getLogger(__name__)
CONF = nova.conf.CONF


class FaultWrapper(base_wsgi.Middleware):
    """Calls down the middleware stack, making exceptions into faults."""

    _status_to_type = {}

    @staticmethod
    def status_to_type(status):
        if not FaultWrapper._status_to_type:
            for clazz in utils.walk_class_hierarchy(webob.exc.HTTPError):
                FaultWrapper._status_to_type[clazz.code] = clazz
        return FaultWrapper._status_to_type.get(
                                  status, webob.exc.HTTPInternalServerError)()

    def _error(self, inner, req):
        LOG.exception(_LE("Caught error: %s"), six.text_type(inner))

        safe = getattr(inner, 'safe', False)
        headers = getattr(inner, 'headers', None)
        status = getattr(inner, 'code', 500)
        if status is None:
            status = 500

        msg_dict = dict(url=req.url, status=status)
        LOG.info(_LI("%(url)s returned with HTTP %(status)d"), msg_dict)
        outer = self.status_to_type(status)
        if headers:
            outer.headers = headers
        # NOTE(johannes): We leave the explanation empty here on
        # purpose. It could possibly have sensitive information
        # that should not be returned back to the user. See
        # bugs 868360 and 874472
        # NOTE(eglynn): However, it would be over-conservative and
        # inconsistent with the EC2 API to hide every exception,
        # including those that are safe to expose, see bug 1021373
        if safe:
            user_locale = req.best_match_language()
            inner_msg = translate(inner.message, user_locale)
            outer.explanation = '%s: %s' % (inner.__class__.__name__,
                                            inner_msg)

        notifications.send_api_fault(req.url, status, inner)
        return wsgi.Fault(outer)

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        try:
            return req.get_response(self.application)
        except Exception as ex:
            return self._error(ex, req)


class LegacyV2CompatibleWrapper(base_wsgi.Middleware):

    def _filter_request_headers(self, req):
        """For keeping same behavior with v2 API, ignores microversions
        HTTP headers X-OpenStack-Nova-API-Version and OpenStack-API-Version
        in the request.
        """

        if wsgi.API_VERSION_REQUEST_HEADER in req.headers:
            del req.headers[wsgi.API_VERSION_REQUEST_HEADER]
        if wsgi.LEGACY_API_VERSION_REQUEST_HEADER in req.headers:
            del req.headers[wsgi.LEGACY_API_VERSION_REQUEST_HEADER]
        return req

    def _filter_response_headers(self, response):
        """For keeping same behavior with v2 API, filter out microversions
        HTTP header and microversions field in header 'Vary'.
        """

        if wsgi.API_VERSION_REQUEST_HEADER in response.headers:
            del response.headers[wsgi.API_VERSION_REQUEST_HEADER]
        if wsgi.LEGACY_API_VERSION_REQUEST_HEADER in response.headers:
            del response.headers[wsgi.LEGACY_API_VERSION_REQUEST_HEADER]

        if 'Vary' in response.headers:
            vary_headers = response.headers['Vary'].split(',')
            filtered_vary = []
            for vary in vary_headers:
                vary = vary.strip()
                if (vary == wsgi.API_VERSION_REQUEST_HEADER or
                    vary == wsgi.LEGACY_API_VERSION_REQUEST_HEADER):
                    continue
                filtered_vary.append(vary)
            if filtered_vary:
                response.headers['Vary'] = ','.join(filtered_vary)
            else:
                del response.headers['Vary']
        return response

    @webob.dec.wsgify(RequestClass=wsgi.Request)
    def __call__(self, req):
        req.set_legacy_v2()
        req = self._filter_request_headers(req)
        response = req.get_response(self.application)
        return self._filter_response_headers(response)


class APIMapper(routes.Mapper):
    def routematch(self, url=None, environ=None):
        if url == "":
            result = self._match("", environ)
            return result[0], result[1]
        return routes.Mapper.routematch(self, url, environ)

    def connect(self, *args, **kargs):
        # NOTE(vish): Default the format part of a route to only accept json
        #             and xml so it doesn't eat all characters after a '.'
        #             in the url.
        kargs.setdefault('requirements', {})
        if not kargs['requirements'].get('format'):
            kargs['requirements']['format'] = 'json|xml'
        return routes.Mapper.connect(self, *args, **kargs)


class ProjectMapper(APIMapper):
    def resource(self, member_name, collection_name, **kwargs):
        # NOTE(sdague): project_id parameter is only valid if its hex
        # or hex + dashes (note, integers are a subset of this). This
        # is required to hand our overlaping routes issues.
        project_id_regex = '[0-9a-f\-]+'
        if CONF.osapi_v21.project_id_regex:
            project_id_regex = CONF.osapi_v21.project_id_regex

        project_id_token = '{project_id:%s}' % project_id_regex
        if 'parent_resource' not in kwargs:
            kwargs['path_prefix'] = '%s/' % project_id_token
        else:
            parent_resource = kwargs['parent_resource']
            p_collection = parent_resource['collection_name']
            p_member = parent_resource['member_name']
            kwargs['path_prefix'] = '%s/%s/:%s_id' % (
                project_id_token,
                p_collection,
                p_member)
        routes.Mapper.resource(
            self,
            member_name,
            collection_name,
            **kwargs)

        # while we are in transition mode, create additional routes
        # for the resource that do not include project_id.
        if 'parent_resource' not in kwargs:
            del kwargs['path_prefix']
        else:
            parent_resource = kwargs['parent_resource']
            p_collection = parent_resource['collection_name']
            p_member = parent_resource['member_name']
            kwargs['path_prefix'] = '%s/:%s_id' % (p_collection,
                                                   p_member)
        routes.Mapper.resource(self, member_name,
                                     collection_name,
                                     **kwargs)


class PlainMapper(APIMapper):
    def resource(self, member_name, collection_name, **kwargs):
        if 'parent_resource' in kwargs:
            parent_resource = kwargs['parent_resource']
            p_collection = parent_resource['collection_name']
            p_member = parent_resource['member_name']
            kwargs['path_prefix'] = '%s/:%s_id' % (p_collection, p_member)
        routes.Mapper.resource(self, member_name,
                                     collection_name,
                                     **kwargs)


class APIRouterV21(base_wsgi.Router):
    """Routes requests on the OpenStack v2.1 API to the appropriate controller
    and method.
    """

    @classmethod
    def factory(cls, global_config, **local_config):
        """Simple paste factory, :class:`nova.wsgi.Router` doesn't have one."""
        return cls()

    @staticmethod
    def api_extension_namespace():
        return 'nova.api.v21.extensions'

    def __init__(self, init_only=None):
        def _check_load_extension(ext):
            return self._register_extension(ext)

        self.api_extension_manager = stevedore.enabled.EnabledExtensionManager(
            namespace=self.api_extension_namespace(),
            check_func=_check_load_extension,
            invoke_on_load=True,
            invoke_kwds={"extension_info": self.loaded_extension_info})

        mapper = ProjectMapper()

        self.resources = {}

        # NOTE(cyeoh) Core API support is rewritten as extensions
        # but conceptually still have core
        if list(self.api_extension_manager):
            # NOTE(cyeoh): Stevedore raises an exception if there are
            # no plugins detected. I wonder if this is a bug.
            self._register_resources_check_inherits(mapper)
            self.api_extension_manager.map(self._register_controllers)

        LOG.info(_LI("Loaded extensions: %s"),
                 sorted(self.loaded_extension_info.get_extensions().keys()))
        super(APIRouterV21, self).__init__(mapper)

    def _register_resources_list(self, ext_list, mapper):
        for ext in ext_list:
            self._register_resources(ext, mapper)

    def _register_resources_check_inherits(self, mapper):
        ext_has_inherits = []
        ext_no_inherits = []

        for ext in self.api_extension_manager:
            for resource in ext.obj.get_resources():
                if resource.inherits:
                    ext_has_inherits.append(ext)
                    break
            else:
                ext_no_inherits.append(ext)

        self._register_resources_list(ext_no_inherits, mapper)
        self._register_resources_list(ext_has_inherits, mapper)

    @property
    def loaded_extension_info(self):
        raise NotImplementedError()

    def _register_extension(self, ext):
        raise NotImplementedError()

    def _register_resources(self, ext, mapper):
        """Register resources defined by the extensions

        Extensions define what resources they want to add through a
        get_resources function
        """

        handler = ext.obj
        LOG.debug("Running _register_resources on %s", ext.obj)

        for resource in handler.get_resources():
            LOG.debug('Extended resource: %s', resource.collection)

            inherits = None
            if resource.inherits:
                inherits = self.resources.get(resource.inherits)
                if not resource.controller:
                    resource.controller = inherits.controller
            wsgi_resource = wsgi.ResourceV21(resource.controller,
                                             inherits=inherits)
            self.resources[resource.collection] = wsgi_resource
            kargs = dict(
                controller=wsgi_resource,
                collection=resource.collection_actions,
                member=resource.member_actions)

            if resource.parent:
                kargs['parent_resource'] = resource.parent

            # non core-API plugins use the collection name as the
            # member name, but the core-API plugins use the
            # singular/plural convention for member/collection names
            if resource.member_name:
                member_name = resource.member_name
            else:
                member_name = resource.collection
            mapper.resource(member_name, resource.collection,
                            **kargs)

            if resource.custom_routes_fn:
                resource.custom_routes_fn(mapper, wsgi_resource)

    def _register_controllers(self, ext):
        """Register controllers defined by the extensions

        Extensions define what resources they want to add through
        a get_controller_extensions function
        """

        handler = ext.obj
        LOG.debug("Running _register_controllers on %s", ext.obj)

        for extension in handler.get_controller_extensions():
            ext_name = extension.extension.name
            collection = extension.collection
            controller = extension.controller

            if collection not in self.resources:
                LOG.warning(_LW('Extension %(ext_name)s: Cannot extend '
                                'resource %(collection)s: No such resource'),
                            {'ext_name': ext_name, 'collection': collection})
                continue

            LOG.debug('Extension %(ext_name)s extending resource: '
                      '%(collection)s',
                      {'ext_name': ext_name, 'collection': collection})

            resource = self.resources[collection]
            resource.register_actions(controller)
            resource.register_extensions(controller)
