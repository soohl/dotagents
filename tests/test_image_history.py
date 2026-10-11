"""Saved sessions survive a new store and delete independently."""

from pathlib import Path
import tempfile
import unittest
import uuid

from src.image_history import ImageHistory


class HistoryTests(unittest.TestCase):
    def test_restart_references_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reference = root / "upload.png"
            reference.write_bytes(b"reference")
            store = ImageHistory(root / "saved")
            first, second = uuid.uuid4().hex, uuid.uuid4().hex
            entry = dict(prompt="Original prompt", seed=42, steps=20, size="1024x1024", model="image")
            store.append(first, entry, b"image", [reference])
            store.append(second, dict(entry, prompt="Another session"), b"other", [])
            reference.unlink()
            restarted = ImageHistory(root / "saved")
            saved = restarted.read(first)[0]
            self.assertEqual(Path(saved["image"]).read_bytes(), b"image")
            self.assertEqual(Path(saved["references"][0]).read_bytes(), b"reference")
            self.assertEqual(saved["seed"], 42)
            self.assertEqual(len(restarted.choices()), 2)
            restarted.delete(first)
            self.assertFalse((root / "saved" / first).exists())
            self.assertEqual(len(restarted.read(second)), 1)
            with self.assertRaises(ValueError):
                restarted.delete("../saved")
