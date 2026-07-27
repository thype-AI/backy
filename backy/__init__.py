"""Database backup sidecar: dump a sibling container's database, upload it, shout on failure."""


class BackupError(Exception):
    """Anything that makes the backup untrustworthy. Never swallowed, always notified."""
