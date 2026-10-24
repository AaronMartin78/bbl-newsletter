# Makes `import bbl_newsletter` work for pytest without an editable install.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
