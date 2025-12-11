# GCE Rescue V2

A tool to rescue unbootable Google Compute Engine (GCE) virtual machines by booting them into a rescue environment.

> **Beta Notice**: This is a beta release. Please report issues at [GitHub Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues).

---

## What's New in V2

| Feature | V1 | V2 |
|---------|----|----|
| **Windows Support** | Linux only | Linux + Windows |
| **Auto-Rollback** | Manual recovery needed | Automatic on failure |
| **CLI Style** | Single toggle command | Separate `rescue` / `restore` |
| **Safety Snapshots** | Always created | Optional (default: on) |
| **OS Detection** | N/A | Automatic Linux/Windows |
| **Output Formats** | Text only | JSON, YAML, Table |

---

## When to Use GCE Rescue

Use this tool when your VM won't boot due to:

- **Bad `/etc/fstab` entry** - VM hangs on boot
- **Corrupted boot loader** - GRUB/Windows Boot Manager issues
- **Full disk** - No space left, services won't start
- **Misconfigured firewall** - Locked out of SSH/RDP
- **Broken system files** - Deleted critical files
- **Failed updates** - Kernel or Windows Update broke the system

---

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                        RESCUE MODE                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   BEFORE RESCUE              AFTER RESCUE                        │
│   ┌──────────────┐           ┌──────────────┐                   │
│   │     VM       │           │     VM       │                   │
│   ├──────────────┤           ├──────────────┤                   │
│   │ [Boot Disk]  │  ──────►  │ [Rescue Disk]│ ◄── Boots from    │
│   │  (broken)    │           │  (Debian 12) │     rescue OS     │
│   └──────────────┘           ├──────────────┤                   │
│                              │ [Boot Disk]  │ ◄── Mounted at    │
│                              │  (broken)    │     /mnt/affected │
│                              └──────────────┘                   │
│                                                                  │
│   You can now SSH in and fix files on the broken disk!          │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start

### 1. Install

```bash
# Install from GitHub
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta

# Verify
gce-rescue-v2 --version
```

### 2. Authenticate

```bash
# Login with your Google account
gcloud auth application-default login

# Set your project (optional - can also use --project flag)
gcloud config set project YOUR_PROJECT_ID
```

### 3. Rescue a VM

```bash
# Linux VM
gce-rescue-v2 rescue my-linux-vm --zone us-central1-a

# Windows VM (auto-detected)
gce-rescue-v2 rescue my-windows-vm --zone us-central1-a
```

### 4. Connect and Fix

**Linux:**
```bash
gcloud compute ssh my-linux-vm --zone us-central1-a

# Your broken disk is at /mnt/affected-disk
sudo nano /mnt/affected-disk/etc/fstab
```

**Windows:**
```bash
# Get RDP credentials
gcloud compute reset-windows-password my-windows-vm --zone us-central1-a

# Connect via RDP, broken disk is at D:\ or E:\
```

### 5. Restore

```bash
gce-rescue-v2 restore my-vm --zone us-central1-a
```

---

## Installation Options

### Option 1: Install from GitHub (Recommended for Beta)

```bash
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta
```

### Option 2: Clone and Install Locally

```bash
git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
cd gce-rescue
git checkout v2-beta
pip install -e .
```

### Option 3: Run Without Installing

```bash
git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
cd gce-rescue
git checkout v2-beta
pip install -r gce_rescue_v2/requirements.txt

# Run directly
python -m gce_rescue_v2.cli rescue my-vm --zone us-central1-a
```

### Verify Installation

```bash
gce-rescue-v2 --version
# Output: gce-rescue-v2 2.0.0-beta.1

gce-rescue-v2 --help
```

---

## Requirements

### System Requirements

| Requirement | Details |
|-------------|---------|
| **Python** | 3.9 or higher |
| **gcloud CLI** | Installed and configured |
| **Network** | Access to GCP APIs |

### IAM Permissions

Your account needs these permissions on the target VM:

```
compute.instances.get
compute.instances.stop
compute.instances.start
compute.instances.attachDisk
compute.instances.detachDisk
compute.instances.setMetadata
compute.disks.create
compute.disks.delete
compute.disks.get
compute.snapshots.create (if using snapshots)
```

**Quick setup** - Grant the `Compute Instance Admin (v1)` role:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="user:YOUR_EMAIL" \
  --role="roles/compute.instanceAdmin.v1"
```

---

## Command Reference

### Rescue Command

Put a VM into rescue mode:

```bash
gce-rescue-v2 rescue <VM_NAME> --zone <ZONE> [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--zone` | GCP zone (required) | - |
| `--project` | GCP project ID | Current gcloud config |
| `--no-snapshot` | Skip safety snapshot | Snapshot enabled |
| `--quiet` | No confirmation prompts | Interactive |
| `--format` | Output: `json`, `yaml`, `table`, `disable` | `table` |

**Examples:**

```bash
# Basic rescue
gce-rescue-v2 rescue web-server --zone us-central1-a

# With explicit project
gce-rescue-v2 rescue web-server --zone us-central1-a --project my-project

# Skip snapshot (faster)
gce-rescue-v2 rescue web-server --zone us-central1-a --no-snapshot

# Automation-friendly (no prompts, JSON output)
gce-rescue-v2 rescue web-server --zone us-central1-a --quiet --format json
```

### Restore Command

Return a VM to normal operation:

```bash
gce-rescue-v2 restore <VM_NAME> --zone <ZONE> [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--zone` | GCP zone (required) | - |
| `--project` | GCP project ID | Current gcloud config |
| `--keep-rescue-disk` | Don't delete rescue disk | Delete |
| `--quiet` | No confirmation prompts | Interactive |
| `--format` | Output format | `table` |

**Examples:**

```bash
# Basic restore
gce-rescue-v2 restore web-server --zone us-central1-a

# Keep rescue disk for inspection
gce-rescue-v2 restore web-server --zone us-central1-a --keep-rescue-disk
```

---

## Linux Rescue Guide

### After Rescue

1. **SSH into the VM:**
   ```bash
   gcloud compute ssh my-vm --zone us-central1-a
   ```

2. **Your broken disk is mounted at `/mnt/affected-disk`:**
   ```bash
   ls /mnt/affected-disk/
   # etc  home  root  var  ...
   ```

### Common Fixes

**Fix /etc/fstab:**
```bash
sudo nano /mnt/affected-disk/etc/fstab
# Comment out or fix the bad entry
```

**Reset root password:**
```bash
sudo chroot /mnt/affected-disk
passwd root
exit
```

**Check disk space:**
```bash
df -h /mnt/affected-disk
# Free up space if needed
sudo rm /mnt/affected-disk/var/log/*.gz
```

**Repair GRUB:**
```bash
sudo chroot /mnt/affected-disk
grub-install /dev/sdb
update-grub
exit
```

**View boot logs:**
```bash
cat /mnt/affected-disk/var/log/boot.log
journalctl -D /mnt/affected-disk/var/log/journal/
```

---

## Windows Rescue Guide

### After Rescue

1. **Get RDP credentials:**
   ```bash
   gcloud compute reset-windows-password my-windows-vm --zone us-central1-a
   ```

2. **Connect via RDP** using the IP and credentials shown

3. **Find the affected disk** - Look for drive `D:\` or `E:\` in File Explorer

### Common Fixes

**Edit system files:**
```
Open Notepad as Administrator
File > Open > D:\Windows\System32\drivers\etc\hosts
```

**Repair boot configuration:**
```powershell
# Open Command Prompt as Administrator
bcdboot D:\Windows /s C:
```

**Access Registry:**
```
1. Open regedit
2. Select HKEY_LOCAL_MACHINE
3. File > Load Hive
4. Navigate to D:\Windows\System32\config\SYSTEM
5. Give it a name like "OFFLINE_SYSTEM"
6. Make your changes
7. File > Unload Hive
```

**Check Event Logs:**
```
Event Viewer > Open Saved Log > D:\Windows\System32\winevt\Logs\System.evtx
```

---

## Example: Full Rescue Session

```bash
# 1. VM is unbootable - rescue it
$ gce-rescue-v2 rescue web-server --zone us-central1-a

GCE Rescue V2 - Rescue Mode
============================

Target: web-server (us-central1-a)
OS Type: Linux (auto-detected)

Pre-flight Checks:
  ✓ Credentials valid
  ✓ IAM permissions verified
  ✓ VM exists and is accessible
  ✓ VM state: RUNNING

This will:
  1. Stop the VM
  2. Create a safety snapshot
  3. Boot from a rescue disk (Debian 12)
  4. Mount original disk at /mnt/affected-disk

Proceed? [Y/n]: y

Executing Rescue:
  ✓ Creating snapshot... done (15s)
  ✓ Stopping VM... done (45s)
  ✓ Creating rescue disk... done (30s)
  ✓ Configuring boot order... done
  ✓ Starting VM... done (60s)

════════════════════════════════════════════════════════
  RESCUE COMPLETE
════════════════════════════════════════════════════════

Connect to your VM:
  $ gcloud compute ssh web-server --zone us-central1-a

Affected disk location:
  /mnt/affected-disk

When finished, restore with:
  $ gce-rescue-v2 restore web-server --zone us-central1-a

Safety snapshot created:
  rescue-snapshot-web-server-1702060800


# 2. SSH in and fix the issue
$ gcloud compute ssh web-server --zone us-central1-a
user@web-server:~$ sudo nano /mnt/affected-disk/etc/fstab
user@web-server:~$ exit

# 3. Restore the VM
$ gce-rescue-v2 restore web-server --zone us-central1-a

GCE Rescue V2 - Restore Mode
============================

Target: web-server (us-central1-a)
Status: Currently in rescue mode

This will:
  1. Stop the VM
  2. Remove rescue disk
  3. Restore original boot disk
  4. Start the VM normally

Proceed? [Y/n]: y

Executing Restore:
  ✓ Stopping VM... done (40s)
  ✓ Detaching rescue disk... done
  ✓ Restoring boot configuration... done
  ✓ Starting VM... done (35s)
  ✓ Cleaning up rescue disk... done

════════════════════════════════════════════════════════
  RESTORE COMPLETE
════════════════════════════════════════════════════════

Your VM is now running with the original boot disk.
```

---

## Automatic Rollback

If something goes wrong during rescue, V2 automatically rolls back:

```
Executing Rescue:
  ✓ Creating snapshot... done
  ✓ Stopping VM... done
  ✓ Creating rescue disk... done
  ✗ Attaching rescue disk... FAILED (quota exceeded)

Automatic Rollback:
  ✓ Deleting rescue disk... done
  ✓ Starting VM... done

Rescue failed but VM has been restored to original state.
Error: Quota exceeded for disk creation in zone us-central1-a
```

---

## Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| **CMEK Encrypted Disks** | Cannot create rescue disk | Decrypt disk first |
| **Shielded VMs** | Secure Boot blocks rescue disk | Disable Secure Boot temporarily |
| **Sole-Tenant VMs** | May fail to start rescue | Use standard VM temporarily |
| **LVM/RAID** | Not auto-mounted | Manually run `vgchange -ay` |
| **LUKS Encryption** | Not auto-mounted | Manually run `cryptsetup luksOpen` |
| **BitLocker** | Not auto-mounted | Use recovery key with `manage-bde` |

---

## Troubleshooting

### "Permission denied" error

```bash
# Check your current account
gcloud auth list

# Re-authenticate
gcloud auth application-default login

# Verify permissions
gcloud compute instances describe VM_NAME --zone ZONE
```

### VM won't start after rescue

```bash
# Check VM status
gcloud compute instances describe VM_NAME --zone ZONE --format="value(status)"

# Check attached disks
gcloud compute instances describe VM_NAME --zone ZONE --format="yaml(disks)"

# View serial console for boot errors
gcloud compute instances get-serial-port-output VM_NAME --zone ZONE
```

### Disk not mounted at /mnt/affected-disk

```bash
# SSH into the rescue VM
gcloud compute ssh VM_NAME --zone ZONE

# Check available disks
lsblk

# Check rescue script log
cat /var/log/gce-rescue.log

# Manually mount
sudo mkdir -p /mnt/affected-disk
sudo mount /dev/sdb1 /mnt/affected-disk
```

### Rescue takes too long

- Use `--no-snapshot` to skip snapshot creation
- Check if the zone has capacity issues
- Try a different zone if possible

---

## Differences from V1

### Command Changes

| Action | V1 | V2 |
|--------|----|----|
| Enter rescue | `gce-rescue -n VM -z ZONE` | `gce-rescue-v2 rescue VM --zone ZONE` |
| Exit rescue | `gce-rescue -n VM -z ZONE` (same) | `gce-rescue-v2 restore VM --zone ZONE` |
| Skip snapshot | `--skip-snapshot` | `--no-snapshot` |
| Force mode | `-f` / `--force` | `--quiet` |

### Behavior Changes

- V2 has **separate** rescue and restore commands (clearer intent)
- V2 **auto-detects** Windows vs Linux
- V2 **auto-rolls back** on failure
- V2 mounts disk at `/mnt/affected-disk` (V1: `/mnt/sysroot`)

---

## Architecture

```
gce_rescue_v2/
├── cli.py              # Command-line interface
├── main.py             # Entry points: rescue_vm(), restore_vm()
├── core/
│   ├── config.py       # Configuration dataclasses
│   └── auth.py         # GCP authentication
├── operations/         # Individual GCP operations
│   ├── stop_vm.py
│   ├── start_vm.py
│   ├── create_disk.py
│   ├── attach_disk.py
│   └── ...
├── orchestration/      # Workflow coordination
│   ├── rescue.py       # Rescue workflow
│   ├── restore.py      # Restore workflow
│   └── rollback.py     # Automatic rollback
├── validators/         # Pre-flight checks
│   ├── credentials.py
│   └── vm_state.py
└── startup_scripts/    # Auto-mount scripts
    ├── rescue_mount.sh
    └── rescue_mount_windows.ps1
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Run tests: `pytest gce_rescue_v2/tests/ -v`
4. Submit a pull request

---

## Support

- **Issues**: [GitHub Issues](https://github.com/GoogleCloudPlatform/gce-rescue/issues)
- **Documentation**: This README
- **V1 Documentation**: [Main README](../README.md)

---

## Version

**Current**: 2.0.0-beta.1

**Changelog**: See [CHANGELOG.md](../CHANGELOG.md)

---

## License

Apache License 2.0 - See [LICENSE](../LICENSE)

---

*GCE Rescue V2 - Rescue your VMs with confidence*
