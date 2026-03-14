# GCE Rescue

[![test badge](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml/badge.svg?branch=main&event=push)](https://github.com/GoogleCloudPlatform/gce-rescue/actions/workflows/test.yml?query=branch%3Amain+event%3Apush)

Rescue unbootable Google Compute Engine VMs. Operates on the same VM — no new instance is created.

**Auto-fix path**: The `repair` command reads serial console output, identifies
the boot failure, and applies a fix automatically end to end.

**Rescue path**: When auto-fix is not available for the detected issue, the
`rescue` command swaps your broken boot disk with a rescue disk and attaches the
original boot disk as a secondary disk, providing a rescue environment for manual
repair. Once fixed, the `restore` command puts your fixed boot disk back.

<p align="center">
  <img src="gce-rescue.svg" alt="GCE Rescue Workflow" width="600">
</p>

> **Note**: GCE Rescue is not an officially supported Google Cloud product. The Google Cloud Support team maintains this repository.

## Installation

### Google Cloud Shell (recommended)

Open [Cloud Shell](https://shell.cloud.google.com) — Python, gcloud, and authentication are already set up.

```bash
pip install git+https://github.com/GoogleCloudPlatform/gce-rescue.git
```

<details>
<summary><b>Local Machine</b></summary>
<br>

**Linux / macOS**

```bash
curl -sSL https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.sh | bash
```

**Windows** (run PowerShell as Administrator)

```powershell
irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.ps1 | iex
```

The installers handle all prerequisites (Python, gcloud, PATH, authentication)
and will prompt before installing anything.

---

**Install from source** (requires Python >= 3.9, [gcloud CLI](https://cloud.google.com/sdk/docs/install), Git)

```bash
git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
cd gce-rescue
pip install .
gcloud auth application-default login
```

</details>

## Usage

| Command | What it does | Modifies VM? |
|---------|-------------|:---:|
| `diagnose` | Identifies boot errors from serial console output | No |
| `repair` | Diagnoses and fixes boot issues automatically | Yes |
| `rescue` | Provides a rescue environment for investigation via SSH/RDP | Yes |
| `restore` | Reverses rescue, puts your fixed boot disk back | Yes |

1. **Start with diagnose** — understand what's wrong (safe, read-only)
   ```bash
   gce-rescue diagnose VM_NAME --zone=ZONE
   ```

2. **Auto-fix available?** — let repair handle it automatically
   ```bash
   gce-rescue repair VM_NAME --zone=ZONE
   ```

3. **Need manual access?** — enter rescue mode, fix it yourself
   ```bash
   gce-rescue rescue VM_NAME --zone=ZONE
   # SSH/RDP in, fix the issue on /mnt/sysroot
   gce-rescue restore VM_NAME --zone=ZONE
   ```

All operations create a snapshot before changes, roll back automatically on
failure, and can resume if interrupted.

### Flags

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

## Uninstall

```bash
pip uninstall gce-rescue

# Linux/macOS (if installed via install script)
rm -rf ~/.gce-rescue
```

## Authentication

gce-rescue uses Application Default Credentials (ADC):

```bash
gcloud auth application-default login
```

On GCE VMs, the VM service account is used automatically.

More info: https://cloud.google.com/docs/authentication/provide-credentials-adc

## Permissions

### Required permissions by command

| Permission | `diagnose` | `repair` | `rescue` | `restore` |
|---|:---:|:---:|:---:|:---:|
| `compute.projects.get` | x | x | x | x |
| `compute.instances.get` | x | x | x | x |
| `compute.instances.getSerialPortOutput` | x | x | x | |
| `compute.instances.stop` | | x | x | x |
| `compute.instances.start` | | x | x | x |
| `compute.instances.attachDisk` | | x | x | x |
| `compute.instances.detachDisk` | | x | x | x |
| `compute.instances.setMetadata` | | x | x | x |
| `compute.disks.create` | | x | x | |
| `compute.disks.delete` | | x | x | x |
| `compute.disks.get` | | x | x | x |
| `compute.disks.createSnapshot` | | x* | x* | |
| `compute.snapshots.create` | | x* | x* | |
| `compute.snapshots.get` | | x* | x* | |
| `compute.snapshots.list` | | x | | x |
| `compute.snapshots.delete` | | x* | x* | |

\* Skippable with `--no-snapshot`

### IAM roles

All permissions above are included in `roles/compute.instanceAdmin.v1`.

| Command | Minimum Role |
|---------|-------------|
| `diagnose` | `roles/compute.viewer` |
| `rescue`, `restore`, `repair` | `roles/compute.instanceAdmin.v1` |

To grant access:

```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="user:EMAIL" \
    --role="roles/compute.instanceAdmin.v1"
```

## V1 Legacy

V1 is available as `gce-rescue-v1` for backward compatibility:

```bash
gce-rescue-v1 -n VM_NAME -z ZONE -p PROJECT
```

See the [V1 documentation](gce_rescue/README.md) for details.

## Contact

GCE Rescue Team: gce-rescue-dev@google.com
