import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class RunnerState:
    pid: Optional[int]
    account: str
    session_dir: str
    workdir: str
    task_names: list[str]
    num_of_dialogs: int
    wait_until_scheduled: bool
    log_path: str
    runner_id: Optional[str] = None
    command: Optional[list[str]] = None
    process_start_ticks: Optional[int] = None
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None


def get_runner_dir(workdir: Path | str) -> Path:
    path = Path(workdir).expanduser() / ".webui"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_runner_state_file(workdir: Path | str) -> Path:
    return get_runner_dir(workdir) / "runner_state.json"


def build_runner_id(account: str, session_dir: Path | str) -> str:
    safe_account = re.sub(r"[^A-Za-z0-9_.-]+", "-", account.strip()).strip("-")
    safe_account = safe_account or "account"
    raw = f"{Path(session_dir).expanduser().resolve()}::{account.strip()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{safe_account}-{digest}"


def get_runner_state_path(
    workdir: Path | str,
    runner_id: Optional[str] = None,
    *,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
) -> Path:
    if runner_id is None and account and session_dir is not None:
        runner_id = build_runner_id(account, session_dir)
    if runner_id is None:
        return get_runner_state_file(workdir)
    return get_runner_dir(workdir) / f"runner-{runner_id}.json"


def get_runner_log_file(
    workdir: Path | str,
    runner_id: Optional[str] = None,
    *,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
) -> Path:
    log_dir = Path(workdir).expanduser() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if runner_id is None and account and session_dir is not None:
        runner_id = build_runner_id(account, session_dir)
    if runner_id is None:
        return log_dir / "webui-runner.log"
    return log_dir / f"webui-runner-{runner_id}.log"


def save_runner_state(workdir: Path | str, state: RunnerState) -> Path:
    state.runner_id = state.runner_id or build_runner_id(state.account, state.session_dir)
    state_file = get_runner_state_path(workdir, runner_id=state.runner_id)
    with open(state_file, "w", encoding="utf-8") as fp:
        json.dump(asdict(state), fp, ensure_ascii=False, indent=2)
    return state_file


def _state_from_data(data: dict[str, Any]) -> RunnerState:
    state = RunnerState(**data)
    state.runner_id = state.runner_id or build_runner_id(
        state.account, state.session_dir
    )
    if state.command is None:
        state.command = build_runner_command(
            state.workdir,
            state.session_dir,
            state.account,
            state.task_names,
            state.num_of_dialogs,
            wait_until_scheduled=state.wait_until_scheduled,
            log_path=state.log_path,
        )
    return state


def _load_state_file(path: Path) -> Optional[RunnerState]:
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return _state_from_data(data)
    except TypeError:
        return None


def list_runner_states(workdir: Path | str) -> list[RunnerState]:
    runner_dir = get_runner_dir(workdir)
    files = [get_runner_state_file(workdir), *sorted(runner_dir.glob("runner-*.json"))]
    deduped: dict[str, tuple[float, RunnerState]] = {}
    for path in files:
        state = _load_state_file(path)
        if state is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        current = deduped.get(state.runner_id or "")
        if current is None or mtime >= current[0]:
            deduped[state.runner_id or ""] = (mtime, state)
    return [
        item[1]
        for item in sorted(
            deduped.values(),
            key=lambda item: (
                item[1].account.lower(),
                item[1].session_dir,
                item[1].started_at or "",
            ),
        )
    ]


def load_runner_state(
    workdir: Path | str,
    runner_id: Optional[str] = None,
    *,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
) -> Optional[RunnerState]:
    state_file = get_runner_state_path(
        workdir,
        runner_id=runner_id,
        account=account,
        session_dir=session_dir,
    )
    state = _load_state_file(state_file)
    if state is not None:
        return state
    if runner_id is not None or account is not None or session_dir is not None:
        return None
    states = list_runner_states(workdir)
    if len(states) == 1:
        return states[0]
    return None


def process_exists(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def get_process_cmdline(pid: Optional[int]) -> Optional[list[str]]:
    if not pid or pid <= 0:
        return None
    cmdline_file = Path("/proc") / str(pid) / "cmdline"
    try:
        data = cmdline_file.read_bytes()
    except OSError:
        return None
    parts = [part.decode("utf-8", errors="ignore") for part in data.split(b"\0") if part]
    return parts or None


def get_process_start_ticks(pid: Optional[int]) -> Optional[int]:
    if not pid or pid <= 0:
        return None
    stat_file = Path("/proc") / str(pid) / "stat"
    try:
        stat_text = stat_file.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    try:
        tail = stat_text.rsplit(") ", 1)[1].split()
        return int(tail[19])
    except (IndexError, ValueError):
        return None


def process_matches_state(state: RunnerState) -> bool:
    if not process_exists(state.pid):
        return False
    if state.process_start_ticks is not None:
        current_ticks = get_process_start_ticks(state.pid)
        if current_ticks is None or current_ticks != state.process_start_ticks:
            return False
    if state.command:
        current_cmdline = get_process_cmdline(state.pid)
        if current_cmdline is None or current_cmdline != state.command:
            return False
    return True


def get_runner_status(
    workdir: Path | str,
    runner_id: Optional[str] = None,
    *,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
) -> tuple[str, Optional[RunnerState]]:
    state = load_runner_state(
        workdir,
        runner_id=runner_id,
        account=account,
        session_dir=session_dir,
    )
    if state is None:
        return "stopped", None
    if process_matches_state(state):
        return "running", state
    return "exited", state


def list_runner_statuses(workdir: Path | str) -> list[tuple[str, RunnerState]]:
    statuses = []
    for state in list_runner_states(workdir):
        statuses.append(
            ("running", state) if process_matches_state(state) else ("exited", state)
        )
    return sorted(
        statuses,
        key=lambda item: (
            0 if item[0] == "running" else 1,
            item[1].account.lower(),
            item[1].session_dir,
        ),
    )


def build_runner_command(
    workdir: Path | str,
    session_dir: Path | str,
    account: str,
    task_names: list[str],
    num_of_dialogs: int,
    wait_until_scheduled: bool = True,
    log_path: Path | str | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "tg_signer",
        "--session_dir",
        str(Path(session_dir).expanduser()),
        "-a",
        account,
        "-w",
        str(Path(workdir).expanduser()),
    ]
    if log_path is not None:
        cmd.extend(["--log-file", str(Path(log_path).expanduser())])
    cmd.extend(["run", "--num-of-dialogs", str(int(num_of_dialogs))])
    if wait_until_scheduled:
        cmd.append("--wait-until-scheduled")
    cmd.extend(task_names)
    return cmd


def start_runner(
    workdir: Path | str,
    session_dir: Path | str,
    account: str,
    task_names: list[str],
    num_of_dialogs: int = 50,
    wait_until_scheduled: bool = True,
) -> RunnerState:
    task_names = sorted({task for task in task_names if task})
    if not task_names:
        raise ValueError("请选择至少一个签到任务")
    runner_id = build_runner_id(account, session_dir)
    status, state = get_runner_status(
        workdir, runner_id=runner_id, account=account, session_dir=session_dir
    )
    if status == "running":
        raise RuntimeError("该账户已有运行中的签到进程，请先停止或重启")

    log_path = get_runner_log_file(
        workdir, runner_id=runner_id, account=account, session_dir=session_dir
    )
    cmd = build_runner_command(
        workdir,
        session_dir,
        account,
        task_names,
        num_of_dialogs,
        wait_until_scheduled=wait_until_scheduled,
        log_path=log_path,
    )
    env = os.environ.copy()
    env["TG_SIGNER_WORKDIR"] = str(Path(workdir).expanduser())
    with open(log_path, "ab") as fp:
        proc = subprocess.Popen(
            cmd,
            stdout=fp,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(Path(workdir).expanduser()),
            start_new_session=True,
            env=env,
        )
    state = RunnerState(
        pid=proc.pid,
        account=account,
        session_dir=str(Path(session_dir).expanduser()),
        workdir=str(Path(workdir).expanduser()),
        task_names=task_names,
        num_of_dialogs=int(num_of_dialogs),
        wait_until_scheduled=wait_until_scheduled,
        log_path=str(log_path),
        runner_id=runner_id,
        command=get_process_cmdline(proc.pid) or cmd,
        process_start_ticks=get_process_start_ticks(proc.pid),
        started_at=now_iso(),
        stopped_at=None,
    )
    save_runner_state(workdir, state)
    return state


def stop_runner(
    workdir: Path | str,
    *,
    runner_id: Optional[str] = None,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
    force: bool = False,
    timeout_seconds: float = 5.0,
) -> Optional[RunnerState]:
    status, state = get_runner_status(
        workdir,
        runner_id=runner_id,
        account=account,
        session_dir=session_dir,
    )
    if state is None:
        return None
    if status == "running" and state.pid:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(state.pid, sig)
        except ProcessLookupError:
            pass
        if not force:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if not process_exists(state.pid):
                    break
                time.sleep(0.1)
            if process_exists(state.pid):
                try:
                    os.killpg(state.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    state.pid = None
    state.stopped_at = now_iso()
    save_runner_state(workdir, state)
    return state


def restart_runner(
    workdir: Path | str,
    runner_id: Optional[str] = None,
    *,
    account: Optional[str] = None,
    session_dir: Optional[Path | str] = None,
) -> RunnerState:
    status, state = get_runner_status(
        workdir,
        runner_id=runner_id,
        account=account,
        session_dir=session_dir,
    )
    if state is None:
        raise RuntimeError("尚未保存过运行配置，无法重启")
    if status == "running":
        stop_runner(
            workdir,
            runner_id=state.runner_id,
            account=state.account,
            session_dir=state.session_dir,
        )
    return start_runner(
        workdir,
        state.session_dir,
        state.account,
        state.task_names,
        state.num_of_dialogs,
        state.wait_until_scheduled,
    )


def summarize_last_runs(workdir: Path | str, records: list[Any]) -> dict[str, Optional[str]]:
    latest: dict[str, Optional[str]] = {}
    for record in records:
        last_run = record.records[0][1] if record.records else None
        current = latest.get(record.task)
        if current is None or (last_run and last_run > current):
            latest[record.task] = last_run
    return latest
