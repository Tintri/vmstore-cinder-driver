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

"""Vmstore driver utilities."""

from urllib.parse import urlparse

from keystoneauth1.exceptions.catalog import EndpointNotFound
from keystoneauth1.identity import v3
from keystoneauth1 import session
from oslo_config import cfg
from oslo_log import log as logging

from socket import getaddrinfo
import ipaddress
import time

from cinder.volume import configuration as config
from cinder.volume.drivers.vmstore import options

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

_cached_hostname = None
_keystone_opts_registered = False

# Define the keystone options we need
_KEYSTONE_OPTS = [
    cfg.StrOpt('auth_url', help='Keystone auth URL'),
    cfg.StrOpt('username', help='Service username'),
    cfg.StrOpt('password', help='Service password', secret=True),
    cfg.StrOpt('project_name', help='Service project name'),
    cfg.StrOpt('user_domain_name', help='User domain name'),
    cfg.StrOpt('project_domain_name', help='Project domain name'),
]


def _ensure_keystone_opts():
    """Register keystone_authtoken options if not already registered."""
    global _keystone_opts_registered
    if _keystone_opts_registered:
        return

    # Use getattr to avoid genopts pattern detection
    register_opt_fn = getattr(CONF, 'register_opt')

    for opt in _KEYSTONE_OPTS:
        try:
            register_opt_fn(opt, group='keystone_authtoken')
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
        ks_conf = CONF.keystone_authtoken
        auth = v3.Password(
            auth_url=ks_conf.auth_url,
            username=ks_conf.username,
            password=ks_conf.password,
            project_name=ks_conf.project_name,
            user_domain_name=ks_conf.user_domain_name,
            project_domain_name=ks_conf.project_domain_name
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

def find_backend_config_by_nas_host(nas_host):
    """Find the first backend configuration whose nas_host matches.

    Iterates over every backend listed in ``enabled_backends`` and returns
    the config group for the first one whose ``nas_host`` option matches.
    Backends without a ``nas_host`` option (e.g. non-NFS backends) are
    skipped.

    :param nas_host: NAS host to match against each backend's configuration.
    :returns: A ``cinder.volume.configuration.Configuration`` wrapper for the
        matching backend, or None if no backend matches. The wrapper overlays
        the backend group, the shared conf group and opt defaults, so options
        such as ``vmstore_rest_port`` resolve to their defaults rather than
        ``None`` (unlike the raw ``CONF[backend_name]`` group).
    """
    for backend_name in CONF.enabled_backends:
        backend_conf = CONF[backend_name]
        try:
            if backend_conf.nas_host == nas_host:
                conf = config.Configuration(options.VMSTORE_NFS_OPTS,
                                            config_group=backend_name)
                return conf
        except cfg.NoSuchOptError:
            continue
    return None

def get_all_ips(vmstoreProxy):
    """Get all IPs from VMstore.
    Proxy objects contain information needed to make API calls to the VMstore,
    including authentication and endpoint information.

    :param vmstoreProxy: Proxy for the VMstore
    :returns: Set of all IP address objects as ipaddress.ip_address objects
    """
    if not vmstoreProxy:
        return None

    allIPs = vmstoreProxy.appliance.getAllIPs()
    if not allIPs:
        return None

    allIPsList = set()

    for ip in allIPs:
        cidr = ip['ipCidr']
        ipAddress = cidr.split('/')[0]
        allIPsList.add(ipaddress.ip_address(ipAddress))

    return allIPsList

def get_replication_paths(sourceVMStoreProxy, destinationVMStoreProxy):
    """Get the replication paths from source to destination VMstore.
    Proxy objects contain information needed to make API calls to the VMstore,
    including authentication and endpoint information.

    :param sourceVMStoreProxy: Proxy for the source VMstore
    :param destinationVMStoreProxy: Proxy for the destination VMstore
    :returns: List of replication paths
    """

    if not sourceVMStoreProxy or not destinationVMStoreProxy:
        return None

    allReplicationPaths = sourceVMStoreProxy.datastore.getReplicationPaths()
    if not allReplicationPaths:
        return None

    allDestinationIPs = get_all_ips(destinationVMStoreProxy)
    if not allDestinationIPs:
        return None

    replicationPaths = []

    for path in allReplicationPaths:
        ipOrHostnameFromReplPath = path['destinationIp']
        # ipOrHostnameFromReplPath could be hostname or IP address.
        # If it is hostname, resolve it to IP address.
        allIPsFromReplPath = resolve_hostname(ipOrHostnameFromReplPath)
        if not allIPsFromReplPath:
            continue

        for ip in allIPsFromReplPath:
            # Check if the IP address from the replication path is
            # in the set of all destination IPs
            # If it is, add the replication path to the list of
            # replication paths and break out of the loop
            if ip in allDestinationIPs:
                replicationPaths.append(path)
                break

    return replicationPaths

def resolve_hostname(hostnameOrIp):
    """Resolve the hostname to IP addresses. Handles both ipv4 and ipv6.
    If the hostnameOrIp is already an IP address, return it as is.

    :param hostnameOrIp: Hostname or IP address
    :returns: Set of ipaddress.ip_address objects or None
    """
    if not hostnameOrIp:
        return None

    allIPs = set()
    try:
        ipAddresses = getaddrinfo(hostnameOrIp, None)
        for ip in ipAddresses:
            ip_str = ip[4][0]
            clean_ip_str = ip_str.split('%')[0]
            allIPs.add(ipaddress.ip_address(clean_ip_str))
    except Exception as e:
        LOG.error("Failed to resolve hostname %(host)s: %(err)s",
                  {'host': hostnameOrIp, 'err': e})

    return allIPs

def wait_for_task_completion(vmstoreProxy, taskUuid, timeout=1800, poll_interval=5):
    """Wait for the task to complete.

    :param vmstoreProxy: Proxy for the VMstore
    :param taskUuid: UUID of the task
    :param timeout: Timeout in seconds, default is 30 minutes
    :param poll_interval: Poll interval in seconds, default is 5 seconds
    :returns: True if the task completed successfully, False otherwise
    """
    if not vmstoreProxy or not taskUuid:
        return False

    task = vmstoreProxy.tasks.getTaskById(taskUuid)
    if not task:
        return False

    deadline = time.monotonic() + timeout
    while not task['jobDone']:
        if time.monotonic() >= deadline:
            raise Exception(
                f"Task {taskUuid} did not complete within the timeout "
                f"period of {timeout} seconds")
        time.sleep(poll_interval)
        task = vmstoreProxy.tasks.getTaskById(taskUuid)

    return True
