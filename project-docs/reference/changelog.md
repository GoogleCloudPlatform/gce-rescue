# Changelog

go/gce-rescue-changelog

<!--* freshness: { owner: 'gokull' reviewed: '2026-02-24' } *-->

[TOC]

All notable changes to GCE Rescue. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versioning follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 2.0.0 (2026-03-17)

### GA Release

First stable release of GCE Rescue V2. V1 remains available as `gce-rescue-v1`.

### Added

*   **Windows support** — Auto-detects OS, uses Windows Server 2022 rescue
    environment with PowerShell startup script and `rescue_admin` RDP account.
*   **`diagnose` command** — Read-only boot diagnostics from serial console
    output. Pattern matching for fstab, GRUB, kernel, filesystem, network, and
    storage errors. Multiple output formats (table, json, yaml).
*   **`repair` command** — Automated diagnose-fix-restore in one command
    (Linux only). Currently supports fstab error auto-fix.
*   **Automatic rollback** — LIFO rollback on failure for all operations.
*   **Session recovery** — Resume or rollback interrupted operations via
    checkpoint stored in VM metadata.
*   **Serial console verification** — Polls for startup script completion
    marker (120 second timeout, warning only).
*   **ARM64 support** — Automatic architecture detection, selects appropriate
    rescue image.
*   **Safety snapshots** — Pre-rescue backup snapshot by default.
*   **Pre-flight validation** — Credential, IAM permission, and VM state
    checks before any operation.
*   **Usage tracking** — User-Agent headers for adoption metrics.
*   **Install scripts** — One-line installers for Linux/macOS/Windows.

### Breaking changes

*   `gce-rescue` command now runs V2 (previously V1).
*   V1 available as `gce-rescue-v1` for backward compatibility.
*   Authentication requires ADC (`gcloud auth application-default login`).

## 2.0.0-beta.5 (2026-02-15)

### Added

*   **`diagnose` command** —Read-only boot diagnostics from serial console.
    Pattern library for fstab errors (UUID not found, device missing, mount
    failed, emergency mode, dependency failed, device timeout, fsck failed).
    OS detection and license type display. Tiered deduplication. Multiple output
    formats (table, json, yaml). Context lines showing surrounding serial output.

*   **`repair` command** —Automated diagnose-fix-restore in one command
    (Linux only). Embeds fix script in startup script (no SSH needed). Creates
    backup snapshot (default). Backs up modified files on disk. Structured
    result parsing from serial console markers. Supports `--no-snapshot`.
    Resume support for interrupted operations.

### Improved

*   Rescue and restore confirmations list all major changes before proceeding.
*   All repair failure paths show backup snapshot name for recovery.
*   Snapshot cleanup hint shown after successful restore.

### Fixed

*   Entry point in pyproject.toml corrected.
*   Snapshot name restored from checkpoint context on resume.
*   Graceful degradation when `instances.get` returns 403.

## 2.0.0-beta.4 (2026-02-02)

### Added

*   **ARM64 support** —Automatic architecture detection and rescue image
    selection. Detects ARM64 from disk architecture field or T2A machine type.
    Auto-selects `debian-12-arm64` rescue image.

*   **Unsupported VM blocking** —Blocks Shielded VMs with Secure Boot and
    Confidential VMs with clear error messages.

*   **Session recovery** —Resume or rollback interrupted rescue/restore
    operations. Checkpoint stored in VM metadata. Interactive prompt: Continue,
    Rollback, or Abort.

*   **Private network VM support** —Shows internal IP when no external IP
    exists. Provides IAP tunnel command.

### Fixed

*   Windows rescue password preserved across session recovery.
*   Completed checkpoints auto-cleared.

## 2.0.0-beta.3 (2026-01-14)

### Added

*   **Serial console verification** —Polls for `GCE-RESCUE-COMPLETE` marker
    (120 second timeout, warning only).
*   **Progress spinner** —Shows current phase with step count.
*   **Browser SSH option** —Cloud Console SSH URL in connection instructions.
*   **Safer confirmation default** —Changed default from Yes to No (`y/N`).
*   **Usage tracking** —User-Agent headers for internal metrics.
*   **Auto debug log** —Automatic log file on errors.

### Fixed

*   SCRATCH disk handling in restore.
*   User-Agent format consistency.

## 2.0.0-beta.2 (2026-01-07)

### Added

*   **Local SSD handling** —SCRATCH disks excluded from operations. Clear
    warning when Local SSD data will be lost.
*   **Enhanced error messages** —Specific suggestions for common failures.

### Fixed

*   Early validation for missing project ID.
*   Suppressed noisy httplib2 timeout warnings.

## 2.0.0-beta.1 (2025-12-11)

### Added

*   **Windows support** —Automatic OS detection. Windows Server 2022 rescue
    environment. PowerShell startup script. Auto-generated `rescue_admin`
    account for RDP.
*   **Auto-rollback** —LIFO rollback order on failure.
*   **Safety snapshots** —Async snapshot creation before rescue. `--no-snapshot`
    flag.
*   **New CLI** —Separate `rescue` and `restore` commands. `--quiet` mode.
    Multiple output formats.
*   **Modular architecture** —Individual operation classes. Orchestrators.
    Validators. State tracking.

### Fixed

*   XFS filesystem duplicate UUID handling with `nouuid` mount option.
*   XFS dirty journal handling with `norecovery` fallback.
*   Windows Guest Agent password reset timing.

## 1.x (Legacy)

See the
[original GCE Rescue repository](https://github.com/GoogleCloudPlatform/gce-rescue)
for V1 changelog.
