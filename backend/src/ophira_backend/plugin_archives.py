"""Bounded inspection of Claude plugin ZIPs without loading their runtime components."""

from __future__ import annotations

import io
import json
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .connectors import check_commercial_https_endpoint
from .skills import MAX_SKILL_ARCHIVE_BYTES, MAX_SKILL_UNPACKED_BYTES

MAX_PLUGIN_FILES = 256
MAX_PLUGIN_SKILLS = 20
MAX_PLUGIN_MCP_SERVERS = 20
_PLUGIN_NAME = re.compile(r"[a-z][a-z0-9-]{0,99}")


@dataclass(frozen=True)
class PluginSkillPackage:
    path: str
    files: dict[str, bytes]

    def zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(self.files.items()):
                archive.writestr(path, content)
        return buffer.getvalue()


@dataclass(frozen=True)
class PluginMcpReference:
    name: str
    url: str
    credentials_required: bool


@dataclass(frozen=True)
class PluginArchive:
    name: str
    version: str | None
    description: str | None
    skills: tuple[PluginSkillPackage, ...]
    mcp_servers: tuple[PluginMcpReference, ...]
    ignored_agents: tuple[str, ...]
    ignored_components: tuple[str, ...]


def _json_object(raw: bytes, label: str) -> dict:
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f"{label} contains duplicate keys")
            result[name] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _unpack(payload: bytes) -> dict[str, bytes]:
    if not payload or len(payload) > MAX_SKILL_ARCHIVE_BYTES:
        raise ValueError("Plugin ZIP is empty or exceeds the 2 MB upload limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("Plugin archive is not a ZIP file") from exc
    with archive:
        entries = archive.infolist()
        if (
            len(entries) > MAX_PLUGIN_FILES
            or sum(item.file_size for item in entries) > MAX_SKILL_UNPACKED_BYTES
        ):
            raise ValueError("Plugin archive exceeds file or expanded byte limits")
        normalized: set[str] = set()
        kinds: dict[str, bool] = {}
        files: dict[str, bytes] = {}
        total = 0
        for item in entries:
            name = item.filename
            path = PurePosixPath(name)
            segments = name.rstrip("/").split("/")
            mode = item.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if (
                not name or len(name) > 300 or "\\" in name or "\x00" in name
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in segments)
                or (path.parts and path.parts[0].endswith(":"))
                or item.flag_bits & 1
                or stat.S_ISLNK(mode)
                or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
            ):
                raise ValueError("Plugin archive contains an unsafe entry")
            folded = path.as_posix().rstrip("/").casefold()
            if folded in normalized:
                raise ValueError("Plugin archive contains ambiguous duplicate paths")
            normalized.add(folded)
            kinds[folded] = item.is_dir()
            if item.is_dir():
                continue
            try:
                with archive.open(item) as source:
                    raw = source.read(MAX_SKILL_UNPACKED_BYTES - total + 1)
            except (NotImplementedError, RuntimeError) as exc:
                raise ValueError("Plugin archive uses unsupported ZIP features") from exc
            total += len(raw)
            if total > MAX_SKILL_UNPACKED_BYTES or len(raw) != item.file_size:
                raise ValueError("Plugin archive exceeded its declared expanded size")
            files[path.as_posix()] = raw
        for path in normalized:
            parts = path.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                if parent in kinds and not kinds[parent]:
                    raise ValueError("Plugin archive contains conflicting paths")
        return files


def _component_paths(value: object, label: str) -> list[str]:
    if isinstance(value, str):
        paths = [value]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        paths = value
    else:
        raise ValueError(f"Plugin {label} paths are invalid")
    if len(paths) > 20:
        raise ValueError(f"Plugin {label} declares too many paths")
    result = []
    for path in paths:
        if path in {".", "./"} and label == "skills":
            result.append("")
            continue
        if not path.startswith("./") or "\\" in path:
            raise ValueError(f"Plugin {label} path must begin with ./")
        parts = path[2:].rstrip("/").split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError(f"Plugin {label} path escapes its package")
        result.append("/".join(parts))
    return result


def _skill_roots(files: dict[str, bytes], manifest: dict) -> list[str]:
    roots = []
    search = ["skills"]
    if "skills" in manifest:
        search.extend(_component_paths(manifest["skills"], "skills"))
    elif not any(path.startswith("skills/") for path in files) and "SKILL.md" in files:
        search.append("")
    for directory in search:
        direct = f"{directory}/SKILL.md" if directory else "SKILL.md"
        if direct in files:
            roots.append(directory)
        prefix = f"{directory}/" if directory else ""
        for path in files:
            if path.startswith(prefix) and path.endswith("/SKILL.md"):
                suffix = path[len(prefix):]
                if suffix.count("/") == 1:
                    roots.append(path.removesuffix("/SKILL.md"))
    unique = sorted(set(roots))
    if len(unique) > MAX_PLUGIN_SKILLS:
        raise ValueError("Plugin contains too many Skills")
    if any(a and b.startswith(a + "/") for a in unique for b in unique if a != b):
        raise ValueError("Plugin Skill roots overlap")
    return unique


def _mcp_sources(files: dict[str, bytes], manifest: dict) -> list[dict]:
    sources = []
    if ".mcp.json" in files:
        sources.append(_json_object(files[".mcp.json"], ".mcp.json"))
    declaration = manifest.get("mcpServers")
    if declaration is None:
        return sources
    for item in declaration if isinstance(declaration, list) else [declaration]:
        if isinstance(item, dict):
            sources.append(item)
        elif isinstance(item, str):
            for path in _component_paths(item, "mcpServers"):
                if path not in files:
                    raise ValueError("Plugin MCP configuration file is missing")
                if path == ".mcp.json" and ".mcp.json" in files:
                    continue
                sources.append(_json_object(files[path], path))
        else:
            raise ValueError("Plugin MCP configuration is invalid")
    return sources


def _remote_mcp_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2000 or "${" in value:
        return None
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").rstrip(".").casefold()
        if (
            parts.port not in {None, 443}
            or host.endswith((".local", ".localhost", ".test", ".invalid"))
            or check_commercial_https_endpoint(value)
        ):
            return None
    except ValueError:
        return None
    return value


def inspect_plugin_archive(payload: bytes) -> PluginArchive:
    uploaded = _unpack(payload)
    manifests = [
        path for path in uploaded if path.endswith("/.claude-plugin/plugin.json")
        or path == ".claude-plugin/plugin.json"
    ]
    if len(manifests) != 1:
        raise ValueError("Plugin ZIP requires one .claude-plugin/plugin.json")
    prefix = manifests[0].removesuffix(".claude-plugin/plugin.json")
    if any(not path.startswith(prefix) for path in uploaded):
        raise ValueError("Plugin ZIP contains files outside the plugin root")
    files = {path[len(prefix):]: raw for path, raw in uploaded.items()}
    manifest = _json_object(files[".claude-plugin/plugin.json"], "plugin.json")
    name = manifest.get("name")
    if not isinstance(name, str) or not _PLUGIN_NAME.fullmatch(name):
        raise ValueError("Plugin name must be kebab-case")
    for field, limit in (("version", 100), ("description", 500)):
        if field in manifest and (
            not isinstance(manifest[field], str) or len(manifest[field]) > limit
        ):
            raise ValueError(f"Plugin {field} is invalid")
    skills = []
    for root in _skill_roots(files, manifest):
        relative = root + "/" if root else ""
        package = {
            path[len(relative):]: raw for path, raw in files.items()
            if path.startswith(relative)
            and (root or not path.startswith((
                ".claude-plugin/", "agents/", "hooks/", "commands/", "workflows/",
                "output-styles/", "bin/", "monitors/",
            )))
            and (root or path not in {".mcp.json", ".lsp.json", "settings.json"})
        }
        if "SKILL.md" not in package:
            raise ValueError("Plugin Skill package is missing SKILL.md")
        skills.append(PluginSkillPackage(root or ".", package))
    servers = {}
    ignored = set()
    for source in _mcp_sources(files, manifest):
        entries = source.get("mcpServers", source)
        if not isinstance(entries, dict):
            raise ValueError("Plugin MCP servers must be an object")
        for server_name, config in entries.items():
            if (
                not isinstance(server_name, str) or not server_name.strip()
                or len(server_name) > 100 or any(ord(char) < 32 for char in server_name)
            ):
                ignored.add("MCP server with invalid name")
                continue
            if server_name in servers:
                raise ValueError("Plugin MCP server name is duplicated")
            if not isinstance(config, dict):
                raise ValueError("Plugin MCP server configuration is invalid")
            url = _remote_mcp_url(config.get("url"))
            if (
                config.get("type", "http") not in {"http", "streamable-http"}
                or url is None
                or "command" in config
            ):
                ignored.add(f"MCP server {server_name}: unsupported local or unsafe transport")
                continue
            servers[server_name] = PluginMcpReference(
                name=server_name, url=url,
                credentials_required=bool(
                    config.get("headers") or config.get("env") or config.get("oauth")
                ),
            )
            if config.get("oauth"):
                ignored.add(f"MCP server {server_name}: OAuth setup was not imported")
    if len(servers) > MAX_PLUGIN_MCP_SERVERS:
        raise ValueError("Plugin declares too many remote MCP servers")
    agent_paths = set(path for path in files if path.startswith("agents/") and path.endswith(".md"))
    if "agents" in manifest:
        for path in _component_paths(manifest["agents"], "agents"):
            agent_paths.update(
                item for item in files if item == path or item.startswith(path + "/")
            )
    ignored.update(key for key in (
        "commands", "hooks", "workflows", "outputStyles", "lspServers", "channels",
        "userConfig", "dependencies", "experimental",
    ) if key in manifest)
    ignored.update(directory for directory in (
        "commands", "hooks", "workflows", "output-styles", "bin", "monitors",
    ) if any(path.startswith(directory + "/") for path in files))
    if "settings.json" in files:
        ignored.add("settings.json")
    if ".lsp.json" in files:
        ignored.add(".lsp.json")
    return PluginArchive(
        name=name, version=manifest.get("version"), description=manifest.get("description"),
        skills=tuple(skills), mcp_servers=tuple(servers.values()),
        ignored_agents=tuple(sorted(agent_paths)), ignored_components=tuple(sorted(ignored)),
    )
