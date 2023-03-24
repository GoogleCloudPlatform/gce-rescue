# GCE Rescue

[![test badge](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml/badge.svg?branch=main&event=push)](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml?query=branch%3Amain+event%3Apush)

GCE Rescue is a tool that can help you fix problems with a virtual machine (VM) instance in Google Cloud. It can be issues with the boot disk or any software-related problems that may prevent the VM from starting up or functioning as intended.

In short, the rescue for the VM occurs as follows:

1. A backup of the current boot disk will be created
2. A temporary boot disk will be created
3. The VM will be booted from a temporary disk, and the current boot disk will be attached for repair
4. You will perform the necessary actions with the broken disk to fix it
5. After that, the VM will be rebooted in normal mode with a fixed old disk

GCE Rescue has an important advantage over creating a duplicate VM instance to repair a faulty boot disk - you don't need to waste time recreating the environment (VPC Networks, Firewalls, VPNs, routes, etc.) because you're using an existing and already configured instance.

> **Warning**
> **GCE Rescue is not an official Google Cloud product.**
> **Google Cloud does not accept any responsibility for the use of this tool.**
> The Google Cloud Support team maintains this repository, but the product is experimental and, therefore, it can be unstable.

## Requirements

To install and use GCE Rescue, you must have:

1. Python environment >= 3.9 ([read more](https://docs.python.org/3.9/tutorial/index.html))
2. `gcloud` CLI ([read more](https://cloud.google.com/sdk/docs/install-sdk))

## Installation

To install GCE Rescue, follow these steps:

1. Clone the git repository to your local machine:

   ```bash
   git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
   ```

2. Navigate to the `gce-rescue` directory:

   ```bash
   cd gce-rescue/
   ```

3. To install GCE Rescue, select one of the following options: 

   * Install GCE Rescue globally

   ```bash
   sudo python3 setup.py install
   ```

   * Install GCE Rescue locally

   ```bash
   python3 setup.py install --user
   ```

> **Note**
>
> If you cannot find the `gce-rescue` executable after your install GCE Rescue, add the Python Library to your `PATH`:
> ```bash
> export PATH=$PATH:$(python3 -m site --user-base)/bin
> ```
> Read more about [how to permanently set $PATH on Linux/Unix](https://stackoverflow.com/q/14637979).

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

Try --help to get a list of all flags.
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


> The log files contain important information about the initial state of the VM instance that may be required to manually restore it.

---

## Example

This section demonstrates the use of GCE Rescue with the example of `test` VM in the `europe-central2-a` region.

1. Preparation and booting in rescue mode.

   The preparation process includes the following steps:
      * The current boot disk will be disconnected
      * A snapshot of the current disk will be created as a backup
      * A new temporary boot disk will be attached to the VM
      * The old disk will be attached to the VM as non-boot

   To start the process, run the following command
      ```bash
      gce-rescue --zone europe-central2-a --name test
      ```

   The successful output will be as follows
      <pre>
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
          https://ssh.cloud.google.com/v2/ssh/projects/my-project/zones/europe-central2-a/instances/test? authuser=0&hl=en_US&useAdminProxy=true&troubleshoot4005Enabled=true
      </pre>

2. Verification of the preparatory actions.

   * Check that the snapshot of the old disk was successfully created
      ```bash
      gcloud compute snapshots list \
         --filter="sourceDisk='europe-central2-a/disks/test'" \
         --format="table(name, sourceDisk, status, creationTimestamp)"
      ```

      The result should be similar to the following
         <pre>
         NAME               SRC_DISK                        STATUS  CREATION_TIMESTAMP
         test-1668043020    europe-central2-a/disks/test    READY   2022-11-10T14:17:00.000+01:00
         </pre>

   * Check that both disks were attached to the VM
      ```bash
      gcloud compute instances describe --zone europe-central2-a test \
         --format="table(disks:format='table(deviceName,boot)')"
      ```

      The result should be similar to the following
         <pre>
         DEVICE_NAME                   BOOT
         linux-rescue-disk-1668043020  True
         test                          False
         </pre>

3. Connection to the VM and repair work.

   > **Note**
   >
   > The `-rescue` suffix was added to the hostname of the VM, to highlight that you are currently in rescue mode.

   Once your VM instance is in rescue mode you can connect to it in any familiar way.

   To make sure that the block device of the old disk was mounted to the `/mnt/sysroot` directory, use the following command
      ```bash
      df -h /mnt/sysroot
      ```

   The result should be similar to the following
   <pre>
   Filesystem      Size  Used Avail Use% Mounted on
   /dev/sdb1       9.7G  2.0G  7.2G  22% /mnt/sysroot
   </pre>

   At this point you should take the necessary actions to restore your faulty boot disk.

   > **Warning**
   >
   > Keep in mind that the old disk is now mounted to a non-root directory. Consider temporarily changing the root directory to the old disk's directory to avoid mistakes with file paths using the following command
   > ```bash
   > chroot /mnt/sysroot
   > ```
   > Read more about [`chroot`](https://man7.org/linux/man-pages/man2/chroot.2.html) command.

   When finished, you can just disconnect from the VM.

4. Restoring to the normal mode.

   To return the VM to the original state, run the first command again
      ```bash
      gce-rescue --zone europe-central2-a --name test
      ```
   The successful output will be as follows
      <pre>
      The instance "test" is currently configured to boot as rescue mode since 2022-11-10 13:17:00.
      Would you like to restore the original configuration ? [y/N]: y
      Restoring VM...
      ┌── Configuring...
      │   └── Progress 4/4 [█████████████████████████████████████████████████████████████]
      ├── Configurations finished.
      └── The instance test was restored! Use the snapshot below if you need to restore the modification made while the instance was in rescue mode.
       Snapshot name: test-1668043020
       More information: https://cloud.google.com/compute/docs/disks/restore-snapshot
      </pre>

   > **Note**
   >
   > Since the snapshot was created before the recovery process, you can restore the original state of the disk if necessary.
   > Read more about how to [Restore from a snapshot](https://cloud.google.com/compute/docs/disks/restore-snapshot).

5. Validation of rescue results.

   * Check that only the old disk is now connected to the VM
      ```bash
      gcloud compute instances describe --zone europe-central2-a test \
         --format="table(disks:format='table(deviceName,boot)')"
      ```

      The result should be similar to the following
         <pre>
         DEVICE_NAME  BOOT
         test         True
         </pre>

   * **Connect to the VM and make sure everything is working as expected**

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
