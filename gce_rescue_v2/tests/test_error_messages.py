"""Tests for error-message suggestion mapping (core/error_messages.py)."""

from gce_rescue_v2.core.error_messages import (
    get_error_suggestion,
    TRUSTED_IMAGE_BLOCKED,
    PERMISSION_DENIED,
    MACHINE_TYPE_CHANGE_FAILED,
    DISK_CREATE_FAILED,
    VM_NOT_FOUND,
)


class TestTrustedImagePolicyMapping:
    """Issue #122: trustedImageProjects org-policy errors get an actionable message."""

    def test_matches_constraint_violation(self):
        err = ("Constraint constraints/compute.trustedImageProjects violated for"
               " projects/my-proj. Image projects/debian-cloud/global/images/family/"
               "debian-12 is not in the trusted image projects list.")
        assert get_error_suggestion(err, operation='create_disk') is TRUSTED_IMAGE_BLOCKED

    def test_matches_trusted_image_phrase(self):
        err = "Image is not from a trusted image project."
        assert get_error_suggestion(err) is TRUSTED_IMAGE_BLOCKED

    def test_wins_over_permission_match(self):
        """A 403/forbidden-flavored policy error still maps to the policy hint."""
        err = ("403 Forbidden: Constraint constraints/compute.trustedImageProjects"
               " violated.")
        assert get_error_suggestion(err, operation='create_disk') is TRUSTED_IMAGE_BLOCKED

    def test_formatted_message_points_at_rescue_image(self):
        out = TRUSTED_IMAGE_BLOCKED.format(
            vm_name='vm-1', zone='us-central1-a', project='my-proj'
        )
        assert '--rescue-image' in out
        assert 'trustedImageProjects' in out

    def test_plain_permission_error_unaffected(self):
        """A normal permission error still maps to PERMISSION_DENIED (no regression)."""
        assert get_error_suggestion("Permission denied", operation='create_disk') is PERMISSION_DENIED


class TestMachineTypeChangeMapping:
    """Issue #204: machine type change errors are not reported as disk errors."""

    def test_cpu_quota(self):
        err = "Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1."
        assert (get_error_suggestion(err, operation='set_machine_type')
                is MACHINE_TYPE_CHANGE_FAILED)

    def test_unknown_machine_type(self):
        err = ("The resource 'projects/p/zones/us-central1-a/machineTypes/"
               "e2-huge' was not found")
        assert (get_error_suggestion(err, operation='set_machine_type')
                is MACHINE_TYPE_CHANGE_FAILED)

    def test_missing_vm_still_maps_to_vm_not_found(self):
        err = "The resource 'projects/p/zones/z/instances/vm-1' was not found"
        assert get_error_suggestion(err, operation='set_machine_type') is VM_NOT_FOUND

    def test_other_operations_unaffected(self):
        err = "Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1."
        assert get_error_suggestion(err, operation='create_disk') is DISK_CREATE_FAILED
