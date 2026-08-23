"""Worker entrypoint.

Run with: taskiq worker bot.entrypoints.worker:broker
"""

from bot.tasks import broker

__all__ = ["broker"]
