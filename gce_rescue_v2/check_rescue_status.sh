#!/bin/bash
# GCE Rescue - Status Check Script
# Run this on the rescue VM to see what happened

echo "=========================================="
echo "    GCE Rescue - Status Check"
echo "=========================================="
echo ""

# Check if we're in rescue mode
if [[ $(hostname) == *"-rescue" ]]; then
    echo "✓ Hostname: $(hostname) (rescue mode active)"
else
    echo "✗ Hostname: $(hostname) (NOT in rescue mode)"
fi
echo ""

# Check status file
if [ -f /var/run/gce-rescue.status ]; then
    status=$(cat /var/run/gce-rescue.status)
    if [ "$status" == "SUCCESS" ]; then
        echo "✓ Auto-mount Status: SUCCESS"
    else
        echo "✗ Auto-mount Status: $status"
    fi
else
    echo "? Auto-mount Status: Unknown (status file not found)"
fi
echo ""

# Check if /mnt/sysroot is mounted
if mountpoint -q /mnt/sysroot 2>/dev/null; then
    echo "✓ /mnt/sysroot: Mounted"
    df -h /mnt/sysroot | tail -1 | awk '{print "  Size: " $2 ", Used: " $3 ", Available: " $4 ", Use%: " $5}'
else
    echo "✗ /mnt/sysroot: NOT mounted"
fi
echo ""

# Check proc, sys, dev mounts
echo "Chroot support:"
mountpoint -q /mnt/sysroot/proc 2>/dev/null && echo "  ✓ proc mounted" || echo "  ✗ proc NOT mounted"
mountpoint -q /mnt/sysroot/sys 2>/dev/null && echo "  ✓ sys mounted" || echo "  ✗ sys NOT mounted"
mountpoint -q /mnt/sysroot/dev 2>/dev/null && echo "  ✓ dev mounted" || echo "  ✗ dev NOT mounted"
echo ""

# Show recent log entries
echo "=========================================="
echo "Recent Log (last 20 lines):"
echo "=========================================="
if [ -f /var/log/gce-rescue.log ]; then
    tail -20 /var/log/gce-rescue.log
else
    echo "Log file not found: /var/log/gce-rescue.log"
fi
echo ""

echo "=========================================="
echo "Full log: cat /var/log/gce-rescue.log"
echo "=========================================="
