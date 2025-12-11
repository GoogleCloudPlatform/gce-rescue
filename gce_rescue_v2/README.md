# GCE Rescue V2

A tool to rescue unbootable Google Compute Engine (GCE) virtual machines by booting them into a rescue environment.

## Overview

When a VM becomes unbootable due to misconfiguration, corrupted files, or boot issues, GCE Rescue V2 allows you to:

1. **Rescue**: Boot the VM from a temporary rescue disk, with the original boot disk attached as a secondary disk for repair
2. **Restore**: Return the VM to its original state after repairs are complete

## Features

- **Automatic Rollback**: If any operation fails, the system automatically rolls back to the previous state
- **Safety Snapshots**: Creates a snapshot of the boot disk before rescue (optional but recommended)
- **Clean Error Messages**: User-friendly error messages instead of raw API responses
- **Modular Architecture**: Each operation is isolated and can be individually tested

## Requirements

- **Python**: 3.9 or higher
- **Google Cloud SDK**: `gcloud` CLI installed and configured
- **Authentication**: Valid GCP credentials with required permissions
- **IAM Permissions**:
  - `compute.instances.get`
  - `compute.instances.stop`
  - `compute.instances.start`
  - `compute.instances.attachDisk`
  - `compute.instances.detachDisk`
  - `compute.instances.setMetadata`
  - `compute.disks.create`
  - `compute.disks.delete`
  - `compute.snapshots.create` (if snapshots enabled)

## Installation

```bash
# Option 1: Install directly from GitHub (recommended for beta)
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta

# Option 2: Clone and install locally
git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
cd gce-rescue
git checkout v2-beta
pip install -e .

# Verify installation
gce-rescue-v2 --help
```

## Quick Start

### Rescue a VM

```bash
# Basic rescue
gce-rescue-v2 rescue my-vm --zone us-central1-a

# With explicit project
gce-rescue-v2 rescue my-vm --zone us-central1-a --project my-project

# Skip snapshot (faster, but less safe)
gce-rescue-v2 rescue my-vm --zone us-central1-a --no-snapshot
```

### Connect to Rescued VM

After rescue completes, SSH into the VM:

```bash
gcloud compute ssh my-vm --zone us-central1-a
```

The original boot disk is mounted at `/mnt/affected-disk`. You can now:
- Edit configuration files
- Fix fstab entries
- Repair boot loader
- Recover data

### Restore the VM

When repairs are complete, restore the VM to normal:

```bash
gce-rescue-v2 restore my-vm --zone us-central1-a
```

## Command Reference

### Rescue Command

```bash
gce-rescue-v2 rescue <VM_NAME> --zone <ZONE> [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--zone` | GCP zone (required) | - |
| `--project` | GCP project ID | Current gcloud project |
| `--snapshot` / `--no-snapshot` | Create safety snapshot | `--snapshot` |
| `--disk-size` | Rescue disk size in GB | 10 |
| `--format` | Output format (json, yaml, table) | table |

### Restore Command

```bash
gce-rescue-v2 restore <VM_NAME> --zone <ZONE> [OPTIONS]
```

| Option | Description | Default |
|--------|-------------|---------|
| `--zone` | GCP zone (required) | - |
| `--project` | GCP project ID | Current gcloud project |
| `--keep-disk` | Don't delete rescue disk | Delete |
| `--format` | Output format (json, yaml, table) | table |

## How It Works

### Rescue Workflow

```
1. Validate       → Check permissions, VM state
2. Stop VM        → Gracefully stop the instance
3. Snapshot       → Create safety snapshot (optional)
4. Detach Boot    → Remove original boot disk
5. Create Rescue  → Create new disk from Debian image
6. Attach Rescue  → Attach rescue disk as boot
7. Attach Orig    → Attach original disk as secondary
8. Set Metadata   → Add startup script to mount disk
9. Start VM       → Boot into rescue environment
```

### Restore Workflow

```
1. Validate       → Check VM is in rescue mode
2. Stop VM        → Stop the rescue environment
3. Detach Rescue  → Remove rescue disk
4. Detach Orig    → Detach original disk
5. Attach Boot    → Re-attach original disk as boot
6. Clear Metadata → Remove rescue startup script
7. Start VM       → Boot normally
8. Delete Rescue  → Clean up rescue disk (optional)
```

## Example Session

```bash
# VM is unbootable - let's rescue it
$ gce-rescue-v2 rescue web-server --zone us-central1-a

Pre-flight Validation:
  [OK] Credentials valid
  [OK] IAM permissions verified
  [OK] VM state valid (RUNNING)

Executing Rescue:
  [OK] VM stopped (45s)
  [OK] Snapshot created: pre-rescue-web-server-1702060800
  [OK] Boot disk detached
  [OK] Rescue disk created
  [OK] Rescue disk attached as boot
  [OK] Original disk attached as secondary
  [OK] Startup script configured
  [OK] VM started (30s)

Rescue Complete!

Connect to VM:
  gcloud compute ssh web-server --zone us-central1-a

Original disk mounted at:
  /mnt/affected-disk

When done, restore with:
  gce-rescue-v2 restore web-server --zone us-central1-a

# SSH and fix the issue
$ gcloud compute ssh web-server --zone us-central1-a
user@web-server:~$ sudo nano /mnt/affected-disk/etc/fstab
user@web-server:~$ exit

# Restore the VM
$ gce-rescue-v2 restore web-server --zone us-central1-a

Pre-flight Validation:
  [OK] Credentials valid
  [OK] IAM permissions verified
  [OK] VM is in rescue mode

Executing Restore:
  [OK] VM stopped (40s)
  [OK] Rescue disk detached
  [OK] Original disk detached
  [OK] Original disk attached as boot
  [OK] Rescue metadata cleared
  [OK] VM started (35s)
  [OK] Rescue disk deleted

Restore Complete!
VM is now running with original boot disk.
```

## Windows Support

GCE Rescue V2 includes full support for Windows VMs:

- **Automatic OS Detection**: Detects Windows vs Linux VMs automatically
- **Windows Server 2022**: Uses Windows Server 2022 Datacenter as rescue environment
- **PowerShell Script**: Automatically mounts affected disk with drive letters (D:, E:, etc.)
- **Desktop Instructions**: Creates a helpful instructions file on the desktop

### Rescue a Windows VM

```bash
# The tool automatically detects Windows VMs
gce-rescue-v2 rescue my-windows-vm --zone us-central1-a
```

### Connect to Rescued Windows VM

After rescue completes, connect via RDP:

```bash
gcloud compute reset-windows-password my-windows-vm --zone us-central1-a
```

The affected disk is automatically mounted. Look for additional drive letters (D:, E:, etc.)

**Common Windows repair tasks:**
- Edit files: `notepad D:\Windows\System32\config\SOFTWARE`
- Registry repair: Load hive in regedit from `D:\Windows\System32\config\`
- Boot repair: `bcdboot D:\Windows /s C:`

## Known Limitations

| Limitation | Description | Workaround |
|------------|-------------|------------|
| **CMEK Encrypted Disks** | Encrypted disks not supported | Feature planned for future |
| **Shielded VMs** | Secure Boot may block rescue disk | Disable Secure Boot temporarily |
| **LVM/RAID** | Auto-mount doesn't work | Manually activate LVM in rescue shell |
| **LUKS Encryption** | Encrypted partitions need passphrase | Manually unlock in rescue shell |
| **BitLocker** | BitLocker encrypted drives need recovery key | Unlock with `manage-bde -unlock D: -RecoveryPassword KEY` |

## Troubleshooting

### "Permission denied" errors

Ensure your account has the required IAM permissions:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="user:your-email@example.com" \
  --role="roles/compute.instanceAdmin.v1"
```

### VM won't start after rescue

The rescue disk may have failed to attach. Check:

```bash
gcloud compute instances describe VM_NAME --zone ZONE --format="value(disks)"
```

### Disk not mounted at /mnt/affected-disk

SSH into the VM and check:

```bash
# Check startup script logs
cat /var/log/gce-rescue.log

# Manually mount if needed
sudo mount /dev/disk/by-id/google-DISK_NAME-part1 /mnt/affected-disk
```

## Architecture

```
gce_rescue_v2/
├── cli.py              # Command-line interface
├── main.py             # Entry point functions
├── core/
│   ├── config.py       # Configuration dataclasses
│   ├── auth.py         # GCP authentication
│   └── exceptions.py   # Custom exceptions
├── operations/         # Individual operations
│   ├── base.py         # Base operation class
│   ├── stop_vm.py
│   ├── start_vm.py
│   ├── create_disk.py
│   ├── delete_disk.py
│   ├── attach_disk.py
│   ├── detach_disk.py
│   ├── create_snapshot.py
│   └── set_metadata.py
├── orchestration/      # Workflow coordination
│   ├── rescue.py       # Rescue workflow
│   ├── restore.py      # Restore workflow
│   ├── rollback.py     # Rollback handler
│   └── state.py        # State tracking
├── validators/         # Pre-flight checks
│   ├── credentials.py
│   ├── iam_permissions.py
│   └── vm_state.py
├── utils/              # Utility functions
│   └── os_detection.py # OS type detection
└── startup_scripts/
    ├── rescue_mount.sh           # Linux auto-mount script
    └── rescue_mount_windows.ps1  # Windows auto-mount script
```

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes and add tests
4. Run tests: `pytest gce_rescue_v2/tests/`
5. Submit a pull request

## License

Apache License 2.0 - See [LICENSE](../LICENSE) for details.

## Version

Current version: **2.0.0-beta.1**

## Support

- **Issues**: https://github.com/GoogleCloudPlatform/gce-rescue/issues
- **Documentation**: This README

---

*GCE Rescue V2 - Rescue your VMs with confidence*
