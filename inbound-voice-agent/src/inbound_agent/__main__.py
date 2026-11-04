"""CLI entry point.

    python -m inbound_agent download-files   # Silero VAD model, once per machine
    python -m inbound_agent dev              # local development, verbose
    python -m inbound_agent start            # production
"""

from livekit.agents import cli

from .worker import build_worker_options


def main() -> None:
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
