import mimetypes, urllib, urlparse, re
from sys import version_info as python_version
from django.utils.timezone import now
from lxml import etree

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound, HttpResponseNotAllowed, HttpResponseBadRequest, \
    HttpResponseNotModified, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.http import parse_etags
from django.shortcuts import render_to_response
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from djangodav.responses import ResponseException, HttpResponsePreconditionFailed, HttpResponseCreated, HttpResponseNoContent, \
    HttpResponseConflict, HttpResponseMediatypeNotSupported, HttpResponseBadGateway, HttpResponseNotImplemented, \
    HttpResponseMultiStatus, HttpResponseLocked
from djangodav.utils import WEBDAV_NSMAP, D, url_join, get_property_tag_list, rfc1123_date
from djangodav import VERSION as djangodav_version
from django import VERSION as django_version, get_version

PATTERN_IF_DELIMITER = re.compile(r'(<([^>]+)>)|(\(([^\)]+)\))')


class DavView(View):
    resource_class = None
    lock_class = None
    acl_class = None
    template_name = 'djangodav/index.html'
    http_method_names = ['options', 'put', 'mkcol', 'head', 'get', 'delete', 'propfind', 'proppatch', 'copy', 'move', 'lock', 'unlock']
    server_header = 'DjangoDav/%s Django/%s Python/%s' % (
        get_version(djangodav_version),
        get_version(django_version),
        get_version(python_version)
    )

    @method_decorator(csrf_exempt)
    def dispatch(self, request, path, *args, **kwargs):
        self.path = path
        self.base_url = request.META['PATH_INFO'][:-len(self.path)]

        meta = request.META.get
        self.xbody = kwargs['xbody'] = None
        if meta('CONTENT_TYPE', '').startswith('text/xml') and int(meta('CONTENT_LENGTH', 0)) > 0:
            self.xbody = kwargs['xbody'] = etree.XPathDocumentEvaluator(
                etree.parse(request, etree.XMLParser(ns_clean=True)),
                namespaces=WEBDAV_NSMAP
            )

        if request.method.lower() in self.http_method_names:
            handler = getattr(self, request.method.lower(), self.http_method_not_allowed)
        else:
            handler = self.http_method_not_allowed
        try:
            resp = handler(request, self.path, *args, **kwargs)
        except ResponseException, e:
            resp = e.response
        if not 'Allow' in resp:
            methods = self._allowed_methods()
            if not methods:
                return HttpResponseForbidden()
            resp['Allow'] = ", ".join(methods)
        if not 'Date' in resp:
            resp['Date'] = rfc1123_date(now())
        if self.server_header:
            resp['Server'] = self.server_header
        return resp

    def options(self, request, path, *args, **kwargs):
        if not self.has_access(self.resource, 'read'):
            return HttpResponseForbidden()
        response = HttpResponse(content_type='text/html')
        response['DAV'] = '1,2'
        response['Content-Length'] = '0'
        if self.path in ('/', '*'):
            return response
        response['Allow'] = ", ".join(self._allowed_methods())
        if self.resource.exists and self.resource.is_object:
            response['Allow-Ranges'] = 'bytes'
        return response

    def _allowed_methods(self):
        allowed = ['OPTIONS']
        if not self.resource.exists:
            res = self.resource.get_parent()
            if not (res.is_collection and res.exists):
                return None
            return allowed + ['PUT', 'MKCOL']
        allowed += ['HEAD', 'GET', 'DELETE', 'PROPFIND', 'PROPPATCH', 'COPY', 'MOVE', 'LOCK', 'UNLOCK']
        if self.resource.is_object:
            allowed += ['PUT']
        return allowed

    def get_access(self, resource):
        """Return permission as DavAcl object. A DavACL should have the following attributes:
        read, write, delete, create, relocate, list. By default we implement a read-only
        system."""
        return self.acl_class(read=True, full=False)

    def has_access(self, resource, method):
        return getattr(self.get_access(resource), method)

    def get_resource_kwargs(self, **kwargs):
        return kwargs

    @cached_property
    def resource(self):
        return self.resource_class(**self.get_resource_kwargs(path=self.path))

    def get_depth(self, default='infinity'):
        depth = str(self.request.META.get('HTTP_DEPTH', default)).lower()
        if not depth in ('0', '1', 'infinity'):
            raise ResponseException(HttpResponseBadRequest('Invalid depth header value %s' % depth))
        if depth == 'infinity':
            depth = -1
        else:
            depth = int(depth)
        return depth

    def evaluate_conditions(self, res):
        if not res.exists:
            return
        etag = res.get_etag()
        mtime = res.get_mtime_stamp()
        cond_if_match = self.request.META.get('HTTP_IF_MATCH', None)
        if cond_if_match:
            etags = parse_etags(cond_if_match)
            if '*' in etags or etag in etags:
                raise ResponseException(HttpResponsePreconditionFailed())
        cond_if_modified_since = self.request.META.get('HTTP_IF_MODIFIED_SINCE', False)
        if cond_if_modified_since:
            # Parse and evaluate, but don't raise anything just yet...
            # This might be ignored based on If-None-Match evaluation.
            cond_if_modified_since = parse_time(cond_if_modified_since)
            if cond_if_modified_since and cond_if_modified_since > mtime:
                cond_if_modified_since = True
            else:
                cond_if_modified_since = False
        cond_if_none_match = self.request.META.get('HTTP_IF_NONE_MATCH', None)
        if cond_if_none_match:
            etags = parse_etags(cond_if_none_match)
            if '*' in etags or etag in etags:
                if self.request.method in ('GET', 'HEAD'):
                    raise ResponseException(HttpResponseNotModified())
                raise ResponseException(HttpResponsePreconditionFailed())
            # Ignore If-Modified-Since header...
            cond_if_modified_since = False
        cond_if_unmodified_since = self.request.META.get('HTTP_IF_UNMODIFIED_SINCE', None)
        if cond_if_unmodified_since:
            cond_if_unmodified_since = parse_time(cond_if_unmodified_since)
            if cond_if_unmodified_since and cond_if_unmodified_since <= mtime:
                raise ResponseException(HttpResponsePreconditionFailed())
        if cond_if_modified_since:
            # This previously evaluated True and is not being ignored...
            raise ResponseException(HttpResponseNotModified())
        # TODO: complete If header handling...
        cond_if = self.request.META.get('HTTP_IF', None)
        if cond_if:
            if not cond_if.startswith('<'):
                cond_if = '<*>' + cond_if
            #for (tmpurl, url, tmpcontent, content) in PATTERN_IF_DELIMITER.findall(cond_if):

    def get(self, request, path, head=False, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        if not path.endswith("/") and self.resource.is_collection:
            return HttpResponseRedirect(request.build_absolute_uri() + "/")
        if path.endswith("/") and self.resource.is_object:
            return HttpResponseRedirect(request.build_absolute_uri().rstrip("/"))
        response = HttpResponse()
        response['Content-Length'] = 0
        acl = self.get_access(self.resource)
        if self.resource.is_object:
            if not acl.read:
                return HttpResponseForbidden()
            if not head:
                response['Content-Length'] = self.resource.getcontentlength
                response.content = self.resource.read()
            response['Content-Type'] = self.resource.content_type
            response['ETag'] = self.resource.getetag
        else:
            if not acl.read:
                return HttpResponseForbidden()
            if not head:
                response = render_to_response(self.template_name, {'res': self.resource, 'base_url': self.base_url})
        response['Last-Modified'] = self.resource.getlastmodified
        return response

    def head(self, request, path, *args, **kwargs):
        return self.get(request, path, head=True, *args, **kwargs)

    def put(self, request, path, *args, **kwargs):
        parent = self.resource.get_parent()
        if not parent.exists:
            return HttpResponseNotFound()
        if self.resource.is_collection:
            return HttpResponseForbidden()
        if not self.resource.exists and not self.has_access(parent, 'write'):
                return HttpResponseForbidden()
        if self.resource.exists and not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()
        created = not self.resource.exists
        self.resource.write(request)
        if created:
            self.__dict__['resource'] = self.resource_class(self.resource.get_path())
            return HttpResponseCreated()
        else:
            return HttpResponseNoContent()

    def delete(self, request, path, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        if not self.has_access(self.resource, 'delete'):
            return HttpResponseForbidden()
        self.lock_class(self.resource).del_locks()
        self.resource.delete()
        response = HttpResponseNoContent()
        self.__dict__['resource'] = self.resource_class(self.resource.get_path())
        return response

    def mkcol(self, request, path, *args, **kwargs):
        if self.resource.exists:
            return HttpResponseNotAllowed(self._allowed_methods())
        if not self.resource.get_parent().exists:
            return HttpResponseConflict()
        length = request.META.get('CONTENT_LENGTH', 0)
        if length and int(length) != 0:
            return HttpResponseMediatypeNotSupported()
        if not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()
        self.resource.create_collection()
        self.__dict__['resource'] = self.resource_class(self.resource.get_path())
        return HttpResponseCreated()

    def relocate(self, request, path, method, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        if not self.has_access(self.resource, 'read'):
            return HttpResponseForbidden()
        dst = urllib.unquote(request.META.get('HTTP_DESTINATION', ''))
        if not dst:
            return HttpResponseBadRequest('Destination header missing.')
        dparts = urlparse.urlparse(dst)
        sparts = urlparse.urlparse(request.build_absolute_uri())
        if sparts.scheme != dparts.scheme or sparts.netloc != dparts.netloc:
            return HttpResponseBadGateway('Source and destination must have the same scheme and host.')
        # adjust path for our base url:
        dst = self.resource_class(dparts.path[len(self.base_url):])
        if not dst.get_parent().exists:
            return HttpResponseConflict()
        if not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()
        overwrite = request.META.get('HTTP_OVERWRITE', 'T')
        if overwrite not in ('T', 'F'):
            return HttpResponseBadRequest('Overwrite header must be T or F.')
        overwrite = (overwrite == 'T')
        if not overwrite and dst.exists:
            return HttpResponsePreconditionFailed('Destination exists and overwrite False.')
        dst_exists = dst.exists
        if dst_exists:
            self.lock_class(self.resource).del_locks()
            self.lock_class(dst).del_locks()
            dst.delete()
        errors = getattr(self.resource, method)(dst, *args, **kwargs)
        if errors:
            return HttpResponseMultiStatus() # WAT?
        if dst_exists:
            return HttpResponseNoContent()
        return HttpResponseCreated()

    def copy(self, request, path, xbody):
        depth = self.get_depth()
        if depth != -1:
            return HttpResponseBadRequest()
        return self.relocate(request, path, 'copy', depth=depth)

    def move(self, request, path, xbody):
        if not self.has_access(self.resource, 'delete'):
            return HttpResponseForbidden()
        return self.relocate(request, path, 'move')

    def lock(self, request, path, xbody=None, *args, **kwargs):
        # TODO Lock refreshing
        if not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()

        if not xbody:
            return HttpResponseBadRequest('Lockinfo required')

        try:
            depth = int(request.META.get('HTTP_DEPTH', '0'))
        except ValueError:
            return HttpResponseBadRequest('Wrong depth')

        try:
            timeout = int(request.META.get('HTTP_LOCK_TIMEOUT', 'Seconds-600')[len('Seconds-'):])
        except ValueError:
            return HttpResponseBadRequest('Wrong timeout')

        owner = None
        try:
            owner_obj = xbody('/D:lockinfo/D:owner')[0]  # TODO: WEBDAV_NS
        except IndexError:
            owner_obj = None
        else:
            if owner_obj.text:
                owner = owner_obj.text
            if len(owner_obj):
                owner = owner_obj[0].text

        try:
            lockscope_obj = xbody('/D:lockinfo/D:lockscope/*')[0] # TODO: WEBDAV_NS
        except IndexError:
            return HttpResponseBadRequest('Lock scope required')
        else:
            lockscope = lockscope_obj.xpath('local-name()')

        try:
            locktype_obj = xbody('/D:lockinfo/D:locktype/*')[0] # TODO: WEBDAV_NS
        except IndexError:
            return HttpResponseBadRequest('Lock type required')
        else:
            locktype = locktype_obj.xpath('local-name()')

        token = self.lock_class(self.resource).acquire(lockscope, locktype, depth, timeout, owner)
        if not token:
            return HttpResponseLocked('Already locked')

        body = D.activelock(*([
            D.locktype(locktype_obj),
            D.lockscope(lockscope_obj),
            D.depth(unicode(depth)),
            D.timeout("Second-%s" % timeout),
            D.locktoken(D.href('opaquelocktoken:%s' % token))]
            + ([owner_obj] if not owner_obj is None else [])
        ))

        return HttpResponse(etree.tostring(body, pretty_print=True, xml_declaration=True, encoding='utf-8'), content_type='application/xml')

    def unlock(self, request, path, xbody=None, *args, **kwargss):
        if not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()

        token = request.META.get('HTTP_LOCK_TOKEN')
        if not token:
            return HttpResponseBadRequest('Lock token required')
        if not self.lock_class(self.resource).release(token):
            return HttpResponseForbidden()
        return HttpResponseNoContent()

    def propfind(self, request, path, xbody=None, *args, **kwargs):
        if not self.has_access(self.resource, 'read'):
            return HttpResponseForbidden()

        if not self.resource.exists:
            return HttpResponseNotFound()

        if not self.get_access(self.resource):
            return HttpResponseForbidden()

        get_all_props, get_prop, get_prop_names = True, False, False
        if xbody:
            get_prop = [p.xpath('local-name()') for p in xbody('/D:propfind/D:prop/*')]
            get_all_props = xbody('/D:propfind/D:allprop')
            get_prop_names = xbody('/D:propfind/D:propname')
            if int(bool(get_prop)) + int(bool(get_all_props)) + int(bool(get_prop_names)) != 1:
                return HttpResponseBadRequest()

        children = self.resource.get_descendants(depth=self.get_depth(), include_self=True)

        if get_prop_names:
            responses = [
                D.response(
                    D.href(url_join(self.base_url, child.get_path())),
                    D.propstat(
                        D.prop(*[
                            D(name) for name in child.ALL_PROPS
                        ]),
                        D.status('HTTP/1.1 200 OK'),
                    ),
                )
                for child in children
            ]
        else:
            responses = [
                D.response(
                    D.href(url_join(self.base_url, child.get_path())),
                    D.propstat(
                        D.prop(
                            *get_property_tag_list(child, *(get_prop if get_prop else child.ALL_PROPS))
                        ),
                        D.status('HTTP/1.1 200 OK'),
                    ),
                )
                for child in children
            ]

        body = D.multistatus(*responses)
        response = HttpResponseMultiStatus(etree.tostring(body, pretty_print=True, xml_declaration=True, encoding='utf-8'))
        return response

    def proppatch(self, request, path, *args, **kwargs):
        if not self.resource.exists:
            return HttpResponseNotFound()
        if not self.has_access(self.resource, 'write'):
            return HttpResponseForbidden()
        depth = self.get_depth(default="0")
        if depth != 0:
            return HttpResponseBadRequest('Invalid depth header value %s' % depth)
        return HttpResponseNotImplemented()