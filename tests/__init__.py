import os
import sys

# The Worker imports modules from src/ by bare name; mirror that for tests.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
