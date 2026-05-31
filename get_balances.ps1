# Bitcoin Core - 获取有余额的地址
# PowerShell 脚本

$ErrorActionPreference = "Stop"

Write-Host "=" -f Green -n 100
Write-Host "Bitcoin Core - 获取有余额的地址" -f Green
Write-Host "=" -f Green -n 100

$bitcoinCli = "G:\Bitcoin\daemon\bitcoin-cli.exe"
$datadir = "G:\Bitcoin"
$walletName = "plz"

function Run-BitcoinCli {
    param([string[]]$args)
    $cmd = @($bitcoinCli, "-datadir=$datadir")
    if ($walletName) { $cmd += "-rpcwallet=$walletName" }
    $cmd += $args

    $result = & $cmd 2>&1
    if ($LASTEXITCODE -ne 0) {
        return $null, $result
    }
    return $result, $null
}

Write-Host "`n钱包: $walletName" -f Cyan
Write-Host "数据目录: $datadir" -f Cyan

Write-Host "`n📋 测试连接..." -f Yellow
$walletInfo, $err = Run-BitcoinCli @("getwalletinfo")

if ($err) {
    Write-Host "❌ 无法连接到 Bitcoin 守护进程" -f Red
    Write-Host "`n错误信息:" -f Red
    Write-Host $err -f Red
    Write-Host "`n⚠️  请先启动 bitcoind:" -f Yellow
    Write-Host "& '$($bitcoinCli.Replace('bitcoin-cli', 'bitcoind'))' -daemon -datadir='$datadir'" -f Cyan
    exit 1
}

Write-Host "✅ 连接成功!" -f Green

Write-Host "`n📊 钱包信息:" -f Yellow
try {
    $json = $walletInfo | ConvertFrom-Json
    Write-Host "  余额: $($json.balance) BTC" -f Green
    Write-Host "  未确认余额: $($json.unconfirmed_balance) BTC" -f Cyan
    Write-Host "  交易数: $($json.txcount)" -f White
} catch {
    Write-Host $walletInfo
}

Write-Host "`n🔍 获取未花费交易输出..." -f Yellow
$utxos, $err = Run-BitcoinCli @("listunspent", "0", "9999999")

if (-not $utxos) {
    Write-Host "⚠️  没有找到未花费的输出" -f Yellow
    exit 0
}

try {
    $utxoList = $utxos | ConvertFrom-Json
    Write-Host "✅ 找到 $($utxoList.Count) 个未花费的输出" -f Green

    $addressBalances = @{}

    foreach ($utxo in $utxoList) {
        $addr = $utxo.address
        $amount = $utxo.amount

        if (-not $addressBalances.ContainsKey($addr)) {
            $addressBalances[$addr] = 0.0
        }
        $addressBalances[$addr] += $amount
    }

    Write-Host "`n📋 获取钱包中的所有地址..." -f Yellow

    $labels, $_ = Run-BitcoinCli @("listlabels")
    $allAddresses = @()

    try {
        $labelList = $labels | ConvertFrom-Json
        foreach ($label in $labelList) {
            $addrs, $_ = Run-BitcoinCli @("getaddressesbylabel", $label)
            try {
                $addrDict = $addrs | ConvertFrom-Json
                foreach ($addr in $addrDict.PSObject.Properties.Name) {
                    if (-not ($allAddresses -contains $addr)) {
                        $allAddresses += $addr
                    }
                }
            } catch {}
        }
    } catch {}

    foreach ($addr in $addressBalances.Keys) {
        if (-not ($allAddresses -contains $addr)) {
            $allAddresses += $addr
        }
    }

    $addressesWithDetails = @()

    foreach ($addr in $allAddresses) {
        $balance = if ($addressBalances.ContainsKey($addr)) { $addressBalances[$addr] } else { 0.0 }

        $type = "Unknown"
        $addrInfo, $_ = Run-BitcoinCli @("getaddressinfo", $addr)
        try {
            $info = $addrInfo | ConvertFrom-Json
            if ($info.iswitness) { $type = "SegWit" }
            elseif ($info.isscript) { $type = "P2SH" }
            else { $type = "Legacy" }
        } catch {}

        $addressesWithDetails += [PSCustomObject]@{
            Address = $addr
            Type = $type
            Balance = $balance
        }
    }

    $addressesWithDetails = $addressesWithDetails | Sort-Object -Property Balance -Descending

    Write-Host "`n"
    Write-Host "=" -f Green -n 100
    Write-Host "📊 比特币地址余额表（按余额降序排列）" -f Green
    Write-Host "=" -f Green -n 100

    $table = $addressesWithDetails | ForEach-Object {
        [PSCustomObject]@{
            排名 = $addressesWithDetails.IndexOf($_) + 1
            "比特币地址" = $_.Address
            类型 = $_.Type
            "余额 (BTC)" = "{0:F8}" -f $_.Balance
        }
    }

    $table | Format-Table -AutoSize

    $totalBalance = ($addressesWithDetails | Measure-Object -Property Balance -Sum).Sum
    $addressesWithPositive = ($addressesWithDetails | Where-Object { $_.Balance -gt 0 } | Measure-Object).Count

    Write-Host "`n📈 统计:" -f Yellow
    Write-Host "  地址总数: $($addressesWithDetails.Count)" -f White
    Write-Host "  有余额的地址: $addressesWithPositive" -f Green
    Write-Host "  总余额: $totalBalance BTC" -f Cyan

    $outputJson = "G:\Bitcoin\wallet_balances.json"
    $addressesWithDetails | ConvertTo-Json -Depth 10 | Out-File -FilePath $outputJson -Encoding UTF8
    Write-Host "`n💾 JSON 数据已保存到: $outputJson" -f Green

    $outputMd = "G:\Bitcoin\wallet_balances_table.md"
    $mdContent = @"
# 比特币钱包地址余额表

**钱包名称**: $walletName
**数据目录**: $datadir
**生成时间**: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
**排序方式**: 按余额降序

## 地址余额表

| 排名 | 比特币地址 | 类型 | 余额 (BTC) |
|:----:|-----------|:----:|:----------:|
"@

    for ($i = 0; $i -lt $addressesWithDetails.Count; $i++) {
        $addr = $addressesWithDetails[$i]
        $mdContent += "`n| $($i + 1) | `$($addr.Address)`$ | $($addr.Type) | $($addr.Balance:F8) |"
    }

    $mdContent += @"

## 统计信息

- **地址总数**: $($addressesWithDetails.Count) 个
- **有余额的地址**: $addressesWithPositive 个
- **总余额**: $totalBalance BTC
"@

    $mdContent | Out-File -FilePath $outputMd -Encoding UTF8
    Write-Host "📝 Markdown 表格已保存到: $outputMd" -f Green

} catch {
    Write-Host "⚠️  无法解析数据，显示原始输出:" -f Yellow
    Write-Host $utxos
}
