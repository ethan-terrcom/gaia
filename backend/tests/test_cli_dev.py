"""Tests for `gaia dev fake-run` (doc 13)."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import pytest

from gaia import cli
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings

DUMMY_TOKEN = "dummy" + "-api-token-value"


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    value = Settings(data_dir=tmp_path / "gaia-data")
    monkeypatch.setattr(cli, "get_settings", lambda: value)
    return value


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    fake = InMemorySecretStore()
    fake.set(SecretName.GAIA_API_TOKEN, DUMMY_TOKEN)
    monkeypatch.setattr(cli, "get_secret_store", lambda: fake)
    return fake


def test_dev_fake_run_posts_and_prints_the_run_id(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Response:
        calls["url"] = url
        calls["headers"] = kwargs["headers"]
        calls["json"] = kwargs["json"]
        return _Response(200, {"run_id": "run-123"})

    monkeypatch.setattr(httpx, "post", fake_post)

    assert cli.main(["dev", "fake-run", "--events", "4", "--interval", "0.25"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "run-123"
    assert DUMMY_TOKEN not in captured.out + captured.err
    assert calls["url"] == "http://127.0.0.1:8765/api/v1/dev/fake-run"
    assert calls["headers"] == {"Authorization": f"Bearer {DUMMY_TOKEN}"}
    assert calls["json"] == {"events": 4, "interval_s": 0.25}


def test_dev_fake_run_reports_a_connection_error_without_the_token(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: object) -> _Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "could not reach Gaia" in captured.err
    assert "boom" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_fake_run_reports_a_non_200_status(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: _Response(422, {}))

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "Gaia answered 422" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_fake_run_reports_a_non_json_body(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: _Response(200, ValueError("not json")),
    )

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "non-JSON" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_with_an_unknown_action_is_an_error() -> None:
    assert cli._dev(argparse.Namespace(dev_command="other")) == 2
