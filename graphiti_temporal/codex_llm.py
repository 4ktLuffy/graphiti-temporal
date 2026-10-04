"""Graphiti's LLM calls answered by the Codex CLI, so real-model runs need no API key.

Each call is one `codex exec`: Graphiti's system message replaces Codex's agent instructions
(`model_instructions_file`), the user message is the prompt, and the response model's JSON schema
is passed as `--output-schema`. Adapted from judge-admissibility's `bench/codex_judge.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.prompts.models import Message

WORKDIR = Path(tempfile.gettempdir()) / 'graphiti-temporal-codex'
# Phrases Codex prints when the account is out of usage; a run must stop, not record failures.
QUOTA_SIGNS = ('usage limit', 'quota', 'out of credits', 'insufficient credits', 'rate limit',
               '429 too many requests', 'purchase more credits')  # fmt: skip


class CodexQuotaExhausted(RuntimeError):
    """Codex is out of credits or rate-limited. Reset the account, then rerun."""


def _strict(schema: Any) -> Any:
    """Codex's structured output wants every object closed and every property required."""
    if isinstance(schema, dict):
        out = {k: _strict(v) for k, v in schema.items() if k not in ('title', 'default')}
        if out.get('type') == 'object' and 'properties' in out:
            out['additionalProperties'] = False
            out['required'] = list(out['properties'])
        return out
    if isinstance(schema, list):
        return [_strict(v) for v in schema]
    return schema


def _write_once(path: Path, text: str) -> Path:
    if not path.exists():
        partial = path.with_suffix(f'.{id(text)}.tmp')
        partial.write_text(text)
        partial.replace(path)
    return path


class CodexLLMClient(LLMClient):
    def __init__(
        self,
        model: str = 'gpt-5.6-luna',
        effort: str = 'low',
        timeout: float = 300,
        concurrency: int = 4,
        slots: asyncio.Semaphore | None = None,
    ):
        super().__init__(LLMConfig(model=model), cache=False)
        self.codex_model, self.effort, self.timeout = model, effort, timeout
        self._slots = slots or asyncio.Semaphore(concurrency)
        self.calls: list[dict[str, Any]] = []
        WORKDIR.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return f'codex:{self.codex_model}@{self.effort}'

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        system = '\n'.join(m.content for m in messages if m.role == 'system')
        prompt = '\n\n'.join(m.content for m in messages if m.role != 'system')
        system += '\nReply with JSON only, matching the requested schema.'
        instructions = _write_once(
            WORKDIR / f'instr-{hashlib.sha256(system.encode()).hexdigest()[:16]}.md', system
        )
        args = [
            'codex', 'exec', '-m', self.codex_model, '-c', f'model_reasoning_effort="{self.effort}"',
            '-c', f'model_instructions_file="{instructions}"',
            '--skip-git-repo-check', '--sandbox', 'read-only', '--ephemeral', '--color', 'never',
        ]  # fmt: skip
        if response_model is not None:
            schema = json.dumps(_strict(response_model.model_json_schema()))
            path = WORKDIR / f'schema-{hashlib.sha256(schema.encode()).hexdigest()[:16]}.json'
            args += ['--output-schema', str(_write_once(path, schema))]
        with tempfile.NamedTemporaryFile(dir=WORKDIR, suffix='.out', delete=False) as f:
            out = Path(f.name)
        args += ['-o', str(out), prompt]

        async with self._slots:
            proc = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, cwd=WORKDIR,
            )  # fmt: skip
            try:
                log, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
            except TimeoutError:
                proc.kill()
                raise
        text = log.decode(errors='replace')
        if proc.returncode != 0 and any(sign in text.lower() for sign in QUOTA_SIGNS):
            raise CodexQuotaExhausted(f'Codex usage exhausted: {text[-300:]}')
        if proc.returncode != 0:
            raise RuntimeError(f'codex exited {proc.returncode}: {text[-400:]}')
        reply = out.read_text().strip()
        out.unlink(missing_ok=True)
        tokens = None
        if 'tokens used' in text:
            with contextlib.suppress(ValueError):
                tokens = int(text.split('tokens used', 1)[1].split()[0].replace(',', ''))
        if reply.startswith('```'):
            reply = reply.strip('`').removeprefix('json').strip()
        parsed = json.loads(reply)
        # Kept so a wrong outcome can be traced to what the model saw and said.
        self.calls.append(
            {
                'response_model': getattr(response_model, '__name__', None),
                'tokens': tokens,
                'prompt': prompt,
                'reply': parsed,
            }
        )
        return parsed
