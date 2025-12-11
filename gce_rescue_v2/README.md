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

## Common Use Cases

| Problem | How to Fix |
|---------|------------|
| Bad `/etc/fstab` entry | Edit `/mnt/affected-disk/etc/fstab` |
| Full disk | Delete files from `/mnt/affected-disk/var/log/` |
| Locked out (firewall/SSH) | Fix config in `/mnt/affected-disk/etc/` |
| Failed OS update | Restore packages or boot config |
| Corrupted boot loader | Run `grub-install` in chroot |

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

## After Rescue

### Linux

```bash
gcloud compute ssh my-vm --zone us-central1-a

# Your broken disk is at /mnt/affected-disk
ls /mnt/affected-disk/
sudo nano /mnt/affected-disk/etc/fstab
```

### Windows

```bash
# Get RDP credentials
gcloud compute reset-windows-password my-vm --zone us-central1-a

# Connect via RDP - your disk is at D:\ or E:\
```

## Upgrading from V1

| V1 | V2 |
|----|----|
| `gce-rescue -n VM -z ZONE` | `gce-rescue-v2 rescue VM --zone ZONE` |
| Same command to restore | `gce-rescue-v2 restore VM --zone ZONE` |
| Linux only | Linux + Windows |
| Manual recovery on failure | Automatic rollback |

## Limitations

- **Shielded VMs**: Disable Secure Boot temporarily
- **Encrypted disks (CMEK/LUKS/BitLocker)**: Requires manual decryption
- **LVM/RAID**: Run `vgchange -ay` manually after rescue

## Support

- [Report Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)
- [V1 Documentation](../README.md)

---

Apache License 2.0
