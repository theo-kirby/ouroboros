from ouroboros.recorder import Recorder


def test_log_survives_dead_terminal(tmp_path):
    calls = []

    def bad_echo(line):
        calls.append(line)
        raise OSError(5, "Input/output error")

    rec = Recorder(tmp_path / "run", echo=bad_echo)
    rec.log("one")
    rec.log("two")
    assert len(calls) == 1 and rec.echo is None
    assert "one" in rec.log_path.read_text() and "two" in rec.log_path.read_text()
