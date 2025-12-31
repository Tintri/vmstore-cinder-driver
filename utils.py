# Copyright 2025 DDN, Inc. All rights reserved.
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

"""Vmstore driver utilities."""

from urllib.parse import urlparse

from keystoneauth1.exceptions.catalog import EndpointNotFound
from keystoneauth1.identity import v3
from keystoneauth1 import session
from keystonemiddleware import auth_token
from oslo_config import cfg
from oslo_log import log as logging

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

_cached_hostname = None
_keystone_opts_registered = False


def _ensure_keystone_opts():
    """Register keystone_authtoken options if not already registered."""
    global _keystone_opts_registered
    if _keystone_opts_registered:
        return

    # Register options from keystonemiddleware
    for group, options in auth_token.list_opts():
        keystone_opts = list(options)
        for opt in keystone_opts:
            try:
                CONF.register_opt(opt, group=group)
            except cfg.DuplicateOptError:
                pass  # Already registered

    # Explicitly register required auth options in keystone_authtoken group
    required_opts = [
        cfg.StrOpt('auth_url'),
        cfg.StrOpt('username'),
        cfg.StrOpt('password', secret=True),
        cfg.StrOpt('project_name'),
        cfg.StrOpt('user_domain_name'),
        cfg.StrOpt('project_domain_name'),
    ]
    for opt in required_opts:
        try:
            CONF.register_opt(opt, group='keystone_authtoken')
        except cfg.DuplicateOptError:
            pass  # Already registered

    _keystone_opts_registered = True


def get_keystone_hostname():
    """Get the Keystone service hostname.

    Attempts to resolve the Keystone hostname from the service catalog.
    Falls back to parsing the auth_url from configuration if the
    service catalog lookup fails.

    :returns: The Keystone hostname or None if resolution fails.
    :rtype: str or None
    """
    global _cached_hostname
    if _cached_hostname:
        return _cached_hostname

    _ensure_keystone_opts()

    try:
        auth = v3.Password(
            auth_url=CONF.keystone_authtoken.auth_url,
            username=CONF.keystone_authtoken.username,
            password=CONF.keystone_authtoken.password,
            project_name=CONF.keystone_authtoken.project_name,
            user_domain_name=CONF.keystone_authtoken.user_domain_name,
            project_domain_name=CONF.keystone_authtoken.project_domain_name
        )

        sess = session.Session(auth=auth)

        keystone_url = sess.get_endpoint(
            service_type='identity',
            interface='public'
        )
        hostname = urlparse(keystone_url).hostname
        LOG.debug("Resolved Keystone hostname via service catalog: %(host)s",
                  {'host': hostname})
        _cached_hostname = hostname
        return hostname

    except EndpointNotFound:
        LOG.warning("Keystone endpoint not found in service catalog, "
                    "falling back to config auth_url.")
    except Exception as e:
        LOG.warning("Error resolving Keystone endpoint dynamically: %(err)s",
                    {'err': e})

    # Fallback from config
    try:
        fallback_hostname = urlparse(
            CONF.keystone_authtoken.auth_url
        ).hostname
        LOG.debug("Parsed Keystone hostname from config: %(host)s",
                  {'host': fallback_hostname})
        _cached_hostname = fallback_hostname
        return fallback_hostname
    except Exception as e:
        LOG.error("Failed to parse Keystone hostname from config: %(err)s",
                  {'err': e})
        return None
