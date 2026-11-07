"""Read-only exporter smoke test, intentionally prints no private chat contents."""
import json
from pathlib import Path

from admin_agent import runtime_state
from admin_export import export

report, assets = export(Path(__file__).resolve().parent, runtime_state())
print(json.dumps({
    'runtime': {key: report['runtime'].get(key) for key in ('worker_state', 'trial_state', 'old_gateway_state', 'run_id')},
    'scans': [{key: item.get(key) for key in ('id', 'scope', 'status', 'expected', 'observed', 'position_count', 'coverage_valid')}
              for item in report['scans']],
    'contacts': len(report['contacts']), 'summary': report['summary'],
    'asset_count': len(assets), 'asset_bytes': sum(len(value) for value in assets.values()),
    'telemetry_bytes': len(json.dumps(report, ensure_ascii=True).encode()),
}, ensure_ascii=True))
