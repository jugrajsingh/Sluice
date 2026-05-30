from sluice_core.models import Message, QueueDepth


def test_message_defaults_and_fields():
    m = Message(id="abc", body=b"payload", ack_token="tok")
    assert m.id == "abc"
    assert m.body == b"payload"
    assert m.attributes == {}
    assert m.receive_count == 1
    assert m.ack_token == "tok"


def test_queue_depth_defaults_zero():
    d = QueueDepth()
    assert (d.visible, d.in_flight, d.delayed) == (0, 0, 0)
