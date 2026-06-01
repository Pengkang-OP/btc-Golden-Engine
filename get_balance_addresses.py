"""Query Bitcoin Core wallet for addresses with non-zero balances.

Connects to a running bitcoind via bitcoin-cli, fetches wallet info,
unspent outputs, and address labels, then prints a sorted balance table.
Supports fallback to cached data when the daemon is offline.

用法:
    python get_balance_addresses.py
"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

BITCOIN_CLI_PATH = r"G:\Bitcoin\daemon\bitcoin-cli.exe"
BITCOIN_DATADIR = r"G:\Bitcoin"
WALLET_NAME = "plz"


def run_bitcoin_cli(args):
    """Run bitcoin-cli command and return the result."""
    cmd = [BITCOIN_CLI_PATH, "-datadir=" + BITCOIN_DATADIR]
    if WALLET_NAME:
        cmd.extend(["-rpcwallet=" + WALLET_NAME])
    cmd.extend(args)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            return None, result.stderr

        try:
            return json.loads(result.stdout), None
        except Exception:
            return result.stdout, None

    except Exception as e:
        return None, str(e)


def get_wallet_info():
    """Get general wallet information."""
    return run_bitcoin_cli(["getwalletinfo"])


def get_unspent_outputs():
    """Get list of unspent transaction outputs (UTXOs)."""
    return run_bitcoin_cli(["listunspent", "0", "9999999"])


def get_addresses_by_label(label):
    """Get addresses with a specific label."""
    return run_bitcoin_cli(["getaddressesbylabel", label])


def list_labels():
    """List all labels in the wallet."""
    return run_bitcoin_cli(["listlabels"])


def calculate_address_balances(utxos):
    """Calculate balance for each address from UTXOs.
    Returns a dictionary of address -> balance.
    """
    address_balances = {}

    for utxo in utxos:
        address = utxo.get("address", "")
        amount = utxo.get("amount", 0.0)

        if address:
            if address not in address_balances:
                address_balances[address] = 0.0
            address_balances[address] += amount

    return address_balances


def get_address_info(address):
    """Get information about an address."""
    return run_bitcoin_cli(["getaddressinfo", address])


def get_address_balance(address):
    """Get balance for a specific address (requires block filter index)."""
    try:
        return run_bitcoin_cli(["getaddressbalance", address])
    except Exception:
        return None, None


def main():
    """CLI 入口：连接钱包、获取地址余额并输出表格。."""
    print("=" * 100)
    print("Bitcoin Core - 获取有余额的地址")
    print("=" * 100)

    print(f"\n钱包名称: {WALLET_NAME}")
    print(f"数据目录: {BITCOIN_DATADIR}")

    wallet_info, err = get_wallet_info()

    if err:
        print("\n❌ 无法连接到 Bitcoin 守护进程:")
        print(f"   {err}")
        print("\n⚠️  请先启动 bitcoind:")
        print(
            f"   & '{BITCOIN_CLI_PATH.replace('bitcoin-cli', 'bitcoind')}' -daemon -datadir='{BITCOIN_DATADIR}'",
        )

        # 尝试从已有的文件读取地址
        print("\n📋 尝试从已有数据文件读取地址信息...")

        existing_file = Path(r"g:\Bitcoin\wallet_addresses_balances.json")
        if existing_file.exists():
            with open(existing_file, encoding="utf-8") as f:
                addresses = json.load(f)

            print("\n📊 已知地址（余额需要从 Bitcoin Core 查询）:")
            print_address_table(addresses)
        else:
            print("\n未找到现有的地址数据文件。")
        return None

    print("\n✅ 成功连接到 Bitcoin 守护进程!")

    if wallet_info:
        print("\n📋 钱包信息:")
        if isinstance(wallet_info, dict):
            print(f"   - 钱包余额: {wallet_info.get('balance', 0.0):.8f} BTC")
            print(
                f"   - 未确认余额: {wallet_info.get('unconfirmed_balance', 0.0):.8f} BTC",
            )
            print(f"   - 已确认交易数: {wallet_info.get('txcount', 0)}")

    # 获取未花费的交易输出
    print("\n🔍 正在获取未花费交易输出...")
    utxos, err = get_unspent_outputs()

    if err:
        print(f"❌ 获取 UTXOs 失败: {err}")
        return None

    if not utxos:
        print("⚠️  未找到任何未花费的交易输出")
        utxos = []
    else:
        print(f"✅ 找到 {len(utxos)} 个未花费的交易输出")

    # 计算地址余额
    print("\n📊 正在计算地址余额...")
    address_balances = calculate_address_balances(utxos)

    # 获取所有地址
    print("\n📍 正在获取钱包中的所有地址...")
    labels, err = list_labels()
    all_addresses = set()

    if labels and isinstance(labels, list):
        for label in labels:
            addrs, _ = get_addresses_by_label(label)
            if addrs and isinstance(addrs, dict):
                all_addresses.update(addrs.keys())

    # 添加从 UTXOs 中找到的地址
    all_addresses.update(address_balances.keys())

    # 准备完整的地址列表（包括余额为 0 的）
    addresses_with_details = []

    for address in all_addresses:
        balance = address_balances.get(address, 0.0)
        addr_info, _ = get_address_info(address)

        addr_type = "Unknown"
        if addr_info and isinstance(addr_info, dict):
            if addr_info.get("iswitness", False):
                addr_type = "SegWit"
            elif addr_info.get("isscript", False):
                addr_type = "P2SH"
            else:
                addr_type = "Legacy"

        addresses_with_details.append(
            {"address": address, "type": addr_type, "balance": balance},
        )

    # 按余额降序排序
    addresses_with_details.sort(key=lambda x: x["balance"], reverse=True)

    # 打印表格
    print("\n" + "=" * 100)
    print("📊 比特币地址余额表（按余额降序排列）")
    print("=" * 100)
    print_address_table(addresses_with_details)

    # 保存到文件
    output_file = Path(r"g:\Bitcoin\wallet_addresses_with_balances.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(addresses_with_details, f, indent=2, ensure_ascii=False)

    print(f"\n💾 数据已保存到: {output_file}")

    # 保存 Markdown 表格
    md_file = Path(r"g:\Bitcoin\wallet_addresses_with_balances_table.md")
    with open(md_file, "w", encoding="utf-8") as f:
        f.write(generate_markdown_table(addresses_with_details))

    print(f"📝 Markdown 表格已保存到: {md_file}")

    return addresses_with_details


def print_address_table(addresses) -> None:
    """Print address table in formatted ASCII."""
    print(
        f"{'排名':<6} {'比特币地址 (Base58Check)':<45} {'类型':<10} {'余额 (BTC)':<18}",
    )
    print("-" * 100)

    for i, addr_info in enumerate(addresses, 1):
        balance_str = f"{addr_info['balance']:.8f}"
        balance_display = (
            f"{balance_str}" if addr_info["balance"] > 0 else f"{balance_str}"
        )
        print(
            f"{i:<6} {addr_info['address']:<45} {addr_info['type']:<10} {balance_display:<18}",
        )

    print("-" * 100)

    total_balance = sum(a["balance"] for a in addresses)
    addresses_with_balance = sum(1 for a in addresses if a["balance"] > 0)

    print("\n📈 统计:")
    print(f"   - 地址总数: {len(addresses)}")
    print(f"   - 有余额的地址: {addresses_with_balance}")
    print(f"   - 总余额: {total_balance:.8f} BTC")


def generate_markdown_table(addresses):
    """Generate Markdown formatted table."""
    md = "# 比特币钱包地址余额表\n\n"
    md += f"**钱包名称**: {WALLET_NAME}\n"
    md += f"**数据目录**: {BITCOIN_DATADIR}\n"
    md += f"**生成时间**: {datetime.now():%Y-%m-%d}\n"
    md += "**排序方式**: 按余额降序\n\n"

    md += "## 地址余额表\n\n"
    md += "| 排名 | 比特币地址 (Base58Check/Bech32) | 类型 | 余额 (BTC) |\n"
    md += "|:----:|-----------------------------------|:----:|:----------:|\n"

    for i, addr_info in enumerate(addresses, 1):
        md += f"| {i} | `{addr_info['address']}` | {addr_info['type']} | {addr_info['balance']:.8f} |\n"

    total_balance = sum(a["balance"] for a in addresses)
    addresses_with_balance = sum(1 for a in addresses if a["balance"] > 0)

    md += "\n## 统计信息\n\n"
    md += f"- **地址总数**: {len(addresses)} 个\n"
    md += f"- **有余额的地址**: {addresses_with_balance} 个\n"
    md += f"- **总余额**: {total_balance:.8f} BTC\n"

    return md


if __name__ == "__main__":
    main()
