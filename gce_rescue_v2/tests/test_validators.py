"""
Unit tests for validators.

Covers:
- CredentialsValidator
- IAMPermissionsValidator
- VMStateValidator
- ValidationRunner
"""

from unittest.mock import Mock, patch

import pytest
from google.auth.exceptions import DefaultCredentialsError
from googleapiclient.errors import HttpError

from gce_rescue_v2.validators.base import ValidationRunner, BaseValidator
from gce_rescue_v2.validators.credentials import CredentialsValidator
from gce_rescue_v2.validators.iam_permissions import IAMPermissionsValidator
from gce_rescue_v2.validators.vm_state import VMStateValidator


class TestCredentialsValidator:
    """Tests for CredentialsValidator."""

    @patch("google.auth.default")
    def test_valid_credentials(self, mock_default, mock_compute):
        """Test with valid credentials."""
        creds = Mock()
        creds.valid = True
        mock_default.return_value = (creds, "proj-1")

        validator = CredentialsValidator(mock_compute, "proj-1", "zone-1")
        result = validator.validate()

        assert result.passed is True
        assert "proj-1" in result.message
        assert result.details["credentials_type"] == type(creds).__name__

    @patch("google.auth.default")
    def test_invalid_credentials_refresh_fails(self, mock_default, mock_compute):
        """Test with invalid/expired credentials refresh error."""
        creds = Mock()
        creds.valid = False
        creds.refresh.side_effect = Exception("refresh failed")
        mock_default.return_value = (creds, "proj-1")

        validator = CredentialsValidator(mock_compute, "proj-1", "zone-1")
        result = validator.validate()

        assert result.passed is False
        assert "refresh failed" in result.details["error"]

    @patch("google.auth.default")
    def test_no_credentials(self, mock_default, mock_compute):
        """Test with no credentials configured."""
        mock_default.side_effect = DefaultCredentialsError("no creds")

        validator = CredentialsValidator(mock_compute, "proj-1", "zone-1")
        result = validator.validate()

        assert result.passed is False
        assert "No credentials found" in result.message
        assert "gcloud auth application-default login" in result.details["fix"]


class TestIAMPermissionsValidator:
    """Tests for IAMPermissionsValidator."""

    def _set_permissions(self, mock_compute, permissions):
        instances = mock_compute.instances.return_value
        tester = instances.testIamPermissions.return_value
        tester.execute.return_value = {"permissions": permissions}
        return instances

    def test_all_permissions_present(self, mock_compute):
        """Test when user has all required permissions."""
        validator = IAMPermissionsValidator(mock_compute, "proj", "zone", "vm-1")
        self._set_permissions(mock_compute, validator.INSTANCE_PERMISSIONS)

        result = validator.validate()

        assert result.passed is True
        assert len(result.details["granted"]) == len(validator.INSTANCE_PERMISSIONS)

    def test_missing_stop_permission(self, mock_compute):
        """Test when compute.instances.stop is missing."""
        validator = IAMPermissionsValidator(mock_compute, "proj", "zone", "vm-1")
        perms = [p for p in validator.INSTANCE_PERMISSIONS if p != "compute.instances.stop"]
        self._set_permissions(mock_compute, perms)

        result = validator.validate()

        assert result.passed is False
        assert "compute.instances.stop" in result.details["missing"]

    def test_partial_permissions(self, mock_compute):
        """Test when some permissions are missing."""
        validator = IAMPermissionsValidator(mock_compute, "proj", "zone", "vm-1")
        granted = validator.INSTANCE_PERMISSIONS[:2]  # only first two
        self._set_permissions(mock_compute, granted)

        result = validator.validate()

        assert result.passed is False
        for perm in validator.INSTANCE_PERMISSIONS[2:]:
            assert perm in result.details["missing"]

    def test_vm_not_found_is_skipped(self, mock_compute):
        """Test 404 on VM is treated as skip (handled later)."""
        from googleapiclient.errors import HttpError

        resp = Mock(status=404, reason="not found")
        instances = mock_compute.instances.return_value
        tester = instances.testIamPermissions.return_value
        tester.execute.side_effect = HttpError(resp, b"{}")

        validator = IAMPermissionsValidator(mock_compute, "proj", "zone", "vm-1")
        result = validator.validate()

        assert result.passed is True
        assert "Skipped" in result.message


class TestVMStateValidator:
    """Tests for VMStateValidator."""

    def _set_vm(self, mock_compute, payload):
        instances = mock_compute.instances.return_value
        instances.get.return_value.execute.return_value = payload
        return instances

    def test_vm_running(self, mock_compute):
        """Test VM in RUNNING state - should pass."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()
        assert result.passed is True
        assert result.details["current_state"] == "RUNNING"

    def test_vm_terminated(self, mock_compute):
        """Test VM in TERMINATED state - should pass."""
        payload = {
            "status": "TERMINATED",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()
        assert result.passed is True

    def test_vm_invalid_state(self, mock_compute):
        """Test VM in SUSPENDED state - should fail."""
        payload = {
            "status": "SUSPENDED",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()
        assert result.passed is False
        assert "invalid state" in result.message

    def test_vm_already_in_rescue(self, mock_compute):
        """Test VM already has rescue metadata."""
        payload = {
            "status": "RUNNING",
            "metadata": {"items": [{"key": "rescue-mode", "value": "123"}]},
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()
        assert result.passed is False
        assert "rescue mode" in result.message

    def test_vm_not_found(self, mock_compute):
        """Test VM does not exist."""
        resp = Mock(status=404, reason="Not Found")
        error = HttpError(resp, b"{}")
        instances = mock_compute.instances.return_value
        instances.get.return_value.execute.side_effect = error

        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")
        result = validator.validate()

        assert result.passed is False
        assert "not found" in result.message

    def test_vm_no_boot_disk(self, mock_compute):
        """Test VM with no boot disk."""
        payload = {"status": "RUNNING", "disks": []}
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is False
        assert "no boot disk" in result.message.lower()

    def test_confidential_vm_blocked(self, mock_compute):
        """Test Confidential VM is blocked with clear message."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
            "confidentialInstanceConfig": {"enableConfidentialCompute": True}
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is False
        assert "Confidential VM" in result.message
        assert "not supported" in result.message.lower()
        assert "fix" in result.details

    def test_confidential_vm_with_type(self, mock_compute):
        """Test Confidential VM shows type in error message."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
            "confidentialInstanceConfig": {
                "enableConfidentialCompute": True,
                "confidentialInstanceType": "SEV_SNP"
            }
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is False
        assert "SEV_SNP" in result.message

    def test_shielded_vm_secure_boot_allowed(self, mock_compute):
        """Test Shielded VM with Secure Boot is allowed (rescue image is signed)."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
            "shieldedInstanceConfig": {"enableSecureBoot": True}
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is True

    def test_shielded_vm_without_secure_boot_allowed(self, mock_compute):
        """Test Shielded VM without Secure Boot is allowed."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
            "shieldedInstanceConfig": {
                "enableSecureBoot": False,
                "enableVtpm": True,
                "enableIntegrityMonitoring": True
            }
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is True

    def test_non_confidential_non_shielded_vm_allowed(self, mock_compute):
        """Test regular VM without Confidential or Shielded is allowed."""
        payload = {
            "status": "RUNNING",
            "disks": [{"source": "projects/p/zones/z/disks/d", "boot": True, "deviceName": "d"}],
        }
        self._set_vm(mock_compute, payload)
        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")

        result = validator.validate()

        assert result.passed is True


class TestValidationRunner:
    """Tests for ValidationRunner."""

    class _PassValidator(BaseValidator):
        @property
        def name(self):
            return "Pass"

        def validate(self):
            from gce_rescue_v2.validators.base import ValidationResult

            return ValidationResult(validator_name=self.name, passed=True, message="ok")

    class _FailValidator(BaseValidator):
        @property
        def name(self):
            return "Fail"

        def validate(self):
            from gce_rescue_v2.validators.base import ValidationResult

            return ValidationResult(validator_name=self.name, passed=False, message="bad")

    def test_all_pass(self):
        """Test when all validators pass."""
        runner = ValidationRunner()
        runner.add(self._PassValidator(None, "p", "z"))
        runner.add(self._PassValidator(None, "p", "z"))

        results = runner.run_all()
        assert results.all_passed() is True
        assert len(results.get_failures()) == 0

    def test_one_fails(self):
        """Test when one validator fails."""
        runner = ValidationRunner()
        runner.add(self._PassValidator(None, "p", "z"))
        runner.add(self._FailValidator(None, "p", "z"))

        results = runner.run_all()
        assert results.all_passed() is False
        assert len(results.get_failures()) == 1

    def test_multiple_fail(self):
        """Test when multiple validators fail."""
        runner = ValidationRunner()
        runner.add(self._FailValidator(None, "p", "z"))
        runner.add(self._FailValidator(None, "p", "z"))

        results = runner.run_all()
        assert results.all_passed() is False
        assert len(results.get_failures()) == 2

    def test_empty_validators(self):
        """Test with no validators added."""
        runner = ValidationRunner()
        results = runner.run_all()
        assert results.all_passed() is True
        assert results.results == []

