# GCE Rescue V2

**Rescue unbootable Google Compute Engine VMs** - Boot into a rescue environment to fix broken configurations, corrupted files, or boot issues.

> **Beta**: Report issues at [GitHub Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)

## Quick Start

```bash
# Install
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta

# Rescue your VM (works for both Linux and Windows)
gce-rescue-v2 rescue my-vm --zone us-central1-a

# Connect and fix (Linux: SSH, Windows: RDP)
gcloud compute ssh my-vm --zone us-central1-a
# Your disk is at /mnt/affected-disk

# Restore when done
gce-rescue-v2 restore my-vm --zone us-central1-a
```

## What It Does

When your VM won't boot, GCE Rescue:

1. **Creates a safety snapshot** of your boot disk
2. **Boots your VM from a rescue disk** (Debian 12 or Windows Server 2022)
3. **Mounts your broken disk** so you can access and fix files
4. **Restores everything** when you're done

If anything fails, it **automatically rolls back** to the original state.

## Requirements

- **Python 3.9+** and **gcloud CLI** installed
- **IAM permissions**: `Compute Instance Admin (v1)` role or equivalent

```bash
# Quick permission setup
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="user:YOUR_EMAIL" \
  --role="roles/compute.instanceAdmin.v1"
```

## Commands

### Rescue

```bash
gce-rescue-v2 rescue VM_NAME --zone ZONE [--project PROJECT] [--no-snapshot] [--quiet]
```

### Restore

```bash
gce-rescue-v2 restore VM_NAME --zone ZONE [--project PROJECT] [--quiet]
```

### Options

| Flag | Description |
|------|-------------|
| `--zone` | GCP zone (required) |
| `--project` | GCP project (default: current gcloud config) |
| `--no-snapshot` | Skip safety snapshot (faster) |
| `--quiet` | No confirmation prompts (for automation) |
| `--format` | Output format: `table`, `json`, `yaml` |

## Example: Linux VM

Fix a bad `/etc/fstab` entry that prevents boot:

```bash
# 1. Rescue the VM
$ gce-rescue-v2 rescue web-server --zone us-central1-a

Creating snapshot... done
Stopping VM... done
Creating rescue disk... done
Starting in rescue mode... done

Rescue complete! Connect with:
  gcloud compute ssh web-server --zone us-central1-a

Your disk is mounted at: /mnt/affected-disk

# 2. SSH in and fix the issue
$ gcloud compute ssh web-server --zone us-central1-a

user@web-server:~$ sudo nano /mnt/affected-disk/etc/fstab
# Comment out or fix the bad entry, save and exit

user@web-server:~$ exit

# 3. Restore the VM
$ gce-rescue-v2 restore web-server --zone us-central1-a

Stopping VM... done
Restoring boot disk... done
Starting VM... done

Restore complete! Your VM is back to normal.
```

## Example: Windows VM

Fix a misconfigured service that prevents boot:

```bash
# 1. Rescue the VM
$ gce-rescue-v2 rescue win-server --zone us-central1-a

Creating snapshot... done
Stopping VM... done
Creating rescue disk... done
Starting in rescue mode... done

Rescue complete! Get RDP credentials with:
  gcloud compute reset-windows-password win-server --zone us-central1-a

Your disk is mounted at: D:\ or E:\

# 2. Get RDP credentials and connect
$ gcloud compute reset-windows-password win-server --zone us-central1-a

ip_address: 35.192.0.1
username: rescue_admin
password: A1b2C3d4E5f6

# 3. RDP into the VM, fix files on D:\ or E:\
#    Example: D:\Windows\System32\config\

# 4. Restore the VM
$ gce-rescue-v2 restore win-server --zone us-central1-a

Stopping VM... done
Restoring boot disk... done
Starting VM... done

Restore complete! Your VM is back to normal.
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
