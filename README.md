Vmstore Openstack cinder driver (NFS).

# Prerequisites
Install NFS client
```bash
apt install nfs-common
```

# Installation
Clone Vmstore driver for the desired version:
```
git clone -b <branch> https://github.com/Tintri/vmstore-cinder-driver.git
```

Create vmstore folder and copy the files
```bash
mkdir -p /usr/lib/python2.7/dist-packages/cinder/volume/drivers/vmstore
cp -r vmstore-cinder-driver/* /usr/lib/python2.7/dist-packages/cinder/volume/drivers/vmstore/
```

Restart Openstack Cinder service
```bash
sudo systemctl restart openstack-cinder-volume.service
```
