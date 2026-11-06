"""Each case runs real C++ code, including under ASan/UBSan in CI."""
import subprocess
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("case", ["prefixes", "lpm", "paths", "failure", "ties", "wire", "ipv4", "options_fragments", "fuzz"])
def test_core(case):
    subprocess.run([str(ROOT / "build/core-tests"), case], check=True, timeout=20)
