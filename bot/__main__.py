"""Development entrypoint: long polling.

Never a production path — use `uvicorn bot.entrypoints.api:app` (webhook,
health probes, metrics) for that instead.
"""

from bot.entrypoints.polling import run

if __name__ == "__main__":
    run()
