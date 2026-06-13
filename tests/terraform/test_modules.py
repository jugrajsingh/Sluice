import os
import shutil
import subprocess

import pytest

terraform = shutil.which("terraform")
pytestmark = pytest.mark.skipif(terraform is None, reason="terraform not installed")

MODULES = ["infra/terraform/modules/sluice-vm-gce", "infra/terraform/modules/sluice-vm-ec2"]


@pytest.mark.parametrize("mod", MODULES)
def test_fmt_clean(mod):
    out = subprocess.run([terraform, "fmt", "-check", "-recursive", mod], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


@pytest.mark.parametrize("mod", MODULES)
@pytest.mark.skipif(
    os.environ.get("SLUICE_TF_VALIDATE") != "1", reason="set SLUICE_TF_VALIDATE=1 (downloads providers)"
)
def test_validate(mod, tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "main.tf").write_text(f'module "vm" {{\n  source = "{os.path.abspath(mod)}"\n  name = "t"\n  app = "t"\n}}\n')
    assert subprocess.run([terraform, f"-chdir={wd}", "init", "-backend=false"], capture_output=True).returncode == 0
    out = subprocess.run([terraform, f"-chdir={wd}", "validate"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
