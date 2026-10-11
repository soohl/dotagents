"""Persistent image sessions. Each manifest is replaced atomically."""

import json
import os
from pathlib import Path
import re
import shutil
import uuid


class ImageHistory:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def directory(self, key):
        if not re.fullmatch(r"[0-9a-f]{32}", key or ""):
            raise ValueError("Invalid session")
        return self.root / key

    def read(self, key):
        if not key:
            return []
        path = self.directory(key) / "session.json"
        return json.loads(path.read_text()) if path.exists() else []

    def choices(self):
        paths = sorted(self.root.glob("*/session.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [(json.loads(p.read_text())[0]["prompt"][:60], p.parent.name) for p in paths]

    def append(self, key, entry, image, references, mask=None):
        directory = self.directory(key)
        directory.mkdir(exist_ok=True, mode=0o700)
        entries = self.read(key)
        assets = directory / uuid.uuid4().hex
        assets.mkdir(mode=0o700)
        try:
            output = assets / f"{assets.name}.png"
            output.write_bytes(image)
            saved = []
            for index, source in enumerate(references):
                target = assets / f"{assets.name}-reference-{index}{Path(source).suffix}"
                shutil.copyfile(source, target)
                saved.append(str(target))
            entry = dict(entry, image=str(output), references=saved)
            if mask is not None:
                target = assets / "mask.png"
                target.write_bytes(mask)
                entry["edit"] = dict(entry["edit"], mask=str(target))
            entries.append(entry)
            temporary = directory / "session.tmp"
            temporary.write_text(json.dumps(entries))
            os.replace(temporary, directory / "session.json")
        except Exception:
            shutil.rmtree(assets)
            raise
        return entries

    def delete(self, key):
        if key:
            shutil.rmtree(self.directory(key), ignore_errors=True)
