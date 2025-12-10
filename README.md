# GCE Rescue

A tool to rescue unbootable Google Compute Engine (GCE) virtual machines by booting them into a rescue environment.

> **Note**: GCE Rescue is not an officially supported Google Cloud product. The Google Cloud Support team maintains this repository, but the product is experimental.

---

> ### GCE Rescue V2 Beta is Available!
>
> **New features:** Windows support, automatic rollback, simplified CLI
>
> ```bash
> # Install with beta
> pip install --pre gce-rescue
>
> # Use V2
> gce-rescue-v2 rescue my-vm --zone=us-central1-a
> gce-rescue-v2 restore my-vm --zone=us-central1-a
> ```
>
> V1 (`gce-rescue`) remains available for Linux-only workflows.

---

## Versions

| Version | Status | Description |
|---------|--------|-------------|
| **V2 (Beta)** | Active Development | New architecture with Windows support, auto-rollback |
| V1 | Maintenance | Original version, Linux only |

---

## GCE Rescue V2 (Beta) - Recommended

### Features

- **Automatic OS Detection** - Detects Linux vs Windows VMs automatically
- **Windows Support** - Full support for Windows Server VMs
- **Auto-Rollback** - Automatically rolls back on failure
- **Safety Snapshots** - Creates snapshot before rescue (default enabled)
- **Simplified CLI** - Clean, minimal interface

### Quick Start

```bash
# Install
cd gce_rescue_v2
pip install -r requirements.txt

# Rescue a VM (Linux or Windows - auto-detected)
python cli.py rescue my-vm --zone=us-central1-a

# Connect to rescued VM
# Linux:   gcloud compute ssh my-vm --zone=us-central1-a
# Windows: RDP using credentials shown after rescue

# Restore when done
python cli.py restore my-vm --zone=us-central1-a
```

### CLI Reference

```
RESCUE:
  python cli.py rescue <VM_NAME> --zone=<ZONE> [OPTIONS]

  Options:
    --project PROJECT    GCP project (default: gcloud config)
    --no-snapshot        Skip safety snapshot (faster but riskier)
    --quiet              No interactive prompts (for automation)
    --format FORMAT      Output: json, yaml, table

RESTORE:
  python cli.py restore <VM_NAME> --zone=<ZONE> [OPTIONS]

  Options:
    --project PROJECT    GCP project
    --keep-rescue-disk   Don't delete rescue disk after restore
    --quiet              No interactive prompts
```

### How It Works

```
RESCUE:
  1. Stop VM
  2. Create safety snapshot
  3. Detach boot disk
  4. Create rescue disk (Debian 12 or Windows Server 2022)
  5. Attach rescue disk as boot
  6. Attach original disk as secondary
  7. Start VM in rescue mode

RESTORE:
  1. Stop VM
  2. Detach rescue disk
  3. Re-attach original disk as boot
  4. Start VM normally
  5. Delete rescue disk
```

### After Rescue

| OS | Connection | Affected Disk Location |
|----|------------|------------------------|
| Linux | `gcloud compute ssh VM --zone=ZONE` | `/mnt/sysroot` |
| Windows | RDP (credentials shown after rescue) | `D:\` (or next available) |

### Requirements

- Python 3.9+
- `gcloud` CLI installed and authenticated
- IAM permissions: `compute.instances.*`, `compute.disks.*`, `compute.snapshots.create`

### Full Documentation

See [gce_rescue_v2/README.md](gce_rescue_v2/README.md) for complete documentation.

---

## GCE Rescue V1 (Legacy)

The original version of GCE Rescue. Linux support only.

### Installation

```bash
git clone https://github.com/GoogleCloudPlatform/gce-rescue.git
cd gce-rescue
python3 setup.py install --user
```

### Usage

```bash
# Enter rescue mode
gce-rescue --zone=us-central1-a --name=my-vm

# Exit rescue mode (run same command again)
gce-rescue --zone=us-central1-a --name=my-vm
```

### V1 Documentation

See the [V1 documentation](docs/v1-readme.md) for complete details.

---

## Authentication

Both versions use Application Default Credentials (ADC):

```bash
gcloud auth application-default login
```

More info: https://cloud.google.com/docs/authentication/provide-credentials-adc

---

## Permissions

Minimum IAM permissions required:

| Action | Permissions |
|--------|-------------|
| VM Control | `compute.instances.stop`, `compute.instances.start` |
| Disk Operations | `compute.instances.attachDisk`, `compute.instances.detachDisk`, `compute.disks.create`, `compute.disks.delete` |
| Snapshots | `compute.snapshots.create`, `compute.disks.createSnapshot` |
| Metadata | `compute.instances.setMetadata` |

---

## License

Apache License 2.0 - See [LICENSE](LICENSE) for details.

## Contact

GCE Rescue Team: gce-rescue-dev@google.com
