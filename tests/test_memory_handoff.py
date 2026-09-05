from ouroboros.memory.handoff import HandoffMemory


def test_orient_and_record(tmp_path):
    root = tmp_path / ".ouroboros"
    mem = HandoffMemory(root, recent=2)
    mem.ensure()
    assert "first iteration" in mem.orient_prompt()
    assert "handoff/0001.md" in mem.record_prompt()
    before = mem.snapshot()
    assert not mem.verify_recorded(before)
    for i in range(1, 4):
        mem.next_path().write_text(f"## Did\n\nunit {i}\n")
    assert mem.verify_recorded(before)
    orient = mem.orient_prompt()
    assert "unit 3" in orient and "unit 2" in orient and "unit 1" not in orient
    assert mem.last_summary() == "unit 3"
    assert "handoff/0004.md" in mem.record_prompt()
