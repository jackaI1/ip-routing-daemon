import subprocess
import sys
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="routerd is Linux-only")
BASE = "router 1\ntun rt0\nstatus /tmp/router-status.json\nprefix 10.1.0.0/24\npeer 2 172.16.12.2 1\n"

@pytest.mark.parametrize("suffix", ["", "peer 1 192.0.2.1 1\n", "peer 2 192.0.2.1 1\n", "peer 3 172.16.12.2 1\n",
                                    "peer 3 192.0.2.1 0\n", "router 7\n", "unknown value\n", "prefix 1.2.3.4/99\n",
                                    "tun bad\n", "prefix 10.1.0.0/24\n", "peer 4 192.0.2.1 1 extra\n"])
def test_config_validation(tmp_path, suffix):
    path = tmp_path / "router.conf"
    path.write_text(BASE + suffix)
    result = subprocess.run([str(ROOT / "build/routerd"), "--check-config", str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if suffix == "" else 1)
