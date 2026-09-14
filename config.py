import json
import os as _os
import sys as _sys
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet


def _app_dir() -> Path:
    """Base directory of the application (exe or script location)."""
    if getattr(_sys, "frozen", False):
        # PyInstaller bundle — exe lives in the install directory
        return Path(_sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _data_dir() -> Path:
    """Return the data directory, creating it if necessary.

    CYROUTER_DATA_DIR env overrides everything (server deployments put
    data on a separate volume without touching the app directory).
    Falls back to %APPDATA%/cyRouter/data when the exe's own directory
    isn't writable (e.g. Program Files on end-user machines).
    """
    env = _os.environ.get("CYROUTER_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    default = _app_dir() / "data"
    try:
        default.mkdir(parents=True, exist_ok=True)
        # Confirm we can actually write here
        (default / ".write_test").touch()
        (default / ".write_test").unlink()
        return default
    except (OSError, PermissionError):
        pass
    appdata = _os.environ.get("APPDATA", "")
    if appdata:
        fallback = Path(appdata) / "cyRouter" / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    default.mkdir(parents=True, exist_ok=True)
    return default


DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
KEY_PATH = DATA_DIR / ".key"

# In-memory config cache with TTL
_config_cache: dict | None = None
_config_cache_ts: float = 0.0
_CONFIG_CACHE_TTL = 30.0  # seconds — save_config() invalidates immediately


def _get_or_create_key() -> bytes:
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    return key


def is_frozen() -> bool:
    """True when running inside a PyInstaller bundle (exe release)."""
    return getattr(_sys, "frozen", False)


# Dev builds default to 20100; frozen (release) builds use 20108 so the two
# can coexist on the same machine without port conflicts.
DEFAULT_PORT = 20108 if is_frozen() else 20100


_fernet = Fernet(_get_or_create_key())


def encrypt_value(plain: str) -> str:
    return _fernet.encrypt(plain.encode()).decode()


def decrypt_value(cipher: str) -> str:
    return _fernet.decrypt(cipher.encode()).decode()


def load_config() -> dict:
    global _config_cache, _config_cache_ts
    if (
        _config_cache is not None
        and (time.time() - _config_cache_ts) < _CONFIG_CACHE_TTL
    ):
        return _config_cache
    if not CONFIG_PATH.exists():
        cfg = {"port": DEFAULT_PORT, "host": "127.0.0.1", "providers": [], "routes": []}
    else:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError(
                f"Failed to load config from {CONFIG_PATH}: {e}\n"
                "The file may be corrupted. Fix the JSON or delete it to start fresh."
            ) from e
    # Backfill port if missing from an older config file
    if "port" not in cfg:
        cfg["port"] = DEFAULT_PORT
    _config_cache = cfg
    _config_cache_ts = time.time()
    return cfg


def save_config(cfg: dict):
    global _config_cache, _config_cache_ts
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp file in same dir, then os.replace()
    fd, tmp_path = tempfile.mkstemp(prefix=".cfg-", suffix=".tmp", dir=str(DATA_DIR))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        _os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _config_cache = cfg
    _config_cache_ts = time.time()
    # Invalidate provider cache since config changed
    try:
        from router import _invalidate_provider_cache

        _invalidate_provider_cache()
    except ImportError:
        pass


def add_provider(provider: dict) -> dict:
    cfg = load_config()
    providers = cfg.get("providers", [])
    for i, p in enumerate(providers):
        if p["name"] == provider["name"]:
            providers[i] = provider
            save_config(cfg)
            return provider
    providers.append(provider)
    cfg["providers"] = providers
    save_config(cfg)
    return provider


def remove_provider(name: str) -> bool:
    cfg = load_config()
    providers = cfg.get("providers", [])
    before = len(providers)
    cfg["providers"] = [p for p in providers if p["name"] != name]
    save_config(cfg)
    return len(cfg["providers"]) < before
