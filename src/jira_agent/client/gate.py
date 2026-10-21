import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from jira_agent.config import WriteMode, default_home, private_dir


class ConfirmationRequired(Exception):
    def __init__(self, action: str, preview: dict):
        self.action = action
        self.preview = preview
        super().__init__(f"confirmation required for: {action}")


class WriteGate:
    def __init__(
        self,
        write_mode: WriteMode,
        confirmed: bool = False,
        site_url: str = "",
        audit_path: Path | None = None,
    ):
        self.write_mode = write_mode
        self.confirmed = confirmed
        self.site_url = site_url
        self.audit_path = audit_path or default_home() / "audit.log"

    def run(self, action: str, preview: dict, fn: Callable[[], object]):
        if self.write_mode == WriteMode.DRY_RUN:
            return {"dry_run": True, "action": action, "preview": preview}
        if not self.confirmed:
            raise ConfirmationRequired(action, preview)
        result = fn()
        self._audit(action, preview, result)
        return result

    def _audit(self, action: str, preview: dict, result) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "site": self.site_url,
            "action": action,
            "target": preview.get("target"),
            "fields": sorted((preview.get("fields") or {}).keys()),
            "result_key": result.get("key") if isinstance(result, dict) else None,
        }
        private_dir(self.audit_path.parent)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
