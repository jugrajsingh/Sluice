from sluice_cli.spec import load_and_validate

BASE = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: m}\nspec:\n  image: r/x:1\n"


def test_should_suggest_correct_field_for_typo():
    spec, errs = load_and_validate(BASE + "  scaling: {maxInstance: 7}\n")
    assert spec is None
    assert any("maxInstance" in e and "maxInstances" in e for e in errs)


def test_should_surface_queue_block_error():
    spec, errs = load_and_validate(BASE + "  queue: {ref: m, bogus: 1}\n")
    assert spec is None
    assert any("queue" in e for e in errs)


def test_should_return_spec_when_valid():
    spec, errs = load_and_validate(BASE + "  scaling: {maxInstances: 2}\n")
    assert errs == [] and spec.scaling.max_instances == 2
