import shutil
import subprocess

import pytest

helm = shutil.which("helm")
pytestmark = pytest.mark.skipif(helm is None, reason="helm not installed")


def test_umbrella_templates_render():
    out = subprocess.run([helm, "template", "sluice", "charts/sluice"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "kind: Deployment" in out.stdout
    assert "kind: CustomResourceDefinition" not in out.stdout  # no CRDs — spec store only


def test_umbrella_lints_clean():
    out = subprocess.run([helm, "lint", "charts/sluice"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_probes_and_metrics_annotations_present():
    out = subprocess.run([helm, "template", "sluice", "charts/sluice"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.count("livenessProbe") >= 3
    assert out.stdout.count("readinessProbe") >= 2
    assert 'prometheus.io/scrape: "true"' in out.stdout
