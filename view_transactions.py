"""View Bitcoin Core wallet transactions with summary statistics.

Connects to bitcoind via bitcoin-cli, fetches all transaction history,
displays a formatted table sorted by time, and saves JSON + Markdown reports.

用法:
    python view_transactions.py
"""

import datetime
import json
import subprocess
from pathlib import Path

BITCOIN_CLI = r"G:\Bitcoin\daemon\bitcoin-cli.exe"
DATADIR = r"G:\Bitcoin"
WALLET = "plz"


def run_cli(args: list[str]) -> tuple[object | str | None, str | None]:
    """Run bitcoin-cli command and return (result, error)."""
    cmd = [BITCOIN_CLI, f"-datadir={DATADIR}"]
    if WALLET:
        cmd.append(f"-rpcwallet={WALLET}")
    cmd.extend(args)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode == 0:
            try:
                return json.loads(result.stdout), None
            except Exception:
                return result.stdout, None
        return None, result.stderr
    except Exception as e:
        return None, str(e)


def format_timestamp(ts: int) -> str:
    """Convert Unix timestamp to human-readable datetime string."""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)


def main() -> None:
    """CLI 入口：获取并显示钱包交易列表与统计摘要。."""
    print("=" * 100)
    print("Bitcoin 交易数据查看工具")
    print("=" * 100)

    print(f"\n钱包: {WALLET}")
    print(f"数据目录: {DATADIR}")

    print("\n📋 测试连接...")
    wallet_info, err = run_cli(["getwalletinfo"])

    if err:
        print("❌ 无法连接到 Bitcoin 守护进程")
        print(f"\n错误: {err}")
        print("\n⚠️  请先启动 bitcoind:")
        print(
            f"& '{BITCOIN_CLI.replace('bitcoin-cli', 'bitcoind')}' -daemon -datadir='{DATADIR}'",
        )
        return

    print("✅ 连接成功!")

    if isinstance(wallet_info, dict):
        print("\n📊 钱包信息:")
        print(f"   - 余额: {wallet_info.get('balance', 0.0):.8f} BTC")
        print(f"   - 交易数: {wallet_info.get('txcount', 0)}")

    print("\n🔍 获取交易列表...")
    transactions, err = run_cli(["listtransactions", "*", "9999"])

    if err:
        print(f"❌ 获取交易列表失败: {err}")
        return

    if not transactions or (isinstance(transactions, list) and len(transactions) == 0):
        print("⚠️  未找到任何交易记录")
        return

    assert isinstance(transactions, list)
    print(f"✅ 找到 {len(transactions)} 笔交易\n")

    print("=" * 100)
    print("📊 交易列表（按时间降序）")
    print("=" * 100)
    print(f"{'序号':<6} {'时间':<20} {'类型':<12} {'金额 (BTC)':<18} {'交易ID':<64}")
    print("-" * 100)

    tx_list = transactions if isinstance(transactions, list) else []

    for i, tx in enumerate(tx_list, 1):
        time_str = format_timestamp(tx.get("time", 0))
        tx_type = tx.get("type", "unknown")
        amount = tx.get("amount", 0.0)
        txid = tx.get("txid", "N/A")

        amount_str = f"{amount:+.8f}"
        txid_short = txid[:64] if len(txid) > 64 else txid

        print(f"{i:<6} {time_str:<20} {tx_type:<12} {amount_str:>15}  {txid_short}")

    print("-" * 100)

    total_sent = sum(abs(tx.get("amount", 0)) for tx in tx_list if tx.get("amount", 0) < 0)
    total_received = sum(tx.get("amount", 0) for tx in tx_list if tx.get("amount", 0) > 0)
    tx_count = len(tx_list)

    print("\n📈 统计摘要:")
    print(f"   - 交易总数: {tx_count} 笔")
    print(f"   - 总发送: {total_sent:.8f} BTC")
    print(f"   - 总接收: {total_received:.8f} BTC")
    print(f"   - 净变化: {total_received - total_sent:.8f} BTC")

    # 按类型统计
    tx_types = {}
    for tx in tx_list:
        tx_type = tx.get("type", "unknown")
        if tx_type not in tx_types:
            tx_types[tx_type] = {"count": 0, "total": 0.0}
        tx_types[tx_type]["count"] += 1
        tx_types[tx_type]["total"] += abs(tx.get("amount", 0))

    print("\n📊 按类型统计:")
    for tx_type, stats in sorted(tx_types.items()):
        print(f"   - {tx_type}: {stats['count']} 笔, 金额: {stats['total']:.8f} BTC")

    # 保存数据
    output_json = Path(r"g:\Bitcoin\wallet_transactions.json")
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(tx_list, f, indent=2, ensure_ascii=False)

    print(f"\n💾 JSON 数据已保存到: {output_json}")

    # 生成 Markdown 报告
    md_content = f"""# Bitcoin 钱包交易记录

**钱包**: {WALLET}
**数据目录**: {DATADIR}
**生成时间**: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

## 统计摘要

- **交易总数**: {tx_count} 笔
- **总发送**: {total_sent:.8f} BTC
- **总接收**: {total_received:.8f} BTC
- **净变化**: {total_received - total_sent:.8f} BTC

## 交易列表

| 序号 | 时间 | 类型 | 金额 (BTC) | 交易ID |
|:----:|------|:----:|:----------:|--------|
"""

    for i, tx in enumerate(tx_list, 1):
        time_str = format_timestamp(tx.get("time", 0))
        tx_type = tx.get("type", "unknown")
        amount = tx.get("amount", 0.0)
        txid = tx.get("txid", "N/A")

        md_content += f"| {i} | {time_str} | {tx_type} | {amount:+.8f} | `{txid}` |\n"

    md_content += "\n## 按类型统计\n\n"

    for tx_type, stats in sorted(tx_types.items()):
        md_content += f"- **{tx_type}**: {stats['count']} 笔, 总计 {stats['total']:.8f} BTC\n"

    output_md = Path(r"g:\Bitcoin\wallet_transactions_report.md")
    with open(output_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"📝 Markdown 报告已保存到: {output_md}")

    # 显示最新 5 笔交易详情
    if tx_list:
        print("\n" + "=" * 100)
        print("📋 最新 5 笔交易详情")
        print("=" * 100)

        recent_txs = sorted(tx_list, key=lambda x: x.get("time", 0), reverse=True)[:5]

        for i, tx in enumerate(recent_txs, 1):
            print(f"\n【交易 {i}】")
            print(f"  时间: {format_timestamp(tx.get('time', 0))}")
            print(f"  类型: {tx.get('type', 'unknown')}")
            print(f"  金额: {tx.get('amount', 0.0):+.8f} BTC")
            print(f"  交易ID: {tx.get('txid', 'N/A')}")
            print(f"  确认数: {tx.get('confirmations', 0)}")

            if "address" in tx:
                print(f"  地址: {tx['address']}")

            if "fee" in tx:
                print(f"  手续费: {tx['fee']:.8f} BTC")


if __name__ == "__main__":
    main()
