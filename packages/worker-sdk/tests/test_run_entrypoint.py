from sluice_worker.run import load_handler


def test_load_handler_from_path():
    h = load_handler("sluice_worker.handler:BaseHandler")
    assert h.__name__ == "BaseHandler"
