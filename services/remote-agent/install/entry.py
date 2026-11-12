"""Entry point for the PyInstaller build (celius-agent.exe)."""

from celius_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
