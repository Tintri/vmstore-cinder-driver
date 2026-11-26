# Read-only lab helper for the image cache lab test (VME-36): test/lab/image-cache/run.sh.
#
# Runs on a cinder-volume host with the service's own Python and environment,
# for example on PCD:
#   (set -a; . /opt/pf9/pf9-cindervolume-base/cinder.env; set +a;
#    /opt/pf9/pf9-cindervolume-base/bin/python cache_lab.py \
#        --conf-dir /opt/pf9/etc/pf9-cindervolume-base/conf.d config <backend>)
#
# Reads the configuration the way cinder-volume does (config directory in sorted
# order, the backend's section with [backend_defaults] as fallback) and Cinder's
# database. Changes nothing: cache entries are listed with a read-only query,
# never with the lookup Cinder uses, which updates last_used.
#
# Commands:
#   config <backend>     CONFIG line: cache options of the backend, whether the
#                        internal tenant is set (IDs are not printed), direct URL
#                        schemes, clusters of the backend's services
#   entries <image-id>   one ENTRY line per image cache entry of the image
#   volume <id>...       one VOL line per volume
# The last line is END when the command completed.
import argparse
import logging
import sys

from oslo_config import cfg

from cinder.common import config  # noqa: F401  registers options
from cinder import context
from cinder import db
# image_utils first: it imports cinder.image.glance (which registers
# allowed_direct_url_schemes); importing glance first is a circular import.
from cinder.image import image_utils  # noqa: F401
from cinder import objects


def backend_conf(backend):
    """The backend's configuration, as the volume manager and driver see it."""
    # Imported here: the volume manager needs objects.register_all() first.
    from cinder.volume import configuration
    from cinder.volume import driver as volume_driver
    from cinder.volume import manager

    if backend not in cfg.CONF.list_all_sections():
        raise RuntimeError('no [%s] section in the service configuration'
                           % backend)
    conf = configuration.Configuration(manager.volume_backend_opts,
                                       config_group=backend)
    conf.append_config_values(volume_driver.volume_opts)
    # The cache options moved from volume_opts to image_opts in newer releases.
    conf.append_config_values(getattr(volume_driver, 'image_opts', []))
    return conf


def global_option(name):
    try:
        return getattr(cfg.CONF, name)
    except cfg.NoSuchOptError:
        return '?'


def is_set(value):
    return 'set' if value else 'unset'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf-dir', required=True)
    parser.add_argument('command', choices=['config', 'entries', 'volume'])
    parser.add_argument('args', nargs='+')
    args = parser.parse_args()

    # Cinder logs through std logging; this helper's output is only its own
    # tagged lines.
    logging.disable(logging.CRITICAL)
    objects.register_all()
    cfg.CONF([], project='cinder', default_config_dirs=[args.conf_dir])
    ctx = context.get_admin_context()

    if args.command == 'config':
        conf = backend_conf(args.args[0])
        schemes = global_option('allowed_direct_url_schemes')
        # Cache entries are per host unless the backend's services are in a
        # cluster; the cluster option is registered only by the service itself.
        clusters = sorted({
            svc.cluster_name or 'None'
            for svc in objects.ServiceList.get_all(
                ctx, {'binary': 'cinder-volume'})
            if svc.host.split('@', 1)[-1].split('#', 1)[0] == args.args[0]})
        print('CONFIG backend=%s enabled=%s max_size_gb=%s max_count=%s '
              'internal_project=%s internal_user=%s direct_url_schemes=%s '
              'clusters=%s'
              % (args.args[0], conf.safe_get('image_volume_cache_enabled'),
                 conf.safe_get('image_volume_cache_max_size_gb'),
                 conf.safe_get('image_volume_cache_max_count'),
                 is_set(global_option('cinder_internal_tenant_project_id')),
                 is_set(global_option('cinder_internal_tenant_user_id')),
                 ','.join(schemes) if isinstance(schemes, list) else schemes,
                 ','.join(clusters) or 'None'))
    elif args.command == 'entries':
        for entry in db.image_volume_cache_get_all(ctx,
                                                   image_id=args.args[0]):
            print('ENTRY volume=%s host=%s cluster=%s size=%s last_used=%s'
                  % (entry.volume_id, entry.host, entry.cluster_name,
                     entry.size,
                     entry.last_used and entry.last_used.isoformat()))
    else:
        internal = global_option('cinder_internal_tenant_project_id')
        for vid in args.args:
            v = objects.Volume.get_by_id(ctx, vid)
            print('VOL %s status=%s record=%s name_id=%s share=%s host=%s '
                  'cluster=%s internal_project=%s size=%s'
                  % (vid, v.status, (v.admin_metadata or {}).get('format'),
                     v.name_id, v.provider_location, v.host, v.cluster_name,
                     'yes' if internal and v.project_id == internal else 'no',
                     v.size))
    print('END')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:  # report one line; never a traceback
        text = ' '.join(str(error).split())
        print('ERROR %s: %s' % (type(error).__name__, text[:400]))
        sys.exit(1)
