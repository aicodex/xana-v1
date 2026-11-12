#!/usr/bin/env python3
"""Offline, backed-up conversion of conversation history to the single-loop format.

Runtime never imports this script. Run --apply only after all services for the
selected projects have stopped. Without --apply this only produces a plan.
Malformed records are rejected rather than silently discarded.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

KINDS = {'user', 'assistant', 'tool', 'system', 'compact'}
UNKNOWN = '运行中断，工具执行结果未知；请先核实外部状态，勿直接重复操作。'


def json_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def encode_lines(rows: list[dict]) -> bytes:
    return ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode()


def normalize(row: dict) -> dict:
    row = deepcopy(row)
    payload = row.get('payload', {})
    meta = payload.get('metadata') or {}
    if payload.get('role') == 'assistant':
        calls = meta.get('tool_calls')
        if calls is None and 'tool_call' in meta:
            calls = [meta.pop('tool_call')]
        if calls is not None:
            converted = []
            for call in calls:
                ident = call.get('call_id') or call.get('id')
                name = call.get('tool_name') or call.get('name')
                arguments = call.get('input', call.get('arguments', {}))
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not ident or not name or not isinstance(arguments, dict):
                    raise ValueError('unrecognized tool call; source preserved for manual inspection')
                converted.append({'call_id': ident, 'tool_name': name, 'input': arguments})
            meta['tool_calls'] = converted
    payload['metadata'] = meta
    if payload:
        row['payload'] = payload
    return row


def repair_tools(rows: list[dict]) -> list[dict]:
    """Insert unknown results before the next unrelated message, never replay effects."""
    output = []
    pending: dict[tuple[str, str], dict[str, tuple[str, dict]]] = {}

    def flush(key):
        for ident, (name, source) in pending.get(key, {}).items():
            output.append({'row_id': f'migration:unknown:{sha256((source["row_id"]+ident).encode()).hexdigest()[:24]}',
                'session_id': key[0], 'agent_name': key[1], 'kind': 'tool', 'ts': source.get('ts', 0),
                'payload': {'role': 'tool', 'content': UNKNOWN,
                    'metadata': {'tool_use_id': ident, 'tool_name': name, 'ok': False}},
                'metadata': {'migration': 'single-loop'}})
        pending.pop(key, None)

    for row in rows:
        row = normalize(row)
        key = (row['session_id'], row['agent_name'])
        role = row['payload']['role']
        if role != 'tool':
            flush(key)
        if role == 'tool':
            ident = row['payload']['metadata'].get('tool_use_id')
            matched = pending.get(key, {}).pop(ident, None)
            if matched is None:
                original = deepcopy(row['payload'])
                row['kind'], row['payload']['role'] = 'user', 'user'
                name = original['metadata'].get('tool_name') or '?'
                row['payload']['content'] = f'[恢复的工具结果 {name}；原调用不在当前历史中]\n' + original['content']
                row['payload']['metadata'] = {'recovered_tool_result': original['metadata']}

        output.append(row)
        for call in row['payload']['metadata'].get('tool_calls', ()):
            pending.setdefault(key, {})[call['call_id']] = (call['tool_name'], row)
    for key in tuple(pending):
        flush(key)
    return output


def convert_history(path: Path, trace_root: Path) -> tuple[bytes, int]:
    original = json_lines(path)
    output = []
    used = {r['row_id'] for r in original if r.get('kind') != 'placeholder'}
    placeholders = 0
    for row in original:
        if row.get('kind') != 'placeholder':
            if row.get('kind') not in KINDS:
                raise ValueError(f'unknown history kind in {path.name}')
            output.append(row)
            continue
        placeholders += 1
        metadata = row['payload']['metadata']
        rid = metadata['run_id']
        # Explicit stale compact reservations carry no output. Their complete
        # source is retained in the migration backup, not converted to summaries.
        if metadata.get('metadata', {}).get('kind') == 'compact':
            continue
        trace = trace_root / f'{rid}.jsonl'
        if not trace.exists():
            trace = trace_root / f'{rid}.aborted.jsonl'
        trace_rows = json_lines(trace)
        completed = any(item.get('marker') == 'run_completed' and item.get('status') == 'completed' for item in trace_rows)
        for item in trace_rows:
            # The user row is already immediately before this reservation.
            if item.get('kind') not in {'assistant', 'tool'}:
                continue
            item = deepcopy(item)
            item['session_id'], item['agent_name'] = row['session_id'], row['agent_name']
            if item['row_id'] not in used:
                output.append(item)
                used.add(item['row_id'])
        if not completed:
            output.append({'row_id': f'migration:interrupted:{rid}',
                'session_id': row['session_id'], 'agent_name': row['agent_name'],
                'kind': 'assistant', 'ts': row.get('ts', 0), 'metadata': {'migration': 'single-loop'},
                'payload': {'role': 'assistant', 'content': '这次回答在旧服务中中断；已恢复原有输出，未重新执行工具。',
                    'metadata': {}}})
    return encode_lines(repair_tools(output)), placeholders


@dataclass(frozen=True)
class Change:
    path: Path
    before: bytes
    after: bytes


def plan(project: Path) -> tuple[list[Change], dict[str, int]]:
    changes: list[Change] = []
    counts = {'configs': 0, 'histories': 0, 'reservations': 0, 'checkpoints': 0, 'records': 0}
    sessions = project / 'sessions'
    for path in sorted(sessions.glob('*.json')):
        before = path.read_bytes()
        data = json.loads(before)
        if 'async_mode' in data:
            data.pop('async_mode')
            changes.append(Change(path, before, json.dumps(data, ensure_ascii=False, indent=2).encode()))
            counts['configs'] += 1
    for path in sorted(sessions.glob('*.jsonl')):
        before = path.read_bytes()
        after, count = convert_history(path, sessions / 'runs')
        # Avoid reformatting an unchanged current-format log.
        if json_lines(path) != [json.loads(line) for line in after.splitlines()]:
            changes.append(Change(path, before, after))
            counts['histories'] += 1
        counts['reservations'] += count
    for path in sorted((project / 'runs').glob('*.json')):
        before = path.read_bytes()
        data = json.loads(before)
        changed = False
        checkpoint = data.get('last_checkpoint')
        if checkpoint and checkpoint.get('name') == 'after_compact' and checkpoint.get('payload', {}).get('version', 1) != 2:
            data['last_checkpoint'] = None
            changed = True
            counts['checkpoints'] += 1
        metadata = data.get('metadata') or {}
        if 'attached' in metadata:
            metadata.pop('attached')
            data['metadata'] = metadata
            changed = True
        if data.get('status') in {'running', 'paused'}:
            rid = data['run_id']
            trace = sessions / 'runs' / f'{rid}.jsonl'
            markers = [r for r in json_lines(trace) if r.get('marker') == 'run_completed']
            if markers and markers[-1].get('status') in {'completed', 'cancelled', 'failed'}:
                data['status'] = markers[-1]['status']
                if markers[-1].get('ts') is not None:
                    data.setdefault('metadata', {})['finished_at'] = markers[-1]['ts']
                if data['status'] == 'completed':
                    data['exit_reason'] = None
                changed = True
            elif data.get('status') == 'running':
                data['status'], data['exit_reason'] = 'paused', 'shutdown_force'
                changed = True
        if changed and data != json.loads(before):
            changes.append(Change(path, before, json.dumps(data, ensure_ascii=False).encode()))
            counts['records'] += 1
    return changes, counts


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    with temporary.open('xb') as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_stopped(project: Path) -> None:
    pidfile = project / 'run' / 'serve.pid'
    if not pidfile.exists():
        return
    pid = int(pidfile.read_text().strip())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise RuntimeError(f'{project.name}: service is still running; stop and drain before applying migration')


def apply(project: Path, changes: list[Change], backup: Path) -> None:
    require_stopped(project)
    # Check the complete plan before touching any source, including retries.
    for change in changes:
        if change.path.read_bytes() != change.before:
            raise RuntimeError('source changed after planning; regenerate migration plan')
    backup.mkdir(parents=True, exist_ok=False)
    manifest = []
    for change in changes:
        relative = change.path.relative_to(project)
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, change.before)
        manifest.append({'path': str(relative), 'before': sha256(change.before).hexdigest(),
                         'after': sha256(change.after).hexdigest()})
    atomic_write(backup / 'manifest.json', json.dumps({'project': str(project), 'changes': manifest}, indent=2).encode())
    for change in changes:
        if change.path.read_bytes() != change.before:
            raise RuntimeError('source changed while applying; backups retained, stop and re-plan')
        atomic_write(change.path, change.after)
    atomic_write(backup / 'complete', b'complete\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects', nargs='+', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-root', type=Path)
    args = parser.parse_args()
    if args.apply and args.backup_root is None:
        parser.error('--apply requires --backup-root')
    for project in args.projects:
        project = project.resolve()
        changes, counts = plan(project)
        if args.apply and changes:
            apply(project, changes, args.backup_root / f'{project.name}-{time.time_ns()}')
        print(json.dumps({'project': project.name, 'apply': args.apply, 'changed_files': len(changes), **counts}))

if __name__ == '__main__':
    main()
