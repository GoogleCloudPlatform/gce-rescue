# GCE Rescue #
[![test badge](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml/badge.svg?branch=main&event=push)](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml?query=branch%3Amain+event%3Apush)


This page shows you how to rescue a virtual machine (VM) instance by using GCE Rescue. 

With GCE Rescue, you can boot the VM instance using a temporary boot disk to fix any problem that may be stopping the VM instance. Specifically, GCE Rescue uses a temporary Linux image as the VM instance’s boot disk to let you do maintenance on the faulty boot disk while it is in rescue mode.

When running GCE Rescue, it creates a snapshot of the existing boot disk for backup. 

After you’ve fixed the faulty disk, you can then restore the original configuration by running GCE Rescue again to reboot the VM instance in normal mode again.

The advantage of using GCE Rescue is that it uses the resources already configured on the VM instance, such as networking, VPC firewalls or routes, to restore the faulty boot disk instead of creating a duplicate VM instance to restore the faulty boot disk. 

>Note: **`GCE Rescue is not an officially supported Google Cloud product`**. The Google Cloud Support team maintains this repository, but the product is experimental and, therefore, it can be unstable.

## Requirements

To install and use GCE Rescue, you must have:

1. Python environment >= 3.7 ([read more](https://docs.python.org/3.7/tutorial/index.html))
2. `gcloud` CLI ([read more](https://cloud.google.com/sdk/docs/install-sdk))

> **Note**
>
> The requirement of Python >= 3.7 was inherited from the [`google-api-python-client`](https://github.com/googleapis/google-api-python-client/tree/main#supported-python-versions) package.
>
> Despite the fact that `gce-rescue` can be installed in some ways with a Python version < 3.7 and may work, this is not recommended and is not supported.

## Installation ##


To install GCE Rescue, follow these steps:

1. Clone the git repository to your local machine:

``` 
$ git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
```

2. Navigate to the `cd/gce-rescue` folder:

```
$ cd gce-rescue/
```

3. To install GCE Rescue, select one of the following options: 

* Install GCE Rescue globally.

``` 
$ sudo python3 setup.py install
```

* Install GCE Rescue locally.

```
$ python3 setup.py install --user
```

> Note: If you cannot find the gce-rescue executable after your install 
GCE Rescue, add the Python Library to your PATH:
>
```
$ export PATH=$PATH:$(python3 -m site --user-base)/bin
```

---

## Usage ##

```
gce-rescue --help
usage: gce-rescue [-h] [-p PROJECT] -z ZONE -n NAME [-d] [-f] [--skip-snapshot]

GCE Rescue v0.4-beta - Set/Reset GCE instances to boot in rescue mode.

optional arguments:
  -h, --help            show this help message and exit
  -p PROJECT, --project PROJECT
                        The project-id that has the instance.
  -z ZONE, --zone ZONE  Zone where the instance is created.
  -n NAME, --name NAME  Instance name.
  -d, --debug           Print to the log file in debug leve
  -f, --force           Don't ask for confirmation.
  --skip-snapshot       Skip backing up the disk using a snapshot.
```

- ### --zone ### 
  - The instances zone. (REQUIRED)
- ### --name ###
  - The instance name (not instance ID). (REQUIRED)
- ### --project ###
  - The project-id of the faulty instance. (OPTIONAL)
- ### --force ###
  - Do not ask for confirmation. It can be useful when running from a script.
- ### --debug ###
  - If provided, the log output will be set to DEBUG level. (OPTIONAL)
  - The log file will be created on ./ containing the VM name and timestamp on the name, that can be used to help to troubleshoot failed executions as well as to manually recover the instance's original configuration, if necessary.

  - > The log files contain important information about the initial state of the VM instance that may be required to manually restore it.


- ### --skip-snapshot ###
  - Skip the snapshot creation. (OPTIONAL) 
  - Before setting your instance in rescue mode, GCE Rescue will always create a snapshot of your boot disk before taking any action. For some users this might be time consuming and not always necessary. Use this argument if you want to skip this step.

---

## Examples ##

```shellscript
$ gce-rescue --zone europe-central2-a --name test

This option will boot the instance test in RESCUE MODE.
If your instance is running it will be rebooted.
Do you want to continue [y/N]: y
Starting...
┌── Configuring...
│   └── Progress 6/6 [█████████████████████████████████████████████████████████████]
├── Configurations finished.
└── Your instance is READY! You can now connect your instance "test" via:
  1. CLI. (add --tunnel-through-iap if necessary)
    $ gcloud compute ssh test --zone=europe-central2-a --project=my-project --ssh-flag="-o StrictHostKeyChecking=no"
  OR
  2. Google Cloud Console:
    https://ssh.cloud.google.com/v2/ssh/projects/my-project/zones/europe-central2-a/instances/test?authuser=0&hl=en_US&useAdminProxy=true&troubleshoot4005Enabled=true

```

Once your VM instance is in rescue mode you can connect via SSH, as you normally would do.

Notice that `-rescue` was added to your hostname, to highlight that you are currently in rescue mode.

The original boot disk should be automatically mounted on `/mnt/sysroot`:

```shellscript
user@test-rescue:~$ lsblk
NAME    MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda       8:0    0   10G  0 disk
├─sda1    8:1    0  9.9G  0 part /
├─sda14   8:14   0    3M  0 part
└─sda15   8:15   0  124M  0 part /boot/efi
sdb       8:16   0   30G  0 disk
├─sdb1    8:17   0    2M  0 part
├─sdb2    8:18   0   20M  0 part
└─sdb3    8:19   0   30G  0 part /mnt/sysroot

user@test-rescue:~$ chroot /mnt/sysroot
```

At this point you should take the necessary actions to restore your faulty boot disk.

When finished you can close your SSH connections and restore the VM instance to the original mode, by running the same command again:

```shellscript
$ gce-rescue --zone europe-central2-a --name test

The instance "test" is currently configured to boot as rescue mode since 2022-11-01 12:05:08.
Would you like to restore the original configuration ? [y/N]: y
Restoring VM...
- Configuring...
 \- Progress 4/4 [█████████████████████████████████████████████████████████████]
- Configurations finished.
- The instance test was restored! Use the snapshot below if you need to restore the modification made while the instance was in rescue mode.
 Snapshot name: test-1668009968
 More information: https://cloud.google.com/compute/docs/disks/restore-snapshot

```

> A snapshot was taken before setting the instance in Rescue Mode and can be used to recover the disk status.
You will be able to idenfiy the snapshot name, like in the example above is: `test-1668009968`.

#
# You are ready !

When you connect again you will noticed the your instance is back to the normal mode:

```shellscript
user@test:~> uptime
 12:24:18  up   0:05,  1 user,  load average: 0.00, 0.00, 0.00

user@test:~> lsblk
NAME   MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS
sda      8:0    0  30G  0 disk
├─sda1   8:1    0   2M  0 part
├─sda2   8:2    0  20M  0 part /boot/efi
└─sda3   8:3    0  30G  0 part /

user@test:~>
```

---

## Authentication ##

This script makes use of Application Default Credentials (ADC). Make sure you have gcloud installed and your ADC updated.

You can find more information on: https://cloud.google.com/docs/authentication/provide-credentials-adc

----

## Permissions ##

This is the list of the minimal IAM permissions required.

| Description | Permissions|
|----------:|----------|
| Start and stop instance | compute.instances.stop <br/> compute.instances.start |
| Create and remove disk | compute.instances.attachDisk on the instance <br/> compute.instances.detachDisk on the instance <br/> compute.images.useReadOnly on the image if creating a new root persistent disk <br/> compute.disks.use on the disk if attaching an existing disk in read/write mode  <br/> compute.disks.setLabels on the disk if setting labels |
| Create snapshot | compute.snapshots.create on the project <br/> compute.disks.createSnapshot on the disk |
| Configure metadata | compute.instances.setMetadata if setting metadata  <br/> compute.instances.setLabels on the instance if setting labels |

----

## Contact ##

### GCE Rescue Team ###
gce-rescue-dev@google.com
