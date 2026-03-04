# GCE Rescue

[![test badge](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml/badge.svg?branch=main&event=push)](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml?query=branch%3Amain+event%3Apush)

Rescue unbootable Google Compute Engine VMs. Automatically swaps the boot disk on the same VM so you keep your IP, networking, and configuration.

> **Note**: GCE Rescue is not an officially supported Google Cloud product. The Google Cloud Support team maintains this repository.

## Quick Start

```bash
# Install
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git

# Authenticate
gcloud auth application-default login

# Diagnose boot issues (read-only)
gce-rescue diagnose my-vm --zone=us-central1-a

# Auto-fix (Linux, currently fstab errors)
gce-rescue repair my-vm --zone=us-central1-a

# Manual fix (Linux + Windows)
gce-rescue rescue my-vm --zone=us-central1-a
# SSH/RDP in, fix the issue, then:
gce-rescue restore my-vm --zone=us-central1-a
```

## Requirements

- Python >= 3.9
- `gcloud` CLI ([install](https://cloud.google.com/sdk/docs/install))
- `Compute Instance Admin (v1)` IAM role or equivalent

## Installation

```bash
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git
```

Verify:

```bash
gce-rescue --version
```

## Commands

```
VM won't boot
    |
    +-- Not sure what's wrong?
    |   gce-rescue diagnose    (read-only, safe anytime)
    |
    +-- diagnose found a fixable issue (e.g. fstab)?
    |   gce-rescue repair      (auto-fix, Linux only)
    |
    +-- Need manual access to the disk?
    |   gce-rescue rescue      (enter rescue mode)
    |   SSH/RDP in and fix
    |   gce-rescue restore     (exit rescue mode)
    |
    +-- VM stuck from a previous rescue?
        gce-rescue restore     (or re-run rescue to resume/rollback)
```

| Command | Description |
|---------|-------------|
| `gce-rescue diagnose VM --zone ZONE` | Analyze serial console for boot errors (read-only) |
| `gce-rescue repair VM --zone ZONE` | Auto-diagnose and fix boot issues (Linux only) |
| `gce-rescue rescue VM --zone ZONE` | Enter rescue mode for manual repair |
| `gce-rescue restore VM --zone ZONE` | Exit rescue mode, restore original boot disk |

| Flag | Description |
|------|-------------|
| `--zone` | GCP zone (required) |
| `--project` | GCP project (default: current gcloud config) |
| `--no-snapshot` | Skip safety snapshot (faster) |
| `--quiet` | No confirmation prompts (for automation) |
| `--format` | Output format: `json`, `yaml`, `table` |

## Features

| Feature | Description |
|---------|-------------|
| **Linux + Windows** | Auto-detects OS, uses appropriate rescue environment |
| **Boot Diagnostics** | Serial console analysis for fstab, GRUB, kernel, filesystem errors |
| **Auto-Repair** | Automated fix for fstab errors (more categories planned) |
| **Automatic Rollback** | Operations roll back on failure |
| **Session Recovery** | Resume or rollback interrupted operations |
| **Safety Snapshots** | Backup snapshot before any changes (default) |
| **ARM64 Support** | Automatic architecture detection |

## How It Works

```
BEFORE (won't boot)          AFTER RESCUE (same VM)
+-------------+              +-------------+
|    VM       |              |    VM       | Same IP!
+-------------+              +-------------+
| Boot:       |              | Boot: [Rescue Disk]
| [Original]  |  -------->   | Secondary: [Original]
| (broken)    |              |   at /mnt/sysroot
+-------------+              +-------------+
```

GCE Rescue swaps the boot disk on the same VM (not creating a new one), preserving networking, IPs, and configuration.

## V1 Legacy

V1 is available as `gce-rescue-v1` for backward compatibility:

```bash
gce-rescue-v1 -n VM_NAME -z ZONE -p PROJECT
```

See the [V1 documentation](gce_rescue/README.md) for details.

## Authentication

Uses Application Default Credentials (ADC):

```bash
gcloud auth application-default login
```

More info: https://cloud.google.com/docs/authentication/provide-credentials-adc

## Permissions

Minimum IAM permissions required:

| Operation | Permissions |
|-----------|-------------|
| Start/stop instance | `compute.instances.stop`, `compute.instances.start` |
| Disk operations | `compute.instances.attachDisk`, `compute.instances.detachDisk`, `compute.disks.use`, `compute.images.useReadOnly` |
| Create snapshot | `compute.snapshots.create`, `compute.disks.createSnapshot` |
| Configure metadata | `compute.instances.setMetadata`, `compute.instances.setLabels` |

## Contact

GCE Rescue Team: gce-rescue-dev@google.com
