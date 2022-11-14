# GCE Rescue # 

The core idea is to make analogies to the steps taken to rescue a physical instance, where a rescue boot disk is plugged into the machine, changing the order of the boot disks and using the faulty disk as secondary.

Once the user is in rescue mode, they can take the steps necessary to change/restore any configuration on the target disk. This script will attempt to automount the faulty disk in /mnt/sysroot.

At the end, the user runs this script again to restore the configuration to boot from the original (now recovered) disk.

The main advantage to using this approach, rather than creating a 2nd instance, is to make use of the resources already configured for the VM, such as networking, VPC firewalls and routes, policies, permissions, etc.

> IMPORTANT: *This is not an officially supported Google product.*
> Note that this is not an officially supported Google product, but a community effort. The Google Cloud Support team maintains this code and we do our best to avoid causing any problems in your projects, but we give no guarantees to that end.


## Installation ##

```shellscript
$ git clone https://github.com/googlecloudplatform/gce-rescue.git
$ cd gce-rescue/
$ sudo python3 setup.py install
```

If you want to install locally, only for your user, you can avoid sudo:
```shellscript
$ python3 setup.py install --user
```

If you are unable to find the `gce-rescue` executable, you may need to add the Python Library to your PATH. 

Example:

```shellscript
$ export PATH=$PATH:$(python3 -m site --user-base)/bin
```
> NOTE: 
>
> Add the line above to your ~/.profile to make it persistent.
 

---

## Usage ##

```
$ gce-rescue --help

       USAGE: gce-rescue [flags]
flags:

./gce-rescue.py:
  --[no]debug: Print to the log file in debug level.
    (default: 'false')
  --[no]force: Don't ask for confirmation.
    (default: 'false')
  --name: Instance name.
  --project: The project-id that has the instance.
  --zone: Zone where the instance is created.
  
Try --helpfull to get a list of all flags.
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


> The log files contain important information about the initial state of the VM that may be required to manually restore it.

---

## Examples ##

```shellscript
$ gce-rescue --zone europe-central2-a --name test

This option will boot the instance test in RESCUE MODE.
If your instance is running it will be rebooted.
Do you want to continue [y/N]: y
Starting...
- Configuring...
 \- Progress 6/6 [█████████████████████████████████████████████████████████████]
- Configurations finished.
- Your instance is READY! You can now connect your instance "test" via:
  1. CLI. (add --tunnel-through-iap if necessary)
    $ gcloud compute ssh test --zone=europe-central2-a --project=my-project --ssh-flag="-o StrictHostKeyChecking=no"
  OR
  2. Google Cloud Console:
    https://ssh.cloud.google.com/v2/ssh/projects/my-project/zones/europe-central2-a/instances/test?authuser=0&hl=en_US&useAdminProxy=true&troubleshoot4005Enabled=true&troubleshoot255Enabled=true&sshTroubleshootingToolEnabled=true

$
```

Once your instance is in Rescue Mode you can connect via SSH, as you normally would do.

Notice that `-rescue` was added to your hostname to highlight that you are currently in Rescue Mode.

The original boot disk should be automatically mounted on `/mnt/sysroot`:

```shellscript
user@test-rescue:~$ lsblk
NAME    MAJ:MIN RM  SIZE RO TYPE MOUNTPOINT
sda       8:0    0   10G  0 disk
├─sda1    8:1    0  9.9G  0 part /
├─sda14   8:14   0    3M  0 part
└─sda15   8:15   0  124M  0 part /boot/efi
sdb       8:16   0   10G  0 disk
├─sdb1    8:17   0    2M  0 part
├─sdb2    8:18   0   20M  0 part
└─sdb3    8:19   0   10G  0 part /mnt/sysroot

user@test-rescue:~$ chroot /mnt/sysroot
```

At this point you should take actions, depending on your needs: recovering the boot system or just extracting files.

When finished you can close your SSH connections and restore the VM to the original mode, by running the same command again:

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
$
```

> A snapshot was taken before setting the instance in Rescue Mode and can be used to recover the disk status.
You will be able to idenfiy the snapshot name on the message above: `test-1668009968`.

# You are ready !# 

When you connect again you will noticed the your instance is back to the normal mode:

```shellscript
user@test:~> uptime
 12:24:18  up   0:05,  1 user,  load average: 0.00, 0.00, 0.00

user@test:~> lsblk
NAME   MAJ:MIN RM SIZE RO TYPE MOUNTPOINTS
sda      8:0    0  10G  0 disk
├─sda1   8:1    0   2M  0 part
├─sda2   8:2    0  20M  0 part /boot/efi
└─sda3   8:3    0  10G  0 part /

user@test:~>
```

---

## Authentication ##

This script makes use of ADC via gcloud to authenticate. Make sure you have gcloud installed and your ADC updated.

You can find more information on: https://cloud.google.com/docs/authentication/provide-credentials-adc

## Permissions ##

This is the list of the minimal IAM permissions required.

| Description | Permissions|
|----------:|----------|
| Start and stop instance | compute.instances.stop <br/> compute.instances.start |
| Create and remove disk | compute.instances.attachDisk on the instance <br/> compute.instances.detachDisk on the instance <br/> compute.images.useReadOnly on the image if creating a new root persistent disk <br/> compute.disks.use on the disk if attaching an existing disk in read/write mode  <br/> compute.disks.setLabels on the disk if setting labels |
| Create snapshot | compute.snapshots.create on the project <br/> compute.disks.createSnapshot on the disk |
| Configure metadata | compute.instances.setMetadata if setting metadata  <br/> compute.instances.setLabels on the instance if setting labels |


## Contact ##

### GCE Rescue Team ###
gce-rescue-dev@google.com
