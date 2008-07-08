from deliverance.exceptions import DeliveranceSyntaxError, AbortProxy
from deliverance.pagematch import AbstractMatch
from deliverance.util.converters import asbool
from deliverance.middleware import DeliveranceMiddleware
from deliverance.ruleset import RuleSet
from deliverance.log import SavingLogger
from deliverance.util.uritemplate import uri_template_substitute
from deliverance.util.nesteddict import NestedDict
from deliverance.security import execute_pyref
from lxml.etree import tostring as xml_tostring
from lxml.html import document_fromstring, tostring
from pyref import PyReference
from webob import Request
from webob import exc
import urlparse
from wsgiproxy.exactproxy import proxy_exact_request
import re
import socket
from lxml.etree import Comment
from tempita import html_quote
import os
import string
from paste.fileapp import FileApp
import urllib
import posixpath

class ProxySet(object):

    def __init__(self, proxies, ruleset, source_location=None):
        self.proxies = proxies
        self.ruleset = ruleset
        self.source_location = source_location
        self.deliverator = DeliveranceMiddleware(self.proxy_app, self.rule_getter)

    @classmethod
    def parse_xml(cls, el, source_location):
        proxies = []
        for child in el:
            if child.tag == 'proxy':
                proxies.append(Proxy.parse_xml(child, source_location))
        ruleset = RuleSet.parse_xml(el, source_location)
        return cls(proxies, ruleset, source_location)

    def proxy_app(self, environ, start_response):
        request = Request(environ)
        log = environ['deliverance.log']
        for proxy in self.proxies:
            ## FIXME: obviously this is wonky:
            if proxy.match(request, None, None, log):
                try:
                    return proxy.forward_request(environ, start_response)
                except AbortProxy, e:
                    log.debug(
                        self, '<proxy> aborted (%s), trying next proxy' % e)
                    continue
                ## FIXME: should also allow for AbortTheme?
        log.error(self, 'No proxy matched the request; aborting with a 404 Not Found error')
        ## FIXME: better error handling would be nice:
        resp = exc.HTTPNotFound()
        return resp(environ, start_response)

    def rule_getter(self, get_resource, app, orig_req):
        return self.ruleset

    def application(self, environ, start_response):
        req = Request(environ)
        log = SavingLogger(req, self.deliverator)
        req.environ['deliverance.log'] = log
        return self.deliverator(environ, start_response)

class Proxy(object):

    def __init__(self, match, dest,
                 request_modifications, response_modifications,
                 strip_script_name=False, keep_host=False,
                 source_location=None):
        self.match = match
        self.match.proxy = self
        self.dest = dest
        self.strip_script_name = strip_script_name
        self.keep_host = keep_host
        self.request_modifications = request_modifications
        self.response_modifications = response_modifications
        self.source_location = source_location

    def log_description(self, log=None):
        parts = []
        if log is None:
            parts.append('&lt;proxy')
        else:
            parts.append('&lt;<a href="%s" target="_blank">proxy</a>' % log.link_to(self.source_location, source=True))
        if self.strip_script_name:
            parts.append('strip-script-name="1"')
        if self.keep_host:
            parts.append('keep-host="1"')
        parts.append('/&gt;<br>\n')
        parts.append('&nbsp;' + self.dest.log_description(log))
        parts.append('<br>\n')
        if self.request_modifications:
            if len(self.request_modifications) > 1:
                parts.append('&nbsp;%i request modifications<br>\n' % len(self.request_modifications))
            else:
                parts.append('&nbsp;1 request modification<br>\n')
        if self.response_modifications:
            if len(self.response_modifications) > 1:
                parts.append('&nbsp;%i response modifications<br>\n' % len(self.response_modifications))
            else:
                parts.append('&nbsp;1 response modification<br>\n')
        parts.append('&lt;/proxy&gt;')
        return ' '.join(parts)

    @classmethod
    def parse_xml(cls, el, source_location):
        assert el.tag == 'proxy'
        match = ProxyMatch.parse_xml(el, source_location)
        dest = None
        request_modifications = []
        response_modifications = []
        strip_script_name = False
        keep_host = False
        for child in el:
            if child.tag == 'dest':
                if dest is not None:
                    raise DeliveranceSyntaxError(
                        "You cannot have more than one <dest> tag (second tag: %s)"
                        % xml_tostring(child),
                        element=child, source_location=source_location)
                dest = ProxyDest.parse_xml(child, source_location)
            elif child.tag == 'transform':
                if child.get('strip-script-name'):
                    strip_script_name = asbool(child.get('strip-script-name'))
                if child.get('keep-host'):
                    keep_host = asbool(child.get('keep-host'))
                ## FIXME: error on other attrs
            elif child.tag == 'request':
                request_modifications.append(
                    ProxyRequestModification.parse_xml(child, source_location))
            elif child.tag == 'response':
                response_modifications.append(
                    ProxyResponseModification.parse_xml(child, source_location))
            elif child.tag is Comment:
                continue
            else:
                raise DeliveranceSyntaxError(
                    "Unknown tag in <proxy>: %s" % xml_tostring(child),
                    element=child, source_location=source_location)
        return cls(match, dest, request_modifications, response_modifications,
                   strip_script_name=strip_script_name, keep_host=keep_host,
                   source_location=source_location)

    def forward_request(self, environ, start_response):
        request = Request(environ)
        prefix = self.match.strip_prefix()
        if prefix:
            if prefix.endswith('/'):
                prefix = prefix[:-1]
            path_info = request.path_info
            if not path_info.startswith(prefix + '/'):
                log.warn(self, "The match would strip the prefix %r from the request path (%r), but they do not match"
                         % (prefix + '/', path_info))
            else:
                request.script_name = request.script_name + prefix
                request.path_info = path_info[len(prefix):]
        log = request.environ['deliverance.log']
        for modifier in self.request_modifications:
            request = modifier.modify_request(request, log)
        if self.dest.next:
            raise AbortProxy
        dest = self.dest(request, log)
        log.debug(self, '<proxy> matched; forwarding request to %s' % dest)
        response, orig_base, proxied_base, proxied_url = self.proxy_to_dest(request, dest)
        for modifier in self.response_modifications:
            response = modifier.modify_response(request, response, orig_base, proxied_base, proxied_url, log)
        return response(environ, start_response)

    def proxy_to_dest(self, request, dest):
        # Not using request.copy because I don't want to copy wsgi.input:
        # FIXME: handle file:
        orig_base = request.application_url
        proxy_req = Request(request.environ.copy())
        scheme, netloc, path, query, fragment = urlparse.urlsplit(dest)
        assert not fragment, (
            "Unexpected fragment: %r" % fragment)
        if scheme == 'file':
            return self.proxy_to_file(request, dest)
        proxy_req.path_info = path + request.path_info
        proxy_req.server_name = netloc.split(':', 1)[0]
        if ':' in netloc:
            proxy_req.server_port = netloc.split(':', 1)[1]
        elif scheme == 'http':
            proxy_req.server_port = '80'
        elif scheme == 'https':
            proxy_req.server_port = '443'
        else:
            assert 0, "bad scheme: %r (from %r)" % (scheme, dest)
        if not self.keep_host:
            proxy_req.host = netloc
        proxied_url = '%s://%s%s' % (scheme, netloc, proxy_req.path_qs)
        if query:
            if proxy_req.query_string:
                proxy_req.query_string += '&'
            ## FIXME: add query before or after existing query?
            proxy_req.query_string += query
        proxy_req.headers['X-Forwarded-For'] = request.remote_addr
        proxy_req.headers['X-Forwarded-Scheme'] = request.scheme
        proxy_req.headers['X-Forwarded-Server'] = request.host
        ## FIXME: something with path? proxy_req.headers['X-Forwarded-Path']
        ## (now we are only doing it with strip_script_name)
        if self.strip_script_name:
            proxy_req.headers['X-Forwarded-Path'] = proxy_req.script_name
            proxy_req.script_name = ''
        try:
            resp = proxy_req.get_response(proxy_exact_request)
        except socket.error, e:
            ## FIXME: really wsgiproxy should handle this
            ## FIXME: which error?
            ## 502 HTTPBadGateway, 503 HTTPServiceUnavailable, 504 HTTPGatewayTimeout?
            if isinstance(e.args, tuple) and len(e.args) > 1:
                error = e.args[1]
            else:
                error = str(e)
            resp = exc.HTTPServiceUnavailable(
                'Could not proxy the request to %s:%s : %s' % (proxy_req.server_name, proxy_req.server_port, error))
        return resp, orig_base, dest, proxied_url

    def proxy_to_file(self, request, dest):
        orig_base = request.application_url
        ## FIXME: security restrictions here?
        assert dest.startswith('file:')
        filename = urllib.unquote('/' + dest[len('file:'):].lstrip('/'))
        rest = posixpath.normpath(request.path_info)
        proxied_url = dest.lstrip('/') + '/' + urllib.quote(rest.lstrip('/'))
        ## FIXME: handle /->/index.html
        filename = filename.rstrip('/') + '/' + rest.lstrip('/')
        app = FileApp(filename)
        # I don't really need a copied request here, because FileApp is so simple:
        resp = request.get_response(app)
        return resp, orig_base, dest, proxied_url
        
class ProxyMatch(AbstractMatch):
    
    element_name = 'proxy'
    
    @classmethod
    def parse_xml(cls, el, source_location):
        ## FIXME: this should have a way of indicating what portion of the path to strip
        return cls(**cls.parse_match_xml(el, source_location))
    
    def debug_description(self):
        return '<proxy>'

    def log_context(self):
        return self.proxy

    def strip_prefix(self):
        if self.path:
            return self.path.strip_prefix()
        return None

class ProxyDest(object):

    def __init__(self, href=None, pyref=None, next=False, source_location=None):
        self.href = href
        self.pyref = pyref
        self.next = next
        self.source_location = source_location

    @classmethod
    def parse_xml(cls, el, source_location):
        href = el.get('href')
        pyref = PyReference.parse_xml(el, source_location, 
                                      default_function='get_proxy_dest', default_objs=dict(AbortProxy=AbortProxy))
        next = asbool(el.get('next'))
        if next and (href or pyref):
            raise DeliveranceSyntaxError(
                'If you have a next="1" attribute you cannot also have an href or pyref attribute',
                element=el, source_location=source_location)
        return cls(href, pyref, next=next, source_location=source_location)

    def __call__(self, request, log):
        assert not self.next
        if self.pyref:
            if not execute_pyref(request):
                log.error(
                    self, "Security disallows executing pyref %s" % self.pyref)
            else:
                return self.pyref(request, log)
        ## FIXME: is this nesting really needed?
        ## we could just use HTTP_header keys...
        vars = NestedDict(request.environ, request.headers, dict(here=posixpath.dirname(self.source_location)))
        return uri_template_substitute(self.href, vars)

    def log_description(self, log=None):
        parts = ['&lt;dest']
        if self.href:
            if log is not None:
                parts.append('href="%s"' % html_quote(html_quote(self.href)))
            else:
                ## FIXME: definite security issue with the link through here:
                ## FIXME: Should this be source=True?
                parts.append('href="<a href="%s" target="_blank">%s</a>"' % 
                             (html_quote(log.link_to(self.href)), html_quote(html_quote(self.href))))
        if self.pyref:
            parts.append('pref="%s"' % html_quote(self.pyref))
        if self.next:
            parts.append('next="1"')
        parts.append('/&gt;')
        return ' '.join(parts)

class ProxyRequestModification(object):
    def __init__(self, pyref=None, header=None, content=None,
                 source_location=None):
        self.pyref = pyref
        self.header = header
        self.content = content
        self.source_location = source_location

    @classmethod
    def parse_xml(cls, el, source_location):
        assert el.tag == 'request'
        pyref = PyReference.parse_xml(
            el, source_location,
            default_function='modify_proxy_request', 
            default_objs=dict(AbortProxy=AbortProxy))
        header = el.get('header')
        content = el.get('content')
        if (not header and content) or (not content and header):
            raise DeliveranceSyntaxError(
                "If you provide a header attribute you must provide a content attribute, and vice versa",
                element=el, source_location=source_location)
        return cls(pyref, header, content, source_location)
        
    def modify_request(self, request, log):
        if self.pyref:
            if not execute_pyref(request):
                log.error(
                    self, "Security disallows executing pyref %s" % self.pyref)
            else:
                result = self.pyref(request, log)
                if isinstance(result, dict):
                    request = Request(result)
                elif isinstance(result, Request):
                    request = result
        if self.header:
            request.headers[self.header] = self.content
        return request

class ProxyResponseModification(object):
    def __init__(self, pyref=None, header=None, content=None, rewrite_links=False,
                 source_location=None):
        self.pyref = pyref
        self.header = header
        self.content = content
        self.rewrite_links = rewrite_links

    @classmethod
    def parse_xml(cls, el, source_location):
        assert el.tag == 'response'
        pyref = PyReference.parse_xml(
            el, source_location,
            default_function='modify_proxy_response', 
            default_objs=dict(AbortProxy=AbortProxy))
        header = el.get('header')
        content = el.get('content')
        if (not header and content) or (not content and header):
            raise DeliveranceSyntaxError(
                "If you provide a header attribute you must provide a content attribute, and vice versa",
                element=el, source_location=source_location)
        rewrite_links = asbool(el.get('rewrite-links'))
        return cls(pyref=pyref, header=header, content=content, rewrite_links=rewrite_links, 
                   source_location=source_location)

    _cookie_domain_re = re.compile(r'(domain="?)([a-z0-9._-]*)("?)', re.I)

    ## FIXME: instead of proxied_base/proxied_path, should I keep the modified request object?
    def modify_response(self, request, response, orig_base, proxied_base, proxied_url, log):
        if not proxied_base.endswith('/'):
            proxied_base += '/'
        if not orig_base.endswith('/'):
            orig_base += '/'
        assert proxied_url.startswith(proxied_base), (
            "Unexpected proxied_url %r, doesn't start with proxied_base %r"
            % (proxied_url, proxied_base))
        assert request.url.startswith(orig_base), (
            "Unexpected request.url %r, doesn't start with orig_base %r"
            % (request.url, orig_base))
        if self.pyref:
            if not execute_pyref(request):
                log.error(
                    self, "Security disallows executing pyref %s" % self.pyref)
            else:
                result = self.pyref(request, response, orig_base, proxied_base, proxied_url, log)
                if isinstance(result, Response):
                    response = result
        if self.header:
            response.headers[self.header] = self.content
        if self.rewrite_links:
            if response.content_type != 'text/html':
                log.debug(self, 'Not rewriting links in response from %s, because Content-Type is %s' % (proxied_url, response.content_type))
                return response
            body_doc = document_fromstring(response.body, base_url=proxied_url)
            body_doc.make_links_absolute()
            def link_repl_func(link):
                if not link.startswith(proxied_base):
                    # External link, so we don't rewrite it
                    return link
                new = orig_base + link[len(proxied_base):]
                return new
            body_doc.rewrite_links(link_repl_func)
            response.body = tostring(body_doc)
            if response.location:
                ## FIXME: if you give a proxy like http://openplans.org, and it redirects to
                ## http://www.openplans.org, it won't be rewritten and that can be confusing
                ## -- it *shouldn't* be rewritten, but some better log message is required
                loc = urlparse.urljoin(proxied_url, response.location)
                loc = link_repl_func(loc)
                response.location = loc
            if response.headers.get('set-cookie'):
                cook = response.headers['set-cookie']
                old_domain = urlparse.urlsplit(proxied_url)[1].lower()
                new_domain = req.host.split(':', 1)[0].lower()
                def rewrite_domain(match):
                    domain = match.group(2)
                    if domain == old_domain:
                        ## FIXME: doesn't catch wildcards and the sort
                        return match.group(1) + new_domain + match.group(3)
                    else:
                        return match.group(0)
                cook = self._cookie_domain_re.sub(rewrite_domain, cook)
                response.headers['set-cookie'] = cook
        return response

class ProxySettings(object):
    """
    Represents the settings for the proxy
    """
    def __init__(self, server_host, execute_pyref=True, display_local_files=True,
                 dev_allow_ips=None, dev_deny_ips=None, dev_htpasswd=None, dev_users=None,
                 dev_expiration=60,
                 source_location=None):
        self.server_host = server_host
        self.execute_pyref = execute_pyref
        self.display_local_files = display_local_files
        self.dev_allow_ips = dev_allow_ips
        self.dev_deny_ips = dev_deny_ips
        self.dev_htpasswd = dev_htpasswd
        self.dev_expiration = dev_expiration
        self.dev_users = dev_users
        self.source_location = source_location

    @classmethod
    def parse_xml(cls, el, source_location, environ=None, traverse=False):
        if traverse and el.tag != 'server-settings':
            try:
                el = el.xpath('//server-settings')[0]
            except IndexError:
                raise DeliveranceSyntaxError(
                    "There is no <server-settings> element",
                    element=el)
        if environ is None:
            environ = os.environ
        assert el.tag == 'server-settings'
        server_host = 'localhost:8080'
        ## FIXME: should these defaults be passed in:
        execute_pyref = True
        display_local_files = True
        dev_allow_ips = []
        dev_deny_ips = []
        dev_htpasswd = None
        dev_expiration = 60
        dev_users = {}
        for child in el:
            if child.tag is Comment:
                continue
            ## FIXME: should some of these be attributes?
            elif child.tag == 'server':
                server_host = cls.substitute(child.text, environ)
            elif child.tag == 'execute-pyref':
                pyref = asbool(cls.substitute(child.text, environ))
            elif child.tag == 'dev-allow':
                dev_allow_ips.extend(cls.substitute(child.text, environ).split())
            elif child.tag == 'dev-deny':
                dev_deny_ips.extend(cls.substitute(child.text, environ).split())
            elif child.tag == 'dev-htpasswd':
                dev_htpasswd = cls.substitute(child.text, environ)
            elif child.tag == 'dev-expiration':
                dev_expiration = cls.substitute(child.text, environ)
                if dev_expiration:
                    dev_expiration = int(dev_expiration)
            elif child.tag == 'display-local-files':
                display_local_files = asbool(cls.substitute(child.text, environ))
            elif child.tag == 'dev-user':
                username = cls.substitute(child.get('username', ''), environ)
                ## FIXME: allow hashed password?
                password = cls.substitute(child.get('password', ''), environ)
                if not username or not password:
                    raise DeliveranceSyntaxError(
                        "<dev-user> must have both a username and password attribute",
                        element=child)
                if username in dev_users:
                    raise DeliveranceSyntaxError(
                        '<dev-user username="%s"> appears more than once' % username,
                        element=el)
                dev_users[username] = password
            else:
                raise DeliveranceSyntaxError(
                    'Unknown element in <server-settings>: <%s>' % child.tag,
                    element=child)
        if dev_users and dev_htpasswd:
            raise DeliveranceSyntaxError(
                "You can use <dev-htpasswd> or <dev-user>, but not both",
                element=el)
        ## FIXME: add a default allow_ips of 127.0.0.1?
        return cls(server_host, execute_pyref=execute_pyref, display_local_files=display_local_files,
                   dev_allow_ips=dev_allow_ips, dev_deny_ips=dev_deny_ips, 
                   dev_users=dev_users, dev_expiration=dev_expiration,
                   source_location=source_location)

    @property
    def host(self):
        return self.server_host.split(':', 1)[0]

    @property
    def port(self):
        if ':' in self.server_host:
            return int(self.server_host.split(':', 1)[1])
        else:
            return 80

    @property
    def base_url(self):
        host = self.host
        if host == '0.0.0.0' or not host:
            host = '127.0.0.1'
        if self.port != 80:
            host += ':%s' % self.port
        return 'http://' + host

    @staticmethod
    def substitute(template, environ):
        if environ is None:
            return template
        return string.Template(template).substitute(environ)

    def middleware(self, app):
        """
        Wrap the given application in an appropriate DevAuth and Security instance
        """
        from devauth import DevAuth, convert_ip_mask
        from deliverance.security import SecurityContext
        if self.dev_users:
            password_checker = self.check_password
        else:
            password_checker = None
        app = SecurityContext.middleware(app, execute_pyref=self.execute_pyref,
                                         display_local_files=self.display_local_files)
        app = DevAuth(
            app,
            allow=convert_ip_mask(self.dev_allow_ips),
            deny=convert_ip_mask(self.dev_deny_ips),
            password_file=self.dev_htpasswd,
            password_checker=password_checker,
            expiration=self.dev_expiration,
            login_mountpoint='/.deliverance')
        return app

    def check_password(self, username, password):
        assert self.dev_users
        return self.dev_users.get(username) == password
