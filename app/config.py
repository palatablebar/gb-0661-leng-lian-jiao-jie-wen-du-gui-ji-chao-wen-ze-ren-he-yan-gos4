from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / 'sample_data'
RULE_VERSION = 'cold-2026.11.1'
MAX_ROWS = 10000
MAX_BYTES = 2000000
OPERATING_OFFSET = 8          # 业务日按 UTC+08:00 划分
CLOSING_CUTOFF_HOUR = 10      # 次日 10:00 为迟到记录截止
LEDGER_TOLERANCE = '0.00001'
