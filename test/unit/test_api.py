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

"""Unit tests for VMstore API client."""

import json
from unittest import mock

import requests

from cinder.tests.unit import test
from cinder.tests.unit.volume.drivers.vmstore import set_vmstore_overrides
from cinder.volume.drivers.vmstore import api


def _make_response(status_code, body=None, method='GET'):
    """Build a minimal requests.Response for testing."""
    response = mock.Mock(spec=requests.Response)
    response.status_code = status_code
    response.ok = status_code < 400
    response.content = json.dumps(body).encode() if body is not None else b''
    response.cookies = {}
    response.request = mock.Mock()
    response.request.method = method
    return response


class VmstoreExceptionTestCase(test.TestCase):

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreExceptionTestCase, self).setUp()

    def test_exception_with_dict_data(self):
        data = {
            'typeId': 'TestError',
            'code': 'TEST_ERROR',
            'source': 'TestSource',
            'message': 'Test message',
            'causeDetails': 'Test cause details'
        }
        exc = api.VmstoreException(data)
        self.assertEqual('TEST_ERROR', exc.code)
        self.assertIn('Test cause details', str(exc))
        self.assertIn('TestSource', str(exc))
        self.assertIn('TestError', str(exc))

    def test_exception_with_string_data(self):
        exc = api.VmstoreException('Simple error message')
        self.assertEqual('ERR_API', exc.code)
        self.assertIn('Simple error message', str(exc))

    def test_exception_with_kwargs(self):
        exc = api.VmstoreException(
            code='CUSTOM_CODE',
            causeDetails='Custom details',
            source='CustomSource'
        )
        self.assertEqual('CUSTOM_CODE', exc.code)
        self.assertIn('Custom details', str(exc))
        self.assertIn('CustomSource', str(exc))

    def test_exception_defaults(self):
        exc = api.VmstoreException()
        self.assertEqual('ERR_API', exc.code)
        self.assertIn('No details', str(exc))
        self.assertIn('CinderDriver', str(exc))


class VmstoreCollectionsTestCase(test.TestCase):

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreCollectionsTestCase, self).setUp()
        self.mock_proxy = mock.Mock()
        self.collections = api.VmstoreCollections(self.mock_proxy)

    def test_path_generation(self):
        name = 'volume with spaces'
        path = self.collections.path(name)
        self.assertIn('volume+with+spaces', path)

    def test_key_generation(self):
        self.collections.namespace = 'test_ns'
        self.collections.prefix = 'test_prefix'
        key = self.collections.key('test_name')
        self.assertEqual('test_ns:test_prefix_test_name', key)

    def test_delete_not_found_returns_success(self):
        error = api.VmstoreException(code='RESOURCE_NOT_FOUND')
        self.mock_proxy.delete.side_effect = error
        result = self.collections.delete('test_resource')
        self.assertIsNone(result)

    def test_delete_other_error_raises(self):
        error = api.VmstoreException(code='OTHER_ERROR')
        self.mock_proxy.delete.side_effect = error
        self.assertRaises(
            api.VmstoreException,
            self.collections.delete,
            'test_resource'
        )


class VmstoreProxyTestCase(test.TestCase):

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreProxyTestCase, self).setUp()
        self.mock_conf = mock.Mock()
        self.mock_conf.vmstore_rest_protocol = 'https'
        self.mock_conf.vmstore_rest_address = '192.168.1.1'
        self.mock_conf.vmstore_rest_port = 443
        self.mock_conf.vmstore_user = 'admin'
        self.mock_conf.vmstore_password = 'secret'
        self.mock_conf.vmstore_rest_retry_count = 3
        self.mock_conf.vmstore_refresh_retry_count = 1
        self.mock_conf.vmstore_rest_backoff_factor = 1
        self.mock_conf.vmstore_rest_connect_timeout = 30
        self.mock_conf.vmstore_rest_read_timeout = 300
        self.mock_conf.driver_ssl_cert_verify = False
        self.mock_conf.driver_ssl_cert_path = None

    @mock.patch('requests.Session')
    def test_proxy_initialization(self, mock_session):
        proxy = api.VmstoreProxy('nfs', 'backend1', self.mock_conf)
        self.assertEqual('192.168.1.1', proxy.host)
        self.assertEqual(443, proxy.port)
        self.assertEqual('https', proxy.scheme)
        self.assertEqual(3, proxy.retries)

    @mock.patch('requests.Session')
    def test_proxy_client_version(self, mock_session):
        proxy = api.VmstoreProxy('nfs', 'backend1', self.mock_conf,
                                 client_version='Tintri-Cinder-Driver-3.0.10')
        self.assertEqual('Tintri-Cinder-Driver-3.0.10',
                         proxy.headers['Tintri-Api-Client'])

    @mock.patch('requests.Session')
    def test_url_generation(self, mock_session):
        proxy = api.VmstoreProxy('nfs', 'backend1', self.mock_conf)
        url = proxy.url('/test/path')
        self.assertEqual('https://192.168.1.1:443/api/v310/test/path', url)

    @mock.patch('requests.Session')
    def test_explicit_http_verbs_exist(self, mock_session):
        """get/post/delete/put must be real methods, not __getattr__ synthesis."""
        proxy = api.VmstoreProxy('nfs', 'backend1', self.mock_conf)
        for verb in ('get', 'post', 'delete', 'put'):
            self.assertTrue(callable(getattr(proxy, verb)),
                            'Expected %s to be a callable method' % verb)
        self.assertFalse(hasattr(type(proxy), '__getattr__'),
                         'VmstoreProxy must not use __getattr__ dispatch')


class VmstoreProxyCheckErrorTestCase(test.TestCase):
    """Tests for _check_error special-case handling."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreProxyCheckErrorTestCase, self).setUp()
        # Build a minimal proxy without hitting the network
        mock_conf = mock.Mock()
        mock_conf.vmstore_rest_protocol = 'https'
        mock_conf.vmstore_rest_address = '192.168.1.1'
        mock_conf.vmstore_rest_port = 443
        mock_conf.vmstore_user = 'admin'
        mock_conf.vmstore_password = 'secret'
        mock_conf.vmstore_rest_retry_count = 1
        mock_conf.vmstore_refresh_retry_count = 1
        mock_conf.vmstore_rest_backoff_factor = 1
        mock_conf.vmstore_rest_connect_timeout = 30
        mock_conf.vmstore_rest_read_timeout = 300
        mock_conf.driver_ssl_cert_verify = False
        mock_conf.driver_ssl_cert_path = None
        with mock.patch('requests.Session'):
            self.proxy = api.VmstoreProxy('nfs', 'b', mock_conf)

    def test_ok_response_does_not_raise(self):
        response = _make_response(200, {'key': 'val'})
        self.proxy._check_error(response, {'key': 'val'})  # must not raise

    def test_delete_404_failed_to_lookup_is_success(self):
        content = {'causeDetails': 'Failed to lookup resource abc'}
        response = _make_response(404, content, method='DELETE')
        self.proxy._check_error(response, content)  # must not raise

    def test_404_does_not_exist_raises_resource_not_found(self):
        content = {'message': 'Resource does not exist'}
        response = _make_response(404, content)
        exc = self.assertRaises(
            api.VmstoreException,
            self.proxy._check_error, response, content)
        self.assertEqual('RESOURCE_NOT_FOUND', exc.code)

    def test_500_resource_busy_raises(self):
        content = {'code': 'RESOURCE_BUSY', 'message': 'busy'}
        response = _make_response(500, content)
        self.assertRaises(
            api.VmstoreException,
            self.proxy._check_error, response, content)

    def test_generic_error_raises(self):
        content = {'message': 'Something went wrong', 'code': 'ERR_API'}
        response = _make_response(503, content)
        self.assertRaises(
            api.VmstoreException,
            self.proxy._check_error, response, content)


class VmstoreProxyCollectTestCase(test.TestCase):
    """Tests for _collect: response parsing and explicit pagination loop."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreProxyCollectTestCase, self).setUp()
        mock_conf = mock.Mock()
        mock_conf.vmstore_rest_protocol = 'https'
        mock_conf.vmstore_rest_address = '192.168.1.1'
        mock_conf.vmstore_rest_port = 443
        mock_conf.vmstore_user = 'admin'
        mock_conf.vmstore_password = 'secret'
        mock_conf.vmstore_rest_retry_count = 1
        mock_conf.vmstore_refresh_retry_count = 1
        mock_conf.vmstore_rest_backoff_factor = 1
        mock_conf.vmstore_rest_connect_timeout = 30
        mock_conf.vmstore_rest_read_timeout = 300
        mock_conf.driver_ssl_cert_verify = False
        mock_conf.driver_ssl_cert_path = None
        with mock.patch('requests.Session'):
            self.proxy = api.VmstoreProxy('nfs', 'b', mock_conf)

    def test_non_paginated_get_returns_content(self):
        content = {'key': 'value'}
        response = _make_response(200, content)
        result = self.proxy._collect('get', response)
        self.assertEqual(content, result)

    def test_post_201_with_items_returns_items(self):
        items = [{'id': '1'}, {'id': '2'}]
        response = _make_response(201, {'items': items})
        result = self.proxy._collect('post', response)
        self.assertEqual(items, result)

    def test_paginated_get_accumulates_all_pages(self):
        page1 = {
            'items': [{'id': 'a'}],
            'links': [{'rel': 'next', 'href': 'https://host/api/v310/res?page=2'}]
        }
        page2 = {'items': [{'id': 'b'}], 'links': []}

        response1 = _make_response(200, page1)
        response2 = _make_response(200, page2)

        with mock.patch.object(self.proxy, '_send_raw', return_value=response2):
            result = self.proxy._collect('get', response1)

        self.assertEqual([{'id': 'a'}, {'id': 'b'}], result)

    def test_empty_response_returns_none(self):
        response = mock.Mock(spec=requests.Response)
        response.status_code = 200
        response.ok = True
        response.content = b''
        response.request = mock.Mock()
        response.request.method = 'DELETE'
        result = self.proxy._collect('delete', response)
        self.assertIsNone(result)

    def test_normalised_delete_404_returns_none(self):
        """_collect returns None when _check_error normalises a DELETE 404."""
        content = {'causeDetails': 'Failed to lookup resource abc'}
        response = _make_response(404, content, method='DELETE')
        result = self.proxy._collect('delete', response)
        self.assertIsNone(result)


class VmstoreProxyExecuteTestCase(test.TestCase):
    """Tests for _execute retry loop and _execute_strict."""

    def setUp(self):
        set_vmstore_overrides()
        super(VmstoreProxyExecuteTestCase, self).setUp()
        mock_conf = mock.Mock()
        mock_conf.vmstore_rest_protocol = 'https'
        mock_conf.vmstore_rest_address = '192.168.1.1'
        mock_conf.vmstore_rest_port = 443
        mock_conf.vmstore_user = 'admin'
        mock_conf.vmstore_password = 'secret'
        mock_conf.vmstore_rest_retry_count = 2
        mock_conf.vmstore_refresh_retry_count = 1
        mock_conf.vmstore_rest_backoff_factor = 0
        mock_conf.vmstore_rest_connect_timeout = 30
        mock_conf.vmstore_rest_read_timeout = 300
        mock_conf.driver_ssl_cert_verify = False
        mock_conf.driver_ssl_cert_path = None
        with mock.patch('requests.Session'):
            self.proxy = api.VmstoreProxy('nfs', 'b', mock_conf)
        # Disable backoff sleep in tests
        self.proxy.delay = mock.Mock(return_value=0)
        self.proxy.update_host = mock.Mock()

    def test_execute_returns_on_first_success(self):
        expected = [{'id': '1'}]
        with mock.patch.object(self.proxy, '_attempt', return_value=expected):
            result = self.proxy._execute('get', 'some/path')
        self.assertEqual(expected, result)

    def test_execute_retries_on_failure_then_succeeds(self):
        error = api.VmstoreException('transient')
        success = [{'id': '1'}]
        with mock.patch.object(self.proxy, '_attempt',
                               side_effect=[error, success]):
            result = self.proxy._execute('get', 'some/path')
        self.assertEqual(success, result)

    def test_execute_raises_after_exhausting_retries(self):
        error = api.VmstoreException('permanent')
        with mock.patch.object(self.proxy, '_attempt', side_effect=error):
            self.assertRaises(
                api.VmstoreException,
                self.proxy._execute, 'get', 'some/path')

    def test_execute_calls_update_host_between_retries(self):
        error = api.VmstoreException('transient')
        success = {'result': 'ok'}
        with mock.patch.object(self.proxy, '_attempt',
                               side_effect=[error, success]):
            self.proxy._execute('get', 'path')
        self.proxy.update_host.assert_called_once()

    def test_execute_strict_raises_immediately_on_error(self):
        bad_response = _make_response(503, {'message': 'unavailable'})
        with mock.patch.object(self.proxy, '_send_raw', return_value=bad_response):
            self.assertRaises(
                api.VmstoreException,
                self.proxy._execute_strict, 'post', 'cinder/host/refresh')

    def test_execute_strict_returns_on_success(self):
        good_response = _make_response(200, {'status': 'ok'})
        with mock.patch.object(self.proxy, '_send_raw', return_value=good_response):
            result = self.proxy._execute_strict('post', 'cinder/host/refresh')
        self.assertEqual({'status': 'ok'}, result)
