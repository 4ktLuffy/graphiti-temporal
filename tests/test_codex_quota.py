"""A Codex usage-limit error must stop a run, not become a failed trial."""

import pytest

from graphiti_temporal import codex_llm
from graphiti_temporal.codex_llm import CodexLLMClient, CodexQuotaExhausted


async def test_usage_limit_raises(monkeypatch, tmp_path):
    fake = tmp_path / 'codex'
    fake.write_text(
        '#!/bin/sh\necho "ERROR: You\'ve hit your usage limit. Try again later."\nexit 1\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}:/usr/bin:/bin')
    monkeypatch.setattr(codex_llm, 'WORKDIR', tmp_path)
    from graphiti_core.prompts.models import Message

    with pytest.raises(CodexQuotaExhausted):
        await CodexLLMClient().generate_response([Message(role='user', content='hi')])
