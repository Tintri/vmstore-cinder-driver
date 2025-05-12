import logging
from urllib.parse import urlparse

from oslo_config import cfg
from keystoneauth1.identity import v3
from keystoneauth1 import session
from keystoneauth1.exceptions.catalog import EndpointNotFound
from keystonemiddleware import auth_token

CONF = cfg.CONF
LOG = logging.getLogger(__name__)

_cached_hostname = None

def _ensure_keystone_opts_registered():
    if 'keystone_authtoken' not in CONF._groups:
        for group, options in auth_token.list_opts():
            CONF.register_opts(list(options), group=group)

    # Fallback: manually register missing options if needed
    for opt_name in ['auth_url', 'username', 'password', 'project_name',
                     'user_domain_name', 'project_domain_name']:
        try:
            getattr(CONF.keystone_authtoken, opt_name)
        except cfg.NoSuchOptError:
            LOG.warning("Missing option '%s' in [keystone_authtoken]; registering manually.", opt_name)
            CONF.register_opt(cfg.StrOpt(opt_name), group='keystone_authtoken')

_ensure_keystone_opts_registered()

def get_keystone_hostname():
    global _cached_hostname
    if _cached_hostname:
        return _cached_hostname

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

        keystone_url = sess.get_endpoint(service_type='identity', interface='public')
        hostname = urlparse(keystone_url).hostname
        LOG.debug("Resolved Keystone hostname via service catalog: %s", hostname)
        _cached_hostname = hostname
        return hostname

    except EndpointNotFound:
        LOG.warning("Keystone endpoint not found in service catalog, falling back to config auth_url.")
    except Exception as e:
        LOG.warning("Error resolving Keystone endpoint dynamically: %s", e)

    # Fallback from config
    try:
        fallback_hostname = urlparse(CONF.keystone_authtoken.auth_url).hostname
        LOG.debug("Parsed Keystone hostname from config: %s", fallback_hostname)
        _cached_hostname = fallback_hostname
        return fallback_hostname
    except Exception as e:
        LOG.error("Failed to parse Keystone hostname from config: %s", e)
        return None
