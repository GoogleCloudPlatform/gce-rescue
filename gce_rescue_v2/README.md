# GCE Rescue V2

**Rescue unbootable Google Compute Engine VMs** - Boot into a rescue environment to fix broken configurations, corrupted files, or boot issues.

> **Beta**: Report issues at [GitHub Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)

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

```bash
$ gce-rescue-v2 rescue web-server --zone us-central1-a

============================================================
GCE Rescue V2 - Rescue Mode
============================================================
VM: web-server
Zone: us-central1-a

Validating...
  [OK] Credentials valid
  [OK] VM exists
  [OK] VM state: RUNNING
  [OK] OS detected: Linux

Executing rescue...
  [OK] Snapshot created: rescue-snapshot-web-server-1702060800
  [OK] VM stopped
  [OK] Rescue disk created
  [OK] Boot configuration updated
  [OK] VM started in rescue mode

============================================================
[OK] Rescue completed successfully!
============================================================

Your VM is now in rescue mode.
Connect via SSH: gcloud compute ssh web-server --zone=us-central1-a
Affected disk mounted at: /mnt/sysroot

When done, restore your VM:
  gce-rescue-v2 restore web-server --zone=us-central1-a
```

**Connect and fix:**

```bash
$ gcloud compute ssh web-server --zone us-central1-a

user@web-server:~$ sudo nano /mnt/sysroot/etc/fstab
# Fix the issue, save and exit

user@web-server:~$ exit
```

**Restore:**

```bash
$ gce-rescue-v2 restore web-server --zone us-central1-a

============================================================
GCE Rescue V2 - Restore Mode
============================================================
VM: web-server
Zone: us-central1-a

Validating...
  [OK] VM is in rescue mode

Executing restore...
  [OK] VM stopped
  [OK] Rescue disk detached
  [OK] Boot disk restored
  [OK] VM started

============================================================
[OK] Restore completed successfully!
============================================================

Your VM has been restored to normal operation.
Connect via SSH: gcloud compute ssh web-server --zone=us-central1-a
```

## Example: Windows VM

```bash
$ gce-rescue-v2 rescue win-server --zone us-central1-a

============================================================
GCE Rescue V2 - Rescue Mode
============================================================
VM: win-server
Zone: us-central1-a

Validating...
  [OK] Credentials valid
  [OK] VM exists
  [OK] VM state: RUNNING
  [OK] OS detected: Windows

Executing rescue...
  [OK] Snapshot created: rescue-snapshot-win-server-1702060800
  [OK] VM stopped
  [OK] Rescue disk created
  [OK] Boot configuration updated
  [OK] VM started in rescue mode

============================================================
[OK] Rescue completed successfully!
============================================================

Your VM is now in rescue mode.

==================================================
  Windows RDP Login Credentials
==================================================
  IP Address: 35.192.0.100
  Username:   rescue_admin
  Password:   xK9mP2nQ5rT8wY3z
==================================================

Note: Wait 2-3 minutes for Windows to fully boot before connecting.

Affected disk mounted at: D:\ (or next available drive letter)

When done, restore your VM:
  gce-rescue-v2 restore win-server --zone=us-central1-a
```

**Connect and fix:**

1. Open Remote Desktop Connection
2. Enter the IP address, username, and password shown above
3. Your broken disk is at `D:\` - browse and fix files
4. Example: `D:\Windows\System32\config\` for registry hives

**Restore:**

```bash
$ gce-rescue-v2 restore win-server --zone us-central1-a

============================================================
GCE Rescue V2 - Restore Mode
============================================================
VM: win-server
Zone: us-central1-a

Validating...
  [OK] VM is in rescue mode

Executing restore...
  [OK] VM stopped
  [OK] Rescue disk detached
  [OK] Boot disk restored
  [OK] VM started

============================================================
[OK] Restore completed successfully!
============================================================

Your VM has been restored to normal operation.
Connect via RDP: gcloud compute reset-windows-password win-server --zone=us-central1-a
```

## Upgrading from V1

| V1 | V2 |
|----|----|
| `gce-rescue -n VM -z ZONE` | `gce-rescue-v2 rescue VM --zone ZONE` |
| Same command to restore | `gce-rescue-v2 restore VM --zone ZONE` |
| Linux only | Linux + Windows |
| Manual recovery on failure | Automatic rollback |

## Support

- [Report Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)
- [V1 Documentation](../README.md)

---

Apache License 2.0
