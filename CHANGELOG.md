# Changelog

All notable changes to GCE Rescue will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
