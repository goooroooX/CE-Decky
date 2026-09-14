from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class PluginPaths:
    user_home: Path
    settings_dir: Path
    runtime_dir: Path
    log_dir: Path
    plugin_dir: Path
    managed_root: Path
    ce_root: Path
    tables_root: Path
    state_root: Path
    cache_root: Path
    temp_root: Path

    @classmethod
    def from_environment(cls) -> "PluginPaths":
        user_home = _required_path("DECKY_USER_HOME")
        settings_dir = _required_path("DECKY_PLUGIN_SETTINGS_DIR")
        runtime_dir = _required_path("DECKY_PLUGIN_RUNTIME_DIR")
        log_dir = _required_path("DECKY_PLUGIN_LOG_DIR")
        plugin_dir = _required_path("DECKY_PLUGIN_DIR")
        managed_root = user_home / ".cheat-engine-decky"
        return cls(
            user_home=user_home,
            settings_dir=settings_dir,
            runtime_dir=runtime_dir,
            log_dir=log_dir,
            plugin_dir=plugin_dir,
            managed_root=managed_root,
            ce_root=managed_root / "ce",
            tables_root=managed_root / "tables",
            state_root=managed_root / "state",
            cache_root=managed_root / "cache",
            temp_root=managed_root / "tmp",
        )

    @classmethod
    def for_tests(cls, root: Path) -> "PluginPaths":
        root = root.resolve()
        user_home = root / "home"
        managed_root = user_home / ".cheat-engine-decky"
        return cls(
            user_home=user_home,
            settings_dir=root / "decky" / "settings",
            runtime_dir=root / "decky" / "runtime",
            log_dir=root / "decky" / "logs",
            plugin_dir=root / "plugin",
            managed_root=managed_root,
            ce_root=managed_root / "ce",
            tables_root=managed_root / "tables",
            state_root=managed_root / "state",
            cache_root=managed_root / "cache",
            temp_root=managed_root / "tmp",
        )

    def ensure(self) -> None:
        # Decky's own paths arrive resolved from the environment. Plugin-owned
        # managed paths are additionally required to stay lexical directories,
        # never symlink redirections into arbitrary user/system locations.
        for path in (self.settings_dir, self.runtime_dir, self.log_dir):
            _ensure_directory(path, reject_resolution_change=False)
        for path in (
            self.managed_root,
            self.ce_root,
            self.tables_root,
            self.state_root,
            self.cache_root,
            self.temp_root,
        ):
            _ensure_directory(path, reject_resolution_change=True)

    @property
    def config_path(self) -> Path:
        return self.settings_dir / "config.json"


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required Decky environment variable {name} is empty")
    return Path(value).expanduser().resolve()


def _ensure_directory(path: Path, *, reject_resolution_change: bool) -> None:
    if path.is_symlink():
        raise RuntimeError(f"plugin directory must not be a symlink: {path}")
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"plugin directory path is not a directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"plugin directory could not be established safely: {path}")
    if reject_resolution_change and path.resolve(strict=True) != path.absolute():
        raise RuntimeError(f"plugin managed directory resolves through a symlink: {path}")
