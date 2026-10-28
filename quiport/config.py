"""Configuration and directory layout for an export run."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .quipclient import DEFAULT_BASE_URL

# Folder that everything the user cares about lives under, per the spec.
CONTENT_DIR_NAME = "Imported from Quip"


@dataclass
class Config:
    export_id: str = "default"
    base_dir: Path = field(default_factory=lambda: Path("."))
    token: str = ""
    base_url: str = DEFAULT_BASE_URL
    sharing: str = "full"  # "full" (3 extra calls/thread) or "lean" (V1 bulk only)
    include_archive: bool = True
    include_starred: bool = True
    retry_errors: bool = False
    quick_sweep: bool = False
    max_filename_len: int = 80

    # -- directories -----------------------------------------------------

    @property
    def root(self) -> Path:
        return self.base_dir / f"export-{self.export_id}"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def content_dir(self) -> Path:
        return self.root / CONTENT_DIR_NAME

    @property
    def extras_dir(self) -> Path:
        return self.root / "_extras"

    @property
    def files_dir(self) -> Path:
        """Blobs live beside content so relative links survive a folder move."""
        return self.content_dir / "_files"

    def ensure_dirs(self) -> None:
        for d in (
            self.root, self.raw_dir, self.state_dir, self.content_dir,
            self.extras_dir, self.extras_dir / "anomalies",
            self.extras_dir / "notes", self.extras_dir / "api-probes",
            self.files_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_args(cls, args) -> "Config":
        token = getattr(args, "token", None) or os.environ.get("QUIP_TOKEN", "")
        return cls(
            export_id=getattr(args, "export_id", None) or "default",
            base_dir=Path(getattr(args, "base_dir", ".")),
            token=token,
            base_url=getattr(args, "base_url", None) or DEFAULT_BASE_URL,
            sharing=getattr(args, "sharing", None) or "full",
            retry_errors=bool(getattr(args, "retry_errors", False)),
            quick_sweep=bool(getattr(args, "quick_sweep", False)),
        )

    @staticmethod
    def add_arguments(parser) -> None:
        parser.add_argument("--export-id", default=os.environ.get("QUIP_EXPORT_ID", "default"),
                            help="names the output directory: export-<id>")
        parser.add_argument("--base-dir", default=".", help="where export-<id> is created")
        parser.add_argument("--token", default=None, help="or set QUIP_TOKEN")
        parser.add_argument("--base-url", default=None, help="VPC customers: change this")
        parser.add_argument("--sharing", choices=("full", "lean"), default="full",
                            help="full adds 3 calls/thread for exact membership; "
                                 "lean derives it from the V1 bulk response")
        parser.add_argument("--quick-sweep", action="store_true",
                            help="skip the /1/threads/recent verification pass "
                                 "during discovery. Faster, but one fewer "
                                 "independent check that no thread was missed")
        parser.add_argument("--retry-errors", action="store_true",
                            help="re-request calls whose cached result was an error")
