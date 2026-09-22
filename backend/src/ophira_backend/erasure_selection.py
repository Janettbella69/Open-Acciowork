"""Bounded read-only domain selection for a human-reviewed erasure transaction."""

import stat

from .erasure_files import MAX_SOURCES_TOTAL, ErasureFile, ErasurePlan, digest
from .project_exports import ExportInventory


class ErasureSelection(ExportInventory):
    def __init__(self, root, authorize):
        super().__init__(root, authorize)
        self.changes = {}
        self.removed_directories = set()
        self.native_sessions = []
        self.guards = {}
        self.discovery_bytes = 0

    def read(self, relative, *, optional=False, record=False):
        raw = super().read(relative, optional=optional, record=record)
        self.discovery_bytes += len(raw or b"")
        if self.discovery_bytes > MAX_SOURCES_TOTAL:
            raise ValueError("Deletion discovery byte limit exceeded.")
        value = None if raw is None else (len(raw), digest(raw))
        if relative in self.guards and self.guards[relative] != value:
            raise ValueError("Data changed during deletion review.")
        self.guards[relative] = value
        return raw

    def change(self, path, after=None):
        before = self.read(path, optional=True)
        if before == after:
            return
        self.register_path(path)
        if path in self.changes and self.changes[path] != (before, after):
            raise ValueError("Overlapping deletion transformations.")
        self.changes[path] = (before, after)

    def remove_tree(self, relative, *, keep=(), depth=0):
        if depth > 32:
            raise ValueError("Deletion directory depth limit exceeded.")
        children = self.children(relative)
        for name, info in children:
            path = f"{relative}/{name}"
            if path in keep:
                continue
            if stat.S_ISDIR(info.st_mode):
                self.remove_tree(path, depth=depth + 1)
                self.removed_directories.add(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                self.change(path)
            else:
                raise ValueError("Deletion storage contains linked or special files.")

    def remove_entry(self, parent, name):
        for child, info in self.children(parent):
            if child != name:
                continue
            path = f"{parent}/{name}"
            if stat.S_ISDIR(info.st_mode):
                self.remove_tree(path)
                self.removed_directories.add(path)
            else:
                self.change(path)

    def plan(self, scope):
        files, replacements = [], {}
        for path, (before, after) in sorted(self.changes.items()):
            files.append(
                ErasureFile(
                    path=path,
                    before_size=None if before is None else len(before),
                    before_sha256=None if before is None else digest(before),
                    after_size=None if after is None else len(after),
                    after_sha256=None if after is None else digest(after),
                )
            )
            if after is not None:
                replacements[path] = after
        return ErasurePlan(
            schema_version=2,
            scope=scope,
            files=files,
            directories=sorted(self.removed_directories),
            native_sessions=self.native_sessions,
        ), replacements
