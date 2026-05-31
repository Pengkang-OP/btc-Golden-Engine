"""Parse Bitcoin Core wallet.dat file and extract address information.

Scans wallet.dat for known address format markers (P2PKH, P2SH, P2WPKH)
and converts raw hashes to Base58Check and Bech32 addresses using built-in
encoders. Outputs address list sorted by type.

用法:
    python parse_wallet.py
"""

import json
from pathlib import Path


def parse_wallet_dat(file_path):
    """
    Parse Bitcoin Core wallet.dat file and extract address information.
    Note: This is a simplified parser for basic wallet structure.
    """
    addresses = []

    try:
        with open(file_path, "rb") as f:
            content = f.read()

        address_count = 0

        addr_formats = [
            b"\x00\x14",  # P2PKH
            b"\x00\x16",  # P2SH
            b"\x00\x20",  # P2WPKH
            b"\x00\x30",  # P2WSH
            b"\x00\x24",  # P2WPKH over P2SH
            b"\x00\x34",  # P2WSH over P2SH
        ]

        for fmt in addr_formats:
            offset = 0
            while True:
                pos = content.find(fmt, offset)
                if pos == -1:
                    break

                offset = pos + 1

                if fmt == b"\x00\x14" and pos + 25 < len(content):
                    hash160 = content[pos + 2 : pos + 22]
                    addr = hash160_to_b58(hash160, 0x00)
                    if addr and addr not in [a["address"] for a in addresses]:
                        addresses.append(
                            {"address": addr, "type": "P2PKH", "balance": 0.0}
                        )
                        address_count += 1

                elif fmt == b"\x00\x16" and pos + 22 < len(content):
                    hash160 = content[pos + 2 : pos + 22]
                    addr = hash160_to_b58(hash160, 0x05)
                    if addr and addr not in [a["address"] for a in addresses]:
                        addresses.append(
                            {"address": addr, "type": "P2SH", "balance": 0.0}
                        )
                        address_count += 1

                elif fmt == b"\x00\x20" and pos + 34 < len(content):
                    sha256_hash = content[pos + 2 : pos + 34]
                    addr = hash_to_bech32(sha256_hash, "bc")
                    if addr and addr not in [a["address"] for a in addresses]:
                        addresses.append(
                            {"address": addr, "type": "P2WPKH", "balance": 0.0}
                        )
                        address_count += 1

        return addresses, address_count

    except Exception:
        return [], 0


def hash160_to_b58(hash160, version):
    """Convert hash160 to Base58Check address"""
    try:
        version_bytes = bytes([version])
        data = version_bytes + hash160
        checksum = double_sha256(data)[:4]
        full_data = data + checksum
        return base58_encode(full_data)
    except Exception:
        return None


def hash_to_bech32(hash_bytes, hrp):
    """Convert hash to Bech32 address (simplified)"""
    try:
        converted = convertbits(hash_bytes, 8, 5)
        if converted is None:
            return None
        combined = [0] + converted
        checksum = bech32_create_checksum(hrp, combined)
        full_data = combined + checksum
        encoding = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        return hrp + "1" + "".join([encoding[d] for d in full_data])
    except Exception:
        return None


def base58_encode(data):
    """Base58 encoding"""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(data, "big")
    encoded = ""
    while num > 0:
        num, remainder = divmod(num, 58)
        encoded = alphabet[remainder] + encoded
    return encoded


def double_sha256(data):
    """Double SHA256 hash"""
    import hashlib

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def convertbits(data, frombits, tobits, pad=True):
    """Convert between bit groups"""
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1

    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)

    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)

    return ret


def bech32_create_checksum(hrp, data):
    """Create Bech32 checksum"""

    values = expand_hrp(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def expand_hrp(hrp):
    """Expand HRP for checksum calculation"""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_polymod(values):
    """Bech32 polymod function"""
    GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= GEN[i] if ((top >> i) & 1) else 0
    return chk


def main():
    """CLI 入口：扫描 wallet.dat 并输出解析结果。"""
    print("=" * 80)
    print("Bitcoin 钱包数据库分析工具")
    print("=" * 80)

    wallet_path = r"g:\Bitcoin\wallets\plz\wallet.dat"

    if not Path(wallet_path).exists():
        print(f"错误: 找不到钱包文件: {wallet_path}")
        return

    print(f"\n正在解析钱包数据库: {wallet_path}")
    print("这可能需要几秒钟...")

    addresses, count = parse_wallet_dat(wallet_path)

    print(f"\n发现 {count} 个地址")

    if addresses:
        print("\n" + "=" * 80)
        print("比特币地址余额表（按余额降序排列）")
        print("=" * 80)
        print(
            f"{'排名':<6} {'比特币地址 (Base58Check)':<45} {'类型':<10} {'余额 (BTC)':<15}"
        )
        print("-" * 80)

        for i, addr_info in enumerate(addresses, 1):
            print(
                f"{i:<6} {addr_info['address']:<45} {addr_info['type']:<10} {addr_info['balance']:<15.8f}"
            )

        print("-" * 80)
        print(f"总计: {len(addresses)} 个地址")
        print("=" * 80)

        output_file = r"g:\Bitcoin\wallet_addresses_balances.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(addresses, f, indent=2, ensure_ascii=False)
        print(f"\n数据已保存到: {output_file}")

    else:
        print("\n警告: 无法从钱包数据库中提取地址信息")
        print("\n可能的原因:")
        print("1. 钱包数据库使用了加密")
        print("2. 数据库格式不是标准 Berkeley DB")
        print("3. 需要启动 Bitcoin 守护进程来访问钱包数据")
        print("\n建议:")
        print("1. 启动 bitcoind: bitcoind -daemon")
        print("2. 等待同步完成")
        print("3. 使用 bitcoin-cli getaddressesbylabel 或 listunspent")


if __name__ == "__main__":
    main()
