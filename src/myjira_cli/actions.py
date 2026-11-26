"""A small durable action receipt; uncertainty is never an automatic retry."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .common import CliError, digest, file_lock, result, write_json_atomic


def load_action(state_dir: str, operation_id: str):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 256:
        raise CliError('INPUT', 'Invalid operation ID.')
    directory = Path(state_dir).expanduser().resolve()
    key = digest(operation_id)
    with file_lock(str(directory / (key + '.lock'))):
        path = directory / (key + '.json')
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise CliError('CONFLICT', 'Cannot read action receipt.')
        if not isinstance(record, dict) or record.get('operation_id') != operation_id:
            raise CliError('CONFLICT', 'Action receipt identity is invalid.')
        return record


def run_action(state_dir: str, operation_id: str, payload: dict,
               execute: Callable[[], dict], reconcile: Callable[[], dict]) -> dict:
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 256:
        raise CliError('INPUT', 'Operation ID must be a non-empty string of at most 256 characters.')
    directory = Path(state_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = digest(operation_id)
    receipt = directory / (key + '.json')
    with file_lock(str(directory / (key + '.lock'))):
        payload_hash = digest(payload)
        prior = None
        if receipt.exists():
            try:
                prior = json.loads(receipt.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise CliError('CONFLICT', 'Action receipt is unreadable; inspect it before retrying.')
            if prior.get('payload_hash') != payload_hash:
                raise CliError('CONFLICT', 'Operation ID was already used for different inputs.')
            if prior.get('status') in ('CONFIRMED', 'FAILED'):
                return prior['result']
        record = {'schema_version': 1, 'operation_id': operation_id, 'payload_hash': payload_hash,
                  'status': 'PENDING', 'updated_at': datetime.now(timezone.utc).isoformat()}
        if 'recovery' in payload:
            record['recovery'] = payload['recovery']
        write_json_atomic(str(receipt), record)
        try:
            output = reconcile() if prior else execute()
            if not isinstance(output, dict) or output.get('status') not in ('SUCCESS', 'FAILED', 'UNKNOWN'):
                raise CliError('RESULT_UNKNOWN', 'Action callback returned no verifiable result.')
        except CliError as exc:
            # Reconciliation failure cannot disprove an already attempted write.
            uncertain = bool(prior) or exc.code in ('RESULT_UNKNOWN', 'LOCAL_ERROR')
            output = result('UNKNOWN' if uncertain else 'FAILED', error=exc.as_dict())
        except (Exception, KeyboardInterrupt):
            output = result('UNKNOWN', error={'code': 'RESULT_UNKNOWN', 'message': 'Execution interrupted; reconcile before retrying.'})
        output.update(operation_id=operation_id, receipt_path=str(receipt))
        record.update(status={'SUCCESS': 'CONFIRMED', 'FAILED': 'FAILED', 'UNKNOWN': 'UNKNOWN'}[output['status']],
                      result=output, updated_at=datetime.now(timezone.utc).isoformat())
        write_json_atomic(str(receipt), record)
        return output
