from .base import AgentInterface, Proposal
from .heuristic_agent import HeuristicAgent

# Optional LLM agents — imported lazily by name in main.py to avoid SDK installs
__all__ = ["AgentInterface", "Proposal", "HeuristicAgent"]
