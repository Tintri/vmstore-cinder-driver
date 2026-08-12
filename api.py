# Copyright 2026 DDN, Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""VMstore REST API client for Cinder driver.

    Version history:

    .. code-block:: none
        3.0-beta - Initial driver version.
        3.0.6    - cache volume stats, filter list snapshots
        3.0.8    - Fix BUG: lock uuid Encode
        3.0.10   - Refactor -> Transport layer: proxy dispatch + request engine
"""

import hashlib
import json
import posixpath
from typing import Any
from urllib import parse as urlparse

from eventlet import greenthread
from oslo_log import log as logging
from oslo_utils import strutils
import requests

from cinder import exception
from cinder.i18n import _

LOG = logging.getLogger(__name__)

ASYNC_WAIT = 0.25


class VmstoreException(exception.VolumeDriverException):
    """Exception class for VMstore driver errors."""

    def __init__(self, data=None, **kwargs):
        defaults = {
            'typeId': 'VmstoreError',
            'code': 'ERR_API',
            'source': 'CinderDriver',
            'message': 'Unknown error',
            'causeDetails': 'No details'
        }
        if isinstance(data, dict):
            for key in defaults:
                if key in kwargs:
                    continue
                if key in data:
                    kwargs[key] = data[key]
                else:
                    kwargs[key] = defaults[key]
        elif isinstance(data, str):
            if 'causeDetails' not in kwargs:
                kwargs['causeDetails'] = data
        for key in defaults:
            if key not in kwargs:
                kwargs[key] = defaults[key]
        if (kwargs['causeDetails'] == defaults['causeDetails'] and
                kwargs.get('message') and
                kwargs['message'] != defaults['message']):
            kwargs['causeDetails'] = kwargs['message']
        message = ('%(causeDetails)s (source: %(source)s, '
                   'typeId: %(typeId)s, code: %(code)s)') % kwargs
        self.code = kwargs['code']
        del kwargs['causeDetails']
        super(VmstoreException, self).__init__(message)


class VmstoreCollections(object):
    def __init__(self, proxy):
        self.proxy = proxy
        self.namespace = 'vmstore'
        self.prefix = 'instance'
        self.root = '/collections'
        self.subj = 'collection'
        self.properties = []

    def path(self, name):
        quoted_name = urlparse.quote_plus(name)
        return posixpath.join(self.root, quoted_name)

    def key(self, name):
        return '%s:%s_%s' % (self.namespace, self.prefix, name)

    def get(self, payload):
        LOG.debug('Get properties of %(subj)s %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        return self.proxy.get(self.root, payload)

    def set(self, payload=None):
        LOG.debug('Modify properties of %(subj)s %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        return self.proxy.put(self.root, payload)

    def list(self, payload=None):
        LOG.debug('Getting list of %(subj)s: %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        return self.proxy.get(self.root, payload)

    def create(self, payload=None):
        LOG.debug('Create %(subj)s: %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        try:
            return self.proxy.post(self.root, payload)
        except VmstoreException as error:
            if error.code != 'RESOURCE_EXIST':
                raise

    def delete(self, payload):
        LOG.debug('Delete %(subj)s %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        path = self.path(payload)
        try:
            return self.proxy.delete(path, payload)
        except VmstoreException as error:
            if error.code == 'RESOURCE_NOT_FOUND':
                LOG.debug('Resource not found during delete, treating as '
                          'success: %(payload)s', {'payload': payload})
                return
            raise


class VmstoreClones(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreClones, self).__init__(proxy)
        self.root = 'cinder/clone'
        self.subj = 'Clones'


class VmstoreVirtualDisks(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreVirtualDisks, self).__init__(proxy)
        self.root = 'virtualDisk'
        self.subj = 'VirtualDisk'

    def get(self, uuid):
        path = '%s?uuid=%s' % (self.root, uuid)
        return self.proxy.get(path)


class VmstoreSnapshots(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreSnapshots, self).__init__(proxy)
        self.root = 'snapshot'
        self.subj = 'VolumeSnapshot'

    def list(self, payload=None):
        """List snapshots with optional filtering.

        :param payload: Dict of filter parameters (e.g. {'contain': name}).
                        Keys and values are URL-encoded into the query string.
        :return: List of snapshots matching the filters
        """
        path = self.root
        if payload and isinstance(payload, dict):
            query_params = []
            for key, value in payload.items():
                encoded_value = urlparse.quote_plus(str(value))
                query_params.append('%s=%s' % (key, encoded_value))
            if query_params:
                path = '%s?%s' % (self.root, '&'.join(query_params))
        LOG.debug('Getting list of %(subj)s with path: %(path)s',
                  {'subj': self.subj, 'path': path})
        return self.proxy.get(path)

    def create(self, payload=None):
        LOG.debug('Create %(subj)s: %(payload)s',
                  {'subj': self.subj, 'payload': payload})
        path = posixpath.join('cinder', self.root)
        try:
            return self.proxy.post(path, payload)
        except VmstoreException as error:
            if error.code != 'RESOURCE_EXIST':
                raise


class VmstoreAppliance(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreAppliance, self).__init__(proxy)
        self.root = 'appliance'
        self.subj = 'appliance'

    def getAllIPs(self):
        path = posixpath.join(self.root, 'default', 'ips')
        return self.proxy.get(path)


class VmstoreCinderRefresh(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreCinderRefresh, self).__init__(proxy)
        self.root = 'cinder/host/refresh'
        self.subj = 'cinderRefresh'

    def create(self, payload=None):
        """Fire hypervisor refresh. Raises immediately on any error.

        Overrides base create() to use _execute_strict: the refresh endpoint
        has distinct semantics — any failure must surface to the caller rather
        than being swallowed by the standard retry-and-log loop.
        """
        LOG.debug('Create %s: %s', self.subj, payload)
        return self.proxy._execute_strict('post', self.root, payload)


class VmstoreDatastore(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreDatastore, self).__init__(proxy)
        self.root = 'datastore'
        self.subj = 'datastore'

    def getReplicationPaths(self):
        path = posixpath.join(self.root, 'default', 'replicationPath')
        return self.proxy.get(path)


class VmstoreVirtualMachines(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreVirtualMachines, self).__init__(proxy)
        self.root = 'vm'
        self.subj = 'VirtualMachines'


class VmstoreTasks(VmstoreCollections):
    def __init__(self, proxy):
        super(VmstoreTasks, self).__init__(proxy)
        self.root = 'task'
        self.subj = 'Tasks'

    def getTaskById(self, task_id):
        path = posixpath.join(self.root, task_id)
        return self.proxy.get(path)

class VmstoreProxy(object):
    def __init__(self, proto, backend, conf, client_version=None):
        self.clones = VmstoreClones(self)
        self.virtual_disk = VmstoreVirtualDisks(self)
        self.snapshots = VmstoreSnapshots(self)
        self.appliance = VmstoreAppliance(self)
        self.cinder_refresh = VmstoreCinderRefresh(self)
        self.datastore = VmstoreDatastore(self)
        self.virtual_machines = VmstoreVirtualMachines(self)
        self.tasks = VmstoreTasks(self)
        self.version = None
        self.lock = None
        # REST API minor version negotiated at login via fullApiVersion.
        # Left None until fetched from /api/info; without it the appliance
        # falls back to a very old schema (e.g. no ipCidr on appliance IPs).
        self.api_version = None

        if client_version is None:
            client_version = 'Tintri-Cinder-Driver'
        self.headers = {
            'Content-Type': 'application/json',
            'X-XSS-Protection': '1',
            'Tintri-Api-Client': client_version
        }
        self.scheme = conf.vmstore_rest_protocol
        self.host = conf.vmstore_rest_address
        self.port = conf.vmstore_rest_port
        self.username = conf.vmstore_user
        self.password = conf.vmstore_password
        self.backend = backend
        self.retries = conf.vmstore_rest_retry_count
        self.refresh_retries = conf.vmstore_refresh_retry_count
        self.backoff = conf.vmstore_rest_backoff_factor
        self.timeout = (conf.vmstore_rest_connect_timeout,
                        conf.vmstore_rest_read_timeout)
        self.session = requests.Session()
        self.session.verify = conf.driver_ssl_cert_verify
        self.session.auth = (self.username, self.password)
        if self.session.verify and conf.driver_ssl_cert_path:
            self.session.verify = conf.driver_ssl_cert_path
        self.session.headers.update(self.headers)
        if not conf.driver_ssl_cert_verify:
            requests.packages.urllib3.disable_warnings()
        self.token = ""
        # Log in up front so the session negotiates the appliance's preferred
        # API version (fullApiVersion). This must happen before any request so
        # responses use the modern schema rather than the basic-auth default.
        self._auth()
        self.update_lock()

    # ------------------------------------------------------------------
    # Public HTTP interface — four explicit verbs
    # ------------------------------------------------------------------

    def get(self, path, payload=None):
        return self._execute('get', path, payload)

    def post(self, path, payload=None):
        return self._execute('post', path, payload)

    def delete(self, path, payload=None):
        return self._execute('delete', path, payload)

    def put(self, path, payload=None):
        return self._execute('put', path, payload)

    # ------------------------------------------------------------------
    # Execution engine
    # ------------------------------------------------------------------

    def _execute(self, method, path, payload=None):
        """Retry loop: up to retries+1 attempts with backoff and session refresh.

        On each failure the error is logged and the session is refreshed before
        the next attempt. After exhausting all attempts the last exception is
        re-raised.
        """
        last_error = None
        info = '%s %s' % (method.upper(), self.url(path))
        for attempt in range(self.retries + 1):
            if last_error:
                self.delay(attempt)
                self.update_host()
                LOG.debug('Retry %s attempt %s/%s after: %s',
                          info, attempt, self.retries, last_error)
            try:
                return self._attempt(method, path, payload)
            except VmstoreException as exc:
                last_error = exc
                LOG.error('Failed %s: %s', info, exc)
        LOG.error('Reached maximum %s retries for %s: %s',
                  self.retries, info, last_error)
        raise last_error or VmstoreException(
            message=_('All %s retries exhausted for %s') % (self.retries, info))

    def _execute_strict(self, method, path, payload=None):
        """Execution for endpoints that must raise immediately on any error.

        Uses refresh_retries as the attempt budget but raises on the first
        failure without logging-and-continuing. Intended for VmstoreCinderRefresh
        where the caller needs to handle the error explicitly.
        """
        last_error = None
        for attempt in range(self.refresh_retries + 1):
            if last_error:
                self.delay(attempt)
                self.update_host()
            try:
                response = self._send_raw(method, path, payload)
            except Exception as exc:
                raise VmstoreException(str(exc), code='RESOURCE_NOT_FOUND')
            content = self._parse_content(response)
            if not response.ok:
                raise VmstoreException(content)
            return content
        raise last_error or VmstoreException(
            message=_('Strict execution exhausted retries without a response'))

    def _attempt(self, method, path, payload=None):
        """Single attempt: send request, re-auth on 401, collect response."""
        response = self._send_raw(method, path, payload)
        if response.status_code == requests.codes.unauthorized:
            if self._auth():
                response = self._send_raw(method, path, payload)
        return self._collect(method, response)

    def _collect(self, method, response):
        """Parse response and follow pagination links for GET requests.

        Pagination uses an explicit loop rather than recursive hook callbacks,
        making each page fetch visible and independently testable.
        """
        content = self._parse_content(response)
        self._check_error(response, content)

        # Normalised to success by _check_error (e.g. idempotent DELETE 404)
        if not response.ok:
            return None

        # Some endpoints return None or 0 for success with no body
        if content is None or content == 0:
            return content

        # POST 201 Created with items: return immediately, no pagination
        if (response.status_code == requests.codes.created
                and isinstance(content, dict)
                and 'items' in content):
            return content['items']

        # Non-paginated response or non-GET verb: return content as-is
        if (method != 'get'
                or not isinstance(content, dict)
                or 'items' not in content):
            return content

        # Paginated GET: explicit loop
        results = list(content['items'])
        next_path, next_payload = self._next_link(content)
        while next_path:
            r = self._send_raw('get', next_path, next_payload)
            c = self._parse_content(r)
            self._check_error(r, c)
            if isinstance(c, dict):
                results.extend(c.get('items', []))
                next_path, next_payload = self._next_link(c)
            else:
                break
        return results

    # ------------------------------------------------------------------
    # HTTP primitives
    # ------------------------------------------------------------------

    def _send_raw(self, method, path, payload=None):
        """Send one HTTP request. No retry, no error handling, no side effects."""
        if method not in ('get', 'post', 'put', 'delete'):
            raise VmstoreException(
                code='INVALID_ARGUMENT',
                message=_('Request method %s not supported') % method)
        if not path:
            raise VmstoreException(
                code='INVALID_ARGUMENT',
                message=_('Request path is required'))
        url = self.url(path)
        kwargs: dict[str, Any] = {'timeout': self.timeout}
        if payload and method in ('post', 'put'):
            kwargs['data'] = json.dumps(payload)
        if 'v310/appliance' not in url:
            # Mask password/secret-like keys so credentials never hit the logs.
            safe_payload = (strutils.mask_dict_password(payload)
                            if isinstance(payload, dict) else payload)
            LOG.debug('%s %s %s', method.upper(), url, safe_payload)
        return self.session.request(method, url, **kwargs)

    def _parse_content(self, response):
        """Parse JSON response body. Returns None for empty responses."""
        if not response.content:
            return None
        try:
            return json.loads(response.content)
        except (TypeError, ValueError) as exc:
            raise VmstoreException(
                code='INVALID_ARGUMENT',
                message=_('JSON parse error on response: %s') % exc)

    def _check_error(self, response, content):
        """Raise VmstoreException for error responses, with documented exceptions.

        Special cases (do NOT raise):
          - DELETE 404 with 'Failed to lookup' — idempotent, treat as success.

        Special cases (raise with typed code):
          - 404 with 'does not exist' in message — RESOURCE_NOT_FOUND.
          - 500 RESOURCE_BUSY — propagate immediately so caller can retry.

        Snapshot-delete with active clones is logged before raising so the
        collection-level handler in VmstoreCollections.delete() can catch it.
        """
        if response.ok:
            return
        # Idempotent DELETE: 404 "Failed to lookup" is success
        if (response.status_code == requests.codes.not_found
                and response.request.method == 'DELETE'
                and 'Failed to lookup' in (
                    (content or {}).get('causeDetails') or '')):
            return
        # Typed not-found
        if (isinstance(content, dict)
                and content.get('message')
                and 'does not exist' in content['message']):
            raise VmstoreException(content['message'], code='RESOURCE_NOT_FOUND')
        # Resource busy — let the retry loop handle it
        if (response.status_code == requests.codes.server_error
                and isinstance(content, dict)
                and content.get('code') == 'RESOURCE_BUSY'):
            raise VmstoreException(content)
        # Active-clone guard on snapshot delete
        if (isinstance(content, dict)
                and 'live VM is still present' in (
                    content.get('causeDetails') or '')):
            LOG.info('Could not delete snapshot with existing clones; '
                     'will be cleaned up when the parent volume is deleted')
        raise VmstoreException(content)

    def _next_link(self, content):
        """Return (path, payload) for the next page link, or (None, None)."""
        for link in content.get('links', []):
            if isinstance(link, dict) and link.get('rel') == 'next':
                href = link.get('href', '')
                parsed = urlparse.urlparse(href)
                payload = urlparse.parse_qs(parsed.query)
                return parsed.path, payload
        return None, None

    def _auth(self):
        """Re-authenticate and update session token. Returns True on success.

        Sends fullApiVersion so the appliance serves the modern API schema
        (e.g. ipCidr on appliance IPs). On success the session cookie becomes
        the sole credential; basic auth is dropped so the negotiated version
        governs every subsequent request.
        """
        payload = {
            'username': self.username,
            'typeId': ('com.tintri.api.rest.vcommon.dto.rbac.'
                       'RestApiCredentials'),
            'password': self.password,
        }
        version = self._get_preferred_api_version()
        if version:
            payload['fullApiVersion'] = version
        self.delete_bearer()
        response = self._send_raw('post', '/session/login', payload)
        if 'JSESSIONID' in response.cookies:
            token = response.cookies['JSESSIONID']
            if token:
                self.update_token(token)
                # Rely on the versioned session cookie, not basic auth, so the
                # appliance keeps using the fullApiVersion we negotiated.
                self.session.auth = None
                return True
        return False

    def _get_preferred_api_version(self):
        """Return the appliance's preferred REST API version from /api/info.

        The value is cached after the first successful lookup. Returns None on
        any failure so login can still proceed (falling back to the default
        API version). Note /api/info lives outside the /api/v310 base path, so
        the URL is built directly rather than via url().
        """
        if self.api_version:
            return self.api_version
        netloc = '%s:%d' % (self.host, self.port)
        info_url = urlparse.urlunsplit(
            (self.scheme, netloc, '/api/info', None, None))
        try:
            response = self.session.get(info_url, timeout=self.timeout)
            content = self._parse_content(response)
            if isinstance(content, dict) and content.get('preferredVersion'):
                self.api_version = content['preferredVersion']
                LOG.info('Using VMstore preferred API version %s',
                         self.api_version)
        except Exception as exc:
            LOG.warning('Could not fetch preferred API version from '
                        '%(url)s: %(err)s', {'url': info_url, 'err': exc})
        return self.api_version

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def delete_bearer(self):
        if 'Authorization' in self.session.headers:
            del self.session.headers['Authorization']

    def update_bearer(self, token):
        bearer = 'JSESSIONID=%s' % token
        self.session.headers['cookie'] = bearer

    def update_token(self, token):
        self.token = token
        self.update_bearer(token)

    def update_host(self):
        self.update_lock()
        self.update_bearer(self.token)

    def update_lock(self):
        """Refresh the appliance UUID-based coordination lock.

        Uses _attempt (single try, no retry) rather than the public get() to
        avoid infinite recursion: _execute calls update_host() between retries,
        which calls update_lock(), which must not re-enter _execute.
        """
        try:
            result = self._attempt('get', 'appliance')
            if not result:
                return False
            uuid = result[0]['uuid']
        except Exception:
            return False

        lock = uuid['uuid'].encode('utf-8')
        self.lock = hashlib.md5(lock, usedforsecurity=False).hexdigest()
        LOG.info('Coordination lock for group %(backend)s: %(lock)s',
                 {'backend': self.backend, 'lock': self.lock})
        return True

    def url(self, path=None):
        if not path:
            path = ''
        netloc = '%s:%d/api/v310' % (self.host, self.port)
        components = (self.scheme, netloc, path, None, None)
        return urlparse.urlunsplit(components)

    def delay(self, attempt, sync=True):
        backoff = self.backoff
        if not sync:
            backoff = ASYNC_WAIT
        if self.retries > 0:
            attempt %= self.retries
            if attempt == 0:
                attempt = self.retries
        interval = float(backoff * (2 ** (attempt - 1)))
        LOG.debug('Waiting for %(interval)s seconds', {'interval': interval})
        greenthread.sleep(interval)  # type: ignore # eventlet accepts float seconds
        return interval
