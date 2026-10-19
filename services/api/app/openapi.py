"""Print the OpenAPI document: `python -m app.openapi > openapi.json`.

Used to generate packages/api-client. Keys are sorted so the output is stable in Git.
"""

import json
import sys

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    app = create_app(Settings(app_env="development", static_root=None))
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
