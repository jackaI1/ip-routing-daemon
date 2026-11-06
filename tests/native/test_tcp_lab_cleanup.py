"""Failure injection here must never create real namespaces or processes."""
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

import pytest

SPEC = importlib.util.spec_from_file_location('tcp_lab', Path(__file__).resolve().parents[2] / 'lab/netns.py')
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


def test_namespace_delete_error_does_not_skip_other_owned_resources(tmp_path):
    deleted = []

    def command(*args, **kwargs):
        if args[:3] == ('ip', 'netns', 'del'):
            deleted.append(args[3])
            if args[3] == 'owned-4':
                raise subprocess.CalledProcessError(1, args, stderr='injected failure')
        return subprocess.CompletedProcess(args, 0, 'owned-4\n' if args == ('ip', 'netns', 'list') else '', '')

    with patch.object(lab, 'run', side_effect=command):
        with pytest.raises(RuntimeError, match='cleanup'):
            lab.cleanup_resources([], [], ['owned-1', 'owned-2', 'owned-3', 'owned-4'], [], tmp_path, 'owned')
    assert deleted == ['owned-4', 'owned-3', 'owned-2', 'owned-1']
    report = json.loads((tmp_path / 'cleanup.json').read_text())
    assert report['remaining_namespaces'] == ['owned-4']
    assert report['remaining_owned'] == 1
    assert len(report['errors']) == 1


def test_process_signal_error_does_not_skip_wait_or_namespace_cleanup(tmp_path):
    process = Mock(pid=123)
    process.poll.side_effect = [None, 0]
    process.send_signal.side_effect = ProcessLookupError('injected race')
    with patch.object(lab, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as command:
        with pytest.raises(RuntimeError, match='cleanup'):
            lab.cleanup_resources([process], [], ['owned-1'], [], tmp_path, 'owned')
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=3)
    command.assert_any_call('ip', 'netns', 'del', 'owned-1')
    assert json.loads((tmp_path / 'cleanup.json').read_text())['remaining_owned'] == 0


def test_failed_link_deletion_still_measures_surviving_owned_link(tmp_path):
    def command(*args, **kwargs):
        if args[:3] == ('ip', 'link', 'del'):
            raise subprocess.CalledProcessError(1, args, stderr='injected failure')
        return subprocess.CompletedProcess(args, 0, '', '')

    with patch.object(lab, 'run', side_effect=command), patch.object(lab.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
        with pytest.raises(RuntimeError, match='cleanup'):
            lab.cleanup_resources([], [], [], ['owned-veth'], tmp_path, 'owned')
    report = json.loads((tmp_path / 'cleanup.json').read_text())
    assert report['remaining_links'] == ['owned-veth']
    assert report['remaining_owned'] == 1
