import os
import shutil
import subprocess

import pytest

docker = shutil.which("docker")
pytestmark = pytest.mark.skipif(
    docker is None or os.environ.get("SLUICE_BUILD_IMAGES") != "1",
    reason="set SLUICE_BUILD_IMAGES=1 with docker available",
)

IMAGES = ["gateway", "autoscaler", "console", "worker-base"]


@pytest.mark.parametrize("name", IMAGES)
def test_image_builds(name):
    out = subprocess.run(
        [docker, "build", "-f", f"docker/Dockerfile.{name}", "-t", f"sluice-{name}:test", "."],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr[-3000:]


def test_autoscaler_image_has_terraform():
    out = subprocess.run(
        [docker, "run", "--rm", "--entrypoint", "terraform", "sluice-autoscaler:test", "version"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "Terraform" in out.stdout


def test_example_image_builds_from_worker_base():
    out = subprocess.run(
        [docker, "build", "-t", "sluice-example-seg:test", "examples/segmentation"], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr[-3000:]
