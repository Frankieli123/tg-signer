import asyncio

import pytest
from click.testing import CliRunner

import tg_signer.cli.signer as signer_cli


class DummySigner:
    def __init__(self):
        self.calls = []

    def app_run(self, coroutine=None):
        if coroutine is not None:
            asyncio.run(coroutine)

    async def run(
        self,
        num_of_dialogs,
        only_once=False,
        force_rerun=False,
        wait_until_scheduled=False,
    ):
        self.calls.append(
            {
                "method": "run",
                "num_of_dialogs": num_of_dialogs,
                "only_once": only_once,
                "force_rerun": force_rerun,
                "wait_until_scheduled": wait_until_scheduled,
            }
        )

    async def send_text(
        self, chat_id, text, delete_after=None, message_thread_id=None, **kwargs
    ):
        self.calls.append(
            {
                "method": "send_text",
                "chat_id": chat_id,
                "text": text,
                "delete_after": delete_after,
                "message_thread_id": message_thread_id,
                "kwargs": kwargs,
            }
        )

    async def send_dice_cli(
        self, chat_id, emoji, delete_after=None, message_thread_id=None, **kwargs
    ):
        self.calls.append(
            {
                "method": "send_dice_cli",
                "chat_id": chat_id,
                "emoji": emoji,
                "delete_after": delete_after,
                "message_thread_id": message_thread_id,
                "kwargs": kwargs,
            }
        )

    async def schedule_messages(
        self,
        chat_id,
        text,
        crontab,
        next_times,
        random_seconds,
        message_thread_id=None,
    ):
        self.calls.append(
            {
                "method": "schedule_messages",
                "chat_id": chat_id,
                "text": text,
                "crontab": crontab,
                "next_times": next_times,
                "random_seconds": random_seconds,
                "message_thread_id": message_thread_id,
            }
        )

    async def get_schedule_messages(self, chat_id):
        self.calls.append(
            {
                "method": "get_schedule_messages",
                "chat_id": chat_id,
            }
        )

    async def list_topics(self, chat_id, limit=20):
        self.calls.append(
            {
                "method": "list_topics",
                "chat_id": chat_id,
                "limit": limit,
            }
        )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def dummy_signer(monkeypatch):
    dummy = DummySigner()
    monkeypatch.setattr(signer_cli, "get_signer", lambda *_args, **_kwargs: dummy)
    return dummy


def patch_task_list(monkeypatch, task_names):
    class DummyTaskLister:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def get_task_list(self):
            return list(task_names)

    monkeypatch.setattr(signer_cli, "UserSigner", DummyTaskLister)


def test_send_text_supports_message_thread_id(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        ["send-text", "--message-thread-id", "1", "123456", "checkin"],
    )

    assert result.exit_code == 0
    assert dummy_signer.calls[0]["method"] == "send_text"
    assert dummy_signer.calls[0]["message_thread_id"] == 1


def test_send_dice_supports_message_thread_id(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        ["send-dice", "--message-thread-id", "1", "123456", "🎲"],
    )

    assert result.exit_code == 0
    assert dummy_signer.calls[0]["method"] == "send_dice_cli"
    assert dummy_signer.calls[0]["message_thread_id"] == 1


def test_schedule_messages_supports_message_thread_id(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        [
            "schedule-messages",
            "--crontab",
            "0 6 * * *",
            "--message-thread-id",
            "1",
            "123456",
            "checkin",
        ],
    )

    assert result.exit_code == 0
    assert dummy_signer.calls[0]["method"] == "schedule_messages"
    assert dummy_signer.calls[0]["message_thread_id"] == 1


def test_list_topics(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        ["list-topics", "--chat_id", "-1003763902761", "--limit", "50"],
    )

    assert result.exit_code == 0
    assert dummy_signer.calls[0]["method"] == "list_topics"
    assert dummy_signer.calls[0]["chat_id"] == -1003763902761
    assert dummy_signer.calls[0]["limit"] == 50


def test_run_supports_wait_until_scheduled(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        ["run", "--wait-until-scheduled", "morning_task", "evening_task"],
    )

    assert result.exit_code == 0
    assert [call["method"] for call in dummy_signer.calls] == ["run", "run"]
    assert all(call["wait_until_scheduled"] is True for call in dummy_signer.calls)


def test_multi_run_supports_wait_until_scheduled(dummy_signer, runner):
    result = runner.invoke(
        signer_cli.tg_signer,
        [
            "multi-run",
            "--wait-until-scheduled",
            "-a",
            "acct_a",
            "-a",
            "acct_b",
            "shared_task",
        ],
    )

    assert result.exit_code == 0
    assert [call["method"] for call in dummy_signer.calls] == ["run", "run"]
    assert all(call["wait_until_scheduled"] is True for call in dummy_signer.calls)


def test_start_discovers_all_tasks_and_waits_by_default(
    monkeypatch, dummy_signer, runner
):
    patch_task_list(monkeypatch, ["dibao", "beejrfy"])

    result = runner.invoke(
        signer_cli.tg_signer,
        ["start"],
    )

    assert result.exit_code == 0
    assert "自动发现任务: beejrfy, dibao" in result.output
    assert [call["method"] for call in dummy_signer.calls] == ["run", "run"]
    assert all(call["wait_until_scheduled"] is True for call in dummy_signer.calls)


def test_start_supports_alias_run_all(monkeypatch, dummy_signer, runner):
    patch_task_list(monkeypatch, ["dibao"])

    result = runner.invoke(
        signer_cli.tg_signer,
        ["run_all"],
    )

    assert result.exit_code == 0
    assert dummy_signer.calls[0]["method"] == "run"
    assert dummy_signer.calls[0]["wait_until_scheduled"] is True


def test_start_fails_when_no_tasks_found(monkeypatch, runner):
    patch_task_list(monkeypatch, [])

    result = runner.invoke(
        signer_cli.tg_signer,
        ["start"],
    )

    assert result.exit_code != 0
    assert "No sign tasks found under workdir" in result.output
