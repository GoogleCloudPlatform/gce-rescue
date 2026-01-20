"""
GCE Rescue - Checkpoint Manager

Manages operation checkpoints for resumable rescue/restore operations.
Checkpoints are stored in VM metadata to enable recovery from interrupted sessions.
"""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

# Metadata key for checkpoint data
CHECKPOINT_KEY = 'gce-rescue-checkpoint'

# Schema version for future compatibility
CHECKPOINT_VERSION = 1


@dataclass
class CompletedOperation:
    """Record of a completed operation with rollback data."""
    name: str
    step: int
    rollback_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'name': self.name,
            'step': self.step,
            'rollback_data': self.rollback_data
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CompletedOperation':
        """Create from dictionary."""
        return cls(
            name=data['name'],
            step=data['step'],
            rollback_data=data.get('rollback_data', {})
        )


@dataclass
class CheckpointData:
    """
    Complete checkpoint state for a rescue/restore operation.

    This captures everything needed to resume or rollback an interrupted operation.
    """
    version: int
    operation: str  # 'rescue' or 'restore'
    session_id: str
    started_at: str
    updated_at: str
    current_step: int
    total_steps: int
    completed_operations: List[CompletedOperation]
    context: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            'version': self.version,
            'operation': self.operation,
            'session_id': self.session_id,
            'started_at': self.started_at,
            'updated_at': self.updated_at,
            'current_step': self.current_step,
            'total_steps': self.total_steps,
            'completed_operations': [op.to_dict() for op in self.completed_operations],
            'context': self.context
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CheckpointData':
        """Create from dictionary."""
        return cls(
            version=data.get('version', 1),
            operation=data['operation'],
            session_id=data['session_id'],
            started_at=data['started_at'],
            updated_at=data['updated_at'],
            current_step=data['current_step'],
            total_steps=data['total_steps'],
            completed_operations=[
                CompletedOperation.from_dict(op)
                for op in data.get('completed_operations', [])
            ],
            context=data.get('context', {})
        )

    @classmethod
    def from_json(cls, json_str: str) -> 'CheckpointData':
        """Create from JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)

    def get_last_completed_operation(self) -> Optional[str]:
        """Get the name of the last completed operation."""
        if self.completed_operations:
            return self.completed_operations[-1].name
        return None

    def get_next_step_number(self) -> int:
        """Get the next step number to execute (1-indexed)."""
        return self.current_step + 1

    def get_age_seconds(self) -> float:
        """Get the age of this checkpoint in seconds."""
        started = datetime.fromisoformat(self.started_at.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        return (now - started).total_seconds()

    def get_age_display(self) -> str:
        """Get human-readable age string."""
        seconds = self.get_age_seconds()
        if seconds < 60:
            return f"{int(seconds)} seconds ago"
        minutes = int(seconds / 60)
        if minutes < 60:
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = int(minutes / 60)
        if hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = int(hours / 24)
        return f"{days} day{'s' if days != 1 else ''} ago"


class CheckpointManager:
    """
    Manages checkpoint persistence for resumable operations.

    Checkpoints are stored in VM metadata under the key 'gce-rescue-checkpoint'.
    This allows recovery from interrupted sessions by detecting incomplete
    operations when the same command is run again.

    Example:
        mgr = CheckpointManager(compute, project, zone, vm_name, logger)

        # Start tracking a new rescue operation
        session_id = mgr.create_checkpoint('rescue', total_steps=9, context={...})

        # After each successful operation
        mgr.update_checkpoint(step=1, operation_name='Stop VM', rollback_data={...})

        # On successful completion
        mgr.clear_checkpoint()

        # To check for incomplete operation
        checkpoint = mgr.load_checkpoint()
        if checkpoint:
            # Incomplete operation found - prompt user
            pass
    """

    # Maximum retries for metadata operations
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    # Stale threshold in hours (checkpoints older than this get auto-prompted)
    STALE_THRESHOLD_HOURS = 1.0

    def __init__(self, compute, project: str, zone: str, vm_name: str, logger=None):
        """
        Initialize checkpoint manager.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            vm_name: Name of VM being operated on
            logger: Optional logger for debug output
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.logger = logger
        self._current_session_id: Optional[str] = None

    def _log_debug(self, message: str):
        """Log debug message with component prefix."""
        if self.logger:
            self.logger.debug(f"[Checkpoint] {message}", stacklevel=2)

    def _log_info(self, message: str):
        """Log info message."""
        if self.logger:
            self.logger.info(message)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def _get_vm_metadata(self) -> Tuple[Dict[str, str], str]:
        """
        Get current VM metadata as dict and fingerprint.

        Returns:
            Tuple of (metadata_dict, fingerprint)
        """
        vm = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        metadata = vm.get('metadata', {})
        fingerprint = metadata.get('fingerprint', '')
        items = metadata.get('items', [])

        metadata_dict = {item['key']: item['value'] for item in items}
        return metadata_dict, fingerprint

    def _set_vm_metadata(self, metadata_dict: Dict[str, str], fingerprint: str) -> bool:
        """
        Set VM metadata with retry logic.

        Args:
            metadata_dict: Metadata as key-value dict
            fingerprint: Metadata fingerprint for optimistic locking

        Returns:
            True if successful, False otherwise
        """
        import time

        items = [{'key': k, 'value': v} for k, v in metadata_dict.items()]
        body = {
            'fingerprint': fingerprint,
            'items': items
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                operation = self.compute.instances().setMetadata(
                    project=self.project,
                    zone=self.zone,
                    instance=self.vm_name,
                    body=body
                ).execute()

                # Wait for operation to complete
                if self._wait_for_operation(operation):
                    return True

            except Exception as e:
                self._log_debug(f"Metadata set attempt {attempt + 1} failed: {e}")

                # Refresh fingerprint on conflict
                if 'fingerprint' in str(e).lower() or 'precondition' in str(e).lower():
                    try:
                        _, fingerprint = self._get_vm_metadata()
                        body['fingerprint'] = fingerprint
                    except Exception:
                        pass

                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)

        return False

    def _wait_for_operation(self, operation: dict, timeout: int = 60) -> bool:
        """Wait for a GCP operation to complete."""
        import time

        operation_name = operation.get('name')
        if not operation_name:
            return True

        start_time = time.time()
        while True:
            if time.time() - start_time > timeout:
                return False

            try:
                result = self.compute.zoneOperations().get(
                    project=self.project,
                    zone=self.zone,
                    operation=operation_name
                ).execute()

                if result.get('status') == 'DONE':
                    return 'error' not in result

            except Exception:
                pass

            time.sleep(1)

    def create_checkpoint(self, operation_type: str, total_steps: int,
                         context: Dict[str, Any]) -> str:
        """
        Create initial checkpoint for a new operation.

        Args:
            operation_type: 'rescue' or 'restore'
            total_steps: Total number of steps in the operation
            context: Operation context (vm_name, original_disk, etc.)

        Returns:
            Session ID for this operation
        """
        session_id = str(uuid.uuid4())[:8]
        self._current_session_id = session_id

        now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        checkpoint = CheckpointData(
            version=CHECKPOINT_VERSION,
            operation=operation_type,
            session_id=session_id,
            started_at=now,
            updated_at=now,
            current_step=0,
            total_steps=total_steps,
            completed_operations=[],
            context=context
        )

        self._log_debug(f"Creating checkpoint: session={session_id}, operation={operation_type}")

        # Get current metadata and add checkpoint
        metadata_dict, fingerprint = self._get_vm_metadata()
        metadata_dict[CHECKPOINT_KEY] = checkpoint.to_json()

        if not self._set_vm_metadata(metadata_dict, fingerprint):
            self._log_error("Warning: Failed to save initial checkpoint to metadata")

        return session_id

    def update_checkpoint(self, step: int, operation_name: str,
                         rollback_data: Dict[str, Any] = None,
                         context_updates: Dict[str, Any] = None) -> bool:
        """
        Update checkpoint after a successful operation step.

        Args:
            step: Step number that just completed (1-indexed)
            operation_name: Name of the completed operation
            rollback_data: Data needed to rollback this operation
            context_updates: Optional context updates

        Returns:
            True if checkpoint was saved successfully
        """
        self._log_debug(f"Updating checkpoint: step={step}, operation={operation_name}")

        # Load current checkpoint
        checkpoint = self.load_checkpoint()
        if not checkpoint:
            self._log_error("Warning: No checkpoint found to update")
            return False

        # Verify session ID matches
        if checkpoint.session_id != self._current_session_id:
            self._log_error(f"Warning: Session ID mismatch: {checkpoint.session_id} vs {self._current_session_id}")

        # Add completed operation
        completed_op = CompletedOperation(
            name=operation_name,
            step=step,
            rollback_data=rollback_data or {}
        )
        checkpoint.completed_operations.append(completed_op)
        checkpoint.current_step = step
        checkpoint.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

        # Update context if provided
        if context_updates:
            checkpoint.context.update(context_updates)

        # Save updated checkpoint
        metadata_dict, fingerprint = self._get_vm_metadata()
        metadata_dict[CHECKPOINT_KEY] = checkpoint.to_json()

        if not self._set_vm_metadata(metadata_dict, fingerprint):
            self._log_error("Warning: Failed to save checkpoint update to metadata")
            return False

        return True

    def load_checkpoint(self) -> Optional[CheckpointData]:
        """
        Load checkpoint from VM metadata.

        Returns:
            CheckpointData if checkpoint exists, None otherwise
        """
        try:
            metadata_dict, _ = self._get_vm_metadata()
            checkpoint_json = metadata_dict.get(CHECKPOINT_KEY)

            if not checkpoint_json:
                return None

            return CheckpointData.from_json(checkpoint_json)

        except json.JSONDecodeError as e:
            self._log_error(f"Warning: Corrupted checkpoint data: {e}")
            return None
        except Exception as e:
            self._log_debug(f"Error loading checkpoint: {e}")
            return None

    def clear_checkpoint(self) -> bool:
        """
        Clear checkpoint from VM metadata (on successful completion).

        Returns:
            True if cleared successfully
        """
        self._log_debug("Clearing checkpoint")

        try:
            metadata_dict, fingerprint = self._get_vm_metadata()

            if CHECKPOINT_KEY in metadata_dict:
                del metadata_dict[CHECKPOINT_KEY]
                return self._set_vm_metadata(metadata_dict, fingerprint)

            return True

        except Exception as e:
            self._log_error(f"Warning: Failed to clear checkpoint: {e}")
            return False

    def detect_incomplete(self, operation_type: str = None) -> Optional[CheckpointData]:
        """
        Detect if there's an incomplete operation for this VM.

        Args:
            operation_type: Optional filter for specific operation type ('rescue' or 'restore')

        Returns:
            CheckpointData if incomplete operation found, None otherwise
        """
        checkpoint = self.load_checkpoint()

        if not checkpoint:
            return None

        # Filter by operation type if specified
        if operation_type and checkpoint.operation != operation_type:
            return None

        # Check if operation is incomplete (not all steps done)
        if checkpoint.current_step < checkpoint.total_steps:
            self._log_debug(
                f"Incomplete operation detected: {checkpoint.operation} "
                f"({checkpoint.current_step}/{checkpoint.total_steps} steps)"
            )
            return checkpoint

        # Checkpoint exists but operation is complete - auto-clear stale checkpoint
        # This handles the case where Ctrl+C happened after last step but before cleanup
        self._log_debug(
            f"Found completed checkpoint ({checkpoint.current_step}/{checkpoint.total_steps}), "
            "auto-clearing stale checkpoint"
        )
        self.clear_checkpoint()
        return None

    def is_stale(self, checkpoint: CheckpointData) -> bool:
        """
        Check if a checkpoint is stale (older than threshold).

        Stale checkpoints may indicate abandoned operations and can be
        safely prompted without requiring --force.

        Args:
            checkpoint: Checkpoint to check

        Returns:
            True if checkpoint is stale
        """
        age_hours = checkpoint.get_age_seconds() / 3600
        return age_hours > self.STALE_THRESHOLD_HOURS

    def is_concurrent(self, checkpoint: CheckpointData) -> bool:
        """
        Check if checkpoint appears to be from a concurrent/active session.

        A checkpoint is considered potentially concurrent if:
        - It's recent (not stale)
        - Session ID doesn't match current session

        Args:
            checkpoint: Checkpoint to check

        Returns:
            True if checkpoint may be from concurrent session
        """
        if self.is_stale(checkpoint):
            return False

        if self._current_session_id and checkpoint.session_id == self._current_session_id:
            return False

        return True

    def set_session_id(self, session_id: str):
        """
        Set the current session ID (for resuming operations).

        Args:
            session_id: Session ID to adopt
        """
        self._current_session_id = session_id

    def get_rollback_operations(self, checkpoint: CheckpointData) -> List[CompletedOperation]:
        """
        Get completed operations in reverse order for rollback.

        Args:
            checkpoint: Checkpoint containing completed operations

        Returns:
            List of CompletedOperation in reverse order
        """
        return list(reversed(checkpoint.completed_operations))
