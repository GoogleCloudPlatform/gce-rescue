# GCE Rescue V2

**Rescue unbootable Google Compute Engine VMs** - Boot into a rescue environment to fix broken configurations, corrupted files, or boot issues.

> **Beta**: Report issues at [GitHub Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)

## What's New in V2

| Area | V1 | V2 |
|------|----|----|
| **OS Support** | Linux only | Linux + Windows |
| **CLI Style** | Short flags (`-n`, `-z`) | gcloud-style (`--zone=`) |
| **Commands** | Single command (toggles) | Separate `rescue` / `restore` |
| **Architecture** | Task-based | Operation-based with rollback |

## Prerequisites

### 1. Install Python 3.9+

```bash
python3 --version   # Should be 3.9 or higher
```

### 2. Install gcloud CLI

```bash
# Check if installed
gcloud --version

# If not installed, see: https://cloud.google.com/sdk/docs/install
```

### 3. Authenticate

```bash
gcloud auth application-default login
```

### 4. Set Project (optional)

```bash
gcloud config set project YOUR_PROJECT_ID

# Or use --project flag with each command
```

### 5. IAM Permissions

Your account needs `Compute Instance Admin (v1)` role or equivalent:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="user:YOUR_EMAIL" \
  --role="roles/compute.instanceAdmin.v1"
```

## Installation

```bash
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta

# Verify
gce-rescue-v2 --version
```

## Commands

```bash
# Rescue (enter rescue mode)
gce-rescue-v2 rescue VM_NAME --zone ZONE [--project PROJECT] [--no-snapshot] [--quiet]

# Restore (exit rescue mode)
gce-rescue-v2 restore VM_NAME --zone ZONE [--project PROJECT] [--quiet]
```

| Flag | Description |
|------|-------------|
| `--zone` | GCP zone (required) |
| `--project` | GCP project (default: current gcloud config) |
| `--no-snapshot` | Skip safety snapshot (faster) |
| `--quiet` | No confirmation prompts (for automation) |
| `--format` | Output format: `table`, `json`, `yaml` |

## Example: Linux VM

**Rescue:**

```bash
$ gce-rescue-v2 rescue web-server --zone=us-central1-a

You are about to rescue instance [web-server] in zone [us-central1-a].

The following actions will be performed:
 - Stop instance [web-server].
 - Create a snapshot of your affected boot disk.
 - Create a rescue disk and boot from it.
 - Attach your affected boot disk for repair.

Do you want to continue (y/N)? y

Rescuing instance [web-server]:
 (5/5) [Stopping -> Snapshotting -> Creating rescue disk -> Starting -> Attaching affected disk] done.

Rescue mode enabled for instance [web-server].

Affected disk mounted at: /mnt/sysroot
Backup snapshot: rescue-snapshot-web-server-1736871234

Next Steps:
1. Connect to the instance:
   $ gcloud compute ssh web-server --zone=us-central1-a --project=my-project

2. Fix the issue (affected boot disk is mounted at /mnt/sysroot).

3. Restore original configuration:
   $ gce-rescue-v2 restore web-server --zone=us-central1-a --project=my-project
```

**Connect and fix:**

```bash
$ gcloud compute ssh web-server --zone=us-central1-a

user@web-server:~$ sudo nano /mnt/sysroot/etc/fstab
# Fix the issue, save and exit

user@web-server:~$ exit
```

**Restore:**

```bash
$ gce-rescue-v2 restore web-server --zone=us-central1-a

You are about to restore instance [web-server] in zone [us-central1-a] project [my-project].

The following actions will be performed:
 - Stop instance [web-server].
 - Delete the rescue disk.
 - Restore your affected boot disk as the primary boot device.
 - Start instance [web-server].

Do you want to continue (y/N)? y

Restoring instance [web-server]:
 (3/3) [Stopping -> Restoring affected disk -> Starting] done.

Instance [web-server] restored to normal operation.

Connect to the instance:
  a. Using gcloud CLI (add --tunnel-through-iap if needed):
     $ gcloud compute ssh web-server --zone=us-central1-a --project=my-project
  OR
  b. Using Google Cloud Console:
     https://ssh.cloud.google.com/v2/ssh/projects/my-project/zones/us-central1-a/instances/web-server?authuser=0&hl=en_US&useAdminProxy=true
```

## Example: Windows VM

**Rescue:**

```bash
$ gce-rescue-v2 rescue win-server --zone=us-central1-a

You are about to rescue instance [win-server] in zone [us-central1-a].

The following actions will be performed:
 - Stop instance [win-server].
 - Create a snapshot of your affected boot disk.
 - Create a rescue disk and boot from it.
 - Attach your affected boot disk for repair.

Do you want to continue (y/N)? y

Rescuing instance [win-server]:
 (5/5) [Stopping -> Snapshotting -> Creating rescue disk -> Starting -> Attaching affected disk] done.

Rescue mode enabled for instance [win-server].

Affected disk mounted at: D:\
Backup snapshot: rescue-snapshot-win-server-1736871234

Next Steps:
1. Connect via RDP:
   IP: <EXTERNAL_IP>
   User: rescue_admin
   Password: <GENERATED_PASSWORD>

2. Fix the issue (affected boot disk is mounted at D:\).

3. Restore original configuration:
   $ gce-rescue-v2 restore win-server --zone=us-central1-a --project=my-project
```

**Connect and fix:**

1. Open Remote Desktop Connection
2. Enter the IP address, username, and password shown in output
3. Your broken disk is at `D:\` - browse and fix files
4. Example: `D:\Windows\System32\config\` for registry hives

**Restore:**

```bash
$ gce-rescue-v2 restore win-server --zone=us-central1-a

You are about to restore instance [win-server] in zone [us-central1-a] project [my-project].

The following actions will be performed:
 - Stop instance [win-server].
 - Delete the rescue disk.
 - Restore your affected boot disk as the primary boot device.
 - Start instance [win-server].

Do you want to continue (y/N)? y

Restoring instance [win-server]:
 (3/3) [Stopping -> Restoring affected disk -> Starting] done.

Instance [win-server] restored to normal operation.

Connect via RDP using your original credentials.

Forgot password? Reset it:
  $ gcloud compute reset-windows-password win-server --zone=us-central1-a --project=my-project
```

## Upgrading from V1

| Action | V1 | V2 |
|--------|----|----|
| Enter rescue | `gce-rescue -n VM -z ZONE` | `gce-rescue-v2 rescue VM --zone ZONE` |
| Exit rescue | `gce-rescue -n VM -z ZONE` (same) | `gce-rescue-v2 restore VM --zone ZONE` |
| Skip snapshot | `--skip-snapshot` | `--no-snapshot` |
| No prompts | `-f` or `--force` | `--quiet` |

## Support

- [Report Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)
- [V1 Documentation](../README.md)

---

Apache License 2.0
