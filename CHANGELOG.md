# Changelog

All notable changes to GCE Rescue will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
