# Boot Error Patterns

Add new boot error patterns by creating or editing YAML files in this directory.

## Quick Start

To add patterns for a new category (e.g., kernel errors), create `kernel.yaml`:

```yaml
category: kernel
fix_guidance: "Check kernel: ls /mnt/sysroot/boot/vmlinuz-*"

patterns:
  - name: kernel_panic
    severity: critical
    description: "Kernel panic - not syncing"
    regex:
      - 'Kernel panic - not syncing'
    fixes:
      - "Check if kernel exists: ls /mnt/sysroot/boot/vmlinuz-*"
      - "Reinstall kernel from rescue mode"
```

To add a pattern to an existing category, edit the corresponding YAML file and add a new
entry under `patterns:`.

## YAML Schema

### Top-level fields

| Field | Required | Description |
|-------|----------|-------------|
| `category` | Yes | Category name (e.g., `fstab`, `grub`, `kernel`, `filesystem`, `initramfs`) |
| `fix_guidance` | Yes | One-line summary shown in the consolidated fix section |
| `patterns` | Yes | List of pattern definitions |

### Pattern fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (snake_case, prefixed with category) |
| `severity` | Yes | One of: `critical`, `error`, `warning` |
| `description` | Yes | Human-readable description of the error |
| `regex` | Yes | List of regex patterns to match in serial console output |
| `fixes` | Yes | List of suggested fix steps |

### Severity levels

- **critical** - VM cannot boot (e.g., missing root filesystem, kernel panic)
- **error** - Boot is impaired (e.g., non-root mount failure)
- **warning** - Potential issue detected (e.g., slow fsck)

## Regex Tips

- Patterns are matched with `re.MULTILINE | re.IGNORECASE`
- Use `.*` for wildcards, `[\w-]+` for word characters with hyphens
- Escape backslashes in YAML strings: use `\\w` in double-quoted strings, or `\w` in single-quoted strings
- Test your regex against real serial console output before submitting

## Testing a New Pattern

```bash
# Run the pattern loader tests to validate all YAML files
python -m pytest gce_rescue_v2/tests/test_pattern_loader.py -v

# Test against a real VM
python -m gce_rescue_v2.cli diagnose VM_NAME --zone=ZONE --project=PROJECT
```

## File Naming

- One file per category: `fstab.yaml`, `grub.yaml`, `kernel.yaml`, etc.
- Files are loaded alphabetically; order does not affect matching.
