import os
import subprocess
import tarfile
import time
from pathlib import Path

from radar.health import healthy

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OLD = 'b' * 40


def test_heartbeat(tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    assert not healthy(heartbeat)
    heartbeat.touch()
    assert healthy(heartbeat)
    old = time.time() - 120
    os.utime(heartbeat, (old, old))
    assert not healthy(heartbeat)


def prepare(tmp_path, session=True, previous=False):
    base = tmp_path / 'radar-promocoes'
    shared = base / 'shared'
    (shared / 'data').mkdir(parents=True)
    (shared / '.env').write_text('SECRET=preserve-me\n')
    (shared / 'data' / 'radar.sqlite3').write_bytes(b'preserve database')
    if session:
        (shared / 'data' / 'telegram.session').touch()
        (shared / 'data' / 'telegram.session').write_bytes(b'session')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'compose.yaml').write_text('services: {}\n')
    with tarfile.open(base / f'{SHA}.tar.gz', 'w:gz') as archive:
        archive.add(source / 'compose.yaml', arcname='compose.yaml')
    if previous:
        (base / 'releases' / OLD).mkdir(parents=True)
        (base / 'current').symlink_to(base / 'releases' / OLD)
    commands = tmp_path / 'commands'
    commands.mkdir()
    docker = commands / 'docker'
    docker.write_text('''#!/usr/bin/env bash
printf '%s %s\\n' "$RADAR_RELEASE" "$*" >> "$HOME/docker-calls"
if [[ "${FAIL_NEW:-0}" == 1 && "$RADAR_RELEASE" == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" && "$*" == *"up -d"* ]]; then exit 1; fi
''')
    docker.chmod(0o755)
    return base, dict(os.environ, HOME=str(tmp_path), PATH=str(commands) + ':' + os.environ['PATH'])


def execute(env, release=SHA):
    return subprocess.run(['bash', str(ROOT / 'deploy/remote.sh'), release], env=env, text=True, capture_output=True)


def test_successful_release_preserves_runtime(tmp_path):
    base, env = prepare(tmp_path)
    result = execute(env)
    assert result.returncode == 0, result.stderr
    assert (base / 'current').resolve() == base / 'releases' / SHA
    assert (base / 'shared/.env').read_text() == 'SECRET=preserve-me\n'
    assert (base / 'shared/data/radar.sqlite3').read_bytes() == b'preserve database'
    assert not (base / f'{SHA}.tar.gz').exists()


def test_failed_health_rolls_back(tmp_path):
    base, env = prepare(tmp_path, previous=True)
    result = execute(dict(env, FAIL_NEW='1'))
    assert result.returncode == 1
    assert (base / 'current').resolve() == base / 'releases' / OLD
    calls = (tmp_path / 'docker-calls').read_text()
    assert f'{OLD} compose up -d --no-build --wait' in calls
    assert 'Restored previous release' in result.stdout


def test_missing_session_does_not_restart(tmp_path):
    base, env = prepare(tmp_path, session=False)
    result = execute(env)
    assert result.returncode == 1
    assert 'Authenticate first' in result.stderr
    assert 'compose up' not in (tmp_path / 'docker-calls').read_text()


def test_failed_first_release_is_stopped(tmp_path):
    base, env = prepare(tmp_path)
    result = execute(dict(env, FAIL_NEW='1'))
    assert result.returncode == 1
    assert not (base / 'current').exists()
    assert 'compose stop radar' in (tmp_path / 'docker-calls').read_text()


def test_rejects_invalid_release(tmp_path):
    result = execute(dict(os.environ, HOME=str(tmp_path)), release='../../invalid')
    assert result.returncode == 1
    assert 'Invalid commit SHA' in result.stderr
