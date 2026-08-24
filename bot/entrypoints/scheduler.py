"""Scheduler entrypoint. Exactly one instance may run.

Run with: taskiq scheduler bot.entrypoints.scheduler:scheduler
"""

from bot.tasks import scheduler

__all__ = ["scheduler"]
