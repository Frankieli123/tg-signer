import json
import logging
import os
from pathlib import Path
from typing import Callable, Dict

from nicegui import app, ui
from pydantic import TypeAdapter

from tg_signer.webui.data import (
    CONFIG_META,
    DEFAULT_LOG_FILE,
    DEFAULT_WORKDIR,
    LOG_DIR,
    ConfigKind,
    SessionAccount,
    delete_config,
    discover_session_accounts,
    get_workdir,
    list_log_files,
    list_task_names,
    load_config,
    load_logs,
    load_sign_records,
    load_user_infos,
    save_config,
)
from tg_signer.webui.interactive import InteractiveSignerConfig
from tg_signer.webui.runner import (
    get_runner_log_file,
    get_runner_status,
    list_runner_statuses,
    start_runner,
    stop_runner,
    summarize_last_runs,
)
from tg_signer.webui.schema_utils import clean_schema

SIGNER_TEMPLATE: Dict[str, object] = {
    "chats": [
        {
            "chat_id": 123456789,
            "message_thread_id": None,
            "name": "示例任务",
            "delete_after": None,
            "actions": [{"action": 1, "text": "签到"}],
            "action_interval": 1,
        }
    ],
    "sign_at": "0 6 * * *",
    "random_seconds": 0,
    "sign_interval": 1,
}

MONITOR_TEMPLATE: Dict[str, object] = {
    "match_cfgs": [
        {
            "chat_id": "@channel_or_user",
            "rule": "contains",
            "rule_value": "关键词",
            "from_user_ids": None,
            "always_ignore_me": False,
            "default_send_text": "自动回复",
            "ai_reply": False,
            "ai_prompt": None,
            "send_text_search_regex": None,
            "delete_after": None,
            "ignore_case": True,
            "forward_to_chat_id": None,
            "external_forwards": None,
            "push_via_server_chan": False,
            "server_chan_send_key": None,
        }
    ]
}


AUTH_CODE_ENV = "TG_SIGNER_GUI_AUTHCODE"
AUTH_STORAGE_KEY = "tg_signer_gui_auth_code"
logger = logging.getLogger("tg-signer")


class UIState:
    def __init__(self) -> None:
        self.workdir: Path = get_workdir(DEFAULT_WORKDIR)
        self.log_path: Path = self.workdir / DEFAULT_LOG_FILE
        self.log_limit: int = 200
        self.record_filter: str = ""
        self.runner_account: str = os.environ.get("TG_ACCOUNT", "my_account")
        self.runner_session_dir: str = os.environ.get("TG_SIGNER_SESSION_DIR", ".")
        self.runner_num_of_dialogs: int = 50
        self.runner_wait_until_scheduled: bool = True

    def set_workdir(self, path_str: str) -> None:
        self.workdir = get_workdir(Path(path_str).expanduser())
        self.log_path = self.workdir / DEFAULT_LOG_FILE

    def set_log_path(self, path_str: str) -> None:
        path = Path(path_str or DEFAULT_LOG_FILE).expanduser()
        self.log_path = path if path.is_absolute() else self.workdir / path


state = UIState()


def pretty_json(data: Dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def notify_error(exc: Exception) -> None:
    ui.notify(f"{exc}", type="negative")


def tone_card_classes(tone: str = "neutral") -> str:
    palette = {
        "neutral": "bg-white border border-gray-200",
        "info": "bg-sky-50 border border-sky-200",
        "success": "bg-emerald-50 border border-emerald-200",
        "warning": "bg-amber-50 border border-amber-200",
    }
    return palette.get(tone, palette["neutral"])


def build_stat_card(title: str, hint: str = "", *, tone: str = "neutral"):
    with ui.card().classes(f"min-w-[220px] flex-1 shadow-sm {tone_card_classes(tone)}"):
        ui.label(title).classes("text-xs font-semibold uppercase tracking-wide text-gray-500")
        value = ui.label("-").classes("text-xl font-semibold text-gray-800")
        detail = ui.label(hint).classes("text-xs leading-5 text-gray-500")
    return value, detail


class BaseConfigBlock:
    def __init__(
        self,
        kind: ConfigKind,
        template: Dict[str, object],
    ):
        self.kind = kind
        self.template = template
        self.title = "签到配置 (signer)" if kind == "signer" else "监控配置 (monitor)"
        self.root_dir, self.cfg_cls = CONFIG_META[kind]
        with ui.card().classes("w-full shadow-md"):
            ui.label(self.title).classes("text-lg font-semibold")
            ui.label(f"目录: {self.root_dir}/<name>/config.json").classes(
                "text-sm text-gray-500"
            )
            with ui.row().classes("items-end w-full gap-3"):
                self.select = ui.select(
                    label="选择配置",
                    options=[],
                    with_input=True,
                    on_change=self.load_current,
                ).classes("min-w-[240px]")
                ui.button("重置", on_click=self.clear_selection).props("outline")
                self.name_input = ui.input(
                    label="保存为/新建名称",
                    placeholder="my_task",
                ).classes("min-w-[200px]")
                ui.button("使用示例", on_click=self.fill_template)
                self.setup_toolbar()

            # MonitorConfig schema causes json_editor to fail rendering due to "format": "uri" etc.
            # We need to clean the schema before passing it to the editor.
            schema = TypeAdapter(self.cfg_cls | None).json_schema()
            if self.kind == "monitor":
                schema = clean_schema(schema)

            def on_change(e):
                self.editor.properties["content"] = e.content

            self.editor = ui.json_editor(
                {"content": {"json": None}},
                schema=schema,
                on_change=on_change,
            )
            self.selected_name: dict[str, str] = {"value": ""}

            with ui.row().classes("gap-2 items-center"):
                ui.button("刷新列表", on_click=self.refresh_options)
                ui.button("加载", on_click=self.load_current)
                ui.button("保存", color="primary", on_click=self.save_current)
                ui.button("删除", color="negative", on_click=self.delete_current)
            self.setup_footer()

    def clear_selection(self) -> None:
        self.select.value = None
        self.name_input.value = ""
        self.fill_template()
        self.selected_name["value"] = ""

    def setup_toolbar(self):
        """Override to add more buttons to the top toolbar"""
        pass

    def setup_footer(self):
        """Override to add more buttons to the bottom footer"""
        pass

    def __call__(self, *args, **kwargs):
        self.refresh_options()

    def refresh_options(self) -> None:
        options = list_task_names(self.kind, state.workdir)
        self.select.options = options
        self.select.update()

    def load_current(self) -> None:
        target = self.select.value
        if not target:
            return
        try:
            entry = load_config(self.kind, target, workdir=state.workdir)
            self.editor.properties["content"]["json"] = entry.payload
            self.name_input.value = entry.name
            self.editor.update()
            self.name_input.update()
            self.editor.run_editor_method(":expand", "[]", "path => true")
            self.selected_name["value"] = target
            self.on_loaded(target)
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def on_loaded(self, target: str):
        """Hook called after config is loaded"""
        pass

    def save_current(self) -> None:
        target = (self.name_input.value or self.select.value or "").strip()
        if not target:
            ui.notify("请先填写配置名称", type="warning")
            return
        try:
            save_config(
                self.kind,
                target,
                self.editor.properties["content"]["json"] or "{}",
                workdir=state.workdir,
            )
            self.refresh_options()
            self.select.value = target
            self.select.update()
            ui.notify("保存成功", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)

    def fill_template(self) -> None:
        self.editor.properties["content"]["json"] = self.template
        self.editor.update()

    def delete_current(self) -> None:
        target = (self.select.value or "").strip() or (
            self.name_input.value or ""
        ).strip()
        if not target:
            ui.notify("请选择要删除的配置", type="warning")
            return
        try:
            delete_config(self.kind, target, workdir=state.workdir)
            self.refresh_options()
            if self.select.value == target:
                self.select.value = None
                self.select.update()
            ui.notify("已删除配置", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)


class SignerBlock(BaseConfigBlock):
    def __init__(
        self,
        template: Dict[str, object],
        *,
        goto_records: Callable[[str], None] = lambda _task: None,
    ):
        self.record_btn = None
        self.record_hint = None
        self._goto_records = goto_records
        super().__init__("signer", template)

    def setup_toolbar(self):
        ui.button("交互式配置", on_click=self.open_interactive).props("outline")

    def setup_footer(self):
        self.record_hint = ui.label("").classes("text-sm text-primary")
        self.record_btn = ui.button(
            "查看签到记录",
            color="primary",
            on_click=self.goto_records,
        ).classes("min-w-[120px]")
        self.record_btn.disable()

    def on_loaded(self, target: str):
        records = load_sign_records(state.workdir)
        has_record = any(r.task == target for r in records)
        if has_record:
            self.record_btn.enable()
            self.record_hint.text = f"发现签到记录: {target}"
        else:
            self.record_btn.disable()
            self.record_hint.text = "无签到记录"
        self.record_hint.update()
        self.record_btn.update()

    def goto_records(self):
        self._goto_records(self.selected_name["value"])

    def open_interactive(self):
        def on_complete():
            self.refresh_options()
            # If the user saved a config with the same name as currently selected, reload it
            if self.select.value:
                self.load_current()

        initial_config = self.editor.properties["content"].get("json")
        initial_name = self.name_input.value or self.select.value or ""

        wizard = InteractiveSignerConfig(
            state.workdir,
            on_complete=on_complete,
            initial_config=initial_config,
            initial_name=initial_name,
        )
        wizard.open()


class MonitorBlock(BaseConfigBlock):
    def __init__(self, template: Dict[str, object]):
        super().__init__("monitor", template)


class RunnerControlBlock:
    def __init__(self, on_refresh_all: Callable[[], None] | None = None):
        self.on_refresh_all = on_refresh_all
        self.session_choices: dict[str, SessionAccount] = {}
        self.status_label = None
        self.detail_label = None
        self.selection_label = None
        self.mode_label = None
        self.mode_hint = None
        self.webui_label = None
        self.webui_hint = None
        self.account_select = None
        self.account_hint = None
        self.task_rows = None
        self.runner_rows = None
        self.start_btn = None
        self.stop_btn = None
        self.restart_btn = None

        with ui.card().classes("w-full shadow-sm gap-4"):
            ui.label("任务运行").classes("text-lg font-semibold")
            ui.label("选择已有签到配置并启动独立任务进程，Web 页面只负责控制，不承担执行。").classes(
                "text-sm text-gray-500"
            )

            with ui.row().classes("w-full gap-3 flex-wrap"):
                self.status_label, self.detail_label = build_stat_card(
                    "当前任务进程",
                    "尚未启动。",
                    tone="success",
                )
                self.mode_label, self.mode_hint = build_stat_card(
                    "任务运行方式",
                    "从这里启动后会在后台持续运行。",
                    tone="info",
                )
                self.webui_label, self.webui_hint = build_stat_card(
                    "当前账户",
                    "显示当前选择账户与默认日志。",
                    tone="info",
                )

            with ui.card().classes("w-full shadow-none border border-gray-200"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3 flex-wrap"
                ):
                    with ui.column().classes("gap-1"):
                        ui.label("任务选择").classes("text-base font-semibold")
                        self.selection_label = ui.label("").classes(
                            "text-sm text-gray-500"
                        )
                    with ui.row().classes("gap-2 flex-wrap"):
                        ui.button(
                            "全选", on_click=self.select_all_tasks
                        ).props("outline dense")
                        ui.button(
                            "清空", on_click=self.clear_selected_tasks
                        ).props("outline dense")
                        ui.button(
                            "选中上次运行", on_click=self.select_saved_tasks
                        ).props("outline dense")
                self.task_select = ui.select(
                    label="选择签到任务",
                    options=[],
                    multiple=True,
                    with_input=True,
                ).classes("w-full")

            with ui.card().classes("w-full shadow-none border border-gray-200"):
                ui.label("运行参数").classes("text-base font-semibold")
                ui.label("直接选择已登录账户，系统会自动带出对应的 Session 别名和目录。").classes(
                    "text-sm text-gray-500"
                )
                with ui.row().classes("w-full items-end gap-3 flex-wrap"):
                    self.account_select = ui.select(
                        label="已登录账户",
                        options=[],
                        on_change=lambda _e: self.on_account_selected(),
                    ).classes("min-w-[420px] flex-1")
                    ui.button("刷新账户列表", on_click=self.refresh_account_choices).props(
                        "outline"
                    )
                self.account_hint = ui.label("").classes("text-sm text-gray-500")
                initial_account = (state.runner_account or "").strip() or "my_account"
                with ui.expansion("手动覆盖 / 高级设置").classes(
                    "w-full rounded-lg border border-gray-200 bg-gray-50 px-3"
                ):
                    with ui.row().classes("w-full gap-4 flex-wrap"):
                        self.account_input = ui.input(
                            label="Session 别名",
                            value=initial_account,
                            placeholder="my_account",
                        ).classes("min-w-[220px] flex-1")
                        self.session_dir_input = ui.input(
                            label="Session 目录",
                            value=state.runner_session_dir,
                            placeholder=".",
                        ).classes("min-w-[280px] flex-1")
                    ui.label("如果自动发现不到账户，可在这里手动填写。").classes(
                        "text-xs text-gray-500"
                    )
                    ui.separator()
                    ui.label("运行控制").classes("text-sm font-semibold")
                    self.num_dialogs_input = ui.number(
                        label="登录时最近对话数量",
                        value=state.runner_num_of_dialogs,
                        min=1,
                        format="%d",
                    ).classes("w-full max-w-[260px]")
                    self.wait_switch = ui.switch(
                        "启动后等待计划时间（不补发）",
                        value=state.runner_wait_until_scheduled,
                    )
                    ui.label("开启后，任务只会在下一次计划时间触发，不会在启动瞬间补发。").classes(
                        "text-xs text-gray-500"
                    )

            with ui.row().classes("w-full gap-2 flex-wrap"):
                self.start_btn = ui.button("启动选中任务", color="primary")
                self.stop_btn = ui.button("停止当前账户", color="negative")
                self.restart_btn = ui.button("重启当前账户").props("outline")
                ui.button("刷新状态", on_click=self.refresh_all).props("outline")

            self.start_btn.on_click(self.start_selected)
            self.stop_btn.on_click(self.stop_current)
            self.restart_btn.on_click(self.restart_selected)

            with ui.card().classes("w-full shadow-none border border-gray-200"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3 flex-wrap"
                ):
                    ui.label("任务状态").classes("text-base font-semibold")
                    ui.label("显示最后执行时间和当前选择状态。").classes(
                        "text-sm text-gray-500"
                    )
                self.task_rows = ui.column().classes("w-full gap-2 mt-2")

            with ui.card().classes("w-full shadow-none border border-gray-200"):
                with ui.row().classes(
                    "w-full items-center justify-between gap-3 flex-wrap"
                ):
                    ui.label("账户进程列表").classes("text-base font-semibold")
                    ui.label("每个账户各自维护一个后台任务进程。").classes(
                        "text-sm text-gray-500"
                    )
                self.runner_rows = ui.column().classes("w-full gap-2 mt-2")

    def __call__(self, *args, **kwargs):
        self.refresh()

    @staticmethod
    def _account_choice_label(entry: SessionAccount) -> str:
        primary = entry.display_name or entry.account
        if entry.user_id:
            primary = f"{primary} ({entry.user_id})"
        return f"{primary} · {entry.account} · {entry.session_dir}"

    def _current_account_label(self) -> str:
        account = (self.account_input.value or "").strip() or "my_account"
        session_dir = (self.session_dir_input.value or "").strip() or "."
        return f"{account} · {session_dir}"

    def _selected_session_entry(self) -> SessionAccount | None:
        label = self.account_select.value if self.account_select else None
        if not label:
            return None
        return self.session_choices.get(str(label))

    def _apply_session_entry(self, entry: SessionAccount) -> None:
        state.runner_account = entry.account
        state.runner_session_dir = entry.session_dir
        self.account_input.value = entry.account
        self.session_dir_input.value = entry.session_dir
        self.account_input.update()
        self.session_dir_input.update()

    def refresh_account_choices(self) -> None:
        search_session_dir = (
            self.session_dir_input.value or state.runner_session_dir or "."
        ).strip() or "."
        entries = discover_session_accounts(search_session_dir, state.workdir)
        current_account = (self.account_input.value or state.runner_account or "").strip()
        current_session_dir = (
            self.session_dir_input.value or state.runner_session_dir or "."
        ).strip() or "."
        current_label = self.account_select.value if self.account_select else None

        self.session_choices = {
            self._account_choice_label(entry): entry for entry in entries
        }
        options = list(self.session_choices.keys())
        self.account_select.options = options

        preferred_label = None
        if current_label in self.session_choices:
            preferred_label = current_label
        else:
            for label, entry in self.session_choices.items():
                if (
                    entry.account == current_account
                    and entry.session_dir == current_session_dir
                ):
                    preferred_label = label
                    break
        if preferred_label is None and len(options) == 1:
            preferred_label = options[0]

        self.account_select.value = preferred_label
        self.account_select.update()

        if preferred_label and preferred_label in self.session_choices:
            entry = self.session_choices[preferred_label]
            self._apply_session_entry(entry)
            summary = f"已发现 {len(options)} 个登录账户，当前选择 {entry.account}"
            if entry.user_id:
                summary = f"已发现 {len(options)} 个登录账户，当前选择 {entry.display_name or entry.account} ({entry.user_id})"
            self.account_hint.text = summary
        elif options:
            self.account_hint.text = (
                f"已发现 {len(options)} 个登录账户，请先选择一个；如需覆盖，可在“手动覆盖 / 高级设置”里填写。"
            )
        else:
            self.account_hint.text = (
                "未自动发现登录账户。系统会扫描当前 Session 目录、工作目录、其上级目录、当前目录和家目录；"
                "如果仍为空，请在“手动覆盖 / 高级设置”里填写 Session 别名和目录。"
            )
        self.account_hint.update()

    def on_account_selected(self) -> None:
        entry = self._selected_session_entry()
        if entry is not None:
            self._apply_session_entry(entry)
        self.select_saved_tasks()

    def refresh_all(self) -> None:
        if self.on_refresh_all is not None:
            self.on_refresh_all()
            return
        self.refresh()

    def _current_runner_status(self) -> tuple[str, object | None]:
        account = (self.account_input.value or state.runner_account or "").strip() or "my_account"
        session_dir = (
            self.session_dir_input.value or state.runner_session_dir or "."
        ).strip() or "."
        return get_runner_status(
            state.workdir,
            account=account,
            session_dir=session_dir,
        )

    def select_all_tasks(self) -> None:
        self.task_select.value = list_task_names("signer", state.workdir)
        self.task_select.update()
        self.refresh()

    def clear_selected_tasks(self) -> None:
        self.task_select.value = []
        self.task_select.update()
        self.refresh()

    def select_saved_tasks(self) -> None:
        _, runner_state = self._current_runner_status()
        self.task_select.value = runner_state.task_names if runner_state else []
        self.task_select.update()
        self.refresh_all()

    def _selected_tasks(self) -> list[str]:
        value = self.task_select.value or []
        if isinstance(value, str):
            return [value]
        return [str(v) for v in value if v]

    def _runner_settings(self) -> tuple[str, str, int, bool]:
        account = (self.account_input.value or "my_account").strip() or "my_account"
        session_dir = (self.session_dir_input.value or ".").strip() or "."
        try:
            num_of_dialogs = int(self.num_dialogs_input.value or 50)
        except (TypeError, ValueError) as e:
            raise ValueError("最近对话数量必须是整数") from e
        wait_until_scheduled = bool(self.wait_switch.value)
        state.runner_account = account
        state.runner_session_dir = session_dir
        state.runner_num_of_dialogs = num_of_dialogs
        state.runner_wait_until_scheduled = wait_until_scheduled
        return account, session_dir, num_of_dialogs, wait_until_scheduled

    def _sync_runner_inputs(self, runner_state: object | None) -> None:
        if runner_state is not None:
            state.runner_account = runner_state.account
            state.runner_session_dir = runner_state.session_dir
            state.runner_num_of_dialogs = runner_state.num_of_dialogs
            state.runner_wait_until_scheduled = runner_state.wait_until_scheduled
        self.session_dir_input.value = state.runner_session_dir
        self.account_input.value = state.runner_account
        self.num_dialogs_input.value = state.runner_num_of_dialogs
        self.wait_switch.value = state.runner_wait_until_scheduled

        self.session_dir_input.update()
        self.account_input.update()
        self.num_dialogs_input.update()
        self.wait_switch.update()

    def refresh_task_options(self, preferred_tasks: list[str] | None = None) -> None:
        options = list_task_names("signer", state.workdir)
        current_value = self.task_select.value
        selected = self._selected_tasks()
        if current_value is None and preferred_tasks:
            selected = [task for task in preferred_tasks if task in options]
        else:
            selected = [task for task in selected if task in options]
        self.task_select.options = options
        self.task_select.value = selected
        self.task_select.update()

    def refresh(self) -> None:
        self.refresh_account_choices()
        status, runner_state = self._current_runner_status()
        self._sync_runner_inputs(runner_state)
        saved_tasks = runner_state.task_names if runner_state else []
        self.refresh_task_options(saved_tasks)
        selected_tasks = self._selected_tasks()
        runner_statuses = list_runner_statuses(state.workdir)
        running_count = sum(1 for runner_status, _ in runner_statuses if runner_status == "running")
        running_tasks = runner_state.task_names if status == "running" and runner_state else []

        if status == "running" and runner_state:
            self.status_label.text = f"{running_count} 个运行中"
            self.detail_label.text = (
                f"PID {runner_state.pid} | {len(runner_state.task_names)} 个任务 | "
                f"账号 {runner_state.account}"
            )
        elif running_count:
            self.status_label.text = f"{running_count} 个运行中"
            self.detail_label.text = f"当前选择账户未运行；后台仍有 {running_count} 个账户任务。"
        elif runner_state:
            self.status_label.text = "已保存账户任务"
            self.detail_label.text = (
                f"当前账户上次运行 {len(runner_state.task_names)} 个任务，可直接重启。"
            )
        else:
            self.status_label.text = "未运行"
            self.detail_label.text = "当前没有活跃的签到任务进程。"
        self.status_label.update()
        self.detail_label.update()

        self.mode_label.text = "后台常驻"
        self.mode_hint.text = "每个账户会独立启动后台子进程，浏览器关闭不影响执行。"
        runner_log_path = get_runner_log_file(
            state.workdir,
            account=state.runner_account,
            session_dir=state.runner_session_dir,
        )
        if runner_state:
            runner_log_path = Path(runner_state.log_path).expanduser()
        state.set_log_path(str(runner_log_path))
        self.mode_label.update()
        self.mode_hint.update()
        self.webui_label.text = state.runner_account or "my_account"
        self.webui_hint.text = (
            f"Session: {state.runner_session_dir} | 日志: {runner_log_path}"
        )
        self.webui_label.update()
        self.webui_hint.update()

        if runner_state:
            detail_text = (
                f"账户: {self._current_account_label()} | "
                f"任务: {', '.join(runner_state.task_names)} | "
                f"日志: {runner_state.log_path}"
            )
        else:
            detail_text = f"账户: {self._current_account_label()} | 日志: {runner_log_path}"

        options = self.task_select.options or []
        if not options:
            self.selection_label.text = "当前工作目录下还没有 signer 配置，请先去“配置”里创建任务。"
        elif selected_tasks:
            self.selection_label.text = (
                f"已选择 {len(selected_tasks)} / {len(options)} 个任务 | {detail_text}"
            )
        else:
            self.selection_label.text = (
                f"共 {len(options)} 个可选任务，尚未选择 | {detail_text}"
            )
        self.selection_label.update()

        records = load_sign_records(state.workdir)
        last_runs = summarize_last_runs(state.workdir, records)
        tasks_to_show = selected_tasks or running_tasks or list(options)

        self.task_rows.clear()
        with self.task_rows:
            if not tasks_to_show:
                ui.label("没有可展示的任务").classes("text-gray-500 text-sm")
            for task in tasks_to_show:
                last_run = last_runs.get(task) or "未执行"
                running = status == "running" and runner_state and task in running_tasks
                selected = task in selected_tasks
                if running:
                    badge = "运行中"
                    badge_cls = "text-positive"
                elif selected:
                    badge = "已选择"
                    badge_cls = "text-sky-700"
                else:
                    badge = "未选中"
                    badge_cls = "text-gray-500"
                with ui.row().classes(
                    "w-full items-center justify-between rounded border border-gray-200 px-3 py-2"
                ):
                    ui.label(task).classes("font-medium")
                    with ui.row().classes("gap-4 items-center"):
                        ui.label(f"最后执行: {last_run}").classes(
                            "text-sm text-gray-500"
                        )
                        ui.label(badge).classes(f"text-sm {badge_cls}")
        self.task_rows.update()

        self.runner_rows.clear()
        with self.runner_rows:
            if not runner_statuses:
                ui.label("尚未启动任何账户任务").classes("text-gray-500 text-sm")
            for runner_status, state_ in runner_statuses:
                badge_text = "运行中" if runner_status == "running" else "已退出"
                badge_cls = "text-positive" if runner_status == "running" else "text-gray-500"
                with ui.row().classes(
                    "w-full items-center justify-between rounded border border-gray-200 px-3 py-3"
                ):
                    with ui.column().classes("gap-1"):
                        ui.label(f"{state_.account} · {state_.session_dir}").classes(
                            "font-medium"
                        )
                        ui.label(
                            f"{badge_text} | {len(state_.task_names)} 个任务 | 日志: {state_.log_path}"
                        ).classes(f"text-sm {badge_cls}")
                    with ui.row().classes("gap-2 flex-wrap"):
                        ui.button(
                            "选中此账户",
                            on_click=lambda _=None, account=state_.account, session_dir=state_.session_dir: self.select_runner_account(
                                account, session_dir
                            ),
                        ).props("outline dense")
                        if runner_status == "running":
                            ui.button(
                                "停止",
                                on_click=lambda _=None, runner_id=state_.runner_id, account=state_.account, session_dir=state_.session_dir: self.stop_specific_runner(
                                    runner_id, account, session_dir
                                ),
                                color="negative",
                            ).props("dense")
                        ui.button(
                            "重启",
                            on_click=lambda _=None, account=state_.account, session_dir=state_.session_dir: self.restart_specific_runner(
                                account, session_dir
                            ),
                        ).props("outline dense")
        self.runner_rows.update()

        is_running = status == "running"
        has_selection = bool(selected_tasks)
        if is_running:
            self.start_btn.disable()
            self.stop_btn.enable()
        else:
            if has_selection:
                self.start_btn.enable()
            else:
                self.start_btn.disable()
        if is_running:
            self.stop_btn.enable()
        else:
            self.stop_btn.disable()
        if has_selection:
            self.restart_btn.enable()
        else:
            self.restart_btn.disable()
        self.start_btn.update()
        self.stop_btn.update()
        self.restart_btn.update()

    def select_runner_account(self, account: str, session_dir: str) -> None:
        state.runner_account = account
        state.runner_session_dir = session_dir
        self.account_input.value = account
        self.session_dir_input.value = session_dir
        self.account_input.update()
        self.session_dir_input.update()
        for label, entry in self.session_choices.items():
            if entry.account == account and entry.session_dir == session_dir:
                self.account_select.value = label
                self.account_select.update()
                break
        self.select_saved_tasks()

    def stop_specific_runner(
        self, runner_id: str | None, account: str, session_dir: str
    ) -> None:
        try:
            stop_runner(
                state.workdir,
                runner_id=runner_id,
                account=account,
                session_dir=session_dir,
            )
            ui.notify(f"已停止账户 {account}", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)
        self.refresh_all()

    def restart_specific_runner(self, account: str, session_dir: str) -> None:
        self.select_runner_account(account, session_dir)
        self.restart_selected()

    def start_selected(self) -> None:
        try:
            account, session_dir, num_of_dialogs, wait_until_scheduled = (
                self._runner_settings()
            )
            runner_state = start_runner(
                state.workdir,
                session_dir,
                account,
                self._selected_tasks(),
                num_of_dialogs=num_of_dialogs,
                wait_until_scheduled=wait_until_scheduled,
            )
            ui.notify(
                f"已启动任务: {', '.join(runner_state.task_names)}", type="positive"
            )
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)
        self.refresh_all()

    def stop_current(self) -> None:
        try:
            account, session_dir, _, _ = self._runner_settings()
            state_ = stop_runner(
                state.workdir,
                account=account,
                session_dir=session_dir,
            )
            if state_ is None:
                ui.notify("当前账户没有运行中的签到进程", type="warning")
            else:
                ui.notify(f"已停止账户 {account} 的签到进程", type="positive")
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)
        self.refresh_all()

    def restart_selected(self) -> None:
        try:
            account, session_dir, num_of_dialogs, wait_until_scheduled = (
                self._runner_settings()
            )
            stop_runner(
                state.workdir,
                account=account,
                session_dir=session_dir,
            )
            runner_state = start_runner(
                state.workdir,
                session_dir,
                account,
                self._selected_tasks(),
                num_of_dialogs=num_of_dialogs,
                wait_until_scheduled=wait_until_scheduled,
            )
            ui.notify(
                f"已重启任务: {', '.join(runner_state.task_names)}", type="positive"
            )
        except Exception as exc:  # noqa: BLE001
            notify_error(exc)
        self.refresh_all()


def user_info_block() -> Callable[[], None]:
    container = ui.column().classes("w-full gap-2")

    def refresh() -> None:
        container.clear()
        entries = load_user_infos(state.workdir)
        with container:
            if not entries:
                ui.label("未找到用户信息").classes("text-gray-500")
                return
            for entry in entries:
                data = entry.data if isinstance(entry.data, dict) else {"raw": entry.data}
                name = (
                    data.get("first_name")
                    or data.get("username")
                    or data.get("last_name")
                    or ""
                )
                header = f"{entry.user_id} {name}".strip()
                with ui.expansion(header, icon="person"):
                    ui.label(f"文件: {entry.path}")
                    ui.code(pretty_json(data), language="json").classes("w-full")

                    if entry.latest_chats:
                        ui.separator().classes("my-2")
                        ui.label(f"最近聊天 ({len(entry.latest_chats)})").classes(
                            "font-semibold"
                        )

                        chat_rows = []
                        for chat in entry.latest_chats:
                            chat_rows.append(
                                {
                                    "id": chat.get("id"),
                                    "title": chat.get("title")
                                    or chat.get("first_name")
                                    or "N/A",
                                    "type": chat.get("type"),
                                    "username": chat.get("username") or "",
                                }
                            )

                        ui.table(
                            columns=[
                                {
                                    "name": "id",
                                    "label": "ID",
                                    "field": "id",
                                    "align": "left",
                                },
                                {
                                    "name": "title",
                                    "label": "名称",
                                    "field": "title",
                                    "align": "left",
                                },
                                {
                                    "name": "type",
                                    "label": "类型",
                                    "field": "type",
                                    "align": "left",
                                },
                                {
                                    "name": "username",
                                    "label": "用户名",
                                    "field": "username",
                                    "align": "left",
                                },
                            ],
                            rows=chat_rows,
                            pagination=10,
                        ).classes("w-full").props("flat dense")
                    else:
                        ui.label("未找到最近聊天记录").classes(
                            "text-gray-500 text-sm mt-2"
                        )

    return refresh


class SignRecordBlock:
    def __init__(self):
        self.container = ui.column().classes("w-full gap-3")
        with ui.row().classes("items-end gap-3"):
            self.filter_input = ui.input(
                label="筛选任务/用户",
                placeholder="输入任务名或用户ID过滤",
                value=state.record_filter,
                on_change=lambda e: self._update_filter(e.value),
            ).classes("w-full")
            ui.button("清除筛选", on_click=lambda: self._update_filter("")).props(
                "outline"
            )
        self.status = ui.label("").classes("text-sm text-gray-500")

    def _update_filter(self, value: str) -> None:
        state.record_filter = value or ""
        self.refresh()

    def refresh(
        self,
    ) -> None:
        self.container.clear()
        records = load_sign_records(state.workdir)
        keyword = (state.record_filter or "").lower().strip()
        if keyword:
            records = [
                r
                for r in records
                if keyword in r.task.lower()
                or (r.user_id and keyword in str(r.user_id).lower())
            ]
        with self.container:
            if not records:
                self.status.text = "未找到匹配的签到记录" if keyword else "尚无签到记录"
                self.status.update()
                return
            self.status.text = f"共 {len(records)} 组记录"
            self.status.update()
            for record in records:
                user_text = record.user_id or "默认"
                header = f"{record.task} / {user_text}（{len(record.records)}条）"
                with ui.expansion(header, icon="event").classes("shadow-sm"):
                    ui.label(f"文件: {record.path}").classes("text-gray-500")
                    if not record.records:
                        ui.label("暂无记录").classes("text-gray-500")
                        continue
                    rows = [{"日期": k, "时间": v} for k, v in record.records]
                    ui.table(
                        columns=[
                            {"name": "日期", "label": "日期", "field": "日期"},
                            {"name": "时间", "label": "时间", "field": "时间"},
                        ],
                        rows=rows,
                    ).classes("w-full").props("flat dense")

    def __call__(self, *args, **kwargs):
        return self.refresh()


def log_block() -> Callable[[], None]:
    with ui.card().classes("w-full shadow-sm"):
        ui.label("日志查看").classes("text-md font-semibold")
        ui.label("查看最新日志行，可自定义文件路径和行数。").classes(
            "text-sm text-gray-500 mb-1"
        )

        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            limit_input = ui.number(
                label="日志行数",
                value=state.log_limit,
                min=10,
                max=2000,
                format="%d",
            ).classes("w-32")
            log_select = ui.select(
                label=f"选择日志文件（{LOG_DIR}/）",
                options=[],
                on_change=lambda e: select_log_file(e.value),
            ).classes("min-w-[220px]")
            log_path_input = ui.input(
                label="日志路径（可自定义）", value=str(state.log_path)
            ).classes("w-full")
        log_area = ui.scroll_area().classes(
            "w-full bg-gray-50 rounded-lg border border-gray-200"
        )
        log_area.style("max-height: 420px")
        with log_area:
            log_list = (
                ui.column()
                .classes("w-full gap-0 p-3 font-mono text-sm")
                .style("white-space: pre;")
            )
        last_synced_log_path = {"value": str(state.log_path)}

        def classify_line(line: str) -> str:
            upper = line.upper()
            if "ERROR" in upper:
                return "text-red-700"
            if "WARN" in upper:
                return "text-amber-700"
            if "INFO" in upper:
                return "text-blue-700"
            return "text-gray-800"

        def refresh_log_options() -> None:
            options = []
            for base_dir in [LOG_DIR, state.workdir / "logs"]:
                for path in list_log_files(base_dir):
                    path_str = str(path)
                    if path_str not in options:
                        options.append(path_str)
            for _, runner_state in list_runner_statuses(state.workdir):
                path_str = str(Path(runner_state.log_path).expanduser())
                if path_str not in options:
                    options.append(path_str)
            current_path = str(state.log_path)
            if current_path and current_path not in options:
                options.insert(0, current_path)
            log_select.options = options
            log_select.value = current_path
            log_select.update()
            log_path_input.value = current_path
            log_path_input.update()
            last_synced_log_path["value"] = current_path

        def select_log_file(path_value: str | None) -> None:
            if not path_value:
                return
            state.set_log_path(path_value)
            refresh()

        def refresh() -> None:
            try:
                state.log_limit = int(limit_input.value or state.log_limit)
            except ValueError:
                state.log_limit = 200
            input_path = str(log_path_input.value or "").strip()
            if input_path and input_path != last_synced_log_path["value"]:
                state.set_log_path(input_path)
            refresh_log_options()
            path, lines = load_logs(state.log_limit, state.log_path)
            log_list.clear()
            if not lines:
                with log_list:
                    if path.is_file():
                        ui.label(f"日志文件存在但当前还没有内容: {path}").classes(
                            "text-gray-500 text-sm"
                        )
                    else:
                        ui.label(f"未找到日志文件: {path}").classes(
                            "text-gray-500 text-sm"
                        )
                log_list.update()
                if path.is_file():
                    refresh_status(f"日志文件为空: {path}")
                else:
                    refresh_status(f"未找到日志文件: {path}")
                return

            with log_list:
                for line in lines:
                    color = classify_line(line)
                    ui.label(line).classes(f"w-full {color}").style("white-space: pre;")
            log_list.update()
            refresh_status(f"文件: {path} | 显示最新 {len(lines)} 行")

        with ui.row().classes("gap-2 mt-2 items-center justify-between"):
            ui.button("刷新日志", on_click=refresh)
            log_status = ui.label("").classes("text-xs text-gray-500")

        def refresh_status(text: str) -> None:
            log_status.text = text
            log_status.update()

        refresh_log_options()

    return refresh


def top_controls(on_refresh: Callable[[], None]) -> None:
    with ui.card().classes("w-full shadow-sm"):
        ui.label("工作区").classes("text-lg font-semibold")
        ui.label("切换后会同时刷新配置、运行状态、签到记录和日志。").classes(
            "text-sm text-gray-500"
        )
        with ui.row().classes("items-end w-full gap-3 flex-wrap"):
            workdir_input = ui.input(
                label="工作目录",
                value=str(state.workdir),
                placeholder=".signer",
            ).classes("w-full")
            ui.button(
                "应用并刷新",
                color="primary",
                on_click=lambda: _apply_paths(workdir_input, on_refresh),
            )


def _apply_paths(workdir_input, on_refresh: Callable[[], None]) -> None:
    try:
        state.set_workdir(workdir_input.value or str(DEFAULT_WORKDIR))
        ui.notify(f"已切换工作目录: {state.workdir}", type="positive")
    except Exception as exc:  # noqa: BLE001
        notify_error(exc)
        return
    on_refresh()


class OverviewBlock:
    def __init__(self):
        with ui.row().classes("w-full gap-3 flex-wrap"):
            self.workdir_value, self.workdir_hint = build_stat_card(
                "当前工作目录",
                "配置、记录和日志都从这里读取。",
                tone="info",
            )
            self.signer_value, self.signer_hint = build_stat_card(
                "签到任务数",
                "可在“任务运行”里选择性启动。",
            )
            self.monitor_value, self.monitor_hint = build_stat_card(
                "监控任务数",
                "仅用于消息监控配置。",
            )
            self.runner_value, self.runner_hint = build_stat_card(
                "任务进程",
                "显示当前后台任务状态。",
                tone="success",
            )

    def __call__(self, *args, **kwargs):
        self.refresh()

    def refresh(self) -> None:
        signer_tasks = list_task_names("signer", state.workdir)
        monitor_tasks = list_task_names("monitor", state.workdir)
        runner_statuses = list_runner_statuses(state.workdir)
        running_count = sum(
            1 for runner_status, _ in runner_statuses if runner_status == "running"
        )

        self.workdir_value.text = str(state.workdir)
        self.signer_value.text = str(len(signer_tasks))
        self.monitor_value.text = str(len(monitor_tasks))

        if running_count:
            self.runner_value.text = f"{running_count} 个运行中"
            self.runner_hint.text = f"共保存 {len(runner_statuses)} 个账户任务配置。"
        elif runner_statuses:
            self.runner_value.text = "已保存"
            self.runner_hint.text = f"共保存 {len(runner_statuses)} 个账户任务配置。"
        else:
            self.runner_value.text = "未运行"
            self.runner_hint.text = "当前没有后台签到任务。"

        self.workdir_value.update()
        self.signer_value.update()
        self.monitor_value.update()
        self.runner_value.update()
        self.runner_hint.update()


def _build_dashboard(container) -> None:
    with container:
        with ui.card().classes("w-full shadow-sm bg-slate-900 text-white"):
            ui.label("TG Signer Web 控制台").classes(
                "text-3xl font-semibold tracking-tight"
            )
            ui.label(
                "把配置管理、任务运行、签到记录和日志放到同一个面板里，减少命令切换。"
            ).classes("text-sm text-slate-200")
        refreshers: list[Callable[[], None]] = []
        refresh_records: "SignRecordBlock"

        def refresh_all() -> None:
            for refresh in refreshers:
                try:
                    refresh()
                except Exception:
                    logger.exception("WebUI refresh failed")

        top_controls(refresh_all)
        refreshers.append(OverviewBlock())

        with ui.tabs().classes("w-full") as tabs:
            tab_configs = ui.tab("配置")
            tab_runner = ui.tab("任务运行")
            tab_users = ui.tab("用户")
            tab_records = ui.tab("记录")
            tab_logs = ui.tab("日志")

        def goto_records(task_name: str) -> None:
            tabs.value = tab_records
            tabs.update()
            refresh_records.filter_input.set_value(task_name)

        with ui.tab_panels(tabs, value=tab_configs).classes("w-full"):
            with ui.tab_panel(tab_configs):
                ui.label(
                    "管理 signer 和 monitor 的配置文件，支持查看、编辑和删除。"
                ).classes("text-gray-600")
                with ui.tabs().classes("mt-2") as sub_tabs:
                    tab_signer = ui.tab("Signer")
                    tab_monitor = ui.tab("Monitor")
                with ui.tab_panels(sub_tabs, value=tab_signer).classes("w-full"):
                    with ui.tab_panel(tab_signer):
                        refreshers.append(
                            SignerBlock(SIGNER_TEMPLATE, goto_records=goto_records)
                        )
                    with ui.tab_panel(tab_monitor):
                        refreshers.append(MonitorBlock(MONITOR_TEMPLATE))

            with ui.tab_panel(tab_runner):
                ui.label("Web 页面负责控制，任务本身在后台子进程执行。").classes(
                    "text-gray-600"
                )
                refreshers.append(RunnerControlBlock(refresh_all))

            with ui.tab_panel(tab_users):
                ui.label("查看当前已登录账户信息 (users/*/me.json)。").classes(
                    "text-gray-600"
                )
                refreshers.append(user_info_block())

            with ui.tab_panel(tab_records):
                ui.label("签到记录 sign_record.json").classes("text-gray-600")
                refresh_records = SignRecordBlock()
                refreshers.append(refresh_records)

            with ui.tab_panel(tab_logs):
                ui.label("查看日志文件的最新行。").classes("text-gray-600")
                refreshers.append(log_block())

        refresh_all()


def _auth_gate(container, auth_code: str, on_success: Callable[[], None]) -> None:
    with container:
        ui.label("TG Signer Web 控制台").classes(
            "text-2xl font-semibold tracking-wide mb-2"
        )
        ui.label("已启用访问控制，请输入 Auth Code 继续使用 Web 控制台。").classes(
            "text-gray-600"
        )
        with ui.column().classes("w-full items-center"):
            with ui.card().classes("w-full max-w-xl shadow-md"):
                ui.label("Auth Code 验证").classes("text-lg font-semibold")
                ui.label("检测到auth_code环境变量已配置，首次访问需验证。").classes(
                    "text-sm text-gray-500"
                )
                code_input = ui.input(
                    label="Auth Code",
                    placeholder="请输入授权码",
                    password=True,
                    password_toggle_button=True,
                ).classes("w-full")
                status = ui.label("").classes("text-sm text-negative")

                def verify() -> None:
                    # TODO: Security improvements needed
                    # 1. Add rate limiting (e.g. max 5 attempts per minute) to prevent brute-force attacks.
                    # 2. Use secrets.compare_digest(code, auth_code) to prevent timing attacks.
                    code = (code_input.value or "").strip()
                    if not code:
                        ui.notify("请输入授权码", type="warning")
                        return
                    if code != auth_code:
                        status.text = "授权码错误，请重试"
                        status.update()
                        code_input.set_value("")
                        ui.notify("认证失败", type="negative")
                        return
                    app.storage.user[AUTH_STORAGE_KEY] = auth_code
                    ui.notify("认证成功", type="positive")
                    container.clear()
                    on_success()

                ui.button("验证并进入", color="primary", on_click=verify).classes(
                    "w-full mt-2"
                )


def build_ui(auth_code: str = None) -> None:
    ui.page_title("TG Signer Web 控制台")
    ui.add_head_html("<style>body { background: #f8fafc; }</style>")
    root = ui.column().classes("w-full max-w-[1400px] mx-auto gap-4 px-4 py-4")

    def render_dashboard() -> None:
        root.clear()
        _build_dashboard(root)

    auth_code = auth_code or (os.environ.get(AUTH_CODE_ENV) or "").strip()
    if not auth_code:
        render_dashboard()
        return

    if app.storage.user.get(AUTH_STORAGE_KEY) == auth_code:
        render_dashboard()
        return

    root.clear()
    _auth_gate(root, auth_code, render_dashboard)


def main(host: str = None, port: int = None, storage_secret: str = None) -> None:
    ui.run(
        build_ui,
        title="TG Signer WebUI",
        favicon="⚙️",
        reload=False,
        host=host,
        port=port,
        show=False,
        storage_secret=storage_secret or os.urandom(10).hex(),
    )
