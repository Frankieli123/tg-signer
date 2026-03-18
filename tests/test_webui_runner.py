from tg_signer.webui import runner


def test_build_runner_command_includes_selected_tasks_and_wait_flag(tmp_path):
    log_path = tmp_path / "logs" / "runner.log"
    cmd = runner.build_runner_command(
        tmp_path / ".signer",
        tmp_path / "sessions",
        "acct",
        ["task_b", "task_a"],
        25,
        wait_until_scheduled=True,
        log_path=log_path,
    )

    assert cmd[:3] == [runner.sys.executable, "-m", "tg_signer"]
    assert "--session_dir" in cmd
    assert "-a" in cmd
    assert "--log-file" in cmd
    assert "--wait-until-scheduled" in cmd
    assert cmd[-2:] == ["task_b", "task_a"]


def test_runner_state_roundtrip(tmp_path):
    state = runner.RunnerState(
        pid=1234,
        account="acct",
        session_dir=str(tmp_path / "sessions"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / "logs" / "webui-runner.log"),
        started_at="2026-03-18T00:00:00+00:00",
    )

    runner.save_runner_state(tmp_path, state)
    loaded = runner.load_runner_state(tmp_path)

    assert loaded == state


def test_load_runner_state_backfills_legacy_command(tmp_path):
    state_file = runner.get_runner_state_file(tmp_path)
    state_file.write_text(
        """
{
  "pid": 1234,
  "account": "acct",
  "session_dir": "/tmp/sessions",
  "workdir": "/tmp/.signer",
  "task_names": ["task_a"],
  "num_of_dialogs": 50,
  "wait_until_scheduled": true,
  "log_path": "/tmp/.signer/logs/webui-runner.log",
  "started_at": "2026-03-18T00:00:00+00:00",
  "stopped_at": null
}
""".strip(),
        encoding="utf-8",
    )

    loaded = runner.load_runner_state(tmp_path)

    assert loaded is not None
    assert loaded.command is not None
    assert loaded.command[:3] == [runner.sys.executable, "-m", "tg_signer"]


def test_start_runner_persists_state_and_spawns_subprocess(monkeypatch, tmp_path):
    calls = {}

    class DummyPopen:
        def __init__(self, cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            self.pid = 4321

    monkeypatch.setattr(runner.subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(
        runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer"]
    )
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 9999)

    state = runner.start_runner(
        tmp_path / ".signer",
        tmp_path / "sessions",
        "acct",
        ["task_b", "task_a"],
        num_of_dialogs=20,
        wait_until_scheduled=True,
    )

    assert state.pid == 4321
    assert state.task_names == ["task_a", "task_b"]
    assert state.command == ["python", "-m", "tg_signer"]
    assert state.process_start_ticks == 9999
    assert calls["cmd"][-2:] == ["task_a", "task_b"]
    assert calls["kwargs"]["start_new_session"] is True
    assert calls["kwargs"]["env"]["TG_SIGNER_DISABLE_CONSOLE_LOG"] == "1"
    assert runner.load_runner_state(tmp_path / ".signer") == state


def test_start_runner_allows_multiple_accounts(monkeypatch, tmp_path):
    calls = []

    class DummyPopen:
        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))
            self.pid = 4300 + len(calls)

    monkeypatch.setattr(runner.subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(
        runner,
        "get_process_cmdline",
        lambda pid: ["python", "-m", "tg_signer", str(pid)],
    )
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 9000 + pid)

    state_a = runner.start_runner(
        tmp_path / ".signer",
        tmp_path / "sessions_a",
        "acct_a",
        ["task_a"],
    )
    state_b = runner.start_runner(
        tmp_path / ".signer",
        tmp_path / "sessions_b",
        "acct_b",
        ["task_b"],
    )

    states = runner.list_runner_states(tmp_path / ".signer")

    assert state_a.runner_id != state_b.runner_id
    assert {state.account for state in states} == {"acct_a", "acct_b"}
    assert len(calls) == 2


def test_stop_runner_marks_state_stopped(monkeypatch, tmp_path):
    state = runner.RunnerState(
        pid=5678,
        account="acct",
        session_dir=str(tmp_path / "sessions"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner.log"),
        command=["python", "-m", "tg_signer"],
        process_start_ticks=2222,
        started_at="2026-03-18T00:00:00+00:00",
    )
    runner.save_runner_state(tmp_path / ".signer", state)

    kill_calls = []

    monkeypatch.setattr(runner, "process_exists", lambda pid: pid is not None)
    monkeypatch.setattr(runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer"])
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 2222)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: kill_calls.append((pid, sig)))

    stopped = runner.stop_runner(tmp_path / ".signer", force=True)

    assert stopped is not None
    assert stopped.pid is None
    assert stopped.stopped_at is not None
    assert kill_calls == [(5678, runner.signal.SIGKILL)]


def test_stop_runner_targets_selected_account(monkeypatch, tmp_path):
    state_a = runner.RunnerState(
        pid=5678,
        account="acct_a",
        session_dir=str(tmp_path / "sessions_a"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner-a.log"),
        command=["python", "-m", "tg_signer"],
        process_start_ticks=2222,
        started_at="2026-03-18T00:00:00+00:00",
    )
    state_b = runner.RunnerState(
        pid=6789,
        account="acct_b",
        session_dir=str(tmp_path / "sessions_b"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_b"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner-b.log"),
        command=["python", "-m", "tg_signer"],
        process_start_ticks=3333,
        started_at="2026-03-18T00:00:00+00:00",
    )
    runner.save_runner_state(tmp_path / ".signer", state_a)
    runner.save_runner_state(tmp_path / ".signer", state_b)

    kill_calls = []

    monkeypatch.setattr(runner, "process_exists", lambda pid: pid is not None)
    monkeypatch.setattr(runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer"])
    monkeypatch.setattr(
        runner,
        "get_process_start_ticks",
        lambda pid: 2222 if pid == 5678 else 3333,
    )
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: kill_calls.append((pid, sig)))

    stopped = runner.stop_runner(
        tmp_path / ".signer",
        account="acct_a",
        session_dir=str(tmp_path / "sessions_a"),
        force=True,
    )

    remaining = runner.load_runner_state(
        tmp_path / ".signer",
        account="acct_b",
        session_dir=str(tmp_path / "sessions_b"),
    )

    assert stopped is not None
    assert stopped.account == "acct_a"
    assert kill_calls == [(5678, runner.signal.SIGKILL)]
    assert remaining is not None
    assert remaining.pid == 6789


def test_get_runner_status_marks_stale_pid_as_exited(monkeypatch, tmp_path):
    state = runner.RunnerState(
        pid=1234,
        account="acct",
        session_dir=str(tmp_path / "sessions"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner.log"),
        command=["python", "-m", "tg_signer"],
        process_start_ticks=3333,
        started_at="2026-03-18T00:00:00+00:00",
    )
    runner.save_runner_state(tmp_path / ".signer", state)

    monkeypatch.setattr(runner, "process_exists", lambda pid: True)
    monkeypatch.setattr(runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer"])
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 4444)

    status, loaded = runner.get_runner_status(tmp_path / ".signer")

    assert status == "exited"
    assert loaded == state


def test_process_matches_state_accepts_same_process_with_updated_cmdline(
    monkeypatch, tmp_path
):
    state = runner.RunnerState(
        pid=1234,
        account="acct",
        session_dir=str(tmp_path / "sessions"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner.log"),
        command=["python", "-m", "tg_signer", "old"],
        process_start_ticks=3333,
        started_at="2026-03-18T00:00:00+00:00",
    )

    monkeypatch.setattr(runner, "process_exists", lambda pid: True)
    monkeypatch.setattr(
        runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer", "new"]
    )
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 3333)

    assert runner.process_matches_state(state) is True


def test_stop_runner_does_not_kill_stale_pid(monkeypatch, tmp_path):
    state = runner.RunnerState(
        pid=4321,
        account="acct",
        session_dir=str(tmp_path / "sessions"),
        workdir=str(tmp_path / ".signer"),
        task_names=["task_a"],
        num_of_dialogs=50,
        wait_until_scheduled=True,
        log_path=str(tmp_path / ".signer" / "logs" / "webui-runner.log"),
        command=["python", "-m", "tg_signer"],
        process_start_ticks=5555,
        started_at="2026-03-18T00:00:00+00:00",
    )
    runner.save_runner_state(tmp_path / ".signer", state)

    kill_calls = []

    monkeypatch.setattr(runner, "process_exists", lambda pid: True)
    monkeypatch.setattr(runner, "get_process_cmdline", lambda pid: ["python", "-m", "tg_signer"])
    monkeypatch.setattr(runner, "get_process_start_ticks", lambda pid: 6666)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: kill_calls.append((pid, sig)))

    stopped = runner.stop_runner(tmp_path / ".signer", force=True)

    assert stopped is not None
    assert stopped.pid is None
    assert kill_calls == []
