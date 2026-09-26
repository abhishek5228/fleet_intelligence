"""
Ensures `import main` and `import advanced.hybrid_search` work regardless of
where pytest is invoked from (repo root or backend/ directly) — this file
must live in backend/ alongside main.py.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
