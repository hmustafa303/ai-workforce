from workforce.ledger import Ledger, Task, new_task_id


def _task(**kw):
    base = dict(id=new_task_id(), role="research", engine="crew", status="queued",
                title="t", instructions="do it")
    base.update(kw)
    return Task(**base)


def test_create_get_roundtrip(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    t = led.create(_task(acceptance=["a", "b"], write=True,
                         verification={"passed": True}))
    got = led.get(t.id)
    assert got.acceptance == ["a", "b"] and got.write is True
    assert got.verification == {"passed": True}
    assert [e["kind"] for e in led.events(t.id)] == ["created"]


def test_update_only_if_status(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    t = led.create(_task(status="cancelled"))
    assert led.update(t.id, only_if_status=("running",), status="done") is False
    assert led.get(t.id).status == "cancelled"
    assert led.update(t.id, status="done") is True


def test_claim_slot_respects_limit_and_reaps_dead_workers(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    a, b = led.create(_task()), led.create(_task())
    alive = {111: True, 222: True}
    assert led.claim_slot(a.id, 111, 1, is_alive=alive.get) == "claimed"
    assert led.claim_slot(b.id, 222, 1, is_alive=alive.get) == "wait"
    alive[111] = False  # worker A died
    assert led.claim_slot(b.id, 222, 1, is_alive=alive.get) == "claimed"
    assert led.get(a.id).status == "failed"
    assert led.get(a.id).error == "worker process died"
    assert led.get(b.id).attempts == 1


def test_claim_slot_gone_when_cancelled(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    t = led.create(_task(status="cancelled"))
    assert led.claim_slot(t.id, 1, 1) == "gone"


def test_feedback_and_stats(tmp_path):
    led = Ledger(tmp_path / "l.sqlite3")
    t = led.create(_task())
    led.add_feedback(t.id, 1, "fix the test")
    assert led.feedback(t.id)[0]["text"] == "fix the test"
    led.update(t.id, status="done", seconds=4.0, model_used="m")
    row = led.stats()[0]
    assert row["tasks"] == 1 and row["succeeded"] == 1 and row["model"] == "m"
