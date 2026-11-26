# Read-only lab helper for test/lab/volume-extend/run.sh: prints the Cinder record of
# volumes (status, size, format record, file name, share, host). Copied from the
# volume-migration suite, with the size added, so each suite runs on its own.
#
# Runs on a cinder-volume host with the service's own Python and environment, for
# example on PCD:
#   (set -a; . /opt/pf9/pf9-cindervolume-base/cinder.env; set +a;
#    /opt/pf9/pf9-cindervolume-base/bin/python volume_record.py \
#        --conf-dir /opt/pf9/etc/pf9-cindervolume-base/conf.d <volume-id>...)
import argparse

from oslo_config import cfg

from cinder.common import config  # noqa: F401  registers options
from cinder import context
from cinder import objects


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf-dir', required=True)
    parser.add_argument('volume_ids', nargs='+')
    args = parser.parse_args()

    objects.register_all()
    cfg.CONF([], project='cinder', default_config_dirs=[args.conf_dir])
    ctx = context.get_admin_context()

    for vid in args.volume_ids:
        v = objects.Volume.get_by_id(ctx, vid)
        print('VOL', vid, 'status=%s' % v.status, 'size=%s' % v.size,
              'record=%s' % (v.admin_metadata or {}).get('format'),
              'name_id=%s' % v.name_id, 'share=%s' % v.provider_location,
              'host=%s' % v.host)


if __name__ == '__main__':
    main()
