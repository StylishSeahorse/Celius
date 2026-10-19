"""On-disk layout under STORAGE_ROOT:

quarantine/<upload id>.part   uploads in progress (never served)
blobs/<sha[:2]>/<sha256>      stored content, deduplicated by hash
thumbnails/<file id>.webp
"""

import os
import shutil
from pathlib import Path


class Storage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def quarantine(self, upload_id) -> Path:
        path = self.root / "quarantine" / f"{upload_id}.part"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def blob(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError("invalid content hash")
        return self.root / "blobs" / sha256[:2] / sha256

    def thumbnail(self, file_id) -> Path:
        return self.root / "thumbnails" / f"{file_id}.webp"

    def commit(self, quarantined: Path, sha256: str) -> Path:
        """Move verified content into place. Identical content is stored once."""
        target = self.blob(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            quarantined.unlink(missing_ok=True)
        else:
            os.replace(quarantined, target)  # atomic on the same filesystem
        return target

    def write_new(self, data_path: Path, sha256: str) -> Path:
        """Store a file the server produced itself (e.g. an export)."""
        target = self.blob(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            tmp = target.with_suffix(".tmp")
            shutil.copyfile(data_path, tmp)
            os.replace(tmp, target)
        return target

    def remove_blob(self, sha256: str) -> None:
        self.blob(sha256).unlink(missing_ok=True)
