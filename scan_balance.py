"""Scan UTXO set for specific Bitcoin addresses and report balances.

Uses bitcoin-cli scantxoutset RPC to check a predefined list of addresses
against the current UTXO set, displaying total balance for each.

用法:
    python scan_balance.py
"""

import subprocess
import json

ADDRESSES = [
    "bc1q8kjxlkffnrpja09g5z3sj5pmrqtaz0f6cdx7lh",
    "bc1qwhpzxcsqduvtekegzy7t4gpm49wcdz74ywth0z",
    "bc1q3cfkxu772k0r2jgzk2vkzy22r04letevaegm9y",
    "bc1qh6843llhjwc53rq80p5y764nht90zr466nme0n",
    "bc1qms4ng7v5et269wh2jdh3e9t4hxsmr2hmryd5et",
    "16cwCyjw3bdSweEajmQkzGYG2rz8vjhGQB",
]

BITCOIN_CLI = r"G:\Bitcoin\daemon\bitcoin-cli.exe"
DATADIR = r"G:\Bitcoin"


def run_cli(args):
    """Run bitcoin-cli with given args and parse JSON output."""
    cmd = [BITCOIN_CLI, f"-datadir={DATADIR}"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except Exception:
            return result.stdout
    else:
        return {"error": result.stderr}


print("=" * 80)
print("Bitcoin 地址余额扫描工具")
print("=" * 80)

print("\n正在扫描 UTXO 集...\n")

descriptors = [f"addr({addr})" for addr in ADDRESSES]

cmd_args = ["scantxoutset", "start", json.dumps(descriptors)]
result = run_cli(cmd_args)

if "error" in result:
    print(f"错误: {result['error']}")
else:
    print("扫描结果:\n")
    print(json.dumps(result, indent=2))

    if "total_amount" in result:
        print(f"\n总余额: {result['total_amount']} BTC")
        print(f"成功数: {result.get('success', 0)}")
