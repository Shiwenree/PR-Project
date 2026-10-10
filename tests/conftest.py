import os
import sys

# make the project importable, and never depend on a real .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GITHUB_USERNAME", "me")
