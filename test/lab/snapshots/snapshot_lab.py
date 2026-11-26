# Lab helper for the snapshot delete lab test (VME-34): test/lab/snapshots/run.sh.
#
# Runs on a cinder-volume host with the service's own Python and environment,
# for example on PCD:
#   (set -a; . /opt/pf9/pf9-cindervolume-base/cinder.env; set +a;
#    /opt/pf9/pf9-cindervolume-base/bin/python snapshot_lab.py \
#        --conf-dir /opt/pf9/etc/pf9-cindervolume-base/conf.d \
#        --prefix vme34 snapshot <snapshot-id>)
#
# VMstore calls use the installed driver's own REST client, built from the
# backend's section of the service configuration the way cinder-volume builds
# it. Credentials are read in place and never printed, logged or written.
#
# Output lines start with a tag (VERSION, SNAP, ARRAY, DELETED, ERROR,
# REFUSED); the last line is END when the command completed.
import argparse
import importlib
import logging
import sys

from oslo_config import cfg
from oslo_utils import importutils

from cinder.common import config  # noqa: F401  registers options
from cinder import context
from cinder import objects
from cinder.volume import configuration
from cinder.volume import driver as volume_driver


def backend_of(snapshot):
    """Backend section of the snapshot's volume, from host@backend#pool."""
    host = snapshot.volume.host or ''
    if '@' not in host:
        raise RuntimeError('volume %s has no backend in host %r'
                           % (snapshot.volume_id, host))
    return host.split('@', 1)[1].split('#', 1)[0]


def backend_conf(backend):
    """The backend's configuration, as the volume manager and driver see it."""
    # Imported here: the volume manager needs objects.register_all() first.
    from cinder.volume import manager

    if backend not in cfg.CONF.list_all_sections():
        raise RuntimeError('no [%s] section in the service configuration'
                           % backend)
    conf = configuration.Configuration(manager.volume_backend_opts,
                                       config_group=backend)
    conf.append_config_values(volume_driver.volume_opts)
    return conf


def driver_modules(conf):
    """Driver class from volume_driver, plus api and options next to it."""
    cls = importutils.import_class(conf.volume_driver)
    package = cls.__module__.rsplit('.', 1)[0]
    api = importlib.import_module(package + '.api')
    options = importlib.import_module(package + '.options')
    return cls, api, options


def vmstore_client(backend):
    conf = backend_conf(backend)
    _cls, api, options = driver_modules(conf)
    conf.append_config_values(options.VMSTORE_NFS_OPTS)
    # Same constructor arguments in every 3.0.x release.
    return api.VmstoreProxy('nfs', backend, conf)


def snapshot_line(snap):
    return ('SNAP %s name=%s status=%s provider_id=%s volume=%s deleted=%s'
            % (snap.id, snap.display_name, snap.status, snap.provider_id,
               snap.volume_id, snap.deleted))


def array_uuids(client, name):
    """UUIDs of VMstore snapshots whose description is exactly name."""
    uuids = []
    for item in client.snapshots.list({'contain': name}) or []:
        if isinstance(item, dict) and item.get('description') == name:
            uuids.append(item['uuid']['uuid'])
    return uuids


def guard(snap, prefix):
    name = snap.display_name or ''
    if not prefix or not name.startswith(prefix + '-'):
        print('REFUSED snapshot %s is named %r, not %s-*: not a test object'
              % (snap.id, name, prefix))
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--conf-dir', required=True)
    parser.add_argument('--conf-file', action='append', default=[])
    parser.add_argument('--backend',
                        help='backend section; default: from the snapshot '
                             "volume's host")
    parser.add_argument('--prefix', default='',
                        help='test object prefix; array-delete and '
                             'clear-provider-id refuse other snapshots')
    parser.add_argument('command', choices=[
        'driver-version', 'snapshot', 'array-list', 'array-delete',
        'clear-provider-id'])
    parser.add_argument('snapshot_id', nargs='?')
    args = parser.parse_args()
    if args.command != 'driver-version' and not args.snapshot_id:
        parser.error('%s needs a snapshot id' % args.command)
    if args.command == 'driver-version' and not args.backend:
        parser.error('driver-version needs --backend')

    # The driver and Cinder log through std logging; with nothing configured,
    # warnings would reach stderr. Silence them all: this helper's output is
    # only its own tagged lines.
    logging.disable(logging.CRITICAL)

    objects.register_all()
    cfg.CONF([], project='cinder',
             default_config_files=args.conf_file or None,
             default_config_dirs=[args.conf_dir])
    # read_deleted: the run checks the array after Cinder deleted the snapshot
    # and its volume.
    ctx = context.get_admin_context(read_deleted='yes')

    if args.command == 'driver-version':
        cls, _api, _options = driver_modules(backend_conf(args.backend))
        module = sys.modules[cls.__module__]
        print('VERSION %s class=%s.%s file=%s'
              % (getattr(cls, 'VERSION', '?'), cls.__module__, cls.__name__,
                 getattr(module, '__file__', '?')))
        print('END')
        return 0

    snap = objects.Snapshot.get_by_id(ctx, args.snapshot_id)
    name = snap.name  # snapshot-<id>, the description the driver gives VMstore

    if args.command == 'snapshot':
        print(snapshot_line(snap))
        print('END')
        return 0

    if args.command == 'clear-provider-id':
        if not guard(snap, args.prefix):
            return 2
        if snap.deleted:
            print('ERROR snapshot %s is deleted' % snap.id)
            return 1
        live = objects.Snapshot.get_by_id(context.get_admin_context(),
                                          args.snapshot_id)
        live.provider_id = None
        live.save()
        print(snapshot_line(live))
        print('END')
        return 0

    client = vmstore_client(args.backend or backend_of(snap))

    if args.command == 'array-list':
        for uuid in array_uuids(client, name):
            print('ARRAY %s' % uuid)
        print('END')
        return 0

    # array-delete
    if not guard(snap, args.prefix):
        return 2
    for uuid in array_uuids(client, name):
        client.snapshots.delete(uuid)
        print('DELETED %s' % uuid)
    print('END')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:  # report one line; never a traceback
        text = ' '.join(str(error).split())
        print('ERROR %s: %s' % (type(error).__name__, text[:400]))
        sys.exit(1)
