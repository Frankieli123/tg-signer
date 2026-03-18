import json
import sqlite3

from tg_signer.webui.data import (
    build_account_options,
    discover_session_accounts,
    list_account_names,
    load_user_infos,
    resolve_session_dir,
)


def test_resolve_session_dir_uses_workdir_for_relative_path(tmp_path):
    resolved = resolve_session_dir(".", tmp_path / ".signer")

    assert resolved == tmp_path / ".signer"


def test_list_account_names_discovers_session_files(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "alpha.session").write_text("", encoding="utf-8")
    (session_dir / "beta.session_string").write_text("dummy", encoding="utf-8")
    (session_dir / "gamma.session-wal").write_text("", encoding="utf-8")
    (session_dir / "ignore.txt").write_text("", encoding="utf-8")

    accounts = list_account_names(session_dir, tmp_path / ".signer")

    assert accounts == ["alpha", "beta"]


def test_list_account_names_supports_relative_session_dir(tmp_path):
    workdir = tmp_path / ".signer"
    session_dir = workdir / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "delta.session").write_text("", encoding="utf-8")

    accounts = list_account_names("sessions", workdir)

    assert accounts == ["delta"]


def test_build_account_options_keeps_manual_account_first(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "alpha.session").write_text("", encoding="utf-8")

    accounts = build_account_options(
        session_dir,
        tmp_path / ".signer",
        preferred_accounts=["my_account", "alpha", "my_account"],
    )

    assert accounts == ["my_account", "alpha"]


def test_discover_session_accounts_reads_user_from_session_file(tmp_path):
    workdir = tmp_path / ".signer"
    user_dir = workdir / "users" / "10001"
    user_dir.mkdir(parents=True, exist_ok=True)
    json.dump(
        {"first_name": "Alice", "username": "alice"},
        open(user_dir / "me.json", "w", encoding="utf-8"),
    )

    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(session_dir / "alpha.session") as conn:
        conn.execute("CREATE TABLE sessions (user_id INTEGER, is_bot INTEGER)")
        conn.execute("INSERT INTO sessions (user_id, is_bot) VALUES (10001, 0)")
        conn.commit()

    entries = discover_session_accounts(
        session_dir,
        workdir,
        search_dirs=[session_dir],
    )

    assert len(entries) == 1
    assert entries[0].account == "alpha"
    assert entries[0].user_id == "10001"
    assert entries[0].display_name == "Alice"


def test_discover_session_accounts_prefers_sqlite_session_over_session_string(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "alpha.session_string").write_text("dummy", encoding="utf-8")
    with sqlite3.connect(session_dir / "alpha.session") as conn:
        conn.execute("CREATE TABLE sessions (user_id INTEGER, is_bot INTEGER)")
        conn.execute("INSERT INTO sessions (user_id, is_bot) VALUES (10001, 0)")
        conn.commit()

    entries = discover_session_accounts(
        session_dir,
        tmp_path / ".signer",
        search_dirs=[session_dir],
    )

    assert len(entries) == 1
    assert entries[0].path.name == "alpha.session"


def test_load_user_infos_handles_nested_json_string(tmp_path):
    workdir = tmp_path / ".signer"
    user_dir = workdir / "users" / "10001"
    user_dir.mkdir(parents=True, exist_ok=True)
    nested_json = json.dumps({"first_name": "Alice", "username": "alice"})
    json.dump(nested_json, open(user_dir / "me.json", "w", encoding="utf-8"))
    json.dump([], open(user_dir / "latest_chats.json", "w", encoding="utf-8"))

    entries = load_user_infos(workdir)

    assert len(entries) == 1
    assert entries[0].data["first_name"] == "Alice"
