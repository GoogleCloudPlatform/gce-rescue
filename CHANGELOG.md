# Changelog

All notable changes to GCE Rescue will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0-beta.5] - 2026-02-15

### Added

- **`diagnose` command**: Read-only boot diagnostics from serial console
  - Pattern library for fstab errors (UUID not found, device missing, mount
    failed, emergency mode, dependency failed, device timeout, fsck failed)
  - OS detection and license type display (PAYG, BYOL, free-tier)
  - Tiered deduplication (category + description level)
  - Multiple output formats: table, json, yaml
  - Context lines showing surrounding serial output for each error

- **`repair` command**: Automated diagnose-fix-restore in one command (Linux only)
  - Embeds fix script directly in startup script (no SSH needed)
  - Creates backup snapshot before any changes (default: enabled)
  - Backs up modified files on disk (e.g., `/etc/fstab.gce-repair-backup`)
  - Structured result parsing from serial console markers
  - Multi-line progress display showing rescue/repair/restore phases
  - Supports `--no-snapshot` for faster repair
  - Resume support for interrupted repair operations
  - fstab fix script: comments out invalid UUID, device, and label entries

### Improved

- **Safety and transparency**: Users now see exactly what the tool will change
  - Rescue confirmation lists all major changes (disk detach, metadata
    replacement, rescue disk creation)
  - Restore confirmation shows disk swap, metadata restore, rescue disk deletion
  - All repair failure paths show backup snapshot name for recovery
  - Snapshot cleanup hint shown after successful restore with `gcloud` delete
    command
  - Repair results show fstab backup location on disk

- **Documentation**: Complete overhaul of internal docs
  - Architecture doc rewritten with diagrams and safety mechanism details
  - CLI reference updated with decision tree, diagnose and repair sections
  - FAQ updated with repair safety info, roadmap, and new feature tables
  - Contributing guide updated with fix script and boot pattern guides

### Fixed

- Entry point in pyproject.toml corrected from `gce-rescue` to `gce-rescue-v2`
- Snapshot-on-resume: snapshot name restored from checkpoint context
- Graceful degradation when `instances.get` returns 403

## [2.0.0-beta.4] - 2026-02-02

### Added

- **ARM64 Support**: Automatic architecture detection and rescue image selection
  - Detects ARM64 from disk architecture field or T2A machine type
  - Auto-selects `debian-12-arm64` rescue image for ARM64 instances
  - Works with Ampere Altra (T2A) VMs

- **Unsupported VM Blocking**: Clear validation errors for incompatible VM types
  - Blocks Shielded VMs with Secure Boot (can't boot unsigned rescue disk)
  - Blocks Confidential VMs (encrypted memory prevents external disk access)
  - Includes actionable suggestions in error messages

## [2.0.0-beta.4] - 2026-01-20

### Added

- **Session Recovery**: Resume or rollback interrupted rescue/restore operations
  - Checkpoint stored in VM metadata (`gce-rescue-checkpoint`)
  - Detects incomplete operations on next run
  - Interactive prompt: Continue, Rollback, or Abort
  - Idempotent operations handle partial state gracefully
  - Progress resumes from where it left off (e.g., `(3/5)` not `(0/5)`)
  - Log file persists across session recovery

- **Transitional State Handling**: Graceful handling of VM states
  - Handles STAGING, PROVISIONING, STOPPING states during resume
  - Waits for stable state before proceeding

- **Private Network VM Support**: Better UX for VMs without external IP
  - Shows internal IP when no external IP exists
  - Provides IAP tunnel command for RDP access

### Fixed

- Windows rescue password now preserved across session recovery
- Completed checkpoints auto-cleared to prevent stale state errors

## [2.0.0-beta.3] - 2025-01-14

### Added

- **Serial Console Verification**: Polls serial console for `GCE-RESCUE-COMPLETE` marker
  - 120 second timeout with warning (doesn't fail)
  - Confirms startup script actually ran successfully

- **Progress Spinner**: Visual progress indicator during operations
  - Shows current phase with step count (e.g., `(3/5) [Stopping -> ...]`)
  - Clean single-line updates

- **Browser SSH Option**: Added browser-based SSH connection option
  - Shows both gcloud CLI and Cloud Console SSH URL
  - Includes IAP tunnel hint for private VMs

- **Safer Confirmation Default**: Changed default from Yes to No (`y/N`)
  - Prevents accidental confirmations
  - Explicit 'y' required to proceed

- **Usage Tracking**: User-Agent headers for internal metrics
  - Tracks rescue/restore lifecycle phases
  - Hierarchical labels (rescue/validate, rescue/execute, etc.)

- **Auto Debug Log**: Automatic log file creation on errors
  - Saved to temp directory with timestamp
  - Includes full debug output for troubleshooting

- **Improved CLI Output**: gcloud-style formatting
  - Color-coded status (green OK, red FAIL, yellow WARN)
  - Better error messages with permission guidance
  - Clear next steps after rescue/restore

### Fixed

- SCRATCH disk handling in restore orchestration
- User-Agent format consistency (hyphen not underscore)
- Local SSD warning clarified: "data lost" instead of "disk deleted"

## [2.0.0-beta.2] - 2025-01-07

### Added

- **Local SSD Handling**: Proper handling of VMs with scratch disks
  - SCRATCH disks excluded from detach/restore operations
  - Clear warning when Local SSD data will be lost
  - Prevents restore failures on VMs with ephemeral storage

- **Enhanced Error Messages**: More actionable error guidance
  - Specific suggestions for common failure scenarios

### Fixed

- Early validation for missing project ID
- CLI updated to industry standard format
- Suppressed noisy httplib2 timeout warnings
- Resolved pylint warnings in V1 code

### Documentation

- Comprehensive V2 README rewrite with customer focus
- Added Linux and Windows example workflows
- Updated What's New comparison table

## [2.0.0-beta.1] - 2025-12-11

### Added

- **Windows Support**: Full support for Windows Server VMs
  - Automatic OS detection via `guestOsFeatures`, licenses, and disk source
  - Windows Server 2022 as rescue environment
  - PowerShell startup script for automatic disk mounting
  - Auto-generated `rescue_admin` account for RDP access
  - Credentials displayed immediately after rescue completes

- **Auto-Rollback**: Automatic rollback on failure
  - All operations tracked with rollback data
  - LIFO (Last In, First Out) rollback order
  - Graceful handling of partial failures

- **Safety Snapshots**: Snapshot creation before rescue (default enabled)
  - Async snapshot creation for faster operation
  - `--no-snapshot` flag to skip if needed
  - Snapshot verification before attaching affected disk

- **New CLI**: Simplified, gcloud-style interface
  - Separate `rescue` and `restore` commands
  - Minimal flags for beta (removed risky overrides)
  - `--quiet` mode for automation
  - Multiple output formats: json, yaml, table

- **Modular Architecture**: Clean separation of concerns
  - Individual operation classes (StopVM, StartVM, AttachDisk, etc.)
  - Orchestrators for rescue and restore workflows
  - Validators for pre-flight checks
  - State tracking for rollback

### Changed

- Default rescue image upgraded to Debian 12 (for XFS/Btrfs compatibility)
- Affected disk mount point renamed from "original disk" to "affected disk"
- Improved error messages with actionable suggestions

### Fixed

- XFS filesystem duplicate UUID handling with `nouuid` mount option
- XFS dirty journal handling with `norecovery` fallback
- Windows Guest Agent password reset timing issues

## [1.x] - Legacy

See the original GCE Rescue documentation for v1.x changes.
