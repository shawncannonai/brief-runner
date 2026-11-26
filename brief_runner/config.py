"""Config loading: TOML (Python 3.11+) or JSON, with defaults that work out of the box."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_QUOTA_PATTERNS = [
    r"(?i)rate[ _-]?limit",
    r"(?i)usage limit",
    r"(?i)quota",
    r"(?i)too many requests",
    r"\b429\b",
    r"(?i)overloaded",
]

CONFIG_NAMES = ("brief-runner.toml", "brief-runner.json")


@dataclass
class Engine:
    name: str
    cmd: list[str]
    prompt: str = "stdin"          # "stdin" | "arg" (prompt appended as last argv) | "file" (path appended)
    timeout: float | None = None   # per-engine override of Config.timeout

    def __post_init__(self) -> None:
        if not self.cmd:
            raise ValueError(f"engine {self.name!r}: cmd is empty")
        if self.prompt not in ("stdin", "arg", "file"):
            raise ValueError(f"engine {self.name!r}: prompt must be stdin, arg or file, not {self.prompt!r}")


@dataclass
class Config:
    engines: list[Engine]
    state_dir: Path = Path("state")
    timeout: float = 1800.0        # wall clock per attempt, seconds
    kill_grace: float = 5.0        # SIGTERM -> SIGKILL gap
    max_engines: int = 3           # attempts per run, walking down the engine chain
    done_window: float = 3600.0    # a brief that succeeded this recently is not re-run (0 = off)
    required_lines: list[str] = field(default_factory=lambda: ["RESULT:"])
    quota_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_QUOTA_PATTERNS))
    cwd: str | None = None         # working directory for the agent (default: the caller's)

    def engine(self, name: str) -> Engine:
        for e in self.engines:
            if e.name == name:
                return e
        raise KeyError(f"no engine named {name!r} (have: {', '.join(e.name for e in self.engines)})")


def _read(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    try:
        import tomllib
    except ModuleNotFoundError as exc:  # Python 3.10
        raise RuntimeError(f"{path}: TOML needs Python 3.11+; use a .json config instead") from exc
    return tomllib.loads(text)


def find_config(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit)
    env = os.environ.get("BRIEF_RUNNER_CONFIG")
    if env:
        return Path(env)
    for name in CONFIG_NAMES:
        if Path(name).is_file():
            return Path(name)
    return None


def from_dict(d: dict, base: Path | None = None) -> Config:
    engines = [Engine(name=e["name"], cmd=list(e["cmd"]), prompt=e.get("prompt", "stdin"),
                      timeout=e.get("timeout")) for e in d.get("engines", [])]
    if not engines:
        engines = [Engine("claude", ["claude", "-p"], "stdin")]
    state = os.environ.get("BRIEF_RUNNER_STATE") or d.get("state_dir")
    if state:
        state_dir = Path(state)
        if base and not state_dir.is_absolute():
            state_dir = Path(os.path.normpath(base / state_dir))
    elif os.environ.get("XDG_STATE_HOME"):
        state_dir = Path(os.environ["XDG_STATE_HOME"]) / "brief-runner"
    else:
        state_dir = Path("state")
    known = {"timeout", "kill_grace", "max_engines", "done_window", "required_lines", "quota_patterns", "cwd"}
    kw = {k: d[k] for k in known if k in d}
    return Config(engines=engines, state_dir=state_dir, **kw)


def load_config(path: str | Path | None = None) -> Config:
    p = find_config(str(path) if path else None)
    if p is None:
        return from_dict({})
    return from_dict(_read(p), base=p.resolve().parent)
