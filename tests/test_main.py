from pathlib import Path
import runpy
import types

import pytest

import tg_signer


def test_python_dash_m_invokes_cli(monkeypatch):
    called = {"count": 0}

    def fake_tg_signer():
        called["count"] += 1
        return 0

    monkeypatch.setattr(
        tg_signer,
        "cli",
        types.SimpleNamespace(tg_signer=fake_tg_signer),
        raising=False,
    )

    main_file = Path(__file__).resolve().parents[1] / "tg_signer" / "__main__.py"

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(main_file), run_name="__main__")

    assert exc_info.value.code == 0
    assert called["count"] == 1
