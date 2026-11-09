import os, shutil, subprocess, pytest
HERE = os.path.dirname(__file__)
@pytest.mark.skipif(not shutil.which("php"), reason="php CLI not installed")
def test_plugin_pure_functions():
    r = subprocess.run(["php", os.path.join(HERE, "php", "test_bridge.php")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
