"""brief-runner: one fresh agent per brief, no duplicate work."""
from .config import Config, Engine, load_config, from_dict
from .ledger import Ledger
from .runner import RunResult, Attempt, run_brief, brief_hash, check_report

__all__ = ["Config", "Engine", "load_config", "from_dict", "Ledger", "RunResult", "Attempt", "run_brief",
           "brief_hash", "check_report"]
__version__ = "0.1.0"
